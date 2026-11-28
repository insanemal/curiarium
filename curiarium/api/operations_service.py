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

This module owns the plan codec, the workflow planners (`create-project`,
`split-and-create`, `adopt`, `extract-and-create`, `extract`, `library-scan`,
`import-link`, `import-zip` and `enrich`), submit, the reads, cancel, undo,
supersede and prune. It never executes a workflow: the runner does, and the
routes only ever write a row.

`import-link` and `import-zip` are the two ingest doors without their doors: the
same primitives (`import_service.import_asset_from_url`,
`zip_service.extract_zip_entries_to_assets`) now run inside a step body, and the
plan is the review the one-call route never had. Planning an import fetches
nothing: `normalize_import_url` resolves DNS, so the planner keeps to string
checks and the paid/gated table, and the fetch happens once, at run time. When a
plan names a project, its single step is `attach-project` instead of
`fetch`/`fetch-zip`: the gated outcome is a property of the fetch and its page
together, so splitting it would reimplement `import_into_project`'s decision.

`extract` is the in-place archive door without its door: the deleted
`POST /import/archive` and `GET /import/archive/entries` are one plan and two
steps (`extract`, `register`), and the review's `excluded` is the listing's junk
verdict. It is not `extract-and-create`: it files no archive, creates no project
and is not idempotent at plan time, because a second extract is a fact about the
disk the operator must see rather than a zero-step answer.

`split-and-create` is a COMPOSITION, not a second implementation of anything: it
plans `move_service.preflight_refusals` over the hoists `proposal_engine` implies
and then one `create-project` per hoisted folder, using the same member rule and
the same create verb `create-project` uses. Its undo folds the move ledger back
and deletes only the projects its own create steps reported creating.

`enrich` is the deleted `POST /bundles/enrich` door without its door: one step
calls `bundle_service.enrich_bundles` unchanged, so the per-key vocabulary, the
ONE cache write per call and the partial-success rule are still the primitive's.
Its target is deliberately empty - the workflow's target is a WORKING SET named
by `params["keys"]`, not one page - and its undo removes only the cache records
that run itself wrote.

`extract-and-create` is the same kind of composition, and it is what replaced the
deleted one-call `POST /import/archive/project`: `archive_service.plan_extraction`
reads the archive's listing once and names every file the run would write (and
every member it would leave out), `archive_service.extract_out` writes them, the
`move` step files the archive under `<twin>/_source/<archive>.zip`, a `register`
step registers EXACTLY the planned paths and refuses a partial result naming
every miss, and the `create-project` step creates the project from the register
step's verified set. The
archive's new home is `_source`, not the review queue, because the review row for
a `.zip` is how the door is offered; filing it beside its twin is the owner's
existing rule for an archive that became a folder.
"""

import base64
import hashlib
import json
import math
import os
import posixpath
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, delete, select

import adopt_service
import archive_service
import audit
import bundle_drift
import bundle_enrich
import bundle_service
import folder_service
import import_policy
import library_scan
import move_service
import permissions
import project_create_service
import project_service
import proposal_engine
import schemas
import seams
import settings_service
import trash_service
import zip_service
from auth import AuthContext
from db import engine
from file_utils import is_model_path
from models import Asset, Folder, Operation, OperationLock, OperationStep, Project, ProjectAsset

PLAN_VERSION = 1
WORKFLOWS = (
    "create-project",
    "split-and-create",
    "adopt",
    "extract-and-create",
    "extract",
    "library-scan",
    "import-link",
    "import-zip",
    "enrich",
)

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
# `library-scan` is CONTRIBUTE: the rubric's answer (Mf-gciy, 2026-11-27) - a
# scan removes nothing. Its capacity consequence - an unbounded whole-root walk -
# is paid for with bounded work in the planner instead of a higher rank:
# `LIBRARY_SCAN_COOLDOWN_SECONDS` bounds the whole-root case and the plan's
# `target` already carries a folder scope for the bounded one.
WORKFLOW_LEVELS: dict[str, str] = {
    "create-project": permissions.CONTRIBUTE,
    "adopt": permissions.CONTRIBUTE,
    "extract-and-create": permissions.CONTRIBUTE,
    "import-folder": permissions.CONTRIBUTE,
    "import-zip": permissions.CONTRIBUTE,
    "import-link": permissions.CONTRIBUTE,
    "extract": permissions.CONTRIBUTE,
    "enrich": permissions.CONTRIBUTE,
    "split-and-create": permissions.CONTRIBUTE,
    "relocate": permissions.CONTRIBUTE,
    "library-scan": permissions.CONTRIBUTE,
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
    # are one plan and `Operation.target` reads the way `declared_paths` does. An
    # import's target is a URL, whose path a directory-shaped strip would falsify:
    # `https://example.com/model/` and `https://example.com/model` are two pages.
    normalized = (target or "").strip()
    if workflow not in ("import-link", "import-zip"):
        normalized = normalized.strip("/")
    if workflow == "extract":
        # An extract names a library path, and a path is not a directory spelling:
        # a leading space is part of the file name, so nothing is trimmed here.
        # `archive_service` normalises the path itself, containment included.
        normalized = target or ""
    if workflow == "enrich":
        # An enrich's target is a WORKING SET, not one page: `params["keys"]`
        # names it, so a page-shaped target is not part of the plan and two
        # spellings of the same request digest alike.
        normalized = ""
    intent = {"v": PLAN_VERSION, "workflow": workflow, "target": normalized, "params": params or {}}
    if workflow == "create-project":
        steps, review, declared = _plan_create_project(session, normalized, params or {})
    elif workflow == "split-and-create":
        steps, review, declared = _plan_split_and_create(session, normalized, params or {})
    elif workflow == "adopt":
        steps, review, declared = _plan_adopt(session, normalized, params or {})
    elif workflow == "extract-and-create":
        steps, review, declared = _plan_extract_and_create(session, normalized, params or {})
    elif workflow == "extract":
        steps, review, declared = _plan_extract(session, normalized, params or {})
    elif workflow == "library-scan":
        steps, review, declared = _plan_library_scan(session, normalized, params or {})
    elif workflow == "import-link":
        steps, review, declared = _plan_import_link(session, normalized, params or {})
    elif workflow == "import-zip":
        steps, review, declared = _plan_import_zip(session, normalized, params or {})
    elif workflow == "enrich":
        steps, review, declared = _plan_enrich(session, normalized, params or {})
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
                "this row is an archive: extract it first with the extract operation "
                "(POST /operations/plan with workflow 'extract'), then split the folder it "
                "produced (the split never copies or unpacks)"
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
    """The `extract-and-create` planner: a `.zip` in, extract + file + register + create out.

    This is the workflow that replaced `POST /import/archive/project`. The order
    below is the contract:

      1. the DEDUPE is asked FIRST, before the zip is opened, so a second click
         answers the zero-step already-created plan even after the archive has been
         filed under `_source` and deleted. Nothing in this plan reads the disk
         before this question: the answer that survives the run's own side effects
         is what keeps a re-plan from a 404;
      2. the archive is resolved - the declared path, else the copy an earlier run
         already filed at `<twin>/_source/<basename>` - so a re-plan after a failed
         register or create answers the two remaining steps instead of 404ing on a
         file the run itself moved;
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
    path rather than an id because no asset row exists until the register step has
    run; register resolves it from the listing's planned order.

    The step order is extract -> move -> register -> create-project, and the
    planned list is the ONE list: it names the extract's entries, the register
    step's paths, the review's `members` and the create's provenance. There is no
    discovery step left in this workflow: a file dropped elsewhere between the plan
    and the run is a rescan's job, never this operation's, and that rescan is the
    `library-scan` workflow over `library_scan.scan_subtree`, so the walk stays one
    primitive behind one route.

    The review also carries the archive's own listing (`entries`), which is the
    deleted `GET /import/archive/entries` restored onto the plan, sizes included,
    so the entry picker has a source; it is the listing the plan actually read,
    never a second and differently-filtered read.
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
    member_paths = [pair["path"] for pair in paths]
    grouped_members = sum(group.members for group in selection.groups)
    if grouped_members != len(member_paths) or len(set(member_paths)) != len(member_paths):
        # A SERVER bug, not an operator refusal: the listing's own groups and its own
        # planned list are the same fact counted twice, and a plan where they differ
        # would show one number in the review and write another set on disk.
        raise RuntimeError(
            f"the archive listing's groups name {grouped_members} file(s) but its planned "
            f"list names {len(member_paths)} "
            f"({len(member_paths) - len(set(member_paths))} duplicate path(s)): the review "
            "and the run would disagree about the members, so no steps were built"
        )
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
                # back. And registration is the next-but-one step's job: an extract
                # that started its own walk would register the tree while the archive
                # was still beside it.
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

    register_index = len(steps)
    steps.append(
        {
            "index": register_index,
            "op": "register",
            "summary": (
                f"register the {len(planned)} planned file(s) the extraction wrote "
                f"under {selection.twin!r}"
            ),
            # The ONE list: these are the listing's planned paths, in listing
            # order, and the register step writes a row for exactly these. The
            # archive's new home rides along because today's scan left a row there
            # and a filed archive without one would be a silent regression - it is
            # NOT a member, since the create reads only `paths`.
            "args": {"key": selection.twin, "paths": paths, "archive_path": home},
        }
    )
    steps.append(
        {
            "index": register_index + 1,
            "op": "create-project",
            "summary": f"create project {name!r} from {len(planned)} member file(s)",
            "args": {
                # The reviewed set is the REGISTER step's verified set, never a
                # second resolve: a file that appeared between the review and the
                # run must not be attached without approval.
                "resolve_from": register_index,
                "key": selection.twin,
                "name": name,
                "archive": archive,
                "create_origin": project_create_service.ORIGIN_ARCHIVE_EXTRACT,
                "provenance": {
                    "archive": archive,
                    "twin": selection.twin,
                    "planned": len(member_paths),
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

    # The archive's own listing, restored onto the review: this is what the
    # deleted `GET /import/archive/entries` served, sizes included. It is read from
    # the archive the plan actually opened - the filed copy under `_source` for a
    # re-plan - from the same central directory, and it writes nothing.
    listing = zip_service.list_zip_entries(archive_service.source_archive(selection.path))
    review = {
        "workflow": "extract-and-create",
        "target": archive,
        "moves": len(moves),
        "creates": 1,
        "adopts": 0,
        "deletes": 0,
        "members": len(member_paths),
        "groups": [
            {"name": group.name, "members": group.members, "models": group.models}
            for group in selection.groups
        ],
        # A PATH, not an id: the register step resolves the cover from the listing's
        # planned order. An id here would be a client-visible value nothing has
        # produced yet.
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
        "entries": listing,
    }
    return steps, review, [archive, selection.twin]


def _resolve_extract_source(
    archive: str, twin_rel: str, home: str, entries: Optional[list]
) -> Tuple[archive_service.ExtractionPlan, bool]:
    """The archive's listing, from its declared path or the copy a run already filed.

    A 404 on the declared path is the ONLY status that falls through: the archive
    may already be at `<twin>/_source/<basename>` because an earlier run's register
    or create step failed after the move. Any other refusal (400, 413, 415) is the
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
        "entries": [],
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


# --- extract ------------------------------------------------------------------


INPLACE_EXTRACT_PAYLOAD_RESIDUE = (
    "the extracted files stay: they are library content now and their asset rows are the "
    "library's, so only the empty folder rows this run created are removed"
)


def _plan_extract(session: Session, target: str, params: dict) -> Tuple[list, dict, List[str]]:
    """The `extract` planner: the deleted in-library archive door as two steps.

    This is the workflow that replaced `POST /import/archive` and
    `GET /import/archive/entries` together: the listing is the review, and the
    write is the run. `archive_service.plan_extraction` reads the archive ONCE
    and is the one source of both the planned list and the junk verdict, so the
    review's `excluded` is exactly the listing the deleted GET served.

    That deleted listing is restored ONTO this review, sizes included: `entries`
    is the archive's whole member list (`zip_service.list_zip_entries`, the same
    central directory and the same `junk` verdict, sorted by path), so the entry
    picker a client restores has a source without a second route. The listing is
    deliberately NOT narrowed by the selection: an operator who planned a narrow
    review must still be able to widen it.

    The extract step writes the CALLER'S SELECTION - `params["entries"]` when it
    is a list, else the whole archive - which is the write set the deleted door
    had, so no member it would have written is lost: a hidden member the review
    excludes is still written to disk. The REGISTER step names only the `planned`
    paths, because the scan's own naming rules are what turn files into rows; a
    written file the scan would never register therefore gets no row.

    There is deliberately NO zero-step plan here, unlike `extract-and-create`: a
    second extract of the same archive is not a run that already happened, it is
    a run whose answer is `already_present`. The operator asked to extract and
    the workflow must report that it looked - a zero-step plan would hide the
    idempotent answer behind an empty step list.

    The scan-running guard is the extract-and-create planner's, sentence for
    sentence: `library_scan.register` rewrites rows a concurrent walk also
    writes, so the register step waits for the walk exactly as that workflow's
    register step does.
    """
    archive = target or ""
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
    if seams.scan_status().get("running"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"a library scan is running; nothing was planned for {archive!r}. "
                "Try again when it finishes"
            ),
        )

    selection = archive_service.plan_extraction(archive, entries)
    planned = list(selection.planned)
    if not planned:
        raise HTTPException(
            status_code=409,
            detail=(
                f"nothing to extract from {archive!r}: every selected entry is junk, a "
                "directory member, or a name the library scan would never register"
            ),
        )

    cover = next((entry for entry in planned if is_model_path(entry.rel_path)), None)
    steps = [
        {
            "index": 0,
            "op": "extract",
            "summary": (
                f"extract {len(planned)} file(s) from {posixpath.basename(archive)!r} "
                f"into {selection.twin!r} ({len(selection.excluded)} left out of the listing)"
            ),
            # `audit_ctx` is the ONE difference from extract-and-create's extract
            # step: this extract IS the operator's own action, so the primitive's
            # `archive.extract` row is written under the runner's context. The
            # composition records itself through its own steps instead.
            #
            # The SELECTION, not the planned list: the deleted door wrote whatever
            # it was handed (`extract_out` re-derives `plan_selection` itself and
            # drops junk), so passing the planned list would lose every member the
            # review excludes on the scan's account - a hidden file would exist on
            # disk after the door and stop existing after this workflow.
            "args": {
                "path": archive,
                "entries": entries if isinstance(entries, list) else None,
                "audit_ctx": True,
                "folders": _folder_snapshot(session, selection.twin),
            },
        },
        {
            "index": 1,
            "op": "register",
            "summary": (
                f"register the {len(planned)} file(s) the extraction wrote "
                f"under {selection.twin!r}"
            ),
            # The archive path rides along so the archive keeps the row a scan
            # would have left at its own path; it is not a member.
            "args": {
                "key": selection.twin,
                "paths": [{"path": entry.rel_path, "entry": entry.entry} for entry in planned],
                "archive_path": archive,
                "folders": _folder_snapshot(session, selection.twin),
            },
        },
    ]
    # The archive's own listing, restored onto the review: this is what the
    # deleted `GET /import/archive/entries` served, sizes included. It is read from
    # the same archive the planner just opened - the filed copy under `_source` for
    # a re-plan - from the same central directory, and it writes nothing.
    listing = zip_service.list_zip_entries(archive_service.source_archive(selection.path))
    review = {
        "workflow": "extract",
        "target": archive,
        "moves": 0,
        "creates": 0,
        "adopts": 0,
        "deletes": 0,
        "members": len(planned),
        "groups": [
            {"name": group.name, "members": group.members, "models": group.models}
            for group in selection.groups
        ],
        # A PATH, not an id: the register step resolves the cover from the
        # planned order, because no asset row exists until it runs.
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
        "move_list": [],
        "projects": [],
        "entries": listing,
    }
    return steps, review, [archive, selection.twin]


# --- library-scan -------------------------------------------------------------

# One whole-library walk per window, charged to the LIBRARY rather than to the
# submitter: a demoted account cannot reset its own allowance by losing its role,
# and N contributors do not get N walks. The budget is charged on SUBMISSION, so
# it bounds the queue as well as the disk. It is whole-root only
# (`target == ""`): a folder walk is bounded by the folder, is serialised by its
# declared path, never runs the library-wide folder backfill, and is the bounded
# alternative the refusal names.
LIBRARY_SCAN_COOLDOWN_SECONDS = 600

# Every status a row can hold while it still counts against the window. A
# cancelled scan charges only when it started: one cancelled before it ran walked
# nothing and costs nothing.
CHARGING_SCAN_STATUSES = frozenset(
    {STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING, STATUS_DONE, STATUS_FAILED}
)


def _charges_the_library_scan_budget(operation: Operation) -> bool:
    """Whether one `library-scan` row spent part of the library's scan allowance."""
    if operation.status in CHARGING_SCAN_STATUSES:
        return True
    return operation.status == STATUS_CANCELLED and operation.started_at is not None


def whole_root_scan_blocker(session: Session) -> Optional[Tuple[Operation, int]]:
    """The newest whole-root scan inside the cooldown, and the seconds remaining.

    `None` when a whole-root scan may be planned now. Rows are walked newest-first
    and the walk stops at the first row that is either outside the window
    (everything older is outside it too) or charges; a row inside the window that
    spent nothing is skipped, because it spent nothing.
    """
    current = datetime.fromisoformat(now())
    statement = (
        select(Operation)
        .where(Operation.workflow == "library-scan")
        .where(Operation.target == "")
        .order_by(Operation.created_at.desc(), Operation.id.desc())
    )
    for operation in session.exec(statement):
        age = (current - datetime.fromisoformat(operation.created_at)).total_seconds()
        if age >= LIBRARY_SCAN_COOLDOWN_SECONDS:
            return None
        if _charges_the_library_scan_budget(operation):
            return operation, math.ceil(LIBRARY_SCAN_COOLDOWN_SECONDS - age)
    return None


def refuse_whole_root_scan_inside_cooldown(session: Session) -> None:
    """Refuse a whole-root scan while the library's last one is inside the window.

    A 429 rather than a 409: in this module a 409 means "the library changed,
    re-review" or "no such folder", a different and actionable fact, while a rate
    limit is its own fact and `Retry-After` is what a client honours. Raised from
    the PLANNER, so both doors - `POST /operations/plan` and the submit-time
    `recompute_plan` - refuse before a row exists.
    """
    blocker = whole_root_scan_blocker(session)
    if blocker is None:
        return
    operation, remaining = blocker
    elapsed = datetime.fromisoformat(now()) - datetime.fromisoformat(operation.created_at)
    age = max(0, int(elapsed.total_seconds()))
    raise HTTPException(
        status_code=429,
        headers={"Retry-After": str(remaining)},
        detail=(
            f"a whole-library scan was submitted by {operation.submitted_by} {age} s ago, and "
            f"one whole-library scan per {LIBRARY_SCAN_COOLDOWN_SECONDS // 60} minutes is the limit: "
            f"the next one is allowed in {remaining} s. A folder scan has no cooldown."
        ),
    )


def _plan_library_scan(session: Session, target: str, params: dict) -> Tuple[list, dict, List[str]]:
    """The `library-scan` planner: one `scan` step over the whole library or a folder.

    The target is the scope: ``''`` is the whole library, anything else is a
    library-relative folder that must exist, refused as a 409 before a step is
    planned. The scope is normalised through ``library_scan.normalise_scope`` so a
    plan and the primitive refuse the same strings.

    `members` is 0 ON PURPOSE: a plan must never walk the tree. The walk IS the
    operation, and a second walk at plan time would double the cost and make the
    plan digest depend on the disk rather than on the request, so the review
    cannot count files. What it can name is the pruning policy, which is fixed.

    `declared_paths` is `[scope]`, so a whole-root scan declares `['']` - which
    ``paths_overlap`` already treats as overlapping everything. That means a
    queued root scan serialises with every operation, and the whole-root case is
    ALSO rate-limited by ``LIBRARY_SCAN_COOLDOWN_SECONDS``: one walk per window,
    charged to the library on submission, because serialisation alone bounds
    concurrency and not frequency.
    """
    # A parameter this planner never reads must not become part of the reviewed
    # digest: the import planners refuse unknown fields for the same reason, and
    # a scan's only input is its scope.
    if params:
        raise HTTPException(
            status_code=400,
            detail=f"a library scan takes no params: {sorted(params)} are not part of this workflow",
        )
    try:
        scope = library_scan.normalise_scope(target or "")
    except library_scan.ScanScopeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if scope and not (Path(library_scan.STORAGE) / scope).is_dir():
        raise HTTPException(
            status_code=409,
            detail=f"no folder {scope!r} in the library: nothing was planned",
        )
    # A scoped plan is never rate-limited: the folder bounds the walk and its
    # declared path serialises it. The bad-scope 409 above keeps its precedence,
    # so a caller is never told "too soon" about a folder that does not exist.
    if not scope:
        refuse_whole_root_scan_inside_cooldown(session)

    steps = [
        {
            "index": 0,
            "op": "scan",
            "summary": (
                f"scan {scope or 'the whole library'}: register the files it finds "
                "and stamp the ones it cannot"
            ),
            "args": {"scope": scope},
        }
    ]
    review = {
        "workflow": "library-scan",
        "target": scope,
        "moves": 0,
        "creates": 0,
        "adopts": 0,
        "deletes": 0,
        "members": 0,
        "groups": [],
        "cover": None,
        "excluded": library_scan.skip_rules(scope),
        "existing_project_id": None,
        "existing_project_name": None,
        "move_list": [],
        "projects": [],
    }
    return steps, review, [scope]


# --- import-link / import-zip -------------------------------------------------

# The fields the deleted doors read off `ImportRequest`. `url` is deliberately
# not one of them: it is the workflow's target and travels as the step's own
# argument, so a plan cannot be asked to fetch one URL and described by another.
IMPORT_REQUEST_FIELDS = (
    "title",
    "notes",
    "tags",
    "filename",
    "makerworld_cookie",
    "thingiverse_cookie",
    "folder_id",
)


def _import_url(target: str) -> str:
    """The page URL a plan is for, checked with string operations only.

    A pure check on the parsed scheme: the planner must never open a socket, and
    asking DNS whether a host exists is exactly the double fetch the review is
    there to prevent (the file would arrive once at plan time and again at run
    time, and the digest would depend on the network rather than on the request).
    """
    url = (target or "").strip()
    if not url:
        raise HTTPException(
            status_code=409, detail="the target must be the page URL to import from"
        )
    if urlparse(url).scheme not in ("http", "https"):
        raise HTTPException(status_code=409, detail="the target must be an http(s) URL")
    return url


def _import_body(url: str, params: dict, *, extra: Sequence[str] = ()) -> schemas.ImportRequest:
    """The typed import request a step will carry, validated before a step exists.

    A plan is the server's, so the only field a client controls is `params`: a key
    outside this list is refused (a client cannot smuggle anything in beside the
    request it names, a step list included), and a value the schema would refuse
    is a 400 here rather than a step that cannot run.
    """
    allowed = set(IMPORT_REQUEST_FIELDS) | set(extra)
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown import parameter(s) {', '.join(unknown)}: an import takes "
                f"{', '.join(IMPORT_REQUEST_FIELDS)}"
            ),
        )
    values = {key: params[key] for key in IMPORT_REQUEST_FIELDS if key in params}
    try:
        return schemas.ImportRequest(url=url, **values)
    except ValidationError as error:
        first = error.errors()[0]
        field = first["loc"][-1] if first["loc"] else "the request"
        raise HTTPException(
            status_code=400, detail=f"the import request is not valid: {field} {first['msg']}"
        ) from None


def _import_destination(session: Session, params: dict) -> Tuple[List[str], str]:
    """Where an import lands: the paths it declares and the review's own label.

    The folder is checked HERE, before a step exists, so a stale plan is refused at
    plan time with the door's own sentence rather than as a failed run, and the
    declared path is the folder's own `storage_path` because that is the prefix a
    lock can honestly serialise. A folder with no `storage_path` yet - one a zip
    import created, whose path is derived from its ancestors - cannot name a
    prefix, so the import declares the empty path and serialises with everything,
    exactly as a whole-root scan does.
    """
    folder_id = params.get("folder_id") or None
    if not folder_id:
        return [""], "the library root"
    try:
        folder_service.validate_parent_folder(session, folder_id)
    except HTTPException as error:
        raise HTTPException(status_code=409, detail=str(error.detail)) from error
    folder = session.get(Folder, folder_id)
    if folder is None:  # pragma: no cover - the check above just read the row
        raise HTTPException(status_code=409, detail="Parent folder not found")
    return [folder.storage_path or ""], f"the folder {folder.name!r}"


def _import_exclusions(url: str, destination: str, *, zip_entries: bool) -> List[dict]:
    """What an import will NOT do, as review prose with a reason per rule.

    The door is gone but its prohibitions are not: a plan cannot know the bytes it
    has not fetched, so the review has to name the refusals the step will apply
    instead of leaving the operator to guess what a blank page means.
    """
    rules = [
        {
            "path": url,
            "reason": (
                "the file is fetched at RUN time and not at plan time, so its "
                "filename and size are unknown here"
            ),
        },
        {
            "path": url,
            "reason": "a paid or account-gated page is refused before any request is made",
        },
        {
            "path": url,
            "reason": (
                "one file is capped by IMPORT_MAX_BYTES (IMPORT_MAX_MB), and every "
                "redirect hop is SSRF-validated"
            ),
        },
        {
            "path": url,
            "reason": "a session cookie is sent only to a credentialed host (makerworld, thingiverse)",
        },
    ]
    if zip_entries:
        rules.extend(
            [
                {
                    "path": url,
                    "reason": "a junk member (OS metadata) is reported in failed[] and never written",
                },
                {
                    "path": url,
                    "reason": (
                        "the entry list is fixed by this plan; the archive's own listing "
                        "is what POST /import/zip/entries answers"
                    ),
                },
                {
                    "path": destination,
                    "reason": (
                        "each member is capped by IMPORT_MAX_BYTES and the whole "
                        "selection by IMPORT_ZIP_MAX_BYTES (IMPORT_ZIP_MAX_MB)"
                    ),
                },
                {
                    "path": destination,
                    "reason": "a member that fails alone does not fail the others",
                },
            ]
        )
    return rules


def _project_target(session: Session, params: dict) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """The project an import attaches to: (id, requested name, existing name).

    An explicit `project_id` is proved to EXIST here, at plan time, so a stale
    target is refused before the plan exists instead of after the fetch has
    downloaded something. The primitive's 404 becomes a 409 because this is the
    plan refusing - not an import discovering a missing row mid-run - and the
    sentence is the primitive's own. A `project_name` is only checked for not
    being blank: a name is a NAME, so a collision and its suffix are decided by
    the run that creates the row, never guessed here.
    """
    project_id = params.get("project_id") or None
    project_name = params.get("project_name")
    if project_name is not None:
        if not isinstance(project_name, str) or not project_name.strip():
            raise HTTPException(status_code=400, detail="params.project_name must not be blank")
    existing_name: Optional[str] = None
    if project_id:
        if not isinstance(project_id, str):
            raise HTTPException(status_code=400, detail="params.project_id must be a project id")
        try:
            project_service.require_project(project_id)
        except HTTPException as error:
            raise HTTPException(status_code=409, detail=str(error.detail)) from error
        project = session.get(Project, project_id)
        existing_name = project.name if project is not None else None
    return project_id, project_name, existing_name


def _import_review(
    workflow: str,
    url: str,
    destination: str,
    *,
    creates: int,
    members: int,
    zip_entries: bool,
    existing_project_id: Optional[str] = None,
    existing_project_name: Optional[str] = None,
) -> dict:
    """The standard review key set, with the import's own prohibitions as `excluded`."""
    return {
        "workflow": workflow,
        "target": url,
        "moves": 0,
        "creates": creates,
        "adopts": 0,
        "deletes": 0,
        "members": members,
        "groups": [],
        "cover": None,
        "excluded": _import_exclusions(url, destination, zip_entries=zip_entries),
        "existing_project_id": existing_project_id,
        "existing_project_name": existing_project_name,
        "move_list": [],
        "projects": [],
    }


def _plan_import_link(session: Session, target: str, params: dict) -> Tuple[list, dict, List[str]]:
    """The `import-link` planner: the deleted link door as ONE step.

    The target IS the page URL. The paid/gated refusal runs FIRST and its 403 is
    the door's own sentence - it is a URL-only check, so it precedes the one call
    that would resolve DNS (`normalize_import_url`, which the runner makes at run
    time and this planner never makes at all).

    When `params` name a project the one step is `attach-project`, not `fetch`,
    and the plan's `[fetch, attach-project]` split is deliberately refused: the
    gated outcome (a 401/403 with a readable page, the page's own gated flag, and
    a `CredentialRefusal` never being gated) is ONE decision `import_into_project`
    makes from the attempt AND its page together. Splitting the fetch from the
    attach would reimplement `attempt.page`, `parse_page_metadata`, `_is_gated`
    and the details-only `{'_files'}` marker, which is exactly the rewrite the
    ticket forbids. One step keeps the primitive whole.

    `creates` is 1 because the single-file import always creates exactly one asset
    row; a zip selection inside a project creates one per selected entry. A name
    collision is the 409 the door answered, not a plan-time count to reconcile.
    """
    url = _import_url(target)
    import_policy.refuse_paid_or_gated(url)
    body = _import_body(url, params, extra=("project_id", "project_name", "entries"))
    declared, destination = _import_destination(session, params)
    request = body.model_dump(exclude={"url"}, exclude_none=True)
    project_id, project_name, existing_name = _project_target(session, params)
    entries = params.get("entries")
    if entries is not None and (
        not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries)
    ):
        raise HTTPException(status_code=400, detail="entries must be a list of zip entry paths")
    if entries is not None and not (project_id or project_name):
        # A link selection exists only to choose which members a PROJECT import
        # extracts; a plain link import has no member list, so accepting one and
        # ignoring it would be a plan that does not say what the run will do.
        raise HTTPException(
            status_code=400, detail="entries is only valid when the import names a project"
        )
    host = urlparse(url).hostname or url

    if project_id or project_name:
        label = project_name or project_id
        if entries is None:
            summary = f"fetch {host} and attach it to project {label}"
        else:
            summary = f"fetch {host} and attach {len(entries)} file(s) to project {label}"
        steps = [
            {
                "index": 0,
                "op": "attach-project",
                "summary": summary,
                "args": {
                    "url": url,
                    "request": request,
                    "entries": list(entries) if entries is not None else None,
                    "project_id": project_id,
                    "project_name": project_name,
                },
            }
        ]
        creates = 1 if entries is None else len(entries)
        review = _import_review(
            "import-link",
            url,
            destination,
            creates=creates,
            members=len(entries) if entries is not None else 0,
            zip_entries=entries is not None,
            existing_project_id=project_id,
            existing_project_name=existing_name,
        )
        return steps, review, declared

    steps = [
        {
            "index": 0,
            "op": "fetch",
            "summary": (
                f"fetch {urlparse(url).hostname or url} and import it into {destination}"
            ),
            "args": {"url": url, "request": request, "created_via": "link-import"},
        }
    ]
    review = _import_review(
        "import-link", url, destination, creates=1, members=0, zip_entries=False
    )
    return steps, review, declared


def _plan_import_zip(session: Session, target: str, params: dict) -> Tuple[list, dict, List[str]]:
    """The `import-zip` planner: the deleted zip door as ONE step.

    The entry list is required and validated at PLAN time: it is the selection the
    operator confirmed in the entries modal, so an archive's own listing can never
    silently become the run's selection, and a `str` or a list holding a non-string
    is a 400 rather than a step that fails on a member it cannot name.

    ONE step and not the plan's `[fetch, extract-entries]` pair, on purpose: the
    door downloads to a temp file and deletes it in a `finally`. Splitting the
    download from the extraction would hand that temp path across a step boundary
    that no step owns, so a cancelled or failed operation would leak it - one body
    keeps the door's guarantee that the temp file is always deleted.

    When `params` name a project the one step is `attach-project`, for the reason
    `_plan_import_link` records: the gated outcome and its page are one decision
    inside `import_into_project`, so the primitive stays whole.
    """
    url = _import_url(target)
    import_policy.refuse_paid_or_gated(url)
    body = _import_body(url, params, extra=("entries", "project_id", "project_name"))
    entries = params.get("entries")
    if not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries):
        raise HTTPException(status_code=400, detail="entries must be a list of zip entry paths")
    declared, destination = _import_destination(session, params)
    request = body.model_dump(exclude={"url"}, exclude_none=True)
    project_id, project_name, existing_name = _project_target(session, params)
    host = urlparse(url).hostname or url

    if project_id or project_name:
        label = project_name or project_id
        steps = [
            {
                "index": 0,
                "op": "attach-project",
                "summary": f"fetch {host} and attach {len(entries)} file(s) to project {label}",
                "args": {
                    "url": url,
                    "request": request,
                    "entries": list(entries),
                    "project_id": project_id,
                    "project_name": project_name,
                },
            }
        ]
        review = _import_review(
            "import-zip",
            url,
            destination,
            creates=len(entries),
            members=len(entries),
            zip_entries=True,
            existing_project_id=project_id,
            existing_project_name=existing_name,
        )
        return steps, review, declared

    noun = "entry" if len(entries) == 1 else "entries"
    steps = [
        {
            "index": 0,
            "op": "fetch-zip",
            "summary": (
                f"fetch {urlparse(url).hostname or url} and import {len(entries)} "
                f"selected zip {noun} into {destination}"
            ),
            "args": {
                "url": url,
                "request": request,
                "entries": list(entries),
                "created_via": "zip-import",
            },
        }
    ]
    review = _import_review(
        "import-zip", url, destination, creates=len(entries), members=len(entries), zip_entries=True
    )
    return steps, review, declared


# --- enrich -------------------------------------------------------------------

# The fields the deleted enrich door read off `BundleEnrichRequest`. `keys` is
# the workflow's working set and travels in `params`; `refresh` and the cookie
# ride beside it into the one step's args.
ENRICH_PARAM_FIELDS = ("keys", "refresh", "thingiverse_cookie")


def _enrich_keys(params: dict) -> List[str]:
    """The bundle keys a plan names, checked before a step exists.

    A key is a storage-root-relative bundle path, so the list is refused unless
    it is a non-empty list of non-blank strings: a bare string would be iterated
    character by character, a blank key would declare the whole library with
    nothing to read, and an empty list is the zeroness the deleted door answered
    with an empty result - a plan with nothing to do is a 400, never a no-op.

    The cap is `bundle_service.ENRICH_MAX_KEYS` and its refusal is the
    primitive's own sentence, so the plan and a direct call refuse alike.
    """
    unknown = sorted(set(params) - set(ENRICH_PARAM_FIELDS))
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown enrich parameter(s) {', '.join(unknown)}: an enrich takes "
                f"{', '.join(ENRICH_PARAM_FIELDS)}"
            ),
        )
    keys = params.get("keys")
    if (
        not isinstance(keys, list)
        or not keys
        or any(not isinstance(key, str) or not key.strip() for key in keys)
    ):
        raise HTTPException(status_code=400, detail="keys must be a list of bundle keys")
    if len(keys) > bundle_service.ENRICH_MAX_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"enrich at most {bundle_service.ENRICH_MAX_KEYS} bundles at a time",
        )
    if not isinstance(params.get("refresh", False), bool):
        raise HTTPException(status_code=400, detail="params.refresh must be true or false")
    cookie = params.get("thingiverse_cookie")
    if cookie is not None and not isinstance(cookie, str):
        raise HTTPException(
            status_code=400, detail="params.thingiverse_cookie must be a Cookie header string"
        )
    return [key.strip() for key in keys]


def _enrich_exclusions() -> List[dict]:
    """What an enrich will and will not do, as review prose with a reason per rule.

    The door is gone but its contract is not: the plan cannot read a page without
    fetching it, so the review has to name the rules the step will apply instead
    of leaving the operator to discover them in the results.
    """
    return [
        {
            "path": "",
            "reason": (
                "only an EMPTY provenance field is filled: a truthy creator, licence or "
                "licence URL the row already holds is never overwritten"
            ),
        },
        {
            "path": "",
            "reason": (
                "the licence and its URL are ONE pair, taken together or not at all, so a "
                "row with a licence token and no link keeps both"
            ),
        },
        {
            "path": "",
            "reason": (
                "the licence only ever comes from the page: there is no default and no "
                "guess, and a page that states none leaves the licence null"
            ),
        },
        {
            "path": "",
            "reason": (
                "a refusal (401/403/404 or a page with no metadata) is a per-bundle RESULT "
                "with the vendor's remedy sentence, never a failed step, so one rate-limited "
                "page does not fail the others"
            ),
        },
        {
            "path": "",
            "reason": (
                "a fresh cache record answers without a request, and the report reads the "
                "same cache and never fetches"
            ),
        },
        {
            "path": "",
            "reason": "`refresh` re-reads a page the cache already answered, and caches the newer record",
        },
        {
            "path": "",
            "reason": (
                f"at most {bundle_service.ENRICH_MAX_KEYS} bundles per operation, refused at "
                "plan time above that"
            ),
        },
    ]


def _plan_enrich(session: Session, target: str, params: dict) -> Tuple[list, dict, List[str]]:
    """The `enrich` planner: the deleted enrichment door as ONE step.

    The primitive (`bundle_service.enrich_bundles`) is unchanged and is the only
    thing that fetches. Planning is therefore NETWORK-FREE for the import
    planner's reason: a plan may not fetch, because it would read every page
    twice - once to grade the review, once to write the cache - and its digest
    would depend on the network rather than on the request.

    The whole working set is ONE operation, not one step per key, because the
    primitive writes the cache ONCE per call: a step per bundle would be a
    settings write per bundle and a lost entry under concurrency.

    The target is deliberately `""`: the workflow's target is a WORKING SET, not
    one page, and `params["keys"]` names it. The keys are also the
    `declared_paths`, so the prefix lock serialises an enrich with any operation
    touching the same folder, and an empty selection is refused rather than
    planned as nothing to do.
    """
    keys = _enrich_keys(params)
    refresh = bool(params.get("refresh", False))
    cookie = params.get("thingiverse_cookie")
    summary = f"read the source page(s) for {len(keys)} bundle(s)"
    if refresh:
        summary += " again, ignoring the cache"
    steps = [
        {
            "index": 0,
            "op": "enrich",
            "summary": summary,
            # The cookie goes INSIDE `request` so `operations_runner._scrub_cookies`
            # redacts it in the persisted step args and plan, exactly as it does
            # for an import; the body reads it from the in-memory args first.
            "args": {
                "keys": keys,
                "refresh": refresh,
                "request": ({"thingiverse_cookie": cookie} if cookie is not None else {}),
            },
        }
    ]
    review = {
        "workflow": "enrich",
        "target": "",
        "moves": 0,
        "creates": 0,
        "adopts": 0,
        "deletes": 0,
        "members": 0,
        "groups": [],
        "cover": None,
        "excluded": _enrich_exclusions(),
        "existing_project_id": None,
        "existing_project_name": None,
        "move_list": [],
        "projects": [],
    }
    return steps, review, keys


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
    session: Session,
    state: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    workflow: Optional[str] = None,
    target: Optional[str] = None,
    submitted_by: Optional[str] = None,
) -> Tuple[List[Operation], int]:
    """One newest-first page of the queue, narrowed by the filters the UI asks with.

    `state` and `workflow` are vocabulary and are therefore validated: an unknown
    value is a client bug, refused with the list it should have used.

    `target` and `submitted_by` are exact-match and deliberately NOT validated.
    They exist because two questions are properties of the whole ledger, not of a
    page: "is there an operation for THIS target I can resume" must be one exact
    question rather than a client-side scan of a truncated page, and "only mine"
    must be a server filter rather than a lie about a page. An unknown target or
    submitter answers `[]`, the honest answer, and neither can become an existence
    oracle: this route is READ and `GET /operations/{operation_id}` already
    answers the same question to the same caller.
    """
    if state is not None and state not in STATUSES:
        raise HTTPException(status_code=400, detail="unknown state")
    if workflow is not None and workflow not in WORKFLOWS:
        raise HTTPException(status_code=400, detail="unknown workflow")
    filters = []
    if state:
        filters.append(Operation.status == state)
    if workflow:
        filters.append(Operation.workflow == workflow)
    # `is not None`, never truthiness: `""` is the library root, a legal target.
    if target is not None:
        filters.append(Operation.target == target)
    if submitted_by is not None:
        filters.append(Operation.submitted_by == submitted_by)
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


def _step_of(session: Session, operation_id: str, op: str) -> Optional[OperationStep]:
    """The first step of `op`, or `None`; the steps table is the record, not the plan."""
    statement = (
        select(OperationStep)
        .where(OperationStep.operation_id == operation_id, OperationStep.op == op)
        .order_by(OperationStep.step_index)
    )
    return session.exec(statement).first()


def _project_step(session: Session, operation_id: str) -> Optional[OperationStep]:
    return _step_of(session, operation_id, "create-project")


def undo(session: Session, operation_id: str, ctx: Optional[audit.AuditContext]) -> dict:
    """Undo one finished operation, workflow by workflow.

    The guards below are the shared ones - a superseded, already-undone, unfinished
    or nothing-changed operation answers the same 409 whichever workflow it was -
    and only the body is workflow-shaped.

    `library-scan` is `UNDO_NONE`, so the shared nothing-changed guard fires before
    any workflow body runs: a scan writes no bytes and its rows are the library's
    own files, so undoing it would mean deleting rows - a delete workflow, which is
    WRITE and not this ticket. The deleted door had no undo either.

    `import-link` and `import-zip` undo the one asset row (and bytes) their fetch
    step created, and the zip leg also deletes the folder rows the mirrored tree
    created - never a folder that predated the run, and never a file it did not
    write. What neither leg does is put the remote file back: there is nothing on
    the library side to restore, so the undo removes what the run added. A plan
    whose step attached the import to a project delegates to the attach leg
    first, which removes the memberships and only the project the run created.

    `extract` removes only the empty folder rows its register step measured
    creating, and never the bytes: the extracted files are library content now,
    and its response says so.

    `enrich` drops only the cache records its own run fetched, and only while
    they are still its own; it edits no bundle row and no project, because this
    step never wrote one.
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
    if operation.workflow == "extract":
        return _undo_extract(session, operation, ctx)
    if operation.workflow == "import-link":
        return _undo_import_link(session, operation, ctx)
    if operation.workflow == "import-zip":
        return _undo_import_zip(session, operation, ctx)
    if operation.workflow == "enrich":
        return _undo_enrich(session, operation, ctx)
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
        # The PATHS this run's move created and the undo pruned, deliberately not
        # called `folders_removed`: that name on the wire is an import undo's COUNT
        # of mirrored folder rows, and one name cannot be a list in one leg and a
        # number in another. This leg's list is not part of `OperationUndoOut`; the
        # route serves the pair above and the residue sentence below.
        "directories_removed": folders_removed,
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


def _import_undo_event(operation: Operation, detail: dict) -> audit.Event:
    """The one `operation.undo` row an import undo writes, with its own detail."""
    return audit.Event(audit.OPERATION_UNDO, operation.id, detail)


def _undo_payload(operation: Operation, **own) -> dict:
    """The whole `OperationUndoOut` shape, with one import leg's own keys filled in.

    Every leg answers the same key set so the wire body never depends on which
    workflow ran: the keys this leg has nothing to say about are explicitly null
    (never a fabricated zero), and the route hands the dict straight to the
    response model. A leg that removed what the run added and told the client only
    `undone: True` is the false state this shape exists to prevent - the operator's
    next question is always "what went", and the answer is here.
    """
    payload = {
        "operation_id": operation.id,
        "undone": True,
        "project_id": None,
        "project_name": None,
        "moves": None,
        "projects_removed": None,
        "members_removed": None,
        "asset_id": None,
        "filename": None,
        "assets_removed": None,
        "folders_removed": None,
        "entries_failed": None,
        "project_removed": None,
        "payload_residue": None,
        "records_dropped": None,
        "records_kept": None,
    }
    payload.update(own)
    return payload


def _undo_extract(
    session: Session, operation: Operation, ctx: Optional[audit.AuditContext]
) -> dict:
    """Remove only the empty folder rows the `register` step measured creating.

    The register step records `created_folders` as the ids after minus the ids
    before its own call, so a folder that already existed is not in the list and
    is never a candidate. The ids are then tried deepest first and only while
    empty: a folder that holds a child or an asset (the extraction's own files, or
    anything an operator put there later) keeps its row. This is the same pass the
    zip import's undo uses, because it answers the same question.

    It NEVER un-extracts. The extracted bytes are library content now, their asset
    rows are the library's, and removing them would be a delete workflow rather
    than an undo - so the response says the payload stays, in words. A resumed
    undo finds the folders already gone and still writes its one row.
    """
    result = _result_of(_step_of(session, operation.id, "register"))
    created_folders = [
        folder_id for folder_id in result.get("created_folders") or [] if isinstance(folder_id, str)
    ]
    folders_removed = _remove_created_import_folders(created_folders)

    event = _import_undo_event(
        operation,
        {
            "folders_removed": folders_removed,
            "payload_residue": INPLACE_EXTRACT_PAYLOAD_RESIDUE,
        },
    )
    with Session(engine) as final:
        current = final.get(Operation, operation.id)
        if current is not None:
            audit.stage(final, ctx, event)
            current.undo_state = UNDO_UNDONE
            final.add(current)
            final.commit()

    return _undo_payload(
        operation,
        folders_removed=len(folders_removed),
        payload_residue=INPLACE_EXTRACT_PAYLOAD_RESIDUE,
    )


def _undo_attach_project(
    session: Session, operation: Operation, ctx: Optional[audit.AuditContext]
) -> dict:
    """Take back exactly the memberships the `attach-project` step added.

    `added` in the step's result IS the list of memberships the run created, so a
    membership an operator added afterwards is not in it and survives. The project
    row is deleted only when that same result says the step CREATED it: an import
    into a project the operator already had must leave that project alone, and the
    `created` flag is the step's own answer, re-read rather than recomputed.

    The shared supersede guard has already run before this body: a project a later
    operation adopted makes this operation `superseded`, so the delete is as safe
    as the row it names.

    One `operation.undo` row, staged into the same commit as the delete; a resumed
    undo finds the memberships and the project already gone and still records the
    act.
    """
    result = _result_of(_step_of(session, operation.id, "attach-project"))
    project_id = result.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise HTTPException(
            status_code=409, detail="nothing to undo: this import attached no project"
        )
    created = bool(result.get("created"))
    added = [asset_id for asset_id in result.get("added") or [] if isinstance(asset_id, str)]

    removed = 0
    with Session(engine) as member_session:
        present = [
            member.asset_id
            for member in member_session.exec(
                select(ProjectAsset).where(
                    ProjectAsset.project_id == project_id,
                    ProjectAsset.asset_id.in_(added),
                )
            )
        ] if added else []
        if present:
            member_session.exec(
                delete(ProjectAsset).where(
                    ProjectAsset.project_id == project_id,
                    ProjectAsset.asset_id.in_(present),
                )
            )
            member_session.commit()
        removed = len(present)

    project_removed = False
    project_name: Optional[str] = None
    with Session(engine) as final:
        project = final.get(Project, project_id)
        if project is not None:
            project_name = project.name
        event = _import_undo_event(
            operation,
            {
                "project_id": project_id,
                "project_name": project_name,
                "members_removed": removed,
                "project_removed": created and project is not None,
            },
        )
        audit.stage(final, ctx, event)
        current = final.get(Operation, operation.id)
        if current is not None:
            current.undo_state = UNDO_UNDONE
            final.add(current)
        if created and project is not None:
            # `delete_project` commits once, so the membership delete, the audit
            # row, the `undo_state` flip and the project's two tables are one fact.
            project_service.delete_project(final, project, audit_ctx=None)
            project_removed = True
        else:
            final.commit()

    return _undo_payload(
        operation,
        project_id=project_id,
        project_name=project_name,
        members_removed=removed,
        project_removed=project_removed,
    )


def _undo_import_link(
    session: Session, operation: Operation, ctx: Optional[audit.AuditContext]
) -> dict:
    """Delete the one asset row the `fetch` step created, and its bytes with it.

    `trash_service.trash_asset` is the ONE primitive that removes an asset row and
    moves its bytes to the trash (`bulk_service.delete_assets` is that primitive
    per item; `routers/uploads.delete_asset_row_only` deliberately leaves foreign
    bytes alone and is the wrong one here, because the bytes at that managed path
    are ours). The asset is re-read in this session first, so a resumed undo of a
    row that is already gone is a no-op rather than a 404.

    The `operation.undo` row is staged into the SAME session as the delete, so the
    record and the removal commit together, and `audit_ctx=None` keeps
    `asset.delete` out of this ledger - the undo row is the operation's own record.

    What it does NOT do is restore anything from the remote: nothing came from the
    library, so nothing is put back. A path a later operation has taken is already
    refused by the shared superseded guard, which runs before this body.
    """
    if _step_of(session, operation.id, "attach-project") is not None:
        return _undo_attach_project(session, operation, ctx)
    result = _result_of(_step_of(session, operation.id, "fetch"))
    asset_id = result.get("asset_id")
    filename = result.get("filename")
    if not result.get("created") or not asset_id:
        raise HTTPException(
            status_code=409, detail="nothing to undo: this import created no asset"
        )

    event = _import_undo_event(operation, {"asset_id": asset_id, "filename": filename})
    asset = session.get(Asset, asset_id)
    removed = 0
    if asset is not None:
        audit.stage(session, ctx, event)
        trash_service.trash_asset(session, asset, audit_ctx=None)
        removed = 1

    with Session(engine) as final:
        current = final.get(Operation, operation.id)
        if current is not None:
            if asset is None:
                # The row is already gone (a resumed undo, or an operator deleted
                # it): the row still commits, so this act has its own record.
                audit.stage(final, ctx, event)
            current.undo_state = UNDO_UNDONE
            final.add(current)
            final.commit()

    # `assets_removed` is what this act actually removed, not what the run wrote: a
    # resumed undo of an already-deleted row answers 0 and the row it names.
    return _undo_payload(
        operation, asset_id=asset_id, filename=filename, assets_removed=removed
    )


def _folder_depth(session: Session, folder: Folder) -> int:
    """How deep a folder row sits, from its parent chain; the delete order key."""
    depth = 0
    seen = set()
    parent_id = folder.parent_id
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        depth += 1
        parent = session.get(Folder, parent_id)
        parent_id = parent.parent_id if parent is not None else None
    return depth


def _folder_is_empty(session: Session, folder_id: str) -> bool:
    """Whether no folder and no asset row still points at `folder_id`."""
    child = session.exec(select(Folder.id).where(Folder.parent_id == folder_id)).first()
    if child is not None:
        return False
    member = session.exec(select(Asset.id).where(Asset.folder_id == folder_id)).first()
    return member is None


def _remove_created_import_folders(folder_ids: Sequence[str]) -> List[str]:
    """Delete the folder rows a zip run created, deepest first, only when empty.

    Deepest first because `Folder.parent_id` is a real foreign key and a parent
    with a child row cannot be deleted; empty-only because a row that still holds
    a child or an asset is not this undo's to remove - it holds something the run
    did not put there. Every id is attempted and a missing row is skipped, so a
    resumed undo is a no-op.

    The answer is which of `folder_ids` are GONE once this returns, read back off
    the table rather than the list this loop deleted: trashing the last asset under
    a mirrored folder retires that folder row itself
    (`trash_service.trash_asset`), so the pass finds it already gone and a list of
    its own deletes would tell the operator one row left the sidebar when two did.
    """
    with Session(engine) as session:
        ordered = []
        for folder_id in folder_ids:
            folder = session.get(Folder, folder_id)
            if folder is not None:
                ordered.append((_folder_depth(session, folder), folder_id))
    for _depth, folder_id in sorted(ordered, reverse=True):
        with Session(engine) as session:
            folder = session.get(Folder, folder_id)
            if folder is None or not _folder_is_empty(session, folder_id):
                continue
            session.delete(folder)
            session.commit()
    with Session(engine) as session:
        surviving = set(session.exec(select(Folder.id).where(Folder.id.in_(list(folder_ids)))))
    return [folder_id for folder_id in folder_ids if folder_id not in surviving]


def _undo_import_zip(
    session: Session, operation: Operation, ctx: Optional[audit.AuditContext]
) -> dict:
    """Remove exactly what the `fetch-zip` step reported creating, and nothing else.

    The step's result is the ledger: `created` is the asset ids it wrote and
    `created_folders` is the folder rows the mirrored tree made, measured around
    the extract so a folder that already existed is not in it. In this order:

      1. every asset id in `created` that still exists is removed with its bytes,
         through `trash_service.trash_asset` (the one row+bytes deleter);
      2. every folder in `created_folders` is deleted, deepest first, and only
         when nothing points at it any more;
      3. the ONE `operation.undo` row is staged into the first commit that had
         something to write, or into the final stamp when nothing did.

    `failed` echoes the entry names the run itself could not write, so the
    operator's retry list survives the undo.

    What it does NOT do is un-import anything from the remote, and it does not
    touch an asset or a folder this operation did not create - an operator's
    folder at the same name keeps its row. Resumable, not atomic: every step is
    idempotent, so a crash between the deletes is repaired by the next attempt.
    """
    if _step_of(session, operation.id, "attach-project") is not None:
        return _undo_attach_project(session, operation, ctx)
    result = _result_of(_step_of(session, operation.id, "fetch-zip"))
    created = [asset_id for asset_id in result.get("created") or [] if isinstance(asset_id, str)]
    created_folders = [
        folder_id for folder_id in result.get("created_folders") or [] if isinstance(folder_id, str)
    ]
    failed = [name for name in result.get("failed") or [] if isinstance(name, str)]
    if not created and not created_folders:
        raise HTTPException(
            status_code=409, detail="nothing to undo: this import created nothing"
        )

    event = _import_undo_event(
        operation, {"assets_removed": created, "folders_removed": created_folders, "failed": failed}
    )
    removed: List[str] = []
    staged = False
    for asset_id in created:
        with Session(engine) as delete_session:
            asset = delete_session.get(Asset, asset_id)
            if asset is None:
                continue
            if not staged:
                audit.stage(delete_session, ctx, event)
                staged = True
            trash_service.trash_asset(delete_session, asset, audit_ctx=None)
        removed.append(asset_id)

    folders_removed = _remove_created_import_folders(created_folders)

    with Session(engine) as final:
        current = final.get(Operation, operation.id)
        if current is not None:
            if not staged:
                audit.stage(final, ctx, event)
            current.undo_state = UNDO_UNDONE
            final.add(current)
            final.commit()

    return _undo_payload(
        operation,
        assets_removed=len(removed),
        folders_removed=len(folders_removed),
        entries_failed=failed,
    )


def _undo_enrich(
    session: Session, operation: Operation, ctx: Optional[audit.AuditContext]
) -> dict:
    """Drop only the cache records this run fetched, and only while they are still its own.

    MEASURED: this step writes NO library row. `bundle_service.enrich_bundles`
    fills the REPORT (computed from disk and the cache) and writes the enrichment
    cache plus one `bundle.enrich` audit row per page that answered. A project's
    marker copies those fields only when a LATER create runs, and that copy is
    that operation's own record. So the truthful inverse is to remove what this
    run cached - and only what it cached: a `cached`, `refused`, `unavailable` or
    `unknown` result wrote nothing here. This undo never edits a bundle row or a
    project.

    The stamp is the ownership check: a record is dropped only while its
    `fetched_at` still equals the `fetched_at` this step reported, because a
    later enrich's newer record belongs to that operation and not to this one. A
    record that is already gone (a resumed undo) is a no-op and is counted
    neither way, and the cache write, the one `operation.undo` row and the
    `undo_state` flip commit together.
    """
    result = _result_of(_step_of(session, operation.id, "enrich"))
    records = bundle_service.get_enrichments()
    dropped: List[str] = []
    kept = 0
    for entry in result.get("results") or []:
        if not isinstance(entry, dict) or entry.get("status") != bundle_enrich.STATUS_FETCHED:
            continue
        cache_key = bundle_enrich.cache_key(entry.get("source_url"))
        record = records.get(cache_key) if cache_key is not None else None
        if record is None:
            continue
        if record.get("fetched_at") != entry.get("fetched_at"):
            kept += 1
            continue
        del records[cache_key]
        dropped.append(cache_key)
    if dropped:
        settings_service.set_setting(
            settings_service.BUNDLE_ENRICHMENTS_KEY,
            json.dumps(records, sort_keys=True),
            session=session,
        )
    audit.stage(
        session,
        ctx,
        audit.Event(
            audit.OPERATION_UNDO,
            operation.id,
            {"records_dropped": dropped, "records_kept": kept},
        ),
    )
    operation.undo_state = UNDO_UNDONE
    session.add(operation)
    session.commit()
    return _undo_payload(operation, records_dropped=len(dropped), records_kept=kept)


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
