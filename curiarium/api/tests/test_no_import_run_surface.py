"""The `import_run` surface is the mount importer's, and nothing else's (Mf-y2u3).

`import_run` came from Mf-7fvj as mount-import observability: the startup walk
writes run rows, `GET /imports/runs{,/{run_id}}` reads them. When this ticket
converted the four import doors into operations, the plan expected a fold - and
the measurement said there was nothing to fold: NO route, planner, step body or
door ever wrote an `import_run` row. A door's record is the operation row, by
construction.

The table is dropped in a later migration (the follow-up ticket Mf-y2u3 filed,
which owns migration 17), and a DROP is append-only: it cannot be undone and it
destroys rows. So the reference set is pinned HERE, exactly, BEFORE the rows are
proven unread:

* a new writer or reader appearing outside the mount path fails this test, so
  the follow-up migration cannot be written against a reference set that moved;
* a writer disappearing from the mount path fails it too, so the pin cannot be
  satisfied by deleting the last caller of a table nobody reads.

The scan is AST-based on purpose: a name in a docstring or a comment is not a
reference. A textual grep would have counted this file's own prose, which is how
the first draft of this pin failed against itself.
"""

import ast
from pathlib import Path
from typing import Dict, Set

API = Path(__file__).resolve().parent.parent

# The identifiers that make a module part of the surface: the table's name, the
# module that owns it, the class, and the three response models the read routes
# serve. A module is in the reference set when its CODE mentions any of them.
MARKERS = frozenset(
    {
        "import_run",
        "import_runs",
        "ImportRun",
        "ImportRunSummaryOut",
        "ImportRunDetailOut",
        "ImportRunListOut",
    }
)

# Every module that may mention those names, with the one reason each may.
EXPECTED: Dict[str, str] = {
    # The only writer, and the only place a run's lifecycle lives.
    "import_runs.py": "the begin/finish/fail/skip writers and the live overlay",
    "mount_import.py": "the startup mount walk, which is the only caller of them",
    # The only reader, behind the two GET routes.
    "routers/import_runs.py": "GET /imports/runs and GET /imports/runs/{run_id}",
    # The model, and the barrel that re-exports the two response models.
    "models.py": "the SQLModel class for the table migration 008 created",
    "schemas/import_runs.py": "the three response models the read routes serve",
    "schemas/__init__.py": "re-exports those models through the barrel",
    # Wiring: the router include and the interrupted-run repair on boot.
    "main.py": "the router include and import_runs.mark_interrupted() at startup",
}

# `migrations.py` is checked separately, and textually, because the ledger names
# the table only inside raw SQL: migration 008's CREATE and the index. It is the
# reason a DROP must be a NEW migration and never an edit of 008.
LEDGER = "migrations.py"

# The test modules that legitimately drive those surfaces. They are listed so the
# follow-up can see, before it deletes anything, which assertions must be
# re-homed (Mf-y2u3's plan re-homes `test_import_runs.py` as an operation-row
# test, because the doors' record is the operation row).
EXPECTED_TESTS = {
    "test_import_runs.py",
    "test_mount_import.py",
    "test_permissions_matrix.py",
}


def _references(path: Path) -> Set[str]:
    """Which MARKERS this module's CODE mentions; docstrings and comments excluded.

    Names, attributes, import aliases, and the names a module DEFINES or takes as
    an argument - which is how `models.py`'s class and a route's parameter count as
    references without a textual grep.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.split(".")[-1])
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            found.add(node.name)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
    return found & MARKERS


def _imports_the_surface(path: Path) -> bool:
    """Whether a module IMPORTS the owner module or the model class.

    The test-file half's signal: a test that drives the read routes or the writers
    imports one of the two, and a test that merely mentions the table in an SQL
    literal does not.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name.split(".")[-1] == "import_runs" for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == "ImportRun" for alias in node.names
        ):
            return True
    return False


def test_the_reference_set_is_exactly_the_mount_importer_and_its_readers() -> None:
    """Any module outside `EXPECTED` that references the surface is a NEW surface.

    The false state this catches: a later ticket adding a writer or a reader - a
    step body recording a run, a route listing one - after this pin was written,
    which the follow-up migration's DROP would then destroy rows under. The
    reverse direction is pinned too, so deleting the last caller reds instead of
    quietly satisfying the test.
    """
    found = {
        str(path.relative_to(API))
        for path in API.rglob("*.py")
        if "tests" not in path.parts and _references(path)
    }

    assert found == set(EXPECTED), {
        "new": sorted(found - set(EXPECTED)),
        "gone": sorted(set(EXPECTED) - found),
    }


def test_the_table_is_created_by_one_append_only_migration_and_nowhere_else() -> None:
    """The ledger names the table in migration 008, so a drop is a new entry.

    Textual on purpose and scoped to one file: the raw SQL is a string literal and
    an AST walk would have to re-implement SQL parsing to see it. The assertion is
    that the CREATE and the index live in exactly one migration, which is what
    makes `drop import_run` a new, un-undoable ledger entry.
    """
    source = (API / LEDGER).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS import_run (" in source
    assert "Migration(8, \"import_run_history\"" in source
    assert source.count("CREATE TABLE IF NOT EXISTS import_run") == 1


def test_no_route_planner_step_or_door_names_import_run() -> None:
    """The three surfaces this ticket moved cannot have touched it.

    Narrower than the pin above and deliberately so: it is AC-9's sentence spelled
    as the files this ticket edited. A planner or a step body that referenced
    `import_run` would be the fold the measurement said was vacuous.
    """
    surfaces = [
        API / "operations_service.py",
        API / "operations_runner.py",
        API / "routers" / "imports.py",
        API / "routers" / "uploads.py",
        API / "import_service.py",
        API / "project_import.py",
        API / "archive_service.py",
        API / "bundle_service.py",
    ]
    for path in surfaces:
        assert not _references(path), (
            f"{path.name} references import_run: a door's record is the operation row"
        )


def test_the_tests_that_drive_the_surface_are_named() -> None:
    """The follow-up deletes modules, so the tests driving them are listed first.

    The signal is an IMPORT of the owner module or of the model class, because that
    is what driving the surface means: `test_import_runs.py` posts the two read
    routes, `test_mount_import.py` drives the walk, and the permission sweep has a
    call spec for the routes. A test that only spells the table in an SQL literal
    (the migration round-trips) is listed separately, and is the reason a DROP must
    be a NEW migration rather than an edit of 008.
    """
    found = {
        path.name
        for path in (API / "tests").glob("test_*.py")
        if path.name != Path(__file__).name and _imports_the_surface(path)
    }
    assert found == EXPECTED_TESTS, {
        "new": sorted(found - EXPECTED_TESTS),
        "gone": sorted(EXPECTED_TESTS - found),
    }
    database = (API / "tests")
    for name in EXPECTED_TESTS:
        source = (database / name).read_text(encoding="utf-8")
        assert "import_run" in source or "ImportRun" in source, name
