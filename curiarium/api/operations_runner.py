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

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, delete, select

import audit
import bundle_service
import operations_service
import project_create_service
from db import engine
from models import Operation, OperationLock, OperationStep, Project, ProjectAsset

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


def _create_project(operation_id: str, step_index: int, args: dict) -> dict:
    """The `create-project` step body: `bundle_service.create_project`, one verb.

    The members are handed IN from the step's args rather than re-resolved from the
    key, and that is the review contract, not an optimisation: the operator approved
    a plan that names its members, so the runner must execute that set - a rescan
    between the review and the run would otherwise attach files nobody approved.
    The verb still owns the exclusion, the group merge, the dedupe and the write, so
    a second run of this step is `already_created` with the project the first run
    made instead of a duplicate.
    """
    with Session(engine) as session:
        outcome = bundle_service.create_project(
            session,
            key=args["key"],
            name=args["name"],
            members=[
                project_create_service.MemberCandidate(entry["asset_id"], entry["path"])
                for entry in args["members"]
            ],
            marker={"key": args["key"], "detector": "operations", "rel_dir": args["key"]},
            cover_asset_id=args["cover_asset_id"],
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


_STEP_BODIES = {
    "create-project": _create_project,
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
