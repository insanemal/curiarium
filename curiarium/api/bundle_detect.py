"""Decide whether a directory (or an archive) is one vendor bundle, and how sure.

A *bundle* is one thing a vendor shipped as a whole: the model files, its preview
images, and the README/LICENSE that say who made it. This module turns a listing
plus the handful of text documents the caller opened into a ``BundleFacts`` row,
or into ``None`` when the listing is not a bundle at all. It is pure on purpose -
no ``Session``, no ``engine``, no ``open()``, no ``Path.read_*``, no
``os.scandir`` - so every rule below can be tested from literals, and the one
disk+DB layer (``bundle_service``) owns every byte this module is handed.

**The stamp and the refused ledger (Mf-9ygi).** Every report row carries the
``rule_set`` stamp of the tables that decided it, and every path the scan did NOT
offer carries the rule that refused it, so two runs are comparable by construction
or explicitly not. ``rule_set_tables`` captures every row-deciding constant BY
VALUE, ``rule_set_stamp`` hashes it, and ``tests/test_bundle_detect.py`` pins the
hash: an edit to a table cannot go unnoticed. The refusal vocabulary
(``REFUSAL_*``) is deliberately NOT part of that stamp - it names an absence, it
does not decide a row.

The rules, strongest first:

* R1 ``thingiverse-readme`` / ``high``: the bundle root holds ``README.txt``
  and/or ``LICENSE.txt`` - at least one - and the pair carries both of
  Thingiverse's markers, in whichever document they shipped - one says ``created
  by Thingiverse user <name>`` and ``licensed under <token>``, the other says
  ``<title> by <creator> on Thingiverse: <url>``. Which file holds which is
  never assumed: real exports swap them. Title, creator, licence and the source
  URL come from whichever document carried them; the description is the
  document that carried the created-by sentence, whitespace-collapsed.
* R2 ``thingiverse-sibling-docs`` / ``high``: the same pair shipped beside the
  files as ``<bundle>_README`` / ``<bundle>_LICENSE``, either as two files or as
  two directories each holding one text file (the legacy flattening). Same
  enrichment as R1.
* R4 ``thingiverse-id-name`` / ``medium``: nothing parses, but the directory
  name starts with a Thingiverse id (``5390184_Title``) and the tree has at
  least one model at the bundle's own depth. The id buys the source URL and the
  thing id - provenance inferred from a name - and nothing else: creator,
  licence and licence URL stay ``None``. R4 outranks R3 because an inferred id
  is strictly more than a shape guess; it still requires a model at the own
  depth, so photos or a pile of subdirectories is not enough.
* R3 ``generic-bundle`` / ``low``: nothing parses, but the tree still looks like
  one thing - at least one model at the bundle's own depth, one image and one
  text file. The model evidence may be at depth 0, at depth 1 under the named
  ``R3_MODEL_DIRS`` (``files/``), or at depth 1 under exactly ONE model-bearing
  subdirectory of any name (the "one folder in, models out" shape). Two or more
  unnamed model-bearing subdirectories, with no model at the root and none under
  the named directory, is a pile of things rather than a bundle. Name only:
  every provenance field is ``None``, because a guess is worse than a blank. The
  depth bound is what stops an ancestor qualifying on its descendants' models;
  images and text stay unbounded because neither can supply the trio alone.
* R5 ``title-name`` / ``low``: the folder's NAME reads like a title a person
  chose - a phrase with a separator, or one capitalised word longer than the
  floor - and the tree holds at least one model at the folder's own depth. The
  name is the evidence, so creator, licence and URL stay ``None`` exactly as
  under R3. It is offered by ``bundle_service`` as a post-pass AFTER the archive
  promotion, never by ``detect_bundle``: the folder pass is deepest-first, so a
  rule inside it would claim a parent's row for its child and lose an
  archive-owned row.

Anything else is not a bundle and ``detect_bundle`` returns ``None``. A README
that exists but lacks the markers is not R1 - both high-confidence rules miss and
the bundle falls through to R3, or to ``None`` when the trio is absent. That
distinction is what makes "confidence high" mean "the vendor told us" rather than
"there is a .txt file".

Classification is deliberately SUFFIX ONLY (``classify_entry``). It never looks
at a stem: the Mf-tkdp identity bug derived display names from stems and silently
dropped 67 files whose stems collided across extensions, so keying a role off a
stem here would reintroduce exactly that class of bug. ``a/part.stl`` and
``b/part.stl`` are two distinct entries with two distinct roles because their
paths differ; a file's role is decided by its extension alone. The image
suffixes are not a second table - they are ``library_scan.IMAGE_SUFFIXES``, the
constant the scanner and the thumbnailer already agree on.

A nested archive is a supporting file, never a candidate and never evidence
(Mf-74x5). A ``.zip`` below a detected bundle directory belongs to that bundle:
it is counted as an archive and nothing else, so it can neither become a second
row nor supply a model, image or text that would make the enclosing directory a
bundle. Its central directory may still be listed to decide pairing and its
bytes hashed for duplicate identity, but nothing inside it is read as evidence
and nothing is ever extracted. ``is_bundle_member`` is the one predicate for
that membership.

Every heuristic in this module is proven by ``tests/test_bundle_detect.py``: the
suffix table, the shared image-suffix constant, both parser shapes and their
negative cases, the licence table, the display name, the twin key, the cover
pick, the id-in-a-name extraction, and the rule precedence including the
marker-less README falling to low confidence and R4's medium reading.

The module also owns the vocabulary for pairing an archive with a directory by
its *entry root* rather than its filename (``common_entry_root``,
``is_generic_container_root``) and the facts for a directory a verified archive
owns but which no rule offers on its own (``archive_owned_facts``). Those facts
are what ``bundle_service`` now offers as a LOW candidate: the archive supplies
the directory's identity and the confidence says the shape was inferred. The
archive is offered whatever else the directory holds - images and text are
decoration, so neither is required and neither disqualifies - and
own-depth/cardinality (``_own_depth_model_count``) is the only shape guard left.
Both pairing routes - the entry root and the stem - decide membership through the
ONE containment predicate ``archive_payload_contained``, so the two can never
disagree about what an archive's payload is. This module still only judges a
listing; the service decides creatability. It stays pure: the caller reads the
archive's entry list and this module only judges it.

Content identity is used ONLY to report duplicate archives and to suppress the
copy as a bundle row; it never enters ``twin_verified``, pairing or an integrity
claim. Identity is a content hash, and the grouping is size-first: an archive
whose size is unique in the library is never read, because a byte-identical copy
is necessarily equal-size. The worst case is a library where every archive is
the same size - then every archive is read exactly once. The report below only
renders text; it never deletes, renames or moves a file.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import AbstractSet, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from config import IMPORT_ALLOWED_EXTS
from import_resolvers import thingiverse_thing_id_from_url
from library_scan import IMAGE_SUFFIXES

# The model extensions the import door accepts, minus the archive: a `.zip` is a
# container, never a bundle member.
MODEL_SUFFIXES = frozenset(IMPORT_ALLOWED_EXTS - {".zip"})
# R3's model evidence is bounded to the bundle's own depth: a model at the bundle
# root, one level down under this NAMED model directory, or one level down under
# exactly one model-bearing subdirectory of any name. This constant is only the
# named allowance - the single unnamed subdirectory is allowed by cardinality,
# while two or more unnamed model-bearing subdirectories, with no model at the
# root and none under this name, is a pile of things rather than a bundle.
# Without the bound every ancestor of a bundle qualifies on its descendants'
# models.
R3_MODEL_DIRS = frozenset({"files"})
# Directory names that say nothing about what a bundle IS: they are the shape a
# download or a hand extraction lands in, not a name a vendor chose. The live
# library measured 51 of 57 archive entry roots pointing at one of these, so a
# root-matching rule without this guard would pair half the library to the wrong
# directory. An EXACT casefolded set, never a prefix or a fuzzy match: `my files/`
# and `files_and_docs/` are real bundle roots, and loose matching is exactly the
# mis-pairing the guard exists to stop.
GENERIC_CONTAINER_ROOTS = frozenset({"files", "images", "models", "stl"})
# The directory rsync and hand extraction leave behind: an AppleDouble artifact,
# never a vendor's choice. Named rather than grown into a junk list, because the
# next junk name would be a guess.
_MACOSX_ROOT = "__macosx"
# Prose that can carry provenance. Deliberately short: a `.pdf` is a document,
# not a sibling README.
BUNDLE_TEXT_EXTS = frozenset({".txt", ".md", ".pdf", ".html", ".htm", ".rtf"})
# One document is read bounded, both by the number of candidates and by bytes,
# so a vendor README cannot make a dry-run read a whole disk. The candidate cap
# is four because two readable pairs means four named documents
# (``README.txt``, ``LICENSE.txt``, ``<bundle>_README``, ``<bundle>_LICENSE``):
# at three the second sibling document is cut and the sibling merge can never
# complete.
BUNDLE_DOC_MAX_BYTES = 64 * 1024
MAX_DOC_CANDIDATES = 4

MODEL, IMAGE, TEXT, ARCHIVE, OTHER = "model", "image", "text", "archive", "other"

DETECTOR_THINGIVERSE_README = "thingiverse-readme"
DETECTOR_THINGIVERSE_SIBLING = "thingiverse-sibling-docs"
DETECTOR_THINGIVERSE_ID_NAME = "thingiverse-id-name"
DETECTOR_GENERIC = "generic-bundle"
# The rule-set version, bumped by hand whenever a row-deciding table changes.
# It is a human-readable companion to the derived stamp below: the version says
# *which* revision a report was taken at, the stamp proves *what* the tables were.
RULE_SET_VERSION = "2026-11-04.1"
CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

# The confidences that rest on inference rather than on a document the vendor
# wrote: a shape guess and a name guess. Every container guard asks whether a
# candidate's confidence is IN this set, so a future inferred confidence cannot
# escape a guard merely by not being `low`.
INFERRED_CONFIDENCES = frozenset({CONFIDENCE_MEDIUM, CONFIDENCE_LOW})
# The confidences that OUTRANK a shape inference. This is a rank, not a claim that
# these rules read evidence: `medium` rests on a name and `high` on a document pair,
# and both are stronger statements than a `low` guess made from the tree's shape
# alone. Used where a LOW, inference-based row must never swallow a candidate a
# stronger rule produced (ADR 0010 Decision 18, `Mf-v3i3`). Membership, never a
# literal comparison, so a future confidence cannot slip the guard.
STRONG_CONFIDENCES = frozenset({CONFIDENCE_HIGH, CONFIDENCE_MEDIUM})

SHAPE_ROOT_DOCS = "root-docs"
SHAPE_SIBLING_DOCS = "sibling-docs"
SHAPE_ID_NAME = "id-name"
SHAPE_GENERIC = "generic"
DETECTOR_TITLE_NAME = "title-name"
SHAPE_TITLE_NAME = "title-name"

# R5's exclusion list: names that say "a part of a thing", never "a thing". A
# hand extraction and a vendor both ship these as payload directories, so
# offering one would be the pile problem in a smaller shape. Matched
# case-folded on the WHOLE display segment, never a prefix and never fuzzily:
# `files` is a part name, `files 2` and `Fooparts` are not refused by this list.
TITLE_PART_NAMES = frozenset(
    {"files", "source", "src", "images", "img", "renders", "textures", "docs", "stl", "stls", "models", "meshes", "parts"}
)
# A single capitalised word is a title only if it is longer than this. Four is
# the measured floor: `Taffi` (5) is a real title in the library while `Beta`
# (4) is a pile member, so the bound is `len > 4` and not `>=`.
TITLE_SINGLE_WORD_FLOOR = 4
# The ONE switch for R5. Read only inside `title_name_facts`, so turning the
# rule off is one edit and every caller (scan, report, create) follows.
TITLE_NAME_BUNDLES_ENABLED = True

# Branch B's shape: one capitalised word. `[A-Z]` first is what refuses a
# lowercase single word; the lowercase clause in `is_title_name` is what
# refuses an ALL-CAPS label, because `STAGETOP` is a product code, not a title.
_TITLE_WORD = re.compile(r"[A-Z][A-Za-z0-9]*")

_ROOT_README = "readme.txt"
_ROOT_LICENSE = "license.txt"
_SIBLING_README = "_README"
_SIBLING_LICENSE = "_LICENSE"

# A Thingiverse export roots its own entries at `NNNNNNN_Title`, so a directory
# carrying that shape carries provenance. Four digits, because three-digit runs
# are common in ordinary names; a mandatory `_`, because `2024-10-31_backup` is
# a date and not an export; `^`-anchored, because `my_5390184_thing` is the
# operator's own naming and predates the export.
_THINGIVERSE_ID_IN_NAME = re.compile(r"^(\d{4,})_")
_THINGIVERSE_THING_URL = "https://www.thingiverse.com/thing:{}"

# One pass over a README, anchored on the two marker phrases Thingiverse writes.
# The alternation keeps a single scan and one decision per marker.
_README_MARKERS = re.compile(
    r"created\s+by\s+thingiverse\s+user\s+(?P<creator>[^,\n]+)"
    r"|licensed\s+under\s+(?P<licence>[^,\n;.]+)",
    re.IGNORECASE,
)
# The license line: "<title> by <creator> on Thingiverse: <url>".
# Deliberately unanchored and not MULTILINE: a marker may sit mid-line after a
# sentence of its own, and the title must not cross a newline. The title group is
# greedy so the LAST " by " before " on Thingiverse:" wins - a title that itself
# contains " by " keeps it, and the creator stays the short trailing token.
_LICENSE_LINE = re.compile(
    r"(?P<title>.+) by (?P<creator>.+?) on Thingiverse:\s*(?P<url>https?://\S+)",
    re.IGNORECASE,
)
# A sentence boundary needs the whitespace: a bare dot belongs to art, so a title
# like ``v1.2`` survives where ``Notes. 2-color cat`` is trimmed to the tail.
_SENTENCE_BOUNDARY = re.compile(r"[.!?]\s+")
_LEADING_ORDINAL = re.compile(r"^\d+[_-]+")
_TRAILING_JUNK = ",;."

_CREATIVE_COMMONS = "https://creativecommons.org/licenses/"
_CC0_URL = "https://creativecommons.org/publicdomain/zero/1.0/"

# The explicit table: a licence token only maps to a URL we know by heart. An
# unknown token keeps its own text and gets no URL - a guessed licence link is a
# legal statement we are not entitled to make.
_LICENCE_TABLE: dict = {
    "cc_by": ("CC BY", f"{_CREATIVE_COMMONS}by/4.0/"),
    "cc_by_sa": ("CC BY-SA", f"{_CREATIVE_COMMONS}by-sa/4.0/"),
    "cc_by_nc": ("CC BY-NC", f"{_CREATIVE_COMMONS}by-nc/4.0/"),
    "cc_nc": ("CC BY-NC", f"{_CREATIVE_COMMONS}by-nc/4.0/"),
    "cc_by_nc_sa": ("CC BY-NC-SA", f"{_CREATIVE_COMMONS}by-nc-sa/4.0/"),
    "cc_nc_sa": ("CC BY-NC-SA", f"{_CREATIVE_COMMONS}by-nc-sa/4.0/"),
    "cc_by_nd": ("CC BY-ND", f"{_CREATIVE_COMMONS}by-nd/4.0/"),
    "cc_by_nc_nd": ("CC BY-NC-ND", f"{_CREATIVE_COMMONS}by-nc-nd/4.0/"),
    "cc_nc_nd": ("CC BY-NC-ND", f"{_CREATIVE_COMMONS}by-nc-nd/4.0/"),
    "cc0": ("CC0", _CC0_URL),
    "cc_zero": ("CC0", _CC0_URL),
    "public_domain": ("CC0", _CC0_URL),
}
_TOKEN_SEPARATORS = re.compile(r"[\s\-]+")

# Why a listing was refused, named by the rule that refused it. A report that
# lists only what it offers makes an absence invisible - the 46 archives of
# Mf-9ygi's investigation were exactly that - so every candidate this module
# declines carries one of these reasons. They are vocabulary, not tables: the
# stamp covers what DECIDES a row, and a refusal is the absence of a row.
REFUSAL_R1_NO_MARKERS = "R1:no-markers"
REFUSAL_R2_NO_MARKERS = "R2:no-markers"
REFUSAL_R3_NO_OWN_DEPTH_MODEL = "R3:no-own-depth-model"
REFUSAL_R3_TRIO_NO_IMAGE = "R3:trio-no-image"
REFUSAL_R3_TRIO_NO_TEXT = "R3:trio-no-text"
REFUSAL_R4_NO_OWN_DEPTH_MODEL = "R4:no-own-depth-model"
REFUSAL_R5_DISABLED = "R5:disabled"
REFUSAL_R5_NOT_TITLE = "R5:not-title"
REFUSAL_R5_NO_OWN_DEPTH_MODEL = "R5:no-own-depth-model"
REFUSAL_PROMOTION_NO_OWN_DEPTH_MODEL = "promotion:no-own-depth-model"

REFUSAL_RULES = frozenset(
    {
        REFUSAL_R1_NO_MARKERS,
        REFUSAL_R2_NO_MARKERS,
        REFUSAL_R3_NO_OWN_DEPTH_MODEL,
        REFUSAL_R3_TRIO_NO_IMAGE,
        REFUSAL_R3_TRIO_NO_TEXT,
        REFUSAL_R4_NO_OWN_DEPTH_MODEL,
        REFUSAL_R5_DISABLED,
        REFUSAL_R5_NOT_TITLE,
        REFUSAL_R5_NO_OWN_DEPTH_MODEL,
        REFUSAL_PROMOTION_NO_OWN_DEPTH_MODEL,
    }
)


def _regex_table(pattern) -> dict:
    """One compiled regex as the canonical ``{pattern, flags}`` table entry."""
    return {"pattern": pattern.pattern, "flags": pattern.flags}


def rule_set_tables() -> dict:
    """Every constant a row-deciding path consults, captured BY VALUE.

    This is the artefact a report's stamp is derived from. Each value is read
    from the module at call time, so editing a table changes the stamp that
    ``tests/test_bundle_detect.py`` pins - a table edit cannot go unnoticed.

    The capture is by value, never by reference: sets become sorted lists, a
    regex becomes ``{pattern, flags}``, and ``_LICENCE_TABLE`` becomes a sorted
    dict of ``key -> [text, url]``, so the digest is a function of the tables
    and not of process memory, dict insertion order or set iteration order.

    Deliberately EXCLUDED, because they are enrichment or walk bounds rather
    than row-deciding tables: ``PREVIEW_SUFFIXES``, the cover reason strings
    (``COVER_*``) and the ``MATCH_*`` identities in ``bundle_service``, plus the
    scan bounds (``MAX_SCAN_ENTRIES``, ``HASH_CHUNK_BYTES``, ``DEFAULT_LIMIT``).
    Including them would make a cosmetic edit look like a classification change,
    which is exactly the noise this stamp exists to remove.
    """
    return {
        # Role names.
        "MODEL": MODEL,
        "IMAGE": IMAGE,
        "TEXT": TEXT,
        "ARCHIVE": ARCHIVE,
        "OTHER": OTHER,
        # Detectors.
        "DETECTOR_THINGIVERSE_README": DETECTOR_THINGIVERSE_README,
        "DETECTOR_THINGIVERSE_SIBLING": DETECTOR_THINGIVERSE_SIBLING,
        "DETECTOR_THINGIVERSE_ID_NAME": DETECTOR_THINGIVERSE_ID_NAME,
        "DETECTOR_GENERIC": DETECTOR_GENERIC,
        "DETECTOR_TITLE_NAME": DETECTOR_TITLE_NAME,
        # Confidences.
        "CONFIDENCE_HIGH": CONFIDENCE_HIGH,
        "CONFIDENCE_MEDIUM": CONFIDENCE_MEDIUM,
        "CONFIDENCE_LOW": CONFIDENCE_LOW,
        "INFERRED_CONFIDENCES": sorted(INFERRED_CONFIDENCES),
        "STRONG_CONFIDENCES": sorted(STRONG_CONFIDENCES),
        # Shapes.
        "SHAPE_ROOT_DOCS": SHAPE_ROOT_DOCS,
        "SHAPE_SIBLING_DOCS": SHAPE_SIBLING_DOCS,
        "SHAPE_ID_NAME": SHAPE_ID_NAME,
        "SHAPE_GENERIC": SHAPE_GENERIC,
        "SHAPE_TITLE_NAME": SHAPE_TITLE_NAME,
        # Suffix tables and shape bounds.
        "MODEL_SUFFIXES": sorted(MODEL_SUFFIXES),
        "IMAGE_SUFFIXES": sorted(IMAGE_SUFFIXES),
        "BUNDLE_TEXT_EXTS": sorted(BUNDLE_TEXT_EXTS),
        "R3_MODEL_DIRS": sorted(R3_MODEL_DIRS),
        "GENERIC_CONTAINER_ROOTS": sorted(GENERIC_CONTAINER_ROOTS),
        "_MACOSX_ROOT": _MACOSX_ROOT,
        "BUNDLE_DOC_MAX_BYTES": BUNDLE_DOC_MAX_BYTES,
        "MAX_DOC_CANDIDATES": MAX_DOC_CANDIDATES,
        # R5's vocabulary.
        "TITLE_PART_NAMES": sorted(TITLE_PART_NAMES),
        "TITLE_SINGLE_WORD_FLOOR": TITLE_SINGLE_WORD_FLOOR,
        "TITLE_NAME_BUNDLES_ENABLED": TITLE_NAME_BUNDLES_ENABLED,
        "_TITLE_WORD": _regex_table(_TITLE_WORD),
        # The document names and marker grammar.
        "_ROOT_README": _ROOT_README,
        "_ROOT_LICENSE": _ROOT_LICENSE,
        "_SIBLING_README": _SIBLING_README,
        "_SIBLING_LICENSE": _SIBLING_LICENSE,
        "_THINGIVERSE_ID_IN_NAME": _regex_table(_THINGIVERSE_ID_IN_NAME),
        "_THINGIVERSE_THING_URL": _THINGIVERSE_THING_URL,
        "_README_MARKERS": _regex_table(_README_MARKERS),
        "_LICENSE_LINE": _regex_table(_LICENSE_LINE),
        "_SENTENCE_BOUNDARY": _regex_table(_SENTENCE_BOUNDARY),
        "_LEADING_ORDINAL": _regex_table(_LEADING_ORDINAL),
        "_TRAILING_JUNK": _TRAILING_JUNK,
        "_TOKEN_SEPARATORS": _regex_table(_TOKEN_SEPARATORS),
        # The licence table: a token only ever maps to a URL we know by heart.
        "_LICENCE_TABLE": {key: list(value) for key, value in sorted(_LICENCE_TABLE.items())},
        "_CREATIVE_COMMONS": _CREATIVE_COMMONS,
        "_CC0_URL": _CC0_URL,
    }


def rule_set_stamp() -> str:
    """The sha256 stamp of the rule tables as they are NOW, not as they were imported.

    Read at call time on purpose: a test (or an operator's debug session) can
    change one table and re-derive the stamp, which is how the pin below is
    proven to cover every row-deciding constant.
    """
    canonical = json.dumps(rule_set_tables(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# The stamp of the tables as this process imported them: what every report row
# records, so two reports are comparable by construction or explicitly not.
RULE_SET_STAMP = rule_set_stamp()


def rule_set() -> dict:
    """The rule set a report is taken against: its version and its stamp."""
    return {"version": RULE_SET_VERSION, "stamp": rule_set_stamp()}



@dataclass(frozen=True)
class BundleEntry:
    """One file inside a bundle: its bundle-relative path and its size."""

    rel_path: str
    size: int


@dataclass(frozen=True)
class BundleSnapshot:
    """The listing a bundle is judged from, with no filesystem behind it."""

    bundle_name: str
    rel_dir: str
    entries: Tuple[BundleEntry, ...]


@dataclass(frozen=True)
class BundleDoc:
    """One document the caller actually opened, already bounded and decoded."""

    rel_path: str
    text: str


@dataclass(frozen=True)
class BundleCounts:
    """How many entries of each role the snapshot holds."""

    model: int = 0
    image: int = 0
    text: int = 0
    other: int = 0
    archive: int = 0


@dataclass(frozen=True)
class BundleFacts:
    """What one detect call concluded about one snapshot."""

    detector: str
    confidence: str
    metadata_shape: str
    title: str
    creator: Optional[str]
    licence: Optional[str]
    licence_url: Optional[str]
    source_url: Optional[str]
    thing_id: Optional[str]
    description: Optional[str]
    cover_entry: Optional[str]
    counts: BundleCounts
    model_entries: Tuple[str, ...]


@dataclass(frozen=True)
class Detection:
    """One decision: the facts it offers, or the rule that refused the listing.

    Exactly one of the two is set. ``facts`` is the row a caller may offer (a
    ``BundleFacts``); ``refusal`` is one of ``REFUSAL_RULES`` and means the
    listing is not a bundle. Neither is ever both, so a caller cannot mistake a
    refusal for a weak offer.
    """

    facts: Optional[BundleFacts]
    refusal: Optional[str]


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


@dataclass(frozen=True)
class _DocMarkers:
    """The markers one document carries, whichever filename it was shipped as."""

    creator: Optional[str]
    licence_token: Optional[str]
    title: Optional[str]
    line_creator: Optional[str]
    source_url: Optional[str]
    thing_id: Optional[str]


@dataclass(frozen=True)
class _PairMarkers:
    """One named pair's findings, merged; every field was read, never guessed."""

    creator: str
    licence_token: str
    title: str
    source_url: str
    thing_id: Optional[str]
    description: Optional[str]


_EMPTY_MARKERS = _DocMarkers(
    creator=None,
    licence_token=None,
    title=None,
    line_creator=None,
    source_url=None,
    thing_id=None,
)


def _path_order(rel_path: str) -> Tuple[str, str]:
    """Sort key that never leaks readdir order into a report."""
    return rel_path.casefold(), rel_path


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


def twin_key(name: str) -> str:
    """The identity a folder and a ``.zip`` share: casefolded, one ``.zip`` off."""
    key = (name or "").strip().casefold()
    if key.endswith(".zip"):
        key = key[: -len(".zip")]
    return key


def is_macosx_artifact(rel_path: str) -> bool:
    """Whether this entry is AppleDouble cruft rather than a file of the bundle.

    Only the first ``/`` segment decides, so ``__MACOSX/17_Thing/._a.stl`` and a
    bare top-level ``__MACOSX`` both count. The one place this vocabulary lives:
    both the pure root rule and the disk-side pairing rule skip through it, so
    they cannot disagree about what an archive's payload is.
    """
    head, _separator, _rest = (rel_path or "").partition("/")
    return head.casefold() == _MACOSX_ROOT


def is_bundle_member(rel_path: str, bundle_dirs: AbstractSet[str]) -> bool:
    """Whether ``rel_path`` is a file of one of the detected bundle directories.

    Membership is strict and raw: the path must sit BELOW the directory, so a
    ``<name>.zip`` beside a ``<name>/`` is that directory's twin (provenance),
    not one of its files, and the comparison is case-sensitive exactly like the
    container strip and ``_forget_claimed_subtree`` it has to agree with. This
    is the one place the Mf-74x5 rule is expressed: a nested archive is a
    supporting file of the enclosing bundle, never a bundle of its own.
    """
    head, separator, _rest = (rel_path or "").rpartition("/")
    while separator:
        if head in bundle_dirs:
            return True
        head, separator, _rest = head.rpartition("/")
    return False


def common_entry_root(rel_paths: Iterable[str]) -> Optional[str]:
    """The one top-level directory every entry sits under, or ``None``.

    The root carries the first casing seen, so the value can name a directory on
    disk without a second lookup. ``None`` when there is no single answer: empty
    input, an entry at the top level (a loose file is not a bundle's wrapper), or
    two different first segments. Segments are compared casefolded.

    A top-level ``__MACOSX`` segment and everything beneath it is ignored
    (``is_macosx_artifact``): it is a deterministic artifact of the
    rsync/hand-extraction workflow this report exists for, so counting it would
    make every such archive rootless. Entries are files only; a directory entry
    never reaches this function.
    """
    root: Optional[str] = None
    for rel_path in rel_paths:
        if is_macosx_artifact(rel_path):
            continue
        head, separator, _rest = (rel_path or "").partition("/")
        if not separator:
            return None
        if root is None:
            root = head
        elif head.casefold() != root.casefold():
            return None
    return root


def is_generic_container_root(name: str) -> bool:
    """Whether ``name`` says nothing about which bundle an archive holds."""
    return (name or "").strip().casefold() in GENERIC_CONTAINER_ROOTS


def _payload_entries(archive_entries: Iterable[Tuple[str, int]]) -> List[Tuple[str, int]]:
    """The archive's real payload: ``__MACOSX`` cruft dropped, ``(path, size)`` kept."""
    return [
        (str(rel_path), int(size))
        for rel_path, size in archive_entries
        if not is_macosx_artifact(str(rel_path))
    ]


def _payload_present(
    payload: Iterable[Tuple[str, int]], folder: AbstractSet[Tuple[str, int]]
) -> bool:
    """Whether every payload entry sits in the folder's listing, path casefolded."""
    return all((rel_path.casefold(), size) in folder for rel_path, size in payload)


def _payload_without_root(
    payload: Iterable[Tuple[str, int]], entry_root: str
) -> Optional[List[Tuple[str, int]]]:
    """Every payload entry with ``entry_root``'s first segment split off.

    ``None`` as soon as one entry sits outside the root. The first segment is split
    off rather than sliced by the length of a casefolded prefix: casefolding may
    change a string's length, and a slice would then cut the tail in the wrong
    place.
    """
    wanted = (entry_root or "").casefold()
    stripped: List[Tuple[str, int]] = []
    for rel_path, size in payload:
        head, separator, rest = rel_path.partition("/")
        if not separator or head.casefold() != wanted:
            return None
        stripped.append((rest, size))
    return stripped


def archive_payload_contained(
    archive_entries: Iterable[Tuple[str, int]],
    folder_entries: Iterable[BundleEntry],
    entry_root: Optional[str] = None,
) -> bool:
    """Whether this archive's payload is contained in this directory's listing.

    The ONE containment predicate behind both pairing routes, so they cannot
    disagree about what an archive's payload is or about when it fits a folder.
    It judges two listings and a string; it never touches disk.

    Containment, not equality: a lived-in directory legitimately holds more than
    the archive that shipped it (extracted, then a second zip dropped in, then a
    render saved by hand), and equality would refuse the very pairs this exists
    for. Declared size is an archive header field, so this decides pairing and is
    never an integrity claim: a lie in the header only makes a pairing wrong,
    never a bundle corrupt. The skip is ``is_macosx_artifact`` - the same
    predicate ``common_entry_root`` applies - so the two layers agree on what the
    payload is, and an archive of cruft alone has verified nothing and is refused.

    ``entry_root`` given is the strict strip-and-compare: every payload entry's
    first segment must casefold-equal the root and the remainder plus its declared
    size must sit in the folder. An entry outside the root is ``False``. There is
    deliberately no identity shortcut on this route - trying one would silently
    widen the rule that produced the root.

    ``entry_root is None`` is the stem route, where nobody has named the wrapper.
    The wrapper decision is therefore made ONCE for the whole archive, never per
    entry and never twice: identity first (the archive's paths are already the
    folder's paths), else a single ``common_entry_root`` strip. Two strips are
    refused on purpose - ``a/files/x.stl`` must not verify against a directory
    holding ``x.stl``, because that would pair a wrapper's wrapper with anything
    nested one level deeper.
    """
    payload = _payload_entries(archive_entries)
    if not payload:
        return False
    folder = {(entry.rel_path.casefold(), entry.size) for entry in folder_entries}
    if entry_root is not None:
        stripped = _payload_without_root(payload, entry_root)
        return stripped is not None and _payload_present(stripped, folder)
    if _payload_present(payload, folder):
        return True
    root = common_entry_root([rel_path for rel_path, _size in payload])
    if root is None:
        return False
    stripped = _payload_without_root(payload, root)
    return stripped is not None and _payload_present(stripped, folder)


def _trim_marker_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip().strip(_TRAILING_JUNK).strip()
    return cleaned or None


def _trim_to_last_sentence(value: Optional[str]) -> Optional[str]:
    """``value`` from its last sentence boundary on; unchanged when there is none.

    A marker can sit mid-line behind a sentence of its own (``Notes. 2-color
    standing cat by nervoussystem on Thingiverse: ...``), and that leading
    sentence is prose, not the title. The boundary needs the whitespace, so
    ``v1.2`` survives.
    """
    if not value:
        return value
    return _SENTENCE_BOUNDARY.split(value)[-1].strip() or None


def parse_thingiverse_readme(text: str) -> Tuple[Optional[str], Optional[str]]:
    """``(creator, licence_token)`` from a Thingiverse README, one scan.

    Each half is ``None`` unless its own marker matched: a README that mentions a
    licence but not the uploader must not invent an author.
    """
    creator: Optional[str] = None
    licence: Optional[str] = None
    for match in _README_MARKERS.finditer(text or ""):
        if creator is None and match.group("creator"):
            creator = _trim_marker_value(match.group("creator"))
        if licence is None and match.group("licence"):
            licence = _trim_marker_value(match.group("licence"))
        if creator and licence:
            break
    return creator, licence


def parse_thingiverse_license(text: str) -> Optional[Tuple[str, str, Optional[str], str]]:
    """``(title, creator, thing_id, url)`` from a Thingiverse license line, or ``None``.

    The thing id is read with the shipped ``import_resolvers`` helper, not with a
    second regex, so the report and the import door cannot disagree about what a
    Thingiverse URL means.

    The title is whatever precedes the last `` by ... on Thingiverse:`` on one
    line, trimmed at its last ``". "``/``"! "``/``"? "`` sentence boundary.
    Known limitation, accepted rather than guessed around: a genuine title that
    contains ``". "`` - ``Dr. No Replica`` - loses its leading part and comes
    back as ``No Replica``.
    """
    match = _LICENSE_LINE.search(text or "")
    if match is None:
        return None
    title = _trim_to_last_sentence(_trim_marker_value(match.group("title")))
    creator = _trim_marker_value(match.group("creator"))
    url = match.group("url").rstrip(").,")
    if not title or not creator or not url:
        return None
    return title, creator, thingiverse_thing_id_from_url(url), url


def licence_from_token(token: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """``(display, url)`` for a known licence token; ``(token, None)`` otherwise.

    ``token is None`` stays ``(None, None)``. An unknown token is echoed back
    unmodified and gets no URL: this function never invents a licence link.
    """
    if not token:
        return None, None
    normalised = _TOKEN_SEPARATORS.sub("_", token.strip().lower()).strip("_")
    known = _LICENCE_TABLE.get(normalised)
    if known is not None:
        return known
    return token, None


def _root_entry(snapshot: BundleSnapshot, wanted: str) -> Optional[str]:
    """The depth-0 entry whose whole name is ``wanted``, case-insensitively."""
    target = wanted.casefold()
    for entry in snapshot.entries:
        if "/" in entry.rel_path:
            continue
        if entry.rel_path.casefold() == target:
            return entry.rel_path
    return None


def _sibling_doc(snapshot: BundleSnapshot, suffix: str) -> Optional[str]:
    """The ``<bundle><suffix>`` document, as a file or inside such a directory.

    The directory shape is the legacy flattening: the vendor's documents were
    someone else's directory, so the name is a directory and the document is the
    first text file one level below it.
    """
    wanted = f"{snapshot.bundle_name}{suffix}".casefold()
    ordered = sorted(snapshot.entries, key=lambda entry: _path_order(entry.rel_path))
    for entry in ordered:
        if "/" in entry.rel_path:
            continue
        if entry.rel_path.casefold() == wanted:
            return entry.rel_path
    for entry in ordered:
        parts = entry.rel_path.split("/")
        if len(parts) != 2 or parts[0].casefold() != wanted:
            continue
        if _is_text(entry.rel_path, entry.size):
            return entry.rel_path
    return None


def _root_pair_paths(snapshot: BundleSnapshot) -> Tuple[str, ...]:
    """The root pair's existing documents, README-name first; at least one."""
    found = (_root_entry(snapshot, _ROOT_README), _root_entry(snapshot, _ROOT_LICENSE))
    return tuple(path for path in found if path)


def _sibling_pair_paths(snapshot: BundleSnapshot) -> Tuple[str, ...]:
    """The sibling pair's existing documents, README-name first; at least one."""
    found = (_sibling_doc(snapshot, _SIBLING_README), _sibling_doc(snapshot, _SIBLING_LICENSE))
    return tuple(path for path in found if path)


def generic_doc_candidates(snapshot: BundleSnapshot) -> Tuple[str, ...]:
    """The first text entry anywhere, for a bundle with no known document pair."""
    texts = sorted(
        (entry.rel_path for entry in snapshot.entries if _is_text(entry.rel_path, entry.size)),
        key=_path_order,
    )
    return tuple(texts[:1])


def doc_entry_candidates(snapshot: BundleSnapshot) -> Tuple[str, ...]:
    """At most ``MAX_DOC_CANDIDATES`` bundle-relative paths worth opening.

    Priority order: the root README/LICENSE pair, then the sibling pair (file or
    legacy directory shape), then - for a generic bundle - the first text entry
    anywhere. The caller reads exactly these and nothing else.
    """
    candidates: List[str] = []
    for wanted in (_ROOT_README, _ROOT_LICENSE):
        found = _root_entry(snapshot, wanted)
        if found:
            candidates.append(found)
    for suffix in (_SIBLING_README, _SIBLING_LICENSE):
        found = _sibling_doc(snapshot, suffix)
        if found:
            candidates.append(found)
    candidates.extend(generic_doc_candidates(snapshot))

    ordered: List[str] = []
    for candidate in candidates:
        if candidate not in ordered:
            ordered.append(candidate)
    return tuple(ordered[:MAX_DOC_CANDIDATES])


def _counts(snapshot: BundleSnapshot) -> BundleCounts:
    counts = {MODEL: 0, IMAGE: 0, TEXT: 0, OTHER: 0, ARCHIVE: 0}
    for entry in snapshot.entries:
        counts[classify_entry(entry.rel_path, entry.size)] += 1
    return BundleCounts(
        model=counts[MODEL],
        image=counts[IMAGE],
        text=counts[TEXT],
        other=counts[OTHER],
        archive=counts[ARCHIVE],
    )


def _cover_entry(snapshot: BundleSnapshot) -> Optional[str]:
    """The largest image, ties broken by path so the pick is a function of the tree."""
    images = sorted(
        ((entry.rel_path, entry.size) for entry in snapshot.entries if classify_entry(entry.rel_path, entry.size) == IMAGE),
        key=lambda item: (-item[1], _path_order(item[0])),
    )
    return images[0][0] if images else None


def _own_depth_model_count(snapshot: BundleSnapshot) -> int:
    """Models at the bundle's own depth, in one of R3's three allowed shapes.

    A model counts when it is at depth 0, when it is at depth 1 under the named
    ``R3_MODEL_DIRS`` (``files/``), or when every depth-1 model lives under a
    single model-bearing subdirectory of any name - the "one folder in, models
    out" shape (``STL/``, a vendor wrapper, a wrapped zip's own directory).

    R1 and R2 are bounded by construction - their marker documents live at depth
    0 - but R3 counts roles, so an unbounded model count lets any ancestor of a
    bundle qualify on its descendants' models. Images and text stay unbounded:
    neither can supply the trio alone, and a real generic bundle keeps its
    previews in ``images/``. Cardinality, not a longer name list, is the bound: a
    conventional-name table only ever helps names someone thought of, while two
    or more unnamed model-bearing subdirectories with nothing at depth 0 or
    under the named directory really is a pile of things rather than a bundle.
    """
    own = 0
    unnamed: Dict[str, int] = {}
    for entry in snapshot.entries:
        if classify_entry(entry.rel_path, entry.size) != MODEL:
            continue
        parts = entry.rel_path.split("/")
        if len(parts) == 1:
            own += 1
        elif len(parts) == 2:
            if parts[0] in R3_MODEL_DIRS:
                own += 1
            else:
                unnamed[parts[0]] = unnamed.get(parts[0], 0) + 1
    if own:
        return own
    if len(unnamed) == 1:
        return next(iter(unnamed.values()))
    return 0


def _model_entries(snapshot: BundleSnapshot) -> Tuple[str, ...]:
    return tuple(
        sorted(
            (entry.rel_path for entry in snapshot.entries if classify_entry(entry.rel_path, entry.size) == MODEL),
            key=_path_order,
        )
    )


def _collapse(text: str) -> str:
    return " ".join((text or "").split())


def _scan_doc(text: Optional[str]) -> _DocMarkers:
    """Every marker ``text`` carries, whichever filename it was shipped as.

    The filename is never consulted: the two Thingiverse layouts differ only in
    which document carries which sentence, so asking the text is the only
    question both layouts answer the same way.
    """
    if not text:
        return _EMPTY_MARKERS
    creator, licence_token = parse_thingiverse_readme(text)
    parsed = parse_thingiverse_license(text)
    if parsed is None:
        return _DocMarkers(
            creator=creator,
            licence_token=licence_token,
            title=None,
            line_creator=None,
            source_url=None,
            thing_id=None,
        )
    title, line_creator, thing_id, url = parsed
    return _DocMarkers(
        creator=creator,
        licence_token=licence_token,
        title=title,
        line_creator=line_creator,
        source_url=url,
        thing_id=thing_id,
    )


def _merge_pair(paths: Sequence[str], docs: Mapping[str, str]) -> Optional[_PairMarkers]:
    """Fold one named pair's documents into facts, or ``None`` if a field is absent.

    ``paths`` is the pair's existing documents in a fixed order (README-name
    first, LICENSE-name second) and is only a tie-break: every field takes the
    first non-empty value seen, so no field is preferred because of which file
    held it. The created-by sentence outranks the title line's creator, and the
    document that carried it supplies the description; the title-line creator is
    a fallback only when the pair carries no created-by sentence at all, and that
    fallback has no created-by document, so it gets no description.
    """
    created_by_creator: Optional[str] = None
    created_by_text: Optional[str] = None
    fallback_creator: Optional[str] = None
    licence_token: Optional[str] = None
    title: Optional[str] = None
    source_url: Optional[str] = None
    thing_id: Optional[str] = None

    for path in paths:
        text = docs.get(path)
        markers = _scan_doc(text)
        if created_by_creator is None and markers.creator:
            created_by_creator = markers.creator
            created_by_text = text
        if fallback_creator is None and markers.line_creator:
            fallback_creator = markers.line_creator
        if licence_token is None and markers.licence_token:
            licence_token = markers.licence_token
        if title is None and markers.title:
            title = markers.title
        if source_url is None and markers.source_url:
            source_url = markers.source_url
            thing_id = markers.thing_id

    creator = created_by_creator or fallback_creator
    if not creator or not licence_token or not title or not source_url:
        return None
    return _PairMarkers(
        creator=creator,
        licence_token=licence_token,
        title=title,
        source_url=source_url,
        thing_id=thing_id,
        description=created_by_text,
    )


def _high_facts(
    detector: str,
    shape: str,
    snapshot: BundleSnapshot,
    pair: _PairMarkers,
    counts: BundleCounts,
    cover: Optional[str],
    models: Tuple[str, ...],
) -> BundleFacts:
    licence, licence_url = licence_from_token(pair.licence_token)
    return BundleFacts(
        detector=detector,
        confidence=CONFIDENCE_HIGH,
        metadata_shape=shape,
        title=pair.title or display_name(snapshot.bundle_name),
        creator=pair.creator,
        licence=licence,
        licence_url=licence_url,
        source_url=pair.source_url,
        thing_id=pair.thing_id,
        description=_collapse(pair.description or "")[:2000] or None,
        cover_entry=cover,
        counts=counts,
        model_entries=models,
    )


def _generic_facts(
    snapshot: BundleSnapshot,
    counts: BundleCounts,
    cover: Optional[str],
    models: Tuple[str, ...],
) -> BundleFacts:
    return _name_only_facts(DETECTOR_GENERIC, CONFIDENCE_LOW, SHAPE_GENERIC, snapshot, counts, cover, models)


def _name_only_facts(
    detector: str,
    confidence: str,
    shape: str,
    snapshot: BundleSnapshot,
    counts: BundleCounts,
    cover: Optional[str],
    models: Tuple[str, ...],
    thing_id: Optional[str] = None,
) -> BundleFacts:
    """A row built from the tree's own shape, with no vendor document behind it.

    Creator, licence and licence URL are **always** ``None`` here, and there is
    deliberately no parameter that could set them. An id in a name says where a
    thing came from, never who made it or what it may be reused for, and the
    Creative Commons variants differ materially (`cc-nc` forbids the commercial
    use `cc-sa` allows), so a guessed licence is a legal claim we are not
    entitled to make - and inheriting one from a sibling bundle would launder a
    guess into an attribution.

    The name-derived id is the one piece of provenance allowed through, and the
    URL and the id cannot disagree: the URL is built from the id, and the stored
    ``thing_id`` is then read *back* out of that URL by the shipped resolver, so
    ``thingiverse_thing_id_from_url(source_url) == thing_id`` by construction.
    """
    source_url = _THINGIVERSE_THING_URL.format(thing_id) if thing_id is not None else None
    return BundleFacts(
        detector=detector,
        confidence=confidence,
        metadata_shape=shape,
        title=display_name(snapshot.bundle_name),
        creator=None,
        licence=None,
        licence_url=None,
        source_url=source_url,
        thing_id=thingiverse_thing_id_from_url(source_url) if source_url is not None else None,
        description=None,
        cover_entry=cover,
        counts=counts,
        model_entries=models,
    )


def detect_bundle_decision(snapshot: BundleSnapshot, docs: Mapping[str, str]) -> Detection:
    """Classify one snapshot: the facts it offers, or the rule that refused it.

    ``docs`` is keyed by the bundle-relative path the caller actually opened, so
    this function can only parse what it was handed - it never reaches for disk.

    The order is R1, R2, R4, R3. R4 sits **before** R3 on purpose: an id in the
    name is provenance we inferred, which is strictly more than a shape guess, so
    an id-named directory that would also satisfy R3's trio must read `medium`
    and not `low`. Both R4 and R3 gate on the same ``_own_depth_model_count``
    predicate, so the one model-bearing-directory cardinality bound governs both
    and is computed once.

    When no rule fires, the refusal names the FIRST rule the listing failed, in
    rule order: a document pair that exists but did not merge (R1, then R2), then
    R4's missing own-depth model, then R3's three requirements in the order they
    are checked. That is the whole point of the reason - an absence in a report is
    otherwise indistinguishable from a directory nobody scanned.
    """
    counts = _counts(snapshot)
    models = _model_entries(snapshot)
    cover = _cover_entry(snapshot)
    own_models = _own_depth_model_count(snapshot)

    root_pair_paths = _root_pair_paths(snapshot)
    root_pair = _merge_pair(root_pair_paths, docs)
    if root_pair is not None:
        facts = _high_facts(DETECTOR_THINGIVERSE_README, SHAPE_ROOT_DOCS, snapshot, root_pair, counts, cover, models)
        return Detection(facts=facts, refusal=None)

    sibling_pair_paths = _sibling_pair_paths(snapshot)
    sibling_pair = _merge_pair(sibling_pair_paths, docs)
    if sibling_pair is not None:
        facts = _high_facts(
            DETECTOR_THINGIVERSE_SIBLING, SHAPE_SIBLING_DOCS, snapshot, sibling_pair, counts, cover, models
        )
        return Detection(facts=facts, refusal=None)

    thing_id = thingiverse_id_from_name(snapshot.bundle_name)
    if thing_id is not None and own_models >= 1:
        facts = _name_only_facts(
            DETECTOR_THINGIVERSE_ID_NAME,
            CONFIDENCE_MEDIUM,
            SHAPE_ID_NAME,
            snapshot,
            counts,
            cover,
            models,
            thing_id,
        )
        return Detection(facts=facts, refusal=None)

    if own_models >= 1 and counts.image >= 1 and counts.text >= 1:
        return Detection(facts=_generic_facts(snapshot, counts, cover, models), refusal=None)

    # Nothing fired: name the FIRST rule the listing failed, in rule order.
    if root_pair_paths:
        refusal = REFUSAL_R1_NO_MARKERS
    elif sibling_pair_paths:
        refusal = REFUSAL_R2_NO_MARKERS
    elif thing_id is not None and own_models < 1:
        refusal = REFUSAL_R4_NO_OWN_DEPTH_MODEL
    elif own_models < 1:
        refusal = REFUSAL_R3_NO_OWN_DEPTH_MODEL
    elif counts.image < 1:
        refusal = REFUSAL_R3_TRIO_NO_IMAGE
    else:
        refusal = REFUSAL_R3_TRIO_NO_TEXT
    return Detection(facts=None, refusal=refusal)


def detect_bundle(snapshot: BundleSnapshot, docs: Mapping[str, str]) -> Optional[BundleFacts]:
    """The facts of one snapshot; ``None`` means "this is not a vendor bundle".

    The reason a listing was refused is ``detect_bundle_decision``'s, and callers
    that need it ask that one; this wrapper keeps every existing caller and test
    reading the same answer it always did.
    """
    return detect_bundle_decision(snapshot, docs).facts


def archive_owned_decision(snapshot: BundleSnapshot) -> Detection:
    """``archive_owned_facts``' decision: the facts, or the rule that refused them."""
    facts = archive_owned_facts(snapshot)
    if facts is not None:
        return Detection(facts=facts, refusal=None)
    return Detection(facts=None, refusal=REFUSAL_PROMOTION_NO_OWN_DEPTH_MODEL)


def archive_owned_facts(snapshot: BundleSnapshot) -> Optional[BundleFacts]:
    """The facts for a directory that a verified archive owns, whatever it holds.

    A vendor extraction can lose its README and still be one thing: twelve models
    and two renders and no ``.txt`` anywhere, or a README and a stack of previews
    beside the models, or models alone. R3 needs a model, an image and a text file,
    so it refuses those trees and the directory would otherwise render no row at
    all even though the archive beside it proves what it is. A verified pairing is
    therefore the identity here and images and text are decoration: neither is
    required, and a stray ``.txt`` does not disqualify. This is rendered as the
    same ``generic-bundle`` / ``low`` row as any other guess, so a promoted
    directory is creatable exactly like one; only a directory the container rule
    refuses comes back as ``blocked`` / ``archive_only``.

    ``_own_depth_model_count`` is now the ONLY shape guard, and it is load-bearing:
    the archive says WHICH thing the directory is, never that a pile of things is
    one thing. That bound is what keeps a directory holding two or more unnamed
    model-bearing subdirectories - or models buried at depth 2 - from being
    promoted on an ancestor's or a child's models. It is reused deliberately, so
    the Mf-h0p8 container bound and the Mf-snd8 cardinality rule apply here for
    free and an ancestor can never qualify on its children's models. Do not soften
    it to make a case pass: loosening it promotes piles.

    Every provenance field stays ``None`` - an entry root or a stem is evidence of
    WHICH thing this is, never of who made it.

    This function does NOT make a directory creatable and does NOT let R3 through:
    it only turns a listing into facts, and the service decides what to do with
    them - a directory offered this way has to be a resolved ``Folder`` with
    registered model assets, exactly like any other LOW row. It also does NOT
    claim a subtree and does NOT touch the store: it turns a listing into facts
    and stops there.
    """
    counts = _counts(snapshot)
    if _own_depth_model_count(snapshot) < 1:
        return None
    return _generic_facts(snapshot, counts, _cover_entry(snapshot), _model_entries(snapshot))


def is_title_name(name: str) -> bool:
    """Whether a folder name reads like a title a person chose, not a part.

    Computed on the DISPLAY name, whitespace-collapsed, so `17_ClockworkMonster`
    is judged as `ClockworkMonster` and `mac_deckbox` as `mac deckbox` - the
    name the operator sees, not the raw segment.

    Exclusion first, on the whole case-folded segment: `files`, `source` and the
    rest of ``TITLE_PART_NAMES`` are parts, whatever else they look like.
    Branch A: any non-alphanumeric character is a space or a separator, which is
    a person's phrasing and is the signal for `Rocket Filement support`,
    `skull mask` and `printable carrage`. Branch B: one capitalised word longer
    than ``TITLE_SINGLE_WORD_FLOOR`` that carries a lowercase letter, which is
    the signal for `Stagetop`, `LittleTup`, `GavinIsABot` and `Taffi`.

    Branch C - a vendor-ish suffix (`_v2`, `-remix`, `thingiverse`) - is
    DECLINED deliberately: every suffix table is a guess about names someone
    thought of, the measured library needed it for none of the nine, and the
    same names already reach branch A through their separator. See ADR 0010
    Decision 21.

    `files` and `source` fail every branch twice over: the exclusion refuses
    them first, and with the list emptied branch A finds no separator and
    branch B's `fullmatch` is `None` on a leading lowercase character.
    """
    cleaned = " ".join(display_name(name).split())
    if cleaned.casefold() in TITLE_PART_NAMES:
        return False
    if any(not character.isalnum() for character in cleaned):
        return True
    if _TITLE_WORD.fullmatch(cleaned) is None:
        return False
    if len(cleaned) <= TITLE_SINGLE_WORD_FLOOR:
        return False
    return any(character.islower() for character in cleaned)


def title_name_facts(snapshot: BundleSnapshot) -> Optional[BundleFacts]:
    """The facts for a title-shaped folder of models, or ``None``.

    This is R5: the name is the only evidence, so the row is ``low`` and every
    provenance field is ``None`` - a name proves nothing about who made a thing
    or what it may be reused for. It is deliberately NOT a branch of
    ``detect_bundle``: the service runs it as a post-pass after the archive
    promotion, because the folder pass is deepest-first and a rule inside it
    would migrate a parent's row into a child and lose an archive-owned row
    (ADR 0010 Decision 21).

    ``_own_depth_model_count`` is the one shape guard and it is load-bearing:
    without a model at the folder's own depth the name alone is not a bundle,
    so a pile of unnamed model-bearing subdirectories and models buried at
    depth 2 stay refused exactly as they are under R3.

    The switch ``TITLE_NAME_BUNDLES_ENABLED`` is read HERE and nowhere else.
    """
    if not TITLE_NAME_BUNDLES_ENABLED:
        return None
    if not is_title_name(snapshot.bundle_name):
        return None
    if _own_depth_model_count(snapshot) < 1:
        return None
    return _name_only_facts(
        DETECTOR_TITLE_NAME,
        CONFIDENCE_LOW,
        SHAPE_TITLE_NAME,
        snapshot,
        _counts(snapshot),
        _cover_entry(snapshot),
        _model_entries(snapshot),
    )


def title_name_decision(snapshot: BundleSnapshot) -> Detection:
    """``title_name_facts``' decision: the facts, or the rule that refused them.

    The refusals are ordered exactly as ``title_name_facts`` decides them: the
    switch first (so turning R5 off reads as one reason, not as "not a title"),
    then the name, then the one shape guard.
    """
    if not TITLE_NAME_BUNDLES_ENABLED:
        return Detection(facts=None, refusal=REFUSAL_R5_DISABLED)
    if not is_title_name(snapshot.bundle_name):
        return Detection(facts=None, refusal=REFUSAL_R5_NOT_TITLE)
    if _own_depth_model_count(snapshot) < 1:
        return Detection(facts=None, refusal=REFUSAL_R5_NO_OWN_DEPTH_MODEL)
    return Detection(facts=title_name_facts(snapshot), refusal=None)
