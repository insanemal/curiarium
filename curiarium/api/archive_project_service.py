"""Create one project from the files the operator said an archive holds.

`POST /bundles/create` answers the question "what is this unlabelled folder?" and
answers it with rules: the trio, compound grouping, archive-owned promotion, the
container bound. Those rules exist because a folder that arrived by `rsync` has
no label, so guessing is the only option available. This module answers a
different question. The operator ticked one archive and pressed *Extract and
create*: that click already states that these entries are one thing, so nothing
here guesses and **no bundle rule is consulted on this path at all**. The module
deliberately imports neither ``bundle_detect``, ``bundle_service``,
``bundle_drift`` nor ``archive_service`` — the last of those would pull
``bundle_detect`` in for a twin-pairing check this door has no use for — and
``tests/test_archive_project.py`` refuses the door if any of them appears in its
transitive import closure, or if any rule function is called while it runs.

The design is a set comparison of knowns, not a guess about shape:

1. The entry listing is read from the archive itself, so the exact paths are
   known before a byte is written — it is the same listing the confirm step
   shows, through the same ``entry_policy`` plan the extract door iterates.
2. Each planned entry names one expected file: ``<twin>/<entry>``, matched
   against registered ``Asset.storage_path``. Nothing is inferred from what the
   scan happened to find.
3. The project is created from the asset ids that resolved, and its groups come
   from the listing's own structure — the common root's subdirectories — never
   from a detector.
4. Provenance goes in the marker (``_archive_project``): the archive path, the
   planned and resolved counts, the common root, the groups, and a fingerprint of
   the listing. A later reader can tell an operator-declared project from a
   detected one, and a second run on the same archive is idempotent **by archive
   path**, never by a bundle key that may not exist.

What still refuses is fact, not rule, and each refusal creates nothing: a partial
extraction reports how many planned entries never appeared; a settle after which
none of the planned paths is a registered asset names the twin directory so the
operator can go and look; and a repeat run on the same archive is already-created
rather than a duplicate. The project and every member are inserted in one
transaction (``project_service.create_project_with_members``), so a refusal — or
a name collision — can never strand an empty project.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from zipfile import BadZipFile, ZipFile

from fastapi import HTTPException
from sqlmodel import Session, select

import audit
import entry_policy
import library_scan
import project_service
import snapshot_guard
import snapshots
import zip_service
from db import engine
from file_utils import is_model_path
from models import Asset, Project

# The marker namespace this door owns. `_bundle` is detection's (see
# `project_service.bundle_marker_of`); a project carrying *this* key instead was
# declared by an operator, and that distinction is the whole reason the marker
# exists. It is written at insert time through `create_project(metadata=...)`, so
# a project and its provenance can never be separated by a crash between commits.
ARCHIVE_PROJECT_MARKER = "_archive_project"

# The four verdicts, every one of them a 200: this door has no policy to refuse
# on, so the verdict is a field rather than a status code.
STATUS_CREATED = "created"
STATUS_ALREADY_CREATED = "already_created"
STATUS_PARTIAL = "partial"
STATUS_NOTHING_RESOLVED = "nothing_resolved"

ARCHIVE_SUFFIX = ".zip"
FINGERPRINT_CHARS = 16

# How many expected paths go into one ``IN`` clause. SQLite caps the host
# variables a statement may bind (``SQLITE_MAX_VARIABLE_NUMBER``: 999 on older
# builds, 32766 since 3.32), and an ``IN`` list binds one per element — so a
# single query over the whole planned set would raise, and 500 the door, on an
# archive with tens of thousands of members. Batched, fixed size, comfortably
# under the smallest limit any build can carry; the per-batch rows are merged
# into one mapping, so the caller cannot tell chunking happened at all.
RESOLVE_QUERY_BATCH = 500


@dataclass(frozen=True)
class PlannedEntry:
    """One entry the listing promised, and where its bytes must have landed.

    ``entry`` is the path inside the archive as the listing reports it;
    ``rel_path`` is that entry rendered the way the extract door renders a
    destination — the twin, then each segment through the library's one filename
    rule — which is the ``Asset.storage_path`` a registering scan must hold.
    """

    entry: str
    rel_path: str


@dataclass(frozen=True)
class ResolvedMember:
    """One planned entry that did become a registered asset."""

    asset_id: str
    entry: str
    rel_path: str


@dataclass(frozen=True)
class GroupCount:
    """One of the common root's subdirectories, and how many members sit in it."""

    name: str
    count: int


@dataclass(frozen=True)
class ProjectRef:
    """A project's two identifying columns.

    Carried instead of the ORM row so no caller can reach for a column that was
    expired when the door's session closed; the route re-reads what it needs
    through `project_service.project_with_stats`, which exists for exactly that.
    """

    id: str
    name: str


@dataclass(frozen=True)
class ArchiveProjectResult:
    """What one extract-and-create request decided.

    ``project`` is the row this run created or found, and ``None`` on both
    refusals — which is the point of the two refusal statuses.
    """

    status: str
    archive: str
    twin: str
    planned: int
    resolved: int
    failed: Tuple[str, ...] = ()
    project: Optional[ProjectRef] = None
    common_root: Optional[str] = None
    groups: Tuple[GroupCount, ...] = ()
    fingerprint: str = ""


def create_project_from_extract(
    path: str,
    entries: Optional[List[str]],
    name: Optional[str],
    ctx: Optional[audit.AuditContext],
) -> ArchiveProjectResult:
    """Make one project from the assets an extraction was supposed to produce.

    ``entries`` is the selection the operator extracted; ``None`` is every member
    the archive holds. ``name`` defaults to the twin directory's own name.

    The idempotency lookup runs *before* the archive is opened, so a repeat click
    still answers already-created after the archive has been deleted, renamed or
    moved to the trash — which is precisely the state a completed extraction
    leaves the archive in. Reading the zip first would answer 404 instead, and a
    404 on the door whose whole job is the operator's own earlier click reads as
    a client error rather than as the idempotent no-op it is.
    """
    normalized = _normalised(path)

    existing = _find_by_archive(normalized)
    if existing is not None:
        return _finish(
            ArchiveProjectResult(
                status=STATUS_ALREADY_CREATED,
                archive=normalized,
                twin=_twin_rel(normalized),
                planned=0,
                resolved=0,
                project=ProjectRef(existing.id, existing.name),
            ),
            ctx,
        )

    source = _source_archive(normalized)
    twin_rel = _twin_rel(normalized)
    try:
        with ZipFile(source) as archive:
            plan = entry_policy.plan_selection(archive, entries)
    except BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid zip file") from None
    _refuse_unusable_plan(plan)

    planned = _plannable(plan.writable, twin_rel)
    if not planned:
        # Nothing in the selection can ever become a registered asset: every
        # member was OS metadata, directory-like, or hidden from the scan. There
        # is nothing to make a project from, and the twin is the place to look.
        return _finish(
            ArchiveProjectResult(
                status=STATUS_NOTHING_RESOLVED,
                archive=normalized,
                twin=twin_rel,
                planned=0,
                resolved=0,
            ),
            ctx,
        )

    found = _resolve(planned)
    # A list in listing order, not the mapping: the members' order is the
    # listing's, and `create_project_with_members` preserves it as `position`.
    members = [found[entry.rel_path] for entry in planned if entry.rel_path in found]
    failed = tuple(sorted(entry.entry for entry in planned if entry.rel_path not in found))

    if not members:
        # Zero of the planned paths is a registered asset. The files may be on
        # disk unregistered, or never written at all, and this door cannot tell
        # the two apart — so it says where they would be and creates nothing.
        return _finish(
            ArchiveProjectResult(
                status=STATUS_NOTHING_RESOLVED,
                archive=normalized,
                twin=twin_rel,
                planned=len(planned),
                resolved=0,
                failed=failed,
            ),
            ctx,
        )

    if failed:
        # The listing said these would be written and they are not registered
        # assets. Creating over the survivors would present a partial extraction
        # as a whole model, which is the lie this door exists to stop. Nothing is
        # written, so a retry after the operator acts — extract the rest, or let a
        # scan finish — resolves cleanly and creates.
        return _finish(
            ArchiveProjectResult(
                status=STATUS_PARTIAL,
                archive=normalized,
                twin=twin_rel,
                planned=len(planned),
                resolved=len(members),
                failed=failed,
            ),
            ctx,
        )

    common_root = _common_root(entry.entry for entry in planned)
    groups = _groups(planned, common_root)
    fingerprint = _fingerprint(entry.entry for entry in planned)
    marker = {
        ARCHIVE_PROJECT_MARKER: {
            "archive": normalized,
            "twin": twin_rel,
            "planned": len(planned),
            "resolved": len(members),
            "fingerprint": fingerprint,
            "common_root": common_root,
            "groups": [{"name": group.name, "count": group.count} for group in groups],
        }
    }
    project_name = (name or "").strip() or posixpath.basename(twin_rel)

    with Session(engine) as session:
        project = project_service.create_project_with_members(
            session,
            project_name,
            None,
            _cover_asset_id(members),
            [member.asset_id for member in members],
            origin="own",
            metadata=marker,
        )
        # Read back before the session closes: after it, scalar attributes are
        # whatever the identity map happens to still hold.
        created = ProjectRef(project.id, project.name)

    return _finish(
        ArchiveProjectResult(
            status=STATUS_CREATED,
            archive=normalized,
            twin=twin_rel,
            planned=len(planned),
            resolved=len(members),
            project=created,
            common_root=common_root,
            groups=groups,
            fingerprint=fingerprint,
        ),
        ctx,
    )


def archive_project_marker_of(metadata_json: Optional[str]) -> dict:
    """This door's marker, or ``{}`` — the one reader of ``_archive_project``.

    The shape `project_service.bundle_marker_of` uses for detection's key: a
    malformed or non-object metadata column reads as no marker rather than
    raising, because a reader of a project must not be taken down by a row
    something else wrote.
    """
    try:
        parsed = json.loads(metadata_json or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    marker = parsed.get(ARCHIVE_PROJECT_MARKER)
    return marker if isinstance(marker, dict) else {}


# --- the plan: what the listing promised -------------------------------------


def _plannable(writable: Sequence[str], twin_rel: str) -> List[PlannedEntry]:
    """The entries that can become a registered asset, in listing order.

    ``plan_selection`` has already dropped junk, directory-like and oversized
    members. What remains must still be something the scan is able to register,
    or a single dotfile in an archive would refuse this door forever: every path
    segment has to be visible (``library_scan._walk_storage`` prunes a
    dot-directory outright) and the basename has to survive
    ``library_scan.is_skipped_name`` — the one rule the scan and the detector
    already share, so this door cannot disagree with what got registered.
    """
    planned: List[PlannedEntry] = []
    for name in writable:
        segments = _member_segments(name, twin_rel)
        if any(segment.startswith(".") for segment in segments):
            continue
        if library_scan.is_skipped_name(segments[-1]):
            continue
        planned.append(PlannedEntry(entry=name, rel_path=posixpath.join(twin_rel, *segments)))
    return planned


def _member_segments(name: str, twin_rel: str) -> List[str]:
    """One member's rendered segments, refusing an escape from the twin.

    ``entry_policy.entry_member_segments`` is the same filename rule both
    extractors use, so a member is looked for exactly where it lands. A segment
    that renders to ``.`` or ``..`` refuses at 400 rather than being reported as
    an entry that failed to resolve: it is a crafted-archive shape, and a loud
    refusal is the honest answer for one.
    """
    try:
        return entry_policy.entry_member_segments(name)
    except entry_policy.EntryPathRefused as error:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{error.name!r} has a path segment that renders to {error.segment!r}, which would escape {twin_rel!r}; "
                "rename the member inside the archive, or select other entries"
            ),
        ) from error


def _refuse_unusable_plan(plan: entry_policy.EntryPlan) -> None:
    """Refuse a selection the extract door itself would have refused.

    The wording is `archive_service.extract_out`'s, deliberately: one selection
    has to mean the same thing at both doors, or an operator who was allowed to
    extract is told the project door disagrees about the archive.
    """
    if plan.missing:
        raise HTTPException(
            status_code=400,
            detail=f"Selection names entries that are not in the archive: {', '.join(sorted(plan.missing))}",
        )
    if plan.oversized:
        raise HTTPException(
            status_code=413,
            detail=(
                "Selected zip entries declare more than the per-file limit of "
                f"{entry_policy.IMPORT_MAX_BYTES} bytes (IMPORT_MAX_MB): "
                + ", ".join(sorted(plan.oversized))
            ),
        )


# --- the comparison: what actually landed ------------------------------------


def _resolve(planned: Sequence[PlannedEntry]) -> Dict[str, ResolvedMember]:
    """Every planned path that is a registered asset's ``storage_path``, by path.

    Keyed by the expected path so the caller can tell a hit from a miss without
    re-deriving either; `create_project_from_extract` immediately flattens it back
    into listing order.

    One ``IN`` query per batch over the exact expected paths — the whole point of
    capturing the listing first. The scan is free to have registered anything else
    in the twin, and detection is free to have declined the folder entirely;
    neither can change this answer, because it is a set comparison against what was
    promised.

    The expected paths are queried in batches of ``RESOLVE_QUERY_BATCH`` because
    SQLite caps how many host variables one statement may bind, and a huge archive
    would otherwise blow that cap over the whole planned set at once. Batching is
    invisible from here: the batches are merged into one mapping, one session
    serves them all, and the answer stays order-independent — the caller re-derives
    listing order itself from ``planned``.
    """
    wanted = {entry.rel_path: entry for entry in planned}
    if not wanted:
        return {}
    keys = list(wanted)
    resolved: Dict[str, ResolvedMember] = {}
    with Session(engine) as session:
        for offset in range(0, len(keys), RESOLVE_QUERY_BATCH):
            rows = session.exec(
                select(Asset.id, Asset.storage_path).where(
                    Asset.storage_path.in_(keys[offset : offset + RESOLVE_QUERY_BATCH])
                )
            ).all()
            for row in rows:
                if row.storage_path in wanted:
                    resolved[row.storage_path] = ResolvedMember(
                        asset_id=row.id,
                        entry=wanted[row.storage_path].entry,
                        rel_path=row.storage_path,
                    )
    return resolved


def _cover_asset_id(members: Sequence[ResolvedMember]) -> Optional[str]:
    """The first member the library calls a model, so the project has a face.

    An extension test, not a detector and not the stored ``mime``. That column is
    written once at registration from ``mimetypes``, whose answer depends on
    whether the host happens to ship ``/etc/mime.types`` — the published image is
    ``python:3.12-slim`` and does not, so every model there is stored as
    ``application/octet-stream`` and a prefix test finds no cover at all; and on a
    host that does ship the file, ``.3mf`` and ``.step`` land on
    ``application/vnd.ms-3mfdocument`` and ``application/p21``, which a ``model/``
    prefix refuses just as hard. Rows written before the table existed keep the
    value they were given, because a re-scan never rewrites ``mime``.
    ``web/src/lib/assetKinds.ts`` reached the same conclusion and matches by
    extension; this is that rule, and ``scripts/check_model_table.py`` pins the
    copies together. Extension only, with no ``or mime`` leg, is also deliberate:
    a document whose upload door was handed ``Content-Type: model/stl`` must not
    hijack the face. ``None`` leaves the project with no cover, which is what a
    project with only documents has always had.

    First-in-listing-order is preserved, so the member order a caller sees is
    unaffected by which member is chosen.
    """
    for member in members:
        if is_model_path(member.rel_path):
            return member.asset_id
    return None


# --- the listing's own structure ---------------------------------------------


def _common_root(entries: Iterable[str]) -> Optional[str]:
    """The one directory every planned entry sits under, or ``None``.

    The listing's own shape, read off the entry paths: the first casing wins so
    the value can name a directory on disk without a second lookup, and ``None``
    is the honest answer when there is no single one — a loose file at the top
    level, or two different first segments. Junk needs no exclusion pass because
    ``plan_selection`` left it out of ``writable`` before this ever saw it.

    ``bundle_detect.common_entry_root`` computes a sibling of this for the
    detector's own purposes. It is not reused, on purpose: reaching for it would
    put the detector in this door's import graph, and this door's whole claim is
    that it has none.
    """
    root: Optional[str] = None
    for entry in entries:
        head, separator, _rest = (entry or "").partition("/")
        if not separator:
            return None
        if root is None:
            root = head
        elif head.casefold() != root.casefold():
            return None
    return root


def _groups(planned: Sequence[PlannedEntry], common_root: Optional[str]) -> Tuple[GroupCount, ...]:
    """The common root's subdirectories, with how many members each holds.

    Groups are read off the listing rather than from a detector because the
    listing is what the operator was shown: a member directly in the common root
    belongs to no group (it is a loose file beside them, not a group of its own),
    and with no common root the twin directory itself is the root, so the
    entry's own first segment is its group.
    """
    counts: Dict[str, int] = {}
    for entry in planned:
        group = _group_of(entry.entry, common_root)
        if group is None:
            continue
        counts[group] = counts.get(group, 0) + 1
    return tuple(GroupCount(name=name, count=count) for name, count in sorted(counts.items()))


def _group_of(entry: str, common_root: Optional[str]) -> Optional[str]:
    """The first segment below the common root, or ``None`` for a loose file."""
    rest = entry
    if common_root:
        prefix = f"{common_root}/"
        if not rest.startswith(prefix):
            return None
        rest = rest[len(prefix) :]
    head, separator, _tail = rest.partition("/")
    return head if separator else None


def _fingerprint(entries: Iterable[str]) -> str:
    """A stable digest of the listing, so a later reader can spot a changed one.

    The sorted planned paths hashed, truncated: long enough to notice that an
    archive's contents changed between two runs, short enough to keep in a
    metadata column beside the counts it belongs to.
    """
    listing = "\n".join(sorted(entries))
    return hashlib.sha256(listing.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


# --- the archive, its twin, and what already exists --------------------------


def _find_by_archive(archive: str) -> Optional[Project]:
    """The project this archive already produced, or ``None``.

    Keyed by the archive path, because a bundle key may not exist: the row this
    flow acts on is exactly the kind detection declines to key, which is the
    failure this door replaces. The comparison is over the normalised
    library-relative path, the same case-sensitive string `asset.storage_path`
    uniqueness uses. The whole `project` table is scanned in Python, as
    `project_service` does for its own de-duplication: household scale, no
    index to miss, and no migration to add a column for it.
    """
    wanted = _normalised(archive)
    if not wanted:
        return None
    with Session(engine) as session:
        for project in session.exec(select(Project)).all():
            marker = archive_project_marker_of(project.metadata_json)
            if _normalised(str(marker.get("archive") or "")) == wanted:
                return project
    return None


def _normalised(path: str) -> str:
    """The echoed library-relative path: POSIX, with ``.`` segments collapsed."""
    candidate = posixpath.normpath(path) if path else ""
    return "" if candidate == "." else candidate


def _source_archive(normalized: str) -> Path:
    """The archive this request names: must exist, be a file, and be a zip."""
    source = _assert_library_path(normalized)
    if not source.is_file():
        raise HTTPException(status_code=404, detail=f"{normalized!r} is not a file in the library")
    if source.suffix.lower() != ARCHIVE_SUFFIX:
        raise HTTPException(status_code=415, detail="Library file is not a zip")
    return source


def _twin_rel(normalized: str) -> str:
    """``<parent>/<sanitised stem>``: where the extract door put the bytes.

    Recomputed rather than imported from ``archive_service`` on purpose: that
    module imports ``bundle_detect`` to check that a twin *pairs* with its
    archive under detection, and this door must be provably free of the detector —
    it never asks whether the directory is a bundle. The pairing refusal did
    happen, one request earlier, on the call that wrote the files; what is needed
    here is only the name they landed under, which is ``<parent>/`` plus
    ``zip_service.sanitize_folder_name`` of the stem — the same two calls, and
    `test_the_twin_name_matches_what_the_extract_door_writes` pins that the two
    doors still agree.
    """
    parent = posixpath.dirname(normalized)
    stem = posixpath.splitext(posixpath.basename(normalized))[0]
    if not stem or stem.startswith("."):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{normalized!r} has no usable twin directory name; "
                "a hidden or empty destination would never be scanned"
            ),
        )
    return posixpath.join(parent, zip_service.sanitize_folder_name(stem))


def _assert_library_path(rel: str) -> Path:
    """Resolve one library-relative path, mapping containment to its HTTP status.

    ``snapshots.assert_library_path`` is the ONE containment rule and
    ``snapshot_guard.status_for`` the one status map, so a bad path cannot mean
    400 on this door and 500 on another.
    """
    try:
        return snapshots.assert_library_path(rel)
    except snapshots.SnapshotPathError as error:
        raise HTTPException(status_code=snapshot_guard.status_for(error), detail=str(error)) from error


# --- the record --------------------------------------------------------------


def _finish(result: ArchiveProjectResult, ctx: Optional[audit.AuditContext]) -> ArchiveProjectResult:
    """Record one verdict and hand it straight back.

    Every verdict is recorded, the two refusals with the rest: the operator's
    click is the evidence this door runs on, so the click belongs in the history
    whether or not a project came of it. Fail-loud through ``audit.record``, like
    the extract door — a create whose row cannot be written must not pass as
    quiet success.
    """
    _record(result, ctx)
    return result


def _record(result: ArchiveProjectResult, ctx: Optional[audit.AuditContext]) -> None:
    """One ``archive.project.create`` row for one click."""
    if ctx is None:
        return
    audit.record(
        audit.Event(
            audit.ARCHIVE_PROJECT_CREATE,
            result.archive,
            {
                "twin": result.twin,
                "status": result.status,
                "planned": result.planned,
                "resolved": result.resolved,
                "project_id": result.project.id if result.project is not None else None,
            },
        ),
        ctx=ctx,
    )
