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
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Set
from zipfile import BadZipFile

from fastapi import HTTPException
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, delete, select

import adopt_service
import archive_service
import asset_service
import audit
import bundle_service
import folder_service
import import_policy
import library_scan
import move_service
import operations_service
import project_create_service
import project_import
import project_service
import schemas
import seams
import zip_service
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
# the marker's group list the plan measured. `extract`/`register` are
# extract-and-create's: the extractor's own write, then the registration that
# turns the plan's named files into rows, refused while any planned file is
# missing. `fetch`/`fetch-zip` are the two ingest doors' own calls: one link, one
# archive, each the primitive the deleted route used with the plan as its review.
# `attach-project` is the project variant of either ingest: one call to
# `project_import.import_into_project`, because the gated outcome and the page it
# reads are one decision that cannot be split across steps.


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
    the archive beside its twin and the step after THAT owns the registration. A
    walk started here would race the move and register the archive at its old
    path.

    The entry policy is EXECUTED, never restated: this body hands the selection to
    the same `extract_out` the deleted route called, so junk, the budget, the
    no-clobber arbiter and the all-or-nothing preflight are that door's, and a
    refusal arrives with that door's sentence (and nothing written).

    `audit_ctx` is the one difference from extract-and-create's extract step. The
    in-place extract IS the operator's own action, so the primitive's own
    `archive.extract` row is written under the runner's context; extract-and-create
    records its composition through its own steps and passes None, so its
    committed behaviour is unchanged.
    """
    try:
        result = archive_service.extract_out(
            args["path"],
            args.get("entries"),
            audit.runner_context() if args.get("audit_ctx") else None,
            start_scan=False,
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


def _register(operation_id: str, step_index: int, args: dict) -> dict:
    """The `register` step body: `library_scan.register`, the primitive, not a walk.

    `library_scan.register` writes a row for EXACTLY the paths it is handed: there
    is no discovery here, no whole-root walk and no pruning, so the members are the
    PLAN's list and no row is ever written for a file the plan did not name. That
    is the whole point of this step: the review named 20 files, and the 20 rows the
    create step attaches are those files, in that order.

    `args["paths"]` is the listing's planned list (the one list the planner
    computed) and `args["archive_path"]` is the archive's filed home, named so the
    `<twin>/_source/<archive>.zip` row a scan used to leave survives - it is
    registered but never a member.

    Every planned path must come back with an id. Misses are collected before the
    refusal, so one entry the archive lied about cannot hide the other nineteen,
    and the refusal names the ENTRY (the archive-relative name the operator saw in
    the listing), not the rendered library path. Zero resolved is its own sentence
    naming the twin: "none of them registered" sends the operator somewhere
    different than "these three did not". A path the primitive REFUSES
    (`RegisterPathError`) becomes a failed step with that refusal's sentence, not a
    traceback.

    The cover is resolved here because this is the first moment an asset id exists
    for it: the plan named the first planned model PATH, and the first such path in
    planned order wins. No cover is not an error.

    `members` and `count` are the shape the create step and the workflow's
    witnesses read; `registered`/`known`/`absent`/`failed` are the primitive's own
    report, kept verbatim so a step row says what was written and what was not.

    `created_folders` is measured with two id sets around the primitive, so an
    undo can delete exactly the folder rows this step made and never one that
    already existed.
    """
    pairs = args.get("paths") or []
    # The ENTRY is what the operator saw in the listing; `path` is where the plan
    # said it would land. A refusal names the entry, so the miss is findable.
    entry_by_path = {pair["path"]: pair.get("entry") or pair["path"] for pair in pairs}
    named = [pair["path"] for pair in pairs]
    if args.get("archive_path"):
        named.append(args["archive_path"])
    try:
        folders_before = _folder_ids()
        result = library_scan.register(named)
        created_folders = sorted(_folder_ids() - folders_before)
    except library_scan.RegisterPathError as error:
        raise RuntimeError(str(error)) from error

    asset_id_by_path = {entry.rel_path: entry.asset_id for entry in result.entries}
    members: List[dict] = []
    misses: List[str] = []
    cover_asset_id: Optional[str] = None
    for pair in pairs:
        asset_id = asset_id_by_path.get(pair["path"])
        if asset_id is None:
            misses.append(entry_by_path[pair["path"]])
            continue
        members.append({"asset_id": asset_id, "path": pair["path"]})
        if cover_asset_id is None and is_model_path(pair["path"]):
            cover_asset_id = asset_id
    if not members:
        raise RuntimeError(
            f"none of the {len(pairs)} planned file(s) are registered under {args['key']!r}; "
            "nothing was created"
        )
    if misses:
        raise RuntimeError(
            f"{len(misses)} of {len(pairs)} planned file(s) did not register under "
            f"{args['key']!r}: "
            + ", ".join(sorted(misses))
        )
    return {
        "key": args["key"],
        "members": members,
        "count": len(members),
        "cover_asset_id": cover_asset_id,
        "registered": [entry.rel_path for entry in result.entries if entry.created],
        "known": [entry.rel_path for entry in result.known],
        "absent": list(result.absent),
        "failed": list(result.failed),
        "created_folders": created_folders,
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

    `resolve_from` is extract-and-create's shape: the members are the REGISTER step's
    verified set (and its cover), not a set named at plan time, because no asset id
    exists until the files have rows. That indirection is guarded, not trusted
    (Oracle B-11): the resolve_from step's OWN `args_json` is re-read and the
    ORDERED member paths must equal the paths the plan named - a difference is a
    FAILURE naming both lists' first divergence, never a warning, because a create
    that re-resolved from the key would attach files nobody approved. The outcome
    must also be `created`, with ONE named exception: a run-time dedupe hit
    (`already_created`) is an identity answer - it keeps the pre-existing project
    untouched and creates nothing - and it is narrated in the result as
    `deduped: True`, whose `members` count is the existing project's, not the
    planned count. `provenance` makes the marker: the archive's identity as the
    key, the twin as `rel_dir`, and the door's own block carrying the archive, the
    planned/resolved counts, the listing's fingerprint, its common root and its
    groups. `create_origin`/`project_origin` pick the door's marker discriminator
    and the `Project.origin` column; both default to what every other caller
    already got.
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
            # Oracle B-11: the resolve_from step's own args ARE the plan's list, and
            # the create must attach that list in that order. A difference is a
            # failed step, never a warning: a register step that re-resolved from the
            # key would attach files the operator never approved.
            planned_paths = _planned_paths_of(step)
            member_paths = [entry["path"] for entry in member_rows]
            if member_paths != planned_paths:
                raise RuntimeError(
                    f"step {resolve_from} did not keep the plan's member list: "
                    + _first_divergence(member_paths, planned_paths)
                )
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
    deduped = outcome.status == project_create_service.STATUS_ALREADY_CREATED
    if not deduped and outcome.status != project_create_service.STATUS_CREATED:
        raise RuntimeError(
            f"create answered {outcome.status!r} for {args['name']!r}: only a created "
            "project, or the already_created identity answer, confirms one"
        )
    return {
        "project_id": outcome.project_id,
        "name": outcome.name,
        "status": outcome.status,
        "match": outcome.match,
        "created": outcome.status == project_create_service.STATUS_CREATED,
        # A run-time dedupe hit is an identity answer, not a create: this project
        # already existed and is untouched, so `members` below is the EXISTING
        # project's count, not the planned one.
        "deduped": deduped,
        "members": len(outcome.members),
    }


def _planned_paths_of(step: OperationStep) -> List[str]:
    """The ORDERED paths a register step's own args named: the plan's list, re-read.

    Read off the OPENED step row's `args_json`, not a fresh resolve from the key, so
    a register step that attached a file nobody named (or dropped one the plan
    named) is caught at the create rather than passed through.
    """
    try:
        resolved_args = json.loads(step.args_json or "{}")
    except (TypeError, ValueError):
        return []
    return [
        pair["path"]
        for pair in (resolved_args.get("paths") or [])
        if isinstance(pair, dict) and "path" in pair
    ]


def _first_divergence(actual_paths: List[str], planned_paths: List[str]) -> str:
    """The sentence naming where two ordered member lists part company.

    Both counts and the first divergent position with both entries: 20 paths in a
    sentence do not help an operator locate the one that moved, and a silent
    difference is exactly the drift this guard exists to refuse.
    """
    shared = min(len(actual_paths), len(planned_paths))
    divergence = shared
    for index in range(shared):
        if actual_paths[index] != planned_paths[index]:
            divergence = index
            break
    actual = actual_paths[divergence] if divergence < len(actual_paths) else "nothing"
    planned = planned_paths[divergence] if divergence < len(planned_paths) else "nothing"
    return (
        f"registered {len(actual_paths)} member(s) but the plan named {len(planned_paths)}; "
        f"the ordered lists first diverge at position {divergence}: "
        f"registered {actual!r}, planned {planned!r}"
    )


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


def _scan(operation_id: str, step_index: int, args: dict) -> dict:
    """The `scan` step body: `library_scan.scan_subtree`, the walk itself.

    The scan is this workflow's whole write, and the runner IS the thread the
    deleted route used to spawn: the walk runs inside this step, so the operation
    row's lifecycle is the scan's lifecycle. `scope` of ``''`` is the whole
    library; anything else is a folder the plan already checked exists.

    The result is ``ScanState.as_dict()`` VERBATIM - no extra keys - because that
    field set is the wire shape the deleted status route published. A second slot
    is refused by the primitive, and the refusal is re-raised with the door's own
    sentence so the step row reads the way the operator used to see the 409.
    """
    scope = args.get("scope") or ""
    try:
        state = library_scan.scan_subtree(scope)
    except library_scan.ScanAlreadyRunning as error:
        raise RuntimeError(library_scan.ALREADY_RUNNING_DETAIL) from error
    return state.as_dict()


def _submitted_by(operation_id: str) -> Optional[str]:
    """The username the operation was submitted by; the human an import belongs to.

    `AssetOrigin.for_principal` read the principal off the request; the runner has
    no request, so it reads the submission instead. A row that is gone answers
    `None`, which `AssetOrigin` records honestly as (NULL, created_via).
    """
    with Session(engine) as session:
        operation = session.get(Operation, operation_id)
        return None if operation is None else operation.submitted_by


def _import_origin(operation_id: str, created_via: str) -> asset_service.AssetOrigin:
    """Where a step-created asset came from: the submitter, the door, the runner."""
    return asset_service.AssetOrigin(
        created_by=_submitted_by(operation_id),
        created_via=created_via,
        audit_ctx=audit.runner_context(),
    )


# The two session cookies an import request can carry, and the value a persisted
# ledger row keeps in their place. Nothing but a dash, so a reader of the row can
# tell "this plan had a cookie" from "this plan had none".
COOKIE_ARG_NAMES = ("makerworld_cookie", "thingiverse_cookie")
COOKIE_REDACTION = "-"


def _redact_request_cookies(request) -> bool:
    """Replace every session cookie in ONE parsed import request with `"-"`.

    The dict is mutated in place and is always one this module just parsed out of a
    persisted row - never the dict a step body is using, which is why the caller
    hands in the JSON it is about to write back rather than the args it was given.
    Returns whether anything changed, so a plan that carried no cookie does not
    write its rows back for nothing.
    """
    if not isinstance(request, dict):
        return False
    redacted = False
    for name in COOKIE_ARG_NAMES:
        if name in request:
            request[name] = COOKIE_REDACTION
            redacted = True
    return redacted


def _redact_plan_cookies(plan) -> bool:
    """Redact the cookies a persisted `plan_json` holds: intent params and step args.

    The plan persists the intent (`intent["params"]` IS the request params the plan
    was built from) and the step list with its args, so the same credential is in a
    second place in the ledger. Both are redacted; the digest and `paths` are
    untouched, and nothing reads a persisted step's args back.
    """
    if not isinstance(plan, dict):
        return False
    intent = plan.get("intent")
    redacted = _redact_request_cookies(intent.get("params") if isinstance(intent, dict) else None)
    for step in plan.get("steps") or []:
        if isinstance(step, dict):
            args = step.get("args")
            if isinstance(args, dict):
                redacted = _redact_request_cookies(args.get("request")) or redacted
    return redacted


def _scrub_cookies(operation_id: str, step_index: int, args: dict) -> None:
    """Take the import's session cookies out of the ledger, now that the step has them.

    THE rule, from `docs/AUDIT.md` and `tests/test_import_policy.py`: a site session
    cookie never comes to rest in a record a human can read back. The deleted import
    doors kept theirs in memory only. An import is a PLAN now, and the plan is
    persisted - `operation_step.args_json` holds the step's args verbatim, and
    `operation.plan_json` holds the intent's params and the step list (which is the
    same args) - so the plan shape is exactly where this rule would have been lost.

    So the credential rides in the plan until the step TAKES it: the body builds its
    request object from the in-memory `args` (which this function never touches), and
    the step then redacts its own persisted rows the moment it has the value - before
    the network call it exists for, so a failure later cannot put it back. From the
    first instant the step runs, the credential is nowhere in the ledger; the
    runner's own audit rows never carried it in the first place (`operation.submit`
    and `operation.done` name the workflow, the target and the counts).

    Best-effort by construction: a row that is missing, unparseable or unwritable is
    a logged no-op, because a read the scrub could not make must never be able to
    fail the import. The in-memory `args` are read only to see whether there is
    anything to redact.
    """
    request = args.get("request")
    if not isinstance(request, dict) or not any(name in request for name in COOKIE_ARG_NAMES):
        return
    try:
        with Session(engine) as session:
            step = session.exec(
                select(OperationStep).where(
                    OperationStep.operation_id == operation_id,
                    OperationStep.step_index == step_index,
                )
            ).first()
            if step is not None:
                persisted = json.loads(step.args_json or "{}")
                if _redact_request_cookies(
                    persisted.get("request") if isinstance(persisted, dict) else None
                ):
                    step.args_json = json.dumps(persisted, sort_keys=True)
                    session.add(step)
            operation = session.get(Operation, operation_id)
            if operation is not None:
                plan = json.loads(operation.plan_json or "{}")
                if _redact_plan_cookies(plan):
                    operation.plan_json = json.dumps(plan, sort_keys=True)
                    session.add(operation)
            session.commit()
    except Exception:  # a redaction must never be the reason an import fails
        log.warning("could not redact the import cookie on step %s of %s", step_index, operation_id)


def _fetch(operation_id: str, step_index: int, args: dict) -> dict:
    """The `fetch` step body: the deleted link door's call, in the runner's thread.

    The door's order is the contract: `refuse_paid_or_gated` first (URL-only, no
    DNS), then `normalize_import_url`, then the fetch itself. The calls go through
    `seams.` - the documented patch surface - and never to the service functions
    directly: the whole suite stubs `main.import_asset_from_url`,
    `main.normalize_import_url` and `main.download_import_to_temp`, so calling the
    service would silently stop honouring those stubs and open a real socket in
    every test that patches them.

    A door refusal becomes the step's own sentence, without its status prefix, so
    the row reads the way the operator used to see the response.

    `_scrub_cookies` sits between the request object and the call on purpose: the
    body has the cookie from the instant it is built, and the persisted rows lose
    it before the fetch that needs it - so no failure after this point can leave a
    session cookie at rest (see that function for the whole rule).
    """
    origin = _import_origin(operation_id, "link-import")
    try:
        import_policy.refuse_paid_or_gated(args["url"])
        url = seams.normalize_import_url(args["url"])
        body = schemas.ImportRequest(url=url, **args["request"])
        # The request object has the cookie now, so the ledger row lets it go before
        # the one call it is for: a failed fetch cannot leave it behind.
        _scrub_cookies(operation_id, step_index, args)
        asset = seams.import_asset_from_url(url, body, origin=origin)
    except HTTPException as error:
        raise RuntimeError(_refusal_detail(error)) from error
    return {
        "created": True,
        "asset_id": asset.id,
        "path": asset.storage_path,
        "filename": asset.filename,
        "size": asset.size,
        "url": url,
    }


def _folder_ids() -> Set[str]:
    """Every folder row id, read in its own session: the before/after measurement."""
    with Session(engine) as session:
        return set(session.exec(select(Folder.id)))


def _fetch_zip(operation_id: str, step_index: int, args: dict) -> dict:
    """The `fetch-zip` step body: the deleted zip door's sequence, sentence for sentence.

    One body, not a fetch and an extract, because the door's guarantee is that the
    temp file is ALWAYS deleted: the `finally` that unlinks it lives here, so a
    cancelled or failed operation cannot leak the file across a step boundary.

    The `seams.` accessors are the patch surface (`main.download_import_to_temp`
    and `main.normalize_import_url` are what the suite stubs), while
    `extract_zip_entries_to_assets` has no seam and is the same function the
    archive and project doors call - the policy it executes is `entry_policy`'s,
    not a copy.

    `created_folders` is measured with two id sets around the extract, so the undo
    knows exactly which mirror rows this run created and never touches a folder
    that already existed.

    `_scrub_cookies` runs once the request object is built and before the download,
    the same order as `fetch`: the credential is redacted from the ledger before
    any byte is requested.
    """
    origin = _import_origin(operation_id, "zip-import")
    body = schemas.ImportZipExtractRequest(url=args["url"], **args["request"], entries=args["entries"])
    try:
        import_policy.refuse_paid_or_gated(args["url"])
        url = seams.normalize_import_url(args["url"])
        # Same order as `fetch`: the body has the cookie, the ledger is redacted, and
        # only then does the download happen.
        _scrub_cookies(operation_id, step_index, args)
        tmp_path, filename, _mime = seams.download_import_to_temp(url, body)
        try:
            if Path(filename).suffix.lower() != ".zip":
                raise HTTPException(status_code=415, detail="Imported file is not a zip")
            folders_before = _folder_ids()
            try:
                assets, failed = zip_service.extract_zip_entries_to_assets(
                    tmp_path, body.entries, body, origin=origin
                )
            except BadZipFile:
                raise HTTPException(status_code=400, detail="Invalid zip file") from None
            created_folders = sorted(_folder_ids() - folders_before)
        finally:
            tmp_path.unlink(missing_ok=True)
    except HTTPException as error:
        raise RuntimeError(_refusal_detail(error)) from error
    return {
        "created": [asset.id for asset in assets],
        "assets": [
            {
                "asset_id": asset.id,
                "path": asset.storage_path,
                "filename": asset.filename,
                "size": asset.size,
            }
            for asset in assets
        ],
        "failed": failed,
        "filename": filename,
        "created_folders": created_folders,
    }


def _attach_project(operation_id: str, step_index: int, args: dict) -> dict:
    """The `attach-project` step body: the deleted project door's own call.

    `project_import.import_into_project` is ONE decision from the fetch to the
    membership: it reads the attempt and its page, decides whether a refusal is a
    gated outcome or the original error, and writes the `{'_files'}` marker only
    when the project it found has no members. Splitting that across steps would
    reimplement `attempt.page`, `parse_page_metadata` and `_is_gated`, so this
    body calls the whole function exactly as the route did.

    `origin` names the link door here; the primitive re-stamps every zip member's
    row `zip-import` itself (`_zip_assets`), so a zip selection inside a project is
    attributed honestly without this body branching.

    A door refusal becomes the step's own sentence, without its status prefix.
    `_scrub_cookies` sits between the request object and the call for the same
    reason as `fetch`: the credential rides in the persisted plan until the body
    has taken it, and no failure after this point can leave it at rest.

    `added` is the membership ids the run created, and it is the whole ledger the
    undo needs: a membership an operator adds later is not in it.
    """
    origin = _import_origin(operation_id, "link-import")
    body = schemas.ImportProjectRequest(
        url=args["url"],
        entries=args.get("entries"),
        project_id=args.get("project_id"),
        project_name=args.get("project_name"),
        **args["request"],
    )
    try:
        import_policy.refuse_paid_or_gated(args["url"])
        url = seams.normalize_import_url(args["url"])
        _scrub_cookies(operation_id, step_index, args)
        outcome = project_import.import_into_project(url, body, origin=origin)
    except HTTPException as error:
        raise RuntimeError(_refusal_detail(error)) from error
    return {
        "project_id": outcome.project_id,
        "created": outcome.created,
        "gated": outcome.gated,
        "message": outcome.message,
        "added": outcome.added,
        "skipped": outcome.skipped,
        "failed": outcome.failed,
        "created_assets": [asset.id for asset in outcome.assets],
        "assets": [
            {
                "asset_id": asset.id,
                "path": asset.storage_path,
                "filename": asset.filename,
                "size": asset.size,
            }
            for asset in outcome.assets
        ],
        "url": url,
    }


class _CookieBody(NamedTuple):
    """The one attribute `resolve_thingiverse_cookie` reads off a request body.

    The resolver's Protocol names exactly `thingiverse_cookie`, and the deleted
    enrich route handed it the pydantic request body. A step's args are a dict,
    and a dict would be a type lie (it has no attribute), so this tiny structural
    stand-in is the body the resolver already expects.
    """

    thingiverse_cookie: Optional[str]


def _cookie_body(request: dict) -> _CookieBody:
    """The resolver's body from a step's `request` mapping."""
    return _CookieBody(request.get("thingiverse_cookie"))


def _enrich(operation_id: str, step_index: int, args: dict) -> dict:
    """The `enrich` step body: the deleted enrichment door's one call, unchanged.

    `bundle_service.enrich_bundles` is not reimplemented here: the per-key
    vocabulary, the ONE cache write per call, the partial-success rule (a refusal
    is a per-bundle result, never a failed step) and the `bundle.enrich` rows it
    writes under the context it is given are all the primitive's. The result is
    returned VERBATIM - `{fetched, cached, refused, unavailable, unknown,
    results}` - because that is the wire shape the deleted route published and
    the undo reads the same `fetched_at` stamps out of it.

    `_scrub_cookies` runs before the resolver and the fetch: the in-memory args
    still have the cookie, but the persisted step args and plan lose it first, so
    no failure after this point can leave a session cookie at rest.
    """
    _scrub_cookies(operation_id, step_index, args)
    # Function-local, like the primitive's own: the resolver is a socket module and
    # this body is the only place the runner needs it.
    from import_resolvers import resolve_thingiverse_cookie

    request = args.get("request") or {}
    cookie = resolve_thingiverse_cookie(_cookie_body(request))
    return bundle_service.enrich_bundles(
        args["keys"],
        cookie=cookie,
        refresh=bool(args.get("refresh")),
        ctx=audit.runner_context(),
    )


_STEP_BODIES = {
    "extract": _extract,
    "move": _move,
    "register": _register,
    "create-project": _create_project,
    "adopt": _adopt,
    "refresh-groups": _refresh_groups,
    "verify-members": _verify_members,
    "verify-cover": _verify_cover,
    # The scan is the `library-scan` workflow's whole write. The deleted route
    # spawned a background thread; the runner IS that thread, so the walk runs
    # inside this step.
    "scan": _scan,
    # `fetch` belongs to `import-link` and `fetch-zip` to `import-zip`: the two
    # deleted ingest doors, whose bodies now run in the runner's thread.
    "fetch": _fetch,
    "fetch-zip": _fetch_zip,
    # `attach-project` is the project target of either ingest workflow, not a
    # workflow of its own: one step, because the primitive's gated decision is
    # the fetch and its page together.
    "attach-project": _attach_project,
    # `enrich` is the `enrich` workflow's whole write: one call to the unchanged
    # `bundle_service.enrich_bundles`.
    "enrich": _enrich,
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
