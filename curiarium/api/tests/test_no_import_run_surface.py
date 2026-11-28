"""The `import_run` surface is gone, and it stays gone (Mf-i9ko).

`import_run` came from Mf-7fvj as mount-import observability: the startup walk
wrote run rows and `GET /imports/runs{,/{run_id}}` read them. Mf-y2u3 proved by
AST that no route, planner, step body or door ever wrote one - a door's record is
the operation row by construction - and Mf-i9ko finished the job: the walk is a
`mount-import` OPERATION whose counters are that operation's step result, the two
read routes are deleted, and migration 17 drops the table.

A DROP is append-only and it destroys rows, so this pin moved with the surface
instead of being deleted with it. Its polarity flipped from "exactly these
modules" to "no module at all", and both halves are now one direction:

* a name reappearing anywhere in production is a NEW writer or a NEW reader of a
  table the ledger dropped - there is no legitimate one left, so it fails;
* the ledger may create the table exactly once (008) and drop it exactly once
  (017), which is what makes the drop a new, un-undoable entry rather than an
  edit of an immutable migration;
* no test module may drive the surface either, so a re-introduced route cannot
  arrive with tests that make it look intended.

The scan is AST-based on purpose: a name in a docstring or a comment is not a
reference. A textual grep would have counted this file's own prose, which is how
the first draft of this pin failed against itself.
"""

import ast
from pathlib import Path
from typing import Set

API = Path(__file__).resolve().parent.parent

# The identifiers that make a module part of the surface: the table's name, the
# module that owned it, the class, and the three response models the read routes
# served. All six stay listed, so re-introducing ANY of those names fails instead
# of slipping past a vocabulary shortened to match what happens to be left.
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

# `migrations.py` is checked separately, and textually, because the ledger names
# the table only inside raw SQL: 008's CREATE and its index, 017's DROP of both.
LEDGER = "migrations.py"

# The test modules that may drive the surface. The set is empty because the
# surface is gone: `_imports_the_surface` is the same signal it always was (an
# import of the owner module or of the model class), so a re-introduced route that
# arrives with a test that imports it reds here. The per-file "the source names
# the table" loop that used to walk this set is gone with the entries - it existed
# to stop the follow-up satisfying the pin by deleting the last caller, and with
# the surface gone the empty set IS the invariant.
EXPECTED_TESTS: Set[str] = set()


def _references(path: Path) -> Set[str]:
    """Which MARKERS this module's CODE mentions; docstrings and comments excluded.

    Names, attributes, import aliases, and the names a module DEFINES or takes as
    an argument - which is how a class or a route parameter counts as a reference
    without a textual grep.
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
    imports one of the two, and a test that merely spells the table in an SQL
    literal (the migration round-trips) does not.
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


def _migration_functions_naming_the_table(source: str) -> Set[str]:
    """Which `_migration_*` bodies mention the table, docstring included.

    An AST walk over the string literals of each migration function, not a grep:
    the ledger names `import_run` inside raw SQL and in 017's docstring, and the
    question here is which function bodies say it, not which lines of the file do.
    """
    tree = ast.parse(source)
    naming: Set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("_migration_"):
            continue
        literals = [
            child.value
            for child in ast.walk(node)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        ]
        if any("import_run" in literal for literal in literals):
            naming.add(node.name)
    return naming


def test_no_module_references_the_import_run_surface() -> None:
    """The production reference set is EMPTY: nothing writes it, nothing reads it.

    The false state this catches: a later ticket adding a writer or a reader - a
    step body recording a run, a route listing one - against a table the ledger
    dropped. There is no legitimate name left to allow, so this assertion has no
    allow-list and no reverse direction to check: a name reappearing IS the
    failure, whether it would write or read.
    """
    found = {
        str(path.relative_to(API))
        for path in API.rglob("*.py")
        if "tests" not in path.parts and _references(path)
    }

    assert found == set(), (
        f"{sorted(found)} references the import_run surface: that name is a new writer or "
        "reader of a table migration 17 dropped, and neither one may come back"
    )


def test_the_ledger_creates_the_table_once_and_drops_it_once() -> None:
    """The table has exactly one CREATE (008) and exactly one DROP (017).

    Textual on purpose and scoped to one file: the raw SQL is a string literal and
    an AST walk would have to re-implement SQL parsing to read it. The two
    `Migration(...)` pins are the other half of the same fact - version AND name,
    the pair `run_migrations` compares - so a DROP can only be a NEW, un-undoable
    ledger entry and never an edit of 008. The AST walk over the migration
    functions is what keeps a second CREATE or DROP from hiding in a third
    function body.
    """
    source = (API / LEDGER).read_text(encoding="utf-8")
    assert source.count("CREATE TABLE IF NOT EXISTS import_run (") == 1
    assert source.count("DROP TABLE IF EXISTS import_run") == 1
    assert source.count('Migration(8, "import_run_history"') == 1
    assert source.count('Migration(17, "drop_import_run_history"') == 1
    assert _migration_functions_naming_the_table(source) == {
        "_migration_008_import_run_history",
        "_migration_017_drop_import_run_history",
    }


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


def test_no_test_module_drives_the_deleted_surface() -> None:
    """No test module imports the owner module or the model class.

    The same `_imports_the_surface` signal as before, now over an empty expected
    set: a test that drives the read routes or the writers imports one of the two,
    and there is nothing left to drive. A test that merely spells the table in an
    SQL literal is not a driver and is not listed - the migration round-trips are
    exactly that, and `test_migration_017.py` owns what a v16 database can still
    show.
    """
    found = {
        path.name
        for path in (API / "tests").glob("test_*.py")
        if path.name != Path(__file__).name and _imports_the_surface(path)
    }
    assert found == EXPECTED_TESTS, (
        f"{sorted(found)} imports the import_run surface: the table migration 17 dropped "
        "has no route, no model and no writer left to test"
    )
