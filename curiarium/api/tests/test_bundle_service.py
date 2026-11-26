"""Bundle detection and creation against a real temp storage root (Mf-vwqf).

Every test builds a real directory tree under a temporary storage root, runs
``library_scan.scan_storage_root()`` so the assets and folder rows are registered
exactly as the live box does, and then asks the API or the service what it sees.
There is no shared conftest in this suite, so the harness below is deliberately a
copy of ``tests/test_projects_api.py``'s: ``isolated_storage`` points every module
that binds a path at import time (``db``, ``main``, ``library_scan``,
``asset_service``, ``asset_file_service`` and ``bundle_service``) at the temp root,
runs the migrations, and deletes the rows the test created on teardown. The HTTP
tests drive the real ``/bundles`` routes through the app; the service-level tests
below prove the same core contract without the request layer.
"""

import ast
import hashlib
import inspect
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import event
from sqlmodel import Session, select

import archive_project_service
import asset_file_service
import asset_service
import audit
import bundle_drift
import bundle_enrich
import bundle_service
import db
import file_utils
import import_resolvers
import library_scan
import main
import project_create_service
import project_service
import settings_service
import zip_service
from db import engine
from migrations import run_migrations
from models import Asset, AssetFile, AuditEvent, Folder, Project, ProjectAsset

MOAI = "2493386_Moai_statue_-No_overhang"
THING_URL = "https://www.thingiverse.com/thing:2493386"
# The shape the ticket is about: a Thingiverse export with models and no preview.
GEAR_BEARING = "Gear Bearing"
README_TEXT = "Moai statue -No overhang\nCreated by Thingiverse user moai_maker\nLicensed under CC BY-NC\n"
LICENSE_TEXT = f"Moai statue -No overhang by moai_maker on Thingiverse: {THING_URL}\n"
STL_BYTES = b"solid cube\nendsolid cube\n"

# The two real Thingiverse exports from the ticket. A real export sometimes ships
# the created-by sentence in README.txt and the title line in LICENSE.txt, the
# other way round from the Moai fixture above.
CAT_NAME = "2-color_standing_cat_62536"
CAT_CREATED_BY = "This thing was created by Thingiverse user nervoussystem, and is licensed under cc-nc-sa.\n"
CAT_TITLE_LINE = "2-color standing cat by nervoussystem on Thingiverse: https://www.thingiverse.com/thing:62536\n"
CAT_URL = "https://www.thingiverse.com/thing:62536"

# The id-named export from the ticket: the directory name itself is the
# provenance, and the derived URL is the only thing it is allowed to state.
DRAGON = "5390184_ARTICULATED_ice_dragon__fixed_"
DRAGON_URL = "https://www.thingiverse.com/thing:5390184"

# The Moai previews, in the order they are written. The fourth is the largest, so
# it is the cover the report must pick.
IMAGE_SIZES = (100, 400, 200, 900, 300)


# --- infrastructure ---------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every path-bound module at a temp root and undo the rows a test made."""
    storage = tmp_path / "storage"
    (storage / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(db, "STORAGE", storage)
    monkeypatch.setattr(db, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(main, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(library_scan, "STORAGE", storage)
    monkeypatch.setattr(asset_service, "STORAGE", storage)
    monkeypatch.setattr(asset_service, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(asset_file_service, "STORAGE", storage)
    monkeypatch.setattr(bundle_service, "STORAGE", storage)
    run_migrations(engine)
    template_before = settings_service.get_storage_template()
    with Session(engine) as session:
        projects_before = {project.id for project in session.exec(select(Project))}
        folders_before = {folder.id for folder in session.exec(select(Folder))}
        assets_before = {asset.id for asset in session.exec(select(Asset))}
    yield storage
    settings_service.set_storage_template(template_before)
    settings_service.set_setting(settings_service.BUNDLE_DISMISSALS_KEY, "")
    settings_service.set_setting(settings_service.BUNDLE_ENRICHMENTS_KEY, "")
    with Session(engine) as session:
        # Projects first: `idx_project_name_unique` is case-insensitive, so a
        # leaked row would 409 the next test's project.
        for membership in list(session.exec(select(ProjectAsset))):
            if membership.project_id not in projects_before:
                session.delete(membership)
        for project in list(session.exec(select(Project))):
            if project.id not in projects_before:
                session.delete(project)
        for asset_file in list(session.exec(select(AssetFile))):
            if asset_file.asset_id not in assets_before:
                session.delete(asset_file)
        for asset in list(session.exec(select(Asset))):
            if asset.id not in assets_before:
                session.delete(asset)
        for folder in list(session.exec(select(Folder))):
            if folder.id not in folders_before:
                session.delete(folder)
        session.commit()


@pytest.fixture
def client() -> TestClient:
    with TestClient(main.app) as test_client:
        yield test_client


def auth_header(client: TestClient) -> dict:
    """Bearer token, or empty when the app is running with auth disabled."""
    health = client.get("/health").json()
    if not health.get("auth_required"):
        return {}
    token = client.post("/login", json={"username": "admin", "password": "super-secret"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def filesystem_snapshot(root: Path) -> tuple:
    """Paths, kinds and sizes of the whole tree; enough to prove nothing moved."""
    return tuple(
        sorted(
            (str(path.relative_to(root)) or ".", path.is_dir(), path.stat().st_size)
            for path in [root, *root.rglob("*")]
        )
    )


def table_counts() -> Dict[str, int]:
    with Session(engine) as session:
        return {
            "projects": len(session.exec(select(Project)).all()),
            "assets": len(session.exec(select(Asset)).all()),
            "folders": len(session.exec(select(Folder)).all()),
            "asset_files": len(session.exec(select(AssetFile)).all()),
            "audit_events": len(session.exec(select(AuditEvent)).all()),
        }


def audit_ctx() -> audit.AuditContext:
    return audit.AuditContext(
        actor=audit.Actor(username="admin", role="admin", kind="session"),
        request_id="test-request",
        method="POST",
        path="/bundles/create",
    )


# --- measurement artifacts (Mf-wsdo) ----------------------------------------
#
# AGENTS.md: "record the data, not just the count". `bundle_serialize.py --diff`
# serialises REPORTS, so it cannot attribute a MEMBERSHIP to a build - which is
# exactly the thing Mf-wsdo moves. These helpers therefore dump one entry per
# MEMBER of the created project (never a bare count), plus the whole report row,
# plus the build identity and the library state, to the path `MV_MEMBER_ARTIFACT`
# names. Unset - which is every ordinary `pytest -q` - nothing is written at all,
# so the suite keeps touching nothing but its temp root.

MEMBER_ARTIFACT_ENV = "MV_MEMBER_ARTIFACT"
# The row keys the artifact records verbatim: what the report SAID, kept beside
# what the create ATTACHED, because they are different questions and an artifact
# that can only print one of them cannot show a ticket that separates them.
ARTIFACT_ROW_KEYS = (
    "status",
    "blocked_reason",
    "detector",
    "confidence",
    "model_count",
    "image_count",
    "text_count",
    "other_count",
    "assets_total",
    "groups",
    "cover",
    "existing_project_id",
    "existing_project_match",
    "bundle_drift_state",
    "bundle_drift_count",
)


def _file_sha256(path: Path) -> str:
    """The hash of one source file, so an artifact can be pinned to a build."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_head() -> str:
    """``git rev-parse HEAD``, or the word ``unavailable`` - never an exception.

    The API image has no ``.git`` and no ``git`` binary, and a measurement that
    crashed the suite for wanting a build id would be a measurement nobody ran.
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    return completed.stdout.strip() or "unavailable"


def _library_state(storage: Path) -> Dict[str, int]:
    """The rows and the files the artifact was read against.

    Counts only, and no absolute path: the temp root's name changes every run, and
    an artifact pair that differs because pytest numbered its directories is not a
    pair anyone can diff.
    """
    files = [path for path in storage.rglob("*") if path.is_file()]
    return {
        **table_counts(),
        "files_on_disk": len(files),
        "archives_on_disk": sum(1 for path in files if path.suffix.lower() == ".zip"),
    }


def _member_artifact(
    *,
    door: str,
    key: str,
    storage: Path,
    project_id: Optional[str],
    row: Optional[Mapping],
    result: Optional[Mapping],
) -> None:
    """Write the member-set measurement artifact, when this run asked for one.

    Everything is read off the database and the disk AFTER the create, in the same
    shape the witnesses assert on: ``members`` is every member's ``storage_path`` with
    its suffix AND its ROLE (``library_scan.classify_entry`` over the bundle-relative
    path, the one classifier), ``counts`` carries the per-role tally beside the
    registered/member totals, ``cover_storage_path`` names the project's own cover
    file (an asset id cannot be read by a human diffing two artifacts), and
    ``cover_is_member`` is the boolean Mf-wsdo turned from ``False`` to ``True``. The
    ``build`` block is the code identity, so the BEFORE and the AFTER cannot be
    confused for one another.

    ``excluded`` is kept as a key and is ALWAYS empty since Mf-7oow: a bundle project
    holds every registered file under its key, so "registered but not a member" is a
    state the create can no longer produce. It is written out anyway so an older
    artifact and a newer one diff into the same shape rather than into a missing key.
    """
    destination = os.environ.get(MEMBER_ARTIFACT_ENV, "").strip()
    if not destination:
        return
    with Session(engine) as session:
        registered_rows = {
            asset.path: asset for asset in bundle_service.registered_assets(session, key)
        }
        registered = set(registered_rows)
        member_ids: set = set()
        cover_asset_id: Optional[str] = None
        cover_storage_path: Optional[str] = None
        member_paths: set = set()
        if project_id is not None:
            member_ids = {
                membership.asset_id
                for membership in session.exec(
                    select(ProjectAsset).where(ProjectAsset.project_id == project_id)
                )
            }
            if member_ids:
                member_paths = {
                    asset.storage_path
                    for asset in session.exec(select(Asset).where(Asset.id.in_(member_ids)))
                }
            project = session.get(Project, project_id)
            cover_asset_id = project.cover_asset_id if project is not None else None
            if cover_asset_id is not None:
                cover = session.get(Asset, cover_asset_id)
                cover_storage_path = cover.storage_path if cover is not None else None
    entries = sorted(registered | member_paths)

    def role_of(path: str) -> str:
        """The one classifier's answer for one member, over its BUNDLE-relative path."""
        asset = registered_rows.get(path)
        return library_scan.classify_entry(path[len(key) + 1 :], asset.size if asset is not None else 0)

    role_tally = {
        role: sum(1 for path in member_paths if role_of(path) == role)
        for role in (
            library_scan.MODEL,
            library_scan.IMAGE,
            library_scan.TEXT,
            library_scan.OTHER,
            library_scan.ARCHIVE,
        )
    }
    payload = {
        "artifact": "bundle-members",
        "ticket": "Mf-wsdo",
        "door": door,
        "key": key,
        "command": " ".join([Path(sys.executable).name, *sys.argv]),
        "env": {MEMBER_ARTIFACT_ENV: destination},
        "build": {
            "bundle_service.py": _file_sha256(Path(bundle_service.__file__)),
            "project_create_service.py": _file_sha256(Path(project_create_service.__file__)),
            "git_head": _git_head(),
        },
        "library_state": _library_state(storage),
        "row": {name: (row or {}).get(name) for name in ARTIFACT_ROW_KEYS},
        "members": [
            {
                "storage_path": path,
                "suffix": bundle_service._suffix(path),
                "role": role_of(path),
                "member": True,
            }
            for path in entries
            if path in member_paths
        ],
        "excluded": [
            {"storage_path": path, "suffix": bundle_service._suffix(path), "member": False}
            for path in entries
            if path not in member_paths
        ],
        "counts": {
            "registered": len(registered),
            "members": len(member_paths),
            "excluded": 0,
            "roles": role_tally,
        },
        "result": dict(result) if result is not None else None,
        "cover_asset_id": cover_asset_id,
        "cover_storage_path": cover_storage_path,
        "cover_is_member": cover_asset_id is not None and cover_asset_id in member_ids,
    }
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# --- fixtures on disk -------------------------------------------------------


def write_file(path: Path, payload: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_zip(path: Path, entries: Mapping[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)


def moai_bundle(root: Path, name: str = MOAI) -> Path:
    """The Thingiverse README/LICENSE bundle: 5 previews, 3 models, no zip."""
    bundle = root / name
    write_text(bundle / "README.txt", README_TEXT)
    write_text(bundle / "LICENSE.txt", LICENSE_TEXT)
    for index, size in enumerate(IMAGE_SIZES):
        write_file(bundle / "images" / f"{index}.jpg", b"j" * size)
    for index in range(3):
        write_file(bundle / "files" / f"part{index}.stl", b"s" * 40)
    return bundle


def generic_bundle(root: Path, name: str) -> Path:
    """A low-confidence bundle: one model, one image, one text and nothing else."""
    bundle = root / name
    write_file(bundle / "part.stl")
    write_file(bundle / "cover.jpg", b"j" * 50)
    write_text(bundle / "notes.txt", "print at 0.2mm")
    return bundle


COLLECTION_KEY = "import/Collection"
AGREEING_KEY = "import/Agreeing"


def collection_bundle(root: Path) -> Path:
    """Three products in one folder: each top folder ships its own README and models.

    The detector reads the listing as one `generic` bundle (a model at its own depth
    plus an image and a text anywhere). The engine reads it as `split-top-folders`,
    20 + 30 + 10 = 60, against a `one-project` of 40 - 30 - 20 = -10: each child's own
    document is a claim to be a product, so the split is the ENGINE's recommendation
    while the app's row recommends something else.
    """
    bundle = root / "import" / "Collection"
    write_file(bundle / "loose.stl", STL_BYTES)
    write_file(bundle / "cover.jpg", b"j" * 100)
    write_text(bundle / "notes.txt", "print at 0.2mm\n")
    for name in ("Widget One", "Widget Two", "Widget Three"):
        write_text(bundle / name / "README.md", f"# {name}\n")
        for index in range(3):
            write_file(bundle / name / f"part{index}.stl", STL_BYTES)
    return bundle


def agreeing_bundle(root: Path) -> Path:
    """One product in one folder: a `generic` row the engine never dissents on.

    ONE model-bearing child and no product document anywhere, so the engine offers
    `one-project` and no split at all (a split needs two top folders carrying
    models).
    """
    bundle = root / "import" / "Agreeing"
    for index in range(3):
        write_file(bundle / "files" / f"part{index}.stl", STL_BYTES)
    write_file(bundle / "cover.jpg", b"j" * 100)
    write_text(bundle / "notes.txt", "print at 0.2mm\n")
    return bundle


def thingiverse_bundle(root: Path, name: str, thing: int) -> Path:
    """A high-confidence bundle whose own documents name its own Thingiverse thing.

    The thing id is a parameter because the create pass treats the same title and
    the same source URL as one project (that is its idempotency contract), so a
    fixture with two bundles needs two sets of documents.
    """
    bundle = root / name
    title = library_scan.display_name(name)
    write_text(bundle / "README.txt", f"{title}\nCreated by Thingiverse user maker_{thing}\nLicensed under CC BY-NC\n")
    write_text(
        bundle / "LICENSE.txt",
        f"{title} by maker_{thing} on Thingiverse: https://www.thingiverse.com/thing:{thing}\n",
    )
    write_file(bundle / "files" / "part.stl", STL_BYTES)
    return bundle


def gear_bearing_bundle(root: Path, name: str = GEAR_BEARING, thing: int = 333333) -> Path:
    """The Gear-Bearing shape: a README pair, two renderable models, no image.

    ``gear.stl`` is larger than ``pinion.stl`` so "the largest renderable model"
    has one answer regardless of path order, and ``bearing.scad`` is a model the
    client renderer has no loader for, so it must never be the cover.
    """
    bundle = root / name
    title = library_scan.display_name(name)
    write_text(bundle / "README.txt", f"{title}\nCreated by Thingiverse user maker_{thing}\nLicensed under CC BY-NC\n")
    write_text(
        bundle / "LICENSE.txt",
        f"{title} by maker_{thing} on Thingiverse: https://www.thingiverse.com/thing:{thing}\n",
    )
    write_file(bundle / "files" / "bearing.scad", b"s" * 10)
    write_file(bundle / "files" / "gear.stl", b"g" * 900)
    write_file(bundle / "files" / "pinion.stl", b"p" * 400)
    return bundle


def png_bytes() -> bytes:
    """A real 2x2 PNG: the thumbnail writer validates the image, not just the type."""
    buffer = BytesIO()
    Image.new("RGB", (2, 2), (30, 120, 200)).save(buffer, "PNG")
    return buffer.getvalue()


def asset_id_at(storage_path: str) -> str:
    """The registered asset id for one storage path, read from the DB, never guessed."""
    with Session(engine) as session:
        asset = session.exec(select(Asset).where(Asset.storage_path == storage_path)).one()
        return asset.id


def legacy_bundle(root: Path, name: str) -> Path:
    """The flattened shape: the sibling documents are directories, not files."""
    bundle = root / name
    write_text(bundle / f"{name}_README" / "readme.txt", README_TEXT)
    write_text(bundle / f"{name}_LICENSE" / "license.txt", LICENSE_TEXT)
    write_file(bundle / "part.stl")
    write_file(bundle / "preview.jpg", b"j" * 20)
    return bundle


def insert_folder_row(name: str, storage_path: Optional[str] = None) -> str:
    """A folder row the way the flattened v3 migration left it: no path."""
    folder = Folder(name=name, tags_json="[]", parent_id=None, storage_path=storage_path)
    with Session(engine) as session:
        session.add(folder)
        session.commit()
        session.refresh(folder)
        return folder.id


def scan() -> None:
    state = library_scan.scan_storage_root()
    assert state.failed == 0, state.errors


def row_for(report: Mapping, key: str) -> dict:
    matches = [row for row in report["bundles"] if row["key"] == key]
    assert len(matches) == 1, f"expected exactly one row for {key}: {report['bundles']}"
    return matches[0]


def project_listing(client: TestClient, headers: dict) -> Dict[str, dict]:
    response = client.get("/projects", headers=headers)
    assert response.status_code == 200, response.text
    return {project["id"]: project for project in response.json()}


def project_asset_ids(client: TestClient, headers: dict, project_id: str) -> List[str]:
    response = client.get(f"/project/{project_id}/assets", headers=headers)
    assert response.status_code == 200, response.text
    return [asset["id"] for asset in response.json()]


def member_count(project_id: str) -> int:
    with Session(engine) as session:
        return len(session.exec(select(ProjectAsset).where(ProjectAsset.project_id == project_id)).all())


def detect(client: TestClient, headers: dict, **params) -> dict:
    response = client.get("/bundles/detect", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def create(client: TestClient, headers: dict, items: List[dict]) -> dict:
    response = client.post("/bundles/create", json={"items": items}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def dismiss(client: TestClient, headers: dict, keys: List[str], dismissed: bool = True) -> dict:
    response = client.post("/bundles/dismiss", json={"keys": keys, "dismissed": dismissed}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# --- the report over HTTP ---------------------------------------------------


def test_the_moai_bundle_becomes_one_project(client: TestClient, isolated_storage: Path) -> None:
    moai_bundle(isolated_storage)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert report["counts"]["total"] == 1
    row = row_for(report, MOAI)
    assert row["confidence"] == "high"
    assert row["detector"] == "engine:one-project"
    assert row["status"] == "ready"
    assert row["model_count"] == 3
    assert row["image_count"] == 5
    assert row["cover"]["rel_path"] == f"{MOAI}/images/3.jpg", "the largest preview is the cover"
    assert row["cover"]["reason"] == "largest-image"
    assert row["assets_total"] == 10, "3 models, 5 images and the two documents"

    created = create(client, headers, [{"key": row["key"]}])

    assert created["created"] == 1
    project_id = created["results"][0]["project_id"]
    project = project_listing(client, headers)[project_id]
    assert project["creator"] == "moai_maker"
    assert project["licence"] == "CC BY-NC"
    assert project["licence_url"] == "https://creativecommons.org/licenses/by-nc/4.0/"
    assert project["source_url"] == THING_URL
    assert project["origin"] == "imported"
    # Mf-7oow: a created project holds EVERY registered file under its key - the
    # three models, the five previews AND the two documents - and the row above counts
    # the same ten. The two numbers agreeing is the point: membership is one rule and
    # the role each file plays is the detector's own classifier.
    assert len(project_asset_ids(client, headers, project_id)) == 10
    assert created["results"][0]["assets_added"] == 10


def test_the_twin_zip_is_provenance_only(client: TestClient, isolated_storage: Path) -> None:
    moai_bundle(isolated_storage)
    twin = isolated_storage / f"{MOAI}.zip"
    # Same entry basenames, flat on purpose: `twin_verified` compares names only.
    write_zip(
        twin,
        {
            "README.txt": README_TEXT.encode(),
            "LICENSE.txt": LICENSE_TEXT.encode(),
            **{f"{index}.jpg": b"j" for index in range(len(IMAGE_SIZES))},
            **{f"part{index}.stl": STL_BYTES for index in range(3)},
        },
    )
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == [MOAI], "the twin zip is never a bundle of its own"
    row = row_for(report, MOAI)
    assert row["archive"] == {"rel_path": f"{MOAI}.zip", "twin_verified": True}

    created = create(client, headers, [{"key": MOAI}])

    assert created["created"] == 1
    project_id = created["results"][0]["project_id"]
    assert len(project_asset_ids(client, headers, project_id)) == 10, (
        "the archive attaches nothing - ten is the DIRECTORY's own ten registered files "
        "(three models, five previews, two documents), not the twin zip's copies, which "
        "are never members"
    )

    # The same stem, different content: surfaced as unverified, still one project.
    write_zip(twin, {"something-else.stl": STL_BYTES})
    report = detect(client, headers)
    assert row_for(report, MOAI)["archive"]["twin_verified"] is False
    assert detect(client, headers)["counts"]["total"] == 1
    assert create(client, headers, [{"key": MOAI}])["created"] == 0
    assert len(project_listing(client, headers)) == 1


def test_a_name_only_create_is_not_enriched(client: TestClient, isolated_storage: Path) -> None:
    generic_bundle(isolated_storage, "Generic Thing")
    scan()
    headers = auth_header(client)
    row = row_for(detect(client, headers), "Generic Thing")
    assert row["confidence"] == "medium"

    created = create(client, headers, [{"key": row["key"], "name_only": True}])

    assert created["created"] == 1
    project = project_listing(client, headers)[created["results"][0]["project_id"]]
    assert project["creator"] is None
    assert project["licence"] is None
    assert project["licence_url"] is None
    assert project["source_url"] is None
    assert project["description"] is None


def test_a_second_run_creates_nothing(client: TestClient, isolated_storage: Path) -> None:
    moai_bundle(isolated_storage)
    scan()
    headers = auth_header(client)
    assert detect(client, headers)["counts"]["already_created"] == 0
    first = create(client, headers, [{"key": MOAI}])
    assert first["created"] == 1
    project_id = first["results"][0]["project_id"]

    report = detect(client, headers)

    assert report["counts"]["already_created"] == 1
    row = row_for(report, MOAI)
    projects = project_listing(client, headers)
    assert row["existing_project_id"] == project_id
    # Identity against the served project, not a literal: the name must be the
    # one the project endpoint returns for that id, read in the same request.
    assert row["existing_project_name"] == projects[project_id]["name"]
    second = create(client, headers, [{"key": MOAI}])
    assert second["created"] == 0
    assert second["skipped"] == 1
    assert len(project_listing(client, headers)) == 1


def test_an_edited_project_is_never_overwritten(client: TestClient, isolated_storage: Path) -> None:
    moai_bundle(isolated_storage)
    scan()
    headers = auth_header(client)
    project_id = create(client, headers, [{"key": MOAI}])["results"][0]["project_id"]
    response = client.patch(f"/project/{project_id}", json={"name": "Edited Moai"}, headers=headers)
    assert response.status_code == 200, response.text
    with Session(engine) as session:
        hand_added = [
            session.exec(select(Asset).where(Asset.storage_path == f"{MOAI}/images/0.jpg")).one().id,
            # Both hand-adds name files the create attaches itself since Mf-7oow (a
            # bundle project holds every registered file under its key), so this call
            # is a no-op over existing members - which is exactly the point below.
            session.exec(select(Asset).where(Asset.storage_path == f"{MOAI}/README.txt")).one().id,
        ]
    assert client.post(f"/project/{project_id}/assets", json={"asset_ids": hand_added}, headers=headers).status_code == 200

    report = detect(client, headers)
    row = row_for(report, MOAI)
    assert row["existing_project_id"] == project_id
    # The name is derived per read, never stored at detect time, so an edited
    # project's new name is what the report says - there is no stale copy.
    assert row["existing_project_name"] == "Edited Moai"
    again = create(client, headers, [{"key": MOAI}])

    assert again["created"] == 0
    assert again["skipped"] == 1
    projects = project_listing(client, headers)
    assert len(projects) == 1
    assert projects[project_id]["name"] == "Edited Moai"
    assert member_count(project_id) == 10, (
        "ten is every file the scan registered under the key, because a bundle create "
        "holds every one of them (Mf-7oow) - the hand-adds below are all members "
        "already, so the number witnesses that re-clicking neither adds nor removes "
        "what a human put there, and that a text file is a member of a bundle project "
        "now (it was the file a create used to skip)"
    )


def test_a_legacy_blank_name_still_reads_as_created(
    client: TestClient, isolated_storage: Path
) -> None:
    """A project whose name is blank is still created: the id is the identity.

    The name is a display fact, so a blank one becomes None rather than leaking
    an empty string, and the row stays out of the queue because
    ``counts.already_created`` keys on the id alone (the Mf-gyxg behaviour).
    """
    moai_bundle(isolated_storage)
    scan()
    headers = auth_header(client)
    project_id = create(client, headers, [{"key": MOAI}])["results"][0]["project_id"]
    with Session(engine) as session:
        project = session.get(Project, project_id)
        assert project is not None
        project.name = ""
        session.add(project)
        session.commit()

    report = detect(client, headers)
    row = row_for(report, MOAI)

    assert row["existing_project_id"] == project_id
    assert row["existing_project_name"] is None
    assert report["counts"]["already_created"] == 1


def test_detection_writes_nothing(client: TestClient, isolated_storage: Path) -> None:
    moai_bundle(isolated_storage)
    generic_bundle(isolated_storage, "Generic Thing")
    legacy_bundle(isolated_storage, "Moai")
    headers = auth_header(client)
    before_files = filesystem_snapshot(isolated_storage)
    before_rows = table_counts()

    report = detect(client, headers, limit=1000)

    assert report["counts"]["total"] == 3
    assert filesystem_snapshot(isolated_storage) == before_files, "detection writes no file"
    assert table_counts() == before_rows, "detection writes no Folder/Asset/Project/audit row"


def test_a_legacy_bundle_is_reported_unresolvable_and_creates_nothing(
    client: TestClient, isolated_storage: Path
) -> None:
    legacy_bundle(isolated_storage, "Moai")
    insert_folder_row("Moai statue -No overhang")
    headers = auth_header(client)

    report = detect(client, headers)

    row = row_for(report, "Moai")
    assert row["detector"] == "engine:one-project"
    assert row["confidence"] == "medium"
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "unresolved_directory"
    assert row["cover"]["reason"] == "largest-image-unregistered", "no registered image exists"

    blocked = create(client, headers, [{"key": "Moai"}])
    assert blocked["created"] == 0
    # `blocked` is no longer a create gate (Mf-z643): only an archive-only row
    # answers `blocked`. The legacy bundle holds no registered assets, so the
    # primitive's empty-member refusal is what answers now - a state refusal,
    # never a shape claim, and still no project.
    assert blocked["blocked"] == 0
    assert blocked["failed"] == 1
    assert blocked["results"][0]["status"] == "failed"
    assert blocked["results"][0]["reason"] == "no member files resolved"
    bulk = create(client, headers, [{"key": "Moai", "name_only": True}])
    assert bulk["created"] == 0, "a bulk name-only create must not invent a project over no files"
    assert bulk["failed"] == 1
    assert project_listing(client, headers) == {}


def test_an_archive_only_bundle_is_read_and_blocked(client: TestClient, isolated_storage: Path) -> None:
    write_zip(
        isolated_storage / "Moai.zip",
        {
            "README.txt": README_TEXT.encode(),
            "LICENSE.txt": LICENSE_TEXT.encode(),
            "images/a.jpg": b"j" * 300,
            "images/b.jpg": b"j" * 100,
            "files/part.stl": STL_BYTES,
        },
    )
    headers = auth_header(client)

    report = detect(client, headers)

    row = row_for(report, "Moai.zip")
    # An archive row is named by its filename and carries NO provenance: the engine
    # measures the payload, not the documents inside it.
    assert row["title"] == "Moai"
    assert row["creator"] is None
    assert row["licence"] is None
    assert row["source_url"] is None
    assert row["cover"] == {
        "asset_id": None,
        "rel_path": None,
        "reason": "none",
        "url": None,
        "thumb_url": None,
    }
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "archive_only"
    assert create(client, headers, [{"key": "Moai.zip"}])["blocked"] == 1
    assert project_listing(client, headers) == {}


def test_dismissing_a_bundle_hides_it_and_undo_brings_it_back(client: TestClient, isolated_storage: Path) -> None:
    moai_bundle(isolated_storage)
    scan()
    headers = auth_header(client)
    assert detect(client, headers)["counts"]["total"] == 1

    dismissed = dismiss(client, headers, [MOAI])
    assert dismissed["dismissed"] == [MOAI]
    report = detect(client, headers)
    assert report["counts"]["total"] == 0
    assert report["counts"]["dismissed"] == 1
    shown = detect(client, headers, include_dismissed=True)
    assert row_for(shown, MOAI)["dismissed"] is True

    dismiss(client, headers, ["ghost/bundle"])
    assert detect(client, headers)["stale_dismissals"] == ["ghost/bundle"], "a rename is surfaced, not hidden"

    dismiss(client, headers, [MOAI], dismissed=False)
    assert detect(client, headers)["counts"]["total"] == 1


def test_bulk_create_creates_every_offered_bundle(client: TestClient, isolated_storage: Path) -> None:
    generic_bundle(isolated_storage, "Alpha Set")
    generic_bundle(isolated_storage, "Beta Set")
    scan()
    headers = auth_header(client)
    report = detect(client, headers)
    assert report["counts"]["medium"] == 2

    created = create(
        client,
        headers,
        [{"key": row["key"], "name_only": True} for row in report["bundles"]],
    )

    assert created["created"] == 2
    assert len(project_listing(client, headers)) == 2


def test_a_wrapped_vendor_zip_is_offered_at_low_and_stays_archive_only(
    client: TestClient, isolated_storage: Path
) -> None:
    """Fates End ships each tower as one wrapper directory inside the archive."""
    write_zip(
        isolated_storage / "01_Fates End - Wizard Tower.zip",
        {
            "01_Wizard/part-a.stl": STL_BYTES,
            "01_Wizard/part-b.stl": STL_BYTES,
            "01_Wizard/preview.jpg": b"j" * 200,
            "01_Wizard/notes.txt": b"print at 0.2mm",
        },
    )
    headers = auth_header(client)

    report = detect(client, headers)

    row = row_for(report, "01_Fates End - Wizard Tower.zip")
    assert row["detector"] == "engine:archive-project"
    assert row["model_count"] == 2
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "archive_only"
    assert create(client, headers, [{"key": row["key"]}])["blocked"] == 1
    assert project_listing(client, headers) == {}


def test_a_single_model_directory_bundle_is_offered_and_creates(
    client: TestClient, isolated_storage: Path
) -> None:
    """LittleTup's model layout, with a synthetic root trio.

    The real directory carries no image and no text, so the preview and the notes
    are made up to isolate the depth rule; only ``articulated-dragon-claws/`` is
    real.
    """
    bundle = isolated_storage / "LittleTup"
    model_paths = [f"LittleTup/articulated-dragon-claws/claw-{index}.stl" for index in range(3)]
    for index in range(3):
        write_file(bundle / "articulated-dragon-claws" / f"claw-{index}.stl", STL_BYTES)
    write_file(bundle / "preview.jpg", b"j" * 100)
    write_text(bundle / "notes.txt", "supports everywhere")
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    row = row_for(report, "LittleTup")
    assert row["detector"] == "engine:one-project"
    assert row["model_count"] == 3

    created = create(client, headers, [{"key": "LittleTup", "name_only": True}])

    assert created["created"] == 1
    with Session(engine) as session:
        # Every registered file under the key, `notes.txt` included (Mf-7oow): the
        # three claws plus the preview and the document are one member set, and the
        # role each plays is the detector's own classifier.
        expected = {
            asset.id
            for asset in session.exec(
                select(Asset).where(
                    Asset.storage_path.in_([*model_paths, "LittleTup/preview.jpg", "LittleTup/notes.txt"])
                )
            )
        }
    assert set(project_asset_ids(client, headers, created["results"][0]["project_id"])) == expected


def test_the_named_files_allowance_survives_a_second_model_subdirectory(
    client: TestClient, isolated_storage: Path
) -> None:
    """Pin the allowance so a future simplification to pure cardinality is caught."""
    bundle = isolated_storage / "Named Allowance"
    write_file(bundle / "files" / "a.stl", STL_BYTES)
    write_file(bundle / "other" / "b.stl", STL_BYTES)
    write_file(bundle / "cover.jpg", b"j" * 50)
    write_text(bundle / "notes.txt", "a trio beside two model folders")
    headers = auth_header(client)

    report = detect(client, headers)

    row = row_for(report, "Named Allowance")
    assert row["confidence"] == "low"
    assert row["model_count"] == 2


# --- the model-preview cover (Mf-15xj) --------------------------------------
#
# A bundle with no image used to report ``reason: none``, so the review row had
# no thumbnail and the created project no visual identity. Its own renderable
# model is the fourth fallback; the reason stays honest about which source won.


def test_a_bundle_with_models_and_no_image_derives_a_model_preview_cover(
    client: TestClient, isolated_storage: Path
) -> None:
    gear_bearing_bundle(isolated_storage)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    row = row_for(report, GEAR_BEARING)
    assert row["detector"] == "engine:one-project"
    assert row["confidence"] == "high"
    assert row["status"] == "ready"
    assert row["image_count"] == 0, "the fixture ships no preview at all"
    gear_id = asset_id_at(f"{GEAR_BEARING}/files/gear.stl")
    assert row["cover"] == {
        "asset_id": gear_id,
        "rel_path": f"{GEAR_BEARING}/files/gear.stl",
        "reason": "model-preview",
        "url": f"/file/{gear_id}/gear.stl",
        "thumb_url": None,
    }, "the largest renderable model, served at the one asset URL shape, with no thumbnail yet"


def test_an_unregistered_image_outranks_a_renderable_model(client: TestClient, isolated_storage: Path) -> None:
    gear_bearing_bundle(isolated_storage)
    scan()
    # Written after the scan, so the largest image on disk has no Asset row: the
    # vendor's own picture still beats a rendered one, exactly as before.
    write_file(isolated_storage / GEAR_BEARING / "preview.png", b"p" * 60)
    headers = auth_header(client)

    row = row_for(detect(client, headers), GEAR_BEARING)

    assert row["cover"]["reason"] == "largest-image-unregistered"
    assert row["cover"]["asset_id"] is None
    assert row["cover"]["url"] is None
    assert row["cover"]["thumb_url"] is None


def test_a_bundle_with_no_image_and_no_renderable_model_reports_no_cover(
    client: TestClient, isolated_storage: Path
) -> None:
    """An OpenSCAD source is a model in the tree but not one the client can render."""
    bundle = isolated_storage / "Scad Only"
    write_text(bundle / "README.txt", "Scad Only\nCreated by Thingiverse user maker_444\nLicensed under CC BY-NC\n")
    write_text(
        bundle / "LICENSE.txt",
        "Scad Only by maker_444 on Thingiverse: https://www.thingiverse.com/thing:444444\n",
    )
    write_text(bundle / "notes.txt", "parametric source only")
    write_file(bundle / "model.scad", b"s" * 20)
    scan()
    headers = auth_header(client)

    row = row_for(detect(client, headers), "Scad Only")

    assert row["cover"]["reason"] == "none"
    assert row["cover"]["rel_path"] is None
    assert row["cover"]["url"] is None
    assert row["cover"]["thumb_url"] is None


def test_a_laser_file_is_not_offered_as_a_model_preview_cover(client: TestClient, isolated_storage: Path) -> None:
    """A .lbrn2 is a MODEL_SUFFIXES member, but the client has no loader for it."""
    bundle = isolated_storage / "Laser Cut"
    write_text(bundle / "README.txt", "Laser Cut\nCreated by Thingiverse user maker_555\nLicensed under CC BY-NC\n")
    write_text(
        bundle / "LICENSE.txt",
        "Laser Cut by maker_555 on Thingiverse: https://www.thingiverse.com/thing:555555\n",
    )
    write_file(bundle / "part.lbrn2", b"l" * 30)
    scan()
    headers = auth_header(client)

    row = row_for(detect(client, headers), "Laser Cut")

    assert row["model_count"] == 1, "the laser file is still a model the bundle owns"
    assert row["cover"]["reason"] == "none", "but a cover can never be rendered from it"


def test_creating_a_model_preview_bundle_sets_the_project_cover_to_the_model(
    client: TestClient, isolated_storage: Path
) -> None:
    gear_bearing_bundle(isolated_storage)
    scan()
    headers = auth_header(client)

    created = create(client, headers, [{"key": GEAR_BEARING}])

    assert created["created"] == 1
    project = project_listing(client, headers)[created["results"][0]["project_id"]]
    assert project["cover_asset_id"] == asset_id_at(f"{GEAR_BEARING}/files/gear.stl")


def test_a_generated_thumbnail_makes_a_model_preview_project_cover_render(
    client: TestClient, isolated_storage: Path
) -> None:
    gear_bearing_bundle(isolated_storage)
    scan()
    headers = auth_header(client)
    created = create(client, headers, [{"key": GEAR_BEARING}])
    project_id = created["results"][0]["project_id"]
    cover_asset_id = asset_id_at(f"{GEAR_BEARING}/files/gear.stl")

    assert project_listing(client, headers)[project_id]["cover_thumb_url"] is None, "no thumbnail row yet"

    generated = client.post(
        f"/asset/{cover_asset_id}/thumbnail-generated",
        files={"file": ("thumb.png", png_bytes(), "image/png")},
        headers=headers,
    )
    assert generated.status_code == 200, generated.text

    listed = project_listing(client, headers)[project_id]
    assert re.fullmatch(rf"/thumb/{cover_asset_id}\.jpg\?v=\d+", listed["cover_thumb_url"] or ""), listed


def test_the_live_shape_has_no_container_bundle(client: TestClient, isolated_storage: Path) -> None:
    """The 2026-11-01 report's first row: 'import' as a bundle holding the tree.

    'import' holds its own depth-0 model+image+text trio - which R3 would accept
    even with the depth bound - plus a residual model-only subdirectory. It
    contains bundles, so it is a container, not a bundle.
    """
    import_dir = isolated_storage / "import"
    write_file(import_dir / "loose.stl", STL_BYTES)
    write_file(import_dir / "loose.jpg", b"j" * 40)
    write_text(import_dir / "notes.txt", "the container's own trio")
    write_file(import_dir / "residual" / "only.stl", STL_BYTES)
    thingiverse_bundle(import_dir / "ParentOf", "Parent Of Thing", 222222)
    moai_bundle(import_dir)
    thingiverse_bundle(import_dir, "Wall Mount Bracket", 111111)
    scan()
    headers = auth_header(client)

    report = detect(client, headers, limit=1000)

    keys = {row["key"] for row in report["bundles"]}
    # `import/` is transparent: never a row, but its children are read normally.
    assert "import" not in keys, "the app's import area is never a row"
    assert keys == {
        "import/ParentOf",
        f"import/{MOAI}",
        "import/Wall Mount Bracket",
        "import/residual",
    }, "every child folder with models is the engine's own row"
    assert row_for(report, "import/residual")["detector"] == "engine:one-project"
    moai_row = row_for(report, f"import/{MOAI}")
    assert moai_row["confidence"] == "high"
    assert moai_row["model_count"] == 3, "models at depth 1 under files/ are still counted"

    created = create(client, headers, [{"key": row["key"]} for row in report["bundles"]])

    assert created["created"] == len(report["bundles"])
    assert "import" not in {project["name"] for project in project_listing(client, headers).values()}
    members: List[str] = []
    for row, result in zip(report["bundles"], created["results"], strict=True):
        # EVERY registered file under the key (Mf-7oow), which is what the row's own
        # `assets_total` counts over the same subtree - so the witness still says
        # "each project holds exactly its own bundle's files", now with text and
        # archives in the set.
        assert member_count(result["project_id"]) == row["assets_total"], (
            f"{row['key']}: one project, every one of its own bundle's files"
        )
        members.extend(project_asset_ids(client, headers, result["project_id"]))
    assert len(members) == len(set(members)), "no asset belongs to two bundles"


# --- the same contract through the service ---------------------------------
#
# These run before the router lane lands; they exercise the identical code paths
# the HTTP tests above drive.


def test_service_the_moai_bundle_is_ready_with_a_cover(isolated_storage: Path) -> None:
    moai_bundle(isolated_storage)
    scan()

    report = bundle_service.build_report()

    row = row_for(report, MOAI)
    assert report["counts"] == {
        "total": 1,
        "high": 1,
        "medium": 0,
        "low": 0,
        "ready": 1,
        "blocked": 0,
        "dismissed": 0,
        "already_created": 0,
        "offered": 1,
        "withheld": 0,
    }
    assert row["detector"] == "engine:one-project"
    assert row["archive"] is None, "a folder with no sibling zip reports no archive"
    assert row["cover"]["asset_id"] is not None
    assert row["cover"]["rel_path"] == f"{MOAI}/images/3.jpg"
    assert row["description_excerpt"] == "Moai statue -No overhang"


def test_service_the_limit_bounds_the_report_and_a_path_scopes_it(isolated_storage: Path) -> None:
    vendor = isolated_storage / "vendor"
    # Two archives under one folder: the folder itself has no models, so the
    # engine reads it as `archive-only` and the walk descends to the zips.
    for name in ("Alpha", "Beta"):
        write_zip(
            vendor / f"{name}.zip",
            {f"{name}/part.stl": STL_BYTES, f"{name}/cover.jpg": b"j" * 20, f"{name}/notes.txt": b"print"},
        )
    scan()

    bounded = bundle_service.build_report(limit=1)

    assert [row["key"] for row in bounded["bundles"]] == ["vendor/Alpha.zip"]
    assert bounded["truncated"] is True, "a withheld row is reported, not hidden"
    assert bounded["counts"]["total"] == 2, "the counts still describe everything found"
    assert bounded["counts"]["offered"] == 2
    assert bounded["counts"]["withheld"] == 1
    assert bundle_service.build_report(limit=50)["truncated"] is False

    scoped = bundle_service.build_report(path="vendor")

    assert scoped["root"] == "vendor"
    assert [row["key"] for row in scoped["bundles"]] == ["vendor/Alpha.zip", "vendor/Beta.zip"]


def test_the_report_refuses_a_path_that_leaves_the_storage_root(
    client: TestClient, isolated_storage: Path, tmp_path: Path
) -> None:
    vendor = isolated_storage / "vendor"
    moai_bundle(vendor, "Alpha Bundle")
    moai_bundle(tmp_path / "outside", "Outside Bundle")
    # A symlink inside the library is the one shape a segment check cannot see:
    # the walk is seeded with it and follows it wherever it points.
    (isolated_storage / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    headers = auth_header(client)

    for escape in ("..", "../outside", "a/../../b", "/etc", "a\x00b", "escape"):
        response = client.get("/bundles/detect", params={"path": escape}, headers=headers)
        assert response.status_code == 400, f"{escape!r} must not walk out of the library: {response.text}"
        assert "Outside Bundle" not in response.text

    scoped = detect(client, headers, path="vendor")

    assert [row["key"] for row in scoped["bundles"]] == ["vendor/Alpha Bundle"], "a plain relative path still scopes"
    assert scoped["counts"]["total"] == 1
    assert "Outside Bundle" not in [row["key"] for row in scoped["bundles"]]


def test_service_a_folder_and_its_twin_zip_are_one_candidate(isolated_storage: Path) -> None:
    moai_bundle(isolated_storage)
    write_zip(
        isolated_storage / f"{MOAI}.zip",
        {
            "README.txt": README_TEXT.encode(),
            "LICENSE.txt": LICENSE_TEXT.encode(),
            **{f"{index}.jpg": b"j" for index in range(len(IMAGE_SIZES))},
            **{f"part{index}.stl": STL_BYTES for index in range(3)},
        },
    )
    scan()

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    assert [candidate.key for candidate in candidates] == [MOAI]
    report = bundle_service.build_report()
    assert row_for(report, MOAI)["archive"]["twin_verified"] is True


def test_service_detection_writes_nothing(isolated_storage: Path) -> None:
    moai_bundle(isolated_storage)
    generic_bundle(isolated_storage, "Generic Thing")
    legacy_bundle(isolated_storage, "Moai")
    before_files = filesystem_snapshot(isolated_storage)
    before_rows = table_counts()

    report = bundle_service.build_report()

    assert report["counts"]["total"] == 3
    assert filesystem_snapshot(isolated_storage) == before_files
    assert table_counts() == before_rows


def test_service_the_legacy_bundle_is_blocked(isolated_storage: Path) -> None:
    legacy_bundle(isolated_storage, "Moai")
    insert_folder_row("Moai statue -No overhang")

    row = row_for(bundle_service.build_report(), "Moai")

    assert row["confidence"] == "medium"
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "unresolved_directory"


def test_service_the_archive_is_read_without_extracting(isolated_storage: Path) -> None:
    write_zip(
        isolated_storage / "Moai.zip",
        {
            "README.txt": README_TEXT.encode(),
            "LICENSE.txt": LICENSE_TEXT.encode(),
            "images/a.jpg": b"j" * 30,
            "images/b.jpg": b"j" * 10,
            "files/part.stl": STL_BYTES,
        },
    )

    report = bundle_service.build_report()

    row = row_for(report, "Moai.zip")
    assert row["title"] == "Moai"
    assert row["creator"] is None
    assert row["cover"]["rel_path"] is None
    assert row["blocked_reason"] == "archive_only"
    assert not (isolated_storage / "README.txt").exists(), "reading an archive extracts nothing"


def test_service_a_second_run_and_an_edited_project_are_untouched(isolated_storage: Path) -> None:
    moai_bundle(isolated_storage)
    scan()
    ctx = audit_ctx()
    first = bundle_service.create_bundles([bundle_service.BundleCreateItem(key=MOAI)], ctx)
    assert first["created"] == 1
    project_id = first["results"][0]["project_id"]
    assert first["results"][0]["assets_added"] == 10, (
        "every registered file under the key - three models, five previews and the two "
        "marker documents (Mf-7oow); the second run below still creates nothing"
    )
    with Session(engine) as session:
        project = project_service.get_project(session, project_id)
        project_service.update_project(session, project, {"name"}, "Edited Moai", None, None, audit_ctx=None)
    with Session(engine) as session:
        hand_added = [
            session.exec(select(Asset).where(Asset.storage_path == f"{MOAI}/images/0.jpg")).one().id,
            # As in the HTTP witness above: the text member is the hand-add the ATTACH
            # table cannot produce, so it is the one the count below can still see.
            session.exec(select(Asset).where(Asset.storage_path == f"{MOAI}/README.txt")).one().id,
        ]
    project_service.add_assets(project_id, hand_added)

    second = bundle_service.create_bundles([bundle_service.BundleCreateItem(key=MOAI)], ctx)

    assert second["created"] == 0
    assert second["skipped"] == 1
    assert second["results"][0]["reason"] == "already_created"
    with Session(engine) as session:
        projects = session.exec(select(Project)).all()
        assert len(projects) == 1
        assert projects[0].name == "Edited Moai"
    assert member_count(project_id) == 10, (
        "the service-level twin of the witness above: re-running the create over an "
        "edited project adds nothing, and ten is every registered file under the key - "
        "the create attaches them all itself since Mf-7oow, so this number witnesses "
        "that a member a human put there survives the skip (and that re-adding one the "
        "create already attached changes nothing)"
    )
    report = bundle_service.build_report()
    assert report["counts"]["already_created"] == 1


def test_service_dismissals_round_trip_and_report_stale_keys(isolated_storage: Path) -> None:
    moai_bundle(isolated_storage)
    scan()
    ctx = audit_ctx()

    assert bundle_service.set_dismissals([MOAI], True, ctx)["dismissed"] == [MOAI]
    report = bundle_service.build_report()
    assert report["counts"]["total"] == 0
    assert report["counts"]["dismissed"] == 1
    assert bundle_service.build_report(include_dismissed=True)["bundles"][0]["dismissed"] is True

    bundle_service.set_dismissals(["ghost/bundle"], True, ctx)
    assert bundle_service.build_report()["stale_dismissals"] == ["ghost/bundle"]

    bundle_service.set_dismissals([MOAI], False, ctx)
    assert bundle_service.build_report()["counts"]["total"] == 1
    assert set(bundle_service.get_dismissals()) == {"ghost/bundle"}
    with Session(engine) as session:
        record = session.exec(select(AuditEvent).where(AuditEvent.action == "bundle.dismiss")).first()
    assert record is not None, "a dismissal is an audited act"
    assert json.loads(record.detail_json or "{}") == {"dismissed": True}


def test_service_a_name_match_is_a_skip_and_an_unknown_key_is_a_failure(isolated_storage: Path) -> None:
    generic_bundle(isolated_storage, "Generic Thing")
    scan()
    with Session(engine) as session:
        project_service.create_project(session, "Generic Thing", None, None, origin="imported")

    result = bundle_service.create_bundles(
        [
            bundle_service.BundleCreateItem(key="Generic Thing"),
            bundle_service.BundleCreateItem(key="does/not/exist"),
        ],
        audit_ctx(),
    )

    assert result["skipped"] == 1 and result["failed"] == 1
    assert result["results"][0]["reason"] == "already_created", "an exact name match is idempotency, not a second row"
    assert result["results"][1]["reason"] == "unknown bundle"
    with Session(engine) as session:
        assert len(session.exec(select(Project)).all()) == 1


def test_a_409_from_the_name_index_becomes_a_skip_not_a_500(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moai_bundle(isolated_storage)
    scan()
    headers = auth_header(client)

    def refuse(*_args, **_kwargs):
        raise HTTPException(status_code=409, detail="Project name already exists")

    # `_clean_name` runs before the NOCASE index, so a typed name that cleans to an
    # existing one slips the pre-check; only a race reaches this path otherwise.
    # Mf-yhjo re-points this at `create_project_with_members`: the door writes the
    # project and its members through that one call now, in one commit, and
    # `create_project` is no longer on the path at all. The verdict the test pins is
    # unchanged - a 409 from the name index is a skip - and so is the stronger half:
    # one commit is what makes "a refused name creates no row" true rather than a
    # lucky ordering of two commits.
    monkeypatch.setattr(project_service, "create_project_with_members", refuse)

    response = client.post("/bundles/create", json={"items": [{"key": MOAI}]}, headers=headers)

    assert response.status_code == 200, response.text
    created = response.json()
    assert created["created"] == 0
    assert created["skipped"] == 1
    assert created["failed"] == 0
    assert created["results"][0]["status"] == "skipped"
    with Session(engine) as session:
        assert session.exec(select(Project)).all() == [], "a refused name creates no row"


def test_create_still_creates_an_ordinary_depth_zero_trio(isolated_storage: Path) -> None:
    """The belt is not a blanket refusal: a genuine low bundle still creates."""
    generic_bundle(isolated_storage, "Generic Thing")
    scan()
    report = bundle_service.build_report()
    row = row_for(report, "Generic Thing")
    candidates, _truncated = bundle_service.iter_bundle_candidates(isolated_storage)
    candidate = next(item for item in candidates if item.key == "Generic Thing")

    result = bundle_service._create_one(
        bundle_service.BundleCreateItem(key="Generic Thing", name_only=True), row, candidate, audit_ctx()
    )

    assert result["status"] == "created"
    with Session(engine) as session:
        assert len(session.exec(select(Project)).all()) == 1


# --- the bounded zip reader -------------------------------------------------


def test_the_zip_reader_reads_a_member_without_extracting(tmp_path: Path) -> None:
    zip_path = tmp_path / "bundle.zip"
    write_zip(zip_path, {"README.txt": README_TEXT.encode()})

    assert zip_service.read_zip_text_entry(zip_path, "README.txt", 4096) == README_TEXT
    assert not (tmp_path / "README.txt").exists(), "nothing is extracted"


def test_the_zip_reader_refuses_a_declared_oversize_member(tmp_path: Path) -> None:
    zip_path = tmp_path / "bundle.zip"
    write_zip(zip_path, {"README.txt": README_TEXT.encode()})

    assert zip_service.read_zip_text_entry(zip_path, "README.txt", len(README_TEXT) - 1) is None
    assert zip_service.read_zip_text_entry(zip_path, "README.txt", len(README_TEXT)) == README_TEXT


def test_the_zip_reader_rejects_a_traversal_member(tmp_path: Path) -> None:
    assert not zip_service.normalize_zip_entry_path("..")
    assert not zip_service.normalize_zip_entry_path("a/../b")
    zip_path = tmp_path / "bundle.zip"
    write_zip(zip_path, {"..": b"nope", "a/../b": b"nope", "ok.txt": b"fine"})

    assert zip_service.read_zip_text_entry(zip_path, "..", 4096) is None
    assert zip_service.read_zip_text_entry(zip_path, "a/../b", 4096) is None
    assert zip_service.read_zip_text_entry(zip_path, "ok.txt", 4096) == "fine"


def test_the_zip_reader_answers_none_for_a_missing_member_and_a_bad_archive(tmp_path: Path) -> None:
    zip_path = tmp_path / "bundle.zip"
    write_zip(zip_path, {"ok.txt": b"fine"})
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip")

    assert zip_service.read_zip_text_entry(zip_path, "missing.txt", 4096) is None
    assert zip_service.read_zip_text_entry(broken, "ok.txt", 4096) is None


# --- the real ticket shape: swapped documents -------------------------------
#
# A real Thingiverse export is not keyed to a filename: either document may hold
# the created-by sentence or the title line. These tests drive the ticket's real
# swapped shape through the same scan + detect + create path the box uses.


def swapped_cat_bundle(root: Path) -> Path:
    """The ticket's swapped root pair: README is the title line, LICENSE the created-by."""
    bundle = root / CAT_NAME
    write_text(bundle / "README.txt", CAT_TITLE_LINE)
    write_text(bundle / "LICENSE.txt", CAT_CREATED_BY)
    write_file(bundle / "files" / "part.stl", STL_BYTES)
    write_file(bundle / "images" / "1.jpg", b"j" * 20)
    return bundle


def test_a_swapped_cat_bundle_is_high_ready_and_creates_one_project(client: TestClient, isolated_storage: Path) -> None:
    swapped_cat_bundle(isolated_storage)
    scan()
    headers = auth_header(client)

    row = row_for(detect(client, headers), CAT_NAME)

    assert row["confidence"] == "high"
    assert row["detector"] == "engine:one-project"
    assert row["metadata_shape"] == "one-project"
    assert row["title"] == "2-color standing cat"
    assert row["creator"] == "nervoussystem"
    assert row["licence"] == "CC BY-NC-SA"
    assert row["licence_url"] == "https://creativecommons.org/licenses/by-nc-sa/4.0/"
    assert row["source_url"] == CAT_URL
    assert row["status"] == "ready", "the unchanged Mf-um7x readiness path"
    assert row["model_count"] == 1

    created = create(client, headers, [{"key": CAT_NAME}])

    assert created["created"] == 1
    project_id = created["results"][0]["project_id"]
    project = project_listing(client, headers)[project_id]
    assert project["name"] == "2-color standing cat"
    assert project["creator"] == "nervoussystem"
    assert project["licence"] == "CC BY-NC-SA"
    assert project["licence_url"] == "https://creativecommons.org/licenses/by-nc-sa/4.0/"
    assert project["source_url"] == CAT_URL
    assert project["description"] is None, "the engine derives no prose description from the swapped doc pair"
    assert member_count(project_id) == 4, (
        "the cat's model, its preview AND the two swapped documents (Mf-7oow): every "
        "registered file under the key, whatever role the detector gives it"
    )


def test_a_swapped_zip_is_high_and_stays_archive_only(client: TestClient, isolated_storage: Path) -> None:
    write_zip(
        isolated_storage / "Cat.zip",
        {
            "README.txt": CAT_TITLE_LINE.encode(),
            "LICENSE.txt": CAT_CREATED_BY.encode(),
            "files/part.stl": STL_BYTES,
            "images/1.jpg": b"j" * 20,
        },
    )
    headers = auth_header(client)

    row = row_for(detect(client, headers), "Cat.zip")

    # An archive row is its filename and the engine's payload reading, never the
    # documents inside it.
    assert row["title"] == "Cat"
    assert row["creator"] is None
    assert row["source_url"] is None
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "archive_only"

    blocked = create(client, headers, [{"key": "Cat.zip"}])

    assert blocked["blocked"] == 1
    assert project_listing(client, headers) == {}


def test_the_fourth_document_is_read_so_a_sibling_merge_can_complete(client: TestClient, isolated_storage: Path) -> None:
    bundle = isolated_storage / "Mixed"
    write_text(bundle / "README.txt", CAT_CREATED_BY)
    write_text(bundle / "LICENSE.txt", "All rights reserved.\n")
    write_text(bundle / "Mixed_README", CAT_TITLE_LINE)
    write_text(bundle / "Mixed_LICENSE", CAT_CREATED_BY)
    write_file(bundle / "files" / "part.stl", STL_BYTES)
    scan()
    headers = auth_header(client)

    row = row_for(detect(client, headers), "Mixed")

    # The root pair cannot complete (no title/url), so the sibling pair must be
    # reached. At the old cap of three the fourth document, `Mixed_LICENSE`, was
    # never read, so this tree fell to low confidence.
    assert row["confidence"] == "high"
    assert row["detector"] == "engine:one-project"
    assert row["creator"] == "nervoussystem"


def test_a_swapped_legacy_sibling_bundle_is_detected_and_creates(client: TestClient, isolated_storage: Path) -> None:
    name = "Legacy Cat"
    bundle = isolated_storage / name
    write_text(bundle / f"{name}_README" / "readme.txt", CAT_TITLE_LINE)
    write_text(bundle / f"{name}_LICENSE" / "license.txt", CAT_CREATED_BY)
    write_file(bundle / "part.stl", STL_BYTES)
    write_file(bundle / "preview.jpg", b"j" * 20)
    scan()
    headers = auth_header(client)

    row = row_for(detect(client, headers), name)

    assert row["confidence"] == "medium"
    assert row["detector"] == "engine:one-project"
    assert row["metadata_shape"] == "one-project"
    assert row["title"] == "2-color standing cat"
    assert row["creator"] == "nervoussystem"
    assert row["status"] == "ready"

    created = create(client, headers, [{"key": name}])

    assert created["created"] == 1
    project_id = created["results"][0]["project_id"]
    project = project_listing(client, headers)[project_id]
    assert project["creator"] == "nervoussystem"
    assert project["source_url"] == CAT_URL
    assert member_count(project_id) == 4, (
        "``part.stl``, ``preview.jpg`` AND the two flattened sibling documents: a bundle "
        "project holds every registered file under its key (Mf-7oow), and the documents "
        "are ``text`` members rather than exclusions"
    )


# --- entry-root pairing (Mf-6b82) -------------------------------------------

CLOCKWORK_DIR = "import/17_ClockworkMonster"
CLOCKWORK_ROOT = "17_ClockworkMonster"
# The same 14 files under a name R5 refuses, for the Decision-18 silence: a
# lowercase single word is not a title, so the shape alone stays invisible.
NO_TITLE_ROOT = "17_clockworkmonster"
CLOCKWORK_ZIP = "import/17_Fates End - ClockworkMonster Tower.zip"
CLOCKWORK_DUPLICATE_ZIP = "import/17_Fates End - ClockworkMonster Tower (1).zip"
GENERIC_ZIP = "import/Generic Download.zip"


def clockwork_entries(root: str = CLOCKWORK_ROOT) -> Dict[str, bytes]:
    """The real pair: 12 models, 2 renders, no text, every entry rooted at ``root``."""
    entries = {f"{root}/Clockwork_Part{index:02d}.stl": b"s" * (40 + index) for index in range(12)}
    entries[f"{root}/Clockwork_Render0.png"] = b"p" * 60
    entries[f"{root}/Clockwork_Render1.png"] = b"p" * 80
    return entries


def clockwork_directory(storage: Path, root: str = CLOCKWORK_ROOT) -> None:
    """The directory alone: 12 models and 2 renders, no text and no archive."""
    for rel_path, payload in clockwork_entries(root).items():
        write_file(storage / "import" / rel_path, payload)


def clockwork_pair(storage: Path) -> None:
    """The directory, its archive and the byte-identical browser duplicate."""
    clockwork_directory(storage)
    entries = clockwork_entries()
    write_zip(storage / CLOCKWORK_ZIP, entries)
    # copyfile, not a second write_zip: writestr timestamps can differ across
    # seconds, and these two archives must be byte-identical by construction.
    shutil.copyfile(storage / CLOCKWORK_ZIP, storage / CLOCKWORK_DUPLICATE_ZIP)


def test_service_an_archive_pairs_with_the_directory_its_entries_are_rooted_at(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-1: one directory, two byte-identical zips, one creatable low bundle.

    The vendor rename means the stem rule cannot see the pair; the archive's
    entry root does. The archive supplies the directory's identity - the listing
    has 12 models and 2 renders and no text, so no rule of its own offers it -
    and the low confidence says the shape was inferred. The duplicate zip is
    reported in ``duplicates`` and is neither a row nor a second pairing.
    """
    clockwork_pair(isolated_storage)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == [CLOCKWORK_DIR], (
        "the pair is one row, and neither zip is a row of its own"
    )
    row = row_for(report, CLOCKWORK_DIR)
    assert row["archive"] == {"rel_path": CLOCKWORK_ZIP, "twin_verified": True}
    assert row["detector"] == "engine:one-project"
    assert row["confidence"] == "medium"
    assert row["model_count"] == 12
    assert row["image_count"] == 2
    assert row["text_count"] == 0
    assert row["status"] == "ready"
    assert row["blocked_reason"] is None
    assert row["assets_total"] == 14, (
        "the 12 STL models and the 2 PNG renders: the library scan registers every "
        "file, so a preview is an asset even though the import door would not take it"
    )

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)
    assert truncated is False
    assert [candidate.key for candidate in candidates] == [CLOCKWORK_DIR]
    assert candidates[0].archive_path == CLOCKWORK_ZIP
    assert candidates[0].entry_root == CLOCKWORK_ROOT

    assert len(report["duplicates"]) == 1
    duplicate = report["duplicates"][0]
    assert duplicate["paths"] == [CLOCKWORK_ZIP, CLOCKWORK_DUPLICATE_ZIP]
    assert duplicate["reclaimable_bytes"] == duplicate["size"]
    assert duplicate["size"] == (isolated_storage / CLOCKWORK_ZIP).stat().st_size
    assert CLOCKWORK_DUPLICATE_ZIP not in {row["key"] for row in report["bundles"]}, (
        "the copy is reported in duplicates, never as a second bundle row"
    )

    created = create(client, headers, [{"key": CLOCKWORK_DIR}])

    assert created["created"] == 1
    assert created["blocked"] == 0
    project_id = created["results"][0]["project_id"]
    assert len(project_listing(client, headers)) == 1
    assert len(project_asset_ids(client, headers, project_id)) == 14, (
        "the directory's own twelve models AND its two PNG renders, attached from the "
        "store like any LOW bundle - the ATTACH table is models plus images (Mf-wsdo), "
        "and `assets_total` above already counted the same fourteen"
    )


def test_service_an_archive_with_macosx_cruft_still_pairs(
    client: TestClient, isolated_storage: Path
) -> None:
    """AppleDouble cruft is not payload, so it must not unpair a verified archive.

    ``common_entry_root`` already ignores a top-level ``__MACOSX`` segment, so the
    pure layer reports the real root; without the same skip in
    ``entry_root_verified`` the disk layer refused the pair on the first cruft
    entry, which made that tolerance dead code end to end. The archive is
    rewritten in place with the real entries plus the cruft, so the genuine pair
    is the only thing under test.
    """
    clockwork_pair(isolated_storage)
    crufted = dict(clockwork_entries())
    crufted[f"__MACOSX/{CLOCKWORK_ROOT}/._Clockwork_Part00.stl"] = b"cr" * 30
    crufted["__MACOSX/._17_ClockworkMonster"] = b"cr" * 20
    write_zip(isolated_storage / CLOCKWORK_ZIP, crufted)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == [CLOCKWORK_DIR], (
        "the crufted archive still pairs, and neither zip is a row of its own"
    )
    assert row_for(report, CLOCKWORK_DIR)["archive"] == {"rel_path": CLOCKWORK_ZIP, "twin_verified": True}


def test_service_a_generic_files_root_is_refused_where_a_genuine_root_pairs(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-2: the discriminator. Same containment, same sizes - only the root differs.

    Both fixtures are built in one storage, so the generic archive is refused AND
    the Clockwork pair still pairs in the same report. The generic archive's
    payloads are byte-identical to the directory it is rooted at, so the name
    guard is the only thing that can reject it.
    """
    generic_payloads = {"a.stl": b"s" * 40, "b.stl": b"s" * 41, "preview.jpg": b"j" * 50, "notes.txt": b"print 0.2mm"}
    for name, payload in generic_payloads.items():
        write_file(isolated_storage / "import" / "files" / name, payload)
    write_zip(
        isolated_storage / GENERIC_ZIP,
        {f"files/{name}": payload for name, payload in generic_payloads.items()},
    )
    clockwork_pair(isolated_storage)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    generic = row_for(report, GENERIC_ZIP)
    assert generic["archive"] is None
    assert generic["status"] == "blocked"
    assert generic["blocked_reason"] == "archive_only"
    assert not any((row["archive"] or {}).get("rel_path") == GENERIC_ZIP for row in report["bundles"]), (
        "no row may claim the generic-rooted archive"
    )
    assert row_for(report, CLOCKWORK_DIR)["archive"] == {"rel_path": CLOCKWORK_ZIP, "twin_verified": True}
    assert {row["key"] for row in report["bundles"]} == {"import/files", GENERIC_ZIP, CLOCKWORK_DIR}, (
        "the generic-rooted directory keeps its own row and pairs with nothing"
    )
    assert row_for(report, "import/files")["archive"] is None


# The ten constructed pairs of the mini-library below: six roots that say nothing
# about their bundle, four that do. Both numbers are THIS fixture's, not the
# owner's library (where 51 of 57 measured entry roots were generic).
ENTRY_ROOT_CASES = (
    "files",
    "Files",
    "images",
    "models",
    "stl",
    "STL",
    "17_ClockworkMonster",
    "LittleTup",
    "Taffi",
    "GavinIsABot",
)


def test_service_the_generic_container_guard_rejects_six_of_ten_entry_roots(isolated_storage: Path) -> None:
    """A constructed mini-library of ten real bundles, identical in every pair.

    Each directory holds a model, an image and a text file, so all ten are
    ``generic-bundle``/``low`` candidates and each ``<name> Download.zip`` is an R3
    archive of its own. The payloads are byte-identical inside a pair, so
    containment would verify for all ten; only ``GENERIC_CONTAINER_ROOTS`` decides.
    Both numbers are measured from this fixture's candidate list rather than by
    subtraction, and they are this fixture's, not the owner's library's (where 51
    of 57 measured entry roots were generic).
    """
    generic_names = {"files", "Files", "images", "models", "stl", "STL"}
    for index, name in enumerate(ENTRY_ROOT_CASES):
        payloads = {
            f"{name}/part.stl": b"s" * (40 + index),
            f"{name}/preview.jpg": b"j" * (100 + index),
            f"{name}/notes.txt": b"n" * (10 + index),
        }
        for rel_path, payload in payloads.items():
            write_file(isolated_storage / rel_path, payload)
        write_zip(isolated_storage / f"{name} Download.zip", payloads)

    candidates, _truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    paired = sorted(candidate.key for candidate in candidates if candidate.entry_root)
    unpaired = sorted(
        candidate.key
        for candidate in candidates
        if candidate.archive_path is None and not candidate.is_archive
    )
    own_archive_rows = sorted(candidate.key for candidate in candidates if candidate.is_archive)

    assert (len(paired), len(unpaired)) == (4, 6), (
        f"accepted={len(paired)} rejected={len(unpaired)} of {len(ENTRY_ROOT_CASES)}"
    )
    assert paired == sorted(f"{name}" for name in ENTRY_ROOT_CASES if name not in generic_names)
    assert unpaired == sorted(f"{name}" for name in ENTRY_ROOT_CASES if name in generic_names)
    assert own_archive_rows == sorted(f"{name} Download.zip" for name in ENTRY_ROOT_CASES if name in generic_names), (
        "a refused archive stays a candidate of its own; a consumed pairing would hide it"
    )


def test_service_a_bundle_pairs_with_its_entry_root_archive_and_attaches_nothing_from_it(
    client: TestClient, isolated_storage: Path
) -> None:
    """The same shape as the Clockwork pair, but a real bundle: models + images + text."""
    payloads = {
        "Gizmo Kit/part-a.stl": b"s" * 40,
        "Gizmo Kit/part-b.stl": b"s" * 41,
        "Gizmo Kit/preview.jpg": b"j" * 50,
        "Gizmo Kit/notes.txt": b"print at 0.2mm",
    }
    for rel_path, payload in payloads.items():
        write_file(isolated_storage / "import" / rel_path, payload)
    write_zip(isolated_storage / "import" / "Gizmo Assembly.zip", payloads)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == ["import/Gizmo Kit"], "the archive is provenance only"
    row = row_for(report, "import/Gizmo Kit")
    assert row["archive"] == {"rel_path": "import/Gizmo Assembly.zip", "twin_verified": True}
    assert row["confidence"] == "medium"

    created = create(client, headers, [{"key": "import/Gizmo Kit"}])

    assert created["created"] == 1
    project_id = created["results"][0]["project_id"]
    assert len(project_asset_ids(client, headers, project_id)) == 4, (
        "the archive attaches nothing: four is the directory's own registered set - two "
        "models, one preview and ``notes.txt`` (Mf-7oow) - never the zip's copies"
    )


def test_service_a_nested_archive_is_a_member_not_a_second_bundle(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-1: the real Clockwork shape with the nested archive inside it.

    The directory is one creatable bundle and the nested zip is a supporting
    file: no second row, no second project, and it is never read as a model. The
    directory survives the container strip even though its own member is
    archive-only at strip time, and the member filter then removes the zip - the
    two orders this decision owns.
    """
    clockwork_pair(isolated_storage)
    nested = {"inner/part.stl": STL_BYTES, "inner/preview.jpg": b"j" * 50, "inner/notes.txt": b"n" * 20}
    nested_zip = f"{CLOCKWORK_DIR}/Clockwork_v1.8.zip"
    write_zip(isolated_storage / nested_zip, nested)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == [CLOCKWORK_DIR], (
        "one bundle: the nested zip is a member, not a second row"
    )
    row = row_for(report, CLOCKWORK_DIR)
    assert row["archive"] == {"rel_path": CLOCKWORK_ZIP, "twin_verified": True}
    assert row["model_count"] == 12, "the nested zip is an archive, never a model"
    assert row["status"] == "ready"
    assert row["blocked_reason"] is None
    assert row["assets_total"] == 15, (
        "the 12 STL models, the 2 PNG renders and the nested zip, which the scan registers"
    )
    duplicate_paths = {path for group in report["duplicates"] for path in group["paths"]}
    assert nested_zip not in duplicate_paths, "a member is never offered for deletion"
    assert [group["paths"] for group in report["duplicates"]] == [[CLOCKWORK_ZIP, CLOCKWORK_DUPLICATE_ZIP]]

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)
    assert truncated is False
    assert [candidate.key for candidate in candidates] == [CLOCKWORK_DIR]
    assert any(rel_path == "Clockwork_v1.8.zip" for rel_path, _size in candidates[0].files), (
        "the nested archive is a file of the bundle's own listing"
    )


def test_service_a_pairing_names_the_directory_not_its_descendants(isolated_storage: Path) -> None:
    """AC-4: a pairing qualifies the directory its entry root names, no deeper.

    ``import/Pile/Sub`` is a models-and-images directory of its own and holds no
    archive, so only a descendant rule could offer it: the pairing names ``Pile``.
    Under "a paired root also qualifies strict descendants" both rows would
    appear and this test fails.
    """
    loose_stl, loose_png = STL_BYTES, b"p" * 70
    write_file(isolated_storage / "import" / "Pile" / "loose.stl", loose_stl)
    write_file(isolated_storage / "import" / "Pile" / "render.png", loose_png)
    write_file(isolated_storage / "import" / "Pile" / "Sub" / "sub.stl", b"s" * 41)
    write_file(isolated_storage / "import" / "Pile" / "Sub" / "sub.png", b"p" * 50)
    write_zip(
        isolated_storage / "import" / "Pile Download.zip",
        {"Pile/loose.stl": loose_stl, "Pile/render.png": loose_png},
    )

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    assert [candidate.key for candidate in candidates] == ["import/Pile"], (
        "the pairing named the parent; the child is offered by nothing"
    )
    assert bundle_service.build_report()["bundles"][0]["archive"] == {
        "rel_path": "import/Pile Download.zip",
        "twin_verified": True,
    }


def test_service_a_paired_extraction_inside_a_bundle_is_not_a_second_row(
    client: TestClient, isolated_storage: Path
) -> None:
    """The mirror case of the pile: payload inside a claimed bundle stays payload.

    ``Parent Thing`` is a high bundle, so the member filter removes its nested
    archive from the pairing pass; the extraction that archive would have owned is
    inside that bundle and must not become a row of its own, exactly as before the
    rule landed. The high row keeps the whole subtree in its own listing, so its
    models are attached to the one project and no archive can double-attach them.
    """
    title = "Wall Mount Bracket"
    parent = isolated_storage / "Parent Thing"
    write_text(parent / "README.txt", f"{title}\nCreated by Thingiverse user bracket_maker\nLicensed under CC BY-NC\n")
    write_text(
        parent / "LICENSE.txt",
        f"{title} by bracket_maker on Thingiverse: https://www.thingiverse.com/thing:111111\n",
    )
    write_file(parent / "parent_part.stl", STL_BYTES)
    extraction = "Nested Export"
    write_file(parent / extraction / "part.stl", b"s" * 41)
    write_file(parent / extraction / "render.png", b"p" * 60)
    write_zip(
        parent / "Nested Download.zip",
        {f"{extraction}/part.stl": b"s" * 41, f"{extraction}/render.png": b"p" * 60},
    )
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == ["Parent Thing"]
    parent_row = row_for(report, "Parent Thing")
    assert parent_row["confidence"] == "high"
    assert parent_row["archive"] is None, "the nested archive is a member, not this bundle's provenance"

    created = create(client, headers, [{"key": "Parent Thing"}])

    assert created["created"] == 1
    assert created["failed"] == 0
    project_id = created["results"][0]["project_id"]
    assert len(project_asset_ids(client, headers, project_id)) == 6, (
        "the parent's own two documents and its model, the unclaimed extraction's model "
        "and render, and the nested archive itself: every registered file under the key "
        "is a member (Mf-7oow) - one project, never two"
    )


def test_service_a_nested_archive_alone_does_not_make_a_directory_a_bundle(
    isolated_storage: Path,
) -> None:
    """AC-2: a directory whose only payload is a nested archive is not a bundle.

    The zip's own entries are a full R3 trio, so the fixture is meaningful: the
    zip is offered as its own archive bundle, while the enclosing directory is
    not. Under the rejected rule "a nested archive's entries count as evidence
    for the enclosing bundle" the directory would become a bundle and this test
    would fail.
    """
    nested = {"inner/part.stl": STL_BYTES, "inner/preview.jpg": b"j" * 50, "inner/notes.txt": b"n" * 20}
    nested_zip = "Solo/Clockwork_v1.8.zip"
    write_zip(isolated_storage / nested_zip, nested)

    report = bundle_service.build_report()

    assert "Solo" not in {row["key"] for row in report["bundles"]}
    assert [row["key"] for row in report["bundles"]] == [nested_zip], (
        "the zip itself is a bundle; the enclosing directory is not"
    )
    candidates, _truncated = bundle_service.iter_bundle_candidates(isolated_storage)
    assert [candidate.key for candidate in candidates] == [nested_zip]


def test_service_a_nested_archive_is_never_offered_for_deletion(isolated_storage: Path) -> None:
    """AC-3: a member of a live bundle is never the deletable side of a duplicate.

    The nested zip is byte-identical to a shorter-named copy outside the bundle,
    so the shortest-name rule would make the member the duplicate-to-delete. The
    report must drop the member from the group instead.
    """
    clockwork_pair(isolated_storage)
    nested = {"inner/part.stl": STL_BYTES, "inner/preview.jpg": b"j" * 50, "inner/notes.txt": b"n" * 20}
    nested_zip = f"{CLOCKWORK_DIR}/Clockwork_v1.8.zip"
    outside_copy = "import/Clockwork_v1.8 copy.zip"
    write_zip(isolated_storage / nested_zip, nested)
    shutil.copyfile(isolated_storage / nested_zip, isolated_storage / outside_copy)

    report = bundle_service.build_report()

    duplicate_paths = {path for group in report["duplicates"] for path in group["paths"]}
    assert nested_zip not in duplicate_paths, "a bundle member is never offered for deletion"
    assert nested_zip not in report["duplicates_text"]
    assert [group["paths"] for group in report["duplicates"]] == [[CLOCKWORK_ZIP, CLOCKWORK_DUPLICATE_ZIP]]
    assert outside_copy in {row["key"] for row in report["bundles"]}, (
        "the outside copy was the group's keep, so it is still offered as its own archive bundle"
    )


def test_service_a_nested_archive_cannot_pair_a_sibling_directory(isolated_storage: Path) -> None:
    """AC-4: the entry-root pass must not resurrect a member as a second bundle.

    The nested archive's name differs from the subdirectory it is rooted at, so
    the stem-twin rule misses and only the entry-root rule could pair it. It must
    not: the archive is a file of the enclosing bundle, and the subdirectory is
    not a second project.
    """
    clockwork_pair(isolated_storage)
    sub = {
        "Clockwork_v1.8/part.stl": STL_BYTES,
        "Clockwork_v1.8/preview.jpg": b"j" * 50,
    }
    for rel_path, payload in sub.items():
        write_file(isolated_storage / CLOCKWORK_DIR / rel_path, payload)
    nested_zip = f"{CLOCKWORK_DIR}/Clockwork v1.8 Download.zip"
    write_zip(isolated_storage / nested_zip, sub)

    report = bundle_service.build_report()

    assert [row["key"] for row in report["bundles"]] == [CLOCKWORK_DIR]
    row = row_for(report, CLOCKWORK_DIR)
    assert row["archive"] == {"rel_path": CLOCKWORK_ZIP, "twin_verified": True}
    assert nested_zip not in {row["key"] for row in report["bundles"]}


# --- stem-twin pairing (Mf-it2a) --------------------------------------------

# The ticket's real directory names. The first four are directories no rule
# offers on its own - models alone, models plus a stray text, models plus an
# image - and the Cinderwing archive is rooted at the generic ``files/``, which
# only the stem route may strip.
SNVB623 = "SnVB623"
SNVD623 = "SnVD623"
CARD_BOX = "ultimate-card-box-v4-model_files"
CINDERWING = "Cinderwing3D - Crystal Dragon"


def test_service_a_verified_stem_twin_pairs_its_directory(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-1: four real pairs are rows and their archives are provenance.

    Each directory is a same-parent stem twin of its archive and its archive
    payload was written to disk byte-identically, so containment verifies:

    * SnVB623 - models only, every entry rooted at the directory name;
    * SnVD623 - models only, FLAT entries, so the identity-first leg is exercised;
    * ultimate-card-box-v4-model_files - models plus ``notes.txt``, no image;
    * Cinderwing3D - Crystal Dragon - models plus one render, rooted at ``files/``,
      the real generic entry root only the stem route may strip.

    All four are asserted because each is failed by a different mutation: delete
    the stem branch in ``_build_candidates`` and the archive becomes a row of its
    own instead of this directory's provenance, so every ``archive_path`` /
    ``entry_root`` assertion below reds.
    """
    root = isolated_storage / "import"
    write_file(root / SNVB623 / "model-a.stl", b"s" * 40)
    write_file(root / SNVB623 / "model-b.stl", b"s" * 41)
    write_zip(
        root / f"{SNVB623}.zip",
        {f"{SNVB623}/model-a.stl": b"s" * 40, f"{SNVB623}/model-b.stl": b"s" * 41},
    )
    write_file(root / SNVD623 / "flat-a.stl", b"s" * 42)
    write_file(root / SNVD623 / "flat-b.stl", b"s" * 43)
    write_zip(root / f"{SNVD623}.zip", {"flat-a.stl": b"s" * 42, "flat-b.stl": b"s" * 43})
    write_file(root / CARD_BOX / "card-box.stl", b"s" * 44)
    write_text(root / CARD_BOX / "notes.txt", "print at 0.2mm")
    write_zip(
        root / f"{CARD_BOX}.zip",
        {f"{CARD_BOX}/card-box.stl": b"s" * 44, f"{CARD_BOX}/notes.txt": b"print at 0.2mm"},
    )
    write_file(root / CINDERWING / "dragon.stl", b"s" * 45)
    write_file(root / CINDERWING / "dragon-body.stl", b"s" * 46)
    write_file(root / CINDERWING / "dragon.png", b"p" * 60)
    write_zip(
        root / f"{CINDERWING}.zip",
        {
            "files/dragon.stl": b"s" * 45,
            "files/dragon-body.stl": b"s" * 46,
            "files/dragon.png": b"p" * 60,
        },
    )

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    sets = [
        # (directory, MODEL files, IMAGE files, TEXT files) - the member count is the
        # sum, because a created project holds every registered file under its key
        # (Mf-7oow) while the two models-only pairs above keep proving the shape rule
        # without an image.
        (SNVB623, 2, 0, 0),
        (SNVD623, 2, 0, 0),
        (CARD_BOX, 1, 0, 1),
        (CINDERWING, 2, 1, 0),
    ]
    by_key = {candidate.key: candidate for candidate in candidates}
    assert sorted(by_key) == sorted(f"import/{name}" for name, _models, _images, _text in sets), (
        "four promoted directories and nothing else"
    )
    for name, _models, _images, _text in sets:
        candidate = by_key[f"import/{name}"]
        assert candidate.archive_path == f"import/{name}.zip", f"the verified stem pairing owns {name}"
        assert candidate.entry_root is None, "the stem route names no entry root"

    scan()
    headers = auth_header(client)
    report = detect(client, headers)

    assert sorted(row["key"] for row in report["bundles"]) == sorted(by_key)
    for name, _models, _images, _text in sets:
        row = row_for(report, f"import/{name}")
        assert row["detector"] == "engine:one-project"
        assert row["status"] == "ready"
        assert row["archive"] == {"rel_path": f"import/{name}.zip", "twin_verified": True}

    created = create(client, headers, [{"key": f"import/{name}"} for name, _models, _images, _text in sets])

    assert created["created"] == 4
    assert created["blocked"] == 0
    assert created["failed"] == 0
    assert len(project_listing(client, headers)) == 4
    for result, (name, models, images, text) in zip(created["results"], sets, strict=True):
        assert member_count(result["project_id"]) == models + images + text, (
            f"{name} holds only its own files - its models, its images and its documents "
            "(Mf-7oow); the archive attaches nothing"
        )
    assert member_count(created["results"][0]["project_id"]) == 2, "the models-only pair is exactly two"


# --- a promoted row never swallows a stronger candidate (Mf-v3i3) ------------

# The owner's own shape: a file extracted beside the zip it came in. The inner
# archive is a genuine R1 export (all four provenance markers), so it is a
# `high`-confidence candidate in its own right, and the identical payload on
# disk is what makes the stem route's containment check verify for the parent.
INNER_README = "Inner Thing\nCreated by Thingiverse user maker_inner\nLicensed under CC BY-NC\n"
INNER_LICENSE = "Inner Thing by maker_inner on Thingiverse: https://www.thingiverse.com/thing:777777\n"
INNER_URL = "https://www.thingiverse.com/thing:777777"


def test_service_a_stem_twin_that_does_not_match_is_reported_unverified(isolated_storage: Path) -> None:
    """A namesake archive is provenance, and a payload mismatch is information.

    ``xbox360shell.zip`` declares a size that differs from the file on disk (the
    basenames still agree, so the names-only remark is ``true``) and
    ``razercatearsremixmodelfiles.zip`` holds entries whose names match nothing
    (the remark is ``false``). The directories are the engine's own rows, so the
    operator's stem pairing still attaches each archive; the report says what the
    names say, never a checksum claim.
    """
    root = isolated_storage / "import"
    write_file(root / "xbox360shell" / "shell.stl", b"s" * 47)
    write_file(root / "xbox360shell" / "shell.png", b"p" * 50)
    write_zip(
        root / "xbox360shell.zip",
        {"xbox360shell/shell.stl": b"s" * 48, "xbox360shell/shell.png": b"p" * 50},
    )
    write_file(root / "razercatearsremixmodelfiles" / "ears.stl", b"s" * 49)
    write_file(root / "razercatearsremixmodelfiles" / "ears.png", b"p" * 51)
    write_zip(
        root / "razercatearsremixmodelfiles.zip",
        {
            "razercatearsremixmodelfiles/other.stl": b"s" * 49,
            "razercatearsremixmodelfiles/other.png": b"p" * 51,
        },
    )

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    assert [candidate.key for candidate in candidates] == [
        "import/razercatearsremixmodelfiles",
        "import/xbox360shell",
    ]
    assert [candidate.archive_path for candidate in candidates] == [
        "import/razercatearsremixmodelfiles.zip",
        "import/xbox360shell.zip",
    ]
    report = bundle_service.build_report()
    assert [row["key"] for row in report["bundles"]] == [
        "import/razercatearsremixmodelfiles",
        "import/xbox360shell",
    ]
    assert row_for(report, "import/razercatearsremixmodelfiles")["archive"] == {
        "rel_path": "import/razercatearsremixmodelfiles.zip",
        "twin_verified": False,
    }
    assert row_for(report, "import/xbox360shell")["archive"] == {
        "rel_path": "import/xbox360shell.zip",
        "twin_verified": True,
    }, "names-only: the declared sizes differ but the basenames agree"


def test_service_a_failed_stem_verification_consumes_the_archive(isolated_storage: Path) -> None:
    """AC-3 tail: a failed stem check is the end of that archive, not a retry.

    ``outofmarbles.zip`` names the sibling ``xbox360shell`` by its entry root,
    so a fall-through to the entry-root route after the stem check fails WOULD
    pair ``xbox360shell``. The stem is the operator's own statement of which
    directory the archive is, and a failed check must never be retried under a
    weaker name: the archive stays with ``outofmarbles`` and is reported
    unverified, never attached to ``xbox360shell``.
    """
    root = isolated_storage / "import"
    write_file(root / "outofmarbles" / "marbles.stl", b"s" * 52)
    write_file(root / "outofmarbles" / "marbles.png", b"p" * 53)
    write_file(root / "xbox360shell" / "shell.stl", b"s" * 54)
    write_file(root / "xbox360shell" / "shell.png", b"p" * 55)
    write_zip(
        root / "outofmarbles.zip",
        {"xbox360shell/shell.stl": b"s" * 54, "xbox360shell/shell.png": b"p" * 55},
    )

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    assert [candidate.key for candidate in candidates] == [
        "import/outofmarbles",
        "import/xbox360shell",
    ]
    by_key = {candidate.key: candidate for candidate in candidates}
    assert by_key["import/outofmarbles"].archive_path == "import/outofmarbles.zip"
    assert by_key["import/xbox360shell"].archive_path is None, "never retried under the entry-root name"
    report = bundle_service.build_report()
    assert [row["key"] for row in report["bundles"]] == [
        "import/outofmarbles",
        "import/xbox360shell",
    ]
    assert row_for(report, "import/outofmarbles")["archive"] == {
        "rel_path": "import/outofmarbles.zip",
        "twin_verified": False,
    }
    assert row_for(report, "import/xbox360shell")["archive"] is None


def test_service_a_stem_twin_in_another_parent_pairs_nothing(isolated_storage: Path) -> None:
    """AC-4a: a stem twin is the directory BESIDE the archive, never a namesake deeper.

    ``import/Cinderwing3D - Crystal Dragon.zip`` names nothing in its own parent;
    the directory it matches sits under ``import/Deep``. Mutating
    ``_twin_directory`` to search every ancestor parent pairs them, so this test
    discriminates the same-parent bound of the stem route.

    The containing directory is the engine's own row and the archive is an
    ``archive-project`` row; neither is ATTACHED to the other. Under the same
    mutation the directory row gains
    ``archive.rel_path == "import/Cinderwing3D - Crystal Dragon.zip"`` and reds.
    """
    root = isolated_storage / "import"
    write_file(root / "Deep" / CINDERWING / "dragon.stl", b"s" * 56)
    write_file(root / "Deep" / CINDERWING / "dragon.png", b"p" * 57)
    write_zip(
        root / f"{CINDERWING}.zip",
        {f"{CINDERWING}/dragon.stl": b"s" * 56, f"{CINDERWING}/dragon.png": b"p" * 57},
    )

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    assert [candidate.key for candidate in candidates] == [
        "import/Deep",
        f"import/{CINDERWING}.zip",
    ], "a namesake under another parent is not the archive's twin"
    by_key = {candidate.key: candidate for candidate in candidates}
    assert by_key["import/Deep"].archive_path is None
    assert by_key[f"import/{CINDERWING}.zip"].is_archive is True
    report = bundle_service.build_report()
    assert [row["key"] for row in report["bundles"]] == [
        f"import/{CINDERWING}.zip",
        "import/Deep",
    ]
    row = row_for(report, "import/Deep")
    assert row["archive"] is None, "the zip under import/ is never attached to a namesake under import/Deep"
    assert row_for(report, f"import/{CINDERWING}.zip")["detector"] == "engine:archive-project"


def test_service_an_entry_root_naming_another_parent_pairs_nothing(isolated_storage: Path) -> None:
    """AC-4b: an entry root names a directory in the archive's OWN parent only.

    ``import/Gizmo.zip`` is rooted at ``xbox360_shell/``, but that directory sits
    under ``import/other``. Mutating ``_entry_root_pair`` to search every parent
    pairs them, so this test discriminates its same-parent bound.

    The containing directory is the engine's own row and the archive is an
    ``archive-project`` row; neither is ATTACHED to the other.
    """
    root = isolated_storage / "import"
    write_file(root / "other" / "xbox360_shell" / "shell.stl", b"s" * 58)
    write_file(root / "other" / "xbox360_shell" / "shell.png", b"p" * 59)
    write_zip(
        root / "Gizmo.zip",
        {"xbox360_shell/shell.stl": b"s" * 58, "xbox360_shell/shell.png": b"p" * 59},
    )

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    assert [candidate.key for candidate in candidates] == [
        "import/other",
        "import/Gizmo.zip",
    ], "the named directory is not a sibling of the archive"
    by_key = {candidate.key: candidate for candidate in candidates}
    assert by_key["import/other"].archive_path is None
    assert by_key["import/Gizmo.zip"].is_archive is True
    report = bundle_service.build_report()
    assert [row["key"] for row in report["bundles"]] == [
        "import/Gizmo.zip",
        "import/other",
    ]
    assert row_for(report, "import/other")["archive"] is None, (
        "an archive rooted at a same-named directory under another parent is never attached"
    )


def test_service_a_stem_twin_reports_the_names_only_answer(
    client: TestClient, isolated_storage: Path
) -> None:
    """The stem route keeps the names-only ``twin_verified`` at report time.

    The directory holds a ``spare.stl`` the flat archive does not, so the
    basename sets differ and the answer is ``False``: the operator paired them,
    and the mismatch is information rather than an assertion to raise
    (``test_the_twin_zip_is_provenance_only`` pins the matching case).
    """
    root = isolated_storage / "import"
    write_file(root / SNVD623 / "flat-a.stl", b"s" * 42)
    write_file(root / SNVD623 / "flat-b.stl", b"s" * 43)
    write_file(root / SNVD623 / "spare.stl", b"s" * 44)
    write_zip(root / f"{SNVD623}.zip", {"flat-a.stl": b"s" * 42, "flat-b.stl": b"s" * 43})
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    row = row_for(report, f"import/{SNVD623}")
    assert row["archive"] == {"rel_path": f"import/{SNVD623}.zip", "twin_verified": False}, (
        "basename set equality is FALSE here (the folder has spare.stl, the archive does not)"
    )


# --- duplicate archives (Mf-z4vv) -------------------------------------------

# The four constructed duplicate groups of the fixture below. The payload sizes
# are distinct so no group shares a size with another archive: only the copies
# inside a group are equal-size, which is exactly the pair that gets hashed.
DUPLICATE_GROUP_PAYLOADS = (("Big", 400), ("Medium", 300), ("Small", 200), ("Tiny", 100))
UNIQUE_PAYLOAD_SIZES = (550, 650, 750)


def bundle_zip_entries(model: bytes) -> Dict[str, bytes]:
    """A zip the R3 rule offers as a bundle: one model, one image, one text, at the root."""
    return {"part.stl": model, "preview.jpg": b"j" * 20, "notes.txt": b"print at 0.2mm\n"}


def archive_pair(storage: Path, stem: str, model: bytes) -> tuple:
    """One original archive plus a byte-identical browser copy, made with copyfile.

    A second ``write_zip`` would usually match too, but ``writestr`` timestamps can
    straddle a second boundary and a byte-identical fixture must not be flaky.
    """
    original = f"{stem}.zip"
    duplicate = f"{stem} (1).zip"
    write_zip(storage / original, bundle_zip_entries(model))
    shutil.copyfile(storage / original, storage / duplicate)
    return original, duplicate


def duplicate_library(storage: Path) -> Dict[str, tuple]:
    """Four duplicate groups, three unique-size zips and one one-byte-different pair."""
    groups = {name: archive_pair(storage, f"duplicates/{name}", b"s" * size) for name, size in DUPLICATE_GROUP_PAYLOADS}
    for index, size in enumerate(UNIQUE_PAYLOAD_SIZES):
        write_zip(storage / f"unique/Only {index}.zip", bundle_zip_entries(b"u" * size))
    write_zip(storage / "boundary/Diff A.zip", bundle_zip_entries(b"A" * 150))
    write_zip(storage / "boundary/Diff B.zip", bundle_zip_entries(b"B" * 150))
    return groups


def test_service_four_duplicate_groups_are_reported_once_with_byte_counts(isolated_storage: Path) -> None:
    """AC-1: four copies become four reclaimable groups, never eight bundle rows."""
    groups = duplicate_library(isolated_storage)

    report = bundle_service.build_report()

    assert len(report["duplicates"]) == 4
    rows = {row["key"] for row in report["bundles"]}
    sizes: List[int] = []
    for original, duplicate in groups.values():
        group = next(group for group in report["duplicates"] if group["paths"][0] == original)
        assert group["paths"] == [original, duplicate], "the shortest name is the authoritative original"
        assert group["reclaimable_bytes"] == group["size"] == (isolated_storage / original).stat().st_size
        assert len(group["sha256"]) == 64
        assert duplicate not in rows, "a byte-identical copy is never a second bundle row"
        assert original in rows, "the original is still a zip-only bundle"
        sizes.append(group["size"])
    assert len(set(sizes)) == 4, "each group has its own byte size, so no cross-group hashing"
    assert report["duplicates_total_bytes"] == sum(sizes)
    assert "keep\t" in report["duplicates_text"]
    assert "duplicate\t" in report["duplicates_text"]
    assert report["duplicates"][0]["sha256"] in report["duplicates_text"]
    assert "boundary/Diff A.zip" in rows and "boundary/Diff B.zip" in rows, (
        "one changed byte is not a duplicate, so both stay candidates"
    )


def test_service_a_one_byte_difference_is_not_a_duplicate_and_both_stay_candidates(
    isolated_storage: Path,
) -> None:
    """The boundary: equal stat size, one payload byte different, two bundles."""
    write_zip(isolated_storage / "boundary/Diff A.zip", bundle_zip_entries(b"A" * 150))
    write_zip(isolated_storage / "boundary/Diff B.zip", bundle_zip_entries(b"B" * 150))
    first = isolated_storage / "boundary/Diff A.zip"
    second = isolated_storage / "boundary/Diff B.zip"
    assert first.stat().st_size == second.stat().st_size, "the fixture must be equal-size for the test to mean anything"

    report = bundle_service.build_report()
    candidates, _truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert report["duplicates"] == []
    assert report["duplicates_total_bytes"] == 0
    assert {row["key"] for row in report["bundles"]} == {"boundary/Diff A.zip", "boundary/Diff B.zip"}
    assert {candidate.key for candidate in candidates} == {"boundary/Diff A.zip", "boundary/Diff B.zip"}


def test_service_unique_sizes_are_never_hashed(isolated_storage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Size-first: only the equal-size pair is read, never the uniquely-sized zips."""
    calls: List[Path] = []
    original = bundle_service._content_digest

    def counting_digest(path: Path) -> Optional[str]:
        calls.append(path)
        return original(path)

    monkeypatch.setattr(bundle_service, "_content_digest", counting_digest)
    archive_pair(isolated_storage, "duplicates/Big", b"s" * 400)
    for index, size in enumerate(UNIQUE_PAYLOAD_SIZES):
        write_zip(isolated_storage / f"unique/Only {index}.zip", bundle_zip_entries(b"u" * size))

    report = bundle_service.build_report()

    assert len(report["duplicates"]) == 1
    assert len(calls) == 2, "the pair only: a size that occurs once is never opened"


def test_service_all_equal_size_archives_are_hashed_once(isolated_storage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The honest worst case: four same-size archives are each read exactly once."""
    calls: List[Path] = []
    original = bundle_service._content_digest

    def counting_digest(path: Path) -> Optional[str]:
        calls.append(path)
        return original(path)

    monkeypatch.setattr(bundle_service, "_content_digest", counting_digest)
    names = [f"same/{letter}.zip" for letter in "ABCD"]
    # Same entry names and same payload lengths, different bytes: equal stat size,
    # four different hashes, so the size-first filter cannot exclude any of them.
    for index, name in enumerate(names):
        write_zip(
            isolated_storage / name,
            {
                "part.stl": bytes([65 + index]) * 150,
                "preview.jpg": b"j" * 20,
                "notes.txt": b"print at 0.2mm\n",
            },
        )
    assert len({(isolated_storage / name).stat().st_size for name in names}) == 1

    scan = bundle_service.scan_bundles(isolated_storage)

    assert len(calls) == 4
    assert scan.duplicates == ()
    assert {candidate.key for candidate in scan.candidates} == set(names), "all four are still candidates"


def test_service_a_duplicate_is_refused_by_create(isolated_storage: Path) -> None:
    """A copy is suppressed from both passes, so a create naming it is unknown."""
    _original, duplicate = archive_pair(isolated_storage, "duplicates/Big", b"s" * 400)

    created = bundle_service.create_bundles(
        [bundle_service.BundleCreateItem(key=duplicate)], audit_ctx()
    )

    assert created["failed"] == 1
    assert created["created"] == 0
    assert created["results"][0]["reason"] == "unknown bundle"
    assert table_counts()["projects"] == 0


# --- R4: an id in the name --------------------------------------------------


def write_dragon(storage: Path) -> Path:
    """The ticket's export: one model under ``files/`` and no preview, no text."""
    bundle = storage / "import" / DRAGON
    write_file(bundle / "files" / "ARTICULATED_ice_dragon_fixed_p1.stl", STL_BYTES)
    return bundle


def test_service_an_id_named_model_only_directory_is_offered_at_medium(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-1: the name buys a medium row with a derived URL and nothing else.

    Mutation: delete R4 from ``detect_bundle`` and the report is empty, so the
    row lookup itself reds. Mutation: stamp the row with a creator or a licence
    and the ``None`` assertions red - and the create pass must carry the derived
    URL through while still inventing no attribution.
    """
    write_dragon(isolated_storage)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == [f"import/{DRAGON}"]
    row = row_for(report, f"import/{DRAGON}")
    assert row["detector"] == "engine:one-project"
    assert row["metadata_shape"] == "one-project"
    assert row["source_url"] == DRAGON_URL
    assert row["creator"] is None
    assert row["licence"] is None
    assert row["licence_url"] is None
    assert row["status"] == "ready"
    assert report["counts"]["medium"] == 1
    assert report["counts"]["low"] == 0
    assert report["counts"]["high"] == 0
    assert report["counts"]["total"] == 1

    created = create(client, headers, [{"key": row["key"]}])

    assert created["created"] == 1
    project_id = created["results"][0]["project_id"]
    project = project_listing(client, headers)[project_id]
    assert project["creator"] is None
    assert project["licence"] is None
    assert project["licence_url"] is None
    assert project["source_url"] == DRAGON_URL, "the derived URL survives creation"
    assert member_count(project_id) == 1
    with Session(engine) as session:
        stored = session.exec(select(Project)).one()
    marker = json.loads(stored.metadata_json)["_bundle"]
    assert marker["detector"] == "engine:one-project"
    assert marker["confidence"] == row["confidence"]


def test_service_a_name_only_with_a_derived_url_is_not_sent_name_only_by_the_low_bar(
    client: TestClient, isolated_storage: Path
) -> None:
    """The bulk ``name_only: true`` path writes no provenance, medium included.

    This is the service-side companion to the UI pin: the bulk bar only takes
    rows whose confidence is exactly ``low``, because a ``medium`` row carries a
    derived URL that ``name_only`` would throw away. Mutation: widen the bulk
    bar to ``medium`` and the URL test below reds; the non-name-only create in
    the test above proves the URL is there to lose.
    """
    write_dragon(isolated_storage)
    scan()
    headers = auth_header(client)
    row = row_for(detect(client, headers), f"import/{DRAGON}")
    assert row["source_url"] == DRAGON_URL, "the row does carry a derived URL"

    created = create(client, headers, [{"key": row["key"], "name_only": True}])

    assert created["created"] == 1
    project = project_listing(client, headers)[created["results"][0]["project_id"]]
    assert project["source_url"] is None
    assert project["creator"] is None
    assert project["licence"] is None


def test_service_an_id_named_directory_with_its_stem_twin_is_one_medium_row(
    client: TestClient, isolated_storage: Path
) -> None:
    """One row: the directory is R4-detected, so the archive is provenance.

    The promotion pass skips every key already in ``found`` and R4's gate is the
    same ``_own_depth_model_count`` predicate, so an id-named directory is
    always R4-detected and never promoted. Mutation: let the promotion pass win
    - drop BOTH the ``twin in detected`` skip on the pairing pass and the
    ``found`` skip on the promotion pass - and a second, ``archive_owned``
    candidate appears for the same key, so this test sees two rows (the
    directory would then also read ``low``/``generic-bundle``) and fails.
    Removing either skip alone changes nothing here, because the other one
    still protects this row.
    """
    write_file(isolated_storage / "import" / DRAGON / "part.stl", STL_BYTES)
    write_zip(isolated_storage / "import" / f"{DRAGON}.zip", {f"{DRAGON}/part.stl": STL_BYTES})
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == [f"import/{DRAGON}"]
    row = row_for(report, f"import/{DRAGON}")
    assert row["detector"] == "engine:one-project"
    assert row["status"] == "ready"
    assert row["archive"] == {"rel_path": f"import/{DRAGON}.zip", "twin_verified": True}
    assert report["counts"]["medium"] == 1
    assert report["counts"]["low"] == 0

    created = create(client, headers, [{"key": row["key"]}])

    assert created["created"] == 1
    assert member_count(created["results"][0]["project_id"]) == 1, "the archive attaches nothing"


def test_service_an_id_named_zip_is_an_archive_only_medium_row(
    client: TestClient, isolated_storage: Path
) -> None:
    """``_zip_snapshot`` names an archive's snapshot from the zip's stem.

    R4 therefore sees an id-shaped name for an archive too: a widening from
    directories to archive snapshots, accepted and recorded in ADR 0010
    Decision 20, and harmless because a zip-only row can never create.
    Mutation: gate R4 on directories only (or drop the stem-name snapshot) and
    this zip reads ``generic-bundle``/``low`` with no URL, so the medium and
    ``source_url`` assertions red; the create stays blocked either way, which
    is exactly why the widening is harmless.
    """
    write_zip(isolated_storage / "import" / f"{DRAGON}.zip", {"part.stl": STL_BYTES})
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == [f"import/{DRAGON}.zip"]
    row = row_for(report, f"import/{DRAGON}.zip")
    assert row["confidence"] == "low"
    assert row["detector"] == "engine:archive-project"
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "archive_only"
    assert row["source_url"] == DRAGON_URL

    created = create(client, headers, [{"key": row["key"]}])

    assert created["blocked"] == 1
    assert project_listing(client, headers) == {}


# --- R5: a title-shaped folder name (Mf-hvck) ---------------------------------

# The eleven measured directories from the ticket: nine names that read like a
# title, two that say "a part of a thing". The counts are the measured handfuls;
# the shape under test is the NAME, so a couple of models each is enough.
TITLE_FOLDER_MODEL_COUNTS = {
    "Rocket Filement support": 5,
    "Stagetop": 2,
    "LittleTup": 4,
    "Taffi": 3,
    "GavinIsABot": 2,
    "mac deckbox": 2,
    "skull mask": 2,
    "printable carrage": 2,
    "Filament guide": 2,
}
TITLE_FOLDERS = tuple(TITLE_FOLDER_MODEL_COUNTS)
PART_FOLDERS = ("files", "source")


# --- a hand-made project owns a bundle by its files (Mf-cf3c) ----------------

# The owner's project over the Clockwork files, made by hand: no `_bundle`
# marker, a name the folder-derived suggestion never equals, and no source URL.
CLOCKWORK_PROJECT_NAME = "Fates End - ClockworkMonster Tower"


def clockwork_model_paths(root: str = CLOCKWORK_ROOT) -> List[str]:
    """The 12 storage paths of the Clockwork models, as the scan registers them."""
    return [f"import/{root}/Clockwork_Part{index:02d}.stl" for index in range(12)]


def hand_made_project(name: str, storage_paths: List[str]) -> str:
    """A project a human made over already-registered files, out of band.

    No ``_bundle`` marker and no source URL, because the app never touched it:
    asset containment is the only identity that can find it.
    """
    with Session(engine) as session:
        project = project_service.create_project(session, name, None, None, origin="own")
        project_id = project.id
    project_service.add_assets(project_id, [asset_id_at(path) for path in storage_paths])
    return project_id


class _PoisonedAssetIndex(dict):
    """A membership index that raises if anyone reads it: proof a path never did."""

    def get(self, key: object, default: object = None) -> object:
        raise AssertionError("membership index read on a marker match")

    def __getitem__(self, key: object) -> object:
        raise AssertionError("membership index read on a marker match")

    def __contains__(self, key: object) -> bool:
        raise AssertionError("membership index read on a marker match")


def test_service_a_hand_made_project_owns_a_bundle_by_its_files(isolated_storage: Path) -> None:
    """AC-1: the owner's Clockwork project is reported as this bundle's owner.

    The bundle is offered (R5 reads ``17_ClockworkMonster`` as a title), the
    hand-made project matches none of the three older identities, and the report
    still names it - matched by ``assets`` - without writing anything: the
    project keeps no ``_bundle`` marker, because a read route writes nothing.

    Mutation: drop the asset identity and ``existing_project_id`` goes None and
    the row counts as pending again; stamp the project with a marker and the
    match reads ``marker`` instead, so the ``assets`` assertion reds.
    """
    clockwork_directory(isolated_storage)
    scan()
    project_id = hand_made_project(CLOCKWORK_PROJECT_NAME, clockwork_model_paths())

    report = bundle_service.build_report()

    row = row_for(report, CLOCKWORK_DIR)
    assert row["existing_project_id"] == project_id
    assert row["existing_project_name"] == CLOCKWORK_PROJECT_NAME
    assert row["existing_project_match"] == "assets"
    assert row["status"] == "ready", "the asset identity never changes readiness"
    assert report["counts"]["already_created"] == 1
    with Session(engine) as session:
        stored = project_service.get_project(session, project_id)
    assert "_bundle" not in json.loads(stored.metadata_json or "{}"), (
        "the report only reads: the hand-made project is never stamped"
    )


def test_service_a_project_holding_the_models_owns_a_bundle_that_also_has_images(
    isolated_storage: Path,
) -> None:
    """The pinned reading of "every registered asset": models are the set.

    The bundle has 14 registered assets (12 models and 2 renders) and the project
    holds only the 12 models, and the bundle is STILL claimed. The app only ever
    attaches MODELS to a project (``_create_one``'s ``model_assets``), so literal
    full-set containment would report the owner's own project as not owning the
    bundle and AC-1 would be false in production.

    Mutation: build the containment set from ``own_assets`` instead of ``models``
    and the match goes None here while the project still holds every file a
    create would attach.
    """
    clockwork_directory(isolated_storage)
    scan()
    project_id = hand_made_project(CLOCKWORK_PROJECT_NAME, clockwork_model_paths())

    report = bundle_service.build_report()

    row = row_for(report, CLOCKWORK_DIR)
    assert row["assets_total"] == 14, "the 12 models and the 2 renders are registered"
    assert row["image_count"] == 2
    assert member_count(project_id) == 12, "the project holds only the models"
    assert row["existing_project_match"] == "assets"
    assert row["existing_project_id"] == project_id


def test_service_a_project_holding_some_of_the_files_does_not_claim_the_bundle(
    isolated_storage: Path,
) -> None:
    """AC-3: containment, not intersection - 11 of 12 models claims nothing.

    The project holds almost the whole bundle, which is exactly the shape a
    loosened rule would claim. The row stays pending: no existing project and no
    identity, so the operator still decides.

    Mutation: accept ``count >= 1`` (or compare against an intersection) and
    ``existing_project_id`` becomes this project, so the None assertions red.
    """
    clockwork_directory(isolated_storage)
    scan()
    hand_made_project(CLOCKWORK_PROJECT_NAME, clockwork_model_paths()[:11])

    report = bundle_service.build_report()

    row = row_for(report, CLOCKWORK_DIR)
    assert row["existing_project_id"] is None
    assert row["existing_project_match"] is None
    assert row["status"] == "ready", "the row still appears and is still creatable"
    assert report["counts"]["already_created"] == 0


def test_service_an_empty_asset_list_never_claims_a_bundle(isolated_storage: Path) -> None:
    """AC-4: an empty containment set owns nothing, and never a bundle without assets.

    An empty project exists and the Moai bundle is deliberately NOT scanned, so
    the row has zero registered assets. An empty wanted set must not degenerate
    to "the first project wins": the row reports no owner.

    Mutation: let ``_asset_owner`` answer for an empty wanted set and the empty
    project claims this bundle, so both the None assertions and the direct call
    red.
    """
    moai_bundle(isolated_storage)
    with Session(engine) as session:
        empty = project_service.create_project(session, "Empty Project", None, None, origin="own")
        projects = {empty.id: empty}
        index = project_create_service.project_assets_index(session)

    assert index == {}, "a project that holds nothing contributes no membership"
    assert project_create_service._asset_owner(index, (), projects) is None
    assert project_create_service._asset_owner(index, ("asset-with-no-project",), projects) is None, (
        "an asset id with no membership contributes nothing, so nothing is contained"
    )

    report = bundle_service.build_report()

    row = row_for(report, MOAI)
    assert row["assets_total"] == 0, "the row exists but the scan registered nothing under it"
    assert row["existing_project_id"] is None
    assert row["existing_project_match"] is None
    assert report["counts"]["already_created"] == 0


def test_service_the_four_identities_keep_their_precedence(isolated_storage: Path) -> None:
    """AC-5: marker, then source URL, then name, then assets - in that order.

    Each project below matches a weaker identity too, so every scenario would
    pass under a different order except the one it names: the source project
    also holds the asset, the named project also holds it, and the marker project
    matches the name and holds the asset. The poisoned index proves the marker
    path never even reads the membership index.

    Mutation: move any identity ahead of a stronger one and that scenario's
    ``match`` reds; move assets ahead of the marker and the poisoned index
    raises ``AssertionError`` instead of returning ``MATCH_MARKER``.
    """
    with Session(engine) as session:
        marked = project_service.create_project(
            session,
            "Fates End - ClockworkMonster Tower",
            None,
            None,
            origin="imported",
            metadata={"_bundle": {"key": "import/17_ClockworkMonster"}},
        )
        marked_id = marked.id
        sourced = project_service.create_project(
            session, "Sourced Thing", None, None, source_url=THING_URL, origin="imported"
        )
        sourced_id = sourced.id
        named = project_service.create_project(session, "Named Thing", None, None, origin="own")
        named_id = named.id
        holding = project_service.create_project(session, "Holding Thing", None, None, origin="own")
        holding_id = holding.id

    with Session(engine) as session:
        marker = project_create_service.find_existing_project(
            session,
            key="import/17_ClockworkMonster",
            source_url=THING_URL,
            name="Fates End - ClockworkMonster Tower",
            assets=("asset-marker",),
            asset_index=_PoisonedAssetIndex(),
        )
        source = project_create_service.find_existing_project(
            session,
            key="import/sourced",
            source_url=THING_URL,
            name="Sourced Thing",
            assets=("asset-source",),
            asset_index={"asset-source": (sourced_id,)},
        )
        by_name = project_create_service.find_existing_project(
            session,
            key="import/named",
            source_url=None,
            name="Named Thing",
            assets=("asset-name",),
            asset_index={"asset-name": (named_id,)},
        )
        by_assets = project_create_service.find_existing_project(
            session,
            key="import/assets",
            source_url=None,
            name="Nothing Like It",
            assets=("asset-holding",),
            asset_index={"asset-holding": (holding_id,)},
        )

    assert marker is not None and (marker.project.id, marker.match) == (
        marked_id,
        project_create_service.MATCH_MARKER,
    ), "the marker is proof and never reads the membership index"
    assert source is not None and (source.project.id, source.match) == (
        sourced_id,
        project_create_service.MATCH_SOURCE_URL,
    ), "a source URL outranks both the name and the shared files"
    assert by_name is not None and (by_name.project.id, by_name.match) == (
        named_id,
        project_create_service.MATCH_NAME,
    ), "a name outranks the shared files"
    assert by_assets is not None and (by_assets.project.id, by_assets.match) == (
        holding_id,
        project_create_service.MATCH_ASSETS,
    ), "with nothing stronger, shared files are the identity"


def test_service_one_project_asset_query_serves_every_candidate(isolated_storage: Path) -> None:
    """AC-6: one statement for the whole report, whatever the candidate count.

    Two candidates and then five: the count is the same, so the asset identity
    costs one query per report rather than one per bundle.

    Mutation: build the index inside ``_report_row`` and the count becomes one
    per candidate (2, then 5).
    """
    for index in range(2):
        thingiverse_bundle(isolated_storage, f"Two {index}", 1000 + index)
    scan()

    def project_asset_statements() -> int:
        statements: List[str] = []

        def record(conn, cursor, statement, parameters, context, executemany) -> None:
            if "project_asset" in statement.lower():
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            bundle_service.build_report()
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len(statements)

    assert project_asset_statements() == 1, "two candidates, one membership query"

    for index in range(2, 5):
        thingiverse_bundle(isolated_storage, f"Two {index}", 1000 + index)
    scan()

    assert len(bundle_service.build_report()["bundles"]) == 5
    assert project_asset_statements() == 1, "five candidates, still one membership query"


def test_service_the_clockwork_parent_is_promoted_and_reports_its_owner(isolated_storage: Path) -> None:
    """AC-7: the Mf-v3i3 pairing and the Mf-cf3c identity hold at once.

    The parent directory is promoted by its archive (the nested member is not a
    second row and neither zip is a row) AND that same row reports the hand-made
    project as its owner by ``assets``. Both must be true at once: the bundle
    must survive as the parent candidate before it can be reported as already in
    a project.

    Mutation: let the nested archive become a second row and the key list reds;
    drop the asset identity and the match goes None on the surviving row.
    """
    clockwork_pair(isolated_storage)
    scan()
    project_id = hand_made_project(CLOCKWORK_PROJECT_NAME, clockwork_model_paths())

    report = bundle_service.build_report()

    assert [row["key"] for row in report["bundles"]] == [CLOCKWORK_DIR], (
        "the promoted parent is the only bundle row: neither zip and no nested member"
    )
    row = row_for(report, CLOCKWORK_DIR)
    assert row["archive"] == {"rel_path": CLOCKWORK_ZIP, "twin_verified": True}
    assert row["existing_project_id"] == project_id
    assert row["existing_project_match"] == "assets"


def test_service_a_create_for_an_asset_owned_bundle_is_a_skip(isolated_storage: Path) -> None:
    """AC-8: the service-level create door refuses a second project over the files.

    A POST that dodges the UI is still refused: the create pass asks the same
    four identities, so the bundle is ``skipped``/``already_created`` with the
    hand-made project's id, its members are untouched and no second project row
    appears.

    Mutation: skip the asset identity in ``_create_one`` and a second project is
    created over the same 12 files, so the one-project and member-count
    assertions red.
    """
    clockwork_directory(isolated_storage)
    scan()
    project_id = hand_made_project(CLOCKWORK_PROJECT_NAME, clockwork_model_paths())
    members_before = member_count(project_id)

    created = bundle_service.create_bundles(
        [bundle_service.BundleCreateItem(key=CLOCKWORK_DIR)], audit_ctx()
    )

    assert created["created"] == 0
    assert created["skipped"] == 1
    assert created["results"][0]["status"] == "skipped"
    assert created["results"][0]["reason"] == "already_created"
    assert created["results"][0]["project_id"] == project_id
    assert member_count(project_id) == members_before == 12, "a skip attaches nothing"
    with Session(engine) as session:
        assert len(session.exec(select(Project)).all()) == 1, "no second project over the same files"


# --- R6: a single-root archive with models and images (Mf-tcrw) -------------

FATES_TOWER_ZIP = "03_Fates End - Barbarian Tower.zip"
FATES_TOWER_ROOT = "03_Barbarian"
# The measured `03_Fates End - Barbarian Tower.zip`, entry for entry: 14 models,
# 3 previews and the vendor's own nested deliverable zip under ONE root, no text.
FATES_TOWER_MODELS = (
    f"{FATES_TOWER_ROOT}/Barbarian_DT_v2.2.stl",
    f"{FATES_TOWER_ROOT}/Barbarian_Dec2_top_v2.2.stl",
    f"{FATES_TOWER_ROOT}/Barbarian_Dec_Roof_v1.0.stl",
    f"{FATES_TOWER_ROOT}/BarbarianTray_OpenLock_v1.0.stl",
    f"{FATES_TOWER_ROOT}/Barbarian_DT2_btm_v2.2.stl",
    f"{FATES_TOWER_ROOT}/Barbarian_DT2_top_v2.2.stl",
    f"{FATES_TOWER_ROOT}/BarbarianTray_Lg_Tx_v1.0.stl",
    f"{FATES_TOWER_ROOT}/BarbarianTray_DiceHolder_v1.0.stl",
    f"{FATES_TOWER_ROOT}/BarbarianTray_Lg_Smo_v1.0.stl",
    f"{FATES_TOWER_ROOT}/BarbarianTray_Sm_Tx_v1.0.stl",
    f"{FATES_TOWER_ROOT}/BarbarianTray_Sm_Smo_v1.0.stl",
    f"{FATES_TOWER_ROOT}/Barbarian_DT_roof_v1.1.stl",
    f"{FATES_TOWER_ROOT}/Barbarian_Dec_btm_v2.2.stl",
    f"{FATES_TOWER_ROOT}/Barbarian_Dec_v2.2.stl",
)
# The measured previews, largest first: the visual reference (273497 bytes) is
# the cover, then the render (183190) and the file-name key (24699).
FATES_TOWER_IMAGES = (
    f"{FATES_TOWER_ROOT}/Barbarian_VisualReference.png",
    f"{FATES_TOWER_ROOT}/Barbarian_RENDER.png",
    f"{FATES_TOWER_ROOT}/_FileName_Key.png",
)
FATES_TOWER_NESTED_ZIP = f"{FATES_TOWER_ROOT}/03_Barbarian_2.2.zip"
DICE_JAIL_ROOTS = ("DJ5_ChairOFShame", "DJ6_IronMaiden", "DJ7_BagOfHolding", "DJ8_Altar")


def fates_tower_entries() -> Dict[str, bytes]:
    """The measured Fates End tower: 14 models, 3 previews and the nested zip, no text.

    The names and the entry set are the measured ones and the preview payloads
    carry their measured byte counts, so the cover is decided by real sizes. The
    model and nested-zip payloads are placeholders: the pure layer pins the exact
    measured quantities, and writing 250 MB per model into a temp zip would test
    the disk, not the rule.
    """
    entries = {path: b"s" * (40 + index) for index, path in enumerate(FATES_TOWER_MODELS)}
    entries[FATES_TOWER_IMAGES[0]] = b"j" * 273497
    entries[FATES_TOWER_IMAGES[1]] = b"j" * 183190
    entries[FATES_TOWER_IMAGES[2]] = b"j" * 24699
    entries[FATES_TOWER_NESTED_ZIP] = b"z" * 64
    return entries


def multi_root_entries() -> Dict[str, bytes]:
    """Fates End Dice Jails 5-8: models and an image under FOUR sibling roots."""
    entries: Dict[str, bytes] = {}
    for jail in DICE_JAIL_ROOTS:
        entries[f"{jail}/part-a.stl"] = b"s" * 40
        entries[f"{jail}/part-b.stl"] = b"s" * 41
        entries[f"{jail}/preview.jpg"] = b"j" * 50
    return entries


def refusal_rules(storage: Path) -> Dict[str, str]:
    """Every refusal of one scan, keyed by path, for a readable assertion."""
    return {
        item.rel_path: item.rule
        for item in bundle_service.scan_bundles(storage).refusals
    }


def member_refusals_name_a_real_row(storage: Path) -> List[str]:
    """The ``member-of-bundle`` entries that name nothing - an empty list is the pass.

    The word says "a bundle the operator was OFFERED owns this path". A path that is
    neither a row nor strictly inside a row is owned by nobody, so naming it is a lie:
    the payload has no row, and the ledger points at one that does not exist. That is
    exactly what B-1 produced - a promotion dissolved its child eagerly, a later prune
    vetoed the promotion, and both rows went away - so the promotion's dissolution is
    committed after that prune and this helper is what pins it across a scan. Every
    promotion witness calls it; the general shape is asserted once here rather than
    restated per tree.
    """
    scan = bundle_service.scan_bundles(storage)
    offered_keys = {candidate.key for candidate in scan.candidates}
    offered_dirs = {candidate.key for candidate in scan.candidates if not candidate.is_archive}
    return [
        item.rel_path
        for item in scan.refusals
        if item.rule == bundle_service.REFUSAL_MEMBER_OF_BUNDLE
        and item.rel_path not in offered_keys
        and not any(item.rel_path.startswith(directory + "/") for directory in offered_dirs)
    ]


def test_service_a_single_root_archive_without_a_readme_is_a_low_row(isolated_storage: Path) -> None:
    """AC-1: the Fates End tower becomes a row through ``build_report``, not only a scan.

    The archive has 14 models, 3 previews and the vendor's nested deliverable zip
    under one root and no text, so R3 refuses it for want of a document and R6
    offers it. The nested zip is an entry of the archive's listing, never a member
    of a bundle directory, so it must not move the outcome. ``build_report`` is the
    witness that matters: the row is rendered by ``_report_row`` from the archive
    engine's own facts, the same reading the walk used.

    Every provenance field is ``None`` and the row is ``blocked`` /
    ``archive_only``, so the rule can never create a project on a guess.
    """
    write_zip(isolated_storage / FATES_TOWER_ZIP, fates_tower_entries())

    report = bundle_service.build_report()
    row = row_for(report, FATES_TOWER_ZIP)

    assert row["detector"] == "engine:archive-project"
    assert row["metadata_shape"] == "archive-project"
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "archive_only"
    assert (row["model_count"], row["image_count"], row["text_count"]) == (14, 3, 0)
    assert row["title"] == "Fates End - Barbarian Tower"
    assert (row["creator"], row["licence"], row["licence_url"], row["source_url"]) == (
        None,
        None,
        None,
        None,
    )
    assert row["cover"]["rel_path"] is None, "an archive row's facts carry no cover at all"


def test_service_a_matching_archive_beside_an_offered_twin_stays_consumed(isolated_storage: Path) -> None:
    """AC-2: a same-stem directory some rule already offers owns the row.

    The directory carries its own text file, so R3 offers it; the archive beside it
    has the identical payload without the text and would qualify for R6. The
    archive is the directory's provenance and is ``twin-consumed`` - never a
    second row. Mutation: let R6 decide before the twin guard and the pair becomes
    two rows.
    """
    name = "Barbarian Tower"
    write_file(isolated_storage / name / "Barbarian" / "part-a.stl", STL_BYTES)
    write_file(isolated_storage / name / "Barbarian" / "part-b.stl", STL_BYTES)
    write_file(isolated_storage / name / "Barbarian" / "preview.jpg", b"j" * 50)
    write_text(isolated_storage / name / "notes.txt", "the directory's own text")
    write_zip(
        isolated_storage / f"{name}.zip",
        {
            "Barbarian/part-a.stl": STL_BYTES,
            "Barbarian/part-b.stl": STL_BYTES,
            "Barbarian/preview.jpg": b"j" * 50,
        },
    )

    scan = bundle_service.scan_bundles(isolated_storage)

    assert [candidate.key for candidate in scan.candidates] == [name], "one row, keyed at the directory"
    assert row_for(bundle_service.build_report(), name)["detector"] == "engine:one-project"
    assert refusal_rules(isolated_storage)[f"{name}.zip"] == bundle_service.REFUSAL_TWIN_CONSUMED


def test_service_an_archive_rooted_at_an_offered_sibling_is_consumed_by_it(isolated_storage: Path) -> None:
    """AC-3: the entry root names the sibling directory, so the archive is provenance.

    ``17_Fates End - ClockworkMonster Tower.zip`` roots its entries at
    ``17_ClockworkMonster``, and that directory is offered (promoted by the
    verified pair). No archive row exists and the archive is ``twin-consumed``.
    Mutation: let ``archive_bundle_decision`` bypass pairing and the same bytes
    become a second row.
    """
    clockwork_directory(isolated_storage)
    write_zip(isolated_storage / CLOCKWORK_ZIP, clockwork_entries())

    scan = bundle_service.scan_bundles(isolated_storage)

    assert [candidate.key for candidate in scan.candidates] == [CLOCKWORK_DIR]
    assert refusal_rules(isolated_storage)[CLOCKWORK_ZIP] == bundle_service.REFUSAL_TWIN_CONSUMED


def test_service_byte_identical_matching_archives_yield_one_row_and_one_duplicate(
    isolated_storage: Path,
) -> None:
    """AC-4: the browser's `` (1)`` copy is a duplicate, never a second R6 row.

    ``copyfile`` rather than a second ``writestr`` keeps the two archives
    byte-identical by construction. Mutation: skip the duplicate suppression in
    the zip-only pass and the pair renders two rows for the same bytes.
    """
    original = isolated_storage / "import" / FATES_TOWER_ZIP
    duplicate = isolated_storage / "import" / "03_Fates End - Barbarian Tower (1).zip"
    write_zip(original, fates_tower_entries())
    shutil.copyfile(original, duplicate)

    scan = bundle_service.scan_bundles(isolated_storage)
    keys = {candidate.key for candidate in scan.candidates}

    assert keys == {"import/" + FATES_TOWER_ZIP}, "at most one row for the pair"
    assert refusal_rules(isolated_storage)["import/" + duplicate.name] == bundle_service.REFUSAL_DUPLICATE


def test_service_a_matching_zip_inside_a_bundle_is_still_a_member(isolated_storage: Path) -> None:
    """AC-5: Mf-74x5 is unchanged - a nested archive is a member, not an R6 row.

    The enclosing directory is the real Clockwork bundle (a verified pairing owns
    it, so the container strip never targets it) and the Fates-shaped zip inside it
    is refused ``nested-member``. Mutation: exempt archive-only rows from
    membership and the nested zip is offered as a second, uncreatable row.
    """
    clockwork_pair(isolated_storage)
    nested = f"{CLOCKWORK_DIR}/{FATES_TOWER_ZIP}"
    write_zip(isolated_storage / nested, fates_tower_entries())

    scan = bundle_service.scan_bundles(isolated_storage)

    assert [candidate.key for candidate in scan.candidates] == [CLOCKWORK_DIR]
    assert refusal_rules(isolated_storage)[nested] == bundle_service.REFUSAL_NESTED_MEMBER


def test_service_every_archive_is_offered_or_refused_with_a_named_rule(isolated_storage: Path) -> None:
    """AC-7: the Mf-9ygi ledger invariant holds with the new rows in the mix.

    Every ``.zip`` in the tree is either an offered candidate or a refused entry
    whose rule is in the shared vocabulary, and no path is both. The mix covers an
    R6 row, a duplicate, a model-only refusal and a multi-root refusal. Mutation:
    let a refusal also decide a row (the ledger becomes an input) or add a rule
    word outside ``REFUSAL_RULES`` and one assertion reds.
    """
    write_zip(isolated_storage / "fates" / FATES_TOWER_ZIP, fates_tower_entries())
    write_zip(isolated_storage / "fates" / "models-only.zip", {"part.stl": STL_BYTES, "part2.stl": b"s" * 41})
    write_zip(isolated_storage / "fates" / "four-roots.zip", multi_root_entries())
    original = isolated_storage / "pair" / "Original.zip"
    write_zip(original, fates_tower_entries())
    shutil.copyfile(original, isolated_storage / "pair" / "Original (1).zip")

    scan = bundle_service.scan_bundles(isolated_storage)
    all_zips = {str(path.relative_to(isolated_storage)) for path in isolated_storage.rglob("*.zip")}
    offered = {candidate.key for candidate in scan.candidates if candidate.is_archive}
    refused = {item.rel_path for item in scan.refusals if item.kind == "archive"}

    assert offered | refused == all_zips, "no archive is invisible"
    assert not (offered & refused), "no archive is both a row and a refusal"
    for item in scan.refusals:
        assert item.rule in bundle_service.REFUSAL_RULES, (item.rel_path, item.rule)


# --- enrichment: what the report applies and what the action fetches (Mf-nivh) ---

PAGE_FIXTURE_DIR = Path(__file__).parent.joinpath("fixtures/thingiverse")
DRAGON_CACHE_KEY = "thingiverse:5390184"
MOAI_CACHE_KEY = "thingiverse:2493386"
# A fixed stamp: the report is clock-free, so the freshness of a record never
# changes what a report shows and this literal can be used on either side.
STALE_STAMP = "2020-01-01T00:00:00+00:00"
CACHED_STAMP = "2026-11-05T00:00:00+00:00"
CC_BY_NC_SA_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"


def page_html(name: str) -> str:
    """The real captured page head from this module's own fixture tree."""
    return json.loads(PAGE_FIXTURE_DIR.joinpath(name).read_text())["html"]


def fresh_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def page_record(
    source_url: str = DRAGON_URL,
    fetched_at: str = CACHED_STAMP,
    creator: Optional[str] = "P1lotz",
    licence: Optional[str] = "CC BY-NC-SA",
    licence_url: Optional[str] = CC_BY_NC_SA_URL,
) -> dict:
    """A cache record in the shipped shape (``bundle_enrich.record_from_page``)."""
    return {
        "source_url": source_url,
        "fetched_at": fetched_at,
        "creator": creator,
        "licence": licence,
        "licence_url": licence_url,
    }


def store_enrichments(records: Mapping[str, dict]) -> None:
    settings_service.set_setting(settings_service.BUNDLE_ENRICHMENTS_KEY, json.dumps(dict(records), sort_keys=True))


def enrichments() -> dict:
    return bundle_service.get_enrichments()


def spy_settings_writes(monkeypatch: pytest.MonkeyPatch) -> List[tuple]:
    """Record every ``set_setting`` call so "one write per request" is countable."""
    writes: List[tuple] = []
    original = settings_service.set_setting

    def spy(key: str, value: str, **kwargs) -> None:
        writes.append((key, value))
        original(key, value, **kwargs)

    monkeypatch.setattr(settings_service, "set_setting", spy)
    return writes


def stub_thing_page(monkeypatch: pytest.MonkeyPatch, html: str) -> List[str]:
    """Replace the fetch the service imports INSIDE ``enrich_bundles``; return its calls."""
    calls: List[str] = []

    def fake_fetch(thing_id: str, thingiverse_cookie: Optional[str]) -> str:
        calls.append(thing_id)
        return html

    monkeypatch.setattr(import_resolvers, "fetch_thingiverse_thing_page", fake_fetch)
    return calls


def test_a_report_applies_a_cached_record_to_empty_fields(isolated_storage: Path) -> None:
    """AC-1: a cached page fills the empty creator, licence and licence URL of a row.

    The 5390184 export names its own thing in its directory name and carries no
    documents, so the row starts empty; the record is what makes it attributable.

    Mutation: read the record per row or skip ``fills`` and the three values red;
    stamp an ``enrichment`` block when nothing was taken and the block assertion
    reds. Detection is untouched: detector, confidence and source_url stay.
    """
    write_dragon(isolated_storage)
    scan()
    store_enrichments({DRAGON_CACHE_KEY: page_record()})

    row = row_for(bundle_service.build_report(), f"import/{DRAGON}")

    assert row["creator"] == "P1lotz"
    assert row["licence"] == "CC BY-NC-SA"
    assert row["licence_url"] == CC_BY_NC_SA_URL
    assert row["enrichment"] == {
        "source_url": DRAGON_URL,
        "fetched_at": CACHED_STAMP,
        "fields": ["creator", "licence", "licence_url"],
    }
    assert row["detector"] == "engine:one-project"
    assert row["source_url"] == DRAGON_URL
    assert row["status"] == "ready"


def test_a_stale_record_still_fills_the_report(isolated_storage: Path) -> None:
    """AC-1b: the TTL gates only the enrich ACTION, never what the report shows.

    The 5390184 row starts empty, so an eight-day-old record is the only thing
    that can attribute it, and the report applies it all the same. Mutation: let
    ``_report_row`` honour ``record_is_fresh`` and this reds - the silent
    consequence would be a seven-day-old page suddenly supplying nothing to
    reports OR to creates.
    """
    write_dragon(isolated_storage)
    scan()
    stale_stamp = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
    store_enrichments({DRAGON_CACHE_KEY: page_record(fetched_at=stale_stamp)})

    row = row_for(bundle_service.build_report(), f"import/{DRAGON}")

    assert row["creator"] == "P1lotz"
    assert row["licence"] == "CC BY-NC-SA"
    assert row["enrichment"]["fields"], "the stale page still supplied this row"


def test_a_record_that_supplies_nothing_here_adds_no_block(isolated_storage: Path) -> None:
    """A record that filled none of THIS row's gaps contributes no provenance block.

    The Moai row already carries its README creator and licence, so the record
    under its key - which says something, just nothing this row needed - may not
    be stamped as if it had. Mutation: stamp ``enrichment`` unconditionally and
    this reds; on the web side that would flip the row from ``canEnrich`` to
    ``canRecheck`` for a page that never gave it anything.
    """
    moai_bundle(isolated_storage)
    scan()
    store_enrichments({MOAI_CACHE_KEY: page_record(source_url=THING_URL)})

    row = row_for(bundle_service.build_report(), MOAI)

    assert row["creator"] == "moai_maker", "the detector's creator is not the record's to replace"
    assert row["licence"] == "CC BY-NC", "the detector's licence is not the record's to replace"
    assert row["enrichment"] is None, "a record that supplied nothing here has no block to add"


def test_a_readme_creator_keeps_the_page_out_of_its_field(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2: a value the documents already state is never overwritten; only the gap is filled.

    The seam that builds the row is narrowed here: the licence is dropped from
    the engine's display fields while the README creator, title and URL stay real.

    Mutation: let the record win over a truthy row value and the creator changes
    to P1lotz; keep the licence/licence_url pair apart and only one of the two
    reds.
    """
    moai_bundle(isolated_storage)
    real_row_facts = bundle_service._row_facts

    def licence_less(candidate: bundle_service.BundleCandidate) -> dict:
        display = real_row_facts(candidate)
        display["licence"] = None
        display["licence_url"] = None
        return display

    monkeypatch.setattr(bundle_service, "_row_facts", licence_less)
    scan()
    store_enrichments({"thingiverse:2493386": page_record(source_url=THING_URL)})

    row = row_for(bundle_service.build_report(), MOAI)

    assert row["confidence"] == "high"
    assert row["creator"] == "moai_maker", "the README's creator is not the page's to replace"
    assert row["licence"] == "CC BY-NC-SA"
    assert row["licence_url"] == CC_BY_NC_SA_URL
    assert row["enrichment"]["fields"] == ["licence", "licence_url"]


def test_a_report_opens_no_socket_and_writes_no_cache(isolated_storage: Path) -> None:
    """AC-3: the report is a read, even when a row wants enrichment.

    The corpus holds a row that WOULD be enriched (5390184) beside an ordinary
    README bundle, so "no fetch" is meaningful next to a row that wants one, and
    the cached creator still arrives - the report ran, it just did not call out.

    Mutation: move the fetch into ``_report_row`` (or read the page "while we are
    here") and every tripwire reds.
    """

    def refuse(*args, **kwargs):
        raise AssertionError("the report must not open a socket or write a cache")

    write_dragon(isolated_storage)
    moai_bundle(isolated_storage)
    scan()
    store_enrichments({DRAGON_CACHE_KEY: page_record()})
    # A context, not the fixture's own monkeypatch: the autouse fixture's
    # ``db.STORAGE``/``bundle_service.STORAGE`` patches must survive, and its own
    # teardown writes a setting after the tripwire has to be gone.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket, "socket", refuse)
        mp.setattr(socket, "getaddrinfo", refuse)
        mp.setattr(import_resolvers, "safe_urlopen", refuse)
        mp.setattr(settings_service, "set_setting", refuse)
        report = bundle_service.build_report()

    assert sorted(row["key"] for row in report["bundles"]) == sorted([f"import/{DRAGON}", MOAI])
    assert row_for(report, f"import/{DRAGON}")["creator"] == "P1lotz"


def test_the_report_layer_imports_nothing_that_can_open_a_socket() -> None:
    """AC-4 (structural): the module-scope imports are a frozen, socket-free set.

    This is the structural half; ``test_a_report_opens_no_socket_and_writes_no_cache``
    is the behavioural half and neither is trusted alone - an import can be added
    without a report calling it, and a call can be added behind an attribute walk
    that this file would not see.

    Mutation: add ``import import_resolvers`` (or ``socket``/``urllib``) at the
    top of ``bundle_service`` and the subset assertion reds; delete the fetch by
    deleting its function-local import and the last assertion reds, so the pin
    cannot be satisfied by removing the call it protects.
    """
    allowed = frozenset(
        {
            "asset_urls",
            "audit",
            "bundle_drift",
            "bundle_enrich",
            "dataclasses",
            "datetime",
            "db",
            "fastapi",
            "file_utils",
            "hashlib",
            "json",
            "library_scan",
            "models",
            "pathlib",
            "project_service",
            "settings_service",
            "sqlalchemy",
            "sqlmodel",
            "typing",
            "zip_service",
            "zipfile",
            # Mf-yhjo: the dedupe and the create moved to their own module, which
            # imports `project_service` and nothing that can reach the network. It
            # is listed because it is a module-level import now; the assertion this
            # test exists for - no socket, no resolver, and `fetch_thingiverse_thing_page`
            # reachable only from `enrich_bundles` - is unchanged.
            "project_create_service",
            # Mf-yitm: the read-only proposal engine. Stdlib only, and its ONE
            # subprocess is behind `probe_git=` and is bounded by `git_timeout`
            # (Mf-1agz lifted `_engine_dissent`'s unconditional False: the report DOES
            # probe git now, behind a 5 s budget, because the label corpus needs it).
            # What this test still forbids is a module that can open a SOCKET.
            "proposal_engine",
        }
    )
    forbidden = {"import_resolvers", "safe_http", "socket", "urllib", "http"}

    tree = ast.parse(Path(bundle_service.__file__).read_text())
    module_imports = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_imports.add(node.module.split(".")[0])

    assert module_imports <= allowed, module_imports - allowed
    assert not (module_imports & forbidden), module_imports & forbidden
    assert "fetch_thingiverse_thing_page" in _local_imports(tree, "enrich_bundles")


def _local_imports(tree: ast.Module, function_name: str) -> set:
    """Every name ``function_name``'s own body imports, nested blocks included.

    Both spellings count: ``from import_resolvers import x`` and a bare
    ``import import_resolvers`` reach the same module.
    """
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            names: set = set()
            for inner in ast.walk(node):
                if isinstance(inner, ast.ImportFrom) and inner.module == "import_resolvers":
                    names.update(alias.name for alias in inner.names)
                elif isinstance(inner, ast.Import):
                    names.update(alias.name for alias in inner.names if alias.name == "import_resolvers")
            return names
    raise AssertionError(f"no function named {function_name} in bundle_service")


def _imports_import_resolvers(node: ast.AST) -> bool:
    """Whether ``node``'s subtree reaches ``import_resolvers`` by either import form."""
    for inner in ast.walk(node):
        if isinstance(inner, ast.ImportFrom) and (inner.module or "").split(".")[0] == "import_resolvers":
            return True
        if isinstance(inner, ast.Import) and any(alias.name.split(".")[0] == "import_resolvers" for alias in inner.names):
            return True
    return False


def test_the_report_route_is_not_where_the_fetch_lives() -> None:
    """AC-13: exactly one function in this module reaches for ``import_resolvers``.

    Mutation: call the fetch from ``_report_row``, ``_report_from_scan`` or
    ``build_report`` (by moving or copying the import) and the set grows.
    """
    tree = ast.parse(Path(bundle_service.__file__).read_text())
    importers = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef) and _imports_import_resolvers(node)
    }

    assert importers == {"enrich_bundles"}


def test_enrich_fills_and_reports_what_it_took(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5: one click reads one page, caches it once and the next report shows it.

    Mutation: return the row's values instead of the page's and the result reds;
    write per item and the write count reds; skip the cache and the report after
    the call still has an empty creator.
    """
    write_dragon(isolated_storage)
    scan()
    writes = spy_settings_writes(monkeypatch)
    calls = stub_thing_page(monkeypatch, page_html("thing-5390184.json"))

    result = bundle_service.enrich_bundles([f"import/{DRAGON}"], cookie=None, refresh=False, ctx=audit_ctx())

    assert calls == ["5390184"]
    assert (result["fetched"], result["cached"], result["refused"], result["unavailable"], result["unknown"]) == (
        1,
        0,
        0,
        0,
        0,
    )
    item = result["results"][0]
    assert item["key"] == f"import/{DRAGON}"
    assert item["status"] == "fetched"
    assert item["source_url"] == DRAGON_URL
    assert (item["creator"], item["licence"], item["licence_url"]) == (
        "P1lotz",
        "CC BY-NC-SA",
        CC_BY_NC_SA_URL,
    )
    assert item["filled"] == ["creator", "licence", "licence_url"]
    assert item["reason"] is None
    assert datetime.fromisoformat(item["fetched_at"]).tzinfo is not None

    assert len(writes) == 1, "one write for the whole request"
    key, value = writes[0]
    assert key == settings_service.BUNDLE_ENRICHMENTS_KEY
    assert json.loads(value)[DRAGON_CACHE_KEY]["creator"] == "P1lotz"
    assert json.loads(value)[DRAGON_CACHE_KEY]["fetched_at"] == item["fetched_at"]

    row = row_for(bundle_service.build_report(), f"import/{DRAGON}")
    assert row["creator"] == "P1lotz"
    assert row["enrichment"]["fields"] == ["creator", "licence", "licence_url"]


def test_the_enrich_counters_are_exactly_the_status_vocabulary(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counters and ``bundle_enrich.ENRICH_STATUSES`` are one vocabulary, two files.

    The empty request's response names every counter the action can report, and a
    real answer may only use a status the vocabulary holds. Mutation: add a
    status constant without wiring its counter, or wire a counter the vocabulary
    does not name, and one of the two assertions reds here.
    """
    empty = bundle_service.enrich_bundles([], cookie=None, refresh=False, ctx=audit_ctx())

    assert set(empty) - {"results"} == set(bundle_enrich.ENRICH_STATUSES)

    write_dragon(isolated_storage)
    scan()
    stub_thing_page(monkeypatch, page_html("thing-5390184.json"))

    result = bundle_service.enrich_bundles([f"import/{DRAGON}"], cookie=None, refresh=False, ctx=audit_ctx())

    assert {item["status"] for item in result["results"]} <= bundle_enrich.ENRICH_STATUSES


def test_enrich_does_not_refetch_a_fresh_record(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6: a fresh record answers instead of a fetch, and writes nothing.

    Mutation: compare the TTL the wrong way round (or skip ``record_is_fresh``)
    and the fetch stub raises; drop the cached branch's ``filled`` and the
    reporting assertion reds.
    """
    write_dragon(isolated_storage)
    scan()
    stamp = fresh_stamp()
    store_enrichments({DRAGON_CACHE_KEY: page_record(fetched_at=stamp)})
    writes = spy_settings_writes(monkeypatch)

    def must_not_fetch(thing_id: str, thingiverse_cookie: Optional[str]) -> str:
        raise AssertionError("must not fetch a fresh record")

    monkeypatch.setattr(import_resolvers, "fetch_thingiverse_thing_page", must_not_fetch)

    result = bundle_service.enrich_bundles([f"import/{DRAGON}"], cookie=None, refresh=False, ctx=audit_ctx())

    assert result["cached"] == 1
    assert result["fetched"] == 0
    item = result["results"][0]
    assert item["status"] == "cached"
    assert item["filled"] == ["creator", "licence", "licence_url"]
    assert item["fetched_at"] == stamp
    assert writes == [], "a cached answer caches nothing"


def test_enrich_refetches_a_stale_record(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-7a: past the TTL the page is read again, so a vendor edit is eventually seen."""
    write_dragon(isolated_storage)
    scan()
    store_enrichments({DRAGON_CACHE_KEY: page_record(fetched_at=STALE_STAMP)})
    writes = spy_settings_writes(monkeypatch)
    calls = stub_thing_page(monkeypatch, page_html("thing-5390184.json"))

    result = bundle_service.enrich_bundles([f"import/{DRAGON}"], cookie=None, refresh=False, ctx=audit_ctx())

    assert calls == ["5390184"]
    assert result["fetched"] == 1
    assert len(writes) == 1
    assert json.loads(writes[0][1])[DRAGON_CACHE_KEY]["fetched_at"] != STALE_STAMP


def test_enrich_refresh_reaches_past_a_fresh_record(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-7b: ``refresh=True`` is the operator's "ask again now", TTL or not."""
    write_dragon(isolated_storage)
    scan()
    store_enrichments({DRAGON_CACHE_KEY: page_record(fetched_at=fresh_stamp())})
    calls = stub_thing_page(monkeypatch, page_html("thing-5390184.json"))

    result = bundle_service.enrich_bundles([f"import/{DRAGON}"], cookie=None, refresh=True, ctx=audit_ctx())

    assert calls == ["5390184"]
    assert result["fetched"] == 1


def test_enrich_reports_unknown_and_unavailable_without_a_fetch(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-8: a key that is not a live bundle and a row with no vendor id never leave the server.

    Mutation: look the key up in the request instead of a fresh report, or build
    a cache key from a URL-less row, and one of the two fetch assertions reds.
    """
    generic_bundle(isolated_storage, "Generic Thing")
    scan()
    writes = spy_settings_writes(monkeypatch)

    def must_not_fetch(thing_id: str, thingiverse_cookie: Optional[str]) -> str:
        raise AssertionError("neither key names a page to read")

    monkeypatch.setattr(import_resolvers, "fetch_thingiverse_thing_page", must_not_fetch)

    result = bundle_service.enrich_bundles(
        ["no/such/bundle", "Generic Thing"], cookie=None, refresh=False, ctx=audit_ctx()
    )

    assert (result["unknown"], result["unavailable"]) == (1, 1)
    assert (result["fetched"], result["cached"], result["refused"]) == (0, 0, 0)
    assert [item["status"] for item in result["results"]] == ["unknown", "unavailable"]
    assert result["results"][0]["reason"] == "not a live bundle"
    assert result["results"][1]["reason"] == "this bundle names no vendor id, so there is no page to read"
    assert writes == [], "nothing fetched, nothing cached"


def test_a_page_without_metadata_is_refused_and_not_cached(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-9: a 200 page with no ld+json is the measured "missing thing" answer.

    Mutation: treat the empty parse as a success and the row gains a blank
    record (and the write assertion reds); cache it and a later report would
    claim a page supplied nothing.
    """
    write_dragon(isolated_storage)
    scan()
    writes = spy_settings_writes(monkeypatch)
    stub_thing_page(monkeypatch, page_html("thing-missing.json"))

    result = bundle_service.enrich_bundles([f"import/{DRAGON}"], cookie=None, refresh=False, ctx=audit_ctx())

    assert result["refused"] == 1
    item = result["results"][0]
    assert item["status"] == "refused"
    assert "5390184" in item["reason"]
    assert "no creator or licence" in item["reason"]
    assert writes == []
    row = row_for(bundle_service.build_report(), f"import/{DRAGON}")
    assert (row["creator"], row["licence"], row["licence_url"]) == (None, None, None)


def test_one_vendor_failure_does_not_discard_the_other_answers(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5b: a vendor refusal is a per-item status, so the batch keeps its other answers.

    Mutation: let the first failure raise and the whole batch is a 500 with no
    cache write - the page that DID answer is discarded with the one that did not.
    """
    write_dragon(isolated_storage)
    write_file(isolated_storage / "import" / "62536_standing_cat" / "files" / "cat.stl", STL_BYTES)
    scan()
    writes = spy_settings_writes(monkeypatch)
    page = page_html("thing-5390184.json")

    def fake_fetch(thing_id: str, thingiverse_cookie: Optional[str]) -> str:
        if thing_id == "62536":
            raise HTTPException(
                status_code=502,
                detail=(
                    "Thingiverse answered HTTP 503 for thing 62536, which this action does not read. "
                    "Nothing was changed."
                ),
            )
        return page

    monkeypatch.setattr(import_resolvers, "fetch_thingiverse_thing_page", fake_fetch)

    result = bundle_service.enrich_bundles(
        ["import/62536_standing_cat", f"import/{DRAGON}"], cookie=None, refresh=False, ctx=audit_ctx()
    )

    assert (result["fetched"], result["refused"]) == (1, 1)
    refused, fetched = result["results"]
    assert refused["key"] == "import/62536_standing_cat"
    assert refused["status"] == "refused"
    assert "HTTP 503" in refused["reason"] and "62536" in refused["reason"]
    assert fetched["key"] == f"import/{DRAGON}"
    assert fetched["status"] == "fetched"
    assert fetched["creator"] == "P1lotz"
    assert len(writes) == 1, "one write for the whole request, carrying the page that answered"
    assert set(json.loads(writes[0][1])) == {DRAGON_CACHE_KEY}


def test_two_rows_naming_one_thing_id_are_fetched_once(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5d: two rows naming the same thing in ONE request are one page, one fetch.

    Mutation: look only in ``records`` for a record to reuse and the second row
    asks the vendor for the URL the first row just read, while both answers stay
    the same - so only the call count reds.
    """
    write_dragon(isolated_storage)
    write_file(isolated_storage / "import" / "5390184_dragon_twin" / "files" / "twin.stl", STL_BYTES)
    scan()
    writes = spy_settings_writes(monkeypatch)
    calls = stub_thing_page(monkeypatch, page_html("thing-5390184.json"))

    result = bundle_service.enrich_bundles(
        [f"import/{DRAGON}", "import/5390184_dragon_twin"], cookie=None, refresh=False, ctx=audit_ctx()
    )

    assert calls == ["5390184"], "one page, asked for once"
    assert [item["status"] for item in result["results"]] == ["fetched", "fetched"]
    assert [item["filled"] for item in result["results"]] == [
        ["creator", "licence", "licence_url"],
        ["creator", "licence", "licence_url"],
    ]
    assert len(writes) == 1


def test_a_page_that_fills_nothing_for_this_row_is_still_cached(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-5c: the cache rule is "the page answered", not "the row changed".

    The Moai row already carries its README creator and licence, so the page fills
    nothing for it - and is still cached under its own id: a page that answered is
    a true statement about the vendor, and a sibling row or a later create may
    need it. Only a page that said nothing is uncacheable, so the next click
    retries. Mutation: cache only when ``fields`` is non-empty and the record (and
    the second call's ``cached``) reds.
    """
    moai_bundle(isolated_storage)
    scan()
    writes = spy_settings_writes(monkeypatch)
    calls = stub_thing_page(monkeypatch, page_html("thing-5390184.json"))

    first = bundle_service.enrich_bundles([MOAI], cookie=None, refresh=False, ctx=audit_ctx())

    assert calls == ["2493386"]
    item = first["results"][0]
    assert item["status"] == "fetched"
    assert item["filled"] == [], "the README already named both, so the page took nothing"
    assert item["creator"] == "moai_maker"
    assert len(writes) == 1
    assert json.loads(writes[0][1])[MOAI_CACHE_KEY]["creator"] == "P1lotz"

    second = bundle_service.enrich_bundles([MOAI], cookie=None, refresh=False, ctx=audit_ctx())

    assert second["results"][0]["status"] == "cached"
    assert calls == ["2493386"], "a second click answers from the cache"
    assert len(writes) == 1, "a cached answer writes nothing"


def test_enrich_records_no_cookie_in_the_audit_event(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-10: the audit row says what was taken and never carries the session cookie.

    Mutation: put the request headers (or the cookie) in the detail and the
    substring assertion reds - an audit log is readable by every admin.
    """
    write_dragon(isolated_storage)
    scan()
    stub_thing_page(monkeypatch, page_html("thing-5390184.json"))
    events: List[audit.Event] = []
    monkeypatch.setattr(bundle_service.audit, "record", lambda *recorded, ctx: events.extend(recorded))

    bundle_service.enrich_bundles(
        [f"import/{DRAGON}"], cookie="session=supersecret", refresh=False, ctx=audit_ctx()
    )

    assert len(events) == 1
    event = events[0]
    assert event.action == "bundle.enrich"
    assert event.target == f"import/{DRAGON}"
    assert event.detail == {
        "thing_id": "5390184",
        "source_url": DRAGON_URL,
        "fields": ["creator", "licence", "licence_url"],
    }
    serialised = json.dumps(event.detail, sort_keys=True).lower()
    assert "cookie" not in serialised
    assert "supersecret" not in serialised


def test_a_created_project_records_the_enrichment_in_its_marker(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-11: the created project carries the page's values AND their provenance.

    Mutation: write the marker without ``enrichment`` and the block assertion
    reds; write it only for ``name_only`` and the created values red.
    """
    write_dragon(isolated_storage)
    scan()
    stub_thing_page(monkeypatch, page_html("thing-5390184.json"))
    bundle_service.enrich_bundles([f"import/{DRAGON}"], cookie=None, refresh=False, ctx=audit_ctx())
    row = row_for(bundle_service.build_report(), f"import/{DRAGON}")

    created = bundle_service.create_bundles(
        [bundle_service.BundleCreateItem(key=f"import/{DRAGON}")], audit_ctx()
    )

    assert created["created"] == 1
    with Session(engine) as session:
        project = session.exec(select(Project)).one()
    marker = json.loads(project.metadata_json)["_bundle"]
    assert marker["enrichment"] == row["enrichment"]
    assert (project.creator, project.licence, project.licence_url) == (
        "P1lotz",
        "CC BY-NC-SA",
        CC_BY_NC_SA_URL,
    )


def test_the_cache_is_pruned_to_its_bound(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-12: the write goes through ``prune_records``, so the key cannot grow forever.

    The bound is patched down to 2 rather than seeding 500 records: the property
    under test is that the service prunes at all and keeps the newest, not the
    arithmetic of the shipped limit (that is ``test_bundle_enrich``'s).
    """
    write_dragon(isolated_storage)
    scan()
    store_enrichments(
        {
            "thingiverse:1111": page_record(source_url="https://www.thingiverse.com/thing:1111", fetched_at=STALE_STAMP),
            "thingiverse:2222": page_record(
                source_url="https://www.thingiverse.com/thing:2222", fetched_at="2021-01-01T00:00:00+00:00"
            ),
        }
    )
    stub_thing_page(monkeypatch, page_html("thing-5390184.json"))
    real_prune = bundle_enrich.prune_records

    def bounded(records: Mapping[str, dict], limit: int = bundle_enrich.MAX_ENRICHMENT_ENTRIES) -> dict:
        return real_prune(records, limit=2)

    monkeypatch.setattr(bundle_service.bundle_enrich, "prune_records", bounded)

    bundle_service.enrich_bundles([f"import/{DRAGON}"], cookie=None, refresh=False, ctx=audit_ctx())

    stored = enrichments()
    assert set(stored) == {DRAGON_CACHE_KEY, "thingiverse:2222"}, "the oldest record is the one dropped"


# --- the request budget (R7) ------------------------------------------------

# The reverse proxy's read timeout is the outermost bound on one click, and it
# lives outside this module's build context, so the index is guarded: the API
# image's root is ``curiarium/api``, which is shallower than the repository and
# ``parents[3]`` would raise there (AGENTS.md guardrail 3).
HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[3] if len(HERE.parents) > 3 else None
NGINX_CONF = REPO_ROOT / "curiarium" / "web" / "nginx.conf" if REPO_ROOT is not None else None


def proxy_read_timeout_seconds() -> float:
    """The ``proxy_read_timeout`` nginx.conf states, in seconds."""
    assert NGINX_CONF is not None
    text = NGINX_CONF.read_text(encoding="utf-8")
    match = re.search(r"^\s*proxy_read_timeout\s+(\d+)s;", text, re.MULTILINE)
    assert match is not None, "nginx.conf states no proxy_read_timeout"
    return float(match.group(1))


def test_the_enrich_budget_fits_inside_the_proxy_timeout() -> None:
    """R7: one click's worst case must finish inside the proxy's read timeout.

    The four numbers live in three files and none of them knows the others, so the
    product is asserted here: keys * timeout * (1 + redirects). Mutation: bump any
    of the three (or lower the proxy's timeout) and the click outlives the proxy,
    so the operator gets a gateway timeout instead of the per-item answers.
    """
    if NGINX_CONF is None or not NGINX_CONF.exists():
        pytest.skip("host-only: curiarium/web/nginx.conf is outside the API image build context")

    budget = (
        bundle_service.ENRICH_MAX_KEYS
        * import_resolvers.ENRICH_TIMEOUT_SECONDS
        * (1 + import_resolvers.ENRICH_MAX_REDIRECTS)
    )

    assert budget < proxy_read_timeout_seconds()


# --- R7: one product in many parts (Mf-8xrc) --------------------------------

# The REAL names from the owner's library (Mf-5dnx). A friendlier invention would
# pass the same predicates, so none of these may be renamed: the evidence R7 acts
# on is the names themselves.
STAGETOP = "Stagetop"
STAGETOP_WAVE_ONE = "StageTop Wave One - Personal Use"
STAGETOP_WAVE = "Wave 1.5"
ROCKET = "Rocket Filement support"
ROCKET_GROUPS = (
    "The_Rocket--Circles--Side_Spool_Mount",
    "The_Rocket--Dots--Side_Spool_Mount",
    "The_Rocket--Flowers--Side_Spool_Mount",
    "The_Rocket--Hearts--Side_Spool_Mount",
    "The_Rocket--Solid--Side_Spool_Mount",
    "The_Rocket--Squares--Side_Spool_Mount",
    "The_Rocket--Stars--Side_Spool_Mount",
    "The_Rocket--Triangles--Side_Spool_Mount",
)
XBOX = "xbox360_shell"
XBOX_GROUP = "Wallmount for Lenovo Tab M10 Plus Gen3 - 5461913"

# The measured Stagetop tree: 41 `.stl` and 4 `.step` in the first edition
# across ten part folders, 15 models in the second. The 4 `.step` files sit
# beside `PlayTiles/PlayTile - Blank`'s 4 `.stl`, and `.step` is a
# MODEL_SUFFIXES member, which is why the row reads 60 and not 56.
# `PlayTiles/Accessory` is a `title-name` row on disk and is a group here too.
STAGETOP_LAYOUT = {
    "Accessories": (5, 0),
    "Core": (3, 0),
    "Legs": (5, 0),
    "PlayTiles/PlayTile - 1x1": (4, 0),
    "PlayTiles/PlayTile - 2x2": (4, 0),
    "PlayTiles/PlayTile - 4x4": (3, 0),
    "PlayTiles/PlayTile - Blank": (4, 4),
    "PlayTiles/Accessory": (4, 0),
    "Rails/Rail - Elite": (5, 0),
    "Rails/Rail - Standard": (4, 0),
}

# Each Rocket variant holds 6 real `.stl` under `<group>/<group>/` plus the 6
# macOS AppleDouble `._*.stl` copies under `<group>/__MACOSX/<group>/`. Both are
# on the disk the owner measured, but only the 6 real ones are library content:
# since Mf-94od the count asks the scan's basename predicate, so a group reads 6
# and the row 48, not the 12 / 96 a walk-count reads.
ROCKET_MODELS_PER_GROUP = 6

XBOX_OWN_MODELS = 27
XBOX_GROUP_MODELS = 4
# The group's own README.txt and LICENSE.txt. A create holds every registered file
# under its key (Mf-7oow), so the member count is the models PLUS these two - while
# every `attached`/`model_count` number below stays models-only.
XBOX_GROUP_DOCS = 2
XBOX_MEMBERS = XBOX_OWN_MODELS + XBOX_GROUP_MODELS + XBOX_GROUP_DOCS
XBOX_THING = 5461913

ENDORPHIN = "endorphin"
# The two groups carrying their own Thingiverse id: each is a whole product.
ENDORPHIN_OWN_ID_GROUPS = (
    "3D Printer Electronics Enclosure - 5414731",
    "Ender 5 Dual Y Stepper NEMA 17 Mount - 4591932",
)
# The `STLs` subtree: three title-shaped, model-only payload directories sharing
# the family token `stage`. This is exactly the case the ancestor repo-marker
# rule exists for - without it R7 would swallow these three rows.
ENDORPHIN_STAGE_GROUPS = ("stage-1", "stage-2", "stage-3")
ENDORPHIN_MODEL_FILES = (
    "btt-octopus-raspbery-pi-box-model_files",
    "octopus-pi-case-remix-model_files",
    "prusa-bear-btt-octopus-case-model_files",
)
ENDORPHIN_REPO_FILES = ("_layouts/x.html", "_sass/x.scss", "_includes/x.html", "_config.yml", "Gemfile")


def write_group(folder: Path, models: int, *, steps: int = 0) -> None:
    """One model-bearing group: its models, an image and a text at its OWN level.

    The image and the text are what make the folder a ``generic-bundle``/LOW row
    on its own with R7 switched off. Without them a directory holding only models
    is refused by R3, and absorption would be indistinguishable from absence.
    """
    for index in range(models):
        write_file(folder / f"part-{index}.stl", b"s" * 40)
    for index in range(steps):
        write_file(folder / f"part-{index}.step", b"s" * 40)
    write_file(folder / "cover.jpg", b"j" * 40)
    write_text(folder / "notes.txt", "print at 0.2mm\n")


def write_thingiverse_docs(folder: Path, thing: int) -> None:
    """The real README/LICENSE marker pair, so the folder is HIGH on its own."""
    title = library_scan.display_name(folder.name)
    write_text(folder / "README.txt", f"{title}\nCreated by Thingiverse user maker_{thing}\nLicensed under CC BY-NC\n")
    write_text(
        folder / "LICENSE.txt",
        f"{title} by maker_{thing} on Thingiverse: https://www.thingiverse.com/thing:{thing}\n",
    )


def write_stagetop(storage: Path) -> tuple:
    """The measured Stagetop tree; returns the eleven keys R7 absorbs, one row each.

    Those keys are the rows the same fixture offers with ``COMPOUND_BUNDLES_ENABLED``
    off, which is what makes them evidence of absorption rather than of absence.
    """
    root = storage / "import" / STAGETOP
    keys: List[str] = []
    for rel_dir, (models, steps) in STAGETOP_LAYOUT.items():
        write_group(root / STAGETOP_WAVE_ONE / rel_dir, models, steps=steps)
        keys.append(f"import/{STAGETOP}/{STAGETOP_WAVE_ONE}/{rel_dir}")
    write_group(root / STAGETOP_WAVE, 15)
    keys.append(f"import/{STAGETOP}/{STAGETOP_WAVE}")
    return tuple(keys)


def write_rocket(storage: Path) -> tuple:
    """The measured Rocket tree: 8 variants of 6 models plus 6 AppleDouble copies."""
    root = storage / "import" / ROCKET
    for group in ROCKET_GROUPS:
        for index in range(ROCKET_MODELS_PER_GROUP):
            write_file(root / group / group / f"part-{index}.stl", b"s" * 40)
            write_file(root / group / "__MACOSX" / group / f"._part-{index}.stl", b"s" * 40)
        write_file(root / group / "cover.jpg", b"j" * 40)
        write_text(root / group / "notes.txt", "print at 0.2mm\n")
    return tuple(f"import/{ROCKET}/{group}" for group in ROCKET_GROUPS)


def write_xbox360(storage: Path) -> Path:
    """27 models of the product's own, plus the foreign group it ships beside."""
    root = storage / "import" / XBOX
    for index in range(XBOX_OWN_MODELS):
        write_file(root / f"shell-{index}.stl", b"s" * 40)
    group = root / XBOX_GROUP
    for index in range(XBOX_GROUP_MODELS):
        write_file(group / f"bracket-{index}.stl", b"s" * 40)
    write_thingiverse_docs(group, XBOX_THING)
    return root


def refusal_ledger(report: Mapping) -> Dict[str, str]:
    """Every refused path and the rule that refused it, keyed by storage-relative path."""
    return {item["rel_path"]: item["rule"] for item in report["refused"]}


def service_row(key: str) -> dict:
    """One row read from ``bundle_service.build_report`` - the dict the route serves.

    The per-row ``rule_set`` stamp is not declared on ``BundleDetectItemOut`` - it
    is a service-level fact, one rule set per report - so a response model would
    silently drop it. Reading the service's own dict is the only way to assert it;
    everything else is read from here too, so the two readings cannot disagree
    about a row.
    """
    report = bundle_service.build_report(limit=1000)
    matches = [item for item in report["bundles"] if item["key"] == key]
    assert len(matches) == 1, f"expected exactly one row for {key}: {report['bundles']}"
    return matches[0]


def test_service_stagetop_is_one_row_with_two_groups(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC: Stagetop is ONE row carrying its two editions and 60 parts.

    Mutation: drop the post-pass and the eleven child rows come back (the
    ``report["bundles"]`` shape reds); take ``folded`` instead of the claimed
    ``work`` view and ``model_count`` reads 0 for the parent; skip the ledger
    write and the ``member-of-bundle`` assertions red.
    """
    child_keys = write_stagetop(isolated_storage)
    scan()
    headers = auth_header(client)

    report = detect(client, headers, limit=1000)

    row = row_for(report, f"import/{STAGETOP}")
    assert row["detector"] == "engine:one-project"
    assert row["metadata_shape"] == "one-project"
    assert row["model_count"] == 60
    assert service_row(f"import/{STAGETOP}")["groups"] == [
        {"name": STAGETOP_WAVE_ONE, "model_count": 45, "foreign": False, "separable": False},
        {"name": STAGETOP_WAVE, "model_count": 15, "foreign": False, "separable": False},
    ]
    assert [item for item in report["bundles"] if item["key"].startswith(f"import/{STAGETOP}/")] == []

    ledger = refusal_ledger(report)
    for key in child_keys:
        assert ledger.get(key) == "member-of-bundle", f"{key} was a row and must be named as a member"


def test_service_the_rocket_is_one_row_with_eight_groups(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC: the Rocket is ONE row with eight variants of exactly six models each.

    The measured disk holds six real `.stl` and six AppleDouble `._*` copies per
    group. Since Mf-94od the count asks the library scan's own basename
    predicate, so the copies name no model: a group reads 6 and the row reads 48,
    which is what the project holds. Mutation: count through the suffix alone and
    the eight 6s become eight 12s; order the groups by anything but their name
    and the name list reds.
    """
    group_keys = write_rocket(isolated_storage)
    scan()
    headers = auth_header(client)

    report = detect(client, headers, limit=1000)

    row = row_for(report, f"import/{ROCKET}")
    assert row["detector"] == "engine:one-project"
    assert row["model_count"] == 48
    groups = service_row(f"import/{ROCKET}")["groups"]
    assert [group["name"] for group in groups] == list(ROCKET_GROUPS)
    assert [group["model_count"] for group in groups] == [6] * len(ROCKET_GROUPS)
    assert all(group["foreign"] is False for group in groups)

    ledger = refusal_ledger(report)
    for key in group_keys:
        assert ledger.get(key) == "member-of-bundle", key
    assert [item for item in report["bundles"] if item["key"] != f"import/{ROCKET}"] == []


def test_service_xbox360_shell_has_a_row_covering_its_own_models_and_naming_the_foreign_group(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC: the parent's 27 invisible models come back, and the foreign group is NAMED.

    Owner decision 2: ONE row with the other product's group flagged, and the group
    is what keeps it excludable at create time. The engine reads the folder as a
    split and the row is served as that split (Mf-1agz: the recommendation is the
    engine's); what matters here is that the row EXISTS, covers all 31 models, and
    names the foreign group. Mutation: rank the children as rows and the parent's
    27 models stay invisible, so ``row_for`` finds no row.
    """
    write_xbox360(isolated_storage)
    scan()
    headers = auth_header(client)

    report = detect(client, headers, limit=1000)

    row = row_for(report, f"import/{XBOX}")
    assert row["detector"] == "engine:split-top-folders"
    assert row["model_count"] == XBOX_OWN_MODELS + XBOX_GROUP_MODELS
    assert service_row(f"import/{XBOX}")["groups"] == [
        {"name": XBOX_GROUP, "model_count": XBOX_GROUP_MODELS, "foreign": True, "separable": True}
    ]
    assert [item for item in report["bundles"] if item["key"].startswith(f"import/{XBOX}/")] == []

    ledger = refusal_ledger(report)
    assert ledger.get(f"import/{XBOX}/{XBOX_GROUP}") == "member-of-bundle"

    candidates, _truncated = bundle_service.iter_bundle_candidates(isolated_storage)
    grouped = [candidate for candidate in candidates if candidate.key == f"import/{XBOX}"]
    assert len(grouped) == 1
    own = bundle_service._own_paths(grouped[0].key, grouped[0])
    for index in range(XBOX_OWN_MODELS):
        assert f"import/{XBOX}/shell-{index}.stl" in own


def test_service_a_grouped_row_creates_one_project_with_every_part(
    client: TestClient, isolated_storage: Path
) -> None:
    """A row with groups is creatable on the same terms as any other folder row.

    The folder resolves, so the row is ``ready`` like every other row, and one
    create makes ONE project attaching all 31 models: the 27 of the product's own
    and the foreign group's 4, because ``_own_paths`` is the whole subtree rather
    than a claimed-children view. The ``contains_bundles`` belt cannot fire here -
    ``own_assets == assets`` - and splitting the foreign group is C2's job
    (``Mf-ao6r``), not this slice's. Mutation: give the grouped candidate a
    closed snapshot instead of the whole subtree and the 4 foreign models drop out
    of the create, or the belt reds and nothing is created.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    scan()
    headers = auth_header(client)

    created = create(client, headers, [{"key": f"import/{XBOX}", "shape": "one-project"}])

    assert created["created"] == 1
    assert created["failed"] == 0
    assert member_count(created["results"][0]["project_id"]) == XBOX_MEMBERS, (
        "the 27 own parts, the foreign group's 4 parts AND the group's two marker "
        "documents: a bundle project holds every registered file under its key (Mf-7oow)"
    )


def test_attaching_backups_and_a_document_does_not_move_the_model_count(
    client: TestClient, isolated_storage: Path
) -> None:
    """A backup and a PDF are MEMBERS; they are not models, and the row still says so.

    Mf-7oow's rule has two halves and this witness pins both directions at once:

    * membership moved - the two ``.stlbak`` backups and the vendor ``.pdf`` are in the
      created project, because a bundle project holds every registered file under its
      key, and the exact member set is asserted rather than a count;
    * the ROLE did not - ``classify_entry`` answers ``other`` for a ``.stlbak`` and
      ``text`` for a ``.pdf``, so ``row["model_count"]`` is the two STLs and nothing
      more, while ``other_count``/``text_count`` account for the rest. The backup is
      also made the LARGEST file on disk, so a cover rule that had let one in would
      have picked it.

    Mutation (both run, then restored): make ``_members_of`` return only the MODEL
    entries (or re-add any suffix filter) and the member-set assertion reds with the
    two backups and the PDF missing; make the row's ``model_count`` read the member
    count and the ``(2, 1, 1, 2)`` block reds at 6.
    """
    bundle = isolated_storage / "Backup Kit"
    write_file(bundle / "part-a.stl", STL_BYTES)
    write_file(bundle / "part-b.stl", STL_BYTES)
    write_file(bundle / "cover.jpg", b"j" * 50)
    # Larger than the preview on purpose: a `.stlbak` must never win the cover.
    write_file(bundle / "part-a.stlbak", b"b" * 500)
    write_file(bundle / "part-b.stlbak", b"b" * 501)
    write_text(bundle / "docs.pdf", "the vendor's assembly guide\n")
    scan()
    headers = auth_header(client)

    row = row_for(detect(client, headers), "Backup Kit")

    assert (
        row["model_count"],
        row["image_count"],
        row["text_count"],
        row["other_count"],
    ) == (2, 1, 1, 2), (
        "the two STLs are the only models: a `.stlbak` is `other` and a `.pdf` is `text`, "
        "and the row counts the roles the one classifier answers"
    )
    assert row["assets_total"] == 6
    assert (row["cover"]["reason"], row["cover"]["rel_path"]) == ("largest-image", "Backup Kit/cover.jpg"), (
        "the cover legs are IMAGE_SUFFIXES and PREVIEW_SUFFIXES, so the 500-byte `.stlbak` "
        "is never even a candidate - it is not an image and not a renderable model"
    )

    created = create(client, headers, [{"key": "Backup Kit"}])

    assert created["created"] == 1
    project_id = created["results"][0]["project_id"]
    assert created["results"][0]["assets_added"] == 6
    assert project_storage_paths(project_id) == {
        "Backup Kit/part-a.stl",
        "Backup Kit/part-b.stl",
        "Backup Kit/cover.jpg",
        "Backup Kit/part-a.stlbak",
        "Backup Kit/part-b.stlbak",
        "Backup Kit/docs.pdf",
    }, "every one of them is a member: backups and document included"
    with Session(engine) as session:
        member_set = bundle_service._members_of(
            "Backup Kit", bundle_service.registered_assets(session, "Backup Kit")
        )

    assert [entry.rel_path for entry in member_set.by_role(library_scan.MODEL)] == [
        "part-a.stl",
        "part-b.stl",
    ], "the MODEL view - what `attached` and `Dedupe.containment` are asked with - is the two STLs"
    assert {entry.rel_path: entry.role for entry in member_set.entries}[
        "part-a.stlbak"
    ] == library_scan.OTHER
    assert {entry.rel_path: entry.role for entry in member_set.entries}[
        "docs.pdf"
    ] == library_scan.TEXT
    assert registered_model_paths("Backup Kit") == {"Backup Kit/part-a.stl", "Backup Kit/part-b.stl"}


def test_attaching_backups_creates_no_group_members(
    client: TestClient, isolated_storage: Path
) -> None:
    """A grouped create writes every file, but only MODELS count as a group's `attached`.

    The probe is one product in two parts: two family-named groups (``Bracket Set
    Alpha``, ``Bracket Set Beta``), three STLs spread across them, four ``.stlbak``
    backups and one root PDF - eight registered files, three models. Creating the row
    with groups must hold all eight in the project while the marker's per-group
    ``attached`` stays the MODEL answer: 2 and 1, summing to the row's ``model_count``.

    Mutation (run, then restored): count ``models_by_group`` from the member set
    instead of from ``model_assets`` and Alpha's ``attached`` reads 4 (its two STLs
    plus its two backups) and Beta's 3 - the marker then claims a group attached a
    backup, and the sum of ``attached`` no longer equals ``model_count``.
    """
    key = "import/backup_compound"
    root = isolated_storage / "import" / "backup_compound"
    alpha = root / "Bracket Set Alpha"
    beta = root / "Bracket Set Beta"
    write_file(alpha / "alpha-0.stl", STL_BYTES)
    write_file(alpha / "alpha-1.stl", STL_BYTES)
    write_file(alpha / "alpha-0.stlbak", b"b" * 40)
    write_file(alpha / "alpha-1.stlbak", b"b" * 41)
    write_file(beta / "beta-0.stl", STL_BYTES)
    write_file(beta / "beta-0.stlbak", b"b" * 42)
    write_file(beta / "beta-1.stlbak", b"b" * 43)
    write_text(root / "docs.pdf", "the vendor's assembly guide\n")
    insert_folder_row("backup_compound", key)
    scan()
    headers = auth_header(client)

    row = row_for(detect(client, headers), key)

    assert (row["detector"], row["metadata_shape"]) == ("engine:one-project", "one-project")
    assert row["model_count"] == 3, "three STLs - the four backups are `other`, the PDF is `text`"
    assert row["assets_total"] == 8

    created = create(client, headers, [{"key": key}])

    assert created["created"] == 1
    result = created["results"][0]
    project_id = result["project_id"]
    assert result["assets_added"] == 8, "every registered file under the key is a member"
    assert project_storage_paths(project_id) == registered_asset_paths(key) == {
        f"{key}/{name}"
        for name in (
            "Bracket Set Alpha/alpha-0.stl",
            "Bracket Set Alpha/alpha-1.stl",
            "Bracket Set Alpha/alpha-0.stlbak",
            "Bracket Set Alpha/alpha-1.stlbak",
            "Bracket Set Beta/beta-0.stl",
            "Bracket Set Beta/beta-0.stlbak",
            "Bracket Set Beta/beta-1.stlbak",
            "docs.pdf",
        )
    }

    marker = project_marker(project_id)

    assert marker["groups"] == [
        {
            "name": "Bracket Set Alpha",
            "model_count": 2,
            "foreign": False,
            "separable": False,
            "included": True,
            "attached": 2,
        },
        {
            "name": "Bracket Set Beta",
            "model_count": 1,
            "foreign": False,
            "separable": False,
            "included": True,
            "attached": 1,
        },
    ], (
        "`attached` is the group's MODEL count: the four backups and the PDF created no "
        "group members, even though all eight files are in the project"
    )
    assert sum(group["attached"] for group in marker["groups"]) == row["model_count"] == 3


def test_service_a_folder_with_one_model_bearing_group_is_one_row(
    client: TestClient, isolated_storage: Path
) -> None:
    """A wrapper holding ONE model-bearing group is one project with one group.

    The engine reads the wrapper as the product and stops: the child is a part,
    never a second row.
    """
    write_group(isolated_storage / "import" / "wrapper" / "one product", 2)
    scan()
    headers = auth_header(client)

    report = detect(client, headers, limit=1000)

    row = row_for(report, "import/wrapper")
    assert row["detector"] == "engine:one-project"
    assert row["model_count"] == 2
    assert [group["name"] for group in row["groups"]] == ["one product"]
    assert [item for item in report["bundles"] if item["key"] == "import/wrapper/one product"] == []


# --- The marker's group vocabulary, from literals only ----------------------


def test_the_marker_groups_carry_an_operators_exclusion_forward_by_name() -> None:
    """A rewrite that did not re-ask the question cannot resurrect an excluded group.

    ``included`` is carried forward BY NAME from the older marker, so adopting a
    project an operator already excluded a group from keeps that exclusion even
    with an empty create-time exclusion list. ``separable`` is copied off the wire
    as a real bool, and ``attached`` stays a lookup in the create's own counts.

    Mutation: derive ``included`` from ``excluded`` alone and Beta reads ``True``;
    drop ``separable`` from the written dict and the key-order assertion reds.
    """
    wire = [
        {"name": "Alpha", "model_count": 4, "foreign": False, "separable": True},
        {"name": "Beta", "model_count": 2, "foreign": True, "separable": False},
    ]
    before = [
        {"name": "Beta", "model_count": 2, "foreign": True, "separable": False, "included": False, "attached": 0},
    ]

    written = bundle_service.marker_groups(wire, attached={"Alpha": 3}, excluded=set(), before=before)

    assert [group["name"] for group in written] == ["Alpha", "Beta"]
    for group in written:
        assert list(group) == ["name", "model_count", "foreign", "separable", "included", "attached"]
    alpha, beta = written
    assert alpha["included"] is True, "a name the older marker does not carry falls back to `excluded`"
    assert beta["included"] is False, "the operator's exclusion survives a rewrite that never asked again"
    assert (alpha["attached"], beta["attached"]) == (3, 0)
    assert (alpha["separable"], beta["separable"]) == (True, False)
    assert isinstance(alpha["separable"], bool)


def test_an_excluded_group_is_not_permanent_drift_after_the_marker_is_rewritten() -> None:
    """The drift guard: only a marker group carrying ``included: False`` subtracts.

    ``bundle_drift.excluded_prefixes`` subtracts a group only when ``included is
    False``; a group written without the key never subtracts, so the excluded
    group's parts would read as permanent drift. The helper is what makes the
    marker carry the flag - the engine's four wire keys alone are exactly the
    failure mode it prevents.

    Mutation: write the wire shape into the marker verbatim and the first
    assertion reds while the second still passes.
    """
    wire = [{"name": "Alpha", "model_count": 4, "foreign": True, "separable": True}]

    written = bundle_service.marker_groups(wire, attached={}, excluded={"Alpha"})

    assert bundle_drift.excluded_prefixes("import/Thing", {"groups": written}) == ("import/Thing/Alpha/",)
    assert bundle_drift.excluded_prefixes("import/Thing", {"groups": wire}) == (), (
        "the engine's four keys carry no `included`, so nothing subtracts - the drift the helper prevents"
    )


# --- C2: excluding a flagged foreign group (Mf-ao6r) -------------------------


def project_marker(project_id: str) -> dict:
    """The ``_bundle`` marker one created project carries, as ``_create_one`` wrote it."""
    with Session(engine) as session:
        project = session.get(Project, project_id)
    assert project is not None, f"no project row for {project_id}"
    return json.loads(project.metadata_json or "{}")["_bundle"]


def project_storage_paths(project_id: str) -> set:
    """The storage path of every asset attached to one project, as a set."""
    with Session(engine) as session:
        asset_ids = {
            membership.asset_id
            for membership in session.exec(select(ProjectAsset).where(ProjectAsset.project_id == project_id))
        }
        assets = session.exec(select(Asset).where(Asset.id.in_(asset_ids))).all()
    return {asset.storage_path for asset in assets}


def registered_model_paths(rel_dir: str) -> set:
    """Every registered MODEL file under ``rel_dir``, as storage-relative paths.

    THE MODEL question, and the containment witnesses keep using it: "does a project
    already hold this bundle's files" is asked with these ids, whatever the create
    attaches (Mf-wsdo).
    """
    with Session(engine) as session:
        return {
            asset.path
            for asset in bundle_service.registered_assets(session, rel_dir)
            if bundle_service._suffix(asset.path) in library_scan.MODEL_SUFFIXES
        }


def registered_asset_paths(rel_dir: str) -> set:
    """Every registered file under ``rel_dir``, as storage-relative paths.

    THE membership question since Mf-7oow: a created bundle project holds EVERY
    registered asset under its own key, so this is a plain read of what the scan
    registered - there is no suffix filter, because there is no member table left to
    filter with. The role each of those files plays in the project is the detector's
    own ``classify_entry`` answer, decided once in ``bundle_service._members_of``.

    It sits beside ``registered_model_paths``, which is the MODEL question and stays
    the one a containment or `attached` witness must call: "does a project already
    hold this bundle's parts" is a narrower question than "what does the project
    hold", and the two answering each other is the confusion this pair exists to
    keep apart.
    """
    with Session(engine) as session:
        return {asset.path for asset in bundle_service.registered_assets(session, rel_dir)}


def mount_key() -> str:
    """The xbox fixture's foreign group as a bundle key."""
    return f"import/{XBOX}/{XBOX_GROUP}"


def create_for_exclusion(client: TestClient, headers: dict, exclude: Optional[List[str]] = None) -> dict:
    """Create the xbox row as one project, optionally excluding its foreign group."""
    item: dict = {"key": f"import/{XBOX}", "shape": "one-project"}
    if exclude is not None:
        item["exclude_groups"] = exclude
    return create(client, headers, [item])


# A vendor id in the name -> foreign; its models sit in two unnamed subdirectories,
# so nothing at its own depth -> R5 (the own-depth guard) still refuses it a row.
A_LONE_GROUP = "core"
A_COMPOUND = "toolkit"


def write_foreign_group_never_a_row(storage: Path) -> str:
    """A row whose ONE foreign group is not separable: the exclusion has nowhere to go.

    The engine's own flags: a group is ``foreign`` off its own README and
    ``separable`` only when its name could stand as a project. This group holds
    four models in two unnamed subdirectories plus a README at its own root: the
    document makes it foreign, and the part-shaped name ``core`` makes it NOT
    separable - exactly the group an operator must not be allowed to exclude,
    because its parts would land in no project and in no row.
    """
    root = storage / "import" / A_COMPOUND
    for index in range(9):
        write_file(root / f"shell-{index}.stl", b"s" * 40)
    write_text(root / A_LONE_GROUP / "README.txt", "a part of the toolkit\n")
    for index in range(2):
        write_file(root / A_LONE_GROUP / "plates" / f"bracket-{index}.stl", b"s" * 40)
    for index in range(2, 4):
        write_file(root / A_LONE_GROUP / "rails" / f"bracket-{index}.stl", b"s" * 40)
    return f"import/{A_COMPOUND}"


def test_service_creating_from_a_grouped_row_makes_one_project_with_every_part(
    client: TestClient, isolated_storage: Path
) -> None:
    """One create of the Stagetop row makes ONE project holding all 60 parts in two groups.

    Since Mf-wsdo the project also holds the eleven group previews, so the member set is
    read with the ATTACH question (`registered_member_paths`) and the marker's
    per-group `attached` is read with the MODEL question - see the two assertions and
    their comments below.

    Mutation: attach only one group's snapshot (or filter the attach set with no
    exclusion asked) and the set equality against every registered member reds.
    """
    write_stagetop(isolated_storage)
    insert_folder_row(STAGETOP, f"import/{STAGETOP}")
    scan()
    headers = auth_header(client)

    created = create(client, headers, [{"key": f"import/{STAGETOP}"}])

    assert created["created"] == 1
    assert created["failed"] == 0
    result = created["results"][0]
    assert result["assets_added"] == 82, (
        "the 60 parts, the eleven group previews AND the eleven group ``notes.txt``: "
        "a bundle project holds every registered file under its key (Mf-7oow), while "
        "the row's `model_count` below stays 60 - membership and role are two answers, "
        "and only the role one is a count of models"
    )
    assert project_storage_paths(result["project_id"]) == registered_asset_paths(f"import/{STAGETOP}")

    row = service_row(f"import/{STAGETOP}")
    assert row["model_count"] == 60
    with Session(engine) as session:
        project = session.get(Project, result["project_id"])
    marker = project_marker(result["project_id"])
    assert marker["detector"] == "engine:one-project"
    assert marker["shape"] == "one-project"
    assert marker["groups"] == [
        {
            "name": STAGETOP_WAVE_ONE,
            "model_count": 45,
            "foreign": False,
            "separable": False,
            "included": True,
            "attached": 45,
        },
        {
            "name": STAGETOP_WAVE,
            "model_count": 15,
            "foreign": False,
            "separable": False,
            "included": True,
            "attached": 15,
        },
    ]
    # Re-expressed, not deleted (Mf-wsdo): `attached` is the MODEL question per group
    # and `assets_added` is the ATTACH answer, so the two are no longer one number. The
    # identity that survives is "the groups' attached counts sum to the MODELS the
    # project holds", which is what the marker claims and what an image leak (a group
    # counted as attaching a preview) still reds on.
    attached_models = [
        path
        for path in project_storage_paths(result["project_id"])
        if bundle_service._suffix(path) in library_scan.MODEL_SUFFIXES
    ]
    assert sum(group["attached"] for group in marker["groups"]) == len(attached_models) == 60, (
        "the marker's per-group `attached` is the MODEL question and stays "
        "models-only, whatever the project holds"
    )
    assert marker["cover"]["reason"] == "largest-image"
    assert project.cover_asset_id == row["cover"]["asset_id"] == marker["cover"]["asset_id"]
    assert marker["cover"]["group"] == STAGETOP_WAVE_ONE


def test_the_row_counts_exactly_what_the_scan_registered(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-2: the row and the project it creates agree on the Rocket's 48.

    The measured disk holds 48 real `.stl` and 48 AppleDouble `._*` copies. Since
    Mf-94od the detector asks the library scan's own basename predicate, so the
    row counts 48 (six per group), the registered model assets are 48, and the
    project's `storage_path` set is the SAME 48 plus the eight group previews the
    ATTACH table gives a created project (Mf-wsdo) — with no `._` path in it, and
    with the marker's per-group `attached` still the MODEL answer, (6, 6) eight
    times. Mutation: count the sidecars again and the row reads 96 while the project
    still holds 48 models, which is exactly the double-count this ticket exists to
    remove.
    """
    write_rocket(isolated_storage)
    insert_folder_row(ROCKET, f"import/{ROCKET}")
    scan()
    headers = auth_header(client)

    created = create(client, headers, [{"key": f"import/{ROCKET}"}])

    assert created["created"] == 1
    result = created["results"][0]
    assert result["assets_added"] == 64, (
        "the 48 real models, the eight group ``cover.jpg`` previews AND the eight group "
        "``notes.txt`` documents: a bundle project holds every registered file under its "
        "key (Mf-7oow) while `model_count` below stays 48 and every group's `attached` "
        "stays 6 - a document or an image counted as a model would move those numbers"
    )

    row = service_row(f"import/{ROCKET}")
    assert row["model_count"] == 48
    assert [group["model_count"] for group in row["groups"]] == [6] * len(ROCKET_GROUPS)
    # A skipped entry is in NO bucket: not a model, and not "other" either.
    assert row["other_count"] == 0

    marker = project_marker(result["project_id"])
    assert [(group["model_count"], group["attached"]) for group in marker["groups"]] == [
        (6, 6)
    ] * len(ROCKET_GROUPS)

    real = {
        f"import/{ROCKET}/{group}/{group}/part-{index}.stl"
        for group in ROCKET_GROUPS
        for index in range(ROCKET_MODELS_PER_GROUP)
    }
    previews = {f"import/{ROCKET}/{group}/cover.jpg" for group in ROCKET_GROUPS}
    documents = {f"import/{ROCKET}/{group}/notes.txt" for group in ROCKET_GROUPS}
    attached = project_storage_paths(result["project_id"])
    assert attached == real | previews | documents, (
        "the project holds the real `.stl` files, their group previews and their group "
        "documents - and no `._*` sidecar: every registered file under the key is a "
        "member (Mf-7oow), the sidecars are not registered at all"
    )
    # The load-bearing assertion: this reads the REGISTERED rows, so it reds if a
    # `._*` sidecar ever registers (the walk-built `real` set could not say that) - and
    # it reads them through the membership question, which is every registered file.
    assert attached == registered_asset_paths(f"import/{ROCKET}")


def test_the_stagetop_row_still_reads_60_with_no_appledouble_on_disk(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-2's non-regression witness: a tree with no AppleDouble keeps its count.

    Stagetop holds 56 `.stl` and 4 `.step` on the measured disk and no `._*`
    copies at all, so the predicate change must leave its row at 60 with groups
    45/15. Mutation: make the predicate skip a real suffix or a `.step` and this
    row drops below 60 even though nothing on its disk moved.
    """
    write_stagetop(isolated_storage)
    scan()
    headers = auth_header(client)

    row = row_for(detect(client, headers, limit=1000), f"import/{STAGETOP}")

    assert row["model_count"] == 60
    assert [(group["name"], group["model_count"]) for group in row["groups"]] == [
        (STAGETOP_WAVE_ONE, 45),
        (STAGETOP_WAVE, 15),
    ]
    assert not any("._" in path for path in registered_model_paths(f"import/{STAGETOP}"))


def test_service_xbox360_shell_creates_one_project_with_all_31_parts(
    client: TestClient, isolated_storage: Path
) -> None:
    """Unexcluded, the row attaches the 27 own parts AND the foreign group's 4.

    The cover documents the un-excluded pick: with no image anywhere, the four
    foreign `.stl` are equal-sized and path order breaks the tie, and ``W`` sorts
    before ``s``, so the mount's `bracket-0.stl` wins. That 40-byte tie and the
    ``Wallmount.../bracket-0.stl`` pick are pinned on purpose (AC-3a); every model
    is equal-sized and ``registered_assets`` already orders by ``storage_path``, so
    any other ordering the code could apply reproduces the same pick. Mutation:
    drop the foreign group from the attach set and the 31 relation reds.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    scan()
    headers = auth_header(client)

    created = create_for_exclusion(client, headers)

    assert created["created"] == 1
    result = created["results"][0]
    assert result["assets_added"] == XBOX_MEMBERS
    expected = (
        {f"import/{XBOX}/shell-{index}.stl" for index in range(XBOX_OWN_MODELS)}
        | {f"{mount_key()}/bracket-{index}.stl" for index in range(XBOX_GROUP_MODELS)}
        | {f"{mount_key()}/README.txt", f"{mount_key()}/LICENSE.txt"}
    )
    assert project_storage_paths(result["project_id"]) == expected, (
        "every registered file under the key, the foreign group's two documents included "
        "(Mf-7oow) - the group's PARTS are still what the marker counts as `attached`"
    )

    marker = project_marker(result["project_id"])
    assert marker["groups"] == [
        {
            "name": XBOX_GROUP,
            "model_count": XBOX_GROUP_MODELS,
            "foreign": True,
            "separable": True,
            "included": True,
            "attached": XBOX_GROUP_MODELS,
        }
    ]
    row = row_for(detect(client, headers, limit=1000), f"import/{XBOX}")
    assert marker["cover"]["reason"] == "model-preview"
    assert marker["cover"]["rel_path"] == f"{mount_key()}/bracket-0.stl"
    assert row["cover"]["asset_id"] == marker["cover"]["asset_id"]
    assert marker["cover"]["group"] == XBOX_GROUP


def test_excluding_the_foreign_group_creates_one_project_of_its_own_27(
    client: TestClient, isolated_storage: Path
) -> None:
    """An exclusion is an ATTACH decision, never a detection one.

    One create of the row with the mount excluded attaches the 27 own parts and
    nothing from the mount; the next report's row is unchanged
    as far as detection is concerned - same counts, same groups, same cover asset,
    same status. Mutation: filter `assets`/`own_assets` in `_create_one` and
    the belt fires on every excluded create; drop the cover re-pick and the cover
    stays on the excluded group's `bracket-0.stl`.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    scan()
    headers = auth_header(client)
    before = row_for(detect(client, headers, limit=1000), f"import/{XBOX}")

    created = create_for_exclusion(client, headers, [XBOX_GROUP])

    assert created["created"] == 1
    assert created["failed"] == 0
    result = created["results"][0]
    assert result["assets_added"] == XBOX_OWN_MODELS
    expected = {f"import/{XBOX}/shell-{index}.stl" for index in range(XBOX_OWN_MODELS)}
    assert project_storage_paths(result["project_id"]) == expected
    for index in range(XBOX_GROUP_MODELS):
        assert f"{mount_key()}/bracket-{index}.stl" not in project_storage_paths(result["project_id"])

    marker = project_marker(result["project_id"])
    assert marker["groups"] == [
        {
            "name": XBOX_GROUP,
            "model_count": XBOX_GROUP_MODELS,
            "foreign": True,
            "separable": True,
            "included": False,
            "attached": 0,
        }
    ]
    with Session(engine) as session:
        project = session.get(Project, result["project_id"])
    assert marker["cover"]["reason"] == "model-preview"
    assert marker["cover"]["rel_path"] == f"import/{XBOX}/shell-0.stl"
    assert not marker["cover"]["rel_path"].startswith(f"import/{XBOX}/{XBOX_GROUP}/")
    assert marker["cover"]["group"] is None
    assert marker["cover"]["asset_id"] == project.cover_asset_id

    after = row_for(detect(client, headers, limit=1000), f"import/{XBOX}")
    for field in ("model_count", "assets_total", "status"):
        assert after[field] == before[field], field
    # The excluded group sits INSIDE the created project's key, so it is no longer
    # a detection row (Mf-z643). The row itself is still the engine's reading of
    # the same disk, and the two shapes a create can choose are still offered.
    assert after["groups"] == before["groups"]
    assert after["detector"] == "engine:split-top-folders"
    assert "one-project" in [match["shape"] for match in after["matches"]]
    assert after["cover"]["asset_id"] == before["cover"]["asset_id"]


def test_the_excluded_group_is_inside_the_project_key_and_is_not_a_row(
    client: TestClient, isolated_storage: Path
) -> None:
    """A descendant of a created project is not a detection row, at either scope.

    This used to be the C2 re-offer witness: the excluded group came back as a
    row of its own. Mf-z643 makes the exclusion a CREATE-time decision only - once
    `import/xbox360_shell` is a project key, everything inside it stops being a
    row, and the marker is the one record of what was excluded. The report must
    name the mount nowhere: not as a row, not in the ledger, at either scope.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    insert_folder_row(XBOX_GROUP, mount_key())
    scan()
    headers = auth_header(client)
    assert create_for_exclusion(client, headers, [XBOX_GROUP])["created"] == 1

    report = detect(client, headers, limit=1000)

    keys = {row["key"] for row in report["bundles"]}
    assert keys == {f"import/{XBOX}"}
    row = row_for(report, f"import/{XBOX}")
    assert row["detector"] == "engine:split-top-folders"
    assert row["existing_project_match"] == "marker"
    assert mount_key() not in refusal_ledger(report)

    walked = bundle_service.scan_bundles(isolated_storage, excluded_keys={"import/xbox360_shell"})
    assert [(item.rel_path, item.rule) for item in walked.refusals] == [("import", "container-rule")]
    assert mount_key() not in {candidate.key for candidate in walked.candidates}

    assert report["counts"]["total"] == 1
    scoped = detect(client, headers, path="import", limit=1000)
    assert scoped["counts"]["total"] == 1
    scoped_keys = {row["key"] for row in scoped["bundles"]}
    assert scoped_keys == {f"import/{XBOX}"}
    assert mount_key() not in {item["rel_path"] for item in scoped["refused"]}


def test_no_directory_is_both_a_row_and_a_refusal(client: TestClient, isolated_storage: Path) -> None:
    """The offered set and the ledger never name the same directory, before or after.

    Mutation: re-offer a group without removing its ledger entry (or filter the
    qualified key instead of the walk-relative one) and both the unscoped and the
    scoped read see a non-empty intersection. The scoped read is the witness: under
    `path="import"` the row key is qualified while the ledger path the filter must
    match is walk-relative, so the unscoped read alone cannot tell them apart.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    insert_folder_row(XBOX_GROUP, mount_key())
    scan()
    headers = auth_header(client)

    def assert_disjoint() -> None:
        report = detect(client, headers, limit=1000)
        # A MANUAL row is deliberately both a row and a ledger entry: the ledger word
        # is its `note` (Mf-z643). Every other row stays disjoint from the ledger.
        rows = {row["key"] for row in report["bundles"] if row["detector"] != "manual"}
        assert rows & set(refusal_ledger(report)) == set()
        scoped = detect(client, headers, path="import", limit=1000)
        scoped_rows = {row["key"] for row in scoped["bundles"] if row["detector"] != "manual"}
        assert scoped_rows & set(refusal_ledger(scoped)) == set()

    assert_disjoint()
    assert create_for_exclusion(client, headers, [XBOX_GROUP])["created"] == 1
    assert_disjoint()


def model_asset_ids(rel_dir: str) -> List[str]:
    """The ids of every registered MODEL asset under ``rel_dir``, in path order."""
    with Session(engine) as session:
        assets = session.exec(select(Asset).where(Asset.storage_path.is_not(None)).order_by(Asset.storage_path))
        return [
            asset.id
            for asset in assets
            if asset.storage_path.startswith(rel_dir + "/")
            and bundle_service._suffix(asset.storage_path) in library_scan.MODEL_SUFFIXES
        ]


def test_a_created_project_key_is_not_re_offered_as_a_descendant(
    client: TestClient, isolated_storage: Path
) -> None:
    """Deleting the mount's Folder changes nothing: the mount is not a row at all.

    The former C2 re-offer needed the marker to offer the group again, and the
    folder check then decided whether it was creatable. Under Mf-z643 the group is
    INSIDE the project key, so it is pruned before any pass and never a row - a
    create of it answers ``unknown bundle`` whatever the folder table says.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    scan()
    headers = auth_header(client)
    assert create_for_exclusion(client, headers, [XBOX_GROUP])["created"] == 1
    with Session(engine) as session:
        folder = session.exec(select(Folder).where(Folder.storage_path == mount_key())).first()
        assert folder is not None
        session.delete(folder)
        session.commit()

    report = detect(client, headers, limit=1000)
    assert [row["key"] for row in report["bundles"]] == [f"import/{XBOX}"]
    assert row_for(report, f"import/{XBOX}")["existing_project_match"] == "marker"

    blocked = create(client, headers, [{"key": mount_key()}])
    assert blocked["blocked"] == 0
    assert blocked["failed"] == 1
    assert blocked["results"][0]["reason"] == "unknown bundle"
    assert table_counts()["projects"] == 1


def test_a_second_create_of_a_grouped_row_is_already_created(
    client: TestClient, isolated_storage: Path
) -> None:
    """A grouped create is idempotent, and a later exclusion never rewrites the marker.

    The second create is a marker hit; the third sends a DIFFERENT exclusion and
    still answers ``already_created`` with the project byte-identical, because
    the marker is proof and the row is never touched again. Mutation: skip the
    existing-project check for a grouped row and the project gets a second
    attach pass or a rewritten marker.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    scan()
    headers = auth_header(client)

    first = create(client, headers, [{"key": f"import/{XBOX}", "name": "shell only", "shape": "one-project"}])
    assert first["created"] == 1
    project_id = first["results"][0]["project_id"]
    with Session(engine) as session:
        metadata_before = session.get(Project, project_id).metadata_json

    second = create(client, headers, [{"key": f"import/{XBOX}", "shape": "one-project"}])

    assert second["skipped"] == 1, second
    assert second["results"][0]["reason"] == "already_created"
    row = row_for(detect(client, headers, limit=1000), f"import/{XBOX}")
    assert row["existing_project_match"] == "marker"

    third = create_for_exclusion(client, headers, [XBOX_GROUP])

    # The row still carries the engine's groups, so the exclusion validates; the
    # marker is what answers, and the project is never touched again.
    assert third["skipped"] == 1
    assert third["results"][0]["reason"] == "already_created"
    with Session(engine) as session:
        assert session.get(Project, project_id).metadata_json == metadata_before
    assert table_counts()["projects"] == 1
    assert member_count(project_id) == XBOX_MEMBERS, (
        "the skip attached nothing a second time; the project still holds every "
        "registered file the first create gave it (Mf-7oow)"
    )

    # Choosing the shape explicitly validates the exclusion again and still
    # answers from the marker: a re-click never rewrites what a human has.
    retry = bundle_service.create_bundles(
        [bundle_service.BundleCreateItem(key=f"import/{XBOX}", shape="one-project", exclude_groups=(XBOX_GROUP,))],
        audit_ctx(),
    )
    assert retry["skipped"] == 1
    assert retry["results"][0]["reason"] == "already_created"


def test_a_second_create_of_the_split_group_is_unknown_bundle(
    client: TestClient, isolated_storage: Path
) -> None:
    """A descendant of a project key is never a row, so it can never be created.

    This used to witness the split row's idempotency. Since Mf-z643 the mount sits
    inside the created project's key and is pruned before any pass, so both the
    first and the second create answer ``unknown bundle`` and only the grouped
    project exists.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    insert_folder_row(XBOX_GROUP, mount_key())
    scan()
    headers = auth_header(client)
    assert create_for_exclusion(client, headers, [XBOX_GROUP])["created"] == 1
    split = create(client, headers, [{"key": mount_key()}])
    assert split["failed"] == 1
    assert split["results"][0]["reason"] == "unknown bundle"

    second = create(client, headers, [{"key": mount_key()}])

    assert second["failed"] == 1
    assert second["results"][0]["reason"] == "unknown bundle"

    report = detect(client, headers, limit=1000)
    assert [row["key"] for row in report["bundles"]] == [f"import/{XBOX}"]
    assert all(row["existing_project_id"] for row in report["bundles"])
    assert report["counts"]["already_created"] == 1
    assert table_counts()["projects"] == 1


def test_a_hand_made_project_over_the_mount_files_is_never_contradicted(
    client: TestClient, isolated_storage: Path
) -> None:
    """A descendant of a project key has no row, so no identity question is asked.

    The former witness reported the hand-made mount project as an ``assets`` hit on
    the re-offered mount row. Mf-z643 prunes everything inside the created project
    key, so the mount has no row to carry that identity - and the report never
    contradicts the project it cannot see: it simply names the mount nowhere.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    scan()
    headers = auth_header(client)
    assert create_for_exclusion(client, headers, [XBOX_GROUP])["created"] == 1

    with Session(engine) as session:
        hand_made = project_service.create_project(session, "Someone Else's Mount", None, None, origin="imported")
    project_service.add_assets(hand_made.id, model_asset_ids(mount_key()))

    report = detect(client, headers, limit=1000)

    assert [row["key"] for row in report["bundles"]] == [f"import/{XBOX}"]
    assert mount_key() not in {row["key"] for row in report["bundles"]}
    assert project_storage_paths(hand_made.id) == {
        f"{mount_key()}/bracket-{index}.stl" for index in range(XBOX_GROUP_MODELS)
    }
    assert table_counts()["projects"] == 2


def test_the_marker_names_the_groups_and_their_counts(
    client: TestClient, isolated_storage: Path
) -> None:
    """The marker's group list is the row's, in order, plus what was attached.

    Since Mf-94od `model_count` is what the SCAN registers, and `attached` is the
    same registered set filtered by the exclusion, so an unexcluded row
    reads `6/6` per Rocket group and only an EXCLUSION can separate the two. The
    account still adds up: for the xbox tree 31 attached minus the foreign 4 is
    the product's own 27. Mutation: count `attached` from the snapshot instead of
    the registered models and the excluded xbox case below reds, because the
    excluded foreign group would still report its 4 as attached. The account's left
    side asks the MODEL question since Mf-wsdo, because `assets_added` is the ATTACH
    answer and the two are no longer the same number.
    """
    write_stagetop(isolated_storage)
    write_rocket(isolated_storage)
    write_xbox360(isolated_storage)
    keys = {"stagetop": f"import/{STAGETOP}", "rocket": f"import/{ROCKET}", "xbox": f"import/{XBOX}"}
    for key in keys.values():
        insert_folder_row(key.rsplit("/", 1)[-1], key)
    scan()
    headers = auth_header(client)

    # The marker is written from the row the create was made from, so the rows are
    # read BEFORE the create - after it the key's subtree is pruned and the row is
    # re-evaluated over the whole subtree (Mf-z643), which no longer carries groups.
    before = {name: service_row(key) for name, key in keys.items()}
    created = create(
        client,
        headers,
        [
            {"key": key, "name": f"{name} parts", **({"shape": "one-project"} if name == "xbox" else {})}
            for name, key in keys.items()
        ],
    )

    assert created["created"] == 3
    for name, result in zip(keys, created["results"], strict=True):
        marker = project_marker(result["project_id"])
        assert [
            {"name": group["name"], "model_count": group["model_count"], "foreign": group["foreign"]}
            for group in marker["groups"]
        ] == [
            {"name": group["name"], "model_count": group["model_count"], "foreign": group["foreign"]}
            for group in before[name]["groups"]
        ], name
        assert all(group["included"] is True for group in marker["groups"]), name

    rocket_marker = project_marker(created["results"][1]["project_id"])
    assert [(group["model_count"], group["attached"]) for group in rocket_marker["groups"]] == [(6, 6)] * len(
        ROCKET_GROUPS
    )

    xbox_marker = project_marker(created["results"][2]["project_id"])
    # Re-expressed for Mf-wsdo: `assets_added` is the ATTACH answer (models PLUS images)
    # and this account is about models, so the left side asks the model question. The
    # xbox fixture ships no image, so no number moves today - the re-expression is what
    # keeps the account honest the day one does.
    xbox_models = [
        path
        for path in project_storage_paths(created["results"][2]["project_id"])
        if bundle_service._suffix(path) in library_scan.MODEL_SUFFIXES
    ]
    assert len(xbox_models) - sum(
        group["attached"] for group in xbox_marker["groups"] if group["included"]
    ) == XBOX_OWN_MODELS


def test_a_create_with_an_exclusion_writes_the_six_key_marker_group(
    client: TestClient, isolated_storage: Path
) -> None:
    """The create door writes every marker group through the one helper, all six keys.

    ``included: False`` is what `bundle_drift.excluded_prefixes` subtracts on, and
    ``separable`` is the wire's honesty flag, so both must reach the written marker.

    Mutation: build the marker group inline again and drop ``separable`` and the
    key-order assertion reds; write `included` as `True` unconditionally and the
    exclusion assertion reds.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    scan()
    headers = auth_header(client)

    created = create_for_exclusion(client, headers, [XBOX_GROUP])

    assert created["created"] == 1
    groups = project_marker(created["results"][0]["project_id"])["groups"]
    assert len(groups) == 1
    group = groups[0]
    assert list(group) == ["name", "model_count", "foreign", "separable", "included", "attached"]
    assert (group["name"], group["foreign"], group["separable"]) == (XBOX_GROUP, True, True)
    assert group["included"] is False
    assert group["attached"] == 0


def test_a_group_free_marker_is_unchanged_in_both_directions(
    client: TestClient, isolated_storage: Path
) -> None:
    """The group marker additions are additive-only: six keys ordinary, eight with groups.

    Mutation: write `shape`/`groups`/`cover` unconditionally and the six-key
    assertions red.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    insert_folder_row(XBOX_GROUP, mount_key())
    thingiverse_bundle(isolated_storage, "Ordinary Thing", 424242)
    insert_folder_row("Ordinary Thing", "Ordinary Thing")
    scan()
    headers = auth_header(client)

    created = create(
        client,
        headers,
        [
            {"key": "Ordinary Thing", "name": "ordinary thing"},
            {
                "key": f"import/{XBOX}",
                "name": "compound thing",
                "shape": "one-project",
                "exclude_groups": [XBOX_GROUP],
            },
        ],
    )
    assert created["created"] == 2

    ordinary = project_marker(created["results"][0]["project_id"])
    compound = project_marker(created["results"][1]["project_id"])
    # Every marker now records the shape it was made from and the rule that offered
    # it (Mf-z643), so the six keys became eight; a grouped row adds its groups and
    # cover, and an ordinary row still does not.
    eight = {"key", "detector", "confidence", "rel_dir", "archive", "twin_verified", "origin", "shape", "rule"}
    # The engine supplies groups for every model-bearing subfolder, so both
    # markers carry the group block; the xbox one is the foreign group.
    assert set(ordinary) - {"enrichment"} == eight | {"groups", "cover"}
    assert (ordinary["shape"], ordinary["rule"]) == ("one-project", "engine:one-project")
    assert [group["name"] for group in ordinary["groups"]] == ["files"]
    assert set(compound) - {"enrichment"} == eight | {"groups", "cover"}
    assert (compound["shape"], compound["rule"]) == ("one-project", "engine:one-project")
    assert [group["name"] for group in compound["groups"]] == [XBOX_GROUP]


def test_an_exclusion_of_a_group_that_was_never_a_row_is_refused(
    client: TestClient, isolated_storage: Path
) -> None:
    """A FOREIGN group that is not separable has nowhere to go, so it is refused.

    The toolkit's group is foreign off its own name and holds four parts, but it
    was never a row of its own - R1, R3 and R5 each refuse it - so creating the
    row without it would leave its parts in no project and in no row, the
    ticket's vanish. Mutation: treat every group as separable and the refusal
    becomes a project holding 9 of its 13 parts.
    """
    key = write_foreign_group_never_a_row(isolated_storage)
    insert_folder_row(A_COMPOUND, key)
    scan()
    headers = auth_header(client)

    row = service_row(key)
    assert row["groups"] == [{"name": A_LONE_GROUP, "model_count": 4, "foreign": True, "separable": False}]

    refused = create(client, headers, [{"key": key, "shape": "one-project", "exclude_groups": [A_LONE_GROUP]}])

    assert refused["failed"] == 1
    assert refused["results"][0]["reason"] == "not_separable_group"
    assert table_counts()["projects"] == 0

    created = create(client, headers, [{"key": key, "shape": "one-project"}])
    assert created["created"] == 1
    held = project_storage_paths(created["results"][0]["project_id"])
    assert held == registered_asset_paths(key)
    assert f"import/{A_COMPOUND}/{A_LONE_GROUP}/plates/bracket-0.stl" in held


def test_the_three_exclusion_refusals(client: TestClient, isolated_storage: Path) -> None:
    """Every unusable exclusion is a `failed` with its own reason, and creates nothing.

    ``unknown_group`` covers a name no group carries and a differently-cased
    spelling of one that is there; ``not_a_foreign_group`` covers a group of the
    product's own, witnessed by both Stagetop groups (the edition and the wave);
    ``not_separable_group`` covers a FOREIGN group that
    was never a row, witnessed by the toolkit's lone group. The rest are the edges
    of the rule: an empty list is a no-op even on a non-compound row, a duplicated
    name still attaches once, a FILE that happens to be named exactly
    ``<key>/<name>`` is not the group, and an already-created row answers the
    validation reason rather than ``already_created``. Mutation: drop the trailing
    slash from the prefix and the file-named-group case attaches only one model;
    move validation after ``find_existing_project`` and the bogus-name case
    answers ``already_created``.
    """
    write_xbox360(isolated_storage)
    write_stagetop(isolated_storage)
    write_foreign_group_never_a_row(isolated_storage)
    moai_bundle(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    insert_folder_row(STAGETOP, f"import/{STAGETOP}")
    insert_folder_row(A_COMPOUND, f"import/{A_COMPOUND}")
    insert_folder_row(MOAI, MOAI)
    write_file(isolated_storage / "Inbox" / "part-0.stl", STL_BYTES)
    write_file(isolated_storage / "Inbox" / "foreign.stl", STL_BYTES)
    scan()
    headers = auth_header(client)

    def reason(items: List[dict]) -> Optional[str]:
        return create(client, headers, items)["results"][0]["reason"]

    assert reason(
        [{"key": f"import/{XBOX}", "shape": "one-project", "exclude_groups": ["No Such Group"]}]
    ) == "unknown_group"
    assert reason(
        [{"key": f"import/{XBOX}", "shape": "one-project", "exclude_groups": [XBOX_GROUP.casefold()]}]
    ) == "unknown_group"
    assert reason([{"key": f"import/{STAGETOP}", "exclude_groups": [STAGETOP_WAVE]}]) == "not_a_foreign_group"
    assert reason([{"key": f"import/{STAGETOP}", "exclude_groups": [STAGETOP_WAVE_ONE]}]) == "not_a_foreign_group"
    assert reason(
        [{"key": f"import/{A_COMPOUND}", "shape": "one-project", "exclude_groups": [A_LONE_GROUP]}]
    ) == "not_separable_group"
    assert table_counts()["projects"] == 0, "a refused exclusion creates nothing"

    plain = create(client, headers, [{"key": MOAI, "exclude_groups": []}])
    assert plain["created"] == 1, "an empty list is a no-op on a non-compound row too"

    duplicated = create(
        client,
        headers,
        [{"key": f"import/{XBOX}", "shape": "one-project", "exclude_groups": [XBOX_GROUP, XBOX_GROUP]}],
    )
    assert duplicated["created"] == 1
    assert duplicated["results"][0]["assets_added"] == XBOX_OWN_MODELS
    assert project_storage_paths(duplicated["results"][0]["project_id"]) == {
        f"import/{XBOX}/shell-{index}.stl" for index in range(XBOX_OWN_MODELS)
    }

    bogus = create(
        client,
        headers,
        [{"key": f"import/{XBOX}", "shape": "one-project", "exclude_groups": ["No Such Group"]}],
    )
    assert bogus["failed"] == 1
    assert bogus["results"][0]["reason"] == "unknown_group", "validation runs before the marker check"

    # A FILE whose name matches a group's is not that group: `foreign.stl` sits
    # beside the `foreign/` directory, and excluding the directory keeps the file.
    write_file(isolated_storage / "Inbox" / "foreign.stl", STL_BYTES)
    write_text(isolated_storage / "Inbox" / "foreign" / "README.txt", "a foreign group\n")
    write_file(isolated_storage / "Inbox" / "foreign" / "bracket.stl", STL_BYTES)
    scan()
    hand_row = service_row("Inbox")
    candidate = next(
        item for item in bundle_service.iter_bundle_candidates(isolated_storage)[0] if item.key == "Inbox"
    )
    assert [group["name"] for group in hand_row["groups"]] == ["foreign"]

    named_like_a_file = bundle_service._create_one(
        bundle_service.BundleCreateItem(
            key="Inbox", name_only=True, shape="one-project", exclude_groups=("foreign",)
        ),
        hand_row,
        candidate,
        audit_ctx(),
    )

    assert named_like_a_file["status"] == "created"
    assert named_like_a_file["assets_added"] == 2, "the file `Inbox/foreign.stl` is not the group"
    assert project_storage_paths(named_like_a_file["project_id"]) == {
        "Inbox/part-0.stl",
        "Inbox/foreign.stl",
    }


def test_a_split_sent_with_its_parent_in_one_request_fails_on_the_second_item(
    client: TestClient, isolated_storage: Path
) -> None:
    """One request cannot both record an exclusion and create from it.

    The report is recomputed once, before any item runs, so the mount is not a row
    when the second item is looked up: ``unknown bundle``. The NEXT read does not
    offer it either - it is inside the created project's key and descendant paths
    are not detection rows (Mf-z643) - so the answer is stable rather than a race.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    insert_folder_row(XBOX_GROUP, mount_key())
    scan()
    headers = auth_header(client)

    created = create(
        client,
        headers,
        [
            {"key": f"import/{XBOX}", "shape": "one-project", "exclude_groups": [XBOX_GROUP]},
            {"key": mount_key()},
        ],
    )

    assert created["created"] == 1
    assert created["failed"] == 1
    assert created["results"][0]["status"] == "created"
    assert created["results"][1]["reason"] == "unknown bundle"

    assert row_for(detect(client, headers, limit=1000), f"import/{XBOX}")["existing_project_match"] == "marker"
    assert mount_key() not in {row["key"] for row in detect(client, headers, limit=1000)["bundles"]}


def test_the_create_audit_records_the_excluded_groups(client: TestClient, isolated_storage: Path) -> None:
    """Every create names its exclusions, `[]` included, so a reader can tell none from absent.

    Mutation: write the field only when something was excluded and the three `[]`
    assertions red; read the excluded names from `item.exclude_groups` instead of
    the matched group order and the duplicated-name create records the name twice.
    """
    write_xbox360(isolated_storage)
    shutil.copytree(isolated_storage / "import" / XBOX, isolated_storage / "import" / f"{XBOX}-copy")
    shutil.copytree(isolated_storage / "import" / XBOX, isolated_storage / "import" / f"{XBOX}-copy-2")
    # A distinct vendor id per copy, or the create door's source-url identity would
    # (correctly) skip them as the same project.
    for index, name in enumerate((f"{XBOX}-copy", f"{XBOX}-copy-2"), start=1):
        write_thingiverse_docs(isolated_storage / "import" / name / XBOX_GROUP, XBOX_THING + index)
    thingiverse_bundle(isolated_storage, "Ordinary Thing", 515151)
    insert_folder_row(XBOX, f"import/{XBOX}")
    insert_folder_row(f"{XBOX}-copy", f"import/{XBOX}-copy")
    insert_folder_row(f"{XBOX}-copy-2", f"import/{XBOX}-copy-2")
    insert_folder_row("Ordinary Thing", "Ordinary Thing")
    scan()
    headers = auth_header(client)

    created = create(
        client,
        headers,
        [
            {
                "key": f"import/{XBOX}",
                "name": "excluded shell",
                "shape": "one-project",
                "exclude_groups": [XBOX_GROUP],
            },
            {"key": f"import/{XBOX}-copy", "name": "whole copy", "shape": "one-project"},
            {
                "key": f"import/{XBOX}-copy-2",
                "name": "twice-excluded copy",
                "shape": "one-project",
                "exclude_groups": [XBOX_GROUP, XBOX_GROUP],
            },
            {"key": "Ordinary Thing", "name": "ordinary thing"},
        ],
    )
    assert created["created"] == 4

    with Session(engine) as session:
        events = session.exec(select(AuditEvent).where(AuditEvent.action == audit.BUNDLE_CREATE)).all()
    details = {event.target: json.loads(event.detail_json or "{}") for event in events}

    assert details[f"import/{XBOX}"]["excluded_groups"] == [XBOX_GROUP]
    assert details[f"import/{XBOX}-copy"]["excluded_groups"] == []
    assert details[f"import/{XBOX}-copy-2"]["excluded_groups"] == [XBOX_GROUP]
    assert details["Ordinary Thing"]["excluded_groups"] == []


def test_a_registered_asset_that_vanished_from_the_disk_still_answers_the_belt(
    client: TestClient, isolated_storage: Path
) -> None:
    """The belt is live on a `low` compound row and its assets stay UNFILTERED.

    ``assets``/``own_assets`` are handed to the belt unfiltered on purpose: the
    only difference between them here is a file deleted after it was registered,
    which is another bundle's asset as far as the belt can tell - so the create is
    refused and nothing is written. This create sends no ``exclude_groups``, so it
    cannot witness the exclusion filter; that mutation is witnessed by
    ``test_excluding_the_foreign_group_creates_one_project_of_its_own_27`` (12
    tests red under it, including that one). Mutation: exempt the compound row
    from the belt and this witness creates a project.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    scan()
    (isolated_storage / "import" / XBOX / "shell-0.stl").unlink()
    headers = auth_header(client)

    created = create(client, headers, [{"key": f"import/{XBOX}", "shape": "one-project"}])

    assert created["failed"] == 1
    assert created["results"][0]["reason"] == "contains_bundles"
    assert table_counts()["projects"] == 0


def test_the_cover_never_picks_an_unregistered_image_under_an_excluded_group(
    client: TestClient, isolated_storage: Path
) -> None:
    """An excluded group's image cannot become the project's face.

    The row's cover names an image the scan never registered under the group; once
    that group is excluded the facts' cover entry is dropped and the same `_cover`
    rule is re-asked over the included parts, so the product's own model wins.
    """
    write_file(isolated_storage / "Inbox" / "part-0.stl", STL_BYTES)
    write_file(isolated_storage / "Inbox" / "foreign" / "bracket.stl", STL_BYTES)
    write_text(isolated_storage / "Inbox" / "foreign" / "README.txt", "a foreign group\n")
    scan()
    # Written after the scan: no Asset row, so the unregistered cover leg is live.
    write_file(isolated_storage / "Inbox" / "foreign" / "cover.jpg", b"j" * 900)

    row = service_row("Inbox")
    assert row["cover"] == {
        "asset_id": None,
        "rel_path": "foreign/cover.jpg",
        "reason": "largest-image-unregistered",
        "url": None,
        "thumb_url": None,
    }
    candidate = next(
        item for item in bundle_service.iter_bundle_candidates(isolated_storage)[0] if item.key == "Inbox"
    )

    result = bundle_service._create_one(
        bundle_service.BundleCreateItem(
            key="Inbox", name_only=True, shape="one-project", exclude_groups=("foreign",)
        ),
        row,
        candidate,
        audit_ctx(),
    )

    assert result["status"] == "created"
    with Session(engine) as session:
        project = session.get(Project, result["project_id"])
        cover = session.get(Asset, project.cover_asset_id)
    assert cover.storage_path == "Inbox/part-0.stl"
    assert not cover.storage_path.startswith("Inbox/foreign/")
    assert "foreign/" not in cover.storage_path, "no cover rel_path may sit under the excluded group"


def test_the_cover_never_picks_a_registered_image_under_an_excluded_group(
    client: TestClient, isolated_storage: Path
) -> None:
    """The ``if prefixes:`` re-ask is over the OWNED and EXCLUDED-OUT set, not the row's.

    The excluded group's `cover.jpg` is a REGISTERED asset, so it is the face the
    row advertises before the exclusion. The create re-asks the same `_cover` rule
    over `owned_assets` minus the excluded prefixes, so the product's own model
    wins and the image is not a member either.
    """
    write_file(isolated_storage / "Inbox" / "part-0.stl", STL_BYTES)
    write_file(isolated_storage / "Inbox" / "foreign" / "bracket.stl", STL_BYTES)
    write_text(isolated_storage / "Inbox" / "foreign" / "README.txt", "a foreign group\n")
    write_file(isolated_storage / "Inbox" / "foreign" / "cover.jpg", b"j" * 900)
    scan()

    image_id = asset_id_at("Inbox/foreign/cover.jpg")
    row = service_row("Inbox")
    assert row["cover"]["asset_id"] == image_id
    assert row["cover"]["rel_path"] == "Inbox/foreign/cover.jpg"
    candidate = next(
        item for item in bundle_service.iter_bundle_candidates(isolated_storage)[0] if item.key == "Inbox"
    )

    result = bundle_service._create_one(
        bundle_service.BundleCreateItem(
            key="Inbox", name_only=True, shape="one-project", exclude_groups=("foreign",)
        ),
        row,
        candidate,
        audit_ctx(),
    )

    assert result["status"] == "created", (
        "a cover the exclusion removed is re-asked, not carried over - and it is not a "
        "cover outside the member set either, which the primitive would refuse"
    )
    project_id = result["project_id"]
    with Session(engine) as session:
        project = session.get(Project, project_id)
        cover = session.get(Asset, project.cover_asset_id)

    assert cover.storage_path == "Inbox/part-0.stl", (
        "the product's own model is the face once the excluded group's image is out of "
        "the owned set - the same `_cover` rule, re-asked, not a second rule"
    )
    assert project.cover_asset_id != image_id
    assert project_storage_paths(project_id) == {"Inbox/part-0.stl"}, (
        "the excluded group's image is not a member either, so the cover and the member "
        "set agree about what the exclusion meant"
    )


def test_the_marker_names_the_group_of_an_unregistered_cover(isolated_storage: Path) -> None:
    """An unregistered cover is attributed to its group in ONE key space.

    ``_cover``'s registered legs name a storage-relative ``asset.path``; its
    ``largest-image-unregistered`` leg names a BUNDLE-relative entry.
    ``_create_one`` settles the one storage-relative form before the marker is
    built, so the same field is never recorded in two spaces.
    """
    write_file(isolated_storage / "Inbox" / "part-0.stl", STL_BYTES)
    write_file(isolated_storage / "Inbox" / "part-1.stl", STL_BYTES)
    write_text(isolated_storage / "Inbox" / "gear" / "README.txt", "a gear group\n")
    write_file(isolated_storage / "Inbox" / "gear" / "bracket.stl", STL_BYTES)
    write_file(isolated_storage / "Inbox" / "grip-5461913" / "grip.stl", STL_BYTES)
    scan()
    # Written after the scan, so the largest image has no Asset row and the
    # unregistered cover leg names it.
    write_file(isolated_storage / "Inbox" / "gear" / "cover.jpg", b"j" * 900)

    row = service_row("Inbox")
    assert row["cover"]["reason"] == "largest-image-unregistered"
    assert row["cover"]["rel_path"] == "gear/cover.jpg", "the row's unregistered leg is bundle-relative"
    candidate = next(
        item for item in bundle_service.iter_bundle_candidates(isolated_storage)[0] if item.key == "Inbox"
    )

    result = bundle_service._create_one(
        bundle_service.BundleCreateItem(key="Inbox", name_only=True, shape="one-project"),
        row,
        candidate,
        audit_ctx(),
    )

    assert result["status"] == "created"
    marker = project_marker(result["project_id"])
    assert marker["cover"]["reason"] == "largest-image-unregistered"
    assert marker["cover"]["rel_path"] == "Inbox/gear/cover.jpg"
    assert marker["cover"]["group"] == "gear"
    with Session(engine) as session:
        assert session.get(Project, result["project_id"]).cover_asset_id is None


# --- drift on an already-created row (Mf-8i04) ------------------------------


def report_statements(*, mention: str = "") -> List[str]:
    """The statements one ``build_report`` runs, whitespace-normalised and optionally filtered."""
    statements: List[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        bundle_service.build_report(limit=1000)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return [statement for statement in statements if mention in statement]


REGISTERED_INDEX_SQL = "select asset.id, asset.storage_path, asset.size"


def project_drift_out(client: TestClient, headers: dict, project_id: str) -> dict:
    """The project route's body, so the two surfaces can be compared to each other."""
    response = client.get(f"/project/{project_id}/bundle-drift", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def write_drift_compound(storage: Path) -> str:
    """The measured compound shape, sized so the naive over-count equals the model count.

    Two editions of the SAME product (the real Stagetop names, so R7's family
    token is the evidence it acts on), each holding two part folders, and every
    part folder ships two models PLUS the ``cover.jpg`` and ``notes.txt`` the
    create door never attaches. Eight models and eight support files are
    registered, so the naive all-asset difference reads 8 where the honest drift
    is 0 - the ratio the owner's tree measured as 26 and 26 of 52.
    """
    root = storage / "import" / "DriftCompound"
    for edition in (STAGETOP_WAVE_ONE, STAGETOP_WAVE):
        for part in ("Part 0", "Part 1"):
            folder = root / edition / part
            for index in range(2):
                write_file(folder / f"part-{index}.stl", b"s" * 40)
            write_file(folder / "cover.jpg", b"j" * 40)
            write_text(folder / "notes.txt", "print at 0.2mm\n")
    return "import/DriftCompound"


def service_assets_under(rel_dir: str) -> List[Tuple[str, str, int]]:
    """Every registered row under ``rel_dir`` as ``(id, path, size)``, read from the DB."""
    with Session(engine) as session:
        registered = bundle_service.registered_index(session)
    return [
        (asset_id, path, size)
        for asset_id, path, size in registered
        if path.startswith(rel_dir + "/")
    ]


def test_service_a_grown_bundle_row_reports_the_new_parts(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-3/AC-7: the already-created row names the new parts, and no create is re-offered.

    The xbox compound row is created with every part, so the row answers
    ``known``/0. One model is then written into the folder and rescanned: the row
    is still a marker match with the same ``existing_project_id`` - the marker's
    job is duplicate prevention, and it is unchanged - but now carries
    ``bundle_drift_state`` ``known`` and ``bundle_drift_count`` 1. The project
    route names the same asset, and adding exactly it clears both. A second
    create for that key is still ``skipped``/``already_created`` with one Project.

    Mutation: emit 0 for an unresolvable walk root and the grown row reads 0
    while the route finds the part; drop either key from the schema and the
    response model silently loses it.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    scan()
    headers = auth_header(client)

    created = create(client, headers, [{"key": f"import/{XBOX}", "shape": "one-project"}])

    assert created["created"] == 1
    project_id = created["results"][0]["project_id"]
    fresh = service_row(f"import/{XBOX}")
    assert fresh["existing_project_match"] == "marker"
    assert fresh["bundle_drift_state"] == "known"
    assert fresh["bundle_drift_count"] == 0
    assert project_drift_out(client, headers, project_id)["count"] == 0

    write_file(isolated_storage / "import" / XBOX / "new-part.stl", b"s" * 40)
    scan()
    new_id = asset_id_at(f"import/{XBOX}/new-part.stl")

    grown = service_row(f"import/{XBOX}")

    assert grown["existing_project_match"] == "marker"
    assert grown["existing_project_id"] == project_id
    assert grown["bundle_drift_state"] == "known"
    assert grown["bundle_drift_count"] == 1
    drift = project_drift_out(client, headers, project_id)
    assert drift == {"state": "known", "count": 1, "asset_ids": [new_id]}
    assert drift["count"] == len(drift["asset_ids"])

    # AC-7: the marker still owns the key, so create is never re-offered.
    second = create(client, headers, [{"key": f"import/{XBOX}", "shape": "one-project"}])
    assert second["created"] == 0
    assert second["skipped"] == 1
    assert second["results"][0]["reason"] == "already_created"
    with Session(engine) as session:
        assert len(session.exec(select(Project)).all()) == 1

    added = client.post(
        f"/project/{project_id}/assets", json={"asset_ids": drift["asset_ids"]}, headers=headers
    ).json()

    assert added["added"] == [new_id]
    assert service_row(f"import/{XBOX}")["bundle_drift_count"] == 0
    assert project_drift_out(client, headers, project_id) == {
        "state": "known",
        "count": 0,
        "asset_ids": [],
    }


def test_service_a_fresh_bundle_project_reports_no_new_parts(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-4: creation attaches every member, so both surfaces answer the honest zero.

    The measured compound shape: eight models in four part folders, each folder also
    holding the ``cover.jpg`` and ``notes.txt`` the scan registers. Since Mf-7oow the
    create attaches ALL sixteen - a bundle project holds every registered file under
    its key - so the queue row and the project route both answer ``known``/0 AND the
    naive all-asset difference answers 0 as well. That agreement is the point: the
    naive difference used to be non-zero (4 under Mf-wsdo's models-plus-images table,
    8 before it) precisely because membership was a suffix table, so "everything the
    scan registered" and "everything the project holds" were two different sets.
    Mutation: filter the member set down to models (or re-add any suffix filter) and
    ``assets_added`` reads 8, ``naive`` reads 8 and this reds.
    """
    key = write_drift_compound(isolated_storage)
    insert_folder_row("DriftCompound", key)
    scan()
    headers = auth_header(client)

    created = create(client, headers, [{"key": key}])

    assert created["created"] == 1
    result = created["results"][0]
    project_id = result["project_id"]
    assert result["assets_added"] == 16, (
        "all sixteen registered files: the eight models, the four group covers AND the "
        "four ``notes.txt`` (Mf-7oow - membership is every registered asset under the key)"
    )
    assert project_storage_paths(project_id) == registered_asset_paths(key)

    registered = service_assets_under(key)
    members = set(project_asset_ids(client, headers, project_id))
    naive = [path for asset_id, path, _size in registered if asset_id not in members]

    assert len(registered) == 16, "eight models and eight support files"
    assert len(members) == 16 and naive == [], (
        "the naive all-asset difference is EMPTY: membership and registration are one "
        "set now, so there is nothing left for a drift prompt to offer"
    )

    row = service_row(key)
    assert row["bundle_drift_state"] == "known"
    assert row["bundle_drift_count"] == 0
    assert project_drift_out(client, headers, project_id) == {
        "state": "known",
        "count": 0,
        "asset_ids": [],
    }


def test_service_an_excluded_group_is_not_drift(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-5: an excluded group's models stay unattached and are never offered as new parts.

    The compound create excludes the foreign group, so its four models are
    registered and unattached BY DESIGN. The marker records ``included: False``,
    and drift subtracts that prefix: the row and the route both answer
    ``known``/0, and no id the route offers sits under the group's path.

    Mutation: drop the ``included: False`` subtraction and the row answers 4
    forever, with "Add them" attaching exactly what the operator excluded.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    scan()
    headers = auth_header(client)

    created = create_for_exclusion(client, headers, [XBOX_GROUP])

    assert created["created"] == 1
    project_id = created["results"][0]["project_id"]
    marker = project_marker(project_id)
    assert [(group["name"], group["included"]) for group in marker["groups"]] == [(XBOX_GROUP, False)]

    excluded_prefix = f"import/{XBOX}/{XBOX_GROUP}/"
    unattached = registered_model_paths(f"import/{XBOX}") - project_storage_paths(project_id)
    assert len(unattached) == XBOX_GROUP_MODELS
    assert all(path.startswith(excluded_prefix) for path in unattached), "only the excluded group is unattached"

    row = service_row(f"import/{XBOX}")
    assert row["bundle_drift_state"] == "known"
    assert row["bundle_drift_count"] == 0
    drift = project_drift_out(client, headers, project_id)
    assert drift == {"state": "known", "count": 0, "asset_ids": []}
    assert not any(path.startswith(excluded_prefix) for path in project_storage_paths(project_id))


def test_service_a_stale_walk_root_reports_unknown_rather_than_a_zero(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-2 on the QUEUE: a root no registered row lives under is `unknown`, not 0.

    The route pins the real rename; this pins the queue's own wiring, which is the
    half `bundle_service` owns: the row answers drift from ONE report-wide index,
    so its presence probe is `bundle_drift.has_rows_under(registered, root)` and
    adds no statement. The reachable queue shape is a root that still walks as a
    candidate with no registered row under it - its registered rows deleted, or a
    folder copied in ahead of the rescan. The app's own rename does not reach the
    queue: for a detection marker it moves the key too, so the row matches by
    shared files and reads `not_applicable` instead, and a zip marker's identity
    key never equals a candidate path at all, so that row is `not_applicable` as
    well. The fixture below is deliberately synthetic - `rel_dir` differs from
    the marker's `key`, which no door writes - because what is under test is the
    WIRING (marker match plus the presence answer), not a production marker.
    What it pins is the answer: `unknown`/null, never the `known`/0 it used to
    claim.

    Mutation: always answer the probe `True` (or drop it) and this row reads
    `known`/0 - the false zero the ticket forbids, on the surface the ticket is
    named for.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    scan()
    headers = auth_header(client)
    with Session(engine) as session:
        stale = project_service.create_project(session, "Stale Root", None, None, origin="imported")
    project_service.set_project_metadata_marker(
        stale.id, {"_bundle": {"key": f"import/{XBOX}", "rel_dir": "import/Gone"}}
    )

    row = row_for(detect(client, headers, limit=1000), f"import/{XBOX}")

    assert row["existing_project_id"] == stale.id
    assert row["existing_project_match"] == "marker", "the key still names this project"
    assert row["bundle_drift_state"] == "unknown"
    assert row["bundle_drift_count"] is None
    # Measured, not asserted from prose: the presence probe reads the report-wide
    # `registered` index this row already holds, so a whole report over this tree
    # costs 8 statements (folders, memberships, the registered index, the settings
    # reads, this candidate's own reads, and the ONE `select(Project)` that derives
    # `excluded_keys` before the walk). A probe that queried per candidate would
    # be 9.
    assert len(report_statements()) == 8


def test_service_the_drift_index_is_one_statement_per_report(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-8: the registered index is ONE statement, however many candidates there are.

    Two candidates and then five: exactly one statement selects
    ``asset.id, asset.storage_path, asset.size`` in each report, so drift is
    answered from a report-wide list rather than one read per row. The whole
    report's statement count grows only by the existing per-candidate asset read,
    which is measured here so the index's own cost stays pinned.

    Mutation: call ``registered_assets`` per row to answer drift and the index
    count becomes one per candidate (2, then 5).
    """
    for index in range(2):
        thingiverse_bundle(isolated_storage, f"Two {index}", 1000 + index)
    scan()

    two_candidates = len(report_statements())
    two_index = report_statements(mention=REGISTERED_INDEX_SQL)

    for index in range(2, 5):
        thingiverse_bundle(isolated_storage, f"Two {index}", 1000 + index)
    scan()

    five_candidates = len(report_statements())
    five_index = report_statements(mention=REGISTERED_INDEX_SQL)

    assert len(two_index) == 1, "two candidates, one registered index"
    assert len(five_index) == 1, "five candidates, still one registered index"
    # Measured, not guessed: six report-wide statements (folders, memberships,
    # the registered index, the two settings reads, and the one `select(Project)`
    # that derives `excluded_keys` before the walk) plus three per candidate (its
    # asset read, the marker lookup's project list and its own gates). Drift adds
    # the registered index and nothing per row.
    assert two_candidates == 12, "two candidates"
    assert five_candidates == 21, "five candidates: three more rows"



# --- a verified twin judged on the unclaimed subtree (Mf-wd3v) ---------------

# The live witness, measured on the real library: the vendor extraction
# ``import/01_Fates End - Wizard Tower`` beside ``01_Fates End - Wizard Tower.zip``.
# Every one of its files sits in ``01_Wizard``, so the pairing question - "does
# this archive's payload sit in this directory on disk" - is asked of ``folded``,
# the unclaimed full-subtree listing, and never of ``work``, the post-claim view the
# folder pass has already emptied (``_compound_pass`` documents the same trap).
TOWER_DIR = "import/01_Fates End - Wizard Tower"
TOWER_ZIP = "import/01_Fates End - Wizard Tower.zip"
TOWER_ROOT = "01_Wizard"
TOWER_MEMBER_DIR = f"{TOWER_DIR}/{TOWER_ROOT}"
TOWER_NESTED_ZIP = f"{TOWER_MEMBER_DIR}/01_Wizard_v5.4.zip"
TOWER_MODEL_NAMES = tuple(f"part-{index}.stl" for index in range(1, 11))
TOWER_PREVIEW_NAMES = ("Wizard_RENDER.png", "Wizard_VisualReference.png")
# The visual reference is the larger preview, so the cover pick has one answer.
TOWER_PREVIEW_SIZES = (60, 80)
TOWER_TEXT_NAME = "Wizard_Dice_v5.4_fixes.txt"
TOWER_TEXT_BYTES = b"Dice v5.4 fixes: the tray prints at 0.2 mm\n"
# The vendor's own deliverable, written as a REAL archive: models and a render under
# one root with no text is what makes it a candidate, and a candidate is what makes
# ``01_Wizard`` a member of the tower rather than a row of its own.
TOWER_NESTED_ENTRIES = {
    "01_Wizard_v5.4/Wizard_Dice_v5.4.stl": b"z" * 60,
    "01_Wizard_v5.4/Wizard_Dice_v5.4_render.png": b"j" * 90,
}

# AC-3's pile: two UNNAMED model-bearing subdirectories, nothing at the parent's own
# depth, beside the archive that shipped them. ``alpha`` and ``beta`` are lowercase
# single words so R5 stays out of it and the shape guard is the only thing under test.
PILE_NAME = "pile"
PILE_DIR = f"import/{PILE_NAME}"
PILE_ZIP = f"{PILE_DIR}.zip"
PILE_SUBDIRS = ("alpha", "beta")

# AC-4's other buried shape: the models sit at depth two, where no own-depth count
# can reach them whatever the pairing decides.
DEEP_DIR = "import/deep"
DEEP_ZIP = "import/deep.zip"
DEEP_PAYLOAD = {
    "sub/deeper/part-a.stl": b"s" * 40,
    "sub/deeper/part-b.stl": b"s" * 41,
    "sub/deeper/preview.png": b"p" * 50,
}

# Decision 18's two parents. ``P`` holds its own model plus a whole nested export,
# so only the stronger-candidate guard can stop it; ``Q`` holds nothing of its own
# and its one child is a claimed export, so the claim is what answers.
STRONG_PARENT_DIR = "import/P"
STRONG_PARENT_ZIP = "import/P.zip"
STRONG_CHILD_ZIP = "import/P/moai_export.zip"
CLAIMED_PARENT_DIR = "import/Q"
CLAIMED_PARENT_ZIP = "import/Q.zip"
CLAIMED_CHILD_DIR = "import/Q/Moai_statue"

# The parent of Decision 18 with the OTHER kind of child: ``import/W`` holds nothing of
# its own, its single child is an R3-shaped export (models, a preview, a document), and
# that export SHIPS a readable nested archive. Models plus a render under one root and
# no text is what makes the nested zip candidate-shaped on its own - which is exactly
# the threat: a surviving archive row below an emptied ancestor is what turns the
# ancestor's OWN row into the Decision 18 swallow. Since Mf-ny89 this ancestor is
# promoted rather than ``pair_only`` (its single child is LOW, so the promotion fires),
# and the threat is sharper, not softer - a promoted key joins ``bundle_dirs`` AND
# ``contained_keys``, so the swallow runs by both routes. The witness below pins that
# no archive row survives beneath it either way.
EMPTY_CLAIMED_DIR = "import/W"
EMPTY_CLAIMED_ZIP = f"{EMPTY_CLAIMED_DIR}.zip"
EMPTY_CLAIMED_CHILD_DIR = f"{EMPTY_CLAIMED_DIR}/Moai_statue"
EMPTY_CLAIMED_CHILD_MODELS = tuple(f"part{index}.stl" for index in range(3))
EMPTY_CLAIMED_CHILD_PREVIEW = "preview.png"
EMPTY_CLAIMED_CHILD_TEXT = "notes.txt"
EMPTY_CLAIMED_CHILD_TEXT_BYTES = b"Print at 0.2 mm\n"
EMPTY_CLAIMED_NESTED_ZIP = f"{EMPTY_CLAIMED_CHILD_DIR}/vendor_export.zip"
EMPTY_CLAIMED_NESTED_ENTRIES = {
    "vendor/part-a.stl": b"z" * 60,
    "vendor/render.png": b"j" * 90,
}

# --- the promotion guard replaces the claimed-ancestor branch (Mf-8acz) ------

# The deletion witness. Mf-8acz removed ``_single_claimed_child_subtree`` and put the
# inverted question - ``_promotion_would_double_own``, True meaning REFUSE - at the
# single decision site in ``_build_candidates``. The structural tests below are the
# witness that the old helper is gone by every spelling, that the new guard is asked
# exactly once, and that Decision 18's ``_holds_stronger_candidate`` guard survived the
# deletion: an over-deletion that took the second guard with the first reds four
# behavioural witnesses elsewhere in this file, and this one names it in one line.


# Mf-ny89's three parents, each the promoted live shape with exactly one thing
# changed, so the ONLY thing that can decide them is ``_promotion_would_double_own``
# and the ``_holds_stronger_candidate`` guard that follows it. Every child below them is a
# ``low_export_directory`` - models, a preview and a ``notes.txt``, no vendor markers -
# because a HIGH child would be refused promotion by the stronger-candidate leg and
# prove nothing about the shape question. ``import/M``'s one child is the exception on
# purpose: the id-named dragon is MEDIUM, and MEDIUM outranks a shape inference.
#
# * ``import/M`` - one MEDIUM child: the guard direction, parent stays pair-only.
# * ``import/L`` - one claimed child PLUS a model of its own at its own depth: the
#   LOOSE FILE negative, a wrapper holds nothing of its own.
# * ``import/D`` - TWO claimed children: the PILE negative. Its children are
#   ``files/`` and ``models/`` because ``R3_MODEL_DIRS`` counts the models under
#   ``files/`` as own-depth, so ``_own_depth_model_count`` lets the pairing through
#   and only the two-first-segments answer can refuse it - unlike ``pile_fixture``,
#   whose UNCLAIMED ``alpha/`` and ``beta/`` are refused by that inherited bound
#   before this helper is ever consulted.
MEDIUM_PARENT_DIR = "import/M"
MEDIUM_PARENT_ZIP = f"{MEDIUM_PARENT_DIR}.zip"
MEDIUM_CHILD_DIR = f"{MEDIUM_PARENT_DIR}/{DRAGON}"
MEDIUM_CHILD_MODEL = "ARTICULATED_ice_dragon_fixed_p1.stl"
# The shape that makes the ``work`` leg load-bearing rather than decorative: the
# parent's ONE child is not itself a row - ``A`` holds two models of its own plus a
# claimed grandchild - so the parent's post-claim view still lists ``A/own0.stl`` while
# ``folded`` still answers "one first segment". Emptiness is the only question the
# emptied view can answer honestly, and here it is the only thing that keeps the parent
# a container instead of the row that swallows a claimed grandchild.
PARTIAL_PARENT_DIR = "import/Z"
PARTIAL_PARENT_ZIP = f"{PARTIAL_PARENT_DIR}.zip"
PARTIAL_CHILD_DIR = f"{PARTIAL_PARENT_DIR}/A"
PARTIAL_CHILD_MODELS = ("own0.stl", "own1.stl")
PARTIAL_GRANDCHILD_DIR = f"{PARTIAL_CHILD_DIR}/B"
LOOSE_PARENT_DIR = "import/L"
LOOSE_PARENT_ZIP = f"{LOOSE_PARENT_DIR}.zip"
LOOSE_CHILD_DIR = f"{LOOSE_PARENT_DIR}/parts"
LOOSE_OWN_MODEL = f"{LOOSE_PARENT_DIR}/leftover.stl"
DOUBLE_PARENT_DIR = "import/D"
DOUBLE_PARENT_ZIP = f"{DOUBLE_PARENT_DIR}.zip"
DOUBLE_CHILD_DIRS = ("files", "models")

# B-1, the promotion the nested-member pass later VETOES. Three levels, two verified
# twins, and the payload one level deeper than either twin's directory:
# ``import/X/x_info.stl`` (a model of its own, so ``X`` is NOT a wrapper - it answers
# pair-only), ``import/X/P/A/{part0.stl,part1.stl,part2.stl,preview.png,notes.txt}``
# (the R3 trio that earns ``A`` a ``low`` row, and the claim that empties ``P``),
# ``import/X/P.zip`` the verified twin of ``P``, ``import/X.zip`` the verified twin of
# ``X``. ``P`` is therefore promoted on its single claimed child - and then the prune
# takes ``P.zip`` away as a member of the pair-only ``X``, so the promotion itself is
# dropped. Whether anything is left to create from is decided purely by WHEN the
# promotion's dissolution was committed; see ``b1_vetoed_promotion_tree``.
B1_PARENT_DIR = "import/X"
B1_PARENT_ZIP = f"{B1_PARENT_DIR}.zip"
B1_PARENT_OWN_MODEL = f"{B1_PARENT_DIR}/x_info.stl"
B1_MID_DIR = f"{B1_PARENT_DIR}/P"
B1_MID_ZIP = f"{B1_MID_DIR}.zip"
B1_CHILD_DIR = f"{B1_MID_DIR}/A"

EMPTY_TWIN_DIR = "import/emptypair"
EMPTY_TWIN_ZIP = "import/emptypair.zip"
EMPTY_TWIN_ENTRIES = {
    "emptypair/part-a.stl": b"s" * 40,
    "emptypair/preview.png": b"p" * 50,
}

# The live tree as it ACTUALLY sits on the operator's library, listed off
# ``/mnt/ceph/3D_models/import/01_Fates End - Wizard Tower``: the extraction
# directory ``01_Wizard`` holds exactly twenty entries - fifteen STLs, three PNGs,
# the vendor's ``.txt`` and the vendor's own deliverable zip. The ticket's shape
# (ten STLs, two previews, no text file) is an approximation of this one, so the
# counts below are transcribed from disk rather than from the ticket.
LIVE_MODEL_NAMES = (
    "Wizard_Tray_Lg_Tx_v1.4.stl",
    "Wizard_Tray_Lg_Sm_v1.3.stl",
    "Wizard_Tray_OL_v1.3.stl",
    "Wizard_Dec2_Mid_v5.5.stl",
    "Wizard_Dice_roof_v5.4.stl",
    "Wizard_Tray_Sm_Tx_v1.4.stl",
    "Wizard_Tray_DiceHold_v1.4.stl",
    "Wizard_Dec_v5.4.stl",
    "Wizard_Dice_v5.4.stl",
    "AddOn_Wizard_Tray_Lg_Tx_Circle.stl",
    "AddOn_Wizard_Tray_Lg_Smo_Circle.4.stl",
    "Wizard_Dec2_Btm_v5.5.stl",
    "Wizard_DT2_Mid_v5.5.stl",
    "Wizard_DT2_Btm_v5.5.stl",
    "Wizard_Dec_Roof_v5.4.stl",
)
# Three previews, not the two the ticket counted; the sizes are distinct so "the
# largest preview" has one answer whatever the walk's order.
LIVE_PREVIEW_NAMES = ("Wizard_RENDER.png", "Wizard_VisualReference.png", "_FileName_Key.png")
LIVE_PREVIEW_SIZES = (60, 70, 80)
# 498 bytes on the real disk; the payload below is shorter and that is fine - it is
# the file's PRESENCE that decides the shape, because it is what R3 reads.
LIVE_TEXT_NAME = "Wizard_Dice_v5.4_fixes.txt"
LIVE_TEXT_BYTES = b"Dice v5.4 fixes: the tray prints at 0.2 mm\n"
# The vendor's own deliverable, in its own shipping order and BARE-NAMED (no wrapper
# directory). It carries models AND a render, so its own shape is an archive
# candidate: that is what makes ``01_Wizard`` a member of the tower instead of a row
# of its own, which is the live case Mf-wd3v has to reproduce.
LIVE_NESTED_NAMES = (
    "AddOn_Wizard_Tray_Lg_Smo_Circle.4.stl",
    "AddOn_Wizard_Tray_Lg_Tx_Circle.stl",
    "Wizard_DT2_Btm_v5.5.stl",
    "Wizard_DT2_Mid_v5.5.stl",
    "Wizard_Dec2_Btm_v5.5.stl",
    "Wizard_Dec2_Mid_v5.5.stl",
    "Wizard_Dec_Roof_v5.4.stl",
    "Wizard_Dec_v5.4.stl",
    "Wizard_Dice_roof_v5.4.stl",
    "Wizard_Dice_v5.4.stl",
    "Wizard_Dice_v5.4_fixes.txt",
    "Wizard_Tray_DiceHold_v1.3.stl",
    "Wizard_Tray_Lg_Sm_v1.3.stl",
    "Wizard_Tray_Lg_Tx_v1.3.stl",
    "Wizard_Tray_OL_v1.3.stl",
    "Wizard_Tray_Sm_Smo_v1.3.stl",
    "Wizard_Tray_Sm_Tx_v1.4.stl",
    "Wizard_VisualReference.png",
)


def twin_archive_of(storage: Path, rel_dir: str, archive: str) -> None:
    """Write ``archive`` with one entry per file of ``rel_dir``, same bytes.

    Every entry declares the size the file ACTUALLY has on disk: containment
    compares declared sizes, so an archive that lies about its payload is refused
    for the wrong reason and the test measures the wrong thing.
    """
    base = storage / rel_dir
    entries = {
        str(path.relative_to(base)): path.read_bytes() for path in sorted(base.rglob("*")) if path.is_file()
    }
    write_zip(storage / archive, entries)


def tower_extraction(storage: Path, *, with_text: bool, with_nested_zip: bool = True) -> None:
    """The extraction on disk plus the archive it came in, with or without the ``.txt``.

    ``with_text`` is the ONLY difference between the two shapes the ticket measured.
    With it R3 offers ``01_Wizard`` a row of its own and the claim empties the
    parent's own view; without it nothing below the parent is claimed at all.

    ``with_nested_zip=False`` leaves the vendor's deliverable out, so the child holds
    no candidate-shaped archive and nothing drops it out of the offered set: with a
    pair-only parent that shape showed TWO rows, the child keeping a ``ready`` row of
    its own. It is written before ``twin_archive_of``, which reads the bytes off disk -
    an archive that declared a file the child does not hold would be refused
    ``twin-unverified`` and the test would measure nothing.
    """
    payload = {f"{TOWER_ROOT}/{name}": b"s" * (40 + index) for index, name in enumerate(TOWER_MODEL_NAMES)}
    for name, size in zip(TOWER_PREVIEW_NAMES, TOWER_PREVIEW_SIZES, strict=True):
        payload[f"{TOWER_ROOT}/{name}"] = b"p" * size
    if with_text:
        payload[f"{TOWER_ROOT}/{TOWER_TEXT_NAME}"] = TOWER_TEXT_BYTES
    for name, content in payload.items():
        write_file(storage / TOWER_DIR / name, content)
    if with_nested_zip:
        write_zip(storage / TOWER_NESTED_ZIP, TOWER_NESTED_ENTRIES)
    twin_archive_of(storage, TOWER_DIR, TOWER_ZIP)


def live_tower_extraction(storage: Path) -> None:
    """The MEASURED live tree, written file for file as the library holds it.

    Fifteen models, three previews, the vendor ``.txt``, the vendor's own readable
    deliverable zip, and ``01_Fates End - Wizard Tower.zip`` declaring the bytes each
    of those twenty files really has on disk (``twin_archive_of`` writes that part).
    """
    payload: Dict[str, bytes] = {
        f"{TOWER_ROOT}/{name}": b"s" * (40 + index) for index, name in enumerate(LIVE_MODEL_NAMES)
    }
    for name, size in zip(LIVE_PREVIEW_NAMES, LIVE_PREVIEW_SIZES, strict=True):
        payload[f"{TOWER_ROOT}/{name}"] = b"p" * size
    payload[f"{TOWER_ROOT}/{LIVE_TEXT_NAME}"] = LIVE_TEXT_BYTES
    for rel_path, content in payload.items():
        write_file(storage / TOWER_DIR / rel_path, content)
    nested: Dict[str, bytes] = {}
    for index, name in enumerate(LIVE_NESTED_NAMES):
        if name.endswith(".png"):
            nested[name] = b"j" * (90 + index)
        elif name.endswith(".txt"):
            nested[name] = LIVE_TEXT_BYTES
        else:
            nested[name] = b"z" * (200 + index)
    write_zip(storage / TOWER_NESTED_ZIP, nested)
    twin_archive_of(storage, TOWER_DIR, TOWER_ZIP)


def pile_fixture(storage: Path, *, with_archive: bool) -> None:
    """Two unnamed model-bearing subdirectories, and optionally the archive of them."""
    entries: Dict[str, bytes] = {}
    for index, name in enumerate(PILE_SUBDIRS):
        for suffix, filler in (("part.stl", b"s"), ("preview.png", b"p")):
            content = filler * (40 + index)
            write_file(storage / PILE_DIR / name / suffix, content)
            entries[f"{PILE_NAME}/{name}/{suffix}"] = content
    if with_archive:
        write_zip(storage / PILE_ZIP, entries)


def thingiverse_export_entries() -> Dict[str, bytes]:
    """A whole vendor export as archive entries: both documents, five previews, three models."""
    entries: Dict[str, bytes] = {"README.txt": README_TEXT.encode(), "LICENSE.txt": LICENSE_TEXT.encode()}
    for index, size in enumerate(IMAGE_SIZES):
        entries[f"images/{index}.jpg"] = b"j" * size
    for index in range(3):
        entries[f"files/part{index}.stl"] = b"s" * (40 + index)
    return entries


def low_export_directory(storage: Path, rel_dir: str) -> None:
    """A child the folder pass CLAIMS as a LOW row: three models, a preview, a document.

    ``notes.txt`` rather than ``README.txt``/``LICENSE.txt`` is the whole point: the
    trio earns R3's ``low`` row and nothing more, so a parent whose only content is
    this child is bounded by the shape question alone. A vendor document pair would
    make the child ``high`` and ``_holds_stronger_candidate`` would answer before the
    shape was ever asked - which is ``import/Q``'s witness, not these.
    """
    base = storage / rel_dir
    for index in range(3):
        write_file(base / f"part{index}.stl", b"s" * (40 + index))
    write_file(base / "preview.png", b"p" * 70)
    write_file(base / "notes.txt", EMPTY_CLAIMED_CHILD_TEXT_BYTES)


def b1_vetoed_promotion_tree(storage: Path) -> None:
    """The B-1 tree: a promotion the nested-member pass vetoes after it recorded a dissolution.

    Built bottom-up so each twin declares the bytes its directory really holds
    (``twin_archive_of`` reads them off disk): the R3-shaped ``A`` first, then ``P``'s
    twin, then ``X``'s - which is why the parent's own model is written before the
    archives and never inside either of them. ``X`` is a pair-only container because a
    model of its own stands beside the bundle it ships; ``P`` is a real wrapper and is
    promoted on ``A``; and ``P.zip`` sitting strictly inside the pair-only ``X`` is then
    a member, so the prune drops ``P``'s pairing and the promoted row with it.
    """
    low_export_directory(storage, B1_CHILD_DIR)
    write_file(storage / B1_PARENT_OWN_MODEL, STL_BYTES)
    twin_archive_of(storage, B1_MID_DIR, B1_MID_ZIP)
    twin_archive_of(storage, B1_PARENT_DIR, B1_PARENT_ZIP)


def files_under(storage: Path, rel_dir: str) -> List[Path]:
    """The files a directory holds, as the library scan counted them - never a literal.

    A row's ``assets_total`` is the number of registered files under its key, so the
    assertion is read off the fixture it was built from: change the fixture and the
    number moves with it. Counting the ``.zip`` beside the directory is the mistake
    this helper exists to prevent - a loose ``LIKE 'dir%'`` on the live library
    counted ``01_Fates End - Wizard Tower.zip`` as one of the tower's own files.
    """
    return [path for path in (storage / rel_dir).rglob("*") if path.is_file()]


def nested_vendor_zip_is_a_member(storage: Path) -> None:
    """Mf-74x5, asserted the same way for both tower shapes.

    The vendor's nested deliverable is a file of the bundle, never a candidate: it
    is absent from the offered set and the ledger names it ``nested-member``.
    """
    candidates = bundle_service.scan_bundles(storage).candidates

    assert [candidate.key for candidate in candidates] == [TOWER_DIR], (
        "one row for the extraction and no row for the archive inside it"
    )
    assert refusal_rules(storage)[TOWER_NESTED_ZIP] == bundle_service.REFUSAL_NESTED_MEMBER


def test_service_a_promoted_towers_nested_vendor_archive_is_a_member_not_a_row(
    isolated_storage: Path,
) -> None:
    """AC-2 (the promoted shape): Mf-74x5 holds for the row the pairing created.

    ``01_Wizard_v5.4.zip`` is the vendor's own deliverable, sitting inside a bundle
    directory the scan offered: it is a supporting file, never a candidate and never
    a pairing. Before Mf-wd3v it WAS the row - the archive of the whole tower.

    Mutation: exempt archive-only rows from ``is_bundle_member`` and the nested zip
    surfaces again as a second, uncreatable ``archive_only`` row.
    """
    tower_extraction(isolated_storage, with_text=False)

    nested_vendor_zip_is_a_member(isolated_storage)


def test_service_a_claimed_child_towers_nested_vendor_archive_is_a_member_not_a_row(
    isolated_storage: Path,
) -> None:
    """AC-2 (the claimed-child shape): membership does not depend on how the row was earned.

    The same tree with the vendor's ``.txt`` added. Since Mf-ny89 both tower shapes
    are ``archive_owned``, so this is no longer the promoted/pair-only pair its
    original name promised; what still separates it from the doc-less sibling above
    is HOW the parent got its listing. Here ``01_Wizard`` is claimed, so
    ``work[TOWER_DIR]`` is empty and the row is built off ``folded``; there the two
    listings agree. The nested archive is a member either way - a zip inside a
    bundle directory proves nothing about a sibling directory whatever the parent's
    own answer is - but only THIS shape asks that question of a listing the claim
    emptied.

    Mutation: revert the ``folded`` fallback (``entries = work.get(folder) or []``)
    and this row loses the member answer while the doc-less sibling keeps it - the
    emptied own view means the pairing never verifies, no ``archive_owned`` key
    reaches ``bundle_dirs``, and the vendor's zip survives as a row. (The old claim
    here - narrow ``bundle_dirs`` to the ``archive_owned`` rows and only this shape
    breaks - was measured false by Mf-ny89: both shapes are ``archive_owned`` now,
    that mutation is green on both, and the witness that really catches it is
    ``test_service_a_paired_extraction_inside_a_bundle_is_not_a_second_row``.)
    """
    tower_extraction(isolated_storage, with_text=True)

    nested_vendor_zip_is_a_member(isolated_storage)


def test_service_a_verified_twin_no_longer_answers_the_no_facts_reason(isolated_storage: Path) -> None:
    """The ledger move Mf-wd3v exists for: ``twin-unverified`` was a lie.

    The live shape's archive IS verified - its payload sits in ``01_Wizard`` on disk -
    so the ledger says ``twin-consumed``, the reason that names the directory it was
    consumed by. While the pairing was judged against the emptied post-claim view it
    said the no-facts reason instead, which is what made the absence impossible to
    read: the facts were on disk the whole time.

    Mutation: read site 1 off ``work`` (``work.get(twin, ())``) and this tree answers
    ``twin-unverified`` again, which is the word this test forbids anywhere in it.
    """
    tower_extraction(isolated_storage, with_text=True)

    rules = refusal_rules(isolated_storage)

    assert rules[TOWER_ZIP] == bundle_service.REFUSAL_TWIN_CONSUMED
    assert bundle_service.REFUSAL_TWIN_UNVERIFIED not in rules.values(), (
        "nothing in a tree whose archive verified is refused for want of facts"
    )


# --- what the promotion is FOR: the operator creates the project (Mf-ny89) ----
#
# Everything above is about which rows the report offers. The ticket's complaint is
# about what happens NEXT - "it won't let me create it as a project" - and a row the
# report refused while never reading its own directory is a row the UI has nothing to
# send for. (Its ``assets_total`` 0 belongs to that skipped read, not to the disk: the
# same pair-only shape measures 7 files under ``import/X`` and 20 under the tower now.)
# So the promotion earns nothing until the real create pass makes a
# project out of it, and the project has to be the CHILD's payload under the PARENT's
# key: a ``ready`` row that creates 0 assets, or creates a project holding somebody
# else's parts, leaves the operator exactly where the ticket found them.


def test_a_promoted_tower_create_attaches_its_preview_images(
    client: TestClient, isolated_storage: Path
) -> None:
    """Mf-7oow, on the live tower: EVERY registered file is a member of its project.

    The Mf-wsdo complaint was narrower - the owner had to hand-add the vendor's three
    previews because the member set was models plus images and nothing else. Mf-7oow
    removes the table: a bundle project holds every registered asset under its key, so
    the tower's twenty files are all members and each one carries the role the
    detector's own ``classify_entry`` gives it.

    * ``assets_added`` and the ``project_asset`` count are 20: fifteen models, three
      previews, the vendor's ``.txt`` and its nested deliverable zip, all under
      ``01_Wizard/``;
    * the ``.txt`` and the nested ``.zip`` are MEMBERS now - named here so the roles
      are pinned by name, not by a count. ``classify_entry`` reads them as ``text`` and
      ``archive``, which is exactly what the row's ``text_count``/``archive_count`` say;
    * the cover is a member. Before Mf-wsdo the project's ``cover_asset_id`` was the
      largest PNG (``_FileName_Key.png``, 80 bytes against 70 and 60) and that id was
      NOT in the member set - SET AND DANGLING, which is what
      ``.plans/measurements/2026-11-10-bundle-members-mf-wsdo-before.json`` records as
      ``cover_is_member: false``;
    * the picker's own predicate finds them: filtering the member paths by
      ``library_scan.IMAGE_SUFFIXES`` returns the three PNGs, which is the
      server-side shape of ``members.filter(isImageAsset)`` being non-empty.

    The fact counts and the member count are asserted in ONE block below, because the
    trap is reading them as one number: ``model_count``/``image_count``/``assets_total``
    are what the DETECTOR read (15/3/20), while ``assets_added`` is what the CREATE
    wrote - now the same 20, and a ``model_count`` moved to 20 would be a project and a
    row disagreeing about what a model is.

    Mutation (run, then restored): make ``_members_of`` return only the MODEL entries
    (i.e. re-add any suffix filter) and ``(assets_added, member_count)`` answers
    ``(15, 15)`` while the ``.txt``/nested-zip membership assertions red; change the
    row's ``model_count`` to read the member count and the ``(20, 15, 3, 20)`` block
    reds.
    """
    live_tower_extraction(isolated_storage)
    scan()
    headers = auth_header(client)

    row = row_for(detect(client, headers), TOWER_DIR)
    created = create(client, headers, [{"key": TOWER_DIR}])
    result = created["results"][0]
    project_id = result["project_id"]

    # The measurement goes down BEFORE the assertions, so a red run still produces the
    # artifact - that is how the BEFORE pair was captured against unmodified production
    # code, with these assertions failing.
    _member_artifact(
        door="detection",
        key=TOWER_DIR,
        storage=isolated_storage,
        project_id=project_id,
        row=row,
        result=result,
    )

    assert (result["status"], result["reason"]) == ("created", None)
    member_paths = project_storage_paths(project_id)
    model_paths = {f"{TOWER_MEMBER_DIR}/{name}" for name in LIVE_MODEL_NAMES}
    preview_paths = {f"{TOWER_MEMBER_DIR}/{name}" for name in LIVE_PREVIEW_NAMES}

    assert (
        result["assets_added"],
        member_count(project_id),
    ) == (20, 20), "every registered file: fifteen models, three previews, a .txt and a nested zip"
    assert (
        result["assets_added"],
        row["model_count"],
        row["image_count"],
        row["assets_total"],
    ) == (20, 15, 3, 20), (
        "two different questions in one block: what the CREATE wrote (20: every "
        "registered file) against what the DETECTOR counted (15 models, 3 images, 20 "
        "files) - the role counts stay role counts, and 20/15/3/20 is the ticket"
    )
    assert member_paths == model_paths | preview_paths | {
        f"{TOWER_MEMBER_DIR}/{LIVE_TEXT_NAME}",
        f"{TOWER_MEMBER_DIR}/01_Wizard_v5.4.zip",
    }, (
        "the members are the fifteen STLs, the three PNGs, the vendor's .txt AND its "
        "nested zip - every registered file under the key, whatever its role"
    )
    assert all(path.startswith(f"{TOWER_MEMBER_DIR}/") for path in member_paths), (
        "every member still comes out of ``01_Wizard/``, the child the promotion read"
    )

    text_path = f"{TOWER_MEMBER_DIR}/{LIVE_TEXT_NAME}"
    nested_zip = f"{TOWER_MEMBER_DIR}/01_Wizard_v5.4.zip"
    with Session(engine) as session:
        registered = {asset.path for asset in bundle_service.registered_assets(session, TOWER_DIR)}

    assert registered == model_paths | preview_paths | {text_path, nested_zip}, (
        "the twenty files the scan registered, named, so the two exclusions below are "
        "named against a set that cannot quietly have shrunk"
    )
    assert text_path in registered and text_path in member_paths, (
        "the vendor's ``Wizard_Dice_v5.4_fixes.txt`` IS a member, with role ``text`` - "
        "the one classifier says so and the row's ``text_count`` agrees"
    )
    assert nested_zip in registered and nested_zip in member_paths, (
        "the vendor's own deliverable ``01_Wizard/01_Wizard_v5.4.zip`` is a member too, "
        "with role ``archive`` - a nested archive is a member of the bundle AND of the "
        "project it makes (Mf-7oow; Mf-wsdo had excluded it)"
    )

    assert (row["cover"]["reason"], row["cover"]["rel_path"]) == (
        "largest-image",
        f"{TOWER_MEMBER_DIR}/_FileName_Key.png",
    ), "the largest of the three previews (80 bytes against 70 and 60)"
    with Session(engine) as session:
        project = session.get(Project, project_id)
    assert project is not None
    member_ids = set(project_asset_ids(client, headers, project_id))
    assert project.cover_asset_id == row["cover"]["asset_id"], (
        "the card's cover and the project's cover are one expression now - and on THIS "
        "create, which excludes no group, that is also a membership: every asset `_cover` "
        "can answer is bounded to its image leg (`IMAGE_SUFFIXES`) or its model leg "
        "(`PREVIEW_SUFFIXES`, inside `MODEL_SUFFIXES`), and the member set is every "
        "registered file. Do not read it as a general 'the row cannot advertise what its "
        "project will not hold': for a create that EXCLUDES a group the row's cover is "
        "chosen exclusion-blind over the whole owned set and is routinely not a member - "
        "the re-ask in `_create_one` is what replaces it, and "
        "`test_the_cover_never_picks_a_registered_image_under_an_excluded_group` is that "
        "case"
    )
    assert project.cover_asset_id in member_ids, (
        "THE flip: this assertion was False before the ticket, because the cover was "
        "set from a PNG the project was never given - set and dangling"
    )

    picked = {path for path in member_paths if bundle_service._suffix(path) in library_scan.IMAGE_SUFFIXES}

    assert picked == preview_paths, (
        "the landing page filters its members by image kind; server-side that filter is "
        "this line, and an empty set here is the empty cover picker the owner hit"
    )


def test_a_created_tower_row_answers_its_own_project_id_and_a_second_create_is_a_skip(
    client: TestClient, isolated_storage: Path
) -> None:
    """The answer a created tower row gives AFTER the create.

    The row reports the project it became - by MARKER, with the name read back from
    the project endpoint for that same id - the report counts it once under
    ``already_created``, and a second ``POST /bundles/create`` for the key is a
    ``skipped``/``already_created`` carrying the SAME id rather than a second
    project over the same files.
    """
    live_tower_extraction(isolated_storage)
    scan()
    headers = auth_header(client)

    first = create(client, headers, [{"key": TOWER_DIR}])

    assert first["created"] == 1, "nothing below is worth measuring unless the row creates once"
    project_id = first["results"][0]["project_id"]
    assert project_id is not None

    report = detect(client, headers)
    row = row_for(report, TOWER_DIR)
    projects = project_listing(client, headers)

    assert row["existing_project_id"] == project_id, (
        "a created row answers the project it became, from the marker it wrote"
    )
    assert row["existing_project_name"] == projects[project_id]["name"]
    assert row["existing_project_match"] == "marker", "the create's own marker is the identity"
    assert report["counts"]["already_created"] == 1

    second = create(client, headers, [{"key": TOWER_DIR}])

    assert (second["created"], second["skipped"], second["blocked"], second["failed"]) == (
        0,
        1,
        0,
        0,
    )
    assert (second["results"][0]["status"], second["results"][0]["reason"]) == (
        "skipped",
        "already_created",
    )
    assert second["results"][0]["project_id"] == project_id, "the same project, not a new one"
    assert len(project_listing(client, headers)) == 1, "no duplicate project over the tower's files"
    assert table_counts()["projects"] == 1
    assert member_count(project_id) == 20, (
        "a skip attaches nothing a second time - twenty is what the first create "
        "attached (every registered file under the key, Mf-7oow), not a second helping"
    )


# --- every row asks the database (Mf-quv4) ------------------------------------
#
# An archive-only row used to be answered WITHOUT reading the database: the
# candidate's SHAPE said "nothing to create", so ``registered_assets``, the
# containment set, ``assets_total`` and the registered legs of the cover were all
# skipped together. But status is not a shape question - ADR 0010 Decision 7 makes
# it a database question (a resolved ``Folder`` row and registered models) - so the
# short-circuit answered a database question from disk shape and left a refused row
# describing a directory it had never opened. It is gone: every row asks, and ONE
# predicate (own registered models the report's claim map leaves it, not an archive,
# not a container of an offered directory, a resolved folder) decides every status.
# The witnesses below are the ones the deleted line cannot satisfy: each asserts
# something about the READ, not only about the verdict.


def endorphin_pile(storage: Path) -> None:
    """The AC-2b pile: ``endorphin``, its own loose model and render, two real child
    bundles, and ``endorphin.zip`` holding exactly those two loose files.

    This is the tree of
    ``test_service_a_verified_stem_twin_does_not_promote_a_pile_of_bundles``,
    restated rather than shared so that witness stays untouched. The twin verifies,
    so only the container rule can refuse the promotion, and the pile stays the
    blocked ``archive_only`` row - which is exactly what makes it the right row to
    ask "and what do you say about the files under your own key?".
    """
    root = storage / "import" / "endorphin"
    write_file(root / "loose.stl", STL_BYTES)
    write_file(root / "render.png", b"p" * 70)
    for name, model in (("Alpha", "a.stl"), ("Beta", "b.stl")):
        write_file(root / name / model, b"s" * 41)
        write_file(root / name / f"{name.lower()}.png", b"p" * 50)
        write_text(root / name / "notes.txt", "print at 0.2mm")
    write_zip(
        storage / "import" / "endorphin.zip",
        {"endorphin/loose.stl": STL_BYTES, "endorphin/render.png": b"p" * 70},
    )


def test_an_endorphin_pile_reports_the_assets_its_key_holds(
    client: TestClient, isolated_storage: Path
) -> None:
    """A row counts and covers the files its key holds.

    Eight files sit under ``import/endorphin`` (the two children's six plus the
    pile's own loose model and render), the library scan registered all eight, and
    the row reports them and covers itself with the largest registered image. A
    DIRECTORY row is never ``archive_only``: that word names a `.zip` row's one door.
    """
    endorphin_pile(isolated_storage)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    files = files_under(isolated_storage, "import/endorphin")
    assert len(files) == 8, "the two children's six files plus the pile's own model and render"
    pile = row_for(report, "import/endorphin")
    assert (pile["status"], pile["blocked_reason"]) == ("ready", None), (
        "a DIRECTORY row is never archive_only: that word is the `.zip` row's door"
    )
    assert pile["assets_total"] == len(files) > 0, (
        "the row counts every registered file under its key - the answer a row that "
        "never asked the database could not give"
    )
    assert pile["cover"]["asset_id"] is not None, (
        "the cover is backed by a registered asset, not by the unregistered leg"
    )
    assert pile["cover"]["reason"] == "largest-image", (
        "and it won the registered-image leg like any other row's cover would"
    )


def test_a_zip_key_cannot_answer_the_creatable_predicate_from_disk(
    client: TestClient, isolated_storage: Path
) -> None:
    """The double-create that cannot happen, pinned as a fact about rows and indexes.

    Creation happens from registered assets under the key and from a ``Folder`` row
    whose ``storage_path`` IS the key. Neither can exist for a ``.zip`` key:
    ``registered_assets`` matches the prefix ``<key>/`` and no file lives inside a
    file, and ``_folder_index`` indexes only rows whose ``storage_path`` is NOT NULL
    - which is also why the extraction door cannot leave one behind:
    ``zip_service.get_or_create_folder`` creates its ``Folder`` rows with
    ``storage_path`` left NULL, so an extracted tree registers directories a zip key
    can never match either.

    The corpus is the twin pair from the ticket (``import/twin/`` beside
    ``import/twin.zip``) PLUS ``import/lone_export.zip``, because the twin is
    consumed as provenance and never becomes a row: only the lone archive answers
    the second half of this pin, and it answers ``archive_only`` - never
    ``unresolved_directory``, which would close the Extract door
    (``ui/review/bundleRows.ts`` ``extractPathFor`` opens on ``archive_only`` alone).
    That is the arm-order pin: the archive arm is tested before the folder arm.

    Mutation (run): move the ``is_archive or pair_only`` arm BELOW the folder arm and
    ``lone_export["blocked_reason"]`` becomes ``unresolved_directory`` - measured -
    and this reds while every status assertion above stays green.
    """
    thingiverse_bundle(isolated_storage / "import", "twin", 616161)
    write_zip(isolated_storage / "import" / "twin.zip", {"README.txt": b"x", "files/part.stl": STL_BYTES})
    write_zip(isolated_storage / "import" / "lone_export.zip", thingiverse_export_entries())
    scan()
    headers = auth_header(client)

    with Session(engine) as session:
        folders = bundle_service._folder_index(session)
        for zip_key in ("import/twin.zip", "import/lone_export.zip"):
            assert bundle_service.registered_assets(session, zip_key) == [], (
                f"{zip_key}: no registered asset can live under a zip prefix"
            )
            assert zip_key.casefold() not in folders, (
                f"{zip_key}: no Folder row can resolve a zip key, least of all one an "
                "extraction made (``storage_path`` stays NULL there)"
            )

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == ["import/lone_export.zip", "import/twin"], (
        "the twin archive is provenance on the directory row, never a row of its own"
    )
    lone = row_for(report, "import/lone_export.zip")
    assert (lone["status"], lone["blocked_reason"]) == ("blocked", "archive_only"), (
        "the archive arm BEFORE the folder arm, or the Extract door closes on it"
    )
    assert lone["assets_total"] == 0, "the honest zero of a lookup that ran and found nothing"
    assert row_for(report, "import/twin")["status"] == "ready", "the directory it twins is unaffected"


def test_an_excluded_group_row_and_its_compound_parent_are_mutually_inert(
    client: TestClient, isolated_storage: Path
) -> None:
    """A row is not cancelled by the subtree of the group it excluded.

    The former witness re-offered the excluded group as a row and proved the two rows
    were inert to one another. Mf-z643 prunes everything inside the created project
    key, so the group has no row at all - but the half that MATTERED is still
    asserted: the row is not cancelled by the (pruned) group it excluded,
    and no create can resurrect the group as a second project.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    insert_folder_row(XBOX_GROUP, mount_key())
    scan()
    headers = auth_header(client)
    assert create_for_exclusion(client, headers, [XBOX_GROUP])["created"] == 1, (
        "the compound was created with the group excluded"
    )

    report = detect(client, headers, limit=1000)

    compound = row_for(report, f"import/{XBOX}")
    assert (compound["status"], compound["blocked_reason"]) == ("ready", None), (
        "the parent is not cancelled by the group it excluded"
    )
    assert mount_key() not in {row["key"] for row in report["bundles"]}

    created = create(client, headers, [{"key": mount_key(), "name": "the excluded mount"}])

    assert (created["created"], created["blocked"], created["failed"]) == (0, 0, 1), (
        "the group is inside the project key, so it is not a row and not creatable"
    )
    assert created["results"][0]["reason"] == "unknown bundle"


def test_no_blocked_reason_outside_the_published_vocabulary(
    client: TestClient, isolated_storage: Path
) -> None:
    """Mf-quv4 added a predicate and no vocabulary: four answers, all of them old.

    A second door onto the same disk facts is where a new word sneaks in, so the
    whole vocabulary is pinned over a corpus that reaches every arm at once: the
    lone archive (``archive_only``, the arm the Extract door needs), a high bundle
    whose folder row never got a ``storage_path`` (``unresolved_directory``), a
    registered folder with nothing registered under it (``not_scanned``) and the
    ready rows beside them (``None``).

    Mutation: any new ``blocked_reason`` literal in ``_report_row`` - a
    ``contains_bundles`` or a ``claimed_elsewhere`` on ``blocked``, say - reds the
    set equality here AND the per-row assertion, and ``test_bundle_serialize.py``'s
    "no refusal names an offered path" reds if the predicate ever answers a row out
    of existence instead of answering it ``blocked``.

    The arms, measured over this corpus: the archive answers ``archive_only``,
    ``Unresolved Thing`` (a folder row with no ``storage_path``, the flattened
    migration's shape) answers ``unresolved_directory``, ``Not Scanned Thing`` (a
    registered folder with nothing registered under it) answers ``not_scanned``,
    and every other row in the report answers ``ready`` with ``None`` - including
    the endorphin directory row, which is never ``archive_only``.
    """
    assert {
        bundle_service.ARCHIVE_ONLY,
        bundle_service.UNRESOLVED_DIRECTORY,
        bundle_service.NOT_SCANNED,
    } == {"archive_only", "unresolved_directory", "not_scanned"}, (
        "the three words this module has always answered with - nothing added"
    )

    endorphin_pile(isolated_storage)
    write_zip(isolated_storage / "import" / "lone_export.zip", thingiverse_export_entries())
    scan()
    # The two database-shaped arms need rows the scan never made: the flattened
    # migration's folder row with no ``storage_path``, and a folder row whose assets
    # were never registered. Both are written AFTER the scan, which is what makes
    # them the honest shapes of those two refusals rather than a fixture trick.
    legacy_bundle(isolated_storage, "Unresolved Thing")
    insert_folder_row("Unresolved Thing")
    generic_bundle(isolated_storage, "Not Scanned Thing")
    insert_folder_row("Not Scanned Thing", "Not Scanned Thing")
    headers = auth_header(client)

    report = detect(client, headers, limit=1000)

    reasons = {row["blocked_reason"] for row in report["bundles"]}
    assert reasons <= {None, "archive_only", "unresolved_directory", "not_scanned"}, reasons
    for row in report["bundles"]:
        assert (row["status"] == "blocked") == (row["blocked_reason"] is not None), row["key"]
    assert {
        (row["key"], row["status"], row["blocked_reason"])
        for row in report["bundles"]
        if row["status"] == "blocked"
    } == {
        ("import/lone_export.zip", "blocked", "archive_only"),
        ("Unresolved Thing", "blocked", "unresolved_directory"),
        ("Not Scanned Thing", "blocked", "not_scanned"),
    }, "all three arms answered by one corpus, in one report"


# --- one dedupe for all three project-creation doors (Mf-yhjo) ----------------
#
# Three doors make a project: the typed form (``POST /projects``), the folder the
# detector read (``POST /bundles/create``) and the archive that twins that folder
# (``POST /import/archive/project``). Before this ticket each of them decided
# SEPARATELY whether the thing already existed, and two of the three decided by
# DIFFERENT rules - so the one live tower became TWO projects with TWO names
# (``Fates End - Wizard Tower`` from the folder, ``01_Fates End - Wizard Tower`` from
# the ``.zip`` beside it) and the operator was asked to confirm one product twice.
#
# What the doors now share is ``project_create_service``: one ``find_existing_project``,
# one ``member_asset_ids``, one ``create_project_from_members``. So the witnesses here
# put BOTH real doors on the disk at once, over the measured fixture, in both
# directions. That is the whole point: each door has always deduped against its own
# previous click, and that is worth nothing to an operator who clicked the other one.
# The direction is load-bearing and both halves are pinned separately, because the two
# directions are not the same read - a folder project is recognised from the archive
# door through the twin the create recorded in its marker, and an archive project is
# recognised from the report through the ``archive:``-prefixed key its own create wrote.


def archive_project_create(client: TestClient, headers: dict, path: str = TOWER_ZIP) -> dict:
    """The OTHER door: ``POST /import/archive/project`` over one library archive.

    The archive door's own suite is ``tests/test_archive_project.py``; this file reaches
    it because the ticket is about the two doors meeting, and a door imported through a
    helper would be a door the HTTP layer is not really running.
    """
    response = client.post("/import/archive/project", json={"path": path}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def bundle_drift_of(client: TestClient, headers: dict, project_id: str) -> dict:
    """What ``GET /project/{id}/bundle-drift`` answers for one project, as the wire has it."""
    response = client.get(f"/project/{project_id}/bundle-drift", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def add_project_members(client: TestClient, headers: dict, project_id: str, asset_ids: List[str]) -> dict:
    """``POST /project/{id}/assets`` - the one action a ``known`` drift answer offers."""
    response = client.post(f"/project/{project_id}/assets", json={"asset_ids": asset_ids}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def project_rows() -> List[Project]:
    """Every ``Project`` row in the database - the count a duplicated door would move."""
    with Session(engine) as session:
        return list(session.exec(select(Project)))


def project_metadata(project_id: str) -> str:
    """One project's raw ``metadata_json`` column, exactly as the database holds it.

    The string, not a parsed dict: a reader under test is ``archive_extract_payload_of``,
    and handing it a dict it was never written to read would test the test.
    """
    with Session(engine) as session:
        project = session.get(Project, project_id)
    assert project is not None, f"no project row for {project_id}"
    return project.metadata_json or "{}"


def archive_extract_payload_of_project(project_id: str) -> dict:
    """One project's archive-door payload, through the ONE reader of it.

    The reader is handed the ``metadata_json`` COLUMN, not a parsed dict: its whole
    contract is "a metadata column a human may have edited", so it does its own
    ``json.loads`` and answers ``{}`` for anything it cannot read. Handing it a dict
    would ask it a question it was never written to answer.
    """
    return project_create_service.archive_extract_payload_of(project_metadata(project_id))


def rewrite_metadata(project_id: str, metadata: dict) -> str:
    """Replace one project's metadata column by hand, and hand back what was written.

    This is how a pre-namespace row is built: written literally, by the test, because
    no code path writes that shape any more and the ticket forbids a migration.
    """
    with Session(engine) as session:
        project = session.get(Project, project_id)
        assert project is not None, f"no project row for {project_id}"
        project.metadata_json = json.dumps(metadata)
        session.add(project)
        session.commit()
    return project_metadata(project_id)


def test_a_project_created_from_the_bundle_is_the_project_its_twin_archive_would_have_made(
    client: TestClient, isolated_storage: Path
) -> None:
    """The folder door's project IS the project the archive door would have made.

    Measured on the real fixture through both real routes: the bundle create makes one
    project (``Fates End - Wizard Tower``, 15 models out of ``01_Wizard``), and the
    archive door asked about the very ``.zip`` that twins that folder answers
    ``already_created`` carrying **the same project id**, with the full ``planned``/
    ``resolved`` 20/20 it computed before it decided to write nothing. One ``Project``
    row stands at the end, and the create attached nothing a second time.

    The identity that answers is the twin the bundle create recorded in its marker
    (``archive: import/...zip`` sits in the marker's ``archive`` field), which is the
    dedupe's second leg seen from the other door - ``MATCH_MARKER``, not a fifth
    verdict. The old door answered ``created`` with a second row named
    ``01_Fates End - Wizard Tower`` and two ``Project`` rows; that is what this file
    did not have before Mf-yhjo and what it must never lose.

    Mutation (each of these is measured red, and each is a different half of the
    answer). Drop ``key=twin_rel`` from ``archive_project_service``'s
    ``Dedupe(key=twin_rel, archive=normalized)`` - the leg this direction runs on, since
    the folder project's marker key IS the twin path - and the door answers ``created``
    with a second row. Stop ``bundle_service._create_one`` writing ``"key": key`` into
    its marker, and there is no identity left to find. Let ``create_project_from_members``
    write through the dedupe answer (skip its ``find_existing_project`` call) and the
    second project appears whatever the door asks. And the honest bound, stated because
    it is not this witness: dropping ``archive=`` from the BUNDLE door's own ``Dedupe``
    does NOT red this test - measured, it reds
    ``test_an_archive_created_project_still_covers_the_folder_after_a_model_lands_in_it``
    instead - because a detection marker deliberately exposes no ``archive`` identity at
    all, so here the twin is matched on the key leg and never on the archive leg.
    """
    live_tower_extraction(isolated_storage)
    scan()
    headers = auth_header(client)

    created = create(client, headers, [{"key": TOWER_DIR}])

    assert (created["created"], created["skipped"], created["blocked"], created["failed"]) == (
        1,
        0,
        0,
        0,
    ), "the folder door creates once, which is the half the other door has to see"
    bundle_result = created["results"][0]
    assert (bundle_result["status"], bundle_result["reason"]) == ("created", None)
    project_id = bundle_result["project_id"]
    assert project_id is not None
    assert bundle_result["name"] == "Fates End - Wizard Tower", "the detector's name, ordinal stripped"
    assert bundle_result["assets_added"] == len(LIVE_MODEL_NAMES) + len(LIVE_PREVIEW_NAMES) + 2 == 20, (
        "the tower's fifteen models, three previews, its ``.txt`` and its nested zip - "
        "every registered file under the key (Mf-7oow)"
    )

    body = archive_project_create(client, headers)

    assert body["status"] == "already_created", (
        "the `.zip` beside the folder this door already made into a project is not a "
        "second product - the pre-Mf-yhjo door answered `created` here"
    )
    assert body["project"] is not None, "a refusal carries no project; an already-created answer carries THE one"
    assert body["project"]["id"] == project_id, "the same project, not a second one with a second name"
    assert body["project"]["name"] == "Fates End - Wizard Tower", "the folder's name survives, not the twin's"
    assert (body["planned"], body["resolved"]) == (20, 20), (
        "the door still read the listing and still resolved every entry - it refused "
        "at the dedupe, after the comparison, so a changed archive cannot hide"
    )

    assert table_counts()["projects"] == 1, "TWO rows is the defect this ticket exists to close"
    assert member_count(project_id) == 20, (
        "an already-created answer attaches nothing a second time - the twenty the "
        "bundle door attached (15 models, 3 previews, the ``.txt`` and the nested zip, "
        "Mf-7oow) are still the whole set, and the archive door's twenty-entry listing "
        "added not one of them"
    )


def test_a_project_created_from_the_archive_is_the_project_its_twin_folder_would_have_made(
    client: TestClient, isolated_storage: Path
) -> None:
    """The same claim from the other door first - and the report has to say it out loud.

    The archive door clicks first: ``created``, named ``01_Fates End - Wizard Tower``,
    20 assets. Then the folder door is asked what it can make of ``TOWER_DIR``, and the
    row answers ``ready`` carrying ``existing_project_id`` = that project,
    ``existing_project_match`` = ``"marker"``, ``bundle_drift_state`` = ``"known"`` and
    ``bundle_drift_count`` = ``0``; the report counts it once under
    ``already_created``; and the create pass that follows answers ``skipped`` /
    ``already_created`` with the SAME id and leaves one ``Project`` row.

    The two drift keys are the new part, and they are the reason the archive door's
    marker has to live in the same namespace as the detection door's: the drift walk
    reads ``rel_dir``, and an archive-extract marker carries its twin there. A project
    found on shared files (``assets``) reports ``not_applicable`` with a ``None`` count
    because it proves the files and not the intent - so the pre-Mf-yhjo shape is
    exactly what this row answered before: ``existing_project_match: "assets"``,
    ``bundle_drift_state: "not_applicable"``, ``bundle_drift_count: None``. A row that
    says "you already made this" while refusing to say whether the folder has grown is
    the same defect wearing a different face, which is why the route shape is pinned
    here too rather than only in ``test_projects_api.py``.

    Mutation (measured red, one at a time). Stop ``create_project_from_members``
    promoting the payload's ``twin`` to the marker's ``rel_dir`` and the match stays
    ``marker`` while both drift keys go ``unknown``/``None`` - the walk root is the one
    field the archive door does not write itself. Make ``bundle_drift.project_drift``
    answer without consulting membership (``is_member=None``) and the count answers 1
    where nothing is missing; the same refusal on the report side
    (``_report_row``'s own ``is_member``) reds here too. Drop ``archive=twin`` from
    ``_report_row``'s ``find_existing_project`` call and this direction loses its marker
    leg outright - ``existing_project_id`` goes ``None`` and the create below answers
    ``created`` with a second row over the same twenty files. And skip the dedupe inside
    ``create_project_from_members`` and the row still says ``already_created`` while the
    create makes a second project, which is what the last three assertions are for.

    What this witness does NOT depend on, measured: the archive door's own ``key=`` leg.
    The report asks with the CANDIDATE's key and the twin archive, so it is
    ``_report_row``'s ``archive=twin`` that carries this direction; the door's ``key=``
    leg is the one the other direction runs on, and it is pinned there.
    """
    live_tower_extraction(isolated_storage)
    scan()
    headers = auth_header(client)

    body = archive_project_create(client, headers)

    assert body["status"] == "created", "the archive door goes first, so it really writes"
    assert (body["planned"], body["resolved"]) == (20, 20)
    project_id = body["project"]["id"]
    assert body["project"]["name"] == "01_Fates End - Wizard Tower", "this door names the twin it wrote"
    assert member_count(project_id) == 20, "every entry the listing promised, not the models only"

    report = detect(client, headers)
    row = row_for(report, TOWER_DIR)

    assert (row["status"], row["blocked_reason"]) == ("ready", None), (
        "the row is still creatable-looking - `already_created` is a fact about the "
        "database, not a reason to hide the row from the operator"
    )
    assert row["existing_project_id"] == project_id
    assert row["existing_project_name"] == "01_Fates End - Wizard Tower"
    assert row["existing_project_match"] == "marker", (
        "the archive create wrote a key this report can read - `assets` here would be "
        "the pre-Mf-yhjo answer: two doors agreeing by accident of shared files"
    )
    assert (row["bundle_drift_state"], row["bundle_drift_count"]) == ("known", 0), (
        "`known`, not `not_applicable`: the marker names a walk root, and nothing under "
        "it is outside the project, because this door attached all twenty"
    )
    assert report["counts"]["already_created"] == 1

    created = create(client, headers, [{"key": TOWER_DIR}])

    assert (created["created"], created["skipped"], created["blocked"], created["failed"]) == (
        0,
        1,
        0,
        0,
    ), "the folder door creates nothing over a project the archive door already made"
    assert (created["results"][0]["status"], created["results"][0]["reason"]) == (
        "skipped",
        "already_created",
    )
    assert created["results"][0]["project_id"] == project_id
    assert created["results"][0]["assets_added"] == 0
    assert len(project_rows()) == 1, "TWO rows is the live complaint this ticket closes"
    assert member_count(project_id) == 20, "a skip touches neither the project nor its members"

    assert bundle_drift_of(client, headers, project_id) == {"state": "known", "count": 0, "asset_ids": []}, (
        "the route answers the same question the row answered, and `known` with no "
        "parts is a real answer here - not `not_applicable`, which is what a project "
        "this door never made would have to say"
    )


def test_the_archive_door_still_attaches_every_resolved_entry_of_the_tower(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-5's non-regression, pinned: the archive door's member set did not move.

    Mf-wsdo and Mf-7oow changed ONE door - the bundle door's member set - and the claim
    is that the archive door is untouched, because it never had a suffix table at all:
    what the operator ticked IS the file set, and ``create_project_from_extract`` hands
    the primitive every entry it resolved. So the same live tower through
    ``POST /import/archive/project`` resolves 20 of 20 and attaches all twenty - fifteen
    STLs, three PNGs, the vendor ``.txt`` AND the nested deliverable zip - and its cover
    is still the first member ``is_model_path`` answers for, which is
    ``file_utils.MODEL_SUFFIXES``, a third table neither ticket touches.

    It is also the producer of the archive door's measurement artifact (AGENTS.md wants
    the data, not the count: ``.plans/measurements/2026-11-10-bundle-members-mf-wsdo-archive-*.json``,
    written only when ``MV_MEMBER_ARTIFACT`` names a path). The pair must diff clean
    across the ticket, which is the honest form of "the archive door did not move":
    every member named on both sides, not a ``member_count == 20`` that would also be
    true of a door that attached the wrong twenty files.

    Mutation: filter the archive door's ``resolved`` list by any suffix table (the
    bundle door's deleted one included) and 20 becomes 18, red here and at
    ``planned``/``resolved``, while the ``.txt`` and the nested ``.zip`` drop out of the
    member set. That is the pair of artifacts this witness exists to keep comparable.
    """
    live_tower_extraction(isolated_storage)
    scan()
    headers = auth_header(client)

    body = archive_project_create(client, headers)
    project_id = body["project"]["id"]

    _member_artifact(
        door="archive_extract",
        key=TOWER_DIR,
        storage=isolated_storage,
        project_id=project_id,
        row=row_for(detect(client, headers), TOWER_DIR),
        result=body,
    )

    assert body["status"] == "created"
    assert (body["planned"], body["resolved"]) == (20, 20), (
        "the listing was the file set before this ticket and is the file set after it - "
        "this door has no kind to ask about"
    )
    assert member_count(project_id) == 20
    member_paths = project_storage_paths(project_id)
    with Session(engine) as session:
        registered = {asset.path for asset in bundle_service.registered_assets(session, TOWER_DIR)}

    assert member_paths == registered, (
        "all twenty registered files under the twin, including the two the bundle door "
        "excludes - the two doors answer different questions and their member sets "
        "legitimately differ, which is why this one is pinned here and not shared"
    )


def test_the_archive_door_never_answers_from_what_a_project_already_holds(
    client: TestClient, isolated_storage: Path
) -> None:
    """Containment is not an identity a click on one ``.zip`` carries.

    File containment - "some project already holds every one of these files" - is the
    weakest of the four identities and the only one that can merge a product with
    something a human assembled for another reason. Whether that is an answer depends
    entirely on who is asking, which is why it is a flag on ``Dedupe`` and not a leg that
    always runs. This is the half that says the archive door must not set it: a pile
    someone made by hand over exactly the tower's 20 files must not eat the Extract
    click, because the operator asked for the archive's own project and no marker can
    undo a merge made on somebody else's file list.

    Mutation: ``assets=True`` in ``archive_project_service``'s ``Dedupe`` - the shape the
    bundle door uses - and the click answers ``already_created`` carrying the hand-made
    id and one ``Project`` row stands. Its partner,
    ``test_the_bundle_door_answers_from_what_a_project_already_holds``, is the half that
    says the folder door must. Nothing else in the suite distinguishes the two doors:
    every other cross-door test runs on marker identity, where both doors agree.
    """
    live_tower_extraction(isolated_storage)
    scan()
    headers = auth_header(client)

    with Session(engine) as session:
        held = sorted(asset.path for asset in bundle_service.registered_assets(session, TOWER_DIR))

    assert len(held) >= 15, held
    hand_made_project("Someone else's wizard pile", held)

    body = archive_project_create(client, headers)

    assert body["status"] == "created", (
        "the archive door answered from what a project already holds: containment is "
        "not an identity a click on one .zip carries"
    )
    assert len(project_rows()) == 2, "the hand-made pile and the archive's project are two products"


def test_the_bundle_door_answers_from_what_a_project_already_holds(
    client: TestClient, isolated_storage: Path
) -> None:
    """The other half of the flag: the folder door holds containment, and has to.

    It is asking about a folder a scan registered, so a hand-made project over exactly
    those files IS the product - the row says ``existing_project_match == "assets"`` and
    the create is ``skipped`` / ``already_created`` carrying the hand-made id rather than
    a second project over files the library already has.

    Mutation: drop ``assets=True`` from ``bundle_service``'s ``Dedupe`` and both
    assertions red - the row finds nothing and the create makes a duplicate.
    """
    live_tower_extraction(isolated_storage)
    scan()
    headers = auth_header(client)

    hand_made_project("Someone else's wizard pile", sorted(registered_model_paths(TOWER_DIR)))

    row = row_for(detect(client, headers), TOWER_DIR)

    assert row["existing_project_match"] == "assets", (
        "containment is the only identity the folder row holds against a project with "
        "no marker and no source URL; if it answers anything else the leg is not running"
    )

    created = create(client, headers, [{"key": TOWER_DIR}])

    assert (created["created"], created["skipped"]) == (0, 1), (
        "the folder door did not recognise the hand-made project over its own files - "
        "drop `assets=True` from its `Dedupe` and this is the sentence"
    )
    assert created["results"][0]["reason"] == "already_created"
    assert created["results"][0]["project_id"] is not None
    assert len(project_rows()) == 1


def test_the_models_only_filter_is_still_the_answer_to_the_model_question(
    client: TestClient, isolated_storage: Path
) -> None:
    """Containment is asked with the MODELS even though the create ATTACHES 18 files.

    Mf-wsdo gave the bundle door two tables and so gave ``Dedupe`` a ``containment``
    field: "does a project already hold this bundle's files" is the MODEL question, while
    "what does the project hold" is the member question. This is the seam between them,
    on the live tower. A human's hand-made project over exactly the fifteen STLs - no
    previews, because a person printing a tower keeps the models - must still be
    recognised as the bundle's owner: the row answers ``existing_project_match ==
    "assets"`` and the create answers ``skipped``/``already_created`` carrying that id,
    leaving ONE ``Project`` row.

    Why it is a witness and not a re-run of
    ``test_the_bundle_door_answers_from_what_a_project_already_holds``: before this
    ticket both doors asked containment with the member set, so the two questions had
    one table and one assertion set. Now a create that answers 18 members and a dedupe
    that asks with 15 ids can disagree, and the failure mode is silent and expensive -
    every re-click on a bundle whose project a human made by hand becomes a duplicate
    project, which is the defect Mf-yhjo closed.

    Mutation (run, then restored): drop ``containment=tuple(model_assets)`` from
    ``_create_one``'s ``Dedupe`` so the primitive asks with the widened member set, and
    the ROW assertions stay green while ``created`` becomes ``(1, 0)``, the reason loses
    ``already_created`` and ``project_rows()`` holds two projects over the same fifteen
    files.
    """
    live_tower_extraction(isolated_storage)
    scan()
    headers = auth_header(client)

    project_id = hand_made_project("Someone else's wizard pile", sorted(registered_model_paths(TOWER_DIR)))

    row = row_for(detect(client, headers), TOWER_DIR)

    assert row["existing_project_match"] == "assets", (
        "the report still asks the model question - its containment set is the models "
        "the create would attach, and widening what a project HOLDS did not widen this"
    )

    created = create(client, headers, [{"key": TOWER_DIR}])

    assert (created["created"], created["skipped"]) == (0, 1), (
        "15 members held, 18 members on offer: containment is asked with the 15, or the "
        "operator gets a second project every time they press create again"
    )
    assert created["results"][0]["reason"] == "already_created"
    assert created["results"][0]["project_id"] == project_id
    assert len(project_rows()) == 1
    assert member_count(project_id) == 15, "a skip attaches nothing - not the previews either"


def test_an_archive_created_project_still_covers_the_folder_after_a_model_lands_in_it(
    client: TestClient, isolated_storage: Path
) -> None:
    """A late model in the folder does not make the folder a second product.

    The harder half of the previous witness, and the shape the operator actually hits:
    they extracted, pressed create, and only then dropped another STL into
    ``01_Wizard``. The row must still name the archive door's project
    (``existing_project_match`` still ``"marker"``, not demoted to ``"assets"`` and not
    ``None``) and it must now say so with a number: ``bundle_drift_state`` ``known``,
    ``bundle_drift_count`` ``1``. The create still answers ``skipped`` /
    ``already_created`` with that id and there is still ONE ``Project`` row.

    Before Mf-yhjo this row answered ``existing_project_id: None`` with
    ``existing_project_match: None`` - the folder's file set no longer matched any
    project's member set, so the assets leg missed and the marker leg did not exist -
    and the create answered ``created`` with a SECOND project named
    ``Fates End - Wizard Tower`` holding 16 models. That is the duplication the ticket
    is about, and it is exactly the case a "same files" dedupe cannot see: the drift is
    the whole point of the case, so the answer cannot come from the file set.

    Mutation (measured red, one at a time). Drop ``archive=`` from
    ``bundle_service._create_one``'s ``Dedupe(...)`` - the twin the folder row pairs with
    - and the row keeps its ``ready`` badge while the create below answers ``created``
    with the second project and its 16th model: the marker leg is the only thing that
    still answers this case, and it answers through that field. Drop
    ``archive=twin`` from ``_report_row``'s ``find_existing_project`` and the row itself
    goes ``existing_project_id: None``, which is the ticket's original complaint. Drop
    the membership filter from the report's drift call and the count answers 16 (every
    model under the root, member or not) where this pins ``1``. And skip the dedupe in
    ``create_project_from_members``: the row still tells the operator the project exists
    and the create duplicates it anyway.
    """
    live_tower_extraction(isolated_storage)
    scan()
    headers = auth_header(client)

    body = archive_project_create(client, headers)

    assert body["status"] == "created"
    project_id = body["project"]["id"]

    write_file(isolated_storage / TOWER_DIR / "01_Wizard" / "part-late.stl", b"s" * 4242)
    scan()

    row = row_for(detect(client, headers), TOWER_DIR)

    assert row["existing_project_match"] == "marker", (
        "the marker leg is what still answers once the folder no longer matches any "
        "member set exactly - `assets` cannot answer this case, it is the case"
    )
    assert row["existing_project_id"] == project_id
    assert (row["bundle_drift_state"], row["bundle_drift_count"]) == ("known", 1), (
        "one part outside the project, said as a number, on a project the OTHER door made"
    )

    created = create(client, headers, [{"key": TOWER_DIR}])

    assert (created["created"], created["skipped"]) == (0, 1), (
        "a folder that has grown is not a new product - pre-Mf-yhjo this answered "
        "`created` with a second project holding 16 models"
    )
    assert len(project_rows()) == 1
    assert member_count(project_id) == 20, "the drift stays a question, it is not an attach"


def test_an_archive_created_projects_drift_answer_is_membership_not_a_snapshot(
    client: TestClient, isolated_storage: Path
) -> None:
    """The drift answer is re-derived from membership, so it cannot be a stored count.

    Same fixture, same late model, then the operator does the one thing the route
    offers: ``POST /project/{id}/assets`` with exactly the id the drift answer named.
    The count goes back to ``0`` and the id is gone from ``asset_ids``. Nothing about
    the project's marker changed in between - the archive door wrote it before the file
    existed, and it still names 20 planned and 20 resolved entries while the folder
    holds 21 - so an answer that came from that payload could not possibly follow a
    membership edit.

    This is the intent behind condition 5 of the ticket, and it is why the marker stores
    no member list: a member list in the marker would be a second source of truth for
    "what is in this project", and the two would disagree the first time anyone added
    a part by hand. The project's ``project_asset`` rows are the only member list, and
    drift is a difference against them.

    Mutation (measured). Make the drift answer ignore membership at the route -
    ``bundle_drift.project_drift``'s ``is_member=members.__contains__`` replaced by
    ``None`` - and the count stays ``1`` after the add below: THIS reds, together with
    ``test_a_project_created_from_the_archive_is_the_project_its_twin_folder_would_have_made``.
    The same mutation on the REPORT side (``_report_row``'s ``is_member``) leaves this
    test green and reds the witness before it instead, because the row and the route
    read membership through two different hands - the report's prebuilt asset index and
    the route's correlated ``EXISTS`` flag - which is exactly why both witnesses exist.
    Hand-writing a member list into the marker is the other way to satisfy a snapshot,
    and there is no such field to hand-write: ``resolved`` stays 20 below whatever the
    project's members become, which the last assertion states out loud.
    """
    live_tower_extraction(isolated_storage)
    scan()
    headers = auth_header(client)

    body = archive_project_create(client, headers)

    assert body["status"] == "created"
    project_id = body["project"]["id"]
    assert bundle_drift_of(client, headers, project_id) == {"state": "known", "count": 0, "asset_ids": []}, (
        "the zero before the late model, so the number below is a change and not a coincidence"
    )

    write_file(isolated_storage / TOWER_DIR / "01_Wizard" / "part-late.stl", b"s" * 4242)
    scan()
    late_id = asset_id_at(f"{TOWER_MEMBER_DIR}/part-late.stl")

    drifted = bundle_drift_of(client, headers, project_id)

    assert (drifted["state"], drifted["count"]) == ("known", 1)
    assert drifted["asset_ids"] == [late_id], "the answer names the part, which is what the add below sends back"

    added = add_project_members(client, headers, project_id, [late_id])

    assert (added["added"], added["skipped"], added["failed"]) == ([late_id], [], [])
    assert member_count(project_id) == 21

    after = bundle_drift_of(client, headers, project_id)

    assert (after["state"], after["count"]) == ("known", 0), (
        "membership, not a snapshot: nothing in this project's marker was rewritten, "
        "and the payload still says 20 planned / 20 resolved"
    )
    assert late_id not in after["asset_ids"]
    assert archive_extract_payload_of_project(project_id)["resolved"] == 20, (
        "the marker still counts the entries the listing promised - it is provenance, "
        "and the drift answer demonstrably did not come from it"
    )


def test_a_project_written_before_the_marker_namespace_is_still_found_by_the_dedupe(
    client: TestClient, isolated_storage: Path
) -> None:
    """A pre-namespace row is still found - read, never migrated.

    Rows written by the pre-Mf-yhjo archive door carry their whole payload in the
    ``_archive_project`` block and no ``_bundle`` at all. This test writes one literally -
    ``rewrite_metadata`` replaces the column with the exact old shape - and then asks
    the two live questions: does the report still report that project for ``TOWER_DIR``
    (``existing_project_id`` set, ``existing_project_match`` ``"marker"``) and does the
    create still answer ``skipped`` / ``already_created`` with that id. Then the reader
    half: ``archive_extract_payload_of`` still finds the ``archive`` path in a hand
    written legacy block, and answers ``{}`` for a DETECTION marker row, which is the
    same rule seen from the side that must NOT match - a detection row is not an
    archive extract.

    The "still a marker match" claim is the load-bearing one. Without the legacy
    fallback in ``_read_marker`` the row falls to the assets leg and answers
    ``"assets"`` - which LOOKS similar and is not: the two doors disagree on the name by
    design, so an assets-only read is an answer that survives only while the files are
    identical, and it also silently drops the drift question.

    Mutation (measured). Rename or delete the ``LEGACY_ARCHIVE_MARKER`` block
    ``_read_marker`` falls back to - the row stops being a marker and the
    ``"marker"`` assertion above reds first, with the payload read below redding on the
    same mutation. Stop ``_create_one`` writing ``"key"`` into its marker and the
    detection row at the end loses the key that makes it a detection row. Skip the
    dedupe in ``create_project_from_members`` and the create in the middle answers
    ``created`` over a project the archive door already made. And for the ``{}`` at the
    end, the honest statement is that its two guards are individually redundant on this
    corpus and only the pair is load-bearing: flattening the ``archive_extract``
    sub-block into the flat marker (so the two doors' two ``archive`` fields meet in one
    dict) STILL answers ``{}`` while the origin check stands, and dropping the origin
    check STILL answers ``{}`` while the sub-block holds the payload - doing BOTH, which
    is what removing the namespace actually means, and the ``{}`` assertion reds with a
    whole detection marker in its place.

    What this test also pins, because it is the bound of "read, not migrated": the
    legacy row keeps its DEDUPE but not its DRIFT number. ``bundle_drift.classify`` is
    handed ``project_service.bundle_marker_of`` directly - the ``_bundle`` namespace and
    nothing else - so a row with no ``_bundle`` is ``not_applicable``, not ``unknown``
    and never a zero: there is no question to answer, and the module docstring's "no
    migration" rule stops at the reader that has to name a walk root.
    """
    live_tower_extraction(isolated_storage)
    scan()
    headers = auth_header(client)

    body = archive_project_create(client, headers)

    assert body["status"] == "created"
    project_id = body["project"]["id"]

    written = rewrite_metadata(
        project_id,
        {
            "_archive_project": {
                "archive": TOWER_ZIP,
                "twin": TOWER_DIR,
                "planned": 20,
                "resolved": 20,
                "fingerprint": "0" * 16,
                "common_root": "01_Wizard",
                "groups": [],
            }
        },
    )

    row = row_for(detect(client, headers), TOWER_DIR)

    assert row["existing_project_id"] == project_id, "the row written before the namespace is still found"
    assert row["existing_project_match"] == "marker", (
        "`assets` here is the legacy fallback not being read: the two doors disagree on "
        "the name on purpose, so only the block itself can prove this is that project"
    )
    assert (row["bundle_drift_state"], row["bundle_drift_count"]) == ("not_applicable", None), (
        "the honest bound: the legacy row is found by the dedupe and cannot be drifted, "
        "because the drift walk reads the `_bundle` namespace and this row has none"
    )

    created = create(client, headers, [{"key": TOWER_DIR}])

    assert (created["created"], created["skipped"]) == (0, 1)
    assert (created["results"][0]["status"], created["results"][0]["reason"]) == (
        "skipped",
        "already_created",
    )
    assert created["results"][0]["project_id"] == project_id
    assert len(project_rows()) == 1, "no migration ran, and none was needed"

    assert project_create_service.archive_extract_payload_of(written)["archive"] == TOWER_ZIP, (
        "the door still tells its story from the block it wrote before the namespace existed "
        "- and it is handed the raw column, because that is what a reader of a row gets"
    )

    # The mirror half: a DETECTION marker is not an archive extract, and the only
    # reader of the archive payload must say so with `{}` rather than with the
    # detection door's own `archive` field (which names the same `.zip`, for a
    # different purpose, in a different shape).
    generic_bundle(isolated_storage, "Plain Generic")
    scan()
    detection_create = create(client, headers, [{"key": "Plain Generic", "name_only": True}])

    assert detection_create["created"] == 1, "a real bundle create, so the row below is a real detection marker"
    detection_id = detection_create["results"][0]["project_id"]
    assert project_marker(detection_id)["key"] == "Plain Generic"
    assert project_create_service.archive_extract_payload_of(project_metadata(detection_id)) == {}, (
        "the detection door's `archive` is provenance, not an archive-extract payload - "
        "reading it here would hand every detected project an archive identity"
    )


def _callee_name(call: ast.Call) -> Optional[str]:
    """The function one call names, spelled either way: ``f(...)`` or ``pkg.f(...)``.

    The two doors reach the seam differently - ``bundle_service`` goes through the
    module attribute, ``archive_project_service`` through ``project_create_service.`` -
    and a pin that only saw one spelling would be a pin on one door.
    """
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _top_level_function(module, name: str) -> ast.FunctionDef:
    """``name``'s top-level definition, read out of the module's own source file.

    Top level only on purpose: a body nested inside another function is not the door
    the operator's click runs, and a helper could otherwise satisfy the pin from out of
    sight. ``_local_imports`` reads the same way for the same reason.
    """
    for node in ast.parse(Path(module.__file__).read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no top-level function named {name} in {module.__name__}")


def _string_comparands(body: ast.AST) -> List[str]:
    """Every string literal that sits on either side of a comparison in ``body``."""
    literals: List[str] = []
    for node in ast.walk(body):
        if not isinstance(node, ast.Compare):
            continue
        for operand in [node.left, *node.comparators]:
            literals.extend(
                inner.value
                for inner in ast.walk(operand)
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str)
            )
    return literals


# The deleted ATTACH table's name, assembled from two halves HERE on purpose: a
# literal in this file would be found by the grep the witness below feeds.
_DELETED_MEMBER_TABLE = "PROJECT_MEMBER" + "_SUFFIXES"
# Every model and image suffix the detector knows. A door that compares against one
# of these is a door answering the membership question itself.
KIND_SUFFIXES = (".stl", ".3mf", ".step", ".stp", ".lbrn", ".lbrn2", ".png", ".jpg", ".jpeg", ".webp", ".bmp")


def test_a_project_create_refuses_a_cover_outside_its_member_set(
    client: TestClient, isolated_storage: Path
) -> None:
    """The belt guard: a cover is a MEMBERSHIP question, and it is asked before the write.

    ``project_service.create_project_with_members`` checks that a cover asset EXISTS and
    never that it is a member - which is precisely why the live tower's project came out
    SET AND DANGLING (the artifact's ``cover_is_member: false``): the row offered the
    largest PNG, the create attached the models only, and nobody compared the two. The
    bundle door now picks its cover from the same owned set it attaches, so a cover
    outside the member set cannot happen by construction; this drives the primitive
    directly to pin the belt that makes it impossible to write one anyway.

    The ``require_members`` gate is the load-bearing half, and two witnesses beside it
    say why: ``tests/test_projects_api.py``'s ``test_create_project_rejects_an_unknown_cover_asset``
    expects a 404 from ``get_asset`` for ``POST /projects``, and
    ``test_a_cover_needs_no_membership_and_survives_membership_removal`` creates a project
    with zero members and a cover - "a cover is not a membership". Both are the manual
    door, which passes ``require_members=False``; a guard that ignored the flag would red
    them for the wrong reason. The counter-case below is that door's shape run through
    this one.

    Mutation (run, then restored): delete the guard and the first assertion answers
    ``created`` with a ``cover_asset_id`` no ``project_asset`` row backs - and nothing in
    the suite would notice except these two assertions, which is the hole the live box
    fell into.

    Mf-7oow note: the guard reads the member list it is HANDED, and the bundle door now
    hands it every registered file under the key. The models-only list below is
    therefore still exactly the "door that forgot" this belt exists for.
    """
    live_tower_extraction(isolated_storage)
    scan()

    cover_id = asset_id_at(f"{TOWER_MEMBER_DIR}/_FileName_Key.png")
    members = [
        project_create_service.MemberCandidate(asset_id_at(path), path)
        for path in sorted(registered_model_paths(TOWER_DIR))
    ]

    with Session(engine) as session:
        borrowed = project_create_service.create_project_from_members(
            session,
            name="A tower wearing a borrowed face",
            origin=project_create_service.ORIGIN_DETECTION,
            members=members,
            marker={"key": TOWER_DIR, "rel_dir": TOWER_DIR},
            cover_asset_id=cover_id,
        )

    assert borrowed.status == project_create_service.STATUS_REFUSED, (
        "the PNG is a registered asset, so `create_project_with_members` would have "
        "accepted it - the membership question is the one this guard owns"
    )
    assert borrowed.reason == "cover asset is not a member of the project"
    assert borrowed.members == ()
    assert borrowed.project_id is None
    assert project_rows() == [], (
        "ZERO Project rows: the guard sits after the dedupe, after `refuse` and after "
        "the empty-member refusal, and BEFORE the write - a refusal has written nothing"
    )

    with Session(engine) as session:
        # The `POST /projects` shape (`routers/projects.py`: `require_members=False`,
        # `members=()`, a typed cover) still answers `created`: the guard is this door's
        # rule, not the manual door's.
        manual = project_create_service.create_project_from_members(
            session,
            name="A form-typed project with a cover",
            origin=project_create_service.ORIGIN_MANUAL,
            members=(),
            require_members=False,
            cover_asset_id=cover_id,
        )

    assert manual.status == project_create_service.STATUS_CREATED
    assert manual.members == (), "a cover is not a membership - the manual door is untouched"

    with Session(engine) as session:
        # The positive control: the same members, this time with the PNG among them -
        # what the bundle door's `_members_of` hands over (every registered file) -
        # still writes.
        honest = project_create_service.create_project_from_members(
            session,
            name="A tower wearing its own face",
            origin=project_create_service.ORIGIN_DETECTION,
            members=[*members, project_create_service.MemberCandidate(cover_id, f"{TOWER_MEMBER_DIR}/_FileName_Key.png")],
            marker={"key": TOWER_DIR, "rel_dir": TOWER_DIR},
            cover_asset_id=cover_id,
        )

    assert honest.status == project_create_service.STATUS_CREATED
    assert len(honest.members) == 16, "fifteen models plus the preview that is the cover"


def test_an_obj_file_is_a_member_with_role_other(
    client: TestClient, isolated_storage: Path
) -> None:
    """The ``.obj`` ruling, measured: registrable, a MEMBER, and ``other`` to the classifier.

    The engine's own `MODEL_EXTS` calls ``.obj`` a model, so the row's
    ``model_count`` counts it; the ONE classifier ``library_scan.classify_entry``
    calls it ``other``, and that is the role the created project records. The two
    answers are pinned side by side on purpose: the row is the engine's measurement
    and the member role is the classifier's answer.

    Mutation (run, then restored): filter ``_members_of`` with any suffix table and
    the member count and the role map below red.
    """
    write_file(isolated_storage / "Obj Thing" / "part.stl", STL_BYTES)
    write_file(isolated_storage / "Obj Thing" / "part.obj", b"v\n")
    write_file(isolated_storage / "Obj Thing" / "cover.jpg", b"j" * 50)
    write_text(isolated_storage / "Obj Thing" / "notes.txt", "print at 0.2mm\n")
    scan()
    headers = auth_header(client)

    # The engine's model table and the classifier disagree about `.obj`: the row's
    # engine measurement counts it, while the ROLE the project records is the
    # classifier's answer, `other`.
    assert ".obj" in file_utils.MODEL_SUFFIXES
    assert library_scan.classify_entry("part.obj", 2) == library_scan.OTHER
    row = row_for(detect(client, headers), "Obj Thing")

    assert (row["model_count"], row["image_count"], row["other_count"]) == (2, 1, 0), (
        "the engine's model table counts both the STL and the .obj"
    )

    created = create(client, headers, [{"key": "Obj Thing"}])

    assert created["results"][0]["assets_added"] == 4, (
        "an `.obj` is a MEMBER now (Mf-7oow): four files, one of them `other`"
    )
    assert project_storage_paths(created["results"][0]["project_id"]) == {
        "Obj Thing/part.stl",
        "Obj Thing/part.obj",
        "Obj Thing/cover.jpg",
        "Obj Thing/notes.txt",
    }, "the model, the preview, the .obj and the document - and the `.obj` is still not a model"
    assert registered_model_paths("Obj Thing") == {"Obj Thing/part.stl"}, (
        "the MODEL question is unchanged: `file_utils.MODEL_SUFFIXES` is not this answer"
    )
    assert registered_asset_paths("Obj Thing") == {
        "Obj Thing/part.stl",
        "Obj Thing/part.obj",
        "Obj Thing/cover.jpg",
        "Obj Thing/notes.txt",
    }
    with Session(engine) as session:
        member_set = bundle_service._members_of(
            "Obj Thing", bundle_service.registered_assets(session, "Obj Thing")
        )

    assert {entry.rel_path: entry.role for entry in member_set.entries} == {
        "part.stl": library_scan.MODEL,
        "part.obj": library_scan.OTHER,
        "cover.jpg": library_scan.IMAGE,
        "notes.txt": library_scan.TEXT,
    }, "the project holds what the row counted, role for role"


def test_the_member_roles_are_the_classifiers_own(
    client: TestClient, isolated_storage: Path
) -> None:
    """The role table, measured on a mixed folder: `_members_of` asks ONE classifier.

    Membership is every registered asset under the key; the ROLE is
    ``library_scan.classify_entry``'s answer and nothing else. Pinned by NAME, so a
    suffix added to (or dropped from) any detector table moves this witness instead of
    silently changing what a project holds:

    * model   - ``.stl .3mf .step .stp .lbrn .lbrn2`` (the import allow-list minus the
      archive);
    * image   - ``.png .jpg .jpeg .webp .bmp`` (``IMAGE_SUFFIXES``);
    * text    - ``.pdf .txt .md .html .htm .rtf`` (``BUNDLE_TEXT_EXTS``);
    * other   - a backup (``.stlbak``), a 3D tool file the import door refuses
      (``.3dm``), a slicer project (``.scad``, ``.gcode``), and an extension nothing
      in this library knows;
    * archive - ``.zip``: a MEMBER now (it was the one file a create excluded), and
      still never a model.

    Mutation (each run, then restored): answer ``other`` for a ``.pdf`` and the text
    tally reds; add ``.txt`` to ``IMAGE_SUFFIXES`` and both the text and image tallies
    red; drop the ``.zip`` arm from ``classify_entry`` and the archive tally reds;
    filter ``_members_of`` by a suffix table and the member-set equality reds with the
    ``other`` and ``archive`` files missing.
    """
    roles = {
        "part.stl": library_scan.MODEL,
        "part.3mf": library_scan.MODEL,
        "part.step": library_scan.MODEL,
        "part.stp": library_scan.MODEL,
        "part.lbrn": library_scan.MODEL,
        "part.lbrn2": library_scan.MODEL,
        "render.png": library_scan.IMAGE,
        "render.jpg": library_scan.IMAGE,
        "render.jpeg": library_scan.IMAGE,
        "render.webp": library_scan.IMAGE,
        "render.bmp": library_scan.IMAGE,
        "docs.pdf": library_scan.TEXT,
        "notes.txt": library_scan.TEXT,
        "notes.md": library_scan.TEXT,
        "page.html": library_scan.TEXT,
        "page.htm": library_scan.TEXT,
        "notes.rtf": library_scan.TEXT,
        "part.stlbak": library_scan.OTHER,
        "part.3dm": library_scan.OTHER,
        "part.scad": library_scan.OTHER,
        "part.gcode": library_scan.OTHER,
        "mystery.qqq": library_scan.OTHER,
        "deliverable.zip": library_scan.ARCHIVE,
    }
    for name in roles:
        write_file(isolated_storage / "Roles" / name, b"x")
    scan()

    with Session(engine) as session:
        member_set = bundle_service._members_of(
            "Roles", bundle_service.registered_assets(session, "Roles")
        )

    assert {entry.rel_path: entry.role for entry in member_set.entries} == roles, (
        "membership is every registered file, and each one's role is the detector's own answer"
    )
    assert sorted(entry.rel_path for entry in member_set.entries) == sorted(roles), (
        "every registered file under the key is a MEMBER - nothing is filtered by role"
    )
    tallies: Dict[str, int] = {}
    for entry in member_set.entries:
        tallies[entry.role] = tallies.get(entry.role, 0) + 1
    assert tallies == {
        library_scan.MODEL: 6,
        library_scan.IMAGE: 5,
        library_scan.TEXT: 6,
        library_scan.OTHER: 5,
        library_scan.ARCHIVE: 1,
    }, "the role tallies of the mixed folder above - one per member, no member twice"
    for role in (library_scan.MODEL, library_scan.IMAGE, library_scan.TEXT, library_scan.OTHER, library_scan.ARCHIVE):
        assert [entry.rel_path for entry in member_set.by_role(role)] == sorted(
            name for name, found in roles.items() if found == role
        ), f"`by_role({role!r})` is the only view on the set, and it keeps input order"
    assert [entry.rel_path for entry in member_set.by_role(library_scan.MODEL)][:2] == [
        "part.3mf",
        "part.lbrn",
    ], "input order (path order) is preserved verbatim, so `ProjectAsset.position` cannot move"

    with Session(engine) as session:
        stray = bundle_service.registered_assets(session, "Roles")
    with pytest.raises(ValueError, match="is not under bundle key"):
        bundle_service._members_of("Some Other Key", stray)


def test_the_old_member_table_and_its_kinds_seam_are_gone_everywhere() -> None:
    """Mf-7oow's deletion, made checkable: no source file READS or BINDS the table.

    The ticket deleted ONE suffix table and ONE ``kinds=`` seam, and a table is
    exactly the thing that comes back by accident - a helper, a second module, a
    copied set. So this parses every ``.py`` under ``curiarium/api`` and demands that
    the name appear as NO identifier anywhere: not a module-level constant, not an
    attribute read, not an import. Prose is not parsed, which is deliberate - the
    pin exists to keep the RULE dead, and a comment naming the deleted table in the
    past tense cannot decide a member. It also says the same facts about the modules
    themselves:

    * no module carries the name, ``bundle_service`` and ``library_scan`` included;
    * ``project_create_service`` has neither ``member_asset_ids`` nor ``_suffix``;
    * ``create_project_from_members`` takes no ``kinds`` - it writes the member list
      the door handed it (``asset_ids = [candidate.asset_id for candidate in members]``).

    The name is assembled from two halves above on purpose: a literal here would be
    found by the very grep that feeds this assertion.

    Mutation: re-add the constant to ``bundle_service`` and the grep reds naming the
    file; re-add ``kinds=`` to the primitive and the signature assertion reds;
    re-introduce ``member_asset_ids`` and ``hasattr`` reds.
    """
    api_root = Path(bundle_service.__file__).resolve().parent
    hits: List[str] = []
    for path in sorted(api_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == _DELETED_MEMBER_TABLE:
                hits.append(f"{path.relative_to(api_root).as_posix()}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr == _DELETED_MEMBER_TABLE:
                hits.append(f"{path.relative_to(api_root).as_posix()}:{node.lineno}")

    assert hits == [], f"the deleted member suffix table is back in {hits}"
    for module in (bundle_service, project_create_service):
        assert not hasattr(module, _DELETED_MEMBER_TABLE), module.__name__
    assert not hasattr(project_create_service, "member_asset_ids"), (
        "the seam is deleted: `create_project_from_members` writes the list it is handed"
    )
    assert not hasattr(project_create_service, "_suffix"), (
        "the module names no file extension at all any more - the role is the detector's answer"
    )
    assert "kinds" not in inspect.signature(project_create_service.create_project_from_members).parameters, (
        "there is no table left to hand down"
    )


def test_the_member_set_is_decided_once_by_the_detectors_own_classifier() -> None:
    """THE new contract at the source: one member function, one role decision.

    ``_create_one`` asks ``bundle_service._members_of`` exactly ONCE and reads two
    views off it - the member list it writes, and ``by_role(MODEL)`` for ``attached``,
    ``models_by_group`` and ``Dedupe.containment``. It refuses:

    * a second ``_members_of`` call: a member set rebuilt from a different list is the
      Mf-quv4 hand-off returning;
    * a ``_suffix`` call, or a comparison against a model/image suffix literal: that is
      a door answering the membership question itself;
    * a ``classify_entry`` call: the ROLE is decided inside ``_members_of``, once, so
      a door cannot answer differently from the row it was created from;
    * a ``.suffix`` attribute read in ``project_create_service``'s whole source: the
      module that writes the project names no extension at all.

    And the mirror half: ``_members_of`` holds exactly ONE ``classify_entry`` call, so
    the one classifier is asked once per asset and nowhere outside ``library_scan`` -
    and the archive door consults no bundle rule at all (its resolved listing IS its
    member set, pinned by
    ``test_the_archive_door_still_attaches_every_resolved_entry_of_the_tower``).

    Mutations (each run, then restored): call ``_members_of`` twice and the count
    reds; put a ``classify_entry`` call into ``_create_one`` and the door assertion
    reds; filter inside ``_members_of`` with a suffix table and
    ``test_the_member_roles_are_the_classifiers_own`` and
    ``test_service_a_fresh_bundle_project_reports_no_new_parts`` red instead - this
    witness pins the SHAPE of the decision, those two pin its ANSWER.
    """
    create_one = _top_level_function(bundle_service, "_create_one")
    members_of = _top_level_function(bundle_service, "_members_of")

    door_calls = [
        name for name in (_callee_name(node) for node in ast.walk(create_one) if isinstance(node, ast.Call)) if name
    ]
    assert door_calls.count("_members_of") == 1, (
        "the door builds its member set once, from one list - a second build is a second opinion"
    )
    assert "_suffix" not in door_calls, "the create door filters by kind itself"
    assert "classify_entry" not in door_calls, (
        "the role is decided in `_members_of`; a door asking again could answer "
        "differently from the row it was created from"
    )
    assert not [
        text for text in _string_comparands(create_one) if text.lower().endswith(KIND_SUFFIXES)
    ], "the create door compares against a model or image suffix literal"
    by_role_reads = [node for node in ast.walk(create_one) if isinstance(node, ast.Attribute) and node.attr == "by_role"]
    assert len(by_role_reads) == 1, (
        "`by_role` is the only view on the member set, and the door asks it once - for MODEL"
    )

    members_of_calls = [
        name for name in (_callee_name(node) for node in ast.walk(members_of) if isinstance(node, ast.Call)) if name
    ]
    assert members_of_calls.count("classify_entry") == 1, (
        "exactly one role decision per asset, and it is the detector's own classifier"
    )

    seam_tree = ast.parse(Path(project_create_service.__file__).read_text())
    assert [
        node for node in ast.walk(seam_tree) if isinstance(node, ast.Attribute) and node.attr == "suffix"
    ] == [], "the project-writing module reads no file extension, let alone filters on one"

    archive_body = _top_level_function(archive_project_service, "create_project_from_extract")
    archive_calls = {
        name for name in (_callee_name(node) for node in ast.walk(archive_body) if isinstance(node, ast.Call)) if name
    }
    assert not archive_calls & {"_suffix", "_members_of", "classify_entry"}, (
        "the archive door consults no bundle rule: the entries the operator ticked ARE the member set"
    )


def test_asking_with_no_key_matches_no_project(
    client: TestClient, isolated_storage: Path
) -> None:
    """A door that holds no key must not match a project whose marker has no key.

    ``find_existing_project``'s marker leg reads ``wanted_key and marker.get("key")``,
    and the ``wanted_key`` half is the whole defence: both sides are ``None`` for a
    keyless ask over a keyless marker, and ``None == None`` is a match. A keyless marker
    is not hypothetical - a row written before the key was required has one, and the zip
    door's ``zip-project:<url>`` is a key no folder candidate can ever hold. Delete
    ``wanted_key and`` and this reds while every other test stays green, because every
    other test asks with a key.
    """
    live_tower_extraction(isolated_storage)
    scan()
    headers = auth_header(client)

    project_id = archive_project_create(client, headers)["project"]["id"]

    with Session(engine) as session:
        project = session.get(Project, project_id)
        marker = json.loads(project.metadata_json)[project_create_service.BUNDLE_MARKER]
        marker.pop("key")
        marker[project_create_service.ORIGIN_ARCHIVE_EXTRACT].pop("key", None)
        project.metadata_json = json.dumps({project_create_service.BUNDLE_MARKER: marker})
        session.add(project)
        session.commit()

        # The ask a door makes when it holds no identity of its own: every leg falsy.
        assert project_create_service.find_existing_project(session) is None
        assert project_create_service.find_existing_project(session, key=None) is None
        # A real key finds nothing either - the marker has none to answer with.
        assert project_create_service.find_existing_project(session, key=TOWER_DIR) is None


def test_the_create_door_asks_the_ownership_map_the_report_answered_with(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The door that ATTACHES reads the same one-owner map the REPORT read.

    ``_claim_owners`` settles which row owns each registered model; both the report
    and ``_create_one`` reach it through ``_own_assets``/``_claimed_assets``. This
    witness spies the expression the create calls and pins that it is handed a
    non-None map, and that the map is exactly the one a scan of the same disk builds.
    """
    live_tower_extraction(isolated_storage)
    scan()
    headers = auth_header(client)

    seen: List[tuple] = []
    creating = {"inside": False}
    real = bundle_service._claimed_assets
    real_create_one = bundle_service._create_one

    def spy(assets, *, key, claims):
        if creating["inside"]:
            seen.append((key, claims))
        return real(assets, key=key, claims=claims)

    def spy_create_one(*args, **kwargs):
        creating["inside"] = True
        try:
            return real_create_one(*args, **kwargs)
        finally:
            creating["inside"] = False

    monkeypatch.setattr(bundle_service, "_claimed_assets", spy)
    monkeypatch.setattr(bundle_service, "_create_one", spy_create_one)

    created = create(client, headers, [{"key": TOWER_DIR}])

    assert created["created"] == 1
    assert seen, "the create door built a member set without consulting ownership"
    tower_asks = [claims for key, claims in seen if key == TOWER_DIR]
    assert len(tower_asks) == 1, "one owned-set build per create - no second opinion"
    assert tower_asks[0] is not None, (
        "the create door attached everything its snapshot named, ignoring the map that "
        "decided which row owns each model"
    )
    expected = bundle_service._claim_owners(
        bundle_service.scan_bundles(isolated_storage).candidates, scope=""
    )
    assert tower_asks[0] == expected, "the create used a different map from a fresh scan"
    assert project_storage_paths(created["results"][0]["project_id"]) == registered_asset_paths(TOWER_DIR)


def test_no_member_create_leaves_an_empty_project_behind(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One transaction or none: an attach set that names a stranger cannot strand a project.

    The bundle door, asked to create the tower row, with its member builder
    (``bundle_service._members_of``) answering the real member set PLUS ``no-such-asset``
    - one id no ``Asset`` row holds. The honest way to build that state is to patch the
    builder for the duration of one real ``POST /bundles/create``: the door still runs,
    the report still runs, the cover is still a member (so the pre-write cover guard is
    not what refuses), and the create dies inside
    ``project_service.create_project_with_members``, which checks every id BEFORE it
    inserts. So the item answers ``failed`` and the ``Project`` table is still empty.

    The point is the single transaction, and what it replaced: the old door committed the
    project row, then attached files in a SECOND session through
    ``project_service.add_assets`` - a second door whose contract is per-id failure, so
    one unknown id left a project that holds nothing, visible in the list, with no
    members and no way to get them. ``create_project_from_members`` is the only write
    path now and it is one commit, which is what an empty table here measures.

    Mutation: make ``create_project_with_members`` insert first and check ids afterwards
    (or split it back into ``create_project`` + ``add_assets``, the pre-Mf-yhjo shape)
    and ``session.exec(select(Project)).all()`` returns a row. Make the door swallow the
    404 into a ``skipped`` result and the status assertion reds - and that one matters,
    because a skip that wrote a project is a project nobody asked for wearing the word
    "skipped".
    """
    live_tower_extraction(isolated_storage)
    scan()
    headers = auth_header(client)
    row = row_for(detect(client, headers), TOWER_DIR)

    real_members_of = bundle_service._members_of

    def with_a_stranger(key: str, assets) -> "bundle_service.MembersOf":
        """The real member set plus one id no ``Asset`` row holds."""
        member_set = real_members_of(key, assets)
        stranger = bundle_service.ProjectMember(
            asset=bundle_service.RegisteredAsset(
                id="no-such-asset",
                path=f"{key}/no-such-asset.stl",
                size=1,
                mime="application/octet-stream",
                thumb_version=0,
                filename="no-such-asset.stl",
            ),
            role=library_scan.MODEL,
            rel_path="no-such-asset.stl",
        )
        return bundle_service.MembersOf(entries=(*member_set.entries, stranger))

    monkeypatch.setattr(bundle_service, "_members_of", with_a_stranger)

    created = create(client, headers, [{"key": row["key"]}])

    assert created["created"] == 0
    assert created["failed"] == 1
    item = created["results"][0]
    assert (item["status"], item["project_id"]) == ("failed", None), (
        "it 404s out of `create_project_with_members` before the insert, so the door "
        "has nothing to report a project id for"
    )
    with Session(engine) as session:
        assert session.exec(select(Project)).all() == [], (
            "the whole create is one transaction: no project row, no member rows, "
            "nothing for a later reader to find"
        )


def test_an_existing_project_is_answered_before_a_refusal_the_door_brought_with_it() -> None:
    """Step 1 before step 2, pinned because three pieces of prose now claim it.

    ``create_project_from_members`` asks the dedupe and only then honours ``refuse``, so
    a row that BOTH already exists and would have been refused reports ``already_created``
    carrying the project the operator can open, not ``failed/contains_bundles`` about a
    create that never happens. That ordering is base's, not a new decision -
    ``_create_one`` asked ``find_existing_project`` before its contains-bundles belt - and
    the primitive inherits it, which is exactly the kind of fact that silently reverses
    the first time someone moves a line. Every ``contains_bundles`` test in this file
    runs the belt against a library with no project yet, so not one of them can see the
    order; the group-exclusion refusal is the opposite case and must stay AHEAD of the
    dedupe, which is where ``_create_one`` still answers it.

    Mutation: move the ``refuse`` check above the dedupe in ``create_project_from_members``
    and this reds - the operator is told a create was refused when the project is sitting
    in the library.
    """
    with Session(engine) as session:
        made = project_create_service.create_project_from_members(
            session,
            name="A tower the belt would have refused",
            origin=project_create_service.ORIGIN_DETECTION,
            members=(),
            require_members=False,
            marker={"key": "import/a tower", "rel_dir": "import/a tower"},
        )
        assert made.status == project_create_service.STATUS_CREATED

        refused_by_belt = project_create_service.Dedupe(key="import/a tower")
        answer = project_create_service.create_project_from_members(
            session,
            name="A tower the belt would have refused",
            origin=project_create_service.ORIGIN_DETECTION,
            members=(),
            require_members=False,
            dedupe=refused_by_belt,
            refuse="contains_bundles",
        )

        assert (answer.status, answer.reason) == (
            project_create_service.STATUS_ALREADY_CREATED,
            None,
        ), (
            "the project exists and is returned; a refusal about a create that was never "
            "attempted is the less useful sentence"
        )
        assert answer.project_id == made.project_id
        assert answer.match == project_create_service.MATCH_MARKER


def test_a_create_asked_for_no_members_refuses_instead_of_naming_an_empty_project() -> None:
    """The primitive's own third step, tested at the primitive.

    Neither door can reach this through its route today: the archive door checks its own
    resolved list before calling, and a bundle row that is ``ready`` has models by
    definition. That is exactly why it is pinned here rather than through a door - it is
    the guarantee the doors are allowed to lean on ("a create cannot leave a project with
    no files"), and a guarantee only reachable by a caller that forgets its own check is
    not a guarantee. Delete step 3 of ``create_project_from_members`` and this reds while
    every route-level test stays green.

    ``require_members=False`` is the other half: ``POST /projects`` is a form with no
    member list, and it must be allowed to make a project holding nothing at all.
    """
    with Session(engine) as session:
        refused = project_create_service.create_project_from_members(
            session,
            name="A project that was never going to have files",
            origin=project_create_service.ORIGIN_DETECTION,
            members=(),
        )

        assert (refused.status, refused.project_id) == (
            project_create_service.STATUS_REFUSED,
            None,
        )
        assert refused.reason == "no member files resolved"
        assert refused.members == ()

        # The same call with the requirement lifted is a create, not a refusal.
        allowed = project_create_service.create_project_from_members(
            session,
            name="A form, typed",
            origin=project_create_service.ORIGIN_MANUAL,
            members=(),
            require_members=False,
        )

        assert allowed.status == project_create_service.STATUS_CREATED
        assert allowed.members == ()
        assert session.exec(select(Project)).all() != []


def test_a_create_that_names_a_containment_set_without_the_assets_leg_raises() -> None:
    """``Dedupe.containment`` renames the ids the ASSETS leg asks with - no leg, no meaning.

    ``containment`` exists because the bundle door's member set is wider than the files
    that define its bundle (Mf-wsdo), and it is threaded to exactly one place:
    ``find_existing_project(assets=asked if dedupe.assets else ())``. A door that names
    ``containment`` and leaves ``assets`` False therefore asks NO containment identity at
    all, and the silence is total: the create goes on to write, the report keeps saying
    ``assets`` about the row, and the door believes it asked a question it never asked.
    That is a programming error rather than a verdict an operator can act on, so it is a
    ``ValueError`` beside the ``unknown create door`` one - the same class of mistake the
    primitive already refuses to guess about.

    The positive control below is what keeps the raise from being a ban: the same call
    with ``assets=True`` is the bundle door's own shape and still creates. Nothing reaches
    the database in the raising half, which is what the empty ``Project`` table measures -
    the precondition sits above ``member_asset_ids``, not beside the write.

    Mutation (both halves run, then restored, the production hash re-verified). Delete the
    raise and the call above stops raising: it walks past the dedupe, reaches
    ``create_project_with_members`` and dies there on ``HTTPException`` 404 ``Asset not
    found: a`` - a door that believed it had asked a containment identity, deduping
    against nothing and then failing for the wrong reason. Delete ``assets=True`` from the
    control instead and the control raises, which is the same fact read from the other
    side.
    """
    with Session(engine) as session:
        with pytest.raises(ValueError, match="containment"):
            project_create_service.create_project_from_members(
                session,
                name="A door that named ids and asked no leg",
                origin=project_create_service.ORIGIN_DETECTION,
                members=[project_create_service.MemberCandidate("a", "files/part0.stl")],
                dedupe=project_create_service.Dedupe(assets=False, containment=("a",)),
            )

    assert project_rows() == [], "the precondition is above the write, not beside it"

    with Session(engine) as session:
        asked = project_create_service.create_project_from_members(
            session,
            name="A door that asked the leg it named ids for",
            origin=project_create_service.ORIGIN_DETECTION,
            members=(),
            require_members=False,
            dedupe=project_create_service.Dedupe(assets=True, containment=("a",)),
        )

    assert asked.status == project_create_service.STATUS_CREATED, (
        "naming `containment` WITH the assets leg is the bundle door's own shape and must "
        "stay legal - the raise is a precondition, not a ban"
    )


def test_the_zip_row_keeps_its_extract_door_after_its_archive_became_a_project(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `.zip` row the Extract door already served must still be an Extract door.

    Fates End ships a tower as one wrapper archive and nothing else, so the archive is
    the row: detection offers ``01_Fates End - Wizard Tower.zip`` as
    ``blocked``/``archive_only`` because the web Extract door opens on exactly that
    reason (``ui/review/bundleRows.ts`` ``extractPathFor``). The operator extracts it,
    the files register, the archive door makes its project - and then the twin directory
    is deleted, which is the state that actually asks the question: a project now exists
    whose marker names THIS archive, and the row that names the same path is still the
    only place the operator can re-extract from.

    So the row must keep its own answer. It does, because an archive-extract marker's
    ``key`` is the archive's IDENTITY (``archive:`` + the path,
    ``project_create_service.archive_identity``) and not the path: the marker leg
    compares that field against a candidate's storage path, and a bare ``.zip`` path
    sitting in it is the row matching its own project. Measured with the prefix
    dropped, this row answers ``existing_project_match == "marker"``, the report's
    ``already_created`` goes 0 -> 1 for it, and the web layer hides already_created
    rows behind "Show created" (``BundleReviewView``'s own caption says so) - the door
    is gone and the queue looks tidier. Same shape, same reasoning as the zip-project
    door's ``zip-project:<url>`` key, and the same safety argument: no
    ``Asset.storage_path`` can carry a ``:``.

    Mutation: return ``normalised`` from ``archive_identity`` instead of the prefixed
    form and the two assertions that read the row's identity red together -
    ``existing_project_match`` becomes ``marker`` carrying that project id, and
    ``counts.already_created`` goes 0 -> 1. The ``(status, blocked_reason)`` assertion
    does NOT move, and saying so is the point: status is computed before the dedupe is
    asked, and ``is_archive`` forces ``blocked``/``archive_only`` whatever the dedupe
    says - which is exactly why the door survives the mutation while the web layer, that
    filters on ``already_created`` and not on ``blocked_reason``, does not. Delete the
    ``is_archive`` half of ``_report_row``'s twin leg too and the answer still does not
    move, the other thing this test is for: the guard is the second lock and the prefix
    is the load-bearing one, because an ``is_archive`` candidate carries no
    ``archive_path`` of its own, so that leg never even fires for one. Recording that
    beats leaving a comment that credits the wrong half.
    """
    monkeypatch.setattr(main, "start_scan", lambda: True)
    zip_rel = "01_Fates End - Wizard Tower.zip"
    twin_rel = "01_Fates End - Wizard Tower"
    write_zip(
        isolated_storage / zip_rel,
        {
            "01_Wizard/part-a.stl": b"s" * 44,
            "01_Wizard/part-b.stl": b"s" * 45,
            "01_Wizard/preview.jpg": b"j" * 200,
            "01_Wizard/notes.txt": b"print at 0.2mm\n",
        },
    )
    headers = auth_header(client)

    extracted = client.post("/import/archive", json={"path": zip_rel}, headers=headers)
    assert extracted.status_code == 200, extracted.text
    assert extracted.json()["destination"] == twin_rel
    scan()

    made = client.post("/import/archive/project", json={"path": zip_rel}, headers=headers)
    assert made.status_code == 200, made.text
    body = made.json()
    assert (body["status"], body["planned"], body["resolved"]) == ("created", 4, 4)
    project_id = body["project"]["id"]

    # The state that asks the question: the project stands, the twin it was built from
    # is gone, and the archive the row names has not moved.
    shutil.rmtree(isolated_storage / twin_rel)
    scan()
    report = detect(client, headers)

    row = row_for(report, zip_rel)
    assert (row["status"], row["blocked_reason"]) == ("blocked", "archive_only"), (
        "the reason the web Extract door opens on, still offered for the archive that "
        "already has a project - a dedupe that costs a door is not a dedupe gained"
    )
    assert (row["existing_project_id"], row["existing_project_match"]) == (None, None), (
        "the row is not reported as a project that already became: its `key` is a "
        "storage path, and the marker's `key` is an `archive:`-prefixed identity, so "
        "the two key spaces never meet"
    )
    assert report["counts"]["already_created"] == 0, (
        "counted from `existing_project_id`, so an identity that leaked into the path "
        "space would hide this row behind 'Show created' and read as a tidy queue"
    )
    refused = create(client, headers, [{"key": zip_rel}])
    assert refused["blocked"] == 1 and refused["results"][0]["reason"] == "archive_only", (
        "the create door still refuses it for the same reason it refuses it today, "
        "which is the sentence the web layer turns back into the Extract button"
    )
    listing = project_listing(client, headers)
    assert list(listing) == [project_id] and listing[project_id]["name"] == twin_rel.rsplit("/", 1)[-1], (
        "the archive's project is still there and still the only thing that was made"
    )


# --- detection stops being a gatekeeper (Mf-z643) ---------------------------
#
# Every row answers with a LIST of shapes (`matches`), the manual scope is always
# on offer, and a path inside an existing project's key stops being a detection
# row. These witness the acceptance criteria from the report's side.


def vault_tree(storage: Path) -> Path:
    """A refused product tree: two products, each with families of models beneath."""
    vault = storage / "import" / "Vault"
    # Two products that share no family token, so R7 declines them as a pile -
    # this is the empty hand, not a compound with another name.
    for product in ("alpha", "beta"):
        for family in ("core", "shell"):
            write_file(vault / product / family / "part.stl", b"s" * 40)
    write_file(vault / "preview.jpg", b"j" * 50)
    return vault


def test_every_row_carries_the_recommended_match_first(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-1: ``matches[0]`` is the row's own answer, in the engine's own order.

    Mutation: build the match list from a different measurement (or rank it again)
    and the equalities between ``matches[0]`` and the row's own fields red for some
    row. A non-creatable offer always says why, in the engine's own sentence.
    """
    moai_bundle(isolated_storage)
    write_stagetop(isolated_storage)
    generic_bundle(isolated_storage, "Generic Thing")
    vault_tree(isolated_storage)
    scan()
    headers = auth_header(client)

    report = detect(client, headers, limit=1000)

    assert len(report["bundles"]) >= 4
    for row in report["bundles"]:
        matches = row["matches"]
        assert matches, row["key"]
        assert all(match["shape"] and match["rule"] and match["confidence"] for match in matches), row["key"]
        assert matches[0]["shape"] == row["metadata_shape"], row["key"]
        assert matches[0]["rule"] == row["detector"], row["key"]
        assert row["detector"] == f"engine:{row['metadata_shape']}", row["key"]
        assert matches[0]["confidence"] == row["confidence"], row["key"]
        assert matches[0]["groups"] == row["groups"], row["key"]
        assert len({match["shape"] for match in matches}) == len(matches), row["key"]
        for match in matches:
            assert isinstance(match["creatable"], bool), row["key"]
            if match["creatable"]:
                assert match["note"] is None, (
                    f"{row['key']}: a creatable offer needs no arranging note"
                )
            else:
                assert match["note"].startswith("needs arranging first: "), row["key"]


def test_the_engine_is_the_one_ranking_engine(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-3/AC-4: every row's matches ARE the engine's offers, recommended first.

    `import/Agreeing` is one product in one folder: the engine offers `one-project`
    and nothing else. `import/Collection` holds three top folders that each ship
    their own document: the engine recommends `split-top-folders` and still offers
    `one-project` as the alternative the operator may choose.
    """
    agreeing_bundle(isolated_storage)
    collection_bundle(isolated_storage)
    scan()

    agreeing = service_row(AGREEING_KEY)

    assert agreeing["metadata_shape"] == "one-project"
    assert [match["shape"] for match in agreeing["matches"]] == ["one-project"]
    assert agreeing["matches"][0]["creatable"] is True

    collection = service_row(COLLECTION_KEY)

    assert collection["metadata_shape"] == "split-top-folders"
    assert [match["shape"] for match in collection["matches"]] == [
        "split-top-folders",
        "one-project",
    ]
    split, one_project = collection["matches"]
    assert split["rule"] == "engine:split-top-folders"
    assert split["creatable"] is False
    assert split["note"].startswith("needs arranging first: ")
    assert split["model_count"] == collection["model_count"]
    assert one_project["rule"] == "engine:one-project"
    assert one_project["creatable"] is True


def test_a_path_inside_an_existing_project_key_is_not_a_detection_row(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-4: the project's subtree is pruned, the key stays, and drift still answers.

    ``import/Stagetop`` is a project key: the report answers no row strictly
    inside it, keeps the key's own ``already_created`` row, ``GET
    /project/{id}/bundle-drift`` still answers ``known``, and a SCOPED report at
    that key answers no rows at all.

    Mutation: filter candidates after detection instead of pruning before it and
    the ledger/duplicates inside the project leak into the report; exclude the key
    itself and the ``already_created`` row disappears.
    """
    write_stagetop(isolated_storage)
    insert_folder_row(STAGETOP, f"import/{STAGETOP}")
    scan()
    headers = auth_header(client)
    with Session(engine) as session:
        project = project_service.create_project(
            session,
            "Stagetop project",
            None,
            None,
            origin="imported",
            metadata={
                "_bundle": {
                    "key": f"import/{STAGETOP}",
                    "detector": "compound-bundle",
                    "confidence": "low",
                    "rel_dir": f"import/{STAGETOP}",
                    "archive": None,
                    "twin_verified": None,
                }
            },
        )

    report = detect(client, headers, limit=1000)

    inside = [row for row in report["bundles"] if row["key"].startswith(f"import/{STAGETOP}/")]
    assert inside == [], "every path inside the project key is pruned"
    key_row = row_for(report, f"import/{STAGETOP}")
    assert key_row["existing_project_id"] == project.id
    assert key_row["existing_project_match"] == "marker"
    assert all(not item["rel_path"].startswith(f"import/{STAGETOP}/") for item in report["refused"])

    drift = bundle_drift_of(client, headers, project.id)
    assert drift["state"] == "known", "the project still owns its marker's key"

    scoped = detect(client, headers, path=f"import/{STAGETOP}", limit=1000)
    assert scoped["bundles"] == [], "a report scoped at the project key answers no rows"
    assert scoped["refused"] == []


def test_scan_bundles_accepts_excluded_keys_explicitly_in_marker_spelling(isolated_storage: Path) -> None:
    """``excluded_keys`` is keyword-only and spelled the way a marker stores it.

    Mutation: treat the keys as walk-relative under a scope (``_qualify`` instead
    of ``_unqualify``) and a scoped report stops excluding what the marker names.
    """
    write_stagetop(isolated_storage)

    scan = bundle_service.scan_bundles(
        isolated_storage, excluded_keys=frozenset({f"import/{STAGETOP}"})
    )

    keys = {candidate.key for candidate in scan.candidates}
    assert f"import/{STAGETOP}" in keys, "the key itself is never excluded"
    assert not any(key.startswith(f"import/{STAGETOP}/") for key in keys)
    assert not any(item.rel_path.startswith(f"import/{STAGETOP}/") for item in scan.refusals)
    assert bundle_service.scan_bundles(isolated_storage).candidates, "the default argument changes nothing"


def test_a_create_without_a_shape_records_the_recommended_shape(
    client: TestClient, isolated_storage: Path
) -> None:
    """No ``shape`` means the row's own answer, byte-identical to the old marker.

    Mutation: default the chosen shape to anything but ``metadata_shape`` and this
    reds - the field is additive, never a behaviour change for existing clients.
    """
    write_stagetop(isolated_storage)
    insert_folder_row(STAGETOP, f"import/{STAGETOP}")
    scan()
    row = service_row(f"import/{STAGETOP}")
    assert row["metadata_shape"] == "one-project"

    created = bundle_service.create_bundles(
        [bundle_service.BundleCreateItem(key=f"import/{STAGETOP}")], audit_ctx()
    )

    assert created["created"] == 1
    marker = project_marker(created["results"][0]["project_id"])
    assert marker["shape"] == row["metadata_shape"] == "one-project"
    assert marker["rule"] == row["detector"] == "engine:one-project"
    assert [group["name"] for group in marker["groups"]] == [
        group["name"] for group in row["groups"]
    ], "the compound marker still names the row's groups, in order"


