"""Migration 015 records the operations ledger.

015 is DB-only and append-only: three new tables and their indexes, no column of
an earlier table changed, no backfill, no filesystem work. The tests here pin the
ledger (version AND name, which `run_migrations` refuses to mismatch), the
objects that exist afterwards, and the three properties a later change could
quietly break:

* re-applying 015 is harmless - every object is `IF NOT EXISTS` - and it
  preserves a row that is already there;
* a v14 database upgrades to 15 without changing the rows it already had;
* `operation_step.operation_id` is a real foreign key, so a step for an unknown
  operation is refused rather than silently orphaned (the FK pragma is on in the
  API engine and in the helper engine below).

Later migrations may be appended, so the version assertions scope themselves the
way `test_migration_014.py` does - there is deliberately no
`MIGRATIONS[-1].version == 15` pin.
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from migrations import MIGRATIONS, _migration_015_operations_core, applied_migrations, run_migrations

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
}

OPERATION_TABLES = ("operation", "operation_step", "operation_lock")
OPERATION_INDEXES = (
    "idx_operation_idempotency_key_unique",
    "idx_operation_status_created_at",
    "idx_operation_created_at",
    "idx_operation_target",
    "idx_operation_step_operation_index",
    "idx_operation_lock_operation_id",
)


def sqlite_engine(path: Path) -> Engine:
    database_engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(database_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        # Same per-connection pragma the API engine installs in `db.py`.
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    return database_engine


def make_v14_engine(path: Path) -> Engine:
    database_engine = sqlite_engine(path)
    assert run_migrations(database_engine, MIGRATIONS[:14]) == 14
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


def columns(database_engine: Engine, table: str) -> dict[str, tuple]:
    with database_engine.connect() as conn:
        rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]): tuple(row[1:6]) for row in rows}


def migration_015():
    return next(migration for migration in MIGRATIONS if migration.version == 15)


def insert_operation(conn, operation_id: str, status: str = "queued") -> None:
    conn.exec_driver_sql(
        "INSERT INTO operation(id, workflow, target, plan_json, plan_digest, idempotency_key, "
        "status, submitted_by, request_id, created_at) "
        "VALUES (?, 'create-project', 'Row', '{}', 'digest', ?, ?, 'tester', '-', '2026-01-01T00:00:00.000000+00:00')",
        (operation_id, f"key-{operation_id}", status),
    )


def test_fresh_database_reaches_v15(tmp_path: Path) -> None:
    database_engine = sqlite_engine(tmp_path / "fresh.db")

    assert run_migrations(database_engine) == MIGRATIONS[-1].version
    assert migration_015().name == "operations_core"
    ledger = applied_migrations(database_engine)
    assert {version: ledger[version] for version in SHIPPED_LEDGER} == SHIPPED_LEDGER
    # Version AND name, which is the pair `run_migrations` compares on a re-run.
    assert ledger[15] == "operations_core"


def test_015_creates_the_three_tables_and_their_indexes(tmp_path: Path) -> None:
    database_engine = sqlite_engine(tmp_path / "objects.db")
    run_migrations(database_engine)

    assert set(OPERATION_TABLES) <= table_names(database_engine)
    assert set(OPERATION_INDEXES) <= index_names(database_engine)

    assert set(columns(database_engine, "operation")) == {
        "id",
        "workflow",
        "target",
        "plan_json",
        "plan_digest",
        "idempotency_key",
        "status",
        "submitted_by",
        "submitted_by_id",
        "request_id",
        "created_at",
        "started_at",
        "finished_at",
        "error",
        "undo_state",
        "superseded_by",
    }
    # `index` is an SQLite keyword, so the column is `step_index`; the pair is
    # what makes a step's position a fact rather than an ordering accident.
    assert set(columns(database_engine, "operation_step")) == {
        "id",
        "operation_id",
        "step_index",
        "op",
        "args_json",
        "status",
        "result_json",
        "error",
        "started_at",
        "finished_at",
    }
    assert set(columns(database_engine, "operation_lock")) == {"path", "operation_id", "created_at"}


def test_a_v14_database_upgrades_without_touching_its_rows(tmp_path: Path) -> None:
    database_engine = make_v14_engine(tmp_path / "upgrade.db")
    with database_engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO asset(id, filename, mime, size, name, storage_path) "
            "VALUES ('a1', 'cube.stl', 'model/stl', 7, 'cube', 'Row/cube.stl')"
        )
        conn.exec_driver_sql(
            "INSERT INTO project(id, name, created_at, updated_at) "
            "VALUES ('p1', 'Row', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )

    assert run_migrations(database_engine) == MIGRATIONS[-1].version

    with database_engine.connect() as conn:
        asset = conn.exec_driver_sql(
            "SELECT id, filename, name, storage_path FROM asset"
        ).fetchone()
        project = conn.exec_driver_sql("SELECT id, name FROM project").fetchone()
        operations = conn.exec_driver_sql("SELECT COUNT(*) FROM operation").fetchone()[0]
    assert tuple(asset) == ("a1", "cube.stl", "cube", "Row/cube.stl")
    assert tuple(project) == ("p1", "Row")
    assert operations == 0


def test_re_applying_015_is_a_no_op(tmp_path: Path) -> None:
    database_engine = sqlite_engine(tmp_path / "reapply.db")
    assert run_migrations(database_engine) == 15

    with database_engine.begin() as conn:
        insert_operation(conn, "op-1", status="queued")

    # A second `run_migrations` sees 15 recorded and applies nothing.
    assert run_migrations(database_engine) == 15
    # A direct apply of the migration function is harmless (`IF NOT EXISTS`) and
    # keeps the row it finds.
    with database_engine.begin() as conn:
        _migration_015_operations_core(conn)
    with database_engine.connect() as conn:
        rows = conn.exec_driver_sql("SELECT id, status FROM operation").fetchall()
    assert [tuple(row) for row in rows] == [("op-1", "queued")]


def test_a_step_for_an_unknown_operation_is_refused(tmp_path: Path) -> None:
    database_engine = sqlite_engine(tmp_path / "fk.db")
    run_migrations(database_engine)

    with pytest.raises(IntegrityError):
        with database_engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO operation_step(id, operation_id, step_index, op, status) "
                "VALUES ('s1', 'no-such-operation', 0, 'create-project', 'pending')"
            )


def test_the_idempotency_key_is_unique(tmp_path: Path) -> None:
    database_engine = sqlite_engine(tmp_path / "unique.db")
    run_migrations(database_engine)

    with database_engine.begin() as conn:
        insert_operation(conn, "op-1")

    with pytest.raises(IntegrityError):
        with database_engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO operation(id, workflow, target, plan_json, plan_digest, "
                "idempotency_key, status, submitted_by, request_id, created_at) "
                "VALUES ('op-2', 'create-project', 'Row', '{}', 'd', 'key-op-1', 'queued', "
                "'tester', '-', '2026-01-01T00:00:00.000000+00:00')"
            )


def test_the_status_and_undo_vocabularies_are_closed(tmp_path: Path) -> None:
    database_engine = sqlite_engine(tmp_path / "checks.db")
    run_migrations(database_engine)

    with pytest.raises(IntegrityError):
        with database_engine.begin() as conn:
            insert_operation(conn, "op-1", status="exploded")

    with pytest.raises(IntegrityError):
        with database_engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO operation(id, workflow, target, plan_json, plan_digest, "
                "idempotency_key, status, submitted_by, request_id, created_at, undo_state) "
                "VALUES ('op-3', 'create-project', 'Row', '{}', 'd', 'k3', 'done', 'tester', "
                "'-', '2026-01-01T00:00:00.000000+00:00', 'maybe')"
            )
