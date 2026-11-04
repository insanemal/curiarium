"""The dev-only serializer and its diff, over a real temp corpus (Mf-9ygi).

The artifact is the deliverable: a row-level JSON snapshot of one report with the
rule-set stamp that produced it and a rule for every path that was NOT offered.
These tests build one corpus that exercises every kind of refusal the scan can
make, serialise it, and then prove the three properties the ticket asks for:

* the artifact is deterministic and a superset of the hand-taken baseline;
* every archive is either offered or refused with a named rule;
* a rule change produces a diff that names each changed, added and removed row,
  and the stamp marks the two runs as not comparable - the confusion the 114 ->
  112 delta caused.
"""

import json
import re
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Dict, Mapping

import pytest

import bundle_detect
import bundle_serialize
import bundle_service
import main
import routers
from db import engine
from migrations import run_migrations

THING_URL = "https://www.thingiverse.com/thing:2493386"
MOAI_README = "Moai statue -No overhang\nCreated by Thingiverse user moai_maker\nLicensed under CC BY-NC\n"
MOAI_LICENSE = f"Moai statue -No overhang by moai_maker on Thingiverse: {THING_URL}\n"
NO_MARKERS = "just some notes about printing\n"
FATES_END = "import/01_Fates End - Wizard Tower"
DRAGON = "import/5390184_DRAGON"
PILE = "import/pile"
DUPLICATE_SUPPRESSED = "import/dupes/B.zip"


def write_file(path: Path, payload: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_zip(path: Path, entries: Mapping[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)


def trio() -> Dict[str, bytes]:
    """The R3 shape: one model, one image, one text."""
    return {"a.stl": b"solid a\n", "b.png": b"png", "c.txt": b"notes\n"}


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One corpus with a witness for every refusal the scan can produce."""
    root = tmp_path / "storage"
    area = root / "import"
    area.mkdir(parents=True)

    # R5 only: twelve models and two previews, no text at all.
    fates = area / "01_Fates End - Wizard Tower"
    for index in range(1, 13):
        write_file(fates / f"a{index:02d}.stl", b"solid\n")
    write_file(fates / "p1.jpg", b"j" * 20)
    write_file(fates / "p2.jpg", b"j" * 20)

    # R1: the real Thingiverse document pair.
    moai = area / "Moai"
    write_file(moai / "model.stl", b"solid moai\n")
    write_text(moai / "README.txt", MOAI_README)
    write_text(moai / "LICENSE.txt", MOAI_LICENSE)

    # R3, R4 and R5 in three shapes.
    write_file(area / "Trio" / "a.stl", b"solid\n")
    write_file(area / "Trio" / "b.png", b"png")
    write_text(area / "Trio" / "c.txt", "notes\n")
    write_file(area / "5390184_DRAGON" / "a.stl", b"solid\n")
    write_file(area / "5390184_DRAGON" / "b.png", b"png")
    write_text(area / "5390184_DRAGON" / "c.txt", "notes\n")
    write_file(area / "Clockwork" / "a.stl", b"solid\n")
    write_file(area / "Clockwork" / "b.png", b"png")
    write_zip(area / "Clockwork" / "inner.zip", {"x.stl": b"solid\n"})

    # A twin directory and its archive: the directory is the bundle.
    write_file(area / "Twin" / "a.stl", b"solid\n")
    write_file(area / "Twin" / "b.png", b"png")
    write_text(area / "Twin" / "c.txt", "notes\n")
    write_zip(area / "Twin.zip", trio())

    # Archive-only shapes, one per reason.
    write_zip(area / "model-only.zip", {"a.stl": b"solid\n"})
    # No image on purpose: with a full trio R3 would offer this archive as a
    # generic bundle and the marker-less README would never be the reason. The
    # refusal only exists when NO rule fires.
    write_zip(area / "readme.zip", {"README.txt": NO_MARKERS.encode(), "a.stl": b"solid\n"})
    write_zip(
        area / "All Pokemon.zip",
        {f"root{index}/a.stl": b"solid\n" for index in range(60)}
        | {f"root{index}/b.png": b"png" for index in range(60)},
    )
    write_file(area / "broken.zip", b"not a zip")

    # Byte-identical duplicates: one row, one `duplicate` refusal.
    duel = area / "dupes"
    # Distinct bytes from Twin.zip on purpose: identical bytes would make this a
    # duplicate of the twin and the corpus would lose its surviving-copy row.
    write_zip(duel / "A.zip", {"d.stl": b"solid d\n", "e.png": b"png e", "f.txt": b"notes f\n"})
    write_file(duel / "B.zip", (duel / "A.zip").read_bytes())

    # A plain archive row, a pile, and two directories with nothing to offer.
    # Deliberately different bytes from the dupes pair: same bytes would make it a
    # duplicate and the corpus would lose its only plain archive row.
    write_zip(area / "offered.zip", {"solo.stl": b"solid solo\n", "solo.png": b"png2", "solo.txt": b"txt2\n"})
    write_file(area / "pile" / "stl" / "a.stl", b"solid\n")
    write_file(area / "pile" / "models" / "b.stl", b"solid\n")
    write_file(area / "pile" / "b.png", b"png")
    write_text(area / "pile" / "c.txt", "notes\n")
    write_file(area / "loose" / "notes.gcode", b"G1\n")
    # Not title-shaped on purpose: a hyphen would make R5 offer it and the
    # marker-less README would never be the reason for the absence.
    write_text(area / "markerless" / "README.txt", NO_MARKERS)
    write_file(area / "markerless" / "a.stl", b"solid\n")

    monkeypatch.setattr(bundle_service, "STORAGE", root)
    run_migrations(engine)
    return root


def jsonable(artifact: dict) -> dict:
    """The artifact without its timestamp: everything else must be reproducible."""
    return {key: value for key, value in artifact.items() if key != "taken_at"}


def fresh_decision(root: Path, candidate: bundle_service.BundleCandidate) -> bundle_detect.Detection:
    """The pure decision for one candidate, asked again from the snapshot."""
    if candidate.title_name:
        return bundle_detect.title_name_decision(candidate.snapshot)
    if candidate.pair_only or candidate.archive_owned:
        return bundle_detect.archive_owned_decision(candidate.snapshot)
    if candidate.is_archive:
        return bundle_service._detect_archive_decision(root / candidate.snapshot.rel_dir, candidate.snapshot)
    return bundle_service._detect_folder_decision(root, candidate.snapshot)


def rows_by_path(artifact: dict) -> Dict[str, dict]:
    return {row["rel_path"]: row for row in artifact["rows"]}


def refused_by_path(artifact: dict) -> Dict[str, dict]:
    return {item["rel_path"]: item for item in artifact["refused"]}


def test_the_artifact_is_deterministic_and_a_superset_of_the_baseline_schema(storage: Path) -> None:
    """Two runs of one corpus differ only by ``taken_at``, and keep every baseline key.

    Mutation: let set iteration or walk order leak into the artifact and this
    reds; a measurement that cannot be reproduced is not evidence.
    """
    first = bundle_serialize.serialise_report(storage)
    second = bundle_serialize.serialise_report(storage)

    assert json.dumps(jsonable(first), sort_keys=True) == json.dumps(jsonable(second), sort_keys=True)

    # A superset of the hand-taken baseline: every one of its keys, same meaning.
    for key in ("taken_at", "image_sha", "library", "counts", "duplicates", "rows", "offered_keys", "dirs_not_offered"):
        assert key in first, f"the artifact dropped the baseline key {key}"
    assert first["artifact_schema"] == bundle_serialize.ARTIFACT_SCHEMA
    assert first["library"]["assets"]["total"] is not None or first["database"]["note"]
    assert first["duplicates"]["bytes"] >= 0

    rows = first["rows"]
    assert rows, "the corpus must offer something"
    for row in rows:
        for key in bundle_serialize.BASELINE_ROW_KEYS:
            assert key in row, f"row {row['rel_path']} lost the baseline key {key}"
        assert row["rule_set"] == first["rule_set"]["stamp"]
        assert row["rule_set"] == bundle_detect.RULE_SET_STAMP
    assert [row["rel_path"] for row in rows] == sorted(row["rel_path"] for row in rows)
    assert [item["rel_path"] for item in first["refused"]] == sorted(
        item["rel_path"] for item in first["refused"]
    )

    assert first["dirs_not_offered"], "a corpus with refused directories must name them"
    for entry in first["dirs_not_offered"]:
        assert entry["rule"] and entry["rule"] != "unclassified", entry
        assert entry["rel_path"] not in first["offered_keys"]

    assert first["offered_keys"] == [row["rel_path"] for row in rows]
    assert first["counts"]["total"] == len(rows)
    assert first["counts"]["high"] + first["counts"]["medium"] + first["counts"]["low"] == len(rows)


def test_every_archive_is_either_offered_or_refused_with_a_rule(storage: Path) -> None:
    """The 46 invisible archives of Mf-9ygi cannot happen again: an archive has a row or a rule.

    Mutation: drop any ``_refuse`` call from an archive pass and this reds on the
    partition, which is exactly the absence the owner could not see.
    """
    artifact = bundle_serialize.serialise_report(storage)
    every_zip = {
        str(path.relative_to(storage)) for path in storage.rglob("*.zip") if path.is_file()
    }
    offered = {row["rel_path"] for row in artifact["rows"] if row["rel_path"].endswith(".zip")}
    refused = refused_by_path(artifact)
    refused_archives = {path for path, item in refused.items() if item["kind"] == "archive"}

    assert offered & refused_archives == set(), "an archive cannot be both offered and refused"
    assert offered | refused_archives == every_zip, sorted(every_zip - (offered | refused_archives))
    assert len(offered) == 2, sorted(offered)  # the surviving duplicate and the plain archive

    for path, item in refused.items():
        assert item["rule"] in bundle_service.REFUSAL_RULES, (path, item["rule"])

    expected = {
        "import/Twin.zip": bundle_service.REFUSAL_TWIN_CONSUMED,
        "import/Clockwork/inner.zip": bundle_service.REFUSAL_NESTED_MEMBER,
        DUPLICATE_SUPPRESSED: bundle_service.REFUSAL_DUPLICATE,
        "import/broken.zip": bundle_service.REFUSAL_UNREADABLE_ARCHIVE,
        "import/model-only.zip": bundle_detect.REFUSAL_R3_TRIO_NO_IMAGE,
        "import/readme.zip": bundle_detect.REFUSAL_R1_NO_MARKERS,
        "import/All Pokemon.zip": bundle_detect.REFUSAL_R3_NO_OWN_DEPTH_MODEL,
    }
    for path, rule in expected.items():
        assert refused[path]["rule"] == rule, (path, refused[path]["rule"])


def test_a_rule_change_diff_names_every_changed_added_and_removed_row(
    storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A coverage test over one corpus: the diff names every row the rules moved.

    This is the acceptance criterion of the ticket, and the mutation is the ticket
    itself - change the rules and the tool has to say which rows moved, rather than
    leaving an operator to compare two counts from memory.
    """
    before = bundle_serialize.serialise_report(storage)

    with monkeypatch.context() as patched:
        patched.setattr(bundle_detect, "TITLE_NAME_BUNDLES_ENABLED", False)
        patched.setattr(bundle_detect, "_THINGIVERSE_ID_IN_NAME", re.compile(r"^(\d{9,})_"))
        patched.setattr(bundle_detect, "DETECTOR_GENERIC", "generic-bundle-v2")
        patched.setattr(bundle_detect, "R3_MODEL_DIRS", frozenset({"files", "stl"}))
        after = bundle_serialize.serialise_report(storage)

    old_rows = rows_by_path(before)
    new_rows = rows_by_path(after)
    added = sorted(set(new_rows) - set(old_rows))
    removed = sorted(set(old_rows) - set(new_rows))
    common = sorted(set(old_rows) & set(new_rows))
    detector_changed = [path for path in common if old_rows[path]["detector"] != new_rows[path]["detector"]]
    confidence_changed = [path for path in common if old_rows[path]["confidence"] != new_rows[path]["confidence"]]

    assert added, "the corpus no longer exercises ADDED rows (widened R3_MODEL_DIRS should offer import/pile)"
    assert removed, "the corpus no longer exercises REMOVED rows (disabling R5 should drop the Fates End row)"
    assert detector_changed, "the corpus no longer exercises a detector change (DETECTOR_GENERIC was renamed)"
    assert confidence_changed, (
        "the corpus no longer exercises a confidence change (the 5390184_DRAGON row should fall medium -> low)"
    )

    diff = bundle_serialize.diff_artifacts(before, after)

    assert diff["comparable"] is False
    assert "rule set" in diff["reason"]
    assert diff["added"] == added
    assert diff["removed"] == removed
    assert [item["rel_path"] for item in diff["detector_changed"]] == detector_changed
    assert [item["rel_path"] for item in diff["confidence_changed"]] == confidence_changed

    dragon = next(item for item in diff["confidence_changed"] if item["rel_path"] == DRAGON)
    assert (dragon["old"], dragon["new"]) == ("medium", "low")
    assert DRAGON in detector_changed
    assert FATES_END in removed
    assert PILE in added

    text = bundle_serialize.format_diff(diff)
    assert "comparable: NO" in text
    for name in (*added, *removed, *detector_changed, *confidence_changed):
        assert name in text, f"{name} missing from the printed diff"


def test_refusals_are_additive_and_do_not_change_the_offered_rows(storage: Path) -> None:
    """The ledger is derived from the candidate set, never an input to it.

    Mutation: let a refusal influence candidate selection and the scan's keys and
    the report's rows stop agreeing - the one thing a measurement cannot afford.
    """
    previous = bundle_service.STORAGE
    bundle_service.STORAGE = storage
    try:
        scan = bundle_service.scan_bundles(storage)
        report = bundle_service.build_report(limit=bundle_service.MAX_SCAN_ENTRIES)
    finally:
        bundle_service.STORAGE = previous

    offered = {row["rel_path"] for row in report["bundles"]}
    assert offered == {candidate.key for candidate in scan.candidates}

    rows = {row["rel_path"]: row for row in report["bundles"]}
    for candidate in scan.candidates:
        decision = fresh_decision(storage, candidate)
        assert decision.facts is not None, candidate.key
        assert rows[candidate.key]["detector"] == decision.facts.detector
        assert rows[candidate.key]["confidence"] == decision.facts.confidence

    assert report["refused"], "the corpus must have refused paths"
    for item in report["refused"]:
        assert item["rel_path"] not in offered, item
    assert scan.refusals, "the scan itself must carry the ledger"
    assert {item.rel_path for item in scan.refusals} == {item["rel_path"] for item in report["refused"]}


def test_the_diff_marks_a_pre_mf9ygi_artifact_not_comparable(storage: Path) -> None:
    """An artifact with no stamp is a new baseline, never a like-for-like comparison.

    Mutation: fall back to comparing stamps-less artifacts and the tool invites
    exactly the "one run and my memory" comparison this ticket exists to stop.
    """
    before = bundle_serialize.serialise_report(storage)
    after = bundle_serialize.serialise_report(storage)
    unstamped = deepcopy(before)
    unstamped.pop("rule_set")

    diff = bundle_serialize.diff_artifacts(unstamped, after)

    assert diff["comparable"] is False
    assert "no rule-set stamp" in diff["reason"]
    assert "comparable: NO" in bundle_serialize.format_diff(diff)


def test_the_cli_module_is_unreachable_over_http(storage: Path) -> None:
    """The serializer is a CLI, not a surface: no router, no route, no import from main.

    Mutation: import it from a router and this reds before it ships - a dev tool
    inside the app would add a permission and audit surface nobody classified.
    """
    assert not hasattr(bundle_serialize, "APIRouter")
    assert routers.__path__, "the routers package is the surface this tool must stay out of"
    api_root = Path(__file__).resolve().parents[1]
    source = (api_root / "bundle_serialize.py").read_text(encoding="utf-8")
    import_lines = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not any("fastapi" in line for line in import_lines), import_lines
    assert not any("router" in line for line in import_lines), import_lines

    assert "bundle_serialize" not in Path(main.__file__).read_text(encoding="utf-8")
    for path in (api_root / "routers").glob("*.py"):
        assert "bundle_serialize" not in path.read_text(encoding="utf-8"), path
