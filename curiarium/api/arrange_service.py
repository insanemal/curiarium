"""Arrange one detected bundle's bytes on disk, and undo it (Mf-v9eg).

Detection says what a folder IS; this module is the one operation that makes the
disk match a chosen shape. `plan` is a READ: it re-walks, asks the engine for the
move list the shape implies, and returns that list, the directories it needs, the
prose rendered from it and every refusal - a 200 with refusals is the honest
preview, and `apply` raises the first one after the client echoes the plan id back.

The order `apply` keeps is the contract:

1. the same plan, recomputed - refusals first, then a plan id that no longer
   matches is a stale preview;
2. commit 1: the project row(s) and the ledger in their markers. Durable before a
   single byte moves, so an interrupted arrange is a project with a replayable
   record, never a project with nothing behind it;
3. the move loop, through `asset_service.move_managed_file` - the ONE mover. A
   directory is moved whole by its rename fallback; there is no copy path, so an
   `EXDEV` is translated into a refusal and the ledger records how far it got;
4. commit 2: the rows (`ensure_directory_row` for the created directories, folder
   subtree paths, asset paths, retired wrapper rows) in one transaction;
5. the self-report (`unseeable_directories`, `stale_asset_paths`) on the wire,
   never swallowed, and the audit row.

`undo` folds the landed prefix backwards through the same mover, restores the
rows from the ledger's snapshots and removes exactly the memberships the ledger
recorded as added - it never deletes a `Project` row, because the ledger lives in
that project's marker and keeping it is what makes a second undo a 409.

A `.zip` row is never arranged: there is no copy path, so the refusal names the
extract door. `move_folder` is never called here; this module writes no second
mover.
"""

import errno
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

from fastapi import HTTPException
from sqlmodel import Session, select

import asset_service
import audit
import bundle_detect
import bundle_service
import folder_paths
import folder_service
import project_create_service
import project_service
import proposal_engine
import seams
from db import engine
from models import Asset, Folder, Project, ProjectAsset

# The declared cap. A plan above it is refused by name, before any byte moves.
MAX_ARRANGE_MOVES = 400
# The ledger's home inside `_bundle`, beside `key`/`detector`/`groups`.
ARRANGE_KEY = "arrange"

# The shapes `proposal_engine.arrange_moves` can enact. A shape outside this set
# AND outside the row's own `matches` is a client error; the row's detector shapes
# (e.g. `generic`) are offered by the report but enact no move, which is
# `nothing_to_arrange` rather than `unknown_shape`.
ENGINE_SHAPES = frozenset(proposal_engine.ONE_PROJECT_SHAPES) | frozenset(proposal_engine.SPLIT_SHAPES) | frozenset(
    {"add-to-project"}
)


@dataclass(frozen=True)
class ArrangeItem:
    """One previewed plan echoed back: the id proves the tree has not changed."""

    key: str
    shape: str
    plan_id: str


def _now() -> str:
    """ISO-8601 UTC with the column precision the rest of the app writes."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _refusal(status: int, reason: str, detail: str) -> dict:
    """One refusal in the wire vocabulary `BundleArrangeRefusalOut` declares."""
    return {"status": status, "reason": reason, "detail": detail}


def _root() -> Path:
    """The storage root at call time: tests monkeypatch `bundle_service.STORAGE`."""
    return Path(bundle_service.STORAGE)


def _report_rows(root: Path) -> Dict[str, dict]:
    """The report's rows keyed by storage-relative key, computed exactly as a create is.

    One walk serves the row lookup; the report is the only door that owns
    `excluded_keys`, so a key a create recognises is a key this preview does too.
    """
    scan = bundle_service.scan_bundles(root, excluded_keys=bundle_service._excluded_keys(""))
    report = bundle_service._report_from_scan(
        scan,
        root=root,
        scope="",
        limit=bundle_service.MAX_SCAN_ENTRIES,
        include_dismissed=True,
    )
    return {row["key"]: row for row in report["bundles"]}


# --- plan ------------------------------------------------------------------


def plan(*, key: str, shape: str) -> dict:
    """The arrangement preview: moves, prose, creates, refusals and the plan id.

    An unknown key raises a 409 - there is nothing to preview. Every refusal
    after that is RETURNED, including the archive row's, because the operator
    needs the whole picture in one answer.
    """
    root = _root()
    cleaned = (key or "").strip()
    row = _report_rows(root).get(cleaned)
    if row is None:
        raise HTTPException(status_code=409, detail=f"unknown bundle: {cleaned}")
    if row.get("blocked_reason") == bundle_service.ARCHIVE_ONLY:
        refusal = _refusal(
            409,
            "archive_not_extracted",
            "this row is an archive: extract it first with POST /import/archive, then arrange the "
            "folder it produced (the arrange never copies or unpacks)",
        )
        return _plan_out(cleaned, shape, [], [], [refusal], row)
    facts = proposal_engine.measure(str(root / cleaned), cleaned)
    moves = proposal_engine.arrange_moves(facts, shape)
    text = proposal_engine.arrange_proposal(facts, shape)
    creates = _creates(root, moves)
    targets = _project_targets(cleaned, shape, moves, row)
    pending = _pending_memberships(targets, moves)
    refusals = _refusals(root, row=row, key=cleaned, shape=shape, moves=moves, creates=creates, pending=pending)
    return _plan_out(cleaned, shape, moves, text, refusals, row, creates=creates)


def _plan_out(
    key: str,
    shape: str,
    moves: Sequence[dict],
    text: Sequence[str],
    refusals: Sequence[dict],
    row: Mapping,
    *,
    creates: Optional[Sequence[dict]] = None,
) -> dict:
    """The wire payload, with the plan id hashed over exactly what it shows."""
    creates = list(creates or [])
    return {
        "key": key,
        "shape": shape,
        "plan_id": proposal_engine.plan_digest(key, shape, moves, creates),
        "moves": list(moves),
        "text": list(text),
        "creates": creates,
        "refusals": list(refusals),
        "report": _report_out(row, shape, refusals),
    }


def _report_out(row: Mapping, shape: str, refusals: Sequence[dict]) -> dict:
    """The row's own verdict: is this shape what the row already recommends?"""
    matches = [match for match in row.get("matches") or [] if isinstance(match, Mapping)]
    recommended = matches[0].get("shape") if matches else row.get("metadata_shape")
    return {
        "creatable": bool(recommended == shape and not refusals),
        "blocked_reason": row.get("blocked_reason"),
    }


def _creates(root: Path, moves: Sequence[dict]) -> List[dict]:
    """The destination directories the moves imply: deduped, sorted, never the root."""
    paths = sorted({os.path.dirname(move["to"]) for move in moves} - {"", "."})
    return [{"path": path, "exists": (root / path).is_dir()} for path in paths]


def _refusals(
    root: Path,
    *,
    row: Mapping,
    key: str,
    shape: str,
    moves: Sequence[dict],
    creates: Sequence[dict],
    pending: Sequence[Tuple[dict, Sequence[str]]],
) -> List[dict]:
    """Every reason this plan cannot run, in one answer, never just the first.

    `unknown_shape` is settled first and suppresses `nothing_to_arrange`: a shape
    the engine cannot enact has no move list, so "changes nothing" would describe
    the unknown shape rather than the row. `nothing_to_arrange` is about the whole
    operation, not about bytes: creating or extending a project changes what the
    library holds even when the move list is empty, so the refusal is only made
    when no move, no created directory AND no missing membership is left.
    """
    offered = [match.get("shape") for match in row.get("matches") or [] if isinstance(match, Mapping)]
    known = shape in offered or shape in ENGINE_SHAPES
    refusals: List[dict] = []
    if not known:
        refusals.append(
            _refusal(409, "unknown_shape", f'"{shape}" is not a shape "{key}" offers; nothing was planned')
        )
    elif not moves and not creates and not _any_pending(pending):
        root_rel = pending[0][0]["root"] if pending else key
        refusals.append(
            _refusal(
                409,
                "nothing_to_arrange",
                f'this shape would change nothing: no moves, and the project already holds every file under "{root_rel}"',
            )
        )
    if len(moves) > MAX_ARRANGE_MOVES:
        refusals.append(
            _refusal(
                409,
                "too_many_moves",
                f'"{key}" needs {len(moves)} moves, above the declared cap of {MAX_ARRANGE_MOVES}; nothing moved',
            )
        )
    if seams.scan_status().get("running"):
        refusals.append(
            _refusal(
                409,
                "scan_running",
                f'a library scan is running; nothing was moved for "{key}". Try again when it finishes',
            )
        )
    with Session(engine) as session:
        for move in moves:
            refusals.extend(_move_refusals(root, session, move))
    return refusals


def _move_refusals(root: Path, session: Session, move: Mapping) -> List[dict]:
    """Every refusal one move carries, naming the path in each sentence."""
    source = move["from"]
    target = move["to"]
    findings: List[dict] = []
    first = target.split("/", 1)[0]
    if first.startswith(".") or first in folder_service.SYSTEM_DIRS:
        findings.append(
            _refusal(
                400,
                "hidden_destination",
                f'the destination "{target}" is in a system or hidden directory; refusing to move "{source}" into it',
            )
        )
    if not _inside_root(root, source):
        findings.append(_refusal(400, "outside_root", f'the source "{source}" resolves outside the storage root'))
    if not _inside_root(root, target):
        findings.append(
            _refusal(400, "outside_root", f'the destination "{target}" resolves outside the storage root')
        )
    if findings:
        return findings
    source_path = root / source
    target_path = root / target
    if not source_path.exists():
        return [_refusal(409, "missing_source", f'the source "{source}" is not on disk; nothing moved')]
    if target_path.exists():
        return [
            _refusal(
                409,
                "destination_exists",
                f'the destination "{target}" already exists; refusing to move "{source}" onto it',
            )
        ]
    anchor = _nearest_existing(target_path.parent)
    if anchor is not None and _stat_device(source_path) != _stat_device(anchor):
        findings.append(
            _refusal(
                409,
                "cross_device",
                "cross-device move refused: nothing moved, and this operation has no copy path "
                f'("{source}" -> "{target}")',
            )
        )
    pinned = _pinned_folder(session, source)
    if pinned is not None:
        folder, derived = pinned
        findings.append(
            _refusal(
                409,
                "pinned_row",
                f'the folder row "{folder.name}" ({folder.id}) implies "{derived}" while the move source is '
                f'"{source}"; the scan can never heal this, so the arrange refuses',
            )
        )
    return findings


def _inside_root(root: Path, relative: str) -> bool:
    """Whether a storage-relative path resolves to something below the root."""
    if not relative:
        return False
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve()
    return candidate != resolved_root and resolved_root in candidate.parents


def _nearest_existing(path: Path) -> Optional[Path]:
    """The closest ancestor of `path` that exists, or None at the filesystem root."""
    current = path
    while True:
        if current.exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def _stat_device(path: Path) -> int:
    """The device a path lives on; the one seam the cross-device pre-flight reads."""
    return os.stat(path).st_dev


def _pinned_folder(session: Session, source: str) -> Optional[Tuple[Folder, str]]:
    """A row that owns `source` while its PARENT implies a different path.

    This is the case `folder_paths.converge_ancestor_paths` can write and the scan
    can never heal: the row's stored path and the path its parent line derives are
    two different folders. The arrange refuses rather than converge, because
    moving the bytes would leave the row describing the other one.
    """
    wanted = source.casefold()
    for row in session.exec(select(Folder).where(Folder.storage_path.is_not(None))):
        if (row.storage_path or "").casefold() != wanted:
            continue
        parent_relative = folder_paths.preview_converged_path(session, row.parent_id)
        derived = folder_paths.join_relative(parent_relative, row.name)
        if derived != source:
            return row, derived
    return None


# --- apply -----------------------------------------------------------------


def apply(item: ArrangeItem, ctx: audit.AuditContext) -> dict:
    """Apply the previewed plan: projects first, then bytes, then rows, then the record."""
    project_ids, ledger = _ledger_for_plan(item.plan_id)
    if ledger is not None and ledger.get("undone"):
        project_ids, ledger = [], None
    if ledger is not None:
        if ledger.get("applied"):
            return _applied_out(ledger, project_ids, already_applied=True)
        return _finish(item, ledger, project_ids, ctx)
    preview = plan(key=item.key, shape=item.shape)
    if preview["refusals"]:
        first = preview["refusals"][0]
        raise HTTPException(status_code=first["status"], detail=first["detail"])
    if preview["plan_id"] != item.plan_id:
        raise HTTPException(status_code=409, detail="the tree changed since this plan was previewed; preview it again")
    return _fresh_apply(item, preview, ctx)


def _ledger_for_plan(plan_id: str) -> Tuple[List[str], Optional[dict]]:
    """Every project whose marker carries this plan, and the ledger itself."""
    if not plan_id:
        return [], None
    project_ids: List[str] = []
    ledger: Optional[dict] = None
    with Session(engine) as session:
        for project in session.exec(select(Project)):
            marker = project_service.bundle_marker_of(project.metadata_json)
            candidate = marker.get(ARRANGE_KEY)
            if not isinstance(candidate, dict) or candidate.get("plan") != plan_id:
                continue
            project_ids.append(project.id)
            if ledger is None:
                ledger = candidate
    return project_ids, ledger


def _fresh_apply(item: ArrangeItem, preview: Mapping, ctx: audit.AuditContext) -> dict:
    """The first run: commit 1, the moves, commit 2, the marker, the audit row."""
    root = _root()
    row = _report_rows(root).get(item.key)
    if row is None:
        raise HTTPException(status_code=409, detail=f"unknown bundle: {item.key}")
    moves = [{"from": move["from"], "to": move["to"], "kind": move["kind"]} for move in preview["moves"]]
    creates = [entry["path"] for entry in preview["creates"]]
    snapshot = _snapshot(moves, creates)
    targets = _project_targets(item.key, item.shape, moves, row)
    asset_rows = _asset_rows()
    arrange_id = str(uuid4())
    ledger = {
        "id": arrange_id,
        "key": item.key,
        "plan": preview["plan_id"],
        "shape": item.shape,
        "at": _now(),
        "applied": False,
        "undone": False,
        "done": 0,
        "moves": [[move["from"], move["to"], move["kind"]] for move in moves],
        "creates": creates,
        "projects": [],
        "folders": snapshot["folders"],
        "assets": snapshot["assets"],
        "rel_dir": targets[0]["root"] if targets else item.key,
    }
    project_ids, projects = _create_projects(item, row, targets, moves, asset_rows, ledger)
    ledger = {**ledger, "projects": projects}
    # Commit 1 is durable before the first byte moves: the projects and the full
    # ledger (ids included) are one marker write each.
    _write_ledger(project_ids, ledger)
    _move_forward(root, moves, ledger, project_ids, 0)
    return _after_moves(ledger, project_ids, ctx)


def _detection_marker(row: Mapping, shape: str, root_rel: str) -> dict:
    """The detection marker this arrange writes; the ledger is added beside it."""
    archive = row.get("archive") or {}
    rule = None
    for match in row.get("matches") or []:
        if isinstance(match, Mapping) and match.get("shape") == shape:
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
        "groups": list(row.get("groups") or []),
    }


def _project_targets(key: str, shape: str, moves: Sequence[dict], row: Mapping) -> List[dict]:
    """One project per product root, in the order the moves name them.

    A project already covering the target root is EXTENDED (``reuse``), never
    doubled: `add-to-project` is the whole operation when the folder already sits
    inside the project, and a project whose folders grew on disk keeps its identity
    when the arrange re-members it.
    """
    if shape in proposal_engine.SPLIT_SHAPES:
        roots: List[str] = []
        for move in moves:
            if move["kind"] != "dir" or move["to"] in roots:
                continue
            roots.append(move["to"])
        return [{"root": root, "name": proposal_engine.folder_title(os.path.basename(root))} for root in roots]
    root = key
    for move in moves:
        if move["from"] == key:
            root = move["to"]
            break
    existing = _project_covering(root)
    if existing is not None:
        return [{"root": root, "name": existing.name, "reuse": existing.id}]
    return [{"root": root, "name": row.get("name_suggestion") or "bundle"}]


def _project_covering(key: str) -> Optional[Project]:
    """The existing project whose marker key is a path prefix of `key`, if any."""
    best: Optional[Project] = None
    best_key = ""
    with Session(engine) as session:
        for project in session.exec(select(Project)):
            marker_key = project_service.bundle_marker_of(project.metadata_json).get("key")
            if not isinstance(marker_key, str) or not marker_key:
                continue
            if key != marker_key and not key.startswith(marker_key + "/"):
                continue
            if len(marker_key) > len(best_key):
                best, best_key = project, marker_key
    return best


def _asset_rows() -> List[Tuple[str, str, int]]:
    """(id, storage path, size) for every registered file, read once."""
    with Session(engine) as session:
        return [(asset.id, asset.storage_path or "", int(asset.size or 0)) for asset in session.exec(select(Asset))]


def _pending_memberships(targets: Sequence[dict], moves: Sequence[dict]) -> List[Tuple[dict, List[str]]]:
    """Each target with the memberships this arrange would ADD, read without writing.

    The delta is what makes `plan` honest about the operation's core case: creating
    a project or extending an existing one changes the library even when the move
    list is empty, so `nothing_to_arrange` is only true when the delta is empty too.
    A membership the project already holds is never pending, so an extend can never
    duplicate one.
    """
    asset_rows = _asset_rows()
    pending: List[Tuple[dict, List[str]]] = []
    with Session(engine) as session:
        for target in targets:
            wanted = [
                asset_id
                for asset_id, path, _size in asset_rows
                if _under(_project_path(path, moves), target["root"])
            ]
            if target.get("reuse"):
                held = set(
                    session.exec(select(ProjectAsset.asset_id).where(ProjectAsset.project_id == target["reuse"]))
                )
                wanted = [asset_id for asset_id in wanted if asset_id not in held]
            pending.append((target, wanted))
    return pending


def _any_pending(pending: Sequence[Tuple[dict, Sequence[str]]]) -> bool:
    """Whether any target root is still missing a membership."""
    return any(members for _target, members in pending)


def _project_path(path: str, moves: Sequence[dict]) -> str:
    """Where a current row path will live once the move list has run, in order."""
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


def _create_projects(
    item: ArrangeItem,
    row: Mapping,
    targets: Sequence[dict],
    moves: Sequence[dict],
    asset_rows: Sequence[Tuple[str, str, int]],
    ledger: Mapping,
) -> Tuple[List[str], List[dict]]:
    """Commit 1: one project per target root, with detection marker plus ledger.

    A target that names an existing project is EXTENDED: only the memberships it
    is missing are inserted, and the ledger's `members` snapshot is exactly those
    added ids, so undo removes what this arrange added and nothing else.
    """
    projected = [(asset_id, path, size, _project_path(path, moves)) for asset_id, path, size in asset_rows]
    project_ids: List[str] = []
    projects: List[dict] = []
    with Session(engine) as session:
        asset_index = project_create_service.project_assets_index(session)
        for target in targets:
            root_rel = target["root"]
            members = [
                project_create_service.MemberCandidate(asset_id, path)
                for asset_id, path, _size, new_path in projected
                if _under(new_path, root_rel)
            ]
            model_ids = [
                asset_id
                for asset_id, path, size, new_path in projected
                if _under(new_path, root_rel) and bundle_detect.is_counted_model(path, size)
            ]
            member_ids = [member.asset_id for member in members]
            if target.get("reuse"):
                added = _extend_project(session, target["reuse"], member_ids)
                project_ids.append(target["reuse"])
                projects.append(
                    {"id": target["reuse"], "root": root_rel, "created": False, "members": added}
                )
                continue
            marker = _detection_marker(row, item.shape, root_rel)
            marker[ARRANGE_KEY] = dict(ledger, done=0, applied=False)
            outcome = project_create_service.create_project_from_members(
                session,
                name=target["name"],
                origin=project_create_service.ORIGIN_DETECTION,
                members=members,
                marker=marker,
                dedupe=project_create_service.Dedupe(
                    key=root_rel,
                    name=target["name"],
                    assets=True,
                    containment=tuple(model_ids),
                ),
                cover_asset_id=None,
                require_members=False,
                refuse=None,
                description=row.get("description_excerpt"),
                creator=row.get("creator"),
                source_url=row.get("source_url"),
                project_origin="imported",
                licence=row.get("licence"),
                licence_url=row.get("licence_url"),
                asset_index=asset_index,
            )
            if outcome.status == project_create_service.STATUS_REFUSED:
                raise HTTPException(
                    status_code=409,
                    detail=f'the project for "{root_rel}" was refused: {outcome.reason}',
                )
            if outcome.project_id is None:
                raise HTTPException(status_code=409, detail=f'the project for "{root_rel}" was not written')
            project_ids.append(outcome.project_id)
            # A freshly created project holds exactly `member_ids`; a dedupe hit is an
            # existing project the arrange must EXTEND, and the ledger records only
            # what that extend actually added.
            added = member_ids
            if outcome.status == project_create_service.STATUS_ALREADY_CREATED:
                added = _extend_project(session, outcome.project_id, member_ids)
            projects.append(
                {
                    "id": outcome.project_id,
                    "root": root_rel,
                    "created": outcome.status == project_create_service.STATUS_CREATED,
                    "members": added,
                }
            )
    return project_ids, projects


def _extend_project(session: Session, project_id: str, member_ids: Sequence[str]) -> List[str]:
    """Add only the memberships `project_id` is missing, committing them together."""
    held = set(session.exec(select(ProjectAsset.asset_id).where(ProjectAsset.project_id == project_id)))
    added = [asset_id for asset_id in member_ids if asset_id not in held]
    stamp = _now()
    for asset_id in added:
        session.add(ProjectAsset(project_id=project_id, asset_id=asset_id, added_at=stamp))
    session.commit()
    return added


def _snapshot(moves: Sequence[dict], creates: Sequence[str]) -> dict:
    """The folder and asset rows an undo must restore, read before any byte moves."""
    with Session(engine) as session:
        folders = list(session.exec(select(Folder)))
        assets = list(session.exec(select(Asset)))
    sources = [move["from"] for move in moves]
    folder_entries: List[dict] = []
    for folder in folders:
        path = folder.storage_path or ""
        if not path:
            continue
        if _is_within_any(path, sources) or _is_ancestor_of_any(path, sources) or path in creates:
            folder_entries.append(
                {
                    "id": folder.id,
                    "name": folder.name,
                    "tags_json": folder.tags_json,
                    "parent_id": folder.parent_id,
                    "storage_path": folder.storage_path,
                }
            )
    folder_entries.sort(key=lambda entry: ((entry["storage_path"] or ""), entry["id"]))
    touched: Dict[str, str] = {}
    for move in moves:
        for asset in assets:
            path = asset.storage_path or ""
            if not path:
                continue
            if move["kind"] == "dir":
                if path.startswith(move["from"] + "/"):
                    touched[asset.id] = path
            elif path == move["from"]:
                touched[asset.id] = path
    asset_entries = sorted(([asset_id, path] for asset_id, path in touched.items()), key=lambda entry: entry[1])
    return {"folders": folder_entries, "assets": asset_entries}


def _is_within_any(path: str, prefixes: Sequence[str]) -> bool:
    """Whether `path` IS or sits below one of `prefixes`."""
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _is_ancestor_of_any(path: str, paths: Sequence[str]) -> bool:
    """Whether one of `paths` IS or sits below `path`."""
    return any(other == path or other.startswith(path + "/") for other in paths)


def _any_under(paths: Sequence[str], root: str) -> bool:
    """Whether one of `paths` IS or sits below `root` (the asset-side question)."""
    return _is_ancestor_of_any(root, paths)


def _write_ledger(project_ids: Sequence[str], ledger: Mapping) -> None:
    """Merge the whole ledger into each project's `_bundle`, preserving other keys.

    `merge_project_marker` replaces a `_`-prefixed key wholesale, so the WHOLE
    `_bundle` dict is passed - never `{"_bundle": {"arrange": ...}}`, which would
    drop `key`/`detector`/`groups`/`rel_dir` on the floor.
    """
    with Session(engine) as session:
        for project_id in dict.fromkeys(project_ids):
            project = session.get(Project, project_id)
            if project is None:
                continue
            bundle = dict(project_service.bundle_marker_of(project.metadata_json))
            bundle[ARRANGE_KEY] = dict(ledger)
            project.metadata_json = project_service.merge_project_marker(project.metadata_json, {"_bundle": bundle})
            project.updated_at = _now()
            session.add(project)
        session.commit()


def _move_forward(root: Path, moves: Sequence[dict], ledger: Mapping, project_ids: Sequence[str], start: int) -> int:
    """Run the moves from `start`, recording how far it got before it stopped."""
    landed = start
    try:
        for move in moves[start:]:
            asset_service.move_managed_file(root / move["from"], root / move["to"])
            landed += 1
    except HTTPException as error:
        _record_partial(ledger, project_ids, landed)
        reason = str(error.detail) if error.detail else "a move was refused"
        raise HTTPException(
            status_code=409,
            detail=_partial_detail(reason, landed, len(moves)),
        ) from None
    except OSError as error:
        _record_partial(ledger, project_ids, landed)
        if error.errno != errno.EXDEV:
            raise
        raise HTTPException(status_code=409, detail=_exdev_detail(landed, len(moves))) from None
    return landed


def _record_partial(ledger: Mapping, project_ids: Sequence[str], landed: int) -> None:
    """Persist the landed count so a re-post resumes rather than re-plans."""
    _write_ledger(project_ids, {**ledger, "applied": False, "done": landed})


def _partial_detail(reason: str, landed: int, total: int) -> str:
    """A stop's sentence: how far it got, and that a re-post finishes it."""
    if landed == 0:
        return f"{reason}; nothing moved"
    return f"{reason}; {landed} of {total} moves landed, so re-post the same plan to finish"


def _exdev_detail(landed: int, total: int) -> str:
    """A cross-device stop's sentence; the pre-flight names the same rule."""
    if landed == 0:
        return "cross-device move refused: nothing moved, and this operation has no copy path"
    return f"cross-device move refused after {landed} of {total} moves landed; re-post the same plan to finish"


def _finish(item: ArrangeItem, ledger: Mapping, project_ids: Sequence[str], ctx: audit.AuditContext) -> dict:
    """Resume a partial apply from its ledger, then run commit 2 and the record."""
    root = _root()
    moves = [
        {"from": entry[0], "to": entry[1], "kind": entry[2]}
        for entry in ledger.get("moves") or []
        if isinstance(entry, Sequence) and len(entry) == 3
    ]
    start = int(ledger.get("done") or 0)
    _move_forward(root, moves, ledger, project_ids, start)
    return _after_moves(ledger, project_ids, ctx)


def _after_moves(ledger: Mapping, project_ids: Sequence[str], ctx: audit.AuditContext) -> dict:
    """Commit 2, the completed ledger, the self-report and the audit row."""
    root = _root()
    moves = [
        {"from": entry[0], "to": entry[1], "kind": entry[2]}
        for entry in ledger.get("moves") or []
        if isinstance(entry, Sequence) and len(entry) == 3
    ]
    folder_ids = [entry["id"] for entry in ledger.get("folders") or [] if isinstance(entry, Mapping)]
    _rewrite_rows(root, moves, list(ledger.get("creates") or []), folder_ids)
    completed = {**ledger, "applied": True, "done": len(moves)}
    _write_ledger(project_ids, completed)
    audit.record(
        audit.Event(
            audit.ARRANGE_APPLY,
            str(completed.get("key") or ""),
            {
                "arrange_id": completed.get("id"),
                "plan": completed.get("plan"),
                "shape": completed.get("shape"),
                "moves": completed.get("moves") or [],
                "project_ids": [entry.get("id") for entry in completed.get("projects") or []],
                "created_project": any(entry.get("created") for entry in completed.get("projects") or []),
                "done": completed.get("done"),
            },
        ),
        ctx=ctx,
    )
    return _applied_out(completed, project_ids, already_applied=False)


def _applied_out(ledger: Mapping, project_ids: Sequence[str], *, already_applied: bool) -> dict:
    """The apply response: the ledger's own identity plus the current self-report."""
    projects = [entry for entry in ledger.get("projects") or [] if isinstance(entry, Mapping)]
    ids = [entry.get("id") for entry in projects if entry.get("id")]
    if not ids:
        ids = [project_id for project_id in project_ids if project_id]
    return {
        "key": ledger.get("key") or "",
        "shape": ledger.get("shape") or "",
        "plan_id": ledger.get("plan") or "",
        "arrange_id": ledger.get("id") or "",
        "project_id": ids[0] if ids else None,
        "project_ids": ids,
        "applied": bool(ledger.get("applied")),
        "already_applied": already_applied,
        "moves": len(ledger.get("moves") or []),
        "unseeable": folder_service.unseeable_directories(_root()),
        "stale": folder_service.stale_asset_paths(),
    }


def _rewrite_rows(root: Path, moves: Sequence[dict], creates: Sequence[str], folder_ids: Sequence[str]) -> None:
    """Commit 2: adopt created directories, move rows, retire wrappers, one commit."""
    with Session(engine) as session:
        for relative in sorted(creates, key=lambda path: (path.count("/"), path)):
            folder_service.ensure_directory_row(session, relative)
        for move in moves:
            if move["kind"] != "dir":
                continue
            folder = _folder_at(session, move["from"])
            if folder is None:
                continue
            parent_relative = os.path.dirname(move["to"])
            segment = os.path.basename(move["to"])
            paths = folder_paths.derive_subtree_paths(session, folder, parent_relative, segment)
            subtree = {}
            for row in folder_paths.folder_subtree(session, folder.id):
                subtree[row.id] = row
            folder_service.apply_folder_paths(session, subtree, paths)
            parent_row = _folder_at(session, parent_relative) if parent_relative else None
            if parent_row is not None:
                folder.parent_id = parent_row.id
                session.add(folder)
            folder_service.apply_asset_path_rewrites(
                session, folder_service.plan_asset_rewrites(session, move["from"], move["to"])
            )
        for move in moves:
            if move["kind"] != "file":
                continue
            asset = session.exec(select(Asset).where(Asset.storage_path == move["from"])).first()
            if asset is not None:
                asset.storage_path = move["to"]
                session.add(asset)
        _retire_gone_folders(session, root, folder_ids)
        session.commit()


def _folder_at(session: Session, relative: str) -> Optional[Folder]:
    """The row that names `relative`, case-insensitively, or None."""
    return session.exec(select(Folder).where(Folder.storage_path.collate("NOCASE") == relative)).first()


def _retire_gone_folders(session: Session, root: Path, folder_ids: Sequence[str]) -> None:
    """Delete rows whose directory the moves left empty and gone, deepest first."""
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


# --- undo ------------------------------------------------------------------


def undo(arrange_id: str, ctx: audit.AuditContext) -> dict:
    """Fold the landed moves back, restore the rows, drop the added memberships.

    A `Project` row is NEVER deleted here, not even one this arrange created, and
    the plan's "remove a created project if its membership is unchanged" is
    deliberately not done. A project owns no storage path and no bytes
    (`routers/projects.py` says so), so keeping it cannot make the folder view
    disagree with the disk. It is also what makes the second undo a 409: the ledger
    lives in the created project's `_bundle["arrange"]`, so deleting that project
    would delete the carrier of the record and turn the repeat into a 404.

    What undo does remove is exactly the memberships the ledger snapshotted as
    added - `projects[*].members`. A membership the operator added since is not in
    that snapshot and is left alone; a move whose two ends disagree refuses rather
    than guessing.
    """
    matches = _projects_for_arrange(arrange_id)
    if not matches:
        raise HTTPException(status_code=404, detail="arrange not found")
    ledger = matches[0][1]
    if ledger.get("undone"):
        raise HTTPException(status_code=409, detail="this arrange was already undone")
    root = _root()
    moves = [entry for entry in ledger.get("moves") or [] if isinstance(entry, Sequence) and len(entry) == 3]
    done = int(ledger.get("done") or 0)
    landed = moves[:done]
    for entry in reversed(landed):
        source = root / entry[0]
        target = root / entry[1]
        has_source = source.exists()
        has_target = target.exists()
        if has_target and not has_source:
            asset_service.move_managed_file(target, source)
        elif has_target and has_source:
            raise HTTPException(
                status_code=409,
                detail=f'cannot undo: "{entry[0]}" is present at both ends; refusing to guess',
            )
        else:
            raise HTTPException(
                status_code=409,
                detail=f'cannot undo: "{entry[0]}" is present at neither end; refusing to guess',
            )
    memberships_removed = _restore_rows(root, ledger)
    restored = {**ledger, "undone": True}
    _write_ledger([project_id for project_id, _ledger in matches], restored)
    audit.record(
        audit.Event(
            audit.ARRANGE_UNDO,
            str(restored.get("key") or ""),
            {
                "arrange_id": restored.get("id"),
                "plan": restored.get("plan"),
                "shape": restored.get("shape"),
                "moves": len(landed),
                "memberships_removed": memberships_removed,
                "projects_removed": [],
            },
        ),
        ctx=ctx,
    )
    return {
        "arrange_id": restored.get("id") or arrange_id,
        "key": restored.get("key") or "",
        "undone": True,
        "moves": len(landed),
        "project_removed": False,
        "projects_removed": [],
        "unseeable": folder_service.unseeable_directories(root),
        "stale": folder_service.stale_asset_paths(),
    }


def _projects_for_arrange(arrange_id: str) -> List[Tuple[str, dict]]:
    """Every project whose marker carries this arrange id."""
    found: List[Tuple[str, dict]] = []
    if not arrange_id:
        return found
    with Session(engine) as session:
        for project in session.exec(select(Project)):
            marker = project_service.bundle_marker_of(project.metadata_json)
            ledger = marker.get(ARRANGE_KEY)
            if isinstance(ledger, dict) and ledger.get("id") == arrange_id:
                found.append((project.id, ledger))
    return found


def _restore_rows(root: Path, ledger: Mapping) -> int:
    """Restore asset and folder paths, drop created rows, drop added memberships."""
    with Session(engine) as session:
        for entry in ledger.get("assets") or []:
            if not isinstance(entry, Sequence) or len(entry) != 2:
                continue
            asset = session.get(Asset, entry[0])
            if asset is None:
                continue
            asset.storage_path = entry[1]
            session.add(asset)
        folders = [entry for entry in ledger.get("folders") or [] if isinstance(entry, Mapping)]
        folders.sort(key=lambda entry: (entry.get("storage_path") or "").count("/"))
        for entry in folders:
            folder = session.get(Folder, entry.get("id"))
            if folder is None:
                session.add(
                    Folder(
                        id=entry.get("id"),
                        name=entry.get("name") or "",
                        tags_json=entry.get("tags_json") or "[]",
                        parent_id=entry.get("parent_id"),
                        storage_path=entry.get("storage_path"),
                    )
                )
            else:
                folder.name = entry.get("name") or folder.name
                folder.tags_json = entry.get("tags_json") or folder.tags_json
                folder.parent_id = entry.get("parent_id")
                folder.storage_path = entry.get("storage_path")
                session.add(folder)
            session.flush()
        _remove_created_rows(session, root, [path for path in ledger.get("creates") or [] if isinstance(path, str)])
        removed = _remove_added_memberships(session, ledger)
        session.commit()
    return removed


def _remove_created_rows(session: Session, root: Path, creates: Sequence[str]) -> None:
    """Delete a created destination row only when its directory is gone and unreferenced."""
    asset_paths = [asset.storage_path or "" for asset in session.exec(select(Asset)) if asset.storage_path]
    rows = [
        folder
        for folder in session.exec(select(Folder))
        if (folder.storage_path or "") and _is_within_any(folder.storage_path or "", list(creates))
    ]
    rows.sort(key=lambda folder: (folder.storage_path or "").count("/"), reverse=True)
    for folder in rows:
        path = folder.storage_path or ""
        if (root / path).exists():
            continue
        if _any_under(asset_paths, path):
            continue
        if session.exec(select(Folder).where(Folder.parent_id == folder.id)).first() is not None:
            continue
        session.delete(folder)
        session.flush()


def _remove_added_memberships(session: Session, ledger: Mapping) -> int:
    """Delete exactly the memberships the ledger recorded as added, and nothing else.

    `projects[*].members` is the snapshot of what the arrange ADDED: every member
    of a project it created, and only the missing ones for a project it extended.
    A membership the operator added afterwards is not in the snapshot, and a
    membership that predates the arrange was never snapshotted, so both survive.
    """
    removed = 0
    for entry in ledger.get("projects") or []:
        if not isinstance(entry, Mapping):
            continue
        project_id = entry.get("id")
        if not project_id:
            continue
        for asset_id in entry.get("members") or []:
            if not isinstance(asset_id, str):
                continue
            membership = session.exec(
                select(ProjectAsset).where(
                    ProjectAsset.project_id == project_id, ProjectAsset.asset_id == asset_id
                )
            ).first()
            if membership is None:
                continue
            session.delete(membership)
            removed += 1
    session.flush()
    return removed
