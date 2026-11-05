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
attaches no files. What the report then says in ``twin_verified`` depends on who
paired them. For a directory some rule detects on its own, it is the names-only
comparison - the casefolded *basenames* inside the zip against the casefolded
basenames of the folder's recursive entries, never a checksum - because the
operator paired them and a mismatch is information for the operator (``False``),
not an assertion to raise. For a directory the pairing itself promoted, it is a
re-verified containment: the same containment check that had to pass for the
archive to pair at all, re-run against the bytes on disk now.

**A nested archive is a member, never a candidate.** A ``.zip`` strictly below a
detected bundle's directory belongs to that bundle: it is counted as an archive
and nothing else, so it never becomes a second row, never pairs a sibling
directory through its entry root, and never supplies a model, image or text that
would make the enclosing directory a bundle. Its central directory may still be
listed to decide pairing and its bytes hashed for duplicate identity, but nothing
inside it is read as evidence and nothing is ever extracted.
``bundle_detect.is_bundle_member`` is the one predicate for that membership; the
container strip still runs first, so a LOW folder that holds only a zip-only bundle is a
container rather than a bundle and that zip is not "nested" at all. The member
filter runs after the strip on purpose - see the note at the strip.

**Renamed extractions pair on the archive's entry root.** A vendor rename breaks
the stem rule: ``import/17_ClockworkMonster/`` beside ``17_Fates End -
ClockworkMonster Tower.zip`` shares nothing but an ordinal prefix. So a second
pass reads the archives the stem rule left behind, computes each one's common
entry root (``bundle_detect.common_entry_root``) and pairs it with a *sibling*
directory of that name - same parent only, so a root naming an ancestor or a
directory elsewhere never pairs - unless the root is a generic container name
(``files``/``images``/``models``/``stl``), which would pair half a real library
to the wrong thing. ``entry_root_verified`` then requires containment of
(casefolded bundle-relative path, declared size) against the directory's
post-claim listing. Containment, not equality: a lived-in directory holds more
than its archive. Declared size is an archive header field, so this is pairing
evidence, never an integrity claim. On either path ``twin_verified`` is a
re-verified containment for a directory the pairing promoted - it is not "always
true": a ``False`` means the bytes changed since the scan. For a directory a rule
detects on its own, the stem path's ``twin_verified`` stays an operator remark.
The archive attaches nothing on either path.

**Duplicate archives are one bundle.** Identity for an archive is its content
hash (sha256 over the file, read in bounded chunks). Two archives with the same
hash are one bundle: the copy is suppressed from both archive passes - it never
becomes a second row and can never pair a second directory - and reported in
``duplicates`` with its ``reclaimable_bytes`` and a copyable tab-separated
``duplicates_text``. Hashing is size-first: only archives sharing a size with
another archive are opened, so a uniquely-sized archive is never read, and the
worst case (every archive the same size) reads each exactly once. The report
never deletes, renames or moves anything: it only renders text.

A duplicate that is a *member* of a live bundle is never on the deletable side:
it is removed from the group, and a group left with fewer than two non-member
paths disappears. An outside copy whose only twin is a member is therefore either
still offered as its own archive bundle (when it was the group's keep) or was
already suppressed (when it was not); the report never invites deleting part of a
live bundle.

**A verified pairing makes an offered-nowhere directory a low bundle.** The
Clockwork shape - models and images and no text - is not an R3 bundle, so a rule
that only read the listing would render nothing for it. When a pairing has proved
that an archive beside a directory owns that directory, the archive supplies what
the listing cannot: its identity. The directory is therefore offered as a real
``generic-bundle`` / ``low`` candidate keyed at the DIRECTORY, with that archive
reported as its ``archive`` and no file attached from it - the archive is
provenance, the low confidence says the shape was inferred. It is then creatable
exactly like any other low candidate: a resolved ``Folder`` and registered model
assets, nothing about the detector. The pairing arrives one of two ways: by the
archive's entry root, or by a stem twin whose directory no rule offers on its own
and whose payload was verified by containment. Images and text inside the
directory are neither required nor disqualifying; own-depth/cardinality
(``bundle_detect._own_depth_model_count``) is the only shape guard left, and it
is load-bearing - it is what keeps a pile from being promoted on an ancestor's
or a child's models.

Be honest about the bounds. **Without a pairing the shape is silent**: model
files and a render are too weak on their own, so nothing is offered and nothing
can be created - unless the folder's NAME reads like a title, which is R5's
separate signal and not a loosening of this shape rule (ADR 0010 Decision 21).
**The pairing never overrides the container rule**: a directory
that holds bundles keeps the blocked ``archive_only`` row and creates nothing, so
a pile of models with one zip somewhere inside it is still a pile. **A promotion
is refused entirely - no row at all - when the directory's subtree holds a
candidate a stronger rule produced** (``medium`` or ``high``), because then the
directory is a container the operator made by hand and the child is the real
bundle (ADR 0010 Decision 18, ``Mf-v3i3``). **A pairing
names the directory itself and never a descendant**, so it can never qualify one.
**A stem twin of a directory some rule already offers keeps the names-only
answer** (the archive is that directory's provenance, never a pairing), and **a
stem twin whose directory no rule offers and whose payload fails containment is
consumed** rather than retried under the weaker entry-root name. A refused pair
stays visible rather than vanishing, which is why it keeps the archive-only row.

A zip with no extracted twin is still read, through
``zip_service.read_zip_text_entry`` (bounded, never extracted), so the report can
show its provenance; it is always ``blocked``/``archive_only``, because creation
happens from registered assets. The extraction/registration door for an archive
already in the library is ``POST /import/archive`` (Mf-1zmd): it extracts the zip
in place into the twin directory this detector already pairs, and the next scan
registers it, so no detector change is involved.

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
an exact casefolded name, then asset containment over the models a create would
attach - and a hit is ``skipped`` with the existing project id and the project
is never touched. A human edit is therefore never overwritten by
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

**Every absence is named (Mf-9ygi).** A scan also returns the ledger of what it
refused: one ``BundleRefusal`` per path the walk saw and did not offer, carrying
the rule that refused it (``REFUSAL_RULES``). The ledger is derived AFTER the
candidate set is final and is never an input to it, so reporting a refusal can
never change which rows are offered. This is the part the 114 -> 112
investigation lacked: 46 archives were invisible and the report said nothing at
all about them.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import AbstractSet, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple
from zipfile import BadZipFile

from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import Session, select

import asset_urls
import audit
import bundle_detect
import bundle_enrich
import db
import library_scan
import project_service
import settings_service
import zip_service
from db import engine
from models import Asset, Folder, Project, ProjectAsset

# The storage root bundle keys are resolved against. A module global so tests can
# point it at a temp tree exactly like ``library_scan.STORAGE``.
STORAGE = db.STORAGE
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

# A create-time refusal: the candidate's assets include another bundle's. This is
# a reason on CREATE_FAILED, never a blocked_reason - `blocked` means database
# readiness only (Mf-um7x).
CONTAINS_BUNDLES = "contains_bundles"

# Which of the four identities found an existing project. Reported because they
# are not equally certain, so the client can say how sure it is.
MATCH_MARKER = "marker"
MATCH_SOURCE_URL = "source_url"
MATCH_NAME = "name"
MATCH_ASSETS = "assets"

# Why this service refused a path the walk saw. The detector owns the R1-R5
# vocabulary (``bundle_detect.REFUSAL_RULES``); these are the refusals only the
# scan can make, because only the scan sees the whole tree: a duplicate archive, a
# hidden twin, an archive nobody could open, a folder that holds bundles, and a
# file that belongs to a live bundle. A path that is not offered carries exactly
# one of these or one detector reason - never silence, which is what made the
# invisible archives of Mf-9ygi expensive to find.
REFUSAL_CONTAINER = "container-rule"
REFUSAL_DUPLICATE = "duplicate"
REFUSAL_NESTED_MEMBER = "nested-member"
REFUSAL_TWIN_CONSUMED = "twin-consumed"
REFUSAL_TWIN_UNVERIFIED = "twin-unverified"
REFUSAL_UNREADABLE_ARCHIVE = "unreadable-archive"
REFUSAL_MEMBER_OF_BUNDLE = "member-of-bundle"

# Every reason a refusal row may carry: the detector's vocabulary plus this
# service's, so a caller can validate a ledger without knowledge of either half.
REFUSAL_RULES = bundle_detect.REFUSAL_RULES | frozenset(
    {
        REFUSAL_CONTAINER,
        REFUSAL_DUPLICATE,
        REFUSAL_NESTED_MEMBER,
        REFUSAL_TWIN_CONSUMED,
        REFUSAL_TWIN_UNVERIFIED,
        REFUSAL_UNREADABLE_ARCHIVE,
        REFUSAL_MEMBER_OF_BUNDLE,
    }
)


@dataclass(frozen=True)
class BundleCandidate:
    """One directory or zip that looks like a bundle, before any database read.

    ``confidence`` is internal: it decides whether a candidate may claim a
    subtree. It is never returned in a report row - the row carries the facts
    from its own detect call. ``entry_root`` is the archive entry root that
    paired this bundle's archive, ``None`` when the stem rule paired it.
    ``pair_only`` means a paired directory the container rule refuses: it is
    shown, never creatable. ``archive_owned`` means a verified pairing owns this
    directory: the archive supplies its identity on the entry-root route or on the
    stem route, and the low confidence says the shape was inferred, so it is
    offered at LOW even when no rule offers the directory on its own and it is
    creatable like any other LOW candidate. ``title_name`` means the row exists
    only because the folder's NAME reads like a title, so its facts come from
    ``bundle_detect.title_name_facts`` and its confidence is LOW.
    """

    key: str
    snapshot: bundle_detect.BundleSnapshot
    archive_path: Optional[str]
    is_archive: bool
    confidence: str
    entry_root: Optional[str] = None
    pair_only: bool = False
    archive_owned: bool = False
    title_name: bool = False

    @property
    def offers_archive_only(self) -> bool:
        """Whether the row is reported ``blocked``/``archive_only``.

        An archive of its own, and a paired directory the container rule refuses,
        are the same answer to the operator: there is nothing here to create, and
        the remedy is the import door.
        """
        return self.is_archive or self.pair_only


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
    """One walk of the storage tree: the candidates, the duplicates, the refusals."""

    candidates: List[BundleCandidate]
    truncated: bool
    duplicates: Tuple[bundle_detect.DuplicateGroup, ...] = ()
    # Every path the walk refused to offer, sorted and named by its rule. Derived
    # from the candidate set, never an input to it: adding a refusal can never
    # change which rows are offered.
    refusals: Tuple[BundleRefusal, ...] = ()


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


def _relates_to_any(rel_dir: str, taken: AbstractSet[str]) -> bool:
    """Whether ``rel_dir`` is already spoken for by a row, or is a container.

    Three legs, and each one is a distinct refusal: the key IS a row (it was
    claimed by a rule, promoted by a pairing or recorded as pair-only); it sits
    INSIDE a row, so a name-only guess must never re-offer a bundle's own
    subtree as a second thing; or it HOLDS a row, which is the container rule -
    a folder of bundles is a pile whatever its name says. ``taken`` includes
    archive rows, so a folder holding a zip-only bundle is a container too.
    """
    prefix = rel_dir + "/"
    return any(
        key == rel_dir or key.startswith(prefix) or rel_dir.startswith(key + "/") for key in taken
    )


def _holds_stronger_candidate(rel_dir: str, candidates: Sequence[BundleCandidate]) -> bool:
    """Whether a STRONGER rule already detected a bundle strictly below ``rel_dir``.

    An ``archive_owned`` row is a LOW shape inference, and it must never swallow a
    bundle a stronger rule produced (ADR 0010 Decision 18, owner decision
    2026-11-04: "I will sometimes extract a file and copy the zip in" - such a
    parent is a hand-made container and the child is the real bundle). The
    discriminating question is "does the subtree hold a candidate a stronger rule
    produced", never "does it hold a zip": a zip that is only a MEMBER of the
    enclosing bundle is not a candidate at that strength, which is why the
    Clockwork directory keeps its row (Decision 17). The LOW/MEDIUM boundary is
    ``bundle_detect.STRONG_CONFIDENCES``, membership rather than a literal.
    """
    prefix = rel_dir + "/"
    return any(
        candidate.key.startswith(prefix) and candidate.confidence in bundle_detect.STRONG_CONFIDENCES
        for candidate in candidates
    )


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
    return _detect_folder_decision(root, snapshot).facts


def _detect_folder_decision(root: Path, snapshot: bundle_detect.BundleSnapshot) -> bundle_detect.Detection:
    """The folder's decision, so a refusal can name its rule as well as a row its facts."""
    docs = _read_docs(Path(root) / snapshot.rel_dir, bundle_detect.doc_entry_candidates(snapshot))
    return bundle_detect.detect_bundle_decision(snapshot, docs)


def _detect_archive(zip_path: Path, snapshot: bundle_detect.BundleSnapshot) -> Optional[bundle_detect.BundleFacts]:
    """The archive's facts, shared with ``_candidate_facts`` when a row is rendered.

    It delegates to ``_detect_archive_decision`` so the archive carrier's rule set
    - R1/R2/R4/R3 and then R6 - is decided in exactly one place; a row rendered
    here can never disagree with the candidate the walk offered.
    """
    return _detect_archive_decision(zip_path, snapshot).facts


def _detect_archive_decision(zip_path: Path, snapshot: bundle_detect.BundleSnapshot) -> bundle_detect.Detection:
    """The archive's decision: the bounded read, then the archive-carrier rules.

    ``archive_bundle_decision`` is a wrapper around ``detect_bundle_decision``, so
    R1/R2/R4/R3 keep their precedence and R6 is consulted only when all four
    refused. A directory is never handed this function; R6 is archive-only.
    """
    docs: Dict[str, str] = {}
    for candidate in bundle_detect.doc_entry_candidates(snapshot):
        text = zip_service.read_zip_text_entry(zip_path, candidate, bundle_detect.BUNDLE_DOC_MAX_BYTES)
        if text is not None:
            docs[candidate] = text
    return bundle_detect.archive_bundle_decision(snapshot, docs)


def _refusal_of(detection: bundle_detect.Detection) -> str:
    """The rule that refused this detection; a refused decision always names one."""
    if detection.refusal is None:
        raise RuntimeError("a refused detection must name its rule")
    return detection.refusal


def _refuse(ledger: Dict[str, str], rel_path: str, rule: str, *, override: bool = False) -> None:
    """Record why ``rel_path`` is not offered. The FIRST refusal wins by default.

    First-wins is what makes the ledger stable: the folder pass sees a directory
    before any later pass does, so its reason is the most specific one. A pass
    that has strictly better information (a promotion refusing a pair it has
    already examined) passes ``override=True``.
    """
    if override:
        ledger[rel_path] = rule
    else:
        ledger.setdefault(rel_path, rule)


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


def _build_candidates(
    root: Path,
    files_by_dir: Mapping[str, Sequence[Tuple[str, int]]],
    dirs_by_parent: Mapping[str, set],
    suppressed: AbstractSet[str],
    refusals: Dict[str, str],
) -> List[BundleCandidate]:
    """Every bundle under ``root`` from one walk's listings.

    ``suppressed`` holds the duplicate archives: a byte-identical copy is not a
    bundle of its own, so both archive passes skip it and only the group's
    authoritative path can be a row (or a pairing).

    ``refusals`` is the ledger this function WRITES: every path it declines gets
    a rule, so a caller can say why something is missing. It is never read back to
    decide anything, so recording a refusal cannot change which rows are offered.
    """
    folded = _subtree(files_by_dir)
    found: List[BundleCandidate] = []
    # The strict ancestors of every bundle already claimed. An INFERRED candidate
    # in this set contains a bundle and is therefore a container, never a bundle -
    # a guess must never swallow a subtree. The guard covers every inferred
    # confidence, not just `low`: a medium guess (an id in a name) can swallow a
    # subtree exactly as a low shape guess can. It is skipped *without* claiming,
    # so its own files still bubble up to a legitimate ancestor.
    claimed_ancestors: set = set()
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
            # Nothing left in this directory's own view, but the walk DID see files
            # below it: they were all claimed by child bundles, so this directory
            # holds bundles and is a container, never a bundle.
            if folded.get(rel_dir):
                _refuse(refusals, rel_dir, REFUSAL_CONTAINER)
            continue
        snapshot = _folder_snapshot(rel_dir, entries)
        detection = _detect_folder_decision(root, snapshot)
        facts = detection.facts
        if facts is None:
            _refuse(refusals, rel_dir, _refusal_of(detection))
            continue
        if facts.confidence in bundle_detect.INFERRED_CONFIDENCES and rel_dir in claimed_ancestors:
            _refuse(refusals, rel_dir, REFUSAL_CONTAINER)
            continue
        found.append(
            BundleCandidate(
                key=rel_dir,
                snapshot=snapshot,
                archive_path=None,
                is_archive=False,
                confidence=facts.confidence,
            )
        )
        _forget_claimed_subtree(work, rel_dir)
        ancestor = _parent(rel_dir)
        while ancestor:
            claimed_ancestors.add(ancestor)
            ancestor = _parent(ancestor)

    # Pairing: the stem rule has first refusal, then the entry-root rule for the
    # archives it left behind. Each pairing is `(zip_rel, Optional[entry_root])` - no
    # entry root means the stem route paired it.
    #
    # A stem twin whose directory some rule already offers keeps the old answer
    # byte-for-byte: the archive is that directory's provenance, never a pairing.
    # A stem twin whose directory NO rule offers is verified by containment (the
    # archive's own entries decide the wrapper) and joins the same map with no entry
    # root. A stem twin that fails that verification is CONSUMED: it does not fall
    # through to the entry-root pass, because the stem is the operator's own
    # statement of which directory the archive is, and a failed check must never be
    # retried under a weaker name.
    #
    # The entry-root pass then requires the archive's common entry root to name a
    # sibling DIRECTORY (same parent only) and its entries to contain nothing the
    # directory's post-claim listing does not hold. A duplicate archive is skipped:
    # the content hash already says which of the pair is the original.
    pairs: Dict[str, List[Tuple[str, Optional[str]]]] = {}
    paired_zips: set = set()
    detected = {candidate.key for candidate in found}
    for rel_path in sorted(_zip_files(files_by_dir), key=lambda item: (item.casefold(), item)):
        if rel_path in suppressed:
            _refuse(refusals, rel_path, REFUSAL_DUPLICATE)
            continue
        twin = _twin_directory(rel_path, dirs_by_parent)
        if twin is not None:
            if twin in detected:
                # The directory is offered on its own: the archive is its
                # provenance, and a second row from the same bytes is a duplicate.
                _refuse(refusals, rel_path, REFUSAL_TWIN_CONSUMED)
                continue
            if _stem_pair_verified(root / rel_path, work.get(twin, ())):
                pairs.setdefault(twin, []).append((rel_path, None))
                paired_zips.add(rel_path)
            else:
                # Consumed: the stem is the operator's statement of which directory
                # this archive is, and a failed check is never retried under a weaker
                # name. The pairing that did not happen is named, not silent.
                _refuse(refusals, rel_path, REFUSAL_TWIN_UNVERIFIED)
            continue
        paired = _entry_root_pair(root, rel_path, dirs_by_parent, work)
        if paired is None:
            # Silence here is deliberate: the zip-only pass below decides this
            # archive, and two answers for one path would be a lie.
            continue
        folder, entry_root = paired
        pairs.setdefault(folder, []).append((rel_path, entry_root))
        paired_zips.add(rel_path)

    # Zip-only candidates: an archive whose extracted twin is not beside it. A
    # duplicate is skipped here too, so a copy never becomes a second bundle row.
    for rel_path in sorted(_zip_files(files_by_dir), key=lambda item: (item.casefold(), item)):
        if rel_path in suppressed:
            # Already recorded as `duplicate` by the pairing pass.
            continue
        if rel_path in paired_zips:
            # Provisional: the promotion loop below overrides this when it refuses
            # the pairing, and the reconciliation drops it when the directory is
            # offered. A pairing that does produce a row leaves the archive with no
            # row of its own, which is exactly what `twin-consumed` says.
            _refuse(refusals, rel_path, REFUSAL_TWIN_CONSUMED)
            continue
        twin = _twin_directory(rel_path, dirs_by_parent)
        if twin:
            _refuse(refusals, rel_path, REFUSAL_TWIN_UNVERIFIED)
            continue
        snapshot = _zip_snapshot(root / rel_path, rel_path)
        if snapshot is None:
            _refuse(refusals, rel_path, REFUSAL_UNREADABLE_ARCHIVE)
            continue
        detection = _detect_archive_decision(root / rel_path, snapshot)
        facts = detection.facts
        if facts is None:
            _refuse(refusals, rel_path, _refusal_of(detection))
            continue
        found.append(
            BundleCandidate(
                key=rel_path,
                snapshot=snapshot,
                archive_path=None,
                is_archive=True,
                confidence=facts.confidence,
            )
        )

    # A directory a verified archive owns and no rule offers on its own - paired by
    # its ENTRY ROOT or by its STEM. The archive supplies the identity, so this is a
    # real LOW candidate keyed at the DIRECTORY. The container rule applies to a
    # pairing exactly as it does to any other guess - a pairing that names a
    # directory holding a CLAIMED bundle is a pile and proves nothing - and such a
    # refused pairing keeps the blocked row instead, so the pair stays visible and
    # creates nothing. A pairing refused because the subtree holds a STRONGER
    # candidate is silent instead; see the guard in the loop.
    #
    # A pairing that names a directory INSIDE an already-claimed bundle never
    # reaches a row of its own, and needs no extra refusal here: the archive it
    # paired with sits inside that bundle too, so the member filter at the end
    # prunes the pairing and the promoted candidate with it. A containing LOW
    # guess is the mirror case and loses deliberately: promoting this directory
    # puts its key in `contained_keys`, so the strip below drops the guess rather
    # than the archived evidence.
    pair_only_specs: Dict[str, Tuple[bundle_detect.BundleSnapshot, bundle_detect.BundleFacts]] = {}
    for folder in sorted(pairs, key=lambda item: (item.casefold(), item)):
        pair_zips = [zip_rel for zip_rel, _entry_root in pairs[folder]]
        if any(candidate.key == folder for candidate in found):
            # The directory is offered on its own; the archive stays attached to it
            # as provenance, and its `twin-consumed` reason is what the ledger says.
            continue
        entries = work.get(folder) or []
        if not entries:
            for zip_rel in pair_zips:
                _refuse(refusals, zip_rel, REFUSAL_TWIN_UNVERIFIED, override=True)
            continue
        snapshot = _folder_snapshot(folder, entries)
        detection = bundle_detect.archive_owned_decision(snapshot)
        facts = detection.facts
        if facts is None:
            for zip_rel in pair_zips:
                _refuse(refusals, zip_rel, _refusal_of(detection), override=True)
            continue
        if folder in claimed_ancestors:
            pair_only_specs[folder] = (snapshot, facts)
            for zip_rel in pair_zips:
                _refuse(refusals, zip_rel, REFUSAL_TWIN_CONSUMED, override=True)
            continue
        # A promoted LOW row must never swallow a bundle a STRONGER rule produced
        # (ADR 0010 Decision 18, owner decision 2026-11-04). The parent gets NO row
        # at all - deliberately not `pair_only_specs`: a pair-only key joins
        # `bundle_dirs`, which makes `_nested_archives` mark the strong child a
        # member, i.e. the swallow again by a different route. The residual is
        # silence for the parent, the same silence this function already documents
        # for a pruned promotion (a directory simply not offered is never a wrong
        # project and never a delete invitation), and the archive of the refused
        # pair attaches to no row because the zip-only pass has already run. The
        # child keeps its `blocked`/`archive_only` archive row, whose remedy is the
        # import door, exactly like any other archive-only row. The guard bounds
        # the PROMOTION and nothing else: it sits after the `claimed_ancestors`
        # branch on purpose, so a directory holding a CLAIMED child keeps its
        # blocked pair-only row (the container rule at its existing site) and the
        # ordering must not be "tidied" later. A weaker candidate below - a LOW
        # archive, e.g. a member zip inside the Clockwork directory - does not
        # refuse the promotion; that is the discriminator.
        if _holds_stronger_candidate(folder, found):
            for zip_rel in pair_zips:
                _refuse(refusals, zip_rel, REFUSAL_CONTAINER, override=True)
            continue
        found.append(
            BundleCandidate(
                key=folder,
                snapshot=snapshot,
                archive_path=None,
                is_archive=False,
                confidence=facts.confidence,
                archive_owned=True,
            )
        )
        for zip_rel in pair_zips:
            _refuse(refusals, zip_rel, REFUSAL_TWIN_CONSUMED, override=True)

    # R5: a title-shaped folder of models is a LOW, name-derived bundle. This is
    # a post-pass and NOT a `detect_bundle` branch, and the placement is
    # load-bearing: the folder pass is deepest-first, so a rule inside it would
    # claim `LittleTup`'s row for a child and lose `17_ClockworkMonster`'s
    # promoted row (16 witnesses red, 12 service and 4 pure). It runs after the
    # promotion so a verified archive keeps its stronger `archive_owned` row, and
    # before the container strip so a title folder holding a bundle is refused by
    # `_relates_to_any` rather than offered as a pile. Shallowest-first, so a
    # title parent claims its title child instead of the other way round.
    taken = {candidate.key for candidate in found}
    taken.update(pair_only_specs)
    for rel_dir in sorted(work, key=lambda item: (_depth(item), item.casefold(), item)):
        if _relates_to_any(rel_dir, taken):
            # Already a row, inside one, or holding one. The two legs are different
            # answers: a member of a live bundle is a member, everything else here
            # is a container.
            member = any(rel_dir.startswith(key + "/") for key in taken)
            _refuse(refusals, rel_dir, REFUSAL_MEMBER_OF_BUNDLE if member else REFUSAL_CONTAINER)
            continue
        entries = work[rel_dir]
        if not entries:
            continue
        snapshot = _folder_snapshot(rel_dir, entries)
        detection = bundle_detect.title_name_decision(snapshot)
        facts = detection.facts
        if facts is None:
            # setdefault semantics on purpose: an earlier, more specific detector
            # reason for this directory must survive this post-pass.
            _refuse(refusals, rel_dir, _refusal_of(detection))
            continue
        found.append(
            BundleCandidate(
                key=rel_dir,
                snapshot=snapshot,
                archive_path=None,
                is_archive=False,
                confidence=facts.confidence,
                title_name=True,
            )
        )
        taken.add(rel_dir)

    # An archive-only bundle is still a bundle: a low folder that strictly
    # contains one is a container too, and the folder pass cannot see it because
    # the archive is read after the directory walk. `contained_keys` is the keys of
    # both kinds of archive-only row - an archive of its own, and a paired
    # directory the container rule refuses - plus every directory a verified
    # pairing promoted, because a promoted candidate is a bundle in its own right.
    # A folder of INFERRED confidence that strictly contains any of them holds
    # nothing but another thing's payload and is therefore a container, never a
    # bundle. The guard covers every inferred confidence, not just `low`: a
    # medium guess (an id in a name) containing a promoted or archive-only
    # bundle is a pile exactly as a low one is.
    #
    # None of those rows is ever a *target* of the strip. A pair-only directory
    # and a promoted one are verified evidence rather than a guess, and the
    # archive's membership does not depend on the row, so dropping one would only
    # hide the pair the operator can see on disk. The exemption swallows nothing:
    # a guess that contains a promoted candidate is still dropped, which is the
    # container rule applied to this pass. The promoted exemption is Decision 17's
    # obligation: the Clockwork directory owns its nested zip, that zip is
    # archive-only at this point, so without the exemption the directory would
    # vanish and the zip would be the only row.
    contained_keys = [candidate.key for candidate in found if candidate.offers_archive_only]
    contained_keys.extend(pair_only_specs)
    contained_keys.extend(candidate.key for candidate in found if candidate.archive_owned)
    if contained_keys:
        survived: List[BundleCandidate] = []
        for candidate in found:
            if (
                not candidate.is_archive
                and not candidate.pair_only
                and not candidate.archive_owned
                and candidate.confidence in bundle_detect.INFERRED_CONFIDENCES
                and any(key.startswith(candidate.key + "/") for key in contained_keys)
            ):
                _refuse(refusals, candidate.key, REFUSAL_CONTAINER)
                continue
            survived.append(candidate)
        found = survived

    # A zip inside a detected bundle is a supporting file of that bundle, never a
    # candidate and never a pairing (Mf-74x5). The bundle directories are the
    # folder candidates that survived the strip plus the pair-only directories
    # above; membership is strict, so a twin beside a directory is not a member.
    bundle_dirs = {candidate.key for candidate in found if not candidate.is_archive}
    bundle_dirs.update(pair_only_specs)
    nested = _nested_archives(files_by_dir, bundle_dirs)
    if nested:
        for candidate in found:
            if candidate.is_archive and candidate.key in nested:
                _refuse(refusals, candidate.key, REFUSAL_NESTED_MEMBER)
        found = [
            candidate
            for candidate in found
            if not (candidate.is_archive and candidate.key in nested)
        ]
        pairs = {
            folder: [pair for pair in folder_pairs if pair[0] not in nested]
            for folder, folder_pairs in pairs.items()
        }
        pairs = {folder: folder_pairs for folder, folder_pairs in pairs.items() if folder_pairs}
        pair_only_specs = {
            folder: spec for folder, spec in pair_only_specs.items() if folder in pairs
        }
        # A promoted candidate whose pairing was just pruned is no longer
        # archive-owned: a zip inside a bundle proved nothing about a sibling
        # directory. The residual is silence - that directory is simply not
        # offered - which is never a wrong project and never a delete invitation.
        for candidate in found:
            if candidate.archive_owned and candidate.key not in pairs:
                _refuse(refusals, candidate.key, REFUSAL_MEMBER_OF_BUNDLE)
        found = [
            candidate
            for candidate in found
            if not (candidate.archive_owned and candidate.key not in pairs)
        ]

    for folder in sorted(pair_only_specs, key=lambda item: (item.casefold(), item)):
        snapshot, facts = pair_only_specs[folder]
        archive_path, entry_root = _paired_archive(pairs[folder])
        found.append(
            BundleCandidate(
                key=folder,
                snapshot=snapshot,
                archive_path=archive_path,
                is_archive=False,
                confidence=facts.confidence,
                entry_root=entry_root,
                pair_only=True,
            )
        )

    # A folder candidate's archive is provenance: reported, never a member. A row
    # that is already archive-only is reported as it was built.
    with_twins: List[BundleCandidate] = []
    for candidate in found:
        if candidate.offers_archive_only:
            with_twins.append(candidate)
            continue
        stem = _twin_archive(candidate.key, files_by_dir)
        archive, entry_root = stem, None
        if candidate.archive_owned or stem is None:
            paired = pairs.get(candidate.key)
            if paired:
                archive, entry_root = _paired_archive(paired)
        with_twins.append(
            BundleCandidate(
                key=candidate.key,
                snapshot=candidate.snapshot,
                archive_path=archive,
                is_archive=candidate.is_archive,
                confidence=candidate.confidence,
                entry_root=entry_root,
                pair_only=candidate.pair_only,
                archive_owned=candidate.archive_owned,
                title_name=candidate.title_name,
            )
        )
    with_twins.sort(key=lambda candidate: (candidate.key.casefold(), candidate.key))
    return with_twins


def scan_bundles(root: Path) -> BundleScan:
    """One walk of the storage tree: bundle candidates plus duplicate archives.

    Detection is one deepest-first pass. ``bundle_detect.detect_bundle`` applies R1 ->
    R2 -> R4 -> R3 strongest-first within a single directory, so the only ordering that
    pass needs is children before parents: a child bundle is claimed before its
    parent is evaluated, and claiming it removes it from the parent's view. The
    R5 name post-pass runs afterwards and shallowest-first, over keys no row
    already owns, so it can only add a row and never reorder a claim. A directory can therefore only ever be a bundle in its own right - it
    never inherits a child's models, and a directory that holds nothing but child
    bundles is not a bundle at all. An INFERRED candidate - ``low``, or the
    ``medium`` of an id in the name - whose key is a strict ancestor of an
    already-claimed bundle is a *container* and is skipped without claiming, so
    its own files still bubble up to a legitimate ancestor; a HIGH candidate is
    exempt because its documents sit at depth 0 and it attaches only its own
    models.

    Byte-identical archives are one bundle, so the walk also groups duplicates by
    content hash and suppresses every copy from both archive passes.

    A ``.zip`` strictly below a detected bundle's directory is a *member* of that
    bundle (``bundle_detect.is_bundle_member``): it is never a candidate and never
    a pairing, and it is removed from every duplicate group before it is reported,
    so the report never offers part of a live bundle for deletion.
    """
    root = Path(root)
    files_by_dir, dirs_by_parent, truncated = _walk_tree(root)
    duplicates = duplicate_archives(root, files_by_dir)
    suppressed = frozenset(bundle_detect.duplicate_archive_paths(duplicates))
    refusals: Dict[str, str] = {}
    candidates = _build_candidates(root, files_by_dir, dirs_by_parent, suppressed, refusals)
    bundle_dirs = {candidate.key for candidate in candidates if not candidate.is_archive}
    nested = _nested_archives(files_by_dir, bundle_dirs)
    duplicates = _without_nested_members(duplicates, nested)
    return BundleScan(
        candidates=candidates,
        truncated=truncated,
        duplicates=duplicates,
        refusals=_reconcile_refusals(
            refusals,
            candidates,
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
    nested: AbstractSet[str],
    suppressed: AbstractSet[str],
    files_by_dir: Mapping[str, Sequence[Tuple[str, int]]],
) -> Tuple[BundleRefusal, ...]:
    """Turn the ledger written during the passes into one reason per refused path.

    The passes record reasons as they go, which means a keep can still look like a
    refusal until the whole candidate set is known - an archive paired with a
    directory that was then promoted is "consumed", not missing. This step reads
    the FINAL candidate set and settles each entry, so the ledger never contradicts
    the rows. It only ever edits the ledger: no candidate is added or dropped here.
    """
    offered_keys = {candidate.key for candidate in candidates}
    offered_dirs = {candidate.key for candidate in candidates if not candidate.is_archive}
    attached = {candidate.archive_path for candidate in candidates if candidate.archive_path}
    all_zips = set(_zip_files(files_by_dir))

    for rel_path in all_zips:
        if rel_path in offered_keys:
            refusals.pop(rel_path, None)
        elif rel_path in nested:
            refusals[rel_path] = REFUSAL_NESTED_MEMBER
        elif rel_path in suppressed:
            refusals[rel_path] = REFUSAL_DUPLICATE
        elif rel_path in attached:
            refusals[rel_path] = REFUSAL_TWIN_CONSUMED
        elif refusals.get(rel_path) == REFUSAL_TWIN_CONSUMED:
            # A twin directory existed but no row was offered for it, so the archive
            # was not consumed by anything: the pairing was refused.
            refusals[rel_path] = REFUSAL_CONTAINER

    for rel_path in list(refusals):
        if rel_path in all_zips:
            continue
        if rel_path in offered_keys:
            refusals.pop(rel_path)
        elif _strictly_inside_any(rel_path, offered_dirs):
            refusals[rel_path] = REFUSAL_MEMBER_OF_BUNDLE

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
        if bundle_detect.is_bundle_member(rel_path, bundle_dirs)
    }


def duplicate_archives(
    root: Path, files_by_dir: Mapping[str, Sequence[Tuple[str, int]]]
) -> Tuple[bundle_detect.DuplicateGroup, ...]:
    """Byte-identical archives, grouped by sha256, hashing only shared-size entries.

    Size-first is the bound: a byte-identical copy is necessarily equal-size, so
    an archive whose size occurs once is never opened. The worst case is a library
    where every archive is the same size - then each archive is read exactly once,
    never more. An unreadable archive contributes no identity and no group.
    """
    identities: List[bundle_detect.ArchiveIdentity] = []
    for size, paths in bundle_detect.equal_size_archive_groups(_zip_entries(files_by_dir)):
        for rel_path in paths:
            digest = _content_digest(Path(root) / rel_path)
            if digest is not None:
                identities.append(bundle_detect.ArchiveIdentity(rel_path=rel_path, size=size, digest=digest))
    return bundle_detect.duplicate_groups(identities)


def _without_nested_members(
    groups: Sequence[bundle_detect.DuplicateGroup], nested: AbstractSet[str]
) -> Tuple[bundle_detect.DuplicateGroup, ...]:
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
    kept: List[bundle_detect.DuplicateGroup] = []
    for group in groups:
        paths = tuple(path for path in group.paths if path not in nested)
        if len(paths) < 2:
            continue
        kept.append(bundle_detect.DuplicateGroup(digest=group.digest, size=group.size, paths=paths))
    return tuple(kept)


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


def _entry_root_pair(
    root: Path,
    rel_path: str,
    dirs_by_parent: Mapping[str, set],
    work: Mapping[str, Sequence[bundle_detect.BundleEntry]],
) -> Optional[Tuple[str, str]]:
    """``(folder_rel, entry_root)`` for an archive that pairs by its entry root.

    Same-parent only: a root naming an ancestor or a directory elsewhere in the
    library is not evidence that the two sit together, and pairing across parents
    would attach one archive to a directory it was never extracted into. The
    comparison set is ``work``'s post-claim listing, so pairing can never disagree
    with the claim tree. A generic container root is refused outright (the name
    says nothing about which thing the archive holds), and any read failure is
    ``None`` - never an exception on a read route.
    """
    zip_path = Path(root) / rel_path
    try:
        entries = zip_service.list_zip_entries(zip_path)
    except (BadZipFile, OSError):
        return None
    entry_root = bundle_detect.common_entry_root([str(entry["path"]) for entry in entries])
    if entry_root is None or bundle_detect.is_generic_container_root(entry_root):
        return None
    parent = _parent(rel_path)
    wanted = entry_root.casefold()
    for name in sorted(dirs_by_parent.get(parent, ())):
        if name.casefold() != wanted:
            continue
        folder = f"{parent}/{name}" if parent else name
        if entry_root_verified(zip_path, entry_root, work.get(folder, ())):
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


def project_assets_index(session: Session) -> Dict[str, Tuple[str, ...]]:
    """Every asset's project memberships as ``{asset id: (project id, ...)}``.

    Loaded once per report: deciding whether a project contains a bundle's files
    is a dictionary lookup, not a table scan per candidate, so the cost is one
    statement for the whole report however many bundles are offered.

    Each tuple is ordered by ``(asset id, project id)`` so nothing downstream
    depends on row order, and only assets with a ``storage_path`` are indexed -
    the same refusal the rest of the report applies, because a row with no path
    is not a file this bundle could contain.
    """
    index: Dict[str, List[str]] = {}
    statement = (
        select(ProjectAsset)
        .join(Asset, Asset.id == ProjectAsset.asset_id)
        .where(Asset.storage_path.is_not(None))
        .order_by(ProjectAsset.asset_id, ProjectAsset.project_id)
    )
    for membership in session.exec(statement):
        index.setdefault(membership.asset_id, []).append(membership.project_id)
    return {asset_id: tuple(project_ids) for asset_id, project_ids in index.items()}


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
    return [
        RegisteredAsset(
            id=asset.id,
            path=asset.storage_path or "",
            size=int(asset.size or 0),
            mime=asset.mime or "",
            thumb_version=int(asset.thumb_version or 0),
            filename=asset.filename or "",
        )
        for asset in session.exec(statement)
    ]


def _own_paths(key: str, candidate: BundleCandidate) -> set:
    """The storage paths the candidate's own snapshot names, never a child's."""
    return {f"{key}/{entry.rel_path}" for entry in candidate.snapshot.entries}


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


def _stem_pair_verified(zip_path: Path, folder_entries: Sequence[bundle_detect.BundleEntry]) -> bool:
    """Whether an archive verifies as the twin of the directory beside it.

    This is the route for a stem twin whose directory no rule offers on its own:
    the operator's names agree and the payload has to agree too, or the archive is
    not that directory's provenance. The archive's own entries decide the wrapper -
    identity first, else ONE ``common_entry_root`` strip, never per entry and never
    two strips - through ``bundle_detect.archive_payload_contained``, the same
    predicate the entry-root route uses. Declared size is header data, so this is
    pairing evidence and never an integrity claim. Any read failure is ``False`` -
    never an exception on a read route.
    """
    try:
        entries = zip_service.list_zip_entries(Path(zip_path))
    except (BadZipFile, OSError):
        return False
    return bundle_detect.archive_payload_contained(
        ((str(entry["path"]), int(entry["size"])) for entry in entries),
        folder_entries,
    )


def entry_root_verified(
    zip_path: Path,
    entry_root: str,
    folder_entries: Sequence[bundle_detect.BundleEntry],
) -> bool:
    """Whether every entry of the archive sits inside ``entry_root`` and on disk.

    Strict strip-and-compare on this route: the root was named before the check, so
    the caller already decided which wrapper to strip. Containment, not equality,
    because a lived-in directory legitimately holds more than the archive that
    shipped it. Declared size comes from the archive's header, so this is pairing
    evidence and never an integrity claim: it decides which directory an archive
    belongs to, and a lie in the header only makes a pairing wrong, never a bundle
    corrupt. Any read failure is ``False`` - never an exception on a read route.

    The ``__MACOSX`` skip, the first-segment split and the refusal of an archive of
    cruft alone all live in ``bundle_detect.archive_payload_contained``, which both
    pairing routes call, so the two routes cannot disagree about what an archive's
    payload is.
    """
    try:
        entries = zip_service.list_zip_entries(Path(zip_path))
    except (BadZipFile, OSError):
        return False
    return bundle_detect.archive_payload_contained(
        ((str(entry["path"]), int(entry["size"])) for entry in entries),
        folder_entries,
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
    live = {candidate.key for candidate in iter_bundle_candidates(STORAGE)[0]}
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


class ExistingProject(NamedTuple):
    """The project a bundle already became, and which identity proved it.

    The identity is reported because the four are not equally certain: a
    ``_bundle`` marker is proof the app made this project from this bundle, a
    source URL is strong evidence of the same origin, a name match is a guess,
    and asset containment is strong evidence about the FILES that says nothing
    about intent - a human may have made that project for their own reasons.
    """

    project: Project
    match: str


def _asset_owner(
    index: Mapping[str, Tuple[str, ...]],
    asset_ids: Sequence[str],
    projects: Mapping[str, Project],
) -> Optional[Project]:
    """The one project that holds every wanted asset, or None.

    Containment, not intersection: an asset id with no membership contributes
    nothing, so a project holding only SOME of the files can never reach
    ``len(asset_ids)`` and a large project that happens to hold one file cannot
    claim a bundle it does not represent. An empty wanted set owns nothing. A
    membership row whose project row is missing is ignored, so a dangling row
    can never claim a bundle.

    Ties break on ``(lower(project.name), project.id)``: row order must never
    decide which project owns a bundle.
    """
    wanted = tuple(dict.fromkeys(asset_ids))
    if not wanted:
        return None
    counts: Dict[str, int] = {}
    for asset_id in wanted:
        for project_id in index.get(asset_id, ()):
            counts[project_id] = counts.get(project_id, 0) + 1
    owners = [
        projects[project_id]
        for project_id, count in counts.items()
        if count == len(wanted) and project_id in projects
    ]
    if not owners:
        return None
    return min(owners, key=lambda project: (project.name.lower(), project.id))


def find_existing_project(
    session: Session,
    *,
    key: str,
    source_url: Optional[str],
    name: str,
    assets: Sequence[str] = (),
    asset_index: Optional[Mapping[str, Tuple[str, ...]]] = None,
) -> Optional[ExistingProject]:
    """The project this bundle already became, in the order that cannot lie.

    Four identities, strongest first. The marker key is the identity a previous
    create wrote; the source URL covers a project imported by hand from the same
    page; an exact casefolded name covers a project made before the marker
    existed; asset containment covers a project a human made by hand over the
    bundle's own files. Any hit means ``already_created`` and the row is never
    touched again.

    The marker returns from inside the loop on purpose: it is proof, so it is
    also the cheapest answer - a marker match never reads the membership index.
    """
    wanted_name = (name or "").strip().casefold()
    projects_by_id: Dict[str, Project] = {}
    by_name: Optional[Project] = None
    for project in session.exec(select(Project).order_by(func.lower(Project.name), Project.id)):
        projects_by_id[project.id] = project
        if _bundle_marker(project.metadata_json).get("key") == key:
            return ExistingProject(project, MATCH_MARKER)
        if by_name is None and wanted_name and project.name.strip().casefold() == wanted_name:
            by_name = project
    existing = project_service.find_project_by_source(session, source_url)
    if existing is not None:
        return ExistingProject(existing, MATCH_SOURCE_URL)
    if by_name is not None:
        return ExistingProject(by_name, MATCH_NAME)
    wanted = tuple(dict.fromkeys(asset for asset in assets if asset))
    if not wanted:
        # An empty containment set can never claim a bundle.
        return None
    # Lazy: a marker, source or name hit never pays for the index, and a caller
    # that did not prebuild one still gets the asset identity.
    index = asset_index if asset_index is not None else project_assets_index(session)
    owner = _asset_owner(index, wanted, projects_by_id)
    return ExistingProject(owner, MATCH_ASSETS) if owner is not None else None


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
    if candidate.title_name:
        return bundle_detect.title_name_facts(candidate.snapshot)
    if candidate.pair_only or candidate.archive_owned:
        return bundle_detect.archive_owned_facts(candidate.snapshot)
    if candidate.is_archive:
        return _detect_archive(Path(root) / candidate.snapshot.rel_dir, candidate.snapshot)
    return _detect_folder(Path(root), candidate.snapshot)


def _cover(assets: Sequence[RegisteredAsset], facts: bundle_detect.BundleFacts) -> dict:
    """The cover a created project would get, and the reason it got it.

    The chain is ordered by source, never by size: a registered preview image
    beats a referenced-but-unregistered one, which beats a model the client can
    render, which beats no cover at all. An unregistered image outranks a
    renderable model on purpose - a picture the vendor shipped is the bundle's
    own face, while a rendered model is an inference. Size only breaks ties
    inside one source.

    ``url``/``thumb_url`` are set exactly when ``asset_id`` is: the pair is the
    served paths for those bytes, so a row with no asset has nothing to serve.
    """
    images = sorted(
        (asset for asset in assets if _suffix(asset.path) in bundle_detect.IMAGE_SUFFIXES),
        key=lambda asset: (-asset.size, asset.path),
    )
    if images:
        return _cover_of(images[0], COVER_LARGEST_IMAGE)
    if facts.cover_entry:
        return _cover_none_fields(facts.cover_entry, COVER_LARGEST_IMAGE_UNREGISTERED)
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


def _cover_none_fields(rel_path: Optional[str], reason: str) -> dict:
    """A cover with no registered asset: a named-but-unserved path, or nothing.

    ``url``/``thumb_url`` are None because there is no asset id to serve: the
    invariant is that the pair is set exactly when ``asset_id`` is.
    """
    return {"asset_id": None, "rel_path": rel_path, "reason": reason, "url": None, "thumb_url": None}


def _report_row(
    session: Session,
    *,
    root: Path,
    scope: str,
    candidate: BundleCandidate,
    folders: Mapping[str, str],
    asset_index: Mapping[str, Tuple[str, ...]],
    dismissals: Mapping[str, dict],
    enrichments: Mapping[str, dict],
) -> Optional[dict]:
    """One wire row for one candidate, or ``None`` when its own detect refuses it.

    The row is clock-free: a cached page's facts fill an empty creator or licence
    whether or not the record is still fresh, because TTL gates only the enrich
    action, never what the report shows. That is what keeps two reports of the
    same disk identical from one minute to the next, and it means a record that
    supplies nothing THIS row needed contributes no ``enrichment`` block at all.
    """
    facts = _candidate_facts(root, candidate)
    if facts is None:
        return None
    key = _qualify(scope, candidate.key)
    # The disk row is the truth and the page may only fill what it left blank, so
    # the page can never overwrite a creator a human or the vendor already named.
    cache_key = bundle_enrich.cache_key(facts.source_url)
    record = enrichments.get(cache_key) if cache_key is not None else None
    values, fields = bundle_enrich.fills(facts.creator, facts.licence, facts.licence_url, record)
    enrichment = bundle_enrich.provenance(record, fields) if fields else None
    suggestion = (facts.title or "").strip() or "bundle"
    assets: List[RegisteredAsset] = []
    own_assets: List[RegisteredAsset] = []
    # The asset ids this row would attach: a row that offers nothing to create
    # has nothing assets can claim, so its containment set stays empty.
    containment: List[str] = []
    if candidate.offers_archive_only:
        # Nothing to create, so nothing is looked up: a pair-only directory does
        # not even report the assets a scan may already hold under its key.
        status, blocked_reason = BLOCKED, ARCHIVE_ONLY
    else:
        assets = registered_assets(session, key)
        # Only the candidate's OWN entries decide status and cover: a registered
        # asset below a nested child bundle belongs to that bundle, not to this one.
        own = _own_paths(key, candidate)
        own_assets = [asset for asset in assets if asset.path in own]
        models = [asset for asset in own_assets if _suffix(asset.path) in bundle_detect.MODEL_SUFFIXES]
        # Deliberate reading of "every registered asset": the app only ever
        # attaches MODELS to a project (``_create_one``'s ``model_assets``), so
        # literal full-set containment over images and text too would report the
        # owner's own hand-made project as NOT owning the bundle. The models are
        # the containment set.
        containment = [asset.id for asset in models]
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
            "twin_verified": _archive_twin_verified(root, candidate.archive_path, candidate),
        }
    existing = find_existing_project(
        session,
        key=key,
        source_url=facts.source_url,
        name=suggestion,
        assets=containment,
        asset_index=asset_index,
    )
    # One row read, two derived fields: the id is the identity, the name is what
    # a human can read. A blank/whitespace name is reported as None so the wire
    # never implies a name it does not have.
    existing_project_id = existing.project.id if existing else None
    existing_project_name = existing.project.name if existing else None
    if existing_project_name is not None and not existing_project_name.strip():
        existing_project_name = None
    return {
        "key": key,
        "rel_path": key,
        "title": facts.title,
        "name_suggestion": suggestion,
        "creator": values["creator"],
        "licence": values["licence"],
        "licence_url": values["licence_url"],
        "enrichment": enrichment,
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
        "existing_project_id": existing_project_id,
        "existing_project_name": existing_project_name,
        "existing_project_match": existing.match if existing else None,
        "dismissed": key in dismissals,
    }


def _archive_twin_verified(root: Path, archive_path: str, candidate: BundleCandidate) -> bool:
    """``twin_verified`` for the archive this candidate attaches.

    Which answer is honest depends on who paired the two. A directory the entry-root
    route OR the stem route promoted, and a pair-only directory, was verified by
    containment at scan time, so its archive is **re-verified at report time**: a
    ``False`` there means the bytes on disk changed since the scan, never that the
    pairing was never checked. A stem twin of a directory some rule offers keeps the
    names-only answer, because there the operator paired them and a mismatch is
    information for the operator rather than an assertion to raise.
    """
    if candidate.entry_root:
        return entry_root_verified(root / archive_path, candidate.entry_root, candidate.snapshot.entries)
    if candidate.archive_owned or candidate.pair_only:
        return _stem_pair_verified(root / archive_path, candidate.snapshot.entries)
    return twin_verified(root / archive_path, candidate.snapshot.entries)


def build_report(*, path: str = "", limit: int = DEFAULT_LIMIT, include_dismissed: bool = False) -> dict:
    """The whole detection report, read from disk now. Writes nothing.

    ``path`` scopes the walk (``""`` is the whole storage root) and only changes
    what is scanned: every key stays storage-root-relative, so a scoped read and
    a whole-storage read name the same bundle, and a create pass can find a row
    a scoped report showed. Duplicate archives are reported scoped the same way
    but are never capped by ``limit``: the walk is already bounded by
    ``MAX_SCAN_ENTRIES``, and a reclaimable-bytes total that stopped at a page
    boundary would be a number the operator could not act on.
    """
    scope = _scope(path)
    root = STORAGE / scope if scope else STORAGE
    if not _is_inside_storage(root):
        raise HTTPException(status_code=400, detail="path must stay inside the storage root")
    return _report_from_scan(
        scan_bundles(root), root=root, scope=scope, limit=limit, include_dismissed=include_dismissed
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
    """
    dismissals = get_dismissals()
    if enrichments is None:
        enrichments = get_enrichments()
    # One rule set for the whole report, so every row's stamp and the report's own
    # are the same value even if a table is edited mid-run.
    rule_set = bundle_detect.rule_set()
    rows: List[dict] = []
    with Session(engine) as session:
        folders = _folder_index(session)
        # One query for ALL candidates: containment is a dict lookup per row.
        asset_index = project_assets_index(session)
        for candidate in scan.candidates:
            row = _report_row(
                session,
                root=root,
                scope=scope,
                candidate=candidate,
                folders=folders,
                asset_index=asset_index,
                dismissals=dismissals,
                enrichments=enrichments,
            )
            if row is not None:
                row["rule_set"] = rule_set["stamp"]
                rows.append(row)

    visible = rows if include_dismissed else [row for row in rows if not row["dismissed"]]
    counts = {
        "total": len(visible),
        "high": sum(1 for row in visible if row["confidence"] == bundle_detect.CONFIDENCE_HIGH),
        "medium": sum(1 for row in visible if row["confidence"] == bundle_detect.CONFIDENCE_MEDIUM),
        "low": sum(1 for row in visible if row["confidence"] == bundle_detect.CONFIDENCE_LOW),
        "ready": sum(1 for row in visible if row["status"] == READY),
        "blocked": sum(1 for row in visible if row["status"] == BLOCKED),
        "dismissed": sum(1 for row in rows if row["dismissed"]),
        "already_created": sum(1 for row in visible if row["existing_project_id"]),
    }
    bound = max(0, int(limit))
    live_keys = {_qualify(scope, candidate.key) for candidate in scan.candidates}
    scoped_duplicates = tuple(
        bundle_detect.DuplicateGroup(
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
        "duplicates_text": bundle_detect.duplicate_report_text(scoped_duplicates),
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
    *,
    asset_index: Optional[Mapping[str, Tuple[str, ...]]] = None,
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
    # The project records what the page supplied, so a later reader can tell the
    # vendor's word from a human's; `name_only` deliberately records none.
    if not item.name_only and row.get("enrichment"):
        marker["_bundle"]["enrichment"] = row["enrichment"]
    with Session(engine) as session:
        # The same set the report used: the files this bundle's snapshot names,
        # never a nested child bundle's models under the recursive prefix. Hoisted
        # so the existing-project check and the create-time belt read one query.
        assets = registered_assets(session, key)
        own = _own_paths(key, candidate)
        own_assets = [asset for asset in assets if asset.path in own]
        model_assets = [asset.id for asset in own_assets if _suffix(asset.path) in bundle_detect.MODEL_SUFFIXES]
        existing = find_existing_project(
            session,
            key=key,
            source_url=enrichment["source_url"],
            name=name,
            assets=model_assets,
            asset_index=asset_index,
        )
        if existing is not None:
            return _result(
                key,
                CREATE_SKIPPED,
                project_id=existing.project.id,
                name=existing.project.name,
                reason="already_created",
            )
        # The create-time belt is the same rule: every inferred confidence is
        # refused when its assets include another bundle's, not just `low`. The
        # difference is another bundle's assets, and the refusal comes before
        # the project row, so it can never leave a half-created project.
        if row["confidence"] in bundle_detect.INFERRED_CONFIDENCES and len(assets) > len(own_assets):
            return _result(key, CREATE_FAILED, reason=CONTAINS_BUNDLES)
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
    ``blocked`` since the report was rendered creates nothing. One walk serves
    both the report and the candidate lookup, so a duplicate archive is refused
    as ``unknown bundle`` by construction: it is suppressed from both.
    """
    scan = scan_bundles(STORAGE)
    report = _report_from_scan(scan, root=STORAGE, scope="", limit=MAX_SCAN_ENTRIES, include_dismissed=True)
    rows = {row["key"]: row for row in report["bundles"]}
    by_key = {candidate.key: candidate for candidate in scan.candidates}
    # One query for the whole create pass, so a create is never O(items) queries.
    with Session(engine) as session:
        asset_index = project_assets_index(session)
    counters = {CREATE_CREATED: 0, CREATE_SKIPPED: 0, CREATE_BLOCKED: 0, CREATE_FAILED: 0}
    results: List[dict] = []
    for item in items:
        key = (item.key or "").strip()
        result = _create_one(item, rows.get(key), by_key.get(key), ctx, asset_index=asset_index)
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
        thing_id = bundle_detect.thingiverse_thing_id_from_url(row["source_url"] or "")
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
        # Function-local on purpose: the one name that can open a socket stays out
        # of this module's scope so the report path cannot reach it (see the AST
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
