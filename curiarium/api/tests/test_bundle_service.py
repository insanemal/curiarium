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

import json
import re
import shutil
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Mapping, Optional
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from sqlmodel import Session, select

import asset_file_service
import asset_service
import audit
import bundle_detect
import bundle_service
import db
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
        "cover": {"asset_id": None, "rel_path": "Inbox/loose.jpg", "reason": "largest-image-unregistered"},
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
CLOCKWORK_ZIP = "import/17_Fates End - ClockworkMonster Tower.zip"
CLOCKWORK_DUPLICATE_ZIP = "import/17_Fates End - ClockworkMonster Tower (1).zip"
GENERIC_ZIP = "import/Generic Download.zip"


def clockwork_entries(root: str = CLOCKWORK_ROOT) -> Dict[str, bytes]:
    """The real pair: 12 models, 2 renders, no text, every entry rooted at ``root``."""
    entries = {f"{root}/Clockwork_Part{index:02d}.stl": b"s" * (40 + index) for index in range(12)}
    entries[f"{root}/Clockwork_Render0.png"] = b"p" * 60
    entries[f"{root}/Clockwork_Render1.png"] = b"p" * 80
    return entries


def clockwork_directory(storage: Path) -> None:
    """The directory alone: 12 models and 2 renders, no text and no archive."""
    for rel_path, payload in clockwork_entries().items():
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
    """
    clockwork_directory(isolated_storage)
    scan()
    headers = auth_header(client)

    assert bundle_service.build_report()["bundles"] == []
    assert bundle_service.iter_bundle_candidates(isolated_storage) == ([], False)

    created = create(client, headers, [{"key": CLOCKWORK_DIR}])

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
