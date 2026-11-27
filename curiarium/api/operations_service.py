"""The operations ledger: server-computed plans, one queue, one in-process runner.

The server computes every plan. A client cannot invent a step list: `POST
/operations` accepts a `plan_id` and an `idempotency_key` and no steps, and the
`plan_id` is a signed-by-construction `(intent, digest)` pair the server issued.
The plan is recomputed at submit, so a stale plan - one whose steps no longer
match the library - is refused with a 409 instead of running something nobody
reviewed.

The database is the truth and the runner is in-process (`operations_runner`).
An `operation` row is the durable record of an intent; the runner is one thread
pool that reads queued rows, takes prefix locks and executes steps. There is one
shared queue, attributed to the submitting user - not a queue per user - so two
operations over the same paths serialise instead of racing.

This module owns the plan codec, the `create-project` planner, submit, the
reads, cancel, undo, supersede and prune. It never executes a workflow: the
runner does, and the routes only ever write a row.
"""

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import List, NamedTuple, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, delete, select

import audit
import bundle_service
import library_scan
import project_create_service
import project_service
from db import engine
from models import Asset, Operation, OperationLock, OperationStep, Project, ProjectAsset

PLAN_VERSION = 1
WORKFLOWS = ("create-project",)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_CANCELLING = "cancelling"
STATUS_CANCELLED = "cancelled"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
TERMINAL_STATUSES = frozenset({STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED})
STATUSES = frozenset(
    {STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING, STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED}
)

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

UNDO_NONE = "none"
UNDO_AVAILABLE = "available"
UNDO_UNDONE = "undone"
UNDO_SUPERSEDED = "superseded"

PRUNE_KEEP_NEWEST = 200
PRUNE_MAX_AGE_DAYS = 30

STALE_PLAN_MESSAGE = "the library changed since this plan was reviewed; review it again"
BAD_PLAN_MESSAGE = "plan_id is not a plan this server issued"


class Plan(NamedTuple):
    """One computed plan: the intent, its steps, the review and the digest.

    `steps` is a list of `{"index", "op", "summary", "args"}`; `args` stay
    server-side and `plan_response()` drops them.
    """

    intent: dict
    steps: list
    review: dict
    digest: str
    declared_paths: List[str]


# The plan id codec ------------------------------------------------------------


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode())


def plan_digest(intent: dict, steps: list, review: dict) -> str:
    return hashlib.sha256(_canonical({"intent": intent, "steps": steps, "review": review})).hexdigest()


def encode_plan_id(intent: dict, digest: str) -> str:
    return f"{_b64(_canonical(intent))}.{digest}"


def decode_plan_id(plan_id: str) -> Tuple[dict, str]:
    """Decode a plan id back to `(intent, digest)`; anything malformed is a 400."""
    try:
        encoded, digest = (plan_id or "").split(".", 1)
        intent = json.loads(_unb64(encoded))
    except Exception:
        raise HTTPException(status_code=400, detail=BAD_PLAN_MESSAGE) from None
    if (
        not isinstance(intent, dict)
        or intent.get("v") != PLAN_VERSION
        or intent.get("workflow") not in WORKFLOWS
        or not isinstance(intent.get("target"), str)
    ):
        # A well-formed blob that names a workflow this build does not serve was
        # not issued by this server, so it is a 400 and never an "unknown
        # workflow" 400 dressed up as a request error.
        raise HTTPException(status_code=400, detail=BAD_PLAN_MESSAGE)
    return intent, digest


# Planning ---------------------------------------------------------------------


def build_plan(session: Session, workflow: str, target: str, params: Optional[dict] = None) -> Plan:
    """Compute the plan for `workflow` over `target`; never writes anything."""
    if workflow not in WORKFLOWS:
        raise HTTPException(status_code=400, detail=f"unknown workflow {workflow!r}")
    # The intent carries the NORMALISED target, so two spellings of one directory
    # are one plan and `Operation.target` reads the way `declared_paths` does.
    normalized = (target or "").strip().strip("/")
    intent = {"v": PLAN_VERSION, "workflow": workflow, "target": normalized, "params": params or {}}
    steps, review, declared = _plan_create_project(session, normalized, params or {})
    digest = plan_digest(intent, steps, review)
    return Plan(intent=intent, steps=steps, review=review, digest=digest, declared_paths=declared)


def plan_response(plan: Plan) -> dict:
    return {
        "plan_id": encode_plan_id(plan.intent, plan.digest),
        "workflow": plan.intent["workflow"],
        "target": plan.intent["target"],
        "steps": [
            {"index": step["index"], "op": step["op"], "summary": step["summary"]}
            for step in plan.steps
        ],
        "review": plan.review,
    }


def recompute_plan(plan_id: str) -> Plan:
    """Rebuild a plan from a fresh session and refuse it if it changed."""
    intent, digest = decode_plan_id(plan_id)
    with Session(engine) as session:
        plan = build_plan(session, intent.get("workflow"), intent.get("target"), intent.get("params") or {})
    if plan.digest != digest:
        raise HTTPException(status_code=409, detail=STALE_PLAN_MESSAGE)
    return plan


def _group_of(rel_path: str) -> str:
    """The first path segment of a bundle-relative path, or `""` for the root."""
    return rel_path.split("/", 1)[0] if "/" in rel_path else ""


def _plan_create_project(session: Session, target: str, params: dict) -> Tuple[list, dict, List[str]]:
    """The `create-project` planner: one library-relative directory key in, three steps out."""
    key = (target or "").strip().strip("/")
    if not key:
        raise HTTPException(
            status_code=409,
            detail="the target must be a library-relative directory key (not the library root)",
        )
    assets = bundle_service.registered_assets(session, key)
    if not assets:
        raise HTTPException(status_code=409, detail=f"no registered files under {key!r}")
    # The one owner of the member rule, through its public name (Mf-215n promoted
    # the alias, so this door no longer reaches for a private one).
    member_set = bundle_service.members_of(key, assets)
    entries = list(member_set.entries)

    groups: dict = {}
    for entry in entries:
        group_name = _group_of(entry.rel_path)
        group = groups.setdefault(group_name, {"name": group_name, "members": 0, "models": 0})
        group["members"] += 1
        if entry.role == library_scan.MODEL:
            group["models"] += 1
    group_list = list(groups.values())

    # Named and deduped FIRST: the "already a member" exclusion below needs to
    # know which project this plan's own create would answer, so it never
    # excludes the members of that project.
    name = (params.get("name") or "").strip() or key.rsplit("/", 1)[-1]
    existing = project_create_service.find_existing_project(session, key=key, name=name)

    excluded: List[dict] = []

    # 1. A group that is already its own project is excluded wholesale.
    known = bundle_service.known_project_keys(session)
    excluded_groups = {
        group_name for group_name in groups if group_name and f"{key}/{group_name}" in known
    }
    for group_name in sorted(excluded_groups):
        excluded.append(
            {"path": f"{key}/{group_name}", "reason": "this directory is already its own project"}
        )
    remaining = [entry for entry in entries if _group_of(entry.rel_path) not in excluded_groups]

    # One query for the Asset rows, one for the membership map; no per-member reads.
    member_ids = [entry.asset.id for entry in remaining]
    asset_rows = (
        {asset.id: asset for asset in session.exec(select(Asset).where(Asset.id.in_(member_ids)))}
        if member_ids
        else {}
    )
    membership: dict = {}
    if member_ids:
        statement = (
            select(ProjectAsset.asset_id, Project.id, Project.name)
            .join(Project, Project.id == ProjectAsset.project_id)
            .where(ProjectAsset.asset_id.in_(member_ids))
        )
        for asset_id, project_id, project_name in session.exec(statement):
            membership[asset_id] = (project_id, project_name)

    # 2. A file the last scan flagged missing is not a member.
    missing_ids = set()
    for entry in remaining:
        asset = asset_rows.get(entry.asset.id)
        if asset is not None and asset.missing_since:
            excluded.append(
                {
                    "path": asset.storage_path or entry.asset.path,
                    "reason": "the file is missing from disk (flagged by the last scan)",
                }
            )
            missing_ids.add(entry.asset.id)
    after_missing = [entry for entry in remaining if entry.asset.id not in missing_ids]

    # 3. A file already a member of ANOTHER project is not a member of a second
    #    one. The project this plan's own create would dedupe to is not "another":
    #    its members are exactly what a re-run attaches, so excluding them would
    #    make a second review of the same row answer "nothing to create".
    member_removed = set()
    for entry in after_missing:
        owner = membership.get(entry.asset.id)
        if owner is None or (existing is not None and owner[0] == existing.project.id):
            continue
        asset = asset_rows[entry.asset.id]
        excluded.append(
            {
                "path": asset.storage_path or entry.asset.path,
                "reason": f"already a member of project {owner[1]!r}",
            }
        )
        member_removed.add(entry.asset.id)
    members = [entry for entry in after_missing if entry.asset.id not in member_removed]

    if not members:
        raise HTTPException(
            status_code=409, detail=f"nothing to create from {key!r}: every file under it is excluded"
        )

    cover = bundle_service.cover_for_registered([entry.asset for entry in members], None)

    count = len(members)
    asset_ids = [entry.asset.id for entry in members]
    cover_asset_id = cover.get("asset_id")
    steps = [
        {
            "index": 0,
            "op": "create-project",
            "summary": f"create project {name!r} from {count} member file(s)",
            "args": {
                "key": key,
                "name": name,
                "members": [
                    {"asset_id": entry.asset.id, "path": entry.asset.path} for entry in members
                ],
                "cover_asset_id": cover_asset_id,
            },
        },
        {
            "index": 1,
            "op": "verify-members",
            "summary": f"verify {count} member file(s) landed in the project",
            "args": {"project_step": 0, "asset_ids": asset_ids},
        },
        {
            "index": 2,
            "op": "verify-cover",
            "summary": "verify the project's cover",
            "args": {"project_step": 0, "cover_asset_id": cover_asset_id},
        },
    ]
    review = {
        "workflow": "create-project",
        "target": key,
        "moves": 0,
        "creates": 0 if existing else 1,
        "adopts": count,
        "deletes": 0,
        "members": count,
        "groups": group_list,
        "cover": {
            "asset_id": cover.get("asset_id"),
            "rel_path": cover.get("rel_path"),
            "reason": cover.get("reason"),
        }
        if cover.get("asset_id")
        else None,
        "excluded": excluded,
        "existing_project_id": existing.project.id if existing else None,
        "existing_project_name": existing.project.name if existing else None,
    }
    return steps, review, [key]


# Submit -----------------------------------------------------------------------


def _existing_or_conflict(existing: Operation, plan: Plan, key: str) -> Tuple[Operation, bool]:
    if existing.plan_digest == plan.digest:
        return existing, False
    raise HTTPException(
        status_code=409,
        detail=(
            f"idempotency_key {key!r} was already used for workflow {existing.workflow!r} "
            f"on target {existing.target!r} with plan digest {existing.plan_digest}; "
            f"this plan is workflow {plan.intent['workflow']!r} on target "
            f"{plan.intent['target']!r} with plan digest {plan.digest}"
        ),
    )


def submit(
    session: Session,
    plan_id: str,
    idempotency_key: str,
    *,
    ctx: Optional[audit.AuditContext],
    submitted_by: str,
    submitted_by_id: Optional[str],
) -> Tuple[Operation, bool]:
    """Insert one queued operation and its steps, or answer an existing one.

    FAIL-CLOSED: the audit row is staged in the same transaction as the
    operation row, so an audit insert failure rolls the operation back too.
    """
    key = (idempotency_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="idempotency_key is required")
    plan = recompute_plan(plan_id)

    existing = session.exec(select(Operation).where(Operation.idempotency_key == key)).first()
    if existing is not None:
        return _existing_or_conflict(existing, plan, key)

    operation = Operation(
        workflow=plan.intent["workflow"],
        target=plan.intent["target"],
        plan_json=json.dumps(
            {
                "intent": plan.intent,
                "paths": plan.declared_paths,
                "steps": plan.steps,
                "review": plan.review,
            },
            sort_keys=True,
        ),
        plan_digest=plan.digest,
        idempotency_key=key,
        status=STATUS_QUEUED,
        submitted_by=submitted_by,
        submitted_by_id=submitted_by_id,
        request_id=ctx.request_id if ctx is not None else "-",
        created_at=now(),
    )
    session.add(operation)
    for step in plan.steps:
        session.add(
            OperationStep(
                operation_id=operation.id,
                step_index=step["index"],
                op=step["op"],
                args_json=_canonical(step["args"]).decode(),
                status=STEP_PENDING,
            )
        )
    audit.stage(
        session,
        ctx,
        audit.Event(
            audit.OPERATION_SUBMIT,
            operation.id,
            {
                "workflow": operation.workflow,
                "target": operation.target,
                "steps": len(plan.steps),
                "idempotency_key": key,
            },
        ),
    )
    try:
        session.commit()
    except IntegrityError:
        # A concurrent racer won the unique idempotency index; answer like the
        # "existing" case, or the 409 when the digest differs.
        session.rollback()
        existing = session.exec(select(Operation).where(Operation.idempotency_key == key)).first()
        if existing is None:
            raise
        return _existing_or_conflict(existing, plan, key)
    session.refresh(operation)
    return operation, True


# Reads ------------------------------------------------------------------------


def get_operation(session: Session, operation_id: str) -> Operation:
    operation = session.get(Operation, operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Operation not found")
    return operation


def steps_for(session: Session, operation_id: str) -> List[OperationStep]:
    statement = (
        select(OperationStep)
        .where(OperationStep.operation_id == operation_id)
        .order_by(OperationStep.step_index)
    )
    return list(session.exec(statement))


def _step_payload(step: OperationStep) -> dict:
    return {
        "index": step.step_index,
        "op": step.op,
        "status": step.status,
        "result": json.loads(step.result_json) if step.result_json else None,
        "error": step.error,
        "started_at": step.started_at,
        "finished_at": step.finished_at,
    }


def operation_payload(session: Session, operation: Operation) -> dict:
    """The `OperationOut` shape, steps included; `args_json` is never exposed."""
    return {
        "id": operation.id,
        "workflow": operation.workflow,
        "target": operation.target,
        "status": operation.status,
        "submitted_by": operation.submitted_by,
        "created_at": operation.created_at,
        "started_at": operation.started_at,
        "finished_at": operation.finished_at,
        "error": operation.error,
        "undo_state": operation.undo_state,
        "superseded_by": operation.superseded_by,
        "steps": [_step_payload(step) for step in steps_for(session, operation.id)],
    }


def list_operations(
    session: Session, state: Optional[str] = None, limit: int = 50, offset: int = 0
) -> Tuple[List[Operation], int]:
    if state is not None and state not in STATUSES:
        raise HTTPException(status_code=400, detail="unknown state")
    filters = []
    if state:
        filters.append(Operation.status == state)
    total = session.exec(select(func.count()).select_from(Operation).where(*filters)).one()
    statement = (
        select(Operation)
        .where(*filters)
        .order_by(Operation.created_at.desc(), Operation.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(session.exec(statement)), int(total)


# Cancel -----------------------------------------------------------------------


def _cancel_detail(previous: str, result: str) -> dict:
    """The one detail shape a cancel row carries, for BOTH branches.

    `previous` is the state the caller found the operation in and `result` is
    what it is now, both plain strings (`queued`/`cancelled` for a queued
    operation, `running`/`cancelling` for a running one). One name for the pair
    is what keeps the two branches from drifting apart - the row is read by an
    operator, so the two answers must be spelled the same way.
    """
    return {"previous": previous, "result": result}


def cancel(session: Session, operation_id: str, ctx: Optional[audit.AuditContext]) -> Operation:
    operation = get_operation(session, operation_id)
    if operation.status in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"the operation is already {operation.status}")
    if operation.status == STATUS_CANCELLING:
        raise HTTPException(status_code=409, detail="the operation is already being cancelled")
    if operation.status == STATUS_QUEUED:
        operation.status = STATUS_CANCELLED
        operation.finished_at = now()
        operation.undo_state = UNDO_NONE
        session.exec(delete(OperationLock).where(OperationLock.operation_id == operation.id))
        audit.stage(
            session,
            ctx,
            audit.Event(
                audit.OPERATION_CANCEL, operation.id, _cancel_detail("queued", "cancelled")
            ),
        )
        session.add(operation)
        session.commit()
    elif operation.status == STATUS_RUNNING:
        # The runner finishes the running step and skips the rest.
        operation.status = STATUS_CANCELLING
        audit.stage(
            session,
            ctx,
            audit.Event(
                audit.OPERATION_CANCEL, operation.id, _cancel_detail("running", "cancelling")
            ),
        )
        session.add(operation)
        session.commit()
    return operation


# Undo -------------------------------------------------------------------------


def _project_step(session: Session, operation_id: str) -> Optional[OperationStep]:
    statement = (
        select(OperationStep)
        .where(
            OperationStep.operation_id == operation_id,
            OperationStep.op == "create-project",
        )
        .order_by(OperationStep.step_index)
    )
    return session.exec(statement).first()


def undo(session: Session, operation_id: str, ctx: Optional[audit.AuditContext]) -> dict:
    operation = get_operation(session, operation_id)
    if operation.undo_state == UNDO_SUPERSEDED:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": f"a later operation ({operation.superseded_by}) changed the same paths after this one",
                "superseded_by": operation.superseded_by,
                "undo_state": "superseded",
            },
        )
    if operation.undo_state == UNDO_UNDONE:
        raise HTTPException(status_code=409, detail="the operation has already been undone")
    if operation.status not in TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail="the operation has not finished")
    if operation.undo_state == UNDO_NONE:
        raise HTTPException(
            status_code=409, detail="this operation changed nothing that can be undone"
        )

    step = _project_step(session, operation.id)
    result = json.loads(step.result_json) if step and step.result_json else {}
    if not result.get("created"):
        operation.undo_state = UNDO_NONE
        session.add(operation)
        session.commit()
        raise HTTPException(
            status_code=409,
            detail="nothing to undo: the project already existed before this operation",
        )
    project = session.get(Project, result.get("project_id"))
    if project is None:
        raise HTTPException(
            status_code=409, detail="the project this operation created is already gone"
        )

    project_id = project.id
    project_name = project.name
    # Staged BEFORE the delete on purpose: `delete_project` commits once, so the
    # audit row, the `undo_state` flip and the two deleted tables are one atomic
    # fact - an injected audit failure leaves the project alive. The files are
    # never touched, and `audit_ctx=None` keeps `project.delete` out of this
    # ledger (the undo row is the operation's own record).
    operation.undo_state = UNDO_UNDONE
    audit.stage(
        session,
        ctx,
        audit.Event(
            audit.OPERATION_UNDO,
            operation.id,
            {"project_id": project_id, "project_name": project_name},
        ),
    )
    session.add(operation)
    project_service.delete_project(session, project, audit_ctx=None)
    return {
        "operation_id": operation.id,
        "undone": True,
        "project_id": project_id,
        "project_name": project_name,
    }


# Supersede and prune ----------------------------------------------------------


def declared_paths(operation: Operation) -> List[str]:
    """The library paths this operation declared it would touch, from `plan_json`."""
    try:
        plan = json.loads(operation.plan_json)
    except (TypeError, ValueError):
        return []
    paths = plan.get("paths")
    if not isinstance(paths, list):
        return []
    return [path for path in paths if isinstance(path, str)]


def paths_overlap(a: str, b: str) -> bool:
    """Equal prefixes, or one nested in the other; the empty path overlaps everything."""
    a = (a or "").strip("/")
    b = (b or "").strip("/")
    if not a or not b:
        return True
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def supersede_overlapping(session: Session, operation: Operation) -> List[str]:
    """Mark every other undoable operation over these paths as superseded.

    Called by the runner in the transaction that marks `operation` terminal. It
    commits nowhere; the caller does.
    """
    this_paths = declared_paths(operation)
    statement = select(Operation).where(
        Operation.id != operation.id,
        Operation.status.in_(tuple(TERMINAL_STATUSES)),
        Operation.undo_state == UNDO_AVAILABLE,
    )
    superseded: List[str] = []
    for other in session.exec(statement):
        other_paths = declared_paths(other)
        if any(paths_overlap(a, b) for a in this_paths for b in other_paths):
            other.undo_state = UNDO_SUPERSEDED
            other.superseded_by = operation.id
            session.add(other)
            superseded.append(other.id)
    return superseded


def _ordering_key(operation: Operation) -> str:
    return operation.finished_at or operation.created_at


def _cutoff() -> str:
    """The prune boundary in the same ISO-8601 format the rows are written with."""
    return (datetime.now(timezone.utc) - timedelta(days=PRUNE_MAX_AGE_DAYS)).isoformat(
        timespec="microseconds"
    )


def prune(session: Optional[Session] = None) -> int:
    """Delete old terminal rows, keeping a recent window and the newest 200.

    Keeps `(terminal rows finished within the last 30 days) union (the newest 200
    terminal rows older than 30 days)`. Steps and locks are deleted explicitly,
    never relying on the FK pragma, and a `queued`/`running`/`cancelling` row is
    never touched. Uses the caller's session when given and commits inside only
    when it opened its own.
    """
    owns_session = session is None
    if session is None:
        session = Session(engine)
    try:
        cutoff = _cutoff()
        terminal = list(
            session.exec(select(Operation).where(Operation.status.in_(tuple(TERMINAL_STATUSES))))
        )
        older = [operation for operation in terminal if _ordering_key(operation) < cutoff]
        older.sort(key=_ordering_key, reverse=True)
        doomed = older[PRUNE_KEEP_NEWEST:]
        ids = [operation.id for operation in doomed]
        if ids:
            session.exec(delete(OperationStep).where(OperationStep.operation_id.in_(ids)))
            session.exec(delete(OperationLock).where(OperationLock.operation_id.in_(ids)))
            session.exec(delete(Operation).where(Operation.id.in_(ids)))
            if owns_session:
                session.commit()
        return len(ids)
    finally:
        if owns_session:
            session.close()


def now() -> str:
    """ISO-8601 UTC with a fixed precision, so lexical order is time order.

    Public because the runner stamps the step and operation rows it writes with
    the same clock; a second spelling of "now" in another module would make the
    prune's lexicographic window and the rows it bounds disagree.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
