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

Provenance is a FIELD, not a precondition. The API image flattens this module to
``/app/bundle_serialize.py``, where a fixed ``parents[2]`` is an ``IndexError``:
the last three tests below copy the module to a directory with no ``.git`` above
it, on the host and in the image alike, and prove a measurement is still taken -
with a null build sha, never a fabricated one.
"""

import importlib.util
import json
import os
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


def load_serializer_without_a_checkout(target_dir: Path):
    """A real copy of the module, at a path with no `.git` above it.

    The image's defect is path-shaped: at `/app/bundle_serialize.py` a fixed
    `parents[2]` is an IndexError. A copy in tmp_path reproduces that shape for
    real - on the host and in the image - so this is the condition, not a mock.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    source = Path(bundle_serialize.__file__).resolve()
    copy_path = target_dir / "bundle_serialize_no_repo.py"
    copy_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("bundle_serialize_no_repo", copy_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The environment `main()` binds for the app it is about to import. It mutates the
# real process environment, so a test that calls it hands the values back.
BOUND_BY_MAIN = ("FILE_STORAGE", "DB_URL", "AUTH_MODE", "IMPORT_MOUNT_ON_STARTUP")


def preserve_environment(patched: pytest.MonkeyPatch) -> None:
    """Register the variables `main()` binds, so `patched`'s undo puts them back."""
    for name in BOUND_BY_MAIN:
        if name in os.environ:
            patched.setenv(name, os.environ[name])
        else:
            patched.delenv(name, raising=False)


def test_repo_root_finds_a_checkout_and_never_indexes_past_the_filesystem_root(tmp_path: Path) -> None:
    """`_repo_root` walks to `.git` and answers None, never an IndexError.

    Mutation: remove the helper and this reds on the missing answer; answer with a
    fixed index instead of a walk and it reds wherever the module is shallow - the
    image's `/app/bundle_serialize.py`, whose `parents` is `['/app', '/']`, which
    is the IndexError the image gate hit. A copy deeper in tmp_path cannot show
    that shape (index 2 is a real ancestor there), which is why the depth is
    asserted by the host-only half below rather than pretended here.
    """
    no_checkout = tmp_path / "no-checkout"
    copy = load_serializer_without_a_checkout(no_checkout)

    assert copy._repo_root() is None, "a directory with no .git above it has no repository root"
    module_copy = no_checkout / "bundle_serialize_no_repo.py"
    assert copy._repo_root(module_copy) is None
    assert copy._repo_root(module_copy.parent) is None

    # A real checkout is found, from a file and from a directory inside it.
    fake = tmp_path / "fake"
    (fake / "sub").mkdir(parents=True)
    write_text(fake / "sub" / "file.py", "# a module in a checkout\n")
    (fake / ".git").mkdir()
    assert copy._repo_root(fake / "sub") == fake.resolve()
    assert copy._repo_root(fake / "sub" / "file.py") == fake.resolve()

    # A linked worktree keeps `.git` as a FILE, so existence is the test, not is_dir().
    linked = tmp_path / "linked"
    (linked / "nested").mkdir(parents=True)
    write_text(linked / ".git", f"gitdir: {tmp_path / 'fake' / '.git' / 'worktrees' / 'linked'}\n")
    assert copy._repo_root(linked / "nested") == linked.resolve()

    # The host-only half: this module's own answer against the real checkout. In
    # the image there is no checkout above /app, which is the same condition as
    # the copy above; asserting a path that does not exist there would be a lie.
    module_path = Path(bundle_serialize.__file__).resolve()
    checkout = module_path.parents[2] if len(module_path.parents) > 2 else None
    if checkout is None:
        assert bundle_serialize._repo_root() is None
        pytest.skip(f"{module_path} has no checkout above it (the image's /app shape)")
    assert bundle_serialize._repo_root() == checkout
    assert (checkout / ".git").exists()
    assert (checkout / "curiarium" / "api" / "bundle_serialize.py").exists()


class GitMustNotBeCalled:
    """A stand-in for the copy's `subprocess` that reds if git is asked anything.

    Without a repository root there is nothing to ask git about: a measurement
    with no checkout takes its null provenance and moves on.
    """

    def run(self, *args, **kwargs):
        raise AssertionError(
            "subprocess.run was reached although there is no repository root above this module"
        )


def test_a_report_still_measures_when_there_is_no_repository(
    tmp_path: Path, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A measurement without provenance is still a measurement: null sha, full artifact.

    Mutation: drop the `repo_root is None` guard in `_build_sha` and, with no
    checkout anywhere above, it still asks `git -C` whichever ancestor it picked -
    an IndexError where the module sits shallow (the image's `/app`), a pointless
    git call where it does not - so the `GitMustNotBeCalled` stand-in below reds;
    let `_build_sha` invent a sha and the artifact lies about where it came from.
    """
    copy = load_serializer_without_a_checkout(tmp_path / "no-checkout")
    monkeypatch.setattr(copy, "subprocess", GitMustNotBeCalled())

    artifact = copy.serialise_report(storage)

    assert artifact["build_sha"] is None, "provenance is a field, not a precondition"
    assert artifact["build_dirty"] is False, "a null sha must not claim a dirty checkout"

    # Complete, not a stub: the numbers the artifact exists for are all still here.
    assert artifact["artifact_schema"] == copy.ARTIFACT_SCHEMA
    assert artifact["rows"], "the corpus must still offer rows without a repository"
    assert artifact["refused"], "the corpus must still record refusals without a repository"
    for item in artifact["refused"]:
        assert item["rule"] in bundle_service.REFUSAL_RULES, item
    assert artifact["offered_keys"] == [row["rel_path"] for row in artifact["rows"]]
    assert artifact["counts"]["total"] == len(artifact["rows"])
    assert artifact["rule_set"]["stamp"] == bundle_detect.RULE_SET_STAMP
    for row in artifact["rows"]:
        for key in copy.BASELINE_ROW_KEYS:
            assert key in row, f"row {row['rel_path']} lost the baseline key {key}"

    # And it is writable and round-trips through the same module's loader.
    out = tmp_path / "out" / "report.json"
    copy.write_artifact(artifact, out)
    assert copy.load_artifact(out) == json.loads(json.dumps(artifact))


def test_the_cli_needs_no_repository_root_to_serialise_or_diff(
    tmp_path: Path, storage: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both `main()` paths work with no checkout above the module, or refuse cleanly.

    Mutation: restore the fixed index at the top of `main()` and the no-db run below
    raises IndexError where the module sits shallow (the image's `/app`) and
    FileNotFoundError on a `curiarium.db` that is not there where it does not;
    drop the `--db` guard and the no-db run crashes instead of exiting 2 with
    usage. A crash is not an argparse refusal, and neither is actionable.
    """
    copy = load_serializer_without_a_checkout(tmp_path / "no-checkout")
    first = copy.serialise_report(storage)

    old_path = tmp_path / "old.json"
    new_path = tmp_path / "new.json"
    copy.write_artifact(first, old_path)
    changed = deepcopy(first)
    changed["rows"][0]["status"] = "not-a-real-status"
    copy.write_artifact(changed, new_path)

    with pytest.MonkeyPatch.context() as patched:
        preserve_environment(patched)

        # 1. The module-level `_repo_root()` call cannot raise on the diff path.
        assert copy.main(["--diff", str(old_path), str(new_path)]) == 0
        out = capsys.readouterr().out
        assert "comparable:" in out, out
        assert "status changed (1)" in out, out

        # 2. Without --db there is no default database to find: refuse, do not crash.
        with pytest.raises(SystemExit) as exit_info:
            copy.main(["--root", str(storage), "--out", str(tmp_path / "x.json")])
        assert exit_info.value.code == 2
        assert "--db" in capsys.readouterr().err
        assert not (tmp_path / "x.json").exists(), "a refused run must not write an artifact"

        # 3. With --db supplied the artifact is complete, with null provenance.
        db_file = tmp_path / "empty.sqlite"
        db_file.write_bytes(b"")
        out_path = tmp_path / "report.json"
        assert copy.main(["--root", str(storage), "--out", str(out_path), "--db", str(db_file)]) == 0

    written = copy.load_artifact(out_path)
    assert written["build_sha"] is None
    assert written["build_dirty"] is False
    assert written["rows"], "the CLI must still serialise a corpus without a repository"
    assert written["offered_keys"] == [row["rel_path"] for row in written["rows"]]
