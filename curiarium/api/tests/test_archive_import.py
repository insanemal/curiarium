"""Extract an archive the library already holds into its twin (Mf-1zmd).

The defect this closes: 28 detected bundles reported `blocked_reason:
archive_only` because the library was full of `.zip` files and nothing could
extract one that was *already there* - the only zip doors started from bytes the
server had just fetched. `POST /import/archive` extracts in place into
`<parent>/<stem>`, which is the directory `proposal_engine.twin_key` already pairs,
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
from typing import Dict, List, Optional
from uuid import uuid4
from zipfile import ZipFile, ZipInfo

import pytest
from fastapi.testclient import TestClient
from httpx import Response
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
import operations_runner
import settings_service
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


def plan_extract(
    client: TestClient, headers: dict, path: str, entries: Optional[List[str]] = None
) -> "Response":
    """Ask the `extract` workflow to plan one in-library archive."""
    params = {"entries": entries} if entries is not None else {}
    return client.post(
        "/operations/plan",
        json={"workflow": "extract", "target": path, "params": params},
        headers=headers,
    )


def planned_extract(
    client: TestClient, headers: dict, path: str, entries: Optional[List[str]] = None
) -> dict:
    response = plan_extract(client, headers, path, entries)
    assert response.status_code == 200, response.text
    return response.json()


def run_extract(
    client: TestClient,
    headers: dict,
    path: str,
    entries: Optional[List[str]] = None,
    *,
    expect_done: bool = True,
) -> dict:
    """Plan, submit and run one `extract`; the finished operation is the response."""
    plan = planned_extract(client, headers, path, entries)
    submitted = client.post(
        "/operations",
        json={"plan_id": plan["plan_id"], "idempotency_key": f"archive-{uuid4().hex}"},
        headers=headers,
    )
    assert submitted.status_code == 202, submitted.text
    operations_runner.dispatch_once()
    operations_runner.wait_idle(timeout=30)
    operation = client.get(f"/operations/{submitted.json()['id']}", headers=headers).json()
    if expect_done:
        assert operation["status"] == "done", operation
    return operation


def step_result(operation: dict, op: str) -> dict:
    step = next(step for step in operation["steps"] if step["op"] == op)
    assert step["result"] is not None, step
    return step["result"]


def extract_refusal(
    client: TestClient, headers: dict, path: str, entries: Optional[List[str]] = None
) -> tuple[int, str]:
    response = plan_extract(client, headers, path, entries)
    detail = response.json().get("detail")
    assert isinstance(detail, str), response.text
    return response.status_code, detail


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

    before = detect(client, headers)
    blocked = row_for(before, "Moai.zip")
    assert blocked["status"] == "blocked"
    assert blocked["blocked_reason"] == "archive_only"

    operation = run_extract(client, headers, "Moai.zip")
    expected_bytes = len(README_TEXT.encode()) + len(LICENSE_TEXT.encode()) + len(STL_BYTES)
    assert step_result(operation, "extract") == {
        "path": "Moai.zip",
        "destination": "Moai",
        "files": 3,
        "bytes": expected_bytes,
        "already_present": False,
        "scan_started": False,
    }
    # The workflow's own register step is what turns the files into rows; a rescan
    # now is a no-op that must still succeed.
    state = library_scan.scan_storage_root()
    assert state.failed == 0, state.errors

    after = detect(client, headers)
    ready = row_for(after, "Moai")
    assert ready["status"] == "ready"
    assert ready["blocked_reason"] is None
    # The engine read the documents the extract wrote - documented at the root, so
    # `high` - and the archive it came from is its provenance, not a second bundle.
    assert ready["detector"] == "engine:one-project"
    assert ready["metadata_shape"] == "one-project"
    assert ready["confidence"] == "high"
    assert ready["archive"]["rel_path"] == "Moai.zip"
    assert ready["archive"]["twin_verified"] is True
    assert [row["key"] for row in after["bundles"]] == ["Moai"]


# --- AC-2: placement, and the listing is the zip's own ----------------------


def test_extraction_lands_in_the_twin_and_the_listing_matches_the_zip(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    write_zip(storage / "Moai.zip", {"Moai.stl": STL_BYTES, "files/part.stl": b"part"})
    headers = auth_header(client)

    plan = planned_extract(client, headers, "Moai.zip")
    assert plan["review"]["members"] == 2
    assert {group["name"] for group in plan["review"]["groups"]} == {"", "files"}
    assert sum(group["members"] for group in plan["review"]["groups"]) == 2

    run_extract(client, headers, "Moai.zip")
    assert (storage / "Moai" / "Moai.stl").read_bytes() == STL_BYTES
    assert (storage / "Moai" / "files" / "part.stl").read_bytes() == b"part"

    write_zip(storage / "Subset.zip", {"one.stl": b"one", "two.stl": b"two"})
    subset = run_extract(client, headers, "Subset.zip", ["one.stl"])
    assert step_result(subset, "extract")["files"] == 1
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

    plan = planned_extract(client, headers, "Evil.zip")
    # The traversal member is not a member at all: the normaliser drops it, so it
    # can be neither listed nor selected. The absolute path is neutralised into a
    # relative one, and the percent-encoded name is a dot-prefixed name the scan
    # would never register - so the review excludes it.
    assert plan["review"]["members"] == 2
    excluded = {entry["path"] for entry in plan["review"]["excluded"]}
    assert excluded == {"..%2fencoded.stl"}, plan["review"]["excluded"]
    assert {group["name"] for group in plan["review"]["groups"]} == {"", "etc"}
    assert sum(group["members"] for group in plan["review"]["groups"]) == 2

    operation = run_extract(client, headers, "Evil.zip")
    extracted = {str(path.relative_to(storage)) for path in storage.rglob("*") if path.is_file()}
    # The deleted door wrote the caller's SELECTION, and the workflow must write
    # the same set: the dot-named member lands on disk even though no row follows.
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
    # The other half of the same fact: written is not registered. The register
    # step names only the planned paths, so the dot-named file has no asset row.
    registered = set(step_result(operation, "register")["registered"])
    assert "Evil/..%2fencoded.stl" not in registered, registered
    assert {"Evil/etc/absolute.stl", "Evil/safe.stl", "Evil.zip"} <= registered, registered


# --- AC-3: idempotency, and no-clobber without a partial publish ------------


def test_a_second_identical_extract_changes_nothing(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    write_zip(storage / "Moai.zip", {"Moai.stl": STL_BYTES})
    headers = auth_header(client)
    first = run_extract(client, headers, "Moai.zip")
    assert step_result(first, "extract")["files"] == 1
    before = tree(storage)

    second = run_extract(client, headers, "Moai.zip")

    assert step_result(second, "extract") == {
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

    operation = run_extract(client, headers, "Second.zip", expect_done=False)

    assert operation["status"] == "failed"
    error = next(step for step in operation["steps"] if step["op"] == "extract")["error"]
    assert "Second/pile.stl" in error
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

    operation = run_extract(client, headers, "Moai.zip", expect_done=False)

    assert operation["status"] == "failed"
    error = next(step for step in operation["steps"] if step["op"] == "extract")["error"]
    assert "Moai" in error
    assert (storage / "Moai").read_bytes() == b"occupied"
    assert staging_residue(storage) == []


def test_a_false_no_clobber_mid_flight_rolls_back_the_files_this_request_published(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collision branch: `_move_without_clobber` returns False, not raises."""
    storage = isolated_storage
    write_zip(storage / "Race.zip", {"first.stl": b"first", "second.stl": b"second"})
    headers = auth_header(client)
    real_move = archive_service._move_without_clobber
    calls = {"count": 0}

    def collide_on_the_second_move(old, new):
        calls["count"] += 1
        if calls["count"] == 2:
            return False
        return real_move(old, new)

    monkeypatch.setattr(archive_service, "_move_without_clobber", collide_on_the_second_move)

    operation = run_extract(client, headers, "Race.zip", expect_done=False)

    assert operation["status"] == "failed"
    error = next(step for step in operation["steps"] if step["op"] == "extract")["error"]
    assert "Race/second.stl" in error
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
    real_move = archive_service._move_without_clobber
    calls = {"count": 0}

    def raise_on_the_second_move(old, new):
        calls["count"] += 1
        if calls["count"] == 2:
            raise NotADirectoryError("a file was planted where a directory had to be")
        return real_move(old, new)

    monkeypatch.setattr(archive_service, "_move_without_clobber", raise_on_the_second_move)

    operation = run_extract(client, headers, "Race.zip", expect_done=False)

    assert operation["status"] == "failed"
    error = next(step for step in operation["steps"] if step["op"] == "extract")["error"]
    assert "a file was planted where a directory had to be" in error
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
    """`_twin_rel` builds a name `proposal_engine.twin_key` would never pair.

    Such an extraction would succeed on disk and leave the bundle `archive_only`
    forever, so it is a 400 naming the rename, with nothing written.
    """
    storage = isolated_storage
    write_zip(storage / archive_name, {"Moai.stl": STL_BYTES})
    headers = auth_header(client)
    before = tree(storage)

    status, detail = extract_refusal(client, headers, archive_name)

    assert status == 400, detail
    assert "Rename the archive first" in detail
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

    operation = run_extract(client, headers, "Moai.zip")

    assert step_result(operation, "extract")["destination"] == "Moai"
    assert (storage / "Moai" / "Moai.stl").read_bytes() == STL_BYTES


# --- a self-conflicting selection is a request defect ------------------------


def test_a_member_that_is_another_members_directory_is_refused_before_staging(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    write_zip(storage / "Conflict.zip", {"sub": b"file", "sub/file.stl": b"file"})
    headers = auth_header(client)
    before = tree(storage)

    operation = run_extract(client, headers, "Conflict.zip", expect_done=False)

    assert operation["status"] == "failed"
    detail = next(step for step in operation["steps"] if step["op"] == "extract")["error"]
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
    before = tree(storage)

    status, detail = extract_refusal(client, headers, "sub/Evil.zip")

    assert status == 400, detail
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
    before = tree(storage)

    status, detail = extract_refusal(client, headers, "Dot.zip")

    assert status == 400, detail
    assert " . " in detail
    assert tree(storage) == before
    assert not (storage / "Dot").exists(), "the twin is never the member's own file"
    assert staging_residue(storage) == []


def test_a_padded_dot_member_at_the_root_is_refused(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    write_zip(storage / "Evil.zip", {".. /evil.stl": b"escape"})
    headers = auth_header(client)
    before = tree(storage)

    status, detail = extract_refusal(client, headers, "Evil.zip")

    assert status == 400, detail
    assert ".. /evil.stl" in detail
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

    operation = run_extract(client, headers, "Notes.zip")

    assert step_result(operation, "extract")["files"] == 2
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
    before = tree(storage)

    status, detail = extract_refusal(client, headers, "Big.zip")

    assert status == 413, detail
    assert "20 bytes" in detail
    assert "IMPORT_ZIP_MAX_MB" in detail
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
    before = tree(storage)

    operation = run_extract(client, headers, "Lies.zip", expect_done=False)

    assert operation["status"] == "failed"
    detail = next(step for step in operation["steps"] if step["op"] == "extract")["error"]
    assert "IMPORT_ZIP_MAX_MB" in detail
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
    before = tree(storage)

    status, detail = extract_refusal(client, headers, "Giant.zip")

    assert status == 413, detail
    assert "big.stl" in detail, "the refusal names the entry that is too big"
    assert "IMPORT_MAX_MB" in detail
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
    before = tree(storage)

    operation = run_extract(client, headers, "Giant.zip", expect_done=False)

    assert operation["status"] == "failed"
    detail = next(step for step in operation["steps"] if step["op"] == "extract")["error"]
    assert "IMPORT_MAX_MB" in detail
    assert tree(storage) == before
    assert not (storage / "Giant").exists()
    assert staging_residue(storage) == []


# --- containment on the request path itself --------------------------------


def test_a_request_path_outside_the_library_is_never_read(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    headers = auth_header(client)
    (storage / "notes.txt").write_text("not a zip", encoding="utf-8")
    before = tree(storage)

    for path in ("../../etc/passwd", "/etc/passwd"):
        status, detail = extract_refusal(client, headers, path)
        assert status == 400, (path, detail)

    assert plan_extract(client, headers, "missing.zip").status_code == 404
    assert plan_extract(client, headers, "notes.txt").status_code == 415
    assert tree(storage) == before
    # A hidden twin would be pruned by every scan, so the extraction would exist
    # on disk and nowhere the product can see it: refused before any read.
    write_zip(storage / ".Moai.zip", {"Moai.stl": STL_BYTES})
    before_hidden = tree(storage)
    status, detail = extract_refusal(client, headers, ".Moai.zip")
    assert status == 400, detail
    assert tree(storage) == before_hidden
    assert tree(storage)["notes.txt"] == b"not a zip"


# --- audit ------------------------------------------------------------------


def test_an_idempotent_extract_writes_no_audit_row_and_a_real_one_writes_exactly_one(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    archive = f"Audit-{uuid4().hex}.zip"
    write_zip(storage / archive, {"Moai.stl": STL_BYTES})
    headers = auth_header(client)
    first_value = f"archive-{uuid4().hex}"

    plan = planned_extract(client, headers, archive)
    submitted = client.post(
        "/operations",
        json={"plan_id": plan["plan_id"], "idempotency_key": "archive-audit"},
        headers={**headers, "X-Request-ID": first_value},
    )
    assert submitted.status_code == 202, submitted.text
    operations_runner.dispatch_once()
    operations_runner.wait_idle(timeout=30)

    rows = events_for(first_value)
    assert [row.action for row in rows] == [audit.OPERATION_SUBMIT], "the submit is the route's own row"
    with Session(engine) as session:
        extract_rows = list(
            session.exec(
                select(AuditEvent).where(
                    AuditEvent.action == audit.ARCHIVE_EXTRACT,
                    AuditEvent.target == archive,
                )
            )
        )
    assert len(extract_rows) == 1, "the primitive wrote the operator's extract row"
    row = extract_rows[0]
    assert row.request_id == "-" and row.method == "RUNNER"
    assert json.loads(row.detail_json) == {
        "destination": Path(archive).stem,
        "files": 1,
        "bytes": len(STL_BYTES),
        "scan_started": False,
    }

    run_extract(client, headers, archive)
    with Session(engine) as session:
        after = list(
            session.exec(
                select(AuditEvent).where(
                    AuditEvent.action == audit.ARCHIVE_EXTRACT,
                    AuditEvent.target == archive,
                )
            )
        )
    assert len(after) == 1, "an extract that wrote nothing records nothing"
