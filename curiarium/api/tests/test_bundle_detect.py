"""Unit proofs for the pure bundle vocabulary (Mf-vwqf).

Nothing here touches disk, a database or HTTP: `bundle_detect` takes a listing
and the documents the caller already opened, so every rule is pinned from
literals. The one exception is the drift test, which imports `library_scan` and
`config` to prove this module reuses *their* constants instead of keeping a
quiet second copy.

The heuristics pinned here:

* `classify_entry` is suffix-only, including the upper-case suffix and the
  two-files-one-stem regression that the Mf-tkdp bug was about;
* the model and image suffix sets are the shipped ones;
* both Thingiverse parsers, their negative shapes and the licence table
  (including "no invented URL");
* `display_name` and `twin_key`;
* the rule order R1 -> R2 -> R3 -> not-a-bundle, the marker-less README falling
  to low confidence, the sibling pair in both its file and directory shapes,
  R3's model depth bound (its three accepted and rejected shapes, and R1 still
  accepting a bundle whose models live under ``files/``), and the cover pick
  including its tie-break.
"""

from typing import Dict, Mapping

import pytest

import bundle_detect
import library_scan
from config import IMPORT_ALLOWED_EXTS

README_TEXT = "Moai statue -No overhang\nCreated by Thingiverse user moai_maker\nLicensed under CC BY-NC\n"
LICENSE_TEXT = (
    "Moai statue -No overhang by moai_maker on Thingiverse: https://www.thingiverse.com/thing:2493386\n"
)
THING_URL = "https://www.thingiverse.com/thing:2493386"

# The two real Thingiverse exports from the ticket. Their markers are shipped in
# either document, so the tests below swap them and expect one identical bundle.
CAT_NAME = "2-color_standing_cat_62536"
CAT_CREATED_BY = "This thing was created by Thingiverse user nervoussystem, and is licensed under cc-nc-sa.\n"
CAT_TITLE_LINE = "2-color standing cat by nervoussystem on Thingiverse: https://www.thingiverse.com/thing:62536\n"
CAT_URL = "https://www.thingiverse.com/thing:62536"
JULIEN_CREATED_BY = "This thing was created by Thingiverse user Julien_DaCosta, and is licensed under cc-nc.\n"
JULIEN_TITLE_LINE = "Moai statue -No overhang by Julien_DaCosta on Thingiverse: https://www.thingiverse.com/thing:2493386\n"


def snapshot(bundle_name: str, files: Mapping[str, int]) -> bundle_detect.BundleSnapshot:
    """A snapshot from a {bundle-relative path: size} mapping."""
    return bundle_detect.BundleSnapshot(
        bundle_name=bundle_name,
        rel_dir=bundle_name,
        entries=tuple(bundle_detect.BundleEntry(rel_path=path, size=size) for path, size in files.items()),
    )


def r1_docs() -> Dict[str, str]:
    return {"README.txt": README_TEXT, "LICENSE.txt": LICENSE_TEXT}


def provenance(facts: bundle_detect.BundleFacts) -> tuple:
    """The provenance attributes that must not depend on which document held what."""
    return (
        facts.detector,
        facts.confidence,
        facts.metadata_shape,
        facts.title,
        facts.creator,
        facts.licence,
        facts.licence_url,
        facts.source_url,
        facts.thing_id,
    )


# --- classify_entry ---------------------------------------------------------


@pytest.mark.parametrize(
    ("rel_path", "role"),
    [
        ("part.stl", bundle_detect.MODEL),
        ("part.STL", bundle_detect.MODEL),
        ("part.3MF", bundle_detect.MODEL),
        ("part.step", bundle_detect.MODEL),
        ("bundle.zip", bundle_detect.ARCHIVE),
        ("bundle.ZIP", bundle_detect.ARCHIVE),
        ("notes.txt", bundle_detect.TEXT),
        ("guide.md", bundle_detect.TEXT),
        ("manual.pdf", bundle_detect.TEXT),
        ("page.html", bundle_detect.TEXT),
        ("preview.jpg", bundle_detect.IMAGE),
        ("preview.PNG", bundle_detect.IMAGE),
        ("preview.webp", bundle_detect.IMAGE),
        ("README", bundle_detect.OTHER),
        ("archive.7z", bundle_detect.OTHER),
        ("", bundle_detect.OTHER),
    ],
)
def test_classify_entry_reads_the_suffix_only(rel_path: str, role: str) -> None:
    assert bundle_detect.classify_entry(rel_path, 0) == role


def test_two_files_sharing_a_stem_are_two_model_entries() -> None:
    """The Mf-tkdp regression: identity is the path, never the stem."""
    files = {"files/part.stl": 10, "files/part.3mf": 20, "cover.jpg": 30, "notes.txt": 40}
    facts = bundle_detect.detect_bundle(snapshot("two-stems", files), {})

    assert facts is not None
    assert facts.counts.model == 2, "both same-stem files survive classification"
    assert facts.model_entries == ("files/part.3mf", "files/part.stl")
    assert len(set(facts.model_entries)) == 2


def test_r3_accepts_a_model_at_depth_one_under_files() -> None:
    files = {"files/part.stl": 10, "cover.jpg": 20, "notes.txt": 30}

    facts = bundle_detect.detect_bundle(snapshot("files-shape", files), {})

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_GENERIC
    assert facts.confidence == bundle_detect.CONFIDENCE_LOW


def test_r3_accepts_the_depth_zero_trio_unchanged() -> None:
    files = {"part.stl": 10, "cover.jpg": 20, "notes.txt": 30}

    facts = bundle_detect.detect_bundle(snapshot("root-trio", files), {})

    assert facts is not None
    assert facts.confidence == bundle_detect.CONFIDENCE_LOW


def test_r3_rejects_a_model_under_an_unknown_directory() -> None:
    files = {"stl/part.stl": 10, "cover.jpg": 20, "notes.txt": 30}

    assert bundle_detect.detect_bundle(snapshot("stl-shape", files), {}) is None


def test_r3_rejects_a_model_below_depth_one() -> None:
    files = {"files/deep/part.stl": 10, "cover.jpg": 20, "notes.txt": 30}

    assert bundle_detect.detect_bundle(snapshot("deep-shape", files), {}) is None


def test_the_depth_bound_does_not_touch_r1() -> None:
    """A real bundle whose models live at depth 1 under files/ still parses HIGH."""
    files = {"README.txt": 100, "LICENSE.txt": 100, "files/part.stl": 200, "images/a.jpg": 300}

    facts = bundle_detect.detect_bundle(snapshot("2493386_Moai_statue_-No_overhang", files), r1_docs())

    assert facts is not None
    assert facts.confidence == bundle_detect.CONFIDENCE_HIGH
    assert facts.counts.model == 1


# --- shared constants -------------------------------------------------------


def test_the_suffix_tables_are_the_shared_ones() -> None:
    """Proves the reuse: a second private table would silently drift."""
    assert bundle_detect.MODEL_SUFFIXES == frozenset(IMPORT_ALLOWED_EXTS - {".zip"})
    assert bundle_detect.IMAGE_SUFFIXES == library_scan.IMAGE_SUFFIXES


# --- parse_thingiverse_readme ----------------------------------------------


def test_parse_thingiverse_readme_reads_both_markers() -> None:
    creator, token = bundle_detect.parse_thingiverse_readme(README_TEXT)

    assert creator == "moai_maker"
    assert token == "CC BY-NC"


def test_parse_thingiverse_readme_without_markers_is_empty() -> None:
    assert bundle_detect.parse_thingiverse_readme("Thanks for downloading my model. Enjoy!") == (None, None)
    assert bundle_detect.parse_thingiverse_readme("") == (None, None)


def test_parse_thingiverse_readme_reports_each_half_independently() -> None:
    creator, token = bundle_detect.parse_thingiverse_readme("created by Thingiverse user ada\n")

    assert creator == "ada"
    assert token is None, "a creator without a licence must not invent a licence"


# --- parse_thingiverse_license ---------------------------------------------


def test_parse_thingiverse_license_reads_title_creator_id_and_url() -> None:
    parsed = bundle_detect.parse_thingiverse_license(LICENSE_TEXT)

    assert parsed == ("Moai statue -No overhang", "moai_maker", "2493386", THING_URL)


def test_parse_thingiverse_license_without_the_line_is_empty() -> None:
    assert bundle_detect.parse_thingiverse_license("All rights reserved.") is None
    assert bundle_detect.parse_thingiverse_license("") is None


def test_parse_thingiverse_license_trims_trailing_punctuation_from_the_url() -> None:
    parsed = bundle_detect.parse_thingiverse_license(
        "Spoon by ada on Thingiverse: https://www.thingiverse.com/thing:77001).\n"
    )

    assert parsed is not None
    assert parsed[3] == "https://www.thingiverse.com/thing:77001"
    assert parsed[2] == "77001"


# --- licence_from_token ----------------------------------------------------


@pytest.mark.parametrize(
    ("token", "display", "url"),
    [
        ("cc-by", "CC BY", "https://creativecommons.org/licenses/by/4.0/"),
        ("CC BY", "CC BY", "https://creativecommons.org/licenses/by/4.0/"),
        ("cc-by-sa", "CC BY-SA", "https://creativecommons.org/licenses/by-sa/4.0/"),
        ("cc-by-nc", "CC BY-NC", "https://creativecommons.org/licenses/by-nc/4.0/"),
        ("cc-nc", "CC BY-NC", "https://creativecommons.org/licenses/by-nc/4.0/"),
        ("cc-by-nc-sa", "CC BY-NC-SA", "https://creativecommons.org/licenses/by-nc-sa/4.0/"),
        ("cc-nc-sa", "CC BY-NC-SA", "https://creativecommons.org/licenses/by-nc-sa/4.0/"),
        ("cc-by-nd", "CC BY-ND", "https://creativecommons.org/licenses/by-nd/4.0/"),
        ("cc-by-nc-nd", "CC BY-NC-ND", "https://creativecommons.org/licenses/by-nc-nd/4.0/"),
        ("cc-nc-nd", "CC BY-NC-ND", "https://creativecommons.org/licenses/by-nc-nd/4.0/"),
        ("cc0", "CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
        ("cc-zero", "CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
        ("public-domain", "CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
    ],
)
def test_licence_from_token_maps_every_known_token(token: str, display: str, url: str) -> None:
    assert bundle_detect.licence_from_token(token) == (display, url)


def test_licence_from_token_never_invents_a_url() -> None:
    assert bundle_detect.licence_from_token("WTFPL") == ("WTFPL", None)
    assert bundle_detect.licence_from_token(None) == (None, None)


# --- display_name / twin_key ----------------------------------------------


def test_display_name_strips_the_ordinal_and_the_underscores() -> None:
    assert bundle_detect.display_name("2493386_Moai_statue_-No_overhang") == "Moai statue -No overhang"


@pytest.mark.parametrize("name", ["", "   ", "_", "123_"])
def test_display_name_is_never_empty(name: str) -> None:
    assert bundle_detect.display_name(name)


def test_twin_key_casefolds_and_drops_one_trailing_zip() -> None:
    assert bundle_detect.twin_key("Moai_Statue.zip") == "moai_statue"
    assert bundle_detect.twin_key("  MOAI_STATUE.ZIP ") == "moai_statue"
    assert bundle_detect.twin_key("Moai_Statue") == "moai_statue"
    assert bundle_detect.twin_key("archive.zip.zip") == "archive.zip", "only one suffix is removed"


# --- detect_bundle rules ---------------------------------------------------


def test_r1_wins_over_r3_on_the_same_snapshot() -> None:
    files = {"README.txt": 100, "LICENSE.txt": 100, "part.stl": 200, "cover.jpg": 300, "notes.txt": 50}
    facts = bundle_detect.detect_bundle(snapshot("2493386_Moai_statue_-No_overhang", files), r1_docs())

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_THINGIVERSE_README
    assert facts.confidence == bundle_detect.CONFIDENCE_HIGH
    assert facts.metadata_shape == bundle_detect.SHAPE_ROOT_DOCS
    assert facts.counts.model == 1 and facts.counts.image == 1 and facts.counts.text == 3
    assert facts.title == "Moai statue -No overhang"
    assert facts.creator == "moai_maker"
    assert facts.licence == "CC BY-NC"
    assert facts.licence_url == "https://creativecommons.org/licenses/by-nc/4.0/"
    assert facts.source_url == THING_URL
    assert facts.thing_id == "2493386"
    assert facts.description is not None and "moai_maker" in facts.description
    assert facts.cover_entry == "cover.jpg"


def test_a_readme_without_markers_falls_to_low_confidence() -> None:
    """Acceptance: a vendor README alone must not become "high confidence"."""
    files = {"README.txt": 100, "LICENSE.txt": 100, "part.stl": 200, "cover.jpg": 300}
    docs = {"README.txt": "Thanks for downloading! Print at 0.2mm.", "LICENSE.txt": "All rights reserved."}

    facts = bundle_detect.detect_bundle(snapshot("Generic Thing", files), docs)

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_GENERIC
    assert facts.confidence == bundle_detect.CONFIDENCE_LOW
    assert facts.metadata_shape == bundle_detect.SHAPE_GENERIC
    assert facts.title == "Generic Thing"
    assert facts.creator is None
    assert facts.licence is None
    assert facts.licence_url is None
    assert facts.source_url is None
    assert facts.thing_id is None
    assert facts.description is None


def test_r2_accepts_the_sibling_pair_as_files() -> None:
    files = {"Moai_README": 100, "Moai_LICENSE": 100, "part.stl": 200, "cover.jpg": 300}
    docs = {"Moai_README": README_TEXT, "Moai_LICENSE": LICENSE_TEXT}

    facts = bundle_detect.detect_bundle(snapshot("Moai", files), docs)

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_THINGIVERSE_SIBLING
    assert facts.confidence == bundle_detect.CONFIDENCE_HIGH
    assert facts.metadata_shape == bundle_detect.SHAPE_SIBLING_DOCS
    assert facts.creator == "moai_maker"
    assert facts.source_url == THING_URL


def test_r2_accepts_the_legacy_directory_shape() -> None:
    files = {"Moai_README/readme.txt": 100, "Moai_LICENSE/license.txt": 100, "part.stl": 200, "cover.jpg": 300}
    docs = {"Moai_README/readme.txt": README_TEXT, "Moai_LICENSE/license.txt": LICENSE_TEXT}

    assert bundle_detect.doc_entry_candidates(snapshot("Moai", files))[:2] == (
        "Moai_README/readme.txt",
        "Moai_LICENSE/license.txt",
    )
    facts = bundle_detect.detect_bundle(snapshot("Moai", files), docs)

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_THINGIVERSE_SIBLING
    assert facts.confidence == bundle_detect.CONFIDENCE_HIGH
    assert facts.thing_id == "2493386"


def test_a_literal_only_bundle_is_not_a_bundle() -> None:
    files = {"README.txt": 100, "LICENSE.txt": 100, "manual.pdf": 100}

    assert bundle_detect.detect_bundle(snapshot("Docs Only", files), {}) is None


def test_cover_is_the_largest_image_ties_broken_by_path() -> None:
    files = {"part.stl": 10, "notes.txt": 10, "images/b.jpg": 900, "a.jpg": 900, "c.jpg": 100}

    facts = bundle_detect.detect_bundle(snapshot("generic", files), {})

    assert facts is not None
    assert facts.cover_entry == "a.jpg", "equal sizes fall back to the lexicographic path"


def test_cover_is_none_without_an_image() -> None:
    files = {"Moai_README": 100, "Moai_LICENSE": 100, "part.stl": 200}
    facts = bundle_detect.detect_bundle(
        snapshot("Moai", files), {"Moai_README": README_TEXT, "Moai_LICENSE": LICENSE_TEXT}
    )

    assert facts is not None
    assert facts.cover_entry is None


def test_doc_candidates_stop_at_the_root_pair_for_an_r1_bundle() -> None:
    files = {"README.txt": 100, "LICENSE.txt": 100, "part.stl": 200, "notes.txt": 50}

    assert bundle_detect.doc_entry_candidates(snapshot("Moai", files)) == ("README.txt", "LICENSE.txt")


def test_doc_candidates_are_bounded_and_deduplicated() -> None:
    files = {"readme.txt": 100, "license.txt": 100, "Moai_README": 100, "Moai_LICENSE": 100, "notes.md": 50}

    candidates = bundle_detect.doc_entry_candidates(snapshot("Moai", files))

    assert len(candidates) <= bundle_detect.MAX_DOC_CANDIDATES
    assert len(set(candidates)) == len(candidates)
    assert candidates[0] == "readme.txt"


def test_generic_doc_candidates_are_the_first_text_entry() -> None:
    files = {"part.stl": 10, "zeta.txt": 10, "Alpha.md": 10}

    assert bundle_detect.generic_doc_candidates(snapshot("generic", files)) == ("Alpha.md",)
    assert bundle_detect.generic_doc_candidates(snapshot("generic", {"part.stl": 10})) == ()


@pytest.mark.parametrize("docs", [{}, {"README.txt": ""}], ids=["no-docs", "empty-doc"])
def test_a_missing_or_empty_document_is_never_high_confidence(docs: Mapping[str, str]) -> None:
    files = {"README.txt": 100, "LICENSE.txt": 100, "part.stl": 200, "cover.jpg": 300}
    facts = bundle_detect.detect_bundle(snapshot("thing", files), docs)

    assert facts is not None
    assert facts.confidence == bundle_detect.CONFIDENCE_LOW
    assert facts.creator is None


def test_a_readme_that_parses_without_a_licence_is_not_r1() -> None:
    files = {"README.txt": 100, "LICENSE.txt": 100, "part.stl": 200, "cover.jpg": 300}
    docs = {
        "README.txt": "created by Thingiverse user ada\n",
        "LICENSE.txt": "Licensed under CC BY\n",
    }

    facts = bundle_detect.detect_bundle(snapshot("thing", files), docs)

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_GENERIC, "the LICENSE line is missing its title/URL"


# --- the real ticket documents: layout independence -------------------------


def test_the_ticket_julien_documents_parse() -> None:
    assert bundle_detect.parse_thingiverse_readme(JULIEN_CREATED_BY) == ("Julien_DaCosta", "cc-nc")
    assert bundle_detect.parse_thingiverse_license(JULIEN_TITLE_LINE) == (
        "Moai statue -No overhang",
        "Julien_DaCosta",
        "2493386",
        "https://www.thingiverse.com/thing:2493386",
    )


def test_the_canonical_root_pair_reads_created_by_and_title_line() -> None:
    files = {"README.txt": 100, "LICENSE.txt": 100, "files/part.stl": 200, "images/1.jpg": 300}
    docs = {"README.txt": CAT_CREATED_BY, "LICENSE.txt": CAT_TITLE_LINE}

    facts = bundle_detect.detect_bundle(snapshot(CAT_NAME, files), docs)

    assert facts is not None
    assert provenance(facts) == (
        bundle_detect.DETECTOR_THINGIVERSE_README,
        bundle_detect.CONFIDENCE_HIGH,
        bundle_detect.SHAPE_ROOT_DOCS,
        "2-color standing cat",
        "nervoussystem",
        "CC BY-NC-SA",
        "https://creativecommons.org/licenses/by-nc-sa/4.0/",
        CAT_URL,
        "62536",
    )
    assert facts.description is not None
    assert facts.description.startswith(CAT_CREATED_BY.strip())


def test_the_swapped_root_pair_is_the_same_bundle() -> None:
    files = {"README.txt": 100, "LICENSE.txt": 100, "files/part.stl": 200, "images/1.jpg": 300}
    canonical = bundle_detect.detect_bundle(snapshot(CAT_NAME, files), {"README.txt": CAT_CREATED_BY, "LICENSE.txt": CAT_TITLE_LINE})
    swapped = bundle_detect.detect_bundle(snapshot(CAT_NAME, files), {"README.txt": CAT_TITLE_LINE, "LICENSE.txt": CAT_CREATED_BY})

    assert canonical is not None and swapped is not None
    assert provenance(swapped) == provenance(canonical)
    assert (
        swapped.title,
        swapped.creator,
        swapped.licence,
        swapped.licence_url,
        swapped.source_url,
        swapped.thing_id,
    ) == (
        "2-color standing cat",
        "nervoussystem",
        "CC BY-NC-SA",
        "https://creativecommons.org/licenses/by-nc-sa/4.0/",
        CAT_URL,
        "62536",
    )
    assert swapped.description == canonical.description, "the description follows the created-by document"


def test_one_root_document_carrying_both_markers_is_high() -> None:
    files = {"README.txt": 100, "files/part.stl": 200, "images/1.jpg": 300}
    docs = {"README.txt": CAT_TITLE_LINE + CAT_CREATED_BY}

    facts = bundle_detect.detect_bundle(snapshot(CAT_NAME, files), docs)

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_THINGIVERSE_README
    assert facts.confidence == bundle_detect.CONFIDENCE_HIGH
    assert facts.metadata_shape == bundle_detect.SHAPE_ROOT_DOCS
    assert (facts.title, facts.creator, facts.source_url, facts.thing_id) == (
        "2-color standing cat",
        "nervoussystem",
        CAT_URL,
        "62536",
    )


def test_a_mid_line_title_does_not_keep_the_preceding_sentence() -> None:
    files = {"README.txt": 100, "LICENSE.txt": 100, "files/part.stl": 200, "images/1.jpg": 300}
    docs = {"README.txt": "Notes. " + CAT_TITLE_LINE, "LICENSE.txt": CAT_CREATED_BY}

    facts = bundle_detect.detect_bundle(snapshot(CAT_NAME, files), docs)

    assert facts is not None
    assert facts.confidence == bundle_detect.CONFIDENCE_HIGH
    assert facts.title == "2-color standing cat", "the prose sentence before the marker is not the title"


def test_the_license_document_alone_can_carry_the_whole_root_pair() -> None:
    files = {"LICENSE.txt": 100, "files/part.stl": 200, "images/1.jpg": 300}
    docs = {"LICENSE.txt": CAT_TITLE_LINE + CAT_CREATED_BY}

    facts = bundle_detect.detect_bundle(snapshot(CAT_NAME, files), docs)

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_THINGIVERSE_README
    assert facts.confidence == bundle_detect.CONFIDENCE_HIGH
    assert facts.metadata_shape == bundle_detect.SHAPE_ROOT_DOCS
    assert facts.title == "2-color standing cat"


def test_the_swapped_sibling_files_pair_is_the_same_bundle() -> None:
    files = {"Cat_README": 100, "Cat_LICENSE": 100, "part.stl": 200, "cover.jpg": 300}
    canonical = bundle_detect.detect_bundle(snapshot("Cat", files), {"Cat_README": CAT_CREATED_BY, "Cat_LICENSE": CAT_TITLE_LINE})
    swapped = bundle_detect.detect_bundle(snapshot("Cat", files), {"Cat_README": CAT_TITLE_LINE, "Cat_LICENSE": CAT_CREATED_BY})

    assert canonical is not None and swapped is not None
    assert provenance(swapped) == provenance(canonical)
    assert swapped.detector == bundle_detect.DETECTOR_THINGIVERSE_SIBLING
    assert swapped.confidence == bundle_detect.CONFIDENCE_HIGH
    assert swapped.metadata_shape == bundle_detect.SHAPE_SIBLING_DOCS
    assert (swapped.title, swapped.creator, swapped.source_url) == ("2-color standing cat", "nervoussystem", CAT_URL)
    assert canonical.creator == "nervoussystem", "the canonical sibling case still parses"


def test_the_swapped_sibling_directories_pair_is_the_same_bundle() -> None:
    files = {
        "Cat_README/readme.txt": 100,
        "Cat_LICENSE/license.txt": 100,
        "part.stl": 200,
        "cover.jpg": 300,
    }
    docs = {"Cat_README/readme.txt": CAT_TITLE_LINE, "Cat_LICENSE/license.txt": CAT_CREATED_BY}
    view = snapshot("Cat", files)

    assert bundle_detect.doc_entry_candidates(view)[:2] == (
        "Cat_README/readme.txt",
        "Cat_LICENSE/license.txt",
    )
    facts = bundle_detect.detect_bundle(view, docs)

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_THINGIVERSE_SIBLING
    assert facts.confidence == bundle_detect.CONFIDENCE_HIGH
    assert facts.metadata_shape == bundle_detect.SHAPE_SIBLING_DOCS
    assert (facts.title, facts.creator, facts.source_url) == ("2-color standing cat", "nervoussystem", CAT_URL)


def test_one_sibling_document_carrying_both_markers_is_high() -> None:
    files = {"Cat_README": 100, "part.stl": 200, "cover.jpg": 300}
    docs = {"Cat_README": CAT_TITLE_LINE + CAT_CREATED_BY}

    facts = bundle_detect.detect_bundle(snapshot("Cat", files), docs)

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_THINGIVERSE_SIBLING
    assert facts.confidence == bundle_detect.CONFIDENCE_HIGH
    assert facts.metadata_shape == bundle_detect.SHAPE_SIBLING_DOCS
    assert (facts.title, facts.creator, facts.source_url) == (
        "2-color standing cat",
        "nervoussystem",
        CAT_URL,
    )


def test_the_title_line_creator_is_the_fallback_when_no_created_by_exists() -> None:
    """The fallback path, a new low->high promotion the old name-keyed parser
    could never produce: no document carries a created-by sentence, so the title
    line's creator supplies the creator, and the pair therefore has no created-by
    document to describe the bundle.
    """
    files = {"README.txt": 100, "LICENSE.txt": 100, "files/part.stl": 200, "images/1.jpg": 300}
    docs = {
        "README.txt": "Licensed under cc-by.\n",
        "LICENSE.txt": "Gizmo by ada on Thingiverse: https://www.thingiverse.com/thing:7\n",
    }

    facts = bundle_detect.detect_bundle(snapshot("Gizmo", files), docs)

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_THINGIVERSE_README
    assert facts.confidence == bundle_detect.CONFIDENCE_HIGH
    assert facts.metadata_shape == bundle_detect.SHAPE_ROOT_DOCS
    assert facts.title == "Gizmo"
    assert facts.creator == "ada", "the title-line creator is the fallback"
    assert facts.licence == "CC BY"
    assert facts.licence_url == "https://creativecommons.org/licenses/by/4.0/"
    assert facts.source_url == "https://www.thingiverse.com/thing:7"
    assert facts.thing_id == "7"
    assert facts.description is None, "the fallback has no created-by document, so it must not invent one"


INCOMPLETE_PAIR_FILES = {
    "README.txt": 100,
    "LICENSE.txt": 100,
    "Thing_README": 100,
    "part.stl": 200,
    "cover.jpg": 300,
}


@pytest.mark.parametrize(
    "docs",
    [
        pytest.param({"README.txt": "This thing was created by Thingiverse user ada.\n"}, id="creator-only"),
        pytest.param({"README.txt": "Licensed under cc-nc-sa.\n"}, id="token-only"),
        pytest.param({"LICENSE.txt": CAT_TITLE_LINE}, id="title-line-only"),
        pytest.param({"README.txt": CAT_CREATED_BY}, id="creator-and-token"),
        pytest.param(
            {
                "README.txt": "This thing was created by Thingiverse user ada, and is shared here.\n",
                "LICENSE.txt": "Spoon by ada on Thingiverse:\n",
            },
            id="creator-and-title-without-url",
        ),
        pytest.param(
            {"README.txt": "Licensed under cc-nc-sa.\n", "Thing_README": CAT_TITLE_LINE},
            id="no-created-by-token-and-title-in-separate-pairs",
        ),
        pytest.param(
            {"README.txt": CAT_CREATED_BY, "LICENSE.txt": "Spoon by ada on Thingiverse:\n"},
            id="creator-token-and-title-without-url",
        ),
    ],
)
def test_an_incomplete_named_pair_stays_low_and_invents_nothing(docs: Mapping[str, str]) -> None:
    """No pair missing a field is high, and the low row fills no provenance field.

    The title line and the token live in different named pairs in one case: a
    title line alone carries a creator fallback, so a same-pair token+title line
    with no created-by sentence is actually complete - see
    ``test_the_title_line_creator_is_the_fallback_when_no_created_by_exists``.
    """
    facts = bundle_detect.detect_bundle(snapshot("Thing", INCOMPLETE_PAIR_FILES), docs)

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_GENERIC
    assert facts.confidence == bundle_detect.CONFIDENCE_LOW
    assert (
        facts.creator,
        facts.licence,
        facts.licence_url,
        facts.source_url,
        facts.thing_id,
        facts.description,
    ) == (None, None, None, None, None, None)


def test_a_generic_notes_file_with_both_markers_stays_low() -> None:
    """Deliberate: merging is scoped to a named pair, so a stray prose file cannot
    manufacture provenance."""
    files = {"notes.txt": 100, "part.stl": 200, "cover.jpg": 300}
    docs = {"notes.txt": CAT_TITLE_LINE + CAT_CREATED_BY}

    facts = bundle_detect.detect_bundle(snapshot("Stray Notes", files), docs)

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_GENERIC
    assert facts.confidence == bundle_detect.CONFIDENCE_LOW
    assert (
        facts.creator,
        facts.licence,
        facts.licence_url,
        facts.source_url,
        facts.thing_id,
        facts.description,
    ) == (None, None, None, None, None, None)


def test_a_title_that_contains_by_keeps_it_at_parse_level() -> None:
    parsed = bundle_detect.parse_thingiverse_license("Spoon by ada by bob on Thingiverse: https://www.thingiverse.com/thing:9")

    assert parsed == ("Spoon by ada", "bob", "9", "https://www.thingiverse.com/thing:9")


def test_a_title_that_contains_by_keeps_it_through_detection() -> None:
    files = {"README.txt": 100, "LICENSE.txt": 100, "part.stl": 200, "cover.jpg": 300}
    docs = {
        "README.txt": "This thing was created by Thingiverse user bob, and is licensed under cc-nc.\n",
        "LICENSE.txt": "Spoon by ada by bob on Thingiverse: https://www.thingiverse.com/thing:9\n",
    }

    facts = bundle_detect.detect_bundle(snapshot("Spoon", files), docs)

    assert facts is not None
    assert facts.title == "Spoon by ada"
    assert facts.creator == "bob"


def test_a_bare_dot_in_a_version_is_not_a_sentence_boundary() -> None:
    parsed = bundle_detect.parse_thingiverse_license("Bracket v1.2 by ada on Thingiverse: https://www.thingiverse.com/thing:12")

    assert parsed is not None
    assert parsed[0] == "Bracket v1.2"


def test_parse_thingiverse_license_drops_a_preceding_sentence() -> None:
    parsed = bundle_detect.parse_thingiverse_license("Notes. " + CAT_TITLE_LINE)

    assert parsed is not None
    assert parsed[0] == "2-color standing cat"


def test_a_title_containing_a_sentence_boundary_is_a_known_limitation() -> None:
    """Accepted and documented in the parser docstring/ADR, not a bug to fix.

    A genuine title with ``". "`` loses its leading part because that whitespace
    is indistinguishable from a sentence of prose sitting before the marker.
    """
    parsed = bundle_detect.parse_thingiverse_license("Dr. No Replica by ada on Thingiverse: https://www.thingiverse.com/thing:14")

    assert parsed is not None
    assert parsed[0] == "No Replica"


def test_the_license_line_does_not_need_a_line_start() -> None:
    parsed = bundle_detect.parse_thingiverse_license("Thanks for downloading.\n" + CAT_TITLE_LINE)

    assert parsed is not None
    assert parsed[0] == "2-color standing cat"


def test_all_four_named_documents_fit_the_candidate_cap() -> None:
    files = {
        "README.txt": 100,
        "LICENSE.txt": 100,
        "Mixed_README": 100,
        "Mixed_LICENSE": 100,
        "part.stl": 200,
    }

    candidates = bundle_detect.doc_entry_candidates(snapshot("Mixed", files))

    assert bundle_detect.MAX_DOC_CANDIDATES == 4
    assert candidates == ("README.txt", "LICENSE.txt", "Mixed_README", "Mixed_LICENSE")
    # At the old cap of three the fourth named document was dropped, which starved
    # the sibling merge whenever the root pair could not complete.
    assert "Mixed_LICENSE" not in candidates[:3]
