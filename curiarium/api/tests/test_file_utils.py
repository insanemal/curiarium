"""Tests for `file_utils`: the bundle-member path helpers (Mf-1agz).

`is_bundle_member` and `is_generic_container_root` decide what a detected bundle
owns and which archive entry roots are meaningless, so they are pinned beside
the module that owns them. They moved here from the bundle detector's tests with
the helpers themselves.
"""

import pytest

import file_utils

CLOCKWORK_ROOT = "17_ClockworkMonster"


def test_is_bundle_member_is_strictly_below_and_raw() -> None:
    assert file_utils.is_bundle_member("A/B/c.zip", {"A"}) is True
    assert file_utils.is_bundle_member("A/B/c.zip", {"A/B"}) is True
    assert file_utils.is_bundle_member("A/c.zip", {"A"}) is True
    assert file_utils.is_bundle_member("A.zip", {"A"}) is False, "a twin beside the directory is not a member"
    assert file_utils.is_bundle_member("AB/c.zip", {"A"}) is False, "a segment boundary, not a string prefix"
    assert file_utils.is_bundle_member("A/B/c.zip", {"a"}) is False, "raw and case-sensitive, like the strip"
    assert file_utils.is_bundle_member("A/B/c.zip", set()) is False
    assert file_utils.is_bundle_member("c.zip", {"A"}) is False


@pytest.mark.parametrize("name", ["files", "Files", "images", "models", "stl", "STL"])
def test_generic_container_roots_are_the_four_names_casefolded(name: str) -> None:
    assert file_utils.is_generic_container_root(name) is True


@pytest.mark.parametrize("name", ["files_and_docs", "my files", "FileShare", CLOCKWORK_ROOT])
def test_a_genuine_root_is_not_a_generic_container(name: str) -> None:
    """An exact set, not a prefix test: these are real bundle roots in the library."""
    assert file_utils.is_generic_container_root(name) is False
