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


# --- R6: a single-root archive with models and images (Mf-tcrw) ----------


# The measured `03_Fates End - Barbarian Tower.zip`, entry for entry: one root
# directory, 14 models, 3 previews (the largest is the cover), no text at all and
# the vendor's own nested deliverable zip. Sizes are the measured byte counts.
FATES_TOWER_COVER = "03_Barbarian/Barbarian_VisualReference.png"
FATES_TOWER_FILES = {
    "03_Barbarian/Barbarian_VisualReference.png": 273497,
    "03_Barbarian/Barbarian_DT_v2.2.stl": 25078084,
    "03_Barbarian/Barbarian_Dec2_top_v2.2.stl": 25034084,
    "03_Barbarian/_FileName_Key.png": 24699,
    "03_Barbarian/Barbarian_RENDER.png": 183190,
    "03_Barbarian/Barbarian_Dec_Roof_v1.0.stl": 14721584,
    "03_Barbarian/BarbarianTray_OpenLock_v1.0.stl": 15039484,
    "03_Barbarian/Barbarian_DT2_btm_v2.2.stl": 25024784,
    "03_Barbarian/Barbarian_DT2_top_v2.2.stl": 25046284,
    "03_Barbarian/BarbarianTray_Lg_Tx_v1.0.stl": 15009084,
    "03_Barbarian/BarbarianTray_DiceHolder_v1.0.stl": 15021384,
    "03_Barbarian/BarbarianTray_Lg_Smo_v1.0.stl": 15008884,
    "03_Barbarian/BarbarianTray_Sm_Tx_v1.0.stl": 15026484,
    "03_Barbarian/BarbarianTray_Sm_Smo_v1.0.stl": 15023184,
    "03_Barbarian/Barbarian_DT_roof_v1.1.stl": 25000084,
    "03_Barbarian/03_Barbarian_2.2.zip": 207981695,
    "03_Barbarian/Barbarian_Dec_btm_v2.2.stl": 25036284,
    "03_Barbarian/Barbarian_Dec_v2.2.stl": 25046484,
}

# The measured `Fates End - DiceJails.zip`: one root, FOUR subdirectories, 8
# models with one render beside each jail's own models, and the DJ1 deliverable
# zip. No text, and no model at the root or directly under it.
DICE_JAILS_FILES = {
    "_DiceJails/DJ1_WoodenCrate/Render_WoodenCrate.png": 427937,
    "_DiceJails/DJ4_Mimic/Mimic_Render.png": 231263,
    "_DiceJails/DJ3_GelatinousCube/Cube_Render3.png": 347707,
    "_DiceJails/DJ2_StoneJail/Render_StoneJail_02.png": 424941,
    "_DiceJails/DJ1_WoodenCrate/DJ1_Crate_Top_EasyOpen_v1.2.stl": 15043984,
    "_DiceJails/DJ1_WoodenCrate/DJ1_Crate_Top_v1.2.stl": 15540984,
    "_DiceJails/DJ1_WoodenCrate/DJ1_CrateEasyPrint_v1.3.stl": 25783984,
    "_DiceJails/DJ1_WoodenCrate/DJ1_Crate_v1.2.stl": 15486784,
    "_DiceJails/DJ2_StoneJail/DJ2_StoneJail_v1.2.stl": 15017284,
    "_DiceJails/DJ2_StoneJail/DJ2_StoneJail_top_v1.2.stl": 15744384,
    "_DiceJails/DJ4_Mimic/DJ_Mimic_v2.0.stl": 25381184,
    "_DiceJails/DJ3_GelatinousCube/DJ_Cube_v1.6.stl": 25003784,
    "_DiceJails/DJ1_WoodenCrate/DJ1_Crate_v1.2.zip": 51237946,
}


def fates_tower_files() -> Dict[str, int]:
    """The measured Fates End tower: 14 models, 3 previews and a nested zip, no text."""
    return dict(FATES_TOWER_FILES)


def dice_jails_files() -> Dict[str, int]:
    """The measured DiceJails shape: 8 models across FOUR subdirectories of ONE root."""
    return dict(DICE_JAILS_FILES)


def test_single_root_archive_offers_the_fates_end_tower_shape() -> None:
    """R6: 14 models, 3 previews and a nested zip under one root and no text.

    R3 refuses this listing for want of a text file - exactly the hole the clause
    fills. The nested deliverable zip is an entry of the listing like any other:
    it counts as an archive and changes nothing else. Mutation: require a text
    file and the facts are ``None`` again; drop the single-root guard and the
    pile cases below are offered.
    """
    snapshot_ = snapshot("03_Fates End - Barbarian Tower", fates_tower_files())

    assert bundle_detect.detect_bundle_decision(snapshot_, {}).refusal == bundle_detect.REFUSAL_R3_TRIO_NO_TEXT

    facts = bundle_detect.single_root_archive_facts(snapshot_)

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_ARCHIVE_BUNDLE == "archive-bundle"
    assert facts.confidence == bundle_detect.CONFIDENCE_LOW
    assert facts.metadata_shape == bundle_detect.SHAPE_ARCHIVE_ROOT == "archive-root"
    assert (
        facts.creator,
        facts.licence,
        facts.licence_url,
        facts.source_url,
        facts.thing_id,
        facts.description,
    ) == (None, None, None, None, None, None)
    assert (facts.counts.model, facts.counts.image, facts.counts.text) == (14, 3, 0)
    assert facts.counts.archive == 1, "the vendor's nested deliverable zip is an entry, not a model"
    assert facts.cover_entry == FATES_TOWER_COVER, "the largest image is the cover"
    assert len(facts.model_entries) == 14

    decision = bundle_detect.archive_bundle_decision(snapshot_, {})
    assert decision.refusal is None
    assert decision.facts == facts


def test_the_clause_catches_the_dice_jails_shape_r3s_depth_bound_misses() -> None:
    """8 models across FOUR subdirectories of ONE root is one archive, not a pile.

    The interpretation is asserted FIRST: R3's own-depth count is 0 for this
    listing, so ``detect_bundle_decision`` refuses it. The archive IS the wrapper,
    which is why the single-root test replaces the depth bound here rather than
    sitting next to it. Mutation: consult ``_own_depth_model_count`` inside
    ``single_root_archive_facts`` and the clause returns ``None`` for the very
    shape it was written for.
    """
    snapshot_ = snapshot("Fates End - DiceJails", dice_jails_files())

    assert bundle_detect._own_depth_model_count(snapshot_) == 0
    assert bundle_detect.detect_bundle_decision(snapshot_, {}).facts is None
    assert bundle_detect.single_root_archive_facts(snapshot_) is not None

    composed = bundle_detect.archive_bundle_decision(snapshot_, {})

    assert composed.refusal is None
    assert composed.facts is not None
    assert composed.facts.detector == bundle_detect.DETECTOR_ARCHIVE_BUNDLE
    assert (composed.facts.counts.model, composed.facts.counts.image) == (8, 4)
    assert composed.facts.counts.archive == 1, "the DJ1 deliverable zip is an entry, not a model"


def test_models_only_archives_stay_refused_at_the_zip_root_and_under_one_root() -> None:
    """No image means no evidence it is one thing rather than a dump.

    The zip-root listing has no wrapper and the rooted one has a single root, so
    only the missing image differs from the offered shape; both stay refused at
    the primary R3 refusal. Mutation: relax the image leg and both become rows -
    the 9 measured models-only archives each cost a low-confidence row.
    """
    at_top = snapshot("model-only-top", {f"part{index}.stl": 10 + index for index in range(4)})
    under_root = snapshot("model-only-root", {f"Root/part{index}.stl": 10 + index for index in range(3)})

    assert bundle_detect.common_entry_root(entry.rel_path for entry in at_top.entries) is None
    assert bundle_detect.common_entry_root(entry.rel_path for entry in under_root.entries) == "Root"
    for snapshot_ in (at_top, under_root):
        assert bundle_detect.single_root_archive_facts(snapshot_) is None
        decision = bundle_detect.archive_bundle_decision(snapshot_, {})
        assert decision.facts is None
        assert decision.refusal == bundle_detect.REFUSAL_R3_TRIO_NO_IMAGE


def test_three_hundred_root_directories_are_a_pile_and_stay_refused() -> None:
    """A single root is the pile guard: 300 roots with a model and an image each.

    Mutation: accept the first root instead of the common one and this becomes a
    row; the measured ``All Pokemon.zip`` pile is the same shape at scale.
    """
    files: Dict[str, int] = {}
    for index in range(300):
        files[f"Root{index}/part.stl"] = 10
        files[f"Root{index}/preview.jpg"] = 20

    pile = snapshot("pile", files)

    assert bundle_detect.common_entry_root(entry.rel_path for entry in pile.entries) is None
    assert bundle_detect.single_root_archive_facts(pile) is None

    primary = bundle_detect.detect_bundle_decision(pile, {})
    composed = bundle_detect.archive_bundle_decision(pile, {})

    assert composed.facts is None
    assert composed.refusal == primary.refusal == bundle_detect.REFUSAL_R3_NO_OWN_DEPTH_MODEL


def test_the_four_root_near_miss_stays_refused() -> None:
    """Fates End Dice Jails 5-8: 8 models and 4 images across FOUR roots.

    Every part would qualify alone and the archive is still four things - the one
    multi-root near-miss. Mutation: turn the guard into ``any root`` and this is
    one row.
    """
    files: Dict[str, int] = {}
    for jail in ("DJ5_ChairOFShame", "DJ6_IronMaiden", "DJ7_BagOfHolding", "DJ8_Altar"):
        files[f"{jail}/part-a.stl"] = 10
        files[f"{jail}/part-b.stl"] = 11
        files[f"{jail}/preview.jpg"] = 20

    near_miss = snapshot("Fates End Dice Jails 5-8", files)

    assert bundle_detect.single_root_archive_facts(near_miss) is None

    primary = bundle_detect.detect_bundle_decision(near_miss, {})
    composed = bundle_detect.archive_bundle_decision(near_miss, {})

    assert composed.facts is None
    assert composed.refusal == primary.refusal == bundle_detect.REFUSAL_R3_NO_OWN_DEPTH_MODEL


def test_the_all_pokemon_pile_keeps_its_primary_refusal() -> None:
    """``All Pokemon.zip``: 607 roots, 2 models at depth 2, 2096 images, 607 text.

    The 2 are the only entries under the detector's own ``MODEL_SUFFIXES``. The
    1626 ``.obj``, 1627 ``.blend`` and 226 ``.fbx`` beside them are not extensions
    the import door accepts, and the 3255 the ticket counted is ``.obj`` +
    ``.blend`` + ``.stl`` written up as a model count. A text file already keeps
    R6 away, and the many roots would keep it away too: the refusal is R3's
    own-depth one, unchanged by the new clause.
    """
    files: Dict[str, int] = {
        "Root0/models/bulbasaur.stl": 100,
        "Root0/models/charmander.stl": 101,
    }
    for index in range(607):
        files[f"Root{index}/notes.txt"] = 5
        files[f"Root{index}/card-a.jpg"] = 20
        files[f"Root{index}/card-b.jpg"] = 21
        files[f"Root{index}/card-c.jpg"] = 22
    for index in range(2096 - 607 * 3):
        files[f"Root{index}/extra.jpg"] = 30

    pile = snapshot("All Pokemon", files)
    counts = bundle_detect._counts(pile)

    assert (counts.model, counts.image, counts.text) == (2, 2096, 607)
    assert bundle_detect.common_entry_root(entry.rel_path for entry in pile.entries) is None
    assert bundle_detect.single_root_archive_facts(pile) is None

    primary = bundle_detect.detect_bundle_decision(pile, {})
    composed = bundle_detect.archive_bundle_decision(pile, {})

    assert composed.facts is None
    assert composed.refusal == primary.refusal == bundle_detect.REFUSAL_R3_NO_OWN_DEPTH_MODEL


def test_a_loose_file_archive_has_no_wrapper_and_stays_refused() -> None:
    """Models and images at the zip top level have no single root, so no wrapper.

    Mutation: let ``common_entry_root`` accept a top-level file as a root and a
    loose-file archive becomes a bundle on no evidence of a wrapper at all.
    """
    loose = snapshot(
        "loose",
        {"part-a.stl": 10, "part-b.stl": 11, "preview.jpg": 20, "card.jpg": 21},
    )

    assert bundle_detect.common_entry_root(entry.rel_path for entry in loose.entries) is None
    assert bundle_detect.single_root_archive_facts(loose) is None

    decision = bundle_detect.archive_bundle_decision(loose, {})

    assert decision.facts is None
    assert decision.refusal == bundle_detect.REFUSAL_R3_TRIO_NO_TEXT


def test_a_high_archive_and_r3s_trio_are_returned_unchanged() -> None:
    """The wrapper's whole safety property: R6 runs after R1/R2/R4/R3, never instead.

    Mutation: evaluate R6 before the primary decision, or return its facts
    unconditionally, and this README pair reads ``archive-bundle`` / ``low`` while
    R3's trio is downgraded to a different detector.
    """
    high = snapshot(
        "Moai",
        {"README.txt": 100, "LICENSE.txt": 100, "files/part.stl": 200, "images/a.jpg": 60},
    )

    assert bundle_detect.single_root_archive_facts(high) is None, "a text file is R3's business, not R6's"

    high_decision = bundle_detect.archive_bundle_decision(high, r1_docs())

    assert high_decision.refusal is None
    assert high_decision.facts is not None
    assert high_decision.facts.detector == bundle_detect.DETECTOR_THINGIVERSE_README
    assert high_decision.facts.confidence == bundle_detect.CONFIDENCE_HIGH
    assert high_decision.facts.metadata_shape == bundle_detect.SHAPE_ROOT_DOCS

    trio = snapshot("Trio", {"Root/part.stl": 10, "Root/preview.jpg": 20, "Root/notes.txt": 5})
    trio_decision = bundle_detect.archive_bundle_decision(trio, {})

    assert trio_decision.refusal is None
    assert trio_decision.facts is not None
    assert trio_decision.facts.detector == bundle_detect.DETECTOR_GENERIC
    assert trio_decision.facts.confidence == bundle_detect.CONFIDENCE_LOW


def test_macosx_cruft_cannot_make_a_real_bundle_rootless() -> None:
    """The root guard reuses ``common_entry_root``, which already skips ``__MACOSX``.

    Mutation: re-implement the root test without the cruft skip and rsync junk
    strips a real vendor bundle of its wrapper.
    """
    files = fates_tower_files()
    files["__MACOSX/03_Barbarian/._Barbarian_DT_v2.0.stl"] = 5
    files["__MACOSX/._03_Barbarian"] = 4
    snapshot_ = snapshot("03_Fates End - Barbarian Tower", files)

    assert bundle_detect.common_entry_root(entry.rel_path for entry in snapshot_.entries) == "03_Barbarian"
    assert bundle_detect.single_root_archive_facts(snapshot_) is not None


def test_a_text_file_hands_the_listing_back_to_r3_even_at_depth_two() -> None:
    """The clause fills only the missing-document hole; it never second-guesses R3.

    The listing is the DiceJails depth (own-depth count 0) with one text file
    added: model, image and single root are all satisfied, only the text differs,
    and the refusal must be the PRIMARY one - ``R3:no-own-depth-model`` - rather
    than an offering. The untexted twin of the same listing IS offered, which is
    what makes the text the only difference. Mutation: drop the ``counts.text``
    guard and this becomes a row while
    ``Fates End_Bonus_Delivery-Banks.zip`` does too.
    """
    buried = dice_jails_files()
    no_text = snapshot("Buried No Text", buried)
    with_text = snapshot("Buried With Text", {**buried, "notes.txt": 5})

    assert bundle_detect.single_root_archive_facts(no_text) is not None
    assert bundle_detect.single_root_archive_facts(with_text) is None

    primary = bundle_detect.detect_bundle_decision(with_text, {})
    composed = bundle_detect.archive_bundle_decision(with_text, {})

    assert composed.facts is None
    assert composed.refusal == primary.refusal == bundle_detect.REFUSAL_R3_NO_OWN_DEPTH_MODEL


# --- the rule-set stamp and the refusal vocabulary (Mf-9ygi) ----------------

# The version a report records, and the stamp of the tables as imported. Both are
# LITERALS on purpose: an edit to any row-deciding table changes the derived hash
# and this test reds, which is the whole point of the stamp - the 114 -> 112 delta
# of Mf-9ygi could not be explained because nothing recorded what the rules were.
# The two stamped constants that live in ANOTHER module, with the name they have
# there: the sweep has to rebind the owner, because `rule_set_tables` reads the
# owning module and a patch of this module's own name would move nothing (Mf-94od).
RULE_SET_TABLE_HOMES = {
    "SKIPPED_FILENAMES": (library_scan, "IGNORED_FILENAMES"),
    "HIDDEN_NAME_PREFIX": (library_scan, "HIDDEN_NAME_PREFIX"),
}

RULE_SET_VERSION = "2026-11-05.2"
RULE_SET_STAMP = "0f317ee0fa2ff200a703a8c10be1c6f565d50d4263b3629a1004dffee7c0c911"

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
    "DETECTOR_ARCHIVE_BUNDLE": "archive-bundle-v2",
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
    "SHAPE_ARCHIVE_ROOT": "archive-root-v2",
    "MODEL_SUFFIXES": frozenset({".obj"}),
    "IMAGE_SUFFIXES": frozenset({".webp"}),
    # The scan's own two tables, stamped because the model count now asks them
    # (Mf-94od). Each replacement differs from the shipped value on its own.
    "SKIPPED_FILENAMES": frozenset({"~$lock.tmp"}),
    "HIDDEN_NAME_PREFIX": "~",
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
    "DETECTOR_COMPOUND": "compound-bundle-v2",
    "SHAPE_COMPOUND": "compound-v2",
    "COMPOUND_BUNDLES_ENABLED": False,
    "COMPOUND_MIN_GROUPS": 5,
    "COMPOUND_TOKEN_MIN_LENGTH": 9,
    "COMPOUND_GLUE_TOKENS": frozenset({"vs"}),
    "COMPOUND_REPO_MARKERS": frozenset({".hg"}),
    "COMPOUND_GROUP_DOC_STEMS": frozenset({"copying"}),
    "COMPOUND_FOREIGN_ID": re.compile(r"(?i)^vendor-(\d+)-"),
    "COMPOUND_OWN_ID": re.compile(r"(?i)^thing-(\d+)-"),
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
    investigation lacked. This bump is Mf-94od's rule table change: the model
    count asks the library scan's own basename predicate, so `SKIPPED_FILENAMES`
    and `HIDDEN_NAME_PREFIX` join the stamp and the version moves with them. The
    previous bump was Mf-8xrc's ten `COMPOUND_*` tables.
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
            # The two scan tables live in `library_scan` and are stamped by value
            # from there, so the sweep must rebind them WHERE THEY LIVE: patching
            # this module's own name would prove nothing and would not move the
            # stamp (Mf-94od).
            owner, attribute = RULE_SET_TABLE_HOMES.get(name, (bundle_detect, name))
            patched.setattr(owner, attribute, replacement)
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
    # Mf-94od's entry: the two scan tables are captured by value, from the scan
    # module itself, and the predicate is the SAME function object.
    assert tables["SKIPPED_FILENAMES"] == sorted(library_scan.IGNORED_FILENAMES)
    assert tables["HIDDEN_NAME_PREFIX"] == library_scan.HIDDEN_NAME_PREFIX
    assert bundle_detect.is_skipped_name is library_scan.is_skipped_name
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


# --- R7: one product in many parts (Mf-8xrc) ---------------------------------

# The real directory names, because every number below was measured on these
# trees: no invented fixture can prove the owner's library reads as one row.
STAGETOP = "Stagetop"
STAGETOP_WAVE_ONE = "StageTop Wave One - Personal Use"
STAGETOP_WAVE = "Wave 1.5"
ROCKET_GROUPS = (
    "The_Rocket--Circles--Side_Spool_Mount",
    "The_Rocket--Dots--Side_Spool_Mount",
    "The_Rocket--Flowers--Side_Spool_Mount",
    "The_Rocket--Hearts--Side_Spool_Mount",
    "The_Rocket--Solid--Side_Spool_Mount",
    "The_Rocket--Squares--Side_Spool_Mount",
    "The_Rocket--Stars--Side_Spool_Mount",
    "The_Rocket--Triangles--Side_Spool_Mount",
)
XBOX_GROUP = "Wallmount for Lenovo Tab M10 Plus Gen3 - 5461913"


def _models(prefix: str, count: int, suffix: str = ".stl") -> Dict[str, int]:
    """``count`` models under ``prefix``, named so the fixture never depends on order."""
    return {f"{prefix}/part{index:02d}{suffix}": 10 for index in range(count)}


def _root_models(count: int) -> Dict[str, int]:
    """``count`` models at depth 0: the folder's own files, nothing below them."""
    return {f"part{index:02d}.stl": 10 for index in range(count)}


def stagetop_files() -> Dict[str, int]:
    """``import/Stagetop`` by this module's own table: 45 in wave one, 15 in Wave 1.5.

    The wave-one ``.stl`` sit one level down inside the group's own payload
    directories (Core / Legs / Accessories / Rails) and the four ``.step`` files
    sit beside ``PlayTile - Blank``'s four ``.stl``, so the shape is a real
    nested tree rather than a flat list. ``PlayTiles/Accessory`` is a
    ``title-name`` row on disk and is a group here too. The ``.step`` files are
    what makes 60 and not 56: ``.step`` is a ``MODEL_SUFFIXES`` member, and the
    plan's 56 was a ``.stl``-only count.
    """
    files: Dict[str, int] = {}
    files.update(_models(f"{STAGETOP_WAVE_ONE}/Accessories", 5))
    files.update(_models(f"{STAGETOP_WAVE_ONE}/Core", 3))
    files.update(_models(f"{STAGETOP_WAVE_ONE}/Legs", 5))
    for play_tile, count in (("PlayTile - 1x1", 4), ("PlayTile - 2x2", 4), ("PlayTile - 4x4", 3)):
        files.update(_models(f"{STAGETOP_WAVE_ONE}/PlayTiles/{play_tile}", count))
    blank = f"{STAGETOP_WAVE_ONE}/PlayTiles/PlayTile - Blank"
    files.update(_models(blank, 4))
    files.update(_models(blank, 4, suffix=".step"))
    files.update(_models(f"{STAGETOP_WAVE_ONE}/PlayTiles/Accessory", 4))
    files.update(_models(f"{STAGETOP_WAVE_ONE}/Rails/Rail - Elite", 5))
    files.update(_models(f"{STAGETOP_WAVE_ONE}/Rails/Rail - Standard", 4))
    files.update(_models(STAGETOP_WAVE, 15))
    return files


def rocket_files() -> Dict[str, int]:
    """``import/Rocket Filement support``: 8 groups of 6 REAL models, plus the sidecars.

    The measured disk holds 6 real ``.stl`` under ``<group>/<group>/`` and 6
    AppleDouble ``._*`` copies under the NESTED ``<group>/__MACOSX/<group>/``.
    Both are on disk; only the six real ones are library content, so the group
    reads 6 and the row 48 (Mf-94od).
    """
    files: Dict[str, int] = {}
    for group in ROCKET_GROUPS:
        files.update(_models(f"{group}/{group}", 6))
        files.update({f"{group}/__MACOSX/{group}/._part{index:02d}.stl": 10 for index in range(6)})
    return files


def xbox_files() -> Dict[str, int]:
    """``import/xbox360_shell``: 27 of its own models plus one foreign group of 4."""
    files = _root_models(27)
    files.update(_models(f"{XBOX_GROUP}/files", 4))
    return files


def test_a_compound_folder_is_low_name_derived_and_names_every_group() -> None:
    """60 parts in two editions is ONE low row that names both groups.

    Mutation: drop ``dataclasses.replace(facts, groups=groups)`` and the row loses
    its breakdown; count ``.stl`` only and 60 becomes 56 with groups 41/15, which
    is the plan's stale number rather than this module's own suffix table.
    """
    facts = bundle_detect.compound_bundle_facts(snapshot(STAGETOP, stagetop_files()))

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_COMPOUND
    assert facts.confidence == bundle_detect.CONFIDENCE_LOW
    assert facts.metadata_shape == bundle_detect.SHAPE_COMPOUND
    assert facts.counts.model == 60
    assert facts.groups == (
        bundle_detect.BundleGroup(STAGETOP_WAVE_ONE, 45, False),
        bundle_detect.BundleGroup(STAGETOP_WAVE, 15, False),
    )
    assert bundle_detect._compound_family_token(facts.groups) == "wave"
    assert facts.title == bundle_detect.display_name(STAGETOP) == "Stagetop"
    # LOW and name-derived: nothing here states provenance.
    assert facts.creator is None
    assert facts.licence is None
    assert facts.licence_url is None
    assert facts.source_url is None
    assert facts.thing_id is None
    assert facts.description is None


def test_detect_bundle_still_refuses_the_snapshot_compound_facts_offers() -> None:
    """R7 is not a ``detect_bundle`` branch: the folder walk is deepest-first.

    Mutation: add a compound branch to ``detect_bundle_decision`` and this very
    snapshot starts answering from a walk pass that would hand the parent's row
    to its first child - the reason R5 is a post-pass too.
    """
    files = stagetop_files()

    assert bundle_detect.compound_bundle_facts(snapshot(STAGETOP, files)) is not None
    assert bundle_detect.detect_bundle(snapshot(STAGETOP, files), {}) is None


def test_repository_metadata_refuses_a_compound() -> None:
    """A checkout holding several products is descended, never merged into one row.

    Mutation: drop the marker leg and a git dump with a shared token becomes a
    compound row - endorphin's failure mode. Both reachable marker shapes are
    covered: a hidden FILE the walk yields (``Gemfile``) and a Jekyll entry
    (``_layouts/``), since hidden DIRECTORIES are pruned before this module sees
    them. The NEGATIVE witness is a marker one level INSIDE a group: the marker
    leg is the folder's own first segments only, so a payload ``Gemfile`` is not
    evidence about the folder being judged.
    """
    for marker in ("Gemfile", "_layouts/page.html"):
        files = dict(stagetop_files())
        files[marker] = 10
        assert bundle_detect.compound_bundle_facts(snapshot(STAGETOP, files)) is None, marker

    deep_marker = dict(stagetop_files())
    deep_marker[f"{STAGETOP_WAVE_ONE}/Gemfile"] = 10
    assert bundle_detect.compound_bundle_facts(snapshot(STAGETOP, deep_marker)) is not None


def test_two_groups_with_their_own_vendor_ids_are_not_compound() -> None:
    """Each group carrying its own thing id is its own product, not a part.

    Mutation: drop the own-id leg and two shipped products merge on nothing but a
    shared word. The family token is asserted to EXIST here, so the refusal
    cannot be the Leg A token leg wearing the own-id leg's name.
    """
    files = dict(_models("Octopus Enclosure - 5414731/files", 4))
    files.update(_models("Octopus Mount - 4591932/files", 2))
    judged = snapshot("Unsorted", files)

    assert bundle_detect._compound_family_token(bundle_detect.compound_groups(judged)) == "octopus"
    assert bundle_detect.compound_bundle_facts(judged) is None


def test_a_foreign_group_is_named_and_never_split() -> None:
    """27 own models plus somebody else's 4 is ONE row that names the foreign group.

    Mutation: split on the foreign group and its 4 parts become a second row;
    drop the group breakdown and the operator cannot see what to split at
    confirmation (C2).
    """
    facts = bundle_detect.compound_bundle_facts(snapshot("xbox360_shell", xbox_files()))

    assert facts is not None
    assert facts.detector == bundle_detect.DETECTOR_COMPOUND
    assert facts.confidence == bundle_detect.CONFIDENCE_LOW
    assert facts.counts.model == 31
    assert facts.groups == (bundle_detect.BundleGroup(XBOX_GROUP, 4, True),)


def test_a_foreign_group_is_known_by_its_own_root_document() -> None:
    """``<group>/README.txt`` is somebody's thing; ``<group>/docs/README.txt`` is payload.

    Mutation: match the document at any depth and every group that vendors a docs
    folder reads as foreign, which turns ordinary piles into compounds. All three
    stems the predicate accepts are covered - ``readme``/``license``/``licence`` -
    so a narrowed stem set reds here rather than silently losing a foreign flag.
    """
    group = "Gadget Bracket"
    for document in ("README.txt", "LICENSE.txt", "LICENCE"):
        shallow = {f"{group}/part00.stl": 10, f"{group}/{document}": 10}
        assert bundle_detect.compound_groups(snapshot("shallow", shallow))[0].foreign is True, document

    deep = {f"{group}/part00.stl": 10, f"{group}/docs/README.txt": 10}
    assert bundle_detect.compound_groups(snapshot("deep", deep))[0].foreign is False

    own_heavy = _root_models(3)
    own_heavy.update(deep)
    assert bundle_detect.compound_bundle_facts(snapshot("deep", own_heavy)) is None


def test_leg_b_does_not_merge_two_readme_bearing_unrelated_groups() -> None:
    """Two README-bearing sub-projects under a root-heavy folder stay refused.

    Mutation: drop Leg B's ``any(COMPOUND_FOREIGN_ID ...)`` requirement and two
    unrelated projects merge into one row on nothing but "the folder's own models
    are the majority".
    """
    files = _root_models(27)
    files.update({"Widget Case/part00.stl": 10, "Widget Case/README.txt": 10})
    files.update({"Gadget Stand/part00.stl": 10, "Gadget Stand/README.txt": 10})
    judged = snapshot("pile", files)

    assert [group.foreign for group in bundle_detect.compound_groups(judged)] == [True, True]
    assert bundle_detect.compound_bundle_facts(judged) is None


def test_leg_b_needs_every_group_foreign_and_dominant_own_models() -> None:
    """Leg B's three bounds, each with the shape that breaks it.

    Mutation: drop ``all(foreign)`` and the folder's own sub-project is swallowed;
    drop the majority floor and 4 own models claim a 10-model pile; drop the
    vendor-id requirement (the previous test's shape) and unrelated groups merge.
    The passing shape shares NO token between its groups, which proves Leg B asks
    for no family evidence: the vendor id and the majority are its evidence.
    """
    # (a) one foreign group beside one group of the folder's own: refused.
    one_own = _root_models(27)
    one_own.update(_models("Widget Mount - 5461913/files", 2))
    one_own.update(_models("Gadget Bracket/files", 2))

    # (b) every group foreign with a vendor id, own models the minority: the
    # anti-swallow floor refuses.
    minority = _root_models(4)
    minority.update(_models("Widget Mount - 5461913/files", 5))
    minority.update(_models("Gadget Bracket/files", 5))
    minority.update({"Gadget Bracket/README.txt": 10})

    # (c) the passing shape.
    passing = _root_models(27)
    passing.update(_models("Widget Mount - 5461913/files", 2))
    passing.update(_models("Gadget Bracket/files", 2))
    passing.update({"Gadget Bracket/README.txt": 10})

    assert bundle_detect.compound_bundle_facts(snapshot("one-own", one_own)) is None
    assert bundle_detect.compound_bundle_facts(snapshot("minority", minority)) is None

    facts = bundle_detect.compound_bundle_facts(snapshot("passing", passing))
    assert facts is not None
    assert facts.groups == (
        bundle_detect.BundleGroup("Gadget Bracket", 2, True),
        bundle_detect.BundleGroup("Widget Mount - 5461913", 2, True),
    )
    assert bundle_detect._compound_family_token(facts.groups) is None


def test_two_groups_with_their_own_vendor_ids_refuse_the_leg_b_shape() -> None:
    """The own-id refusal and the group floor are ONE constant, and this is its witness.

    The shape is Leg B on every other count - models of its own, a strict majority,
    every group foreign, vendor ids present - and only the own-id refusal keeps it
    from being one row. Mutation: raise ``COMPOUND_MIN_GROUPS`` and this folder
    becomes compound, because two own ids no longer reach the threshold that is
    also the group floor. That is the drift the single constant is meant to be
    honest about.
    """
    files = _root_models(27)
    files.update(_models("Gadget Mount - 1234567/files", 2))
    files.update(_models("Widget Bracket - 7654321/files", 2))
    judged = snapshot("pile", files)

    groups = bundle_detect.compound_groups(judged)
    assert all(group.foreign for group in groups), "every group looks foreign"
    assert all(bundle_detect.COMPOUND_OWN_ID.search(group.name) for group in groups), "and every group is a product"
    assert bundle_detect.compound_bundle_facts(judged) is None


def test_a_family_token_must_be_a_word() -> None:
    """A shared token only binds two groups when it is a product word.

    Every refused pair below shares exactly ONE token, so the refusal cannot be
    "no token at all": ``the`` is refused TWICE over (it is glue and it is not
    strictly longer than the length floor), ``with`` is glue and the only glue
    word the length floor does not also refuse, ``files`` is a part name, ``abc``
    is not strictly longer than the floor, and ``2024`` is all digits. Emptying
    ``COMPOUND_GLUE_TOKENS`` is what makes ``with widget``/``with gadget`` a
    compound, which is why that pair is the glue table's behavioural witness.
    The final pair shares ``wander`` and must FIRE, so the table cannot pass by
    refusing every shape thrown at it.
    """
    pairs = {
        "the": ("the widget", "the gadget", False),
        "with": ("with widget", "with gadget", False),
        "files": ("files widget", "files gadget", False),
        "abc": ("abc widget", "abc gadget", False),
        "2024": ("2024 widget", "2024 gadget", False),
        "wander": ("wander widget", "wander gadget", True),
    }
    for token, (first, second, fires) in pairs.items():
        files = {f"{first}/part00.stl": 10, f"{second}/part00.stl": 10}
        judged = snapshot("family", files)
        groups = bundle_detect.compound_groups(judged)
        assert bundle_detect._compound_tokens(first) & bundle_detect._compound_tokens(second) == {token}, token
        facts = bundle_detect.compound_bundle_facts(judged)
        assert (facts is not None) is fires, token
        if fires:
            assert facts is not None
            assert bundle_detect._compound_family_token(groups) == token


def test_compound_groups_are_a_function_of_the_tree_not_of_readdir_order() -> None:
    """Casefold order, so two names that disagree with raw order still sort the same.

    Mutation: sort on the raw name and ``Zeta Parts`` lands before ``alpha parts``;
    the row's breakdown would then depend on which comparison the machine chose.
    """
    files = {"Zeta Parts/part00.stl": 10, "alpha parts/part00.stl": 10}

    names = [group.name for group in bundle_detect.compound_groups(snapshot("parts", files))]

    assert names == ["alpha parts", "Zeta Parts"]


def test_apple_double_cruft_is_not_a_group() -> None:
    """A top-level ``__MACOSX`` never names a group, and its sidecar is no model.

    Mutation: group on ``parts[0]`` with no artifact check and the junk directory
    becomes a group of its own. The count is pinned at ONE on purpose: since
    Mf-94od a ``._*`` sidecar is not counted anywhere, because the library scan
    does not register it and the row describes what the library HOLDS.
    """
    files = {"Widget Wrapper/part00.stl": 10, "__MACOSX/Widget Wrapper/._part00.stl": 10}
    judged = snapshot("cruft", files)

    assert [group.name for group in bundle_detect.compound_groups(judged)] == ["Widget Wrapper"]
    assert bundle_detect._counts(judged).model == 1, "the sidecar is not a model"


# --- the model count asks the scan's own predicate (Mf-94od) -----------------


def counted_sites(judged: bundle_detect.BundleSnapshot) -> tuple:
    """Every MODEL count site, asked together so no one site can drift alone."""
    return (
        bundle_detect._counts(judged).model,
        bundle_detect._model_entries(judged),
        bundle_detect.compound_groups(judged),
        bundle_detect.root_model_count(judged),
        bundle_detect._own_depth_model_count(judged),
    )


def test_a_bare_appledouble_sidecar_is_not_a_model() -> None:
    """``a.stl`` + ``._a.stl`` in one group is ONE part, not two (the ticket's AC).

    A ``._`` sidecar is a resource fork. The library scan skips it by name, so a
    row that counts what the scan registers must skip it too. Mutation: count
    through ``classify_entry`` alone and every site below reads two.
    """
    judged = snapshot("Widget", {"a.stl": 10, "._a.stl": 10})

    counts, entries, groups, root, own = counted_sites(judged)

    assert counts == 1
    assert entries == ("a.stl",)
    assert groups == ()
    assert root == 1
    assert own == 1
    # Counted in NO bucket, OTHER included: a skipped entry is not an unclassified
    # file either. Mutation: `counts[OTHER] += 1` before the `continue` and the
    # Rocket would report `other_count: 48` with nothing else moving.
    other = bundle_detect._counts(judged)
    assert (other.model, other.image, other.text, other.other, other.archive) == (1, 0, 0, 0, 0)


def test_a_nested_macosx_appledouble_sidecar_is_not_a_model() -> None:
    """The measured Rocket shape at depth 3: ``<group>/__MACOSX/<group>/._x.stl``.

    This is the disk's own nesting, not a flat sidecar, and it is why the fix
    asks the scan's BASENAME rule instead of its junk vocabulary: the scan
    registers a non-dot file under a nested ``__MACOSX`` (measured:
    ``g/__MACOSX/g/plain.stl`` registers), so wiring ``is_junk_entry`` in would
    under-count a real part. Mutation: count the sidecars and the two groups read
    two each and the row reads four.
    """
    files: Dict[str, int] = {}
    for group in ROCKET_GROUPS[:2]:
        files[f"{group}/{group}/part.stl"] = 10
        files[f"{group}/__MACOSX/{group}/._part.stl"] = 10
    judged = snapshot("Rocket Filement support", files)

    counts, entries, groups, root, own = counted_sites(judged)

    assert counts == 2
    assert set(entries) == {f"{group}/{group}/part.stl" for group in ROCKET_GROUPS[:2]}
    assert [(group.name, group.model_count) for group in groups] == [
        (ROCKET_GROUPS[0], 1),
        (ROCKET_GROUPS[1], 1),
    ]
    assert root == 0
    assert own == 0


def test_a_hidden_model_name_is_not_counted() -> None:
    """``.hidden.stl`` is not a model, and ``is_junk_entry`` calls it NOT junk.

    That is exactly why the fix cannot be wired to the junk predicate: that
    vocabulary recognises ``._`` sidecars and OS debris, and an ordinary name with
    a leading dot is invisible to it. The scan's basename rule is the one that
    skips it, so the count asks that rule. Mutation: replace the scan predicate
    with ``entry_policy.is_junk_entry`` and this row reads two.
    """
    from entry_policy import is_junk_entry

    assert is_junk_entry(".hidden.stl") is False, "the junk predicate cannot see a bare leading dot"

    judged = snapshot("Widget", {"a.stl": 10, ".hidden.stl": 10})

    counts, entries, groups, root, own = counted_sites(judged)

    assert counts == 1
    assert entries == ("a.stl",)
    assert groups == ()
    assert root == 1
    assert own == 1


def test_an_appledouble_preview_is_still_evidence_and_the_cover_rule_sees_it() -> None:
    """The evidence view keeps the WALK reading; only the COUNT asks the scan.

    `._cover.png` is not registered by the library scan, so it is counted in no
    bucket. Narrowing the EVIDENCE view too would refuse a real bundle whose only
    preview is an AppleDouble copy - a worse error than a cover pick that is never
    attached, and an unregistered cover already has a named reason
    (`largest-image-unregistered`). Mutation: route `_cover_entry` through
    `is_counted_entry` and a bundle whose only image is a sidecar loses its face.
    The DECISION-level half of this is
    `test_a_sidecar_only_preview_still_offers_the_row` below: keeping the cover
    pick wide is worthless if the row is refused before the cover is consulted.
    """
    judged = snapshot("Widget", {"a.stl": 10, "._cover.png": 20})

    assert bundle_detect._counts(judged).image == 0, "the sidecar is not a counted image"
    assert bundle_detect._cover_entry(judged) == "._cover.png", "but it is still evidence"


def test_a_sidecar_only_preview_still_offers_the_row() -> None:
    """R3's trio gate keeps the EVIDENCE reading, so a preview is still a preview.

    The reported counts are registration-based, but the gate that decides whether
    a row is OFFERED asks whether the folder HAS a preview and a document. If it
    read the narrowed counts, a bundle whose only preview is an AppleDouble
    sidecar would newly refuse `R3:trio-no-image` - the exact "worse error"
    Decision 25 declines, and a verdict change this ticket did not earn.

    Three pins, because the verdict and the reason are separate lines: the trio
    gate must not read the counts, the `R3:trio-no-image` REASON must not either
    (a folder with a sidecar preview and no document is `trio-no-text`, not
    `trio-no-image`), and the one verdict that DOES move is the accepted risk R3 -
    a tree whose only model is a sidecar holds no geometry, so it refuses
    `R3:no-own-depth-model` instead of offering a phantom part.
    """
    judged = snapshot("Widget", {"a.stl": 10, "._cover.png": 20, "._notes.txt": 20})

    decision = bundle_detect.detect_bundle_decision(judged, {})

    assert decision.refusal is None, "a sidecar-only preview must not refuse the row"
    assert decision.facts is not None
    assert decision.facts.detector == bundle_detect.DETECTOR_GENERIC
    # ... and the row still REPORTS what the scan would register: no image, no text.
    counts = bundle_detect._counts(judged)
    assert (counts.image, counts.text) == (0, 0)

    # The REASON line is the other half: an image the walk sees but the scan does
    # not register is still an image this row is not refused FOR.
    preview_only = snapshot("Widget", {"a.stl": 10, "._cover.png": 20})
    assert (
        bundle_detect.detect_bundle_decision(preview_only, {}).refusal
        == bundle_detect.REFUSAL_R3_TRIO_NO_TEXT
    ), "the refusal must name the missing document, not the present preview"

    # The verdict that DOES move, and it is the honest one: no registered model.
    phantom_only = snapshot("Widget", {"._a.stl": 10, "._cover.png": 20, "._notes.txt": 20})
    assert (
        bundle_detect.detect_bundle_decision(phantom_only, {}).refusal
        == bundle_detect.REFUSAL_R3_NO_OWN_DEPTH_MODEL
    ), "a tree whose only model is a sidecar holds no geometry"


def test_the_document_candidates_keep_the_walk_reading_too() -> None:
    """The third evidence consumer: a sidecar document is still worth opening.

    `generic_doc_candidates` decides which file the caller OPENS for provenance,
    which is evidence rather than a count. Mutation: filter it through
    `is_counted_entry` and a tree whose only notes are AppleDouble loses its one
    chance at a title and a licence.
    """
    judged = snapshot("Widget", {"a.stl": 10, "._notes.txt": 20})

    assert bundle_detect.generic_doc_candidates(judged) == ("._notes.txt",)
    assert bundle_detect._counts(judged).text == 0, "and it is still not a counted document"


def test_the_count_driven_doors_read_the_narrowed_counts_on_purpose() -> None:
    """R6 and R7 decide FROM counts, so they move with the rule - and they should.

    The R3 ladder keeps the walk reading because "does this folder have a
    preview" is evidence. These two doors ask a different question and answer it
    about what the library HOLDS: R6 asks whether the vendor shipped a document
    and a preview beside the models, R7 asks whether every group is a real pile of
    parts. A resource fork is neither, so a skipped entry must not count for them.

    Mutation, both directions: count a `._README.txt` as text and the archive row
    that R6 was written for stops being offered; count sidecar-only groups as
    groups and a folder holding no geometry becomes a compound row.
    """
    # R6's trio reads the buckets: a sidecar document is not a document, so the
    # archive stays offerable ...
    archived = snapshot(
        "archive",
        {"Root/part.stl": 10, "Root/render.png": 20, "Root/._README.txt": 30},
    )
    assert bundle_detect._counts(archived).text == 0
    assert bundle_detect.single_root_archive_facts(archived) is not None
    # ... while a REAL document keeps R6 out of R3's way, exactly as before.
    with_text = snapshot(
        "archive",
        {"Root/part.stl": 10, "Root/render.png": 20, "Root/README.txt": 30},
    )
    assert bundle_detect.single_root_archive_facts(with_text) is None

    # R7's group leg reads them too: groups whose models are all AppleDouble name
    # no group at all, so the folder is not a compound row.
    sidecar_groups = {
        f"{group}/__MACOSX/{group}/._part.stl": 10 for group in ROCKET_GROUPS[:3]
    }
    judged = snapshot("Rocket Filement support", sidecar_groups)
    assert bundle_detect.compound_groups(judged) == ()
    assert bundle_detect.compound_bundle_facts(judged) is None, "no geometry, no compound row"


def test_bundle_detect_asks_the_scan_its_own_predicate() -> None:
    """One rule, two doors: the detector re-exports the scanner's own predicate.

    The truth table includes the case the plan gate had to correct - a non-dot
    file under a nested ``__MACOSX`` is REGISTERED - and ``include_hidden=True``
    is asked of the same function rather than re-derived. Mutation: give the
    detector its own copy of the rule and the identity assertions red before any
    count can drift.
    """
    assert bundle_detect.is_skipped_name is library_scan.is_skipped_name
    assert bundle_detect.is_counted_entry("a.stl") is True

    counted = {
        "a.stl": True,
        "._a.stl": False,
        ".hidden.stl": False,
        "__MACOSX/a.stl": True,
        "g/__MACOSX/g/plain.stl": True,
        "g/plain.stl": True,
        "Thumbs.db": False,
    }
    for rel_path, expected in counted.items():
        assert bundle_detect.is_counted_entry(rel_path) is expected, rel_path

    # The nested `__MACOSX` entry above is the premise the whole fix rests on, and
    # the SCANNER side of it is pinned where it belongs:
    # `test_library_scan.py::test_a_nested_macosx_tree_registers_its_dead_file_and_skips_the_sidecar`.

    # The hidden door is the SAME function, so admitting dot names is one flag.
    assert library_scan.is_skipped_name("._a.stl", include_hidden=True) is False
    assert library_scan.is_skipped_name(".hidden.stl", include_hidden=True) is False
    assert library_scan.is_skipped_name(".DS_Store", include_hidden=True) is True


def test_a_single_model_bearing_group_is_not_compound() -> None:
    """One group is a wrapper, not a product in several parts.

    Mutation: lower ``COMPOUND_MIN_GROUPS`` to 1 and a plain wrapper with a
    model-bearing child becomes a compound row. The last two shapes pin the two
    bounds Leg B still needs: the vendor id, and a STRICT majority.
    """
    assert bundle_detect.compound_bundle_facts(snapshot("wrapper", dict(_models("Widget", 3)))) is None

    readme_only = _root_models(3)
    readme_only.update({"Gadget/part00.stl": 10, "Gadget/README.txt": 10})
    assert bundle_detect.compound_bundle_facts(snapshot("readme-only", readme_only)) is None

    equal = _root_models(3)
    equal.update(_models("Widget Mount - 5461913/files", 3))
    assert bundle_detect.compound_bundle_facts(snapshot("equal", equal)) is None


def test_root_models_are_strictly_at_depth_zero_and_the_wave_15_leg_is_not_one_of_them() -> None:
    """``root_model_count`` is depth 0 only; ``_own_depth_model_count`` cannot serve R7.

    Mutation: build R7 on ``_own_depth_model_count`` and every wrapper with one
    model-bearing child fires, because that function answers 15 for
    ``import/Stagetop`` - the ``Wave 1.5`` models, which are a CHILD's pile, and
    a wrapper would read them as a product of its own.
    """
    stagetop = snapshot(STAGETOP, stagetop_files())

    assert bundle_detect.root_model_count(stagetop) == 0
    assert bundle_detect._own_depth_model_count(stagetop) == 15, "the leg R7 must not use"

    xbox = snapshot("xbox360_shell", xbox_files())
    assert bundle_detect.root_model_count(xbox) == 27, "the foreign group's 4 are not the folder's own"
    assert bundle_detect._own_depth_model_count(xbox) == 27


def test_compound_facts_is_none_when_the_switch_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """One switch turns R7 off for every caller, and the stamp is not order-dependent.

    Mutation: read the flag anywhere but inside ``compound_bundle_facts`` (or
    nowhere) and the rule cannot be turned off at all. The Rocket is the
    ticket's other witness: 48 parts in 8 groups while the switch is on, six per
    group, because the 48 AppleDouble sidecars are not library content (Mf-94od).
    The ``monkeypatch.context()`` restores the flag, so a later stamp comparison
    still sees the shipped tables.
    """
    judged = snapshot("Rocket Filement support", rocket_files())

    with monkeypatch.context() as patched:
        patched.setattr(bundle_detect, "COMPOUND_BUNDLES_ENABLED", False)
        assert bundle_detect.compound_bundle_facts(judged) is None

    facts = bundle_detect.compound_bundle_facts(judged)
    assert facts is not None
    assert facts.counts.model == 48
    assert tuple(group.name for group in facts.groups) == ROCKET_GROUPS
    assert all(group.model_count == 6 for group in facts.groups)
    assert bundle_detect._compound_family_token(facts.groups) == "mount"
    assert bundle_detect.rule_set_stamp() == RULE_SET_STAMP, "the context restores the flag"
