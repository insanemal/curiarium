"""Turn what is on disk into a dry-run bundle report, then into projects.

This is the only layer that touches both bytes and rows. `bundle_detect` decides
what a listing means; this module enumerates the storage tree, resolves each
candidate against the database, serves the report, and - only when a human
confirms - creates one project per bundle.

**The report is recomputed from disk on every read.** There is no cache, no
stored candidate table and no migration: a directory added by ``rsync``, a folder
renamed by hand or a zip dropped next to its twin changes the very next
``GET``/``build_report`` response. That is the whole reason detection can be
trusted on a box someone else administers.

**The folder is the truth; a twin zip is provenance only.** When a directory and
a ``<name>.zip`` sit side by side, the directory is the bundle and the archive
attaches no files. The archive is reported with ``twin_verified``: a comparison
of the casefolded *basenames* inside the zip with the casefolded basenames of the
folder's recursive entries. Names only, never a checksum - the archive attaches
nothing, so a mismatch is information for the operator (``False``), not an
assertion to raise. A zip with no extracted twin is still read, through
``zip_service.read_zip_text_entry`` (bounded, never extracted), so the report can
show its provenance; it is always ``blocked``/``archive_only``, because creation
happens from registered assets and extraction/registration is the existing
``POST /import/zip`` + library-scan path.

**Why ``_folder_index`` keys on ``Folder.storage_path``.** A bundle is only
creatable when its directory is a *registered* directory, meaning a ``Folder``
row whose ``storage_path`` is that relative path. The rows migrated from the
flattened v3 schema carry ``storage_path IS NULL`` (Mf-um7x), so they can never
match - that single fact *is* the legacy refusal. It keys on the row, never on
which detector fired: heal the folder row (a rescan gives it its real path) and
the same bundle becomes creatable with no code change, while an R2 bundle that
still has only a flattened row stays visible as ``high`` confidence and
``blocked``.

**The three commit windows, and why a re-run is safe.** One created bundle
commits three times: the project row *with* its ``_bundle`` marker (one
transaction, through ``project_service.create_project(metadata=...)``), then the
member assets (``project_service.add_assets``), then the audit row
(``audit.record``). Two shared services own their sessions, so this cannot be one
transaction without refactoring them; a crash between the windows can leave a
project with a marker and no members. It cannot duplicate one: every create pass
first asks ``find_existing_project`` - the marker key, then the source URL, then
an exact casefolded name - and a hit is ``skipped`` with the existing project id
and the project is never touched. A human edit is therefore never overwritten by
a re-run, and the report says ``already_created`` before anyone presses create.

**A bundle's project attaches only the files its snapshot names.** Detection is
one deepest-first pass, so a child bundle is claimed before its parent is
considered: the parent never inherits the child's models, and a directory that
holds nothing but child bundles is not a bundle at all. A created project
therefore attaches exactly the model files its own snapshot names - never a
nested bundle's models under the same recursive prefix.

**Bounds.** The walk is capped at ``MAX_SCAN_ENTRIES`` (``truncated`` says it
stopped early) and a document read is capped at
``bundle_detect.BUNDLE_DOC_MAX_BYTES``. ``limit`` bounds the *report*: the counts
still describe everything that was found, and ``truncated`` is true when rows
were withheld. Detection itself writes nothing - no row, no file, no folder
healing - so the report is safe on a read route.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
from zipfile import BadZipFile

from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import Session, select

import audit
import bundle_detect
import db
import library_scan
import project_service
import settings_service
import zip_service
from db import engine
from models import Asset, Folder, Project

# The storage root bundle keys are resolved against. A module global so tests can
# point it at a temp tree exactly like ``library_scan.STORAGE``.
STORAGE = db.STORAGE
# A walk is bounded so a pathological tree cannot make a read request run forever.
MAX_SCAN_ENTRIES = 200_000
# The report's own bound. ``build_report`` never truncates by accident: a caller
# that needs every row (a create pass) asks for more than the walk can produce.
DEFAULT_LIMIT = 200

READY = "ready"
BLOCKED = "blocked"
UNRESOLVED_DIRECTORY = "unresolved_directory"
NOT_SCANNED = "not_scanned"
ARCHIVE_ONLY = "archive_only"

COVER_LARGEST_IMAGE = "largest-image"
COVER_LARGEST_IMAGE_UNREGISTERED = "largest-image-unregistered"
COVER_NONE = "none"

CREATE_CREATED = "created"
CREATE_SKIPPED = "skipped"
CREATE_BLOCKED = "blocked"
CREATE_FAILED = "failed"


@dataclass(frozen=True)
class BundleCandidate:
    """One directory or zip that looks like a bundle, before any database read."""

    key: str
    snapshot: bundle_detect.BundleSnapshot
    archive_path: Optional[str]
    is_archive: bool


def _entry_size(entry) -> int:
    """One ``stat`` per file; a file that vanished mid-walk is simply zero bytes."""
    try:
        return int(entry.stat(follow_symlinks=False).st_size)
    except OSError:
        return 0


def _suffix(path: str) -> str:
    return PurePath(path or "").suffix.lower()


def _depth(rel_dir: str) -> int:
    return len([part for part in rel_dir.split("/") if part])


def _parent(rel_dir: str) -> str:
    return rel_dir.rsplit("/", 1)[0] if "/" in rel_dir else ""


def _basename(rel_dir: str) -> str:
    return rel_dir.rsplit("/", 1)[-1]


def _folder_snapshot(rel_dir: str, entries: Sequence[bundle_detect.BundleEntry]) -> bundle_detect.BundleSnapshot:
    return bundle_detect.BundleSnapshot(bundle_name=_basename(rel_dir), rel_dir=rel_dir, entries=tuple(entries))


def _read_bounded(path: Path, max_bytes: int) -> Optional[str]:
    """Read at most ``max_bytes + 1`` bytes; over the cap the document is skipped."""
    try:
        with Path(path).open("rb") as handle:
            payload = handle.read(max_bytes + 1)
    except OSError:
        return None
    if len(payload) > max_bytes:
        return None
    return payload.decode("utf-8", errors="replace")


def _read_docs(base: Path, candidates: Sequence[str]) -> Dict[str, str]:
    docs: Dict[str, str] = {}
    for candidate in candidates:
        text = _read_bounded(base / candidate, bundle_detect.BUNDLE_DOC_MAX_BYTES)
        if text is not None:
            docs[candidate] = text
    return docs


def _detect_folder(root: Path, snapshot: bundle_detect.BundleSnapshot) -> Optional[bundle_detect.BundleFacts]:
    docs = _read_docs(Path(root) / snapshot.rel_dir, bundle_detect.doc_entry_candidates(snapshot))
    return bundle_detect.detect_bundle(snapshot, docs)


def _detect_archive(zip_path: Path, snapshot: bundle_detect.BundleSnapshot) -> Optional[bundle_detect.BundleFacts]:
    docs: Dict[str, str] = {}
    for candidate in bundle_detect.doc_entry_candidates(snapshot):
        text = zip_service.read_zip_text_entry(zip_path, candidate, bundle_detect.BUNDLE_DOC_MAX_BYTES)
        if text is not None:
            docs[candidate] = text
    return bundle_detect.detect_bundle(snapshot, docs)


def _zip_snapshot(zip_path: Path, rel_path: str) -> Optional[bundle_detect.BundleSnapshot]:
    """The listing of one archive, or ``None`` when it is not readable at all."""
    try:
        entries = zip_service.list_zip_entries(zip_path)
    except (BadZipFile, OSError):
        return None
    return bundle_detect.BundleSnapshot(
        bundle_name=Path(rel_path).stem,
        rel_dir=rel_path,
        entries=tuple(bundle_detect.BundleEntry(rel_path=entry["path"], size=int(entry["size"])) for entry in entries),
    )


def _subtree(files_by_dir: Mapping[str, Sequence[Tuple[str, int]]]) -> Dict[str, List[bundle_detect.BundleEntry]]:
    """Every entry below each directory, folded deepest-first: O(files), not O(dirs x files)."""
    folded: Dict[str, List[bundle_detect.BundleEntry]] = {
        rel_dir: [bundle_detect.BundleEntry(rel_path=name, size=size) for name, size in files]
        for rel_dir, files in files_by_dir.items()
    }
    for rel_dir in sorted(folded, key=lambda item: (-_depth(item), item)):
        if not rel_dir:
            continue
        parent = _parent(rel_dir)
        prefix = _basename(rel_dir)
        folded.setdefault(parent, []).extend(
            bundle_detect.BundleEntry(rel_path=f"{prefix}/{entry.rel_path}", size=entry.size) for entry in folded[rel_dir]
        )
    return folded


def _forget_claimed_subtree(work: Mapping[str, List[bundle_detect.BundleEntry]], claimed_dir: str) -> None:
    """Drop a claimed bundle's files from every ancestor's view.

    Deepest-first is what makes this sound: by the time a directory is claimed its
    children have already been evaluated, so what is left in its list is what this
    bundle owns. Only the claimed subtree is removed, never a whole intermediate
    directory: claiming ``A/B/C`` strips ``C/...`` from ``A/B`` and only
    ``B/C/...`` from ``A``, so ``A`` keeps ``A/B``'s own unclaimed files.
    """
    suffix = _basename(claimed_dir)
    parent = _parent(claimed_dir)
    while parent:
        kept = work.get(parent)
        if kept is not None:
            prefix = suffix + "/"
            work[parent] = [entry for entry in kept if not entry.rel_path.startswith(prefix)]
        suffix = _basename(parent) + "/" + suffix
        parent = _parent(parent)


def iter_bundle_candidates(root: Path) -> Tuple[List[BundleCandidate], bool]:
    """Every bundle under ``root``, plus whether the walk was cut short.

    One deepest-first pass. ``bundle_detect.detect_bundle`` already applies R1 ->
    R2 -> R3 strongest-first within a single directory, so the only ordering a
    tree-wide walk needs is children before parents: a child bundle is claimed
    before its parent is evaluated, and claiming it removes it from the parent's
    view. A directory can therefore only ever be a bundle in its own right - it
    never inherits a child's models, and a directory that holds nothing but child
    bundles is not a bundle at all.
    """
    root = Path(root)
    files_by_dir: Dict[str, List[Tuple[str, int]]] = {}
    dirs_by_parent: Dict[str, set] = {"": set()}
    directories = 0
    files = 0
    truncated = False
    for rel_dir, file_entries, _ignored in library_scan.iter_directories(root):
        if rel_dir:
            dirs_by_parent.setdefault(_parent(rel_dir), set()).add(_basename(rel_dir))
            directories += 1
        sizes = files_by_dir.setdefault(rel_dir, [])
        for entry in file_entries:
            files += 1
            sizes.append((entry.name, _entry_size(entry)))
        if directories + files > MAX_SCAN_ENTRIES:
            truncated = True
            break

    folded = _subtree(files_by_dir)
    found: List[BundleCandidate] = []
    # A mutable view of each directory's OWN entries. Claiming a bundle drops its
    # whole branch from every ancestor, so a parent can only ever be a bundle in
    # its own right: it never inherits a child's models, and a container
    # directory that holds nothing but child bundles is not a bundle at all.
    work: Dict[str, List[bundle_detect.BundleEntry]] = {
        rel_dir: list(entries) for rel_dir, entries in folded.items() if rel_dir
    }
    for rel_dir in sorted(work, key=lambda item: (-_depth(item), item.casefold(), item)):
        entries = work[rel_dir]
        if not entries:
            continue
        snapshot = _folder_snapshot(rel_dir, entries)
        facts = _detect_folder(root, snapshot)
        if facts is None:
            continue
        found.append(BundleCandidate(key=rel_dir, snapshot=snapshot, archive_path=None, is_archive=False))
        _forget_claimed_subtree(work, rel_dir)

    # Zip-only candidates: an archive whose extracted twin is not beside it.
    for rel_path in sorted(_zip_files(files_by_dir), key=lambda item: (item.casefold(), item)):
        twin = _twin_directory(rel_path, dirs_by_parent)
        if twin:
            continue
        snapshot = _zip_snapshot(root / rel_path, rel_path)
        if snapshot is None:
            continue
        if _detect_archive(root / rel_path, snapshot) is None:
            continue
        found.append(BundleCandidate(key=rel_path, snapshot=snapshot, archive_path=None, is_archive=True))

    # A folder candidate's archive is provenance: reported, never a member.
    with_twins: List[BundleCandidate] = []
    for candidate in found:
        if candidate.is_archive:
            with_twins.append(candidate)
            continue
        archive = _twin_archive(candidate.key, files_by_dir)
        with_twins.append(
            BundleCandidate(
                key=candidate.key,
                snapshot=candidate.snapshot,
                archive_path=archive,
                is_archive=candidate.is_archive,
            )
        )
    with_twins.sort(key=lambda candidate: (candidate.key.casefold(), candidate.key))
    return with_twins, truncated


def _zip_files(files_by_dir: Mapping[str, Sequence[Tuple[str, int]]]) -> List[str]:
    return [
        f"{rel_dir}/{name}" if rel_dir else name
        for rel_dir, files in files_by_dir.items()
        for name, _size in files
        if _suffix(name) == ".zip"
    ]


def _twin_directory(rel_path: str, dirs_by_parent: Mapping[str, set]) -> Optional[str]:
    parent = _parent(rel_path)
    wanted = bundle_detect.twin_key(_basename(rel_path))
    for name in sorted(dirs_by_parent.get(parent, ())):
        if bundle_detect.twin_key(name) == wanted:
            return f"{parent}/{name}" if parent else name
    return None


def _twin_archive(rel_dir: str, files_by_dir: Mapping[str, Sequence[Tuple[str, int]]]) -> Optional[str]:
    parent = _parent(rel_dir)
    wanted = bundle_detect.twin_key(_basename(rel_dir))
    for name, _size in sorted(files_by_dir.get(parent, ())):
        if _suffix(name) != ".zip":
            continue
        if bundle_detect.twin_key(name) == wanted:
            return f"{parent}/{name}" if parent else name
    return None


def _folder_index(session: Session) -> Dict[str, str]:
    """Every resolved folder as ``{casefolded storage_path: folder id}``.

    Loaded once per report: resolving a bundle is a dictionary lookup, not a
    table scan per candidate, and the first row for a path wins so a hand-made
    duplicate cannot make the answer depend on row order.

    The match is on ``Folder.storage_path``, the column that says "this row is a
    directory". A migrated v3 row has it NULL (Mf-um7x) and therefore never
    matches, which is exactly the legacy refusal; a rescan that heals the row
    makes the same bundle creatable without any change here.
    """
    index: Dict[str, str] = {}
    for folder in session.exec(select(Folder).where(Folder.storage_path.is_not(None))):
        key = (folder.storage_path or "").casefold()
        if key and key not in index:
            index[key] = folder.id
    return index


def registered_assets(session: Session, rel_dir: str) -> List[Tuple[str, str, int, str]]:
    """``(asset_id, storage_path, size, mime)`` for every asset under ``rel_dir``."""
    prefix = (rel_dir or "") + "/"
    statement = (
        select(Asset)
        .where(
            Asset.storage_path.is_not(None),
            func.substr(Asset.storage_path, 1, len(prefix)) == prefix,
        )
        .order_by(Asset.storage_path)
    )
    return [
        (asset.id, asset.storage_path or "", int(asset.size or 0), asset.mime or "")
        for asset in session.exec(statement)
    ]


def twin_verified(zip_path: Path, folder_entries: Sequence[bundle_detect.BundleEntry]) -> bool:
    """Whether the archive's entry basenames match the folder's, casefolded.

    Names only: the archive attaches nothing, so this is a provenance remark, not
    an integrity check. Any read failure is ``False`` - never an exception on a
    read route.
    """
    try:
        entries = zip_service.list_zip_entries(Path(zip_path))
    except (BadZipFile, OSError):
        return False
    archive_names = sorted({PurePath(entry["path"]).name.casefold() for entry in entries})
    folder_names = sorted({PurePath(entry.rel_path).name.casefold() for entry in folder_entries})
    return archive_names == folder_names


def get_dismissals() -> Dict[str, dict]:
    """The dismissal map, keyed by bundle key; anything malformed reads ``{}``."""
    raw = settings_service.get_setting(settings_service.BUNDLE_DISMISSALS_KEY)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): value if isinstance(value, dict) else {} for key, value in parsed.items()}


def set_dismissals(keys: Sequence[str], dismissed: bool, ctx: audit.AuditContext) -> dict:
    """Add or remove dismissal keys, audit each change, report what is stale.

    One JSON value is read, modified and written: two concurrent dismiss requests
    can lose an entry. Accepted at household scale (and audited), rather than a
    table for a working set nobody reads one row at a time.
    """
    current = get_dismissals()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    actor = getattr(ctx.actor, "username", None) if ctx else None
    changed: List[str] = []
    for key in keys:
        cleaned = (key or "").strip()
        if not cleaned:
            continue
        if dismissed:
            if cleaned not in current:
                current[cleaned] = {"at": stamp, "by": actor}
                changed.append(cleaned)
        elif cleaned in current:
            del current[cleaned]
            changed.append(cleaned)
    if changed:
        settings_service.set_setting(settings_service.BUNDLE_DISMISSALS_KEY, json.dumps(current, sort_keys=True))
    for key in changed:
        audit.record(audit.Event(audit.BUNDLE_DISMISS, key, {"dismissed": dismissed}), ctx=ctx)
    live = {candidate.key for candidate in iter_bundle_candidates(STORAGE)[0]}
    stale = sorted((key for key in current if key not in live), key=lambda key: (key.casefold(), key))
    return {"dismissed": sorted(current), "stale_dismissals": stale}


def _bundle_marker(metadata_json: Optional[str]) -> dict:
    """The reserved ``_bundle`` marker of one project row, or ``{}``."""
    try:
        parsed = json.loads(metadata_json or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    marker = parsed.get("_bundle")
    return marker if isinstance(marker, dict) else {}


def find_existing_project(
    session: Session,
    *,
    key: str,
    source_url: Optional[str],
    name: str,
) -> Optional[Project]:
    """The project this bundle already became, in the order that cannot lie.

    The marker key is the identity a previous create wrote; the source URL covers
    a project imported by hand from the same page; an exact casefolded name
    covers a project made before the marker existed. Any hit means
    ``already_created`` and the row is never touched again.
    """
    wanted_name = (name or "").strip().casefold()
    by_name: Optional[Project] = None
    for project in session.exec(select(Project).order_by(func.lower(Project.name), Project.id)):
        if _bundle_marker(project.metadata_json).get("key") == key:
            return project
        if by_name is None and wanted_name and project.name.strip().casefold() == wanted_name:
            by_name = project
    existing = project_service.find_project_by_source(session, source_url)
    if existing is not None:
        return existing
    return by_name


def _scope(path: str) -> str:
    """The storage-relative subtree a report is limited to; never leaves the root.

    `path` is operator input on a READ route, and `_walk_storage` resolves `..` at
    the syscall level (its own pruning only stops a *symlinked* directory), so a
    literal `..` segment would walk out of the library. An absolute path, a
    home-relative `~` and any `..` component are therefore a 400, not a silent
    reinterpretation - the same rule `snapshots.resolve_view_path` applies to an
    operator-supplied path joined to a store root. A NUL byte is refused here too
    because it is the one value `os.scandir` raises `ValueError` on, which the
    walker's `OSError` catch would not turn into a skipped directory.
    """
    raw = (path or "").strip()
    if not raw:
        return ""
    if "\x00" in raw or raw.startswith(("/", "~")):
        raise HTTPException(status_code=400, detail="path must stay inside the storage root")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise HTTPException(status_code=400, detail="path must stay inside the storage root")
    return "/".join(parts)


def _is_inside_storage(root: Path) -> bool:
    """Whether a scoped root still resolves inside the storage root.

    `_walk_storage` prunes a symlinked *child* but never checks the directory it is
    seeded with, so a symlink inside the library named as `path` would be followed
    wherever it points. Resolving both sides rather than comparing strings is also
    what keeps a deployment whose storage root is itself a symlink or a mount
    working.
    """
    try:
        resolved = Path(root).resolve()
        storage = Path(STORAGE).resolve()
    except OSError:
        return False
    return resolved == storage or storage in resolved.parents


def _qualify(scope: str, rel_path: str) -> str:
    return f"{scope}/{rel_path}" if scope else rel_path


def _candidate_facts(root: Path, candidate: BundleCandidate) -> Optional[bundle_detect.BundleFacts]:
    if candidate.is_archive:
        return _detect_archive(Path(root) / candidate.snapshot.rel_dir, candidate.snapshot)
    return _detect_folder(Path(root), candidate.snapshot)


def _cover(assets: Sequence[Tuple[str, str, int, str]], facts: bundle_detect.BundleFacts) -> dict:
    images = sorted(
        (asset for asset in assets if _suffix(asset[1]) in bundle_detect.IMAGE_SUFFIXES),
        key=lambda asset: (-asset[2], asset[1]),
    )
    if images:
        return {"asset_id": images[0][0], "rel_path": images[0][1], "reason": COVER_LARGEST_IMAGE}
    if facts.cover_entry:
        return {"asset_id": None, "rel_path": facts.cover_entry, "reason": COVER_LARGEST_IMAGE_UNREGISTERED}
    return {"asset_id": None, "rel_path": None, "reason": COVER_NONE}


def _report_row(
    session: Session,
    *,
    root: Path,
    scope: str,
    candidate: BundleCandidate,
    folders: Mapping[str, str],
    dismissals: Mapping[str, dict],
) -> Optional[dict]:
    facts = _candidate_facts(root, candidate)
    if facts is None:
        return None
    key = _qualify(scope, candidate.key)
    suggestion = (facts.title or "").strip() or "bundle"
    assets: List[Tuple[str, str, int, str]] = []
    own_assets: List[Tuple[str, str, int, str]] = []
    if candidate.is_archive:
        status, blocked_reason = BLOCKED, ARCHIVE_ONLY
    else:
        assets = registered_assets(session, key)
        # Only the candidate's OWN entries decide status and cover: a registered
        # asset below a nested child bundle belongs to that bundle, not to this one.
        own = {f"{key}/{entry.rel_path}" for entry in candidate.snapshot.entries}
        own_assets = [asset for asset in assets if asset[1] in own]
        models = [asset for asset in own_assets if _suffix(asset[1]) in bundle_detect.MODEL_SUFFIXES]
        if key.casefold() not in folders:
            status, blocked_reason = BLOCKED, UNRESOLVED_DIRECTORY
        elif not models:
            status, blocked_reason = BLOCKED, NOT_SCANNED
        else:
            status, blocked_reason = READY, None
    archive = None
    if candidate.archive_path:
        archive = {
            "rel_path": _qualify(scope, candidate.archive_path),
            "twin_verified": twin_verified(root / candidate.archive_path, candidate.snapshot.entries),
        }
    existing = find_existing_project(session, key=key, source_url=facts.source_url, name=suggestion)
    return {
        "key": key,
        "rel_path": key,
        "title": facts.title,
        "name_suggestion": suggestion,
        "creator": facts.creator,
        "licence": facts.licence,
        "licence_url": facts.licence_url,
        "source_url": facts.source_url,
        "detector": facts.detector,
        "confidence": facts.confidence,
        "metadata_shape": facts.metadata_shape,
        "description_excerpt": facts.description,
        "model_count": facts.counts.model,
        "image_count": facts.counts.image,
        "text_count": facts.counts.text,
        "other_count": facts.counts.other,
        "cover": _cover(own_assets, facts),
        "assets_total": len(assets),
        "status": status,
        "blocked_reason": blocked_reason,
        "archive": archive,
        "existing_project": existing.id if existing else None,
        "dismissed": key in dismissals,
    }


def build_report(*, path: str = "", limit: int = DEFAULT_LIMIT, include_dismissed: bool = False) -> dict:
    """The whole detection report, read from disk now. Writes nothing.

    ``path`` scopes the walk (``""`` is the whole storage root) and only changes
    what is scanned: every key stays storage-root-relative, so a scoped read and
    a whole-storage read name the same bundle, and a create pass can find a row
    a scoped report showed.
    """
    scope = _scope(path)
    root = STORAGE / scope if scope else STORAGE
    if not _is_inside_storage(root):
        raise HTTPException(status_code=400, detail="path must stay inside the storage root")
    candidates, walk_truncated = iter_bundle_candidates(root)
    dismissals = get_dismissals()
    rows: List[dict] = []
    with Session(engine) as session:
        folders = _folder_index(session)
        for candidate in candidates:
            row = _report_row(
                session, root=root, scope=scope, candidate=candidate, folders=folders, dismissals=dismissals
            )
            if row is not None:
                rows.append(row)

    visible = rows if include_dismissed else [row for row in rows if not row["dismissed"]]
    counts = {
        "total": len(visible),
        "high": sum(1 for row in visible if row["confidence"] == bundle_detect.CONFIDENCE_HIGH),
        "low": sum(1 for row in visible if row["confidence"] == bundle_detect.CONFIDENCE_LOW),
        "ready": sum(1 for row in visible if row["status"] == READY),
        "blocked": sum(1 for row in visible if row["status"] == BLOCKED),
        "dismissed": sum(1 for row in rows if row["dismissed"]),
        "already_created": sum(1 for row in visible if row["existing_project"]),
    }
    bound = max(0, int(limit))
    live_keys = {candidate.key for candidate in candidates}
    live_keys = {_qualify(scope, key) for key in live_keys}
    return {
        "root": root.name,
        "truncated": walk_truncated or len(visible) > bound,
        "counts": counts,
        "bundles": visible[:bound],
        "stale_dismissals": sorted(
            (key for key in dismissals if key not in live_keys), key=lambda key: (key.casefold(), key)
        ),
    }


@dataclass(frozen=True)
class BundleCreateItem:
    """One confirmed bundle: which one, what to call it, and whether to enrich it."""

    key: str
    name: Optional[str] = None
    name_only: bool = False


def _result(
    key: str,
    status: str,
    *,
    project_id: Optional[str] = None,
    name: Optional[str] = None,
    assets_added: int = 0,
    skipped_assets: int = 0,
    failed_assets: Optional[List[dict]] = None,
    reason: Optional[str] = None,
) -> dict:
    return {
        "key": key,
        "status": status,
        "project_id": project_id,
        "name": name,
        "assets_added": assets_added,
        "skipped_assets": skipped_assets,
        "failed_assets": failed_assets or [],
        "reason": reason,
    }


def _enrichment(row: Mapping, name_only: bool) -> dict:
    """The provenance a create writes; ``name_only`` deliberately writes none."""
    if name_only:
        return {"creator": None, "licence": None, "licence_url": None, "source_url": None, "description": None}
    return {
        "creator": row["creator"],
        "licence": row["licence"],
        "licence_url": row["licence_url"],
        "source_url": row["source_url"],
        "description": row["description_excerpt"],
    }


def _create_one(
    item: BundleCreateItem,
    row: Optional[Mapping],
    candidate: Optional[BundleCandidate],
    ctx: audit.AuditContext,
) -> dict:
    key = (item.key or "").strip()
    if row is None or candidate is None:
        return _result(key, CREATE_FAILED, reason="unknown bundle")
    if row["status"] == BLOCKED:
        return _result(key, CREATE_BLOCKED, reason=row["blocked_reason"] or BLOCKED)
    name = (item.name or "").strip() or row["name_suggestion"]
    enrichment = _enrichment(row, item.name_only)
    marker = {
        "_bundle": {
            "key": key,
            "detector": row["detector"],
            "confidence": row["confidence"],
            "rel_dir": row["rel_path"],
            "archive": (row["archive"] or {}).get("rel_path"),
            "twin_verified": (row["archive"] or {}).get("twin_verified") if row["archive"] else None,
        }
    }
    with Session(engine) as session:
        existing = find_existing_project(session, key=key, source_url=enrichment["source_url"], name=name)
        if existing is not None:
            return _result(
                key,
                CREATE_SKIPPED,
                project_id=existing.id,
                name=existing.name,
                reason="already_created",
            )
        try:
            project = project_service.create_project(
                session,
                name,
                enrichment["description"],
                row["cover"]["asset_id"],
                creator=enrichment["creator"],
                source_url=enrichment["source_url"],
                licence=enrichment["licence"],
                licence_url=enrichment["licence_url"],
                origin="imported",
                metadata=marker,
            )
        except HTTPException as exc:
            if exc.status_code == 409:
                # `_clean_name` runs before the NOCASE index, so a typed name that
                # cleans to an existing one slips the pre-check: a skip, never a 500.
                return _result(key, CREATE_SKIPPED, name=name, reason="project name already exists")
            return _result(key, CREATE_FAILED, name=name, reason=str(exc.detail))
        project_id = project.id
        project_name = project.name

    with Session(engine) as session:
        # The same set the report used: the files this bundle's snapshot names,
        # never a nested child bundle's models under the recursive prefix.
        own = {f"{key}/{entry.rel_path}" for entry in candidate.snapshot.entries}
        model_assets = [
            asset[0]
            for asset in registered_assets(session, key)
            if asset[1] in own and _suffix(asset[1]) in bundle_detect.MODEL_SUFFIXES
        ]
    added, skipped, failed = project_service.add_assets(project_id, model_assets)
    audit.record(
        audit.Event(
            audit.BUNDLE_CREATE,
            key,
            {
                "project_id": project_id,
                "created": True,
                "assets_added": len(added),
                "assets_failed": len(failed),
                "detector": row["detector"],
                "confidence": row["confidence"],
                "name_only": bool(item.name_only),
            },
        ),
        ctx=ctx,
    )
    return _result(
        key,
        CREATE_CREATED,
        project_id=project_id,
        name=project_name,
        assets_added=len(added),
        skipped_assets=len(skipped),
        failed_assets=failed,
    )


def create_bundles(items: Sequence[BundleCreateItem], ctx: audit.AuditContext) -> dict:
    """Create one project per confirmed bundle; never a duplicate, never a clobber.

    The report is recomputed first and every item is looked up in it: a key the
    client sent is never trusted on its own, and a bundle that turned
    ``blocked`` since the report was rendered creates nothing.
    """
    candidates, _walk_truncated = iter_bundle_candidates(STORAGE)
    report = build_report(include_dismissed=True, limit=MAX_SCAN_ENTRIES)
    rows = {row["key"]: row for row in report["bundles"]}
    by_key = {candidate.key: candidate for candidate in candidates}
    counters = {CREATE_CREATED: 0, CREATE_SKIPPED: 0, CREATE_BLOCKED: 0, CREATE_FAILED: 0}
    results: List[dict] = []
    for item in items:
        key = (item.key or "").strip()
        result = _create_one(item, rows.get(key), by_key.get(key), ctx)
        counters[result["status"]] += 1
        results.append(result)
    return {
        "created": counters[CREATE_CREATED],
        "skipped": counters[CREATE_SKIPPED],
        "blocked": counters[CREATE_BLOCKED],
        "failed": counters[CREATE_FAILED],
        "results": results,
    }
