"""The report's recommendation IS the engine's, on every row, for every kind of row.

This is the invariant `Mf-1agz` exists for. `bundle_service` serves `matches` as the
engine's own option list, in the engine's order, with ``engine:<shape>`` as each
rule, so the operator acts on exactly the ranking that was validated - and a second
reading has no way to reach the wire.

The trees below are SYNTHETIC on purpose (fake product names, no ``/mnt/ceph``):
each one reproduces a real shape from the owner's library that the old detector got
wrong, so CI keeps the agreement test without the live mount:

* ``elecrow`` - a flat download folder (three models, a preview, a document).
* ``OutOfMarbles`` - three products in one folder, each with its own README.
* ``Stagetop`` - one product in nine part-shaped folders, no documents at all.
* ``Vault`` - one product whose payload is an archive wrapper beside it.
* ``models-only.zip`` - the archive leg.
* ``import/Stagetop/pieces`` under an existing project key - the `add-to-project`
  leg, which only exists when `project_keys` are injected in storage-relative
  spelling.

Every row is then re-ranked from its own facts, and the row must match exactly.
"""

import ast
import inspect
from pathlib import Path
from typing import Dict, List

import pytest
from sqlmodel import Session, delete, select

import asset_file_service
import asset_service
import bundle_service
import db
import library_scan
import main
import project_service
import proposal_engine
from db import engine
from migrations import run_migrations
from models import Asset, AssetFile, Folder, Project, ProjectAsset

STL = b"solid part\nendsolid part\n"
PNG = b"\x89PNG\r\n\x1a\n"


# --- harness ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp storage root bound into every module that caches one, and a clean DB."""
    storage = tmp_path / "storage"
    (storage / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(db, "STORAGE", storage)
    monkeypatch.setattr(db, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(main, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(library_scan, "STORAGE", storage)
    monkeypatch.setattr(asset_service, "STORAGE", storage)
    monkeypatch.setattr(asset_service, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(asset_file_service, "STORAGE", storage)
    monkeypatch.setattr(bundle_service, "STORAGE", storage)
    run_migrations(engine)
    with Session(engine) as session:
        for membership in session.exec(select(ProjectAsset)):
            session.delete(membership)
        for row in session.exec(select(Project)):
            session.delete(row)
        for row in session.exec(select(AssetFile)):
            session.delete(row)
        for row in session.exec(select(Asset)):
            session.delete(row)
        for row in session.exec(select(Folder)):
            session.delete(row)
        session.commit()
    yield storage
    with Session(engine) as session:
        session.execute(delete(ProjectAsset))
        session.execute(delete(Project))
        session.execute(delete(AssetFile))
        session.execute(delete(Asset))
        session.execute(delete(Folder))
        session.commit()


def write_file(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def write_text(path: Path, text: str) -> Path:
    return write_file(path, text.encode())


def write_zip(path: Path, entries: Dict[str, bytes]) -> Path:
    import zipfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


def rows(report: dict) -> Dict[str, dict]:
    return {row["key"]: row for row in report["bundles"]}


# --- the synthetic trees ----------------------------------------------------


def elecrow_tree(storage: Path) -> str:
    """A flat download folder: models, a preview and a document, no hierarchy."""
    key = "elecrow-7-inch-screen-console-model_files"
    folder = storage / key
    for name in ("shell-bottom.stl", "shell-top.stl", "stand.stl"):
        write_file(folder / name, STL)
    write_file(folder / "preview.jpg", PNG)
    write_text(folder / "readme.txt", "print at 0.2mm\n")
    write_text(folder / "print-settings.ini", "layer=0.2\n")
    return key


def marbles_tree(storage: Path) -> str:
    """Three products in one folder: each child carries its own README."""
    key = "import/OutOfMarbles"
    loose = storage / key
    write_file(loose / "base.stl", STL)
    write_file(loose / "cover.jpg", PNG)
    for name in ("Marble Run", "Marble Lift", "Marble Funnel"):
        write_text(loose / name / "README.md", f"# {name}\nby the workshop\n")
        for index in range(3):
            write_file(loose / name / f"part{index}.stl", STL)
        write_file(loose / name / "preview.jpg", PNG)
    return key


def stagetop_tree(storage: Path) -> str:
    """One product in nine part-shaped folders; no document anywhere."""
    key = "import/Stagetop"
    for name in (
        "Accessories", "Core", "Legs", "PlayTile - 1x1", "PlayTile - 2x2",
        "Rail - Elite", "Rail - Standard", "Wave 1.5", "Wave 2",
    ):
        for index in range(4):
            write_file(storage / key / name / f"part-{index}.stl", STL)
        write_file(storage / key / name / "preview.jpg", PNG)
    return key


def vault_tree(storage: Path) -> str:
    """One product whose payload is an archive wrapper beside it (`Vault`)."""
    key = "Vault"
    wrapper = storage / key / "Vault Archive_18.11.2026"
    write_text(wrapper / "Instructions Manual_14.11.26.txt", "Vault - assembly instructions\n")
    for name in ("01. DRAWERS", "02. SUSTAINERS", "03. MODIFICATIONS"):
        for index in range(4):
            write_file(wrapper / name / f"part-{index}.stl", STL)
        write_file(wrapper / name / "preview.jpg", PNG)
    return key


# --- the invariant ----------------------------------------------------------


def expected_matches(candidate_key: str, path: Path, project_keys) -> List[proposal_engine.Option]:
    """The engine's own ranking for one row, measured independently of the report."""
    if candidate_key.lower().endswith(".zip"):
        facts, options = _archive_ranking(path, candidate_key)
        return options
    facts = proposal_engine.measure(str(path), candidate_key)
    options, _reasons, _repo = proposal_engine.propose(
        facts, project_keys, git_timeout=bundle_service.GIT_PROBE_TIMEOUT
    )
    return options


def _archive_ranking(path: Path, key: str):
    facts = proposal_engine.measure_archive(str(path), key)
    options, _reasons = proposal_engine.propose_archive(facts)
    return facts, options


def assert_row_agrees(row: dict, options, *, expected_kind: str) -> None:
    """One row against the engine's list, field for field and in order."""
    assert row["key"], row
    assert [match["shape"] for match in row["matches"]] == [option.shape for option in options], row["key"]
    assert (
        [match["confidence"] for match in row["matches"]]
        == [option.confidence for option in options]
    ), row["key"]
    for match, option in zip(row["matches"], options, strict=True):
        # The one token that names which engine produced this reading.
        assert match["rule"] == f"engine:{option.shape}"
        creatable = option.shape in (
            bundle_service.ARCHIVE_CREATABLE_SHAPES
            if expected_kind == "archive"
            else bundle_service.DIRECTORY_CREATABLE_SHAPES
        )
        assert match["creatable"] is creatable, (row["key"], match)
        # A non-creatable match always says why; a creatable one carries a note
        # only when its boundary was unreadable (AC-4, Mf-ky16).
        if not creatable:
            assert match["note"].startswith("needs arranging first: "), match["note"]
        else:
            assert match["note"] is None or option.note == match["note"], (row["key"], match)
    # The row's own three wire fields ARE `matches[0]`, by construction.
    assert row["metadata_shape"] == options[0].shape
    assert row["confidence"] == options[0].confidence
    assert row["detector"] == row["matches"][0]["rule"]
    # Nothing but the engine ranks: no legacy reading can be on this wire.
    for match in row["matches"]:
        assert match["rule"] == "engine:" + match["shape"], match


def test_every_row_is_recommended_by_the_engine(isolated_storage: Path) -> None:
    """The whole report, row by row, against an independent re-ranking."""
    keys = {
        elecrow_tree(isolated_storage): "directory",
        marbles_tree(isolated_storage): "directory",
        stagetop_tree(isolated_storage): "directory",
        vault_tree(isolated_storage): "directory",
    }
    archive_key = "import/models-only.zip"
    write_zip(isolated_storage / archive_key, {"a.stl": STL, "b.stl": STL, "c.stl": STL})
    keys[archive_key] = "archive"

    report = bundle_service.build_report(limit=bundle_service.MAX_SCAN_ENTRIES)
    by_key = rows(report)

    assert set(by_key) == set(keys), sorted(set(by_key) ^ set(keys))

    for key, kind in keys.items():
        options = expected_matches(key, isolated_storage / key, frozenset())
        assert options, key
        assert_row_agrees(by_key[key], options, expected_kind=kind)

    # The shapes the owner's library actually produces, named so a change is loud.
    assert by_key[elecrow_key()]["metadata_shape"] == "one-project"
    assert by_key["import/OutOfMarbles"]["metadata_shape"] == "split-top-folders"
    assert by_key["import/OutOfMarbles"]["matches"][0]["creatable"] is False
    assert by_key["import/Stagetop"]["metadata_shape"] == "one-project"
    assert by_key["Vault"]["metadata_shape"] == "one-project"
    assert by_key[archive_key]["metadata_shape"] == "archive-project"
    assert by_key[archive_key]["blocked_reason"] == bundle_service.ARCHIVE_ONLY
    assert by_key[archive_key]["matches"][0]["creatable"] is True

    # And the split's children are the split: one row, never one per product.
    assert [key for key in by_key if key.startswith("import/OutOfMarbles/")] == []
    assert [key for key in by_key if key.startswith("Vault/")] == []


def elecrow_key() -> str:
    return "elecrow-7-inch-screen-console-model_files"


def test_an_existing_project_key_reaches_the_engine_as_add_to_project(isolated_storage: Path) -> None:
    """`project_keys` are injected in STORAGE-relative spelling, under a scope.

    A scoped report whose root sits INSIDE an existing project key must read its
    children as `add-to-project` - the reading that only exists when the engine is
    handed the marker keys. Mutation: drop the injection (pass `()` or the
    walk-relative keys) and every row below reads `one-project` instead.
    """
    project_key = "import/Stagetop"
    for name in ("piece one", "piece two"):
        for index in range(3):
            write_file(isolated_storage / project_key / "pieces" / name / f"part{index}.stl", STL)
        write_file(isolated_storage / project_key / "pieces" / name / "preview.jpg", PNG)
    with Session(engine) as session:
        project_service.create_project(
            session,
            "Stagetop project",
            None,
            None,
            origin="imported",
            metadata={"_bundle": {"key": project_key, "detector": "engine:one-project"}},
        )

    scope = f"{project_key}/pieces"
    report = bundle_service.build_report(path=scope, limit=bundle_service.MAX_SCAN_ENTRIES)
    by_key = {row["key"]: row for row in report["bundles"]}
    assert len(by_key) == 2, sorted(by_key)

    project_keys = bundle_service._marker_keys()
    for key, row in by_key.items():
        options = expected_matches(key, isolated_storage / key, project_keys)
        assert options[0].shape == "add-to-project", (key, [o.shape for o in options])
        assert_row_agrees(row, options, expected_kind="directory")
        assert row["matches"][0]["creatable"] is False
        assert row["matches"][0]["note"].startswith("needs arranging first: ")


def test_no_legacy_reading_can_reach_the_wire() -> None:
    """The seam is structural: the module cannot produce a second reading.

    `bundle_detect` is deleted, and neither its name nor its ranking entry point may
    come back through `bundle_service`. Mutation: re-add `import bundle_detect` (or
    a `shape_matches` call) and this reds before any fixture could.
    """
    source = inspect.getsource(bundle_service)
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "bundle_detect" not in imported, "the deleted detector is back"
    assert "bundle_detect" not in source, "a docstring or a local import names the deleted detector"
    assert "shape_matches" not in source, "the legacy ranking entry point is back"

    # The engine is the only module that ranks, and it is imported.
    assert "proposal_engine" in imported
