"""Discover files that were put into the managed storage root by other tools.

This is the first half of making folders filesystem-authoritative: the library
is scanned as it exists on disk, and matching assets are registered *where they
lie* instead of being re-templated into a new location. Folders are created from
the directory tree, so ``rsync``/``cp`` followed by "rescan" is a supported way
to add files.

Missing files are flagged, never purged:

* a scan sets ``missing_since`` when it first notices that an asset's file is
  gone and clears it when the file reappears. The timestamp is when the scan
  noticed the absence, not when the file disappeared;
* rows and files are never deleted, moved or renamed by a scan - losing a row
  because a network mount hiccuped would be worse than showing a stale entry;
* ``disk_mtime`` stays reserved and unwritten; ``is_reference`` is written by
  mount import (migration 004 / Mf-k0kf) and a scan leaves it untouched;
* ``thumbs/`` and ``bundles/`` are app-owned and skipped.

The flag scope is wider than the ``missing`` counter scope: reference rows
(``source_path`` set) are flagged when their source is gone, but only managed
rows (``source_path`` NULL, ``storage_path`` set) are counted in ``missing``.
The flag answers "are the bytes gone?" and deliberately does not apply
``main.is_mount_source_allowed``: a source that exists but is outside the
current mount allowlist is present on disk and stays unflagged, even though
``/file`` will still refuse to serve it. Degenerate rows with both
``storage_path`` and ``source_path`` NULL are skipped by the candidate query.
Scanning a non-standard root (as the tests do) flags every managed asset that
is absent from that root. Thumbnails are untouched: a cached sidecar keeps
rendering, and ``/thumb`` / ``/file`` return 404 (never 500) when the bytes are
gone.

How a scan behaves:

* the tree is walked iteratively, one directory at a time, so peak memory is a
  single directory's entry list plus its accepted-file list, the batch being
  registered and the set of relative paths already known - not the whole tree of
  rows;
* rows are committed in batches of ``BATCH_SIZE``. An interruption (a crash, a
  killed process, a raised error) leaves whole committed batches behind and the
  in-flight batch rolled back, so a rescan picks up exactly where the last one
  stopped;
* counters are per run and describe this scan only. ``registered`` counts rows
  that were actually committed; a row that failed and was rolled back is
  ``failed``, never ``registered``;
* folder rows are created only for a directory that had a file to register in
  this run - never eagerly on directory entry. A walk over real directories (the
  storage scan, mount copy mode) gives its folder rows the real relative path,
  because folder rename and delete have to move the real directory; a reference
  walk leaves it ``None`` because the bytes live outside storage. A row that
  already owns the directory is *adopted* instead of duplicated, and the
  asset-evidence backfill below heals migrated rows the walk never touches. A
  name-matched row with no path whose directory another row already owns keeps
  its NULL path: the file still registers against it, and the existing owner
  keeps the directory, so neither write is forced through the unique index;
* prepared-print metadata is inspected inline on a best-effort basis; a file
  that cannot be read registers anyway and is not counted as a failure (the
  startup backfill retries it);
* a display name is derived per directory before the rows go pending: a stem
  that is unique in its directory keeps the bare stem, two or more nameable
  files sharing a stem split by extension (``part (stl)`` / ``part (3mf)``), and
  a suffix the library does not recognise as a model file keeps its whole
  filename (``multifilament.s#5``). The derivation is a pure function of the
  directory listing plus the folder's already-taken names, read once per
  directory, so the same tree gets the same names on every scan and a batch
  boundary cannot change a name (Mf-tkdp);
* an existing row is never renamed: a name taken before this rule existed (a bare
  ``multifilament``) stays as it is and its siblings step around it;
* a row that still cannot be inserted (a concurrent upload took the name between
  the read and the insert) fails exactly that one row with ``"<rel_path>:
  <error>"`` while the rest of the batch still lands.

Every run is its own state object:

* a run is created once, published once as the current run, and every counter
  and failure note is written through that instance - never through a reused
  counter bag that a later run resets out from under an earlier one;
* the first ``MAX_REPORTED_FAILURES`` (100) failures of a run are both logged
  (one WARNING each, ``"<rel_path>: <ExcClass>: <exc>"``) and captured on the
  run; every failure after that is still counted, but neither logged
  individually nor captured. ``failed == len(errors) + errors_omitted`` always
  holds, so a client can show "the first N of M" without the counts disagreeing;
* a run that still holds the slot refuses a second one: ``start_scan`` returns
  False and a direct ``scan_storage_root`` raises ``ScanAlreadyRunning``. A
  finished run stays the current one until the next run replaces it, because the
  web client keys "the scan just finished" off ``finished_at``;
* a run logs exactly one summary line when it ends, and an aborted run logs one
  too - its counters plus a recorded ``scan aborted: ...`` failure.

Three registration writers share this machinery: the storage scan
(``scan_storage_root``, which also owns the missing-flag pass and the
asset-evidence folder backfill), mount import (``mount_import`` via
``register_tree``) and the explicit ``register(paths)`` door (extract-and-create,
whose members are the archive listing's planned paths). They are serialised by
``_write_lock``, because all three create folder rows. Only the storage scan
flags files missing, so pointing a registration walk at a mount can never mark
the library missing. ``register`` takes an explicit list: it never walks for
discovery, so an unnamed file sitting beside a named one gets no row, and it
never flags - it clears ``missing_since`` on the paths it verified present and
stamps nothing. Mount import is not a backfill caller: its asset paths are
relative to the mount root, not the storage root.

The storage scan itself has two entry points. ``scan_storage_root`` walks the
whole root and heals folder paths from asset evidence afterwards;
``scan_subtree(scope)`` walks one folder inside it and deliberately does NOT
call that backfill, because ``backfill_paths_from_asset_dirs`` reasons over every
folder and asset row in the library - discovery outside the scope the run
declared. ``scan_subtree('')`` delegates to ``scan_storage_root``, so there is
one implementation of a whole-root run.
"""
import json
import logging
import os
import posixpath
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path, PurePath
from typing import Callable, Dict, Iterable, Iterator, List, NamedTuple, Optional, Sequence, Tuple

from sqlalchemy import or_, update
from sqlmodel import Session, select

import folder_paths
from asset_service import ensure_model_thumbnail, folder_name_index, save_thumb
from config import DEFAULT_MOUNT_IMPORT_EXTS, IMPORT_ALLOWED_EXTS
from db import STORAGE, engine
from file_utils import guess_mime_from_path
from models import Asset, Folder
from prepared_print import inspect_prepared_print
from zip_service import sanitize_folder_name

log = logging.getLogger(__name__)

# App-owned directories inside the storage root. Never library content.
RESERVED_DIRS = {"thumbs", "bundles", ".incoming"}
# OS/tool debris that is never a library item.
IGNORED_FILENAMES = {"Thumbs.db", "desktop.ini", ".DS_Store"}
# A name starting with this is hidden. Hidden FILE names are skipped unless the
# caller asked for them; hidden DIRECTORIES are pruned by the walk itself.
HIDDEN_NAME_PREFIX = "."
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
# Extensions whose stem is a usable display name. This is a snapshot of the
# module constant on purpose - NOT parse_mount_import_exts(MOUNT_IMPORT_EXTS_RAW):
# a deployment's extension override decides what mount import copies, and must
# never change what a file is called.
NAMING_EXTS = frozenset(DEFAULT_MOUNT_IMPORT_EXTS)

# Rows are committed in batches so one run does not hold a write transaction for
# the whole tree, and so a long scan leaves a usable partial state.
BATCH_SIZE = 200
# Three batches in a row that cannot commit means the database is not coming
# back on its own; stop instead of hammering it.
MAX_CONSECUTIVE_BATCH_FAILURES = 3

# Failures are both logged and captured, and both are bounded by the same cap so
# the two cannot disagree: `failed == len(errors) + errors_omitted`.
MAX_REPORTED_FAILURES = 100


# --- bundle-member vocabulary ---
# The vocabulary for what a bundle is made of (Mf-1agz): the role tokens, the
# suffix tables, the entry predicates and the pure name helpers. It lives in the
# scan because the scan is one of the doors that asks it - a bundle member is a
# file this module registers, a model is a model by the suffix table the import
# door accepts - and because `bundle_detect` imports every name below and
# re-exports it, so neither module keeps a second copy of a shared rule.

# The model extensions the import door accepts, minus the archive: a `.zip` is a
# container, never a bundle member. This is NOT `file_utils.MODEL_SUFFIXES`, which
# is the five mime-backed suffixes `is_model_path` answers with: the detect side
# also carries .lbrn/.lbrn2 and omits .obj.
MODEL_SUFFIXES = frozenset(IMPORT_ALLOWED_EXTS - {".zip"})
# Prose that can carry provenance. Deliberately short: a `.pdf` is a document,
# not a sibling README. This is the BUNDLE-MEMBER text set, narrower than any
# document set on purpose: every extension here is one a vendor ships beside a
# model.
BUNDLE_TEXT_EXTS = frozenset({".txt", ".md", ".pdf", ".html", ".htm", ".rtf"})
# The role an entry plays inside a bundle.
MODEL, IMAGE, TEXT, ARCHIVE, OTHER = "model", "image", "text", "archive", "other"


@dataclass(frozen=True)
class ArchiveIdentity:
    """One archive's path, byte size and content digest."""

    rel_path: str
    size: int
    digest: str


@dataclass(frozen=True)
class DuplicateGroup:
    """Byte-identical archives: one bundle, the rest reclaimable bytes."""

    digest: str
    size: int
    paths: Tuple[str, ...]  # authoritative (shortest name) first

    @property
    def reclaimable_bytes(self) -> int:
        return self.size * (len(self.paths) - 1)


def archive_order(rel_path: str) -> Tuple[int, str, str]:
    """The order that puts the original before a browser's `` (1)`` duplicate.

    A duplicate-download suffix only ever appends characters, so the shortest name
    is the original file; ties fall back to casefolded then raw text.
    """
    return (len(rel_path), rel_path.casefold(), rel_path)


def equal_size_archive_groups(archives: Iterable[Tuple[str, int]]) -> Tuple[Tuple[int, Tuple[str, ...]], ...]:
    """Sizes shared by two or more archives - the only archives worth hashing.

    Byte-identical archives are necessarily equal-size, so a size that occurs once
    cannot be a duplicate and its bytes are never read. The cost is one hash per
    archive in a shared-size group, never one per archive in the library.
    """
    by_size: Dict[int, List[str]] = {}
    for rel_path, size in archives:
        by_size.setdefault(size, []).append(rel_path)
    return tuple(
        (size, tuple(sorted(paths, key=archive_order)))
        for size, paths in sorted(by_size.items())
        if len(paths) > 1
    )


def duplicate_groups(archives: Iterable[ArchiveIdentity]) -> Tuple[DuplicateGroup, ...]:
    """Byte-identical archives grouped once; a lone digest produces no group."""
    by_identity: Dict[Tuple[str, int], List[str]] = {}
    for archive in archives:
        by_identity.setdefault((archive.digest, archive.size), []).append(archive.rel_path)
    groups = [
        DuplicateGroup(digest=digest, size=size, paths=tuple(sorted(paths, key=archive_order)))
        for (digest, size), paths in by_identity.items()
        if len(paths) > 1
    ]
    return tuple(sorted(groups, key=lambda group: archive_order(group.paths[0])))


def duplicate_archive_paths(groups: Sequence[DuplicateGroup]) -> Tuple[str, ...]:
    """Every archive a group reports as a copy: all but its authoritative path."""
    return tuple(path for group in groups for path in group.paths[1:])


def duplicate_report_text(groups: Sequence[DuplicateGroup]) -> str:
    """The block an operator copies out: one tab-separated line per archive.

    Header lines carry the digest and the group's reclaimable bytes; every archive
    line carries its path and byte size, so the text is actionable with no app.
    """
    if not groups:
        return ""
    total = sum(group.reclaimable_bytes for group in groups)
    lines = [f"# duplicate archives: {len(groups)} group(s), {total} bytes reclaimable"]
    for group in groups:
        lines.append(f"# sha256 {group.digest} size {group.size} reclaimable {group.reclaimable_bytes}")
        lines.append(f"keep\t{group.size}\t{group.paths[0]}")
        for path in group.paths[1:]:
            lines.append(f"duplicate\t{group.size}\t{path}")
    return "\n".join(lines)


def _is_text(rel_path: str, size: int) -> bool:
    return classify_entry(rel_path, size) == TEXT


def classify_entry(rel_path: str, size: int) -> str:
    """The role of one entry, decided by its extension and nothing else.

    ``size`` is accepted for signature symmetry with the callers that already
    hold it; it never changes the answer. The stem is never consulted: two files
    sharing a stem are two files, and a role is a property of the path.
    """
    suffix = PurePath(rel_path).suffix.lower()
    if suffix == ".zip":
        return ARCHIVE
    if suffix in MODEL_SUFFIXES:
        return MODEL
    if suffix in IMAGE_SUFFIXES:
        return IMAGE
    if suffix in BUNDLE_TEXT_EXTS:
        return TEXT
    return OTHER


def is_counted_entry(rel_path: str) -> bool:
    """Whether the library scan would REGISTER this entry at all.

    The scan's rule is the basename predicate alone, asked here through the very
    function ``_register_tree`` calls, so the two doors cannot drift apart
    (Mf-94od). Deliberately NOT ``entry_policy.is_junk_entry`` and NOT
    ``is_macosx_artifact``: the scan REGISTERS a non-dot file under a nested
    ``__MACOSX/`` (measured: ``g/__MACOSX/g/plain.stl`` registers), and the junk
    predicate would UNDER-count a part the project really holds. Classifying a
    ``._a.stl`` sidecar through the junk vocabulary would also miss a plain
    ``.hidden.stl``, which the junk predicate calls not junk at all.

    It asks the scan's DEFAULT door - a hidden name is skipped. The mount door can
    be opened with ``IMPORT_MOUNT_INCLUDE_HIDDEN=1``, and then the scan registers
    names this predicate still refuses; that conditional is named in ADR 0010
    Decision 25 rather than hidden here, because the detector never sees the
    door's flag and a parameter nobody passes would read as support it does not
    have.
    """
    return not is_skipped_name(PurePath(rel_path).name)


def is_counted_model(rel_path: str, size: int) -> bool:
    """A MODEL the scan would register: the model count, through both doors.

    ``classify_entry`` stays SUFFIX-ONLY on purpose (Mf-tkdp's stem rule): a role
    is a property of the path. This composite is what the counting sites ask, so
    an AppleDouble ``._a.stl`` beside ``a.stl`` names no model.
    """
    return classify_entry(rel_path, size) == MODEL and is_counted_entry(rel_path)


# Two private patterns the name helpers below read: the ordinal a site puts first
# in a directory name, and the Thingiverse id in that same position.
_LEADING_ORDINAL = re.compile(r"^\d+[_-]+")
# A Thingiverse export roots its own entries at `NNNNNNN_Title`, so a directory
# carrying that shape carries provenance. Four digits, because three-digit runs
# are common in ordinary names; a mandatory `_`, because `2024-10-31_backup` is
# a date and not an export; `^`-anchored, because `my_5390184_thing` is the
# operator's own naming and predates the export.
_THINGIVERSE_ID_IN_NAME = re.compile(r"^(\d{4,})_")


def display_name(name: str) -> str:
    """A human name from a vendor directory name; never empty.

    A leading ordinal (``2493386_``) is a site's id, not a name, so it is
    dropped; underscores become spaces and whitespace is collapsed. Nothing else
    is rewritten - ``-No_overhang`` keeps its dash, because a dash is content and
    "Moai statue -No overhang" is a better title than a guessed rewrite.
    """
    raw = (name or "").strip()
    stripped = _LEADING_ORDINAL.sub("", raw).replace("_", " ")
    cleaned = " ".join(stripped.split())
    if cleaned:
        return cleaned
    return " ".join(raw.replace("_", " ").split()) or "bundle"


def thingiverse_id_from_name(name: str) -> Optional[str]:
    """The Thingiverse id a vendor export puts first in its name, or ``None``."""
    match = _THINGIVERSE_ID_IN_NAME.match(name)
    return match.group(1) if match else None


class ScanAlreadyRunning(RuntimeError):
    """A second storage scan was started while one still holds the slot."""


# The sentence the deleted route answered with; the exception's own message stays
# lowercase because it is a log/step detail, not the operator-facing refusal.
ALREADY_RUNNING_DETAIL = "A library scan is already running"


class ScanScopeError(ValueError):
    """A scan scope that is not a folder inside the library root."""


def normalise_scope(raw: str) -> str:
    """Normalise a library-relative folder path, or refuse it before any walk.

    A scope of ``''`` - blank, or only slashes and whitespace - is the whole
    library, so a root scan and a scoped scan share one spelling of "the root".
    Anything else is a folder INSIDE the library: an absolute path, an empty,
    ``.`` or ``..`` segment, or a backslash is refused, because each of those
    would let the walk start outside the root or join a path the operator did not
    review. The answer is the stripped library-relative path, no leading or
    trailing slash.
    """
    text = (raw or "").strip()
    if not text.strip("/"):
        return ""
    if text.startswith("/") or "\\" in text:
        raise ScanScopeError(
            "a scan scope must be a folder inside the library: '' scans the whole library"
        )
    scope = text.strip("/")
    if any(part in ("", ".", "..") for part in scope.split("/")):
        raise ScanScopeError(
            "a scan scope must be a folder inside the library: '' scans the whole library"
        )
    return scope


@dataclass
class ScanState:
    running: bool = False
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    registered: int = 0      # files newly added to the library
    known: int = 0           # files already tracked (nothing to do)
    ignored: int = 0         # hidden files, app-owned dirs, debris
    missing: int = 0         # tracked files that are no longer on disk
    failed: int = 0          # files that could not be registered
    errors: List[str] = field(default_factory=list)
    errors_omitted: int = 0  # failures counted but not captured/logged (past the cap)

    def as_dict(self) -> dict:
        """The wire view: every captured text, and no internal-only fields.

        ``errors`` is *not* truncated. ``failed`` is the total and
        ``len(errors)`` is what is shown, which is the pair a client needs; the
        old ``errors[-20:]`` slice silently served 20 of 67 and made the counts
        look wrong (Mf-e8zp). ``errors_omitted`` stays internal for that reason.
        """
        data = asdict(self)
        data.pop("errors_omitted", None)
        return data


@dataclass(eq=False)
class PendingAsset:
    """A file found on disk that is not tracked yet.

    Compared by identity (``eq=False``) so a batch can drop the rows it rejected.
    """

    asset: Asset
    rel_path: str
    rel_dir: str


@dataclass
class TreePlan:
    """What one registration walk over ``root`` registers, and how.

    The walk and the batch machinery are shared; everything that differs between
    the storage scan and a mounted source lives here. Callables are supplied by
    the caller so this module keeps no policy of its own beyond the walk.
    """

    root: Path
    tracked: set  # dedupe keys already in the database
    keys_for: Callable[[str], Sequence[str]]  # relative path -> its dedupe keys
    accept: Callable[[str, int], bool]  # relative path + size -> register it?
    build: Callable[[str, int, str], Asset]  # relative path + size + derived name -> unsaved row
    dest_root: Optional[Path] = None  # copy target; None registers files in place
    prepare: Optional[Callable[[Session, List[PendingAsset], dict], None]] = None
    include_hidden: bool = False
    # The library-relative directory the walk starts at; ``''`` walks the whole
    # root. Relative paths stay relative to the ROOT, not the scope, so a scoped
    # walk's names, folder rows and storage paths are identical to a root scan's.
    scope: str = ""
    # True when every folder this walk creates stands for a real directory under
    # the storage root, so its row must carry that path (the storage scan, and
    # mount copy mode). False for a reference walk, whose folders are grouping
    # only: the bytes live outside storage, so there is no directory to point at.
    folders_are_directories: bool = True


# The most recently started run. Never a reused counter bag: a run is created
# once, published once here, and every mutation goes through that instance.
_current: Optional[ScanState] = None
_state_lock = threading.Lock()

# Only one registration run writes at a time: folder rows are looked up and then
# created, and there is no unique index on (name, parent_id), so two writers can
# duplicate a whole branch. Lock order is always _write_lock -> _state_lock.
_write_lock = threading.Lock()
# Run-scoped counters and plan for the walk active in this thread. The batch
# helpers below are entered from several call sites with frozen signatures, so
# the active run is resolved here instead of being threaded through arguments.
_local = threading.local()


def _active_state() -> Optional[ScanState]:
    """The run owning this thread, else the current run; None before any run.

    The plain read of ``_current`` is deliberate: it is only rebound under
    ``_state_lock``, and any consistent run is an acceptable answer to "where do
    these counters go".
    """
    state = getattr(_local, "state", None)
    if state is not None:
        return state
    return _current


def _active_plan() -> Optional["TreePlan"]:
    """The plan of the registration run active in this thread, if any."""
    return getattr(_local, "plan", None)


def scan_status() -> dict:
    """The current (or last finished) run, or an empty run before the first one."""
    with _state_lock:
        current = _current
    return ScanState().as_dict() if current is None else current.as_dict()


def start_scan() -> bool:
    """Start a background scan. Returns False when one is already running."""
    try:
        run = _begin_run(None)
    except ScanAlreadyRunning:
        return False
    threading.Thread(target=_run_scan, args=(run,), name="library-scan", daemon=True).start()
    return True


def _begin_run(state: Optional[ScanState]) -> ScanState:
    """Claim the single run slot and return the run this call owns.

    A fresh ``ScanState`` starts at zero, which is why a run never has to reset
    another run's counters: the old in-place reset was what let one scan zero the
    numbers of the scan whose status the client was still reading.
    """
    global _current
    with _state_lock:
        if _current is not None and _current.running and _current is not state:
            raise ScanAlreadyRunning("a library scan is already running")
        run = state if state is not None else ScanState()
        run.running = True
        if run.started_at is None:
            run.started_at = time.time()
        run.finished_at = None
        _current = run
        return run


def _run_scan(run: ScanState) -> None:
    """The background entry point: a daemon thread has nowhere to re-raise."""
    try:
        scan_storage_root(state=run)
    except ScanAlreadyRunning:
        # another caller claimed the slot first; this run never walked anything
        pass
    except Exception:
        # already counted on the run and logged by _note_failure
        pass


def note_error(message: str) -> None:
    """Count one failed file against the run that owns this thread, and log it."""
    state = _active_state()
    if state is None:
        # no run owns it: never swallow a failure just because it is uncounted
        log.warning("%s", message)
        return
    _note_failure(state, message)


def _note_failure(state: ScanState, message: str) -> None:
    """Count one failed file, capture it, and log it - all under one cap.

    The cap governs the log and the captured list together, so
    ``failed == len(errors) + errors_omitted`` always holds.
    """
    log_message = False
    capped = False
    with _state_lock:
        state.failed += 1
        if len(state.errors) < MAX_REPORTED_FAILURES:
            state.errors.append(message)
            log_message = True
            capped = len(state.errors) == MAX_REPORTED_FAILURES
        else:
            state.errors_omitted += 1
    if log_message:
        log.warning("%s", message)
    if capped:
        log.warning(
            "library scan: reporting the first %d failures only; further failures are not logged individually",
            MAX_REPORTED_FAILURES,
        )


def _note_batch_failure(rel_paths: Sequence[str], exc: Exception) -> None:
    """Every file in a rolled-back batch failed; one note each, same format."""
    detail = f"{type(exc).__name__}: {exc}"
    state = _active_state()
    for rel_path in rel_paths:
        if state is None:
            log.warning("%s: %s", rel_path, detail)
        else:
            _note_failure(state, f"{rel_path}: {detail}")


def _finish_run(run: ScanState) -> None:
    """Close the run and log exactly one summary line for it.

    In a ``finally`` so an aborted run gets its one line too; it never swallows
    the abort, which is the caller's to re-raise.
    """
    with _state_lock:
        run.running = False
        run.finished_at = time.time()
        duration = run.finished_at - run.started_at if run.started_at is not None else 0.0
    log.log(
        logging.WARNING if run.failed else logging.INFO,
        "library scan finished in %.1fs: registered=%d known=%d ignored=%d missing=%d failed=%d",
        duration, run.registered, run.known, run.ignored, run.missing, run.failed,
    )


def _walk_storage(
    root: Path, *, include_hidden: bool = False, start_rel: str = ""
) -> Iterator[Tuple[str, List[os.DirEntry], int]]:
    """Yield ``(rel_dir, file_entries, ignored_entries)`` per directory.

    Iterative on purpose: a deep tree must not hit the recursion limit. One
    directory is read at a time, so peak memory is that directory's entry list
    plus one pending pair per discovered directory.
    ``file_entries`` are the regular, non-symlink entries; the caller applies
    library policy (hidden names) and does the single ``stat`` per file.

    Directories that cannot be read are skipped, exactly like ``os.walk`` with
    the default ``onerror``. Symlinked directories and the root-level app-owned
    directories are always pruned silently and not counted; so are hidden
    directories, unless the caller passes ``include_hidden=True`` for a source
    whose dot-directories are real content.

    ``start_rel`` seeds the stack at one directory inside ``root`` instead of the
    root itself, while the yielded ``rel_dir`` stays library-relative: a scoped
    walk reports ``scope/child``, not ``child``.
    """
    stack: List[Tuple[str, Path]] = [(start_rel, root / start_rel if start_rel else root)]
    while stack:
        rel_dir, directory = stack.pop()
        try:
            with os.scandir(directory) as scan:
                entries = list(scan)
        except OSError:
            continue
        # readdir order must never leak into a tie-break: names are derived per
        # directory and the order files are named in has to be a function of the
        # tree, not of the filesystem.
        entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))

        file_entries: List[os.DirEntry] = []
        ignored = 0
        for entry in entries:
            try:
                if entry.is_symlink():
                    if entry.is_dir():
                        # a symlinked directory leads outside the root
                        continue
                    ignored += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not include_hidden and entry.name.startswith("."):
                        continue
                    if rel_dir == "" and entry.name in RESERVED_DIRS:
                        continue
                    child_rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                    stack.append((child_rel, Path(entry.path)))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    ignored += 1
                    continue
            except OSError:
                continue
            file_entries.append(entry)
        yield rel_dir, file_entries, ignored


def iter_directories(root: Path, *, include_hidden: bool = False) -> Iterator[Tuple[str, List[os.DirEntry], int]]:
    """Yield ``(rel_dir, file_entries, ignored_entries)`` for a read-only walk.

    A read-only caller outside this module (bundle detection) wants the library's
    own pruning - ``RESERVED_DIRS``, hidden names, symlinked directories - without
    importing the underscore name it lives behind. Same generator, same policy,
    one implementation: this is a documented alias, not a second walker.
    """
    return _walk_storage(root, include_hidden=include_hidden)


def is_skipped_name(name: str, *, include_hidden: bool = False) -> bool:
    """Whether the scan refuses a FILE NAME outright, before any other policy.

    This is the one rule both doors ask: ``_register_tree`` calls it to decide
    what to register, and ``bundle_detect`` calls it to decide what counts as a
    model, so a Mac-made tree's AppleDouble ``._a.stl`` sidecars cannot count on
    one side and be skipped on the other (Mf-94od).

    It is deliberately a predicate over a BASENAME, not a rel path: the scan's
    own rule is exactly this expression, and a rule expressed over the name is
    what the two doors can share without one of them re-deriving the other's
    walk. The hidden-DIRECTORY prune in ``_walk_storage`` keeps its own test
    because it answers a different question, about a directory rather than an
    entry.
    """
    return name in IGNORED_FILENAMES or (not include_hidden and name.startswith(HIDDEN_NAME_PREFIX))


def skip_rules(scope: str) -> List[dict]:
    """The walk's own pruning policy as review prose, one row per rule.

    A scan's review names what the run will NOT look at, so an operator confirming
    a blank page is told why it is blank instead of guessing whether the walk
    failed. Each reason names the constant it enforces, and ``path`` is the scope
    the run declares (``''`` for the whole library).
    """
    path = scope or ""
    return [
        {
            "path": path,
            "reason": f"hidden names (starting with {HIDDEN_NAME_PREFIX!r}) are skipped (HIDDEN_NAME_PREFIX)",
        },
        {
            "path": path,
            "reason": (
                f"the app's own folders {', '.join(sorted(RESERVED_DIRS))} are skipped "
                "(RESERVED_DIRS)"
            ),
        },
        {
            "path": path,
            "reason": (
                f"OS debris {', '.join(sorted(IGNORED_FILENAMES))} is skipped "
                "(IGNORED_FILENAMES)"
            ),
        },
        {
            "path": path,
            "reason": "symlinks are never followed: a symlinked directory is not walked and a symlinked file is ignored",
        },
        {
            "path": path,
            "reason": "non-regular files are ignored: only a regular file is registered",
        },
        {
            "path": path,
            "reason": "the run registers and stamps missing_since; it never deletes a row or a file",
        },
    ]


def _name_parts(filename: str) -> Tuple[str, str]:
    """The on-disk stem of one filename and its lowercased suffix."""
    path = PurePath(filename)
    return path.stem, path.suffix.lower()


def derive_asset_names(present: Sequence[str], wanted: Sequence[str], taken: Iterable[str]) -> dict[str, str]:
    """Give every file in one directory a distinct, stable display name.

    ``present`` is every filename the plan accepts in that directory, tracked or
    not, so the answer is a function of the tree and not of database state;
    ``wanted`` is the subset that needs a name now; ``taken`` is the case-folded
    set of names the folder already holds. Pure: no IO, no session, and ``taken``
    is not mutated - the caller decides what to remember.

    The rule (Mf-tkdp):
    * a suffix the library does not treat as a model file keeps the whole
      filename, so ``multifilament.s#5`` stays itself;
    * a stem that is unique in the directory keeps the bare stem (``solo``);
    * two or more nameable files sharing a stem split by extension
      (``part (stl)`` / ``part (3mf)``);
    * anything still taken escalates with `` (2)``, `` (3)``, ...
    All comparisons are ``casefold()``, a superset of SQLite's ASCII ``NOCASE``,
    so a skipped name is only ever one SQLite would have allowed; the name keeps
    the on-disk casing of the stem and the lowercased extension. Files are named
    in ``(casefold, raw)`` order and every assignment joins the taken-set as it
    is made, which is what makes a batch boundary unable to change a name.
    """
    stem_groups: dict = {}
    for filename in set(present):
        stem, ext = _name_parts(filename)
        if ext in NAMING_EXTS:
            key = stem.casefold()
            stem_groups[key] = stem_groups.get(key, 0) + 1

    remaining = set(taken)
    names: dict = {}
    for filename in sorted(set(wanted), key=lambda f: (f.casefold(), f)):
        stem, ext = _name_parts(filename)
        if ext not in NAMING_EXTS:
            candidates = [filename]
        elif stem_groups.get(stem.casefold(), 0) >= 2:
            candidates = [f"{stem} ({ext[1:]})"]
        else:
            candidates = [stem, f"{stem} ({ext[1:]})"]
        for candidate in candidates:
            if candidate.casefold() not in remaining:
                chosen = candidate
                break
        else:
            last = candidates[-1]
            number = 2
            while f"{last} ({number})".casefold() in remaining:
                number += 1
            chosen = f"{last} ({number})"
        remaining.add(chosen.casefold())
        names[filename] = chosen
    return names


def _asset_file_is_present(
    asset_id: str,
    filename: str,
    storage_path: Optional[str],
    source_path: Optional[str],
    root: Path,
    seen: set,
) -> bool:
    """Whether the bytes an asset row points at still exist on disk.

    The walk's ``seen`` set covers ordinary library files; the direct existence
    checks cover paths the walk prunes or filters (``thumbs/``, dotfiles, debris)
    and reference rows whose ``source_path`` lives outside the storage root.
    """
    managed_rel = storage_path or f"{asset_id}/{filename}"
    if managed_rel in seen:
        return True
    if (root / managed_rel).exists():
        return True
    if source_path and Path(source_path).exists():
        return True
    return False


def _apply_missing_flags(session: Session, flag_ids: List[str], clear_ids: List[str], now: str) -> None:
    """Write one batch of ``missing_since`` changes and commit it once.

    Bulk ``UPDATE`` statements are used so a concurrent ``DELETE`` between the
    candidate read and the write does not raise ``StaleDataError``. The caller
    owns rollback and retry.
    """
    if flag_ids:
        session.execute(update(Asset).where(Asset.id.in_(flag_ids)).values(missing_since=now))
    if clear_ids:
        session.execute(update(Asset).where(Asset.id.in_(clear_ids)).values(missing_since=None))
    session.commit()


def _flag_missing_assets(
    session: Session, root: Path, seen: set, now: str, state: ScanState, *, scope: Optional[str] = None
) -> None:
    """Set or clear ``missing_since`` for every candidate row, in bounded batches.

    Candidates are keyset-paginated by id and written with at most one commit per
    ``BATCH_SIZE`` changes, so neither the candidate set nor the dirty ids are
    held in memory for a whole tree. A rolled-back flag batch is a diagnostic in
    the log only: it is not a failed file, so it must not inflate ``errors``
    without bumping ``failed``.

    A non-empty ``scope`` restricts the candidate query to rows under that folder,
    so a row outside the scope this run declared is never even considered: the
    scan only stamps a file it actually looked for.
    """
    flag_ids: List[str] = []
    clear_ids: List[str] = []
    consecutive_failures = 0
    last_id = ""

    def flush() -> None:
        """Commit the accumulated ids, retrying once after a transaction failure."""
        nonlocal consecutive_failures
        if not flag_ids and not clear_ids:
            return
        try:
            _apply_missing_flags(session, flag_ids, clear_ids, now)
        except Exception:
            session.rollback()
            try:
                _apply_missing_flags(session, flag_ids, clear_ids, now)
            except Exception as exc:
                session.rollback()
                # not a registration failure: these rows simply stay un-flagged
                # (or flagged) and the next scan converges.
                log.warning(
                    "missing-flag batch of %d rolled back: %s: %s",
                    len(flag_ids) + len(clear_ids), type(exc).__name__, exc,
                )
                flag_ids.clear()
                clear_ids.clear()
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_BATCH_FAILURES:
                    raise RuntimeError(
                        f"{consecutive_failures} consecutive flag batches failed to commit"
                    ) from exc
                return
        flag_ids.clear()
        clear_ids.clear()
        consecutive_failures = 0

    while True:
        filters = [
            or_(Asset.storage_path.is_not(None), Asset.source_path.is_not(None)),
            Asset.id > last_id,
        ]
        if scope:
            filters.append(Asset.storage_path.like(f"{scope}/%"))
        rows = session.exec(
            select(Asset.id, Asset.filename, Asset.storage_path, Asset.source_path, Asset.missing_since)
            .where(*filters)
            .order_by(Asset.id)
            .limit(BATCH_SIZE)
        ).all()
        if not rows:
            break

        page_missing = 0
        for asset_id, filename, storage_path, source_path, missing_since in rows:
            last_id = asset_id
            present = _asset_file_is_present(asset_id, filename, storage_path, source_path, root, seen)
            if present:
                if missing_since is not None:
                    clear_ids.append(asset_id)
                continue
            if source_path is None and storage_path is not None:
                page_missing += 1
            # never re-stamp an already-flagged row: its timestamp must stay stable
            if missing_since is None:
                flag_ids.append(asset_id)
        if page_missing:
            with _state_lock:
                state.missing += page_missing
        if len(flag_ids) + len(clear_ids) >= BATCH_SIZE:
            flush()

    flush()


def resolve_dir_folder(
    session: Session,
    rel_dir: str,
    cache: dict,
    *,
    with_storage_path: bool = False,
) -> Optional[str]:
    """Return the folder id for a scanned directory, creating missing rows.

    Mirrors ``zip_service.resolve_zip_folder_id`` for the scan's case (no base
    folder), except that folder rows belong to the caller's batch transaction
    rather than being committed one folder at a time. The flush is not optional:
    it keeps the folder INSERTs out of the per-row savepoints, so a rejected
    asset row can never roll back a folder that its siblings still reference.
    There is also no unique index on ``folder(name, parent_id)``, so an unflushed
    duplicate would silently duplicate a whole branch.

    ``with_storage_path=True`` gives a folder row the real, unsanitised relative
    path of the directory it stands for, which is what makes folder rename and
    delete able to move the actual directory. Every caller that walks real
    directories passes it - the storage scan and mount copy mode alike. Rows
    reused by ``(parent_id, name)`` are healed: a row created before this was
    true has ``storage_path`` NULL, and the next scan fills it in rather than
    leaving a folder that claims to be a directory but is not.

    A directory can also be owned by a row with a *different* label: the
    asset-evidence backfill (``folder_paths.backfill_paths_from_asset_dirs``)
    links a migrated folder to its real directory without renaming it or
    reparenting it. When no ``(parent_id, name)`` row matches, that row is
    adopted by id instead of inserting a second row for the same path - without
    this the first new file in a healed legacy directory dies on
    ``idx_folder_storage_path_unique`` and the scan aborts. Adoption never
    rewrites the adopted row; only its id is reused.

    A ``(parent_id, name)`` row that matches but has no path is healed only when
    that directory is still free. If a different row already owns it, the heal is
    skipped and the name-matched row keeps its NULL path, because flushing the
    write would collide with ``idx_folder_storage_path_unique`` and abort the
    scan.
    """
    parent_id: Optional[str] = None
    parent_key = "root"
    consumed: List[str] = []
    for segment in rel_dir.split("/"):
        if not segment:
            continue
        consumed.append(segment)
        name = sanitize_folder_name(segment)
        key = f"{parent_key}/{name}"
        folder_id = cache.get(key)
        if not folder_id:
            existing = session.exec(
                select(Folder).where(Folder.parent_id == parent_id, Folder.name == name)
            ).first()
            if existing:
                folder_id = existing.id
                if with_storage_path and not existing.storage_path:
                    # heal a row created while the scan still wrote metadata-only
                    # folders (found on the first real deployment, 2026-10-31)
                    storage_path = "/".join(consumed)
                    owner_id = session.exec(
                        select(Folder.id).where(
                            Folder.storage_path.collate("NOCASE") == storage_path
                        )
                    ).first()
                    if owner_id is not None and owner_id != existing.id:
                        # The asset-evidence backfill linked this directory to a
                        # migrated row under a different label. Healing this row
                        # would collide with idx_folder_storage_path_unique and
                        # abort the whole scan, so the existing owner keeps the
                        # directory and this row keeps its NULL path.
                        log.warning(
                            "folder heal skipped: %s already owned by folder %s; folder %s stays pathless",
                            storage_path, owner_id, existing.id,
                        )
                    else:
                        existing.storage_path = storage_path
                        session.add(existing)
                        session.flush()
            else:
                storage_path = "/".join(consumed) if with_storage_path else None
                adopted = None
                if storage_path:
                    # A migrated folder already owns this directory under a
                    # different label; reuse its id instead of colliding with
                    # idx_folder_storage_path_unique.
                    adopted = session.exec(
                        select(Folder).where(
                            Folder.storage_path.collate("NOCASE") == storage_path
                        )
                    ).first()
                if adopted is not None:
                    folder_id = adopted.id
                else:
                    folder = Folder(name=name, tags_json="[]", parent_id=parent_id, storage_path=storage_path)
                    session.add(folder)
                    session.flush()
                    folder_id = folder.id
            cache[key] = folder_id
        parent_id = folder_id
        parent_key = key
    return parent_id


def _prepared_metadata(path: Path, filename: str) -> Optional[str]:
    """Best-effort prepared-print metadata; never a reason to fail a row."""
    try:
        metadata = inspect_prepared_print(path, filename)
    except Exception:
        return None
    return json.dumps(metadata) if metadata else None


def build_asset(root: Path, rel_path: str, size: int, name: str, created_via: str = "scan") -> Asset:
    """Build the whole row up front, so the write phase needs no extra reads.

    A scan has no actor - it is background work with no request and no user - so
    `created_by` stays NULL and only the door is stamped. `created_via` is a
    parameter rather than a hardcoded "scan" because mount import reuses this
    builder for the same walk: the caller names its own door.

    ``name`` is derived by the caller because only the per-directory pre-pass
    knows the folder's siblings and already-taken names (Mf-tkdp), while
    ``filename`` stays the on-disk basename - so a row can have ``name="part
    (stl)"`` with ``filename="part.stl"``. A later metadata rename re-renders the
    filename from the name (``part (stl).stl``), which is cosmetic and out of
    scope for Mf-tkdp.
    """
    filename = Path(rel_path).name
    return Asset(
        name=name,
        filename=filename,
        mime=guess_mime_from_path(rel_path),
        size=size,
        title=name,
        # the whole point of the scan: this row points at the real file
        storage_path=rel_path,
        prepared_metadata_json=_prepared_metadata(root / rel_path, filename),
        created_by=None,
        created_via=created_via,
    )


def _folder_id_for(plan: Optional[TreePlan], session: Session, rel_dir: str, cache: dict) -> Optional[str]:
    """The folder row standing for one directory, created if missing.

    The single folder resolver: the per-directory naming pre-pass and
    ``_assign_folders`` both go through it, so a directory's folder is resolved
    once per run and the cache is the only authority on it. Folder rows stay
    outside the per-row savepoints (``resolve_dir_folder`` flushes but does not
    commit) and land with the batch.
    """
    as_directories = True if plan is None else plan.folders_are_directories
    return resolve_dir_folder(session, rel_dir, cache, with_storage_path=as_directories)


def _assign_folders(session: Session, pending: List[PendingAsset], cache: dict) -> None:
    """Resolve every folder id before any of these assets enters the session.

    Whether the folder rows carry their directory path comes from the plan: a walk
    over real directories (the storage scan, mount copy mode) says yes; a
    reference walk, whose bytes live outside storage, says no.
    """
    plan = _active_plan()
    for item in pending:
        item.asset.folder_id = _folder_id_for(plan, session, item.rel_dir, cache)


def _commit_batch(
    session: Session,
    pending: List[PendingAsset],
    cache: dict,
) -> List[Tuple[str, str, str]]:
    """Insert one batch in a single transaction and return its thumbnail work.

    Each row gets its own savepoint so a row that cannot be inserted (a name
    collision, a bad value) is dropped without poisoning the batch. Raises when
    the batch transaction itself fails; ``pending`` then only holds the rows
    that were not individually rejected.
    """
    plan = _active_plan()
    if plan is not None and plan.prepare is not None:
        # mount-only side effects (materialise folders, copy bytes) have to run on
        # every attempt: _flush_batch retries _commit_batch after a failed commit,
        # and a retry with a cleared folder cache must not fall back to
        # metadata-only folders.
        plan.prepare(session, pending, cache)
    _assign_folders(session, pending, cache)
    rejected: List[PendingAsset] = []
    for item in pending:
        asset = item.asset
        try:
            with session.begin_nested():
                session.add(asset)
                session.flush()
        except Exception as exc:
            if asset in session:
                session.expunge(asset)
            note_error(f"{item.rel_path}: {type(exc).__name__}: {exc}")
            rejected.append(item)
    for item in rejected:
        pending.remove(item)

    thumbnails = [(item.asset.id, item.rel_path, item.asset.mime) for item in pending]
    session.commit()
    # the identity map would otherwise grow to the size of the whole tree
    session.expunge_all()
    return thumbnails


def _save_thumbnails(root: Path, thumbnails: List[Tuple[str, str, str]]) -> None:
    """Best-effort thumbnails, outside any write transaction."""
    for asset_id, rel_path, mime in thumbnails:
        path = root / rel_path
        try:
            suffix = path.suffix.lower()
            if (mime or "").lower().startswith("image/") and suffix in IMAGE_SUFFIXES:
                save_thumb(asset_id, path)
            elif suffix == ".3mf":
                ensure_model_thumbnail(asset_id, path)
        except Exception:
            # a thumbnail is nice to have; it must not cost us the registration
            pass


def _flush_batch(
    session: Session,
    pending: List[PendingAsset],
    cache: dict,
    root: Path,
) -> Optional[List[str]]:
    """Commit a batch, retrying once after a transaction-level failure.

    Returns the rel paths that were committed, or ``None`` when the batch could
    not be committed at all; those files are counted as failed and left for the
    next scan.
    """
    try:
        thumbnails = _commit_batch(session, pending, cache)
    except Exception:
        # a lock timeout or an IO error: a half-applied transaction must not be
        # trusted, and cached folder ids may now point at rolled-back rows
        session.rollback()
        cache.clear()
        try:
            thumbnails = _commit_batch(session, pending, cache)
        except Exception as exc:
            session.rollback()
            cache.clear()
            # every file still pending failed, in the order it was discovered
            _note_batch_failure([item.rel_path for item in pending], exc)
            return None
    _save_thumbnails(root, thumbnails)
    return [rel_path for _asset_id, rel_path, _mime in thumbnails]


def _directory_names(
    session: Session,
    plan: TreePlan,
    rel_dir: str,
    accepted: Sequence[Tuple[str, int]],
    wanted: Sequence[str],
    cache: dict,
    assigned: dict,
) -> dict[str, str]:
    """Derive display names for one directory's new files, keyed by rel path.

    Runs before the rows go pending, because this is the only point in the walk
    where the directory's whole listing, its folder id and an unflushed session
    all exist at once. One folder read per directory with new files: O(dirs)
    queries, not O(files x folder contents).
    """
    folder_id = _folder_id_for(plan, session, rel_dir, cache)
    bucket = folder_id or ""
    taken = assigned.setdefault(bucket, set())
    taken |= folder_name_index(session, folder_id)
    names = derive_asset_names(
        [Path(rel_path).name for rel_path, _size in accepted],
        [Path(rel_path).name for rel_path in wanted],
        taken,
    )
    taken.update(name.casefold() for name in names.values())
    return {rel_path: names[Path(rel_path).name] for rel_path in wanted}


def _register_tree(session: Session, plan: TreePlan, state: ScanState, seen: set) -> None:
    """Walk ``plan.root`` and register what the plan accepts, in batches.

    Registers only: it never stamps or clears ``missing_since``, so a caller can
    point it at a root that is not the library (a mounted source) without
    flagging the whole library. Counters and error notes go to ``state`` via the
    thread-local active run.
    """
    previous_state = getattr(_local, "state", None)
    previous_plan = getattr(_local, "plan", None)
    _local.state = state
    _local.plan = plan

    folder_cache: dict = {}
    pending: List[PendingAsset] = []
    consecutive_failures = 0
    # Run-scoped names already handed out, keyed by the index's own bucket:
    # ``folder_id or ""`` is exactly ``COALESCE(folder_id, '')`` in
    # idx_asset_folder_name_unique, so it counts rows still sitting in the
    # unflushed ``pending`` batch (which a database read cannot see) and
    # directories that resolve to the same folder. Without it BATCH_SIZE could
    # change a name; with it the run's answer is a function of the tree alone.
    assigned_names: dict = {}

    def flush() -> None:
        """Commit what is pending and account for it."""
        nonlocal consecutive_failures
        if not pending:
            return
        committed = _flush_batch(session, pending, folder_cache, plan.root)
        pending.clear()
        if committed is None:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_BATCH_FAILURES:
                raise RuntimeError(f"{consecutive_failures} consecutive batches failed to commit")
            return
        consecutive_failures = 0
        for rel_path in committed:
            plan.tracked.update(plan.keys_for(rel_path))
        with _state_lock:
            state.registered += len(committed)

    try:
        walk = _walk_storage(plan.root, include_hidden=plan.include_hidden, start_rel=plan.scope)
        for rel_dir, file_entries, ignored in walk:
            if ignored:
                with _state_lock:
                    state.ignored += ignored
            # one directory's accepted files, kept only until its names are derived
            accepted: List[Tuple[str, int]] = []
            for entry in file_entries:
                if is_skipped_name(entry.name, include_hidden=plan.include_hidden):
                    with _state_lock:
                        state.ignored += 1
                    continue
                rel_path = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                # everything this run actually saw, so the missing pass never
                # has to look at a present file twice
                seen.add(rel_path)
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError as exc:
                    note_error(f"{rel_path}: {type(exc).__name__}: {exc}")
                    continue
                if not plan.accept(rel_path, size):
                    with _state_lock:
                        state.ignored += 1
                    continue
                accepted.append((rel_path, size))

            # naming needs the directory's whole accepted listing, so it runs as
            # its own pass before any row of this directory goes pending
            wanted: List[str] = []
            for rel_path, _size in accepted:
                if any(key in plan.tracked for key in plan.keys_for(rel_path)):
                    with _state_lock:
                        state.known += 1
                    continue
                wanted.append(rel_path)
            if not wanted:
                continue
            names = _directory_names(session, plan, rel_dir, accepted, wanted, folder_cache, assigned_names)
            for rel_path, size in accepted:
                if rel_path not in names:
                    continue
                pending.append(
                    PendingAsset(asset=plan.build(rel_path, size, names[rel_path]), rel_path=rel_path, rel_dir=rel_dir)
                )
                if len(pending) >= BATCH_SIZE:
                    flush()

        flush()
    finally:
        _local.state = previous_state
        _local.plan = previous_plan


def _self_key(rel_path: str) -> Tuple[str, ...]:
    """A storage file is tracked by the relative path it lies at."""
    return (rel_path,)


def _accept_every_file(_rel_path: str, _size: int) -> bool:
    """The storage scan takes every file the walk offers."""
    return True


def _storage_plan(root: Path, tracked: set, scope: str = "") -> TreePlan:
    """The plan for the library itself: register every file where it lies."""
    return TreePlan(
        root=root,
        tracked=tracked,
        keys_for=_self_key,
        accept=_accept_every_file,
        build=partial(build_asset, root),
        scope=scope,
    )


def _scan_library(scope: str, state: Optional[ScanState]) -> ScanState:
    """Run one storage scan, optionally over one folder of the library.

    Shared by ``scan_storage_root`` and ``scan_subtree`` so the slot claim, the
    timestamp, the tracked set, the exception path and the ``finally`` close are
    one implementation. Only a whole-root run calls the asset-evidence folder
    backfill; a scoped run refuses it by name in ``scan_subtree``.
    """
    run = _begin_run(state)
    try:
        root = STORAGE.resolve()
        if not root.exists() or not root.is_dir():
            _note_failure(run, f"storage root not found: {root}")
            return run
        if scope and not (root / scope).is_dir():
            _note_failure(run, f"scan scope not found: {scope}")
            return run

        # one timestamp per run: every asset the scan first notices as missing this
        # pass shares it, so the value is stable and order-independent.
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with _write_lock, Session(engine) as session:
            tracked = set(
                session.exec(
                    select(Asset.storage_path).where(Asset.storage_path.is_not(None))
                ).all()
            )

            seen: set = set()
            _register_tree(session, _storage_plan(root, tracked, scope=scope), run, seen)

            # registers nothing and deletes nothing: it only stamps or clears
            # ``missing_since``. The ``missing`` counter covers managed rows only,
            # while reference rows may be flagged too. A scoped run passes its
            # scope, so a row outside it is never a candidate.
            _flag_missing_assets(session, root, seen, now, run, scope=scope or None)

            if not scope:
                # After the walk and after the fresh flags: the walk's own
                # name-heal wins for every directory it touched, and this run's
                # missing flags are part of the liveness evidence. Pure ORM, no
                # filesystem call, so an unmounted volume cannot make a scan fail
                # here. Deliberately NOT called for a scoped run: it reasons over
                # every folder and asset row in the library, i.e. it is discovery
                # outside the scope the run declared.
                folder_paths.backfill_paths_from_asset_dirs(session)

        return run
    except Exception as exc:
        # counted and logged here; re-raised because the interrupted-walk and
        # circuit-breaker contracts pin the raise
        _note_failure(run, f"scan aborted: {type(exc).__name__}: {exc}")
        raise
    finally:
        _finish_run(run)


def scan_storage_root(state: Optional[ScanState] = None) -> ScanState:
    """Walk the storage root and register unknown files. Safe to call directly.

    This is the single owner of a whole-root run: it claims the slot through
    ``_begin_run``, walks, and closes the run in a ``finally``. Counters describe
    this scan only; a caller that needs history reads ``scan_status()``, which
    keeps the last finished run visible. A second direct call while one holds the
    slot raises ``ScanAlreadyRunning``; an abort is recorded on the run and then
    re-raised.
    """
    return _scan_library("", state)


def scan_subtree(rel_dir: str, state: Optional[ScanState] = None) -> ScanState:
    """Walk one folder of the library and register unknown files inside it.

    The scope is validated by ``normalise_scope``; ``''`` is the whole library and
    delegates to ``scan_storage_root``, so a root run and a scoped run share one
    implementation. A scope that is not a directory fails the run with
    ``scan scope not found`` before anything is walked.

    A scoped run never calls ``folder_paths.backfill_paths_from_asset_dirs``: that
    helper reasons over every folder and asset row in the library, which is
    discovery outside the scope this run declared. The walk itself heals the
    folder rows for the directories it touches, which is all a scoped run may do.

    Counters describe this run only, and the run holds the one scan slot exactly
    like a root scan: a scoped run and a background ``start_scan()`` refuse each
    other with ``ScanAlreadyRunning``.
    """
    scope = normalise_scope(rel_dir)
    if not scope:
        return scan_storage_root(state)
    return _scan_library(scope, state)


def register_tree(plan: TreePlan, state: Optional[ScanState] = None) -> ScanState:
    """Register what ``plan`` accepts under its root, in batches.

    Every counter belongs to the returned state, so a mounted import never
    clobbers the library-scan status and never stamps ``missing_since``. Runs are
    serialised with the storage scan, because both create folder rows. ``state``
    lets a caller own and persist the counters; when omitted a fresh state is
    created, exactly as before.
    """
    if state is None:
        state = ScanState(started_at=time.time())
    if not plan.root.exists() or not plan.root.is_dir():
        with _state_lock:
            state.failed += 1
            state.errors.append(f"root not found: {plan.root}")
        return state
    try:
        with _write_lock, Session(engine) as session:
            _register_tree(session, plan, state, set())
    finally:
        state.finished_at = time.time()
    return state


# --- the explicit register door (Mf-u2el) ------------------------------------
#
# Everything above registers what a WALK finds. ``register`` is the third door:
# the caller names the files, and the rows written are exactly that list. It
# reuses the same naming rule, the same folder resolver and the same batch
# machinery, and it never walks for discovery - an unnamed sibling on disk gets
# no row. That is what a caller who already knows its members needs: the plan is
# the list, so no step can contradict it.


class RegisterPathError(ValueError):
    """A path ``register`` refuses before it opens a session or writes a row."""


@dataclass(frozen=True)
class RegisteredPath:
    """One named path that has a row, and whether this call made that row."""

    rel_path: str
    asset_id: str
    created: bool


class RegisterResult(NamedTuple):
    """What one ``register`` call wrote, and what it could not.

    ``entries`` follows the caller's order (first-seen wins for a duplicate);
    ``absent`` names paths that are not a regular file on disk, and ``failed``
    names paths that were handed to a batch and got no row (rejected by a batch
    savepoint). Nothing is ever silently skipped: every named path is in exactly
    one of the three.
    """

    entries: Tuple[RegisteredPath, ...]
    absent: Tuple[str, ...]
    failed: Tuple[str, ...]
    state: ScanState

    @property
    def known(self) -> Tuple[RegisteredPath, ...]:
        """The named paths that already had a row: nothing was written for them."""
        return tuple(entry for entry in self.entries if not entry.created)


def _register_rel_path(raw: str) -> str:
    """Normalise one named path, or refuse it before anything is written.

    Deliberately NOT ``snapshots.assert_library_path``: that guard refuses
    ``thumbs/`` and ``bundles/`` at ANY depth, which would refuse a legitimate
    member a scan registers (``01_Wizard/thumbs/x.stl``). The rules here are the
    walk's own - app-owned directories only at the root, the basename predicate
    at the tail, hidden segments pruned exactly as the walk prunes them - plus the
    containment check that keeps a caller-supplied string inside storage.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise RegisterPathError(f"an empty path is not a library file: {raw!r}")
    if PurePath(raw).is_absolute():
        raise RegisterPathError(f"an absolute path is not registered: {raw}")
    rel_path = posixpath.normpath(raw)
    segments = rel_path.split("/")
    if rel_path in (".", "..") or rel_path.startswith("../") or segments[0].startswith("~"):
        raise RegisterPathError(f"the path escapes the storage root: {raw}")

    root = STORAGE.resolve()
    try:
        resolved = (root / rel_path).resolve()
    except OSError as exc:
        raise RegisterPathError(f"the path cannot be resolved: {raw} ({type(exc).__name__})") from exc
    if not resolved.is_relative_to(root) or resolved == root:
        raise RegisterPathError(f"the path resolves outside the storage root: {raw}")

    if segments[0] in RESERVED_DIRS:
        raise RegisterPathError(f"{segments[0]}/ is app-owned and never library content: {raw}")
    if any(segment.startswith(HIDDEN_NAME_PREFIX) for segment in segments):
        raise RegisterPathError(f"a hidden name is not registered: {raw}")
    if segments[-1] in IGNORED_FILENAMES:
        raise RegisterPathError(f"tool debris is not registered: {raw}")
    if (root / rel_path).is_symlink():
        raise RegisterPathError(f"a symlinked file is not registered: {raw}")
    return rel_path


def _register_rel_paths(paths: Sequence[str]) -> List[str]:
    """Validate every named path, then dedupe preserving first-seen order.

    Validation happens for the whole list before a single path is used, so a
    refused path can never leave the call half-applied: either every name is
    registrable or nothing is written at all.
    """
    seen: set = set()
    rel_paths: List[str] = []
    for raw in paths:
        rel_path = _register_rel_path(raw)
        if rel_path in seen:
            continue
        seen.add(rel_path)
        rel_paths.append(rel_path)
    return rel_paths


def _stored_ids(session: Session, rel_paths: Sequence[str]) -> Dict[str, str]:
    """``{storage_path: id}`` for the named paths, read in ``BATCH_SIZE`` chunks.

    A caller can name a whole project, so the ``IN`` list is never handed to
    SQLite in one piece; an empty list issues no query at all.
    """
    ids: Dict[str, str] = {}
    for start in range(0, len(rel_paths), BATCH_SIZE):
        chunk = list(rel_paths[start:start + BATCH_SIZE])
        rows = session.exec(
            select(Asset.id, Asset.storage_path).where(Asset.storage_path.in_(chunk))
        ).all()
        for asset_id, storage_path in rows:
            ids[storage_path] = asset_id
    return ids


def _directory_listing(root: Path, rel_dir: str) -> Optional[List[Tuple[str, int]]]:
    """The scan's accepted listing for one directory, or ``None`` if unreadable.

    Exactly the walk's own answer - readdir order sorted by ``(casefold, name)``,
    regular non-symlink entries only, ``is_skipped_name`` applied - because the
    naming rule is a function of the whole listing. Reading only the named subset
    would hand the derivation a tree the scan never sees, and a file sharing a
    stem with an unnamed sibling would come out with a different name through
    this door (Oracle N-1).
    """
    try:
        with os.scandir(root / rel_dir if rel_dir else root) as scan:
            entries = list(scan)
    except OSError:
        return None
    entries.sort(key=lambda entry: (entry.name.casefold(), entry.name))
    accepted: List[Tuple[str, int]] = []
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            if is_skipped_name(entry.name):
                continue
            size = entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
        accepted.append((f"{rel_dir}/{entry.name}" if rel_dir else entry.name, size))
    return accepted


def _clear_missing_flags(session: Session, asset_ids: Sequence[str]) -> None:
    """Clear ``missing_since`` on exactly the rows this call verified present.

    One targeted ``UPDATE`` (never ``_flag_missing_assets``, which stamps every
    absent candidate in the library) and best-effort: a failed clear costs a stale
    badge that the next rescan converges, so it is logged rather than raised.
    """
    if not asset_ids:
        return
    try:
        session.execute(update(Asset).where(Asset.id.in_(list(asset_ids))).values(missing_since=None))
        session.commit()
    except Exception as exc:
        session.rollback()
        log.warning(
            "register: clearing missing_since for %d row(s) failed: %s: %s",
            len(asset_ids), type(exc).__name__, exc,
        )


def _register_named(
    rel_paths: Sequence[str], *, created_via: str, state: ScanState
) -> Tuple[Tuple[RegisteredPath, ...], Tuple[str, ...], Tuple[str, ...]]:
    """Write rows for exactly ``rel_paths``; ``register`` owns validation/state.

    The caller has already refused every unregistrable name, so this only looks
    at the disk: a named path that is not a regular file is ``absent``, a named
    path a batch rejected is ``failed``, and everything else gets a row (or
    already had one). It never claims the scan slot, never flags missing rows and
    never deletes or prunes - the folder rows for the directories it touches are
    created/healed as a side effect of resolving them, shallowest first.
    """
    root = STORAGE.resolve()
    present: List[str] = []
    absent: List[str] = []
    for rel_path in rel_paths:
        try:
            is_file = (root / rel_path).is_file()
        except OSError:
            is_file = False
        if is_file:
            present.append(rel_path)
        else:
            # never silently skipped: a missing file, a directory or a dead link
            # is reported, and the rest of the list still proceeds
            absent.append(rel_path)

    attempted: List[str] = []
    folder_cache: dict = {}
    assigned_names: dict = {}
    pending: List[PendingAsset] = []
    with _write_lock, Session(engine) as session:
        tracked = set(_stored_ids(session, rel_paths))
        plan = _storage_plan(root, tracked)
        # For the duration of the writes: note_error/_note_batch_failure count
        # against THIS state, and _assign_folders gives folder rows their paths.
        _local.state = state
        _local.plan = plan

        # One readdir per distinct directory, in first-seen order: directories
        # that resolve to the same folder share the run's naming bucket.
        wanted_by_dir: Dict[str, List[str]] = {}
        for rel_path in present:
            if rel_path in tracked:
                continue
            wanted_by_dir.setdefault(posixpath.dirname(rel_path), []).append(rel_path)

        for rel_dir, wanted in wanted_by_dir.items():
            accepted = _directory_listing(root, rel_dir)
            if accepted is None:
                # the directory cannot be read, so nothing in it can be named
                absent.extend(wanted)
                continue
            sizes = dict(accepted)
            names = _directory_names(session, plan, rel_dir, accepted, wanted, folder_cache, assigned_names)
            for rel_path in wanted:
                if rel_path not in names:
                    # the listing lost it between the existence check and the
                    # readdir: report it rather than write a guessed row
                    absent.append(rel_path)
                    continue
                pending.append(
                    PendingAsset(
                        asset=build_asset(root, rel_path, sizes[rel_path], names[rel_path], created_via),
                        rel_path=rel_path,
                        rel_dir=rel_dir,
                    )
                )
                attempted.append(rel_path)
                if len(pending) >= BATCH_SIZE:
                    _flush_batch(session, pending, folder_cache, root)
                    pending.clear()
        if pending:
            _flush_batch(session, pending, folder_cache, root)
            pending.clear()

        # After the commits: the ids a rejected row never got are what `failed`
        # means, and a present row is what "clear the badge" is allowed to touch.
        rows = _stored_ids(session, present)
        _clear_missing_flags(session, list(rows.values()))

        entries = tuple(
            RegisteredPath(rel_path, rows[rel_path], created=rel_path not in tracked)
            for rel_path in rel_paths
            if rel_path in rows
        )
        failed = tuple(rel_path for rel_path in attempted if rel_path not in rows)
        created = sum(1 for entry in entries if entry.created)
        with _state_lock:
            state.registered += created
            state.known += len(entries) - created

    return entries, absent, failed


def register(
    paths: Sequence[str],
    *,
    created_via: str = "scan",
    state: Optional[ScanState] = None,
) -> RegisterResult:
    """Register EXACTLY the named files, in batches, without walking.

    The third registration door, for a caller that already knows its members (an
    archive listing's planned paths). Four differences from ``scan_storage_root``
    are the whole point:

    * **no discovery**: a row is written only for a named path, so an unnamed file
      beside a named one is left alone;
    * **no scan slot**: it takes ``_write_lock`` in ``_register_tree``'s order and
      never calls ``_begin_run``, so ``scan_status()`` is untouched, a scan in
      flight serialises it exactly as it serialises mount import, and a later
      ``start_scan()`` still claims the slot;
    * **no missing-flag pass**: it never stamps ``missing_since`` and never prunes.
      It clears the badge on the paths it verified present, one targeted
      ``UPDATE``, because a row whose bytes are here is not missing;
    * **no walk-level folder backfill**: ``folder_paths.backfill_paths_from_asset_dirs``
      reasons over every folder and asset row globally, which is discovery again.
      ``resolve_dir_folder(with_storage_path=True)`` still creates and heals every
      folder row for the directories this call touches.

    Every path is validated before a session is opened (``RegisterPathError``:
    empty, absolute, escaping storage, app-owned root directory, hidden segment,
    tool debris, a symlink), so a bad name writes nothing at all. A named path
    that is not a regular file is ``absent``; a named path a batch rejected is
    ``failed``; a named path that already had a row is reported through
    ``RegisterResult.known`` without a second row.

    ``created_via`` defaults to ``"scan"`` on purpose: ``CreatedVia`` is a closed
    literal, and these are the rows a rescan would have made. A caller's own step
    log is the finer record.

    ``state`` lets a caller own the counters; when omitted a fresh state is
    created and its ``started_at``/``finished_at`` are stamped here. The run is
    never published, so nothing ever reads it as the library scan.
    """
    rel_paths = _register_rel_paths(paths)
    owns_state = state is None
    if state is None:
        state = ScanState(started_at=time.time())
    previous_state = getattr(_local, "state", None)
    previous_plan = getattr(_local, "plan", None)
    try:
        if not rel_paths:
            return RegisterResult(entries=(), absent=(), failed=(), state=state)
        entries, absent, failed = _register_named(rel_paths, created_via=created_via, state=state)
        return RegisterResult(entries=entries, absent=tuple(absent), failed=failed, state=state)
    finally:
        _local.state = previous_state
        _local.plan = previous_plan
        if owns_state:
            state.finished_at = time.time()
