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
import json
import re
import shutil
import socket
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

import asset_file_service
import asset_service
import audit
import bundle_detect
import bundle_enrich
import bundle_service
import db
import import_resolvers
import library_scan
import main
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
    assert len(project_asset_ids(client, headers, project_id)) == 3
    assert created["results"][0]["assets_added"] == 3


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
    assert len(project_asset_ids(client, headers, project_id)) == 3, "the archive attaches nothing"

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
        extra = session.exec(select(Asset).where(Asset.storage_path == f"{MOAI}/images/0.jpg")).one()
        extra_id = extra.id
    assert client.post(f"/project/{project_id}/assets", json={"asset_ids": [extra_id]}, headers=headers).status_code == 200

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
    assert member_count(project_id) == 4


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
    assert blocked["blocked"] == 1
    assert blocked["results"][0]["reason"] == "unresolved_directory"
    bulk = create(client, headers, [{"key": "Moai", "name_only": True}])
    assert bulk["created"] == 0, "a bulk name-only create must skip a blocked bundle"
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
        loose = session.exec(select(Asset).where(Asset.storage_path == "Unsorted/loose.stl")).one()
    loose_id = loose.id

    created = create(client, headers, [{"key": "Unsorted", "name_only": True}])

    assert created["created"] == 1
    project_id = created["results"][0]["project_id"]
    assert project_asset_ids(client, headers, project_id) == [loose_id], "never a child bundle's models"


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
        expected = {
            asset.id for asset in session.exec(select(Asset).where(Asset.storage_path.in_(model_paths))).all()
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

    assert [row["key"] for row in report["bundles"]] == ["Inbox/Vendor.zip"]


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
    assert len(project_asset_ids(client, headers, parent_project)) == 1
    assert len(project_asset_ids(client, headers, child_project)) == 3


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
    assert "import/residual" not in keys, "a model-only directory is not a bundle"
    moai_row = row_for(report, f"import/{MOAI}")
    assert moai_row["confidence"] == "high"
    assert moai_row["model_count"] == 3, "models at depth 1 under files/ are still counted"

    created = create(client, headers, [{"key": row["key"]} for row in report["bundles"]])

    assert created["created"] == len(report["bundles"])
    assert "import" not in {project["name"] for project in project_listing(client, headers).values()}
    members: List[str] = []
    for row, result in zip(report["bundles"], created["results"], strict=True):
        assert member_count(result["project_id"]) == row["model_count"], "one project, own models only"
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
    assert first["results"][0]["assets_added"] == 3
    with Session(engine) as session:
        project = project_service.get_project(session, project_id)
        project_service.update_project(session, project, {"name"}, "Edited Moai", None, None, audit_ctx=None)
    with Session(engine) as session:
        extra = session.exec(select(Asset).where(Asset.storage_path == f"{MOAI}/images/0.jpg")).one()
        extra_id = extra.id
    project_service.add_assets(project_id, [extra_id])

    second = bundle_service.create_bundles([bundle_service.BundleCreateItem(key=MOAI)], ctx)

    assert second["created"] == 0
    assert second["skipped"] == 1
    assert second["results"][0]["reason"] == "already_created"
    with Session(engine) as session:
        projects = session.exec(select(Project)).all()
        assert len(projects) == 1
        assert projects[0].name == "Edited Moai"
    assert member_count(project_id) == 4
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
    monkeypatch.setattr(project_service, "create_project", refuse)

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
    assert member_count(project_id) == 1


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
    assert member_count(project_id) == 1


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
    assert len(project_asset_ids(client, headers, project_id)) == 12, (
        "the directory's own models, attached from the store like any LOW bundle"
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
    assert len(project_asset_ids(client, headers, project_id)) == 2, "the archive attaches nothing"


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

    assert bundle_service.build_report()["bundles"] == []
    assert bundle_service.iter_bundle_candidates(isolated_storage) == ([], False)

    created = create(client, headers, [{"key": f"import/{NO_TITLE_ROOT}"}])

    assert created["created"] == 0
    assert created["failed"] == 1
    assert created["results"][0]["reason"] == "unknown bundle"
    assert project_listing(client, headers) == {}


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
    assert len(project_asset_ids(client, headers, project_id)) == 2, (
        "the parent's own model and the unclaimed extraction's model: one project, never two"
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
        (SNVB623, 2),
        (SNVD623, 2),
        (CARD_BOX, 1),
        (CINDERWING, 2),
    ]
    by_key = {candidate.key: candidate for candidate in candidates}
    assert sorted(by_key) == sorted(f"import/{name}" for name, _models in sets), (
        "four promoted directories and nothing else"
    )
    for name, _models in sets:
        candidate = by_key[f"import/{name}"]
        assert candidate.archive_owned is True, f"the verified stem pairing owns {name}"
        assert candidate.entry_root is None, "the stem route names no entry root"
        assert candidate.archive_path == f"import/{name}.zip"

    scan()
    headers = auth_header(client)
    report = detect(client, headers)

    assert sorted(row["key"] for row in report["bundles"]) == sorted(by_key)
    for name, _models in sets:
        row = row_for(report, f"import/{name}")
        assert row["confidence"] == "low", f"{name} is a shape inference"
        assert row["detector"] == "generic-bundle"
        assert row["status"] == "ready"
        assert row["archive"] == {"rel_path": f"import/{name}.zip", "twin_verified": True}

    created = create(client, headers, [{"key": f"import/{name}"} for name, _models in sets])

    assert created["created"] == 4
    assert created["blocked"] == 0
    assert created["failed"] == 0
    assert len(project_listing(client, headers)) == 4
    for result, (name, models) in zip(created["results"], sets, strict=True):
        assert member_count(result["project_id"]) == models, (
            f"{name} attaches only its own models; the archive attaches nothing"
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
    assert [candidate.key for candidate in candidates] == ["import/RepRap-MultiFilament/Alpha"], (
        "the pile is refused by own-depth/cardinality; only Alpha's name reads as a title"
    )
    report = bundle_service.build_report()
    assert [row["key"] for row in report["bundles"]] == ["import/RepRap-MultiFilament/Alpha"]
    alpha = row_for(report, "import/RepRap-MultiFilament/Alpha")
    assert (alpha["detector"], alpha["confidence"]) == ("title-name", "low")


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
    assert candidates == [], "a size or name mismatch pairs nothing"
    assert bundle_service.build_report()["bundles"] == []


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
    assert candidates == [], "the failed stem check consumed the archive"
    assert bundle_service.build_report()["bundles"] == []


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

    assert bundle_service.build_report()["bundles"] == []
    assert bundle_service.iter_bundle_candidates(isolated_storage) == ([], False)

    created = create(client, headers, [{"key": "import/plainparts"}])

    assert created["created"] == 0
    assert created["failed"] == 1
    assert created["results"][0]["reason"] == "unknown bundle"
    assert project_listing(client, headers) == {}


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

    expected = sorted(f"import/{name}" for name in TITLE_FOLDERS)
    assert sorted(row["key"] for row in report["bundles"]) == expected
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
        assert f"import/{name}" not in keys, f"{name} is a part name, never a title"

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)
    assert truncated is False
    assert sorted(candidate.key for candidate in candidates) == expected
    assert all(candidate.title_name is True for candidate in candidates)
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

    assert [row["key"] for row in off["bundles"]] == [MOAI], "only the title rows depend on the switch"
    assert sorted(row["key"] for row in on["bundles"]) == sorted([MOAI, "import/skull mask"])


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
        index = bundle_service.project_assets_index(session)

    assert index == {}, "a project that holds nothing contributes no membership"
    assert bundle_service._asset_owner(index, (), projects) is None
    assert bundle_service._asset_owner(index, ("asset-with-no-project",), projects) is None, (
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
        marker = bundle_service.find_existing_project(
            session,
            key="import/17_ClockworkMonster",
            source_url=THING_URL,
            name="Fates End - ClockworkMonster Tower",
            assets=("asset-marker",),
            asset_index=_PoisonedAssetIndex(),
        )
        source = bundle_service.find_existing_project(
            session,
            key="import/sourced",
            source_url=THING_URL,
            name="Sourced Thing",
            assets=("asset-source",),
            asset_index={"asset-source": (sourced_id,)},
        )
        by_name = bundle_service.find_existing_project(
            session,
            key="import/named",
            source_url=None,
            name="Named Thing",
            assets=("asset-name",),
            asset_index={"asset-name": (named_id,)},
        )
        by_assets = bundle_service.find_existing_project(
            session,
            key="import/assets",
            source_url=None,
            name="Nothing Like It",
            assets=("asset-holding",),
            asset_index={"asset-holding": (holding_id,)},
        )

    assert marker is not None and (marker.project.id, marker.match) == (
        marked_id,
        bundle_service.MATCH_MARKER,
    ), "the marker is proof and never reads the membership index"
    assert source is not None and (source.project.id, source.match) == (
        sourced_id,
        bundle_service.MATCH_SOURCE_URL,
    ), "a source URL outranks both the name and the shared files"
    assert by_name is not None and (by_name.project.id, by_name.match) == (
        named_id,
        bundle_service.MATCH_NAME,
    ), "a name outranks the shared files"
    assert by_assets is not None and (by_assets.project.id, by_assets.match) == (
        holding_id,
        bundle_service.MATCH_ASSETS,
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
    assert rows == expected
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
    assert member_count(created["results"][0]["project_id"]) == XBOX_OWN_MODELS + XBOX_GROUP_MODELS


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
    """Every registered MODEL file under ``rel_dir``, as storage-relative paths."""
    with Session(engine) as session:
        return {
            asset.path
            for asset in bundle_service.registered_assets(session, rel_dir)
            if bundle_service._suffix(asset.path) in bundle_detect.MODEL_SUFFIXES
        }


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

    Mutation: attach only one group's snapshot (or filter the attach set with no
    exclusion asked) and the set equality against every registered model reds.
    """
    write_stagetop(isolated_storage)
    insert_folder_row(STAGETOP, f"import/{STAGETOP}")
    scan()
    headers = auth_header(client)

    created = create(client, headers, [{"key": f"import/{STAGETOP}"}])

    assert created["created"] == 1
    assert created["failed"] == 0
    result = created["results"][0]
    assert result["assets_added"] == 60
    assert project_storage_paths(result["project_id"]) == registered_model_paths(f"import/{STAGETOP}")

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
    assert sum(group["attached"] for group in marker["groups"]) == result["assets_added"]
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
    project's `storage_path` set is the SAME 48 with no `._` path in it. Mutation:
    count the sidecars again and the row reads 96 while the project still holds
    48, which is exactly the double-count this ticket exists to remove.
    """
    write_rocket(isolated_storage)
    insert_folder_row(ROCKET, f"import/{ROCKET}")
    scan()
    headers = auth_header(client)

    created = create(client, headers, [{"key": f"import/{ROCKET}"}])

    assert created["created"] == 1
    result = created["results"][0]
    assert result["assets_added"] == 48

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
    attached = project_storage_paths(result["project_id"])
    assert attached == real, "the project holds the real `.stl` and nothing else"
    # The load-bearing assertion: this reads the REGISTERED rows, so it reds if a
    # `._*` sidecar ever registers (the walk-built `real` set could not say that).
    assert attached == registered_model_paths(f"import/{ROCKET}")


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
    assert result["assets_added"] == XBOX_OWN_MODELS + XBOX_GROUP_MODELS
    expected = {f"import/{XBOX}/shell-{index}.stl" for index in range(XBOX_OWN_MODELS)} | {
        f"{mount_key()}/bracket-{index}.stl" for index in range(XBOX_GROUP_MODELS)
    }
    assert project_storage_paths(result["project_id"]) == expected

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
    for field in ("model_count", "assets_total", "groups", "status"):
        assert after[field] == before[field], field
    assert after["cover"]["asset_id"] == before["cover"]["asset_id"]


def test_the_excluded_group_is_offered_as_its_own_row(
    client: TestClient, isolated_storage: Path
) -> None:
    """An excluded group comes back as a real, creatable row - at both scopes.

    The re-offer is a REPORT-level act: the same walk still names the mount
    ``member-of-bundle``, so the report and the ledger each say what they know.
    Mutation: drop the second loop in `_report_from_scan` and the mount row is
    missing; drop the `reoffered_relative` filter and the key is both a row and a
    refusal; drop `_qualify` from the marker lookup and the scoped read offers
    nothing; delete the re-offer loop's `row["rule_set"]` stamp and the stamp
    assertion reds.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    insert_folder_row(XBOX_GROUP, mount_key())
    scan()
    headers = auth_header(client)
    assert create_for_exclusion(client, headers, [XBOX_GROUP])["created"] == 1

    report = detect(client, headers, limit=1000)

    row = row_for(report, mount_key())
    assert row["detector"] == "thingiverse-readme"
    assert row["confidence"] == "high"
    assert row["status"] == "ready"
    assert row["blocked_reason"] is None
    assert row["groups"] == []
    assert row["model_count"] == XBOX_GROUP_MODELS
    compound = service_row(f"import/{XBOX}")
    assert compound["groups"] == [
        {
            "name": XBOX_GROUP,
            "model_count": XBOX_GROUP_MODELS,
            "foreign": True,
            "separable": True,
        }
    ]
    assert mount_key() not in refusal_ledger(report)
    # The wire model does not declare the per-row stamp - it is a service-level
    # fact, one rule set per report - so this is read from the service's dict.
    assert service_row(mount_key())["rule_set"] == report["rule_set"]["stamp"]

    walked = bundle_service.scan_bundles(isolated_storage)
    assert {item.rel_path: item.rule for item in walked.refusals}[mount_key()] == "member-of-bundle"
    assert mount_key() not in {candidate.key for candidate in walked.candidates}

    assert report["counts"]["total"] == 2
    scoped = detect(client, headers, path="import", limit=1000)
    assert scoped["counts"]["total"] == 2
    scoped_mount = row_for(scoped, mount_key())
    assert scoped_mount["key"] == row["key"], "a scoped read names the same bundle absolutely"
    refused = {item["rel_path"] for item in scoped["refused"]}
    assert f"{XBOX}/{XBOX_GROUP}" not in refused
    assert mount_key() not in refused


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
        rows = {row["key"] for row in report["bundles"]}
        assert rows & set(refusal_ledger(report)) == set()
        scoped = detect(client, headers, path="import", limit=1000)
        scoped_rows = {row["key"] for row in scoped["bundles"]}
        assert scoped_rows & set(refusal_ledger(scoped)) == set()

    assert_disjoint()
    assert create_for_exclusion(client, headers, [XBOX_GROUP])["created"] == 1
    assert_disjoint()


def test_a_dismissed_re_offered_group_is_not_stale_and_a_vanished_key_is(
    client: TestClient, isolated_storage: Path
) -> None:
    """A dismissal of a re-offered group still names a live row; a ghost key does not.

    Mutation: build `live_keys` from `scan.candidates` alone and the dismissed
    mount key reads stale the moment it is hidden, which is exactly the class of
    false stale a re-offer must not introduce.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    insert_folder_row(XBOX_GROUP, mount_key())
    scan()
    headers = auth_header(client)
    assert create_for_exclusion(client, headers, [XBOX_GROUP])["created"] == 1

    dismiss(client, headers, [mount_key()])
    report = detect(client, headers, limit=1000)

    assert mount_key() not in report["stale_dismissals"], "a live row is never stale"
    assert mount_key() not in {row["key"] for row in report["bundles"]}
    assert report["counts"]["dismissed"] == 1

    dismiss(client, headers, ["ghost/dismissed"])
    report = detect(client, headers, limit=1000)
    assert "ghost/dismissed" in report["stale_dismissals"]
    assert mount_key() not in report["stale_dismissals"]


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
    """The re-offer needs the marker, and only the marker: absorbed alone creates nothing.

    Three parts, one contract. (a) Before anything recorded an exclusion the
    absorbed key is not a row and answers ``unknown bundle``. (b) With the
    exclusion recorded, the group creates a project on facts RECOMPUTED from its
    own documents - never the compound's ``low`` - and its marker carries only the
    six keys every row has. (c) A second, exclusion-free tree leaves its own
    foreign group exactly where it was. Mutation: remove the `by_key.update(...)`
    line in `create_bundles` and part (b) answers `unknown bundle`; copy the
    compound row's detector/confidence into the split marker and part (b) reds.
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

    assert create_for_exclusion(client, headers, [XBOX_GROUP])["created"] == 1
    split = create(client, headers, [{"key": mount_key()}])

    assert split["created"] == 1
    project_id = split["results"][0]["project_id"]
    assert project_storage_paths(project_id) == {
        f"{mount_key()}/bracket-{index}.stl" for index in range(XBOX_GROUP_MODELS)
    }
    marker = project_marker(project_id)
    assert (marker["detector"], marker["confidence"]) == ("thingiverse-readme", "high")
    assert marker["key"] == mount_key()
    assert set(marker) - {"enrichment"} == {"key", "detector", "confidence", "rel_dir", "archive", "twin_verified"}

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


def test_a_re_offered_group_without_a_folder_row_is_blocked_and_creates_nothing(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-3e: a re-offered row resolves on the same terms as any other row.

    The row is offered because the marker says so, but a directory with no
    ``Folder.storage_path`` row is still ``blocked``/``unresolved_directory`` and
    creates nothing. The row is removed AFTER the scan on purpose: the assets stay
    registered, so only the resolution is missing. Mutation: let the re-offer skip
    the folder check (or create from an unresolved row) and a project appears
    with no folder behind it.
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

    row = row_for(detect(client, headers, limit=1000), mount_key())
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "unresolved_directory"

    blocked = create(client, headers, [{"key": mount_key()}])
    assert blocked["blocked"] == 1
    assert blocked["results"][0]["reason"] == "unresolved_directory"
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

    assert third["skipped"] == 1
    assert third["results"][0]["reason"] == "already_created"
    with Session(engine) as session:
        assert session.get(Project, project_id).metadata_json == metadata_before
    assert table_counts()["projects"] == 1
    assert member_count(project_id) == XBOX_OWN_MODELS + XBOX_GROUP_MODELS


def test_a_second_create_of_the_split_group_is_already_created(
    client: TestClient, isolated_storage: Path
) -> None:
    """The split row is a row like any other: a marker hit, and two created rows report.

    Mutation: leave the split candidate out of `by_key` (or key the marker with the
    compound's key) and the second create makes a second project.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    insert_folder_row(XBOX_GROUP, mount_key())
    scan()
    headers = auth_header(client)
    assert create_for_exclusion(client, headers, [XBOX_GROUP])["created"] == 1
    split = create(client, headers, [{"key": mount_key()}])
    assert split["created"] == 1

    second = create(client, headers, [{"key": mount_key()}])

    assert second["skipped"] == 1
    assert second["results"][0]["reason"] == "already_created"
    assert row_for(detect(client, headers, limit=1000), mount_key())["existing_project_match"] == "marker"

    report = detect(client, headers, limit=1000)
    assert len(report["bundles"]) == 2
    assert all(row["existing_project_id"] for row in report["bundles"])
    assert report["counts"]["already_created"] == 2


def test_a_hand_made_project_over_the_mount_files_reports_already_created_by_assets(
    client: TestClient, isolated_storage: Path
) -> None:
    """Asset containment is an identity even with no marker, no source and another name.

    A project proved by shared files proves the FILES, not the intent, so there
    is no marker folder to difference against and the row reports
    ``not_applicable`` with a null count - which is not ``0``: the server never
    measured a folder it has no record of.

    Mutation: drop the assets leg of `find_existing_project` for the re-offered
    row (or hand the row only the compound's containment) and this project is
    reported as creatable, so a create would make a duplicate. Map the
    asset-matched row to ``known``/0 and the not_applicable assertion reds.
    """
    write_xbox360(isolated_storage)
    insert_folder_row(XBOX, f"import/{XBOX}")
    scan()
    headers = auth_header(client)
    assert create_for_exclusion(client, headers, [XBOX_GROUP])["created"] == 1

    with Session(engine) as session:
        hand_made = project_service.create_project(session, "Someone Else's Mount", None, None, origin="imported")
    project_service.add_assets(hand_made.id, model_asset_ids(mount_key()))

    row = row_for(detect(client, headers, limit=1000), mount_key())

    assert row["existing_project_id"] == hand_made.id
    assert row["existing_project_match"] == "assets"
    assert row["bundle_drift_state"] == "not_applicable"
    assert row["bundle_drift_count"] is None
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
    excluded foreign group would still report its 4 as attached.
    """
    write_stagetop(isolated_storage)
    write_rocket(isolated_storage)
    write_xbox360(isolated_storage)
    keys = {"stagetop": f"import/{STAGETOP}", "rocket": f"import/{ROCKET}", "xbox": f"import/{XBOX}"}
    for key in keys.values():
        insert_folder_row(key.rsplit("/", 1)[-1], key)
    scan()
    headers = auth_header(client)

    created = create(client, headers, [{"key": key, "name": f"{name} parts"} for name, key in keys.items()])

    assert created["created"] == 3
    for name, result in zip(keys, created["results"], strict=True):
        marker = project_marker(result["project_id"])
        row = service_row(keys[name])
        assert [
            {"name": group["name"], "model_count": group["model_count"], "foreign": group["foreign"]}
            for group in marker["groups"]
        ] == [
            {"name": group["name"], "model_count": group["model_count"], "foreign": group["foreign"]}
            for group in row["groups"]
        ], name
        assert all(group["included"] is True for group in marker["groups"]), name

    rocket_marker = project_marker(created["results"][1]["project_id"])
    assert [(group["model_count"], group["attached"]) for group in rocket_marker["groups"]] == [(6, 6)] * len(
        ROCKET_GROUPS
    )

    xbox_marker = project_marker(created["results"][2]["project_id"])
    assert created["results"][2]["assets_added"] - sum(
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
    split = create(client, headers, [{"key": mount_key()}])
    assert split["created"] == 1

    ordinary = project_marker(created["results"][0]["project_id"])
    compound = project_marker(created["results"][1]["project_id"])
    split_marker = project_marker(split["results"][0]["project_id"])
    six = {"key", "detector", "confidence", "rel_dir", "archive", "twin_verified"}
    assert set(ordinary) - {"enrichment"} == six
    assert set(split_marker) - {"enrichment"} == six
    assert set(compound) - {"enrichment"} == six | {"shape", "groups", "cover"}


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
    yet when the second item is looked up: ``unknown bundle``. The NEXT read offers
    it, because by then the marker exists. Mutation: resolve items against a
    re-scanned report per item and the second item creates a project instead.
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

    assert row_for(detect(client, headers, limit=1000), mount_key())["status"] == "ready"


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
    split = create(client, headers, [{"key": mount_key()}])
    assert split["created"] == 1

    with Session(engine) as session:
        events = session.exec(select(AuditEvent).where(AuditEvent.action == audit.BUNDLE_CREATE)).all()
    details = {event.target: json.loads(event.detail_json or "{}") for event in events}

    assert details[f"import/{XBOX}"]["excluded_groups"] == [XBOX_GROUP]
    assert details[f"import/{XBOX}-copy"]["excluded_groups"] == []
    assert details[f"import/{XBOX}-copy-2"]["excluded_groups"] == [XBOX_GROUP]
    assert details[mount_key()]["excluded_groups"] == []
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
    assert refusal_ledger(report)[mount_key()] == "member-of-bundle"
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
    """AC-4: creation attaches every model, so both surfaces answer the honest zero.

    The measured compound shape: eight models in four part folders, each folder
    also holding the ``cover.jpg`` and ``notes.txt`` a create never attaches. The
    create attaches all eight models, the queue row and the project route both
    answer ``known``/0, and the naive all-asset difference would answer 8 - the
    model count - because the support files stay registered and unattached.
    Mutation: count every registered asset and both answers read 8, prompting on
    the very files creation deliberately skipped.
    """
    key = write_drift_compound(isolated_storage)
    insert_folder_row("DriftCompound", key)
    scan()
    headers = auth_header(client)

    created = create(client, headers, [{"key": key}])

    assert created["created"] == 1
    result = created["results"][0]
    project_id = result["project_id"]
    assert result["assets_added"] == 8
    assert project_storage_paths(project_id) == registered_model_paths(key)

    registered = service_assets_under(key)
    members = set(project_asset_ids(client, headers, project_id))
    naive = [path for asset_id, path, _size in registered if asset_id not in members]

    assert len(registered) == 16, "eight models and eight support files"
    assert len(naive) == 8 == len(members), "the naive difference answers the model count"

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
    # costs 8 statements (folders, memberships, the registered index, the
    # compound splits, the settings reads, and this candidate's own reads). A
    # probe that queried per candidate would be 9.
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
    # the registered index, the compound splits and the two settings reads) plus
    # three per candidate that already existed (its asset read, the marker
    # lookup's project list and its own detect read). Drift adds the one in the
    # middle and nothing per row.
    assert two_candidates == 12, "two candidates"
    assert five_candidates == 21, "five candidates: three more rows"

