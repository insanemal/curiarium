"""The move primitive: one intent, one ledger row, one replayable undo (Mf-215n).

`move_service.move` is the ONE owner of relocation, and these tests pin the four
properties the ticket asks of every primitive against it, each with the mutation
that would red it:

* idempotent - a second call with one intent is a no-op that reports what it
  found, and the ledger still holds ONE row. Mutation: drop the intent lookup and
  the second call moves again, and the tree digest changes;
* undoable - `undo_ledger` folds the recorded pairs back through the same arbiter
  and rewrites the rows, so the tree and the rows are byte-identical to the
  snapshot. Mutation: replay forwards, or restore only the bytes;
* refusing rather than guessing - an occupied target (`Clobbered`), a pair with
  neither bytes nor a row (`UnknownSource`), one intent over two different pair
  lists (`IntentMismatch`), a cross-device mount (`CrossDevice`). Mutation:
  swallow the refusal and the assertion names the damage instead;
* resumable - a refusal that stopped a run leaves the intent recorded with its
  landed count, and a retry under the same intent finishes it. Mutation: treat an
  incomplete row as `already_applied` and the retry silently moves nothing.

The database is the session-wide test database, so every row a test makes is
removed again, and the storage root is a per-test temp directory.
"""

import errno
import hashlib
import os
from pathlib import Path
from typing import Dict

import pytest
from sqlmodel import Session, select

import db
import library_scan
import move_service
from db import engine
from migrations import run_migrations
from models import Asset, Folder, MoveLedger

# --- harness ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the storage root at a temp directory and remove the rows a test made."""
    storage = tmp_path / "storage"
    (storage / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(db, "STORAGE", storage)
    monkeypatch.setattr(db, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(library_scan, "STORAGE", storage)
    run_migrations(engine)
    with Session(engine) as session:
        folders_before = {folder.id for folder in session.exec(select(Folder))}
        assets_before = {asset.id for asset in session.exec(select(Asset))}
        ledger_before = {row.id for row in session.exec(select(MoveLedger))}
    yield storage
    with Session(engine) as session:
        for row in list(session.exec(select(MoveLedger))):
            if row.id not in ledger_before:
                session.delete(row)
        for asset in list(session.exec(select(Asset))):
            if asset.id not in assets_before:
                session.delete(asset)
        for folder in list(session.exec(select(Folder))):
            if folder.id not in folders_before:
                session.delete(folder)
        session.commit()


def write_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def insert_asset(storage_path: str, *, filename: str = "", size: int = 0) -> str:
    """One asset row at `storage_path`, with a name the folder's uniqueness index accepts."""
    filename = filename or Path(storage_path).name
    asset = Asset(
        name=Path(filename).stem,
        filename=filename,
        mime="model/stl",
        size=size,
        storage_path=storage_path,
    )
    with Session(engine) as session:
        session.add(asset)
        session.commit()
        session.refresh(asset)
        return asset.id


def insert_folder(name: str, storage_path: str, parent_id: str = None) -> str:
    folder = Folder(name=name, storage_path=storage_path, parent_id=parent_id)
    with Session(engine) as session:
        session.add(folder)
        session.commit()
        session.refresh(folder)
        return folder.id


def tree_digest(root: Path) -> Dict[str, str]:
    """Every file's path and sha256 under `root`, so "nothing moved" is one comparison."""
    digest: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def asset_rows() -> Dict[str, str]:
    with Session(engine) as session:
        return {asset.id: asset.storage_path or "" for asset in session.exec(select(Asset)) if asset.storage_path}


def folder_rows() -> Dict[str, str]:
    with Session(engine) as session:
        return {folder.id: folder.storage_path or "" for folder in session.exec(select(Folder)) if folder.storage_path}


def ledger_rows(intent: str) -> list:
    with Session(engine) as session:
        return list(session.exec(select(MoveLedger).where(MoveLedger.intent == intent)))


def three_files(storage: Path) -> list:
    """Two loose files and one directory with a file and a row of its own."""
    write_file(storage / "Boxes" / "one.stl", b"one bytes")
    write_file(storage / "Boxes" / "two.stl", b"two bytes")
    write_file(storage / "Boxes" / "Kit" / "part.stl", b"part bytes")
    ids = [
        insert_asset("Boxes/one.stl"),
        insert_asset("Boxes/two.stl"),
        insert_asset("Boxes/Kit/part.stl"),
    ]
    insert_folder("Boxes", "Boxes")
    insert_folder("Kit", "Boxes/Kit")
    return ids


def three_pairs() -> list:
    return [
        move_service.Pair("Boxes/one.stl", "Sorted/one.stl", move_service.KIND_FILE),
        move_service.Pair("Boxes/two.stl", "Sorted/two.stl", move_service.KIND_FILE),
        move_service.Pair("Boxes/Kit", "Sorted/Kit", move_service.KIND_DIRECTORY),
    ]


# --- idempotency -----------------------------------------------------------


def test_a_second_move_with_one_intent_is_a_no_op(isolated_storage: Path) -> None:
    """AC-2: two calls, one intent -> one ledger row, one tree, and no second move."""
    storage = isolated_storage
    three_files(storage)
    pairs = three_pairs()

    first = move_service.move(pairs, intent="test:idempotent", key="test")
    after_first = tree_digest(storage)

    assert first.landed == 3
    assert first.moved is True
    assert first.already_applied is False

    second = move_service.move(pairs, intent="test:idempotent", key="test")

    assert second.already_applied is True, "the intent is the idempotency key: a re-run reports what it found and writes nothing"
    assert second.moved is False
    assert second.landed == 3, "the landed count the first run recorded, reported rather than re-derived"
    assert second.ledger_id == first.ledger_id
    assert len(ledger_rows("test:idempotent")) == 1, "one intent is one row, whatever the retries"
    assert tree_digest(storage) == after_first


def test_a_second_move_still_reports_when_it_found_nothing_to_do(isolated_storage: Path) -> None:
    """A pair whose two ends are one path was never a relocation; the ledger still says so."""
    storage = isolated_storage
    write_file(storage / "Boxes" / "one.stl", b"one bytes")
    insert_asset("Boxes/one.stl")

    outcome = move_service.move(
        [move_service.Pair("Boxes/one.stl", "Boxes/one.stl", move_service.KIND_FILE)],
        intent="test:unchanged",
    )

    assert outcome.landed == 1
    assert outcome.moved is False, "nothing was relocated, so `moved` says so even though the pair landed"


# --- undo ------------------------------------------------------------------


def test_undo_replays_the_ledger_backwards_byte_for_byte(isolated_storage: Path) -> None:
    """AC-3: tree digests, asset rows and folder rows are identical after the undo."""
    storage = isolated_storage
    three_files(storage)
    before_tree = tree_digest(storage)
    before_assets = asset_rows()
    before_folders = folder_rows()

    outcome = move_service.move(three_pairs(), intent="test:undo", key="test")
    assert tree_digest(storage) != before_tree, "the move must actually move something"
    assert before_assets != asset_rows()

    undone = move_service.undo_ledger(outcome.ledger_id)

    assert undone.landed == 3
    assert tree_digest(storage) == before_tree, "the fold backwards is the same arbiter in reverse: every byte is back where it was"
    assert asset_rows() == before_assets, "and the rows describe the disk, not the abandoned layout"
    assert folder_rows() == before_folders
    assert ledger_rows("test:undo")[0].undone == 1

    with pytest.raises(move_service.AlreadyUndone) as refusal:
        move_service.undo_ledger(outcome.ledger_id)

    assert refusal.value.status_code == 409
    assert "already undone" in refusal.value.detail


def test_undo_refuses_when_a_path_is_present_at_both_ends(isolated_storage: Path) -> None:
    """Mutation: guess which end is authoritative and move a file the ledger never named."""
    storage = isolated_storage
    write_file(storage / "Boxes" / "one.stl", b"one bytes")
    insert_asset("Boxes/one.stl")
    outcome = move_service.move(
        [move_service.Pair("Boxes/one.stl", "Sorted/one.stl", move_service.KIND_FILE)],
        intent="test:both-ends",
    )
    write_file(storage / "Boxes" / "one.stl", b"a late arrival with the old name")

    with pytest.raises(move_service.MoveRefused) as refusal:
        move_service.undo_ledger(outcome.ledger_id)

    assert refusal.value.status_code == 409
    assert "present at both ends" in refusal.value.detail
    assert (storage / "Sorted" / "one.stl").read_bytes() == b"one bytes"
    assert (storage / "Boxes" / "one.stl").read_bytes() == b"a late arrival with the old name"


# --- refusals --------------------------------------------------------------


def test_an_occupied_target_is_refused_before_anything_is_written(isolated_storage: Path) -> None:
    """AC-4: Clobbered, no ledger row, tree unchanged, and today's exact sentence."""
    storage = isolated_storage
    write_file(storage / "Boxes" / "one.stl", b"one bytes")
    write_file(storage / "Sorted" / "one.stl", b"committed bytes")
    insert_asset("Boxes/one.stl")
    before = tree_digest(storage)

    with pytest.raises(move_service.Clobbered) as refusal:
        move_service.move(
            [move_service.Pair("Boxes/one.stl", "Sorted/one.stl", move_service.KIND_FILE)],
            intent="test:clobbered",
        )

    assert refusal.value.status_code == 409
    assert refusal.value.detail == "Storage path already exists: Sorted/one.stl", (
        "the door's contract is this sentence; the typed vocabulary is ours, the words are the operator's"
    )
    assert ledger_rows("test:clobbered") == [], "a refusal writes no ledger row"
    assert tree_digest(storage) == before


def test_a_pair_with_no_bytes_and_no_row_is_a_caller_error(isolated_storage: Path) -> None:
    """AC-4: UnknownSource, and nothing is written for it."""
    storage = isolated_storage
    before = tree_digest(storage)

    with pytest.raises(move_service.UnknownSource) as refusal:
        move_service.move(
            [move_service.Pair("Ghost/one.stl", "Sorted/one.stl", move_service.KIND_FILE)],
            intent="test:unknown-source",
        )

    assert refusal.value.status_code == 409
    assert "Ghost/one.stl" in refusal.value.detail
    assert ledger_rows("test:unknown-source") == []
    assert tree_digest(storage) == before


def test_one_intent_over_two_pair_lists_is_refused(isolated_storage: Path) -> None:
    """AC-4: IntentMismatch - one intent is one move, as one key is one plan."""
    storage = isolated_storage
    write_file(storage / "Boxes" / "one.stl", b"one bytes")
    write_file(storage / "Boxes" / "two.stl", b"two bytes")
    insert_asset("Boxes/one.stl")
    insert_asset("Boxes/two.stl")
    move_service.move(
        [move_service.Pair("Boxes/one.stl", "Sorted/one.stl", move_service.KIND_FILE)],
        intent="test:mismatch",
    )

    with pytest.raises(move_service.IntentMismatch) as refusal:
        move_service.move(
            [move_service.Pair("Boxes/two.stl", "Sorted/two.stl", move_service.KIND_FILE)],
            intent="test:mismatch",
        )

    assert refusal.value.status_code == 409
    assert "test:mismatch" in refusal.value.detail
    assert (storage / "Boxes" / "two.stl").read_bytes() == b"two bytes", "the refused intent moved nothing"
    assert len(ledger_rows("test:mismatch")) == 1


def test_a_cross_device_move_is_refused_and_records_how_far_it_got(isolated_storage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-4: an injected EXDEV moves nothing, and the intent stays retryable.

    The refusal is an `OSError` carrying `EXDEV`, not an `HTTPException`: the
    arrange translates exactly that errno into its partial-landed sentence and the
    folder door lets it become a 500, and both behaviours predate this module.
    """
    storage = isolated_storage
    write_file(storage / "Boxes" / "one.stl", b"one bytes")
    insert_asset("Boxes/one.stl")
    before = tree_digest(storage)

    real_link = os.link
    real_rename = os.rename

    def cross_device(*_args, **_kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def unsupported_link(src, dst, *args, **kwargs):
        # The hardlink arbiter cannot help either, so the locked rename is reached
        # and is the one that refuses - exactly the shape a real mount has.
        raise OSError(errno.EOPNOTSUPP, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", unsupported_link)
    monkeypatch.setattr(os, "rename", cross_device)

    with pytest.raises(move_service.CrossDevice) as refusal:
        move_service.move(
            [move_service.Pair("Boxes/one.stl", "Sorted/one.stl", move_service.KIND_FILE)],
            intent="test:cross-device",
        )

    assert isinstance(refusal.value, OSError)
    assert refusal.value.errno == errno.EXDEV
    assert tree_digest(storage) == before, "there is no copy path, so nothing moved at all"
    rows = ledger_rows("test:cross-device")
    assert len(rows) == 1
    assert rows[0].landed == 0
    assert rows[0].applied == 0, "a stopped run is not a complete intent"

    # ... and the intent it recorded is resumable once the filesystem allows it.
    # Restored by attribute, not `monkeypatch.undo()`: that fixture object also
    # holds `isolated_storage`'s path patches, and undoing those would point the
    # retry at the real library root.
    monkeypatch.setattr(os, "link", real_link)
    monkeypatch.setattr(os, "rename", real_rename)
    retried = move_service.move(
        [move_service.Pair("Boxes/one.stl", "Sorted/one.stl", move_service.KIND_FILE)],
        intent="test:cross-device",
    )

    assert retried.already_applied is False, "an incomplete intent resumes; treating it as done would leave the move silently unperformed"
    assert retried.landed == 1
    assert (storage / "Sorted" / "one.stl").read_bytes() == b"one bytes"
    assert len(ledger_rows("test:cross-device")) == 1


def test_a_superseded_ledger_refuses_to_undo(isolated_storage: Path) -> None:
    """A later operation that changed these paths owns the record now."""
    storage = isolated_storage
    write_file(storage / "Boxes" / "one.stl", b"one bytes")
    insert_asset("Boxes/one.stl")
    outcome = move_service.move(
        [move_service.Pair("Boxes/one.stl", "Sorted/one.stl", move_service.KIND_FILE)],
        intent="test:superseded",
    )

    marked = move_service.supersede("test:superseded", by="a later operation")

    assert marked == 1
    with pytest.raises(move_service.Superseded) as refusal:
        move_service.undo_ledger(outcome.ledger_id)

    assert refusal.value.status_code == 409
    assert "superseded by a later operation" in refusal.value.detail
    assert (storage / "Sorted" / "one.stl").read_bytes() == b"one bytes"


def test_an_intent_is_required_and_a_pair_list_is_required(isolated_storage: Path) -> None:
    """The two caller errors that would otherwise write a ledger row about nothing."""
    with pytest.raises(ValueError, match="needs an intent"):
        move_service.move([move_service.Pair("a", "b", move_service.KIND_FILE)], intent="")
    with pytest.raises(ValueError, match="no pairs"):
        move_service.move([], intent="test:empty")


# --- the ledger's own bounded tail -----------------------------------------


def test_prune_keeps_the_newest_rows_and_everything_young(isolated_storage: Path) -> None:
    """Mutation: prune by age alone and a quiet install loses rows it could still undo."""
    mine = "test:prune:"
    with Session(engine) as session:
        for index in range(5):
            session.add(
                MoveLedger(
                    intent=f"{mine}{index}",
                    key="test",
                    pairs_json="[]",
                    landed=0,
                    applied=1,
                    undone=0,
                    # Old and distinct, so "newest" is a fact about the timestamp and
                    # not about which uuid SQLite happened to return first.
                    created_at=f"2000-01-0{index + 1}T00:00:00.000000+00:00",
                )
            )
        session.commit()
        others = [row for row in session.exec(select(MoveLedger)) if not row.intent.startswith(mine)]

    removed = move_service.prune(retain=len(others) + 2, days=30)

    with Session(engine) as session:
        surviving = {row.intent for row in session.exec(select(MoveLedger)) if row.intent.startswith(mine)}

    assert surviving == {f"{mine}3", f"{mine}4"}, "the newest two rows survive whatever their age; the older ones are the tail"
    assert removed == 3
    assert len(others) == len([row for row in _all_ledger_rows() if not row.intent.startswith(mine)]), (
        "a row younger than the horizon is never pruned for being old"
    )


def _all_ledger_rows() -> list:
    with Session(engine) as session:
        return list(session.exec(select(MoveLedger)))
