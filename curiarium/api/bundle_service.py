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
assertion to raise.

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
evidence, never an integrity claim - and on this path ``twin_verified`` is a
pairing condition (always true by construction; an unverified archive simply
does not pair), where the stem path's ``twin_verified`` stays an operator remark.
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

**A verified pairing makes a no-text directory a low bundle.** The Clockwork
shape - models and images, no text - is not an R3 bundle, so a rule that only
read the listing would render nothing for it. When the entry-root pass has
proved that an archive beside a directory owns that directory, the archive
supplies what the listing cannot: its identity. The directory is therefore
offered as a real ``generic-bundle`` / ``low`` candidate keyed at the
DIRECTORY, with that archive reported as its ``archive`` and no file attached
from it - the archive is provenance, the low confidence says the shape was
inferred. It is then creatable exactly like any other low candidate: a resolved
``Folder`` and registered model assets, nothing about the detector.

Be honest about the two bounds. **Without a pairing the shape is silent**: model
files and a render are too weak on their own, so nothing is offered and nothing
can be created. And **the pairing never overrides the container rule**: a
directory that holds bundles keeps the blocked ``archive_only`` row and creates
nothing, so a pile of models with one zip somewhere inside it is still a pile;
and the entry root has to name the directory itself, so a pairing can never
qualify a descendant. A refused pair stays visible rather than vanishing, which
is why it keeps the archive-only row.

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

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import AbstractSet, Dict, List, Mapping, Optional, Sequence, Tuple
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
# Duplicate detection reads an archive in bounded chunks, never one whole file
# into memory: a multi-gigabyte zip is hashed at this cost, not doubled.
HASH_CHUNK_BYTES = 1024 * 1024

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

# A create-time refusal: the candidate's assets include another bundle's. This is
# a reason on CREATE_FAILED, never a blocked_reason - `blocked` means database
# readiness only (Mf-um7x).
CONTAINS_BUNDLES = "contains_bundles"


@dataclass(frozen=True)
class BundleCandidate:
    """One directory or zip that looks like a bundle, before any database read.

    ``confidence`` is internal: it decides whether a candidate may claim a
    subtree. It is never returned in a report row - the row carries the facts
    from its own detect call. ``entry_root`` is the archive entry root that
    paired this bundle's archive, ``None`` when the stem rule paired it.
    ``pair_only`` means a paired directory the container rule refuses: it is
    shown, never creatable. ``archive_owned`` means a verified entry-root pairing
    owns this directory: the archive supplies its identity and the low confidence
    says the shape was inferred, so it is offered at LOW even when no rule finds
    text and it is creatable like any other LOW candidate.
    """

    key: str
    snapshot: bundle_detect.BundleSnapshot
    archive_path: Optional[str]
    is_archive: bool
    confidence: str
    entry_root: Optional[str] = None
    pair_only: bool = False
    archive_owned: bool = False

    @property
    def offers_archive_only(self) -> bool:
        """Whether the row is reported ``blocked``/``archive_only``.

        An archive of its own, and a paired directory the container rule refuses,
        are the same answer to the operator: there is nothing here to create, and
        the remedy is the import door.
        """
        return self.is_archive or self.pair_only


@dataclass(frozen=True)
class BundleScan:
    """One walk of the storage tree: the bundle candidates plus duplicate archives."""

    candidates: List[BundleCandidate]
    truncated: bool
    duplicates: Tuple[bundle_detect.DuplicateGroup, ...] = ()


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
) -> List[BundleCandidate]:
    """Every bundle under ``root`` from one walk's listings.

    ``suppressed`` holds the duplicate archives: a byte-identical copy is not a
    bundle of its own, so both archive passes skip it and only the group's
    authoritative path can be a row (or a pairing).
    """
    folded = _subtree(files_by_dir)
    found: List[BundleCandidate] = []
    # The strict ancestors of every bundle already claimed. A LOW candidate in
    # this set contains a bundle and is therefore a container, never a bundle -
    # a guess must never swallow a subtree. It is skipped *without* claiming, so
    # its own files still bubble up to a legitimate ancestor.
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
            continue
        snapshot = _folder_snapshot(rel_dir, entries)
        facts = _detect_folder(root, snapshot)
        if facts is None:
            continue
        if facts.confidence == bundle_detect.CONFIDENCE_LOW and rel_dir in claimed_ancestors:
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

    # Entry-root pairing: the stem rule has first refusal, so this pass only sees
    # the archives it left behind. Each archive's common entry root must name a
    # sibling DIRECTORY (same parent only) and its entries must contain nothing
    # the directory's post-claim listing does not hold. A duplicate archive is
    # skipped: the content hash already says which of the pair is the original.
    pairs: Dict[str, List[Tuple[str, str]]] = {}
    paired_zips: set = set()
    for rel_path in sorted(_zip_files(files_by_dir), key=lambda item: (item.casefold(), item)):
        if rel_path in suppressed:
            continue
        if _twin_directory(rel_path, dirs_by_parent):
            continue
        paired = _entry_root_pair(root, rel_path, dirs_by_parent, work)
        if paired is None:
            continue
        folder, entry_root = paired
        pairs.setdefault(folder, []).append((rel_path, entry_root))
        paired_zips.add(rel_path)

    # Zip-only candidates: an archive whose extracted twin is not beside it. A
    # duplicate is skipped here too, so a copy never becomes a second bundle row.
    for rel_path in sorted(_zip_files(files_by_dir), key=lambda item: (item.casefold(), item)):
        if rel_path in suppressed:
            continue
        if rel_path in paired_zips:
            continue
        twin = _twin_directory(rel_path, dirs_by_parent)
        if twin:
            continue
        snapshot = _zip_snapshot(root / rel_path, rel_path)
        if snapshot is None:
            continue
        facts = _detect_archive(root / rel_path, snapshot)
        if facts is None:
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

    # A directory a verified archive owns and no rule offers on its own: models
    # and images, no text. The archive supplies the identity, so this is a real
    # LOW candidate keyed at the DIRECTORY. The container rule applies to a
    # pairing exactly as it does to any other guess - a pairing that names a
    # directory holding a claimed bundle is a pile and proves nothing - and a
    # refused pairing keeps the blocked row instead, so the pair stays visible and
    # creates nothing.
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
        if any(candidate.key == folder for candidate in found):
            continue
        entries = work.get(folder) or []
        if not entries:
            continue
        snapshot = _folder_snapshot(folder, entries)
        facts = bundle_detect.archive_owned_facts(snapshot)
        if facts is None:
            continue
        if folder in claimed_ancestors:
            pair_only_specs[folder] = (snapshot, facts)
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

    # An archive-only bundle is still a bundle: a low folder that strictly
    # contains one is a container too, and the folder pass cannot see it because
    # the archive is read after the directory walk. `contained_keys` is the keys of
    # both kinds of archive-only row - an archive of its own, and a paired
    # directory the container rule refuses - plus every directory a verified
    # pairing promoted, because a promoted candidate is a bundle in its own right.
    # A LOW folder strictly containing any of them holds nothing but another
    # thing's payload and is therefore a container, never a bundle.
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
        found = [
            candidate
            for candidate in found
            if not (
                not candidate.is_archive
                and not candidate.pair_only
                and not candidate.archive_owned
                and candidate.confidence == bundle_detect.CONFIDENCE_LOW
                and any(key.startswith(candidate.key + "/") for key in contained_keys)
            )
        ]

    # A zip inside a detected bundle is a supporting file of that bundle, never a
    # candidate and never a pairing (Mf-74x5). The bundle directories are the
    # folder candidates that survived the strip plus the pair-only directories
    # above; membership is strict, so a twin beside a directory is not a member.
    bundle_dirs = {candidate.key for candidate in found if not candidate.is_archive}
    bundle_dirs.update(pair_only_specs)
    nested = _nested_archives(files_by_dir, bundle_dirs)
    if nested:
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
        archive = _twin_archive(candidate.key, files_by_dir)
        entry_root = None
        if archive is None and candidate.key in pairs:
            archive, entry_root = _paired_archive(pairs[candidate.key])
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
            )
        )
    with_twins.sort(key=lambda candidate: (candidate.key.casefold(), candidate.key))
    return with_twins


def scan_bundles(root: Path) -> BundleScan:
    """One walk of the storage tree: bundle candidates plus duplicate archives.

    Detection is one deepest-first pass. ``bundle_detect.detect_bundle`` applies R1 ->
    R2 -> R3 strongest-first within a single directory, so the only ordering a
    tree-wide walk needs is children before parents: a child bundle is claimed
    before its parent is evaluated, and claiming it removes it from the parent's
    view. A directory can therefore only ever be a bundle in its own right - it
    never inherits a child's models, and a directory that holds nothing but child
    bundles is not a bundle at all. A LOW candidate whose key is a strict
    ancestor of an already-claimed bundle is a *container* and is skipped without
    claiming, so its own files still bubble up to a legitimate ancestor; a HIGH
    candidate is exempt because its documents sit at depth 0 and it attaches only
    its own models.

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
    candidates = _build_candidates(root, files_by_dir, dirs_by_parent, suppressed)
    bundle_dirs = {candidate.key for candidate in candidates if not candidate.is_archive}
    nested = _nested_archives(files_by_dir, bundle_dirs)
    duplicates = _without_nested_members(duplicates, nested)
    return BundleScan(candidates=candidates, truncated=truncated, duplicates=duplicates)


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


def _paired_archive(pairs: Sequence[Tuple[str, str]]) -> Tuple[str, str]:
    """``(zip_rel, entry_root)`` to report for one directory.

    The shortest name wins, ties by (casefold, raw): a browser's `` (1)``
    duplicate convention only ever appends characters, so the shortest name is the
    original file. Hash identity for duplicates lives in ``duplicate_archives``;
    this pass still picks the shortest name, because the copy is suppressed before
    pairing and the choice only has to be deterministic.
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


def entry_root_verified(
    zip_path: Path,
    entry_root: str,
    folder_entries: Sequence[bundle_detect.BundleEntry],
) -> bool:
    """Whether every entry of the archive sits inside ``entry_root`` and on disk.

    Containment, not equality, because a lived-in directory legitimately holds more
    than the archive that shipped it (extracted, then a second zip dropped in, then
    a render saved by hand), and equality would refuse the very pair this exists
    for. Declared size comes from the archive's header, so this is pairing evidence
    and never an integrity claim: it decides which directory an archive belongs to,
    and a lie in the header only makes a pairing wrong, never a bundle corrupt.

    The first segment is split off the path rather than sliced by the length of a
    casefolded prefix: casefolding may change a string's length, and a slice would
    then cut the tail in the wrong place.

    An ``__MACOSX/`` entry is AppleDouble cruft, not payload: it is skipped before
    the head check and never counted in ``seen``, so an archive carrying such
    cruft still pairs on its real entries, while an archive of cruft alone has
    verified no entry and is refused. The skip is ``is_macosx_artifact``, the same
    predicate ``common_entry_root`` applies, so the two layers agree on what the
    archive's root is.
    """
    try:
        entries = zip_service.list_zip_entries(Path(zip_path))
    except (BadZipFile, OSError):
        return False
    wanted = (entry_root or "").casefold()
    folder = {(entry.rel_path.casefold(), entry.size) for entry in folder_entries}
    seen = 0
    for entry in entries:
        rel_path = str(entry["path"])
        if bundle_detect.is_macosx_artifact(rel_path):
            continue
        head, separator, rest = rel_path.partition("/")
        if not separator or head.casefold() != wanted:
            return False
        seen += 1
        if (rest.casefold(), int(entry["size"])) not in folder:
            return False
    return seen > 0


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
    if candidate.pair_only or candidate.archive_owned:
        return bundle_detect.archive_owned_facts(candidate.snapshot)
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
    if candidate.offers_archive_only:
        # Nothing to create, so nothing is looked up: a pair-only directory does
        # not even report the assets a scan may already hold under its key.
        status, blocked_reason = BLOCKED, ARCHIVE_ONLY
    else:
        assets = registered_assets(session, key)
        # Only the candidate's OWN entries decide status and cover: a registered
        # asset below a nested child bundle belongs to that bundle, not to this one.
        own = _own_paths(key, candidate)
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
            "twin_verified": _archive_twin_verified(root, candidate.archive_path, candidate),
        }
    existing = find_existing_project(session, key=key, source_url=facts.source_url, name=suggestion)
    # One row read, two derived fields: the id is the identity, the name is what
    # a human can read. A blank/whitespace name is reported as None so the wire
    # never implies a name it does not have.
    existing_project_id = existing.id if existing else None
    existing_project_name = existing.name if existing else None
    if existing_project_name is not None and not existing_project_name.strip():
        existing_project_name = None
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
        "existing_project_id": existing_project_id,
        "existing_project_name": existing_project_name,
        "dismissed": key in dismissals,
    }


def _archive_twin_verified(root: Path, archive_path: str, candidate: BundleCandidate) -> bool:
    """``twin_verified`` for the archive this candidate attaches.

    Two pairing paths, two meanings. The stem path compares basenames and reports a
    mismatch, because the operator paired them and the answer is information. The
    entry-root path already required containment to pair at all, so its answer is a
    pairing condition and is always true here - a ``False`` would mean a pairing
    that should not exist.
    """
    if candidate.entry_root:
        return entry_root_verified(root / archive_path, candidate.entry_root, candidate.snapshot.entries)
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
) -> dict:
    """Turn one walk into the wire report; the disk walk has already happened.

    Duplicate archives are scoped like every other key and rendered as one group
    per content hash plus a copyable text block naming the same paths. They are
    not capped by ``limit``: the walk is already bounded by ``MAX_SCAN_ENTRIES``.
    """
    dismissals = get_dismissals()
    rows: List[dict] = []
    with Session(engine) as session:
        folders = _folder_index(session)
        for candidate in scan.candidates:
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
        if row["confidence"] == bundle_detect.CONFIDENCE_LOW:
            assets = registered_assets(session, key)
            own_assets = [asset for asset in assets if asset[1] in _own_paths(key, candidate)]
            if len(assets) > len(own_assets):
                # The difference is another bundle's assets: refuse before the
                # project row, so a refusal can never leave a half-created project.
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

    with Session(engine) as session:
        # The same set the report used: the files this bundle's snapshot names,
        # never a nested child bundle's models under the recursive prefix.
        own = _own_paths(key, candidate)
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
    ``blocked`` since the report was rendered creates nothing. One walk serves
    both the report and the candidate lookup, so a duplicate archive is refused
    as ``unknown bundle`` by construction: it is suppressed from both.
    """
    scan = scan_bundles(STORAGE)
    report = _report_from_scan(scan, root=STORAGE, scope="", limit=MAX_SCAN_ENTRIES, include_dismissed=True)
    rows = {row["key"]: row for row in report["bundles"]}
    by_key = {candidate.key: candidate for candidate in scan.candidates}
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
