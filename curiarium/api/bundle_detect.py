"""Decide whether a directory (or an archive) is one vendor bundle, and how sure.

A *bundle* is one thing a vendor shipped as a whole: the model files, its preview
images, and the README/LICENSE that say who made it. This module turns a listing
plus the handful of text documents the caller opened into a ``BundleFacts`` row,
or into ``None`` when the listing is not a bundle at all. It is pure on purpose -
no ``Session``, no ``engine``, no ``open()``, no ``Path.read_*``, no
``os.scandir`` - so every rule below can be tested from literals, and the one
disk+DB layer (``bundle_service``) owns every byte this module is handed.

The three rules, strongest first:

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

Every heuristic in this module is proven by ``tests/test_bundle_detect.py``: the
suffix table, the shared image-suffix constant, both parser shapes and their
negative cases, the licence table, the display name, the twin key, the cover
pick, and the rule precedence including the marker-less README falling to low
confidence.
"""

import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

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
DETECTOR_GENERIC = "generic-bundle"
CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"

SHAPE_ROOT_DOCS = "root-docs"
SHAPE_SIBLING_DOCS = "sibling-docs"
SHAPE_GENERIC = "generic"

_ROOT_README = "readme.txt"
_ROOT_LICENSE = "license.txt"
_SIBLING_README = "_README"
_SIBLING_LICENSE = "_LICENSE"

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


def twin_key(name: str) -> str:
    """The identity a folder and a ``.zip`` share: casefolded, one ``.zip`` off."""
    key = (name or "").strip().casefold()
    if key.endswith(".zip"):
        key = key[: -len(".zip")]
    return key


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
    return BundleFacts(
        detector=DETECTOR_GENERIC,
        confidence=CONFIDENCE_LOW,
        metadata_shape=SHAPE_GENERIC,
        title=display_name(snapshot.bundle_name),
        creator=None,
        licence=None,
        licence_url=None,
        source_url=None,
        thing_id=None,
        description=None,
        cover_entry=cover,
        counts=counts,
        model_entries=models,
    )


def detect_bundle(snapshot: BundleSnapshot, docs: Mapping[str, str]) -> Optional[BundleFacts]:
    """Classify one snapshot; ``None`` means "this is not a vendor bundle".

    ``docs`` is keyed by the bundle-relative path the caller actually opened, so
    this function can only parse what it was handed - it never reaches for disk.
    """
    counts = _counts(snapshot)
    models = _model_entries(snapshot)
    cover = _cover_entry(snapshot)

    root_pair = _merge_pair(_root_pair_paths(snapshot), docs)
    if root_pair is not None:
        return _high_facts(DETECTOR_THINGIVERSE_README, SHAPE_ROOT_DOCS, snapshot, root_pair, counts, cover, models)

    sibling_pair = _merge_pair(_sibling_pair_paths(snapshot), docs)
    if sibling_pair is not None:
        return _high_facts(DETECTOR_THINGIVERSE_SIBLING, SHAPE_SIBLING_DOCS, snapshot, sibling_pair, counts, cover, models)

    if _own_depth_model_count(snapshot) >= 1 and counts.image >= 1 and counts.text >= 1:
        return _generic_facts(snapshot, counts, cover, models)
    return None
