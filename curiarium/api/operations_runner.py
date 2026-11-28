"""The in-process operations runner: one dispatcher, one thread pool, DB as truth.

The database is the truth and this runner is in-process. One dispatcher pass
(`dispatch_once`) reads the queued rows, acquires the prefix path locks each one
declares and hands the winners to a worker; `run_forever` is the daemon thread
`main.lifespan` starts. Nothing here is durable state - a crashed process is
repaired by `resume_on_startup`, which reads the same rows.

Every step's status is committed BEFORE and AFTER the step runs, so a process
that dies mid-step can tell "never started" (still `pending`) from "may have run"
(`running`); `resume_on_startup` re-queues the first and fails the second rather
than re-running a step whose side effects are unknown.

Cancel is honoured between steps only: a `cancelling` operation finishes the
step it is in and marks the rest `skipped`. A step never shares a transaction
with the primitive it calls - with SQLite's WAL and `busy_timeout=10000`, a step
holding a write transaction for its whole body would block the dispatcher for
the entire timeout.
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Callable, Dict, List, Optional, Set

from fastapi import HTTPException
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, delete, select

import adopt_service
import archive_service
import audit
import bundle_service
import folder_service
import library_scan
import move_service
import operations_service
import project_create_service
import project_service
from db import engine
from file_utils import is_model_path
from models import Folder, Operation, OperationLock, OperationStep, Project, ProjectAsset

log = logging.getLogger(__name__)

# A module constant, not an env var, deliberately: the suite pins it False in
# tests/conftest.py, and a developer's shell must not be able to flip it (which
# would also add an operator-tunable config key nothing needs).
BACKGROUND_ENABLED = True
POOL_SIZE = 2
DISPATCH_INTERVAL_SECONDS = 0.2

# Test seam: called immediately BEFORE a step body runs, with the operation and
# step it is about to execute.
STEP_GUARD: Optional[Callable[[Operation, OperationStep], None]] = None

_dispatcher_lock = threading.Lock()
_pool_lock = threading.Lock()
_pool_executor: Optional[ThreadPoolExecutor] = None
_futures_lock = threading.Lock()
_futures: Set = set()


def set_step_guard(guard: Optional[Callable[[Operation, OperationStep], None]]) -> None:
    """Install (or clear) the seam a test uses to observe or interrupt a step."""
    global STEP_GUARD
    STEP_GUARD = guard


def should_run() -> bool:
    return BACKGROUND_ENABLED


def _pool() -> ThreadPoolExecutor:
    global _pool_executor
    with _pool_lock:
        if _pool_executor is None:
            _pool_executor = ThreadPoolExecutor(max_workers=POOL_SIZE, thread_name_prefix="operations")
        return _pool_executor


def _discard_future(future) -> None:
    with _futures_lock:
        _futures.discard(future)


def _submit(operation_id: str) -> None:
    future = _pool().submit(run_operation, operation_id)
    with _futures_lock:
        _futures.add(future)
    future.add_done_callback(_discard_future)


def wait_idle(timeout: float = 10.0) -> None:
    """Join every submitted future; the seam a test uses to wait for execution."""
    deadline = time.monotonic() + timeout
    while True:
        with _futures_lock:
            pending = list(_futures)
        if not pending:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        wait(pending, timeout=remaining)


def dispatch_once() -> int:
    """One dispatcher pass: lock, promote and submit what can run now."""
    winners: List[str] = []
    with _dispatcher_lock:
        with Session(engine) as session:
            held = {row.path: row.operation_id for row in session.exec(select(OperationLock))}
            queued = list(
                session.exec(
                    select(Operation)
                    .where(Operation.status == operations_service.STATUS_QUEUED)
                    .order_by(Operation.created_at, Operation.id)
                )
            )
        for operation in queued:
            paths = operations_service.declared_paths(operation)
            if any(operations_service.paths_overlap(path, other) for path in paths for other in held):
                # A blocked operation keeps NO lock rows.
                continue
            if not _claim(operation.id, paths, held):
                # Lost the race (or an overlapping prefix appeared); leave it queued.
                continue
            winners.append(operation.id)
    for operation_id in winners:
        _submit(operation_id)
    return len(winners)


def _claim(operation_id: str, paths: List[str], held: Dict[str, str]) -> bool:
    """Take the locks and promote one operation, re-checking inside the transaction."""
    try:
        with Session(engine) as session:
            current = {row.path: row.operation_id for row in session.exec(select(OperationLock))}
            if any(
                operations_service.paths_overlap(path, other)
                for path in paths
                for other in current
            ):
                return False
            now = operations_service.now()
            for path in paths:
                session.add(OperationLock(path=path, operation_id=operation_id, created_at=now))
            operation = session.get(Operation, operation_id)
            if operation is None:
                session.rollback()
                return False
            operation.status = operations_service.STATUS_RUNNING
            operation.started_at = now
            session.add(operation)
            session.commit()
    except IntegrityError:
        # A concurrent writer took the same lock path or a racer promoted the
        # row; leave it queued for the next pass.
        log.debug("operations dispatcher lost the race for %s", operation_id)
        return False
    for path in paths:
        held[path] = operation_id
    return True


def run_forever(stop: threading.Event, interval: float = DISPATCH_INTERVAL_SECONDS) -> None:
    """The daemon thread: dispatch on an interval until `stop` is set."""
    while not stop.is_set():
        try:
            dispatch_once()
        except Exception:
            log.exception("operations dispatcher pass failed")
        stop.wait(interval)


def _prepare_guard_error(operation: Operation, step: OperationStep) -> Optional[Exception]:
    if STEP_GUARD is None:
        return None
    try:
        STEP_GUARD(operation, step)
    except Exception as exc:
        return exc
    return None


def run_operation(operation_id: str) -> None:
    """Execute one operation's steps on a worker thread; own Sessions only.

    Never lets an exception escape: the row's own `failed` write is the record.
    """
    try:
        _run_operation(operation_id)
    except Exception:
        log.exception("operations worker crashed on %s", operation_id)


def _run_operation(operation_id: str) -> None:
    while True:
        guard_error: Optional[Exception] = None
        with Session(engine) as session:
            operation = session.get(Operation, operation_id)
            if operation is None:
                return
            status = operation.status
            if status == operations_service.STATUS_QUEUED:
                # Inline resume (tests): the dispatcher is not running here, so
                # promote the row the dispatcher would have promoted.
                operation.status = operations_service.STATUS_RUNNING
                operation.started_at = operations_service.now()
                session.add(operation)
                session.commit()
                continue
            if status not in (operations_service.STATUS_RUNNING, operations_service.STATUS_CANCELLING):
                return
            step = session.exec(
                select(OperationStep)
                .where(
                    OperationStep.operation_id == operation_id,
                    OperationStep.status == operations_service.STEP_PENDING,
                )
                .order_by(OperationStep.step_index)
            ).first()
            if step is None:
                # A cancel that arrives while the LAST step runs has nothing left
                # to skip; the operation is still a cancel, not a success.
                _finish(
                    session,
                    operation,
                    operations_service.STATUS_CANCELLED
                    if status == operations_service.STATUS_CANCELLING
                    else operations_service.STATUS_DONE,
                )
                return
            if status == operations_service.STATUS_CANCELLING:
                _skip_remaining(session, operation_id)
                _finish(session, operation, operations_service.STATUS_CANCELLED)
                return
            # Commit `running` BEFORE the body, so a crash tells "may have run".
            step.status = operations_service.STEP_RUNNING
            step.started_at = operations_service.now()
            session.add(step)
            session.commit()
            step_index = step.step_index
            op = step.op
            args = json.loads(step.args_json or "{}")
            guard_error = _prepare_guard_error(operation, step)

        if guard_error is not None:
            _fail(operation_id, step_index, guard_error)
            return
        try:
            result = _execute(operation_id, step_index, op, args)
        except Exception as exc:
            _fail(operation_id, step_index, exc)
            return
        _complete_step(operation_id, step_index, result)


def _skip_remaining(session: Session, operation_id: str) -> None:
    session.exec(
        update(OperationStep)
        .where(
            OperationStep.operation_id == operation_id,
            OperationStep.status == operations_service.STEP_PENDING,
        )
        .values(status=operations_service.STEP_SKIPPED)
    )


def _count_steps(session: Session, operation_id: str, *, status: Optional[str] = None) -> int:
    filters = [OperationStep.operation_id == operation_id]
    if status is not None:
        filters.append(OperationStep.status == status)
    return int(session.exec(select(func.count()).select_from(OperationStep).where(*filters)).one())


def _finish(session: Session, operation: Operation, status: str) -> None:
    """Mark one operation terminal, release its locks and (for `done`) audit+prune."""
    operation_id = operation.id
    workflow = operation.workflow
    target = operation.target
    done_count = _count_steps(session, operation_id, status=operations_service.STEP_DONE)
    step_count = _count_steps(session, operation_id)
    operation.status = status
    operation.finished_at = operations_service.now()
    operation.undo_state = (
        operations_service.UNDO_AVAILABLE if done_count else operations_service.UNDO_NONE
    )
    session.exec(delete(OperationLock).where(OperationLock.operation_id == operation_id))
    if status == operations_service.STATUS_DONE:
        operations_service.supersede_overlapping(session, operation)
    session.add(operation)
    session.commit()
    if status == operations_service.STATUS_DONE:
        audit.record_system(
            audit.OPERATION_DONE,
            operation_id,
            {"workflow": workflow, "target": target, "steps": step_count},
        )
        operations_service.prune()


def _fail(operation_id: str, step_index: int, exc: Exception) -> None:
    error = str(exc) or exc.__class__.__name__
    with Session(engine) as session:
        operation = session.get(Operation, operation_id)
        if operation is None:
            return
        step = session.exec(
            select(OperationStep).where(
                OperationStep.operation_id == operation_id,
                OperationStep.step_index == step_index,
            )
        ).first()
        if step is not None:
            step.status = operations_service.STEP_FAILED
            step.error = error
            step.finished_at = operations_service.now()
            session.add(step)
        earlier_done = session.exec(
            select(func.count())
            .select_from(OperationStep)
            .where(
                OperationStep.operation_id == operation_id,
                OperationStep.status == operations_service.STEP_DONE,
                OperationStep.step_index < step_index,
            )
        ).one()
        operation.status = operations_service.STATUS_FAILED
        operation.error = error
        operation.finished_at = operations_service.now()
        operation.undo_state = (
            operations_service.UNDO_AVAILABLE if earlier_done else operations_service.UNDO_NONE
        )
        session.exec(delete(OperationLock).where(OperationLock.operation_id == operation_id))
        session.add(operation)
        session.commit()
        detail = {"error": error, "workflow": operation.workflow, "target": operation.target}
    audit.record_system(audit.OPERATION_FAILED, operation_id, detail)


def _complete_step(operation_id: str, step_index: int, result: dict) -> None:
    with Session(engine) as session:
        step = session.exec(
            select(OperationStep).where(
                OperationStep.operation_id == operation_id,
                OperationStep.step_index == step_index,
            )
        ).first()
        if step is None:
            return
        step.status = operations_service.STEP_DONE
        step.result_json = json.dumps(result, sort_keys=True) if result is not None else None
        step.finished_at = operations_service.now()
        session.add(step)
        session.commit()


# Step bodies ------------------------------------------------------------------
# Each body takes `(operation_id, step_index, args)` and returns a JSON-safe
# result dict; an exception is what marks the step (and operation) failed.
# `adopt`/`refresh-groups` are the adopt workflow's two writes: membership, then
# the marker's group list the plan measured. `extract`/`scan` are
# extract-and-create's: the extractor's own write, then the walk that turns its
# files into rows, refused while any planned file is missing.


def _refusal_detail(error: HTTPException) -> str:
    """One HTTP refusal's sentence, without its status prefix.

    `str(HTTPException)` is `"409: <detail>"`: a step that let it through would put
    a status code in front of the extract door's own words, and the operator reads
    the row as the refusal. One helper, so every step body that can meet a door
    refusal spells it the same way.
    """
    detail = error.detail
    return detail if isinstance(detail, str) else str(detail)


def _extract(operation_id: str, step_index: int, args: dict) -> dict:
    """The `extract` step body: `archive_service.extract_out`, the one extractor.

    `start_scan=False` is the step order, not an optimisation: the next step files
    the archive beside its twin and the step after THAT owns the scan. A walk
    started here would race the move and register the archive at its old path.

    The entry policy is EXECUTED, never restated: this body hands the selection to
    the same `extract_out` the extract route calls, so junk, the budget, the
    no-clobber arbiter and the all-or-nothing preflight are that door's, and a
    refusal arrives with that door's sentence (and nothing written).
    """
    try:
        result = archive_service.extract_out(
            args["path"], args.get("entries"), None, start_scan=False
        )
    except HTTPException as error:
        raise RuntimeError(_refusal_detail(error)) from error
    return {
        "path": result.path,
        "destination": result.destination,
        "files": result.files,
        "bytes": result.bytes,
        "already_present": result.already_present,
        "scan_started": result.scan_started,
    }


def _scan(operation_id: str, step_index: int, args: dict) -> dict:
    """The `scan` step body: walk, then resolve the REVIEWED paths, all or nothing.

    The walk is `library_scan.scan_storage_root`, the one registering step the
    library already has; this body owns nothing about discovery. What it owns is
    the comparison: every planned path must now be a registered asset under the
    twin. Every miss is collected before the refusal, so one entry the archive
    lied about cannot hide the other nineteen - and the refusal names the ENTRY,
    which is what the operator saw in the listing, not the rendered library path.

    Zero resolved is its own sentence naming the twin: "none of them registered"
    sends the operator to a different place than "these three did not".

    The cover is resolved here because this is the first moment an asset id
    exists for it: the plan named the first planned model PATH, and the first such
    path in planned order wins.
    """
    library_scan.scan_storage_root()
    pairs = args.get("paths") or []
    with Session(engine) as session:
        registered = {
            asset.path: asset.id
            for asset in bundle_service.registered_assets(session, args["key"])
        }
    members: List[dict] = []
    misses: List[dict] = []
    cover_asset_id: Optional[str] = None
    for pair in pairs:
        asset_id = registered.get(pair["path"])
        if asset_id is None:
            misses.append(pair)
            continue
        members.append({"asset_id": asset_id, "path": pair["path"]})
        if cover_asset_id is None and is_model_path(pair["path"]):
            cover_asset_id = asset_id
    if not members:
        raise RuntimeError(
            f"none of the {len(pairs)} planned file(s) are registered under {args['key']!r} "
            "after a scan; nothing was created"
        )
    if misses:
        raise RuntimeError(
            f"{len(misses)} of {len(pairs)} planned file(s) did not register under "
            f"{args['key']!r} after a scan: "
            + ", ".join(sorted(pair["entry"] for pair in misses))
        )
    return {
        "key": args["key"],
        "members": members,
        "count": len(members),
        "cover_asset_id": cover_asset_id,
    }


def _project_id_from_step(session: Session, operation_id: str, step_index: int) -> str:
    step = session.exec(
        select(OperationStep).where(
            OperationStep.operation_id == operation_id,
            OperationStep.step_index == step_index,
        )
    ).first()
    if step is None or not step.result_json:
        raise RuntimeError(f"step {step_index} has no result to verify against")
    project_id = json.loads(step.result_json).get("project_id")
    if not project_id:
        raise RuntimeError(f"step {step_index} did not create a project")
    return project_id


def _move(operation_id: str, step_index: int, args: dict) -> dict:
    """The `move` step body: the bytes through the primitive, the rows in one commit.

    Two transactions, deliberately. `move_service.move` is asked WITHOUT a session,
    so the ledger row is committed and durable before a single byte moves - a crash
    mid-transfer is then a record naming exactly what was about to happen, not a
    tree with no trace of it. The row half is a SECOND transaction: the created
    destination rows (`ensure_directory_row`, shallowest first) and
    `apply_rows(..., retire_ids=<the ids the plan snapshotted>)` commit together.

    Step order is `move` FIRST, the creates after - the reverse of the
    detected-bundle arrange, whose projects-first order existed because its ledger
    lived in a project's marker. Here the operation's step rows are the record, and
    the semantics owed are that a refusal at the move step stops before any project
    exists: the mover's batch pre-flight is what makes that structural.

    A mid-bytes `OSError` (a cross-device rename, a disk that filled up) is
    COMPENSATED: the ledger this intent already wrote is folded back before the
    error is re-raised, so a failed step does not leave half a split on disk with
    an undo record nobody pointed at. The compensation never masks the original
    failure - if it fails too, the move ledger is still there to fold by hand, and
    the error the operator sees is the one that stopped the step.
    """
    pairs = [move_service.Pair(entry[0], entry[1], entry[2]) for entry in args["pairs"]]
    root = move_service.storage_root()
    intent = operations_service.move_intent(operation_id)
    try:
        outcome = move_service.move(
            pairs, intent=intent, key=args.get("key", ""), apply_rows=False, root=root
        )
    except OSError:
        ledger_id = move_service.ledger_id_for_intent(intent)
        if ledger_id:
            try:
                move_service.undo_ledger(ledger_id)
            except Exception:  # the original failure is the one to report
                log.exception("could not compensate the interrupted move %s", ledger_id)
        raise

    retire_ids = [entry["id"] for entry in args.get("folders") or [] if isinstance(entry, dict)]
    with Session(engine) as session:
        for relative in sorted(args.get("creates") or [], key=lambda path: (path.count("/"), path)):
            folder_service.ensure_directory_row(session, relative)
        move_service.apply_rows(pairs, session=session, root=root, retire_ids=retire_ids)
        session.commit()

    # Re-read after the commit: the retirement decided which of the snapshotted
    # rows it was entitled to delete, and the ones that are gone are the entries
    # undo has to put back. Reporting the snapshot would claim deletes that never
    # happened; reporting nothing would lose the row an emptied container owned.
    retired: List[dict] = []
    if retire_ids:
        with Session(engine) as session:
            present = {row.id for row in session.exec(select(Folder).where(Folder.id.in_(retire_ids)))}
        retired = [
            entry for entry in args.get("folders") or []
            if isinstance(entry, dict) and entry.get("id") not in present
        ]

    return {
        "ledger_id": outcome.ledger_id,
        "landed": outcome.landed,
        "already_applied": outcome.already_applied,
        "moves": [[pair.source, pair.target, pair.kind] for pair in outcome.pairs[: outcome.landed]],
        "creates": list(args.get("creates") or []),
        "retired": retired,
    }


def _create_project(operation_id: str, step_index: int, args: dict) -> dict:
    """The `create-project` step body: `bundle_service.create_project`, one verb.

    The members are handed IN from the step's args rather than re-resolved from the
    key, and that is the review contract, not an optimisation: the operator approved
    a plan that names its members, so the runner must execute that set - a rescan
    between the review and the run would otherwise attach files nobody approved.
    The verb still owns the exclusion, the group merge, the dedupe and the write, so
    a second run of this step is `already_created` with the project the first run
    made instead of a duplicate.

    `groups`/`model_ids` are handed in by the split planner and absent for
    `create-project`: a hoisted folder's own group list and the members that are
    printed parts are facts the plan measured, and the marker's `attached` counts
    are only honest when both come from the reviewed plan rather than from a second
    reading of the disk.

    `resolve_from` is extract-and-create's shape: the members are the SCAN step's
    verified set (and its cover), not a set named at plan time, because no asset id
    exists until the walk has registered the files. `provenance` makes the marker:
    the archive's identity as the key, the twin as `rel_dir`, and the door's own
    block carrying the archive, the planned/resolved counts, the listing's
    fingerprint, its common root and its groups. `create_origin`/`project_origin`
    pick the door's marker discriminator and the `Project.origin` column; both
    default to what every other caller already got.
    """
    resolve_from = args.get("resolve_from")
    provenance = args.get("provenance")
    with Session(engine) as session:
        if resolve_from is not None:
            step = session.exec(
                select(OperationStep).where(
                    OperationStep.operation_id == operation_id,
                    OperationStep.step_index == int(resolve_from),
                )
            ).first()
            if step is None or not step.result_json:
                raise RuntimeError(f"step {resolve_from} has no result to create from")
            try:
                resolved = json.loads(step.result_json)
            except (TypeError, ValueError):
                resolved = {}
            member_rows = resolved.get("members") if isinstance(resolved, dict) else None
            cover_asset_id = resolved.get("cover_asset_id") if isinstance(resolved, dict) else None
            if not member_rows:
                raise RuntimeError(f"step {resolve_from} resolved no members to create from")
        else:
            member_rows = args["members"]
            cover_asset_id = args["cover_asset_id"]

        marker = {
            **args.get(
                "marker",
                {"key": args["key"], "detector": "operations", "rel_dir": args["key"]},
            )
        }
        if provenance is not None:
            # The provenance block is the listing's own story, written at create
            # time: the archive, the twin, planned vs RESOLVED (measured now, not
            # promised by the plan), the fingerprint that makes a changed listing
            # visible, and the listing's structure.
            marker = {
                "key": project_create_service.archive_identity(provenance.get("archive")),
                "rel_dir": provenance.get("twin") or args["key"],
                project_create_service.ORIGIN_ARCHIVE_EXTRACT: {
                    **provenance,
                    "resolved": len(member_rows),
                },
            }
            marker = {name: value for name, value in marker.items() if value is not None}

        dedupe = None
        if args.get("archive"):
            # The two identities this workflow holds: the archive as itself, and the
            # twin as the key a detection-created project would carry.
            dedupe = project_create_service.Dedupe(
                key=args["key"], archive=args["archive"]
            )

        outcome = bundle_service.create_project(
            session,
            key=args["key"],
            name=args["name"],
            members=[
                project_create_service.MemberCandidate(entry["asset_id"], entry["path"])
                for entry in member_rows
            ],
            model_ids=args.get("model_ids") or None,
            marker=marker,
            groups=args.get("groups") or (),
            cover_asset_id=cover_asset_id,
            dedupe=dedupe,
            origin=args.get("create_origin") or project_create_service.ORIGIN_DETECTION,
            project_origin=args.get("project_origin") or "own",
        )
    if outcome.status == project_create_service.STATUS_REFUSED:
        raise RuntimeError(f"create refused: {outcome.reason}")
    return {
        "project_id": outcome.project_id,
        "name": outcome.name,
        "status": outcome.status,
        "match": outcome.match,
        "created": outcome.status == project_create_service.STATUS_CREATED,
        "members": len(outcome.members),
    }


def _verify_members(operation_id: str, step_index: int, args: dict) -> dict:
    with Session(engine) as session:
        project_id = _project_id_from_step(session, operation_id, args["project_step"])
        present = {
            asset_id
            for asset_id in session.exec(
                select(ProjectAsset.asset_id).where(ProjectAsset.project_id == project_id)
            )
        }
        missing = set(args["asset_ids"]) - present
        if missing:
            raise RuntimeError(
                f"{len(missing)} member file(s) did not land in the project: "
                + ", ".join(sorted(missing))
            )
        return {"project_id": project_id, "members": len(present), "verified": True}


def _verify_cover(operation_id: str, step_index: int, args: dict) -> dict:
    with Session(engine) as session:
        project_id = _project_id_from_step(session, operation_id, args["project_step"])
        project = session.get(Project, project_id)
        if project is None:
            raise RuntimeError(f"project {project_id} is gone")
        expected = args["cover_asset_id"]
        actual = project.cover_asset_id
        payload = {
            "project_id": project_id,
            "cover_asset_id": actual,
            "expected": expected,
            "verified": actual == expected,
        }
    if actual != expected:
        raise RuntimeError(f"the project's cover is {actual!r}, expected {expected!r}")
    return payload


def _adopt(operation_id: str, step_index: int, args: dict) -> dict:
    """The `adopt` step body: `adopt_service.adopt`, the members the review named.

    Membership only - it never moves bytes. The ids come from the reviewed plan
    verbatim (a rescan between review and run must not attach files nobody
    approved), while `attached` - the candidate list the marker's `attached`
    counts are bucketed from - is re-read at run time so the flag describes the
    project as it is. The engine's groups travel on the step's args, so the
    measurement is the one the operator reviewed; `GroupFacts` is how a
    serialised group list answers the attribute `measure_groups` reads.
    """
    with Session(engine) as session:
        attached = [
            (asset.id, asset.path, asset.size)
            for asset in bundle_service.registered_assets(session, args["key"])
        ]
        outcome = adopt_service.adopt(
            session,
            args["project_id"],
            args["member_ids"],
            key=args["key"],
            root_rel=args["key"],
            attached=attached,
            facts=adopt_service.GroupFacts(list(args.get("groups") or [])),
        )
        entry = adopt_service.entry_of(outcome, root=args["key"], created=False)
    return {
        "project_id": outcome.project_id,
        "key": args["key"],
        "added": len(outcome.added),
        "already_members": len(outcome.already_members),
        "groups_before": outcome.groups_before,
        "groups_after": outcome.groups_after,
        "entry": entry,
    }


def _refresh_groups(operation_id: str, step_index: int, args: dict) -> dict:
    """The `refresh-groups` step body: write the groups the adopt step measured.

    The list is read off the adopt step's stored result, never re-measured, so
    the marker records the groups the operator reviewed. `set_bundle_groups`
    replaces only `groups` and flushes; this body owns the one commit, then reads
    the row back in a fresh session so the verification is a read, not an echo of
    what it just wrote.
    """
    project_step = int(args["project_step"])
    with Session(engine) as session:
        step = session.exec(
            select(OperationStep).where(
                OperationStep.operation_id == operation_id,
                OperationStep.step_index == project_step,
            )
        ).first()
        if step is None or not step.result_json:
            raise RuntimeError(f"step {project_step} has no result to refresh groups from")
        try:
            result = json.loads(step.result_json)
        except (TypeError, ValueError):
            result = {}
    project_id = result.get("project_id") if isinstance(result, dict) else None
    if not project_id:
        raise RuntimeError(f"step {project_step} did not adopt into a project")
    groups = result.get("groups_after")
    if groups is None:
        raise RuntimeError("the adopt step measured no groups to refresh")

    with Session(engine) as session:
        project_service.set_bundle_groups(session, project_id, groups)
        session.commit()
    with Session(engine) as session:
        project = session.get(Project, project_id)
        if project is None:
            raise RuntimeError(f"project {project_id} is gone")
        stored = project_service.bundle_marker_of(project.metadata_json).get("groups")
        if stored != groups:
            raise RuntimeError("the project's marker groups did not land as reviewed")
    return {"project_id": project_id, "groups": len(groups), "verified": True}


_STEP_BODIES = {
    "extract": _extract,
    "move": _move,
    "scan": _scan,
    "create-project": _create_project,
    "adopt": _adopt,
    "refresh-groups": _refresh_groups,
    "verify-members": _verify_members,
    "verify-cover": _verify_cover,
}


def _execute(operation_id: str, step_index: int, op: str, args: dict) -> dict:
    body = _STEP_BODIES.get(op)
    if body is None:
        raise RuntimeError(f"unknown step op {op!r}")
    return body(operation_id, step_index, args)


# Startup repair ---------------------------------------------------------------


def resume_on_startup() -> List[str]:
    """Repair interrupted rows; return the ids it re-queued.

    A step found `running` may have run, so it is failed, never re-run. A row
    whose first unfinished step is still `pending` never started that step, so
    it is safe to re-queue. When the background thread is off (tests) each
    resumed id is executed inline, so one lifespan is one synchronous repair.
    """
    resumed: List[str] = []
    with Session(engine) as session:
        rows = list(
            session.exec(
                select(Operation).where(
                    Operation.status.in_(
                        (operations_service.STATUS_RUNNING, operations_service.STATUS_CANCELLING)
                    )
                )
            )
        )
        for operation in rows:
            steps = list(
                session.exec(
                    select(OperationStep)
                    .where(OperationStep.operation_id == operation.id)
                    .order_by(OperationStep.step_index)
                )
            )
            running = [step for step in steps if step.status == operations_service.STEP_RUNNING]
            if running:
                _fail_interrupted(session, operation, steps, running[0])
                continue
            if all(
                step.status
                in (
                    operations_service.STEP_DONE,
                    operations_service.STEP_FAILED,
                    operations_service.STEP_SKIPPED,
                )
                for step in steps
            ):
                _repair_terminal(session, operation, steps)
                continue
            # The first unfinished step is still `pending`: it never started.
            session.exec(delete(OperationLock).where(OperationLock.operation_id == operation.id))
            operation.status = operations_service.STATUS_QUEUED
            operation.started_at = None
            session.add(operation)
            session.commit()
            resumed.append(operation.id)

    if not should_run():
        for operation_id in resumed:
            run_operation(operation_id)
    return resumed


def _fail_interrupted(
    session: Session, operation: Operation, steps: List[OperationStep], running: OperationStep
) -> None:
    pending = [step for step in steps if step.status == operations_service.STEP_PENDING]
    running.status = operations_service.STEP_FAILED
    running.error = "interrupted"
    running.finished_at = operations_service.now()
    session.add(running)
    operation.status = operations_service.STATUS_FAILED
    operation.error = (
        f"step {running.step_index} ({running.op}) was interrupted before it finished; "
        f"{len(pending)} step(s) not started"
    )
    operation.finished_at = operations_service.now()
    operation.undo_state = (
        operations_service.UNDO_AVAILABLE
        if any(
            step.status == operations_service.STEP_DONE and step.step_index < running.step_index
            for step in steps
        )
        else operations_service.UNDO_NONE
    )
    session.exec(delete(OperationLock).where(OperationLock.operation_id == operation.id))
    session.add(operation)
    session.commit()
    audit.record_system(
        audit.OPERATION_FAILED,
        operation.id,
        {"error": operation.error, "workflow": operation.workflow, "target": operation.target},
    )


def _repair_terminal(
    session: Session, operation: Operation, steps: List[OperationStep]
) -> None:
    every_done = all(step.status == operations_service.STEP_DONE for step in steps)
    any_done = any(step.status == operations_service.STEP_DONE for step in steps)
    operation.status = (
        operations_service.STATUS_DONE if every_done else operations_service.STATUS_CANCELLED
    )
    operation.finished_at = operations_service.now()
    operation.undo_state = (
        operations_service.UNDO_AVAILABLE if any_done else operations_service.UNDO_NONE
    )
    session.exec(delete(OperationLock).where(OperationLock.operation_id == operation.id))
    session.add(operation)
    session.commit()


def shutdown() -> None:
    """Stop the pool without waiting; idempotent."""
    global _pool_executor
    with _pool_lock:
        pool = _pool_executor
        _pool_executor = None
    if pool is not None:
        pool.shutdown(wait=False)
