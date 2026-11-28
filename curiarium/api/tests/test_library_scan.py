"""Tests for the storage-root scanner (filesystem-authoritative library, phase 1)."""
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from typing import Callable, Optional
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, update
from sqlmodel import Session, select

import asset_service
import audit
import library_scan
import main
from config import IMPORT_ALLOWED_EXTS
from db import STORAGE, engine
from library_scan import scan_storage_root
from migrations import run_migrations
from models import Asset, AuditEvent, Folder
from schemas.library import LibraryScanStatusOut


@pytest.fixture(scope="module", autouse=True)
def _scan_schema() -> None:
    """Create the schema the way the app lifespan does.

    The database is normally migrated when the app starts; running this module on
    its own must not depend on some other test having started it first.
    """
    run_migrations(engine)


def _write(rel_path: str, payload: bytes = b"data") -> Path:
    path = STORAGE / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _auth_header(client: TestClient) -> dict:
    health = client.get("/health").json()
    if not health.get("auth_required"):
        return {}
    token = client.post("/login", json={"username": "admin", "password": "super-secret"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


SCAN_WAIT_SECONDS = 10.0


def _scan_is_running(status: dict) -> bool:
    """A run holds the slot, so a second start must be refused."""
    return bool(status["running"])


def _scan_has_stopped(status: dict) -> bool:
    """No run holds the slot, whatever it managed to do."""
    return not status["running"]


def _scan_is_finished(status: dict) -> bool:
    """A run completed: it no longer runs and it stamped its end."""
    return not status["running"] and status["finished_at"] is not None


def _wait_for_scan_status(read_status: Callable[[], dict], done: Callable[[dict], bool], *, message: str) -> dict:
    """Poll `read_status` until `done` holds, on a clock-based deadline.

    Never a fixed iteration count: a slow host must fail with the last payload it
    saw, not with "50 tries were not enough".
    """
    deadline = time.monotonic() + SCAN_WAIT_SECONDS
    while True:
        payload = read_status()
        if done(payload):
            return payload
        if time.monotonic() >= deadline:
            raise AssertionError(f"{message}: last status was {payload}")
        time.sleep(0.01)


def _assets_by_path() -> dict:
    with Session(engine) as session:
        return {a.storage_path: a for a in session.exec(select(Asset)).all()}


def _cleanup(paths, dirs=("Props", "Loose")):
    with Session(engine) as session:
        for asset in session.exec(select(Asset).where(Asset.storage_path.in_(paths))).all():
            session.delete(asset)
        for folder in session.exec(select(Folder)).all():
            if folder.name in dirs or folder.name == "Gears":
                session.delete(folder)
        session.commit()
    for rel in paths:
        (STORAGE / rel).unlink(missing_ok=True)


@pytest.fixture
def library_root():
    created = []
    yield created
    _cleanup(created)


def test_scan_registers_files_where_they_lie(library_root):
    _write("Props/Gears/gear.stl", b"solid gear\n")
    _write("Props/assembly.pdf", b"%PDF-1.4\n")
    _write("Loose/loose.stl", b"solid loose\n")
    library_root.extend(["Props/Gears/gear.stl", "Props/assembly.pdf", "Loose/loose.stl"])
    # app-owned and hidden entries must be ignored
    _write("thumbs/ignored.jpg", b"junk")
    _write(".hidden.stl", b"hidden")

    scan_storage_root()

    found = _assets_by_path()
    for rel in library_root:
        assert rel in found, f"{rel} was not registered"
        # the row points at the file that is already on disk, unchanged
        assert found[rel].filename == Path(rel).name
        assert (STORAGE / found[rel].storage_path).exists()
        assert found[rel].size > 0
    assert "thumbs/ignored.jpg" not in found
    assert ".hidden.stl" not in found

    with Session(engine) as session:
        names = {f.name for f in session.exec(select(Folder)).all()}
    assert {"Props", "Gears", "Loose"} <= names
    assert found["Props/Gears/gear.stl"].folder_id is not None


def test_a_scan_stamps_the_scan_door_and_no_actor(library_root):
    """A scan is background work: no principal, and no audit row for a creation."""
    rel = "Loose/attributed.stl"
    _write(rel, b"solid attributed\n")
    library_root.append(rel)

    scan_storage_root()

    row = _assets_by_path()[rel]
    assert (row.created_by, row.created_via) == (None, "scan")
    with Session(engine) as session:
        events = list(
            session.exec(
                select(AuditEvent).where(AuditEvent.action == audit.ASSET_CREATE, AuditEvent.target == row.id)
            )
        )
    assert events == []


def test_scan_is_idempotent(library_root):
    rel = "Loose/run-twice.stl"
    _write(rel, b"solid x\n")
    library_root.append(rel)

    scan_storage_root()
    first = len(_assets_by_path())
    status = scan_storage_root()

    assert len(_assets_by_path()) == first
    assert status.registered == 0
    assert status.known >= 1


def test_missing_files_are_counted_not_deleted(library_root):
    rel = "Loose/vanishing.stl"
    path = _write(rel, b"solid gone\n")
    library_root.append(rel)

    scan_storage_root()
    assert rel in _assets_by_path()

    path.unlink()
    status = scan_storage_root()

    assert status.missing >= 1
    # the row survives: a mount hiccup must not silently lose the catalogue entry
    assert rel in _assets_by_path()
    assert _assets_by_path()[rel].missing_since is not None
    # Keep it in `library_root`: the fixture's cleanup owns every row this test
    # made, so dropping the path here leaks a row into the shared session DB.


def test_scan_endpoint_returns_before_the_background_run_finishes(
    library_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The POST answers while the worker is still parked mid-run.

    `library_scan._run_scan` resolves `scan_storage_root` through the module
    global at call time, so patching that name is what gates the background
    thread. The worker is held on an Event this test owns, which is why the
    status read below can never race the walk finishing.
    """
    rel = "Loose/background-proof.stl"
    _write(rel, b"solid background\n")
    library_root.append(rel)

    real_scan = library_scan.scan_storage_root
    parked = threading.Event()
    release = threading.Event()

    def parked_scan(state=None):
        parked.set()
        # Bounded, not conditional: if a failure stopped the test before it could
        # release, the worker still delegates and the run closes itself, instead
        # of leaving the module-global run slot claimed for the rest of the
        # session. The bound is well past the test's own deadline, so a parked
        # assertion below can never lose a race with this timeout.
        release.wait(timeout=SCAN_WAIT_SECONDS * 3)
        return real_scan(state=state)

    monkeypatch.setattr(library_scan, "scan_storage_root", parked_scan)

    with TestClient(main.app) as client:
        headers = _auth_header(client)
        # The POST is inside the try: it claims the run slot, so any assertion
        # from here on must still go through the release below.
        try:
            start = client.post("/library/scan", headers=headers)
            assert start.status_code == 202, start.text
            assert parked.wait(timeout=SCAN_WAIT_SECONDS), "the background run never reached the scanner"
            status = client.get("/library/scan", headers=headers)
            assert status.status_code == 200, status.text
            # The work is provably unfinished: the worker is parked on `release`.
            assert status.json()["running"] is True
            assert status.json()["finished_at"] is None
        finally:
            # A failed assertion must not leave the daemon thread holding the run
            # slot, or every later start_scan() would be refused.
            release.set()
            final = _wait_for_scan_status(
                lambda: client.get("/library/scan", headers=headers).json(),
                _scan_is_finished,
                message="the released background run never finished",
            )
        assert final["registered"] >= 1

    assert rel in _assets_by_path()


def test_scan_endpoint_runs_in_background(library_root):
    rel = "Loose/from-api.stl"
    _write(rel, b"solid api\n")
    library_root.append(rel)

    with TestClient(main.app) as client:
        headers = _auth_header(client)
        start = client.post("/library/scan", headers=headers)
        assert start.status_code == 202, start.text
        # `running` is not asserted here: the endpoint may already have finished
        # the walk. Test A owns that proof. The endpoint also keeps the *last*
        # finished run visible, so `started_at` is the per-run identity that
        # distinguishes this run from an earlier finished one.
        started_at = start.json()["started_at"]
        assert started_at is not None

        def this_run_finished(status: dict) -> bool:
            return status["started_at"] == started_at and _scan_is_finished(status)

        status = _wait_for_scan_status(
            lambda: client.get("/library/scan", headers=headers).json(),
            this_run_finished,
            message="the started run never finished",
        )
        assert status["registered"] >= 1

    assert rel in _assets_by_path()


# --- batched, bounded-memory scanning (Mf-025u) ------------------------------
#
# Everything below scans a temp root instead of the shared test storage. The
# database is shared for the whole session, so rows created here are removed at
# teardown and `missing` is only ever asserted as a delta: a scan of a temp root
# makes every other test's rows look absent.


def _write_under(root: Path, rel_path: str, payload: bytes = b"data") -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _library_rows(prefix: str) -> dict:
    """Assets whose storage_path starts with `prefix`, keyed by storage path."""
    with Session(engine) as session:
        return {
            asset.storage_path: asset
            for asset in session.exec(select(Asset)).all()
            if (asset.storage_path or "").startswith(prefix)
        }


def _delete_rows_under(prefix: str) -> None:
    """Delete the asset rows under `prefix`, leaving the files on disk."""
    with Session(engine) as session:
        for asset in session.exec(select(Asset)).all():
            if (asset.storage_path or "").startswith(prefix):
                session.delete(asset)
        session.commit()


def _forge_name_collision(monkeypatch: pytest.MonkeyPatch, *rel_paths: str):
    """Make the naming pre-pass hand these files one identical display name.

    Mf-tkdp derives a name *around* every taken name, so a plain tree can no
    longer produce a unique-index rejection - which is the point of the ticket.
    The tests below still pin what happens *after* one (a rejected row fails only
    itself, and the counts still reconcile), so the rejection is injected at the
    one seam that can still produce it: the pure derivation. Returns the real
    function so a test that needs a later run to derive honestly can restore it.
    """
    forged = {Path(rel_path).name for rel_path in rel_paths}
    real = library_scan.derive_asset_names

    def colliding(present, wanted, taken):
        names = real(present, wanted, taken)
        return {
            filename: ("forge-collision" if filename in forged else name)
            for filename, name in names.items()
        }

    monkeypatch.setattr(library_scan, "derive_asset_names", colliding)
    return real


def _folders_named(name: str) -> list:
    with Session(engine) as session:
        return [
            (folder.id, folder.storage_path)
            for folder in session.exec(select(Folder)).all()
            if folder.name == name
        ]


def _delete_folders_named(name: str) -> None:
    with Session(engine) as session:
        for folder in list(session.exec(select(Folder))):
            if folder.name == name:
                session.delete(folder)
        session.commit()


def _asset_by_id(asset_id: str) -> Optional[Asset]:
    with Session(engine) as session:
        asset = session.get(Asset, asset_id)
        if asset is not None:
            session.expunge(asset)
    return asset


@pytest.fixture
def temp_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Scan a temp storage root, then delete every row the test created."""
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setattr(library_scan, "STORAGE", storage)
    # The schema is created by the app lifespan; a single-file run needs it first.
    run_migrations(engine)
    with Session(engine) as session:
        folders_before = {folder.id for folder in session.exec(select(Folder))}
        assets_before = {asset.id for asset in session.exec(select(Asset))}
    yield storage
    with Session(engine) as session:
        for folder in list(session.exec(select(Folder))):
            if folder.id not in folders_before:
                session.delete(folder)
        for asset in list(session.exec(select(Asset))):
            if asset.id not in assets_before:
                session.delete(asset)
        session.commit()


def test_scan_commits_in_batches(temp_library: Path) -> None:
    total = 450
    # Converge rows left absent by earlier tests first: the counted scan below
    # must do zero flag writes, so its commit count stays deterministic.
    scan_storage_root()
    for index in range(total):
        _write_under(temp_library, f"batched/dir-{index // 50:02d}/part-{index:03d}.stl", b"solid x\n")

    commits = []

    def count_commit(_connection) -> None:
        commits.append(1)

    event.listen(engine, "commit", count_commit)
    try:
        status = scan_storage_root()
    finally:
        event.remove(engine, "commit", count_commit)

    assert status.registered == total
    assert len(_library_rows("batched/")) == total
    # One commit per batch, with slack for folder lookups and stray threads.
    assert len(commits) <= ceil(total / library_scan.BATCH_SIZE) + 2


def test_scan_fails_only_the_row_that_collides(temp_library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The naming pre-pass steps around every taken name, so a plain same-stem
    # pair no longer collides. Inject the rejection at the derivation seam and
    # pin what a scan does with it: one bad row fails, the rest of the batch lands.
    _write_under(temp_library, "collision/part.stl", b"solid a\n")
    _write_under(temp_library, "collision/part.step", b"step a\n")
    _write_under(temp_library, "collision/other.stl", b"solid b\n")
    _write_under(temp_library, "collision/another.stl", b"solid c\n")
    _forge_name_collision(monkeypatch, "collision/part.stl", "collision/part.step")

    status = scan_storage_root()

    rows = _library_rows("collision/")
    assert len(rows) == 3
    assert {"collision/other.stl", "collision/another.stl"} <= set(rows)
    assert len(set(rows) & {"collision/part.stl", "collision/part.step"}) == 1
    assert status.registered == 3
    assert status.failed == 1
    assert len(status.errors) == 1
    assert status.errors[0].split(":")[0] in {"collision/part.stl", "collision/part.step"}


def test_interrupted_scan_leaves_whole_committed_batches(temp_library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = temp_library
    total = library_scan.BATCH_SIZE * 4
    for index in range(total):
        _write_under(storage, f"interrupted/dir-{index // 100:02d}/part-{index:03d}.stl", b"solid x\n")

    real_walk = library_scan._walk_storage
    walked = 0

    def interrupting_walk(root):
        nonlocal walked
        for item in real_walk(root):
            yield item
            walked += len(item[1])
            if walked >= library_scan.BATCH_SIZE * 1.5:
                raise RuntimeError("walker interrupted")

    monkeypatch.setattr(library_scan, "_walk_storage", interrupting_walk)
    with pytest.raises(RuntimeError, match="walker interrupted"):
        scan_storage_root()
    monkeypatch.setattr(library_scan, "_walk_storage", real_walk)

    rows = _library_rows("interrupted/")
    # Exactly one whole batch landed; the in-flight batch was rolled back.
    assert len(rows) == library_scan.BATCH_SIZE
    for rel_path, asset in rows.items():
        assert (storage / rel_path).stat().st_size == asset.size

    status = scan_storage_root()
    assert status.known == library_scan.BATCH_SIZE
    assert status.registered == total - library_scan.BATCH_SIZE
    assert len(_library_rows("interrupted/")) == total


def test_missing_counts_only_files_that_are_really_gone(temp_library: Path) -> None:
    storage = temp_library
    for name in ("one", "two", "three"):
        _write_under(storage, f"missing-parity/{name}.stl", b"solid x\n")
    first = scan_storage_root()
    assert first.registered == 3
    baseline = first.missing

    (storage / "missing-parity" / "two.stl").unlink()
    second = scan_storage_root()
    assert second.missing == baseline + 1
    assert second.known == 2
    assert second.registered == 0
    # Counted, never purged.
    assert "missing-parity/two.stl" in _library_rows("missing-parity/")

    # A tracked row under a pruned location is not missing while the file is there.
    _write_under(storage, "thumbs/present.jpg", b"junk")
    with Session(engine) as session:
        session.add(
            Asset(
                filename="present.jpg",
                mime="image/jpeg",
                size=4,
                name="missing-parity-thumb",
                storage_path="thumbs/present.jpg",
            )
        )
        session.commit()

    third = scan_storage_root()
    assert third.missing == baseline + 1


# --- missing_since flagging (Mf-itr4) ----------------------------------------
#
# A scan never deletes rows or files: it stamps `missing_since` when it first
# notices an asset's bytes are gone and clears the flag when they come back.
# The flag pass covers every candidate row in the session database, so these
# tests scope their assertions to their own storage paths.


def test_scan_flags_missing_files_without_deleting(temp_library: Path) -> None:
    storage = temp_library
    rel = "flagged/gone.stl"
    path = _write_under(storage, rel, b"solid gone\n")

    first = scan_storage_root()
    rows = _library_rows("flagged/")
    assert set(rows) == {rel}
    asset_id = rows[rel].id
    assert rows[rel].missing_since is None
    baseline = first.missing

    path.unlink()
    second = scan_storage_root()

    rows = _library_rows("flagged/")
    assert set(rows) == {rel}
    assert rows[rel].id == asset_id
    stamped = rows[rel].missing_since
    assert stamped is not None
    parsed = datetime.fromisoformat(stamped)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert not (storage / rel).exists()
    assert second.missing == baseline + 1


def test_scan_clears_missing_flag_when_file_returns(temp_library: Path) -> None:
    storage = temp_library
    rel = "returned/model.stl"
    path = _write_under(storage, rel, b"solid gone\n")

    scan_storage_root()
    asset_id = _library_rows("returned/")[rel].id

    path.unlink()
    scan_storage_root()
    assert _library_rows("returned/")[rel].missing_since is not None

    _write_under(storage, rel, b"solid back\n")
    status = scan_storage_root()

    with Session(engine) as session:
        matching = session.exec(select(Asset).where(Asset.storage_path == rel)).all()
    assert len(matching) == 1
    restored = matching[0]
    assert restored.id == asset_id
    assert restored.missing_since is None
    assert status.known >= 1


def test_a_scan_over_an_absent_root_flags_nothing_and_reports_failures(
    temp_library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed scan cannot empty the queue: an absent root only counts a failure.

    `scan_storage_root` checks the root before it opens a session, so a run over
    a missing (or unmounted) root never reaches the missing-flag pass: rows keep
    whatever stamp the last good scan left on them.
    """
    probe = f"absent-root-{uuid4().hex[:12]}"
    with Session(engine) as session:
        flagged = Asset(
            filename=f"{probe}-flagged.stl",
            mime="model/stl",
            size=10,
            name=f"{probe} flagged",
            storage_path=f"absent/{probe}-flagged.stl",
            missing_since="2024-01-01T00:00:00+00:00",
        )
        present = Asset(
            filename=f"{probe}-present.stl",
            mime="model/stl",
            size=10,
            name=f"{probe} present",
            storage_path=f"absent/{probe}-present.stl",
        )
        session.add(flagged)
        session.add(present)
        session.commit()
        flagged_id, present_id = flagged.id, present.id

    absent_root = temp_library / "absent"
    assert not absent_root.exists()
    monkeypatch.setattr(library_scan, "STORAGE", absent_root)

    status = scan_storage_root()

    assert status.failed >= 1
    # No walk happened, so nothing was registered and no flag was written.
    assert status.registered == 0
    assert status.missing == 0
    assert _asset_by_id(flagged_id).missing_since == "2024-01-01T00:00:00+00:00"
    assert _asset_by_id(present_id).missing_since is None


def test_scan_does_not_flag_reference_assets_when_only_storage_path_is_absent(
    temp_library: Path, tmp_path: Path
) -> None:
    # A mount no-copy row owns no managed file; only its source path matters.
    source = tmp_path / "mount" / "Thing.3mf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"3mf")

    with Session(engine) as session:
        asset = Asset(
            filename="Thing.3mf",
            mime="model/3mf",
            size=3,
            name="reference-thing",
            storage_path="ref-no-copy/Thing.3mf",
            source_path=str(source),
            is_reference=1,
        )
        session.add(asset)
        session.commit()
        session.refresh(asset)
        asset_id = asset.id

    # MOUNT_IMPORT_PATH is deliberately unset: presence must not depend on the
    # mount allowlist that governs serving.
    scan_storage_root()
    assert _asset_by_id(asset_id).missing_since is None

    source.unlink()
    scan_storage_root()
    assert _asset_by_id(asset_id).missing_since is not None


def test_asset_list_exposes_missing_since(temp_library: Path) -> None:
    stamped = "2026-01-02T03:04:05+00:00"
    with Session(engine) as session:
        asset = Asset(
            filename="flagged.stl",
            mime="model/stl",
            size=1,
            name="api-flagged",
            storage_path="api-flag/flagged.stl",
            missing_since=stamped,
        )
        session.add(asset)
        session.commit()
        session.refresh(asset)
        asset_id = asset.id

    with TestClient(main.app) as client:
        headers = _auth_header(client)
        response = client.get("/assets?limit=1000", headers=headers)
    assert response.status_code == 200, response.text
    entries = {item["id"]: item for item in response.json()}
    assert entries[asset_id]["missing_since"] == stamped


def test_delete_asset_with_missing_file_succeeds(temp_library: Path) -> None:
    with Session(engine) as session:
        asset = Asset(
            filename="missing.stl",
            mime="model/stl",
            size=1,
            name="deletable",
            storage_path="deleted/missing.stl",
        )
        session.add(asset)
        session.commit()
        session.refresh(asset)
        asset_id = asset.id

    with TestClient(main.app) as client:
        headers = _auth_header(client)
        response = client.delete(f"/asset/{asset_id}", params={"confirm_name": "missing.stl"}, headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    # The bytes are already gone, so nothing moves; the row still goes.
    assert body["ok"] is True
    assert body["trash_path"] is None
    assert body["files"] == 1
    assert _asset_by_id(asset_id) is None


def test_missing_flag_writes_are_batched(temp_library: Path) -> None:
    storage = temp_library
    # Converge rows left absent by earlier tests, so the second scan only writes
    # the flags of the 450 rows this test creates.
    scan_storage_root()

    total = 450
    for index in range(total):
        _write_under(storage, f"flagged-batched/dir-{index // 50:02d}/part-{index:03d}.stl", b"solid x\n")

    first = scan_storage_root()
    assert first.registered == total
    assert all(asset.missing_since is None for asset in _library_rows("flagged-batched/").values())

    for path in storage.rglob("*.stl"):
        path.unlink()

    commits = []

    def count_commit(_connection) -> None:
        commits.append(1)

    event.listen(engine, "commit", count_commit)
    try:
        status = scan_storage_root()
    finally:
        event.remove(engine, "commit", count_commit)

    rows = _library_rows("flagged-batched/")
    assert len(rows) == total
    assert all(asset.missing_since is not None for asset in rows.values())
    assert status.failed == 0
    assert len(commits) <= ceil(total / library_scan.BATCH_SIZE) + 2


def test_folder_move_of_missing_asset_keeps_flag(temp_library: Path) -> None:
    storage = temp_library
    rel = "move-missing/model.stl"
    path = _write_under(storage, rel, b"solid gone\n")

    scan_storage_root()
    asset_id = _library_rows("move-missing/")[rel].id
    path.unlink()
    scan_storage_root()
    stamped = _asset_by_id(asset_id).missing_since
    assert stamped is not None

    with TestClient(main.app) as client:
        headers = _auth_header(client)
        created = client.post("/folders", json={"name": "move-missing-target"}, headers=headers)
        assert created.status_code == 200, created.text
        folder_id = created.json()["id"]
        moved = client.post(f"/asset/{asset_id}/folder", json={"folder_id": folder_id}, headers=headers)
        assert moved.status_code == 200, moved.text
        moved_path = moved.json()["storage_path"]
        assert moved.json()["missing_since"] == stamped

    assert moved_path != rel
    # The moved row points at a new path under the app's real storage root; the
    # move rewrote storage_path without creating the (still missing) file.
    assert not (storage / moved_path).exists()
    assert not (STORAGE / moved_path).exists()
    assert _asset_by_id(asset_id).missing_since == stamped

    scan_storage_root()

    rescanned = _asset_by_id(asset_id)
    assert rescanned.storage_path == moved_path
    assert rescanned.missing_since == stamped
    assert not (storage / moved_path).exists()
    assert not (STORAGE / moved_path).exists()


def test_counters_account_for_every_library_file(temp_library: Path) -> None:
    storage = temp_library
    files = ["counters/a.stl", "counters/nested/b.stl", "counters/nested/deep/c.stl"]
    for rel_path in files:
        _write_under(storage, rel_path, b"solid x\n")
    _write_under(storage, "counters/.hidden.stl", b"hidden")
    _write_under(storage, "counters/nested/.hidden.stl", b"hidden")

    first = scan_storage_root()
    assert first.registered == len(files)
    assert first.registered + first.known + first.failed == len(files)
    assert first.ignored == 2

    second = scan_storage_root()
    assert second.registered == 0
    assert second.known == len(files)
    assert second.registered + second.known + second.failed == len(files)


def test_is_skipped_name_is_the_one_rule_both_doors_ask() -> None:
    """The basename predicate the scan registers by, and what a hidden-name door means.

    `classify_entry`/`is_counted_model` ask this exact function, so the truth
    table is the shared contract rather than a copy of one door's behaviour.
    Mutation: change either half of the expression (a debris name admitted, or the
    hidden half made unconditional) and a line below reds. It is a pure
    predicate, so the REGISTRATION side - what `_register_tree` actually skips -
    is pinned by `test_a_nested_macosx_tree_registers_its_dead_file_and_skips_the_sidecar`
    and `test_a_hidden_directory_is_never_walked` below, not here.
    """
    skipped = {
        "plain.stl": False,
        "._plain.stl": True,
        ".hidden.stl": True,
        "__MACOSX": False,
        "Thumbs.db": True,
        ".DS_Store": True,
        "desktop.ini": True,
    }
    for name, expected in skipped.items():
        assert library_scan.is_skipped_name(name) is expected, name

    # `include_hidden=True` admits a dot NAME but never a debris name: the two
    # halves of the predicate are independent.
    assert library_scan.is_skipped_name("._plain.stl", include_hidden=True) is False
    assert library_scan.is_skipped_name(".hidden.stl", include_hidden=True) is False
    assert library_scan.is_skipped_name(".DS_Store", include_hidden=True) is True
    assert library_scan.is_skipped_name("Thumbs.db", include_hidden=True) is True


def test_a_nested_macosx_tree_registers_its_dead_file_and_skips_the_sidecar(
    temp_library: Path,
) -> None:
    """The premise the whole Mf-94od fix rests on, measured through `_register_tree`.

    The scan REGISTERS a non-dot file under a nested `__MACOSX` - only its dot
    NAMES are skipped - which is why the detector may not be wired to
    `entry_policy.is_junk_entry` (that vocabulary refuses `__MACOSX` outright and
    would UNDER-count a real part). Recorded as a docstring claim in three places
    before this test existed; an absence-only assertion cannot red when someone
    changes the rule, and this one can.

    Mutation: skip anything under a `__MACOSX` directory and `dead` stops
    registering while `run.registered` and `run.ignored` both move.
    """
    storage = temp_library
    _write_under(storage, "mac/real.stl", b"solid real\n")
    _write_under(storage, "mac/__MACOSX/mac/dead.stl", b"solid dead\n")
    _write_under(storage, "mac/__MACOSX/mac/._real.stl", b"sidecar\n")

    run = scan_storage_root()

    assert set(_library_rows("mac/")) == {"mac/real.stl", "mac/__MACOSX/mac/dead.stl"}
    assert run.registered == 2
    assert run.ignored == 1, "exactly the `._*` sidecar, by name"


def test_a_hidden_directory_is_never_walked(temp_library: Path) -> None:
    """The walk's OWN dot rule, which `is_skipped_name` deliberately does not cover.

    `_register_tree` asks the predicate about a NAME; pruning a hidden DIRECTORY
    is a different question answered by `_walk_storage`, and until Mf-94od nothing
    could red if that prune were deleted. The two halves are independent, so a
    tree can hold a hidden directory that would otherwise register a real model.

    Mutation: drop `if not include_hidden and entry.name.startswith(".")` and
    `g/.secret/x.stl` registers, so `run.registered` and the walked set move.
    """
    storage = temp_library
    _write_under(storage, "g/.secret/x.stl", b"solid hidden\n")
    _write_under(storage, "g/kept.stl", b"solid kept\n")

    run = scan_storage_root()

    assert set(_library_rows("g/")) == {"g/kept.stl"}
    assert run.registered == 1

    walked = [rel_dir for rel_dir, _entries, _ignored in library_scan._walk_storage(storage)]
    assert walked == ["", "g"]
    # The prune is a DOOR, not a refusal: asking with it open walks the directory
    # (the mount door uses this for a source whose dot-directories are content).
    walked_open = [
        rel_dir for rel_dir, _entries, _ignored in library_scan._walk_storage(storage, include_hidden=True)
    ]
    assert walked_open == ["", "g", "g/.secret"]


def test_folders_come_only_from_directories_that_register_files(temp_library: Path) -> None:
    storage = temp_library
    _write_under(storage, "revisited/kept.stl", b"solid kept\n")
    _write_under(storage, "hidden-only/.secret.stl", b"hidden")
    first = scan_storage_root()
    assert first.registered == 1
    assert len(_folders_named("revisited")) == 1
    _delete_folders_named("revisited")

    second = scan_storage_root()
    assert second.registered == 0
    assert second.known == 1
    # Neither a directory holding only tracked files nor one holding only a
    # hidden file gets a folder row.
    assert _folders_named("revisited") == []
    assert _folders_named("hidden-only") == []

    for index in range(library_scan.BATCH_SIZE + 50):
        _write_under(storage, f"fresh/part-{index:03d}.stl", b"solid new\n")
    third = scan_storage_root()
    assert third.registered == library_scan.BATCH_SIZE + 50
    # One row for the directory, even though its files span two batches.
    folders = _folders_named("fresh")
    assert len(folders) == 1
    # a scanned folder IS its directory (was None while the scan wrote
    # metadata-only rows; see test_scanning_folders_records_their_directory)
    assert folders[0][1] == "fresh"


def test_unreadable_3mf_still_registers_without_prepared_metadata(temp_library: Path) -> None:
    # inspect_prepared_print swallows BadZipFile, so a corrupt archive is not a
    # registration failure - the startup backfill retries the metadata later.
    _write_under(temp_library, "prepared/broken.3mf", b"not a zip at all")

    status = scan_storage_root()

    rows = _library_rows("prepared/")
    assert set(rows) == {"prepared/broken.3mf"}
    assert rows["prepared/broken.3mf"].prepared_metadata_json is None
    assert status.registered == 1
    assert status.failed == 0
    assert status.errors == []


def test_only_root_level_app_owned_dirs_are_pruned(temp_library: Path) -> None:
    """`thumbs`/`bundles` are app-owned at the storage root only, not below it."""
    _write_under(temp_library, "Props/thumbs/reference.jpg", b"jpeg data\n")
    _write_under(temp_library, "thumbs/ignored.jpg", b"junk")

    with Session(engine) as session:
        folders_before = {folder.id for folder in session.exec(select(Folder))}

    status = scan_storage_root()

    # The nested directory holds ordinary library content; the root one is pruned.
    assert set(_library_rows("Props/")) == {"Props/thumbs/reference.jpg"}
    assert set(_library_rows("thumbs/")) == set()
    assert status.registered == 1
    # Root-level app-owned directories are pruned silently, not counted as debris.
    assert status.ignored == 0

    with Session(engine) as session:
        created = [folder for folder in session.exec(select(Folder)) if folder.id not in folders_before]
    props = next(folder for folder in created if folder.name == "Props")
    created_pairs = {(folder.parent_id, folder.name) for folder in created}
    assert (props.id, "thumbs") in created_pairs
    assert (None, "thumbs") not in created_pairs


# --- batch failure recovery (Mf-025u) ----------------------------------------
#
# `_commit_batch` is replaced through the module global, because `_flush_batch`
# resolves it at call time. The replacement keeps the positional signature
# `_commit_batch(session, pending, cache)`.


def test_transient_batch_failure_is_retried_once(temp_library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = temp_library
    total = library_scan.BATCH_SIZE // 4
    for index in range(total):
        _write_under(storage, f"retried/part-{index:03d}.stl", f"solid {index}\n".encode())

    real_commit = library_scan._commit_batch
    batches = []

    def flaky_commit(session, pending, cache):
        batches.append(len(pending))
        if len(batches) == 1:
            raise RuntimeError("database is locked")
        return real_commit(session, pending, cache)

    monkeypatch.setattr(library_scan, "_commit_batch", flaky_commit)
    status = scan_storage_root()

    expected = {f"retried/part-{index:03d}.stl" for index in range(total)}
    rows = _library_rows("retried/")
    assert status.registered == total
    assert status.failed == 0
    assert status.errors == []
    assert set(rows) == expected
    for rel_path, asset in rows.items():
        assert asset.id
        assert (storage / rel_path).stat().st_size == asset.size
    # One batch: the failure was retried once, not looped over every file.
    assert batches == [total, total]
    # The retried batch still created the directory row exactly once.
    assert len(_folders_named("retried")) == 1


def test_commit_time_batch_failure_reinserts_without_duplicates(
    temp_library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure *after* the row savepoints flushed must roll back and re-insert.

    `test_transient_batch_failure_is_retried_once` fails `_commit_batch` before
    anything reaches the database; here every row has already been flushed into
    the transaction when `Session.commit` refuses, so the retry has to undo the
    rows and the folder row and insert them again - once, not twice.
    """
    storage = temp_library
    total = library_scan.BATCH_SIZE // 4
    for index in range(total):
        _write_under(storage, f"commit-retried/part-{index:03d}.stl", f"solid {index}\n".encode())

    real_commit = Session.commit
    attempts = []
    # A dict, so the closure can flip the flag without a `nonlocal` declaration.
    state = {"raised": False}

    def flaky_commit(session) -> None:
        attempts.append(1)
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError("database is locked")
        real_commit(session)

    monkeypatch.setattr(Session, "commit", flaky_commit)
    status = scan_storage_root()
    commit_attempts = len(attempts)

    expected = {f"commit-retried/part-{index:03d}.stl" for index in range(total)}
    rows = _library_rows("commit-retried/")
    assert status.registered == total
    assert status.failed == 0
    assert status.errors == []
    assert set(rows) == expected
    for rel_path, asset in rows.items():
        assert asset.size == (storage / rel_path).stat().st_size
        assert asset.folder_id is not None
    # Exactly one directory row, created metadata-only by the retried attempt.
    folders = _folders_named("commit-retried")
    assert len(folders) == 1
    # a scanned folder IS its directory (was None while the scan wrote
    # metadata-only rows; see test_scanning_folders_records_their_directory)
    assert folders[0][1] == "commit-retried"
    # One raise plus one real commit: rolled back and re-inserted, not duplicated.
    assert commit_attempts == 2


def test_permanent_batch_failure_fails_the_whole_batch(temp_library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = temp_library
    total = library_scan.BATCH_SIZE // 4
    for index in range(total):
        _write_under(storage, f"uncommittable/part-{index:03d}.stl", b"solid x\n")

    attempts = []

    def failing_commit(session, pending, cache):
        attempts.append(len(pending))
        raise RuntimeError("database is locked")

    monkeypatch.setattr(library_scan, "_commit_batch", failing_commit)
    status = scan_storage_root()

    assert status.registered == 0
    # Every file in the rolled-back batch is accounted for individually, so
    # `failed` still matches the captured texts one for one (Mf-e8zp).
    assert status.failed == total
    assert len(status.errors) == total
    assert status.errors_omitted == 0
    assert all(error.startswith("uncommittable/part-") for error in status.errors)
    assert status.errors[0].startswith("uncommittable/part-")
    # Tried once, retried once, then given up on.
    assert attempts == [total, total]
    assert _library_rows("uncommittable/") == {}


def test_consecutive_batch_failures_stop_the_scan(temp_library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = temp_library
    total = library_scan.MAX_CONSECUTIVE_BATCH_FAILURES * library_scan.BATCH_SIZE
    for index in range(total):
        _write_under(storage, f"circuit/dir-{index // 100:03d}/part-{index:03d}.stl", b"solid x\n")

    def failing_commit(session, pending, cache):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(library_scan, "_commit_batch", failing_commit)
    # The third consecutive failed batch trips the breaker instead of scanning on.
    with pytest.raises(RuntimeError, match="consecutive batches failed to commit"):
        scan_storage_root()

    assert _library_rows("circuit/") == {}


def test_scanning_folders_records_their_directory(library_root):
    """A folder IS a directory, so a scanned folder must carry its path.

    Live finding (2026-10-31): the first real deployment scanned 26 files into 33
    folders and every one had storage_path NULL, because the scan reused the
    metadata-only folder helper. Rename and delete resolve a folder's directory
    from that column, so those folders could not be moved or removed.
    """
    rel = "Props/Gears/scanned.stl"
    _write(rel, b"solid gear\n")
    library_root.append(rel)

    scan_storage_root()

    with Session(engine) as session:
        folders = {f.name: f for f in session.exec(select(Folder)).all()}
    assert folders["Props"].storage_path == "Props"
    assert folders["Gears"].storage_path == "Props/Gears"


def test_rescanning_heals_a_folder_created_without_a_path(library_root):
    """A row from the metadata-only era gains its path on the next scan."""
    rel = "Loose/healed.stl"
    _write(rel, b"solid x\n")
    library_root.append(rel)

    with Session(engine) as session:
        legacy = Folder(name="Loose", tags_json="[]", parent_id=None, storage_path=None)
        session.add(legacy)
        session.commit()

    scan_storage_root()

    with Session(engine) as session:
        healed = session.exec(select(Folder).where(Folder.name == "Loose")).first()
    assert healed is not None
    assert healed.storage_path == "Loose"


# --- per-run state, bounded failure reporting and the run log (Mf-e8zp) -------
#
# A run is its own `ScanState`: the old module-global bag was reset in place at
# the start of the next run, so one scan could zero the numbers a client was
# still reading, and `errors[-20:]` served 20 of 67 failures without saying so.
# Everything below pins the replacements: one run, one state, one summary log
# line, and `failed == len(errors) + errors_omitted`.


def test_a_rejected_file_is_logged_with_its_path_and_error_class(
    temp_library: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A name rejection is injected at the derivation seam (a plain tree can no
    # longer produce one); the rejected row must be readable in the log.
    _write_under(temp_library, "logged/part.stl", b"solid a\n")
    _write_under(temp_library, "logged/part.step", b"step a\n")
    _forge_name_collision(monkeypatch, "logged/part.stl", "logged/part.step")

    with caplog.at_level(logging.WARNING, logger=library_scan.log.name):
        status = scan_storage_root()

    assert status.failed == 1
    rejected_path = ({"logged/part.stl", "logged/part.step"} - set(_library_rows("logged/"))).pop()
    matching = [record for record in caplog.records if record.getMessage().startswith(f"{rejected_path}: ")]
    assert len(matching) == 1
    assert matching[0].levelno == logging.WARNING
    # The class name is what an operator needs to tell a collision from an IO error.
    assert "IntegrityError" in matching[0].getMessage()


def test_a_completed_scan_logs_exactly_one_summary_line(
    temp_library: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_under(temp_library, "summary/one.stl", b"solid x\n")

    with caplog.at_level(logging.INFO, logger=library_scan.log.name):
        status = scan_storage_root()

    summaries = [record for record in caplog.records if record.getMessage().startswith("library scan finished in")]
    assert len(summaries) == 1
    message = summaries[0].getMessage()
    assert summaries[0].levelno == logging.INFO
    assert re.search(r"finished in \d+\.\ds", message)
    snapshot = status.as_dict()
    for counter in ("registered", "known", "ignored", "missing", "failed"):
        assert f"{counter}={snapshot[counter]}" in message
    # The same run is what `scan_status()` serves, counters included.
    assert library_scan.scan_status() == snapshot


def test_an_aborted_scan_still_logs_one_summary_line(
    temp_library: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _write_under(temp_library, "aborted/one.stl", b"solid x\n")

    def exploding_walk(root):
        raise RuntimeError("walker interrupted")
        yield  # pragma: no cover - keeps this a generator so the raise lands mid-walk

    monkeypatch.setattr(library_scan, "_walk_storage", exploding_walk)
    with caplog.at_level(logging.INFO, logger=library_scan.log.name):
        with pytest.raises(RuntimeError, match="walker interrupted"):
            scan_storage_root()

    summaries = [record for record in caplog.records if record.getMessage().startswith("library scan finished in")]
    assert len(summaries) == 1
    assert "failed=1" in summaries[0].getMessage()
    assert any(
        "scan aborted: RuntimeError: walker interrupted" in record.getMessage() for record in caplog.records
    )
    # The aborted run does not leave the module looking like a scan is in flight.
    assert library_scan.scan_status()["running"] is False


def test_failed_count_reconciles_with_captured_texts(
    temp_library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Root-level files are walked first, so the injected collision pair lands in
    # the first batch that commits and the batch below it is the one that gets
    # rolled back. The rejection is injected because the derivation itself can no
    # longer collide.
    _write_under(temp_library, "part.stl", b"solid a\n")
    _write_under(temp_library, "part.step", b"step a\n")
    _forge_name_collision(monkeypatch, "part.stl", "part.step")
    total = library_scan.BATCH_SIZE
    for index in range(total):
        _write_under(temp_library, f"batch-fail/part-{index:03d}.stl", b"solid x\n")

    real_commit = library_scan._commit_batch
    calls = {"n": 0}

    def failing_trailing_batch(session, pending, cache):
        # Calls 2 and 3 are the trailing batch and its one retry.
        calls["n"] += 1
        if calls["n"] in (2, 3):
            raise RuntimeError("database is locked")
        return real_commit(session, pending, cache)

    monkeypatch.setattr(library_scan, "_commit_batch", failing_trailing_batch)
    status = scan_storage_root()

    # 1 row-level rejection (the forged pair) plus the trailing batch and its
    # retry (2 files): the forged pair lands in batch 1, the last 2 files in batch 2.
    assert status.failed == 3
    assert status.failed == len(status.errors) + status.errors_omitted
    assert status.errors_omitted == 0
    for message in status.errors:
        rel_path = message.split(": ", 1)[0]
        assert (temp_library / rel_path).exists(), message


def test_a_truncated_error_list_is_reported_as_a_pair(
    temp_library: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The helper keys on basenames, so two forged names cover every directory; one
    # rejected row per directory makes 5 failures, capped at 3 captured texts.
    forged = []
    for index in range(5):
        _write_under(temp_library, f"truncated-{index}/part.stl", b"solid a\n")
        _write_under(temp_library, f"truncated-{index}/part.step", b"step a\n")
        forged.extend([f"truncated-{index}/part.stl", f"truncated-{index}/part.step"])
    _forge_name_collision(monkeypatch, *forged)

    monkeypatch.setattr(library_scan, "MAX_REPORTED_FAILURES", 3)
    with caplog.at_level(logging.WARNING, logger=library_scan.log.name):
        status = scan_storage_root()

    # The cap bounds the log and the captured list together.
    assert status.failed == 5
    assert len(status.errors) == 3
    assert status.errors_omitted == 2
    notice = [r for r in caplog.records if "further failures are not logged individually" in r.getMessage()]
    assert len(notice) == 1
    # The wire view keeps the pair reconcilable and stays schema-shaped.
    snapshot = library_scan.scan_status()
    assert snapshot["failed"] == len(snapshot["errors"]) + status.errors_omitted


def test_scan_status_fields_match_the_schema() -> None:
    state = library_scan.ScanState()
    assert set(state.as_dict()) == set(LibraryScanStatusOut.model_fields)
    assert "errors_omitted" not in state.as_dict()


def test_two_runs_do_not_share_counters(temp_library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_under(temp_library, "shared-a/part.stl", b"solid a\n")
    _write_under(temp_library, "shared-a/part.step", b"step a\n")
    real_derive = _forge_name_collision(monkeypatch, "shared-a/part.stl", "shared-a/part.step")

    first = scan_storage_root()
    assert first.failed == 1
    assert len(first.errors) == 1
    # The rejected half of the forged collision stays unregistered, so drop it.
    rejected = ({"shared-a/part.stl", "shared-a/part.step"} - set(_library_rows("shared-a/"))).pop()
    (temp_library / rejected).unlink()
    # Withdraw the forge: the surviving sibling is already tracked, and a second
    # forged run would re-collide on a row that is already committed.
    monkeypatch.setattr(library_scan, "derive_asset_names", real_derive)

    _write_under(temp_library, "shared-b/fresh.stl", b"solid fresh\n")
    second = scan_storage_root()

    assert (second.failed, second.errors) == (0, [])
    snapshot = library_scan.scan_status()
    assert snapshot["failed"] == 0
    assert snapshot["errors"] == []
    # `registered` describes only the second run, never the first one's work.
    assert snapshot["registered"] == 1


def test_a_second_scan_is_refused_while_one_is_running(
    temp_library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_under(temp_library, "refused/one.stl", b"solid x\n")
    _write_under(temp_library, "refused/part.stl", b"solid a\n")
    _write_under(temp_library, "refused/part.step", b"step a\n")

    real_walk = library_scan._walk_storage
    entered = threading.Event()
    gate = threading.Event()

    def gated_walk(root):
        entered.set()
        assert gate.wait(timeout=10)
        yield from real_walk(root)

    monkeypatch.setattr(library_scan, "_walk_storage", gated_walk)
    assert library_scan.start_scan() is True
    try:
        assert entered.wait(timeout=10)
        _wait_for_scan_status(
            library_scan.scan_status,
            _scan_is_running,
            message="the started run never showed as running",
        )

        before = library_scan.scan_status()
        assert library_scan.start_scan() is False
        with pytest.raises(library_scan.ScanAlreadyRunning):
            scan_storage_root()
        # A refused run changes nothing about the one holding the slot.
        assert library_scan.scan_status() == before
    finally:
        gate.set()
        _wait_for_scan_status(
            library_scan.scan_status,
            _scan_has_stopped,
            message="the first run never finished",
        )


# --- derived display names (Mf-tkdp) ----------------------------------------
#
# A name is derived per directory before the rows go pending, from the disk
# listing plus the folder's already-taken names. Same tree, same names - on every
# scan, across batch boundaries, and with no rename of an existing row.


def test_a_format_set_and_an_artifact_suffix_all_register(temp_library: Path) -> None:
    _write_under(temp_library, "formats/a.stl", b"solid a\n")
    _write_under(temp_library, "formats/a.3mf", b"3mf a\n")
    _write_under(temp_library, "formats/a.s#5", b"s#5 a\n")

    status = scan_storage_root()

    rows = _library_rows("formats/")
    assert len(rows) == 3
    assert {asset.name for asset in rows.values()} == {"a (stl)", "a (3mf)", "a.s#5"}
    for rel_path, asset in rows.items():
        assert asset.title == asset.name
        assert asset.filename == Path(rel_path).name
    assert status.registered == 3
    assert status.failed == 0


def test_a_single_file_keeps_the_bare_stem(temp_library: Path) -> None:
    _write_under(temp_library, "single/a.stl", b"solid a\n")

    status = scan_storage_root()

    rows = _library_rows("single/")
    assert set(rows) == {"single/a.stl"}
    assert rows["single/a.stl"].name == "a"
    assert status.registered == 1


def test_the_live_electronics_folder_registers_every_file(temp_library: Path) -> None:
    # The live shape: a bare-named row from before the rule existed, then its
    # artefacts. The old row must keep its name and the siblings step around it.
    _write_under(temp_library, "Electronics/multifilament.stl", b"solid m\n")
    scan_storage_root()
    seed = _library_rows("Electronics/")["Electronics/multifilament.stl"]
    seed_snapshot = (seed.id, seed.name, seed.filename)
    assert seed_snapshot[1:] == ("multifilament", "multifilament.stl")

    for suffix in (".fcstd", ".s#5", ".b#9"):
        _write_under(temp_library, f"Electronics/multifilament{suffix}", b"artefact\n")
    second = scan_storage_root()

    rows = _library_rows("Electronics/")
    assert len(rows) == 4
    assert len({asset.name for asset in rows.values()}) == 4
    seed_after = rows["Electronics/multifilament.stl"]
    assert (seed_after.id, seed_after.name, seed_after.filename) == seed_snapshot
    new_names = {rows[f"Electronics/multifilament{suffix}"].name for suffix in (".fcstd", ".s#5", ".b#9")}
    assert new_names == {"multifilament.fcstd", "multifilament.s#5", "multifilament.b#9"}
    assert second.registered == 3
    assert second.failed == 0


def test_the_live_electronics_folder_registers_eleven_rows(temp_library: Path) -> None:
    _write_under(temp_library, "Electronics/multifilament.stl", b"solid m\n")
    scan_storage_root()
    seed = _library_rows("Electronics/")["Electronics/multifilament.stl"]
    seed_snapshot = (seed.id, seed.name, seed.filename)

    artefacts = [
        "multifilament.fcstd",
        "multifilament.brd",
        "multifilament.sch",
        "multifilament.s#3",
        "multifilament.s#5",
        "multifilament.s#7",
        "multifilament.s#9",
        "multifilament.b#3",
        "multifilament.b#5",
        "multifilament.b#9",
    ]
    for name in artefacts:
        _write_under(temp_library, f"Electronics/{name}", b"artefact\n")
    second = scan_storage_root()

    rows = _library_rows("Electronics/")
    assert len(rows) == 11
    assert len({asset.name for asset in rows.values()}) == 11
    seed_after = rows["Electronics/multifilament.stl"]
    assert (seed_after.id, seed_after.name, seed_after.filename) == seed_snapshot
    # Eagle/KiCad artefacts keep their full filenames.
    for name in artefacts:
        assert rows[f"Electronics/{name}"].name == name
    assert second.registered == 10
    assert second.failed == 0


def test_a_format_set_registers_every_member(temp_library: Path) -> None:
    for suffix in (".stl", ".3mf", ".step"):
        _write_under(temp_library, f"set/part{suffix}", b"model\n")

    status = scan_storage_root()

    rows = _library_rows("set/")
    assert len(rows) == 3
    assert {asset.name for asset in rows.values()} == {"part (stl)", "part (3mf)", "part (step)"}
    assert status.failed == 0


def test_a_rescan_does_not_rename_existing_rows(temp_library: Path) -> None:
    _write_under(temp_library, "stable/part.stl", b"solid a\n")
    scan_storage_root()
    original = _library_rows("stable/")["stable/part.stl"]
    snapshot = (original.id, original.name, original.filename, original.storage_path)
    assert snapshot[1] == "part"

    for suffix, expected in ((".3mf", "part (3mf)"), (".step", "part (step)")):
        _write_under(temp_library, f"stable/part{suffix}", b"model\n")
        scan_storage_root()
        rows = _library_rows("stable/")
        row = rows["stable/part.stl"]
        assert (row.id, row.name, row.filename, row.storage_path) == snapshot
        assert rows[f"stable/part{suffix}"].name == expected


def test_the_same_tree_scanned_into_a_cleared_library_gets_the_same_names(temp_library: Path) -> None:
    # The derivation reads the disk listing, not database state, so clearing the
    # rows and rescanning the same tree reproduces the same names.
    for rel_path in ("cleared/part.stl", "cleared/part.3mf", "cleared/part.s#5"):
        _write_under(temp_library, rel_path, b"model\n")
    first = scan_storage_root()
    first_names = {rel_path: asset.name for rel_path, asset in _library_rows("cleared/").items()}
    assert len(first_names) == first.registered

    _delete_rows_under("cleared/")
    assert _library_rows("cleared/") == {}

    second = scan_storage_root()
    second_names = {rel_path: asset.name for rel_path, asset in _library_rows("cleared/").items()}
    assert second_names == first_names
    assert second.registered == len(second_names)


def test_a_format_set_split_across_batches_gets_the_same_names(
    temp_library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Naming a directory happens before any row goes pending, so a batch boundary
    # cannot change a name: this kills "suffix the second one seen".
    dirs = ("split-a", "split-b")
    for directory in dirs:
        for suffix in (".stl", ".3mf", ".step"):
            _write_under(temp_library, f"{directory}/part{suffix}", b"model\n")

    real_batch_size = library_scan.BATCH_SIZE
    monkeypatch.setattr(library_scan, "BATCH_SIZE", 1)
    scan_storage_root()
    first_names = {rel_path: asset.name for rel_path, asset in _library_rows("split-").items()}

    _delete_rows_under("split-")
    monkeypatch.setattr(library_scan, "BATCH_SIZE", real_batch_size)
    scan_storage_root()
    second_names = {rel_path: asset.name for rel_path, asset in _library_rows("split-").items()}

    assert second_names == first_names
    for directory in dirs:
        assert {second_names[f"{directory}/part{suffix}"] for suffix in (".stl", ".3mf", ".step")} == {
            "part (stl)",
            "part (3mf)",
            "part (step)",
        }


def test_names_are_compared_case_insensitively(temp_library: Path) -> None:
    # idx_asset_folder_name_unique is COLLATE NOCASE; casefold() is a superset, so
    # a name SQLite would reject is never handed out.
    _write_under(temp_library, "Case/Part.stl", b"solid a\n")
    _write_under(temp_library, "Case/part.3mf", b"3mf a\n")
    _write_under(temp_library, "Shout/SOLO.stl", b"solid solo\n")
    scan_storage_root()

    case_rows = _library_rows("Case/")
    assert len(case_rows) == 2
    assert {asset.name for asset in case_rows.values()} == {"Part (stl)", "part (3mf)"}

    solo = _library_rows("Shout/")["Shout/SOLO.stl"]
    solo_snapshot = (solo.id, solo.name, solo.filename)
    assert solo.name == "SOLO"

    _write_under(temp_library, "Shout/solo.stl", b"solid solo 2\n")
    scan_storage_root()

    shout_rows = _library_rows("Shout/")
    assert len(shout_rows) == 2
    assert shout_rows["Shout/solo.stl"].name == "solo (stl)"
    solo_after = shout_rows["Shout/SOLO.stl"]
    assert (solo_after.id, solo_after.name, solo_after.filename) == solo_snapshot


LIVE_ELECTRONICS_FILES = [
    "multifilament.stl",
    "multifilament.fcstd",
    "multifilament.brd",
    "multifilament.sch",
    "multifilament.s#3",
    "multifilament.s#5",
    "multifilament.s#7",
    "multifilament.s#9",
    "multifilament.b#3",
    "multifilament.b#5",
    "multifilament.b#9",
]


@pytest.mark.parametrize(
    ("present", "wanted", "taken", "expected"),
    [
        pytest.param(["solo.stl"], ["solo.stl"], set(), {"solo.stl": "solo"}, id="unique-stem"),
        pytest.param(
            ["part.stl", "part.3mf", "part.step"],
            ["part.stl", "part.3mf", "part.step"],
            set(),
            {"part.stl": "part (stl)", "part.3mf": "part (3mf)", "part.step": "part (step)"},
            id="format-set",
        ),
        pytest.param(
            ["multifilament.s#5"], ["multifilament.s#5"], set(), {"multifilament.s#5": "multifilament.s#5"},
            id="artifact-suffix",
        ),
        pytest.param(["assembly.pdf"], ["assembly.pdf"], set(), {"assembly.pdf": "assembly.pdf"}, id="non-naming-ext"),
        pytest.param(["solo.stl"], ["solo.stl"], {"solo"}, {"solo.stl": "solo (stl)"}, id="bare-stem-taken"),
        pytest.param(
            ["solo.stl"], ["solo.stl"], {"solo", "solo (stl)"}, {"solo.stl": "solo (stl) (2)"}, id="escalate"
        ),
        pytest.param(
            ["part (stl)", "part.stl", "part.3mf"],
            ["part (stl)", "part.stl", "part.3mf"],
            set(),
            {"part (stl)": "part (stl)", "part.3mf": "part (3mf)", "part.stl": "part (stl) (2)"},
            id="literal-wins",
        ),
        pytest.param(
            ["Part.stl", "part.3mf"],
            ["Part.stl", "part.3mf"],
            set(),
            {"Part.stl": "Part (stl)", "part.3mf": "part (3mf)"},
            id="casing-preserved",
        ),
        pytest.param(
            ["Part.stl", "PART.stl"],
            ["Part.stl", "PART.stl"],
            set(),
            {"PART.stl": "PART (stl)", "Part.stl": "Part (stl) (2)"},
            id="casefold-tie-break",
        ),
        pytest.param(
            ["solo", "solo.stl"],
            ["solo", "solo.stl"],
            set(),
            {"solo": "solo", "solo.stl": "solo (stl)"},
            id="suffixless-first",
        ),
        pytest.param(
            ["solo.stl"], ["solo.stl"], {"solo (stl)"}, {"solo.stl": "solo"}, id="base-before-suffixed"
        ),
        pytest.param(["x."], ["x."], set(), {"x.": "x."}, id="dot-suffix"),
        pytest.param(
            LIVE_ELECTRONICS_FILES,
            ["multifilament.stl", "multifilament.fcstd", "multifilament.s#5"],
            {"multifilament"},
            {
                "multifilament.stl": "multifilament (stl)",
                "multifilament.fcstd": "multifilament.fcstd",
                "multifilament.s#5": "multifilament.s#5",
            },
            id="live-electronics",
        ),
    ],
)
def test_the_derived_name_table(present, wanted, taken, expected) -> None:
    original_taken = set(taken)
    assert library_scan.derive_asset_names(present, wanted, taken) == expected
    # Pure: the caller's taken-set is not mutated.
    assert taken == original_taken


def test_an_upload_name_is_respected_by_a_later_scan(temp_library: Path) -> None:
    # Upload/scan coherence: an upload's name is a taken name the scan derives
    # around, while the upload door keeps its 409 for a scan-registered name.
    _write_under(temp_library, "Coexist/part.stl", b"solid a\n")
    with Session(engine) as session:
        folder_id = library_scan.resolve_dir_folder(session, "Coexist", {}, with_storage_path=True)
        session.add(
            Asset(
                name="part",
                title="part",
                filename="part.stl",
                mime="model/stl",
                size=(temp_library / "Coexist/part.stl").stat().st_size,
                storage_path="Coexist/part.stl",
                folder_id=folder_id,
            )
        )
        session.commit()

    uploaded = _library_rows("Coexist/")["Coexist/part.stl"]
    uploaded_snapshot = (uploaded.id, uploaded.name, uploaded.filename)

    _write_under(temp_library, "Coexist/part.3mf", b"3mf a\n")
    status = scan_storage_root()

    rows = _library_rows("Coexist/")
    assert status.registered == 1
    assert rows["Coexist/part.3mf"].name == "part (3mf)"
    untouched = rows["Coexist/part.stl"]
    assert (untouched.id, untouched.name, untouched.filename) == uploaded_snapshot

    # The upload door keeps its 409; the scan steps around the upload's names,
    # not the other way round.
    with Session(engine) as session:
        with pytest.raises(HTTPException) as excinfo:
            asset_service.unique_model_name(
                session, asset_service.model_name_from_filename("part.3mf"), folder_id
            )
    assert excinfo.value.status_code == 409


# --- bundle-member vocabulary (Mf-1agz) -------------------------------------
# Moved here with the helpers: `library_scan` owns the tables and the pure
# helpers now, so these tests import from the module that defines them.

CLOCKWORK_ROOT = "17_ClockworkMonster"


def test_the_suffix_tables_are_the_shared_ones() -> None:
    """Proves the reuse: a second private table would silently drift."""
    assert library_scan.MODEL_SUFFIXES == frozenset(IMPORT_ALLOWED_EXTS - {".zip"})
    assert library_scan.IMAGE_SUFFIXES == {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


@pytest.mark.parametrize(
    ("rel_path", "role"),
    [
        ("part.stl", library_scan.MODEL),
        ("part.STL", library_scan.MODEL),
        ("part.3MF", library_scan.MODEL),
        ("part.step", library_scan.MODEL),
        ("bundle.zip", library_scan.ARCHIVE),
        ("bundle.ZIP", library_scan.ARCHIVE),
        ("notes.txt", library_scan.TEXT),
        ("guide.md", library_scan.TEXT),
        ("manual.pdf", library_scan.TEXT),
        ("page.html", library_scan.TEXT),
        ("preview.jpg", library_scan.IMAGE),
        ("preview.PNG", library_scan.IMAGE),
        ("preview.webp", library_scan.IMAGE),
        ("README", library_scan.OTHER),
        ("archive.7z", library_scan.OTHER),
        ("", library_scan.OTHER),
    ],
)
def test_classify_entry_reads_the_suffix_only(rel_path: str, role: str) -> None:
    assert library_scan.classify_entry(rel_path, 0) == role


def test_display_name_strips_the_ordinal_and_the_underscores() -> None:
    assert library_scan.display_name("2493386_Moai_statue_-No_overhang") == "Moai statue -No overhang"


@pytest.mark.parametrize("name", ["", "   ", "_", "123_"])
def test_display_name_is_never_empty(name: str) -> None:
    assert library_scan.display_name(name)


DUPLICATE_DIGEST = "a" * 64


def identity(rel_path: str, size: int, digest: str = DUPLICATE_DIGEST) -> library_scan.ArchiveIdentity:
    return library_scan.ArchiveIdentity(rel_path=rel_path, size=size, digest=digest)


def duplicate_group(paths: tuple, size: int = 500, digest: str = DUPLICATE_DIGEST) -> library_scan.DuplicateGroup:
    return library_scan.DuplicateGroup(digest=digest, size=size, paths=paths)


def test_equal_size_archive_groups_keeps_only_shared_sizes() -> None:
    """A size that occurs once is never hashed: it cannot be a duplicate."""
    groups = library_scan.equal_size_archive_groups([("a.zip", 10), ("b.zip", 10), ("c.zip", 11)])

    assert groups == ((10, ("a.zip", "b.zip")),)


def test_duplicate_groups_puts_the_shortest_name_first() -> None:
    group = library_scan.duplicate_groups([identity("x/Copy (1).zip", 500), identity("x/Copy.zip", 500)])

    assert len(group) == 1
    assert group[0].digest == DUPLICATE_DIGEST
    assert group[0].size == 500
    assert group[0].paths[0] == "x/Copy.zip", "the browser duplicate appends characters, so the shortest is the original"
    assert group[0].reclaimable_bytes == 500


def test_duplicate_groups_ignores_a_lone_digest() -> None:
    assert library_scan.duplicate_groups([identity("only.zip", 500)]) == ()


def test_duplicate_groups_ignores_equal_sizes_with_different_digests() -> None:
    archives = [identity("a.zip", 500, digest="a" * 64), identity("b.zip", 500, digest="b" * 64)]

    assert library_scan.duplicate_groups(archives) == ()


def test_duplicate_archive_paths_excludes_the_authoritative_path() -> None:
    groups = (duplicate_group(("x/Copy.zip", "x/Copy (1).zip", "x/Copy (2).zip")),)

    assert library_scan.duplicate_archive_paths(groups) == ("x/Copy (1).zip", "x/Copy (2).zip")


def test_duplicate_report_text_is_empty_without_groups() -> None:
    assert library_scan.duplicate_report_text(()) == ""


def test_duplicate_report_text_keeps_one_path_and_lists_the_copies() -> None:
    groups = (duplicate_group(("x/Copy.zip", "x/Copy (1).zip"), size=500),)

    text = library_scan.duplicate_report_text(groups)
    lines = text.splitlines()

    assert lines[0] == "# duplicate archives: 1 group(s), 500 bytes reclaimable"
    assert f"# sha256 {DUPLICATE_DIGEST} size 500 reclaimable 500" in lines
    assert "keep\t500\tx/Copy.zip" in lines
    assert "duplicate\t500\tx/Copy (1).zip" in lines


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("5390184_ARTICULATED_ice_dragon__fixed_", "5390184"),
        ("2024_parts", "2024"),
        ("2024-10-31_backup", None),
        ("my_5390184_thing", None),
        ("606cdf0d5199b_thing", None),
        ("539_x", None),
        ("ARTICULATED_ice_5390184", None),
    ],
)
def test_the_id_in_a_name_needs_a_digit_floor_a_separator_and_the_start(name: str, expected: Optional[str]) -> None:
    """The extractor's four decisions, each killable by one mutation.

    * ``\\d{4,}`` -> ``\\d+`` reds ``539_x`` (the four-digit floor);
    * the mandatory ``_`` -> ``_?`` reds ``2024-10-31_backup`` (a date is not an
      export);
    * dropping ``^`` AND switching ``.match`` for ``.search`` reds
      ``my_5390184_thing`` (the anchor).

    Dropping ``^`` alone, or ``.match`` -> ``.search`` with ``^`` kept, are
    provable no-ops: ``.match`` already anchors at position 0 and ``^`` already
    anchors inside ``.search``, so the combined mutation is the killable one.
    ``2024_parts`` is the knowingly accepted false positive of the digit floor;
    it is pinned so a stricter regex that starts refusing it is a visible change.
    """
    assert library_scan.thingiverse_id_from_name(name) == expected


def test_is_counted_entry_asks_the_scan_its_own_predicate() -> None:
    """One rule, two doors: the detector re-exports the scanner's own predicate.

    The truth table includes the case the plan gate had to correct - a non-dot
    file under a nested ``__MACOSX`` is REGISTERED - and ``include_hidden=True``
    is asked of the same function rather than re-derived. Mutation: give the
    detector its own copy of the rule and the identity assertions red before any
    count can drift.
    """
    assert library_scan.is_counted_entry("a.stl") is True

    counted = {
        "a.stl": True,
        "._a.stl": False,
        ".hidden.stl": False,
        "__MACOSX/a.stl": True,
        "g/__MACOSX/g/plain.stl": True,
        "g/plain.stl": True,
        "Thumbs.db": False,
    }
    for rel_path, expected in counted.items():
        assert library_scan.is_counted_entry(rel_path) is expected, rel_path

    # The nested `__MACOSX` entry above is the premise the whole fix rests on, and
    # the SCANNER side of it is pinned where it belongs:
    # `test_library_scan.py::test_a_nested_macosx_tree_registers_its_dead_file_and_skips_the_sidecar`.

    # The hidden door is the SAME function, so admitting dot names is one flag.
    assert library_scan.is_skipped_name("._a.stl", include_hidden=True) is False
    assert library_scan.is_skipped_name(".hidden.stl", include_hidden=True) is False
    assert library_scan.is_skipped_name(".DS_Store", include_hidden=True) is True


# --- the explicit register door (Mf-u2el) ------------------------------------
#
# `register(paths)` is named its files; it never walks for discovery. Every test
# below uses `temp_library` - an isolated storage root with
# `library_scan.STORAGE` monkeypatched - and asserts only against rows it made,
# because the database is shared for the whole session.


def _asset_row_count() -> int:
    with Session(engine) as session:
        return len(session.exec(select(Asset.id)).all())


def _folder_row_count() -> int:
    with Session(engine) as session:
        return len(session.exec(select(Folder.id)).all())


def _stamp_missing_since(rel_paths, stamp: str) -> None:
    """Flag rows the way a scan would, without walking a whole root."""
    with Session(engine) as session:
        ids = session.exec(select(Asset.id).where(Asset.storage_path.in_(list(rel_paths)))).all()
        session.execute(update(Asset).where(Asset.id.in_(list(ids))).values(missing_since=stamp))
        session.commit()


def _delete_folders_under(prefix: str) -> None:
    with Session(engine) as session:
        for folder in list(session.exec(select(Folder))):
            if (folder.storage_path or "").startswith(prefix):
                session.delete(folder)
        session.commit()


def _row_shape(prefix: str) -> dict:
    """``storage_path -> (name, filename, folder's storage_path)`` for a prefix."""
    with Session(engine) as session:
        folders = {folder.id: folder.storage_path for folder in session.exec(select(Folder)).all()}
        return {
            asset.storage_path: (asset.name, asset.filename, folders.get(asset.folder_id))
            for asset in session.exec(select(Asset)).all()
            if (asset.storage_path or "").startswith(prefix)
        }


def test_register_writes_exactly_the_named_paths(temp_library: Path) -> None:
    """Mutation: register the named directory's whole listing instead of the names.

    An unnamed sibling sitting beside the named file is the witness: a walk-shaped
    implementation registers it and `set(rows)` reds.
    """
    _write_under(temp_library, "reg-exact/named.stl", b"solid named\n")
    _write_under(temp_library, "reg-exact/sibling.stl", b"solid sibling\n")
    # a file at the root of storage exercises the empty `rel_dir`"s listing too
    _write_under(temp_library, "reg-root-only.stl", b"solid root\n")

    result = library_scan.register(["reg-exact/named.stl", "reg-root-only.stl"])

    assert [(entry.rel_path, entry.created) for entry in result.entries] == [
        ("reg-exact/named.stl", True),
        ("reg-root-only.stl", True),
    ]
    assert result.known == ()
    assert result.absent == ()
    assert result.failed == ()
    assert set(_library_rows("reg-exact/")) == {"reg-exact/named.stl"}
    assert set(_library_rows("reg-root-only.stl")) == {"reg-root-only.stl"}
    assert result.state.registered == 2
    assert result.state.known == 0


def test_register_reports_a_known_path_without_writing_a_second_row(temp_library: Path) -> None:
    """Mutation: call a duplicate or an already-tracked path newly created.

    `created`/`known` must answer "nothing was written", the row count must stay
    at one, and a path repeated in one call must collapse to one entry.
    """
    _write_under(temp_library, "reg-known/part.stl", b"solid part\n")

    first = library_scan.register(["reg-known/part.stl"])
    second = library_scan.register(["reg-known/part.stl", "reg-known/./part.stl"])

    assert first.entries[0].created is True
    assert second.entries[0].created is False
    assert second.entries[0].asset_id == first.entries[0].asset_id
    assert [entry.rel_path for entry in second.known] == ["reg-known/part.stl"]
    assert second.state.registered == 0
    assert second.state.known == 1
    assert set(_library_rows("reg-known/")) == {"reg-known/part.stl"}


def test_register_reports_an_absent_path_without_aborting_the_rest(temp_library: Path) -> None:
    """Mutation: silently skip a named path that is not a regular file on disk.

    A missing file, a directory named as if it were a file, and a path whose
    parent does not exist are all reported; the present one still registers in the
    same call.
    """
    _write_under(temp_library, "reg-absent/present.stl", b"solid present\n")
    _write_under(temp_library, "reg-absent/nested/deep.stl", b"solid deep\n")

    result = library_scan.register(
        [
            "reg-absent/present.stl",
            "reg-absent/missing.stl",
            "reg-absent/nested",
            "reg-absent/nodir/deep.stl",
        ]
    )

    assert result.absent == ("reg-absent/missing.stl", "reg-absent/nested", "reg-absent/nodir/deep.stl")
    assert [entry.rel_path for entry in result.entries] == ["reg-absent/present.stl"]
    assert result.state.registered == 1
    assert set(_library_rows("reg-absent/")) == {"reg-absent/present.stl"}


@pytest.mark.parametrize(
    "rel_path",
    [
        "",
        "   ",
        "/etc/passwd",
        "../outside.stl",
        "~",
        "~/x.stl",
        "thumbs/x.png",
        "bundles/x.stl",
        ".git/x.stl",
        "reg-parent/.hidden.stl",
        "Thumbs.db",
        "reg-parent/.DS_Store",
    ],
)
def test_register_refuses_an_unregistrable_path_and_writes_no_rows(
    temp_library: Path, rel_path: str
) -> None:
    """Mutation: validate after the session, or reuse the wrong path guard.

    Each case is one rule of the walk - outside the root, an app-owned root
    directory (`thumbs/`, `bundles/`), a hidden segment, tool debris, an empty
    name - and the row/folder counts are compared before and after, so "refused"
    cannot quietly mean "wrote and then raised".
    """
    _write_under(temp_library, "reg-parent/present.stl", b"solid present\n")
    assets_before = _asset_row_count()
    folders_before = _folder_row_count()

    with pytest.raises(library_scan.RegisterPathError):
        library_scan.register([rel_path])

    assert (_asset_row_count(), _folder_row_count()) == (assets_before, folders_before)
    assert _library_rows("reg-parent/") == {}


def test_register_refuses_a_symlinked_file(temp_library: Path) -> None:
    """Mutation: follow a symlink and write a row for a link instead of a file.

    The link lives inside the root and its target is a real library file, so only
    the explicit symlink refusal keeps the row from being written; the real file
    right beside it still registers.
    """
    target = _write_under(temp_library, "reg-link/real.stl", b"solid real\n")
    (temp_library / "reg-link/link.stl").symlink_to(target)
    assets_before = _asset_row_count()

    with pytest.raises(library_scan.RegisterPathError):
        library_scan.register(["reg-link/link.stl"])

    assert _asset_row_count() == assets_before
    assert _library_rows("reg-link/") == {}
    assert library_scan.register(["reg-link/real.stl"]).entries[0].created is True


def test_register_never_claims_the_scan_slot(
    temp_library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: route register through `_begin_run` (claim or publish the slot).

    `scan_status()` must be byte-identical, and a later `start_scan()` must still
    be the call that claims the slot. The worker is parked on an Event this test
    owns and then closed WITHOUT a walk, so no assertion races it and no scan of
    this temp root flags the session's other rows absent.
    """
    _write_under(temp_library, "reg-slot/part.stl", b"solid part\n")
    before = library_scan.scan_status()

    library_scan.register(["reg-slot/part.stl"])

    assert library_scan.scan_status() == before

    parked = threading.Event()
    release = threading.Event()

    def parked_scan(state=None):
        parked.set()
        # bounded, so a failed assertion still lets the run close its slot
        release.wait(timeout=SCAN_WAIT_SECONDS * 3)
        # close the slot the way a finished walk would, without walking
        library_scan._finish_run(state)
        return state

    monkeypatch.setattr(library_scan, "scan_storage_root", parked_scan)
    try:
        assert library_scan.start_scan() is True
        assert parked.wait(timeout=SCAN_WAIT_SECONDS), "the background run never reached the parked scanner"
        assert library_scan.scan_status()["running"] is True
    finally:
        release.set()
        _wait_for_scan_status(
            library_scan.scan_status,
            _scan_is_finished,
            message="the released background run never closed its slot",
        )


def test_register_clears_missing_since_only_on_named_present_paths(temp_library: Path) -> None:
    """Mutation: run the scan's missing pass (`_flag_missing_assets`) from register.

    Three witnesses in one call: a named present row is cleared, a named ABSENT row
    is never stamped, and an unrelated flagged row nobody named is untouched. The
    scan's pass would stamp the absent row and report the unrelated one missing.
    """
    _write_under(temp_library, "reg-flag/kept.stl", b"solid kept\n")
    _write_under(temp_library, "reg-flag/unrelated.stl", b"solid unrelated\n")
    vanished = _write_under(temp_library, "reg-flag/vanished.stl", b"solid vanished\n")
    library_scan.register(["reg-flag/kept.stl", "reg-flag/unrelated.stl", "reg-flag/vanished.stl"])

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _stamp_missing_since(["reg-flag/kept.stl", "reg-flag/unrelated.stl"], stamp)
    vanished.unlink()

    result = library_scan.register(["reg-flag/kept.stl", "reg-flag/vanished.stl"])

    rows = _library_rows("reg-flag/")
    assert result.absent == ("reg-flag/vanished.stl",)
    assert rows["reg-flag/kept.stl"].missing_since is None
    assert rows["reg-flag/vanished.stl"].missing_since is None
    assert rows["reg-flag/unrelated.stl"].missing_since == stamp
    assert result.state.missing == 0


def test_register_names_and_folders_match_a_real_scan(temp_library: Path) -> None:
    """Mutation: derive names from the named subset, or resolve folders pathless.

    The storage scan is the reference door on the very same tree. `part` is named
    in both formats (so it must split into `part (stl)`/`part (3mf)`), while
    `pair/a.stl` is named and its same-stem sibling `pair/a.3mf` is NOT: the
    scan's answer for the named file is `a (stl)` because naming is a function of
    the whole listing, so a subset-derived name (`a`) reds here - and the sibling
    still gets no row. Every folder row must also carry the same real storage path
    (not NULL, not sanitised).
    """
    tree = [
        "reg-parity/part.stl",
        "reg-parity/part.3mf",
        "reg-parity/sub/solo.step",
        "reg-parity/pair/a.stl",
        "reg-parity/pair/a.3mf",
    ]
    for rel_path in tree:
        _write_under(temp_library, rel_path, b"model\n")
    named = [rel_path for rel_path in tree if rel_path != "reg-parity/pair/a.3mf"]

    library_scan.scan_storage_root()
    scanned = _row_shape("reg-parity/")
    assert set(scanned) == set(tree)

    _delete_rows_under("reg-parity/")
    _delete_folders_under("reg-parity")
    assert _row_shape("reg-parity/") == {}

    library_scan.register(named)

    registered = _row_shape("reg-parity/")
    # the unnamed sibling is the one row that differs: no row, no folder orphan
    assert set(registered) == set(named)
    for rel_path in named:
        assert registered[rel_path] == scanned[rel_path], rel_path
    assert registered["reg-parity/part.stl"][:2] == ("part (stl)", "part.stl")
    assert registered["reg-parity/part.3mf"][:2] == ("part (3mf)", "part.3mf")
    assert registered["reg-parity/pair/a.stl"][:2] == ("a (stl)", "a.stl")
    assert {rel_path: shape[2] for rel_path, shape in registered.items()} == {
        "reg-parity/part.stl": "reg-parity",
        "reg-parity/part.3mf": "reg-parity",
        "reg-parity/sub/solo.step": "reg-parity/sub",
        "reg-parity/pair/a.stl": "reg-parity/pair",
    }


def test_register_batches_without_changing_a_name(
    temp_library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: derive a name per flush batch instead of per directory.

    Five named paths in one directory, three of them sharing a stem, with
    BATCH_SIZE=2: a per-batch derivation cannot see the sibling still waiting and
    names the third one differently, so the names must equal the BATCH_SIZE=200
    run's.
    """
    rel_paths = [f"reg-batch/part{suffix}" for suffix in (".stl", ".3mf", ".step")]
    rel_paths += ["reg-batch/solo-a.stl", "reg-batch/solo-b.stl"]
    for rel_path in rel_paths:
        _write_under(temp_library, rel_path, b"model\n")

    real_batch_size = library_scan.BATCH_SIZE
    monkeypatch.setattr(library_scan, "BATCH_SIZE", 2)
    small = library_scan.register(rel_paths)
    small_rows = _library_rows("reg-batch/")

    _delete_rows_under("reg-batch/")
    monkeypatch.setattr(library_scan, "BATCH_SIZE", real_batch_size)
    large = library_scan.register(rel_paths)
    large_rows = _library_rows("reg-batch/")

    assert len(small_rows) == len(large_rows) == len(rel_paths)
    assert small.state.registered == large.state.registered == len(rel_paths)
    assert {rel_path: asset.name for rel_path, asset in small_rows.items()} == {
        rel_path: asset.name for rel_path, asset in large_rows.items()
    }
    assert {small_rows[rel_path].name for rel_path in rel_paths[:3]} == {
        "part (stl)",
        "part (3mf)",
        "part (step)",
    }
