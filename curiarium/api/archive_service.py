"""Extract an archive that is already in the library, in place, into its twin.

`POST /import/zip` can only extract a zip whose bytes the server just fetched, so
an operator who `rsync`ed 169 vendor archives into ``import/`` had no door at
all: bundle detection reported every one of them `archive_only` forever. This
module is that door, and it writes into the same place the vendor would have:
``<parent>/<stem>`` next to ``<parent>/<stem>.zip``, which is exactly the pair
``proposal_engine.twin_key`` already recognises. The detector therefore needs no
change - once the files exist and a scan registers them, the same bundle becomes
`ready`.

Containment is S2's, not a second implementation: ``snapshots.assert_library_path``
confines the request's path and every target, and its ``SnapshotPathError`` is
mapped with ``snapshot_guard.status_for``, so a bad path means the same status
here as it does on every snapshot route. Entry names come from
``entry_policy``'s normaliser, each path segment is rendered by the same module
(per segment, so the tree keeps its shape), folder names come from
``zip_service``'s sanitiser, and the budget from ``entry_policy``'s shared
preflight plus stream - one accounting rule for both extractors. Junk is
classified first, so this door writes only payload: a junk-only selection is a
200 that writes nothing at all.

The listing half of this module is public because a second door now asks the same
questions: the `extract-and-create` operations workflow plans what a selection
would write from the archive's own listing, executes the write through
`extract_out` here, and must not grow a second copy of the entry rule. So
`normalise`, `source_archive`, `twin_rel_for`, `refuse_unusable_plan`,
`planned_entries`, `listing_root`, `listing_groups` and `listing_fingerprint`
are the ONE answer to "what would this selection write, and where", and
`plan_extraction` is that answer in one call. `entry_policy` is EXECUTED here
and never restated: the extractor and the planner both iterate the plan object
`entry_policy.plan_selection` returns.

Three properties make this safe to run over a live library:

* **All-or-nothing.** Every destination is known before a byte is written. If
  *all* of them already exist the request is an idempotent no-op: nothing is
  written, no scan starts and no audit row exists, because a request that changes
  nothing writes nothing. If *any* of them exists the whole request refuses with
  409 naming every blocker, so the operator can act on all of them at once. A
  selection that cannot be written at all - one member that is another member's
  directory, or one whose rendered path segment is ``.`` or ``..`` - refuses with
  400 naming it, before anything is staged.
* **Staged, then published.** The bytes land in a dot-directory beside the zip,
  which ``library_scan._walk_storage`` prunes, so neither the scan nor bundle
  detection can ever see a half-extracted tree. Publication uses
  ``asset_service._move_without_clobber`` - the existing no-clobber arbiter - and
  *any* failure while publishing (a collision, a directory made impossible after
  the preflight, an I/O error) rolls back every file this request already
  published. Nothing this request did not publish is ever unlinked.
* **The scan is the registering step.** Extraction writes files; only a scan
  turns files into `Asset` rows. The response says whether a scan started (a
  scan already running is reported as ``false``, never as a failure), exactly
  like a snapshot restore.
"""

from __future__ import annotations

import hashlib
import posixpath
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional, Sequence, Tuple
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from fastapi import HTTPException

import audit
import entry_policy
import library_scan
import proposal_engine
import seams
import snapshot_guard
import snapshots
import zip_service

# Deliberate private import: `_move_without_clobber` is the existing no-clobber
# arbiter for this storage tree, and the point of reusing it is that an extract
# inherits its guarantee instead of growing a second one that can disagree.
from asset_service import _move_without_clobber
from file_utils import is_model_path
from schemas import ArchiveExtractOut

ARCHIVE_SUFFIX = ".zip"
FINGERPRINT_CHARS = 16


@dataclass(frozen=True)
class PlannedEntry:
    """One entry a listing promised, and where its bytes must land.

    ``entry`` is the path inside the archive as the listing reports it;
    ``rel_path`` is that entry rendered the way the extractor renders a
    destination - the twin, then each segment through the library's one filename
    rule - which is the ``Asset.storage_path`` a registering scan must hold.
    """

    entry: str
    rel_path: str


@dataclass(frozen=True)
class ListingGroup:
    """One bucket of the listing, below the archive's common root.

    ``name`` is the subdirectory's own name, or ``""`` for the files that sit
    directly in the common root (the loose bucket - the same ``""`` group
    `create-project`'s review has always used, so `sum(members) == the plan's
    members` is structurally true here too). ``models`` counts the members the
    extension rule calls a model, because no scan row exists yet at plan time to
    ask for a role.
    """

    name: str
    members: int
    models: int


class ExtractionPlan(NamedTuple):
    """Everything one selection would write, answered before a byte is written.

    ``excluded`` names every requested member that is not planned, with the
    server's own reason - junk, a directory member, a hidden segment, a basename
    the scan skips. A member that is missing or oversized is a refusal, not an
    exclusion: `refuse_unusable_plan` raises it before this exists.
    """

    path: str
    twin: str
    planned: Tuple[PlannedEntry, ...]
    excluded: Tuple[dict, ...]
    common_root: Optional[str]
    groups: Tuple[ListingGroup, ...]
    fingerprint: str


def extract_out(
    path: str,
    entries: Optional[List[str]],
    ctx: Optional[audit.AuditContext],
    *,
    start_scan: bool = True,
) -> ArchiveExtractOut:
    """Extract an archive the library already holds into its twin directory.

    ``entries is None`` means every member the archive holds, normalised and
    de-duplicated: two raw members can normalise to one path, which can only be
    extracted once. The destination is the zip's own parent plus the sanitised
    stem, refused at 400 when that would be empty, hidden, or would not pair with
    the zip under bundle detection: a hidden twin is pruned by the scan and an
    unpaired one never becomes a bundle, so either would exist on disk and
    nowhere else.

    ``start_scan=False`` writes the same bytes and starts no walk, for the one
    caller that owns the scan itself: `extract-and-create` scans in its own step
    after filing the archive, and a background walk started here would race that
    move (and register the archive at its old path). The route keeps the default,
    so its behaviour is byte-identical; the audit row records which it was.
    """
    normalized = normalise(path)
    source = source_archive(normalized)
    twin_rel = twin_rel_for(normalized)
    try:
        with ZipFile(source) as zf:
            plan = entry_policy.plan_selection(zf, entries)
            refuse_unusable_plan(plan)
            if not plan.writable:
                # The other nothing-written outcome: no twin directory, no scan
                # kick and no audit row. Every member was junk or directory-like,
                # so no destination existed and `already_present` stays False;
                # `already_present is False` with `files == 0` is this signature,
                # where the every-destination-exists no-op below answers True.
                return ArchiveExtractOut(
                    path=normalized,
                    destination=twin_rel,
                    files=0,
                    bytes=0,
                    already_present=False,
                    scan_started=False,
                )
            target_rels = [_member_rel(twin_rel, name) for name in plan.writable]
            targets = [_assert_library_path(rel) for rel in target_rels]
            if _already_present(targets):
                # Every destination exists: this request would change nothing, so
                # it writes nothing, starts no scan and records no audit row.
                return ArchiveExtractOut(
                    path=normalized,
                    destination=twin_rel,
                    files=0,
                    bytes=0,
                    already_present=True,
                    scan_started=False,
                )
            _refuse_prefix_conflict(target_rels)
            _refuse_occupied(twin_rel, target_rels, targets)

            staging = source.parent / f".{posixpath.basename(twin_rel)}.extract-{uuid4().hex}"
            created_dirs: List[Path] = []
            written = 0
            published: List[Path] = []
            try:
                for name in plan.writable:
                    staged = staging / name
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        written = entry_policy.stream_entry(
                            zf,
                            plan.members[name],
                            staged,
                            spent_bytes=written,
                        )
                    except entry_policy.EntryTooLarge as error:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"{name!r} expands beyond the per-file limit of "
                                f"{entry_policy.IMPORT_MAX_BYTES} bytes (IMPORT_MAX_MB)"
                            ),
                        ) from error
                    except entry_policy.BudgetExceeded as error:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"Selected zip entries expand beyond {error.total_written} bytes; "
                                f"the limit is {entry_policy.IMPORT_ZIP_MAX_BYTES} bytes (IMPORT_ZIP_MAX_MB)"
                            ),
                        ) from error

                try:
                    for name, target_rel, target in zip(plan.writable, target_rels, targets, strict=True):
                        created_dirs.extend(_make_directories(target.parent, twin_rel))
                        if not _move_without_clobber(staging / name, target):
                            # Something claimed the name after the preflight; the
                            # handler below undoes this request's own publications
                            # and this names the blocker. Nothing else is unlinked.
                            raise HTTPException(
                                status_code=409,
                                detail=f"{target_rel} already exists; a library extract never overwrites",
                            )
                        published.append(target)
                except Exception:
                    # Every mid-publish failure looks the same on disk: the files
                    # published so far, no way to finish. A directory made
                    # impossible after the preflight and an ENOSPC/EIO out of the
                    # move are no different from a collision, so all of them undo
                    # this request's own publications, newest first, before the
                    # original failure propagates.
                    for published_path in reversed(published):
                        published_path.unlink(missing_ok=True)
                    raise
            finally:
                shutil.rmtree(staging, ignore_errors=True)
                # Deepest first, so a directory this request created for one
                # target does not keep an empty one alive. A non-empty one is
                # kept: it holds published bytes, which are the result.
                for directory in sorted(created_dirs, key=lambda item: len(item.parts), reverse=True):
                    try:
                        directory.rmdir()
                    except OSError:
                        continue
    except BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid zip file") from None

    # Never raise when a scan is already running: the bytes are written by now,
    # so `False` is the honest answer rather than a refusal to report success.
    # And a caller that owns the scan (`start_scan=False`) starts none: `False`
    # here means "this request started no walk", never "the library is unscanned".
    scan_started = bool(seams.start_scan()) if start_scan else False

    # One row per successful extract, fail-loud. An idempotent request wrote
    # nothing, so it records nothing, and a selection with nothing writable
    # returned before this point. The guard is defensive only: the route's
    # `audit.context_for` always returns a context and the plan guarantees at
    # least one target, so neither branch is reachable from the route.
    if ctx is not None and target_rels:
        audit.record(
            audit.Event(
                audit.ARCHIVE_EXTRACT,
                normalized,
                {
                    "destination": twin_rel,
                    "files": len(target_rels),
                    "bytes": written,
                    "scan_started": scan_started,
                },
            ),
            ctx=ctx,
        )

    return ArchiveExtractOut(
        path=normalized,
        destination=twin_rel,
        files=len(target_rels),
        bytes=written,
        already_present=False,
        scan_started=scan_started,
    )


def normalise(path: str) -> str:
    """The echoed library-relative path: POSIX, with ``.`` segments collapsed."""
    candidate = posixpath.normpath(path) if path else ""
    return "" if candidate == "." else candidate


def _assert_library_path(rel: str) -> Path:
    """Resolve one library-relative path, mapping containment to its HTTP status.

    ``snapshots.assert_library_path`` is the ONE containment rule (resolution
    first, so ``..`` and symlinks have nothing left to slip through); the status
    is ``snapshot_guard.status_for``'s, so a bad path cannot mean 400 on this
    route and 500 on another.
    """
    try:
        return snapshots.assert_library_path(rel)
    except snapshots.SnapshotPathError as error:
        raise HTTPException(status_code=snapshot_guard.status_for(error), detail=str(error)) from error


def source_archive(normalized: str) -> Path:
    """The archive a request names: must exist, be a file, and be a zip."""
    source = _assert_library_path(normalized)
    if not source.is_file():
        raise HTTPException(status_code=404, detail=f"{normalized!r} is not a file in the library")
    if source.suffix.lower() != ARCHIVE_SUFFIX:
        # The zip-import door says "Imported file is not a zip"; nothing was
        # imported here, so this door says what it actually checked.
        raise HTTPException(status_code=415, detail="Library file is not a zip")
    return source


def twin_rel_for(normalized: str) -> str:
    """``<parent>/<sanitised stem>``: the directory a twin detector pairs with the zip.

    Building a plausible name is not enough: the whole point is that
    ``proposal_engine.twin_key`` pairs the zip with this directory, so the key it
    would compute for the twin must equal the key it computes for the archive.
    Sanitising agrees for clean names and disagrees for a trailing space, a
    whitespace-only stem or a doubled suffix - and a twin that never pairs would
    leave the extraction on disk and the bundle `archive_only` forever, so it is
    refused before anything is read.
    """
    parent = posixpath.dirname(normalized)
    archive_name = posixpath.basename(normalized)
    stem = posixpath.splitext(archive_name)[0]
    if not stem or stem.startswith("."):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{normalized!r} has no usable twin directory name; "
                "a hidden or empty destination would never be scanned"
            ),
        )
    twin_rel = posixpath.join(parent, zip_service.sanitize_folder_name(stem))
    twin_name = posixpath.basename(twin_rel)
    if proposal_engine.twin_key(twin_name) != proposal_engine.twin_key(archive_name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{normalized!r} cannot be extracted into a paired twin: {twin_rel!r} would be keyed "
                f"{proposal_engine.twin_key(twin_name)!r} but the archive is keyed "
                f"{proposal_engine.twin_key(archive_name)!r}, so bundle detection would never pair them "
                "and the extraction would never become a bundle. Rename the archive first."
            ),
        )
    return twin_rel


def _member_rel(twin_rel: str, name: str) -> str:
    """One member's destination: the twin, then its rendered segments.

    The rendering, and the refusal of a segment that renders to ``.`` or ``..``,
    is ``entry_policy.entry_member_segments``'s: one filename rule for both
    extractors. It refuses here, before the rels are resolved and before staging
    exists, so nothing is written for a member that would escape the twin.
    """
    try:
        segments = entry_policy.entry_member_segments(name)
    except entry_policy.EntryPathRefused as error:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{error.name!r} has a path segment that renders to {error.segment!r}, which would escape {twin_rel!r}; "
                "rename the member inside the archive, or select other entries"
            ),
        ) from error
    return posixpath.join(twin_rel, *segments)


def _refuse_prefix_conflict(target_rels: List[str]) -> None:
    """Refuse a selection where one member must be another member's directory.

    ``sub`` and ``sub/file.stl`` cannot both be files. Written in sorted order,
    every member that is a directory prefix of another sorts immediately before
    its first child, so one adjacent pass finds every conflict. This is a defect
    in the request, not in the library, so it is a 400 naming both paths rather
    than an ``IsADirectoryError`` half-way through the staging copy.
    """
    ordered = sorted(target_rels)
    for outer, inner in zip(ordered, ordered[1:], strict=False):
        if inner.startswith(outer + "/"):
            raise HTTPException(
                status_code=400,
                detail=f"{outer} cannot be both a file and a directory of {inner}; select one or the other",
            )


def _already_present(targets: List[Path]) -> bool:
    """Whether every destination already exists, making the request a no-op."""
    return bool(targets) and all(target.exists() or target.is_symlink() for target in targets)


def _refuse_occupied(twin_rel: str, target_rels: List[str], targets: List[Path]) -> None:
    """The all-or-nothing preflight: refuse a partial collision before any write.

    Called only when at least one destination is free (the every-destination case
    returned earlier as an idempotent no-op). Naming *every* blocker in one 409 is
    what lets the operator clear them all in one pass instead of one refusal at a
    time.
    """
    existing = [rel for rel, target in zip(target_rels, targets, strict=True) if target.exists() or target.is_symlink()]
    if existing:
        raise HTTPException(
            status_code=409,
            detail=(
                "refusing to extract: these paths already exist - move or delete them first: "
                + ", ".join(sorted(existing))
            ),
        )
    for label in _directory_labels(twin_rel, target_rels):
        candidate = _assert_library_path(label)
        if candidate.exists() and not candidate.is_dir():
            raise HTTPException(
                status_code=409,
                detail=f"{label} exists and is not a directory; an extract never overwrites it",
            )


def _directory_labels(twin_rel: str, target_rels: List[str]) -> List[str]:
    """Every directory this extract may have to create, twin first."""
    labels: List[str] = [twin_rel]
    seen = {twin_rel}
    for target_rel in target_rels:
        inner = posixpath.dirname(target_rel[len(twin_rel) + 1 :])
        if not inner:
            continue
        current = twin_rel
        for segment in inner.split("/"):
            current = posixpath.join(current, segment)
            if current in seen:
                continue
            seen.add(current)
            labels.append(current)
    return labels


def _make_directories(path: Path, twin_rel: str) -> List[Path]:
    """Create one directory and its parents; the caller prunes it when the request leaves it empty."""
    created: List[Path] = []
    for candidate in [path, *path.parents]:
        if candidate.exists():
            break
        created.append(candidate)
    try:
        for directory in reversed(created):
            directory.mkdir()
    except OSError as error:
        raise HTTPException(
            status_code=409,
            detail=f"{twin_rel} cannot be created; a file is in the way",
        ) from error
    return created


# --- the listing, as a plan ---------------------------------------------------
# One answer to "what would this selection write, and where", asked before a byte
# exists. The extractor below and the `extract-and-create` planner both read it,
# so the plan an operator approves cannot describe a tree the extractor would not
# produce.


def refuse_unusable_plan(plan: entry_policy.EntryPlan) -> None:
    """Refuse a selection the extractor cannot honour, with the extractor's words.

    Two facts, two statuses, the same sentences this module has always answered:
    a pick the archive does not hold is a 400 naming every miss, and a pick whose
    DECLARED size is over the per-file limit is a 413 naming every offender
    before staging exists (a lying archive is still caught byte by byte while it
    streams). Junk is not a refusal: `plan_selection` left it out of `writable`,
    and a junk-only selection is a 200 that writes nothing.
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


def plan_extraction(
    path: str, entries: Optional[List[str]], *, twin: Optional[str] = None
) -> ExtractionPlan:
    """What one selection of one archive would write, and what it would leave out.

    ``twin`` exists for exactly one caller: a re-plan after the archive was filed
    under its twin's ``_source``. Its bytes now live at
    ``<twin>/_source/<archive>.zip``, the listing is read from there, and the
    destinations are still the original twin's - so the caller hands the twin it
    recorded rather than letting ``dirname`` recompute a directory inside
    ``_source``.

    Raises what the extractor raises: 400/413 from `refuse_unusable_plan`, 404
    for a path that is not a file, 415 for a non-zip, 400 for a member whose
    rendered segment would escape the twin. It opens the zip for its LISTING
    only and writes nothing.
    """
    normalized = normalise(path)
    source = source_archive(normalized)
    twin_rel = twin if twin is not None else twin_rel_for(normalized)
    try:
        with ZipFile(source) as zf:
            selection = entry_policy.plan_selection(zf, entries)
    except BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid zip file") from None
    refuse_unusable_plan(selection)
    planned, excluded = planned_entries(selection, twin_rel)
    common_root = listing_root(entry.entry for entry in planned)
    return ExtractionPlan(
        path=normalized,
        twin=twin_rel,
        planned=tuple(planned),
        excluded=tuple(excluded),
        common_root=common_root,
        groups=tuple(listing_groups(planned, common_root)),
        fingerprint=listing_fingerprint(entry.entry for entry in planned),
    )


def planned_entries(
    plan: entry_policy.EntryPlan, twin_rel: str
) -> Tuple[List[PlannedEntry], List[dict]]:
    """The selection's entries that can become registered assets, and the rest.

    `plan.writable` has already dropped junk, directory-like, missing and
    oversized members; what remains must still be something the scan is able to
    register, or a single dotfile inside an archive would refuse the plan
    forever. Every path segment has to be visible (`library_scan._walk_storage`
    prunes a dot-directory outright) and the basename has to survive
    `library_scan.is_skipped_name` - the one rule the scan and the detector
    already share, so a plan cannot disagree with what the scan registers.

    Returns `(planned, excluded)`. ``excluded`` names every requested member
    that is not planned, in request order, with a reason an operator can act on:
    the junk it was, the directory it is, the hidden segment it carries, or the
    name the scan skips. A missing or oversized member is not in here - it
    refused the whole plan above.
    """
    writable = set(plan.writable)
    planned: List[PlannedEntry] = []
    excluded: List[dict] = []
    for name in plan.requested:
        if name in plan.junk:
            excluded.append(
                {"path": name, "reason": "OS metadata (junk); an extract never writes it"}
            )
            continue
        if name in plan.oversized or name in plan.missing:
            # Refused above; a plan cannot exist with one of these in it.
            continue
        if name not in writable:
            excluded.append(
                {"path": name, "reason": "an archive directory member, not a file"}
            )
            continue
        segments = _rendered_segments(name, twin_rel)
        if any(segment.startswith(".") for segment in segments):
            excluded.append(
                {"path": name, "reason": "a hidden name; the library scan prunes it"}
            )
            continue
        if library_scan.is_skipped_name(segments[-1]):
            excluded.append(
                {"path": name, "reason": "a file name the library scan skips"}
            )
            continue
        planned.append(PlannedEntry(entry=name, rel_path=posixpath.join(twin_rel, *segments)))
    return planned, excluded


def _rendered_segments(name: str, twin_rel: str) -> List[str]:
    """One member's rendered segments, refusing an escape from the twin.

    `entry_policy.entry_member_segments` is the same rule the extractor's
    `_member_rel` uses, so a member is looked for exactly where it lands, and a
    segment that renders to ``.`` or ``..`` refuses at 400 rather than being
    reported as a member that failed to resolve.
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


def listing_root(entries: Iterable[str]) -> Optional[str]:
    """The one directory every planned entry sits under, or ``None``.

    The listing's own shape, read off the entry paths: the first casing wins so
    the value can name a directory on disk without a second lookup, and ``None``
    is the honest answer when there is no single one - a loose file at the top
    level, or two different first segments.

    `bundle_detect.common_entry_root` computes a sibling of this for the
    detector's own purposes. It is not reused on purpose: this module's listing
    is the operator's selection, and the detector's root is a guess about a
    folder it did not choose.
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


def listing_groups(
    planned: Sequence[PlannedEntry], common_root: Optional[str]
) -> List[ListingGroup]:
    """The listing's buckets, with how many members each holds.

    Groups are read off the listing rather than from a detector because the
    listing is what the operator is shown: a member directly in the common root
    belongs to no subdirectory, so it is counted in the ``""`` bucket - the same
    convention `create-project`'s review uses for a file at a key's root, which
    is what keeps `sum(members) == the plan's members` a structural fact rather
    than a sentence. With no common root the entry's own first segment is its
    group, exactly as before.
    """
    counts: dict = {}
    for entry in planned:
        group = _group_of(entry.entry, common_root) or ""
        bucket = counts.setdefault(group, [0, 0])
        bucket[0] += 1
        if is_model_path(entry.rel_path):
            bucket[1] += 1
    return [
        ListingGroup(name=name, members=count, models=models)
        for name, (count, models) in sorted(counts.items())
    ]


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


def listing_fingerprint(entries: Iterable[str]) -> str:
    """A stable digest of the listing, so a later reader can spot a changed one.

    The sorted planned paths hashed, truncated: long enough to notice that an
    archive's contents changed between two runs, short enough to keep in a
    metadata column beside the counts it belongs to. It travels in the plan's
    review and the create's marker, and it is one of the reasons a plan over an
    archive that changed on disk is refused as stale rather than run.
    """
    listing = "\n".join(sorted(entries))
    return hashlib.sha256(listing.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]
