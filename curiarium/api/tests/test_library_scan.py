"""Tests for the storage-root scanner (filesystem-authoritative library, phase 1)."""
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from math import ceil
from pathlib import Path
from typing import Callable, Optional
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, select

import asset_service
import audit
import library_scan
import main
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

    `bundle_detect` asks this exact function for its model count, so the truth
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
