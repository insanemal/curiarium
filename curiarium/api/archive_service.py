"""Extract an archive that is already in the library, in place, into its twin.

`POST /import/zip` can only extract a zip whose bytes the server just fetched, so
an operator who `rsync`ed 169 vendor archives into ``import/`` had no door at
all: bundle detection reported every one of them `archive_only` forever. This
module is that door, and it writes into the same place the vendor would have:
``<parent>/<stem>`` next to ``<parent>/<stem>.zip``, which is exactly the pair
``bundle_detect.twin_key`` already recognises. The detector therefore needs no
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

import posixpath
import shutil
from pathlib import Path
from typing import List, Optional
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from fastapi import HTTPException

import audit
import bundle_detect
import entry_policy
import seams
import snapshot_guard
import snapshots
import zip_service

# Deliberate private import: `_move_without_clobber` is the existing no-clobber
# arbiter for this storage tree, and the point of reusing it is that an extract
# inherits its guarantee instead of growing a second one that can disagree.
from asset_service import _move_without_clobber
from schemas import ArchiveEntriesOut, ArchiveExtractOut, ZipEntryOut

ARCHIVE_SUFFIX = ".zip"


def entries_out(path: str) -> ArchiveEntriesOut:
    """One archive's entries, as the entries door reports them. Writes nothing."""
    normalized = _normalised(path)
    source = _source_archive(normalized)
    try:
        entries = zip_service.list_zip_entries(source)
    except BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid zip file") from None
    if not entries:
        raise HTTPException(status_code=400, detail="No files found in zip")
    return ArchiveEntriesOut(path=normalized, entries=[ZipEntryOut(**entry) for entry in entries])


def extract_out(
    path: str,
    entries: Optional[List[str]],
    ctx: Optional[audit.AuditContext],
) -> ArchiveExtractOut:
    """Extract an archive the library already holds into its twin directory.

    ``entries is None`` means every member the archive holds, normalised and
    de-duplicated: two raw members can normalise to one path, which can only be
    extracted once. The destination is the zip's own parent plus the sanitised
    stem, refused at 400 when that would be empty, hidden, or would not pair with
    the zip under bundle detection: a hidden twin is pruned by the scan and an
    unpaired one never becomes a bundle, so either would exist on disk and
    nowhere else.
    """
    normalized = _normalised(path)
    source = _source_archive(normalized)
    twin_rel = _twin_rel(normalized)
    try:
        with ZipFile(source) as zf:
            plan = entry_policy.plan_selection(zf, entries)
            if plan.missing:
                raise HTTPException(
                    status_code=400,
                    detail=f"Selection names entries that are not in the archive: {', '.join(sorted(plan.missing))}",
                )
            if plan.oversized:
                # All-or-nothing means an entry already declared over the per-file
                # limit refuses before staging exists, not after the stream trips
                # the same cap. A lying archive is still caught byte by byte below.
                raise HTTPException(
                    status_code=413,
                    detail=(
                        "Selected zip entries declare more than the per-file limit of "
                        f"{entry_policy.IMPORT_MAX_BYTES} bytes (IMPORT_MAX_MB): "
                        + ", ".join(sorted(plan.oversized))
                    ),
                )
            if not plan.writable:
                # A selection that can write nothing writes nothing: no twin
                # directory, no scan kick and no audit row, exactly like the
                # every-destination-exists no-op below. All of it was junk, or
                # directory-like members, and neither is a payload byte.
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
    scan_started = bool(seams.start_scan())

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


def _normalised(path: str) -> str:
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


def _source_archive(normalized: str) -> Path:
    """The archive this request names: must exist, be a file, and be a zip."""
    source = _assert_library_path(normalized)
    if not source.is_file():
        raise HTTPException(status_code=404, detail=f"{normalized!r} is not a file in the library")
    if source.suffix.lower() != ARCHIVE_SUFFIX:
        # The zip-import door says "Imported file is not a zip"; nothing was
        # imported here, so this door says what it actually checked.
        raise HTTPException(status_code=415, detail="Library file is not a zip")
    return source


def _twin_rel(normalized: str) -> str:
    """``<parent>/<sanitised stem>``: the directory a twin detector pairs with the zip.

    Building a plausible name is not enough: the whole point is that
    ``bundle_detect.twin_key`` pairs the zip with this directory, so the key it
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
    if bundle_detect.twin_key(twin_name) != bundle_detect.twin_key(archive_name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{normalized!r} cannot be extracted into a paired twin: {twin_rel!r} would be keyed "
                f"{bundle_detect.twin_key(twin_name)!r} but the archive is keyed "
                f"{bundle_detect.twin_key(archive_name)!r}, so bundle detection would never pair them "
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
