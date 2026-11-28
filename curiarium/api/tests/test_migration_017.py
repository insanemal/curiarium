"""Migration 017 drops the mount-import run history table.

017 is DB-only, append-only and irreversible: it adds no table and changes no
column, it DROPS `import_run` and its index. 008 created that table as
mount-import observability; the walk is a `mount-import` OPERATION now and its
counters are that operation's step result, so the table has no writer and no
reader left. A DROP cannot be undone and it DESTROYS ROWS, which is why it is
its own ledger entry and never an edit of 008.

The tests pin the ledger (version AND name, which `run_migrations` refuses to
mismatch), the objects that exist afterwards, and the four properties a later
change could quietly break:

* a fresh database reaches 17 and has no `import_run` table and no index on it;
* a v16 database upgrades without changing the rows it already had;
* a row that exists at upgrade time is destroyed with the table - the accepted
  cost, written down rather than assumed;
* re-applying 017 is harmless (`IF EXISTS` on both objects), and a second
  `run_migrations` applies nothing;
* 016 is the last version whose closed CHECK lists can still be shown, so the
  still-meaningful half of `test_migration_008.py` lives here (that module is
  deleted: its "fresh database reaches v8" and `SELECT *` at v8 assertions cannot
  pass once 17 drops the table).

Later migrations may be appended, so the version assertions scope themselves the
way `test_migration_016.py` does - there is deliberately no
`MIGRATIONS[-1].version == 17` pin.
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from migrations import (
    MIGRATIONS,
    Migration,
    _migration_017_drop_import_run_history,
    applied_migrations,
    run_migrations,
)

SHIPPED_LEDGER = {
    1: "initial_schema",
    2: "nested_folders_and_mount_sources",
    3: "asset_metadata_and_bundles",
    4: "folder_identity_and_reference_flags",
    5: "users_and_sessions",
    6: "audit_log",
    7: "projects",
    8: "import_run_history",
    9: "asset_file_order",
    10: "oidc_identities",
    11: "session_rotation_history",
    12: "mfa_totp",
    13: "asset_thumb_version",
    14: "asset_attribution",
    15: "operations_core",
    16: "move_ledger",
}

TIMESTAMP = "2026-01-01T00:00:00.000000+00:00"

# The columns migration 015 actually created, in DDL order, so "the row is
# byte-identical" compares the row a v16 database holds and not a subset of it.
OPERATION_COLUMNS = (
    "id, workflow, target, plan_json, plan_digest, idempotency_key, status, "
    "submitted_by, submitted_by_id, request_id, created_at, started_at, "
    "finished_at, error, undo_state, superseded_by"
)


def sqlite_engine(path: Path) -> Engine:
    database_engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(database_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        # Same per-connection pragma the API engine installs in `db.py`.
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    return database_engine


def make_v16_engine(path: Path) -> Engine:
    database_engine = sqlite_engine(path)
    assert run_migrations(database_engine, MIGRATIONS[:16]) == 16
    return database_engine


def table_names(database_engine: Engine) -> set[str]:
    with database_engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    return {str(row[0]) for row in rows}


def index_names(database_engine: Engine) -> set[str]:
    with database_engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    return {str(row[0]) for row in rows}


def migration_017() -> Migration:
    return next(migration for migration in MIGRATIONS if migration.version == 17)


def insert_operation(conn) -> None:
    """One `operation` row with every column 015 created and no defaults left."""
    conn.exec_driver_sql(
        "INSERT INTO operation(id, workflow, target, plan_json, plan_digest, "
        "idempotency_key, status, submitted_by, submitted_by_id, request_id, "
        "created_at, started_at, finished_at, error, undo_state, superseded_by) "
        "VALUES ('op-1', 'mount-import', '/mnt/models', '{}', 'digest', 'key-op-1', "
        "'done', 'system', NULL, '-', ?, ?, ?, NULL, 'none', NULL)",
        (TIMESTAMP, TIMESTAMP, TIMESTAMP),
    )


def insert_run(
    conn,
    run_id: str,
    *,
    kind: str = "mount",
    status: str = "running",
    mode: str | None = None,
    started_at: float = 1.0,
) -> None:
    conn.exec_driver_sql(
        "INSERT INTO import_run(id, kind, status, mode, started_at) VALUES (?, ?, ?, ?, ?)",
        (run_id, kind, status, mode, started_at),
    )


def operation_rows(database_engine: Engine) -> list[tuple]:
    with database_engine.connect() as conn:
        rows = conn.exec_driver_sql(f"SELECT {OPERATION_COLUMNS} FROM operation ORDER BY id").fetchall()
    return [tuple(row) for row in rows]


def test_fresh_database_reaches_v17_and_has_no_import_run_table(tmp_path: Path) -> None:
    database_engine = sqlite_engine(tmp_path / "fresh.db")

    assert run_migrations(database_engine) == MIGRATIONS[-1].version
    assert migration_017().name == "drop_import_run_history"
    # Version AND name: `(17, "drop_import_run_history")` is the pair
    # `run_migrations` compares on a re-run.
    expected_ledger = {**SHIPPED_LEDGER, 17: "drop_import_run_history"}
    ledger = applied_migrations(database_engine)
    assert {version: ledger[version] for version in expected_ledger} == expected_ledger
    assert "import_run" not in table_names(database_engine)
    assert "idx_import_run_started_at" not in index_names(database_engine)


def test_a_v16_database_upgrades_and_keeps_its_rows(tmp_path: Path) -> None:
    database_engine = make_v16_engine(tmp_path / "upgrade.db")
    with database_engine.begin() as conn:
        insert_operation(conn)
        insert_run(conn, "run-1", status="succeeded", mode="copy")
    operations_before = operation_rows(database_engine)
    assert "import_run" in table_names(database_engine)

    assert run_migrations(database_engine) == MIGRATIONS[-1].version

    # The operation row is the record the table's rows were replaced by, so the
    # upgrade must not touch a single column of it.
    assert operation_rows(database_engine) == operations_before
    assert "import_run" not in table_names(database_engine)
    assert "idx_import_run_started_at" not in index_names(database_engine)


def test_a_row_that_exists_at_upgrade_time_is_dropped_with_the_table(tmp_path: Path) -> None:
    """A row that exists at upgrade time is destroyed - the accepted cost.

    There can be one: a deployment whose previous boot died mid-walk leaves a
    `running` row, and the build that upgrades is the build that no longer reads
    it. The cost is bounded by what such a row said - the counters of a walk that
    never finished, whose files the next boot's walk reports as `known` - and a
    `succeeded` row goes the same way, because no surface lists it any more.
    """
    database_engine = make_v16_engine(tmp_path / "rows.db")
    with database_engine.begin() as conn:
        insert_run(conn, "run-running", status="running", mode="copy", started_at=1.0)
        insert_run(conn, "run-succeeded", status="succeeded", mode="copy", started_at=2.0)
    with database_engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT COUNT(*) FROM import_run").scalar_one() == 2

    assert run_migrations(database_engine) == MIGRATIONS[-1].version

    assert "import_run" not in table_names(database_engine)
    assert "idx_import_run_started_at" not in index_names(database_engine)
    # The falsifier: the rows are not somewhere else, the table is unreachable.
    with pytest.raises(OperationalError):
        with database_engine.connect() as conn:
            conn.exec_driver_sql("SELECT COUNT(*) FROM import_run")


def test_re_applying_017_is_a_no_op(tmp_path: Path) -> None:
    database_engine = sqlite_engine(tmp_path / "reapply.db")
    assert run_migrations(database_engine) == MIGRATIONS[-1].version

    # A second `run_migrations` sees 17 recorded and applies nothing.
    assert run_migrations(database_engine) == MIGRATIONS[-1].version
    # A direct apply on a database with no table left is harmless (`IF EXISTS`).
    with database_engine.begin() as conn:
        _migration_017_drop_import_run_history(conn)
    assert "import_run" not in table_names(database_engine)


def test_016_is_the_last_version_that_can_still_show_the_closed_check_lists(tmp_path: Path) -> None:
    """008's still-meaningful assertions moved here.

    This is why `test_migration_008.py` is deleted rather than trimmed: its
    "fresh database reaches v8" and `SELECT *` at v8 assertions cannot pass once
    17 drops the table. The CHECK lists themselves are a fact about a v16
    database, so they are asserted against `MIGRATIONS[:16]` - a bad `status`, a
    bad `mode` and a bad `kind` are still refused there, and after 17 there is no
    table left to refuse anything.
    """
    database_engine = make_v16_engine(tmp_path / "checks.db")

    with pytest.raises(IntegrityError):
        with database_engine.begin() as conn:
            insert_run(conn, "run-bad-kind", kind="storage")
    with pytest.raises(IntegrityError):
        with database_engine.begin() as conn:
            insert_run(conn, "run-bad-status", status="queued")
    with pytest.raises(IntegrityError):
        with database_engine.begin() as conn:
            insert_run(conn, "run-bad-mode", mode="move")

    assert run_migrations(database_engine) == MIGRATIONS[-1].version

    assert "import_run" not in table_names(database_engine)
