"""The bundle-drift contract, from literals only (Mf-8i04).

`bundle_drift` is the one place the project route and the Review queue get their
answer, so the rules that matter are pinned without a database: which subtree a
marker names (the two key spaces), which registered rows count (models, not the
support files a create never attaches), which are subtracted (membership and an
excluded group), and the order of the answer. Each test names the mutation that
would red it, because every sentence in the module's docstring has to be pinned
somewhere.
"""

from typing import Dict, List, Tuple

import bundle_drift


def marker(**fields: object) -> dict:
    """One ``_bundle`` marker as a dict, so a test names only the fields it cares about."""
    return dict(fields)


def test_walk_root_reads_each_marker_shape() -> None:
    """The walk root is ``rel_dir``, else the detection door's own path ``key``, else nothing.

    A detection marker is what ``bundle_service._create_one`` writes: ``key`` and
    ``rel_dir`` are the same storage-relative path and no ``source`` is recorded.
    A zip-project marker writes ``key = "zip-project:<canonical url>"`` - an
    IDENTITY - so its ``rel_dir`` is the only path in it, and when that is None
    (a mixed-root, flat or one-part selection) there is NO root.

    Mutation: fall back to the identity key and the last case answers ``known``
    with a root that matches nothing - the false zero the ticket forbids.
    """
    detection = marker(key="import/Alpha", rel_dir="import/Alpha", source=None)
    assert bundle_drift.classify(detection) == (bundle_drift.DRIFT_KNOWN, "import/Alpha")

    detection_without_rel_dir = marker(key="import/Beta", source=None)
    assert bundle_drift.walk_root(detection_without_rel_dir) == "import/Beta", "the detection key IS a path"

    zip_with_rel_dir = marker(
        key="zip-project:https://x/y", source="zip-project-import", rel_dir="import/Thing"
    )
    assert bundle_drift.classify(zip_with_rel_dir) == (bundle_drift.DRIFT_KNOWN, "import/Thing")

    zip_without_rel_dir = marker(
        key="zip-project:https://x/y", source="zip-project-import", rel_dir=None
    )
    state, root = bundle_drift.classify(zip_without_rel_dir)

    assert (state, root) == (bundle_drift.DRIFT_UNKNOWN, None)
    assert root != zip_without_rel_dir["key"], "the identity key is never a walk root"


def test_classify_is_exhaustive_over_the_three_states() -> None:
    """No marker, a root, and an identity with no path: one state each, nothing unmapped.

    ``{}`` is a hand-made project or a malformed metadata column - there is no
    question, which is not the same answer as zero. A non-dict (a string or a
    list where the ``_bundle`` object should be) is malformed the same way.

    Mutation: map the no-marker case to ``known``/0 and the not_applicable
    assertion reds, and the client would render no notice but the server would be
    claiming to have measured something it never looked at.
    """
    assert bundle_drift.classify({}) == (bundle_drift.DRIFT_NOT_APPLICABLE, None)
    assert bundle_drift.classify(marker(key="import/Alpha", rel_dir="import/Alpha")) == (
        bundle_drift.DRIFT_KNOWN,
        "import/Alpha",
    )
    assert bundle_drift.classify(
        marker(key="zip-project:https://x/y", source="zip-project-import", rel_dir=None)
    ) == (bundle_drift.DRIFT_UNKNOWN, None)
    for malformed in ("not a marker", ["groups"], 7, None):
        assert bundle_drift.classify(malformed) == (bundle_drift.DRIFT_NOT_APPLICABLE, None)


def test_excluded_prefixes_subtract_an_included_false_group() -> None:
    """Only an explicit ``included: False`` group is subtracted, once, in marker order.

    ``groups`` is a creation-time snapshot; a group included as usual (``True``)
    and a marker written before the flag existed (no ``included`` key) subtract
    nothing, because neither records an exclusion.

    Mutation: subtract every group name and a fresh compound project reports its
    attached parts as excluded; compare with ``is not False`` and a duplicated
    group becomes two prefixes.
    """
    excluded = bundle_drift.excluded_prefixes(
        "import/Compound",
        marker(
            groups=[
                {"name": "Edition One", "included": True},
                {"name": "Edition Two", "included": False},
                {"name": "Legacy", "model_count": 3},
                {"name": "Edition Two", "included": False},
                "not a group",
            ]
        ),
    )

    assert excluded == ("import/Compound/Edition Two/",)
    assert bundle_drift.excluded_prefixes("import/Compound", marker(groups=[])) == ()
    assert bundle_drift.excluded_prefixes("import/Compound", marker()) == ()


def test_drift_is_model_scoped_so_support_files_are_never_drift() -> None:
    """The measured rule: only registered MODELS under the root, not the create door's leftovers.

    The fake set is one row per shape the measured compound tree holds: an
    unattached model (the one real part), an already-membered model, a
    ``cover.jpg``, a ``notes.txt``, an AppleDouble ``._part.stl`` sidecar, a path
    outside the root and a model under an excluded group. Exactly the one
    unattached model comes back.

    Mutation: drop ``bundle_detect.is_counted_model`` and the image, the text and
    the AppleDouble sidecar count too - the naive difference the ticket measured
    as 26 at creation where the honest answer is 0.
    """
    rows: List[Tuple[str, str, int]] = [
        ("model-new", "import/Alpha/part.stl", 40),
        ("model-member", "import/Alpha/member.stl", 40),
        ("image-cover", "import/Alpha/cover.jpg", 40),
        ("text-notes", "import/Alpha/notes.txt", 40),
        ("appledouble", "import/Alpha/._part.stl", 40),
        ("outside", "import/Beta/part.stl", 40),
        ("excluded", "import/Alpha/Excluded/part.stl", 40),
    ]
    members = {"model-member"}

    asset_ids = bundle_drift.drift_asset_ids(
        rows,
        root="import/Alpha",
        prefixes=("import/Alpha/Excluded/",),
        is_member=members.__contains__,
    )

    assert asset_ids == ["model-new"]

    # The over-count is pinned, not described: without the model predicate the
    # same set also claims the preview and the README.
    naive = [
        asset_id
        for asset_id, path, _size in rows
        if path.startswith("import/Alpha/")
        and not path.startswith(("import/Alpha/Excluded/",))
        and asset_id not in members
    ]
    assert naive == ["model-new", "image-cover", "text-notes", "appledouble"]


def test_the_asset_ids_are_ordered_by_path_not_row_order() -> None:
    """The answer is a property of the disk: shuffled input yields the same ordered ids.

    Ordering by ``(storage_path, asset id)`` means two runs over the same tree
    cannot disagree because a query returned its rows differently.

    Mutation: return ids in input order and the two assertions disagree.
    """
    rows: List[Tuple[str, str, int]] = [
        ("b-model", "import/Alpha/z-part.stl", 40),
        ("a-model", "import/Alpha/a-part.stl", 40),
        ("same-path-b", "import/Alpha/m-part.stl", 40),
        ("same-path-a", "import/Alpha/m-part.stl", 40),
    ]
    expected = ["a-model", "same-path-a", "same-path-b", "b-model"]

    assert bundle_drift.drift_asset_ids(rows, root="import/Alpha") == expected
    assert bundle_drift.drift_asset_ids(list(reversed(rows)), root="import/Alpha") == expected


def test_the_accounting_of_the_fake_rows_is_what_the_ticket_measured() -> None:
    """A guard on the docstring's numbers: the naive set is a superset of the honest one.

    The module's docstring states the measured over-count in words (26 naive
    against 0 honest at creation on the compound tree). This pins the shape of
    that claim on literals - every honest id is in the naive set, and the naive
    set is strictly larger whenever any support file is registered - so the
    sentence cannot drift away from the predicate.

    Mutation: widen the honest predicate to every asset and the strict-superset
    assertion reds.
    """
    rows: Dict[str, Tuple[str, str, int]] = {
        "model": ("model", "import/Alpha/part.stl", 40),
        "image": ("image", "import/Alpha/cover.jpg", 40),
        "text": ("text", "import/Alpha/notes.txt", 40),
    }
    members = {"model"}

    honest = bundle_drift.drift_asset_ids(rows.values(), root="import/Alpha", is_member=members.__contains__)
    naive = [asset_id for asset_id, path, _size in rows.values() if asset_id not in members]

    assert honest == [], "an attached model and two support files are an honest zero"
    assert set(honest) < set(naive), "the naive difference is a strict superset where support files exist"
