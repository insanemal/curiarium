"""What a vendor page MEANS, and which of its facts a row may take.

This module is the pure half of enrichment: it turns page bytes into a
``PageFacts``, decides what one licence string is, and decides which of a page's
facts fill an empty row. It never fetches (the caller owns ``safe_urlopen`` and
the cookie plumbing), never reads a row (it is handed the row's three values) and
never guesses a licence - a guessed licence is a legal claim, and the Creative
Commons variants differ materially. Every function below is a function of its
arguments alone, so every rule is pinned from literals in
``tests/test_bundle_enrich.py``.

Why it does not reuse ``import_metadata.parse_page_metadata``: on Thingiverse that
parser reads ``<meta name="author">``, which this site fills with
``Thingiverse.com`` - the marketplace, never the maker - and it accepts a creator
from ANY JSON-LD node, including ``Product.brand.name``. The fixture
``tests/fixtures/thingiverse/thing-5390184.json`` is the proof: the generic
parser answers ``creator="Thingiverse.com"`` for a page whose owner is
``P1lotz``. This module therefore reads only ``CreativeWork`` nodes' ``author``
and ``license``, and it never reads the ``<meta>`` tag at all.

The cache is one JSON value in one ``AppConfig`` key, keyed by vendor and id -
the ``bundle_service.get_dismissals`` idiom. Read-modify-write can lose an entry
under concurrent enrichment; that is accepted at household scale exactly as the
dismissal map accepts it, and the worst case is one extra fetch, never a wrong
row.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

import bundle_detect

# The window a cached page is trusted for: re-opening the queue must not
# re-fetch, but a vendor edit should eventually be seen.
ENRICHMENT_TTL_SECONDS = 7 * 24 * 60 * 60
# The bound for the single JSON key: an enrichment cache is a working set, not a
# table, so it may never grow without limit.
MAX_ENRICHMENT_ENTRIES = 500
# The wire vocabulary a row's status reports, in one place, so a typo cannot
# invent a sixth status: fetched (the page answered just now), cached (a fresh
# record answered instead of a fetch), refused (the vendor refused, and the
# reason says what to do), unavailable (nothing to fetch: the row names no id and
# no source URL) and unknown (the key is not a live bundle any more).
STATUS_FETCHED = "fetched"
STATUS_CACHED = "cached"
STATUS_REFUSED = "refused"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNKNOWN = "unknown"
ENRICH_STATUSES = frozenset({STATUS_FETCHED, STATUS_CACHED, STATUS_REFUSED, STATUS_UNAVAILABLE, STATUS_UNKNOWN})
# What a cache key starts with: the vendor, so a second vendor cannot collide.
CACHE_KEY_PREFIX = "thingiverse:"

_CREATIVE_WORK = "CreativeWork"
_LICENSES_SEGMENT = "licenses"
_PUBLIC_DOMAIN_SEGMENT = "publicdomain"
_PUBLIC_DOMAIN_DISPLAYS = {"zero": "CC0", "mark": "Public Domain Mark"}


@dataclass(frozen=True)
class PageFacts:
    """What one vendor page said about one thing; every field may be absent."""

    creator: Optional[str]
    licence: Optional[str]
    licence_url: Optional[str]

    @property
    def empty(self) -> bool:
        """Whether the page supplied nothing, which is a refusal and not a success."""
        return self.creator is None and self.licence is None and self.licence_url is None


class _JsonLdParser(HTMLParser):
    """Collect the body of every ``<script type="application/ld+json">`` block."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[str] = []
        self._collecting = False
        self._parts: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "script":
            return
        attributes = {name.lower(): (value or "") for name, value in attrs}
        if (attributes.get("type") or "").lower() == "application/ld+json":
            self._collecting = True
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._collecting:
            self._collecting = False
            block = "".join(self._parts).strip()
            if block:
                self.blocks.append(block)
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._collecting:
            self._parts.append(data)


def cache_key(source_url: Optional[str]) -> Optional[str]:
    """``"thingiverse:<id>"`` when the URL names a Thingiverse thing, else ``None``.

    The id is read by the shipped ``bundle_detect.thingiverse_thing_id_from_url``,
    so the cache, the detection report and the import door cannot disagree about
    what a URL means. A blank URL names nothing and gets no key.
    """
    cleaned = (source_url or "").strip()
    if not cleaned:
        return None
    thing_id = bundle_detect.thingiverse_thing_id_from_url(cleaned)
    if not thing_id:
        return None
    return f"{CACHE_KEY_PREFIX}{thing_id}"


def parse_thing_page(html: str) -> Optional[PageFacts]:
    """The page's own answer, or ``None`` when the page carries no thing metadata.

    Only a ``CreativeWork`` node may supply anything. Two things this site puts
    on every page are deliberately NOT evidence: ``<meta name="author">`` is the
    marketplace, and ``Product.brand.name`` is the marketplace's brand block, so
    neither can name a person. A page with neither an author nor a licence is a
    refusal (``None``), never an empty success.
    """
    nodes = _creative_work_nodes(_json_ld_objects(html))
    creator: Optional[str] = None
    licence_value: Optional[str] = None
    for node in nodes:
        if creator is None:
            creator = _author_name(node)
        if licence_value is None:
            licence_value = _license_value(node)
        if creator is not None and licence_value is not None:
            break
    licence, licence_url = licence_from_page(licence_value)
    facts = PageFacts(creator=creator, licence=licence, licence_url=licence_url)
    return None if facts.empty else facts


def licence_from_page(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """``(display, url)`` for a licence a page states, by its own spelling.

    A value that is not a URL is a vendor token (an API-style ``cc-nc-sa``), and
    belongs to ``bundle_detect.licence_from_token``'s table unchanged. A URL is
    read by DEED PATH: the display comes from that same table, while the URL is
    always the page's VERBATIM string. Never the table's own URL: the table
    hardcodes ``.../4.0/``, so a real ``by-nc-sa/3.0`` page would be silently
    re-versioned into a legal claim the vendor never made. An unknown deed slug
    echoes the page's string as both display and URL rather than inventing a
    mapping, and any other URL is echoed verbatim.
    """
    text = _text(value)
    if text is None:
        return None, None
    if not _is_url(text):
        return bundle_detect.licence_from_token(text)
    segments = [segment for segment in urlparse(text).path.split("/") if segment]
    if len(segments) < 2:
        return text, text
    if segments[0] == _LICENSES_SEGMENT:
        return _licensed_deed(text, segments[1])
    if segments[0] == _PUBLIC_DOMAIN_SEGMENT:
        display = _PUBLIC_DOMAIN_DISPLAYS.get(segments[1])
        if display is not None:
            return display, text
    return text, text


def record_from_page(facts: PageFacts, source_url: str, fetched_at: str) -> dict:
    """The cache record for one page: what it said, where and when. Nothing else.

    No cookie, no response body and no title: a cache entry exists to answer a
    later enrich call, not to rebuild the page.
    """
    return {
        "source_url": source_url,
        "fetched_at": fetched_at,
        "creator": facts.creator,
        "licence": facts.licence,
        "licence_url": facts.licence_url,
    }


def record_is_fresh(record: Optional[Mapping], now: datetime, ttl: int = ENRICHMENT_TTL_SECONDS) -> bool:
    """Whether a cache record is still inside ``ttl``; malformed is NOT fresh.

    A missing or unparseable ``fetched_at`` is re-fetched rather than trusted, so
    a damaged record cannot freeze a stale answer in place. A future stamp is
    fresh (a clock behind ours is not evidence of a bad record), and a ``ttl`` of
    zero or less disables the cache entirely.
    """
    if ttl <= 0 or not record:
        return False
    fetched = _fetched_at(record)
    if fetched is None:
        return False
    return _as_utc(now) - fetched <= timedelta(seconds=ttl)


def fills(
    creator: Optional[str],
    licence: Optional[str],
    licence_url: Optional[str],
    record: Optional[Mapping],
) -> Tuple[Dict[str, Optional[str]], Tuple[str, ...]]:
    """The row's final values, and which fields the page supplied and the row took.

    ``values`` always carries all three keys. A truthy row value is never
    overwritten and a value the record lacks is never invented. The licence and
    its URL are ONE field pair, taken only when the row has neither: a row with
    ``licence="cc-by"`` and ``licence_url=None`` keeps both, because a README
    token and a page link would name one licence and link another. ``fields`` is
    ordered ``creator``, ``licence``, ``licence_url`` and names ``licence_url``
    only when a URL was actually written.
    """
    values: Dict[str, Optional[str]] = {"creator": creator, "licence": licence, "licence_url": licence_url}
    fields: List[str] = []
    source = record or {}
    if not creator and source.get("creator"):
        values["creator"] = source["creator"]
        fields.append("creator")
    if not licence and not licence_url and source.get("licence"):
        values["licence"] = source["licence"]
        fields.append("licence")
        if source.get("licence_url"):
            values["licence_url"] = source["licence_url"]
            fields.append("licence_url")
    return values, tuple(fields)


def prune_records(records: Mapping[str, dict], limit: int = MAX_ENRICHMENT_ENTRIES) -> Dict[str, dict]:
    """Keep the newest ``limit`` records by ``fetched_at``, in key order.

    Tolerant by design: a record whose stamp cannot be parsed sorts oldest, so it
    is dropped first, and equal stamps are broken by key so pruning is a function
    of the mapping and not of iteration order.
    """
    if limit <= 0:
        return {}
    newest_first = sorted(records, key=lambda key: _freshness_order(key, records[key]))
    return {key: records[key] for key in sorted(newest_first[:limit])}


def provenance(record: Mapping, fields: Sequence[str]) -> dict:
    """The row's ``_bundle`` provenance block: where, when, and which fields.

    Nothing else: no cookie and no values, because the row already shows the
    values and the block exists to tell the vendor's word from a human's.
    """
    return {"source_url": record.get("source_url"), "fetched_at": record.get("fetched_at"), "fields": list(fields)}


def _licensed_deed(value: str, slug: str) -> Tuple[Optional[str], Optional[str]]:
    """The display from the shared table, the URL always the page's own string."""
    display, table_url = bundle_detect.licence_from_token("cc_" + slug.replace("-", "_"))
    if table_url is None:
        return value, value
    return display, value


def _text(value: Any) -> Optional[str]:
    """The value as text with outer whitespace trimmed, or ``None`` when it is blank."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _is_url(value: str) -> bool:
    """Whether ``value`` is an absolute URL; a malformed one is not."""
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return bool(parsed.scheme and parsed.netloc)


def _json_ld_objects(html: str) -> List[Any]:
    """Every parsed JSON-LD payload on the page; malformed blocks are ignored."""
    parser = _JsonLdParser()
    parser.feed(html or "")
    parser.close()
    payloads: List[Any] = []
    for block in parser.blocks:
        try:
            parsed = json.loads(block)
        except ValueError:
            continue
        if isinstance(parsed, (dict, list)):
            payloads.append(parsed)
    return payloads


def _creative_work_nodes(payloads: Sequence[Any]) -> List[dict]:
    """Every ``CreativeWork`` node, depth-first in document order."""
    nodes: List[dict] = []
    for payload in payloads:
        _collect_creative_works(payload, nodes)
    return nodes


def _collect_creative_works(value: Any, nodes: List[dict]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_creative_works(item, nodes)
        return
    if not isinstance(value, dict):
        return
    if _is_creative_work(value):
        nodes.append(value)
    for child in value.values():
        _collect_creative_works(child, nodes)


def _is_creative_work(node: Mapping) -> bool:
    node_type = node.get("@type")
    if isinstance(node_type, str):
        return node_type == _CREATIVE_WORK
    if isinstance(node_type, list):
        return _CREATIVE_WORK in node_type
    return False


def _author_name(node: Mapping) -> Optional[str]:
    """The first non-empty ``author.name`` of this node, whether author is one or many."""
    author = node.get("author")
    candidates = author if isinstance(author, list) else [author]
    for candidate in candidates:
        if isinstance(candidate, dict):
            name = _text(candidate.get("name"))
            if name is not None:
                return name
    return None


def _license_value(node: Mapping) -> Optional[str]:
    """This node's ``license`` as text; a dict value is read through its ``url``."""
    value = node.get("license")
    if isinstance(value, dict):
        value = value.get("url")
    return _text(value)


def _fetched_at(record: Any) -> Optional[datetime]:
    """A record's ``fetched_at`` as an aware UTC datetime, or ``None``."""
    if not isinstance(record, dict):
        return None
    return _parsed_stamp(record.get("fetched_at"))


def _parsed_stamp(value: Any) -> Optional[datetime]:
    """One ISO-8601 stamp as an aware datetime; a naive stamp is UTC."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _as_utc(stamp)


def _as_utc(stamp: datetime) -> datetime:
    """``stamp`` in UTC; a stamp without a tzinfo is treated as UTC."""
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp


def _freshness_order(key: str, record: Any) -> Tuple[int, float, str]:
    """Newest first, an unparseable stamp last, ties broken by key."""
    stamp = _fetched_at(record)
    if stamp is None:
        return (1, 0.0, key)
    return (0, -stamp.timestamp(), key)
