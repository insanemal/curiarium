"""Turn what is on disk into a dry-run bundle report, then into projects.

This is the only layer that touches both bytes and rows. `proposal_engine` decides
what a listing means - it measures a folder and ranks the readings its facts
support - and this module enumerates the storage tree, asks the engine about each
folder, resolves each row against the database, serves the report, and - only when
a human confirms - creates one project per bundle. There is no second opinion
anywhere in this file: no rule of its own, no legacy reading, no dissent list.

**ONE engine ranks (Mf-1agz).** The row-population rule, in the engine's own
vocabulary, is stated once at ``_build_candidates``: walk top-down; a `leave` or
`archive-only` reading descends and offers no row; a claiming reading
(``one-project``/``git-project``/``add-to-project``) is a row and descent stops; a
split reading is a row and descent stops, because the split's children ARE the
split. The app's ``import/`` area is transparent - it is the drop zone the import
doors write into, so a row for it would offer the whole inbox and hide the
products inside it. A ``.zip`` is a row of its own through
``proposal_engine.measure_archive``/``propose_archive``, unless it is a duplicate,
a member of an offered folder, or one of those folders' provenance.

**The report is recomputed from disk on every read.** There is no cache, no
stored candidate table and no migration: a directory added by ``rsync``, a folder
renamed by hand or a zip dropped next to its twin changes the very next
``GET``/``build_report`` response. That is the whole reason detection can be
trusted on a box someone else administers. The walk costs more than it used to -
one engine measurement per row, and a bounded git probe inside the GET (below) -
and the measured wall clock over the live 175-row tree is recorded in
``.plans/measurements/Mf-1agz-after-summary.md``.

**Git probing, bounded (Mf-1agz).** The report asks git about a checkout
(``GIT_PROBE_TIMEOUT`` = 5 s per row) because the owner-approved label corpus is
7/7 with probing and 6/7 without - ``import/endorphin`` is only read correctly as
a checkout - and 173 of the 175 rows pay one ``os.path.exists`` for the answer.
This reverses Mf-yitm's ``probe_git=False`` default for the API on the record
(ADR 0010, Decision 18 amendment); the bound is what keeps a hung checkout from
costing a GET the engine's own 120 s budget.

**The folder is the truth; a twin zip is provenance only.** When a directory and
a ``<name>.zip`` sit side by side, the directory is the bundle and the archive
attaches no files. ``twin_verified`` says what the pairing evidence is: for a stem
twin it is the names-only comparison (the operator paired them, so a mismatch is
information rather than an assertion), and for an entry-root pairing it is a
re-verified containment - a ``False`` there means the bytes changed since the
scan. The one containment predicate for both routes is
``proposal_engine.archive_payload_contained``.

**A nested archive is a member, never a candidate.** A ``.zip`` strictly below a
row belongs to that row (``file_utils.is_bundle_member``): it never becomes a
second row, never pairs a sibling directory, and never supplies a model that would
make the enclosing folder something else. Its central directory may still be
listed to decide pairing and its bytes hashed for duplicate identity, but nothing
inside it is read as evidence and nothing is ever extracted.

**Duplicate archives are one bundle.** Identity for an archive is its content
hash (sha256 over the file, read in bounded chunks). Two archives with the same
hash are one bundle: the copy is suppressed from the archive pass and reported in
``duplicates`` with its ``reclaimable_bytes`` and a copyable tab-separated
``duplicates_text``. Hashing is size-first, so a uniquely-sized archive is never
opened. The report never deletes, renames or moves anything: it only renders text.

**A row on the report is not the same thing as a row in the queue**: the web list
hides anything carrying ``existing_project_id`` behind its "show created" toggle
(``ui/review/bundleRows.ts`` ``matchesVisibility``).

A ``.zip`` row is always ``blocked``/``archive_only``, because creation happens
from registered assets and its door is extraction. That string is not a verdict -
it is the NAME of the only door, and the client keys the Extract action off it
(``extractPathFor``); whether that door is actionable comes from the engine, in
``matches[0].creatable``. The extraction/registration door is
``POST /import/archive`` (Mf-1zmd): it extracts the zip in place, the next scan
registers it, and the folder it produced becomes a row of its own.

**Why ``_folder_index`` keys on ``Folder.storage_path``.** A bundle is only
creatable when its directory is a *registered* directory, meaning a ``Folder``
row whose ``storage_path`` is that relative path. The rows migrated from the
flattened v3 schema carry ``storage_path IS NULL`` (Mf-um7x), so they can never
match - that single fact *is* the legacy refusal. It keys on the row, never on
which shape was recommended: heal the folder row (a rescan gives it its real path)
and the same bundle becomes creatable with no code change.

**The two commit windows, and why a re-run is safe.** One created bundle commits
twice: the project row *with* its ``_bundle`` marker *and* every member row, in one
transaction through ``project_create_service.create_project_from_members``, then
the audit row (``audit.record``). The audit row is its own window by design. It
cannot duplicate a project: every create pass first asks
``project_create_service.find_existing_project`` - the marker key, then the twin
archive, then the source URL, then an exact casefolded name, then asset
containment over the models a create would attach - and a hit is ``skipped`` with
the existing project id and the project is never touched. A human edit is
therefore never overwritten by a re-run, and the report says ``already_created``
before anyone presses create.

**A bundle's project holds every registered file its own row names.** A created
project holds every registered asset under its key that the row owns - models,
images, text documents, unknowns and nested archives alike. What each of those
files IS in the project (``model`` / ``image`` / ``text`` / ``other`` /
``archive``) is ``library_scan.classify_entry``'s answer, decided once in
``_members_of``, so the project and the row it was created from can never disagree
about a file. The create-time belt refuses a row whose registered subtree holds
more than the row owns (an existing project's files, or files that appeared since
the scan) unless the engine read it with documentary evidence - HIGH is exempt
exactly as it always was (ADR 0010 Decision 15/18).

**Bounds.** The walk is capped at ``MAX_SCAN_ENTRIES`` and so is each row's
measurement (``truncated`` says a bound stopped it). ``limit`` bounds the
*report*: ``counts.total``/``counts.offered`` still describe everything that was
found and ``counts.withheld`` says how many rows the wire did not carry, because a
bounded report that said only ``truncated: true`` made the operator guess whether
the row they wanted was one of the ones dropped (Vault was exactly that row).
Every read writes nothing - no row, no file, no folder healing - so the report is
safe on a read route.

**Every absence is named (Mf-9ygi).** A scan also returns the ledger of what it
refused: one ``BundleRefusal`` per path the walk saw and did not offer, carrying
the rule that refused it (``REFUSAL_RULES``: the engine's own ``leave`` /
``archive-only`` words and the scan's own reasons). The ledger is derived AFTER
the row set is final and is never an input to it, so reporting a refusal can never
change which rows are offered. This is the part the 114 -> 112 investigation
lacked: 46 archives were invisible and the report said nothing at all about them.
"""

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import AbstractSet, Collection, Dict, FrozenSet, List, Mapping, NamedTuple, Optional, Sequence, Tuple
from zipfile import BadZipFile

from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import Session, select

import asset_urls
import audit
import bundle_drift
import bundle_enrich
import db
import file_utils
import library_scan
import project_create_service
import project_service
import proposal_engine
import settings_service
import zip_service
from db import engine
from models import Asset, Folder, Project

# The storage root bundle keys are resolved against. A module global so tests can
# point it at a temp tree exactly like ``library_scan.STORAGE``.
STORAGE = db.STORAGE
# The app's import drop zone, at the storage root. Transparent to the row walk: it
# is where the import doors land a download, so a row for it would offer the whole
# inbox as one project and stop the walk before the products inside it. No row,
# no absorption - and its CHILDREN are read exactly like any other folder.
IMPORT_AREA = "import"
TRANSPARENT_DIRS = frozenset({IMPORT_AREA})
# The engine readings that offer NO row and let the walk descend: there is nothing
# to import in this folder itself, so the answer may be one of its children.
DESCEND_SHAPES = frozenset({"leave", "archive-only"})
# The shapes a create can enact, split by row kind. A directory row is creatable
# exactly when the engine reads it as one project (`one-project`/`git-project`); a
# `.zip` row exactly when the archive engine reads it as something to unpack. A
# split, an `add-to-project` or an `archive-leave` is an honest offer the create
# path refuses with `needs-arrange` (or, for the zip, the Extract door).
DIRECTORY_CREATABLE_SHAPES = frozenset(proposal_engine.ONE_PROJECT_SHAPES)
ARCHIVE_CREATABLE_SHAPES = frozenset({"archive-project", "archive-split"})
# The shapes AUTO runs on: small, explicit, and only the two the create door can
# enact in place. Anything else - an archive row, a split, an add-to-project, a
# shape that needs a move - falls back to guided with a reason. `add-to-project`
# is excluded on purpose: it is not in `creatable_shapes`, its whole operation is
# the arrange door, and auto must never half-do one.
AUTO_SHAPES = frozenset({"one-project", "git-project"})
# The project metadata fields a create may write, in one place.
METADATA_FIELDS = ("creator", "licence", "licence_url", "source_url", "description")
# The bound the report gives `git_state` per row (Mf-1agz). The report probes git
# because the label corpus is 7/7 with probing and 6/7 without; this is what keeps
# one hung checkout from costing a GET the full `GIT_TIMEOUT_SECONDS`.
GIT_PROBE_TIMEOUT = 5.0
# The Thingiverse thing page an id in a name points at. One spelling, so a
# derived URL and a page the app fetched can never name two different things.
THINGIVERSE_THING_URL = "https://www.thingiverse.com/thing:{}"
# A walk is bounded so a pathological tree cannot make a read request run forever.
MAX_SCAN_ENTRIES = 200_000
# The report's own bound. ``build_report`` never truncates by accident: a caller
# that needs every row (a create pass) asks for more than the walk can produce.
DEFAULT_LIMIT = 200
# Duplicate detection reads an archive in bounded chunks, never one whole file
# into memory: a multi-gigabyte zip is hashed at this cost, not doubled.
HASH_CHUNK_BYTES = 1024 * 1024
# One enrich request names a working set, not a library: every key is a page
# fetch, so the click is bounded the way a dismiss batch is.
ENRICH_MAX_KEYS = 25

READY = "ready"
BLOCKED = "blocked"
UNRESOLVED_DIRECTORY = "unresolved_directory"
NOT_SCANNED = "not_scanned"
ARCHIVE_ONLY = "archive_only"

COVER_LARGEST_IMAGE = "largest-image"
COVER_LARGEST_IMAGE_UNREGISTERED = "largest-image-unregistered"
COVER_NONE = "none"
# The bundle has no image; its own model is the face instead, rendered client-side.
COVER_MODEL_PREVIEW = "model-preview"
# MODEL_SUFFIXES minus what the client renderer has no loader for
# (ModelViewer.loadObjectFromAsset: stl | 3mf | step | stp). A .lbrn2 is a
# model a cover can never be rendered from, so it is never offered as one.
PREVIEW_SUFFIXES = frozenset({".stl", ".3mf", ".step", ".stp"})

CREATE_CREATED = "created"
CREATE_SKIPPED = "skipped"
CREATE_BLOCKED = "blocked"
CREATE_FAILED = "failed"
# The one band AUTO runs at. The band is the ENGINE's, never a client's word.
AUTO_NEEDS_HIGH = "auto requires high confidence"

# The two shapes the engine can read a folder as SEVERAL projects are its own
# vocabulary (``proposal_engine.SPLIT_SHAPES``), read here rather than restated. A
# row the engine disagrees with is offered as a match with ``creatable=False``, and
# a create of that shape answers this reason: the arrangement it would need is
# `Mf-v9eg` and does not exist yet.
NEEDS_ARRANGE = "needs-arrange"

# How far behind the engine's top option a reading may sit and still be called a
# close call. This is THIS module's tolerance, not the engine's vocabulary: the
# engine ranks its options by score and has no notion of closeness (its four
# scoring bonuses happen to be 15). Named, so the band is a rule and not a number
# in an `if`.
CLOSE_CALL_POINTS = 15

# A create-time refusal: the candidate's assets include another bundle's. This is
# a reason on CREATE_FAILED, never a blocked_reason - `blocked` means database
# readiness only (Mf-um7x).
CONTAINS_BUNDLES = "contains_bundles"

# Which of the four identities found an existing project. Reported because they
# are not equally certain, so the client can say how sure it is. The four live
# with the dedupe they name (``project_create_service``), and this module reads
# them rather than restating them: a second list here could disagree with the
# answer it labels.
MATCH_MARKER = project_create_service.MATCH_MARKER

# Why this service refused a path the walk saw. Since Mf-1agz the engine owns the
# decision, so its readings ARE the vocabulary - a directory the engine reads as
# `leave` is named with that word - and the rest are the refusals only the scan can
# make, because only the scan sees the whole tree: a duplicate archive, a hidden
# twin, an archive nobody could open, a folder that holds rows, the app's own drop
# zone, an unmeasured subtree, and a file that belongs to a live bundle. A path that
# is not offered carries exactly one of these - never silence, which is what made
# the invisible archives of Mf-9ygi expensive to find.
REFUSAL_CONTAINER = "container-rule"
REFUSAL_DUPLICATE = "duplicate"
REFUSAL_NESTED_MEMBER = "nested-member"
REFUSAL_TWIN_CONSUMED = "twin-consumed"
REFUSAL_TWIN_UNVERIFIED = "twin-unverified"
REFUSAL_UNREADABLE_ARCHIVE = "unreadable-archive"
REFUSAL_MEMBER_OF_BUNDLE = "member-of-bundle"
# The engine's own no-row readings, used verbatim so the ledger and the ranking
# speak one vocabulary: nothing to import here. `leave` is what a folder answers
# when it holds neither models nor archives; `archive-only` is what it answers when
# it holds archives and no models (its zips are their own rows); `archive-leave` is
# the archive engine's word for a `.zip` with nothing unpackable inside it.
REFUSAL_LEAVE = "leave"
REFUSAL_ARCHIVE_ONLY = "archive-only"
REFUSAL_ARCHIVE_LEAVE = "archive-leave"
# The app's import drop zone: never a row, never absorbing.
REFUSAL_IMPORT_AREA = "import-area"
# A subtree the report's entry budget stopped before measuring. Offered nowhere,
# because half a census cannot be ranked honestly.
REFUSAL_TRUNCATED = "truncated"

# Every reason a refusal row may carry.
REFUSAL_RULES = frozenset(
    {
        REFUSAL_CONTAINER,
        REFUSAL_DUPLICATE,
        REFUSAL_NESTED_MEMBER,
        REFUSAL_TWIN_CONSUMED,
        REFUSAL_TWIN_UNVERIFIED,
        REFUSAL_UNREADABLE_ARCHIVE,
        REFUSAL_MEMBER_OF_BUNDLE,
        REFUSAL_LEAVE,
        REFUSAL_ARCHIVE_ONLY,
        REFUSAL_ARCHIVE_LEAVE,
        REFUSAL_IMPORT_AREA,
        REFUSAL_TRUNCATED,
    }
)


@dataclass(frozen=True)
class BundleCandidate:
    """One row: a folder or a `.zip`, and the engine's own reading of it.

    Since Mf-1agz the ENGINE decides which paths are rows and what each row
    recommends; this service only resolves a row against the database, renders it
    and enacts the operator's choice. So a candidate carries the measurement and
    the recommended option, and nothing else: no confidence of its own, no second
    reading, no shape guess.

    ``rel`` is the walk-relative path (what the scan keyed things by), while
    ``facts.rel`` is storage-relative (what the engine compared `project_keys`
    against). ``files`` is the row's OWN subtree listing at scan time -
    bundle-relative ``(path, size)`` - captured by the walk that already had it, so
    twin verification and the create's ownership question never re-read the disk.
    ``archive_path``/``entry_root`` name the `.zip` that is this folder's
    provenance, and which pairing route found it.
    """

    key: str
    rel: str
    facts: object
    options: Tuple[proposal_engine.Option, ...]
    is_archive: bool
    files: Tuple[Tuple[str, int], ...] = ()
    archive_path: Optional[str] = None
    entry_root: Optional[str] = None

    @property
    def option(self) -> proposal_engine.Option:
        """The recommendation: the engine's first option, never re-derived."""
        return self.options[0]

    @property
    def creatable_shapes(self) -> FrozenSet[str]:
        """The shapes a create can enact for a row of this kind."""
        return ARCHIVE_CREATABLE_SHAPES if self.is_archive else DIRECTORY_CREATABLE_SHAPES


@dataclass(frozen=True)
class BundleRefusal:
    """One path the scan saw and did NOT offer, with the rule that refused it.

    ``kind`` says what the path is on disk - ``archive`` or ``directory`` - so a
    consumer can tell a missing row from a missing folder without a second walk.
    """

    rel_path: str
    rule: str
    kind: str


@dataclass(frozen=True)
class BundleScan:
    """One walk of the storage tree: the rows, the duplicates, the refusals."""

    candidates: List[BundleCandidate]
    truncated: bool
    duplicates: Tuple[library_scan.DuplicateGroup, ...] = ()
    # Every path the walk saw and did not offer, sorted and named by its rule.
    # Derived from the candidate set, never an input to it: adding a refusal can
    # never change which rows are offered.
    refusals: Tuple[BundleRefusal, ...] = ()

    @property
    def by_key(self) -> Dict[str, BundleCandidate]:
        return {candidate.key: candidate for candidate in self.candidates}


def _entry_size(entry) -> int:
    """One ``stat`` per file; a file that vanished mid-walk is simply zero bytes."""
    try:
        return int(entry.stat(follow_symlinks=False).st_size)
    except OSError:
        return 0


def _content_digest(path: Path) -> Optional[str]:
    """The sha256 of one archive's bytes, read in bounded chunks; ``None`` on failure.

    An unreadable archive is simply not evidence of a duplicate, never an
    exception on a read route.
    """
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _suffix(path: str) -> str:
    return PurePath(path or "").suffix.lower()


def _depth(rel_dir: str) -> int:
    return len([part for part in rel_dir.split("/") if part])


def _parent(rel_dir: str) -> str:
    return rel_dir.rsplit("/", 1)[0] if "/" in rel_dir else ""


def _basename(rel_dir: str) -> str:
    return rel_dir.rsplit("/", 1)[-1]


def _refuse(ledger: Dict[str, str], rel_path: str, rule: str, *, override: bool = False) -> None:
    """Record why ``rel_path`` is not offered. The FIRST refusal wins by default.

    First-wins is what makes the ledger stable: the row walk sees a directory
    before any later pass does, so its reason is the most specific one. A pass
    that has strictly better information passes ``override=True``.
    """
    if override:
        ledger[rel_path] = rule
    else:
        ledger.setdefault(rel_path, rule)


def _subtree(files_by_dir: Mapping[str, Sequence[Tuple[str, int]]]) -> Dict[str, List[Tuple[str, int]]]:
    """Every entry below each directory, folded deepest-first: O(files), not O(dirs x files)."""
    folded: Dict[str, List[Tuple[str, int]]] = {
        rel_dir: [(name, size) for name, size in files]
        for rel_dir, files in files_by_dir.items()
    }
    for rel_dir in sorted(folded, key=lambda item: (-_depth(item), item)):
        if not rel_dir:
            continue
        parent = _parent(rel_dir)
        prefix = _basename(rel_dir)
        folded.setdefault(parent, []).extend(
            (f"{prefix}/{name}", size) for name, size in folded[rel_dir]
        )
    return folded


def _walk_tree(root: Path) -> Tuple[Dict[str, List[Tuple[str, int]]], Dict[str, set], bool]:
    """One bounded walk: each directory's own files, the directory tree, truncation."""
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
    return files_by_dir, dirs_by_parent, truncated


# --- the row walk (Mf-1agz) ------------------------------------------------
#
# ONE engine ranks, and this is the seam: the walk asks the engine about a folder
# and acts on its answer. The rule, in the engine's own vocabulary:
#
#   * a `leave` reading (nothing to import here) descends: the answer may be one of
#     the children. An `archive-only` reading - no models, but archives - descends
#     for the same reason: the zips below it are their own rows.
#   * a CLAIMING reading (`one-project`/`git-project`/`add-to-project`) is a row and
#     descent STOPS: the folder is the product, and its children are parts of it.
#   * a SPLIT reading (`split-top-folders`/`split-model-folders`) is a row and
#     descent stops too: the split's children ARE the split, so a row per child
#     would offer the same files twice.
#   * the app's `import/` area is transparent: never a row, never absorbing.
#   * a `.zip` is a row of its own through `measure_archive`/`propose_archive`,
#     unless it is a duplicate, a member of an offered folder, or one of those
#     folders' provenance (the twin).
#
# Measured over the live tree: 175 rows (154 one-project, 5 split, 16 zip). The
# derivation, the row-level before/after and the 73 dropped members are recorded in
# `.plans/measurements/Mf-1agz-after-summary.md`; `tests/test_bundle_report_engine.py`
# keeps the invariant that the report's recommendation IS the engine's.


def _row_rel(scope: str, rel_dir: str) -> str:
    """The storage-relative spelling of a walk-relative row key."""
    if not rel_dir:
        return scope
    return _qualify(scope, rel_dir)


def _measure_row(root: Path, rel_dir: str, *, scope: str) -> proposal_engine.Facts:
    """One folder, measured into the facts the row renders and ranks.

    ``max_entries`` is the report's own walk bound: a row is rendered from a walk
    inside a GET, so a pathological folder must not make one request unbounded. A
    walk that hits the bound says so (``Facts.truncated``) and the row is refused
    rather than offered on half a census.
    """
    return proposal_engine.measure(
        str(Path(root) / rel_dir), _row_rel(scope, rel_dir), max_entries=MAX_SCAN_ENTRIES
    )


def _directory_rows(
    root: Path,
    dirs_by_parent: Mapping[str, set],
    folded: Mapping[str, Sequence[Tuple[str, int]]],
    readings: Dict[str, str],
    refusals: Dict[str, str],
    *,
    scope: str,
    project_keys: AbstractSet[str],
) -> Tuple[List[BundleCandidate], bool]:
    """Every directory ROW, top-down, and whether the entry bound stopped the walk.

    ``readings`` is the ledger half of the walk: the engine's word for every folder
    it looked at and did not offer, so ``_reconcile_refusals`` can name each one
    without measuring anything twice.
    """
    rows: List[BundleCandidate] = []
    truncated = False
    stack: List[str] = [""]
    while stack:
        parent = stack.pop()
        for name in sorted(dirs_by_parent.get(parent, ()), key=lambda item: (item.casefold(), item)):
            rel_dir = f"{parent}/{name}" if parent else name
            if rel_dir in TRANSPARENT_DIRS:
                readings[rel_dir] = REFUSAL_IMPORT_AREA
                stack.append(rel_dir)
                continue
            facts = _measure_row(root, rel_dir, scope=scope)
            if facts.truncated:
                # Half a census cannot be ranked honestly, and it must not be
                # presented as a total: the report says it was truncated and this
                # path is refused by name.
                _refuse(refusals, rel_dir, REFUSAL_TRUNCATED)
                readings[rel_dir] = REFUSAL_TRUNCATED
                truncated = True
                continue
            options, _reasons, _repo = proposal_engine.propose(
                facts, project_keys, git_timeout=GIT_PROBE_TIMEOUT
            )
            top = options[0] if options else None
            readings[rel_dir] = top.shape if top is not None else REFUSAL_LEAVE
            if top is None or top.shape in DESCEND_SHAPES:
                stack.append(rel_dir)
                continue
            rows.append(
                BundleCandidate(
                    key=rel_dir,
                    rel=rel_dir,
                    facts=facts,
                    options=tuple(options),
                    is_archive=False,
                    files=tuple(folded.get(rel_dir, ())),
                )
            )
    return rows, truncated


def _archive_row(
    root: Path, rel_path: str, refusals: Dict[str, str], *, scope: str
) -> Optional[BundleCandidate]:
    """One `.zip` as its own row, through the archive engine.

    An unreadable archive is named, never an exception on a read route. A zip the
    archive engine reads as `archive-leave` (nothing inside) is refused with that
    word: there is no project to make from it.
    """
    zip_path = Path(root) / rel_path
    try:
        zip_service.list_zip_entries(zip_path)
    except (BadZipFile, OSError):
        _refuse(refusals, rel_path, REFUSAL_UNREADABLE_ARCHIVE)
        return None
    facts = proposal_engine.measure_archive(str(zip_path), _row_rel(scope, rel_path))
    options, _reasons = proposal_engine.propose_archive(facts)
    top = options[0] if options else None
    if top is None or top.shape not in ARCHIVE_CREATABLE_SHAPES:
        _refuse(refusals, rel_path, REFUSAL_ARCHIVE_LEAVE)
        return None
    return BundleCandidate(
        key=rel_path, rel=rel_path, facts=facts, options=tuple(options), is_archive=True
    )


def _attach_archive(
    candidate: BundleCandidate, zip_rel: str, *, entry_root: Optional[str]
) -> BundleCandidate:
    """One row's provenance archive: the shortest name wins, deterministically.

    A directory can pair with more than one archive (a stem twin and a renamed
    extraction), and the row reports ONE. ``_paired_archive``'s ordering is the
    authoritative path, so the choice never depends on readdir order.
    """
    pairs: List[Tuple[str, Optional[str]]] = []
    if candidate.archive_path:
        pairs.append((candidate.archive_path, candidate.entry_root))
    pairs.append((zip_rel, entry_root))
    archive_path, chosen_root = _paired_archive(pairs)
    return replace(candidate, archive_path=archive_path, entry_root=chosen_root)


def _build_candidates(
    root: Path,
    files_by_dir: Mapping[str, Sequence[Tuple[str, int]]],
    dirs_by_parent: Mapping[str, set],
    suppressed: AbstractSet[str],
    refusals: Dict[str, str],
    readings: Dict[str, str],
    *,
    scope: str = "",
    project_keys: AbstractSet[str] = frozenset(),
) -> Tuple[List[BundleCandidate], bool]:
    """Every ROW under ``root``, from the engine's own reading of each path.

    ``refusals`` and ``readings`` are the ledger this function WRITES: every path it
    declines gets a rule, so a caller can say why something is missing. Neither is
    ever read back to decide anything, so recording a refusal cannot change which
    rows are offered. ``suppressed`` holds the duplicate archives: a byte-identical
    copy is not a bundle of its own, so it is skipped here and reported once, in
    ``duplicates``. ``project_keys`` are storage-relative marker keys, handed to the
    engine so an `add-to-project` reading is possible at all.
    """
    folded = _subtree(files_by_dir)
    rows, truncated = _directory_rows(
        root, dirs_by_parent, folded, readings, refusals,
        scope=scope, project_keys=project_keys,
    )
    by_key: Dict[str, BundleCandidate] = {candidate.key: candidate for candidate in rows}
    offered_dirs = frozenset(by_key)
    archives: List[BundleCandidate] = []
    for rel_path in sorted(_zip_files(files_by_dir), key=lambda item: (item.casefold(), item)):
        if rel_path in suppressed:
            _refuse(refusals, rel_path, REFUSAL_DUPLICATE)
            continue
        if file_utils.is_bundle_member(rel_path, offered_dirs):
            # A zip strictly below a row is a supporting file of that row: never a
            # second row and never a pairing (the nested-member rule, Mf-74x5).
            _refuse(refusals, rel_path, REFUSAL_NESTED_MEMBER)
            continue
        twin = _twin_directory(rel_path, dirs_by_parent)
        if twin is not None and twin in by_key:
            # The directory is a row on its own: the archive is its provenance, and
            # a second row from the same bytes would be a duplicate.
            by_key[twin] = _attach_archive(by_key[twin], rel_path, entry_root=None)
            _refuse(refusals, rel_path, REFUSAL_TWIN_CONSUMED)
            continue
        if twin is not None and not _stem_pair_verified(root / rel_path, folded.get(twin, ())):
            # The stem is the operator's own statement of which directory this
            # archive is, and a failed check is never retried under a weaker name.
            _refuse(refusals, rel_path, REFUSAL_TWIN_UNVERIFIED)
            continue
        paired = _entry_root_pair(root, rel_path, dirs_by_parent, by_key)
        if paired is not None:
            folder, entry_root = paired
            by_key[folder] = _attach_archive(by_key[folder], rel_path, entry_root=entry_root)
            _refuse(refusals, rel_path, REFUSAL_TWIN_CONSUMED)
            continue
        candidate = _archive_row(root, rel_path, refusals, scope=scope)
        if candidate is not None:
            archives.append(candidate)
    return [by_key[candidate.key] for candidate in rows] + archives, truncated


def scan_bundles(
    root: Path,
    *,
    excluded_keys: AbstractSet[str] = frozenset(),
    project_keys: AbstractSet[str] = frozenset(),
    scope: str = "",
) -> BundleScan:
    """One walk of the storage tree: the rows plus the duplicate archives.

    The rows come from the engine's own top-down reading (``_build_candidates``), so
    there is no second ranking here and no pass that could disagree with it.

    ``excluded_keys`` names the storage subtrees an existing project already owns,
    in walk-relative spelling. The walk is PRUNED of every such subtree before any
    pass runs, which is what makes "a path inside an existing project is not a
    detection row" structural rather than a filter applied afterwards: no candidate,
    no refusal and no duplicate inside a project key can ever be produced. The
    project KEY ITSELF is never excluded - ``_strictly_inside_any`` is strict - so
    the row for the project's own directory survives and can still report
    ``already_created``.

    ``project_keys`` are the same keys in STORAGE-relative spelling, handed to the
    engine because that is the space ``Facts.rel`` lives in; an `add-to-project`
    reading is impossible without them. ``scope`` is the storage-relative prefix
    the walk root stands for, so a scoped report's rows answer the same keys a
    whole-storage one would.

    Byte-identical archives are one bundle, so the walk also groups duplicates by
    content hash and suppresses every copy from the archive pass.
    """
    root = Path(root)
    files_by_dir, dirs_by_parent, truncated = _walk_tree(root)
    if excluded_keys:
        files_by_dir, dirs_by_parent = _without_owned_subtrees(files_by_dir, dirs_by_parent, excluded_keys)
    duplicates = duplicate_archives(root, files_by_dir)
    suppressed = frozenset(library_scan.duplicate_archive_paths(duplicates))
    refusals: Dict[str, str] = {}
    readings: Dict[str, str] = {}
    candidates, engine_truncated = _build_candidates(
        root, files_by_dir, dirs_by_parent, suppressed, refusals, readings,
        scope=scope, project_keys=project_keys,
    )
    bundle_dirs = {candidate.key for candidate in candidates if not candidate.is_archive}
    nested = _nested_archives(files_by_dir, bundle_dirs)
    duplicates = _without_nested_members(duplicates, nested)
    return BundleScan(
        candidates=candidates,
        truncated=truncated or engine_truncated,
        duplicates=duplicates,
        refusals=_reconcile_refusals(
            refusals,
            candidates,
            readings=readings,
            nested=nested,
            suppressed=suppressed,
            files_by_dir=files_by_dir,
        ),
    )


def _strictly_inside_any(rel_path: str, keys: AbstractSet[str]) -> bool:
    """Whether ``rel_path`` sits below one of ``keys``, walking its ancestors."""
    parent = _parent(rel_path)
    while parent:
        if parent in keys:
            return True
        parent = _parent(parent)
    return False


def _reconcile_refusals(
    refusals: Dict[str, str],
    candidates: Sequence[BundleCandidate],
    *,
    readings: Mapping[str, str],
    nested: AbstractSet[str],
    suppressed: AbstractSet[str],
    files_by_dir: Mapping[str, Sequence[Tuple[str, int]]],
) -> Tuple[BundleRefusal, ...]:
    """One reason per path the walk saw, settled from the FINAL row set.

    The passes record reasons as they go, which means a keep can still look like a
    refusal until the whole row set is known - an archive paired with a directory is
    "consumed", not missing. This step reads the final set and settles each entry, so
    the ledger never contradicts the rows. It only ever edits the ledger: no candidate
    is added or dropped here.

    Every directory the walk saw is named exactly once, in one of four ways: it is a
    row (no entry), it sits INSIDE a row (``member-of-bundle``), it HOLDS a row
    (``container-rule``), or it is a folder the engine read as `leave`/`archive-only`
    (that word). The last arm is the one the engine's own vocabulary supplies, and
    ``readings`` carries it so the walk never measures a folder twice.
    """
    offered_keys = {candidate.key for candidate in candidates}
    offered_dirs = {candidate.key for candidate in candidates if not candidate.is_archive}
    attached = {candidate.archive_path for candidate in candidates if candidate.archive_path}
    all_zips = set(_zip_files(files_by_dir))
    # Every strict ancestor of every row, computed once: "does this folder hold a
    # row" is then a set membership rather than a prefix scan per directory.
    ancestors: set = set()
    for key in offered_keys:
        parent = _parent(key)
        while parent:
            ancestors.add(parent)
            parent = _parent(parent)

    for rel_path in all_zips:
        if rel_path in offered_keys:
            refusals.pop(rel_path, None)
        elif rel_path in nested:
            refusals[rel_path] = REFUSAL_NESTED_MEMBER
        elif rel_path in suppressed:
            refusals[rel_path] = REFUSAL_DUPLICATE
        elif rel_path in attached:
            refusals[rel_path] = REFUSAL_TWIN_CONSUMED

    for rel_path in sorted(files_by_dir, key=lambda item: (item.casefold(), item)):
        if not rel_path or rel_path in all_zips:
            continue
        if rel_path in offered_keys:
            refusals.pop(rel_path, None)
        elif _strictly_inside_any(rel_path, offered_dirs):
            refusals[rel_path] = REFUSAL_MEMBER_OF_BUNDLE
        elif rel_path in ancestors:
            refusals[rel_path] = REFUSAL_CONTAINER
        elif rel_path in readings:
            refusals[rel_path] = readings[rel_path]
        else:
            # The walk never reached it: the entry bound or a vanished directory.
            refusals.setdefault(rel_path, REFUSAL_TRUNCATED)

    return tuple(
        BundleRefusal(
            rel_path=rel_path,
            rule=refusals[rel_path],
            kind="archive" if rel_path in all_zips else "directory",
        )
        for rel_path in sorted(refusals, key=lambda path: (path.casefold(), path))
    )


def iter_bundle_candidates(root: Path) -> Tuple[List[BundleCandidate], bool]:
    """Every bundle under ``root``, plus whether the walk was cut short.

    The signature is the callers' contract; the walk and its reasoning live in
    ``scan_bundles``, which also reports the duplicate archives.
    """
    scan = scan_bundles(root)
    return scan.candidates, scan.truncated


def _zip_entries(files_by_dir: Mapping[str, Sequence[Tuple[str, int]]]) -> List[Tuple[str, int]]:
    """Every archive in the walk as ``(storage-relative path, size)``."""
    return [
        (f"{rel_dir}/{name}" if rel_dir else name, size)
        for rel_dir, files in files_by_dir.items()
        for name, size in files
        if _suffix(name) == ".zip"
    ]


def _zip_files(files_by_dir: Mapping[str, Sequence[Tuple[str, int]]]) -> List[str]:
    return [rel_path for rel_path, _size in _zip_entries(files_by_dir)]


def _nested_archives(
    files_by_dir: Mapping[str, Sequence[Tuple[str, int]]], bundle_dirs: AbstractSet[str]
) -> set:
    """Every archive that is a file of one of ``bundle_dirs``.

    A member is a supporting file of the enclosing bundle: it is never a
    candidate, never a pairing, and never offered for deletion. Computed from
    every walk entry, including suppressed duplicates, so a member cannot slip
    back in through the suppression set.
    """
    if not bundle_dirs:
        return set()
    return {
        rel_path
        for rel_path in _zip_files(files_by_dir)
        if file_utils.is_bundle_member(rel_path, bundle_dirs)
    }


def duplicate_archives(
    root: Path, files_by_dir: Mapping[str, Sequence[Tuple[str, int]]]
) -> Tuple[library_scan.DuplicateGroup, ...]:
    """Byte-identical archives, grouped by sha256, hashing only shared-size entries.

    Size-first is the bound: a byte-identical copy is necessarily equal-size, so
    an archive whose size occurs once is never opened. The worst case is a library
    where every archive is the same size - then each archive is read exactly once,
    never more. An unreadable archive contributes no identity and no group.
    """
    identities: List[library_scan.ArchiveIdentity] = []
    for size, paths in library_scan.equal_size_archive_groups(_zip_entries(files_by_dir)):
        for rel_path in paths:
            digest = _content_digest(Path(root) / rel_path)
            if digest is not None:
                identities.append(library_scan.ArchiveIdentity(rel_path=rel_path, size=size, digest=digest))
    return library_scan.duplicate_groups(identities)


def _without_nested_members(
    groups: Sequence[library_scan.DuplicateGroup], nested: AbstractSet[str]
) -> Tuple[library_scan.DuplicateGroup, ...]:
    """Drop bundle members from every duplicate group.

    A member of a live bundle is never offered for deletion, so it is removed
    from the group outright - the head included, because a member is not the
    "keep" of anything. A group that keeps fewer than two paths has no
    reclaimable bytes left and disappears. The residual is honest: an outside
    copy whose only twin is a member is either still offered as its own archive
    bundle (when it was the head) or was already suppressed (when it was not),
    and the report never invites deleting a file that is part of a live bundle.
    """
    if not nested:
        return tuple(groups)
    kept: List[library_scan.DuplicateGroup] = []
    for group in groups:
        paths = tuple(path for path in group.paths if path not in nested)
        if len(paths) < 2:
            continue
        kept.append(library_scan.DuplicateGroup(digest=group.digest, size=group.size, paths=paths))
    return tuple(kept)


def _twin_directory(rel_path: str, dirs_by_parent: Mapping[str, set]) -> Optional[str]:
    parent = _parent(rel_path)
    wanted = proposal_engine.twin_key(_basename(rel_path))
    for name in sorted(dirs_by_parent.get(parent, ())):
        if proposal_engine.twin_key(name) == wanted:
            return f"{parent}/{name}" if parent else name
    return None


def _twin_archive(rel_dir: str, files_by_dir: Mapping[str, Sequence[Tuple[str, int]]]) -> Optional[str]:
    parent = _parent(rel_dir)
    wanted = proposal_engine.twin_key(_basename(rel_dir))
    for name, _size in sorted(files_by_dir.get(parent, ())):
        if _suffix(name) != ".zip":
            continue
        if proposal_engine.twin_key(name) == wanted:
            return f"{parent}/{name}" if parent else name
    return None


def _entry_root_pair(
    root: Path,
    rel_path: str,
    dirs_by_parent: Mapping[str, set],
    rows_by_key: Mapping[str, BundleCandidate],
) -> Optional[Tuple[str, str]]:
    """``(folder_rel, entry_root)`` for an archive that pairs by its entry root.

    Same-parent only: a root naming an ancestor or a directory elsewhere in the
    library is not evidence that the two sit together, and pairing across parents
    would attach one archive to a directory it was never extracted into. The
    comparison set is the ROW's own subtree listing, so pairing can never disagree
    with the row it is attaching to. A generic container root is refused outright
    (the name says nothing about which thing the archive holds), and any read
    failure is ``None`` - never an exception on a read route.
    """
    zip_path = Path(root) / rel_path
    try:
        entries = zip_service.list_zip_entries(zip_path)
    except (BadZipFile, OSError):
        return None
    entry_root = proposal_engine.common_entry_root([str(entry["path"]) for entry in entries])
    if entry_root is None or file_utils.is_generic_container_root(entry_root):
        return None
    parent = _parent(rel_path)
    wanted = entry_root.casefold()
    for name in sorted(dirs_by_parent.get(parent, ())):
        if name.casefold() != wanted:
            continue
        folder = f"{parent}/{name}" if parent else name
        row = rows_by_key.get(folder)
        if row is None:
            continue
        if entry_root_verified(zip_path, entry_root, row.files):
            return folder, entry_root
    return None


def _paired_archive(pairs: Sequence[Tuple[str, Optional[str]]]) -> Tuple[str, Optional[str]]:
    """``(zip_rel, entry_root)`` to report for one directory.

    The shortest name wins, ties by (casefold, raw): a browser's `` (1)``
    duplicate convention only ever appends characters, so the shortest name is the
    original file. Hash identity for duplicates lives in ``duplicate_archives``;
    this pass still picks the shortest name, because the copy is suppressed before
    pairing and the choice only has to be deterministic.

    The entry root is ``None`` on the stem route, so a caller must dispatch on
    ``entry_root`` before deciding how to re-verify the archive.
    """
    return min(pairs, key=lambda pair: (len(pair[0]), pair[0].casefold(), pair[0]))


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


def registered_index(session: Session) -> List[Tuple[str, str, int]]:
    """Every registered asset as ``(id, storage_path, size)``, in one statement.

    Built ONCE per report beside the membership index, because drift is answered
    from these two lists by string prefix and dictionary lookup. Reading the set
    per row would be one statement per candidate, so this is the difference
    between one query whatever the candidate count and one per candidate.

    Only rows with a ``storage_path`` are indexed: a row with no location is not
    a file under any bundle's root, the same refusal ``registered_assets`` makes.
    Ordered by ``(storage_path, id)`` so every consumer sees the same order.
    """
    statement = (
        select(Asset.id, Asset.storage_path, Asset.size)
        .where(Asset.storage_path.is_not(None))
        .order_by(Asset.storage_path, Asset.id)
    )
    return [(row[0], row[1], int(row[2] or 0)) for row in session.exec(statement)]


class RegisteredAsset(NamedTuple):
    """One registered asset row, as the bundle report needs to read it.

    A named tuple rather than six positional values: ``_cover`` sorts on ``size``
    and ``path`` while the project gets ``id``, and a positional index in a sort
    key is the kind of thing that silently swaps two strings.
    """

    id: str
    path: str
    size: int
    mime: str
    thumb_version: int
    filename: str


def registered_from_rows(rows: Sequence[Asset]) -> List[RegisteredAsset]:
    """The shape a cover rule sorts, from rows the caller already holds.

    A door that has just created its assets holds them, so re-reading the table
    for bytes it was handed would be a second query for the same rows. A row with
    no `storage_path` reads as the empty path rather than being dropped: the empty
    path has no suffix, so the cover rule classifies it as neither an image nor a
    model - the honest answer for a row with no location.
    """
    return [
        RegisteredAsset(
            id=asset.id,
            path=asset.storage_path or "",
            size=int(asset.size or 0),
            mime=asset.mime or "",
            thumb_version=int(asset.thumb_version or 0),
            filename=asset.filename or "",
        )
        for asset in rows
    ]


def registered_assets(session: Session, rel_dir: str) -> List[RegisteredAsset]:
    """Every registered asset under ``rel_dir``, with what a report row needs.

    ``thumb_version`` is normalised to 0 for an absent column (= no thumbnail),
    and ``filename`` to an empty string, so neither reaches a URL builder as None.
    """
    prefix = (rel_dir or "") + "/"
    statement = (
        select(Asset)
        .where(
            Asset.storage_path.is_not(None),
            func.substr(Asset.storage_path, 1, len(prefix)) == prefix,
        )
        .order_by(Asset.storage_path)
    )
    return registered_from_rows(list(session.exec(statement)))


@dataclass(frozen=True)
class ProjectMember:
    """One registered asset under a bundle key, and the role its entry earned."""

    asset: RegisteredAsset
    # library_scan.classify_entry(...) -> model | image | text | other | archive
    role: str
    # BUNDLE-relative: the asset's storage path minus ``key + "/"``.
    rel_path: str


@dataclass(frozen=True)
class MembersOf:
    """Every member of one bundle, in the order the caller's asset list arrived."""

    entries: Tuple[ProjectMember, ...]

    def by_role(self, role: str) -> Tuple[ProjectMember, ...]:
        """The members whose role is ``role``, in input order. THE one view on the set."""
        return tuple(entry for entry in self.entries if entry.role == role)


def _members_of(key: str, assets: Sequence[RegisteredAsset]) -> MembersOf:
    """THE member set of one bundle: every registered asset under ``key``.

    Membership is exactly "every registered asset under the key" - models, images,
    text documents, unknowns and nested archives alike. There is no suffix table
    deciding who is a member, here or anywhere else in this library.

    The ROLE of each member is decided once, by the scan's own classifier
    (``library_scan.classify_entry``, over the BUNDLE-relative path and the declared
    size), which is the very call the report's own ``model_count``/``image_count``/
    ``text_count``/``other_count``/``archive_count`` are built from. So a project and
    the row it was created from can never disagree about what a file is.

    Input order is preserved verbatim (``registered_assets`` orders by path), so the
    ``ProjectAsset.position`` a create writes stays byte-identical to what the same
    list produced before this function existed.

    ``ValueError`` for an asset whose path is not under ``key + "/"``: that is a
    caller handing in another key's list, and a silently dropped member would be a
    project quietly missing a file.
    """
    prefix = f"{key}/"
    entries: List[ProjectMember] = []
    for asset in assets:
        if not asset.path.startswith(prefix):
            raise ValueError(f"asset {asset.path!r} is not under bundle key {key!r}")
        rel_path = asset.path[len(prefix):]
        entries.append(
            ProjectMember(
                asset=asset,
                role=library_scan.classify_entry(rel_path, asset.size),
                rel_path=rel_path,
            )
        )
    return MembersOf(entries=tuple(entries))


# The public name for the member rule (Mf-215n). An ALIAS, never a copy: the
# operations planner, the create primitive and any later door must ask one
# function, or two doors will eventually disagree about who is a member of a
# bundle. `_members_of` stays because the create door and thirty tests name it,
# and a rename here would be a rename of the rule's witness, not of the rule.
members_of = _members_of


def attached_by_group(key: str, members: Sequence["project_create_service.MemberCandidate"], model_ids: Sequence[str]) -> Dict[str, int]:
    """How many of a create's counted MODELs land in each of the bundle's groups.

    The answer the marker's `attached` field carries, computed once: the bucket is
    the first path segment after the key, which is the engine's own grouping and
    the same vocabulary `wire_groups`/`marker_groups` speak. `model_ids` is the
    answer to "which of these members is a printed part" - the create door reads it
    off `MembersOf.by_role(library_scan.MODEL)`, the operations planner off its own
    role walk - and passing it in is what keeps this helper from deciding the role
    question a second time.

    A member that is not in `model_ids` is not counted; a member whose path is not
    under `key` would bucket wrongly, and `members_of` has already refused that
    case at the door that built the list.
    """
    wanted = set(model_ids)
    prefix = f"{key}/"
    counts: Dict[str, int] = {}
    for member in members:
        if member.asset_id not in wanted:
            continue
        group_name = member.path[len(prefix):].split("/", 1)[0]
        counts[group_name] = counts.get(group_name, 0) + 1
    return counts


def create_project(
    session: Session,
    *,
    key: str,
    name: Optional[str] = None,
    members: Optional[Sequence[project_create_service.MemberCandidate]] = None,
    model_ids: Optional[Sequence[str]] = None,
    marker: Optional[dict] = None,
    groups: Sequence[Mapping] = (),
    excluded_groups: Sequence[str] = (),
    cover_asset_id: Optional[str] = None,
    dedupe: Optional[project_create_service.Dedupe] = None,
    require_members: bool = True,
    refuse: Optional[str] = None,
    asset_index: Optional[Mapping[str, Tuple[str, ...]]] = None,
) -> project_create_service.CreateOutcome:
    """Create the project a library PATH describes, once, whatever the caller re-tries.

    This lives here, beside `members_of` and `attached_by_group`, because the verb
    asks the member rule and the marker vocabulary; `project_create_service` owns the
    dedupe and the single write it delegates to, and it deliberately cannot name this
    module - the archive-extract door imports it, and
    `tests/test_archive_project.py` keeps every bundle module out of that door's
    static import closure.

    The order is the contract:

      1. resolve the path's members - every registered file under `key`, with the
         role the scan gave it - unless the door hands in its own set (`members`),
         which is what the operations runner does so that the step it executes is
         the step the operator reviewed;
      2. drop the members an excluded group owns, so a group the operator said is
         not part of this product can never be a member of it;
      3. merge the groups into the marker: `marker_groups` with the
         `attached_by_group` counts, which is the same pairing the detection door
         and the adopt path use, so a project's `attached` means one thing whichever
         door made it;
      4. ask the dedupe with the identities this verb holds - the path's marker key
         and the project name - and then write through
         `project_create_service.create_project_from_members`, so the project and its
         memberships land in
         one transaction.

    It writes NO audit row, and cannot: there is no `project.create` action in
    `audit.ACTIONS`, and the ticket that built this primitive was forbidden to add
    one. The door that asked audits - `bundle.create` for the detection door, the
    operation's own lifecycle rows for the operations runner - so the record exists
    and it is the door's, which is also where the actor and the request id live.

    It does not pick the cover. A cover is a choice made from evidence the path does
    not hold - the artifact's facts, the operator's click, a recognised preview - so
    the door hands one in and this verb only guarantees that a cover outside the
    member set is refused (the guard in `create_project_from_members`).
    """
    key = (key or "").strip().strip("/")
    if not key:
        raise ValueError("a project is created from a library-relative path, not the library root")

    if members is None:
        entries = members_of(key, registered_assets(session, key)).entries
        members = [project_create_service.MemberCandidate(entry.asset.id, entry.asset.path) for entry in entries]
        model_ids = [entry.asset.id for entry in entries if entry.role == library_scan.MODEL]
    model_ids = list(model_ids or ())

    prefixes = tuple(f"{key}/{group}" for group in excluded_groups if group)
    if prefixes:
        members = [member for member in members if not member.path.startswith(prefixes)]
        kept = {member.asset_id for member in members}
        model_ids = [asset_id for asset_id in model_ids if asset_id in kept]

    stored = dict(marker or {})
    stored.setdefault("key", key)
    stored.setdefault("rel_dir", key)
    if groups:
        stored["groups"] = marker_groups(
            list(groups),
            attached=attached_by_group(key, members, model_ids),
            excluded=set(excluded_groups),
        )

    resolved_name = (name or "").strip() or key.rsplit("/", 1)[-1]
    return project_create_service.create_project_from_members(
        session,
        name=resolved_name,
        origin=project_create_service.ORIGIN_DETECTION,
        members=members,
        # ALWAYS a marker: this verb's identity is the path, and the marker's `key`
        # is the only place that identity is written. A create from a path with no
        # marker would be findable only by name, which is how one folder became two
        # projects with two names.
        marker=stored,
        dedupe=dedupe if dedupe is not None else project_create_service.Dedupe(key=key, name=resolved_name),
        cover_asset_id=cover_asset_id,
        require_members=require_members,
        refuse=refuse,
        asset_index=asset_index,
    )


def _own_paths(key: str, candidate: BundleCandidate) -> set:
    """The storage paths the row's own subtree listing names, never a child's."""
    return {f"{key}/{rel_path}" for rel_path, _size in candidate.files}


def _own_assets(
    assets: Sequence[RegisteredAsset], *, key: str, candidate: BundleCandidate
) -> List[RegisteredAsset]:
    """The registered assets this candidate's OWN snapshot names.

    The ownership half of the answer, and ONE expression for it: a registered asset
    below a nested child bundle belongs to that bundle, not to this row, so a
    recursive prefix read is never the owner's list. Read by the report (cover,
    the create-time belt) and by the create door alike.
    """
    own = _own_paths(key, candidate)
    return [asset for asset in assets if asset.path in own]


def _claimed_assets(
    assets: Sequence[RegisteredAsset], *, key: str, claims: Optional[Mapping[str, str]]
) -> List[RegisteredAsset]:
    """``_own_assets`` narrowed by the report-wide one-owner map.

    THE ownership expression the report and the create door share (Mf-quv4's
    hand-off, closed here). Two rows can name one path - a parent's snapshot and a
    child's - and ``_claim_owners`` settled which one the deepest-first walk gave
    it to, so a container neither reports ready over a child's models nor attaches
    them. ``claims is None`` is the inert re-offered group: it owns nothing and
    forfeits nothing, so it keeps every asset its own snapshot names.

    A path the map is silent about stays with the row that named it, which is what
    lets this one expression serve a list of ALL kinds: the map indexes MODELS only
    (``_claim_owners``), so an image or a README has no entry and no entry means no
    owner, not a forfeit. Written as ``claims.get(asset.path, key)``, not
    ``claims.get(asset.path)``: the latter would drop every non-model, and a
    create's member set holds every registered file under the key (``_members_of``)
    - images, documents, unknowns and nested archives. For the models the map does
    index, the two forms answer identically - every model a non-archive candidate
    names is in it.
    """
    if claims is None:
        return list(assets)
    return [asset for asset in assets if claims.get(asset.path, key) == key]


def twin_verified(zip_path: Path, folder_files: Sequence[Tuple[str, int]]) -> bool:
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
    folder_names = sorted({PurePath(rel_path).name.casefold() for rel_path, _size in folder_files})
    return archive_names == folder_names


def _stem_pair_verified(zip_path: Path, folder_files: Sequence[Tuple[str, int]]) -> bool:
    """Whether an archive verifies as the twin of the directory beside it.

    The operator's names agree, so they are not the evidence - the payload has to
    agree too, or the archive is not that directory's provenance. The archive's own
    entries decide the wrapper - identity first, else ONE ``common_entry_root``
    strip, never per entry and never two strips - through the engine's ONE
    containment predicate. Declared size is header data, so this is pairing
    evidence and never an integrity claim. Any read failure is ``False``.
    """
    try:
        entries = zip_service.list_zip_entries(Path(zip_path))
    except (BadZipFile, OSError):
        return False
    return proposal_engine.archive_payload_contained(
        ((str(entry["path"]), int(entry["size"])) for entry in entries),
        folder_files,
    )


def entry_root_verified(
    zip_path: Path,
    entry_root: str,
    folder_files: Sequence[Tuple[str, int]],
) -> bool:
    """Whether every entry of the archive sits inside ``entry_root`` and on disk.

    Strict strip-and-compare on this route: the root was named before the check, so
    the caller already decided which wrapper to strip. Containment, not equality
    (a lived-in directory legitimately holds more than the archive that shipped
    it), and declared size comes from the archive's header, so this decides which
    directory an archive belongs to and is never an integrity claim. Any read
    failure is ``False`` - never an exception on a read route.
    """
    try:
        entries = zip_service.list_zip_entries(Path(zip_path))
    except (BadZipFile, OSError):
        return False
    return proposal_engine.archive_payload_contained(
        ((str(entry["path"]), int(entry["size"])) for entry in entries),
        folder_files,
        entry_root,
    )


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
    live = {candidate.key for candidate in scan_bundles(STORAGE).candidates}
    stale = sorted((key for key in current if key not in live), key=lambda key: (key.casefold(), key))
    return {"dismissed": sorted(current), "stale_dismissals": stale}


def get_enrichments() -> Dict[str, dict]:
    """The enrichment cache, keyed by ``thingiverse:<id>``; anything malformed reads ``{}``."""
    raw = settings_service.get_setting(settings_service.BUNDLE_ENRICHMENTS_KEY)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): value if isinstance(value, dict) else {} for key, value in parsed.items()}


def _set_enrichments(records: Mapping[str, dict]) -> None:
    """Write the whole enrich map in one read-modify-write per request.

    One write for the whole request is what keeps a bulk enrich from losing an
    entry per item; the accepted concurrency trade-off is the dismissals one, and
    the worst case here is one extra fetch, never a wrong row.
    """
    settings_service.set_setting(settings_service.BUNDLE_ENRICHMENTS_KEY, json.dumps(dict(records), sort_keys=True))


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


def _unqualify(scope: str, key: str) -> Optional[str]:
    """The walk-relative spelling of a STORAGE-relative marker key, or ``None``.

    Candidate keys are WALK-relative while a project marker's key is
    STORAGE-relative, which is exactly why ``_qualify`` (the import/import/Stagetop
    direction) is the wrong one here: a marker key outside the scanned subtree
    must answer ``None`` and never be passed through unchanged, or a top-level
    project key would collide with a same-named walk key under a scope.
    """
    if not scope:
        return key
    if key == scope:
        return ""
    if key.startswith(scope + "/"):
        return key[len(scope) + 1 :]
    return None


def known_project_keys(session: Session) -> FrozenSet[str]:
    """Every storage-relative bundle key a Project's marker names.

    ``project_service.bundle_marker_of`` is the ONE marker reader - never a second
    JSON parse here - so a bad metadata column means the same thing to the create
    door, the create belt and this guard. This is what makes "a path inside an
    existing project is not a detection row" one predicate instead of a rule per
    pass: a key with no marker simply contributes nothing.
    """
    keys = set()
    for project in session.exec(select(Project)):
        key = project_service.bundle_marker_of(project.metadata_json).get("key")
        if isinstance(key, str) and key:
            keys.add(key)
    return frozenset(keys)


def _excluded_keys(scope: str, *, known: Optional[AbstractSet[str]] = None) -> FrozenSet[str]:
    """The marker keys under ``scope``, expressed as WALK-relative subtrees.

    This is the ONE derivation both doors call - ``build_report`` for the report
    and ``create_bundles`` for the create - because a second derivation is exactly
    how a create answers ``unknown bundle`` for a row its own report offered.
    ``known`` lets a caller that already read the marker keys pass them in, so a
    report reads them once (``_marker_keys``) rather than twice.
    """
    if known is None:
        known = _marker_keys()
    relative = (_unqualify(scope, key) for key in known)
    return frozenset(key for key in relative if key is not None)


def _owned_subtree(rel_dir: str, owned: AbstractSet[str]) -> bool:
    """Whether ``rel_dir`` is, or sits inside, a project's marker key.

    The ``"" in owned`` leg is a SCOPED report: the walk root itself is the
    project key, so every path in it is owned and the report answers no rows.
    """
    return "" in owned or _strictly_inside_any(rel_dir, owned)


def _without_owned_subtrees(
    files_by_dir: Mapping[str, Sequence[Tuple[str, int]]],
    dirs_by_parent: Mapping[str, set],
    owned: AbstractSet[str],
) -> Tuple[Dict[str, List[Tuple[str, int]]], Dict[str, set]]:
    """Pruned copies of the walk with every project-owned subtree dropped.

    Dropping the subtree BEFORE anything is decided is what makes "a path inside
    an existing project is not a detection row" structural: no candidate, no
    refusal and no duplicate group can be produced from it. The project KEY ITSELF
    is never dropped (``_strictly_inside_any`` is strict), which is what keeps its
    own ``already_created`` row on the report.

    The key keeps its own subtree LISTING, folded into its entry, because a row is
    built from a directory's folded subtree (``_subtree``): dropping the children
    outright would leave a project key reading zero models and offer a row that
    disagrees with the project it names. Archives are the exception - they are
    dropped rather than folded in, so no ``.zip`` inside a project can ever reach
    the pairing or zip-only passes as a candidate or a duplicate.

    A scope that sits at or below a project key (``"" in owned``) is entirely
    owned, so its walk is empty and the report answers no rows at all.
    """
    if "" in owned:
        return {}, {}
    folded = _subtree(files_by_dir)
    kept_files: Dict[str, List[Tuple[str, int]]] = {}
    for rel_dir, files in files_by_dir.items():
        if _owned_subtree(rel_dir, owned):
            continue
        if rel_dir in owned:
            kept_files[rel_dir] = [
                (rel_path, size)
                for rel_path, size in folded.get(rel_dir, ())
                if _suffix(rel_path) != ".zip"
            ]
            continue
        kept_files[rel_dir] = list(files)
    kept_parents: Dict[str, set] = {}
    for parent, children in dirs_by_parent.items():
        if _owned_subtree(parent, owned):
            continue
        kept_parents[parent] = {
            name for name in children if not _owned_subtree(_qualify(parent, name), owned)
        }
    return kept_files, kept_parents


def wire_groups(groups: Sequence[Mapping]) -> List[dict]:
    """One group list in the wire shape, from the engine's own groups.

    Public because the create path and the adopt path both hand it the engine's
    groups, so the wire vocabulary has exactly one author.

    The engine's groups are hierarchical (`children`), and the wire's
    `BundleGroupOut` is the flat three-field shape the queue renders and the
    create validates its exclusions against, so the TOP level is mapped
    field-for-field and the hierarchy stays in the artifact. `separable` is the
    engine's own answer - "could this group stand as its own project" - which is
    exactly what an exclusion needs to be honest.
    """
    return [
        {
            "name": group["name"],
            "model_count": group["models"],
            "foreign": bool(group["foreign"]),
            "separable": bool(group["separable"]),
        }
        for group in groups
    ]


def marker_groups(
    groups: Sequence[Mapping],
    *,
    attached: Mapping[str, int],
    excluded: Collection[str] = (),
    before: Optional[Sequence[Mapping]] = None,
) -> List[dict]:
    """The ``_bundle`` marker's group list, the ONE place a marker group is built.

    Input is the FLAT wire shape ``wire_groups`` emits (``name``, ``model_count``,
    ``foreign``, ``separable``) - what a row's ``matches[*].groups`` carry - and the
    output adds the two decisions a project makes at create time: ``included`` (the
    operator's answer) and ``attached`` (how many of the group's registered MODEL
    assets this create actually attached).

    ``included`` is carried forward BY NAME from ``before`` whenever that name is
    present there, so a rewrite that did not re-ask the question - adopting a
    project an operator already excluded a group from - can never resurrect the
    group. Only a name absent from ``before`` falls back to ``excluded``, the
    operator's exclusion list at CREATE time.

    The key order is part of the contract because ``bundle_drift.excluded_prefixes``
    subtracts a group only when ``group.get("included") is not False`` fails - i.e.
    only an explicit ``included: False`` subtracts. A marker group written without
    that key never subtracts, so an operator's exclusion would read as permanent
    drift. The create path and the adopt path both call this, so the two can never
    disagree about the marker's vocabulary.
    """
    carried_by_name: Dict[str, object] = {}
    for previous in before or ():
        if not isinstance(previous, Mapping):
            continue
        name = previous.get("name")
        if isinstance(name, str):
            carried_by_name[name] = previous.get("included")
    written: List[dict] = []
    for group in groups:
        name = str(group["name"])
        carried = carried_by_name.get(name)
        included = name not in excluded if carried is None else bool(carried)
        written.append(
            {
                "name": name,
                "model_count": int(group["model_count"]),
                "foreign": bool(group.get("foreign")),
                "separable": bool(group.get("separable")),
                "included": included,
                "attached": int(attached.get(name, 0)),
            }
        )
    return written


def cover_for_registered(
    assets: Sequence[RegisteredAsset],
    cover_entry: Optional[str] = None,
    *,
    allow_model_preview: bool = True,
) -> dict:
    """The cover a created project would get, and the reason it got it.

    The chain is ordered by source, never by size: a registered preview image
    beats a referenced-but-unregistered one, which beats a model the client can
    render, which beats no cover at all. An unregistered image outranks a
    renderable model on purpose - a picture the vendor shipped is the bundle's
    own face, while a rendered model is an inference. Size only breaks ties
    inside one source.

    ``url``/``thumb_url`` are set exactly when ``asset_id`` is: the pair is the
    served paths for those bytes, so a row with no asset has nothing to serve.

    This function IS the one rule both doors ask: the detection create path
    through ``_cover``, and the zip-as-project door directly. ``allow_model_preview``
    is the single knob a door may use to refuse the model leg; passing an empty
    ``cover_entry`` is how a door that ran no walk kills the unregistered leg by
    construction.
    """
    images = sorted(
        (asset for asset in assets if _suffix(asset.path) in library_scan.IMAGE_SUFFIXES),
        key=lambda asset: (-asset.size, asset.path),
    )
    if images:
        return _cover_of(images[0], COVER_LARGEST_IMAGE)
    if cover_entry:
        return _cover_none_fields(cover_entry, COVER_LARGEST_IMAGE_UNREGISTERED)
    if not allow_model_preview:
        return _cover_none_fields(None, COVER_NONE)
    # The bundle has no image at all. Its own registered, client-renderable models
    # are still a face: prefer one that already has a thumbnail (a previous render),
    # then the largest, then path order - the pick is a function of the rows.
    models = sorted(
        (asset for asset in assets if _suffix(asset.path) in PREVIEW_SUFFIXES),
        key=lambda asset: (asset.thumb_version <= 0, -asset.size, asset.path),
    )
    if models:
        return _cover_of(models[0], COVER_MODEL_PREVIEW)
    return _cover_none_fields(None, COVER_NONE)


def _cover_entry(facts) -> Optional[str]:
    """The engine's UNREGISTERED cover candidate: the largest image it found.

    `Facts.cover` carries the reason beside the pick, and only the image reason is
    a face the report can name without a registered asset: the `model-preview` leg
    is `cover_for_registered`'s own, applied to the registered rows, and a
    `.zip`'s facts carry no cover at all.
    """
    cover = getattr(facts, "cover", None) or {}
    chosen = cover.get("chosen") or {}
    if chosen.get("reason") == COVER_LARGEST_IMAGE:
        return chosen.get("path")
    return None


def _cover(assets: Sequence[RegisteredAsset], facts) -> dict:
    """`cover_for_registered` with the engine's facts: the report's one call.

    A one-line delegate on purpose. `_create_one` names the anti-fix it must not
    grow - "a second rule here would be free to disagree with the report's" - and
    the same reasoning binds this module to one implementation of the preference.
    """
    return cover_for_registered(assets, _cover_entry(facts))


def _cover_of(asset: RegisteredAsset, reason: str) -> dict:
    """A cover backed by one registered asset, with its two served paths.

    ``url`` and ``thumb_url`` are built through ``asset_urls`` so the report's
    cover and the asset serializer can never offer two different shapes for the
    same bytes. ``thumb_url`` is still None until a thumbnail row exists.
    """
    return {
        "asset_id": asset.id,
        "rel_path": asset.path,
        "reason": reason,
        "url": asset_urls.primary_url(asset.id, asset.filename),
        "thumb_url": asset_urls.thumb_url(asset.id, asset.thumb_version),
    }


def _claim_owners(candidates: Sequence[BundleCandidate], *, scope: str) -> Dict[str, str]:
    """``path`` -> the ONE row that owns that registered model path.

    One map for the whole report, built ONCE, so "does this row actually own this
    model?" is a dict lookup per row instead of a second opinion per row. It is the
    answer to the anti-fix ``_report_row`` used to be: a row whose key merely
    PREFIXES a registered model used to claim it, which is how a container answered
    ``ready`` over models a deeper row exists to attach.

    Two rules decide a winner:

    * Built from ``scan.candidates`` ONLY, deepest key first, first writer wins. A
      parent's listing and a child's can only overlap where the parent holds the
      child, and since Mf-1agz the row walk never descends into a row - so this is
      a total, unambiguous answer rather than a race. Keeping the ordering is what
      makes the map stable if a future rule ever does overlap.
    * Only ``library_scan.MODEL_SUFFIXES`` paths are claimable. That is not a
      membership rule and never was: a create's member set is every registered
      file under the key, so a project certainly holds previews, documents and
      nested archives. Say what a claim is FOR: this map settles which row's PARTS
      a path is. A preview is a product's face, not one of its parts, and the MODEL
      answer ``_create_one`` reads off the member set as ``model_assets`` (one
      ``by_role`` call) is what ``attached``, the marker's per-group counts and
      ``Dedupe.containment`` are asked with. Letting an image into the map makes two
      rows contest a cover, which re-opens the ``Mf-quv4`` ownership bound this
      bullet exists to keep. And this table is not what keeps a non-model with its
      row - ``_claimed_assets``' ``claims.get(asset.path, key)`` default is: a path
      the map is silent about stays with the row that named it instead of going to
      nobody.

    ``is_archive`` rows are skipped: no registered file can live under a
    ``<x>.zip/`` prefix, so an archive row is never an owner - it is only ever
    provenance, and letting one claim a path would make the real row read
    ``not_scanned``.
    """
    claims: Dict[str, str] = {}
    ordered = sorted(
        (candidate for candidate in candidates if not candidate.is_archive),
        key=lambda candidate: (-_depth(candidate.key), candidate.key.casefold(), candidate.key),
    )
    for candidate in ordered:
        key = _qualify(scope, candidate.key)
        for rel_path in _own_paths(key, candidate):
            if _suffix(rel_path) in library_scan.MODEL_SUFFIXES:
                claims.setdefault(rel_path, key)
    return claims


def _cover_none_fields(rel_path: Optional[str], reason: str) -> dict:
    """A cover with no registered asset: a named-but-unserved path, or nothing.

    ``url``/``thumb_url`` are None because there is no asset id to serve: the
    invariant is that the pair is set exactly when ``asset_id`` is.
    """
    return {"asset_id": None, "rel_path": rel_path, "reason": reason, "url": None, "thumb_url": None}


def _metadata_value(metadata: Mapping, field: str) -> Optional[str]:
    """One engine fact's value, or ``None`` when the engine had no evidence for it."""
    entry = metadata.get(field) or {}
    return entry.get("value")


def _name_source_url(name: str) -> Optional[str]:
    """The vendor URL a Thingiverse ID IN THE NAME states, or ``None``.

    This is the one provenance inheritance Mf-1agz carries over from the old
    detector's R4, and it is not a shape reading: an id-prefixed export
    (``5390184_ARTICULATED_ice_dragon``) names its own thing page, the enrich door
    is the only way to fill the rest of its metadata, and without this the door
    answers "this bundle names no vendor id" for exactly the exports that do. It
    states a URL and nothing else - never a creator, a licence or a title.
    """
    thing_id = library_scan.thingiverse_id_from_name(name)
    if not thing_id:
        return None
    return THINGIVERSE_THING_URL.format(thing_id)


def _metadata_suggestions(facts) -> List[dict]:
    """Every value the engine mined, sorted by field, with its source.

    The row's `title`/`creator`/... display fields answer "what would a create
    write"; these answer "what did a document claim, and where" - the guided
    panel shows them as suggestions an operator accepts, edits or dismisses.
    """
    metadata = getattr(facts, "metadata", {}) or {}
    return [
        {"field": field, "value": str(entry.get("value")), "where": str(entry.get("where") or ""),
         "confidence": str(entry.get("confidence") or "low")}
        for field, entry in sorted(metadata.items())
        if (entry or {}).get("value")
    ]


def _provenance_out(facts) -> Optional[dict]:
    """The engine's provider-URL ledger, or None for a `.zip`'s facts."""
    prov = getattr(facts, "provenance", None)
    if not isinstance(prov, dict):
        return None
    return {
        "candidates": [dict(item) for item in prov.get("candidates") or []],
        "unresolved": [dict(item) for item in prov.get("unresolved") or []],
        "confidence": prov.get("confidence") or "suggested",
    }


def _cover_candidates(facts, *, key: str, owned_assets: Sequence[RegisteredAsset]) -> List[dict]:
    """The engine's ranked faces, with the registered ones resolvable.

    The engine's `rel_path` is BUNDLE-relative; a registered asset's path is
    storage-relative. Resolve each candidate to the row's own asset so the panel
    only offers a face the create can actually set, and keep the unregistered
    ones visible as evidence (no asset_id).
    """
    cover = getattr(facts, "cover", None) or {}
    by_path = {asset.path: asset for asset in owned_assets}
    out = []
    for entry in cover.get("candidates") or []:
        rel = entry.get("path")
        if not rel:
            continue
        asset = by_path.get(f"{key}/{rel}") or by_path.get(rel)
        item = {"rel_path": rel, "reason": entry.get("reason") or "largest-image",
                "bytes": int(entry.get("bytes") or 0), "asset_id": None, "url": None, "thumb_url": None}
        if asset is not None:
            item.update({"asset_id": asset.id,
                         "url": asset_urls.primary_url(asset.id, asset.filename),
                         "thumb_url": asset_urls.thumb_url(asset.id, asset.thumb_version)})
        out.append(item)
    return out


def _images(owned_assets: Sequence[RegisteredAsset]) -> List[dict]:
    """The row's registered images, largest first - the chooser's faces."""
    images = sorted(
        (asset for asset in owned_assets if _suffix(asset.path) in library_scan.IMAGE_SUFFIXES),
        key=lambda asset: (-asset.size, asset.path),
    )
    return [
        {"asset_id": asset.id, "rel_path": asset.path,
         "url": asset_urls.primary_url(asset.id, asset.filename),
         "thumb_url": asset_urls.thumb_url(asset.id, asset.thumb_version)}
        for asset in images
    ]


def _row_facts(candidate: BundleCandidate) -> dict:
    """The display fields a row renders, read off the engine's fact sheet.

    A directory's facts are ``proposal_engine.Facts`` (metadata, provenance,
    groups, twin, cover); a `.zip`'s are ``ArchiveFacts``, which measures the
    archive's payload and nothing else - so an archive row's title is the product
    name its filename carries and its other fields are honestly empty.
    """
    # `ArchiveFacts` carries no fact sheet, so its fields read as absent rather than
    # needing a branch: the only difference the caller sees is the name the fallback
    # uses (the zip's stem, not the directory's).
    metadata = getattr(candidate.facts, "metadata", {}) or {}
    name = Path(candidate.rel).stem if candidate.is_archive else _basename(candidate.rel)
    title = _metadata_value(metadata, "title") or proposal_engine.product_title(
        library_scan.display_name(name)
    )
    return {
        "title": title,
        "name_suggestion": title,
        "creator": _metadata_value(metadata, "creator"),
        "licence": _metadata_value(metadata, "licence"),
        "licence_url": _metadata_value(metadata, "licence_url"),
        "source_url": _metadata_value(metadata, "source_url") or _name_source_url(name),
        "description": _metadata_value(metadata, "description"),
    }


def _match_row(option: proposal_engine.Option, candidate: BundleCandidate) -> dict:
    """One engine option in the wire's ``BundleMatchOut`` shape.

    The engine's own ranking, mapped field-for-field and in its order, so
    ``matches[0]`` IS the recommendation the operator acts on. ``rule`` is the
    ``engine:<shape>`` token the dissent shim already used, which is also the
    create marker's ``rule`` - one token, one owner.

    A non-creatable offer always says why it cannot be enacted. A creatable one
    normally carries no note, and carries one only when the engine has something
    the operator must not miss - today the unreadable-boundary `one-project`
    (Mf-ky16).
    """
    facts = candidate.facts
    creatable = option.shape in candidate.creatable_shapes
    note = option.note if creatable else f"needs arranging first: {option.consequence}"
    return {
        "shape": option.shape,
        "rule": f"engine:{option.shape}",
        "confidence": option.confidence,
        "model_count": int(facts.models),
        "image_count": int(facts.images),
        "text_count": int(facts.docs),
        "other_count": int(facts.others),
        # An archive's facts carry no groups: its payload is one thing inside a zip.
        "groups": [] if candidate.is_archive else wire_groups(facts.groups),
        "note": note,
        "creatable": creatable,
    }


def _report_row(
    session: Session,
    *,
    root: Path,
    scope: str,
    candidate: BundleCandidate,
    folders: Mapping[str, str],
    asset_index: Mapping[str, Tuple[str, ...]],
    registered: Sequence[Tuple[str, str, int]],
    dismissals: Mapping[str, dict],
    enrichments: Mapping[str, dict],
    claims: Optional[Mapping[str, str]],
) -> dict:
    """One wire row for one candidate: the engine's answer, resolved against the DB.

    The row is clock-free: a cached page's facts fill an empty creator or licence
    whether or not the record is still fresh, because TTL gates only the enrich
    action, never what the report shows.
    """
    facts = candidate.facts
    display = _row_facts(candidate)
    key = _qualify(scope, candidate.key)
    cache_key = bundle_enrich.cache_key(display["source_url"])
    record = enrichments.get(cache_key) if cache_key is not None else None
    values, fields = bundle_enrich.fills(
        display["creator"], display["licence"], display["licence_url"], record
    )
    enrichment = bundle_enrich.provenance(record, fields) if fields else None
    # Every row asks the database, including the archive-only ones: what a row
    # ANSWERS is not a reason to leave its own files uncounted and unclaimable -
    # ``assets_total`` and the cover are read out of the same key the operator can
    # see on disk (ADR 0010 Decision 7).
    assets = registered_assets(session, key)
    # The ONE ownership expression, shared with the create door (``_create_one``):
    # the row's own subtree listing, narrowed by the report-wide one-owner map.
    own_assets = _own_assets(assets, key=key, candidate=candidate)
    owned_assets = _claimed_assets(own_assets, key=key, claims=claims)
    models = [asset for asset in owned_assets if _suffix(asset.path) in library_scan.MODEL_SUFFIXES]
    containment = [asset.id for asset in models]
    if models and not candidate.is_archive and key.casefold() in folders:
        status, blocked_reason = READY, None
    elif candidate.is_archive:
        # The Extract door (``ui/review/bundleRows.ts`` ``extractPathFor``) opens only
        # on ``blocked_reason === "archive_only"``, so a zip row keeps that value as
        # the NAME of its one door. What the engine recommends decides whether that
        # door is actionable, and that is ``matches[0].creatable`` - never this field.
        status, blocked_reason = BLOCKED, ARCHIVE_ONLY
    elif key.casefold() not in folders:
        status, blocked_reason = BLOCKED, UNRESOLVED_DIRECTORY
    else:
        status, blocked_reason = BLOCKED, NOT_SCANNED
    archive = None
    if candidate.archive_path:
        archive = {
            "rel_path": _qualify(scope, candidate.archive_path),
            "twin_verified": _archive_twin_verified(root, candidate),
        }
    twin = _qualify(scope, candidate.archive_path) if candidate.archive_path and not candidate.is_archive else None
    existing = project_create_service.find_existing_project(
        session,
        key=key,
        archive=twin,
        source_url=display["source_url"],
        name=display["name_suggestion"],
        assets=containment,
        asset_index=asset_index,
    )
    existing_project_id = existing.project.id if existing else None
    existing_project_name = existing.project.name if existing else None
    if existing_project_name is not None and not existing_project_name.strip():
        existing_project_name = None
    drift_state = bundle_drift.DRIFT_NOT_APPLICABLE
    drift_count: Optional[int] = None
    if existing is not None and existing.match == MATCH_MARKER:
        marker = project_service.bundle_marker_of(existing.project.metadata_json)
        drift_state, drift_root = bundle_drift.classify(
            marker, root_present=lambda root: bundle_drift.has_rows_under(registered, root)
        )
        if drift_root is not None:
            owner = existing.project.id
            asset_ids = bundle_drift.drift_asset_ids(
                registered,
                root=drift_root,
                prefixes=bundle_drift.excluded_prefixes(drift_root, marker),
                is_member=lambda asset_id: owner in asset_index.get(asset_id, ()),
            )
            drift_count = len(asset_ids)
    matches = [_match_row(row_option, candidate) for row_option in candidate.options]
    return {
        "key": key,
        "rel_path": key,
        "title": display["title"],
        "name_suggestion": display["name_suggestion"],
        "creator": values["creator"],
        "licence": values["licence"],
        "licence_url": values["licence_url"],
        "enrichment": enrichment,
        "source_url": display["source_url"],
        # The row's own recommendation, in lockstep with matches[0] by construction:
        # `detector`/`confidence`/`metadata_shape` keep their wire names and now
        # describe the ENGINE's option (Mf-1agz).
        "detector": matches[0]["rule"],
        "confidence": matches[0]["confidence"],
        "metadata_shape": matches[0]["shape"],
        "description_excerpt": display["description"],
        # The counts come off `matches[0]` - the engine's own census - so the row
        # and its recommendation cannot disagree about a single number either.
        "model_count": matches[0]["model_count"],
        "image_count": matches[0]["image_count"],
        "text_count": matches[0]["text_count"],
        "other_count": matches[0]["other_count"],
        "archive_count": _archive_count(candidate),
        "groups": matches[0]["groups"],
        # The engine's whole offer, recommended first: a client can present the
        # alternatives without a second round trip and without ranking them itself.
        "matches": matches,
        "cover": _cover(owned_assets, facts),
        "assets_total": len(assets),
        "status": status,
        "blocked_reason": blocked_reason,
        "archive": archive,
        "existing_project_id": existing_project_id,
        "existing_project_name": existing_project_name,
        "existing_project_match": existing.match if existing else None,
        "bundle_drift_state": drift_state,
        "bundle_drift_count": drift_count,
        "dismissed": key in dismissals,
        "metadata_suggestions": _metadata_suggestions(facts),
        "provenance": _provenance_out(facts),
        "cover_candidates": _cover_candidates(facts, key=key, owned_assets=owned_assets),
        "images": _images(owned_assets),
    }


def _archive_count(candidate: BundleCandidate) -> int:
    """How many archives this row holds, from its own measurement.

    A directory's facts count them as `archives`; an archive's facts list the
    INNER archives it chose not to open, which is the same question one level
    down. An opaque inner archive still counts: it is payload.
    """
    if candidate.is_archive:
        return len(candidate.facts.nested)
    return int(candidate.facts.archives)


def _archive_twin_verified(root: Path, candidate: BundleCandidate) -> bool:
    """``twin_verified`` for the archive this candidate attaches.

    Which answer is honest depends on who paired the two. The entry-root route had
    to prove containment before it paired at all, so its archive is
    **re-verified at report time**: a ``False`` there means the bytes on disk
    changed since the scan, never that the pairing was never checked. The stem
    route keeps the names-only answer: there the operator paired them, and a
    mismatch is information for the operator rather than an assertion to raise.
    """
    archive_path = candidate.archive_path or ""
    if candidate.entry_root:
        return entry_root_verified(root / archive_path, candidate.entry_root, candidate.files)
    return twin_verified(root / archive_path, candidate.files)


def _marker_keys() -> FrozenSet[str]:
    """Every storage-relative bundle key a Project's marker names, one query.

    The ONE read both report doors start from: ``_excluded_keys`` turns it into
    walk-relative subtrees and the engine is handed it verbatim, so the two spaces
    meet at exactly one place (``_qualify``) and neither is re-derived.
    """
    with Session(engine) as session:
        return known_project_keys(session)


def build_report(*, path: str = "", limit: int = DEFAULT_LIMIT, include_dismissed: bool = False) -> dict:
    """The whole detection report, read from disk now. Writes nothing.

    ``path`` scopes the walk (``""`` is the whole storage root) and only changes
    what is scanned: every key stays storage-root-relative, so a scoped read and
    a whole-storage read name the same bundle, and a create pass can find a row
    a scoped report showed. Duplicate archives are reported scoped the same way
    but are never capped by ``limit``: the walk is already bounded by
    ``MAX_SCAN_ENTRIES``, and a reclaimable-bytes total that stopped at a page
    boundary would be a number the operator could not act on.

    This door and ``create_bundles`` are the only two that derive
    ``excluded_keys``: every other scan keeps the default, so no other caller
    silently changes what it offers.
    """
    scope = _scope(path)
    root = STORAGE / scope if scope else STORAGE
    if not _is_inside_storage(root):
        raise HTTPException(status_code=400, detail="path must stay inside the storage root")
    known = _marker_keys()
    scan = scan_bundles(
        root,
        excluded_keys=_excluded_keys(scope, known=known),
        project_keys=known,
        scope=scope,
    )
    return _report_from_scan(
        scan,
        root=root,
        scope=scope,
        limit=limit,
        include_dismissed=include_dismissed,
    )


def _report_from_scan(
    scan: BundleScan,
    *,
    root: Path,
    scope: str,
    limit: int,
    include_dismissed: bool,
    enrichments: Optional[Mapping[str, dict]] = None,
) -> dict:
    """Turn one walk into the wire report; the disk walk has already happened.

    Duplicate archives are scoped like every other key and rendered as one group
    per content hash plus a copyable text block naming the same paths. They are
    not capped by ``limit``: the walk is already bounded by ``MAX_SCAN_ENTRIES``.

    The enrich map is read ONCE per report unless the caller already holds it: a
    read per row would be one Session and one query per bundle.

    ``counts.offered`` is how many rows the report HAS and ``counts.withheld`` is
    how many of them ``limit`` kept off the wire (Mf-1agz): a bounded report that
    said only ``truncated: true`` made the operator guess whether the row they were
    looking for was one of the ones dropped, and Vault was exactly that row.
    """
    dismissals = get_dismissals()
    if enrichments is None:
        enrichments = get_enrichments()
    # ONE rule set for the whole report, so every row's stamp and the report's own
    # are the same value even if a table is edited mid-run.
    rule_set = proposal_engine.rule_set()
    rows: List[dict] = []
    with Session(engine) as session:
        folders = _folder_index(session)
        # One query for ALL rows: containment is a dict lookup per row.
        asset_index = project_create_service.project_assets_index(session)
        # One registered set for the whole report too: drift is a string prefix
        # and a dict lookup per row, never a per-row read.
        registered = registered_index(session)
        # ONE ownership map, built once from the walk's own row set, so every row
        # answers ownership against one settled answer rather than re-deriving it
        # from its own prefix (see ``_claim_owners``).
        claims = _claim_owners(scan.candidates, scope=scope)
        for candidate in scan.candidates:
            row = _report_row(
                session,
                root=root,
                scope=scope,
                candidate=candidate,
                folders=folders,
                asset_index=asset_index,
                registered=registered,
                dismissals=dismissals,
                enrichments=enrichments,
                claims=claims,
            )
            row["rule_set"] = rule_set["stamp"]
            rows.append(row)

    rows.sort(key=lambda row: (row["key"].casefold(), row["key"]))
    visible = rows if include_dismissed else [row for row in rows if not row["dismissed"]]
    bound = max(0, int(limit))
    counts = {
        "total": len(visible),
        "high": sum(1 for row in visible if row["confidence"] == "high"),
        "medium": sum(1 for row in visible if row["confidence"] == "medium"),
        "low": sum(1 for row in visible if row["confidence"] == "low"),
        "ready": sum(1 for row in visible if row["status"] == READY),
        "blocked": sum(1 for row in visible if row["status"] == BLOCKED),
        "dismissed": sum(1 for row in rows if row["dismissed"]),
        "already_created": sum(1 for row in visible if row["existing_project_id"]),
        # What the wire actually carried, and what the bound kept back. `total`
        # keeps its old meaning (the rows this report has); these two say what the
        # client is and is not looking at.
        "offered": len(visible),
        "withheld": max(0, len(visible) - bound),
    }
    live_keys = {_qualify(scope, candidate.key) for candidate in scan.candidates}
    scoped_duplicates = tuple(
        library_scan.DuplicateGroup(
            digest=group.digest,
            size=group.size,
            paths=tuple(_qualify(scope, path) for path in group.paths),
        )
        for group in scan.duplicates
    )
    return {
        "root": root.name,
        "truncated": scan.truncated or len(visible) > bound,
        "counts": counts,
        "bundles": visible[:bound],
        "duplicates": [
            {
                "sha256": group.digest,
                "size": group.size,
                "paths": list(group.paths),
                "reclaimable_bytes": group.reclaimable_bytes,
            }
            for group in scoped_duplicates
        ],
        "duplicates_total_bytes": sum(group.reclaimable_bytes for group in scoped_duplicates),
        "duplicates_text": library_scan.duplicate_report_text(scoped_duplicates),
        "stale_dismissals": sorted(
            (key for key in dismissals if key not in live_keys), key=lambda key: (key.casefold(), key)
        ),
        "rule_set": rule_set,
        "refused": sorted(
            (
                {"rel_path": _qualify(scope, refusal.rel_path), "rule": refusal.rule, "kind": refusal.kind}
                for refusal in scan.refusals
            ),
            key=lambda item: item["rel_path"],
        ),
    }


@dataclass(frozen=True)
class BundleCreateItem:
    """One confirmed bundle: which one, what to call it, and whether to enrich it.

    ``exclude_groups`` names the engine's groups the operator says are NOT part of
    this product. Each must be one of the chosen match's own groups, marked
    ``foreign`` (somebody else's thing) and ``separable`` (it could stand as a
    project of its own), and ``_exclusion_prefixes`` states why a group outside
    that set is refused: its parts would otherwise land in no project and in no
    row. ``shape`` names one of the row's ``matches`` shapes; ``None`` means the
    row's recommended shape. An unoffered shape answers ``failed``/``unknown
    shape``.
    """

    key: str
    name: Optional[str] = None
    name_only: bool = False
    exclude_groups: Tuple[str, ...] = ()
    shape: Optional[str] = None
    # AUTO: one click, high confidence only. The server gates the band and the
    # shape; a client never decides either.
    auto: bool = False
    # The operator's cover decision (`{"mode": ..., "asset_id": ...}`); absent
    # means the engine's own choice stands.
    cover: Optional[Mapping[str, Optional[str]]] = None
    # The metadata the operator ACCEPTED, field -> value or None. Present means
    # nothing outside the map is written; absent means the legacy create.
    metadata: Optional[Mapping[str, Optional[str]]] = None


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


def _auto_refusal(
    item: BundleCreateItem, row: Mapping, candidate: Optional[BundleCandidate]
) -> Optional[str]:
    """Why AUTO may not run on this row, or None. Server-side, before any write.

    The band is the ENGINE's (`row["confidence"]`, i.e. `matches[0].confidence`);
    the client never decides one. The shape list is explicit and small; the move
    question is asked of the engine's own plan, so auto can never half-move a
    folder it cannot finish cleanly - it falls back to guided with a reason.
    """
    if not item.auto:
        return None
    if row.get("confidence") != "high":
        return AUTO_NEEDS_HIGH
    if candidate is None:
        return "auto requires a measured bundle"
    recommended = row.get("metadata_shape") or row.get("detector") or ""
    if recommended not in AUTO_SHAPES:
        return f"auto does not run on {recommended}; review it instead"
    if item.shape and item.shape != recommended:
        return "auto runs the recommended shape; review the shape instead"
    if row.get("status") != READY:
        reason = row.get("blocked_reason") or row.get("status") or "unknown"
        return f"auto requires a ready row: {reason}"
    moves = proposal_engine.arrange_moves(candidate.facts, recommended)
    if moves:
        return f"auto needs arranging first: {len(moves)} move(s); review it instead"
    return None


def _metadata_write(
    item: BundleCreateItem, row: Mapping, candidate: Optional[BundleCandidate]
) -> Tuple[dict, Optional[str]]:
    """The five project metadata values this create may write, and a refusal.

    Three sources, in this order:
    * `item.metadata` present - GUIDED. Only the fields in the map are written;
      a field absent from it, or mapped to None, is never written. That is what
      makes "a licence is never written without an explicit accept" true on the
      server, not in the client.
    * `item.auto` - AUTO. Only fields the ENGINE calls `high` confidence; a
      parsed medium/low suggestion is the guided panel's job, not auto's default.
    * neither - the legacy create path, byte-identical to before this change.
    """
    empty = {field: None for field in METADATA_FIELDS}
    if item.metadata is not None:
        if item.name_only:
            return empty, "name_only and metadata are exclusive"
        unknown = sorted(set(item.metadata) - set(METADATA_FIELDS))
        if unknown:
            return empty, f"unknown metadata field: {unknown[0]}"
        values = dict(empty)
        for field, value in item.metadata.items():
            values[field] = value if isinstance(value, str) and value.strip() else None
        return values, None
    if item.auto:
        facts = getattr(candidate, "facts", None) if candidate is not None else None
        metadata = getattr(facts, "metadata", {}) or {}
        values = dict(empty)
        for field in METADATA_FIELDS:
            entry = metadata.get(field) or {}
            if entry.get("confidence") == "high" and entry.get("value"):
                values[field] = entry["value"]
        return values, None
    return _enrichment(row, item.name_only), None


def _cover_write(item: BundleCreateItem) -> Tuple[Optional[dict], Optional[str]]:
    """The operator's cover decision, validated as a request (not against disk).

    The membership guard lives in `create_project_from_members`, which is the one
    place that knows the member set; this only rejects a malformed mode.
    """
    if item.cover is None:
        return None, None
    mode = item.cover.get("mode")
    if mode not in {"asset", "none"}:
        return None, f"unknown cover mode: {mode}"
    if mode == "asset" and not item.cover.get("asset_id"):
        return None, "cover mode asset needs an asset_id"
    return {"mode": mode, "asset_id": item.cover.get("asset_id") if mode == "asset" else None}, None


def _exclusion_prefixes(
    key: str, groups: Sequence[Mapping], names: Sequence[str]
) -> Tuple[Tuple[str, ...], Optional[str]]:
    """The excluded group prefixes, or the reason the request cannot be honoured.

    Matching is exact and case-sensitive: a group name is a path segment, and two
    spellings of one directory are two directories. The returned prefix carries a
    trailing slash so a FILE literally named ``<key>/<name>`` is never excluded -
    only what sits INSIDE the group's directory is the group. A duplicated name is
    idempotent: the prefixes are de-duplicated through ``dict.fromkeys``, and the
    attach set is built from them, so asking twice can never attach twice.

    Every reason is a ``failed``, never a ``blocked``: ``blocked`` means database
    readiness (Mf-um7x), and an unusable group name is a malformed request.

    The checks run in the plan's order and answer two different questions.
    ``foreign`` is what makes an exclusion MEANINGFUL at all: a group of this
    product's own is not somebody else's thing, so removing it is simply the
    wrong request, and no later fact could rescue it. ``separable`` is what makes
    the exclusion POSSIBLE: the group needs a row of its own to fall back to, so a
    foreign group that was never a row is refused because its parts would
    otherwise land in no project and in no row - the ticket's "when excluded it
    must remain a bundle row of its own rather than vanishing". The remaining
    reason is the malformed request: a name no group of the row carries.
    """
    wanted = tuple(dict.fromkeys(names))
    if not wanted:
        return (), None
    by_name = {group["name"]: group for group in groups}
    for name in wanted:
        group = by_name.get(name)
        if group is None:
            return (), "unknown_group"
        if not group["foreign"]:
            return (), "not_a_foreign_group"
        if not group["separable"]:
            return (), "not_separable_group"
    return tuple(f"{key}/{name}/" for name in wanted), None


def _cover_group(rel_path: Optional[str], key: str, groups: Sequence[Mapping]) -> Optional[str]:
    """The group whose prefix the created cover sits under, or None.

    ``rel_path`` is STORAGE-relative: the caller normalises it first, because
    ``_cover``'s unregistered leg names a BUNDLE-relative entry while its other
    legs name a storage-relative asset path. Recorded in the marker so a later
    reader can tell a cover picked from the product's own parts from one picked
    out of a group, without re-walking.
    """
    if not rel_path:
        return None
    for group in groups:
        if rel_path.startswith(f"{key}/{group['name']}/"):
            return group["name"]
    return None


def _create_one(
    item: BundleCreateItem,
    row: Optional[Mapping],
    candidate: Optional[BundleCandidate],
    ctx: audit.AuditContext,
    *,
    asset_index: Optional[Mapping[str, Tuple[str, ...]]] = None,
    claims: Optional[Mapping[str, str]] = None,
) -> dict:
    key = (item.key or "").strip()
    if row is None or candidate is None:
        return _result(key, CREATE_FAILED, reason="unknown bundle")
    refusal = _auto_refusal(item, row, candidate)
    if refusal is not None:
        return _result(key, CREATE_FAILED, reason=refusal)
    metadata_values, metadata_refusal = _metadata_write(item, row, candidate)
    if metadata_refusal is not None:
        return _result(key, CREATE_FAILED, reason=metadata_refusal)
    cover_choice, cover_refusal = _cover_write(item)
    if cover_refusal is not None:
        return _result(key, CREATE_FAILED, reason=cover_refusal)
    # ``blocked`` still means database readiness (Mf-um7x) and the Extract door
    # still keys on ``archive_only`` - but a shape or scan mismatch is not a
    # refusal. A `.zip` row is blocked/archive_only because its only door is
    # Extract: the create was never going to make a project from an archive.
    if row["blocked_reason"] == ARCHIVE_ONLY:
        return _result(key, CREATE_BLOCKED, reason=row["blocked_reason"] or ARCHIVE_ONLY)
    # The create follows the operator's chosen shape, not the row's recommendation:
    # every create carries the shape it was made from, so a split row arranged into
    # one project and a plain one-project row are distinguishable by their markers.
    # A hand-built row may carry no `metadata_shape` and no `matches` at all; then
    # its detector word is the only signal.
    recommended_shape = row.get("metadata_shape") or row["detector"]
    matches = row.get("matches") or []
    offered = [match["shape"] for match in matches] or [recommended_shape]
    chosen = item.shape or recommended_shape
    if chosen not in offered:
        return _result(key, CREATE_FAILED, reason="unknown shape")
    match = next((candidate_match for candidate_match in matches if candidate_match["shape"] == chosen), None)
    if match is None:
        match = {"shape": recommended_shape, "rule": row["detector"], "groups": row.get("groups") or []}
    # An offer the server cannot enact is honest but not actionable: applying the
    # arrangement it needs is `Mf-v9eg` and does not exist yet. Refusing HERE - before
    # any exclusion validation, before the marker and before the session - means no
    # project, no assets and no marker are written, so a later arrange step starts
    # from a clean row. A hand-built row carries no `matches`, so its synthesised
    # match has no `creatable` at all and `.get(...) is False` is False for it.
    if match.get("creatable") is False:
        return _result(key, CREATE_FAILED, reason=NEEDS_ARRANGE)
    # Validation runs before anything that touches the session, and before
    # `find_existing_project` on purpose: an already-created row whose group list
    # moved answers a validation reason, never `already_created`. The chosen
    # match's groups are the ONE source for exclusion validation and for the
    # marker, so a create can never validate against groups the chosen shape does
    # not have. For the recommended shape this is byte-identical to the row's own
    # `groups`.
    groups = match.get("groups") or []
    prefixes, refusal = _exclusion_prefixes(key, groups, item.exclude_groups)
    if refusal is not None:
        return _result(key, CREATE_FAILED, reason=refusal)
    excluded_names = set(item.exclude_groups)
    name = (item.name or "").strip() or row["name_suggestion"]
    marker = {
        "key": key,
        "detector": row["detector"],
        "confidence": row["confidence"],
        # Every create records the shape it was made from and the rule that offered
        # that shape. `detector`/`confidence` stay byte-identical to the row, and the
        # rule is the ``engine:<shape>`` token - one owner, one spelling.
        "shape": chosen,
        "rule": match.get("rule") or row["detector"],
        "rel_dir": row["rel_path"],
        "archive": (row["archive"] or {}).get("rel_path"),
        "twin_verified": (row["archive"] or {}).get("twin_verified") if row["archive"] else None,
    }
    # The project records what the page supplied, so a later reader can tell the
    # vendor's word from a human's; `name_only` deliberately records none.
    if not item.name_only and row.get("enrichment"):
        marker["enrichment"] = row["enrichment"]
    if item.auto or item.cover is not None or item.metadata is not None:
        marker["operator"] = {
            "auto": bool(item.auto),
            "cover": "engine" if cover_choice is None else cover_choice["mode"],
            "accepted": sorted(f for f, value in metadata_values.items() if value is not None),
        }
    with Session(engine) as session:
        # The same set the report used: the files this bundle's snapshot names,
        # never a nested child bundle's models under the recursive prefix. Hoisted
        # so the existing-project check and the create-time belt read one query.
        assets = registered_assets(session, key)
        # THE ownership expression, the one `_report_row` reads: this row's own
        # snapshot, narrowed by the report-wide one-owner map. Two rows can name
        # one model and the map settled which row the deepest-first walk gave it
        # to; a create that asked its OWN question instead is how a parent and its
        # child became two projects over one set of files (Mf-quv4's hand-off).
        own_assets = _own_assets(assets, key=key, candidate=candidate)
        # The ATTACH set, filtered: an excluded group's parts stay registered and
        # stay in the row's own paths, they are simply not this product's. The
        # empty case is a no-op because `startswith(())` is False.
        included_assets = [asset for asset in own_assets if not asset.path.startswith(prefixes)]
        owned_assets = _claimed_assets(included_assets, key=key, claims=claims)
        # THE MEMBER SET, decided once: every registered asset this row owns (the
        # excluded groups already filtered out above) is a member, and its ROLE comes
        # from the scan's own classifier inside `_members_of`. There is no suffix
        # table anywhere in this path and no `kinds=` hand-down any more - the attach
        # question ("who is in the project") and the role question ("what IS that
        # file") are one expression, so a project and the row it came from cannot
        # disagree about either.
        member_set = _members_of(key, owned_assets)
        members = [
            project_create_service.MemberCandidate(entry.asset.id, entry.asset.path)
            for entry in member_set.entries
        ]
        # `model_assets` is STILL the MODEL answer, read off the same member set: it is
        # the printed parts, and it feeds `attached`, `models_by_group` below and
        # `Dedupe.containment` on the create call. It is a VIEW of the member set, never
        # a second membership test - the roles came from `classify_entry` once, in
        # `_members_of`, and this body holds no `classify_entry` call at all.
        model_assets = [entry.asset.id for entry in member_set.by_role(library_scan.MODEL)]
        # Bucket the attach set by group once, so `attached` below is a lookup
        # rather than a prefix scan repeated for every group. `attached_by_group`
        # is the same bucketing the adopt path asks, so a created project and an
        # adopted one count their groups with one implementation.
        models_by_group = attached_by_group(key, members, model_assets)
        key_prefix = f"{key}/"
        cover = row["cover"]
        if cover_choice is not None and cover_choice["mode"] == "none":
            cover = _cover_none_fields(None, COVER_NONE)
        elif cover_choice is not None:
            chosen_asset = next(
                (asset for asset in owned_assets if asset.id == cover_choice["asset_id"]), None
            )
            if chosen_asset is None:
                return _result(key, CREATE_FAILED, reason="cover asset is not in this bundle")
            cover = _cover_of(chosen_asset, "operator")
        if prefixes and cover_choice is None:
            # Same cover rule, re-asked over the INCLUDED parts of the OWNED set: a
            # second rule here would be free to disagree with the report's, and a
            # wider set than `owned_assets` would be free to disagree with the
            # project. `owned_assets` is the own-and-claimed set minus every excluded
            # group's prefix, so an excluded group's image can still never win the
            # face - it was filtered out two lines above, not here.
            # A facts-level cover under an excluded prefix names the very thing
            # the operator said is not part of this product, so it is dropped
            # rather than allowed to win the unregistered-image leg. The engine's
            # cover entry is BUNDLE-relative while a prefix is storage-relative,
            # so the key is prepended before comparing - without that the two
            # spaces never intersect and the leg wins.
            entry = _cover_entry(candidate.facts)
            if entry and f"{key}/{entry}".startswith(prefixes):
                entry = None
            cover = cover_for_registered(owned_assets, entry)
        # `_cover`'s legs do not share a key space: the registered legs name a
        # storage-relative asset path, the unregistered leg a bundle-relative
        # entry. Settle one storage-relative path here so the marker never
        # records two spaces for one field.
        cover_rel_path = cover["rel_path"]
        if cover_rel_path and not cover_rel_path.startswith(key_prefix):
            cover_rel_path = f"{key_prefix}{cover_rel_path}"
        # Neither `assets` nor `own_assets` is filtered by the exclusion: the belt
        # asks whether the row's registered subtree holds MORE than the row owns,
        # and filtering either list would make that true for every excluded create
        # - so every exclusion would answer `failed`/`contains_bundles`.
        #
        # What moved is WHERE the answer is given: the reason is settled here and
        # handed to the primitive, which asks it AFTER the dedupe and BEFORE the
        # write. A row that both already exists and would be refused therefore
        # still answers `already_created` - the operator has the project, and that
        # is the more useful sentence than one about a create that never happens.
        belt = (
            CONTAINS_BUNDLES
            if row["confidence"] != "high" and len(assets) > len(own_assets)
            else None
        )
        # The group marker runs when the CHOSEN match HAS groups, not on a shape
        # name: the operator's choice decides what the project records, and the
        # groups come from that match (the one source of truth) while `attached`
        # still comes from `models_by_group` - the files this create actually
        # attaches. Members are never decided by the shape:
        # `owned_assets`/`members`/`model_assets`/`cover`/`belt` read the same
        # expressions whatever the choice is.
        if groups:
            # The guard uses the walk's own verdict, not the row's: in-process both
            # come from ONE `scan`, so they cannot disagree.
            # `model_count` is what the SCAN registers, and `attached` is how many
            # registered MODEL assets of the group are in the attach set. Since
            # Mf-94od the count asks the scanner's own basename predicate, so a
            # `._*` AppleDouble sidecar names no model and the two numbers agree on
            # a whole group; they differ only by an EXCLUSION, which `attached`
            # carries alone. That agreement is a property of the two doors asking
            # the same hidden-name rule: the storage scan and the engine both run
            # with hidden names skipped (the default). Opening the MOUNT door with
            # `IMPORT_MOUNT_INCLUDE_HIDDEN=1` makes it register the sidecars the
            # detector still refuses, which separates the numbers again - named in
            # ADR 0010 Decision 25, off by default and untested.
            marker["groups"] = marker_groups(groups, attached=models_by_group, excluded=excluded_names)
            marker["cover"] = {
                "asset_id": cover["asset_id"],
                "rel_path": cover_rel_path,
                "reason": cover["reason"],
                "group": _cover_group(cover_rel_path, key, groups),
            }
        # One dedupe, one write, one transaction. The primitive asks
        # `find_existing_project` with the identities THIS door holds - the bundle
        # key, the twin archive this row pairs with (so a project the Extract door
        # already made from that `.zip` is seen from here), the vendor URL and the
        # name - and then writes the project and every member in the SAME commit,
        # so nothing here strands a project that holds no files.
        try:
            outcome = project_create_service.create_project_from_members(
                session,
                name=name,
                origin=project_create_service.ORIGIN_DETECTION,
                members=members,
                marker=marker,
                dedupe=project_create_service.Dedupe(
                    key=key,
                    archive=(row["archive"] or {}).get("rel_path"),
                    source_url=metadata_values["source_url"],
                    name=name,
                    # Containment is this door's identity to hold: it is asking about a
                    # folder a scan registered, and a hand-made project over those same
                    # files IS the answer. The archive door deliberately does not ask
                    # this leg (`archive_project_service`), because what it holds is one
                    # click on one archive.
                    assets=True,
                    # ... and it is asked with the MODEL set, not with the (wider) set
                    # the project is about to hold: "does a project already hold this
                    # bundle's files" is the same question `_report_row` answers with
                    # its `containment`, which is `models` - so the row and the create
                    # agree, and a hand-made project of models still stops a duplicate
                    # create (Mf-yhjo). Ask this with the member set and every re-click
                    # over a human's project writes a second project.
                    containment=tuple(model_assets),
                ),
                cover_asset_id=cover["asset_id"],
                refuse=belt,
                description=metadata_values["description"],
                creator=metadata_values["creator"],
                source_url=metadata_values["source_url"],
                project_origin="imported",
                licence=metadata_values["licence"],
                licence_url=metadata_values["licence_url"],
                asset_index=asset_index,
            )
        except HTTPException as exc:
            if exc.status_code == 409:
                # `_clean_name` runs before the NOCASE index, so a typed name that
                # cleans to an existing one slips the pre-check: a skip, never a 500.
                return _result(key, CREATE_SKIPPED, name=name, reason="project name already exists")
            return _result(key, CREATE_FAILED, name=name, reason=str(exc.detail))
        if outcome.status == project_create_service.STATUS_ALREADY_CREATED:
            return _result(
                key,
                CREATE_SKIPPED,
                project_id=outcome.project_id,
                name=outcome.name,
                reason="already_created",
            )
        if outcome.status == project_create_service.STATUS_REFUSED:
            # Nothing was written: a refusal is the primitive's word for "I did not
            # even insert", which is the case the belt exists to catch. No `name`, as
            # before - a refused create names nothing, and an item that never existed
            # in the database must not start carrying a name on the wire now that the
            # answer comes from a different module.
            return _result(key, CREATE_FAILED, reason=outcome.reason or CONTAINS_BUNDLES)
        project_id = outcome.project_id
        project_name = outcome.name
        assets_added = len(outcome.members)

    audit.record(
        audit.Event(
            audit.BUNDLE_CREATE,
            key,
            {
                "project_id": project_id,
                "created": True,
                "assets_added": assets_added,
                "assets_failed": 0,
                "detector": row["detector"],
                "confidence": row["confidence"],
                "name_only": bool(item.name_only),
                "auto": bool(item.auto),
                "shape": chosen,
                "cover": "engine" if cover_choice is None else cover_choice["mode"],
                "accepted_fields": sorted(
                    f for f, value in metadata_values.items() if value is not None
                ),
                # Always present, `[]` when nothing was excluded, so a reader can
                # tell "no exclusion" from a row that predates the field.
                "excluded_groups": [group["name"] for group in groups if group["name"] in excluded_names],
            },
        ),
        ctx=ctx,
    )
    # `skipped_assets` and `failed_assets` are kept on the wire at 0 / `[]`, and
    # they are structurally constant now: the create is one commit, so a member is
    # never "already a member" of a project that does not exist yet and an unknown
    # asset id refuses the whole request before the insert (404) rather than
    # failing per id after it. They are not deleted because they are declared
    # fields of `BundleCreateItemOut` and of the serialised OpenAPI fixture, and
    # removing a wire field is a contract change this ticket did not ask for.
    return _result(
        key,
        CREATE_CREATED,
        project_id=project_id,
        name=project_name,
        assets_added=assets_added,
        skipped_assets=0,
        failed_assets=[],
    )


def create_bundles(items: Sequence[BundleCreateItem], ctx: audit.AuditContext) -> dict:
    """Create one project per confirmed bundle; never a duplicate, never a clobber.

    The report is recomputed first and every item is looked up in it: a key the
    client sent is never trusted on its own, and a bundle that turned
    ``archive_only`` since the report was rendered creates nothing - ``blocked``
    alone is a database-readiness answer now, never a shape gate (Mf-z643). One walk serves
    both the report and the candidate lookup, so a duplicate archive is refused
    as ``unknown bundle`` by construction: it is suppressed from both.

    The scope here is the whole storage root, and the exclusion set is derived the
    same way ``build_report`` derives it - one derivation, so a create can never
    answer ``unknown bundle`` for a row its own report offered.
    """
    known = _marker_keys()
    scan = scan_bundles(STORAGE, excluded_keys=_excluded_keys("", known=known), project_keys=known)
    report = _report_from_scan(scan, root=STORAGE, scope="", limit=MAX_SCAN_ENTRIES, include_dismissed=True)
    rows = {row["key"]: row for row in report["bundles"]}
    by_key = scan.by_key
    # One query for the whole create pass, so a create is never O(items) queries.
    with Session(engine) as session:
        asset_index = project_create_service.project_assets_index(session)
    # The same report-wide one-owner map the report rows were stamped with, built
    # from the SAME scan, so the create door and the report cannot disagree about
    # which row owns a model.
    claims = _claim_owners(scan.candidates, scope="")
    counters = {CREATE_CREATED: 0, CREATE_SKIPPED: 0, CREATE_BLOCKED: 0, CREATE_FAILED: 0}
    results: List[dict] = []
    for item in items:
        key = (item.key or "").strip()
        result = _create_one(
            item,
            rows.get(key),
            by_key.get(key),
            ctx,
            asset_index=asset_index,
            claims=claims,
        )
        counters[result["status"]] += 1
        results.append(result)
    return {
        "created": counters[CREATE_CREATED],
        "skipped": counters[CREATE_SKIPPED],
        "blocked": counters[CREATE_BLOCKED],
        "failed": counters[CREATE_FAILED],
        "results": results,
    }


def _enrich_result(
    key: str,
    status: str,
    source_url: Optional[str],
    values: Mapping[str, Optional[str]],
    filled: Sequence[str],
    fetched_at: Optional[str],
    reason: Optional[str],
) -> dict:
    """One enrich result, so every status reports the same fields and no more."""
    return {
        "key": key,
        "status": status,
        "source_url": source_url,
        "creator": values["creator"],
        "licence": values["licence"],
        "licence_url": values["licence_url"],
        "filled": list(filled),
        "fetched_at": fetched_at,
        "reason": reason,
    }


def _row_values(row: Optional[Mapping]) -> Dict[str, Optional[str]]:
    """A row's creator, licence and licence URL as this action leaves them."""
    source = row or {}
    return {"creator": source.get("creator"), "licence": source.get("licence"), "licence_url": source.get("licence_url")}


def enrich_bundles(keys: Sequence[str], *, cookie: Optional[str], refresh: bool, ctx: audit.AuditContext) -> dict:
    """Read the vendor pages these bundles name and cache what the pages say.

    On demand, never part of detection: the report is recomputed from disk on
    every read and must stay a read, so the one outbound call in this module
    lives here and behind a human's click. A key that is not a live bundle and a
    bundle whose row names no vendor id both short-circuit before any fetch, so
    the only request that leaves this server is for a URL the report already
    showed. All fetches share ONE cache write: a record per item would be a
    settings write per item and a lost entry under concurrency.

    A refusal is a per-item status, not a failed request: the same bulk
    partial-success contract as ``create_bundles``, because one rate-limited
    thing must not discard the nine pages that answered.
    """
    cleaned: List[str] = []
    for key in keys:
        text = (key or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > ENRICH_MAX_KEYS:
        raise HTTPException(status_code=400, detail=f"enrich at most {ENRICH_MAX_KEYS} bundles at a time")
    if not cleaned:
        return {"fetched": 0, "cached": 0, "refused": 0, "unavailable": 0, "unknown": 0, "results": []}
    # The `create_bundles` shape: a client key is never trusted, so every key is
    # looked up in a report recomputed from disk right now. One snapshot of the
    # cache serves both the report and the merge below, so a bulk enrich cannot
    # read the map twice and see two different answers.
    scan = scan_bundles(STORAGE)
    records = get_enrichments()
    report = _report_from_scan(
        scan, root=STORAGE, scope="", limit=MAX_SCAN_ENTRIES, include_dismissed=True, enrichments=records
    )
    rows = {row["key"]: row for row in report["bundles"]}
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    pending: Dict[str, dict] = {}
    counters = {
        bundle_enrich.STATUS_FETCHED: 0,
        bundle_enrich.STATUS_CACHED: 0,
        bundle_enrich.STATUS_REFUSED: 0,
        bundle_enrich.STATUS_UNAVAILABLE: 0,
        bundle_enrich.STATUS_UNKNOWN: 0,
    }
    results: List[dict] = []
    for key in cleaned:
        row = rows.get(key)
        if row is None:
            results.append(
                _enrich_result(key, bundle_enrich.STATUS_UNKNOWN, None, _row_values(None), (), None, "not a live bundle")
            )
            counters[bundle_enrich.STATUS_UNKNOWN] += 1
            continue
        # The id comes from the resolver, the one definition ``cache_key`` is built
        # from, so the page fetched and the key written cannot name different
        # things. Both are None for the same URL, hence one refusal.
        #
        # Function-local like the fetch below, and for the same reason: the
        # resolver is the module that can open a socket, so the report path must
        # never reach it through this module's own scope (see the AST test).
        from import_resolvers import thingiverse_thing_id_from_url

        thing_id = thingiverse_thing_id_from_url(row["source_url"] or "")
        cache_key = bundle_enrich.cache_key(row["source_url"])
        if thing_id is None or cache_key is None:
            results.append(
                _enrich_result(
                    key,
                    bundle_enrich.STATUS_UNAVAILABLE,
                    row["source_url"],
                    _row_values(row),
                    (),
                    None,
                    "this bundle names no vendor id, so there is no page to read",
                )
            )
            counters[bundle_enrich.STATUS_UNAVAILABLE] += 1
            continue
        # ``pending`` is not consulted here: the report snapshot predates this
        # request's records, so a row answered from one would claim nothing was
        # filled for it. The fetch branch below reuses the pending record instead.
        record = records.get(cache_key)
        if record and not refresh and bundle_enrich.record_is_fresh(record, now):
            filled = list((row.get("enrichment") or {}).get("fields") or ())
            results.append(
                _enrich_result(
                    key,
                    bundle_enrich.STATUS_CACHED,
                    row["source_url"],
                    _row_values(row),
                    filled,
                    record.get("fetched_at"),
                    None,
                )
            )
            counters[bundle_enrich.STATUS_CACHED] += 1
            continue
        # Function-local on purpose: the two names that can open a socket stay out
        # of this module's scope so the report path cannot reach them (see the AST
        # test). Importing here costs a dict lookup and nothing else.
        from import_resolvers import fetch_thingiverse_thing_page, thingiverse_no_metadata_detail

        new_record = pending.get(cache_key)
        if new_record is None:
            try:
                html = fetch_thingiverse_thing_page(thing_id, cookie)
            except HTTPException as exc:
                results.append(
                    _enrich_result(
                        key,
                        bundle_enrich.STATUS_REFUSED,
                        row["source_url"],
                        _row_values(row),
                        (),
                        None,
                        str(exc.detail),
                    )
                )
                counters[bundle_enrich.STATUS_REFUSED] += 1
                continue
            facts = bundle_enrich.parse_thing_page(html)
            if facts is None:
                results.append(
                    _enrich_result(
                        key,
                        bundle_enrich.STATUS_REFUSED,
                        row["source_url"],
                        _row_values(row),
                        (),
                        None,
                        thingiverse_no_metadata_detail(thing_id),
                    )
                )
                counters[bundle_enrich.STATUS_REFUSED] += 1
                continue
            new_record = bundle_enrich.record_from_page(facts, row["source_url"], stamp)
        row_values = _row_values(row)
        values, fields = bundle_enrich.fills(
            row_values["creator"], row_values["licence"], row_values["licence_url"], new_record
        )
        # The page DID answer, so the record is cached even when it filled nothing
        # for this row: a sibling row with the same id, or a later create, takes it.
        pending[cache_key] = new_record
        audit.record(
            audit.Event(
                audit.BUNDLE_ENRICH,
                key,
                {"thing_id": thing_id, "source_url": row["source_url"], "fields": list(fields)},
            ),
            ctx=ctx,
        )
        results.append(
            _enrich_result(key, bundle_enrich.STATUS_FETCHED, row["source_url"], values, fields, stamp, None)
        )
        counters[bundle_enrich.STATUS_FETCHED] += 1
    if pending:
        _set_enrichments(bundle_enrich.prune_records({**records, **pending}))
    return {**counters, "results": results}
