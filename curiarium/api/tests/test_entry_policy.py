"""One module owns the zip-entry policy: vocabulary, naming, budget, classification.

``entry_policy`` replaced ``zip_junk`` and absorbed the two size gates and the
per-entry failure classifier that used to live in ``zip_service``. The route
tests (``test_zip_junk.py``, ``test_zip_size_limits.py``, ``test_archive_import.py``)
pin the same behaviour through HTTP; this module pins it at the seam, with a real
``ZipFile`` or a stub, so a caller that is not a route cannot drift either.

It also pins the docstring itself, because that docstring is the fix for symptom
4 of Mf-znu1: two code comments used to claim that junk "can never become an
asset" and that both doors were "all-or-nothing", and both claims were wrong in
one direction. The comments are gone; what they overclaimed now lives in one
place that names exactly which routes and which failure model it governs, and
the first test here fails if that naming rots away.
"""

import ast
from pathlib import Path
from typing import Dict, Tuple
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest
from fastapi import HTTPException

import asset_service
import entry_policy

API_ROOT = Path(__file__).resolve().parents[1]
VOCABULARY_NAMES = {"JUNK_SEGMENTS", "JUNK_BASENAMES", "JUNK_PREFIXES"}


def _write_zip(path: Path, entries: Dict[str, bytes]) -> Path:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


# --- the docstring is the policy's own statement of scope -------------------


def test_the_policy_docstring_names_every_door_it_governs() -> None:
    """Symptom 4 of Mf-znu1: two comments overclaimed, so one docstring names the scope.

    The comments said junk "can never become an asset" and that both doors
    "fail a whole request", and each was true of one route and false of another.
    Rather than delete the claims, they moved here as a docstring that names the
    five routes it governs, the two caps it owns, the single classification it
    exposes, its mirror, and the one overclaim it corrects - the storage scan
    registers an ordinary upload or an on-disk file, so "junk can never be an
    asset" holds of the archive-member route, not of the library.
    """
    docstring = entry_policy.__doc__ or ""

    for route in ("/import/zip", "/import/zip/entries", "/import/archive", "/import/archive/entries", "/import/project"):
        assert route in docstring, f"the policy docstring no longer names the route {route}"
    assert "IMPORT_MAX_BYTES" in docstring, "the docstring no longer says which per-file cap it owns"
    assert "IMPORT_ZIP_MAX_BYTES" in docstring, "the docstring no longer says which cumulative cap it owns"
    assert "is_entry_failure" in docstring, "the docstring no longer names the one failure classification"
    assert "test_junk_vocabulary_mirror.py" in docstring, "the docstring no longer names its binding test"
    assert "zipJunk.ts" in docstring, "the docstring no longer names its browser mirror"
    assert "archive-member route" in docstring, "the docstring lost the correction: junk *can* be an asset elsewhere"


# --- one vocabulary, one predicate, one module -----------------------------


def test_the_junk_vocabulary_and_predicate_live_in_one_module() -> None:
    """Name matching only: a literal search would flag two deliberately narrow peers.

    ``library_scan.IGNORED_FILENAMES`` and ``bundle_detect.is_macosx_artifact``
    both mention ``__MACOSX``/``.DS_Store``-shaped debris on purpose - the scan
    list is a filesystem-walk list of three names and the bundle detector asks
    the narrower "is this outside the payload root" question - so grepping for
    the literals would report them as duplicates of the authority. Looking for
    the *names* of the three tables and of the predicate asks the question the
    ticket actually asks: who defines the vocabulary a zip import may admit.
    """
    table_files: set[str] = set()
    predicate_files: set[str] = set()

    for source in sorted(API_ROOT.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                if isinstance(node, ast.FunctionDef) and node.name == "is_junk_entry":
                    predicate_files.add(source.name)
                continue
            if any(isinstance(target, ast.Name) and target.id in VOCABULARY_NAMES for target in targets):
                table_files.add(source.name)

    assert table_files == {"entry_policy.py"}, f"the junk tables are declared in {sorted(table_files)}"
    assert predicate_files == {"entry_policy.py"}, f"is_junk_entry is defined in {sorted(predicate_files)}"


# --- the declared preflight ------------------------------------------------


def test_plan_never_marks_a_junk_member_oversized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Junk is classified before the per-file cap is consulted; size is irrelevant."""
    archive = _write_zip(tmp_path / "mixed.zip", {"part.stl": b"solid", ".DS_Store": bytes(4096)})
    monkeypatch.setattr(entry_policy, "IMPORT_MAX_BYTES", 10)

    with ZipFile(archive) as zf:
        plan = entry_policy.plan_selection(zf, None)

    assert ".DS_Store" in plan.junk
    assert ".DS_Store" not in plan.oversized
    assert plan.oversized == frozenset()
    assert plan.junk.isdisjoint(plan.oversized)
    assert plan.junk.isdisjoint(set(plan.writable))


def test_plan_excludes_junk_from_the_cumulative_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A junk-only selection must not 413 on the cumulative cap it never spends.

    The payload declares a few bytes and the junk member declares far more, with
    the cap between the two: if junk counted toward ``declared`` the plan would
    raise here, and the upload door's junk request would stop being a 200.
    """
    archive = _write_zip(tmp_path / "mixed.zip", {"part.stl": b"abc", ".DS_Store": bytes(2000)})
    monkeypatch.setattr(entry_policy, "IMPORT_ZIP_MAX_BYTES", 100)

    with ZipFile(archive) as zf:
        plan = entry_policy.plan_selection(zf, None)

    assert plan.writable == ("part.stl",)


def test_plan_raises_413_when_payload_alone_exceeds_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preflight still refuses a real overshoot, with the string clients read."""
    archive = _write_zip(tmp_path / "big.zip", {"part.stl": bytes(500)})
    monkeypatch.setattr(entry_policy, "IMPORT_ZIP_MAX_BYTES", 10)

    with ZipFile(archive) as zf, pytest.raises(HTTPException) as raised:
        entry_policy.plan_selection(zf, None)

    assert raised.value.status_code == 413
    assert "expand to" in raised.value.detail
    assert "IMPORT_ZIP_MAX_MB" in raised.value.detail


# --- resolution: directory-like, missing, order ---------------------------


class _DirLikeInfo(ZipInfo):
    """A member that answers ``is_dir()`` true without a trailing slash."""

    def is_dir(self) -> bool:
        return True


class _StubArchive:
    """The only attribute ``plan_selection`` reads from its archive."""

    def __init__(self, infos: Tuple[ZipInfo, ...]) -> None:
        self._infos = infos

    def infolist(self) -> Tuple[ZipInfo, ...]:
        return self._infos


def test_plan_skips_a_directory_like_member_without_calling_it_missing() -> None:
    """The ``is_dir()`` hazard, pinned against a stub rather than a real archive.

    A real ``ZipInfo.is_dir()`` is ``filename.endswith("/")``, and
    ``normalize_zip_entry_path`` already drops every such name, so a directory
    member cannot reach the plan through ``ZipFile``. It can through a hand-made
    or stubbed archive that lies about the flag, which is what this stub is: the
    plan must skip it (no byte budget, no ``writable``) without reporting it as
    ``missing`` - the member is right there, and the upload door lists it in
    ``failed`` instead.
    """
    stub = _StubArchive((_DirLikeInfo("folder"), ZipInfo("real.stl")))

    plan = entry_policy.plan_selection(stub, None)  # type: ignore[arg-type]

    assert "folder" in plan.requested
    assert "folder" not in plan.writable
    assert "folder" not in plan.missing
    assert plan.writable == ("real.stl",)


def test_plan_reports_a_missing_pick_and_keeps_the_request_order(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "two.zip", {"a.stl": b"a", "b.stl": b"b"})

    with ZipFile(archive) as zf:
        plan = entry_policy.plan_selection(zf, ["b.stl", "ghost.stl", "a.stl"])

    assert plan.requested == ("b.stl", "ghost.stl", "a.stl")
    assert plan.missing == frozenset({"ghost.stl"})
    assert plan.writable == ("b.stl", "a.stl")


def test_an_empty_selection_is_still_the_same_400(tmp_path: Path) -> None:
    archive = _write_zip(tmp_path / "one.zip", {"a.stl": b"a"})

    with ZipFile(archive) as zf, pytest.raises(HTTPException) as raised:
        entry_policy.plan_selection(zf, [])

    assert raised.value.status_code == 400
    assert raised.value.detail == "No zip entries selected"


# --- the streamed gate ----------------------------------------------------


def test_stream_entry_returns_the_cumulative_total_and_honours_both_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both caps are re-checked on the bytes, and the total is cumulative.

    The cumulative assertion is the important one: crossing a budget of 10 with
    7 already spent must report 11 (``spent_bytes`` plus the member's bytes), not
    the member's own 4, because the extractor's 413 quotes a selection total.
    That is why the crossing case uses the four-byte member: the production chunk
    is 1 MiB, so a member of 4 bytes is read and checked as one chunk and the
    reported total is the cumulative 11 rather than the local size.
    """
    archive = _write_zip(tmp_path / "five.zip", {"part.stl": b"12345", "short.stl": b"1234"})
    dest = tmp_path / "out.stl"
    per_member = 1024
    cumulative = 1024

    with ZipFile(archive) as zf:
        members = {info.filename: info for info in zf.infolist()}
        with monkeypatch.context() as generous:
            generous.setattr(entry_policy, "IMPORT_MAX_BYTES", per_member)
            generous.setattr(entry_policy, "IMPORT_ZIP_MAX_BYTES", cumulative)
            assert entry_policy.stream_entry(zf, members["part.stl"], dest, spent_bytes=7) == 12

        with monkeypatch.context() as small_member:
            small_member.setattr(entry_policy, "IMPORT_MAX_BYTES", 3)
            small_member.setattr(entry_policy, "IMPORT_ZIP_MAX_BYTES", cumulative)
            with pytest.raises(entry_policy.EntryTooLarge):
                entry_policy.stream_entry(zf, members["part.stl"], dest, spent_bytes=7)

        with monkeypatch.context() as small_budget:
            small_budget.setattr(entry_policy, "IMPORT_MAX_BYTES", per_member)
            small_budget.setattr(entry_policy, "IMPORT_ZIP_MAX_BYTES", 10)
            with pytest.raises(entry_policy.BudgetExceeded) as raised:
                entry_policy.stream_entry(zf, members["short.stl"], dest, spent_bytes=7)
            assert raised.value.total_written == 11


# --- naming ---------------------------------------------------------------


def test_entry_filename_keeps_only_the_last_segment() -> None:
    """``sanitize_filename`` alone would flatten ``a/b/c.stl`` into ``a_b_c.stl``."""
    assert entry_policy.entry_filename("a/b/c.stl") == "c.stl"
    assert entry_policy.entry_filename("a_b.stl") == "a_b.stl"


def test_entry_member_segments_keeps_the_tree_and_refuses_padded_dots() -> None:
    """Rendering per segment is also what creates the ``.``/``..`` segments it refuses.

    ``sanitize_filename`` strips whitespace and NULs before taking the basename,
    so each spelling below renders a dot segment that the exact-match guard in
    ``normalize_zip_entry_path`` would not have caught.
    """
    assert entry_policy.entry_member_segments("a/b/c.stl") == ["a", "b", "c.stl"]

    for raw in (".. ", " ..", "\t..", "..\x00"):
        member = f"a/{raw}/b.stl"
        with pytest.raises(entry_policy.EntryPathRefused) as raised:
            entry_policy.entry_member_segments(member)
        assert raised.value.segment == ".."
        assert raised.value.name == member

    with pytest.raises(entry_policy.EntryPathRefused) as raised:
        entry_policy.entry_member_segments("a/ . /b.stl")
    assert raised.value.segment == "."
    assert raised.value.name == "a/ . /b.stl"


def test_normalize_zip_entry_path_kept_its_name_and_its_behaviour() -> None:
    """The name is unchanged on purpose, and this test is what stops a rename.

    ``zip_service`` re-exports it (``from entry_policy import
    normalize_zip_entry_path``) and ``test_bundle_service.py`` imports it from
    there, so renaming it for symmetry with the other ``entry_*`` helpers would
    break callers that this module does not own.
    """
    for refused in ("..", "a/../b", "", "a/"):
        assert entry_policy.normalize_zip_entry_path(refused) is None

    assert entry_policy.normalize_zip_entry_path("/a//b.stl") == "a/b.stl"


# --- one failure model ----------------------------------------------------


def test_is_entry_failure_is_one_classification() -> None:
    """A name conflict or a missing extension is one entry; every other 400 is the request."""
    assert entry_policy.is_entry_failure(asset_service.AssetFilenameError()) is True
    assert entry_policy.is_entry_failure(HTTPException(status_code=409, detail="x")) is True
    assert entry_policy.is_entry_failure(HTTPException(status_code=400, detail="x")) is False
    assert entry_policy.is_entry_failure(entry_policy.BudgetExceeded(5)) is False
    assert entry_policy.is_entry_failure(OSError()) is False
