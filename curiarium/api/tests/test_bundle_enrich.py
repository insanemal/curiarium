"""Unit proofs for the pure enrichment vocabulary (Mf-nivh).

Nothing here touches a database, a client or a socket: `bundle_enrich` is handed
page bytes and a row's three values, so every rule is pinned from literals. The
only files read are the two Thingiverse page fixtures inside this module's tree, so the in-container build context carries them (AGENTS.md guardrail 3).

The heuristics pinned here:

* the owner's page yields P1lotz and CC BY-NC-SA, while the generic
  `import_metadata` parser would answer `Thingiverse.com` from `<meta author>`
  on the same bytes - the reason this module has its own parser;
* a page with no ld+json node is a refusal, not an empty success;
* a `by-nc-sa/3.0` deed URL is never re-versioned to the table's 4.0, because
  that would invent a legal claim the vendor never made;
* `Product.brand.name` is not a creator and `<meta author>` is never read;
* the licence display comes from `bundle_detect._LICENCE_TABLE` while the URL
  stays the page's own string, and an unknown deed echoes verbatim;
* a page never overwrites a human value, and the licence/licence_url pair is
  taken together or not at all;
* cache freshness is tolerant (malformed is stale, future is fresh) and pruning
  is bounded, newest-first and deterministic.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import bundle_enrich
from import_metadata import parse_page_metadata

FIXTURE_DIR = Path(__file__).parent.joinpath("fixtures/thingiverse")
# A fixed "now" so freshness is a function of the table below, not of the clock.
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def fixture(name: str) -> dict:
    """One page fixture: ``source_url``, ``html`` and its capture notes."""
    return json.loads(FIXTURE_DIR.joinpath(name).read_text())


def ld_json_page(payload) -> str:
    """A page whose head holds exactly one ld+json block and nothing else."""
    return '<html><head><script type="application/ld+json">' + json.dumps(payload) + "</script></head></html>"


def page_record(**overrides) -> dict:
    """A cache record in the shipped shape, overridden field by field."""
    record = {
        "source_url": "https://www.thingiverse.com/thing:5390184",
        "fetched_at": "2026-11-05T00:00:00+00:00",
        "creator": "P1lotz",
        "licence": "CC BY-NC-SA",
        "licence_url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
    }
    record.update(overrides)
    return record


# --- parse_thing_page ------------------------------------------------------


def test_the_owners_page_yields_its_creator_and_licence() -> None:
    page = fixture("thing-5390184.json")

    facts = bundle_enrich.parse_thing_page(page["html"])

    assert facts is not None
    assert facts.creator == "P1lotz"
    assert facts.licence == "CC BY-NC-SA"
    # The page's own deed URL, byte for byte.
    assert facts.licence_url == "https://creativecommons.org/licenses/by-nc-sa/4.0/"


def test_the_generic_page_parser_would_have_lied() -> None:
    page = fixture("thing-5390184.json")

    metadata = parse_page_metadata(page["html"], page["source_url"])

    # <meta name="author"> is the marketplace, not the maker. This is the reason
    # bundle_enrich parses the CreativeWork node itself instead of reusing it.
    assert metadata.creator == "Thingiverse.com"
    assert metadata.licence is None


def test_a_missing_thing_page_has_no_metadata() -> None:
    page = fixture("thing-missing.json")

    # A thing that does not exist answers 200 with a generic page and no ld+json,
    # so "no metadata" must be an explicit refusal and never an empty success.
    assert bundle_enrich.parse_thing_page(page["html"]) is None


def test_a_30_url_is_not_upgraded_to_40() -> None:
    page = ld_json_page(
        {
            "@type": "CreativeWork",
            "author": {"@type": "Person", "name": "P1lotz"},
            "license": "https://creativecommons.org/licenses/by-nc-sa/3.0/",
        }
    )

    facts = bundle_enrich.parse_thing_page(page)

    assert facts is not None
    assert facts.licence == "CC BY-NC-SA"
    # The mutation this pins: substituting the table's 4.0 URL would re-version
    # the vendor's licence, which is a legal claim they never made.
    assert facts.licence_url == "https://creativecommons.org/licenses/by-nc-sa/3.0/"


def test_a_brand_name_is_not_a_creator() -> None:
    page = ld_json_page(
        {
            "@type": "Product",
            "name": "A thing",
            "brand": {"@type": "Brand", "name": "Marketplace"},
            "mainEntityOfPage": {
                "@type": "CreativeWork",
                "license": "https://creativecommons.org/licenses/by/4.0/",
            },
        }
    )

    facts = bundle_enrich.parse_thing_page(page)

    assert facts is not None
    assert facts.creator is None, "Product.brand.name is the marketplace, never the person"
    assert facts.licence == "CC BY", "a CreativeWork node still supplies the licence"
    # A page that carries only the brand block has stated no metadata at all.
    assert bundle_enrich.parse_thing_page(ld_json_page({"@type": "Product", "brand": {"name": "Marketplace"}})) is None


def test_a_page_with_no_licence_leaves_it_null() -> None:
    page = ld_json_page({"@type": "CreativeWork", "author": {"@type": "Person", "name": "P1lotz"}})

    facts = bundle_enrich.parse_thing_page(page)

    assert facts is not None
    assert facts.creator == "P1lotz"
    assert facts.licence is None
    assert facts.licence_url is None


# --- licence_from_page -----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "display"),
    [
        ("https://creativecommons.org/licenses/by/3.0/", "CC BY"),
        ("https://creativecommons.org/licenses/by-sa/3.0/", "CC BY-SA"),
        ("https://creativecommons.org/licenses/by-nc/3.0/", "CC BY-NC"),
        ("https://creativecommons.org/licenses/by-nd/3.0/", "CC BY-ND"),
        ("https://creativecommons.org/licenses/by-nc-sa/3.0/", "CC BY-NC-SA"),
        ("https://creativecommons.org/licenses/by-nc-nd/3.0/", "CC BY-NC-ND"),
        ("https://creativecommons.org/publicdomain/zero/1.0/", "CC0"),
        ("https://creativecommons.org/publicdomain/mark/1.0/", "Public Domain Mark"),
        # An unknown deed maps to nothing: the page's string is echoed as both.
        ("https://creativecommons.org/licenses/unknown-deed/4.0/", "https://creativecommons.org/licenses/unknown-deed/4.0/"),
    ],
)
def test_a_cc_deed_url_maps_by_path_to_its_display(value: str, display: str) -> None:
    licence, licence_url = bundle_enrich.licence_from_page(value)

    assert licence == display
    # Always the page's own string: the table's hardcoded URL is never substituted.
    assert licence_url == value


def test_a_non_cc_licence_string_is_echoed_verbatim() -> None:
    # Not a URL, so it is a vendor token and the token table's domain owns it.
    assert bundle_enrich.licence_from_page("my-own-licence") == ("my-own-licence", None)
    # A known token still maps, URL included: the table is allowed to supply a
    # URL for a token, because a token is not a page's legal statement.
    assert bundle_enrich.licence_from_page("cc-nc") == ("CC BY-NC", "https://creativecommons.org/licenses/by-nc/4.0/")
    assert bundle_enrich.licence_from_page(None) == (None, None)
    assert bundle_enrich.licence_from_page("   ") == (None, None)


# --- fills -----------------------------------------------------------------


def test_a_present_creator_is_never_replaced() -> None:
    values, fields = bundle_enrich.fills("README Author", None, None, page_record(licence=None, licence_url=None))

    assert values == {"creator": "README Author", "licence": None, "licence_url": None}
    assert fields == (), "the page supplied a creator, but this row did not take it"


def test_a_readme_token_blocks_the_page_licence_pair() -> None:
    values, fields = bundle_enrich.fills(None, "cc-by", None, page_record())

    # The pair is taken together or not at all: a README token and a page link
    # would name one licence and link another.
    assert values == {"creator": "P1lotz", "licence": "cc-by", "licence_url": None}
    assert fields == ("creator",)


def test_the_licence_pair_is_written_together() -> None:
    values, fields = bundle_enrich.fills(None, None, None, page_record())

    assert values["licence"] == "CC BY-NC-SA"
    assert values["licence_url"] == "https://creativecommons.org/licenses/by-nc-sa/4.0/"
    assert fields[-2:] == ("licence", "licence_url")


def test_a_record_with_nothing_is_never_applied() -> None:
    record = page_record(creator=None, licence=None, licence_url=None)

    values, fields = bundle_enrich.fills(None, None, None, record)

    assert values == {"creator": None, "licence": None, "licence_url": None}
    assert fields == ()


def test_the_cache_record_carries_no_cookie_and_no_body() -> None:
    facts = bundle_enrich.PageFacts(creator="P1lotz", licence="CC BY-NC-SA", licence_url="https://example.test/deed")

    record = bundle_enrich.record_from_page(facts, "https://www.thingiverse.com/thing:5390184", "2026-11-05T00:00:00+00:00")

    assert record == {
        "source_url": "https://www.thingiverse.com/thing:5390184",
        "fetched_at": "2026-11-05T00:00:00+00:00",
        "creator": "P1lotz",
        "licence": "CC BY-NC-SA",
        "licence_url": "https://example.test/deed",
    }


def test_provenance_names_the_url_the_stamp_and_the_fields_taken() -> None:
    record = page_record()

    block = bundle_enrich.provenance(record, ("creator", "licence"))

    # The values are not copied: the row already shows them.
    assert block == {
        "source_url": "https://www.thingiverse.com/thing:5390184",
        "fetched_at": "2026-11-05T00:00:00+00:00",
        "fields": ["creator", "licence"],
    }


# --- record_is_fresh -------------------------------------------------------


@pytest.mark.parametrize(
    ("fetched_at", "ttl", "fresh"),
    [
        ((NOW - timedelta(hours=1)).isoformat(), bundle_enrich.ENRICHMENT_TTL_SECONDS, True),
        ("2026-11-05T11:00:00Z", bundle_enrich.ENRICHMENT_TTL_SECONDS, True),  # trailing Z is accepted
        ("2026-11-05T11:00:00", bundle_enrich.ENRICHMENT_TTL_SECONDS, True),  # naive is read as UTC
        ((NOW - timedelta(days=8)).isoformat(), bundle_enrich.ENRICHMENT_TTL_SECONDS, False),
        ("not-a-stamp", bundle_enrich.ENRICHMENT_TTL_SECONDS, False),  # malformed is re-fetched
        (None, bundle_enrich.ENRICHMENT_TTL_SECONDS, False),  # missing is re-fetched
        ((NOW + timedelta(days=1)).isoformat(), bundle_enrich.ENRICHMENT_TTL_SECONDS, True),  # future is fresh
        ((NOW - timedelta(hours=1)).isoformat(), 0, False),  # a disabled cache trusts nothing
    ],
)
def test_a_fresh_record_is_fresh_and_a_stale_one_is_not(fetched_at, ttl: int, fresh: bool) -> None:
    record = {} if fetched_at is None else {"fetched_at": fetched_at}

    assert bundle_enrich.record_is_fresh(record, NOW, ttl) is fresh


# --- prune_records ---------------------------------------------------------


def test_the_cache_is_bounded_and_drops_the_oldest() -> None:
    records = {
        "thingiverse:1": {"fetched_at": "2026-11-01T00:00:00+00:00"},
        "thingiverse:2": {"fetched_at": "2026-11-03T00:00:00+00:00"},
        "thingiverse:3": {"fetched_at": "not-a-stamp"},
    }

    pruned = bundle_enrich.prune_records(records, limit=2)

    # Newest first; the garbage stamp sorts oldest and is dropped first.
    assert pruned == {
        "thingiverse:1": {"fetched_at": "2026-11-01T00:00:00+00:00"},
        "thingiverse:2": {"fetched_at": "2026-11-03T00:00:00+00:00"},
    }
    assert list(pruned) == ["thingiverse:1", "thingiverse:2"], "the result is in key order"
    assert bundle_enrich.prune_records(records, limit=3) == records


# --- cache_key -------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_url", "key"),
    [
        ("https://www.thingiverse.com/thing:5390184", "thingiverse:5390184"),
        ("https://www.thingiverse.com/things/5390184", "thingiverse:5390184"),
        ("https://www.printables.com/model/12345-planter", None),
        ("   ", None),
        (None, None),
    ],
)
def test_a_cache_key_is_the_vendor_and_the_id(source_url, key) -> None:
    assert bundle_enrich.cache_key(source_url) == key
