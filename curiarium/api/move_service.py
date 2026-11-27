"""The ONE owner for relocation: the bytes, the rows that describe them, and the ledger undo replays.

Before this module there were four answers to "move this". `asset_service.move_managed_file`
moved a file's bytes and pruned the directory it vacated; `folder_service.move_folder`
called `os.rename` on a directory itself; `asset_service.move_asset_to_folder_by_id`
composed the first with a row update; and `arrange_service` ran its own loop over
`move_managed_file`. Four callers is four chances to get no-clobber, cross-device
and row-consistency wrong in a different order. They now all ask this module, and
the four public names above are thin doors onto it:

  * `move_bytes(source, target)` is the only arbiter of the bytes. `os.link` is
    primary because it is atomic and refuses an existing destination, so two
    concurrent moves onto one target cannot overwrite committed bytes; the locked
    `os.rename` fallback is for filesystems that cannot hardlink, and it is also
    what moves a DIRECTORY (link refuses a directory with `EPERM`, which the
    unsupported-errno set treats as "this filesystem cannot hardlink").
    It answers FALSE for exactly one reason - the destination was occupied, so no
    byte moved - and every caller must read that as a refusal. It does NOT answer
    False for a source that has no bytes: a missing source raises the underlying
    `FileNotFoundError`, and "the row moves but the bytes never did" is the
    caller's own `source.exists()` decision. Those two facts shared one boolean
    once, in this module's byte loop; a clobbered pair was counted as landed and
    its row was rewritten onto another move's path, so the ledger said `applied`
    about a file that existed under a name no row named (Mf-215n). They are kept
    apart here, in `move_bytes`'s contract, and at the one call site that has to
    ask both questions.
  * `move(pairs, intent=...)` is the coarse verb: it refuses before touching
    anything, records ONE ledger row per `intent`, moves the bytes, and rewrites
    the rows. A second call with the same intent is a no-op that reports what it
    found; the same intent with different pairs is refused.
  * `apply_rows(pairs, session=...)` is the row half on its own, for the callers
    that own their transaction order (`folder_service.move_folder`'s convergence
    branch, `arrange_service._rewrite_rows`).
  * `undo_ledger(ledger_id)` replays one ledger backwards: bytes first, then the
    rows, then `undone = 1`. Arrange does not use it - its ledger carries the
    project, membership and group snapshot that this module knows nothing about,
    so arrange's undo asks `move_bytes` directly and marks its own record.

What this module deliberately does NOT do:

  * It never copies. There is no copy path, so `EXDEV` is a refusal
    (`CrossDevice`, an `OSError` carrying `errno.EXDEV` exactly as the bare
    rename used to raise) rather than a slow fallback that silently doubles a
    library. The callers that already text that refusal into a 409 sentence keep
    their sentences.
  * It does not decide WHETHER a move should happen. Intent, key and audit event
    are the caller's: this module moves what it is told, records it, and says no
    when the disk or the ledger contradicts the instruction.
  * It does not own project membership. A move carries no memberships; `adopt_service`
    owns those, and `bundle_service.create_project` owns the create that follows a
    path (with `project_create_service` holding the dedupe and the single write).
  * It writes no audit row of its own. The door stages its event through `events=`
    inside the same transaction as the ledger row and the row rewrite, which is
    why the audit rows the asset and folder doors write are byte-identical to the
    ones they wrote before this module existed.

The ledger is `move_ledger` (schema 16). Its UNIQUE `intent` index is the
idempotency rule made a database fact: same intent and same pairs is a no-op,
same intent and different pairs is `IntentMismatch`. A row is a bounded tail -
`prune()` keeps the newest `MOVE_LEDGER_RETAIN` rows and everything younger than
`MOVE_LEDGER_RETAIN_DAYS`, and `audit_events` stays the permanent record of what
moved. `supersede()` is how a caller that has already folded a subset back by its
own reckoning (arrange) stops a later `undo_ledger` from moving those bytes a
second time.
"""

import errno
import json
import logging
import os
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
from uuid import uuid4

from fastapi import HTTPException
from sqlmodel import Session, select

import audit
import db
import folder_paths
import folder_service
from db import engine
from models import Asset, Folder, MoveLedger

log = logging.getLogger(__name__)

# Serialises the no-hardlink fallback across this process's threads. Coarse on
# purpose: the fallback is rare and a per-destination dict would grow unbounded.
_FALLBACK_MOVE_LOCK = threading.Lock()
# Errnos meaning "this filesystem cannot create hardlinks": SMB/FAT and
# unprivileged Windows mounts refusing link() as a matter of policy (EPERM,
# EACCES); filesystems with no hardlink support at all (ENOTSUP, EOPNOTSUPP);
# the link count cap (EMLINK); and FUSE/SMB servers that report "not
# implemented" or "invalid argument" instead of a dedicated errno (ENOSYS,
# EINVAL). EXDEV is included for completeness even though a rename cannot cross
# filesystems either: falling back only re-raises the same EXDEV. Every other
# OSError propagates unchanged.
_HARDLINK_UNSUPPORTED_ERRNOS = frozenset(
    {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK, errno.ENOSYS, errno.EINVAL}
)

# The ledger's bounded tail, mirroring `operations_service`'s retention rule:
# the newest N rows and anything younger than the horizon survive, whichever set
# is larger, so a quiet install never loses a row it could still undo and a busy
# one never grows without bound.
MOVE_LEDGER_RETAIN = 200
MOVE_LEDGER_RETAIN_DAYS = 30

# The pair's own word for what is on disk. `None` means "infer it", which is
# what every door but the arrange does: a door knows the path and the rows, not
# the plan's classification.
KIND_DIRECTORY = "dir"
KIND_FILE = "file"


class MoveRefused(HTTPException):
    """The primitive declining, in today's HTTP vocabulary.

    Subclassing `HTTPException` is deliberate and is what makes behaviour
    preservation cheap: every door already propagates an `HTTPException` out of
    the mover with exactly this status and detail, so a refusal raised here is
    the same response the mover raised before this module existed - no door has
    to grow a translation table that could drift. The class is what the typed
    vocabulary is for, not the status: a caller that must branch (arrange's
    partial-landed sentence) catches the class, and everything else lets it fly.
    """

    status_code = 409


class Clobbered(MoveRefused):
    """The target is occupied. Nothing was moved and no ledger row was written.

    The detail is byte-identical to the sentence `move_managed_file` raised
    before (`Storage path already exists: <target>`), because that string is in
    the API contract of two doors and in the fallback whatever the new
    vocabulary says.
    """

    def __init__(self, target: str) -> None:
        super().__init__(status_code=409, detail=f"Storage path already exists: {target}")


class UnknownSource(MoveRefused):
    """A pair whose source has neither bytes nor a row: a caller error, not a verdict.

    A source that HAS a row and no bytes is legitimately movable - a mounted
    no-copy row owns no managed file, and the whole asset still moves as a row -
    so this is only raised when both halves are absent, which is the one case
    where the primitive cannot tell what was meant.
    """

    def __init__(self, source: str) -> None:
        super().__init__(status_code=409, detail=f"Nothing to move at {source}: no file and no row names it")


class IntentMismatch(MoveRefused):
    """The same intent, different pairs. One intent is one move, as one key is one plan."""

    def __init__(self, intent: str) -> None:
        super().__init__(
            status_code=409,
            detail=f"the intent {intent!r} was already used for a different set of moves",
        )


class AlreadyUndone(MoveRefused):
    """This ledger was replayed backwards; there is nothing left to undo."""

    def __init__(self, ledger_id: str) -> None:
        super().__init__(status_code=409, detail=f"the move {ledger_id} was already undone")


class Superseded(MoveRefused):
    """A later operation changed these paths, so this ledger no longer describes them."""

    def __init__(self, ledger_id: str, by: str) -> None:
        super().__init__(
            status_code=409,
            detail=f"the move {ledger_id} was superseded by {by}; undo that operation instead",
        )


class UnknownLedger(MoveRefused):
    """No ledger row carries that id - pruned, or never written."""

    def __init__(self, ledger_id: str) -> None:
        super().__init__(status_code=404, detail=f"unknown move: {ledger_id}")


class CrossDevice(OSError):
    """A move that would have to cross a filesystem, which this module will not copy.

    An `OSError` on purpose: before this module the bare `os.rename` raised
    `OSError(EXDEV)` and every caller already branches on exactly that
    (`arrange_service._move_forward` translates it into its partial-landed
    sentence; the folder door lets it become a 500). Making it a named class
    rather than a second vocabulary is what keeps those branches true.
    """

    def __init__(self, message: str) -> None:
        super().__init__(errno.EXDEV, message)


@dataclass(frozen=True)
class Pair:
    """One relocation, in storage-relative spelling.

    Relative, not absolute, because the ledger stores the pair and an undo may
    run against a storage root that was configured differently in between; a
    recorded absolute path would then name a directory that no longer exists.
    `kind` is `dir`/`file` when the caller knows (the arrange does - its plan
    classified the path), and `None` when the primitive must ask the disk.
    """

    source: str
    target: str
    kind: Optional[str] = None

    def as_record(self) -> list:
        return [self.source, self.target, self.kind]


@dataclass(frozen=True)
class MoveOutcome:
    """What one `move()` decided. `landed` counts pairs processed, `moved` counts real relocations.

    `already_applied` is the idempotent answer: nothing was touched, and `landed`
    is what the ledger said the first run got through. `moved` is whether this
    call relocated any bytes at all - a pair whose two ends are one path, or one
    whose row owns no managed file, lands without moving anything.
    """

    ledger_id: str
    pairs: Tuple[Pair, ...]
    landed: int
    moved: bool
    already_applied: bool


def _now() -> str:
    """ISO-8601 UTC with the column precision the rest of the app writes."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def storage_root() -> Path:
    """The storage root at call time, so a test that monkeypatches `db.STORAGE` is honoured."""
    return Path(db.STORAGE)


def relative_path(path: Path) -> str:
    """Express an absolute storage path relative to the library root."""
    root = storage_root().resolve()
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        raise HTTPException(status_code=400, detail="Move path resolved outside the storage directory") from None


def absolute_path(relative: str) -> Path:
    """The other direction, never resolving: a caller's pair is already storage-relative."""
    return storage_root() / relative


def _records(pairs: Sequence[Pair]) -> list:
    return [pair.as_record() for pair in pairs]


def _from_records(records: Sequence[Sequence]) -> List[Pair]:
    """Read a ledger's `pairs_json` back, skipping anything a hand edit broke."""
    pairs: List[Pair] = []
    for record in records:
        if not isinstance(record, Sequence) or isinstance(record, (str, bytes)) or len(record) != 3:
            continue
        source, target, kind = record
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        pairs.append(Pair(source, target, kind if isinstance(kind, str) else None))
    return pairs


def _ledger_by_intent(session: Session, intent: str) -> Optional[MoveLedger]:
    return session.exec(select(MoveLedger).where(MoveLedger.intent == intent)).first()


def _ledger_by_id(session: Session, ledger_id: str) -> Optional[MoveLedger]:
    return session.get(MoveLedger, ledger_id)


def _recorded_pairs(row: MoveLedger) -> List[Pair]:
    try:
        records = json.loads(row.pairs_json or "[]")
    except (TypeError, ValueError):
        return []
    return _from_records(records if isinstance(records, list) else [])


# --- the bytes -------------------------------------------------------------


def _hardlink_move(source: Path, target: Path) -> None:
    """Move bytes by linking then unlinking, so target is either created by us or the move fails.

    ``os.link`` is atomic and refuses an existing destination (file or
    directory), so concurrent moves onto one target cannot overwrite committed
    bytes. It preserves the inode, so content and mtime are unchanged; the
    unlink then retires the old name. A failed unlink rolls the half-move back:
    after this function the bytes have exactly one name, never two and never zero.
    """
    os.link(source, target)
    try:
        source.unlink()
    except OSError:
        target.unlink(missing_ok=True)
        raise


def move_bytes(source: Path, target: Path) -> bool:
    """Move bytes so `target` is either created by us or left untouched. THE one arbiter.

    ``os.link`` is the primary arbiter: it is atomic and refuses an existing
    destination. The fallback for filesystems that cannot hardlink re-checks
    under a lock and renames; the lock serialises this process's moving threads
    only, which is why hardlinking is the default. Either way an occupied
    destination is never overwritten: False is returned.

    False means one thing and only one thing: THE DESTINATION WAS OCCUPIED, and
    no byte moved. It is a refusal, and a caller that has a row to rewrite must
    raise `Clobbered` rather than skip. It is NOT the answer for a source with
    no bytes: that is `FileNotFoundError` out of ``os.link`` (or the rename), and
    the "the row moves, the bytes were never ours" case belongs to the caller's
    own `source.exists()` guard - `move_managed_file` and `move`'s byte loop both
    ask it before calling here.

    A directory also arrives here and takes the fallback: ``os.link`` on a
    directory raises ``EPERM``, which the unsupported set treats as "this
    filesystem cannot hardlink", so the locked ``os.rename`` moves the whole tree
    in one step. That single errno is what makes a directory move work without a
    second mover or a copy path.

    ``EXDEV`` is surfaced as `CrossDevice`, never swallowed and never worked
    around: a cross-device move would need the copy path this module does not
    have.
    """
    try:
        _hardlink_move(source, target)
        return True
    except FileExistsError:
        return False
    except OSError as error:
        # EPERM is the directory path: os.link refuses directories outright, and
        # the rename below is the only way to move one without a copy tree.
        if error.errno not in _HARDLINK_UNSUPPORTED_ERRNOS:
            raise
    with _FALLBACK_MOVE_LOCK:
        if target.exists():
            return False
        try:
            os.rename(source, target)
        except OSError as error:
            if error.errno == errno.EXDEV:
                raise CrossDevice(f"moving {source} to {target} would cross a filesystem") from error
            raise
    return True


def move_managed_file(source: Path, target: Path) -> bool:
    """Move one managed file or directory and prune the directory it vacated.

    A one-screen delegate, deliberately: the arbiter is `move_bytes` and the
    pruning rule is the one this function has always had, so the callers that
    publish bytes through it (`routers/uploads`, `asset_file_service`) keep their
    exact behaviour. It writes NO ledger entry - publishing a streamed upload
    into its rendered path is not a product move, and a ledger row per upload
    would bury the moves an operator actually made.
    """
    if source.resolve() == target.resolve():
        return False
    relative = relative_path(target)
    # Friendly fast path with today's 409 precedence; exists() is a read, so it
    # decides nothing once two moves race - the link in move_bytes is the arbiter.
    if target.exists():
        raise Clobbered(relative)
    if not source.exists():
        # mount no-copy rows own no managed file: the row moves, the bytes do not.
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    old_parent = source.parent
    if not move_bytes(source, target):
        raise Clobbered(relative)
    folder_service.prune_empty_storage_dirs(old_parent)
    return True


# --- the rows --------------------------------------------------------------


def _folder_at(session: Session, relative: str) -> Optional[Folder]:
    """The row that names `relative`, case-insensitively, or None."""
    return session.exec(select(Folder).where(Folder.storage_path.collate("NOCASE") == relative)).first()


def _names_asset(session: Session, relative: str) -> bool:
    return session.exec(select(Asset).where(Asset.storage_path == relative)).first() is not None


def _has_row(session: Session, pair: Pair) -> bool:
    return _folder_at(session, pair.source) is not None or _names_asset(session, pair.source)


def infer_kind(session: Session, pair: Pair) -> str:
    """`dir` when a folder row or the disk says directory, `file` otherwise.

    The row is asked before the disk because a scan-created folder can name a
    path that is not on this filesystem at all, and a move of that row is still a
    directory move.
    """
    if pair.kind in (KIND_DIRECTORY, KIND_FILE):
        return pair.kind
    if _folder_at(session, pair.source) is not None:
        return KIND_DIRECTORY
    try:
        if (storage_root() / pair.source).is_dir():
            return KIND_DIRECTORY
    except OSError:
        pass
    return KIND_FILE


def apply_rows(
    pairs: Sequence[Pair],
    *,
    session: Session,
    root: Optional[Path] = None,
    retire_ids: Optional[Sequence[str]] = None,
) -> None:
    """Rewrite the rows that describe `pairs`, deepest rule first, in the caller's transaction.

    Order is the contract, and it is the order `arrange_service._rewrite_rows` had
    before it delegated here: every directory pair's folder subtree and its assets
    are rewritten before any file pair's row, because a file pair can name a path
    inside a directory pair's target and the directory rewrite would otherwise
    claim it twice.

    `retire_ids` is the arrange's own snapshot scope, and `None` means "retire
    nothing". That default is load-bearing: the asset and folder doors never
    delete a folder row, and a primitive that retired rows for them would delete
    a row for a directory the operator had merely emptied.

    `folder_service.plan_asset_rewrites`/`apply_asset_path_rewrites` are read off
    the module at call time, never imported by name, because
    `tests/test_folder_crud_filesystem.py` monkeypatches them to prove a rename's
    rollback path and a by-name import would bypass the patch.
    """
    root = root or storage_root()
    for pair in pairs:
        if infer_kind(session, pair) != KIND_DIRECTORY:
            continue
        folder = _folder_at(session, pair.source)
        if folder is None:
            continue
        parent_relative = os.path.dirname(pair.target)
        segment = os.path.basename(pair.target)
        paths = folder_paths.derive_subtree_paths(session, folder, parent_relative, segment)
        subtree = {row.id: row for row in folder_paths.folder_subtree(session, folder.id)}
        folder_service.apply_folder_paths(session, subtree, paths)
        parent_row = _folder_at(session, parent_relative) if parent_relative else None
        if parent_row is not None:
            folder.parent_id = parent_row.id
            session.add(folder)
        folder_service.apply_asset_path_rewrites(
            session, folder_service.plan_asset_rewrites(session, pair.source, pair.target)
        )
    for pair in pairs:
        if infer_kind(session, pair) != KIND_FILE:
            continue
        asset = session.exec(select(Asset).where(Asset.storage_path == pair.source)).first()
        if asset is not None:
            asset.storage_path = pair.target
            session.add(asset)
    if retire_ids is not None:
        retire_gone_folders(session, root, retire_ids)


# `move()`'s flag is named `apply_rows` for exactly what it turns on, so inside
# that function the name belongs to the flag; this alias is how the flag still
# reaches the function without a bolted-on second verb.
_apply_rows = apply_rows


def retire_gone_folders(session: Session, root: Path, folder_ids: Sequence[str]) -> None:
    """Delete rows whose directory the moves left empty and gone, deepest first.

    Scoped to `folder_ids` - the rows the caller snapshotted before it moved
    anything - because the caller is the only one that knows which rows it is
    entitled to drop, and an unscoped sweep would delete a row another request
    had just created.
    """
    wanted = set(folder_ids)
    asset_paths = [asset.storage_path or "" for asset in session.exec(select(Asset)) if asset.storage_path]
    rows = [folder for folder in session.exec(select(Folder)) if folder.id in wanted]
    rows.sort(key=lambda folder: (folder.storage_path or "").count("/"), reverse=True)
    for folder in rows:
        path = folder.storage_path or ""
        if not path:
            continue
        absolute = root / path
        if absolute.exists():
            try:
                with os.scandir(absolute) as entries:
                    if next(entries, None) is not None:
                        continue
            except OSError:
                continue
            try:
                os.rmdir(absolute)
            except OSError:
                continue
        if _any_under(asset_paths, path):
            continue
        if session.exec(select(Folder).where(Folder.parent_id == folder.id)).first() is not None:
            continue
        session.delete(folder)


def _any_under(paths: Sequence[str], root: str) -> bool:
    """Whether one of `paths` IS or sits below `root`."""
    return any(path == root or path.startswith(root + "/") for path in paths)


# --- the ledger ------------------------------------------------------------


def _refuse_unusable(row: MoveLedger) -> None:
    """Refuse a ledger a later operation already closed, before anything is decided."""
    if row.undone:
        raise AlreadyUndone(row.id)
    if row.superseded_by:
        raise Superseded(row.id, row.superseded_by)


def _open(intent: str, key: str, pairs: Sequence[Pair]) -> str:
    """Insert the intent's ledger row in its own committed transaction, and return its id.

    The insert commits on its own so the intent is durable before a single byte
    moves: a crash between the two leaves a row with `landed = 0` that names
    exactly what was about to happen. Nothing is held open across the byte loop,
    so a slow move never holds a write lock the rest of the app is waiting on.
    """
    ledger_id = str(uuid4())
    with Session(engine) as session:
        session.add(
            MoveLedger(
                id=ledger_id,
                intent=intent,
                key=key,
                pairs_json=json.dumps(_records(pairs)),
                landed=0,
                applied=0,
                undone=0,
                created_at=_now(),
            )
        )
        session.commit()
    return ledger_id


def _close(ledger_id: str, *, landed: Optional[int] = None, applied: Optional[bool] = None) -> None:
    """Persist how far a run got. Absent arguments are left exactly as they are."""
    with Session(engine) as session:
        row = session.get(MoveLedger, ledger_id)
        if row is None:
            return
        if landed is not None:
            row.landed = landed
        if applied is not None:
            row.applied = 1 if applied else 0
        session.add(row)
        session.commit()


def _record_on(session: Session, *, intent: str, key: str, pairs: Sequence[Pair]) -> MoveLedger:
    """Write the intent's ledger row into a transaction the CALLER commits.

    Used by `move()` when a door hands it a session, and by nothing else: a door
    whose row write and byte move must commit together cannot afford a second
    transaction, and a ledger row that survived a rolled-back row write would
    claim a move the database never saw.

    Deliberately NOT flushed. The row is added and left pending, so every insert
    this move makes lands at the caller's commit - which is where the doors
    already catch IntegrityError and restore the bytes. A flush here would raise
    out of the primitive instead, past the caller's `except IntegrityError`, and
    turn a name race's 409 into a 500.
    """
    row = MoveLedger(
        id=str(uuid4()),
        intent=intent,
        key=key,
        pairs_json=json.dumps(_records(pairs)),
        landed=0,
        applied=0,
        undone=0,
        created_at=_now(),
    )
    session.add(row)
    return row


def supersede(intent_prefix: str, *, by: str) -> int:
    """Mark every live row whose intent starts with `intent_prefix` as superseded.

    Arrange's undo folds its own moves back and restores its own rows, so the
    move ledger rows its apply wrote no longer describe the disk. Marking them
    is what turns a later `undo_ledger` on one of them into a refusal instead of
    a second, blind relocation. Returns how many rows were marked.
    """
    marked = 0
    with Session(engine) as session:
        rows = list(session.exec(select(MoveLedger).where(MoveLedger.intent.startswith(intent_prefix))))
        for row in rows:
            if row.undone or row.superseded_by:
                continue
            row.superseded_by = by
            session.add(row)
            marked += 1
        session.commit()
    return marked


def prune(*, retain: int = MOVE_LEDGER_RETAIN, days: int = MOVE_LEDGER_RETAIN_DAYS) -> int:
    """Drop the ledger's tail: the newest `retain` rows and everything younger than `days` survive.

    Nothing that could still be undone is dropped for being old alone - the two
    rules are a union, not an intersection, exactly like the operations queue's
    retention. Returns how many rows were deleted.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="microseconds")
    with Session(engine) as session:
        rows = list(session.exec(select(MoveLedger).order_by(MoveLedger.created_at.desc(), MoveLedger.id.desc())))
        doomed = [row for row in rows[retain:] if row.created_at < cutoff]
        for row in doomed:
            session.delete(row)
        session.commit()
    return len(doomed)


def _maybe_prune() -> None:
    """Prune once the ledger is over its retained size, and never fail a move over it."""
    try:
        with Session(engine) as session:
            if len(session.exec(select(MoveLedger.id)).all()) <= MOVE_LEDGER_RETAIN:
                return
        prune()
    except Exception:  # pragma: no cover - a prune failure must never fail a move
        log.exception("Move ledger prune failed")


# --- the verb --------------------------------------------------------------


def _preflight(session: Session, pairs: Sequence[Pair]) -> None:
    """Refuse the whole list before writing anything: an occupied target, or nothing to move.

    The whole list, not pair by pair, because "Clobbered leaves the tree
    unchanged and writes no ledger row" must hold for a batch too. Today's
    precedence is kept: an occupied target is reported before a missing source,
    since that is the order `move_managed_file` asked its two questions in.
    """
    root = storage_root()
    for pair in pairs:
        if pair.source == pair.target:
            # A pair whose two ends are one path was never a relocation; the ledger
            # records that it was asked for and `moved` says nothing happened. Asking
            # the clobber question about it would refuse a rename onto itself, which
            # the mover has always answered False to.
            continue
        if (root / pair.target).exists():
            raise Clobbered(pair.target)
    for pair in pairs:
        if pair.source == pair.target:
            continue
        if not (root / pair.source).exists() and not _has_row(session, pair):
            raise UnknownSource(pair.source)


def move(
    pairs: Sequence[Pair],
    *,
    intent: str,
    key: str = "",
    ctx: Optional[audit.AuditContext] = None,
    events: Sequence[audit.Event] = (),
    apply_rows: bool = True,
    session: Optional[Session] = None,
    root: Optional[Path] = None,
    retire_ids: Optional[Sequence[str]] = None,
) -> MoveOutcome:
    """Move `pairs` under one intent: refuse, record, move the bytes, rewrite the rows.

    The order is the contract:

      1. an intent already in the ledger is answered, not re-run. A COMPLETED one
         over the same pairs is `already_applied` and writes nothing; the same
         intent over different pairs is `IntentMismatch`; an undone or superseded
         ledger is its own refusal. A row that is NOT complete is a stopped run,
         and it RESUMES from `landed` - the pairs it already got through are
         skipped, not re-clobbered - so a refusal that stopped a move (a target
         that appeared, a cross-device mount) can be retried under the intent that
         was already recorded;
      2. pre-flight the pairs that are left. Clobbered or UnknownSource here means
         nothing has moved and no ledger row was written for a NEW intent;
      3. open the ledger row (in the caller's transaction when `session` is given,
         on its own otherwise, so the intent is durable before the bytes move);
      4. stage the door's audit events into that same transaction, BEFORE the
         bytes move: a failed audit insert must never leave a file relocated
         while the log says nothing happened;
      5. move the bytes pair by pair, recording `landed` as it goes. A pair whose
         source has no bytes lands without moving any (the row is the move); a
         pair whose TARGET is occupied raises `Clobbered` and does not count, so
         the `except` below records `applied=False, landed=<so far>` and the
         retry resumes at the refused pair;
      6. rewrite the rows and mark the ledger complete.

    `apply_rows=False` is the arrange's shape: it moves the bytes here and writes
    its own rows in its own commit 2, because its ledger carries the folder and
    asset snapshots an undo restores and this module's ledger does not.
    `applied` still means "this intent is complete": for that shape it says the
    byte half is done and the row half belongs to the caller, which is what makes
    a re-post of an arrange that already landed its moves a no-op rather than a
    second attempt to move files that are no longer there.

    A caller's session is used with autoflush OFF, and that is load-bearing. The
    rename door has already set `name`/`filename`/`title` on the asset and staged
    its audit row when it gets here, and it catches `IntegrityError` around its own
    commit to answer a lost name race with a 409. A query in this function would
    autoflush those pending changes, raise the same `IntegrityError` from here
    instead - past that `except` - and turn the race's 409 into a 500. So nothing
    here flushes; every insert and update this call makes lands at the caller's
    commit, where the doors already know how to classify it.
    """
    if not intent:
        raise ValueError("a move needs an intent: it is the idempotency key, not a label")
    ordered = tuple(Pair(pair.source, pair.target, pair.kind) for pair in pairs)
    if not ordered:
        raise ValueError("a move with no pairs would write a ledger row about nothing")
    if events and session is None:
        # The events belong to the caller's transaction; a self-committing move has
        # none to join, and staging them elsewhere would write an audit row that
        # outlives the row write it describes.
        raise ValueError("a self-committing move cannot stage audit events; pass the caller's session")
    root = root or storage_root()
    own_session = session is None
    # See the docstring: autoflush is the difference between the rename race's 409
    # and a 500, and the caller's commit is where the classification belongs. The
    # guard covers the lookup and the pre-flight as well as the transfer, because
    # those queries are the ones an already-pending row would flush.
    guard = nullcontext() if own_session else session.no_autoflush
    probe = session or Session(engine)
    landed = 0

    try:
        with guard:
            existing = _ledger_by_intent(probe, intent)
            ledger = None
            ledger_id = ""
            resume_from = 0
            if existing is not None:
                _refuse_unusable(existing)
                recorded = _recorded_pairs(existing)
                if _records(recorded) != _records(ordered):
                    raise IntentMismatch(intent)
                if existing.applied:
                    return MoveOutcome(
                        ledger_id=existing.id,
                        pairs=tuple(recorded),
                        landed=existing.landed,
                        moved=False,
                        already_applied=True,
                    )
                ledger_id, resume_from = existing.id, existing.landed
                if not own_session:
                    # The resumed row is committed already; updating it through the
                    # caller's session keeps `complete` in that transaction too.
                    ledger = existing
            _preflight(probe, ordered[resume_from:])
            landed = resume_from
            if not ledger_id:
                if own_session:
                    ledger_id = _open(intent, key, ordered)
                else:
                    ledger = _record_on(session, intent=intent, key=key, pairs=ordered)
                    ledger_id = ledger.id
            if not own_session:
                audit.stage(session, ctx, *events)
            moved = False
            for pair in ordered[resume_from:]:
                source = root / pair.source
                target = root / pair.target
                target.parent.mkdir(parents=True, exist_ok=True)
                # The two questions are asked separately, and that is the whole point:
                # a missing source and an occupied target are NOT one fact. Two pairs
                # land without moving bytes - one whose two ends are one path (never a
                # relocation: the pre-flight's rule), and one whose source has no bytes
                # (a mounted no-copy asset, or a row the scan flagged missing, which
                # still moves because the row is what an operator sees). An occupied
                # target is neither: it is a CLOBBER, refused here exactly as
                # `move_managed_file` refuses it. Counting it as landed would rewrite
                # the row onto a path whose bytes another move owns and close the ledger
                # claiming a move that never happened.
                relocates = source.exists() and pair.source != pair.target
                if relocates:
                    if not move_bytes(source, target):
                        raise Clobbered(pair.target)
                    moved = True
                    folder_service.prune_empty_storage_dirs(source.parent)
                landed += 1
                if own_session:
                    _close(ledger_id, landed=landed)
                else:
                    ledger.landed = landed
                    session.add(ledger)
            if apply_rows:
                if own_session:
                    with Session(engine) as writer:
                        _apply_rows(ordered, session=writer, root=root, retire_ids=retire_ids)
                        writer.commit()
                else:
                    _apply_rows(ordered, session=session, root=root, retire_ids=retire_ids)
            if own_session:
                _close(ledger_id, landed=landed, applied=True)
                _maybe_prune()
            else:
                if ledger is not None:
                    ledger.applied = 1
                    ledger.landed = landed
                    session.add(ledger)
    except BaseException:
        if own_session and ledger_id:
            # The intent is already durable, so record how far it got; a re-run
            # with the same intent then reports that instead of guessing.
            try:
                _close(ledger_id, landed=landed, applied=False)
            except Exception:  # pragma: no cover - the original failure is the one to report
                log.exception("Could not record the partial move %s", ledger_id)
        raise
    finally:
        if own_session:
            probe.close()
    return MoveOutcome(
        ledger_id=ledger_id,
        pairs=ordered,
        landed=landed,
        moved=moved,
        already_applied=False,
    )


def reverse_ledger(ledger_id: str) -> Tuple[Pair, ...]:
    """The pairs a ledger's undo replays, in reverse order, refusing an ambiguous end.

    A pair is undoable only when exactly one of its two ends has bytes: both ends
    present or neither present means something else has happened to that path
    since, and guessing would move a file the ledger never named.
    """
    with Session(engine) as session:
        row = _ledger_by_id(session, ledger_id)
        if row is None:
            raise UnknownLedger(ledger_id)
        _refuse_unusable(row)
        pairs = _recorded_pairs(row)
        landed = row.landed
    root = storage_root()
    landed_pairs = pairs[:landed]
    for pair in landed_pairs:
        has_source = (root / pair.source).exists()
        has_target = (root / pair.target).exists()
        if has_target and not has_source:
            continue
        if has_target and has_source:
            raise MoveRefused(
                status_code=409,
                detail=f'cannot undo: "{pair.source}" is present at both ends; refusing to guess',
            )
        raise MoveRefused(
            status_code=409,
            detail=f'cannot undo: "{pair.source}" is present at neither end; refusing to guess',
        )
    return tuple(Pair(pair.target, pair.source, pair.kind) for pair in reversed(landed_pairs))


def undo_ledger(
    ledger_id: str,
    *,
    ctx: Optional[audit.AuditContext] = None,
    events: Sequence[audit.Event] = (),
) -> MoveOutcome:
    """Replay one ledger backwards: bytes, then rows, then `undone = 1`.

    The reverse order is not cosmetic - a directory pair's children are moved
    back before the directory itself, which is the only order the parent exists
    in when the child arrives. A second undo refuses (`AlreadyUndone`), and a
    superseded ledger refuses with the operation that superseded it, so the
    answer to "undo that again" is a sentence rather than a second relocation.

    `reverse_ledger` proved each pair's old name was free when it looked; the
    arbiter is what proves it NOW, and its refusal stops the undo before the rows
    are rewritten. A pair with bytes at neither end - a row-only move, whose file
    was never ours - is refused there rather than skipped, so this loop only ever
    sees pairs the ledger claims moved a file for.
    """
    reversal = reverse_ledger(ledger_id)
    with Session(engine) as session:
        row = _ledger_by_id(session, ledger_id)
        if row is None:
            raise UnknownLedger(ledger_id)
        applied = bool(row.applied)
        root = storage_root()
        for pair in reversal:
            source = root / pair.source
            target = root / pair.target
            target.parent.mkdir(parents=True, exist_ok=True)
            if not move_bytes(source, target):
                # The old name was free a moment ago and is taken now. Refuse: the
                # rows below must never be rewritten onto a path the bytes did not
                # reach, and the ledger must not close `undone` over a pair that is
                # still where the forward move left it.
                raise Clobbered(pair.target)
            folder_service.prune_empty_storage_dirs(source.parent)
        audit.stage(session, ctx, *events)
        if applied:
            _apply_rows(reversal, session=session, root=root)
        row.undone = 1
        session.add(row)
        session.commit()
        return MoveOutcome(
            ledger_id=ledger_id,
            pairs=reversal,
            landed=len(reversal),
            moved=True,
            already_applied=False,
        )
