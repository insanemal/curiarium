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
  and the cover pick including its tie-break.
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


def snapshot(bundle_name: str, files: Mapping[str, int]) -> bundle_detect.BundleSnapshot:
    """A snapshot from a {bundle-relative path: size} mapping."""
    return bundle_detect.BundleSnapshot(
        bundle_name=bundle_name,
        rel_dir=bundle_name,
        entries=tuple(bundle_detect.BundleEntry(rel_path=path, size=size) for path, size in files.items()),
    )


def r1_docs() -> Dict[str, str]:
    return {"README.txt": README_TEXT, "LICENSE.txt": LICENSE_TEXT}


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
    files = {"a/part.stl": 10, "b/part.stl": 20, "cover.jpg": 30, "notes.txt": 40}
    facts = bundle_detect.detect_bundle(snapshot("two-stems", files), {})

    assert facts is not None
    assert facts.counts.model == 2, "both same-stem files survive classification"
    assert facts.model_entries == ("a/part.stl", "b/part.stl")
    assert len(set(facts.model_entries)) == 2


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
