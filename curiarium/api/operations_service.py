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

This module owns the plan codec, the three workflow planners (`create-project`,
`split-and-create` and `adopt`), submit, the reads, cancel, undo, supersede and
prune. It never executes a workflow: the runner does, and the routes only ever
write a row.

`split-and-create` is a COMPOSITION, not a second implementation of anything: it
plans `move_service.preflight_refusals` over the hoists `proposal_engine` implies
and then one `create-project` per hoisted folder, using the same member rule and
the same create verb `create-project` uses. Its undo folds the move ledger back
and deletes only the projects its own create steps reported creating.

`extract-and-create` is the same kind of composition, and it is what replaced the
deleted one-call `POST /import/archive/project`: `archive_service.plan_extraction`
reads the archive's listing once and names every file the run would write (and
every member it would leave out), `archive_service.extract_out` writes them, the
`move` step files the archive under `<twin>/_source/<archive>.zip`, a `scan` step
registers the tree and refuses a partial result naming every miss, and the
`create-project` step creates the project from the scan's VERIFIED set. The
archive's new home is `_source`, not the review queue, because the review row for
a `.zip` is how the door is offered; filing it beside its twin is the owner's
existing rule for an archive that became a folder.
"""

import base64
import hashlib
import json
import os
import posixpath
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, NamedTuple, Optional, Sequence, Tuple

from fastapi import HTTPException, Request
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, delete, select

import adopt_service
import archive_service
import audit
import bundle_drift
import bundle_service
import library_scan
import move_service
import permissions
import project_create_service
import project_service
import proposal_engine
import seams
from auth import AuthContext
from db import engine
from file_utils import is_model_path
from models import Asset, Folder, Operation, OperationLock, OperationStep, Project, ProjectAsset

PLAN_VERSION = 1
WORKFLOWS = ("create-project", "split-and-create", "adopt", "extract-and-create")

# The two shapes a split can enact, and the one it defaults to.
DEFAULT_SPLIT_SHAPE = "split-top-folders"
# The declared cap, the same number the arrange declared: a plan above it is
# refused by name, before any byte moves.
MAX_SPLIT_MOVES = 400

# The owner's rubric, 2026-11-27 (the Mf-215n amendment): an operation is
# CONTRIBUTE unless it can REMOVE data with this action - destruction is not a
# contributor's job, uploading and organising are. The owner's words: "can they
# REMOVE data with this action".
#
# `WORKFLOWS` names the workflows that actually run. A row in `WORKFLOW_LEVELS`
# below for a workflow that is not here is a DECLARATION FOR THE RECORD, not a
# claim that the workflow runs - but the two lists must not disagree about what
# exists: `split-and-create` and `adopt` have planners, so they are in both.
#
# Fails closed: `level_for_workflow` refuses a workflow with no row rather than
# defaulting it to CONTRIBUTE, because an unclassified workflow is a bug.
#
# `library-scan` is deliberately WRITE although the rubric reads contribute: it
# removes nothing, but it is an existing route whose capacity consequence is an
# open question with the owner, so this slice does not widen it.
WORKFLOW_LEVELS: dict[str, str] = {
    "create-project": permissions.CONTRIBUTE,
    "adopt": permissions.CONTRIBUTE,
    "extract-and-create": permissions.CONTRIBUTE,
    "import-folder": permissions.CONTRIBUTE,
    "import-zip": permissions.CONTRIBUTE,
    "import-link": permissions.CONTRIBUTE,
    "enrich": permissions.CONTRIBUTE,
    "split-and-create": permissions.CONTRIBUTE,
    "relocate": permissions.CONTRIBUTE,
    "library-scan": permissions.WRITE,
}


def level_for_workflow(workflow: str) -> str:
    """The level `workflow` needs; a workflow with no row is refused outright.

    Never defaults to CONTRIBUTE: an unclassified workflow is a bug and must not
    run, so this refuses every role - an admin included - with the WRITE denial
    detail rather than guessing what a stranger's workflow would do. The detail
    names WRITE because that is the strictest level a workflow can require; the
    refusal itself is unconditional.
    """
    if workflow not in WORKFLOW_LEVELS:
        raise HTTPException(status_code=403, detail=permissions.DENY_DETAIL[permissions.WRITE])
    return WORKFLOW_LEVELS[workflow]


def authorize_workflow(request: Request, context: Optional[AuthContext], workflow: str) -> None:
    """One decision point: resolve the workflow's level and ask permissions.authorize."""
    permissions.authorize(request, context, level_for_workflow(workflow))


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


def workflow_for_plan_id(plan_id: str) -> str:
    """The workflow a server-issued plan_id names, before anything is written."""
    # The submit handler decides the level before a row is inserted, without
    # recomputing the plan on disk twice.
    intent, _digest = decode_plan_id(plan_id)
    return intent["workflow"]


# Planning ---------------------------------------------------------------------


def build_plan(session: Session, workflow: str, target: str, params: Optional[dict] = None) -> Plan:
    """Compute the plan for `workflow` over `target`; never writes anything."""
    if workflow not in WORKFLOWS:
        raise HTTPException(status_code=400, detail=f"unknown workflow {workflow!r}")
    # The intent carries the NORMALISED target, so two spellings of one directory
    # are one plan and `Operation.target` reads the way `declared_paths` does.
    normalized = (target or "").strip().strip("/")
    intent = {"v": PLAN_VERSION, "workflow": workflow, "target": normalized, "params": params or {}}
    if workflow == "create-project":
        steps, review, declared = _plan_create_project(session, normalized, params or {})
    elif workflow == "split-and-create":
        steps, review, declared = _plan_split_and_create(session, normalized, params or {})
    elif workflow == "adopt":
        steps, review, declared = _plan_adopt(session, normalized, params or {})
    elif workflow == "extract-and-create":
        steps, review, declared = _plan_extract_and_create(session, normalized, params or {})
    else:  # pragma: no cover - decode/`WORKFLOWS` refuse this before it can be built
        raise HTTPException(status_code=400, detail=f"unknown workflow {workflow!r}")
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


def _excluded_members(
    session: Session, key: str, entries: Sequence, existing
) -> Tuple[list, List[dict]]:
    """THE member rule: which of a path's registered files a create would leave out.

    Extracted from the `create-project` planner so ONE rule serves both planners -
    a split's per-folder review and a whole-folder create must not be able to
    disagree about whether a file is a member. Three exclusions, in this order:

      1. a first-level group that is already its own project is excluded wholesale;
      2. a file the last scan flagged missing is not a member;
      3. a file already a member of ANOTHER project is not a member of a second
         one. The project this plan's own create would dedupe to is not "another"
         - its members are exactly what a re-run attaches.

    Returns `(kept entries, excluded)`; `excluded` carries the current storage
    path (what the operator sees on disk now) and a human reason.
    """
    excluded: List[dict] = []

    # 1. A group that is already its own project is excluded wholesale.
    known = bundle_service.known_project_keys(session)
    groups = {_group_of(entry.rel_path) for entry in entries}
    excluded_groups = {group_name for group_name in groups if group_name and f"{key}/{group_name}" in known}
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
    return [entry for entry in after_missing if entry.asset.id not in member_removed], excluded


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

    # Named and deduped FIRST: the "already a member" exclusion needs to know which
    # project this plan's own create would answer, so it never excludes the members
    # of that project.
    name = (params.get("name") or "").strip() or key.rsplit("/", 1)[-1]
    existing = project_create_service.find_existing_project(session, key=key, name=name)

    members, excluded = _excluded_members(session, key, entries, existing)

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


# --- split-and-create --------------------------------------------------------


def _move_path(path: str, moves: Sequence[dict]) -> str:
    """Where a storage-relative path will live once the move list has run, in order."""
    for move in moves:
        source, target, kind = move["from"], move["to"], move["kind"]
        if kind == "dir":
            if path.startswith(source + "/"):
                path = target + path[len(source):]
        elif path == source:
            path = target
    return path


def _under(path: str, root: str) -> bool:
    """Whether `path` is `root` or sits below it, on a segment boundary."""
    return path == root or path.startswith(root + "/")


def _folder_snapshot(session: Session, key: str) -> List[dict]:
    """The folder rows at or below `key`, as the move step's retirement scope.

    Read BEFORE anything moves, and its ids are the only ids the step is entitled
    to retire: `move()` retires nothing by default, and an unscoped sweep would
    delete a row another request had just created. The whole row travels (id,
    name, tags_json, parent_id, storage_path) because the emptied container row
    IS deleted, and `move_ledger`'s reversal rewrites rows it finds - it cannot
    recreate one that is gone.
    """
    rows: List[dict] = []
    for folder in session.exec(select(Folder)):
        path = folder.storage_path or ""
        if not path or not _under(path, key):
            continue
        rows.append(
            {
                "id": folder.id,
                "name": folder.name,
                "tags_json": folder.tags_json,
                "parent_id": folder.parent_id,
                "storage_path": folder.storage_path,
            }
        )
    rows.sort(key=lambda entry: ((entry["storage_path"] or ""), entry["id"]))
    return rows


def _detection_marker(row: dict, shape: str, root_rel: str) -> dict:
    """The detection marker a hoisted folder's project carries.

    The SAME vocabulary the detected-bundle arrange wrote, and read from the
    container's own row through `bundle_service.row_for_key`: the project this
    creates was made from that folder, so its provenance is the row the operator
    saw, not a second reading of the disk.
    """
    archive = row.get("archive") or {}
    rule = None
    for match in row.get("matches") or []:
        if isinstance(match, dict) and match.get("shape") == shape:
            rule = match.get("rule")
            break
    return {
        "key": root_rel,
        "detector": row.get("detector") or "",
        "confidence": row.get("confidence") or "",
        "shape": shape,
        "rule": rule or row.get("detector") or "",
        "rel_dir": root_rel,
        "archive": archive.get("rel_path"),
        "twin_verified": archive.get("twin_verified") if row.get("archive") else None,
    }


def _hoisted_groups(facts: proposal_engine.Facts, key: str, root: str) -> List[dict]:
    """A hoisted folder's OWN groups, read off the container's single walk.

    The engine measured the container, so the hoisted child's subdirectories are
    already in `facts.groups` as the children of the node whose path is the child.
    They are what the project's marker must carry: the container's own group list
    would name folders that are now siblings of the project, not parts of it.
    """
    child = root[len(key) + 1:] if root.startswith(key + "/") else ""
    if not child:
        return []
    stack = list(facts.groups)
    while stack:
        node = stack.pop()
        if node.get("path") == child:
            return bundle_service.wire_groups(node.get("children") or [])
        stack.extend(node.get("children") or [])
    return []


def _plan_split_and_create(session: Session, target: str, params: dict) -> Tuple[list, dict, List[str]]:
    """The `split-and-create` planner: hoist a container's folders, then create one project each.

    A COMPOSITION, and every plan-time refusal is the primitive's own:
    `proposal_engine` decides which moves the shape implies,
    `move_service.preflight_refusals` answers every question about a pair, and the
    member rule is the one `_excluded_members` shares with `create-project`.
    Nothing here re-implements a question another module owns.

    The step order is `move` FIRST and the creates after - deliberately the
    reverse of the detected-bundle arrange, whose projects-first order existed to
    keep its own marker ledger durable before a byte moved. Here the operation's
    step rows are that record, and the semantics this workflow owes are that a
    refusal at the move step stops before any project exists, which the mover's
    batch pre-flight makes structural rather than conventional.

    The review carries the move list AND the projects, because the operator
    approves both halves in one action: the moves are what the app will do, and
    the projects are the outcome they named.
    """
    key = (target or "").strip().strip("/")
    if not key:
        raise HTTPException(
            status_code=409,
            detail="the target must be a library-relative directory key (not the library root)",
        )
    shape = (params.get("shape") or "").strip() or DEFAULT_SPLIT_SHAPE
    if shape not in proposal_engine.SPLIT_SHAPES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{shape!r} is not a split shape; this workflow enacts "
                + ", ".join(repr(known) for known in proposal_engine.SPLIT_SHAPES)
            ),
        )

    row = bundle_service.row_for_key(key)
    if row is None:
        raise HTTPException(status_code=409, detail=f"unknown bundle: {key}")
    if row.get("blocked_reason") == bundle_service.ARCHIVE_ONLY:
        raise HTTPException(
            status_code=409,
            detail=(
                "this row is an archive: extract it first with POST /import/archive, then split the "
                "folder it produced (the split never copies or unpacks)"
            ),
        )

    root_path = Path(bundle_service.STORAGE)
    facts = proposal_engine.measure(str(root_path / key), key)
    moves = proposal_engine.arrange_moves(facts, shape)
    if not moves:
        raise HTTPException(
            status_code=409,
            detail=(
                f"this shape would change nothing: {key!r} has no top folder holding models to hoist; "
                "a folder that is already one product is created, not split"
            ),
        )
    if len(moves) > MAX_SPLIT_MOVES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{key!r} needs {len(moves)} moves, above the declared cap of {MAX_SPLIT_MOVES}; "
                "nothing moved"
            ),
        )
    if seams.scan_status().get("running"):
        raise HTTPException(
            status_code=409,
            detail=f"a library scan is running; nothing was moved for {key!r}. Try again when it finishes",
        )

    pairs = [move_service.Pair(move["from"], move["to"], move["kind"]) for move in moves]
    refusals = move_service.preflight_refusals(pairs, session, root_path)
    if refusals:
        # The primitive's own sentence and its own status, never a second copy of
        # the rule: a plan that says a different thing from the run is the drift
        # this delegation exists to prevent.
        first = refusals[0]
        raise HTTPException(status_code=first["status"], detail=first["detail"])

    roots: List[str] = []
    for move in moves:
        if move["kind"] != "dir" or move["to"] in roots:
            continue
        roots.append(move["to"])
    if not roots:
        raise HTTPException(
            status_code=409, detail=f"{key!r} has no folder to hoist into a project"
        )

    # The member rule asked ONCE over the registered files, then every path
    # projected through the move list: a member's path on the wire is the path it
    # will HAVE, because the create step executes the set the operator reviewed.
    entries = list(bundle_service.members_of(key, bundle_service.registered_assets(session, key)).entries)
    projected = [(entry, _move_path(entry.asset.path, moves)) for entry in entries]

    creates = sorted({os.path.dirname(move["to"]) for move in moves} - {"", "."})

    projects: List[dict] = []
    excluded: List[dict] = []
    create_steps: List[dict] = []
    assigned: set = set()
    dropped: set = set()
    for position, root in enumerate(roots):
        name = proposal_engine.folder_title(os.path.basename(root))
        existing = project_create_service.find_existing_project(session, key=root, name=name)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"a project already covers {root!r}; the split would hoist a folder onto it, so "
                    "nothing was planned"
                ),
            )
        bucket = [entry for entry, path in projected if _under(path, root)]
        kept, root_excluded = _excluded_members(session, root, bucket, existing)
        excluded.extend(root_excluded)
        if not kept:
            raise HTTPException(
                status_code=409,
                detail=f"nothing to create from {root!r}: every file under it is excluded",
            )
        kept_ids = {entry.asset.id for entry in kept}
        # Two different silences: a file the MEMBER RULE dropped already carries its
        # own reason above, and a file no root would hold is named below. Keeping the
        # two apart is what stops one path being served twice with contradictory
        # reasons.
        dropped.update({entry.asset.id for entry in bucket} - kept_ids)
        members = [
            (entry, path) for entry, path in projected if _under(path, root) and entry.asset.id in kept_ids
        ]
        assigned.update(kept_ids)
        cover = bundle_service.cover_for_registered([entry.asset for entry in kept], None)
        groups = _hoisted_groups(facts, key, root)
        projects.append({"name": name, "root": root, "members": len(members)})
        create_index = 1 + 2 * position
        create_steps.append(
            {
                "index": create_index,
                "op": "create-project",
                "summary": f"create project {name!r} from {len(members)} member file(s)",
                "args": {
                    "key": root,
                    "name": name,
                    "members": [
                        {"asset_id": entry.asset.id, "path": path} for entry, path in members
                    ],
                    "model_ids": [
                        entry.asset.id for entry, _path in members if entry.role == library_scan.MODEL
                    ],
                    "groups": groups,
                    "marker": _detection_marker(row, shape, root),
                    "cover_asset_id": cover.get("asset_id"),
                },
            }
        )
        create_steps.append(
            {
                "index": create_index + 1,
                "op": "verify-members",
                "summary": f"verify {len(members)} member file(s) landed in project {name!r}",
                "args": {
                    "project_step": create_index,
                    "asset_ids": [entry.asset.id for entry, _path in members],
                },
            }
        )

    # Everything the split leaves behind is NAMED, with a reason: a registered file
    # that sits at the container's root is not under any folder the shape hoists, so
    # no project would ever hold it, and the operator must not have to discover that
    # from a member count.
    for entry, path in projected:
        if entry.asset.id in assigned or entry.asset.id in dropped:
            continue
        excluded.append(
            {
                "path": path,
                "reason": "not inside one of the folders the split hoists, so no project would hold it",
            }
        )

    steps = [
        {
            "index": 0,
            "op": "move",
            "summary": f"move {len(moves)} top folder(s) out of {key!r}",
            "args": {
                "key": key,
                "pairs": [[pair.source, pair.target, pair.kind] for pair in pairs],
                "creates": creates,
                "folders": _folder_snapshot(session, key),
            },
        },
        *create_steps,
    ]
    review = {
        "workflow": "split-and-create",
        "target": key,
        "moves": len(moves),
        "creates": len(projects),
        "adopts": 0,
        "deletes": 0,
        "members": sum(project["members"] for project in projects),
        "groups": [],
        "cover": None,
        "excluded": excluded,
        "existing_project_id": None,
        "existing_project_name": None,
        "move_list": [
            {
                "source": move["from"],
                "target": move["to"],
                "kind": move["kind"],
                "action": move["action"],
            }
            for move in moves
        ],
        "projects": projects,
    }
    return steps, review, [key, *roots]


# The sentence an undo answers with, and the reason it is a sentence rather than a
# silent fact: an undo that restored the archive but left 467 MB of extracted
# bytes on disk has not undone the run, and the operator is owed that in words.
EXTRACT_PAYLOAD_RESIDUE = (
    "the extracted files stay: no ledger records those bytes, and un-extracting would "
    "delete registered assets; only the project and the archive's move are undone"
)


# --- extract-and-create -------------------------------------------------------


def _plan_extract_and_create(
    session: Session, target: str, params: dict
) -> Tuple[list, dict, List[str]]:
    """The `extract-and-create` planner: a `.zip` in, extract + file + scan + create out.

    This is the workflow that replaced `POST /import/archive/project`. The order
    below is the contract:

      1. the DEDUPE is asked FIRST, before the zip is opened, so a second click
         answers the zero-step already-created plan even after the archive has been
         filed under `_source` and deleted. Nothing in this plan reads the disk
         before this question: the answer that survives the run's own side effects
         is what keeps a re-plan from a 404;
      2. the archive is resolved - the declared path, else the copy an earlier run
         already filed at `<twin>/_source/<basename>` - so a re-plan after a failed
         scan or create answers the two remaining steps instead of 404ing on a file
         the run itself moved;
      3. the listing is read ONCE, through `archive_service.plan_extraction`, which
         is the extractor's own rule (`entry_policy`) and the extractor's own
         refusals; the plan's members and exclusions are that listing;
      4. the archive's move is pre-flighted by `move_service.preflight_refusals`, so
         a blocked destination refuses before the extraction writes a byte.

    Every count in the review is the listing's: `members` is the number of files the
    extract would write, `groups` is the listing's own subdirectories below its
    common root (with `""` for the files sitting directly in it, the same loose
    bucket `create-project` uses), the cover is the first planned model PATH, and
    `excluded` names every member the extractor would leave out. The cover is a
    path rather than an id because no asset row exists until the scan has run; the
    scan step resolves it.
    """
    archive = (target or "").strip().strip("/")
    if not archive:
        raise HTTPException(
            status_code=409, detail="the target must be a library-relative archive path"
        )
    entries = params.get("entries")
    if entries is not None and (
        not isinstance(entries, list) or any(not isinstance(name, str) for name in entries)
    ):
        # A selection is a list of archive-relative names; a bare string would be
        # iterated character by character and a number would die inside the
        # normaliser, so the shape is checked before the zip is opened.
        raise HTTPException(
            status_code=400, detail="params.entries must be a list of entry paths, or omitted"
        )

    existing = project_create_service.find_existing_project(session, archive=archive)
    twin_rel: Optional[str] = None
    if existing is None:
        # The SECOND identity this door holds, and the one Mf-yhjo built this dedupe
        # for: a folder the detection door already made into a project carries the
        # twin as its marker key, so the `.zip` beside it answers already-created
        # rather than becoming a second product with a second name. The archive leg
        # is asked first because it is the one that survives the archive's own filing
        # and deletion; the twin leg needs only a name, so neither opens the zip.
        try:
            twin_rel = archive_service.twin_rel_for(archive)
        except HTTPException:
            twin_rel = None
        if twin_rel is not None:
            existing = project_create_service.find_existing_project(session, key=twin_rel)
    if existing is not None:
        # A ZERO-STEP plan: the run this click asks for has already happened. This
        # is what a second plan answers, and it is asked before the archive is
        # opened, so it also answers after the archive was filed and deleted.
        return [], _extract_already_created_review(archive, existing), [archive]

    if seams.scan_status().get("running"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"a library scan is running; nothing was planned for {archive!r}. "
                "Try again when it finishes"
            ),
        )

    twin_rel = twin_rel or archive_service.twin_rel_for(archive)
    home = posixpath.join(twin_rel, "_source", posixpath.basename(archive))
    selection, filed = _resolve_extract_source(archive, twin_rel, home, entries)
    planned = list(selection.planned)
    if not planned:
        raise HTTPException(
            status_code=409,
            detail=(
                f"nothing to create from {archive!r}: every selected entry is junk, a directory "
                "member, or a name the library scan would never register"
            ),
        )

    name = (params.get("name") or "").strip() or posixpath.basename(selection.twin)
    moves: List[dict] = []
    if not filed:
        refusal = move_service.preflight_refusals(
            [move_service.Pair(archive, home, "file")], session, Path(bundle_service.STORAGE)
        )
        if refusal:
            # The mover's own sentence and its own status: a plan that said a
            # different thing from the run would be the drift this delegation exists
            # to prevent.
            raise HTTPException(
                status_code=refusal[0]["status"], detail=refusal[0]["detail"]
            )
        moves = [
            {"source": archive, "target": home, "kind": "file", "action": "file-archive"}
        ]

    cover = next((entry for entry in planned if is_model_path(entry.rel_path)), None)
    source_dir = posixpath.join(selection.twin, "_source")
    paths = [{"path": entry.rel_path, "entry": entry.entry} for entry in planned]
    steps: List[dict] = []
    if not filed:
        steps.append(
            {
                "index": 0,
                "op": "extract",
                "summary": (
                    f"extract {len(planned)} file(s) from {posixpath.basename(archive)!r} "
                    f"into {selection.twin!r}"
                ),
                # The PLANNED entries, not the raw selection: the review says what
                # this run writes, so the extract writes exactly that - a hidden or
                # junk member the plan excluded is not written behind the operator's
                # back. And the scan is the next-but-one step's job: an extract that
                # started its own walk would register the tree while the archive was
                # still beside it.
                "args": {
                    "path": archive,
                    "entries": [entry.entry for entry in planned],
                    "twin": selection.twin,
                },
            }
        )
        steps.append(
            {
                "index": 1,
                "op": "move",
                "summary": (
                    f"file {posixpath.basename(archive)!r} under {source_dir!r} "
                    "so the twin owns its archive"
                ),
                "args": {
                    "key": selection.twin,
                    "pairs": [[archive, home, "file"]],
                    "creates": [source_dir],
                    # The rows that already existed at `_source`: undo may delete the
                    # row this run made, and only if it was not one of these.
                    "folders": _folder_snapshot(session, source_dir),
                },
            }
        )

    scan_index = len(steps)
    steps.append(
        {
            "index": scan_index,
            "op": "scan",
            "summary": (
                f"scan the library and resolve {len(planned)} planned file(s) "
                f"under {selection.twin!r}"
            ),
            "args": {"key": selection.twin, "paths": paths},
        }
    )
    steps.append(
        {
            "index": scan_index + 1,
            "op": "create-project",
            "summary": f"create project {name!r} from {len(planned)} member file(s)",
            "args": {
                # The reviewed set is the SCAN's verified set, never a second
                # resolve: a rescan between the review and the run must not attach
                # files nobody approved.
                "resolve_from": scan_index,
                "key": selection.twin,
                "name": name,
                "archive": archive,
                "create_origin": project_create_service.ORIGIN_ARCHIVE_EXTRACT,
                "provenance": {
                    "archive": archive,
                    "twin": selection.twin,
                    "planned": len(planned),
                    "fingerprint": selection.fingerprint,
                    "common_root": selection.common_root,
                    "groups": [
                        {"name": group.name, "count": group.members}
                        for group in selection.groups
                    ],
                },
            },
        }
    )

    review = {
        "workflow": "extract-and-create",
        "target": archive,
        "moves": len(moves),
        "creates": 1,
        "adopts": 0,
        "deletes": 0,
        "members": len(planned),
        "groups": [
            {"name": group.name, "members": group.members, "models": group.models}
            for group in selection.groups
        ],
        # A PATH, not an id: the scan step resolves the cover from the listing it
        # verified. An id here would be a client-visible value nothing has produced.
        "cover": (
            {
                "asset_id": None,
                "rel_path": cover.rel_path,
                "reason": "the first model file in the listing",
            }
            if cover is not None
            else None
        ),
        "excluded": list(selection.excluded),
        "existing_project_id": None,
        "existing_project_name": None,
        "move_list": moves,
        "projects": [{"name": name, "root": selection.twin, "members": len(planned)}],
    }
    return steps, review, [archive, selection.twin]


def _resolve_extract_source(
    archive: str, twin_rel: str, home: str, entries: Optional[list]
) -> Tuple[archive_service.ExtractionPlan, bool]:
    """The archive's listing, from its declared path or the copy a run already filed.

    A 404 on the declared path is the ONLY status that falls through: the archive
    may already be at `<twin>/_source/<basename>` because an earlier run's scan or
    create step failed after the move. Any other refusal (400, 413, 415) is the
    extractor's own and is re-raised untouched - a plan must never paper over a
    broken archive by looking somewhere else for it.
    """
    try:
        return archive_service.plan_extraction(archive, entries), False
    except HTTPException as error:
        if error.status_code != 404:
            raise
        missing = error
    try:
        return archive_service.plan_extraction(home, entries, twin=twin_rel), True
    except HTTPException as fallback:
        raise missing from fallback


def _extract_already_created_review(
    archive: str, existing: project_create_service.ExistingProject
) -> dict:
    """The review of a plan with nothing to do, naming the project that answers it."""
    return {
        "workflow": "extract-and-create",
        "target": archive,
        "moves": 0,
        "creates": 0,
        "adopts": 0,
        "deletes": 0,
        "members": 0,
        "groups": [],
        "cover": None,
        "excluded": [],
        "existing_project_id": existing.project.id,
        "existing_project_name": existing.project.name,
        "move_list": [],
        "projects": [],
    }


# --- adopt -------------------------------------------------------------------


def _plan_adopt(session: Session, target: str, params: dict) -> Tuple[list, dict, List[str]]:
    """The `adopt` planner: add a folder's registered files to the project they belong to.

    The target IS the project id, not a bundle key: a bundle key is not an
    identity, because two projects may name one key, so a plan that addressed a
    folder would not know which project to extend. The project's own marker names
    the folder (`rel_dir`, else the detection door's `key`), so the folder comes
    from the row rather than from the request.

    Every refusal happens BEFORE any row exists, and each is one sentence an
    operator can act on. The disk-minus-registered check is the one that must not
    be softened: adopting past it would leave the unregistered files out and the
    next plan would call the project "already complete".

    `declared_paths` is the walk root, so the prefix lock serialises two adopts of
    one folder and `supersede_overlapping` retires an older one - the paths, not
    the project id, are what the runner must hold.
    """
    if not target:
        raise HTTPException(status_code=409, detail="the target must be a project id")
    project = session.get(Project, target)
    if project is None:
        raise HTTPException(status_code=409, detail=f"unknown project: {target}")
    marker = project_service.bundle_marker_of(project.metadata_json)
    if not marker:
        raise HTTPException(
            status_code=409,
            detail=(
                f"project {project.name!r} has no bundle marker: it was not made from a folder, "
                "so there is nothing to adopt"
            ),
        )

    candidate = bundle_drift.walk_root(marker)
    registered = bundle_service.registered_assets(session, candidate) if candidate else []
    state, root = bundle_drift.classify(
        marker,
        root_present=lambda probe: bundle_drift.has_rows_under(
            [(asset.id, asset.path, asset.size) for asset in registered], probe
        ),
    )
    if state != bundle_drift.DRIFT_KNOWN or root is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"project {project.name!r} has a bundle marker that names no folder this library "
                f"still holds ({marker.get('key')!r}); its drift cannot be measured, so nothing "
                "was planned"
            ),
        )

    if seams.scan_status().get("running"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"a library scan is running; nothing was planned for {project.name!r}. "
                "Try again when it finishes"
            ),
        )

    entries = list(bundle_service.members_of(root, registered).entries)

    missing = adopt_service.unregistered_under(session, root)
    if missing:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{missing} file(s) under {root!r} are on disk but not in the library; "
                "rescan the library, then plan again"
            ),
        )

    # Excluded groups subtract FIRST: `str.startswith(())` is False, so an empty
    # tuple drops nothing and a group the operator excluded is named before the
    # member rule ever sees it.
    prefixes = bundle_drift.excluded_prefixes(root, marker)
    locked = [entry for entry in entries if entry.asset.path.startswith(prefixes)]
    excluded: List[dict] = [
        {"path": entry.asset.path, "reason": "this group is excluded from the project"}
        for entry in locked
    ]
    candidates = [entry for entry in entries if not entry.asset.path.startswith(prefixes)]

    existing = project_create_service.ExistingProject(project, project_create_service.MATCH_MARKER)
    members, excluded_rule = _excluded_members(session, root, candidates, existing)
    excluded.extend(excluded_rule)

    # The plan resolves the DELTA: a membership the project already holds is not
    # something this adopt would ADD, so it must not inflate the count the
    # operator approves - and a project that already holds every file under the
    # folder has nothing to plan at all.
    held = set(
        session.exec(select(ProjectAsset.asset_id).where(ProjectAsset.project_id == project.id))
    )
    members = [entry for entry in members if entry.asset.id not in held]

    if not members:
        raise HTTPException(
            status_code=409, detail=f"{project.name!r} already holds every file under {root!r}"
        )

    # The census is STRUCTURAL, not conventional: one bucket per kept member in the
    # same pass that counts them, and the post-condition below is the proof that
    # `sum(groups) == members` can never be a sentence without a number behind it.
    groups: dict = {}
    by_role = {
        role: 0
        for role in (
            library_scan.MODEL,
            library_scan.IMAGE,
            library_scan.TEXT,
            library_scan.ARCHIVE,
            library_scan.OTHER,
        )
    }
    for entry in members:
        name = _group_of(entry.rel_path)
        group = groups.setdefault(name, {"name": name, "members": 0, "models": 0})
        group["members"] += 1
        if entry.role == library_scan.MODEL:
            group["models"] += 1
        by_role[entry.role] = by_role.get(entry.role, 0) + 1
    group_list = list(groups.values())
    counted = sum(group["members"] for group in group_list)
    if counted != len(members) or sum(by_role.values()) != len(members):
        raise RuntimeError(
            f"adopt review census diverged: {counted} grouped and {sum(by_role.values())} by role, "
            f"against {len(members)} members"
        )

    # The engine's groups, ONE walk, and the engine's own list handed to the run:
    # the adopt step's `facts` shim answers `facts.groups` from it, so the marker is
    # refreshed from the measurement the operator reviewed rather than from a
    # second walk of the disk. The wire shape is derived at run time by
    # `marker_groups`, the one place that shape is built.
    facts = proposal_engine.measure(str(Path(bundle_service.STORAGE) / root), root)
    wire = bundle_service.wire_groups(facts.groups)

    member_ids = [entry.asset.id for entry in members]
    count = len(members)
    steps = [
        {
            "index": 0,
            "op": "adopt",
            "summary": f"add {count} file(s) to project {project.name!r}",
            "args": {"project_id": project.id, "key": root, "member_ids": member_ids, "groups": facts.groups},
        },
        {
            "index": 1,
            "op": "refresh-groups",
            "summary": f"refresh the project's {len(wire)} group(s) from the folder",
            "args": {"project_step": 0},
        },
        {
            "index": 2,
            "op": "verify-members",
            "summary": f"verify {count} member file(s) landed in the project",
            "args": {"project_step": 0, "asset_ids": member_ids},
        },
    ]
    review = {
        "workflow": "adopt",
        "target": project.id,
        "moves": 0,
        "creates": 0,
        "adopts": count,
        "deletes": 0,
        "members": count,
        "groups": group_list,
        "cover": None,
        "excluded": excluded,
        "existing_project_id": project.id,
        "existing_project_name": project.name,
        "move_list": [],
        "projects": [],
        "by_role": by_role,
    }
    return steps, review, [root]


# Submit -----------------------------------------------------------------------


def _existing_or_conflict(existing: Operation, intent: dict, digest: str, key: str) -> Tuple[Operation, bool]:
    if existing.plan_digest == digest:
        return existing, False
    raise HTTPException(
        status_code=409,
        detail=(
            f"idempotency_key {key!r} was already used for workflow {existing.workflow!r} "
            f"on target {existing.target!r} with plan digest {existing.plan_digest}; "
            f"this plan is workflow {intent['workflow']!r} on target "
            f"{intent['target']!r} with plan digest {digest}"
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
    # The key is asked FIRST, before the plan is recomputed, because a retry
    # after a timeout must reach the row it already made. `recompute_plan`
    # refuses a plan whose digest moved, and `create-project`'s review carries
    # `existing_project_id` and `creates`, which move as a consequence of the
    # operation itself - so recomputing first made every replay of a finished
    # operation a dead-end 409, and re-planning then yields a different digest
    # that the same key refuses too. This is safe because a replay that answers
    # here resolves to a row that already exists, so no new work is queued and
    # no second operation is created, while the stale-plan guard still runs for
    # every key that finds no row - which is the only place a stale plan could
    # queue unreviewed work.
    intent, digest = decode_plan_id(plan_id)

    existing = session.exec(select(Operation).where(Operation.idempotency_key == key)).first()
    if existing is not None:
        return _existing_or_conflict(existing, intent, digest, key)

    plan = recompute_plan(plan_id)

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
        return _existing_or_conflict(existing, plan.intent, plan.digest, key)
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
    """Undo one finished operation, workflow by workflow.

    The guards below are the shared ones - a superseded, already-undone, unfinished
    or nothing-changed operation answers the same 409 whichever workflow it was -
    and only the body is workflow-shaped.
    """
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

    if operation.workflow == "create-project":
        return _undo_create_project(session, operation, ctx)
    if operation.workflow == "split-and-create":
        return _undo_split_and_create(session, operation, ctx)
    if operation.workflow == "adopt":
        return _undo_adopt(session, operation, ctx)
    if operation.workflow == "extract-and-create":
        return _undo_extract_and_create(session, operation, ctx)
    raise HTTPException(
        status_code=409, detail=f"undo is not implemented for workflow {operation.workflow!r}"
    )


def _undo_create_project(
    session: Session, operation: Operation, ctx: Optional[audit.AuditContext]
) -> dict:
    """The create-project undo, byte for byte what it did before split existed."""
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


def move_intent(operation_id: str) -> str:
    """The intent the split's move step used; deterministic, so it is findable later."""
    return f"operation:{operation_id}:move"


def _result_of(step: Optional[OperationStep]) -> dict:
    """One step's stored result, or `{}` for a step that has none or a broken one."""
    if step is None or not step.result_json:
        return {}
    try:
        payload = json.loads(step.result_json)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _restore_retired(entries: Sequence) -> int:
    """Recreate the folder rows the move step's retirement deleted.

    Shallowest path first, because `folder.parent_id` is a real foreign key: a row
    whose parent is not there yet cannot be inserted. A row that already exists is
    left alone, which is what makes a resumed undo a no-op here.
    """
    ordered = sorted(
        (entry for entry in entries if isinstance(entry, dict) and entry.get("id")),
        key=lambda entry: (
            (entry.get("storage_path") or "").count("/"),
            entry.get("storage_path") or "",
            entry["id"],
        ),
    )
    restored = 0
    with Session(engine) as session:
        for entry in ordered:
            if session.get(Folder, entry["id"]) is not None:
                continue
            session.add(
                Folder(
                    id=entry["id"],
                    name=entry.get("name") or "",
                    tags_json=entry.get("tags_json") or "[]",
                    parent_id=entry.get("parent_id"),
                    storage_path=entry.get("storage_path"),
                )
            )
            session.flush()
            restored += 1
        session.commit()
    return restored


def _undo_split_and_create(
    session: Session, operation: Operation, ctx: Optional[audit.AuditContext]
) -> dict:
    """Fold the split back: the retired rows, the moves, the projects, the state.

    The order is the contract and it is NOT the create-project one:

      1. the folder rows the move step retired are recreated first. The move fold's
         row rewrite takes each hoisted folder's `parent_id` from the row at its old
         parent path, so restoring afterwards would leave every child pointing at
         the grandparent - rows that are not byte-identical to the pre-plan
         snapshot, which is exactly what this undo owes;
      2. `move_service.undo_ledger` folds the landed pairs back and writes the
         rows, and the `operation.undo` audit row is staged INTO that transaction
         so the record commits with `undone = 1`;
      3. only the projects whose create step reported `created: true` are deleted.
         A create that answered "already created" raced the plan and is KEPT:
         deleting a project this operation did not make would destroy somebody
         else's work, and the audit detail names it as kept;
      4. `undo_state = undone` is stamped LAST.

    It is resumable, not atomic. `AlreadyUndone` from the fold is tolerated - a run
    that folded and died leaves the ledger undone, and the retry finishes the
    deletes and the stamp - and every step above is idempotent. What it is not is
    a single transaction: a crash between the fold and the deletes leaves a project
    whose bytes are back, which the next undo attempt repairs.
    """
    steps = steps_for(session, operation.id)
    move_step = next((step for step in steps if step.op == "move"), None)
    move_result = _result_of(move_step)
    ledger_id = move_result.get("ledger_id") or move_service.ledger_id_for_intent(
        move_intent(operation.id)
    )

    created: List[str] = []
    kept: List[str] = []
    for step in steps:
        if step.op != "create-project":
            continue
        result = _result_of(step)
        project_id = result.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            continue
        (created if result.get("created") else kept).append(project_id)

    retired = move_result.get("retired") or []
    _restore_retired(retired)

    folded = 0
    if ledger_id:
        try:
            outcome = move_service.undo_ledger(
                ledger_id,
                ctx=ctx,
                events=(
                    audit.Event(
                        audit.OPERATION_UNDO,
                        operation.id,
                        {
                            # What the fold does, and what the undo will act on: both
                            # lists are read off the step rows, which are the record.
                            "moves": int(move_result.get("landed") or 0),
                            "projects_removed": created,
                            "projects_kept": kept,
                        },
                    ),
                ),
            )
            folded = len(outcome.pairs)
        except move_service.AlreadyUndone:
            # The undo is resumable, not atomic: this run is finishing a fold that
            # already happened, and the audit row it wrote says so.
            folded = int(move_result.get("landed") or 0)

    removed: List[str] = []
    for project_id in created:
        with Session(engine) as delete_session:
            project = delete_session.get(Project, project_id)
            if project is None:
                # Already gone: a resumed undo, or an operator deleted it. Either
                # way there is nothing to remove and nothing to report.
                continue
            project_service.delete_project(delete_session, project, audit_ctx=None)
        removed.append(project_id)

    with Session(engine) as final:
        current = final.get(Operation, operation.id)
        if current is not None:
            current.undo_state = UNDO_UNDONE
            final.add(current)
            final.commit()

    return {
        "operation_id": operation.id,
        "undone": True,
        "moves": folded,
        "projects_removed": removed,
    }


def _undo_extract_and_create(
    session: Session, operation: Operation, ctx: Optional[audit.AuditContext]
) -> dict:
    """Fold the archive back, delete only this run's project, and say what stays.

    The order is the split's, for the split's reason:

      1. `move_service.undo_ledger` folds the archive back beside its twin - bytes,
         then the asset row's `storage_path`, then `undone = 1` - and the
         `operation.undo` row is staged INTO that transaction, so the record and
         the fold commit together. `AlreadyUndone` is tolerated: a run that folded
         and died leaves a retry to finish the rest;
      2. the `_source` folder row this run created is deleted, but ONLY when its
         path is one of the step's own `creates` and was not in the plan's folder
         snapshot, AND the directory is gone from disk. The second condition is the
         measured one: `undo_ledger` prunes an emptied directory, so a directory
         that still exists holds something this undo did not put there and its row
         is not ours to delete;
      3. only projects whose create step reported `created: true` are deleted. A
         create that answered `already_created` raced the plan and is KEPT;
      4. `undo_state = undone` is stamped LAST, one commit. A two-step re-plan (the
         archive was already filed by an earlier operation) has no ledger of its
         own: the audit row is staged into that final commit instead, so there is
         exactly one `operation.undo` row either way.

    What it does NOT do is un-extract. No ledger records those bytes, and removing
    registered assets is not an undo - so the payload stays, and both the audit row
    and the response say so in words. A two-step re-plan also cannot fold the
    EARLIER operation's move (that operation owns it, and this run is superseded by
    it the moment it finishes), so the archive stays filed; the response's
    `payload_residue` names the files, and the module docstring names the archive.

    Resumable, not atomic, exactly like the split's undo: EVERY step above is
    idempotent, so a crash between the fold and the deletes is repaired by the next
    attempt.
    """
    steps = steps_for(session, operation.id)
    move_step = next((step for step in steps if step.op == "move"), None)
    move_result = _result_of(move_step)
    ledger_id = move_result.get("ledger_id") or move_service.ledger_id_for_intent(
        move_intent(operation.id)
    )

    created: List[str] = []
    kept: List[str] = []
    for step in steps:
        if step.op != "create-project":
            continue
        result = _result_of(step)
        project_id = result.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            continue
        (created if result.get("created") else kept).append(project_id)

    folded = 0
    undo_event = audit.Event(
        audit.OPERATION_UNDO,
        operation.id,
        {
            "moves": int(move_result.get("landed") or 0),
            "projects_removed": created,
            "projects_kept": kept,
            "payload_residue": EXTRACT_PAYLOAD_RESIDUE,
        },
    )
    if ledger_id:
        try:
            outcome = move_service.undo_ledger(ledger_id, ctx=ctx, events=(undo_event,))
            folded = len(outcome.pairs)
        except move_service.AlreadyUndone:
            # The row was written by the fold that already happened; staging it
            # again would be a second record of one act.
            folded = int(move_result.get("landed") or 0)

    folders_removed = _remove_created_directories(move_step, move_result)

    removed: List[str] = []
    for project_id in created:
        with Session(engine) as delete_session:
            project = delete_session.get(Project, project_id)
            if project is None:
                continue
            project_service.delete_project(delete_session, project, audit_ctx=None)
        removed.append(project_id)

    with Session(engine) as final:
        current = final.get(Operation, operation.id)
        if current is not None:
            if not ledger_id:
                # No move of its own to join: the row commits with the stamp.
                audit.stage(final, ctx, undo_event)
            current.undo_state = UNDO_UNDONE
            final.add(current)
            final.commit()

    return {
        "operation_id": operation.id,
        "undone": True,
        "moves": folded,
        "projects_removed": removed,
        "folders_removed": folders_removed,
        "payload_residue": EXTRACT_PAYLOAD_RESIDUE,
    }


def _remove_created_directories(move_step: Optional[OperationStep], move_result: dict) -> List[str]:
    """Delete the folder rows for directories THIS operation's move created.

    Two conditions, both measured rather than assumed: the path must be in the
    step's own `creates` and absent from the plan's folder snapshot (so an
    operator's pre-existing `_source` row is never deleted), and the directory
    must be gone from disk (so a directory that still holds someone else's bytes
    keeps its row - the undo pruned only what it emptied).
    """
    if move_step is None:
        return []
    try:
        args = json.loads(move_step.args_json or "{}")
    except (TypeError, ValueError):
        return []
    pre_existing = {
        entry.get("storage_path")
        for entry in args.get("folders") or []
        if isinstance(entry, dict)
    }
    root = Path(bundle_service.STORAGE)
    removed: List[str] = []
    for relative in move_result.get("creates") or []:
        if not isinstance(relative, str) or relative in pre_existing:
            continue
        if (root / relative).exists():
            continue
        with Session(engine) as session:
            folder = session.exec(
                select(Folder).where(Folder.storage_path == relative)
            ).first()
            if folder is None:
                continue
            session.delete(folder)
            session.commit()
        removed.append(relative)
    return removed


def _undo_adopt(
    session: Session, operation: Operation, ctx: Optional[audit.AuditContext]
) -> dict:
    """Take back exactly the memberships the adopt step recorded, then restore its groups.

    The entry IS the ledger: it is the adopt step's stored result, and
    `undo_entry` takes back only what that entry snapshotted - so a membership an
    operator added after the adopt survives. A group the entry never recorded (no
    `groups_before` key) is never touched, and a recorded `None` removes the
    marker's `groups` key.

    ONE commit at the end, so the memberships, the group snapshot, the audit row
    and the `undo_state` flip are one fact. An adopt that added nothing answers a
    409 WITHOUT touching `undo_state`: it stays `available`, because there is
    nothing to undo and the next attempt should say the same thing.
    """
    statement = (
        select(OperationStep)
        .where(OperationStep.operation_id == operation.id, OperationStep.op == "adopt")
        .order_by(OperationStep.step_index)
    )
    step = session.exec(statement).first()
    result = _result_of(step)
    entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
    members = entry.get("members") or []
    if not members:
        raise HTTPException(
            status_code=409, detail="nothing to undo: this adopt added no memberships"
        )

    removed = adopt_service.undo_entry(session, entry)

    project_id = entry.get("id") or result.get("project_id")
    project = session.get(Project, project_id) if project_id else None
    name = project.name if project else None
    audit.stage(
        session,
        ctx,
        audit.Event(
            audit.OPERATION_UNDO,
            operation.id,
            {
                "project_id": project_id,
                "project_name": name,
                "members_removed": removed,
                "groups_restored": adopt_service.ENTRY_GROUPS_BEFORE in entry,
            },
        ),
    )
    operation.undo_state = UNDO_UNDONE
    session.add(operation)
    session.commit()
    return {
        "operation_id": operation.id,
        "undone": True,
        "project_id": project_id,
        "project_name": name,
        "members_removed": removed,
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
