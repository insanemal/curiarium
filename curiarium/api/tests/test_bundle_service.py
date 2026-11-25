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
import dataclasses
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
import bundle_detect
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
    its suffix AND its ROLE (``bundle_detect.classify_entry`` over the bundle-relative
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
        return bundle_detect.classify_entry(path[len(key) + 1 :], asset.size if asset is not None else 0)

    role_tally = {
        role: sum(1 for path in member_paths if role_of(path) == role)
        for role in (
            bundle_detect.MODEL,
            bundle_detect.IMAGE,
            bundle_detect.TEXT,
            bundle_detect.OTHER,
            bundle_detect.ARCHIVE,
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
    title = bundle_detect.display_name(name)
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
    title = bundle_detect.display_name(name)
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
    assert row["detector"] == "thingiverse-readme"
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


def test_a_low_confidence_bundle_is_not_enriched_on_create(client: TestClient, isolated_storage: Path) -> None:
    generic_bundle(isolated_storage, "Generic Thing")
    scan()
    headers = auth_header(client)
    row = row_for(detect(client, headers), "Generic Thing")
    assert row["confidence"] == "low"

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
    assert row["detector"] == "thingiverse-sibling-docs"
    assert row["confidence"] == "high"
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
    assert row["title"] == "Moai statue -No overhang"
    assert row["creator"] == "moai_maker"
    assert row["licence"] == "CC BY-NC"
    assert row["source_url"] == THING_URL
    assert row["cover"] == {
        "asset_id": None,
        "rel_path": "images/a.jpg",
        "reason": "largest-image-unregistered",
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


def test_bulk_create_creates_every_low_confidence_bundle(client: TestClient, isolated_storage: Path) -> None:
    generic_bundle(isolated_storage, "Alpha Set")
    generic_bundle(isolated_storage, "Beta Set")
    scan()
    headers = auth_header(client)
    report = detect(client, headers)
    assert report["counts"]["low"] == 2

    created = create(
        client,
        headers,
        [{"key": row["key"], "name_only": True} for row in report["bundles"]],
    )

    assert created["created"] == 2
    assert len(project_listing(client, headers)) == 2


def test_a_container_directory_is_not_a_bundle(client: TestClient, isolated_storage: Path) -> None:
    write_file(isolated_storage / "P" / "Q" / "a.stl")
    write_file(isolated_storage / "P" / "Q" / "b.jpg", b"j" * 20)
    write_text(isolated_storage / "P" / "Q" / "c.txt", "notes")
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == ["P/Q"], "P holds nothing of its own"
    assert report["counts"]["total"] == 1


def test_a_grandparent_keeps_an_unclaimed_middle_directorys_files(client: TestClient, isolated_storage: Path) -> None:
    inner = isolated_storage / "P" / "Q" / "R"
    write_file(inner / "part.stl", STL_BYTES)
    write_file(inner / "cover.jpg", b"j" * 20)
    write_text(inner / "notes.txt", "a trio in the deepest directory")
    middle = isolated_storage / "P" / "Q"
    write_text(middle / "README.txt", README_TEXT)
    write_text(middle / "LICENSE.txt", LICENSE_TEXT)
    write_file(middle / "middle.stl", STL_BYTES)
    write_file(middle / "middle.jpg", b"j" * 30)
    write_text(middle / "middle.txt", "a trio one level up")
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == ["P/Q", "P/Q/R"], "the middle directory keeps its own files"
    assert row_for(report, "P/Q")["confidence"] == "high"
    assert row_for(report, "P/Q")["model_count"] == 1, "never the child's model under the recursive prefix"


def test_a_grandparent_keeps_the_files_a_middle_directory_did_not_claim(client: TestClient, isolated_storage: Path) -> None:
    inner = isolated_storage / "P" / "Q" / "R"
    write_file(inner / "part.stl", STL_BYTES)
    write_file(inner / "cover.jpg", b"j" * 20)
    write_text(inner / "notes.txt", "a trio in the deepest directory")
    middle = isolated_storage / "P" / "Q"
    write_file(middle / "middle.stl", STL_BYTES)
    write_file(middle / "middle.jpg", b"j" * 30)
    write_text(isolated_storage / "P" / "README.txt", README_TEXT)
    write_text(isolated_storage / "P" / "LICENSE.txt", LICENSE_TEXT)
    write_text(isolated_storage / "P" / "notes.txt", "the grandparent's own text")
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == ["P", "P/Q/R"], "claiming P/Q/R must not strip all of P/Q"
    parent = row_for(report, "P")
    assert parent["confidence"] == "high"
    assert parent["model_count"] == 1, "the grandparent keeps the middle directory's unclaimed model"
    assert parent["image_count"] == 1


def test_a_bundle_attaches_only_its_own_files(client: TestClient, isolated_storage: Path) -> None:
    unsorted_dir = isolated_storage / "Unsorted"
    moai_bundle(unsorted_dir, "111_A")
    moai_bundle(unsorted_dir, "222_B")
    write_text(unsorted_dir / "README.txt", README_TEXT)
    write_text(unsorted_dir / "LICENSE.txt", LICENSE_TEXT)
    write_file(unsorted_dir / "loose.stl", STL_BYTES)
    write_file(unsorted_dir / "loose.jpg", b"j" * 60)
    write_text(unsorted_dir / "notes.txt", "a loose trio")
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    keys = {row["key"] for row in report["bundles"]}
    assert {"Unsorted", "Unsorted/111_A", "Unsorted/222_B"} == keys
    parent = row_for(report, "Unsorted")
    assert parent["detector"] == "thingiverse-readme"
    assert parent["confidence"] == "high"
    assert parent["status"] == "ready", "the parent owns a model of its own"
    with Session(engine) as session:
        own = {
            asset.storage_path: asset.id
            for asset in session.exec(
                select(Asset).where(
                    Asset.storage_path.in_(
                        [
                            "Unsorted/README.txt",
                            "Unsorted/LICENSE.txt",
                            "Unsorted/loose.stl",
                            "Unsorted/loose.jpg",
                            "Unsorted/notes.txt",
                        ]
                    )
                )
            )
        }

    assert set(own) == {
        "Unsorted/README.txt",
        "Unsorted/LICENSE.txt",
        "Unsorted/loose.stl",
        "Unsorted/loose.jpg",
        "Unsorted/notes.txt",
    }, "every one of the parent's own files is registered"

    created = create(client, headers, [{"key": "Unsorted", "name_only": True}])

    assert created["created"] == 1
    project_id = created["results"][0]["project_id"]
    assert set(project_asset_ids(client, headers, project_id)) == set(own.values()), (
        "never a child bundle's files - and the parent's own set is now EVERY registered "
        "file under its key, its model, its preview and its three documents alike "
        "(Mf-7oow); the two child bundles' ten files each stay out"
    )


def test_a_low_container_of_low_confidence_children_is_not_a_bundle(
    client: TestClient, isolated_storage: Path
) -> None:
    unsorted_dir = isolated_storage / "Unsorted"
    generic_bundle(unsorted_dir, "111_A")
    generic_bundle(unsorted_dir, "222_B")
    write_file(unsorted_dir / "loose.stl", STL_BYTES)
    write_file(unsorted_dir / "loose.jpg", b"j" * 60)
    write_text(unsorted_dir / "notes.txt", "a loose trio")
    headers = auth_header(client)

    report = detect(client, headers)

    assert {row["key"] for row in report["bundles"]} == {"Unsorted/111_A", "Unsorted/222_B"}
    assert "Unsorted" not in {row["key"] for row in report["bundles"]}


def test_a_low_container_of_a_zip_only_bundle_is_not_a_bundle(
    client: TestClient, isolated_storage: Path
) -> None:
    inbox = isolated_storage / "Inbox"
    write_file(inbox / "loose.stl", STL_BYTES)
    write_file(inbox / "loose.jpg", b"j" * 60)
    write_text(inbox / "notes.txt", "a loose trio")
    write_zip(
        inbox / "Moai.zip",
        {
            "README.txt": README_TEXT.encode(),
            "LICENSE.txt": LICENSE_TEXT.encode(),
            "images/a.jpg": b"j" * 30,
            "files/part.stl": STL_BYTES,
        },
    )
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == ["Inbox/Moai.zip"]


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
    assert row["detector"] == "generic-bundle"
    assert row["confidence"] == "low"
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
    assert row["detector"] == "generic-bundle"
    assert row["confidence"] == "low"
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


def test_a_low_bundle_holding_a_bundle_is_refused_even_with_the_new_shape(
    client: TestClient, isolated_storage: Path
) -> None:
    """Under the old rule Wrapper was one merged bundle; the containment skip is intended."""
    wrapper = isolated_storage / "Wrapper"
    write_file(wrapper / "a.stl", STL_BYTES)
    write_file(wrapper / "cover.jpg", b"j" * 50)
    write_text(wrapper / "notes.txt", "the wrapper's own trio")
    inner = wrapper / "Inner"
    write_file(inner / "02_Fighter" / "part.stl", STL_BYTES)
    write_file(inner / "inner.jpg", b"j" * 40)
    write_text(inner / "inner.txt", "the inner bundle's own text")
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == ["Wrapper/Inner"]


def test_a_new_shape_folder_containing_a_zip_only_bundle_is_not_a_bundle(
    client: TestClient, isolated_storage: Path
) -> None:
    """The post-pass strip must fire for a folder whose evidence is the new shape."""
    inbox = isolated_storage / "Inbox"
    write_file(inbox / "wrap" / "a.stl", STL_BYTES)
    write_file(inbox / "cover.jpg", b"j" * 50)
    write_text(inbox / "notes.txt", "the folder's own image and text")
    write_zip(
        inbox / "Vendor.zip",
        {"part.stl": STL_BYTES, "cover.jpg": b"j" * 20, "notes.txt": b"the zip's own trio"},
    )
    headers = auth_header(client)

    report = detect(client, headers)

    # `Inbox` is a container and stays off the row list. `Inbox/wrap` is a
    # directory the rules refuse, and since Mf-z643 a refused directory is
    # offered as the MANUAL scope rather than silence - detection is no longer a
    # gatekeeper. The point of this test is that `Inbox` itself is not a bundle.
    assert [row["key"] for row in report["bundles"]] == ["Inbox/Vendor.zip", "Inbox/wrap"]
    assert row_for(report, "Inbox/wrap")["detector"] == "manual"
    assert row_for(report, "Inbox/wrap")["matches"][-1]["note"] == row_for(report, "Inbox/wrap")["matches"][-1]["rule"]


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


def test_a_parent_attaches_only_its_own_models(client: TestClient, isolated_storage: Path) -> None:
    title = "Wall Mount Bracket"
    parent = isolated_storage / "Parent Thing"
    write_text(parent / "README.txt", f"{title}\nCreated by Thingiverse user bracket_maker\nLicensed under CC BY-NC\n")
    write_text(
        parent / "LICENSE.txt",
        f"{title} by bracket_maker on Thingiverse: https://www.thingiverse.com/thing:111111\n",
    )
    write_file(parent / "parent_part.stl", STL_BYTES)
    moai_bundle(parent / "nested")
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    parent_row = row_for(report, "Parent Thing")
    child_row = row_for(report, f"Parent Thing/nested/{MOAI}")
    assert parent_row["detector"] == "thingiverse-readme"
    assert parent_row["confidence"] == "high"
    assert parent_row["model_count"] == 1, "the parent's snapshot names only its own model"
    assert parent_row["cover"]["reason"] == "model-preview", "the child's previews are not the parent's cover"
    assert parent_row["cover"]["rel_path"] == "Parent Thing/parent_part.stl", "the parent's own stl is its face"
    assert parent_row["cover"]["asset_id"] is not None
    assert parent_row["assets_total"] == 13, "assets_total counts the parent's 3 files plus the child's 10"
    assert child_row["confidence"] == "high"

    created = create(client, headers, [{"key": parent_row["key"]}, {"key": child_row["key"]}])

    assert created["created"] == 2
    parent_project, child_project = (result["project_id"] for result in created["results"])
    assert len(project_asset_ids(client, headers, parent_project)) == 3, (
        "the parent's own registered files only: its `.stl` and the two marker "
        "documents beside it (Mf-7oow attaches every one of them)"
    )
    assert len(project_asset_ids(client, headers, child_project)) == 10, (
        "the child's own files only - all ten of them (three models, five previews, two "
        "documents) - so the ownership split this witness exists for is intact"
    )


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
    assert row["detector"] == "thingiverse-readme"
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
    assert "import" not in keys, "a container is not a bundle"
    assert "import/ParentOf" not in keys, "a directory that holds only a bundle is not a bundle"
    # `import/residual` is refused by the rules and is now offered as the MANUAL
    # scope: every path is importable (Mf-z643), so a refused directory is no
    # longer an absence. The rule half of the old assertion is `detector`.
    assert row_for(report, "import/residual")["detector"] == "manual"
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
    }
    assert row["detector"] == "thingiverse-readme"
    assert row["archive"] is None, "a folder with no sibling zip reports no archive"
    assert row["cover"]["asset_id"] is not None
    assert row["cover"]["rel_path"] == f"{MOAI}/images/3.jpg"
    assert row["description_excerpt"].startswith("Moai statue -No overhang Created by Thingiverse user moai_maker")


def test_service_the_limit_bounds_the_report_and_a_path_scopes_it(isolated_storage: Path) -> None:
    vendor = isolated_storage / "vendor"
    moai_bundle(vendor, "Alpha Bundle")
    moai_bundle(vendor, "Beta Bundle")
    scan()

    bounded = bundle_service.build_report(limit=1)

    assert [row["key"] for row in bounded["bundles"]] == ["vendor/Alpha Bundle"]
    assert bounded["truncated"] is True, "a withheld row is reported, not hidden"
    assert bounded["counts"]["total"] == 2, "the counts still describe everything found"
    assert bundle_service.build_report(limit=50)["truncated"] is False

    scoped = bundle_service.build_report(path="vendor")

    assert scoped["root"] == "vendor"
    assert [row["key"] for row in scoped["bundles"]] == ["vendor/Alpha Bundle", "vendor/Beta Bundle"]


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

    assert row["confidence"] == "high"
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
    assert row["creator"] == "moai_maker"
    assert row["cover"]["rel_path"] == "images/a.jpg"
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


def test_create_refuses_a_low_candidate_whose_assets_include_another_bundles(
    isolated_storage: Path,
) -> None:
    """The belt: a low candidate that would swallow a subtree is refused, and writes no row."""
    inbox = isolated_storage / "Inbox"
    write_file(inbox / "loose.stl", STL_BYTES)
    write_file(inbox / "loose.jpg", b"j" * 60)
    write_text(inbox / "notes.txt", "a loose trio")
    moai_bundle(inbox / "Child")
    scan()
    snapshot = bundle_detect.BundleSnapshot(
        bundle_name="Inbox",
        rel_dir="Inbox",
        entries=(
            bundle_detect.BundleEntry(rel_path="loose.stl", size=len(STL_BYTES)),
            bundle_detect.BundleEntry(rel_path="loose.jpg", size=60),
            bundle_detect.BundleEntry(rel_path="notes.txt", size=13),
        ),
    )
    candidate = bundle_service.BundleCandidate(
        key="Inbox", snapshot=snapshot, archive_path=None, is_archive=False, confidence="low"
    )
    row = {
        "key": "Inbox",
        "rel_path": "Inbox",
        "name_suggestion": "Inbox",
        "creator": None,
        "licence": None,
        "licence_url": None,
        "source_url": None,
        "description_excerpt": None,
        "detector": "generic-bundle",
        "confidence": "low",
        "cover": {"asset_id": None, "rel_path": "Inbox/loose.jpg", "reason": "largest-image-unregistered", "url": None, "thumb_url": None},
        "status": "ready",
        "blocked_reason": None,
        "archive": None,
    }

    result = bundle_service._create_one(
        bundle_service.BundleCreateItem(key="Inbox", name_only=True), row, candidate, audit_ctx()
    )

    assert result["status"] == "failed"
    assert result["reason"] == "contains_bundles"
    with Session(engine) as session:
        assert session.exec(select(Project)).all() == [], "a refusal writes no Project row"


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
    assert row["detector"] == "thingiverse-readme"
    assert row["metadata_shape"] == "root-docs"
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
    assert project["description"] == CAT_CREATED_BY.strip()
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

    assert row["confidence"] == "high"
    assert row["creator"] == "nervoussystem"
    assert row["source_url"] == CAT_URL
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
    assert row["detector"] == "thingiverse-sibling-docs"
    assert row["creator"] == "nervoussystem"

    view = bundle_detect.BundleSnapshot(
        bundle_name="Mixed",
        rel_dir="Mixed",
        entries=(
            bundle_detect.BundleEntry(rel_path="README.txt", size=100),
            bundle_detect.BundleEntry(rel_path="LICENSE.txt", size=100),
            bundle_detect.BundleEntry(rel_path="Mixed_README", size=100),
            bundle_detect.BundleEntry(rel_path="Mixed_LICENSE", size=100),
            bundle_detect.BundleEntry(rel_path="files/part.stl", size=len(STL_BYTES)),
        ),
    )
    assert bundle_detect.doc_entry_candidates(view) == (
        "README.txt",
        "LICENSE.txt",
        "Mixed_README",
        "Mixed_LICENSE",
    )


def test_a_swapped_legacy_sibling_bundle_detects_and_creates(client: TestClient, isolated_storage: Path) -> None:
    name = "Legacy Cat"
    bundle = isolated_storage / name
    write_text(bundle / f"{name}_README" / "readme.txt", CAT_TITLE_LINE)
    write_text(bundle / f"{name}_LICENSE" / "license.txt", CAT_CREATED_BY)
    write_file(bundle / "part.stl", STL_BYTES)
    write_file(bundle / "preview.jpg", b"j" * 20)
    scan()
    headers = auth_header(client)

    row = row_for(detect(client, headers), name)

    assert row["confidence"] == "high"
    assert row["detector"] == "thingiverse-sibling-docs"
    assert row["metadata_shape"] == "sibling-docs"
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
    assert row["detector"] == "generic-bundle"
    assert row["confidence"] == "low"
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
    assert candidates[0].pair_only is False
    assert candidates[0].archive_owned is True, "the pairing owns this directory"
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
            write_file(isolated_storage / "Mini" / rel_path, payload)
        write_zip(isolated_storage / "Mini" / f"{name} Download.zip", payloads)

    candidates, _truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    paired = sorted(candidate.key for candidate in candidates if candidate.entry_root)
    unpaired = sorted(
        candidate.key
        for candidate in candidates
        if candidate.archive_path is None and "/" in candidate.key and not candidate.key.endswith(".zip")
    )
    own_archive_rows = sorted(candidate.key for candidate in candidates if candidate.is_archive)

    assert (len(paired), len(unpaired)) == (4, 6), (
        f"accepted={len(paired)} rejected={len(unpaired)} of {len(ENTRY_ROOT_CASES)}"
    )
    assert paired == sorted(f"Mini/{name}" for name in ENTRY_ROOT_CASES if name not in generic_names)
    assert unpaired == sorted(f"Mini/{name}" for name in ENTRY_ROOT_CASES if name in generic_names)
    assert own_archive_rows == sorted(f"Mini/{name} Download.zip" for name in ENTRY_ROOT_CASES if name in generic_names), (
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
    assert row["confidence"] == "low"

    created = create(client, headers, [{"key": "import/Gizmo Kit"}])

    assert created["created"] == 1
    project_id = created["results"][0]["project_id"]
    assert len(project_asset_ids(client, headers, project_id)) == 4, (
        "the archive attaches nothing: four is the directory's own registered set - two "
        "models, one preview and ``notes.txt`` (Mf-7oow) - never the zip's copies"
    )


def test_service_a_container_of_a_paired_directory_is_still_not_a_bundle(
    client: TestClient, isolated_storage: Path
) -> None:
    """A LOW container of a promoted directory is dropped; the pair is offered.

    ``Collection`` is a low bundle in its own right (a loose trio at its own
    depth), so the folder pass claims it. It also strictly contains a directory a
    verified archive owns, and a promoted candidate is a bundle: ``Collection``
    holds another thing's payload, so the container strip drops it. The paired
    directory stays, and because this test never scans it has no ``Folder`` row
    yet - ``unresolved_directory``, the ordinary refusal, not an archive-only row.
    """
    write_file(isolated_storage / "Collection" / "loose.stl", STL_BYTES)
    write_file(isolated_storage / "Collection" / "loose.jpg", b"j" * 60)
    write_text(isolated_storage / "Collection" / "notes.txt", "a loose trio")
    payloads = {"17_Thing/a.stl": STL_BYTES, "17_Thing/b.stl": STL_BYTES, "17_Thing/preview.jpg": b"j" * 50}
    for rel_path, payload in payloads.items():
        write_file(isolated_storage / "Collection" / rel_path, payload)
    write_zip(isolated_storage / "Collection" / "Thing Download.zip", payloads)
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == ["Collection/17_Thing"], (
        "the container Collection is dropped, the paired directory is offered"
    )
    row = row_for(report, "Collection/17_Thing")
    assert row["archive"] == {"rel_path": "Collection/Thing Download.zip", "twin_verified": True}
    assert row["blocked_reason"] == "unresolved_directory"

    candidates, _truncated = bundle_service.iter_bundle_candidates(isolated_storage)
    assert [candidate.key for candidate in candidates] == ["Collection/17_Thing"]
    assert candidates[0].archive_owned is True
    assert candidates[0].pair_only is False


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
    assert candidates[0].archive_owned is True
    assert any(entry.rel_path == "Clockwork_v1.8.zip" for entry in candidates[0].snapshot.entries), (
        "the nested archive is a file of the bundle's own listing"
    )


def test_service_a_no_docs_directory_without_its_archive_is_not_a_bundle(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-2 (b): the same 14 files without the archive are offered by nothing.

    This is the whole safety argument for the rule: 12 models and 2 renders and
    no text is too weak to call a bundle, so the shape is silent unless a
    verified pairing names the directory. It is also the silence the owner's
    117-LOW measurement argues for - his number, not ours, and not a number this
    fixture can re-measure. Under "treat the shape as a bundle regardless" the
    report here would offer a row and create a project, and this test fails.

    R5 changed the fixture's NAME, not its assertions: ``17_ClockworkMonster``
    reads as the title ``ClockworkMonster`` and IS offered now, so this test uses
    a lowercased name R5 refuses. What it pins is that the SHAPE alone is still
    silent.
    """
    clockwork_directory(isolated_storage, NO_TITLE_ROOT)
    scan()
    headers = auth_header(client)

    # The RULE is still silent: no detector offers this shape. Since Mf-z643 the
    # directory is nevertheless offered as the MANUAL scope, because detection is
    # no longer a gatekeeper - the row says "everything under this path" rather
    # than claiming the listing is a bundle.
    report = bundle_service.build_report()
    # The maximal refused root is `import` itself: it holds no row, so it is the
    # one MANUAL row covering everything under this path (Mf-z643).
    assert [row["key"] for row in report["bundles"]] == ["import"]
    assert report["bundles"][0]["detector"] == "manual"
    assert (report["bundles"][0]["model_count"], report["bundles"][0]["image_count"]) == (12, 2)
    # The manual row recommends itself, and the R6 shape is also on offer for the
    # same listing (the plan adds it in rule order for every carrier); the manual
    # entry is FIRST here because it is the recommended shape.
    #
    # Mf-yitm consults the engine on this row too, and MEASURED it offers no
    # dissent: the whole subtree has exactly ONE model-bearing top folder
    # (`17_clockworkmonster/`), so no `split-*` option exists to disagree with. The
    # list is therefore what it was before the engine was wired in - which is the
    # point of the assertion, not an omission from it: had the engine dissented, a
    # `split-*` entry here would be the ONLY source of that shape.
    assert [match["shape"] for match in report["bundles"][0]["matches"]] == ["manual", "archive-root"]
    assert not any(
        match["rule"].startswith("engine:") for match in report["bundles"][0]["matches"]
    ), "one model-bearing top folder: the engine has no split reading to offer"
    manual = report["bundles"][0]["matches"][0]
    assert manual["note"] in bundle_service.REFUSAL_RULES
    assert manual["note"] == report["bundles"][0]["matches"][0]["rule"]
    assert (manual["model_count"], manual["image_count"], manual["text_count"]) == (12, 2, 0)
    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)
    assert truncated is False
    assert [(candidate.key, candidate.manual) for candidate in candidates] == [("import", True)]

    created = create(client, headers, [{"key": "import"}])

    # The manual scope is creatable: the operator's choice replaces the shape
    # gate. It is still a real row with a resolved Folder and registered models,
    # so the create is one project over the whole subtree.
    assert created["created"] == 1
    assert created["results"][0]["assets_added"] == 14
    assert len(project_listing(client, headers)) == 1


def test_service_a_pile_of_bundles_and_a_zip_is_still_not_a_bundle(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-3 (c): the container rule still refuses a pile, pairing or not.

    ``import/Pile`` holds two real bundles plus its own loose model and render,
    and ``import/Pile Download.zip`` is rooted at ``Pile`` and contains exactly
    those two loose files - so containment verifies and only the container rule
    can refuse the promotion. The refused pair keeps the blocked
    ``archive_only`` row: visible on disk, unable to create. Under "promote any
    directory whose archive facts exist" ``Pile`` would be ``ready`` and this
    test fails.
    """
    loose_stl, loose_png = STL_BYTES, b"p" * 70
    write_file(isolated_storage / "import" / "Pile" / "loose.stl", loose_stl)
    write_file(isolated_storage / "import" / "Pile" / "render.png", loose_png)
    for name, model in (("Alpha", "a.stl"), ("Beta", "b.stl")):
        write_file(isolated_storage / "import" / "Pile" / name / model, b"s" * 41)
        write_file(isolated_storage / "import" / "Pile" / name / f"{name.lower()}.png", b"p" * 50)
        write_text(isolated_storage / "import" / "Pile" / name / "notes.txt", "print at 0.2mm")
    write_zip(
        isolated_storage / "import" / "Pile Download.zip",
        {"Pile/loose.stl": loose_stl, "Pile/render.png": loose_png},
    )
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    keys = [row["key"] for row in report["bundles"]]
    assert "import/Pile/Alpha" in keys and "import/Pile/Beta" in keys, "the two child bundles are rows"
    pile = row_for(report, "import/Pile")
    assert pile["status"] == "blocked"
    assert pile["blocked_reason"] == "archive_only", "a refused pair stays visible and creates nothing"

    created = create(client, headers, [{"key": key} for key in keys])

    assert created["created"] == 2
    assert created["blocked"] == 1
    assert created["failed"] == 0
    names = {project["name"] for project in project_listing(client, headers).values()}
    assert "Pile" not in names, "the pile is refused, never made into a project"


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


def test_service_a_verified_stem_twin_promotes_an_offered_nowhere_directory(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-1: four real pairs that no rule of their own offers become low rows.

    Each directory is a same-parent stem twin of its archive and its archive
    payload was written to disk byte-identically, so containment verifies:

    * SnVB623 - models only, every entry rooted at the directory name;
    * SnVD623 - models only, FLAT entries, so the identity-first leg is exercised;
    * ultimate-card-box-v4-model_files - models plus ``notes.txt``, no image;
    * Cinderwing3D - Crystal Dragon - models plus one render, rooted at ``files/``,
      the real generic entry root only the stem route may strip.

    All four are asserted because each is failed by a different mutation: delete
    the stem branch in ``_build_candidates`` and these four directories come back
    as R5 ``title-name`` rows instead of ``archive_owned`` ones, so every
    ``archive_owned`` / ``entry_root`` / ``archive_path`` / ``generic-bundle``
    assertion below reds, while restoring ``counts.image < 1 or counts.text`` in
    ``archive_owned_facts`` silently drops three of them (both models-only pairs
    and the models-plus-text one) and leaves only the models-plus-images pair.
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
        assert candidate.archive_owned is True, f"the verified stem pairing owns {name}"
        assert candidate.entry_root is None, "the stem route names no entry root"
        assert candidate.archive_path == f"import/{name}.zip"

    scan()
    headers = auth_header(client)
    report = detect(client, headers)

    assert sorted(row["key"] for row in report["bundles"]) == sorted(by_key)
    for name, _models, _images, _text in sets:
        row = row_for(report, f"import/{name}")
        assert row["confidence"] == "low", f"{name} is a shape inference"
        assert row["detector"] == "generic-bundle"
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


def test_service_a_pile_of_two_unnamed_model_directories_is_not_promoted(isolated_storage: Path) -> None:
    """AC-2a: the archive verifies and the pile is still refused.

    ``RepRap-MultiFilament`` holds two unnamed model-bearing subdirectories and
    no model at depth 0 and none under ``files/``, so it is a pile of things. Its
    stem archive's payload is exactly those two files, so containment PASSES; the
    only refusal is own-depth/cardinality, which is why removing
    ``_own_depth_model_count``'s cardinality bound promotes this pile and fails
    this test. Deleting the stem branch changes nothing here, so the test
    discriminates the shape guard, not the pairing.

    R5 offers ``Alpha`` (5 letters) as a title row and floor-refuses ``Beta``
    (4), which is the other half of the same guard: the pile is not promoted and
    its children are not swallowed.
    """
    root = isolated_storage / "import"
    write_file(root / "RepRap-MultiFilament" / "Alpha" / "part.stl", b"s" * 40)
    write_file(root / "RepRap-MultiFilament" / "Beta" / "part.stl", b"s" * 41)
    write_zip(
        root / "RepRap-MultiFilament.zip",
        {
            "RepRap-MultiFilament/Alpha/part.stl": b"s" * 40,
            "RepRap-MultiFilament/Beta/part.stl": b"s" * 41,
        },
    )

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    # `Alpha` is R5's title row; `Beta` is refused by the same floor and rides the
    # MANUAL scope (Mf-z643) instead of vanishing. Neither is the pile's row: the
    # pile itself holds a row and is therefore a container.
    assert [candidate.key for candidate in candidates] == [
        "import/RepRap-MultiFilament/Alpha",
        "import/RepRap-MultiFilament/Beta",
    ], "the pile is refused by own-depth/cardinality; only Alpha's name reads as a title"
    report = bundle_service.build_report()
    assert [row["key"] for row in report["bundles"]] == [
        "import/RepRap-MultiFilament/Alpha",
        "import/RepRap-MultiFilament/Beta",
    ]
    alpha = row_for(report, "import/RepRap-MultiFilament/Alpha")
    assert (alpha["detector"], alpha["confidence"]) == ("title-name", "low")
    assert row_for(report, "import/RepRap-MultiFilament/Beta")["detector"] == "manual"


def test_service_a_verified_stem_twin_does_not_promote_a_pile_of_bundles(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-2b: a verified stem twin never swallows a subtree of bundles.

    ``endorphin`` holds its own loose model and render plus two real child
    bundles, and ``endorphin.zip`` contains exactly the two loose files - so the
    stem check verifies and only the container rule can refuse the promotion. The
    children are ``ready`` rows; the pile is one ``blocked``/``archive_only`` row
    and creates nothing. Removing the ``claimed_ancestors`` refusal in the
    promotion pass makes ``endorphin`` ``ready`` and this test fails.
    """
    root = isolated_storage / "import" / "endorphin"
    write_file(root / "loose.stl", STL_BYTES)
    write_file(root / "render.png", b"p" * 70)
    for name, model in (("Alpha", "a.stl"), ("Beta", "b.stl")):
        write_file(root / name / model, b"s" * 41)
        write_file(root / name / f"{name.lower()}.png", b"p" * 50)
        write_text(root / name / "notes.txt", "print at 0.2mm")
    write_zip(
        isolated_storage / "import" / "endorphin.zip",
        {"endorphin/loose.stl": STL_BYTES, "endorphin/render.png": b"p" * 70},
    )
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    keys = [row["key"] for row in report["bundles"]]
    for child in ("import/endorphin/Alpha", "import/endorphin/Beta"):
        assert child in keys, "the two child bundles are rows of their own"
        assert row_for(report, child)["status"] == "ready"
    pile = row_for(report, "import/endorphin")
    assert pile["status"] == "blocked"
    assert pile["blocked_reason"] == "archive_only", "the refused pair stays visible and creates nothing"
    assert pile["archive"] == {"rel_path": "import/endorphin.zip", "twin_verified": True}

    created = create(client, headers, [{"key": key} for key in keys])

    assert created["created"] == 2
    assert created["blocked"] == 1
    assert created["failed"] == 0
    names = {project["name"].casefold() for project in project_listing(client, headers).values()}
    assert "endorphin" not in names, "the pile is refused, never made into a project"


# --- a promoted row never swallows a stronger candidate (Mf-v3i3) ------------

# The owner's own shape: a file extracted beside the zip it came in. The inner
# archive is a genuine R1 export (all four provenance markers), so it is a
# `high`-confidence candidate in its own right, and the identical payload on
# disk is what makes the stem route's containment check verify for the parent.
INNER_README = "Inner Thing\nCreated by Thingiverse user maker_inner\nLicensed under CC BY-NC\n"
INNER_LICENSE = "Inner Thing by maker_inner on Thingiverse: https://www.thingiverse.com/thing:777777\n"
INNER_URL = "https://www.thingiverse.com/thing:777777"


def test_service_a_promoted_row_does_not_swallow_a_nested_high_bundle(
    client: TestClient, isolated_storage: Path
) -> None:
    """The owner's shape: an extracted file beside its zip, and the zip is the bundle.

    ``import/P`` holds ``a.stl`` and ``inner.zip``, and ``import/P.zip`` contains
    exactly ``a.stl``, so the stem check verifies and only the new refusal can
    stop the promotion. ``inner.zip`` is a genuine R1 export on its own, so it is
    a ``high`` candidate and the parent is a hand-made container: it gets no row
    at all, and the child keeps its ``blocked``/``archive_only`` row. The
    identical payload is deliberate - containment has to pass for the promotion
    to be anything but dead code here.

    Mutation: deleting ``if _holds_stronger_candidate(folder, found): continue``
    restores the bug - ``import/P`` returns as a creatable ``archive_owned``
    ``low`` row and ``import/P/inner.zip`` disappears from the report entirely,
    because the member filter then deletes it as a file of the promoted
    directory.
    """
    root = isolated_storage / "import"
    write_zip(
        root / "P" / "inner.zip",
        {"README.txt": INNER_README.encode(), "LICENSE.txt": INNER_LICENSE.encode()},
    )
    write_file(root / "P" / "a.stl", b"s" * 40)
    write_zip(root / "P.zip", {"a.stl": b"s" * 40})

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    assert [candidate.key for candidate in candidates] == ["import/P/inner.zip"], (
        "the parent is a hand-made container: it gets no row at all, so the high child is all there is"
    )
    child = candidates[0]
    assert child.is_archive is True
    assert child.archive_owned is False
    assert child.confidence == "high"

    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == ["import/P/inner.zip"]
    row = row_for(report, "import/P/inner.zip")
    assert row["confidence"] == "high"
    assert row["detector"] == "thingiverse-readme"
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "archive_only"
    assert row["source_url"] == INNER_URL, "the high row is intact, not a stub"

    unknown = create(client, headers, [{"key": "import/P"}])
    assert unknown["created"] == 0
    assert unknown["failed"] == 1
    assert unknown["results"][0]["reason"] == "unknown bundle", "the parent is offered by nothing"
    blocked = create(client, headers, [{"key": "import/P/inner.zip"}])
    assert blocked["blocked"] == 1
    assert blocked["results"][0]["reason"] == "archive_only", (
        "an archive row is never creatable from the report"
    )
    assert project_listing(client, headers) == {}


def test_service_a_promoted_row_does_not_swallow_a_nested_medium_bundle(
    client: TestClient, isolated_storage: Path
) -> None:
    """A ``medium`` child is a stronger candidate too, not only a ``high`` one.

    ``import/M/5390184_inner.zip`` carries an id in its stem and one model at
    depth 0, so R4 offers it at ``medium``; ``import/M.zip`` contains exactly the
    parent's ``a.stl``, so the stem check verifies. The parent is a container and
    gets no row.

    Mutation: narrowing ``bundle_detect.STRONG_CONFIDENCES`` to
    ``frozenset({CONFIDENCE_HIGH})`` re-swallows this child - ``import/M`` comes
    back as the promoted row and the id-named archive vanishes.
    """
    root = isolated_storage / "import"
    write_file(root / "M" / "a.stl", b"s" * 40)
    write_zip(root / "M" / "5390184_inner.zip", {"part.stl": b"s" * 41})
    write_zip(root / "M.zip", {"a.stl": b"s" * 40})

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    assert [candidate.key for candidate in candidates] == ["import/M/5390184_inner.zip"], (
        "the id-named child survives and the parent, a hand-made container, is silent"
    )
    assert candidates[0].confidence == "medium"

    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == ["import/M/5390184_inner.zip"]
    row = row_for(report, "import/M/5390184_inner.zip")
    assert row["confidence"] == "medium"
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "archive_only"
    assert all(row["key"] != "import/M" for row in report["bundles"])


def test_service_a_promoted_row_still_wins_over_a_nested_low_member(
    client: TestClient, isolated_storage: Path
) -> None:
    """The discriminator is confidence, not shape: the Clockwork directory still wins.

    The Clockwork pair promotes its directory and the directory holds its own
    ``Clockwork_v1.8.zip``, whose facts are a LOW ``generic-bundle`` - a
    member-strength candidate, not a stronger one - so the promotion still fires,
    the directory owns the row and the zip is removed as a member. This is
    Decision 17's carve-out and the boundary of the new refusal.

    Mutation: widening the discriminator to "the subtree contains a zip" (the
    zip-only pass has already appended this candidate to ``found``) - or adding
    ``low`` to ``STRONG_CONFIDENCES`` - makes the parent vanish and the member
    surface as the only row.
    """
    clockwork_pair(isolated_storage)
    write_zip(
        isolated_storage / CLOCKWORK_DIR / "Clockwork_v1.8.zip",
        {"inner/part.stl": STL_BYTES, "inner/preview.jpg": b"j" * 50, "inner/notes.txt": b"n" * 20},
    )

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    assert [candidate.key for candidate in candidates] == [CLOCKWORK_DIR], (
        "the LOW member does not refuse the promotion, and is never a row of its own"
    )
    assert candidates[0].archive_owned is True
    assert candidates[0].pair_only is False
    assert all(not candidate.key.endswith("Clockwork_v1.8.zip") for candidate in candidates)

    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == [CLOCKWORK_DIR]
    assert row_for(report, CLOCKWORK_DIR)["status"] == "ready"


def test_service_a_pile_with_a_claimed_child_and_a_verified_twin_keeps_its_blocked_row(
    client: TestClient, isolated_storage: Path
) -> None:
    """The guard bounds the PROMOTION, never the container rule it sits beside.

    ``import/Q`` holds its own loose trio plus a claimed ``medium`` child, and
    ``import/Q.zip`` contains exactly the trio, so the stem check verifies and
    the container rule refuses the promotion: the parent is a blocked
    ``archive_only`` row and creates nothing, while the child keeps its own row.
    The refusal is the ``claimed_ancestors`` branch, which is *above* the new
    guard on purpose.

    Mutation: moving ``if _holds_stronger_candidate(folder, found): continue``
    above the ``if folder in claimed_ancestors:`` branch deletes the blocked
    pair-only row - the strong child refuses the promotion before the container
    rule can keep it visible - so this test reds.
    """
    root = isolated_storage / "import" / "Q"
    write_file(root / "loose.stl", STL_BYTES)
    write_file(root / "render.png", b"p" * 70)
    write_text(root / "notes.txt", "a loose trio")
    write_file(root / "5390184_inner" / "part.stl", b"s" * 41)
    write_zip(
        isolated_storage / "import" / "Q.zip",
        {"loose.stl": STL_BYTES, "render.png": b"p" * 70, "notes.txt": b"a loose trio"},
    )

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    by_key = {candidate.key: candidate for candidate in candidates}
    assert sorted(by_key) == ["import/Q", "import/Q/5390184_inner"]
    assert by_key["import/Q/5390184_inner"].confidence == "medium", "the id-named child is claimed"
    parent = by_key["import/Q"]
    assert parent.pair_only is True, "the container rule keeps the refused pair visible"
    assert parent.archive_owned is False

    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert row_for(report, "import/Q/5390184_inner")["confidence"] == "medium"
    row = row_for(report, "import/Q")
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "archive_only"


def test_service_a_stem_twin_that_does_not_match_pairs_nothing(isolated_storage: Path) -> None:
    """AC-3 (a) and (b): a namesake archive is not evidence until its payload agrees.

    ``xbox360shell.zip`` declares a size that differs from the file on disk and
    ``razercatearsremixmodelfiles.zip`` holds entries whose names match nothing,
    so both verifications fail. Neither directory is offered by a rule of its
    own and neither archive is a bundle of its own (two models only), so "no
    candidates" is the whole assertion. A ``_stem_pair_verified`` mutated to
    ``return True`` promotes both, so this test discriminates the verification.

    The fixture names are deliberately NOT title-shaped: both are lowercase
    single words R5 refuses, so stem verification is the only thing under test.
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
    # No archive PAIRED with a directory. The one candidate is `import` itself,
    # the maximal refused root, offered as the MANUAL scope (Mf-z643): both
    # namesake directories are inside it and the archives are children of it.
    assert [(candidate.key, candidate.manual) for candidate in candidates] == [("import", True)]
    report = bundle_service.build_report()
    assert [row["key"] for row in report["bundles"]] == ["import"]
    assert report["bundles"][0]["detector"] == "manual"
    assert report["bundles"][0]["archive"] is None


def test_service_a_failed_stem_verification_consumes_the_archive(isolated_storage: Path) -> None:
    """AC-3 tail: a failed stem check is the end of that archive, not a retry.

    ``outofmarbles.zip`` names the sibling ``xbox360shell`` by its entry root,
    so a fall-through to the entry-root route after the stem check fails WOULD
    pair ``xbox360shell``. The stem is the operator's own statement of which
    directory the archive is, and a failed check must never be retried under a
    weaker name: neither ``outofmarbles`` nor ``xbox360shell`` may be a row.
    Restoring the fall-through makes ``xbox360shell`` a row and fails this test;
    a ``_stem_pair_verified`` mutated to ``True`` makes ``outofmarbles`` a row
    and fails it too.

    R5 offers the title-shaped spelling of both, so the fixture names are
    lowercased to single words it refuses - the two mutations stay the only
    thing that can affect this result.
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
    # The failed stem check consumed the archive and paired nothing. The one
    # candidate is `import` itself, the maximal refused root, offered as the
    # MANUAL scope (Mf-z643) rather than silence.
    assert [(candidate.key, candidate.manual) for candidate in candidates] == [("import", True)]
    report = bundle_service.build_report()
    assert [row["key"] for row in report["bundles"]] == ["import"]
    assert report["bundles"][0]["detector"] == "manual"
    assert report["bundles"][0]["archive"] is None


def test_service_a_stem_twin_in_another_parent_pairs_nothing(isolated_storage: Path) -> None:
    """AC-4a: a stem twin is the directory BESIDE the archive, never a namesake deeper.

    ``import/Cinderwing3D - Crystal Dragon.zip`` names nothing in its own parent;
    the directory it matches sits under ``import/Deep``. Mutating
    ``_twin_directory`` to search every ancestor parent pairs them, so this test
    discriminates the same-parent bound of the stem route.

    R5 makes that namesake a LOW title row of its own and R6 offers the archive
    (one root, models and images, no text) as a row of its own, so the assertion
    moved to two unpaired rows: the directory is offered on its NAME and the
    archive is an ``archive-bundle`` row, and neither is ATTACHED to the other.
    Under the same mutation the directory row gains
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
        f"import/{CINDERWING}.zip",
        f"import/Deep/{CINDERWING}",
    ], "a namesake under another parent is not the archive's twin"
    by_key = {candidate.key: candidate for candidate in candidates}
    assert by_key[f"import/Deep/{CINDERWING}"].title_name is True
    assert by_key[f"import/Deep/{CINDERWING}"].archive_path is None
    assert by_key[f"import/{CINDERWING}.zip"].is_archive is True
    report = bundle_service.build_report()
    assert [row["key"] for row in report["bundles"]] == [
        f"import/{CINDERWING}.zip",
        f"import/Deep/{CINDERWING}",
    ]
    row = row_for(report, f"import/Deep/{CINDERWING}")
    assert row["archive"] is None, "the zip under import/ is never attached to a namesake under import/Deep"
    assert row["confidence"] == "low"
    assert row_for(report, f"import/{CINDERWING}.zip")["detector"] == "archive-bundle"


def test_service_an_entry_root_naming_another_parent_pairs_nothing(isolated_storage: Path) -> None:
    """AC-4b: an entry root names a directory in the archive's OWN parent only.

    ``import/Gizmo.zip`` is rooted at ``xbox360_shell/``, but that directory sits
    under ``import/other``. Mutating ``_entry_root_pair`` to search every parent
    pairs them, so this test discriminates its same-parent bound.

    R5 makes that directory a LOW title row of its own (``xbox360 shell`` has a
    separator) and R6 offers the archive as a row of its own, so the assertion
    moved to two unpaired rows. Under the same mutation the directory row gains
    that archive and reds.
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
        "import/Gizmo.zip",
        "import/other/xbox360_shell",
    ], "the named directory is not a sibling of the archive"
    by_key = {candidate.key: candidate for candidate in candidates}
    assert by_key["import/other/xbox360_shell"].title_name is True
    assert by_key["import/other/xbox360_shell"].archive_path is None
    assert by_key["import/Gizmo.zip"].is_archive is True
    report = bundle_service.build_report()
    assert [row["key"] for row in report["bundles"]] == [
        "import/Gizmo.zip",
        "import/other/xbox360_shell",
    ]
    assert row_for(report, "import/other/xbox360_shell")["archive"] is None, (
        "an archive rooted at a same-named directory under another parent is never attached"
    )


def test_service_a_promoted_stem_row_is_re_verified_by_containment(
    client: TestClient, isolated_storage: Path
) -> None:
    """The stem route's ``twin_verified`` is containment, re-checked at report time.

    The directory holds a ``spare.stl`` the flat archive does not, so the archive's
    basename set and the directory's are NOT equal - and the answer is still
    ``True``, because every entry of the archive is on disk. Mutating
    ``_archive_twin_verified`` to the names-only ``twin_verified`` for this row
    reports ``false`` and fails this test; the names-only answer stays correct for
    a directory a rule detects (``test_the_twin_zip_is_provenance_only`` pins it).
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
    assert row["archive"] == {"rel_path": f"import/{SNVD623}.zip", "twin_verified": True}, (
        "basename set equality is FALSE here (the folder has spare.stl, the archive does not) "
        "while containment is TRUE"
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
    assert row["confidence"] == "medium"
    assert row["detector"] == "thingiverse-id-name"
    assert row["metadata_shape"] == "id-name"
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
    assert marker["detector"] == "thingiverse-id-name"
    assert marker["confidence"] == "medium"


def test_service_a_doc_less_model_only_directory_without_an_id_is_still_refused(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-2: the same shape under an ordinary name stays invisible.

    Mutation: let R4 fire on any name and the report offers a row here, so both
    the empty-report assertions red and the create is no longer ``unknown
    bundle``.

    R5 now offers the title-shaped spelling (``Plain_parts`` reads as ``Plain
    parts``), so the fixture name is lowercased to one R5 refuses. An ordinary
    name getting nothing is what "R4 does not fire on any name" means.
    """
    write_file(isolated_storage / "import" / "plainparts" / "files" / "a.stl", STL_BYTES)
    scan()
    headers = auth_header(client)

    # R4 does not fire on an ordinary name: no detector offers this shape. The
    # directory itself is offered as the MANUAL scope (Mf-z643), so the create is
    # the operator's choice rather than a rule's claim - and it makes one project.
    report = bundle_service.build_report()
    # The maximal refused root is `import`, offered as the MANUAL scope (Mf-z643).
    assert [row["key"] for row in report["bundles"]] == ["import"]
    assert report["bundles"][0]["detector"] == "manual"
    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)
    assert truncated is False
    assert [(candidate.key, candidate.manual) for candidate in candidates] == [("import", True)]

    created = create(client, headers, [{"key": "import"}])

    assert created["created"] == 1
    assert created["results"][0]["assets_added"] == 1
    assert len(project_listing(client, headers)) == 1


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


def write_children_pile(storage: Path) -> Path:
    """An id-named pile holding two real child bundles plus its own loose trio."""
    pile = storage / "import" / "5390184_pile"
    write_file(pile / "loose.stl", STL_BYTES)
    write_file(pile / "render.png", b"p" * 70)
    write_text(pile / "notes.txt", "a loose trio")
    for name, model in (("Child_A", "a.stl"), ("Child_B", "b.stl")):
        write_file(pile / name / model, b"s" * 41)
        write_file(pile / name / f"{name}.png", b"p" * 50)
        write_text(pile / name / "notes.txt", "print at 0.2mm")
    return pile


def test_service_an_id_named_pile_beside_its_children_is_not_a_bundle(
    client: TestClient, isolated_storage: Path
) -> None:
    """The site-A witness: an inferred id never swallows a subtree.

    The pile carries a full trio AND an id in its name, so R4 offers it at
    ``medium``; only the widened site-A refusal keeps it out of the report.
    Mutation: put site A back to ``== CONFIDENCE_LOW`` and the pile becomes a
    creatable ``medium``/``ready`` row, so its absence assertion reds and the
    create pass makes three projects instead of two.
    """
    write_children_pile(isolated_storage)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    keys = [row["key"] for row in report["bundles"]]
    assert "import/5390184_pile/Child_A" in keys
    assert "import/5390184_pile/Child_B" in keys
    assert row_for(report, "import/5390184_pile/Child_A")["status"] == "ready"
    assert row_for(report, "import/5390184_pile/Child_B")["status"] == "ready"
    assert all(row["key"] != "import/5390184_pile" for row in report["bundles"])

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    assert {candidate.key for candidate in candidates} == {
        "import/5390184_pile/Child_A",
        "import/5390184_pile/Child_B",
    }

    created = create(client, headers, [{"key": key} for key in keys])

    assert created["created"] == 2
    assert created["failed"] == 0
    names = {project["name"] for project in project_listing(client, headers).values()}
    assert names == {"Child A", "Child B"}, "the pile is never made into a project"


def test_service_an_id_named_pile_with_its_stem_twin_is_blocked_and_reads_low(
    client: TestClient, isolated_storage: Path
) -> None:
    """A refused pairing is never stamped with the id provenance it was refused for.

    The archive is exactly the pile's two loose files, so containment verifies
    and only the container rule can refuse the promotion; the pair-only row then
    reads the archive-owned facts, which are ``low``/``generic`` with no URL.
    Mutation: un-widen site A and the pile is appended in the folder pass, so
    the promotion pass skips it (``any(candidate.key == folder for candidate in
    found)``) and there is no ``pair_only`` row at all - this test then sees a
    ``ready``/``medium`` pile and fails.
    """
    loose_stl, loose_png = STL_BYTES, b"p" * 70
    pile = write_children_pile(isolated_storage)
    write_zip(
        isolated_storage / "import" / "5390184_pile.zip",
        {"5390184_pile/loose.stl": loose_stl, "5390184_pile/render.png": loose_png},
    )
    assert (pile / "loose.stl").stat().st_size == len(loose_stl)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert row_for(report, "import/5390184_pile/Child_A")["status"] == "ready"
    assert row_for(report, "import/5390184_pile/Child_B")["status"] == "ready"
    row = row_for(report, "import/5390184_pile")
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "archive_only"
    assert row["archive"] == {"rel_path": "import/5390184_pile.zip", "twin_verified": True}
    assert row["confidence"] == "low"
    assert row["detector"] == "generic-bundle"
    assert row["metadata_shape"] == "generic"
    assert row["source_url"] is None

    created = create(client, headers, [{"key": row["key"]} for row in report["bundles"]])

    assert created["created"] == 2
    assert created["blocked"] == 1
    assert created["failed"] == 0
    names = {project["name"] for project in project_listing(client, headers).values()}
    assert "pile" not in names, "the pile is refused, never made into a project"


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
    assert row["confidence"] == "medium"
    assert row["detector"] == "thingiverse-id-name"
    assert row["status"] == "ready"
    assert row["archive"] == {"rel_path": f"import/{DRAGON}.zip", "twin_verified": True}
    assert report["counts"]["medium"] == 1
    assert report["counts"]["low"] == 0

    created = create(client, headers, [{"key": row["key"]}])

    assert created["created"] == 1
    assert member_count(created["results"][0]["project_id"]) == 1, "the archive attaches nothing"


def test_service_an_id_named_medium_folder_holding_a_vendor_zip_is_stripped(
    client: TestClient, isolated_storage: Path
) -> None:
    """The site-B witness: a medium folder holding a zip-only bundle is a container.

    The folder is offered at ``medium`` by R4 before the zip is ever read, so
    only the widened site-B strip keeps it from swallowing the zip as a member.
    Mutation: put site B back to ``== CONFIDENCE_LOW`` and the folder survives
    into ``bundle_dirs``, ``_nested_archives`` deletes the zip row, and both
    assertions below red - the folder is offered and the archive disappears.
    """
    bundle = isolated_storage / "import" / "5390184_set"
    write_file(bundle / "a.stl", STL_BYTES)
    write_zip(
        bundle / "inner.zip",
        {
            "README.txt": README_TEXT.encode(),
            "LICENSE.txt": LICENSE_TEXT.encode(),
            "images/a.jpg": b"j" * 30,
            "files/part.stl": STL_BYTES,
        },
    )
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    keys = {row["key"] for row in report["bundles"]}
    assert "import/5390184_set" not in keys, "a guess holding another bundle is a container"
    row = row_for(report, "import/5390184_set/inner.zip")
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "archive_only"


def test_create_refuses_a_medium_candidate_whose_assets_include_another_bundles(
    isolated_storage: Path,
) -> None:
    """The site-C witness: the create-time belt covers every inferred confidence.

    Mutation: put site C back to ``== CONFIDENCE_LOW`` and the medium candidate
    falls through to the project insert, so a ``Project`` row exists and both
    the empty-select assertion and the status assertion red.
    """
    inbox = isolated_storage / "Inbox"
    write_file(inbox / "loose.stl", STL_BYTES)
    write_file(inbox / "loose.jpg", b"j" * 60)
    write_text(inbox / "notes.txt", "a loose trio")
    moai_bundle(inbox / "Child")
    scan()
    snapshot = bundle_detect.BundleSnapshot(
        bundle_name="Inbox",
        rel_dir="Inbox",
        entries=(
            bundle_detect.BundleEntry(rel_path="loose.stl", size=len(STL_BYTES)),
            bundle_detect.BundleEntry(rel_path="loose.jpg", size=60),
            bundle_detect.BundleEntry(rel_path="notes.txt", size=13),
        ),
    )
    candidate = bundle_service.BundleCandidate(
        key="Inbox", snapshot=snapshot, archive_path=None, is_archive=False, confidence="medium"
    )
    row = {
        "key": "Inbox",
        "rel_path": "Inbox",
        "name_suggestion": "Inbox",
        "creator": None,
        "licence": None,
        "licence_url": None,
        "source_url": None,
        "description_excerpt": None,
        "detector": "thingiverse-id-name",
        "confidence": "medium",
        "metadata_shape": "id-name",
        "cover": {"asset_id": None, "rel_path": "Inbox/loose.jpg", "reason": "largest-image-unregistered", "url": None, "thumb_url": None},
        "status": "ready",
        "blocked_reason": None,
        "archive": None,
    }

    result = bundle_service._create_one(
        bundle_service.BundleCreateItem(key="Inbox", name_only=True), row, candidate, audit_ctx()
    )

    assert result["status"] == "failed"
    assert result["reason"] == "contains_bundles"
    with Session(engine) as session:
        assert session.exec(select(Project)).all() == [], "a refusal writes no Project row"


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
    assert row["confidence"] == "medium"
    assert row["detector"] == "thingiverse-id-name"
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


def write_title_folder(root: Path, name: str, models: int = 2) -> Path:
    """Models at the folder's own depth and nothing else: the name is all the evidence."""
    folder = root / name
    for index in range(models):
        write_file(folder / f"part{index}.stl", b"s" * (40 + index))
    return folder


def test_service_the_nine_title_shaped_folders_are_offered_and_the_two_parts_are_not(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-1, AC-2: the eleven measured directories, and only nine become rows.

    Each title folder holds models at its own depth and no image and no text, so
    no other rule can offer it: R1/R2 need documents, R4 needs an id and R3
    needs the trio. ``files`` and ``source`` are refused by the exclusion list
    and would fail both title branches anyway. Mutations: the discriminating ones
    are the branch mutations - drop branch A, the floor or the lowercase clause
    and the exact-key assertion reds - while emptying ``TITLE_PART_NAMES`` reds
    only the pure list tests, because those two names fail both branches too.
    """
    import_root = isolated_storage / "import"
    for name, models in TITLE_FOLDER_MODEL_COUNTS.items():
        write_title_folder(import_root, name, models)
    for name in PART_FOLDERS:
        write_title_folder(import_root, name, 1)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    title_keys = sorted(f"import/{name}" for name in TITLE_FOLDERS)
    part_keys = sorted(f"import/{name}" for name in PART_FOLDERS)
    # The nine title rows, plus the two part directories as MANUAL scopes: R5
    # refuses them, and a refused directory is offered as "everything under this
    # path" since Mf-z643. The rule still offers only the nine.
    assert sorted(row["key"] for row in report["bundles"]) == sorted(title_keys + part_keys)
    assert sorted(row["key"] for row in report["bundles"] if row["detector"] == "title-name") == title_keys
    for name in TITLE_FOLDERS:
        row = row_for(report, f"import/{name}")
        assert row["detector"] == "title-name", f"{name} is offered on its name alone"
        assert row["metadata_shape"] == "title-name"
        assert row["confidence"] == "low"
        assert row["status"] == "ready", f"{name} is creatable like any low row"
        assert (row["creator"], row["licence"], row["licence_url"], row["source_url"]) == (
            None,
            None,
            None,
            None,
        ), "a name proves nothing about who made a thing or what it may be reused for"
        assert row["model_count"] >= 1

    keys = {row["key"] for row in report["bundles"]}
    for name in PART_FOLDERS:
        assert f"import/{name}" in keys, "a refused directory is a manual scope, never silence"
        assert row_for(report, f"import/{name}")["detector"] == "manual", (
            f"{name} is a part name, never a title"
        )

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)
    assert truncated is False
    assert sorted(candidate.key for candidate in candidates) == sorted(title_keys + part_keys)
    titles = [candidate for candidate in candidates if candidate.title_name]
    assert sorted(candidate.key for candidate in titles) == title_keys
    assert all(candidate.manual for candidate in candidates if candidate.key in set(part_keys))
    assert all(candidate.archive_path is None for candidate in candidates)

    created = create(client, headers, [{"key": "import/skull mask"}])

    assert created["created"] == 1
    assert len(project_listing(client, headers)) == 1
    project_id = created["results"][0]["project_id"]
    assert member_count(project_id) == 2, "a title row attaches exactly its own models"


def test_service_a_name_derived_row_is_distinguishable_from_a_vendor_row(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-5: a name-derived row never looks like a vendor row on the wire.

    Both rows exist in one report and the triple plus the provenance fields tell
    them apart: the title row is ``title-name``/``title-name``/``low`` with four
    NULLs, the Moai row is ``thingiverse-readme``/``root-docs``/``high`` with its
    documents' creator, licence and URL. Mutation: enrich the title row from any
    source and the NULL quartet reds.
    """
    write_title_folder(isolated_storage / "import", "skull mask", 2)
    moai_bundle(isolated_storage)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    title = row_for(report, "import/skull mask")
    assert (title["detector"], title["metadata_shape"], title["confidence"]) == (
        "title-name",
        "title-name",
        "low",
    )
    assert (title["creator"], title["licence"], title["licence_url"], title["source_url"]) == (
        None,
        None,
        None,
        None,
    )

    vendor = row_for(report, MOAI)
    assert (vendor["detector"], vendor["metadata_shape"], vendor["confidence"]) == (
        "thingiverse-readme",
        "root-docs",
        "high",
    )
    assert vendor["creator"] == "moai_maker"
    assert vendor["licence"] == "CC BY-NC"
    assert vendor["licence_url"] == "https://creativecommons.org/licenses/by-nc/4.0/"
    assert vendor["source_url"] == THING_URL


def test_service_a_title_named_folder_of_bundles_is_still_a_container(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-3 at the NEW site: a title-named folder holding a bundle is a pile.

    ``import/Skull Mask`` holds a child FOLDER bundle and ``import/Stagetop``
    holds a ZIP-ONLY one, and both parents' names read as titles. Both parents
    keep a model of their OWN, so the claim tree leaves their listings non-empty
    and the container guard is the only thing that can refuse them. Neither is a
    row.

    The two halves discriminate two different guards, and that split is the
    point. ``Skull Mask``'s child is an ordinary claimed bundle, which
    ``contained_keys`` does not carry, so the leg of ``_relates_to_any`` that
    refuses a folder HOLDING a taken key is the ONLY refusal: drop it and
    ``import/Skull Mask`` is offered as a pile on its own loose model. The zip
    half is refused by that same leg first and by the ``contained_keys`` strip
    second - an archive-only row is a contained key - so it stays a witness for
    the older site rather than a second witness for the leg.
    """
    write_file(isolated_storage / "import" / "Skull Mask" / "loose.stl", STL_BYTES)
    inner = isolated_storage / "import" / "Skull Mask" / "Inner"
    write_file(inner / "part.stl", STL_BYTES)
    write_file(inner / "cover.jpg", b"j" * 50)
    write_text(inner / "notes.txt", "print at 0.2mm")
    write_file(isolated_storage / "import" / "Stagetop" / "loose.stl", STL_BYTES)
    write_zip(
        isolated_storage / "import" / "Stagetop" / "Vendor.zip",
        {"part.stl": STL_BYTES, "cover.jpg": b"j" * 50, "notes.txt": b"n" * 20},
    )
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    keys = {row["key"] for row in report["bundles"]}
    assert "import/Skull Mask" not in keys, "a folder of bundles is a pile whatever its name says"
    assert "import/Stagetop" not in keys, "a folder holding a zip-only bundle is a container too"
    assert "import/Skull Mask/Inner" in keys
    assert "import/Stagetop/Vendor.zip" in keys

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)
    assert truncated is False
    assert {candidate.key for candidate in candidates} == {
        "import/Skull Mask/Inner",
        "import/Stagetop/Vendor.zip",
    }


def test_service_title_name_detection_is_idempotent(isolated_storage: Path) -> None:
    """AC-6: reading the report twice is the same read, on disk and in the DB.

    ``build_report`` only reads. Mutation: let the title post-pass write a row, a
    dismissal or a file and both the payload equality and the two before/after
    comparisons red.
    """
    write_title_folder(isolated_storage / "import", "skull mask", 2)
    moai_bundle(isolated_storage)
    scan()
    files_before = filesystem_snapshot(isolated_storage)
    counts_before = table_counts()

    first = bundle_service.build_report()
    second = bundle_service.build_report()

    assert first == second
    assert len(first["bundles"]) == 2, "the title row and the vendor row"
    assert filesystem_snapshot(isolated_storage) == files_before
    assert table_counts() == counts_before


def test_service_the_switch_off_removes_only_the_title_rows(
    isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-7: the switch is one attribute the service's behaviour follows.

    Mutation: read ``TITLE_NAME_BUNDLES_ENABLED`` anywhere but inside
    ``title_name_facts`` and one of the two reports keeps a row it should not.
    """
    write_title_folder(isolated_storage / "import", "skull mask", 2)
    moai_bundle(isolated_storage)
    scan()

    monkeypatch.setattr(bundle_detect, "TITLE_NAME_BUNDLES_ENABLED", False)
    off = bundle_service.build_report()
    monkeypatch.setattr(bundle_detect, "TITLE_NAME_BUNDLES_ENABLED", True)
    on = bundle_service.build_report()

    assert [row["key"] for row in off["bundles"]] == [MOAI, "import"], (
        # With R5 off the title row is refused, so the maximal refused root is
        # `import` and it is offered as the MANUAL scope (Mf-z643). Only the
        # title rows depend on the switch, and this is that dependence.
        "only the title rows depend on the switch"
    )
    assert row_for(off, "import")["detector"] == "manual"
    assert sorted(row["key"] for row in on["bundles"]) == sorted([MOAI, "import/skull mask"])
    assert row_for(on, "import/skull mask")["detector"] == "title-name"


def test_service_r4_outranks_the_title_rule_on_an_id_named_folder(
    client: TestClient, isolated_storage: Path
) -> None:
    """Cross-cutting: the id-named export's display name is title-shaped too.

    Both rules apply to this listing; the service must offer the medium R4 row,
    with its derived URL, exactly once. Mutation: dispatch the title facts before
    ``detect_bundle`` (or run the post-pass without ``taken``) and this key comes
    back ``low`` - or twice, which ``row_for`` reds.
    """
    write_dragon(isolated_storage)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == [f"import/{DRAGON}"]
    row = row_for(report, f"import/{DRAGON}")
    assert row["confidence"] == "medium"
    assert row["detector"] == "thingiverse-id-name"
    assert row["source_url"] == DRAGON_URL
    assert report["counts"]["low"] == 0
    assert report["counts"]["total"] == 1


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


def no_offered_row_contains_another_offered_row(storage: Path) -> List[str]:
    """Creatable rows still holding another offered row - an empty list is the pass.

    ``member-of-bundle`` is how a promotion says "I took that row", so the row set a
    promotion leaves behind is ONE row, never the parent and the child side by side.
    This asks that of the OFFERED set: no row the operator could create from - not an
    archive, and not a ``pair_only`` container, which is the answer that legitimately
    sits ABOVE a row it refuses to own (``import/M``, ``import/L``, ``import/D``,
    ``import/X``) - may strictly contain another offered row. ``[]`` is the pass on
    every committed shape, and it is exactly what the deferred
    ``promoted_dissolutions`` apply site exists to keep true. Mutation (run): neuter the
    apply site's removal - write the refusals, skip the ``found`` filter - and on the one
    tree where the dissolution is the only thing that removes the child (the tower whose
    child holds no archive of its own) this returns
    ``['import/01_Fates End - Wizard Tower/01_Wizard']`` while ``member_refusals_name_a_real_row``
    still returns ``[]``. On the trees whose child the archive-containment strip drops
    anyway (the live tower, ``import/W``) it stays empty whatever the apply site does, and
    on B-1 the vetoed wrapper is never offered - which is why it is called from every
    promotion witness rather than trusted from one, and why it is a net over the ROW SET
    rather than a second reading of the ledger.

    Why this is not a copy of ``member_refusals_name_a_real_row``: that helper is
    measured against the LEDGER, and a ledger entry can be honest about a row set that
    is wrong. On a VETOED promotion it passes even in the broken build, because the
    veto leaves a cover row above the dissolved path - ``import/X`` is a row and
    ``import/X/P/A`` sits inside it, so "names a row or sits inside one" is satisfied.
    This helper never reads a refusal at all: it reads the rows themselves, so it
    catches the failure the ledger cannot express - two offered rows for one payload -
    and it does so without a fixture-shaped expected list for somebody to update to
    match a broken build. Neither subsumes the other, which is why each promotion
    witness calls both.
    """
    scan = bundle_service.scan_bundles(storage)
    offered = {candidate.key for candidate in scan.candidates}
    containers = [candidate.key for candidate in scan.candidates if not candidate.is_archive and not candidate.pair_only]
    return sorted(
        key for container in containers for key in offered if key != container and key.startswith(f"{container}/")
    )


def test_service_a_single_root_archive_without_a_readme_is_a_low_row(isolated_storage: Path) -> None:
    """AC-1: the Fates End tower becomes a row through ``build_report``, not only a scan.

    The archive has 14 models, 3 previews and the vendor's nested deliverable zip
    under one root and no text, so R3 refuses it for want of a document and R6
    offers it. The nested zip is an entry of the archive's listing, never a member
    of a bundle directory, so it must not move the outcome. ``build_report`` is the
    witness that matters: the row is rendered by ``_candidate_facts`` ->
    ``_detect_archive``, which shares the decision with the walk. Mutation: route
    only the walk through ``archive_bundle_decision`` and this report row vanishes
    because ``_report_row`` returns ``None`` when its own detect does.

    Every provenance field is ``None`` and the row is ``blocked`` /
    ``archive_only``, so the rule can never create a project on a guess.
    """
    write_zip(isolated_storage / FATES_TOWER_ZIP, fates_tower_entries())

    report = bundle_service.build_report()
    row = row_for(report, FATES_TOWER_ZIP)

    assert row["detector"] == "archive-bundle"
    assert row["confidence"] == "low"
    assert row["metadata_shape"] == "archive-root"
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
    assert row["cover"]["rel_path"] == FATES_TOWER_IMAGES[0], "the largest preview is the cover"


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
    assert row_for(bundle_service.build_report(), name)["detector"] == "generic-bundle"
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


def test_service_models_only_and_multi_root_archives_stay_refused(isolated_storage: Path) -> None:
    """AC-6: the restraint and the pile guard, at the service layer.

    The models-only archive has no image and the four-root archive is four things;
    both are refused at the PRIMARY R3 refusals, which is what makes the new rule
    a missing-document fill rather than a loosening. Mutation: drop the image leg
    and the first becomes a row; accept a non-common root and the second does.
    """
    write_zip(
        isolated_storage / "models-only.zip",
        {"part-a.stl": b"s" * 40, "part-b.stl": b"s" * 41, "part-c.stl": b"s" * 42},
    )
    write_zip(isolated_storage / "four-roots.zip", multi_root_entries())

    scan = bundle_service.scan_bundles(isolated_storage)
    rules = {item.rel_path: item.rule for item in scan.refusals}

    assert scan.candidates == []
    assert rules["models-only.zip"] == bundle_service.bundle_detect.REFUSAL_R3_TRIO_NO_IMAGE
    assert rules["four-roots.zip"] == bundle_service.bundle_detect.REFUSAL_R3_NO_OWN_DEPTH_MODEL


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
    assert row["detector"] == "thingiverse-id-name"
    assert row["confidence"] == "medium"
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

    No detector can produce a HIGH row with a creator and no licence (R1/R2 need
    both markers), so the seam that builds the row is narrowed here: the licence
    is dropped while the README creator, title and URL stay real.

    Mutation: let the record win over a truthy row value and the creator changes
    to P1lotz; keep the licence/licence_url pair apart and only one of the two
    reds.
    """
    moai_bundle(isolated_storage)
    real_facts = bundle_service._candidate_facts

    def licence_less(root: Path, candidate: bundle_service.BundleCandidate):
        facts = real_facts(root, candidate)
        if facts is None:
            return None
        return dataclasses.replace(facts, licence=None, licence_url=None)

    monkeypatch.setattr(bundle_service, "_candidate_facts", licence_less)
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
            "bundle_detect",
            "bundle_drift",
            "bundle_enrich",
            "dataclasses",
            "datetime",
            "db",
            "fastapi",
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
            # subprocess is behind `probe_git=`; `_engine_dissent` passes False, so a
            # report still runs no git and opens no socket. Adding the module here
            # without that call-site is what the behavioural half would catch.
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
    title = bundle_detect.display_name(folder.name)
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


def write_endorphin(storage: Path) -> Dict[str, str]:
    """The git checkout holding five DIFFERENT products - the AC-3 regression.

    Returns ``key -> detector`` for the rows it offers; R7 must not add, remove or
    rewrite a single one of them.
    """
    root = storage / "import" / ENDORPHIN
    for rel_path in ENDORPHIN_REPO_FILES:
        write_text(root / rel_path, "bundle exec jekyll build\n")
    expected: Dict[str, str] = {}
    for name in ENDORPHIN_OWN_ID_GROUPS:
        write_thingiverse_docs(root / name, int(name.rsplit("- ", 1)[1]))
        write_file(root / name / "files" / "part.stl", b"s" * 40)
        expected[f"import/{ENDORPHIN}/{name}"] = "thingiverse-readme"
    for name in ENDORPHIN_STAGE_GROUPS:
        write_file(root / "STLs" / name / "part.stl", b"s" * 40)
        expected[f"import/{ENDORPHIN}/STLs/{name}"] = "title-name"
    for name in ENDORPHIN_MODEL_FILES:
        generic_bundle(root, name)
        expected[f"import/{ENDORPHIN}/{name}"] = "generic-bundle"
    return expected


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
    assert row["detector"] == "compound-bundle"
    assert row["confidence"] == "low"
    assert row["metadata_shape"] == "compound"
    assert row["model_count"] == 60
    assert service_row(f"import/{STAGETOP}")["groups"] == [
        {"name": STAGETOP_WAVE_ONE, "model_count": 45, "foreign": False, "separable": False},
        {"name": STAGETOP_WAVE, "model_count": 15, "foreign": False, "separable": True},
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
    assert row["detector"] == "compound-bundle"
    assert row["confidence"] == "low"
    assert row["model_count"] == 48
    groups = service_row(f"import/{ROCKET}")["groups"]
    assert [group["name"] for group in groups] == list(ROCKET_GROUPS)
    assert [group["model_count"] for group in groups] == [6] * len(ROCKET_GROUPS)
    assert all(group["foreign"] is False for group in groups)

    ledger = refusal_ledger(report)
    for key in group_keys:
        assert ledger.get(key) == "member-of-bundle", key
    assert [item for item in report["bundles"] if item["key"] != f"import/{ROCKET}"] == []


def test_service_a_git_dump_keeps_its_separate_product_rows(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-3: a checkout holding several different products keeps every row it has.

    Two legs refuse ``import/endorphin`` twice over (its own repo markers, then two
    groups carrying their own thing id), and the ancestor rule refuses
    ``import/endorphin/STLs`` - the measured witness: its three ``stage-N``
    directories share the token ``stage`` and would otherwise be compounded,
    swallowing three title-name rows. Mutation: drop ``has_repo_markers`` and the
    checkout is STILL refused twice over by ``compound_bundle_facts`` on the two
    own-id groups (``5414731``, ``4591932``); what actually reds under that
    mutation is ``_inside_repository_dump``, because ``repo_dumps`` goes empty and
    ``import/endorphin/STLs`` is compounded and swallows the three title-name
    rows. Keep the marker test to the key only (no ancestor walk) and the same
    three rows are swallowed, so the ancestor leg is the one this test proves.
    """
    expected = write_endorphin(isolated_storage)
    scan()
    headers = auth_header(client)

    report = detect(client, headers, limit=1000)

    rows = {
        item["key"]: item["detector"]
        for item in report["bundles"]
        if item["key"].startswith(f"import/{ENDORPHIN}")
    }
    # The checkout's own directories R7 must not touch keep the detector they
    # had. `_includes`/`_layouts`/`_sass` are repo payload the rules refuse, and
    # since Mf-z643 they ride the MANUAL scope rather than vanishing.
    manual_dirs = {
        f"import/{ENDORPHIN}/{name}": "manual" for name in ("_includes", "_layouts", "_sass")
    }
    assert rows == {**expected, **manual_dirs}
    assert "import/endorphin/STLs" not in rows
    assert "compound-bundle" not in {item["detector"] for item in report["bundles"]}


def test_service_xbox360_shell_has_a_row_covering_its_own_models_and_naming_the_foreign_group(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC: the parent's 27 invisible models come back, and the foreign group is NAMED.

    Owner decision 2: ONE row with the foreign group flagged, never a split. The
    absorbed ``Wallmount ...`` row is HIGH and the compound row is LOW - that
    low-suppresses-high absorption is deliberate, and the ledger plus the group
    name are what keep it recoverable at create time (C2). Mutation: drop Leg B
    and the parent keeps no row at all, so ``row_for`` finds nothing and its 27
    models stay invisible.
    """
    write_xbox360(isolated_storage)
    scan()
    headers = auth_header(client)

    report = detect(client, headers, limit=1000)

    row = row_for(report, f"import/{XBOX}")
    assert row["detector"] == "compound-bundle"
    assert row["confidence"] == "low"
    assert row["model_count"] == XBOX_OWN_MODELS + XBOX_GROUP_MODELS
    assert service_row(f"import/{XBOX}")["groups"] == [
        {"name": XBOX_GROUP, "model_count": XBOX_GROUP_MODELS, "foreign": True, "separable": True}
    ]
    assert [item for item in report["bundles"] if item["key"].startswith(f"import/{XBOX}/")] == []

    ledger = refusal_ledger(report)
    assert ledger.get(f"import/{XBOX}/{XBOX_GROUP}") == "member-of-bundle"

    candidates, _truncated = bundle_service.iter_bundle_candidates(isolated_storage)
    compound = [candidate for candidate in candidates if candidate.key == f"import/{XBOX}"]
    assert len(compound) == 1
    own = bundle_service._own_paths(compound[0].key, compound[0])
    for index in range(XBOX_OWN_MODELS):
        assert f"import/{XBOX}/shell-{index}.stl" in own


def test_service_a_compound_row_creates_one_project_with_every_part(
    client: TestClient, isolated_storage: Path
) -> None:
    """A compound row is creatable on the same terms as any other folder row.

    The folder resolves, so the row is ``ready`` like every other row, and one
    create makes ONE project attaching all 31 models: the 27 of the product's own
    and the foreign group's 4, because ``_own_paths`` is the whole subtree rather
    than a claimed-children view. The ``contains_bundles`` belt cannot fire here -
    ``own_assets == assets`` - and splitting the foreign group is C2's job
    (``Mf-ao6r``), not this slice's. Mutation: give the compound candidate a
    closed snapshot instead of the whole subtree and the 4 foreign models drop out
    of the create, or the belt reds and nothing is created.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    scan()
    headers = auth_header(client)

    created = create(client, headers, [{"key": f"import/{XBOX}"}])

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

    assert [entry.rel_path for entry in member_set.by_role(bundle_detect.MODEL)] == [
        "part-a.stl",
        "part-b.stl",
    ], "the MODEL view - what `attached` and `Dedupe.containment` are asked with - is the two STLs"
    assert {entry.rel_path: entry.role for entry in member_set.entries}[
        "part-a.stlbak"
    ] == bundle_detect.OTHER
    assert {entry.rel_path: entry.role for entry in member_set.entries}[
        "docs.pdf"
    ] == bundle_detect.TEXT
    assert registered_model_paths("Backup Kit") == {"Backup Kit/part-a.stl", "Backup Kit/part-b.stl"}


def test_attaching_backups_creates_no_group_members(
    client: TestClient, isolated_storage: Path
) -> None:
    """A compound create writes every file, but only MODELS count as a group's `attached`.

    The probe is one product in two parts: two family-named groups (``Bracket Set
    Alpha``, ``Bracket Set Beta``), three STLs spread across them, four ``.stlbak``
    backups and one root PDF - eight registered files, three models. Creating the row
    as ``compound`` must hold all eight in the project while the marker's per-group
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

    assert (row["detector"], row["metadata_shape"]) == ("compound-bundle", "compound")
    assert row["model_count"] == 3, "three STLs - the four backups are `other`, the PDF is `text`"
    assert row["assets_total"] == 8

    created = create(client, headers, [{"key": key, "shape": "compound"}])

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
        {"name": "Bracket Set Alpha", "model_count": 2, "foreign": False, "included": True, "attached": 2},
        {"name": "Bracket Set Beta", "model_count": 1, "foreign": False, "included": True, "attached": 1},
    ], (
        "`attached` is the group's MODEL count: the four backups and the PDF created no "
        "group members, even though all eight files are in the project"
    )
    assert sum(group["attached"] for group in marker["groups"]) == row["model_count"] == 3


def test_service_a_folder_with_one_model_bearing_group_is_not_compound(
    client: TestClient, isolated_storage: Path
) -> None:
    """A wrapper holding ONE model-bearing group stays exactly as it is today.

    One group is a plain wrapper, not "several parts", so R7 must not fire. The
    wrapper is named in lowercase on purpose: a title-shaped wrapper with one
    model-bearing child is already offered by R5, which is what the
    ``root_model_count`` leg inside R7 exists to avoid copying. Mutation: drop
    the two-group floor and this wrapper gains a compound row beside its child.
    """
    write_group(isolated_storage / "import" / "wrapper" / "one product", 2)
    scan()
    headers = auth_header(client)

    report = detect(client, headers, limit=1000)

    assert row_for(report, "import/wrapper/one product")["detector"] == "generic-bundle"
    assert [item for item in report["bundles"] if item["key"] == "import/wrapper"] == []


def test_service_a_bundle_that_is_already_offered_is_never_compounded(
    client: TestClient, isolated_storage: Path
) -> None:
    """The overlap guard: a directory any rule already offered is never compounded.

    The family here answers every predicate R7 has - two groups sharing the token
    ``mount``, no models at the parent's own depth - but the parent is a HIGH
    ``thingiverse-readme`` row already, so the post-pass must skip it and leave it
    byte-for-byte as it was. Mutation: drop the ``key in offered`` guard and the
    high row becomes a LOW compound row, i.e. a rewrite of a verdict no rule
    disputed.
    """
    family = isolated_storage / "import" / "Spool Holder Family"
    write_thingiverse_docs(family, 777777)
    for group in ROCKET_GROUPS[:2]:
        write_file(family / group / "part.stl", b"s" * 40)
    scan()
    headers = auth_header(client)

    report = detect(client, headers, limit=1000)

    assert len(report["bundles"]) == 1
    row = row_for(report, "import/Spool Holder Family")
    assert row["detector"] == "thingiverse-readme"
    assert row["confidence"] == "high"
    assert row["model_count"] == 2
    assert service_row("import/Spool Holder Family")["groups"] == []
    assert "compound-bundle" not in {item["detector"] for item in report["bundles"]}


def test_service_an_offered_low_folder_is_never_recompounded(
    client: TestClient, isolated_storage: Path
) -> None:
    """The overlap guard on the row it is the ONLY protection for: an offered LOW one.

    ``import/Rig`` is offered at LOW by R3 (five own models, an image and a text)
    and is ALSO Leg B's shape: every group below it is foreign, the two foreign
    groups hold four models against the folder's five, and one carries a vendor
    id. The ``thingiverse-readme`` parent in the test above is refused by
    ``_compound_absorbs`` for a different reason, so this fixture is the witness
    for the offered-key guard itself. Mutation: delete ``key in offered`` from
    ``_compound_pass`` and the LOW ``generic-bundle`` row is rewritten into a LOW
    ``compound-bundle`` row, i.e. a verdict no rule disputed is replaced.
    """
    rig = isolated_storage / "import" / "Rig"
    for index in range(5):
        write_file(rig / f"shell-{index}.stl", b"s" * 40)
    write_file(rig / "cover.jpg", b"j" * 40)
    write_text(rig / "notes.txt", "print at 0.2mm\n")
    for index in range(2):
        write_file(rig / "Gizmo Mount Kit - 5461913" / "files" / f"part-{index}.stl", b"s" * 40)
        write_file(rig / "Other Vendor" / "files" / f"part-{index}.stl", b"s" * 40)
    write_text(rig / "Other Vendor" / "README.txt", "print at 0.2mm\n")
    scan()
    headers = auth_header(client)

    report = detect(client, headers, limit=1000)

    assert {item["key"] for item in report["bundles"]} == {"import/Rig"}
    row = row_for(report, "import/Rig")
    assert row["detector"] == "generic-bundle"
    assert row["metadata_shape"] == "generic"
    assert "compound-bundle" not in {item["detector"] for item in report["bundles"]}
    # Both group directories already sit below an OFFERED row, so the existing
    # reconciliation names them ``member-of-bundle``; the guard must not change
    # that, and the post-pass must add no row of its own.
    ledger = refusal_ledger(report)
    for key in ("import/Rig/Gizmo Mount Kit - 5461913", "import/Rig/Other Vendor"):
        assert ledger.get(key) == "member-of-bundle", key


def test_service_a_leg_a_family_never_absorbs_a_high_row(
    client: TestClient, isolated_storage: Path
) -> None:
    """Two whole HIGH exports sharing a word are a pile, not one product in parts.

    ``vendor/`` holds two real Thingiverse exports of one model each, so both are
    ``thingiverse-readme``/``high``, and nothing is offered at ``vendor``'s own
    depth. Leg A fires on the shared token ``bundle``. Mutation: delete the
    STRONG-row half of ``_compound_absorbs`` and the two undisputed HIGH verdicts
    collapse into one LOW row: the rule would rewrite verdicts no rule disputed.

    The second case is the witness for WHICH count the bound asks. ``vendor2``
    holds ``Kits Alpha`` (models at depth 1) and ``Kits Inner`` (a real
    Thingiverse marker pair two levels down, so a ``thingiverse-readme``/``high``
    row). The two names share exactly one token, ``kits``, so Leg A fires on
    ``vendor2`` and the absorbed set holds that HIGH row. ``root_model_count``
    answers 0 and refuses it; ``_own_depth_model_count`` would answer 4 from its
    single-unnamed-subdirectory leg - it sees only ``Kits Alpha``'s depth-1
    models - and would let the row be captured. This is the shape ADR
    superseding-condition 1 forbids: a Leg A family must never absorb a HIGH row.
    ``Kits Alpha`` is itself offered as a LOW ``title-name`` row, so the bound has
    to refuse a mixed set rather than a set of HIGH rows alone.
    """
    vendor = isolated_storage / "vendor"
    thingiverse_bundle(vendor, "Alpha Bundle", 111111)
    thingiverse_bundle(vendor, "Beta Bundle", 222222)
    kits = isolated_storage / "vendor2"
    for index in range(4):
        write_file(kits / "Kits Alpha" / f"part-{index}.stl", STL_BYTES)
    write_thingiverse_docs(kits / "Kits Inner" / "inner", 333333)
    for index in range(4):
        write_file(kits / "Kits Inner" / "inner" / f"part-{index}.stl", STL_BYTES)
    scan()
    headers = auth_header(client)

    report = detect(client, headers, limit=1000)

    high_keys = {"vendor/Alpha Bundle", "vendor/Beta Bundle", "vendor2/Kits Inner/inner"}
    assert {item["key"] for item in report["bundles"]} == high_keys | {"vendor2/Kits Alpha"}
    for key in high_keys:
        row = row_for(report, key)
        assert row["detector"] == "thingiverse-readme", key
        assert row["confidence"] == "high", key
    assert "compound-bundle" not in {item["detector"] for item in report["bundles"]}
    for parent in ("vendor", "vendor2"):
        assert [item for item in report["bundles"] if item["key"] == parent] == [], parent


def test_service_the_switch_off_restores_every_absorbed_row(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turning R7 off gives every absorbed row back, and only R7 moves them.

    ``COMPOUND_BUNDLES_ENABLED`` is read inside ``compound_bundle_facts`` and
    nowhere else, so one switch is the whole difference between one row and eleven.
    Mutation: read the switch in the service instead of the rule, or cache facts,
    and the two reports stop moving together.
    """
    child_keys = write_stagetop(isolated_storage)
    scan()
    headers = auth_header(client)

    with monkeypatch.context() as patched:
        patched.setattr(bundle_detect, "COMPOUND_BUNDLES_ENABLED", False)
        without = detect(client, headers, limit=1000)

    with_rule_on = detect(client, headers, limit=1000)

    assert {item["key"] for item in without["bundles"]} == set(child_keys)
    assert f"import/{STAGETOP}" not in {item["key"] for item in without["bundles"]}
    assert [item["key"] for item in with_rule_on["bundles"]] == [f"import/{STAGETOP}"]



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
            if bundle_service._suffix(asset.path) in bundle_detect.MODEL_SUFFIXES
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
    """Create the xbox compound row, optionally excluding its foreign group."""
    item: dict = {"key": f"import/{XBOX}"}
    if exclude is not None:
        item["exclude_groups"] = exclude
    return create(client, headers, [item])


# A vendor id in the name -> foreign; its models sit in two unnamed subdirectories,
# so nothing at its own depth -> R5 (the own-depth guard) still refuses it a row.
A_LONE_GROUP = "grip-5461913"
A_COMPOUND = "toolkit"


def write_foreign_group_never_a_row(storage: Path) -> str:
    """A Leg B compound whose ONE foreign group was never a row of its own.

    R9's case, verbatim: a group is ``foreign`` off its own README/name, and it
    MAY NEVER HAVE BEEN A ROW AT ALL. This group's name carries a vendor id, so
    ``compound_groups`` calls it foreign; it holds four models in two unnamed
    subdirectories and NOTHING else, so R1 refuses it (no README/LICENSE), R3
    refuses it (no image, no text) and R5 refuses it (no own-depth model).
    It is therefore never a candidate, never absorbed, and reads
    ``separable: false`` - exactly the group an operator must not be allowed to
    exclude, because its parts would land in no project and in no row. The parent
    has no image and no text either, so R3/R5 refuse it and only Leg B fires (own
    models 9 > the group's 4, every group foreign, a vendor id present).
    """
    root = storage / "import" / A_COMPOUND
    for index in range(9):
        write_file(root / f"shell-{index}.stl", b"s" * 40)
    for index in range(2):
        write_file(root / A_LONE_GROUP / "plates" / f"bracket-{index}.stl", b"s" * 40)
    for index in range(2, 4):
        write_file(root / A_LONE_GROUP / "rails" / f"bracket-{index}.stl", b"s" * 40)
    return f"import/{A_COMPOUND}"


def test_service_creating_from_a_compound_row_makes_one_project_with_every_part(
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
    assert marker["detector"] == "compound-bundle"
    assert marker["shape"] == "compound"
    assert marker["groups"] == [
        {"name": STAGETOP_WAVE_ONE, "model_count": 45, "foreign": False, "included": True, "attached": 45},
        {"name": STAGETOP_WAVE, "model_count": 15, "foreign": False, "included": True, "attached": 15},
    ]
    # Re-expressed, not deleted (Mf-wsdo): `attached` is the MODEL question per group
    # and `assets_added` is the ATTACH answer, so the two are no longer one number. The
    # identity that survives is "the groups' attached counts sum to the MODELS the
    # project holds", which is what the marker claims and what an image leak (a group
    # counted as attaching a preview) still reds on.
    attached_models = [
        path
        for path in project_storage_paths(result["project_id"])
        if bundle_service._suffix(path) in bundle_detect.MODEL_SUFFIXES
    ]
    assert sum(group["attached"] for group in marker["groups"]) == len(attached_models) == 60, (
        "the compound marker's per-group `attached` is the MODEL question and stays "
        "models-only, whatever the project holds"
    )
    assert marker["cover"]["reason"] == "largest-image"
    assert project.cover_asset_id == row["cover"]["asset_id"] == marker["cover"]["asset_id"]
    assert marker["cover"]["group"] == STAGETOP_WAVE_ONE


def test_the_row_counts_exactly_what_the_scan_registered(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-2: the compound row and the project it creates agree on the Rocket's 48.

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
    """Unexcluded, the compound row attaches the 27 own parts AND the foreign group's 4.

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

    One create of the compound row with the mount excluded attaches the 27 own
    parts and nothing from the mount; the next report's compound row is unchanged
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
    # a detection row (Mf-z643) and the key is re-evaluated over its whole subtree
    # as a title row. The groups belong to the created marker now; the compound
    # match is still offered (`matches`), so a later create can choose it again.
    assert after["groups"] == []
    assert after["detector"] == "title-name"
    assert "compound" in [match["shape"] for match in after["matches"]]
    assert after["cover"]["asset_id"] == before["cover"]["asset_id"]


def test_the_excluded_group_is_inside_the_project_key_and_is_not_a_row(
    client: TestClient, isolated_storage: Path
) -> None:
    """A descendant of a created project is not a detection row, at either scope.

    This used to be the C2 re-offer witness: the absorbed mount came back as a
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
    assert row["detector"] == "title-name"
    assert row["existing_project_match"] == "marker"
    assert mount_key() not in refusal_ledger(report)

    walked = bundle_service.scan_bundles(isolated_storage, excluded_keys={"import/xbox360_shell"})
    # A manual row keeps its refusing word in the ledger (it is the row's `note`).
    assert [(item.rel_path, item.rule) for item in walked.refusals] == [("import", "R3:trio-no-image")]
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


def test_a_dismissed_re_offered_group_is_not_stale_and_a_vanished_key_is(
    client: TestClient, isolated_storage: Path
) -> None:
    """A dismissal of a LIVE key is not stale; a key naming nothing is.

    The former C2 re-offer witness used the excluded mount, which is now inside the
    project key and therefore no longer a row - so the live key here is the
    project's own key row (Mf-z643). The point is the same one: `live_keys` must
    name everything the report could still show, or a hidden row reads stale.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    insert_folder_row(XBOX_GROUP, mount_key())
    scan()
    headers = auth_header(client)
    assert create_for_exclusion(client, headers, [XBOX_GROUP])["created"] == 1

    dismiss(client, headers, [f"import/{XBOX}"])
    report = detect(client, headers, limit=1000)

    assert f"import/{XBOX}" not in report["stale_dismissals"], "a live row is never stale"
    assert f"import/{XBOX}" not in {row["key"] for row in report["bundles"]}
    assert report["counts"]["dismissed"] == 1
    # The mount is inside the project key, so dismissing it names a key that can
    # never be live again - the correct stale answer.
    dismiss(client, headers, [mount_key()])
    report = detect(client, headers, limit=1000)
    assert mount_key() in report["stale_dismissals"]

    dismiss(client, headers, ["ghost/dismissed"])
    report = detect(client, headers, limit=1000)
    assert "ghost/dismissed" in report["stale_dismissals"]
    assert f"import/{XBOX}" not in report["stale_dismissals"]


def model_asset_ids(rel_dir: str) -> List[str]:
    """The ids of every registered MODEL asset under ``rel_dir``, in path order."""
    with Session(engine) as session:
        assets = session.exec(select(Asset).where(Asset.storage_path.is_not(None)).order_by(Asset.storage_path))
        return [
            asset.id
            for asset in assets
            if asset.storage_path.startswith(rel_dir + "/")
            and bundle_service._suffix(asset.storage_path) in bundle_detect.MODEL_SUFFIXES
        ]


def test_a_re_offered_group_creates_its_own_project_and_only_when_re_offered(
    client: TestClient, isolated_storage: Path
) -> None:
    """A descendant of a created project is never a row, with or without a marker.

    This used to be the C2 re-offer witness. Mf-z643 makes the exclusion a
    CREATE-time decision: once `import/xbox360_shell` is a project key, the mount
    inside it stops being a detection row for both doors, so the marker is the
    only record of what was excluded. (a) Before anything was created the mount
    is not a row. (b) After the exclusion is recorded it is STILL not a row and
    the create answers ``unknown bundle`` - the exclusion lives in the marker.
    (c) A second, exclusion-free tree behaves identically for its own group.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    insert_folder_row(XBOX_GROUP, mount_key())
    scan()
    headers = auth_header(client)

    before_any = create(client, headers, [{"key": mount_key()}])
    assert before_any["failed"] == 1
    assert before_any["results"][0]["reason"] == "unknown bundle"
    assert table_counts()["projects"] == 0

    first = create_for_exclusion(client, headers, [XBOX_GROUP])
    assert first["created"] == 1
    marker = project_marker(first["results"][0]["project_id"])
    assert marker["groups"] == [
        {
            "name": XBOX_GROUP,
            "model_count": XBOX_GROUP_MODELS,
            "foreign": True,
            "included": False,
            "attached": 0,
        }
    ]

    split = create(client, headers, [{"key": mount_key()}])
    assert split["failed"] == 1
    assert split["results"][0]["reason"] == "unknown bundle"
    assert table_counts()["projects"] == 1

    copy_key = f"import/{XBOX}-copy"
    shutil.copytree(isolated_storage / "import" / XBOX, isolated_storage / "import" / f"{XBOX}-copy")
    insert_folder_row(f"{XBOX}-copy", copy_key)
    scan()
    projects_before = table_counts()["projects"]
    assert create(client, headers, [{"key": copy_key, "name": "copy of the shell"}])["created"] == 1
    unstripped = create(client, headers, [{"key": f"{copy_key}/{XBOX_GROUP}"}])
    assert unstripped["failed"] == 1
    assert unstripped["results"][0]["reason"] == "unknown bundle"
    assert table_counts()["projects"] == projects_before + 1


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


def test_a_second_create_of_a_compound_row_is_already_created(
    client: TestClient, isolated_storage: Path
) -> None:
    """A compound create is idempotent, and a later exclusion never rewrites the marker.

    The second create is a marker hit; the third sends a DIFFERENT exclusion and
    still answers ``already_created`` with the project byte-identical, because
    the marker is proof and the row is never touched again. Mutation: skip the
    existing-project check for a compound row and the project gets a second
    attach pass or a rewritten marker.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    scan()
    headers = auth_header(client)

    first = create(client, headers, [{"key": f"import/{XBOX}", "name": "shell only"}])
    assert first["created"] == 1
    project_id = first["results"][0]["project_id"]
    with Session(engine) as session:
        metadata_before = session.get(Project, project_id).metadata_json

    second = create(client, headers, [{"key": f"import/{XBOX}"}])

    assert second["skipped"] == 1
    assert second["results"][0]["reason"] == "already_created"
    row = row_for(detect(client, headers, limit=1000), f"import/{XBOX}")
    assert row["existing_project_match"] == "marker"

    third = create_for_exclusion(client, headers, [XBOX_GROUP])

    # The report's row for the key is re-evaluated over its pruned subtree as a
    # title row, so the compound match - and its groups - is no longer the
    # RECOMMENDED shape. An exclusion naming a group the chosen shape does not
    # carry is a malformed request, never a silent rewrite of the marker.
    assert third["failed"] == 1
    assert third["results"][0]["reason"] == "unknown_group"
    with Session(engine) as session:
        assert session.get(Project, project_id).metadata_json == metadata_before
    assert table_counts()["projects"] == 1
    assert member_count(project_id) == XBOX_MEMBERS, (
        "the skip attached nothing a second time; the project still holds every "
        "registered file the first create gave it (Mf-7oow)"
    )

    # The compound shape is still on offer (`matches`), and choosing it explicitly
    # validates the exclusion again. Post-create the group has no absorbed row
    # left (its subtree is the project's), so the exclusion cannot be honoured -
    # a failed request, never a silent rewrite of the marker.
    compound_retry = bundle_service.create_bundles(
        [bundle_service.BundleCreateItem(key=f"import/{XBOX}", shape="compound", exclude_groups=(XBOX_GROUP,))],
        audit_ctx(),
    )
    assert compound_retry["failed"] == 1
    assert compound_retry["results"][0]["reason"] == "not_separable_group"


def test_a_second_create_of_the_split_group_is_unknown_bundle(
    client: TestClient, isolated_storage: Path
) -> None:
    """A descendant of a project key is never a row, so it can never be created.

    This used to witness the split row's idempotency. Since Mf-z643 the mount sits
    inside the created project's key and is pruned before any pass, so both the
    first and the second create answer ``unknown bundle`` and only the compound
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


def test_the_compound_marker_names_the_groups_and_their_counts(
    client: TestClient, isolated_storage: Path
) -> None:
    """The marker's group list is the row's, in order, plus what was attached.

    Since Mf-94od `model_count` is what the SCAN registers, and `attached` is the
    same registered set filtered by the exclusion, so an unexcluded compound
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
    created = create(client, headers, [{"key": key, "name": f"{name} parts"} for name, key in keys.items()])

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
        if bundle_service._suffix(path) in bundle_detect.MODEL_SUFFIXES
    ]
    assert len(xbox_models) - sum(
        group["attached"] for group in xbox_marker["groups"] if group["included"]
    ) == XBOX_OWN_MODELS


def test_a_non_compound_marker_is_unchanged_in_both_directions(
    client: TestClient, isolated_storage: Path
) -> None:
    """R7's marker additions are additive-only: six keys ordinary, nine compound.

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
            {"key": f"import/{XBOX}", "name": "compound thing", "exclude_groups": [XBOX_GROUP]},
        ],
    )
    assert created["created"] == 2

    ordinary = project_marker(created["results"][0]["project_id"])
    compound = project_marker(created["results"][1]["project_id"])
    # Every marker now records the shape it was made from and the rule that offered
    # it (Mf-z643), so the six keys became eight; the compound adds its groups and
    # cover, and an ordinary row still does not.
    eight = {"key", "detector", "confidence", "rel_dir", "archive", "twin_verified", "origin", "shape", "rule"}
    assert set(ordinary) - {"enrichment"} == eight
    assert (ordinary["shape"], ordinary["rule"]) == ("root-docs", "thingiverse-readme")
    assert set(compound) - {"enrichment"} == eight | {"groups", "cover"}
    assert (compound["shape"], compound["rule"]) == ("compound", "compound-bundle")


def test_an_exclusion_of_a_group_that_was_never_a_row_is_refused(
    client: TestClient, isolated_storage: Path
) -> None:
    """R9: a FOREIGN group with no absorbed row behind it has nowhere to go.

    The toolkit's group is foreign off its own name and holds four parts, but it
    was never a row of its own - R1, R3 and R5 each refuse it - so creating the
    compound without it would leave its parts in no project and in no row, the
    ticket's vanish. Mutation: treat every group as separable and the refusal
    becomes a project holding 9 of its 13 parts.
    """
    key = write_foreign_group_never_a_row(isolated_storage)
    insert_folder_row(A_COMPOUND, key)
    scan()
    headers = auth_header(client)

    row = service_row(key)
    assert row["groups"] == [{"name": A_LONE_GROUP, "model_count": 4, "foreign": True, "separable": False}]

    refused = create(client, headers, [{"key": key, "exclude_groups": [A_LONE_GROUP]}])

    assert refused["failed"] == 1
    assert refused["results"][0]["reason"] == "not_separable_group"
    assert table_counts()["projects"] == 0

    created = create(client, headers, [{"key": key}])
    assert created["created"] == 1
    held = project_storage_paths(created["results"][0]["project_id"])
    assert held == registered_model_paths(key)
    assert f"import/{A_COMPOUND}/{A_LONE_GROUP}/plates/bracket-0.stl" in held


def test_the_three_exclusion_refusals(client: TestClient, isolated_storage: Path) -> None:
    """Every unusable exclusion is a `failed` with its own reason, and creates nothing.

    ``unknown_group`` covers a name no group carries and a differently-cased
    spelling of one that is there; ``not_a_foreign_group`` covers a group of the
    product's own, witnessed by both Stagetop groups (the absorbed edition and
    the never-absorbed one); ``not_separable_group`` covers a FOREIGN group that
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

    assert reason([{"key": f"import/{XBOX}", "exclude_groups": ["No Such Group"]}]) == "unknown_group"
    assert reason([{"key": f"import/{XBOX}", "exclude_groups": [XBOX_GROUP.casefold()]}]) == "unknown_group"
    assert reason([{"key": f"import/{STAGETOP}", "exclude_groups": [STAGETOP_WAVE]}]) == "not_a_foreign_group"
    assert reason([{"key": f"import/{STAGETOP}", "exclude_groups": [STAGETOP_WAVE_ONE]}]) == "not_a_foreign_group"
    assert reason([{"key": f"import/{A_COMPOUND}", "exclude_groups": [A_LONE_GROUP]}]) == "not_separable_group"
    assert table_counts()["projects"] == 0, "a refused exclusion creates nothing"

    plain = create(client, headers, [{"key": MOAI, "exclude_groups": []}])
    assert plain["created"] == 1, "an empty list is a no-op on a non-compound row too"

    duplicated = create(client, headers, [{"key": f"import/{XBOX}", "exclude_groups": [XBOX_GROUP, XBOX_GROUP]}])
    assert duplicated["created"] == 1
    assert duplicated["results"][0]["assets_added"] == XBOX_OWN_MODELS
    assert project_storage_paths(duplicated["results"][0]["project_id"]) == {
        f"import/{XBOX}/shell-{index}.stl" for index in range(XBOX_OWN_MODELS)
    }

    bogus = create(client, headers, [{"key": f"import/{XBOX}", "exclude_groups": ["No Such Group"]}])
    assert bogus["failed"] == 1
    assert bogus["results"][0]["reason"] == "unknown_group", "validation runs before the marker check"

    snapshot = bundle_detect.BundleSnapshot(
        bundle_name="Inbox",
        rel_dir="Inbox",
        entries=(
            bundle_detect.BundleEntry(rel_path="part-0.stl", size=len(STL_BYTES)),
            bundle_detect.BundleEntry(rel_path="foreign.stl", size=len(STL_BYTES)),
        ),
    )
    candidate = bundle_service.BundleCandidate(
        key="Inbox", snapshot=snapshot, archive_path=None, is_archive=False, confidence="low"
    )
    hand_row = {
        "key": "Inbox",
        "rel_path": "Inbox",
        "name_suggestion": "Inbox",
        "creator": None,
        "licence": None,
        "licence_url": None,
        "source_url": None,
        "description_excerpt": None,
        "detector": "generic-bundle",
        "confidence": "low",
        "cover": {"asset_id": None, "rel_path": None, "reason": "none", "url": None, "thumb_url": None},
        "status": "ready",
        "blocked_reason": None,
        "archive": None,
        "groups": [{"name": "foreign.stl", "model_count": 1, "foreign": True, "separable": True}],
    }

    named_like_a_file = bundle_service._create_one(
        bundle_service.BundleCreateItem(key="Inbox", name_only=True, exclude_groups=("foreign.stl",)),
        hand_row,
        candidate,
        audit_ctx(),
    )

    assert named_like_a_file["status"] == "created"
    assert named_like_a_file["assets_added"] == 2, "the file `Inbox/foreign.stl` is not the group"
    assert project_storage_paths(named_like_a_file["project_id"]) == {"Inbox/part-0.stl", "Inbox/foreign.stl"}


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
            {"key": f"import/{XBOX}", "exclude_groups": [XBOX_GROUP]},
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
            {"key": f"import/{XBOX}", "name": "excluded shell", "exclude_groups": [XBOX_GROUP]},
            {"key": f"import/{XBOX}-copy", "name": "whole copy"},
            {
                "key": f"import/{XBOX}-copy-2",
                "name": "twice-excluded copy",
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

    created = create(client, headers, [{"key": f"import/{XBOX}"}])

    assert created["failed"] == 1
    assert created["results"][0]["reason"] == "contains_bundles"
    assert table_counts()["projects"] == 0


def test_the_cover_never_picks_an_unregistered_image_under_an_excluded_group(
    client: TestClient, isolated_storage: Path
) -> None:
    """An excluded group's image cannot become the project's face.

    The row's cover names an unregistered image under the group; once that group is
    excluded the facts' ``cover_entry`` is dropped and the same ``_cover`` rule is
    re-asked over the included parts, so the project's own model wins. Mutation:
    keep ``facts.cover_entry`` and the project's cover is a rel_path under
    ``Inbox/foreign/`` with no asset behind it.
    """
    write_file(isolated_storage / "Inbox" / "part-0.stl", STL_BYTES)
    scan()
    snapshot = bundle_detect.BundleSnapshot(
        bundle_name="Inbox",
        rel_dir="Inbox",
        entries=(
            bundle_detect.BundleEntry(rel_path="part-0.stl", size=len(STL_BYTES)),
            bundle_detect.BundleEntry(rel_path="notes.txt", size=13),
            bundle_detect.BundleEntry(rel_path="foreign/cover.jpg", size=900),
        ),
    )
    candidate = bundle_service.BundleCandidate(
        key="Inbox", snapshot=snapshot, archive_path=None, is_archive=False, confidence="low"
    )
    row = {
        "key": "Inbox",
        "rel_path": "Inbox",
        "name_suggestion": "Inbox",
        "creator": None,
        "licence": None,
        "licence_url": None,
        "source_url": None,
        "description_excerpt": None,
        "detector": "generic-bundle",
        "confidence": "low",
        "cover": {
            "asset_id": None,
            "rel_path": "Inbox/foreign/cover.jpg",
            "reason": "largest-image-unregistered",
            "url": None,
            "thumb_url": None,
        },
        "status": "ready",
        "blocked_reason": None,
        "archive": None,
        "groups": [{"name": "foreign", "model_count": 1, "foreign": True, "separable": True}],
    }

    result = bundle_service._create_one(
        bundle_service.BundleCreateItem(key="Inbox", name_only=True, exclude_groups=("foreign",)),
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

    ``test_the_cover_never_picks_an_unregistered_image_under_an_excluded_group`` next door
    covers the unregistered leg. This is the leg Mf-wsdo makes live: an image the scan
    REGISTERED now sits in the attach table, so an excluded group's cover image is now a
    file the cover rule is willing to pick - and the only thing standing between it and
    the project's face is the exclusion filter on the re-asked set. ``owned_assets`` is
    the own-and-claimed set MINUS the excluded group's prefixes, so it is a subset of what
    the report's row was allowed to advertise, and the project's own model wins again
    (``model-preview``).

    The exclusion is asserted twice over, because the belt guard next to it now punishes
    the mistake instead of hiding it: the image is registered, it is NOT a member, and the
    cover is neither. Mutation (run, then restored): re-ask over ``own_assets`` (or
    ``assets``) instead of ``owned_assets`` and the foreign ``cover.jpg`` wins the cover -
    which is no longer a silently dangling cover, because
    ``project_create_service.create_project_from_members`` refuses a cover outside the
    member set, so the create answers ``failed`` here and ``status == "created"`` reds.
    """
    write_file(isolated_storage / "Inbox" / "part-0.stl", STL_BYTES)
    write_file(isolated_storage / "Inbox" / "foreign" / "cover.jpg", b"j" * 900)
    scan()
    snapshot = bundle_detect.BundleSnapshot(
        bundle_name="Inbox",
        rel_dir="Inbox",
        entries=(
            bundle_detect.BundleEntry(rel_path="part-0.stl", size=len(STL_BYTES)),
            bundle_detect.BundleEntry(rel_path="notes.txt", size=13),
            bundle_detect.BundleEntry(rel_path="foreign/cover.jpg", size=900),
        ),
    )
    candidate = bundle_service.BundleCandidate(
        key="Inbox", snapshot=snapshot, archive_path=None, is_archive=False, confidence="low"
    )
    image_id = asset_id_at("Inbox/foreign/cover.jpg")
    row = {
        "key": "Inbox",
        "rel_path": "Inbox",
        "name_suggestion": "Inbox",
        "creator": None,
        "licence": None,
        "licence_url": None,
        "source_url": None,
        "description_excerpt": None,
        "detector": "generic-bundle",
        "confidence": "low",
        # The report's row, BEFORE the exclusion: the registered preview image is the
        # cover, and it is the file the operator is about to exclude.
        "cover": {
            "asset_id": image_id,
            "rel_path": "Inbox/foreign/cover.jpg",
            "reason": "largest-image",
            "url": None,
            "thumb_url": None,
        },
        "status": "ready",
        "blocked_reason": None,
        "archive": None,
        "groups": [{"name": "foreign", "model_count": 0, "foreign": True, "separable": True}],
    }

    result = bundle_service._create_one(
        bundle_service.BundleCreateItem(key="Inbox", name_only=True, exclude_groups=("foreign",)),
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
    ``largest-image-unregistered`` leg names a BUNDLE-relative snapshot entry.
    ``_create_one`` settles the one storage-relative form before the marker is
    built, so the same field is never recorded in two spaces. Mutation: compare
    the bundle-relative entry against the storage-relative prefix (the
    pre-normalisation code) and ``cover["group"]`` reads None.
    """
    write_file(isolated_storage / "Inbox" / "part-0.stl", STL_BYTES)
    scan()
    # The second vendor-id group and the extra root models are what make this a
    # GENUINE compound snapshot: one README-bearing group named `gear` cannot meet
    # R7's Leg A two-group floor or Leg B's vendor-id requirement on its own, and
    # then `_candidate_facts` would return None instead of the unregistered leg.
    snapshot = bundle_detect.BundleSnapshot(
        bundle_name="Inbox",
        rel_dir="Inbox",
        entries=(
            bundle_detect.BundleEntry(rel_path="part-0.stl", size=len(STL_BYTES)),
            bundle_detect.BundleEntry(rel_path="part-1.stl", size=len(STL_BYTES)),
            bundle_detect.BundleEntry(rel_path="part-2.stl", size=len(STL_BYTES)),
            bundle_detect.BundleEntry(rel_path="notes.txt", size=13),
            bundle_detect.BundleEntry(rel_path="gear/cover.jpg", size=900),
            bundle_detect.BundleEntry(rel_path="gear/bracket.stl", size=40),
            bundle_detect.BundleEntry(rel_path="gear/README.txt", size=10),
            bundle_detect.BundleEntry(rel_path="grip-5461913/grip.stl", size=40),
        ),
    )
    candidate = bundle_service.BundleCandidate(
        key="Inbox",
        snapshot=snapshot,
        archive_path=None,
        is_archive=False,
        confidence="low",
        compound=True,
    )
    facts = bundle_service._candidate_facts(isolated_storage, candidate)
    assert facts is not None and facts.cover_entry == "gear/cover.jpg"

    row = {
        "key": "Inbox",
        "rel_path": "Inbox",
        "name_suggestion": "Inbox",
        "creator": None,
        "licence": None,
        "licence_url": None,
        "source_url": None,
        "description_excerpt": None,
        "detector": "compound-bundle",
        "confidence": "low",
        "cover": {
            "asset_id": None,
            "rel_path": "gear/cover.jpg",
            "reason": "largest-image-unregistered",
            "url": None,
            "thumb_url": None,
        },
        "status": "ready",
        "blocked_reason": None,
        "archive": None,
        "groups": [{"name": "gear", "model_count": 1, "foreign": True, "separable": True}],
    }

    result = bundle_service._create_one(
        bundle_service.BundleCreateItem(key="Inbox", name_only=True),
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


def test_a_pre_c2_marker_re_offers_nothing(client: TestClient, isolated_storage: Path) -> None:
    """A marker written before C2 carries no ``groups``, so it re-offers no part.

    The six-key marker is exactly what C1's create wrote. Absence of ``groups``
    means nothing was ever excluded, so the absorbed mount stays a
    ``member-of-bundle`` and is not creatable. Mutation: treat an absent `groups`
    as "everything was excluded" (or default `included` to False) and the mount is
    re-offered from a marker that predates C2.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    insert_folder_row(XBOX_GROUP, mount_key())
    scan()
    headers = auth_header(client)
    with Session(engine) as session:
        project_service.create_project(
            session,
            "pre-c2 shell",
            None,
            None,
            origin="imported",
            metadata={
                "_bundle": {
                    "key": f"import/{XBOX}",
                    "detector": "compound-bundle",
                    "confidence": "low",
                    "rel_dir": f"import/{XBOX}",
                    "archive": None,
                    "twin_verified": None,
                }
            },
        )

    report = detect(client, headers, limit=1000)

    assert [row["key"] for row in report["bundles"]] == [f"import/{XBOX}"]
    assert mount_key() not in {row["key"] for row in report["bundles"]}
    # The mount is inside the project's key, so it is named nowhere: not a row and
    # not in the ledger either (Mf-z643).
    assert mount_key() not in refusal_ledger(report)
    assert report["counts"]["total"] == 1

    blocked = create(client, headers, [{"key": mount_key()}])
    assert blocked["failed"] == 1
    assert blocked["results"][0]["reason"] == "unknown bundle"


def test_a_refused_container_is_not_creatable_by_key(client: TestClient, isolated_storage: Path) -> None:
    """A refused checkout is not a row, so its key is `unknown bundle` and creates nothing.

    The git dump keeps its five separate product rows (the R7 AC-3 test above) and
    is itself creatable by nobody. Mutation: offer the refused container as a row
    and one create turns five products into one project.
    """
    write_endorphin(isolated_storage)
    scan()
    headers = auth_header(client)
    report = detect(client, headers, limit=1000)
    assert f"import/{ENDORPHIN}" not in {row["key"] for row in report["bundles"]}

    created = create(client, headers, [{"key": f"import/{ENDORPHIN}"}])

    assert created["failed"] == 1
    assert created["results"][0]["reason"] == "unknown bundle"
    assert table_counts()["projects"] == 0


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

    created = create(client, headers, [{"key": f"import/{XBOX}"}])

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
    second = create(client, headers, [{"key": f"import/{XBOX}"}])
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
    # costs 9 statements (folders, memberships, the registered index, the
    # compound splits, the settings reads, this candidate's own reads, and the ONE
    # `select(Project)` that derives `excluded_keys` before the walk). A probe
    # that queried per candidate would be 10.
    assert len(report_statements()) == 9


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
    # Measured, not guessed: seven report-wide statements (folders, memberships,
    # the registered index, the compound splits, the two settings reads, and the
    # one `select(Project)` that derives `excluded_keys` before the walk) plus
    # three per candidate that already existed (its asset read, the marker
    # lookup's project list and its own detect read). Drift adds the one in the
    # middle and nothing per row.
    assert two_candidates == 13, "two candidates"
    assert five_candidates == 22, "five candidates: three more rows"



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


def _calls_named(root: ast.AST, name: str) -> List[ast.Call]:
    """Every direct call of ``name`` anywhere in ``root``'s subtree."""
    return [
        node
        for node in ast.walk(root)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def test_the_promotion_guard_replaces_the_claimed_ancestor_branch() -> None:
    """AC-1 (structural): the helper is deleted, and Decision 18 is still asked.

    Four questions, one mutation each: re-adding
    ``_single_claimed_child_subtree`` as a definition or as a call reds the first
    three; asking ``_promotion_would_double_own`` from anywhere but the one decision
    site (or twice there) reds the count; re-adding the ``if folder in
    claimed_ancestors:`` branch the guard replaced reds the last walk. The
    anti-over-deletion half is the ``_holds_stronger_candidate`` count: variant B
    deleted that guard along with the helper and reds four behavioural witnesses, and
    this test says so in one line rather than four.

    ``claimed_ancestors`` legitimately still exists inside ``_build_candidates`` - the
    set is built for the claim and read for the ``INFERRED_CONFIDENCES`` skip against
    ``rel_dir`` - so this walk forbids only a comparison of ``folder`` against it. An
    absent name would be the wrong pin; a decision site that branches on a claimed
    folder is the right one.
    """
    assert not hasattr(bundle_service, "_single_claimed_child_subtree"), (
        "the claimed-ancestor helper is deleted, not deprecated - re-adding it as a "
        "module attribute is the mutation this witness exists for"
    )

    tree = ast.parse(Path(bundle_service.__file__).read_text())

    definitions = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_single_claimed_child_subtree"
    ]
    assert definitions == [], (
        "a definition of ``_single_claimed_child_subtree`` anywhere in the module - "
        "nested ones included - is the helper coming back"
    )

    stale_calls = _calls_named(tree, "_single_claimed_child_subtree")
    assert stale_calls == [], (
        "a call site outlives the definition it names: re-adding one reds here even "
        "when the function itself is gone"
    )

    build_candidates = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_build_candidates":
            build_candidates = node
    assert build_candidates is not None, (
        "``_build_candidates`` is the one decision site this ticket changed; if it is "
        "gone from the module body, every count below would pass vacuously"
    )

    assert len(_calls_named(build_candidates, "_promotion_would_double_own")) == 1, (
        "the promotion guard is asked exactly once at the decision site - a second call "
        "is a second answer to 'may this twin take the disk', the defect the single "
        "guard was written to end"
    )
    assert len(_calls_named(build_candidates, "_holds_stronger_candidate")) == 1, (
        "Decision 18's guard survives the deletion (ADR 0010): deleting it with the old "
        "helper reds four promoted-row witnesses, and it reds here first"
    )

    claimed_folder_comparisons = [
        node
        for node in ast.walk(build_candidates)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "folder"
        and any(
            isinstance(comparator, ast.Name) and comparator.id == "claimed_ancestors"
            for comparator in node.comparators
        )
    ]
    assert claimed_folder_comparisons == [], (
        "the pairing decision site no longer branches on a claimed folder - "
        "``if folder in claimed_ancestors:`` is the branch the promotion guard "
        "replaced, and re-adding it puts two answers back at the same site"
    )


@pytest.mark.parametrize(
    ("rel_dir", "owned_keys", "work_entries", "expected"),
    [
        pytest.param("import/plain", (), {}, False, id="nothing-below"),
        pytest.param(
            "import/Pile",
            ("import/Pile/files",),
            {"import/Pile": (bundle_detect.BundleEntry(rel_path="own.stl", size=10),)},
            True,
            id="own-file-beside-claim",
        ),
        pytest.param(
            "import/Pile",
            ("import/Pile/files", "import/Pile/models"),
            {},
            True,
            id="two-children",
        ),
        pytest.param("import/wrapper", ("import/wrapper/child",), {}, False, id="one-child"),
    ],
)
def test_the_promotion_guard_refuses_only_a_disk_a_row_below_already_owns(
    rel_dir: str,
    owned_keys: Tuple[str, ...],
    work_entries: Mapping[str, Tuple[bundle_detect.BundleEntry, ...]],
    expected: bool,
) -> None:
    """AC-2: the guard's four answers, pinned without a single file on disk.

    The table is the four shapes the corpora reach, and every one of them is a unit
    question: nothing offered below (the twin owns the disk alone), a file of the
    directory's own beside the claim, two first-segment children (two products), and
    one first-segment child (a wrapper). ``True`` means REFUSE - the promotion guard
    is the inverted replacement for the deleted helper - so the polarity is pinned
    here rather than inferred from a behavioural witness.

    Mutation: drop the ``work`` leg and ``own-file-beside-claim`` reds; drop the
    first-segment count and ``two-children`` reds; invert the initial ``not
    owned_below`` return and ``nothing-below`` reds. A corpus cannot pin any of the
    four alone, which is why this table exists beside the behavioural tests.
    """
    owned_below = [
        bundle_service.BundleCandidate(
            key=key,
            snapshot=bundle_detect.BundleSnapshot(
                bundle_name=key.rsplit("/", 1)[-1], rel_dir=key, entries=()
            ),
            archive_path=None,
            is_archive=False,
            confidence="low",
        )
        for key in owned_keys
    ]
    work = dict(work_entries)

    assert bundle_service._promotion_would_double_own(rel_dir, owned_below, work) is expected, (
        f"{rel_dir} with rows below {owned_keys} and a work view of {sorted(work)} answers "
        f"{expected}; True means the twin must NOT be promoted"
    )


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


def thingiverse_export_directory(storage: Path, rel_dir: str) -> None:
    """The same vendor export as a DIRECTORY: documents and models at its own depth."""
    base = storage / rel_dir
    write_text(base / "README.txt", README_TEXT)
    write_text(base / "LICENSE.txt", LICENSE_TEXT)
    write_file(base / "preview.png", b"p" * 70)
    for index in range(3):
        write_file(base / f"part{index}.stl", b"s" * (40 + index))


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


def role_counts(candidate: bundle_service.BundleCandidate) -> Dict[str, int]:
    """How many entries of a candidate's own listing each pure role claims."""
    counts: Dict[str, int] = {}
    for entry in candidate.snapshot.entries:
        role = bundle_detect.classify_entry(entry.rel_path, entry.size)
        counts[role] = counts.get(role, 0) + 1
    return counts


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


def test_service_an_extraction_whose_files_all_live_in_one_subdirectory_is_promoted(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-1: the document-less tower is ONE low row, keyed at the DIRECTORY.

    Ten models, two previews and the vendor's own deliverable zip, and no ``.txt``
    anywhere: no rule of its own offers the extraction, so the verified stem twin is
    the only thing that can name it. The row is the promoted one - ``archive_owned``,
    never ``pair_only``, the archive attached as provenance and nothing attached from
    it, and the key is the DIRECTORY rather than the nested archive that used to be a
    row of its own. The report is the witness, not just the scan: the operator sees
    10 parts and 2 previews under one creatable ``ready`` row.

    Mutation: the promotion, or ``_own_depth_model_count``'s single-subdirectory leg
    (every model here sits one level down, in ``01_Wizard``), and this row disappears.
    What this shape does NOT pin is worth saying out loud: nothing below the parent is
    claimed, so ``work`` and ``folded`` agree and the fallback is a no-op - the
    claimed-child member witness below
    (``test_service_a_claimed_child_towers_nested_vendor_archive_is_a_member_not_a_row``,
    named for the answer it gave before Mf-ny89) is the discriminator for that line.
    """
    tower_extraction(isolated_storage, with_text=False)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == [TOWER_DIR], (
        "the extraction is one row, keyed at the directory it was extracted into"
    )
    row = row_for(report, TOWER_DIR)
    assert row["confidence"] == "low", "the shape was inferred from the archive, not from a rule"
    assert row["detector"] == "generic-bundle"
    assert row["status"] == "ready", "a promoted directory is creatable like any other low bundle"
    assert row["blocked_reason"] is None
    assert (row["model_count"], row["image_count"], row["text_count"]) == (10, 2, 0)
    assert row["archive"] == {"rel_path": TOWER_ZIP, "twin_verified": True}
    assert row["assets_total"] == 13, (
        "the ten parts, the two previews and the vendor's nested zip: every file the "
        "library scan registered, and nothing at all from the archive itself"
    )
    assert row["cover"]["rel_path"] == f"{TOWER_MEMBER_DIR}/Wizard_VisualReference.png"

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    assert [candidate.key for candidate in candidates] == [TOWER_DIR]
    candidate = candidates[0]
    assert candidate.is_archive is False
    assert candidate.archive_owned is True, "the verified pairing owns this directory"
    assert candidate.pair_only is False
    assert candidate.entry_root is None, "the stem route names no entry root"
    assert candidate.archive_path == TOWER_ZIP
    assert role_counts(candidate) == {
        bundle_detect.MODEL: 10,
        bundle_detect.IMAGE: 2,
        bundle_detect.ARCHIVE: 1,
    }, "the snapshot reports the ten parts and the two previews it holds"


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


def test_service_a_verified_twin_of_a_claimed_ancestor_is_promoted_on_its_single_child(
    isolated_storage: Path,
) -> None:
    """AC-1's other half: the ancestor's ONE child is CLAIMED, and it is still promoted.

    ``Wizard_Dice_v5.4_fixes.txt`` is the whole difference from AC-1: R3 now offers
    ``01_Wizard`` a row of its own and the claim strips every file out of the parent's
    own view, so the listing the pairing is judged on has to be ``folded``, the
    unclaimed subtree - and once it is, the answer is the same row AC-1 gets: one
    ``ready`` line keyed at the DIRECTORY, ``archive_owned``, the twin attached as
    provenance, and the child dissolved into it as ``member-of-bundle``. A parent
    whose entire payload sits in one child is that child's wrapper, not a container of
    somebody else's bundle, and the owner ruling is explicit that the operator gets a
    creatable row rather than a ``blocked``/``archive_only`` line with 0 assets that
    nothing can be made from.

    ``archive_owned is True`` and ``pair_only is False`` are the point: this is the
    promotion, not a weaker reading of the blocked row this test used to assert. The
    child is refused, never offered as a second row - the models are counted once.

    Mutation: read the pairing and the promotion off ``work`` (the pre-fix reading) and
    the emptied listing asks the shape question of nothing - the tower itself renders
    no row, the archive answers ``twin-unverified``, the no-facts reason, and the only
    row left in the tree is the vendor's nested zip: the row Mf-wd3v removed. Make
    ``_promotion_would_double_own`` refuse this directory and
    the row stops being a promoted one. It does NOT go back to being uncreatable -
    measured, that mutation leaves ``import/01_Fates End - Wizard Tower`` answering
    ``(ready, None)`` with ``assets_total`` 14, all fourteen files under the tower - so
    what reds here is ``candidate.pair_only is False``, the candidate's own flag. That
    is the honest shape of the witness since Mf-quv4: the flag says "the container rule
    refused this pairing", and the only thing that flag still decides in the report is
    which EXPLANATION a refused row carries.
    """
    tower_extraction(isolated_storage, with_text=True)
    scan()

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    assert [candidate.key for candidate in candidates] == [TOWER_DIR], (
        "the pair is one row, and the child that owns the models is not a second one"
    )
    candidate = candidates[0]
    assert candidate.confidence == "low"
    assert candidate.pair_only is False, "a single claimed child is a wrapper, not a container"
    assert candidate.archive_owned is True, "the verified pairing owns this directory"
    assert candidate.is_archive is False
    assert candidate.archive_path == TOWER_ZIP
    assert role_counts(candidate) == {
        bundle_detect.MODEL: 10,
        bundle_detect.IMAGE: 2,
        bundle_detect.TEXT: 1,
        bundle_detect.ARCHIVE: 1,
    }
    rules = refusal_rules(isolated_storage)
    assert rules[TOWER_MEMBER_DIR] == bundle_service.REFUSAL_MEMBER_OF_BUNDLE

    row = row_for(bundle_service.build_report(), TOWER_DIR)

    assert (row["status"], row["blocked_reason"]) == ("ready", None), (
        "a wrapper promoted on the one child it holds is creatable like any other low row"
    )
    assert (row["model_count"], row["image_count"], row["text_count"]) == (10, 2, 1)
    assert row["assets_total"] == len(files_under(isolated_storage, TOWER_DIR)), (
        "every file under the tower is counted, none of them out of the archive"
    )
    assert row["archive"] == {"rel_path": TOWER_ZIP, "twin_verified": True}


def test_service_a_verified_twin_of_a_two_directory_pile_is_refused_by_the_inherited_bound(
    isolated_storage: Path,
) -> None:
    """AC-3: the archive verifies, and the pile is still a pile.

    ``import/pile`` holds ``alpha/`` and ``beta/`` - two UNNAMED model-bearing
    subdirectories - with nothing at its own depth, and ``pile.zip`` is exactly their
    payload, so containment PASSES and the pairing now verifies against ``folded``.
    The only thing left that can refuse it is the own-depth/cardinality bound, and
    that bound is INHERITED: ``bundle_detect.archive_owned_facts`` asks
    ``bundle_detect._own_depth_model_count``, the same helper R3 and R5 gate on, and
    Mf-wd3v added no second definition of it in the service.

    Mutation: give ``_own_depth_model_count`` a second, weaker copy on the promotion
    path (or delete its ``len(unnamed) == 1`` leg) and ``import/pile`` becomes a low
    ``archive_owned`` row built on two things' models.
    """
    pile_fixture(isolated_storage, with_archive=True)

    scan = bundle_service.scan_bundles(isolated_storage)

    # No RULE row: the pile proves nothing. `import` is the maximal refused root, so
    # it is offered as the MANUAL scope (Mf-z643) - the rule verdict is the detector.
    assert [(candidate.key, candidate.manual) for candidate in scan.candidates] == [("import", True)]
    rules = refusal_rules(isolated_storage)
    # The pile is inside the offered manual scope now, so the ledger relabels the
    # archive a nested member instead of keeping the promotion's own reason.
    assert rules[PILE_ZIP] == bundle_service.REFUSAL_NESTED_MEMBER, (
        "the pairing verified and the shape guard refused it; the manual row then owns it"
    )
    # The pile itself is inside the offered manual scope, so the ledger relabels it
    # a member of that row rather than keeping the rule that first refused it.
    assert rules[PILE_DIR] == bundle_service.REFUSAL_MEMBER_OF_BUNDLE
    report = bundle_service.build_report()
    assert [row["key"] for row in report["bundles"]] == ["import"]
    assert report["bundles"][0]["detector"] == "manual"
    assert report["bundles"][0]["archive"] is None


def test_service_a_pile_with_no_archive_at_all_offers_nothing(isolated_storage: Path) -> None:
    """AC-4a: with no archive in the mix the pile is refused by the folder rule alone.

    The same two unnamed model-bearing subdirectories and no twin: R3's own-depth
    bound is the answer, which is the answer Mf-wd3v must not have changed. Nothing
    about the pairing is consulted, because there is nothing to pair.

    Mutation: drop the leading-capital requirement from ``bundle_detect._TITLE_WORD``
    and ``alpha`` - five letters, over R5's single-word floor - becomes a title row
    here with no archive anywhere in the tree, which is why these two names are
    lower-case single words.
    """
    pile_fixture(isolated_storage, with_archive=False)

    scan = bundle_service.scan_bundles(isolated_storage)

    # The pile is no RULE row; `import` is the maximal refused root and rides the
    # MANUAL scope (Mf-z643).
    assert [(candidate.key, candidate.manual) for candidate in scan.candidates] == [("import", True)]
    rules = refusal_rules(isolated_storage)
    # `import/pile` is inside the offered manual scope, so it is a member of it now.
    assert rules[PILE_DIR] == bundle_service.REFUSAL_MEMBER_OF_BUNDLE
    assert PILE_ZIP not in rules, "there is no archive in this tree to refuse"


def test_service_a_verified_twin_of_models_at_depth_two_is_refused_by_the_same_bound(
    isolated_storage: Path,
) -> None:
    """The other leg of the inherited bound: models BELOW the own-depth reach.

    ``import/deep`` holds ``sub/deeper/*.stl`` and one preview, and ``deep.zip`` is
    rooted at ``sub/deeper/``, so the pairing verifies and the listing the guard sees
    is the full subtree - with zero models at depth 0, under ``files/``, or under one
    single subdirectory. Mutation: count depth-2 models in
    ``_own_depth_model_count`` and this buried pile is promoted on its descendants.
    """
    for rel_path, content in DEEP_PAYLOAD.items():
        write_file(isolated_storage / DEEP_DIR / rel_path, content)
    write_zip(isolated_storage / DEEP_ZIP, dict(DEEP_PAYLOAD))

    scan = bundle_service.scan_bundles(isolated_storage)

    # No rule promotes it; `import` accepts it as the MANUAL scope (Mf-z643).
    assert [(candidate.key, candidate.manual) for candidate in scan.candidates] == [("import", True)]
    # The archive is inside that offered scope, so the ledger calls it a nested member.
    assert refusal_rules(isolated_storage)[DEEP_ZIP] == bundle_service.REFUSAL_NESTED_MEMBER


def test_service_a_verified_twin_never_promotes_a_directory_holding_a_strong_bundle(
    isolated_storage: Path,
) -> None:
    """AC-4b: ADR 0010 Decision 18 still bounds the promotion the fallback feeds.

    Two parents, both with a verified twin, both holding a real Thingiverse export
    (``README.txt`` plus ``LICENSE.txt``, the shipped Moai markers):

    * ``import/P`` owns a model of its own and holds the export as a nested archive.
      The facts are there for the taking, so only ``_holds_stronger_candidate`` can
      refuse it - and it does, so the parent gets NO row at all, ``archive_owned``
      included; the child keeps its own ``high`` row.
    * ``import/Q`` owns nothing: its only content is the claimed export directory.
      That is the shape the ``folded`` fallback exists for, and the answer is still
      the ancestor's answer - a ``pair_only`` row, never an ``archive_owned`` one.

    Mutation: delete the ``_holds_stronger_candidate`` guard and ``import/P`` returns
    as a creatable ``archive_owned`` row while ``moai_export.zip`` disappears from the
    report, swallowed as a file of the promoted parent. Revert the ``folded`` pairing
    and the ``import/Q`` row disappears too: with the empty own view the promotion
    never reaches the answer at all.
    """
    write_zip(isolated_storage / STRONG_CHILD_ZIP, thingiverse_export_entries())
    write_file(isolated_storage / STRONG_PARENT_DIR / "a.stl", STL_BYTES)
    twin_archive_of(isolated_storage, STRONG_PARENT_DIR, STRONG_PARENT_ZIP)
    thingiverse_export_directory(isolated_storage, CLAIMED_CHILD_DIR)
    twin_archive_of(isolated_storage, CLAIMED_PARENT_DIR, CLAIMED_PARENT_ZIP)
    scan()

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    by_key = {candidate.key: candidate for candidate in candidates}
    assert STRONG_PARENT_DIR not in by_key, (
        "Decision 18: a parent holding a stronger bundle is silent, not pair-only either"
    )
    assert all(candidate.archive_owned is False for candidate in candidates), (
        "no parent is promoted on a strong child's models"
    )
    child = by_key[STRONG_CHILD_ZIP]
    assert (child.is_archive, child.confidence) == (True, "high")
    assert by_key[CLAIMED_CHILD_DIR].confidence == "high", "the claimed export keeps its own row"
    parent = by_key[CLAIMED_PARENT_DIR]
    assert (parent.pair_only, parent.archive_owned) == (True, False)

    rules = refusal_rules(isolated_storage)
    assert rules[STRONG_PARENT_ZIP] == bundle_service.REFUSAL_CONTAINER
    assert rules[CLAIMED_PARENT_ZIP] == bundle_service.REFUSAL_TWIN_CONSUMED
    assert STRONG_PARENT_DIR not in {row["key"] for row in bundle_service.build_report()["bundles"]}


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


def test_service_a_twin_directory_that_is_empty_on_disk_is_still_refused_for_want_of_facts(
    isolated_storage: Path,
) -> None:
    """The branch the ``folded`` fallback had to keep alive.

    ``import/emptypair`` exists and holds NOTHING, while ``emptypair.zip`` holds a
    real payload: the archive verified nothing, and the honest answer is still
    ``twin-unverified``. Widening the pairing to the unclaimed subtree must not turn
    "the directory is empty" into "the directory matched".

    Mutation: relax site 1 from containment to "``folded`` has an entry for the twin"
    and this pair becomes a low ``archive_owned`` row over an empty directory.
    """
    (isolated_storage / EMPTY_TWIN_DIR).mkdir(parents=True)
    write_zip(isolated_storage / EMPTY_TWIN_ZIP, EMPTY_TWIN_ENTRIES)

    scan = bundle_service.scan_bundles(isolated_storage)

    # The directory itself proves nothing; `import` is the maximal refused root and
    # is offered as the MANUAL scope (Mf-z643). The archive's own reason is unchanged.
    assert [(candidate.key, candidate.manual) for candidate in scan.candidates] == [("import", True)], (
        "an empty directory contains no payload"
    )
    assert refusal_rules(isolated_storage)[EMPTY_TWIN_ZIP] == bundle_service.REFUSAL_NESTED_MEMBER, (
        "the archive is inside the offered manual scope; the pair still proved nothing"
    )


def test_service_the_measured_live_tower_is_promoted_at_its_real_counts(isolated_storage: Path) -> None:
    """The LIVE witness at its MEASURED counts, and it is ONE creatable row.

    This is ``/mnt/ceph/3D_models/import/01_Fates End - Wizard Tower`` and its
    extraction ``.../01_Wizard`` as the operator's library actually holds them: twenty
    entries - fifteen STLs (``Wizard_Tray_Lg_Tx_v1.4.stl`` ... ``Wizard_Dec_Roof_v5.4.stl``),
    three PNGs (``Wizard_RENDER.png``, ``Wizard_VisualReference.png``, ``_FileName_Key.png``),
    ``Wizard_Dice_v5.4_fixes.txt`` and the vendor's readable ``01_Wizard_v5.4.zip`` -
    beside the outer ``import/01_Fates End - Wizard Tower.zip`` that declares those
    exact twenty files. The ticket's stated shape (10 STLs, 2 previews, no text file)
    is NOT this tree, and every count here is off disk: the report row is 15/3/1, the
    snapshot is 15 models / 3 images / 1 text / 1 archive, and ``assets_total`` is read
    off the files the fixture wrote rather than from a number in a ticket note.

    ``Wizard_Dice_v5.4_fixes.txt`` is the load-bearing file: its presence is what R3
    reads to offer ``01_Wizard`` a row of its own, and the claim then strips all twenty
    files out of the parent's own view. That used to be the end of the parent's story -
    a claimed ancestor is a container, so the row came back ``blocked``/``archive_only``
    and nothing creatable - and at that build it also answered ``assets_total`` 0 with
    the cover not even a registered asset (``largest-image-unregistered``), because an
    archive-only row skipped the asset lookup outright. Mf-quv4 removed that skip: the
    same fall-through now measures ``(ready, None)``, ``assets_total`` 20 and a
    ``largest-image`` cover backed by a registered asset, which is why this witness
    pins the CANDIDATE flags (``pair_only is False``, ``archive_owned is True``) as the
    thing the mutation breaks. While the child that held every one of those files was
    refused ``member-of-bundle``: one visible, unimportable line. It is not
    the end of the story any more. Every file the parent owns sits inside that ONE
    child, so the parent is the child's wrapper and it is PROMOTED - the operator's
    answer is one ``ready`` row keyed at ``import/01_Fates End - Wizard Tower``, 15
    models / 3 images / 1 text, with ``01_Wizard`` shown inside it: the cover's own
    ``rel_path`` runs through the child, because the child's payload IS the row.

    Mutation: make ``_promotion_would_double_own`` refuse this directory and this row
    is the blocked one again - ``pair_only``,
    ``pair_only`` - measured ``ready`` with ``assets_total`` 20, not the blocked row of
    that build - and the witness that breaks is the candidate's own flags. Delete the
    ``_holds_stronger_candidate`` leg beside it and ``import/Q``'s HIGH export child is
    swallowed by its parent (the ``:7136`` witness reds, not this one - a LOW child is
    exactly what the promotion is for). Change any count here without changing the
    fixture, or drop the ``.txt``, and the row stops being this shape at all - the
    document-less tree is the AC-1 test's job, not this one's.
    """
    live_tower_extraction(isolated_storage)
    scan()
    files = files_under(isolated_storage, TOWER_DIR)

    assert len(files) == len(LIVE_MODEL_NAMES) + len(LIVE_PREVIEW_NAMES) + 2, (
        "the fixture holds the fifteen STLs, the three PNGs, the vendor's .txt and the "
        "vendor's own deliverable zip - the twenty entries the library scan registered"
    )

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    assert [candidate.key for candidate in candidates] == [TOWER_DIR], (
        "one row for the live extraction, and none for the vendor's archive inside it"
    )
    candidate = candidates[0]
    assert candidate.confidence == "low", "the shape came from the archive, not from a rule"
    assert candidate.pair_only is False, "a parent holding nothing but one child is a wrapper"
    assert candidate.archive_owned is True, "the verified pairing owns this directory"
    assert candidate.is_archive is False
    assert candidate.archive_path == TOWER_ZIP
    assert role_counts(candidate) == {
        bundle_detect.MODEL: 15,
        bundle_detect.IMAGE: 3,
        bundle_detect.TEXT: 1,
        bundle_detect.ARCHIVE: 1,
    }, "the twenty files the live tree actually holds"

    result = bundle_service.scan_bundles(isolated_storage)

    assert result.absorbed == (), (
        "a promoted parent is a real row, so R7 can now see it as absorbable: nothing "
        "above ``import/01_Fates End - Wizard Tower`` is a compound, and a promoted "
        "row vanishing into one would take the operator's only creatable line with it"
    )

    row = row_for(bundle_service.build_report(), TOWER_DIR)

    assert (row["status"], row["blocked_reason"]) == ("ready", None), (
        "one ready row for the pair the operator can see on disk"
    )
    assert (row["model_count"], row["image_count"], row["text_count"]) == (15, 3, 1)
    assert row["assets_total"] == len(files), (
        "every file under the tower is registered and counted - and none of them from "
        "the twin archive beside it"
    )
    assert row["detector"] == "generic-bundle"
    assert row["archive"] == {"rel_path": TOWER_ZIP, "twin_verified": True}
    largest_preview = LIVE_PREVIEW_NAMES[LIVE_PREVIEW_SIZES.index(max(LIVE_PREVIEW_SIZES))]
    assert row["cover"]["rel_path"] == f"{TOWER_MEMBER_DIR}/{largest_preview}", (
        "the cover comes out of the claimed child: the child's payload is the row's"
    )
    assert row["cover"]["asset_id"] is not None
    assert row["cover"]["reason"] == "largest-image", "a promoted row covers with a registered asset"

    rules = refusal_rules(isolated_storage)
    assert rules[TOWER_MEMBER_DIR] == bundle_service.REFUSAL_MEMBER_OF_BUNDLE
    assert rules[TOWER_NESTED_ZIP] == bundle_service.REFUSAL_NESTED_MEMBER
    assert rules[TOWER_ZIP] == bundle_service.REFUSAL_TWIN_CONSUMED
    assert TOWER_NESTED_ZIP not in {offered.key for offered in candidates}, (
        "the vendor's deliverable is a file of the bundle, never a row of its own"
    )


def test_service_a_promoted_emptied_claimed_ancestor_holds_no_archive_row_of_its_own(
    isolated_storage: Path,
) -> None:
    """The invariant the ``folded`` fallback rests on, pinned on a PROMOTED ancestor.

    ``import/W`` holds NOTHING of its own. Its one child ``import/W/Moai_statue`` is an
    R3-shaped export - three models, a preview, a ``notes.txt`` - and that export ships
    a real, readable nested archive: models and a render under one root with no text, so
    its own shape is candidate-shaped, which is precisely the point. ``import/W.zip`` is
    the twin of the whole subtree. This is the shape Mf-wd3v's fallback acts on: the
    child's claim strips every file out of the parent's OWN view, and the fallback then
    reads the pair off ``folded``, the unclaimed full-subtree listing - and the answer
    Mf-ny89 rules is the promotion: one ``ready`` row keyed at ``import/W``,
    ``archive_owned``, never ``pair_only``.

    Why THIS ancestor is promoted and ``import/Q`` is not is the load-bearing detail.
    ``W``'s single child earns ``low`` from the shape trio; ``Q``'s earns ``high`` from
    a vendor document pair. One claimed child is the OWNERSHIP question, and it is
    answered by ``_promotion_would_double_own``; the child's STRENGTH is a separate
    question, answered by ``_holds_stronger_candidate``, the next guard at the same
    decision site - so ``Q`` falls through to the pair-only container answer untouched.
    The two tests together are what stops either guard from being "tidied" into the
    other.

    What is pinned here is still not the row, it is the interlock that makes the
    promotion safe. ``_forget_claimed_subtree`` has exactly ONE call site, and it runs
    together with the ``claimed_ancestors`` marking, so "my own view is empty" and "I am
    a claimed ancestor" are always created as a pair; and ``_nested_archives`` together
    with the archive-containment strip keep an archive from SURVIVING as a row below
    such an ancestor. Both halves are load-bearing, and MORE so now that the ancestor
    is a real row rather than a pair-only one: a promoted key joins ``bundle_dirs`` and
    ``contained_keys``, so a surviving archive row underneath it would be swallowed by
    the very row that owns it - the Decision 18 swallow ``_build_candidates`` warns
    about in its own comment.

    Say it out loud: the child is refused ``member-of-bundle``, NOT kept as the R3 row
    its models and document earned it. Since Mf-ny89 that refusal is written directly,
    by the dissolution the promotion performs - the R7 absorption shape, a row that is
    being taken into a bigger one - committed after the nested-member prune rather than
    at the branch itself, which is what stops a VETOED promotion from taking a row it no
    longer justifies (B-1, and its witness). Before Mf-ny89 the same word arrived
    indirectly: the
    archive-containment container strip dropped the LOW folder candidate because its
    subtree held an archive-only row, and ``_reconcile_refusals`` relabelled the drop
    ``member-of-bundle`` because the directory sat strictly inside the row that did
    survive. The operator's answer is unchanged; the rule that writes it moved.

    Mutation: exempt nested archives from membership, or move the zip-only pass before
    the claims, and an archive row survives below the promoted ancestor for its row to
    swallow - the "no archive row" assertion below is what catches it. Make
    ``_promotion_would_double_own`` refuse this directory and the ancestor goes back to
    ``pair_only`` - measured ``(ready, None)`` with ``assets_total`` 6 on this tree, the
    count Mf-quv4 stopped skipping - and the assertion that breaks is the candidate's own
    flag triple.
    Reverting either ``folded`` expression takes the
    ancestor's row away entirely.
    """
    for index, name in enumerate(EMPTY_CLAIMED_CHILD_MODELS):
        write_file(isolated_storage / EMPTY_CLAIMED_CHILD_DIR / name, b"s" * (40 + index))
    write_file(isolated_storage / EMPTY_CLAIMED_CHILD_DIR / EMPTY_CLAIMED_CHILD_PREVIEW, b"p" * 70)
    write_file(isolated_storage / EMPTY_CLAIMED_CHILD_DIR / EMPTY_CLAIMED_CHILD_TEXT, EMPTY_CLAIMED_CHILD_TEXT_BYTES)
    write_zip(isolated_storage / EMPTY_CLAIMED_NESTED_ZIP, EMPTY_CLAIMED_NESTED_ENTRIES)
    twin_archive_of(isolated_storage, EMPTY_CLAIMED_DIR, EMPTY_CLAIMED_ZIP)
    scan()

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    by_key = {candidate.key: candidate for candidate in candidates}
    assert [candidate.key for candidate in candidates] == [EMPTY_CLAIMED_DIR], (
        "the emptied ancestor is the tree's one row - the fallback is what puts it there, "
        "and the dissolution is what keeps its child out"
    )
    parent = by_key.get(EMPTY_CLAIMED_DIR)
    assert parent is not None, "the emptied ancestor is offered - the fallback is what puts it there"
    assert (parent.confidence, parent.pair_only, parent.archive_owned) == ("low", False, True), (
        "an ancestor whose whole payload sits in one claimed child is that child's "
        "wrapper, and a wrapper is promoted rather than shown and blocked"
    )

    deep_archives = [
        candidate.key
        for candidate in candidates
        if candidate.is_archive and candidate.key.startswith(f"{EMPTY_CLAIMED_DIR}/")
    ]
    assert deep_archives == [], (
        "no archive row exists below the promoted ancestor: its key joins "
        "`bundle_dirs` and `contained_keys`, so a surviving archive row there would be "
        "swallowed by the very row that owns it"
    )

    rules = refusal_rules(isolated_storage)
    assert rules[EMPTY_CLAIMED_CHILD_DIR] == bundle_service.REFUSAL_MEMBER_OF_BUNDLE, (
        "the child that earned the claim is a member of the ancestor's row (recorded, not endorsed)"
    )
    assert rules[EMPTY_CLAIMED_NESTED_ZIP] == bundle_service.REFUSAL_NESTED_MEMBER
    assert EMPTY_CLAIMED_CHILD_DIR not in by_key, "the child is not a second row"
    assert rules[EMPTY_CLAIMED_ZIP] == bundle_service.REFUSAL_TWIN_CONSUMED
    assert member_refusals_name_a_real_row(isolated_storage) == []
    assert no_offered_row_contains_another_offered_row(isolated_storage) == [], (
        "the promotion left one row for the payload, not the parent and the child it took"
    )


def test_service_the_promoted_live_tower_dissolves_its_child_row_and_leaves_the_archive_a_member(
    isolated_storage: Path,
) -> None:
    """AC-2 (the live shape): one row, and the only thing dissolved out of it is a ROW.

    Promotion takes the child's row and nothing else. ``01_Wizard`` WAS an offered row
    - R3 gave it one for the ``.txt`` inside it - so it is named ``member-of-bundle`` by
    the dissolution, the same shape R7 uses when a compound absorbs the rows a container
    was hiding. ``01_Wizard_v5.4.zip`` was an offered row too, and it is NOT dissolved:
    an archive inside a bundle is a member, and the pass that owns that answer still
    owns it - ``nested-member``, the word that says "a zip inside a detected bundle",
    not "a row that was taken into a bigger one".

    Mutation: revert the ``claimed_ancestors`` branch and the tower is the pair-only
    row again - the ledger still reads ``member-of-bundle`` for the child, because the
    archive-containment strip and the reconciliation used to write that word, while the
    row itself comes back measuring ``(ready, None)`` with ``assets_total`` 20 (the
    blocked/``assets_total`` 0 answer belongs to the build that skipped the lookup).
    Say what this witness does NOT
    pin: deleting the dissolution ALONE reds nothing here, because in THIS tree the
    child is separately dropped by the archive-containment strip - the test below, whose
    child holds no archive at all, is the one that pins the dissolution ITSELF, and
    ``test_service_a_promotion_the_nested_member_prune_vetoes_takes_no_rows_at_all`` is
    the one that pins WHEN it may be committed (B-1).
    """
    live_tower_extraction(isolated_storage)
    scan()

    candidates = bundle_service.scan_bundles(isolated_storage).candidates

    assert [candidate.key for candidate in candidates] == [TOWER_DIR], (
        "one row for the tower: the child that owns the files is inside it, not beside it"
    )
    assert TOWER_MEMBER_DIR not in {candidate.key for candidate in candidates}
    assert candidates[0].archive_path == TOWER_ZIP
    assert candidates[0].entry_root is None, "the stem route names no entry root"

    rules = refusal_rules(isolated_storage)

    assert rules[TOWER_MEMBER_DIR] == bundle_service.REFUSAL_MEMBER_OF_BUNDLE
    assert rules[TOWER_NESTED_ZIP] == bundle_service.REFUSAL_NESTED_MEMBER
    assert rules[TOWER_ZIP] == bundle_service.REFUSAL_TWIN_CONSUMED
    assert member_refusals_name_a_real_row(isolated_storage) == []
    assert no_offered_row_contains_another_offered_row(isolated_storage) == []

    row = row_for(bundle_service.build_report(), TOWER_DIR)

    assert (row["status"], row["blocked_reason"]) == ("ready", None)
    assert row["assets_total"] == len(files_under(isolated_storage, TOWER_DIR))


def test_service_a_tower_whose_child_holds_no_archive_of_its_own_is_still_one_row(
    isolated_storage: Path,
) -> None:
    """AC-2 (the shape that used to show TWO rows): the archive inside the child is not what dissolves it.

    The same parent and the same claimed ``01_Wizard``, minus the vendor's
    ``01_Wizard_v5.4.zip``. Before the dissolution this tree showed the blocked
    pair-only parent AND a ``ready`` row for ``01_Wizard`` - with no candidate-shaped
    archive in the child, the archive-containment strip had nothing to drop it on, so
    the operator was offered a row that created nothing beside a row that offered the
    very same ten models, two previews, document and file count a second time. The
    lying row was a side effect of the way the FIRST shape happened to lose its child,
    which is why the live shape never showed it.

    Mutation: make the dissolution depend on the nested archive being there (or on the
    container strip that depends on it) and ``01_Wizard`` is a second row again. The
    answer that has to survive is the shape question - one child, nothing of the
    parent's own - and it does not read a single byte of the child's contents.
    Mutation (run): neuter the deferred apply site's removal and this is the only test in
    the file that reds, on the candidate list two lines above; measured directly, this
    tree is also the one where ``no_offered_row_contains_another_offered_row`` returns the
    child's key while ``member_refusals_name_a_real_row`` still says ``[]``.
    """
    tower_extraction(isolated_storage, with_text=True, with_nested_zip=False)
    scan()

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    assert [candidate.key for candidate in candidates] == [TOWER_DIR], (
        "one row, with or without an archive inside the child"
    )

    rules = refusal_rules(isolated_storage)

    assert rules[TOWER_MEMBER_DIR] == bundle_service.REFUSAL_MEMBER_OF_BUNDLE
    assert rules[TOWER_ZIP] == bundle_service.REFUSAL_TWIN_CONSUMED
    assert member_refusals_name_a_real_row(isolated_storage) == []
    assert no_offered_row_contains_another_offered_row(isolated_storage) == [], (
        "the dissolution left one row for the payload, not the parent and the child it "
        "took - the tree where nothing but the dissolution removes that child"
    )

    row = row_for(bundle_service.build_report(), TOWER_DIR)

    assert (row["status"], row["blocked_reason"]) == ("ready", None)
    assert (row["model_count"], row["image_count"], row["text_count"]) == (10, 2, 1)
    assert row["assets_total"] == len(files_under(isolated_storage, TOWER_DIR)), (
        "the thirteen files the child holds, counted once, under the parent's key"
    )


def test_service_a_promotion_the_nested_member_prune_vetoes_takes_no_rows_at_all(
    isolated_storage: Path,
) -> None:
    """B-1: a vetoed promotion takes nothing, and the payload's own row survives.

    Three levels, two verified twins, the payload deeper than either twin (see
    ``b1_vetoed_promotion_tree``). ``import/X`` holds a model of its own beside the
    bundle it ships, so it is not a wrapper and answers as the pair-only container it
    is. ``import/X/P`` IS a wrapper - nothing of its own, one child ``import/X/P/A``
    holding the three models, the preview and the ``notes.txt`` that earned the claim -
    so the ``claimed_ancestors`` branch promotes it on that child and RECORDS the intent
    to dissolve the child's row. Then the nested-member pass runs: ``P.zip`` sits
    strictly inside the pair-only ``X``, so it is a member, its pairing is pruned, and
    the promoted candidate is dropped as no longer archive-owned. The promotion the
    dissolution was justified by is gone.

    Whether anything is left to create from is decided purely by WHEN that dissolution
    was committed. Committed after the prune - what the code does - a vetoed promotion
    takes nothing: ``A`` keeps the ``low`` row its own trio earned and the operator can
    create from it. Committed at branch time, which was the defect, ``A`` was already
    gone when the veto arrived: no row for ``A``, no row for ``P``, one
    ``blocked``/``archive_only`` row for ``X`` and NOTHING obtainable from the whole
    tree - measured, 1 row / 0 creatable rows before the fix, 2 rows / 1 creatable row
    after it. Re-measured after Mf-quv4 the same eager build leaves ``import/X``
    answering ``(ready, None)`` with ``assets_total`` 7 - every file under it, which the
    row now counts - and its create still answers ``failed``/``contains_bundles`` with
    no Project row: the report stopped inventing the refusal and the create-time belt is
    what refuses it now. Five files that exist on disk and belong to nobody, and a
    ledger writing ``member-of-bundle`` for a path whose owning row does not exist: the
    P1 symptom (a directory nobody could import) re-created one level up.

    The invariant, stated once so it can be pointed at: **the ledger never carries
    ``member-of-bundle`` for a path that is neither a row nor strictly inside a row.**
    The word means "a bundle the operator was offered owns this path".
    ``member_refusals_name_a_real_row`` asserts it over the whole scan, and the apply
    site in ``_build_candidates`` makes it structural rather than lucky. Say what it
    does NOT catch on its own: in THIS tree the surviving ``X`` is a row and ``A`` sits
    inside it, so the shape check passes even in the broken build - what catches B-1 is
    the row-level assertion below, that the child keeps its row and that a creatable row
    exists at all. The helper is here because the narrower failure (both rows gone, the
    refusal left naming nothing above it) is caught by it directly.

    Mutation (run): restore the eager dissolve - refuse with the override and drop the
    child from ``found`` inside the branch, leave the apply site in place - and this is
    the only test in the file that reds, on "the child keeps its own row".
    """
    b1_vetoed_promotion_tree(isolated_storage)
    scan()

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    by_key = {candidate.key: candidate for candidate in candidates}
    assert B1_CHILD_DIR in by_key, (
        "the child keeps its own row: the promotion that would have taken it was vetoed, "
        "so its dissolution commits nothing"
    )
    assert [candidate.key for candidate in candidates] == [B1_PARENT_DIR, B1_CHILD_DIR]
    assert B1_MID_DIR not in by_key, "the vetoed promotion is not offered"
    child = by_key[B1_CHILD_DIR]
    assert (child.pair_only, child.archive_owned, child.confidence) == (False, False, "low"), (
        "the row the child's own listing earned, untouched by a promotion that did not survive"
    )

    report = bundle_service.build_report()
    creatable = [row["key"] for row in report["bundles"] if row["status"] == "ready"]
    assert creatable == [B1_CHILD_DIR], (
        "at least one row is creatable - the defect left this tree with none at all"
    )
    child_row = row_for(report, B1_CHILD_DIR)
    assert (child_row["model_count"], child_row["image_count"], child_row["text_count"]) == (3, 1, 1)
    assert child_row["assets_total"] == len(files_under(isolated_storage, B1_CHILD_DIR)), (
        "the five files the child holds are owned by a row, which is what the eager "
        "dissolve took away"
    )
    parent_row = row_for(report, B1_PARENT_DIR)
    assert (parent_row["status"], parent_row["blocked_reason"]) == ("blocked", "archive_only"), (
        "the pair-only container answer for a parent with a model of its own is unchanged"
    )

    rules = refusal_rules(isolated_storage)
    assert member_refusals_name_a_real_row(isolated_storage) == []
    # The row-set half of the same invariant, stated without a fixture-shaped literal:
    # the vetoed wrapper is silence, so the pair-only ``import/X`` is the ONLY row above
    # the payload. Say what it does not do here - under the apply-site neuter this stays
    # ``[]``, because the veto already left the child its row - so this call is the
    # redundancy that holds when the candidate-list literal above is edited to match a
    # broken build, not the first line to red on this tree.
    assert no_offered_row_contains_another_offered_row(isolated_storage) == []
    assert rules[B1_MID_DIR] == bundle_service.REFUSAL_MEMBER_OF_BUNDLE, (
        "the vetoed wrapper is a member of the container that does exist - silence, and "
        "no second row for a directory that owns nothing of its own"
    )
    assert rules[B1_MID_ZIP] == bundle_service.REFUSAL_NESTED_MEMBER
    assert rules[B1_PARENT_ZIP] == bundle_service.REFUSAL_TWIN_CONSUMED


def test_service_a_single_medium_child_keeps_its_own_row_and_its_parent_stays_pair_only(
    isolated_storage: Path,
) -> None:
    """AC-4, the guard direction: ONE child is the shape; the child's STRENGTH still rules.

    ``import/M`` holds nothing but ``5390184_ARTICULATED_ice_dragon__fixed_``, whose own
    model and id-bearing name buy it a MEDIUM row from R4. So ``_promotion_would_double_own``
    answers NO - one child, nothing else - and the answer that decides is
    ``_holds_stronger_candidate``, the next guard at the same decision site: a name-derived
    statement about which thing a directory IS outranks a shape inference about a zip
    beside it (ADR 0010 Decision 18), so the parent is refused the promotion and falls
    through to the ``pair_only`` container answer it always gave, and the dragon keeps
    its own ``medium`` ``ready`` row.

    This is the witness that the ownership guard BOUNDS the promotion rather than
    gating it: when it refuses, the directory still gets its visible ``pair_only`` row,
    so the operator keeps something to act on. The ``_holds_stronger_candidate`` guard
    that follows is what refuses the promotion on STRENGTH alone, and it keeps that
    same pair-only row wherever rows below already own the disk. Delete either and the
    parent is promoted on the dragon's model while the dragon is refused
    ``member-of-bundle`` - the Decision 18 swallow, now wearing the ownership rule's
    clothes. The single-LOW-child witnesses above are what stop the opposite edit.
    """
    write_file(isolated_storage / MEDIUM_CHILD_DIR / MEDIUM_CHILD_MODEL, STL_BYTES)
    twin_archive_of(isolated_storage, MEDIUM_PARENT_DIR, MEDIUM_PARENT_ZIP)
    scan()

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    by_key = {candidate.key: candidate for candidate in candidates}
    assert [candidate.key for candidate in candidates] == [MEDIUM_PARENT_DIR, MEDIUM_CHILD_DIR]
    parent = by_key[MEDIUM_PARENT_DIR]
    assert (parent.pair_only, parent.archive_owned, parent.confidence) == (True, False, "low"), (
        "a strong child bounds the promotion and leaves the container answer alone"
    )
    child = by_key[MEDIUM_CHILD_DIR]
    assert (child.pair_only, child.archive_owned, child.confidence) == (False, False, "medium")
    assert refusal_rules(isolated_storage)[MEDIUM_PARENT_ZIP] == bundle_service.REFUSAL_TWIN_CONSUMED

    report = bundle_service.build_report()

    parent_row = row_for(report, MEDIUM_PARENT_DIR)
    assert (parent_row["status"], parent_row["blocked_reason"]) == ("blocked", "archive_only")
    child_row = row_for(report, MEDIUM_CHILD_DIR)
    assert (child_row["status"], child_row["confidence"], child_row["model_count"]) == ("ready", "medium", 1)


def test_service_a_claimed_ancestor_with_a_file_of_its_own_is_not_promoted(
    isolated_storage: Path,
) -> None:
    """AC-5, the LOOSE FILE negative: a wrapper holds nothing of its own.

    ``import/L`` is the live tower with one file added - ``leftover.stl``, sitting at the
    parent's OWN depth beside the claimed ``parts/``. That single file is the whole
    difference between a vendor's extraction directory and somebody's working folder: it
    says the parent is a thing in its own right, and a promoted row would hand the
    operator 3 of the 4 models in the tree under a key that names a directory holding
    somebody else's bundle. So ``work`` - asked only "empty?", the one question the
    post-claim view answers honestly - says NO, and the parent keeps the ``pair_only``
    container answer while the child keeps the ``low`` row its trio earned.

    Mutation: delete the shape question and the parent is promoted on its child's
    models with its own ``leftover.stl`` as the only evidence against it - this row is
    what reds. What it does NOT pin is the ``work`` leg on its own: a loose file is a
    first segment of ``folded`` too, so both legs answer this tree, and the partly
    claimed child below is what pins ``work`` alone.
    """
    low_export_directory(isolated_storage, LOOSE_CHILD_DIR)
    write_file(isolated_storage / LOOSE_OWN_MODEL, b"s" * 55)
    twin_archive_of(isolated_storage, LOOSE_PARENT_DIR, LOOSE_PARENT_ZIP)
    scan()

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    by_key = {candidate.key: candidate for candidate in candidates}
    assert [candidate.key for candidate in candidates] == [LOOSE_PARENT_DIR, LOOSE_CHILD_DIR]
    parent = by_key[LOOSE_PARENT_DIR]
    assert (parent.pair_only, parent.archive_owned) == (True, False), (
        "one file of its own and the directory is no longer a wrapper"
    )
    assert by_key[LOOSE_CHILD_DIR].confidence == "low", "the child keeps the row it earned"
    assert refusal_rules(isolated_storage)[LOOSE_PARENT_ZIP] == bundle_service.REFUSAL_TWIN_CONSUMED

    row = row_for(bundle_service.build_report(), LOOSE_PARENT_DIR)

    assert (row["status"], row["blocked_reason"]) == ("blocked", "archive_only")


def test_service_a_claimed_ancestor_holding_two_claimed_children_is_not_promoted(
    isolated_storage: Path,
) -> None:
    """AC-5, the PILE negative: one payload, one child - and this one has two.

    ``import/D`` holds ``files/`` and ``models/``, each a claimed LOW export of its own,
    and ``D.zip`` declares exactly their payload, so the pairing verifies. The names are
    the load-bearing choice: ``R3_MODEL_DIRS`` counts the models under ``files/`` as
    own-depth, so ``_own_depth_model_count`` passes the listing and the inherited
    cardinality bound NEVER reaches this decision site - the archive is refused
    ``twin-consumed``, not ``promotion:no-own-depth-model``, which is what proves the
    refusal here came from ``_promotion_would_double_own`` reading two first segments
    off the rows already offered below. The ``pile_fixture`` witnesses do not test this
    helper at all: their ``alpha/`` and ``beta/`` are UNCLAIMED and the inherited bound
    refuses the pairing before the ownership question is asked.

    Mutation: read the single segment off ``work`` (empty, so no segments at all) or
    drop the cardinality test, and ``import/D`` becomes a creatable ``archive_owned``
    row built on two products' models - the pile promoted as one thing because an
    archive happened to ship both.
    """
    for name in DOUBLE_CHILD_DIRS:
        low_export_directory(isolated_storage, f"{DOUBLE_PARENT_DIR}/{name}")
    twin_archive_of(isolated_storage, DOUBLE_PARENT_DIR, DOUBLE_PARENT_ZIP)
    scan()

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    by_key = {candidate.key: candidate for candidate in candidates}
    assert [candidate.key for candidate in candidates] == [
        DOUBLE_PARENT_DIR,
        f"{DOUBLE_PARENT_DIR}/files",
        f"{DOUBLE_PARENT_DIR}/models",
    ]
    parent = by_key[DOUBLE_PARENT_DIR]
    assert (parent.pair_only, parent.archive_owned) == (True, False), (
        "two claimed children is a pile whatever the archive beside it declares"
    )
    for name in DOUBLE_CHILD_DIRS:
        assert by_key[f"{DOUBLE_PARENT_DIR}/{name}"].confidence == "low", (
            "each product keeps the row its own listing earned"
        )
    rules = refusal_rules(isolated_storage)
    assert rules[DOUBLE_PARENT_ZIP] == bundle_service.REFUSAL_TWIN_CONSUMED, (
        "the pairing verified: the shape guard is the only thing that can refuse it, "
        "and the inherited own-depth bound is not it - ``files/`` supplies the models"
    )

    row = row_for(bundle_service.build_report(), DOUBLE_PARENT_DIR)

    assert (row["status"], row["blocked_reason"]) == ("blocked", "archive_only")


def test_service_a_parent_whose_single_child_is_only_partly_claimed_is_not_promoted(
    isolated_storage: Path,
) -> None:
    """The ``work`` leg on its own: one first segment is not yet one wrapper.

    ``import/Z`` holds ``A/`` and nothing else, so ``folded`` answers the segment
    question YES - and ``A`` is not a row of its own: it holds two models and a claimed
    grandchild ``A/B``, which is what the folder pass offered. The claim stripped
    ``A/B`` out of the parent's view and left ``A/own0.stl`` and ``A/own1.stl`` behind,
    so ``work`` is NOT empty and the parent is not a wrapper: it is a directory still
    holding two models of somebody's, above a bundle that is not its own. It keeps the
    ``pair_only`` container answer, ``A`` is refused ``member-of-bundle``, and ``B``
    keeps the ``low`` row its trio earned.

    Mutation: delete the ``work`` leg and read only ``folded`` - one first segment, so
    ``import/Z`` is promoted ``archive_owned`` and its claimed grandchild is dissolved
    into it. That is Decision 18's swallow wearing the shape rule's clothes, and no
    other test in this file reds when it happens: the pile reds on the segment leg and
    the loose file on both, which is why this tree exists.
    """
    for name in PARTIAL_CHILD_MODELS:
        write_file(isolated_storage / PARTIAL_CHILD_DIR / name, b"s" * (30 + len(name)))
    low_export_directory(isolated_storage, PARTIAL_GRANDCHILD_DIR)
    twin_archive_of(isolated_storage, PARTIAL_PARENT_DIR, PARTIAL_PARENT_ZIP)
    scan()

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    by_key = {candidate.key: candidate for candidate in candidates}
    assert [candidate.key for candidate in candidates] == [PARTIAL_PARENT_DIR, PARTIAL_GRANDCHILD_DIR]
    parent = by_key[PARTIAL_PARENT_DIR]
    assert (parent.pair_only, parent.archive_owned) == (True, False), (
        "a leftover subtree under the single child means the parent is not that child's wrapper"
    )
    assert by_key[PARTIAL_GRANDCHILD_DIR].confidence == "low", "the claimed grandchild keeps its own row"

    rules = refusal_rules(isolated_storage)

    assert rules[PARTIAL_CHILD_DIR] == bundle_service.REFUSAL_MEMBER_OF_BUNDLE
    assert rules[PARTIAL_PARENT_ZIP] == bundle_service.REFUSAL_TWIN_CONSUMED

    row = row_for(bundle_service.build_report(), PARTIAL_PARENT_DIR)

    assert (row["status"], row["blocked_reason"]) == ("blocked", "archive_only")


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


def test_a_promoted_tower_creates_one_project_whose_parts_come_out_of_the_child(
    client: TestClient, isolated_storage: Path
) -> None:
    """THE complaint, answered on the real route: it creates, 18 assets, out of ``01_Wizard``.

    The real ``POST /bundles/create`` over the real fixture (``live_tower_extraction``
    plus the real ``library_scan``) gives the measured answer: ``created`` 1 and
    ``blocked``/``skipped``/``failed`` all 0, ``status`` ``created`` with no ``reason``,
    18 assets added - the fifteen STLs AND the three PNGs the vendor actually shipped,
    the ATTACH table of Mf-wsdo - and every member ``storage_path`` running through
    ``01_Wizard/``. The child's payload became the parent's project, which is the entire
    claim of the promotion; the marker is what says the project was made from the
    PARENT's key rather than the child's. The image half of that member set, and the
    cover that sits among it, are pinned witness-for-witness by
    ``test_a_promoted_tower_create_attaches_its_preview_images`` beside this one.

    The CONTAINS_BUNDLES belt passing is this same assertion, not a second one, so it is
    stated both ways. The row is ``low``, which sits in ``bundle_detect.INFERRED_CONFIDENCES``,
    so the belt is LIVE on it: ``_create_one`` answers ``failed``/``contains_bundles``
    before the project row exists whenever ``len(assets) > len(own_assets)`` - registered
    files under the key versus the paths the candidate's own snapshot names. A promoted
    snapshot is built off ``folded``, the unclaimed FULL-SUBTREE listing, so the two sets
    are equal and the create succeeding IS the belt passing; the set equality below says
    it in the belt's own terms rather than trusting the one result line. Say what a
    belt-shaped failure would look like instead: a row promoted on the parent's EMPTIED
    own view registers 20 files against 0 own paths and answers ``failed`` here - which
    is a different defect from the one the promotion fixed, since the ``pair_only``
    ancestor this row replaced answered ``blocked``/``archive_only`` and created nothing.

    Mutation (run, and it moved): make the promotion answer "no wrapper" -
    ``_promotion_would_double_own`` answering ``True`` is the pre-Mf-ny89
    fall-through - and since Mf-quv4 THIS test no longer reds. Measured: the pair-only
    row that mutation leaves answers ``(ready, None, 'low')`` with ``assets_total`` 20,
    its snapshot is the unclaimed full subtree so the create belt passes, and the create
    makes its one project of the child's eighteen files exactly as pinned here. What the mutation
    DOES red is four witnesses beside this one -
    ``test_service_a_verified_twin_of_a_claimed_ancestor_is_promoted_on_its_single_child``,
    ``test_service_the_measured_live_tower_is_promoted_at_its_real_counts``,
    ``test_service_a_promoted_emptied_claimed_ancestor_holds_no_archive_row_of_its_own``
    and ``test_service_a_tower_whose_child_holds_no_archive_of_its_own_is_still_one_row``
    - each on the candidate's own ``pair_only``/``archive_owned`` flags. That is the
    honest division of labour now: the flags prove the promotion happened, this test
    proves the promotion creates. This is the only test in the file that asks the CREATE
    question of the promoted key, which is why that complaint is pinned here and nowhere
    else.
    """
    live_tower_extraction(isolated_storage)
    scan()
    headers = auth_header(client)

    row = row_for(detect(client, headers), TOWER_DIR)

    assert (row["status"], row["blocked_reason"], row["confidence"]) == ("ready", None, "low")
    assert row["assets_total"] == len(files_under(isolated_storage, TOWER_DIR)) == 20

    created = create(client, headers, [{"key": TOWER_DIR}])

    assert (created["created"], created["skipped"], created["blocked"], created["failed"]) == (
        1,
        0,
        0,
        0,
    ), "the promoted row is creatable - the operator's complaint, measured on the real route"
    result = created["results"][0]
    assert (result["status"], result["reason"]) == ("created", None), (
        "not `skipped`, not `unknown bundle`, and not the belt's `contains_bundles`"
    )
    project_id = result["project_id"]
    assert project_id is not None
    assert result["assets_added"] == len(registered_asset_paths(TOWER_DIR)) == 20, (
        "all twenty files the scan registered under the twin - fifteen models, three "
        "previews, the vendor ``.txt`` and its nested deliverable zip (Mf-7oow) - a "
        "promoted row that created 0 assets is the ticket's complaint wearing a `ready` "
        "badge"
    )

    members = project_storage_paths(project_id)

    assert members == registered_asset_paths(TOWER_DIR) == {
        f"{TOWER_MEMBER_DIR}/{name}"
        for name in (*LIVE_MODEL_NAMES, *LIVE_PREVIEW_NAMES, LIVE_TEXT_NAME, "01_Wizard_v5.4.zip")
    }, (
        "the members are every registered file under the parent's key, which here is the "
        "child's whole payload: fifteen STLs, three PNGs, the text and the nested zip"
    )
    assert all(path.startswith(f"{TOWER_MEMBER_DIR}/") for path in members), (
        "every attached member runs through ``01_Wizard/``, so nothing came from anywhere else"
    )
    assert member_count(project_id) == 20
    assert len(project_asset_ids(client, headers, project_id)) == 20, "the served project agrees"
    assert project_marker(project_id)["key"] == TOWER_DIR, "created from the PARENT's key"
    assert project_listing(client, headers)[project_id]["origin"] == "imported"

    # The belt's own comparison, said out loud so a reader never has to infer it from
    # one result line: the promoted snapshot names every registered file under the key.
    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    candidate = next(item for item in candidates if item.key == TOWER_DIR)
    with Session(engine) as session:
        registered = bundle_service.registered_assets(session, TOWER_DIR)
        own = bundle_service._own_paths(TOWER_DIR, candidate)

    assert {asset.path for asset in registered} == own, (
        "the belt is live on this `low` row and satisfied, not bypassed: registered "
        "files under the key and the paths the promoted snapshot names are one set"
    )


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
      ``bundle_detect.IMAGE_SUFFIXES`` returns the three PNGs, which is the
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

    picked = {path for path in member_paths if bundle_service._suffix(path) in bundle_detect.IMAGE_SUFFIXES}

    assert picked == preview_paths, (
        "the landing page filters its members by image kind; server-side that filter is "
        "this line, and an empty set here is the empty cover picker the owner hit"
    )


def test_a_created_promotion_answers_its_own_project_id_and_a_second_create_is_a_skip(
    client: TestClient, isolated_storage: Path
) -> None:
    """The answer a promoted key gives AFTER the create - new surface, not a re-run.

    Before Mf-ny89 this key was outside the create door, and that is why nothing here
    covered it: the row was ``pair_only``, and ``_report_row`` answered such a candidate
    from its flag ALONE - ``blocked``/``archive_only``, with the asset lookup skipped
    entirely, so its containment set was empty by construction ("nothing to create, so
    nothing is looked up"), ``find_existing_project`` was therefore handed no assets to
    prove the files with, and the row pinned ``existing_project_id`` to ``None`` however
    many projects the operator had made. Mf-quv4 removed that skip, so a pair-only row
    is no longer able to hide its own files either: the flag decides which EXPLANATION a
    refused row carries, never whether the report reads the directory. The promotion put
    the key inside the create door, and the post-create contract came with it. Pinned here for the first time: the
    row reports the project it became - by MARKER, with the name read back from the
    project endpoint for that same id - the report counts it once under
    ``already_created``, and a second ``POST /bundles/create`` for the promoted key is a
    ``skipped``/``already_created`` carrying the SAME id rather than a second project over
    the same fifteen files. The idempotency of an ordinary low row is the pattern
    ``test_a_second_run_creates_nothing`` sets; what is new is that it now holds for a row
    whose assets are entirely a claimed child's.

    Mutation (run, and it moved): the same revert as the create witness -
    ``_promotion_would_double_own`` answering ``True`` - no longer reds THIS test, and
    the reason is the ticket's own point. Measured, the fall-through leaves the tower row
    answering ``(ready, None, 'low')`` with its containment set populated, so it creates
    once, answers its own project id and skips the second create exactly as pinned here.
    What that mutation DOES red is the four promoted-row witnesses that read the
    candidate's flags. The duplicate-project assertions below are still what a create door
    without the existing-project check would break; they are here because a promotion that
    hands the operator a row which creates twice is worse than one that creates never.
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
        "a promoted row answers the project it became - the answer a `pair_only` row "
        "could never give, because it handed the identity check an empty asset set"
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


def test_the_refused_endorphin_pile_reports_the_assets_its_key_holds(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-7: the refused pile counts and covers the files its key holds.

    The pile's VERDICT is pinned elsewhere and is not the point here - it stays
    ``blocked``/``archive_only`` because it holds two rows. The point is that the
    verdict is no longer a reason to skip the read: eight files sit under
    ``import/endorphin`` (the two children's six plus the pile's own loose model and
    render), the library scan registered all eight, and the refused row now reports
    them and covers itself with the largest registered image. Under the deleted
    short-circuit all four assertions below were a lookup that never happened, so
    the status was right and the read-out was invented.

    Mutation (run): restore ``if candidate.offers_archive_only: status,
    blocked_reason = BLOCKED, ARCHIVE_ONLY`` with the lookup in the ``else``, and
    THIS reds on two of them - measured ``assert 0 == 8`` for ``assets_total`` and
    ``assert None is not None`` for ``cover["asset_id"]`` - while the status and
    ``blocked_reason`` assertions stay green. That is the whole reason the test is
    written against the read: a shorter report that only pinned the status would
    have survived the line it is meant to forbid.
    """
    endorphin_pile(isolated_storage)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    files = files_under(isolated_storage, "import/endorphin")
    assert len(files) == 8, "the two children's six files plus the pile's own model and render"
    pile = row_for(report, "import/endorphin")
    assert (pile["status"], pile["blocked_reason"]) == ("blocked", "archive_only"), (
        "the verdict is unchanged: a pile that holds rows is still refused"
    )
    assert pile["assets_total"] == len(files) > 0, (
        "the refused row counts every registered file under its key - the answer a "
        "row that never asked the database could not give"
    )
    assert pile["cover"]["asset_id"] is not None, (
        "the cover is backed by a registered asset, not by the unregistered leg"
    )
    assert pile["cover"]["reason"] == "largest-image", (
        "and it won the registered-image leg like any other row's cover would"
    )


def test_a_pair_only_row_with_nothing_offered_under_it_is_ready_and_creates(
    client: TestClient, isolated_storage: Path
) -> None:
    """The secondary witness: ``pair_only`` is not a verdict, and here nothing is inside.

    ``import/G`` holds ONE child, ``part family one``, which the folder pass claimed
    as a ``low`` row - that claim is what makes ``import/G`` a claimed ancestor and
    what makes it ``pair_only`` once its verified twin is paired with it. The child
    does not survive the scan: it holds ``moai_export.zip``, so the archive-containment
    strip refuses it as a container and the nested-member pass then refuses the zip.
    ``import/G`` is therefore a ``pair_only`` row with NO offered row under it - it
    holds no other bundle - and the disk predicate answers it ``ready``, which is the
    whole ticket: the flag was never the reason the row could not be created.

    The shape is reached, not hand-built: the candidate list and the flags below are
    the detector's own answer, and the ledger says so too (the child
    ``member-of-bundle``, the zip ``nested-member``, the twin ``twin-consumed``). The
    create belt stays live on this ``low`` row and PASSES - the pair-only listing is
    the unclaimed full subtree, so the six registered files and the six paths its
    snapshot names are one set - and one project of the payload's three models and its
    preview comes out (four members, the ATTACH table of Mf-wsdo).

    Mutation (run): the deleted short-circuit answers this row ``blocked`` from the
    flag and this reds at the status assertion, measured ``('blocked',
    'archive_only') == ('ready', None)``. Lift that assertion and the read-out reds
    next - measured ``assert 0 == 6`` on ``assets_total`` - and lift that too and the
    create follows, measured ``(created, skipped, blocked, failed) == (0, 0, 1, 0)``
    against the pinned ``(1, 0, 0, 0)``: a row the flag refuses never reaches the
    project row at all.
    """
    child = isolated_storage / "import" / "G" / "part family one"
    write_file(child / "part0.stl", b"s" * 40)
    write_file(child / "part1.stl", b"s" * 41)
    write_file(child / "part2.stl", b"s" * 42)
    write_file(child / "preview.png", b"p" * 70)
    write_text(child / "notes.txt", "print at 0.2mm\n")
    write_zip(child / "moai_export.zip", thingiverse_export_entries())
    twin_archive_of(isolated_storage, "import/G", "import/G.zip")
    scan()
    headers = auth_header(client)

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)

    assert truncated is False
    assert [candidate.key for candidate in candidates] == ["import/G"], (
        "the child the folder pass claimed did not survive the scan, so the pair-only "
        "parent is the tree's only row - the shape the flag used to refuse by itself"
    )
    parent = candidates[0]
    assert (parent.pair_only, parent.is_archive, parent.confidence) == (True, False, "low"), (
        "the row IS a pair-only row: this is the shape the deleted line refused from "
        "the flag, not a weaker reading of something else"
    )
    rules = refusal_rules(isolated_storage)
    assert rules["import/G/part family one"] == bundle_service.REFUSAL_MEMBER_OF_BUNDLE
    assert rules["import/G/part family one/moai_export.zip"] == bundle_service.REFUSAL_NESTED_MEMBER
    assert rules["import/G.zip"] == bundle_service.REFUSAL_TWIN_CONSUMED

    row = row_for(detect(client, headers), "import/G")

    assert (row["status"], row["blocked_reason"]) == ("ready", None), (
        "no offered row under it, a resolved folder and its own models: the same "
        "predicate every other row is judged by"
    )
    assert row["assets_total"] == len(files_under(isolated_storage, "import/G")), (
        "and it reports all six files under its key, the zip included"
    )

    created = create(client, headers, [{"key": "import/G"}])

    assert (created["created"], created["skipped"], created["blocked"], created["failed"]) == (
        1,
        0,
        0,
        0,
    ), "a pair-only row with nothing offered inside it creates exactly one project"
    assert len(project_listing(client, headers)) == 1
    assert created["results"][0]["assets_added"] == 6, (
        "every registered file under the key that the pair owns: the three models of the "
        "payload, ``preview.png``, ``notes.txt`` and the nested zip (Mf-7oow)"
    )


def test_the_pile_holds_the_rows_that_refuse_it_and_claims_none_of_their_models(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-4 and the container leg, on the same ``import/Pile`` tree.

    Three facts, all read off the report and the create pass:

    * the two children are rows of their own INSIDE the pile's key, each ``ready``,
      each owning its own single model;
    * the pile is refused because it HOLDS those two rows - it is an inferred
      (``low``) row, so the container leg applies to it - and its own claimed model
      set is the loose ``loose.stl`` and nothing else. That is observed, not
      asserted: a hand-made project holding the two children's models owns the two
      children and does NOT own the pile, while a hand-made project holding only the
      pile's own model DOES. Which half of the change earns that answer is worth
      saying exactly: the pair-only listing is the post-claim view, which the folder
      pass had already stripped of both children, so the claim map does no work on
      THIS tree (measured: dropping the filter, ``mine = models``, leaves this test
      green). ``test_a_parent_row_never_answers_a_project_built_only_from_its_childs_models``
      is the witness for the filter, on the tree where a parent's listing IS its
      child's files;
    * the pile creates nothing.

    Mutation (run, both directions): dropping the container leg
    (``and any(_strictly_inside_any(other, {key}) for other in offered_dirs)``) makes
    ``import/Pile`` ``ready`` and this reds at its status assertion, with
    ``test_service_a_pile_of_bundles_and_a_zip_is_still_not_a_bundle`` red beside it;
    restoring the deleted short-circuit reds the PILE'S ``existing_project_id``
    assertion here - measured ``assert None == <the pile project's id>``, with the two
    child assertions green, because a child is not an archive-only row - since a row
    that never looked up its assets hands ``find_existing_project`` nothing to answer
    with.
    """
    loose_stl, loose_png = STL_BYTES, b"p" * 70
    write_file(isolated_storage / "import" / "Pile" / "loose.stl", loose_stl)
    write_file(isolated_storage / "import" / "Pile" / "render.png", loose_png)
    for name, model in (("Alpha", "a.stl"), ("Beta", "b.stl")):
        write_file(isolated_storage / "import" / "Pile" / name / model, b"s" * 41)
        write_file(isolated_storage / "import" / "Pile" / name / f"{name.lower()}.png", b"p" * 50)
        write_text(isolated_storage / "import" / "Pile" / name / "notes.txt", "print at 0.2mm")
    write_zip(
        isolated_storage / "import" / "Pile Download.zip",
        {"Pile/loose.stl": loose_stl, "Pile/render.png": loose_png},
    )
    scan()
    children_project = hand_made_project(
        "Project of the two children", ["import/Pile/Alpha/a.stl", "import/Pile/Beta/b.stl"]
    )
    pile_project = hand_made_project("Project of the pile's own part", ["import/Pile/loose.stl"])
    headers = auth_header(client)

    report = detect(client, headers)

    pile = row_for(report, "import/Pile")
    assert (pile["status"], pile["blocked_reason"]) == ("blocked", "archive_only"), (
        "the container answer, and it is the CONTAINER leg that gives it: the pile is "
        "low, and it holds two offered directories"
    )
    for name in ("Alpha", "Beta"):
        child = row_for(report, f"import/Pile/{name}")
        assert (child["status"], child["blocked_reason"]) == ("ready", None), (
            "the row inside the pile is what makes the pile a pile, and it is creatable"
        )
        assert child["existing_project_id"] == children_project, (
            "the child owns its own model: the project holding that one file is its owner"
        )
        assert child["existing_project_match"] == "assets"
    assert pile["existing_project_id"] == pile_project, (
        "the pile's containment is its OWN model only: the project holding just the "
        "children's two models does not own it, which is AC-4 read through the report"
    )
    assert pile["existing_project_match"] == "assets"

    created = create(client, headers, [{"key": key} for key in [row["key"] for row in report["bundles"]]])

    assert (created["created"], created["skipped"], created["blocked"], created["failed"]) == (
        0,
        2,
        1,
        0,
    ), (
        "the two children SKIP - each is already the hand-made project above, which is the "
        "containment answer working - and the pile is the one blocked row"
    )
    assert [result["key"] for result in created["results"] if result["status"] == "blocked"] == [
        "import/Pile"
    ]
    names = {project["name"].casefold() for project in project_listing(client, headers).values()}
    assert "pile" not in names, "the pile stays refused, read-out and all"

    # The counter this ticket moves, recorded where the behaviour is pinned: a
    # refused row that now reports its own assets can be FOUND by a project a human
    # made over those assets, so `already_created` counts it while it stays
    # `blocked`. Measured over the Mf-quv4 corpus: `already_created` 3 -> 4,
    # `blocked` 9 -> 8, `ready` 14 -> 15. It is a consequence, not a bug - but the
    # web queue hides any row carrying `existing_project_id` behind its "show
    # created" toggle (`ui/review/bundleRows.ts` `matchesVisibility`), so a row that
    # is refused AND still needs its Extract door now leaves the default REVIEW
    # queue. That visibility rule is a web ticket, not this one.
    report_after = detect(client, headers, include_dismissed=True)
    assert report_after["counts"]["already_created"] == 3, (
        "the two children, each owned by the hand-made project above, and the pile - "
        "which only counts once the refused row is allowed to report its own assets"
    )
    assert row_for(report_after, "import/Pile")["existing_project_id"] == pile_project


def test_a_parent_row_never_answers_a_project_built_only_from_its_childs_models(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-4, on the shape where it actually bites: the parent's listing is the child's.

    ``import/Q`` owns nothing - its only content is the claimed HIGH export directory
    ``import/Q/Moai_statue`` - so the ``folded`` fallback builds the parent's listing
    off the CHILD'S files, and every model under the parent's prefix is a file the
    deeper row owns. A hand-made project holding exactly those three models therefore
    owns the CHILD row and must own nothing at the parent: the parent's containment is
    the models ``_claim_owners`` leaves it, which here is none, and it answers no
    project at all.

    This is the witness for the claim filter itself. On the pile the parent's own
    listing already excludes its children, so an unfiltered containment set is
    indistinguishable from a filtered one; here the two differ, and measured, dropping
    the filter (``mine = models``) makes ``import/Q`` answer the child's project with
    ``existing_project_match == "assets"`` where this pins ``None``. It also reds
    ``test_service_a_hand_made_project_owns_a_bundle_by_its_files``-style ownership
    assertions elsewhere only when a row's listing names a deeper row's files, which
    is why the shape, not just the assertion, is the point.
    """
    write_zip(isolated_storage / STRONG_CHILD_ZIP, thingiverse_export_entries())
    write_file(isolated_storage / STRONG_PARENT_DIR / "a.stl", STL_BYTES)
    twin_archive_of(isolated_storage, STRONG_PARENT_DIR, STRONG_PARENT_ZIP)
    thingiverse_export_directory(isolated_storage, CLAIMED_CHILD_DIR)
    twin_archive_of(isolated_storage, CLAIMED_PARENT_DIR, CLAIMED_PARENT_ZIP)
    scan()
    child_models = [f"{CLAIMED_CHILD_DIR}/part{index}.stl" for index in range(3)]
    project_id = hand_made_project("A project over the child's three parts", child_models)
    headers = auth_header(client)

    report = detect(client, headers)

    child = row_for(report, CLAIMED_CHILD_DIR)
    assert (child["status"], child["blocked_reason"]) == ("ready", None)
    assert child["existing_project_id"] == project_id, (
        "the project holds every model the child's own listing names, so it owns the child"
    )
    assert child["existing_project_match"] == "assets"
    parent = row_for(report, CLAIMED_PARENT_DIR)
    assert (parent["status"], parent["blocked_reason"]) == ("blocked", "archive_only")
    assert parent["assets_total"] == 6, (
        "the pair-only row reports the six files under its key - all of them the child's"
    )
    assert parent["existing_project_id"] is None, (
        "and it claims none of them: the project over the child's models does not own "
        "the parent, because the parent owns no model of its own"
    )
    assert parent["existing_project_match"] is None


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
    """The compound that absorbed a group is not cancelled by that group's subtree.

    The former witness re-offered the excluded group as a row and proved the two rows
    were inert to one another. Mf-z643 prunes everything inside the created project
    key, so the group has no row at all - but the half that MATTERED is still
    asserted: the compound row is not cancelled by the (pruned) group it absorbed,
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
    refused pile and the lone archive (``archive_only``, the arm the Extract door
    needs), a high bundle whose folder row never got a ``storage_path``
    (``unresolved_directory``), a registered folder with nothing registered under it
    (``not_scanned``) and the ready rows beside them (``None``).

    Mutation: any new ``blocked_reason`` literal in ``_report_row`` - a
    ``contains_bundles`` or a ``claimed_elsewhere`` on ``blocked``, say - reds the
    set equality here AND the per-row assertion, and ``test_bundle_serialize.py``'s
    "no refusal names an offered path" reds if the predicate ever answers a row out
    of existence instead of answering it ``blocked``.

    The four arms, measured over this corpus: the pile and the archive answer
    ``archive_only``, ``Unresolved Thing`` (a folder row with no ``storage_path``,
    the flattened migration's shape) answers ``unresolved_directory``, ``Not Scanned
    Thing`` (a registered folder with nothing registered under it) answers
    ``not_scanned``, and every other row in the report answers ``ready`` with
    ``None``.
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
        ("import/endorphin", "blocked", "archive_only"),
        ("import/lone_export.zip", "blocked", "archive_only"),
        ("Unresolved Thing", "blocked", "unresolved_directory"),
        ("Not Scanned Thing", "blocked", "not_scanned"),
    }, "all four arms answered by one corpus, in one report"


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


def _callee_name_of(node: ast.AST) -> Optional[str]:
    """``_callee_name`` for any node: the function one CALL names, else ``None``.

    So a caller can filter an ``ast.walk`` without pre-selecting ``ast.Call``.
    """
    return _callee_name(node) if isinstance(node, ast.Call) else None


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


def test_an_obj_file_is_a_member_with_role_other_and_is_still_not_a_model(
    client: TestClient, isolated_storage: Path
) -> None:
    """The ``.obj`` ruling, measured: registrable, ``other`` to the detector, a MEMBER.

    ``file_utils.MODEL_SUFFIXES`` calls ``.obj`` a model and ``bundle_detect`` does not,
    so the choice of table decides whether a row counts one. The row does not:
    ``classify_entry("part.obj")`` answers ``other``, so ``other_count`` counts it and
    ``model_count`` does not. And since Mf-7oow the created project HOLDS it anyway,
    with that very same role - membership is every registered file under the key and
    the role is the detector's answer, so the project and the row agree instead of
    disagreeing.

    Mutation (run, then restored): filter ``_members_of`` with
    ``file_utils.MODEL_SUFFIXES`` (or drop any role from the set) and the member count
    and the role map below red; make the row's ``model_count`` read the member count
    and the ``(1, 1, 1)`` block reds.
    """
    write_file(isolated_storage / "Obj Thing" / "part.stl", STL_BYTES)
    write_file(isolated_storage / "Obj Thing" / "part.obj", b"v\n")
    write_file(isolated_storage / "Obj Thing" / "cover.jpg", b"j" * 50)
    write_text(isolated_storage / "Obj Thing" / "notes.txt", "print at 0.2mm\n")
    scan()
    headers = auth_header(client)

    # The two tables disagree about `.obj` and that is the point: `file_utils`' (the web
    # mirror, the archive door's cover rule) calls it a model, the detector's classifier
    # does not. The ROLE is the classifier's answer, so it is `other`.
    assert ".obj" in file_utils.MODEL_SUFFIXES
    assert bundle_detect.classify_entry("part.obj", 2) == bundle_detect.OTHER
    row = row_for(detect(client, headers), "Obj Thing")

    assert (row["model_count"], row["image_count"], row["other_count"]) == (1, 1, 1), (
        "the row counts the STL and the jpg, and counts the .obj as OTHER - which is the "
        "whole reason the role is the detector's answer and never a member table's"
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
        "part.stl": bundle_detect.MODEL,
        "part.obj": bundle_detect.OTHER,
        "cover.jpg": bundle_detect.IMAGE,
        "notes.txt": bundle_detect.TEXT,
    }, "the project holds what the row counted, role for role"


def test_the_member_roles_are_the_classifiers_own(
    client: TestClient, isolated_storage: Path
) -> None:
    """The role table, measured on a mixed folder: `_members_of` asks ONE classifier.

    Membership is every registered asset under the key; the ROLE is
    ``bundle_detect.classify_entry``'s answer and nothing else. Pinned by NAME, so a
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
        "part.stl": bundle_detect.MODEL,
        "part.3mf": bundle_detect.MODEL,
        "part.step": bundle_detect.MODEL,
        "part.stp": bundle_detect.MODEL,
        "part.lbrn": bundle_detect.MODEL,
        "part.lbrn2": bundle_detect.MODEL,
        "render.png": bundle_detect.IMAGE,
        "render.jpg": bundle_detect.IMAGE,
        "render.jpeg": bundle_detect.IMAGE,
        "render.webp": bundle_detect.IMAGE,
        "render.bmp": bundle_detect.IMAGE,
        "docs.pdf": bundle_detect.TEXT,
        "notes.txt": bundle_detect.TEXT,
        "notes.md": bundle_detect.TEXT,
        "page.html": bundle_detect.TEXT,
        "page.htm": bundle_detect.TEXT,
        "notes.rtf": bundle_detect.TEXT,
        "part.stlbak": bundle_detect.OTHER,
        "part.3dm": bundle_detect.OTHER,
        "part.scad": bundle_detect.OTHER,
        "part.gcode": bundle_detect.OTHER,
        "mystery.qqq": bundle_detect.OTHER,
        "deliverable.zip": bundle_detect.ARCHIVE,
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
        bundle_detect.MODEL: 6,
        bundle_detect.IMAGE: 5,
        bundle_detect.TEXT: 6,
        bundle_detect.OTHER: 5,
        bundle_detect.ARCHIVE: 1,
    }, "the role tallies of the mixed folder above - one per member, no member twice"
    for role in (bundle_detect.MODEL, bundle_detect.IMAGE, bundle_detect.TEXT, bundle_detect.OTHER, bundle_detect.ARCHIVE):
        assert [entry.rel_path for entry in member_set.by_role(role)] == sorted(
            name for name, found in roles.items() if found == role
        ), f"`by_role({role!r})` is the only view on the set, and it keeps input order"
    assert [entry.rel_path for entry in member_set.by_role(bundle_detect.MODEL)][:2] == [
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

    * no module carries the name, ``bundle_service`` and ``bundle_detect`` included;
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
    for module in (bundle_service, bundle_detect, project_create_service):
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
    the one classifier is asked once per asset and nowhere outside ``bundle_detect`` -
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
    """Mf-quv4's hand-off, closed: the door that ATTACHES reads the map REPORTS read.

    Mf-quv4 gave the report a one-owner map (``_claim_owners``) and left ``_create_one``
    building its attach set from ``_own_paths``, so a container could be shown owning
    what a deeper child owns while the click handed those same files to the container.
    Both sides now read through one expression (``_own_assets`` / ``_claimed_assets``).
    The suite was not blind to that by accident, but it never discriminated it: a probe
    over the whole 3012-test run measured ``_own_paths`` and ``_claim_owners`` disagreeing
    ZERO times, which is why "they agree today" needed a witness rather than a belief.

    The probe is the ``claims`` argument, and this witness now asks TWO questions of it.

    * One map, not a second opinion. Serve every create ``claims=None`` - the way the
      inert re-offered row is served - and the create still answers ``created`` and
      still attaches exactly the right files on this corpus. What reds is the assertion
      that the door consulted a map at all: ``tower_asks[0] is not None``, the FIRST
      thing to go red - measured, everything before it passes (both row statuses, the
      three ``created == 1`` counts, the spy's own non-empty result and the
      one-build-per-create count). That is the honest form of "the suite cannot see the
      difference today".
    * ONE ``None``, in both doors. ``create_bundles`` serves a key ``claims=None``
      exactly when that key is an absorbed group the walk re-offered as its own row
      (``claims=None if key in absorbed_keys else claims``), and ``_report_from_scan``
      hands ``_report_row`` the same ``None`` for those same rows. Asserted here PER
      KEY: the keys the create asked without a map are exactly the absorbed keys it
      actually created, and every key it asked about that is not an absorbed re-offer
      was asked WITH the map.

    The second half became load-bearing with Mf-wsdo's pre-write cover guard. Pre-ticket,
    a row whose cover sat outside the set its create attaches was a silent cosmetic
    defect - a set-and-dangling ``cover_asset_id`` that nothing compared; post-ticket
    ``create_project_from_members`` refuses that create BEFORE the write, so the same
    divergence surfaces as a hard ``failed`` the operator cannot retry. One ``None`` in
    both doors is what keeps a row and its create from answering ownership under two
    different rules. Drift them one way - hand the re-offered group the real map the
    report withholds from it - and its create answers ``failed``/``no member files
    resolved``, because the map gives every one of its models to the compound parent
    (measured, in the Mutation note below). Drift them the other way - give an ordinary
    row the inert answer - and its create owns whatever its own snapshot named, which is
    the ``Mf-quv4`` bug this witness exists to keep shut. Either way the face the card
    advertised stops being one the project is allowed to wear. The xbox compound below is
    what makes that half observable: without a re-offered group in the pass, the inert
    side of the equality is a comparison between two empty sets.

    Mutations (both measured on this witness, then restored, production hashes re-verified
    against the pristine copies). ``claims=None`` for EVERY item in ``create_bundles`` -
    the inert answer handed to the ordinary rows too - reds exactly where it is described
    above. ``claims=claims`` for every item - the real map handed to the re-offered group
    too - reds EARLIER and harder: the mount create answers ``failed`` with ``reason ==
    "no member files resolved"``, because the map gives every bracket to the compound
    parent and the row that was served a map is left owning nothing it can attach. The two
    directions are not symmetric in severity, which is exactly why this asserts an equality
    rather than one half of it.
    """
    live_tower_extraction(isolated_storage)
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    insert_folder_row(XBOX_GROUP, mount_key())
    scan()
    headers = auth_header(client)
    assert row_for(detect(client, headers), TOWER_DIR)["status"] == "ready"
    assert row_for(detect(client, headers), f"import/{XBOX}")["status"] == "ready", (
        "the compound has to be creatable, or the re-offer below is never created and "
        "the symmetry assertion compares two empty sets"
    )

    seen: List[tuple] = []
    creating = {"inside": False}
    real = bundle_service._claimed_assets
    real_create_one = bundle_service._create_one

    def spy(assets, *, key, claims):
        seen.append((key, claims, creating["inside"]))
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
    # The exclusion is what re-offers the foreign group as a row of its own, and the
    # create of THAT key is the call that reaches the create door with ``claims=None``.
    # Established fixture, established helper - `write_xbox360` and `create_for_exclusion`,
    # the same pair the mutual-inert witness runs.
    excluded = create_for_exclusion(client, headers, [XBOX_GROUP])
    assert excluded["created"] == 1
    # The excluded group is inside the created project key, so there is no re-offered
    # row to create any more (Mf-z643) - it answers `unknown bundle` and is not an
    # inert ask because it never reaches the door at all.
    reoffered = create(client, headers, [{"key": mount_key(), "name": "the excluded mount"}])
    assert reoffered["failed"] == 1
    assert reoffered["results"][0]["reason"] == "unknown bundle"

    # `create_bundles` re-derives the report before it creates, so every key is asked
    # about by `_report_row` too; only the calls made INSIDE the create are this
    # witness. Serve the create `claims=None` - the way the inert re-offered row is
    # served - and `tower_asks[0] is not None` below is the FIRST thing to red:
    # measured, everything above it passes (both row statuses, the three
    # `created == 1` counts, this assertion and the one-build-per-create count).
    from_create = [(key, claims) for key, claims, inside in seen if inside]
    assert from_create, "the create door built a member set without consulting ownership"
    tower_asks = [claims for key, claims in from_create if key == TOWER_DIR]
    assert len(tower_asks) == 1, "one owned-set build per create - no second opinion"
    assert tower_asks[0] is not None, (
        "the create door attached everything its snapshot named, ignoring the map that "
        "decided which row owns each model"
    )

    # The symmetry half. `absorbed_keys` is read from the walk itself - the same
    # expression `create_bundles` uses - so it is not derived from what the door did.
    # The xbox group is inside the created project key now, so nothing is absorbed
    # and no create may be served the inert answer (Mf-z643).
    absorbed_keys = {
        candidate.key
        for candidate in bundle_service.scan_bundles(
            bundle_service.STORAGE, excluded_keys=bundle_service._excluded_keys("")
        ).absorbed
    }
    assert absorbed_keys == set(), "the pruned subtree leaves nothing to absorb"
    asked_without_a_map = {key for key, claims in from_create if claims is None}
    created_keys = {
        result["key"]
        for result in (*created["results"], *excluded["results"], *reoffered["results"])
        if result["status"] == "created"
    }
    assert asked_without_a_map == {key for key in created_keys if key in absorbed_keys}, (
        "the create door was served the inert ownership answer for a row the report does "
        "not serve it to, or was denied it for a row it must be served to"
    )
    assert asked_without_a_map == set(), (
        "no absorbed re-offer exists here, so every create must be asked WITH the map"
    )
    for key, claims in from_create:
        if key in absorbed_keys:
            continue
        assert claims is not None, (
            f"{key}: an ordinary row - not an absorbed re-offer - built its member set "
            "from its own snapshot instead of from the report-wide one-owner map"
        )
    # And what the tower create attached is the files under the one key the report
    # credited: its models AND its previews (the ATTACH table, Mf-wsdo) - not the
    # ``.txt``, not the twin's contents, and not one file a deeper row owns.
    assert project_storage_paths(created["results"][0]["project_id"]) == registered_asset_paths(
        TOWER_DIR
    )


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
            role=bundle_detect.MODEL,
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


def test_every_row_carries_the_recommended_match_first_and_the_manual_scope(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-1: ``matches[0]`` is the row's own answer, the manual scope is on offer.

    Mutation: build the match list from a different snapshot (or forget to put the
    recommended facts first) and the three equality assertions red for some row;
    a manual row recommends itself, so its manual entry is first and carries the
    refusal word as its note. Since Mf-yitm a row may also carry the engine's
    dissent, which is the only match that is NOT creatable: the note invariant is
    therefore two rules, not one - a creatable offer's note is a refusal word or
    nothing, a non-creatable one always says why in the engine's own sentence.
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
        if row["metadata_shape"] == "manual":
            # A manual row recommends the manual entry, whose rule is the refusing
            # word the scope was built from - the one shape where rule != detector.
            assert matches[0]["rule"] == matches[0]["note"], row["key"]
        else:
            assert matches[0]["rule"] == row["detector"], row["key"]
        assert matches[0]["confidence"] == row["confidence"], row["key"]
        assert matches[0]["groups"] == row["groups"], row["key"]
        assert len({match["shape"] for match in matches}) == len(matches), row["key"]
        manual = [match for match in matches if match["shape"] == "manual"]
        assert len(manual) == 1, row["key"]
        if row["metadata_shape"] != "manual":
            assert matches[-1] is manual[0], f"{row['key']}: manual is always the last offer"
        else:
            assert matches[0] is manual[0], "a manual row recommends itself"
            assert manual[0]["note"] in bundle_service.REFUSAL_RULES
        for match in matches:
            # Every match says whether the server can enact it. A creatable shape
            # either carries no note or the refusal word the manual offer was built
            # from; a non-creatable one always carries a note, and that note is the
            # engine's consequence sentence rather than any refusal vocabulary.
            assert isinstance(match["creatable"], bool), row["key"]
            if match["creatable"]:
                assert match["note"] is None or match["note"] in bundle_service.REFUSAL_RULES, row["key"]
            else:
                assert match["note"], f"{row['key']}: a non-creatable offer must say why"
                assert match["note"] not in bundle_service.REFUSAL_RULES, (
                    f"{row['key']}: the engine's note is its own sentence, never a refusal word"
                )
                assert match["note"].startswith("needs arranging first: "), row["key"]
                assert match["model_count"] == row["model_count"], (
                    f"{row['key']}: the engine entry counts the folder this row counted"
                )


def test_service_the_engine_is_consulted_per_row_and_only_dissents_where_it_must(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-3/AC-4: one folder the engine agrees with, one it reads as several projects.

    (i) `import/Agreeing` is one product in one folder: the engine's own answer is
    `one-project` and no split, so the row is byte-identical to before this ticket.
    (ii) `import/Collection` holds three top folders that each ship their own
    document: the engine recommends `split-top-folders` (60) while the detector
    recommends one `generic` bundle, so the row carries that dissent immediately
    before the manual entry, marked `creatable=False`.

    Mutation: serve the engine's offers on every row and (i) gains a shape; keep the
    dissent out of the row and (ii)'s two ordering assertions red.
    """
    agreeing_bundle(isolated_storage)
    collection_bundle(isolated_storage)
    scan()

    agreeing = service_row(AGREEING_KEY)

    assert agreeing["metadata_shape"] == "generic"
    assert [match["shape"] for match in agreeing["matches"]] == ["generic", "title-name", "manual"]
    assert all(match["creatable"] is True for match in agreeing["matches"])
    assert not any(match["rule"].startswith("engine:") for match in agreeing["matches"]), (
        "the engine was consulted and had nothing to add, so nothing was added"
    )

    collection = service_row(COLLECTION_KEY)

    assert collection["metadata_shape"] == "generic"
    shapes = [match["shape"] for match in collection["matches"]]
    assert shapes == ["generic", "title-name", "split-top-folders", "manual"], (
        "the dissent sits between the detector's offers and the manual scope"
    )
    dissent = collection["matches"][-2]
    assert dissent["rule"] == "engine:split-top-folders"
    assert dissent["creatable"] is False
    assert dissent["confidence"] == "high", "the engine's top option here IS the split"
    assert dissent["note"].startswith("needs arranging first: ")
    assert dissent["model_count"] == collection["model_count"] == 10
    assert collection["matches"][-1]["shape"] == "manual"
    assert collection["matches"][-1]["creatable"] is True


def test_a_refused_vault_is_one_manual_row_and_creates_a_manual_project(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-2/AC-3: the dead end dies - a refused tree is one scope, and it creates.

    The vault refuses under R3 (no model at its own depth, two unnamed
    model-bearing products), the ledger keeps naming it with that same rule, and
    the create is the operator's choice: ``shape=manual`` in the marker, the
    refusing word as its rule, and ``detector`` left byte-identical.

    Mutation: make the create gate on ``status`` again and the create answers
    ``blocked`` instead of writing the project.
    """
    vault_tree(isolated_storage)
    generic_bundle(isolated_storage / "import", "Side Bundle")
    scan()
    headers = auth_header(client)

    report = detect(client, headers, limit=1000)

    manual_rows = [row for row in report["bundles"] if row["detector"] == "manual"]
    assert [row["key"] for row in manual_rows] == ["import/Vault"], "one scope per maximal refused root"
    row = manual_rows[0]
    assert row["metadata_shape"] == "manual"
    manual = next(match for match in row["matches"] if match["shape"] == "manual")
    assert manual["note"] == bundle_detect.REFUSAL_R3_NO_OWN_DEPTH_MODEL
    assert manual["rule"] == bundle_detect.REFUSAL_R3_NO_OWN_DEPTH_MODEL
    assert refusal_ledger(report)["import/Vault"] == bundle_detect.REFUSAL_R3_NO_OWN_DEPTH_MODEL, (
        "the ledger still names the path with the rule that refused it"
    )
    assert all(
        (match["note"] is None or match["note"] in bundle_service.REFUSAL_RULES)
        if match["creatable"]
        else bool(match["note"])
        for row in report["bundles"]
        for match in row["matches"]
    ), "a creatable offer's note is a refusal word; a non-creatable one always says why"

    created = create(client, headers, [{"key": "import/Vault"}])

    assert created["created"] == 1
    result = created["results"][0]
    marker = project_marker(result["project_id"])
    assert marker["shape"] == "manual"
    assert marker["rule"] == bundle_detect.REFUSAL_R3_NO_OWN_DEPTH_MODEL
    assert marker["detector"] == "manual"
    assert "groups" not in marker, "manual is a scope, not a compound"
    assert "cover" not in marker
    assert result["assets_added"] == 5, "four models and the preview"


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


def test_a_create_shape_of_manual_writes_a_manual_marker(client: TestClient, isolated_storage: Path) -> None:
    """AC-5: the operator's shape decides the marker, and it must be on offer.

    ``shape=manual`` on a generic row creates one project whose marker says
    ``shape=manual`` and whose rule is the recommended shape's rule; an unoffered
    shape is ``failed``/``unknown shape``; and a create with no shape records the
    row's recommended shape exactly as before this field existed.

    Mutation: ignore ``item.shape`` and every ``shape=manual`` assertion reds;
    accept any shape and the ``unknown shape`` assertion reds.
    """
    generic_bundle(isolated_storage, "Generic Thing")
    scan()
    row = service_row("Generic Thing")
    assert row["metadata_shape"] == "generic"

    manual = bundle_service.create_bundles(
        [bundle_service.BundleCreateItem(key="Generic Thing", shape="manual")], audit_ctx()
    )
    assert manual["created"] == 1
    marker = project_marker(manual["results"][0]["project_id"])
    assert marker["shape"] == "manual"
    # The manual entry's rule is the manual detector when nothing refused the row:
    # there is no refusal word to carry, so the marker says where the scope came from.
    assert marker["rule"] == "manual"
    assert marker["detector"] == "generic-bundle"

    bogus = bundle_service.create_bundles(
        [bundle_service.BundleCreateItem(key="Generic Thing", shape="not-a-shape")], audit_ctx()
    )
    assert bogus["failed"] == 1
    assert bogus["results"][0]["reason"] == "unknown shape"


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
    assert row["metadata_shape"] == "compound"

    created = bundle_service.create_bundles(
        [bundle_service.BundleCreateItem(key=f"import/{STAGETOP}")], audit_ctx()
    )

    assert created["created"] == 1
    marker = project_marker(created["results"][0]["project_id"])
    assert marker["shape"] == row["metadata_shape"] == "compound"
    assert marker["rule"] == row["detector"] == "compound-bundle"
    assert [group["name"] for group in marker["groups"]] == [
        group["name"] for group in row["groups"]
    ], "the compound marker still names the row's groups, in order"


def test_a_manual_create_on_a_compound_row_has_no_groups_block(client: TestClient, isolated_storage: Path) -> None:
    """Choosing ``shape=manual`` on a compound row creates a SCOPE, not a compound.

    Mutation: key the compound marker block on the candidate instead of the chosen
    shape and the manual marker gains a ``groups``/``cover`` block it cannot
    justify.
    """
    write_stagetop(isolated_storage)
    insert_folder_row(STAGETOP, f"import/{STAGETOP}")
    scan()

    created = bundle_service.create_bundles(
        [bundle_service.BundleCreateItem(key=f"import/{STAGETOP}", shape="manual")], audit_ctx()
    )

    assert created["created"] == 1
    marker = project_marker(created["results"][0]["project_id"])
    assert marker["shape"] == "manual"
    assert marker["rule"] == "manual", "the manual entry's own rule, not the recommended one"
    assert "groups" not in marker
    assert "cover" not in marker
