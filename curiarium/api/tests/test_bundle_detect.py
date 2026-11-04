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
* `display_name` and `twin_key`, and `thingiverse_id_from_name` with its digit
  floor, mandatory separator and start anchor;
* the rule order R1 -> R2 -> R4 -> R3 -> not-a-bundle, the marker-less README
  falling to low confidence, the sibling pair in both its file and directory
  shapes, R4's `medium` reading with the name-derived URL and no invented
  attribution, R4 yielding to both high rules, and R3's model depth bound (the
  depth-0, named-directory and single-subdirectory shapes it accepts, the
  depth-2 and multi-subdirectory shapes it refuses, and R1 still accepting a
  bundle whose models live under ``files/``), and the cover pick including its
  tie-break.
"""

import re
from typing import Dict, List, Mapping, Optional, Tuple

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


def test_r3_accepts_a_model_under_one_conventional_directory() -> None:
    """``STL/`` is the shape Cinderwing3D's "Dragon Rose Valentine" ships in."""
    files = {"stl/part.stl": 10, "cover.jpg": 20, "notes.txt": 30}

    facts = bundle_detect.detect_bundle(snapshot("stl-shape", files), {})

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_GENERIC
    assert facts.confidence == bundle_detect.CONFIDENCE_LOW


def test_r3_accepts_the_single_subdirectory_shape() -> None:
    """LittleTup's model layout, with a synthetic root trio.

    The real directory carries no image and no text, so the trio here is made up
    to isolate the depth rule; only ``articulated-dragon-claws/`` is real.
    """
    files = {"articulated-dragon-claws/part.stl": 10, "cover.jpg": 20, "notes.txt": 30}

    facts = bundle_detect.detect_bundle(snapshot("articulated-dragon-claws", files), {})

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_GENERIC
    assert facts.confidence == bundle_detect.CONFIDENCE_LOW
    assert facts.counts.model == 1
    assert facts.model_entries == ("articulated-dragon-claws/part.stl",)


def test_r3_accepts_a_wrapped_subdirectory_beside_loose_images_and_text() -> None:
    """The wrapped vendor zip: cardinality counts MODEL-bearing subdirs only."""
    files = {"01_Wizard/part.stl": 10, "images/a.jpg": 20, "README.txt": 30}

    facts = bundle_detect.detect_bundle(snapshot("01_Fates End - Wizard Tower", files), {})

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_GENERIC
    assert facts.confidence == bundle_detect.CONFIDENCE_LOW


def test_r3_rejects_a_pile_of_model_subdirectories() -> None:
    """OutOfMarbles/Rocket/Stagetop: several model folders is a pile, not a bundle."""
    files = {
        "bucket-lift/x.stl": 10,
        "triple-twister-module/y.stl": 20,
        "cover.jpg": 30,
        "notes.txt": 40,
    }

    assert bundle_detect.detect_bundle(snapshot("OutOfMarbles", files), {}) is None


def test_r3_rejects_a_model_below_the_single_subdirectory() -> None:
    """The depth bound survives the cardinality rule: two levels down is refused."""
    files = {"A/B/part.stl": 10, "cover.jpg": 20, "notes.txt": 30}

    assert bundle_detect.detect_bundle(snapshot("two-levels", files), {}) is None


def test_r3_still_requires_the_whole_trio_under_one_subdirectory() -> None:
    """The single-subdirectory rule widens model evidence only, not the trio."""
    model_and_image = {"part-folder/part.stl": 10, "cover.jpg": 20}
    model_and_text = {"part-folder/part.stl": 10, "notes.txt": 30}
    no_model = {"cover.jpg": 20, "notes.txt": 30}

    assert bundle_detect.detect_bundle(snapshot("no-text", model_and_image), {}) is None
    assert bundle_detect.detect_bundle(snapshot("no-image", model_and_text), {}) is None
    assert bundle_detect.detect_bundle(snapshot("no-model", no_model), {}) is None


def test_r3_counts_only_depth_one_model_prefixes_by_design() -> None:
    """A chosen blind spot, not an accident: only depth-1 prefixes are counted.

    The depth-2 pile under ``b/`` is invisible to cardinality, exactly as a
    depth-2 pile under ``files/`` has always been. Pinned so a future "fix" does
    not silently turn this into a deeper walk.
    """
    files = {"a/x.stl": 10, "b/deep/y.stl": 20, "cover.jpg": 30, "notes.txt": 40}

    facts = bundle_detect.detect_bundle(snapshot("blind-spot", files), {})

    assert facts is not None
    assert facts.confidence == bundle_detect.CONFIDENCE_LOW


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


# --- entry-root pairing vocabulary (Mf-6b82) --------------------------------

CLOCKWORK_ROOT = "17_ClockworkMonster"


def test_common_entry_root_is_the_one_top_level_directory() -> None:
    paths = [f"{CLOCKWORK_ROOT}/Clockwork_Part{index:02d}.stl" for index in range(3)]

    root = bundle_detect.common_entry_root(paths)

    assert root == CLOCKWORK_ROOT


def test_common_entry_root_carries_the_first_seen_casing() -> None:
    assert bundle_detect.common_entry_root(["17_ClockworkMonster/a.stl", "17_clockworkmonster/b.stl"]) == (
        "17_ClockworkMonster"
    )


def test_common_entry_root_of_mixed_roots_is_none() -> None:
    assert bundle_detect.common_entry_root(["17_Thing/a.stl", "Other Thing/b.stl"]) is None


def test_common_entry_root_of_a_top_level_file_is_none() -> None:
    assert bundle_detect.common_entry_root(["17_Thing/a.stl", "README.txt"]) is None


def test_common_entry_root_of_no_entries_is_none() -> None:
    assert bundle_detect.common_entry_root([]) is None


def test_common_entry_root_ignores_the_macosx_artifact() -> None:
    paths = [
        "__MACOSX/17_Thing/._a.stl",
        "__MACOSX/17_Thing/._preview.jpg",
        "17_Thing/a.stl",
        "17_Thing/preview.jpg",
    ]

    assert bundle_detect.common_entry_root(paths) == "17_Thing"


def test_common_entry_root_of_only_macosx_entries_is_none() -> None:
    assert bundle_detect.common_entry_root(["__MACOSX/17_Thing/._a.stl"]) is None


@pytest.mark.parametrize("name", ["files", "Files", "images", "models", "stl", "STL"])
def test_generic_container_roots_are_the_four_names_casefolded(name: str) -> None:
    assert bundle_detect.is_generic_container_root(name) is True


@pytest.mark.parametrize("name", ["files_and_docs", "my files", "FileShare", CLOCKWORK_ROOT])
def test_a_genuine_root_is_not_a_generic_container(name: str) -> None:
    """An exact set, not a prefix test: these are real bundle roots in the library."""
    assert bundle_detect.is_generic_container_root(name) is False


def contained(
    archive: List[Tuple[str, int]],
    folder: List[Tuple[str, int]],
    entry_root: Optional[str] = None,
) -> bool:
    """``archive_payload_contained`` from two ``(rel_path, size)`` literals."""
    folder_entries = [bundle_detect.BundleEntry(rel_path=rel_path, size=size) for rel_path, size in folder]
    return bundle_detect.archive_payload_contained(archive, folder_entries, entry_root)


def test_archive_payload_contained_accepts_an_identity_payload() -> None:
    """A flat archive written over the same paths needs no strip at all."""
    archive = [("part.stl", 40), ("preview.jpg", 20)]

    assert contained(archive, [("part.stl", 40), ("preview.jpg", 20)]), "identity: the paths already agree"


def test_archive_payload_contained_strips_one_wrapper() -> None:
    """The archive's own entries name the wrapper; one strip makes them agree."""
    assert contained([("SnVB623/part.stl", 40)], [("part.stl", 40)]), "one common entry root is stripped"


def test_archive_payload_contained_strips_a_generic_wrapper_on_the_stem_route() -> None:
    """``is_generic_container_root`` is the entry-root route's rule, not this one.

    The name only says nothing about WHICH bundle an archive holds, and it is
    consulted where a root is used to NAME a directory. Here nobody names a
    directory with it - the folder is the stem twin already - so ``files/`` is
    just a wrapper like any other and is stripped once.
    """
    assert contained([("files/part.stl", 40)], [("part.stl", 40)]), (
        "a generic root is refused where it names a directory, not where it is stripped"
    )


def test_archive_payload_contained_is_containment_not_equality() -> None:
    """A lived-in directory legitimately holds more than the archive shipped."""
    archive = [("SnVB623/part.stl", 40)]
    folder = [("part.stl", 40), ("spare.stl", 41), ("render.png", 20)]

    assert contained(archive, folder), "one extra file on disk does not unpair a verified archive"


def test_archive_payload_contained_refuses_two_strips() -> None:
    """The wrapper decision is made once; a wrapper's wrapper is not stripped."""
    archive = [("outer/files/part.stl", 40)]

    assert contained(archive, [("part.stl", 40)]) is False, "two strips would pair a nested thing with anything"
    assert contained(archive, [("files/part.stl", 40)]), "exactly one strip is the rule"


def test_archive_payload_contained_refuses_a_size_mismatch() -> None:
    """Declared size is header data; a lie in it makes the pairing wrong, so refuse."""
    assert contained([("SnVB623/part.stl", 40)], [("part.stl", 41)]) is False


def test_archive_payload_contained_refuses_a_name_mismatch() -> None:
    assert contained([("SnVB623/part.stl", 40)], [("other.stl", 40)]) is False


def test_archive_payload_contained_ignores_macosx_cruft() -> None:
    """AppleDouble entries are not payload, and cruft alone has verified nothing."""
    assert contained([("__MACOSX/._part.stl", 5), ("part.stl", 40)], [("part.stl", 40)]), (
        "the cruft is dropped before the comparison"
    )
    assert contained([("__MACOSX/._part.stl", 5)], [("part.stl", 40)]) is False


def test_archive_payload_contained_without_one_common_root_refuses() -> None:
    """No single wrapper, and no identity, means there is no strip to make."""
    assert contained([("A/part.stl", 40), ("B/other.stl", 41)], [("part.stl", 40)]) is False, (
        "two wrappers: there is no single entry root"
    )
    assert contained([("part.stl", 40), ("A/other.stl", 41)], [("part.stl", 40)]) is False, (
        "a top-level file beside a wrapped one is no single root either"
    )


def test_archive_payload_contained_with_an_entry_root_is_the_strict_strip() -> None:
    """The named root is stripped without any identity shortcut."""
    assert contained([("SnVB623/part.stl", 40)], [("part.stl", 40)], entry_root="SnVB623"), (
        "every entry sits under the named root"
    )
    assert contained(
        [("SnVB623/part.stl", 40), ("Other/x.stl", 1)], [("part.stl", 40)], entry_root="SnVB623"
    ) is False, "an entry outside the root is not this archive's payload"
    assert contained([("part.stl", 40)], [("part.stl", 40)], entry_root="SnVB623") is False, (
        "no identity shortcut on the entry-root route: a flat archive must not widen Mf-6b82"
    )


def clockwork_files() -> Dict[str, int]:
    files = {f"Clockwork_Part{index:02d}.stl": 100 + index for index in range(12)}
    files["Clockwork_Render0.png"] = 400
    files["Clockwork_Render1.png"] = 500
    return files


def test_archive_owned_facts_renders_a_low_generic_row() -> None:
    snapshot_ = snapshot(CLOCKWORK_ROOT, clockwork_files())

    facts = bundle_detect.archive_owned_facts(snapshot_)

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_GENERIC
    assert facts.confidence == bundle_detect.CONFIDENCE_LOW
    assert facts.metadata_shape == bundle_detect.SHAPE_GENERIC
    assert facts.counts.model == 12
    assert facts.counts.image == 2
    assert facts.counts.text == 0
    assert facts.cover_entry == "Clockwork_Render1.png", "the largest image, ties broken by path"
    assert set(facts.model_entries) == {f"Clockwork_Part{index:02d}.stl" for index in range(12)}
    assert facts.model_entries == tuple(sorted(facts.model_entries)), "path order, never readdir order"
    assert (
        facts.creator,
        facts.licence,
        facts.licence_url,
        facts.source_url,
        facts.thing_id,
        facts.description,
    ) == (None, None, None, None, None, None)


def test_detect_bundle_still_refuses_the_snapshot_archive_owned_facts_offers() -> None:
    """AC-6: the rule change is the service's verified pairing, never a looser R3.

    The same listing is refused by ``detect_bundle`` and offered by
    ``archive_owned_facts``. Only ``bundle_service`` may act on the second one,
    and only when an archive's entry root names that directory - so R3 itself has
    not moved, and a models-and-images directory with no archive stays silent.
    """
    snapshot_ = snapshot(CLOCKWORK_ROOT, clockwork_files())

    assert bundle_detect.detect_bundle(snapshot_, {}) is None
    assert bundle_detect.archive_owned_facts(snapshot_) is not None


def test_archive_owned_facts_offers_a_directory_with_text() -> None:
    """A stray text file is decoration now: the archive is the identity.

    The old rule refused any directory holding a ``.txt``; restoring that leg
    (``or counts.text``) makes this test fail, and it is the models-plus-text
    bucket - 8 directories on the owner's library - that the leg silently hid.
    """
    files = {**clockwork_files(), "notes.txt": 20}

    facts = bundle_detect.archive_owned_facts(snapshot(CLOCKWORK_ROOT, files))

    assert facts is not None, "a notes.txt beside the models is decoration, not a refusal"
    assert facts.detector == bundle_detect.DETECTOR_GENERIC
    assert facts.confidence == bundle_detect.CONFIDENCE_LOW
    assert facts.counts.text == 1


def test_archive_owned_facts_offers_a_directory_without_an_image() -> None:
    """The ``counts.image < 1`` leg is gone: this is the models-only bucket.

    Restoring ``counts.image < 1 or ...`` in ``archive_owned_facts`` makes this
    test fail. It is 6 directories on the owner's library, each with models and
    no preview at all.
    """
    files = {f"Clockwork_Part{index:02d}.stl": 100 for index in range(12)}

    facts = bundle_detect.archive_owned_facts(snapshot(CLOCKWORK_ROOT, files))

    assert facts is not None, "models alone are enough when an archive says what the directory is"
    assert facts.counts.image == 0
    assert facts.cover_entry is None


def test_archive_owned_facts_refuses_a_pile_of_two_unnamed_model_bearing_directories() -> None:
    """Own-depth/cardinality is the only shape guard left, and it is load-bearing.

    Two unnamed model-bearing subdirectories, nothing at depth 0 and nothing
    under ``files/``, is a pile of things rather than one bundle - the archive
    identity does not make it one. Removing ``_own_depth_model_count``'s
    cardinality bound (the ``len(unnamed) == 1`` leg) promotes this pile, so
    this test fails exactly when that guard is emptied.
    """
    files = {"Alpha/a.stl": 40, "Beta/b.stl": 41}

    assert bundle_detect.archive_owned_facts(snapshot(CLOCKWORK_ROOT, files)) is None


def test_archive_owned_facts_keeps_the_depth_bound() -> None:
    """The Mf-h0p8 bound still holds: models at depth 2 are not this bundle's."""
    files = {
        "Clockwork_Part00.stl": 100,
        "v1.8/older/Clockwork_Part01.stl": 100,
        "Clockwork_Render0.png": 400,
    }

    assert bundle_detect.archive_owned_facts(snapshot(CLOCKWORK_ROOT, files)) is not None, "one model at depth 0 is enough"

    buried = {"v1.8/older/Clockwork_Part00.stl": 100, "Clockwork_Render0.png": 400}
    assert bundle_detect.archive_owned_facts(snapshot(CLOCKWORK_ROOT, buried)) is None


def test_is_bundle_member_is_strictly_below_and_raw() -> None:
    assert bundle_detect.is_bundle_member("A/B/c.zip", {"A"}) is True
    assert bundle_detect.is_bundle_member("A/B/c.zip", {"A/B"}) is True
    assert bundle_detect.is_bundle_member("A/c.zip", {"A"}) is True
    assert bundle_detect.is_bundle_member("A.zip", {"A"}) is False, "a twin beside the directory is not a member"
    assert bundle_detect.is_bundle_member("AB/c.zip", {"A"}) is False, "a segment boundary, not a string prefix"
    assert bundle_detect.is_bundle_member("A/B/c.zip", {"a"}) is False, "raw and case-sensitive, like the strip"
    assert bundle_detect.is_bundle_member("A/B/c.zip", set()) is False
    assert bundle_detect.is_bundle_member("c.zip", {"A"}) is False


def test_a_nested_archive_is_never_bundle_evidence() -> None:
    """The rejected rule would let a zip's entries make its directory a bundle."""
    only_zip = {"Clockwork_v1.8.zip": 176_000_000}

    assert bundle_detect.classify_entry("Clockwork_v1.8.zip", 176_000_000) == bundle_detect.ARCHIVE
    assert bundle_detect.detect_bundle(snapshot(CLOCKWORK_ROOT, only_zip), {}) is None
    assert bundle_detect.archive_owned_facts(snapshot(CLOCKWORK_ROOT, only_zip)) is None

    without_model = {
        "Clockwork_v1.8.zip": 176_000_000,
        "Clockwork_Render0.png": 400,
        "notes.txt": 20,
    }
    assert bundle_detect.detect_bundle(snapshot(CLOCKWORK_ROOT, without_model), {}) is None
    assert bundle_detect.archive_owned_facts(snapshot(CLOCKWORK_ROOT, without_model)) is None


def test_a_nested_archive_is_counted_as_an_archive_only() -> None:
    files = {
        "part.stl": 100,
        "cover.jpg": 50,
        "notes.txt": 20,
        "Clockwork_v1.8.zip": 1000,
    }

    facts = bundle_detect.detect_bundle(snapshot(CLOCKWORK_ROOT, files), {})

    assert facts is not None
    assert facts.counts.model == 1
    assert facts.counts.image == 1
    assert facts.counts.text == 1
    assert facts.counts.archive == 1
    assert facts.counts.other == 0


# --- duplicate archives (Mf-z4vv) -------------------------------------------

DUPLICATE_DIGEST = "a" * 64


def identity(rel_path: str, size: int, digest: str = DUPLICATE_DIGEST) -> bundle_detect.ArchiveIdentity:
    return bundle_detect.ArchiveIdentity(rel_path=rel_path, size=size, digest=digest)


def duplicate_group(paths: tuple, size: int = 500, digest: str = DUPLICATE_DIGEST) -> bundle_detect.DuplicateGroup:
    return bundle_detect.DuplicateGroup(digest=digest, size=size, paths=paths)


def test_equal_size_archive_groups_keeps_only_shared_sizes() -> None:
    """A size that occurs once is never hashed: it cannot be a duplicate."""
    groups = bundle_detect.equal_size_archive_groups([("a.zip", 10), ("b.zip", 10), ("c.zip", 11)])

    assert groups == ((10, ("a.zip", "b.zip")),)


def test_duplicate_groups_puts_the_shortest_name_first() -> None:
    group = bundle_detect.duplicate_groups([identity("x/Copy (1).zip", 500), identity("x/Copy.zip", 500)])

    assert len(group) == 1
    assert group[0].digest == DUPLICATE_DIGEST
    assert group[0].size == 500
    assert group[0].paths[0] == "x/Copy.zip", "the browser duplicate appends characters, so the shortest is the original"
    assert group[0].reclaimable_bytes == 500


def test_duplicate_groups_ignores_a_lone_digest() -> None:
    assert bundle_detect.duplicate_groups([identity("only.zip", 500)]) == ()


def test_duplicate_groups_ignores_equal_sizes_with_different_digests() -> None:
    archives = [identity("a.zip", 500, digest="a" * 64), identity("b.zip", 500, digest="b" * 64)]

    assert bundle_detect.duplicate_groups(archives) == ()


def test_duplicate_archive_paths_excludes_the_authoritative_path() -> None:
    groups = (duplicate_group(("x/Copy.zip", "x/Copy (1).zip", "x/Copy (2).zip")),)

    assert bundle_detect.duplicate_archive_paths(groups) == ("x/Copy (1).zip", "x/Copy (2).zip")


def test_duplicate_report_text_is_empty_without_groups() -> None:
    assert bundle_detect.duplicate_report_text(()) == ""


def test_duplicate_report_text_keeps_one_path_and_lists_the_copies() -> None:
    groups = (duplicate_group(("x/Copy.zip", "x/Copy (1).zip"), size=500),)

    text = bundle_detect.duplicate_report_text(groups)
    lines = text.splitlines()

    assert lines[0] == "# duplicate archives: 1 group(s), 500 bytes reclaimable"
    assert f"# sha256 {DUPLICATE_DIGEST} size 500 reclaimable 500" in lines
    assert "keep\t500\tx/Copy.zip" in lines
    assert "duplicate\t500\tx/Copy (1).zip" in lines


# --- R4: an id in the name --------------------------------------------------

DRAGON_NAME = "5390184_ARTICULATED_ice_dragon__fixed_"
DRAGON_URL = "https://www.thingiverse.com/thing:5390184"


def test_r4_offers_an_id_named_model_only_directory_at_medium() -> None:
    """AC-1: the ticket's export, models only, is offered at medium.

    Mutation: delete R4 from ``detect_bundle`` and this snapshot returns
    ``None`` (no image and no text means R3 cannot fire), so every assertion
    below reds. Mutation: stamp the row with a creator or a licence and the
    ``None`` assertions red - a name says where a thing came from, never who
    made it or what it may be reused for.
    """
    facts = bundle_detect.detect_bundle(
        snapshot(DRAGON_NAME, {"files/ARTICULATED_ice_dragon_fixed_p1.stl": 40}), {}
    )

    assert facts is not None
    assert facts.confidence == bundle_detect.CONFIDENCE_MEDIUM
    assert facts.detector == bundle_detect.DETECTOR_THINGIVERSE_ID_NAME
    assert facts.metadata_shape == bundle_detect.SHAPE_ID_NAME
    assert facts.source_url == DRAGON_URL
    assert facts.thing_id == "5390184"
    assert facts.creator is None
    assert facts.licence is None
    assert facts.licence_url is None
    assert facts.description is None
    assert bundle_detect.thingiverse_thing_id_from_url(facts.source_url) == facts.thing_id, (
        "the URL and the id are the same statement, by construction"
    )
    assert facts.title == bundle_detect.display_name(DRAGON_NAME)


def test_r4_refuses_a_model_only_directory_without_an_id() -> None:
    """AC-2: the same shape under an ordinary name is offered by nothing.

    Mutation: let R4 fire on any name (drop the id lookup) and both snapshots
    come back as medium rows, so both assertions red.
    """
    assert bundle_detect.detect_bundle(snapshot("Plain_parts", {"files/a.stl": 40}), {}) is None
    assert bundle_detect.detect_bundle(snapshot("Gear Bearing", {"files/a.stl": 40}), {}) is None


def test_r4_outranks_r3_so_an_id_named_trio_reads_medium() -> None:
    """R4 sits before R3: an inferred id is strictly more than a shape guess.

    Mutation: move the R4 block after the R3 block and the id-named trio comes
    back ``low``/``generic``, so the medium assertion reds. The second half
    proves R3 itself is unchanged: the identical trio under a non-id name is
    still ``low``, and it still invents no URL.
    """
    files = {"part.stl": 40, "render.png": 5, "notes.txt": 3}
    id_named = bundle_detect.detect_bundle(snapshot(DRAGON_NAME, files), {})
    plain = bundle_detect.detect_bundle(snapshot("Plain_trio", files), {})

    assert id_named is not None
    assert id_named.confidence == bundle_detect.CONFIDENCE_MEDIUM
    assert id_named.metadata_shape == bundle_detect.SHAPE_ID_NAME
    assert plain is not None
    assert plain.confidence == bundle_detect.CONFIDENCE_LOW
    assert plain.metadata_shape == bundle_detect.SHAPE_GENERIC
    assert plain.source_url is None


def test_r4_yields_to_both_high_rules() -> None:
    """A vendor document always outranks the name, exactly as it outranks R3.

    Mutation: put R4 first and this snapshot comes back medium with the
    name-derived id, so both the high assertion and the document-id assertion
    red.
    """
    files = {"README.txt": len(README_TEXT), "LICENSE.txt": len(LICENSE_TEXT), "part.stl": 40}
    id_named = bundle_detect.detect_bundle(snapshot(DRAGON_NAME, files), r1_docs())
    plain = bundle_detect.detect_bundle(snapshot("Neutral", files), r1_docs())

    assert id_named is not None
    assert id_named.confidence == bundle_detect.CONFIDENCE_HIGH
    assert id_named.detector == bundle_detect.DETECTOR_THINGIVERSE_README
    assert id_named.thing_id == "2493386", "the document's id, never the name's"
    assert provenance(id_named) == provenance(plain), "the name changes nothing once a document speaks"


def test_r4_requires_a_model_at_the_bundles_own_depth() -> None:
    """A name is provenance, never evidence that this is one thing.

    Mutation: drop the ``own_models >= 1`` leg of R4 and both snapshots are
    offered - the photos-plus-notes directory on the trio's text, and the pile
    of two unnamed model-bearing subdirectories on its children's models.
    """
    assert bundle_detect.detect_bundle(snapshot("5390184_photos", {"a.jpg": 1, "notes.txt": 5}), {}) is None
    assert (
        bundle_detect.detect_bundle(
            snapshot("5390184_parts", {"Alpha/part.stl": 40, "Beta/part.stl": 41}), {}
        )
        is None
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("5390184_ARTICULATED_ice_dragon__fixed_", "5390184"),
        ("2024_parts", "2024"),
        ("2024-10-31_backup", None),
        ("my_5390184_thing", None),
        ("606cdf0d5199b_thing", None),
        ("539_x", None),
        ("ARTICULATED_ice_5390184", None),
    ],
)
def test_the_id_in_a_name_needs_a_digit_floor_a_separator_and_the_start(name: str, expected: Optional[str]) -> None:
    """The extractor's four decisions, each killable by one mutation.

    * ``\\d{4,}`` -> ``\\d+`` reds ``539_x`` (the four-digit floor);
    * the mandatory ``_`` -> ``_?`` reds ``2024-10-31_backup`` (a date is not an
      export);
    * dropping ``^`` AND switching ``.match`` for ``.search`` reds
      ``my_5390184_thing`` (the anchor).

    Dropping ``^`` alone, or ``.match`` -> ``.search`` with ``^`` kept, are
    provable no-ops: ``.match`` already anchors at position 0 and ``^`` already
    anchors inside ``.search``, so the combined mutation is the killable one.
    ``2024_parts`` is the knowingly accepted false positive of the digit floor;
    it is pinned so a stricter regex that starts refusing it is a visible change.
    """
    assert bundle_detect.thingiverse_id_from_name(name) == expected


def test_the_accepted_id_lookalike_is_offered_at_medium() -> None:
    """A plain year prefix is indistinguishable from an id and is accepted.

    ``2024_parts`` reads as a Thingiverse id, so the row is medium with the
    derived URL and no attribution invented. The one lookalike the separator
    rule refuses is ``2024-10-31_backup``, and the test above pins that refusal:
    without it, every dated backup directory would claim a source URL. Mutation:
    remove the separator floor and this test still passes while the refusal
    above reds - the pair is what makes the trade visible.
    """
    facts = bundle_detect.detect_bundle(snapshot("2024_parts", {"part.stl": 40}), {})

    assert facts is not None
    assert facts.confidence == bundle_detect.CONFIDENCE_MEDIUM
    assert facts.source_url == "https://www.thingiverse.com/thing:2024"
    assert facts.thing_id == "2024"
    assert facts.creator is None
    assert facts.licence is None


def test_inferred_confidences_holds_both_guesses_and_not_high() -> None:
    """The set is the contract the container guards read.

    Mutation: add a fourth inferred confidence and forget this set - or drop a
    member - and the equality reds. Mutation: put ``high`` in the set and the
    membership assertion reds; the whole point is that a high row is exempt from
    the container guards because its documents sit at depth 0.
    """
    assert bundle_detect.INFERRED_CONFIDENCES == frozenset(
        {bundle_detect.CONFIDENCE_MEDIUM, bundle_detect.CONFIDENCE_LOW}
    )
    assert bundle_detect.CONFIDENCE_HIGH not in bundle_detect.INFERRED_CONFIDENCES


def test_strong_confidences_outrank_a_shape_inference() -> None:
    """The set is the boundary a promoted LOW row may not swallow past.

    Mutation: adding ``low`` widens the guard until a member zip refuses a
    legitimate promotion - the Clockwork shape - and dropping ``medium`` lets an
    id-named child be swallowed; the equality reds either way. Membership is
    what ``_holds_stronger_candidate`` reads, so a future confidence has to be
    placed here deliberately rather than slipping in as ``not low``.
    """
    assert bundle_detect.STRONG_CONFIDENCES == frozenset(
        {bundle_detect.CONFIDENCE_HIGH, bundle_detect.CONFIDENCE_MEDIUM}
    )
    assert bundle_detect.CONFIDENCE_LOW not in bundle_detect.STRONG_CONFIDENCES


# --- R5: a title-shaped folder name (Mf-hvck) -------------------------------

# The literal measurement, not the constant: the anti-shrink tests read this
# tuple so deleting an entry from `TITLE_PART_NAMES` reds instead of shrinking
# the sweep silently.
REQUIRED_TITLE_PART_NAMES = (
    "files",
    "source",
    "src",
    "images",
    "img",
    "renders",
    "textures",
    "docs",
    "stl",
    "stls",
    "models",
    "meshes",
    "parts",
)

# The nine measured names R5 offers, by the branch that accepts each.
TITLE_NAMES_WITH_A_SEPARATOR = (
    "Rocket Filement support",
    "mac deckbox",
    "skull mask",
    "printable carrage",
    "Filament guide",
)
TITLE_NAMES_SINGLE_WORD = ("Stagetop", "LittleTup", "GavinIsABot", "Taffi")


@pytest.mark.parametrize("name", TITLE_NAMES_WITH_A_SEPARATOR)
def test_is_title_name_accepts_a_name_with_a_space_or_separator(name: str) -> None:
    """Branch A: a separator is a person's phrasing.

    Mutation: require a capitalised first word as well and ``mac deckbox`` and
    ``printable carrage`` - both measured titles - are refused.
    """
    assert bundle_detect.is_title_name(name) is True


@pytest.mark.parametrize("name", TITLE_NAMES_SINGLE_WORD)
def test_is_title_name_accepts_a_long_capitalised_single_word(name: str) -> None:
    """Branch B: one capitalised word longer than the floor, with a lowercase letter.

    Mutation: drop the lowercase clause and ``STAGETOP`` gets in; the all-caps
    test below pins that refusal.
    """
    assert bundle_detect.is_title_name(name) is True


@pytest.mark.parametrize("name", ["Beta", "Tup", "Sub"])
def test_is_title_name_refuses_a_short_single_word(name: str) -> None:
    """The FLOOR is load-bearing: four letters is a pile member, not a title.

    Mutation: ``len(cleaned) < TITLE_SINGLE_WORD_FLOOR`` admits ``Beta`` (4) and
    this test reds; ``Taffi`` (5) is a measured title, so the bound is ``> 4``.
    """
    assert bundle_detect.is_title_name(name) is False


@pytest.mark.parametrize("name", ["STAGETOP", "ARTICULATED", "ALLSTL"])
def test_is_title_name_refuses_an_all_caps_label(name: str) -> None:
    """An ALL-CAPS label is a product code, so branch B's lowercase clause refuses it.

    Mutation: drop ``any(character.islower() ...)`` and every one of these is
    offered as a title.
    """
    assert bundle_detect.is_title_name(name) is False


@pytest.mark.parametrize("name", ["residual", "clockworkmonster", "xbox360shell"])
def test_is_title_name_refuses_a_lowercase_single_word(name: str) -> None:
    """``_TITLE_WORD.fullmatch`` is ``None`` on a leading lowercase character.

    Mutation: ``fullmatch`` -> ``search``/``match`` without the ``[A-Z]`` first
    and every ordinary lowercase folder name becomes a title.
    """
    assert bundle_detect.is_title_name(name) is False


def test_the_exclusion_list_contains_every_required_part_name() -> None:
    """The anti-shrink test: deleting an entry from the constant reds here."""
    assert set(REQUIRED_TITLE_PART_NAMES) <= bundle_detect.TITLE_PART_NAMES


def test_a_part_name_is_never_a_title() -> None:
    """Every part name, in every casing and whitespace variant, is refused.

    The variants are looped INSIDE the test over the LITERAL tuple, so one
    deletion from the constant cannot shrink the sweep - it only reds the
    subset test above, which is the point.
    """
    for part in REQUIRED_TITLE_PART_NAMES:
        for variant in (part, part.upper(), part.title(), f" {part} ", f"{part}\t", part.capitalize()):
            assert bundle_detect.is_title_name(variant) is False, variant


@pytest.mark.parametrize("part", sorted(bundle_detect.TITLE_PART_NAMES))
def test_every_name_in_the_exclusion_list_is_refused(part: str) -> None:
    """An ADDED entry is refused too, so the list cannot grow a title by accident."""
    assert bundle_detect.is_title_name(part) is False


def test_files_and_source_are_refused_even_with_the_exclusion_list_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two part names fail every branch twice over: this is the 11 -> 9 proof.

    With ``TITLE_PART_NAMES`` emptied, ``files`` and ``source`` still fail -
    branch A finds no separator and branch B's ``fullmatch`` is ``None`` on a
    leading lowercase character - while the nine title names still pass. Mutation:
    let branch A fire on an empty name or branch B on a lowercase word and both
    refusals red.
    """
    monkeypatch.setattr(bundle_detect, "TITLE_PART_NAMES", frozenset())

    assert bundle_detect.is_title_name("files") is False
    assert bundle_detect.is_title_name("source") is False
    for name in TITLE_NAMES_WITH_A_SEPARATOR + TITLE_NAMES_SINGLE_WORD:
        assert bundle_detect.is_title_name(name) is True, name


def test_title_name_facts_is_none_when_the_switch_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ONE switch is read inside ``title_name_facts`` and nowhere else."""
    snapshot_ = snapshot("skull mask", {"a.stl": 10})

    monkeypatch.setattr(bundle_detect, "TITLE_NAME_BUNDLES_ENABLED", False)

    assert bundle_detect.title_name_facts(snapshot_) is None


@pytest.mark.parametrize("name", ["files", "source", "residual"])
def test_title_name_facts_is_none_for_a_part_name_and_a_lowercase_word(name: str) -> None:
    """A model at the folder's own depth is not enough: the name must read as a title."""
    assert bundle_detect.title_name_facts(snapshot(name, {"a.stl": 10})) is None


def test_title_name_facts_requires_a_model_at_the_folders_own_depth() -> None:
    """The one shape guard: a name alone is never a bundle.

    Mutation: drop the ``_own_depth_model_count`` leg and the pile and the
    depth-2 shape are both offered, so both ``None`` assertions red.
    """
    pile = {"Alpha/a.stl": 40, "Beta/b.stl": 41}
    assert bundle_detect.title_name_facts(snapshot("RepRap-MultiFilament", pile)) is None

    buried = {"v1.8/older/part.stl": 100}
    assert bundle_detect.title_name_facts(snapshot("RepRap-MultiFilament", buried)) is None

    assert bundle_detect.title_name_facts(snapshot("RepRap-MultiFilament", {"part.stl": 100})) is not None


def test_title_name_facts_is_low_name_derived_and_invents_no_provenance() -> None:
    """The row says the name read like a title, and nothing about who made the thing.

    Mutation: pass a ``thing_id`` through ``_name_only_facts`` and the source URL
    assertion reds; nothing in R5 may invent a URL, a creator or a licence.
    """
    files = {"a.stl": 10, "b.stl": 20, "cover.jpg": 30}

    facts = bundle_detect.title_name_facts(snapshot("skull mask", files))

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_TITLE_NAME == "title-name"
    assert facts.metadata_shape == bundle_detect.SHAPE_TITLE_NAME == "title-name"
    assert facts.confidence == bundle_detect.CONFIDENCE_LOW
    assert facts.title == "skull mask"
    assert (
        facts.creator,
        facts.licence,
        facts.licence_url,
        facts.source_url,
        facts.thing_id,
        facts.description,
    ) == (None, None, None, None, None, None)
    assert facts.counts.model == 2
    assert facts.counts.image == 1
    assert facts.cover_entry == "cover.jpg"
    assert facts.model_entries == ("a.stl", "b.stl")


def test_detect_bundle_still_refuses_the_snapshot_title_name_facts_offers() -> None:
    """R5 is NOT a ``detect_bundle`` branch, and this is the pin.

    Mutation: move R5 into ``detect_bundle`` and this snapshot comes back non-None,
    which is exactly the deepest-first migration that loses a promoted row.
    """
    snapshot_ = snapshot("skull mask", {"a.stl": 10, "cover.jpg": 30, "notes.txt": 5})

    assert bundle_detect.detect_bundle(snapshot_, {}) is not None, "that trio is R3"
    assert bundle_detect.detect_bundle(snapshot("skull mask", {"a.stl": 10}), {}) is None
    assert bundle_detect.title_name_facts(snapshot("skull mask", {"a.stl": 10})) is not None


def test_r4_outranks_the_title_rule_on_an_id_named_snapshot() -> None:
    """R4's inferred id beats R5's name-only row, and the service uses the former.

    The display name of the dragon export is title-shaped, so both rules apply to
    the identical listing: the medium row carries the derived URL, the low row
    carries nothing. Mutation: dispatch R5 before R4 in the service and the same
    key comes back low with ``source_url is None``.
    """
    snapshot_ = snapshot(DRAGON_NAME, {"ARTICULATED_ice_dragon_fixed_p1.stl": 40})

    detected = bundle_detect.detect_bundle(snapshot_, {})
    title = bundle_detect.title_name_facts(snapshot_)

    assert detected is not None
    assert detected.confidence == bundle_detect.CONFIDENCE_MEDIUM
    assert detected.detector == bundle_detect.DETECTOR_THINGIVERSE_ID_NAME
    assert detected.source_url == DRAGON_URL
    assert title is not None
    assert title.confidence == bundle_detect.CONFIDENCE_LOW
    assert title.detector == bundle_detect.DETECTOR_TITLE_NAME
    assert title.source_url is None


# --- the rule-set stamp and the refusal vocabulary (Mf-9ygi) ----------------

# The version a report records, and the stamp of the tables as imported. Both are
# LITERALS on purpose: an edit to any row-deciding table changes the derived hash
# and this test reds, which is the whole point of the stamp - the 114 -> 112 delta
# of Mf-9ygi could not be explained because nothing recorded what the rules were.
RULE_SET_VERSION = "2026-11-04.1"
RULE_SET_STAMP = "418445978b9a45fb6d68e9814e7df2c2ae83487181d095496f70741b3d4732b5"

# Every constant `rule_set_tables` captures, with a replacement that differs from
# the shipped value. The names are also the exact key set of the derived table, so
# a constant that stops being stamped (or a table entry that names nothing) reds.
ROW_DECIDING_TABLES = {
    "MODEL": "model-v2",
    "IMAGE": "image-v2",
    "TEXT": "text-v2",
    "ARCHIVE": "archive-v2",
    "OTHER": "other-v2",
    "DETECTOR_THINGIVERSE_README": "thingiverse-readme-v2",
    "DETECTOR_THINGIVERSE_SIBLING": "thingiverse-sibling-docs-v2",
    "DETECTOR_THINGIVERSE_ID_NAME": "thingiverse-id-name-v2",
    "DETECTOR_GENERIC": "generic-bundle-v2",
    "DETECTOR_TITLE_NAME": "title-name-v2",
    "CONFIDENCE_HIGH": "high-v2",
    "CONFIDENCE_MEDIUM": "medium-v2",
    "CONFIDENCE_LOW": "low-v2",
    "INFERRED_CONFIDENCES": frozenset({"low-v2"}),
    "STRONG_CONFIDENCES": frozenset({"high-v2"}),
    "SHAPE_ROOT_DOCS": "root-docs-v2",
    "SHAPE_SIBLING_DOCS": "sibling-docs-v2",
    "SHAPE_ID_NAME": "id-name-v2",
    "SHAPE_GENERIC": "generic-v2",
    "SHAPE_TITLE_NAME": "title-name-shape-v2",
    "MODEL_SUFFIXES": frozenset({".obj"}),
    "IMAGE_SUFFIXES": frozenset({".webp"}),
    "BUNDLE_TEXT_EXTS": frozenset({".nfo"}),
    "R3_MODEL_DIRS": frozenset({"parts"}),
    "GENERIC_CONTAINER_ROOTS": frozenset({"stuff"}),
    "_MACOSX_ROOT": "__macosx-v2",
    "BUNDLE_DOC_MAX_BYTES": 4096,
    "MAX_DOC_CANDIDATES": 9,
    "TITLE_PART_NAMES": frozenset({"pieces"}),
    "TITLE_SINGLE_WORD_FLOOR": 8,
    "TITLE_NAME_BUNDLES_ENABLED": False,
    "_TITLE_WORD": re.compile(r"[A-Z][a-z]+"),
    "_ROOT_README": "readme.md",
    "_ROOT_LICENSE": "licence.md",
    "_SIBLING_README": "_ABOUT",
    "_SIBLING_LICENSE": "_TERMS",
    "_THINGIVERSE_ID_IN_NAME": re.compile(r"^(\d{6,})-"),
    "_THINGIVERSE_THING_URL": "https://example.invalid/thing:{}",
    "_README_MARKERS": re.compile(r"made by (?P<creator>[^,\n]+)"),
    "_LICENSE_LINE": re.compile(r"(?P<title>.+) from (?P<creator>.+?) at (?P<url>https?://\S+)"),
    "_SENTENCE_BOUNDARY": re.compile(r"[;]\s+"),
    "_LEADING_ORDINAL": re.compile(r"^\d+\.+"),
    "_TRAILING_JUNK": "?!",
    "_TOKEN_SEPARATORS": re.compile(r"[.:]+"),
    "_LICENCE_TABLE": {"cc_by": ("CC BY v2", "https://example.invalid/by/5.0/")},
    "_CREATIVE_COMMONS": "https://example.invalid/licenses/",
    "_CC0_URL": "https://example.invalid/publicdomain/zero/2.0/",
}


def test_the_rule_set_stamp_is_pinned_so_a_table_edit_cannot_go_unnoticed() -> None:
    """The stamp a report records is a literal here, so a table edit reds.

    Mutation: change one constant any row-deciding path reads and the pinned hash
    stops matching - which is exactly the audit trail Mf-9ygi's 114 -> 112
    investigation lacked.
    """
    assert bundle_detect.RULE_SET_VERSION == RULE_SET_VERSION
    assert bundle_detect.RULE_SET_STAMP == RULE_SET_STAMP
    assert bundle_detect.rule_set() == {"version": RULE_SET_VERSION, "stamp": RULE_SET_STAMP}
    assert bundle_detect.rule_set_stamp() == RULE_SET_STAMP


def test_every_row_deciding_table_is_inside_the_derived_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each stamped constant, changed on its own, moves the digest.

    The sweep is the guard against a constant that a rule reads but the stamp does
    not capture: such a constant would silently escape the audit trail. Mutation:
    drop any entry from `rule_set_tables` and the key-set assertion below names it.
    """
    baseline = bundle_detect.rule_set_stamp()
    assert re.fullmatch(r"[0-9a-f]{64}", baseline), "the stamp must be a sha256 hexdigest"
    assert set(bundle_detect.rule_set_tables()) == set(ROW_DECIDING_TABLES)

    for name, replacement in ROW_DECIDING_TABLES.items():
        with monkeypatch.context() as patched:
            patched.setattr(bundle_detect, name, replacement)
            assert bundle_detect.rule_set_stamp() != baseline, f"{name} is not inside the rule-set stamp"

    assert bundle_detect.rule_set_stamp() == baseline


def test_the_stamp_covers_the_image_table_and_the_title_word_explicitly() -> None:
    """The two constants a subtler sweep could miss are named here.

    ``IMAGE_SUFFIXES`` is a second module's table re-exported, and ``_TITLE_WORD``
    is a compiled regex whose flags matter as much as its pattern, so both get an
    explicit statement rather than only a dict entry.
    """
    tables = bundle_detect.rule_set_tables()

    assert tables["IMAGE_SUFFIXES"] == sorted(library_scan.IMAGE_SUFFIXES)
    assert tables["_TITLE_WORD"] == {
        "pattern": bundle_detect._TITLE_WORD.pattern,
        "flags": bundle_detect._TITLE_WORD.flags,
    }
    assert tables["_MACOSX_ROOT"] == bundle_detect._MACOSX_ROOT
    assert tables["_LICENCE_TABLE"]["cc_by"] == ["CC BY", "https://creativecommons.org/licenses/by/4.0/"]


def test_each_pure_refusal_names_its_rule() -> None:
    """Every refusal the pure detector makes carries its rule, and nothing else.

    Mutation: return the old bare ``None`` from ``detect_bundle_decision`` and a
    refused directory is invisible again - the 46 archives of Mf-9ygi.
    """
    readmeless_root = snapshot("Moai", {"README.txt": 10, "a.stl": 10})
    readmeless_sibling = snapshot("x", {"x_README": 10, "a.stl": 10})
    id_named_without_model = snapshot("5390184_X", {"a.jpg": 10})
    no_model = snapshot("Widget", {"a.png": 10, "b.txt": 5})
    no_image = snapshot("Widget", {"a.stl": 10, "b.txt": 5})
    no_text = snapshot("Widget", {"a.stl": 10, "b.png": 5})

    cases = (
        (readmeless_root, {"README.txt": "no markers here\n"}, bundle_detect.REFUSAL_R1_NO_MARKERS),
        (readmeless_sibling, {"x_README": "no markers here\n"}, bundle_detect.REFUSAL_R2_NO_MARKERS),
        (id_named_without_model, {}, bundle_detect.REFUSAL_R4_NO_OWN_DEPTH_MODEL),
        (no_model, {}, bundle_detect.REFUSAL_R3_NO_OWN_DEPTH_MODEL),
        (no_image, {}, bundle_detect.REFUSAL_R3_TRIO_NO_IMAGE),
        (no_text, {}, bundle_detect.REFUSAL_R3_TRIO_NO_TEXT),
    )

    for snapshot_, docs, expected in cases:
        decision = bundle_detect.detect_bundle_decision(snapshot_, docs)
        assert decision.refusal == expected, f"{snapshot_.bundle_name} refused as {decision.refusal}"
        assert decision.facts is None
        assert decision.refusal in bundle_detect.REFUSAL_RULES
        assert bundle_detect.detect_bundle(snapshot_, docs) is None

    fired = bundle_detect.detect_bundle_decision(snapshot("Moai", {"README.txt": 10, "LICENSE.txt": 10}), r1_docs())
    assert fired.refusal is None, "a fired rule must not also name a refusal"
    assert fired.facts is not None
    assert fired.facts.confidence == bundle_detect.CONFIDENCE_HIGH


def test_the_promotion_refusal_names_its_rule() -> None:
    """``archive_owned_decision`` refuses with the promotion's own reason.

    Mutation: keep ``archive_owned_facts``' bare ``None`` and a pairing refused for
    want of an own-depth model is indistinguishable from a pairing never examined.
    """
    refused = bundle_detect.archive_owned_decision(snapshot("clockwork", {"a.png": 10}))
    fired = bundle_detect.archive_owned_decision(snapshot("clockwork", {"a.stl": 10}))

    assert refused.refusal == bundle_detect.REFUSAL_PROMOTION_NO_OWN_DEPTH_MODEL
    assert refused.refusal in bundle_detect.REFUSAL_RULES
    assert refused.facts is None
    assert fired.refusal is None
    assert fired.facts is not None
    assert fired.facts.confidence == bundle_detect.CONFIDENCE_LOW


def test_the_title_refusals_are_ordered_like_the_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """R5's three refusals: the switch, then the name, then the own-depth guard.

    Mutation: reorder the branches in ``title_name_decision`` and a disabled rule
    reports "not a title", which is the wrong diagnosis for the operator.
    """
    with monkeypatch.context() as patched:
        patched.setattr(bundle_detect, "TITLE_NAME_BUNDLES_ENABLED", False)
        disabled = bundle_detect.title_name_decision(snapshot("skull mask", {"a.stl": 10}))
        assert disabled.refusal == bundle_detect.REFUSAL_R5_DISABLED
        assert disabled.facts is None

    not_a_title = bundle_detect.title_name_decision(snapshot("files", {"a.stl": 10}))
    too_deep = bundle_detect.title_name_decision(snapshot("Rocket Filement support", {"x/a.stl": 1, "y/b.stl": 1}))
    fired = bundle_detect.title_name_decision(snapshot("Rocket Filement support", {"a.stl": 10}))

    assert not_a_title.refusal == bundle_detect.REFUSAL_R5_NOT_TITLE
    assert too_deep.refusal == bundle_detect.REFUSAL_R5_NO_OWN_DEPTH_MODEL
    assert fired.refusal is None
    assert fired.facts is not None
    assert fired.facts.detector == bundle_detect.DETECTOR_TITLE_NAME
