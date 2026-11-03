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
from pathlib import Path
from typing import Dict, List, Mapping, Optional
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
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
    assert row["cover"] == {"asset_id": None, "rel_path": "images/a.jpg", "reason": "largest-image-unregistered"}
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
    assert parent_row["cover"]["reason"] == "none", "the child's previews are not the parent's cover"
    assert parent_row["assets_total"] == 13, "assets_total counts the parent's 3 files plus the child's 10"
    assert child_row["confidence"] == "high"

    created = create(client, headers, [{"key": parent_row["key"]}, {"key": child_row["key"]}])

    assert created["created"] == 2
    parent_project, child_project = (result["project_id"] for result in created["results"])
    assert len(project_asset_ids(client, headers, parent_project)) == 1
    assert len(project_asset_ids(client, headers, child_project)) == 3


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


def clockwork_pair(storage: Path) -> None:
    """The directory, its archive and the byte-identical browser duplicate."""
    entries = clockwork_entries()
    for rel_path, payload in entries.items():
        write_file(storage / "import" / rel_path, payload)
    write_zip(storage / CLOCKWORK_ZIP, entries)
    write_zip(storage / CLOCKWORK_DUPLICATE_ZIP, entries)


def test_service_an_archive_pairs_with_the_directory_its_entries_are_rooted_at(
    client: TestClient, isolated_storage: Path
) -> None:
    """AC-1: one directory, two byte-identical zips, one blocked row for the pair.

    The ``pair_only`` / ``blocked`` / ``created["blocked"] == 1`` assertions are the
    documented INTERIM: `Mf-t12a` makes such a directory a real low-confidence
    candidate, so exactly those three change and the pairing assertions (the row
    key, the attached archive and its ``twin_verified``, the entry root) stay.
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
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "archive_only"
    assert row["assets_total"] == 0, "a pair-only row reports no assets, admitted interim"

    candidates, truncated = bundle_service.iter_bundle_candidates(isolated_storage)
    assert truncated is False
    assert [candidate.key for candidate in candidates] == [CLOCKWORK_DIR]
    assert candidates[0].pair_only is True
    assert candidates[0].archive_path == CLOCKWORK_ZIP
    assert candidates[0].entry_root == CLOCKWORK_ROOT

    created = create(client, headers, [{"key": CLOCKWORK_DIR}])

    assert created["blocked"] == 1
    assert created["created"] == 0
    assert project_listing(client, headers) == {}


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
    """The zip-only container strip fires for a pair-only candidate too."""
    write_file(isolated_storage / "Collection" / "loose.stl", STL_BYTES)
    write_file(isolated_storage / "Collection" / "loose.jpg", b"j" * 60)
    write_text(isolated_storage / "Collection" / "notes.txt", "a loose trio")
    payloads = {"17_Thing/a.stl": STL_BYTES, "17_Thing/b.stl": STL_BYTES, "17_Thing/preview.jpg": b"j" * 50}
    for rel_path, payload in payloads.items():
        write_file(isolated_storage / "Collection" / rel_path, payload)
    write_zip(isolated_storage / "Collection" / "Thing Download.zip", payloads)
    headers = auth_header(client)

    report = detect(client, headers)

    assert [row["key"] for row in report["bundles"]] == ["Collection/17_Thing"]
    row = row_for(report, "Collection/17_Thing")
    assert row["archive"] == {"rel_path": "Collection/Thing Download.zip", "twin_verified": True}
    assert row["blocked_reason"] == "archive_only"


def test_service_a_paired_directory_that_holds_a_nested_archive_is_still_offered(
    client: TestClient, isolated_storage: Path
) -> None:
    """The container strip may never swallow a verified pair.

    The Clockwork shape (models and images, no text) with a real archive dropped in
    *inside* the directory - the shape `Mf-74x5` exists for. That nested archive is
    a member of the outer pair once `Mf-74x5` lands, but until then it is a row of
    its own, and its presence must not make the outer pair look like a container: a
    pair-only row claims no subtree, so stripping it would hide verified evidence.
    What this test protects is exactly that - the verified PAIR is still offered.
    """
    clockwork_pair(isolated_storage)
    nested = {"inner/part.stl": STL_BYTES, "inner/preview.jpg": b"j" * 50, "inner/notes.txt": b"n" * 20}
    nested_zip = f"{CLOCKWORK_DIR}/Clockwork_v1.8.zip"
    write_zip(isolated_storage / nested_zip, nested)
    scan()
    headers = auth_header(client)

    report = detect(client, headers)

    assert {row["key"] for row in report["bundles"]} == {CLOCKWORK_DIR, nested_zip}
    pair = row_for(report, CLOCKWORK_DIR)
    assert pair["archive"] == {"rel_path": CLOCKWORK_ZIP, "twin_verified": True}, (
        "the verified pair survives the archive-only container strip"
    )
    nested_row = row_for(report, nested_zip)
    assert nested_row["status"] == "blocked"
    assert nested_row["blocked_reason"] == "archive_only"
