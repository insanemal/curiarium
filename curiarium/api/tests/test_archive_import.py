"""Extract an archive the library already holds into its twin (Mf-1zmd).

The defect this closes: 28 detected bundles reported `blocked_reason:
archive_only` because the library was full of `.zip` files and nothing could
extract one that was *already there* - the only zip doors started from bytes the
server had just fetched. `POST /import/archive` extracts in place into
`<parent>/<stem>`, which is the directory `bundle_detect.twin_key` already pairs,
so the detector needs no change: a scan registers the files and the same bundle
becomes `ready`.

Every test writes a real `ZipFile` under a temporary storage root, and the
harness is a copy of `tests/test_bundle_service.py`'s (there is no shared conftest
in this suite): `isolated_storage` points every module that binds a path at import
time at the temp root, runs the migrations and deletes the rows a test created.
The scan is stubbed on the route (`main.start_scan`) so nothing races these
assertions; the test that needs registration drives `library_scan.scan_storage_root`
itself, which is synchronous.
"""

import copy
import json
from pathlib import Path
from typing import Dict, List
from uuid import uuid4
from zipfile import ZipFile, ZipInfo

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

import archive_service
import asset_file_service
import asset_service
import audit
import bundle_service
import db
import entry_policy
import library_scan
import main
import settings_service
import zip_service
from db import engine
from migrations import run_migrations
from models import Asset, AssetFile, AuditEvent, Folder, MfaRecoveryCode, Project, ProjectAsset, User, UserSession

README_TEXT = "Moai statue -No overhang\nCreated by Thingiverse user moai_maker\nLicensed under CC BY-NC\n"
LICENSE_TEXT = (
    "Moai statue -No overhang by moai_maker on Thingiverse: https://www.thingiverse.com/thing:2493386\n"
)
STL_BYTES = b"solid cube\nendsolid cube\n"


# --- infrastructure ---------------------------------------------------------


def truncate_accounts() -> None:
    with Session(engine) as session:
        session.execute(delete(MfaRecoveryCode))
        session.execute(delete(UserSession))
        session.execute(delete(User))
        session.commit()


@pytest.fixture(autouse=True)
def clean_accounts():
    """Start and finish every test without accounts, so the break-glass login works."""
    run_migrations(engine)
    truncate_accounts()
    yield
    truncate_accounts()


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
    with Session(engine) as session:
        projects_before = {project.id for project in session.exec(select(Project))}
        folders_before = {folder.id for folder in session.exec(select(Folder))}
        assets_before = {asset.id for asset in session.exec(select(Asset))}
    yield storage
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


def stub_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the route's scan kick without a background thread.

    The seam resolves `main.start_scan` at call time, so this is the suite's own
    patch point and the assertion order stays deterministic.
    """
    monkeypatch.setattr(main, "start_scan", lambda: True)


# --- fixtures on disk -------------------------------------------------------


def write_zip(path: Path, entries: Dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


def tree(root: Path) -> Dict[str, bytes]:
    """Every path and (for a file) its exact bytes: enough to prove nothing moved."""
    return {
        str(path.relative_to(root)) or ".": path.read_bytes() if path.is_file() else b"<dir>"
        for path in [root, *root.rglob("*")]
    }


def staging_residue(root: Path) -> List[str]:
    return [str(path.relative_to(root)) for path in root.rglob("*") if ".extract-" in path.name]


def detect(client: TestClient, headers: dict) -> dict:
    response = client.get("/bundles/detect", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def row_for(report: dict, key: str) -> dict:
    matches = [row for row in report["bundles"] if row["key"] == key]
    assert len(matches) == 1, f"expected exactly one row for {key}: {report['bundles']}"
    return matches[0]


def events_for(value: str) -> List[AuditEvent]:
    with Session(engine) as session:
        return list(session.exec(select(AuditEvent).where(AuditEvent.request_id == value)))


class ShrunkSizeZipFile:
    """A ZipFile whose infolist() understates file_size but serves the real bytes."""

    def __init__(self, path: Path) -> None:
        self._zip = ZipFile(path)
        self._real_infos: dict[int, ZipInfo] = {}

    def __enter__(self) -> "ShrunkSizeZipFile":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._zip.close()

    def infolist(self) -> List[ZipInfo]:
        declared: List[ZipInfo] = []
        for info in self._zip.infolist():
            shrunken = copy.copy(info)
            shrunken.file_size = 1
            self._real_infos[id(shrunken)] = info
            declared.append(shrunken)
        return declared

    def open(self, info: ZipInfo) -> object:
        return self._zip.open(self._real_infos[id(info)])


# --- AC-1: the bundle the detector called archive_only becomes ready --------


def test_an_archive_only_bundle_becomes_ready_after_extract_and_scan(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    write_zip(
        storage / "Moai.zip",
        {"README.txt": README_TEXT.encode(), "LICENSE.txt": LICENSE_TEXT.encode(), "Moai.stl": STL_BYTES},
    )
    headers = auth_header(client)
    stub_scan(monkeypatch)

    before = detect(client, headers)
    blocked = row_for(before, "Moai.zip")
    assert blocked["status"] == "blocked"
    assert blocked["blocked_reason"] == "archive_only"

    response = client.post("/import/archive", json={"path": "Moai.zip"}, headers=headers)
    assert response.status_code == 200, response.text
    expected_bytes = len(README_TEXT.encode()) + len(LICENSE_TEXT.encode()) + len(STL_BYTES)
    assert response.json() == {
        "path": "Moai.zip",
        "destination": "Moai",
        "files": 3,
        "bytes": expected_bytes,
        "already_present": False,
        "scan_started": True,
    }

    # The route's scan is stubbed, so drive the registering scan here: extraction
    # alone writes files, and only a scan turns them into Asset/Folder rows.
    state = library_scan.scan_storage_root()
    assert state.failed == 0, state.errors

    after = detect(client, headers)
    ready = row_for(after, "Moai")
    assert ready["status"] == "ready"
    assert ready["blocked_reason"] is None
    # The same detector produced the ready row: R1 read the documents the extract
    # wrote, and the archive it came from is its provenance, not a second bundle.
    assert ready["detector"] == "thingiverse-readme"
    assert ready["confidence"] == "high"
    assert ready["archive"]["rel_path"] == "Moai.zip"
    assert ready["archive"]["twin_verified"] is True
    assert [row["key"] for row in after["bundles"]] == ["Moai"]


# --- AC-2: placement, and the listing is the zip's own ----------------------


def test_extraction_lands_in_the_twin_and_the_listing_matches_the_zip(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    archive = write_zip(storage / "Moai.zip", {"Moai.stl": STL_BYTES, "files/part.stl": b"part"})
    headers = auth_header(client)
    stub_scan(monkeypatch)

    listing = client.get("/import/archive/entries", params={"path": "Moai.zip"}, headers=headers)
    assert listing.status_code == 200, listing.text
    assert listing.json() == {"path": "Moai.zip", "entries": zip_service.list_zip_entries(archive)}

    response = client.post("/import/archive", json={"path": "Moai.zip"}, headers=headers)
    assert response.status_code == 200, response.text
    assert (storage / "Moai" / "Moai.stl").read_bytes() == STL_BYTES
    assert (storage / "Moai" / "files" / "part.stl").read_bytes() == b"part"

    write_zip(storage / "Subset.zip", {"one.stl": b"one", "two.stl": b"two"})
    subset = client.post("/import/archive", json={"path": "Subset.zip", "entries": ["one.stl"]}, headers=headers)
    assert subset.status_code == 200, subset.text
    assert subset.json()["files"] == 1
    assert (storage / "Subset" / "one.stl").read_bytes() == b"one"
    assert not (storage / "Subset" / "two.stl").exists(), "an explicit selection extracts only itself"


def test_a_hostile_archive_extracts_nothing_outside_its_twin(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    write_zip(
        storage / "Evil.zip",
        {
            "../escaped.stl": b"escape",
            "/etc/absolute.stl": b"absolute",
            "..%2fencoded.stl": b"encoded",
            "safe.stl": b"safe",
        },
    )
    headers = auth_header(client)
    stub_scan(monkeypatch)

    listing = client.get("/import/archive/entries", params={"path": "Evil.zip"}, headers=headers)
    assert listing.status_code == 200, listing.text
    # The traversal member is not a member at all: the normaliser drops it, so it
    # can be neither listed nor selected. The absolute path is neutralised into a
    # relative one, and the percent-encoded name is just a filename.
    assert [entry["path"] for entry in listing.json()["entries"]] == [
        "..%2fencoded.stl",
        "etc/absolute.stl",
        "safe.stl",
    ]

    response = client.post("/import/archive", json={"path": "Evil.zip"}, headers=headers)
    assert response.status_code == 200, response.text
    extracted = {str(path.relative_to(storage)) for path in storage.rglob("*") if path.is_file()}
    assert extracted == {
        "Evil.zip",
        "Evil/..%2fencoded.stl",
        "Evil/etc/absolute.stl",
        "Evil/safe.stl",
    }
    assert not (storage.parent / "escaped.stl").exists()
    assert not (storage / "escaped.stl").exists()
    outside = [path for path in storage.parent.rglob("*") if path.is_file() and storage not in path.parents]
    assert outside == []


# --- AC-3: idempotency, and no-clobber without a partial publish ------------


def test_a_second_identical_extract_changes_nothing(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    write_zip(storage / "Moai.zip", {"Moai.stl": STL_BYTES})
    headers = auth_header(client)
    stub_scan(monkeypatch)
    first = client.post("/import/archive", json={"path": "Moai.zip"}, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["files"] == 1
    before = tree(storage)

    second = client.post("/import/archive", json={"path": "Moai.zip"}, headers=headers)

    assert second.status_code == 200, second.text
    assert second.json() == {
        "path": "Moai.zip",
        "destination": "Moai",
        "files": 0,
        "bytes": 0,
        "already_present": True,
        "scan_started": False,
    }
    assert tree(storage) == before
    assert staging_residue(storage) == []


def test_a_partially_colliding_extract_refuses_and_publishes_nothing(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    write_zip(storage / "Second.zip", {"keep.stl": b"keep", "pile.stl": b"new"})
    (storage / "Second").mkdir()
    (storage / "Second" / "pile.stl").write_bytes(b"foreign")
    headers = auth_header(client)
    stub_scan(monkeypatch)

    response = client.post("/import/archive", json={"path": "Second.zip"}, headers=headers)

    assert response.status_code == 409, response.text
    assert "Second/pile.stl" in response.json()["detail"]
    assert (storage / "Second" / "pile.stl").read_bytes() == b"foreign"
    assert not (storage / "Second" / "keep.stl").exists(), "a refused extract publishes none of its entries"
    assert staging_residue(storage) == []


def test_a_file_where_the_twin_directory_must_go_is_refused(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    write_zip(storage / "Moai.zip", {"Moai.stl": STL_BYTES})
    (storage / "Moai").write_bytes(b"occupied")
    headers = auth_header(client)
    stub_scan(monkeypatch)

    response = client.post("/import/archive", json={"path": "Moai.zip"}, headers=headers)

    assert response.status_code == 409, response.text
    assert "Moai" in response.json()["detail"]
    assert (storage / "Moai").read_bytes() == b"occupied"
    assert staging_residue(storage) == []


def test_a_false_no_clobber_mid_flight_rolls_back_the_files_this_request_published(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collision branch: `_move_without_clobber` returns False, not raises."""
    storage = isolated_storage
    write_zip(storage / "Race.zip", {"first.stl": b"first", "second.stl": b"second"})
    headers = auth_header(client)
    stub_scan(monkeypatch)
    real_move = archive_service._move_without_clobber
    calls = {"count": 0}

    def collide_on_the_second_move(old, new):
        calls["count"] += 1
        if calls["count"] == 2:
            return False
        return real_move(old, new)

    monkeypatch.setattr(archive_service, "_move_without_clobber", collide_on_the_second_move)

    response = client.post("/import/archive", json={"path": "Race.zip"}, headers=headers)

    assert response.status_code == 409, response.text
    assert "Race/second.stl" in response.json()["detail"]
    assert not (storage / "Race" / "first.stl").exists(), "a file published earlier in this request is rolled back"
    assert not (storage / "Race" / "second.stl").exists()
    assert not (storage / "Race").exists()
    assert staging_residue(storage) == []


def test_a_raising_publish_rolls_back_the_files_this_request_published(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other two branches: a mid-publish `OSError`, not a `False` return.

    `_make_directories` raising its own 409 after the preflight takes exactly the
    same path, so proving the `OSError` case proves the handler that catches both.
    """
    storage = isolated_storage
    write_zip(storage / "Race.zip", {"first.stl": b"first", "second.stl": b"second"})
    headers = auth_header(client)
    stub_scan(monkeypatch)
    real_move = archive_service._move_without_clobber
    calls = {"count": 0}

    def raise_on_the_second_move(old, new):
        calls["count"] += 1
        if calls["count"] == 2:
            raise NotADirectoryError("a file was planted where a directory had to be")
        return real_move(old, new)

    monkeypatch.setattr(archive_service, "_move_without_clobber", raise_on_the_second_move)

    with pytest.raises(NotADirectoryError):
        client.post("/import/archive", json={"path": "Race.zip"}, headers=headers)

    assert not (storage / "Race" / "first.stl").exists(), "the file published before the failure is rolled back"
    assert not (storage / "Race" / "second.stl").exists()
    assert not (storage / "Race").exists()
    assert staging_residue(storage) == []
    assert sorted(path.name for path in storage.iterdir() if path.name != "thumbs") == ["Race.zip"]


# --- the twin must be the directory the detector actually pairs ------------


@pytest.mark.parametrize(
    ("archive_name", "unpaired_twin"),
    [
        # strip() makes the stem differ from the archive's own key.
        ("My model .zip", "My model"),
        # A whitespace-only stem sanitises to "folder" and collides with every
        # other such archive in the parent.
        (" .zip", "folder"),
        # Stripping one suffix leaves a stem that itself ends in ".zip".
        ("foo.zip.zip", "foo.zip"),
    ],
)
def test_an_archive_whose_twin_would_not_pair_is_refused_before_any_write(
    client: TestClient,
    isolated_storage: Path,
    monkeypatch: pytest.MonkeyPatch,
    archive_name: str,
    unpaired_twin: str,
) -> None:
    """`_twin_rel` builds a name `bundle_detect.twin_key` would never pair.

    Such an extraction would succeed on disk and leave the bundle `archive_only`
    forever, so it is a 400 naming the rename, with nothing written.
    """
    storage = isolated_storage
    write_zip(storage / archive_name, {"Moai.stl": STL_BYTES})
    headers = auth_header(client)
    stub_scan(monkeypatch)
    before = tree(storage)

    response = client.post("/import/archive", json={"path": archive_name}, headers=headers)

    assert response.status_code == 400, response.text
    assert "Rename the archive first" in response.json()["detail"]
    assert tree(storage) == before
    assert not (storage / unpaired_twin).exists()
    assert staging_residue(storage) == []


def test_a_cleanly_named_archive_passes_the_pairing_guard(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control: the guard refuses only names that cannot pair."""
    storage = isolated_storage
    write_zip(storage / "Moai.zip", {"Moai.stl": STL_BYTES})
    headers = auth_header(client)
    stub_scan(monkeypatch)

    response = client.post("/import/archive", json={"path": "Moai.zip"}, headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["destination"] == "Moai"
    assert (storage / "Moai" / "Moai.stl").read_bytes() == STL_BYTES


# --- a self-conflicting selection is a request defect ------------------------


def test_a_member_that_is_another_members_directory_is_refused_before_staging(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    write_zip(storage / "Conflict.zip", {"sub": b"file", "sub/file.stl": b"file"})
    headers = auth_header(client)
    stub_scan(monkeypatch)
    before = tree(storage)

    response = client.post("/import/archive", json={"path": "Conflict.zip"}, headers=headers)

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "Conflict/sub" in detail
    assert "Conflict/sub/file.stl" in detail
    assert tree(storage) == before
    assert not (storage / "Conflict").exists()
    assert staging_residue(storage) == []


# --- a member whose rendered segment escapes the twin ----------------------


def test_a_padded_dot_member_in_a_subdirectory_archive_is_refused_before_staging(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`".. "` renders to `".."`, which `normalize_zip_entry_path` never saw.

    Joined with the twin it walked one level up, so this extraction used to answer
    200 with `destination: sub/Evil` and then write `sub/evil.stl` - outside the
    twin it named. The guard runs where the target rels are built, so the refusal
    lands before staging exists and nothing at all is written.
    """
    storage = isolated_storage
    write_zip(storage / "sub" / "Evil.zip", {".. /evil.stl": b"escape", "safe.stl": b"safe"})
    headers = auth_header(client)
    stub_scan(monkeypatch)
    before = tree(storage)

    response = client.post("/import/archive", json={"path": "sub/Evil.zip"}, headers=headers)

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert ".. /evil.stl" in detail
    assert "sub/Evil" in detail
    assert "rename the member" in detail
    assert tree(storage) == before
    assert not (storage / "sub" / "evil.stl").exists(), "nothing lands beside the twin"
    assert not (storage / "sub" / "Evil").exists(), "a refused extract does not create the twin"
    assert staging_residue(storage) == []


def test_a_member_that_renders_to_a_single_dot_is_refused(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`" . "` renders to `"."`: the twin path collapsed and became a file."""
    storage = isolated_storage
    write_zip(storage / "Dot.zip", {" . ": b"dotted"})
    headers = auth_header(client)
    stub_scan(monkeypatch)
    before = tree(storage)

    response = client.post("/import/archive", json={"path": "Dot.zip"}, headers=headers)

    assert response.status_code == 400, response.text
    assert " . " in response.json()["detail"]
    assert tree(storage) == before
    assert not (storage / "Dot").exists(), "the twin is never the member's own file"
    assert staging_residue(storage) == []


def test_a_padded_dot_member_at_the_root_is_refused(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    write_zip(storage / "Evil.zip", {".. /evil.stl": b"escape"})
    headers = auth_header(client)
    stub_scan(monkeypatch)
    before = tree(storage)

    response = client.post("/import/archive", json={"path": "Evil.zip"}, headers=headers)

    assert response.status_code == 400, response.text
    assert ".. /evil.stl" in response.json()["detail"]
    assert tree(storage) == before
    assert not (storage / "evil.stl").exists(), "the root twin has nothing to escape into"
    assert not (storage / "Evil").exists()
    assert staging_residue(storage) == []


def test_ordinary_dotted_member_names_still_extract(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control: the guard refuses dot *segments*, not dots."""
    storage = isolated_storage
    write_zip(storage / "Notes.zip", {"notes..txt": b"dotted name", "sub/part.stl": b"part"})
    headers = auth_header(client)
    stub_scan(monkeypatch)

    response = client.post("/import/archive", json={"path": "Notes.zip"}, headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["files"] == 2
    assert (storage / "Notes" / "notes..txt").read_bytes() == b"dotted name"
    assert (storage / "Notes" / "sub" / "part.stl").read_bytes() == b"part"
    assert staging_residue(storage) == []


# --- AC-4: the two budget gates --------------------------------------------


# Both caps are read by `entry_policy`, which owns the budget: patching
# `zip_service` here would now be an AttributeError, which is the dynamic proof
# that the module, not the door, holds the number.


def test_a_declared_budget_overshoot_refuses_before_any_write(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    write_zip(storage / "Big.zip", {"a.stl": b"a" * 10, "b.stl": b"b" * 10})
    monkeypatch.setattr(entry_policy, "IMPORT_MAX_BYTES", 1000)
    monkeypatch.setattr(entry_policy, "IMPORT_ZIP_MAX_BYTES", 15)
    headers = auth_header(client)
    stub_scan(monkeypatch)
    before = tree(storage)

    response = client.post("/import/archive", json={"path": "Big.zip"}, headers=headers)

    assert response.status_code == 413, response.text
    assert "20 bytes" in response.json()["detail"]
    assert "IMPORT_ZIP_MAX_MB" in response.json()["detail"]
    assert tree(storage) == before
    assert not (storage / "Big").exists()


def test_an_understated_entry_is_refused_after_streaming(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    write_zip(storage / "Lies.zip", {"a.stl": b"a" * 10, "b.stl": b"b" * 10})
    monkeypatch.setattr(entry_policy, "IMPORT_MAX_BYTES", 1000)
    monkeypatch.setattr(entry_policy, "IMPORT_ZIP_MAX_BYTES", 15)
    # Every member declares one byte and serves ten: the preflight passes and the
    # running total is what refuses the request, after the bytes have been read.
    monkeypatch.setattr(archive_service, "ZipFile", ShrunkSizeZipFile)
    headers = auth_header(client)
    stub_scan(monkeypatch)
    before = tree(storage)

    response = client.post("/import/archive", json={"path": "Lies.zip"}, headers=headers)

    assert response.status_code == 413, response.text
    assert "IMPORT_ZIP_MAX_MB" in response.json()["detail"]
    assert tree(storage) == before
    assert not (storage / "Lies").exists()
    assert staging_residue(storage) == []


def test_a_declared_entry_over_the_per_file_cap_is_refused_before_staging(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    write_zip(storage / "Giant.zip", {"big.stl": b"x" * 10})
    monkeypatch.setattr(entry_policy, "IMPORT_MAX_BYTES", 5)
    monkeypatch.setattr(entry_policy, "IMPORT_ZIP_MAX_BYTES", 1000)
    headers = auth_header(client)
    stub_scan(monkeypatch)
    before = tree(storage)

    response = client.post("/import/archive", json={"path": "Giant.zip"}, headers=headers)

    assert response.status_code == 413, response.text
    assert "big.stl" in response.json()["detail"], "the refusal names the entry that is too big"
    assert "IMPORT_MAX_MB" in response.json()["detail"]
    assert tree(storage) == before
    assert not (storage / "Giant").exists()
    assert staging_residue(storage) == []


def test_an_entry_that_outgrows_the_per_file_cap_while_streaming_is_refused(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declaration is metadata: the bytes are still re-checked while streaming."""
    storage = isolated_storage
    write_zip(storage / "Giant.zip", {"big.stl": b"x" * 10})
    monkeypatch.setattr(entry_policy, "IMPORT_MAX_BYTES", 5)
    monkeypatch.setattr(entry_policy, "IMPORT_ZIP_MAX_BYTES", 1000)
    # Declares one byte and serves ten: only the streamed size can refuse it.
    monkeypatch.setattr(archive_service, "ZipFile", ShrunkSizeZipFile)
    headers = auth_header(client)
    stub_scan(monkeypatch)
    before = tree(storage)

    response = client.post("/import/archive", json={"path": "Giant.zip"}, headers=headers)

    assert response.status_code == 413, response.text
    assert "IMPORT_MAX_MB" in response.json()["detail"]
    assert tree(storage) == before
    assert not (storage / "Giant").exists()
    assert staging_residue(storage) == []


# --- containment on the request path itself --------------------------------


def test_a_request_path_outside_the_library_is_never_read(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    headers = auth_header(client)
    stub_scan(monkeypatch)
    (storage / "notes.txt").write_text("not a zip", encoding="utf-8")
    before = tree(storage)

    for path in ("../../etc/passwd", "/etc/passwd"):
        listed = client.get("/import/archive/entries", params={"path": path}, headers=headers)
        assert listed.status_code == 400, (path, listed.text)
        extracted = client.post("/import/archive", json={"path": path}, headers=headers)
        assert extracted.status_code == 400, (path, extracted.text)

    assert client.get("/import/archive/entries", params={"path": "missing.zip"}, headers=headers).status_code == 404
    assert client.post("/import/archive", json={"path": "missing.zip"}, headers=headers).status_code == 404
    assert client.get("/import/archive/entries", params={"path": "notes.txt"}, headers=headers).status_code == 415
    assert client.post("/import/archive", json={"path": "notes.txt"}, headers=headers).status_code == 415
    assert tree(storage) == before
    # A hidden twin would be pruned by every scan, so the extraction would exist
    # on disk and nowhere the product can see it: refused before any read.
    write_zip(storage / ".Moai.zip", {"Moai.stl": STL_BYTES})
    before_hidden = tree(storage)
    hidden = client.post("/import/archive", json={"path": ".Moai.zip"}, headers=headers)
    assert hidden.status_code == 400, hidden.text
    assert tree(storage) == before_hidden
    assert tree(storage)["notes.txt"] == b"not a zip"


# --- audit ------------------------------------------------------------------


def test_an_idempotent_extract_writes_no_audit_row_and_a_real_one_writes_exactly_one(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    write_zip(storage / "Moai.zip", {"Moai.stl": STL_BYTES})
    headers = auth_header(client)
    stub_scan(monkeypatch)
    first_value = f"archive-{uuid4().hex}"

    first = client.post(
        "/import/archive",
        json={"path": "Moai.zip"},
        headers={**headers, "X-Request-ID": first_value},
    )
    assert first.status_code == 200, first.text

    rows = events_for(first_value)
    assert [row.action for row in rows] == [audit.ARCHIVE_EXTRACT]
    row = rows[0]
    assert row.target == "Moai.zip"
    assert json.loads(row.detail_json) == {
        "destination": "Moai",
        "files": 1,
        "bytes": len(STL_BYTES),
        "scan_started": True,
    }

    second_value = f"archive-{uuid4().hex}"
    second = client.post(
        "/import/archive",
        json={"path": "Moai.zip"},
        headers={**headers, "X-Request-ID": second_value},
    )
    assert second.status_code == 200, second.text
    assert second.json()["already_present"] is True
    assert events_for(second_value) == [], "a request that changes nothing writes nothing"
