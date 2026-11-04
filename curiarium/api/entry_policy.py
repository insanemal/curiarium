"""The one policy for both zip doors: vocabulary, naming, budget and failure.

Five routes answer an archive question, and every one of them comes here:

* ``POST /import/zip`` - bytes the server just fetched, extracted into managed
  assets (the upload door);
* ``POST /import/zip/entries`` - that door's listing;
* ``POST /import/archive`` - an archive already in the library, extracted in
  place into its twin directory (the archive door);
* ``GET /import/archive/entries`` - that door's listing;
* ``POST /import/project`` - a project import whose zip branch calls the same
  ``zip_service.extract_zip_entries_to_assets`` as the upload door.

The vocabulary is case-folded and matched per normalised path *segment*:

* ``JUNK_SEGMENTS`` - ``__MACOSX`` and ``$RECYCLE.BIN``: a subtree, so every
  entry whose path holds such a segment is junk, however deep it sits;
* ``JUNK_BASENAMES`` - ``.DS_Store``, ``Thumbs.db``, ``desktop.ini``;
* ``JUNK_PREFIXES`` - ``._`` (AppleDouble sidecars) and ``.Spotlight-``
  (Spotlight indexes).

A segment that starts with a junk prefix makes that segment and everything
beneath it junk, so a directory literally named ``._foo`` is junk too. That is
deliberate: no real model directory is named like an AppleDouble sidecar, and
the children of one are sidecars as well.

No door this module governs can turn a junk entry into a folder, an asset row, a
project member or a cover: both extractors refuse it before a byte is
decompressed, and the two entries listings report the same verdict per member.
What this predicate does not govern is an ordinary file upload and a file already
on disk - the storage scan registers what it finds - so "junk can never become
an asset" is true of the archive-member route, not of the library as a whole.

Two narrower, pre-existing vocabularies stay where they are. This module does
not replace them and they are not wired to this predicate:

* ``bundle_detect.is_macosx_artifact`` matches only a *top-level* ``__MACOSX``
  segment, because bundle pairing and root detection ask the narrower question
  "is this entry outside the archive's payload root";
* ``library_scan.IGNORED_FILENAMES`` is a scan-level list of exactly three names
  for debris a filesystem walk meets, which is a different door.

This predicate is the broader one: it is the only vocabulary that decides what a
zip import may admit.

``curiarium/web/src/lib/zipJunk.ts`` is a hand-maintained mirror of this module,
and ``curiarium/api/tests/test_junk_vocabulary_mirror.py`` binds the two: it
compares the three tables and both test-vector sets, so drift fails a test
instead of shipping silently. The binding covers vocabulary and vectors, not the
matching logic: the mirror reads an empty path through its own ``(path || "")``
and has no ``None`` case at all, while ``is_junk_entry`` is total and answers
``False`` for ``None``. The browser uses the mirror to decide the import modal's
default ticks AND to refuse OS metadata from a locally picked zip, which is
uploaded as ordinary files through the plain upload route. Widen this module
first, then its mirror.

There is one *classification* of failure here, ``is_entry_failure``, used once,
by the upload/zip extractor: a name conflict or a member with no extension is
one bad entry, reported in ``failed``, while every other failure belongs to the
whole request. That is not per-entry tolerance for the archive door. The
in-library extract stays all-or-nothing - its 400/409/413 aborts are pinned by
21 tests - and never gains a ``failed`` list; "one failure model" here means one
classification used once.

This module also owns the budget: it reads both ``IMPORT_MAX_BYTES`` and
``IMPORT_ZIP_MAX_BYTES``, so no door does that arithmetic itself.
``plan_selection`` refuses a declared overshoot before a byte is written and
``stream_entry`` re-checks the bytes it actually reads, because the declaration
is metadata a crafted archive can lie about. Junk is in neither the declared
preflight nor the streamed total: both doors iterate ``EntryPlan.writable``, and
a junk member is not in it.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, List, Optional, Tuple
from zipfile import ZipFile, ZipInfo

from fastapi import HTTPException

from asset_service import AssetFilenameError
from config import IMPORT_MAX_BYTES, IMPORT_ZIP_MAX_BYTES
from file_utils import sanitize_filename

# Mf-ls9v, the six rules in one table so a reader can count them:
#   1. a segment equal to __MACOSX        2. a segment equal to $RECYCLE.BIN
#   3. a segment equal to .DS_Store       4. a segment equal to Thumbs.db
#   5. a segment equal to desktop.ini     6. a segment starting with ._ or .Spotlight-
JUNK_SEGMENTS = frozenset({"__macosx", "$recycle.bin"})
JUNK_BASENAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini"})
JUNK_PREFIXES = ("._", ".spotlight-")


def is_junk_entry(path: str) -> bool:
    """Whether this zip entry path is OS metadata rather than one of its files.

    Total on any input: an empty or missing path is not junk, and nothing but
    separators and case is normalised away. A zip member name is not guaranteed
    to use ``/`` (a hand-made archive can carry ``x\\__MACOSX\\y.stl``), so both
    separators split it, and empty segments between them are ignored.
    """
    if not path:
        return False
    for segment in path.replace("\\", "/").split("/"):
        if not segment:
            continue
        folded = segment.casefold()
        if folded in JUNK_SEGMENTS or folded in JUNK_BASENAMES:
            return True
        if folded.startswith(JUNK_PREFIXES):
            return True
    return False


def normalize_zip_entry_path(name: str) -> Optional[str]:
    cleaned = (name or "").replace("\\", "/").lstrip("/")
    if not cleaned or cleaned.endswith("/"):
        return None
    parts = [part for part in cleaned.split("/") if part]
    if not parts or any(part == ".." for part in parts):
        return None
    parts = [part for part in parts if part != "."]
    return "/".join(parts)


class EntryPathRefused(ValueError):
    """One member whose rendered path segment would leave the destination.

    ``name`` is the member as the archive spells it and ``segment`` is what the
    renderer made of one of its segments (``"."`` or ``".."``), so the caller can
    name both in its 400.
    """

    def __init__(self, name: str, segment: str) -> None:
        super().__init__(f"{name!r} has a path segment that renders to {segment!r}")
        self.name = name
        self.segment = segment


def entry_filename(name: str) -> str:
    """The filename a member lands under: its last segment, rendered.

    The last segment only, because ``sanitize_filename`` replaces the separators
    with ``_`` *and then* takes a basename, so handing it the whole member path
    would basename the separators away and flatten the name.
    """
    return sanitize_filename(name.split("/")[-1])


def entry_member_segments(name: str) -> List[str]:
    """One member's path, each segment rendered by the library's filename rule.

    ``file_utils.sanitize_filename`` is the library's one filename renderer, so a
    member lands exactly as it renders it. Per segment on purpose: applied to the
    whole member name it would basename the separators away and flatten the
    archive's tree into one directory, so each segment keeps its own place.

    Rendering is also the step that can create a ``.`` or ``..`` segment, and one
    of those is refused here, before any destination is resolved and before
    staging exists. ``sanitize_filename`` strips whitespace and NULs before taking
    the basename, so ``".. "``, ``" .."``, ``"\\t.."`` and ``"..\\x00"`` all
    render to ``".."`` while ``" . "`` renders to ``"."``.
    ``normalize_zip_entry_path`` rejects an *exact* ``".."`` part, which is a
    different set: these spellings survive it, and joined into a destination they
    would walk out of the twin (``twin/../evil.stl``) or collapse the twin itself
    into the member's file. Refusing keeps the promise that a member lands
    exactly where its segments render - ``sanitize_path_segment``'s dot-stripping
    fallback would silently rename the member instead, leaving a file whose name
    the archive does not have.
    """
    segments = [sanitize_filename(part) for part in name.split("/")]
    for segment in segments:
        if segment in (".", ".."):
            raise EntryPathRefused(name, segment)
    return segments


@dataclass(frozen=True)
class EntryPlan:
    """What one entry selection resolves to, before a single byte is decompressed.

    ``members`` is every normalised member of the archive, ``requested`` is the
    request's picks after normalisation and de-duplication, and ``writable`` is
    what both extractors iterate: ``requested`` minus junk, oversized, missing
    and directory-like members, in request order.

    ``junk`` is every requested name ``is_junk_entry`` calls OS metadata.
    ``oversized`` are the picks whose *declared* size is over
    ``IMPORT_MAX_BYTES`` among the non-junk ones, and ``missing`` are the picks
    the archive does not hold. The declaration is metadata a crafted archive can
    lie about, so this plan rejects the obviously-too-big before any write and
    ``stream_entry`` re-checks the bytes it actually reads.

    Junk is left out of the declared preflight (a junk-only selection must not
    413 on a cumulative cap) and out of ``writable`` (a junk-only selection must
    write nothing at all), but stays in ``requested``: the upload door reports
    every non-writable pick in its ``failed`` list.
    """

    members: dict[str, ZipInfo]
    requested: Tuple[str, ...]
    writable: Tuple[str, ...]
    junk: FrozenSet[str]
    oversized: FrozenSet[str]
    missing: FrozenSet[str]


def _requested_names(members: dict[str, ZipInfo], selections: Optional[List[str]]) -> Tuple[str, ...]:
    """The request's picks, normalised and de-duplicated, in request order.

    ``selections is None`` means every member, in archive order. De-duplication
    is deliberate: two raw members can normalise to one path (``/a.stl`` and
    ``a.stl``) and only one file can be written there. ``list_zip_entries``
    reports the raw members instead, so an entries listing can show one such path
    twice where this returns it once.
    """
    if selections is None:
        return tuple(members)
    requested: List[str] = []
    seen: set[str] = set()
    for raw in selections:
        name = normalize_zip_entry_path(raw)
        if not name or name in seen:
            continue
        seen.add(name)
        requested.append(name)
    return tuple(requested)


def plan_selection(zf: ZipFile, selections: Optional[List[str]]) -> EntryPlan:
    """Resolve one request's entries and refuse an empty or over-budget selection.

    Shared by both extractors so the two doors cannot disagree about what a
    selection means or when it is too big. The details are the exact strings the
    zip-import route has always answered with, because existing clients depend on
    them.

    Junk is classified FIRST and never consults ``IMPORT_MAX_BYTES``: OS metadata
    is refused whatever its size, so an oversized junk member can neither refuse
    the request nor spend a byte of the budget.
    """
    members: dict[str, ZipInfo] = {}
    for info in zf.infolist():
        name = normalize_zip_entry_path(info.filename)
        if not name:
            continue
        members[name] = info
    requested = _requested_names(members, selections)
    if not requested:
        raise HTTPException(status_code=400, detail="No zip entries selected")
    junk = frozenset(name for name in requested if is_junk_entry(name))
    oversized: set[str] = set()
    declared = 0
    for name in requested:
        if name in junk:
            continue
        info = members.get(name)
        if info is None or info.is_dir():
            continue
        size = int(info.file_size or 0)
        if size > IMPORT_MAX_BYTES:
            oversized.add(name)
            continue
        declared += size
    if declared > IMPORT_ZIP_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Selected zip entries expand to {declared} bytes; "
                f"the limit is {IMPORT_ZIP_MAX_BYTES} bytes (IMPORT_ZIP_MAX_MB)"
            ),
        )
    missing = frozenset(name for name in requested if name not in members)
    writable = tuple(
        name
        for name in requested
        if name not in junk and name not in oversized and name not in missing and not members[name].is_dir()
    )
    return EntryPlan(
        members=members,
        requested=requested,
        writable=writable,
        junk=junk,
        oversized=frozenset(oversized),
        missing=missing,
    )


class EntryTooLarge(ValueError):
    """One member's own bytes crossed ``IMPORT_MAX_BYTES`` while streaming.

    A ``ValueError`` on purpose: the pre-existing extractor treats any
    non-``HTTPException`` failure as one bad entry (``failed``) rather than a bad
    request, and that contract must not change.
    """


class BudgetExceeded(Exception):
    """This member's bytes crossed the selection's remaining budget.

    ``total_written`` is the selection's cumulative total, including what this
    member had written when it crossed, because ``stream_entry`` is handed the
    running total and returns it.
    """

    def __init__(self, total_written: int) -> None:
        super().__init__(f"selected zip entries expand beyond {total_written} bytes")
        self.total_written = total_written


STREAM_CHUNK_BYTES = 1024 * 1024


def stream_entry(zf: ZipFile, info: ZipInfo, dest: Path, *, spent_bytes: int) -> int:
    """Stream one member into ``dest`` and return the selection's new total.

    Both caps are enforced on the bytes, never on the declaration: the member's
    own size against ``IMPORT_MAX_BYTES`` (``EntryTooLarge``) and what is left of
    the selection's budget, given the cumulative ``spent_bytes`` already written
    (``BudgetExceeded``). The chunk size is fixed so a lying archive cannot make
    the copy allocate its real size up front.
    """
    size = 0
    with zf.open(info) as src, open(dest, "wb") as out:
        while True:
            chunk = src.read(STREAM_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > IMPORT_MAX_BYTES:
                raise EntryTooLarge("Extracted file exceeds size limit")
            if spent_bytes + size > IMPORT_ZIP_MAX_BYTES:
                raise BudgetExceeded(spent_bytes + size)
            out.write(chunk)
    return spent_bytes + size


def is_entry_failure(error: BaseException) -> bool:
    """Whether this failure belongs to one zip entry rather than the whole request.

    One classification, used once, by the upload/zip extractor: a name conflict
    or a member with no extension is that entry's defect and goes in ``failed``.
    The in-library archive door is atomic and never gains per-entry tolerance, so
    it never asks.
    """
    return isinstance(error, AssetFilenameError) or (isinstance(error, HTTPException) and error.status_code == 409)
