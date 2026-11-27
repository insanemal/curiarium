"""Migration 016 records the move ledger.

016 is DB-only and append-only: one new table and its indexes, no column of an
earlier table changed, no backfill, no filesystem work. The tests here pin the
ledger (version AND name, which `run_migrations` refuses to mismatch), the
objects that exist afterwards, and the three properties a later change could
quietly break:

* re-applying 016 is harmless - every object is `IF NOT EXISTS` - and it
  preserves a row that is already there;
* a v15 database upgrades to 16 without changing the rows it already had;
* the UNIQUE index on `intent` is what makes "same intent = same intent" a
  database fact, so a second move carrying one intent is refused instead of
  writing a second row (the applied/undone/landed zero defaults are pinned by an
  INSERT that omits them).

Later migrations may be appended, so the version assertions scope themselves the
way `test_migration_015.py` does - there is deliberately no
`MIGRATIONS[-1].version == 16` pin.
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from migrations import MIGRATIONS, _migration_016_move_ledger, applied_migrations, run_migrations

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
}

MOVE_LEDGER_INDEXES = (
    "idx_move_ledger_intent_unique",
    "idx_move_ledger_created_at",
    "idx_move_ledger_undone",
)


def sqlite_engine(path: Path) -> Engine:
    database_engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(database_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        # Same per-connection pragma the API engine installs in `db.py`.
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    return database_engine


def make_v15_engine(path: Path) -> Engine:
    database_engine = sqlite_engine(path)
    assert run_migrations(database_engine, MIGRATIONS[:15]) == 15
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


def migration_016():
    return next(migration for migration in MIGRATIONS if migration.version == 16)


def insert_move(conn, move_id: str, intent: str, key: str = "Old/Row") -> None:
    conn.exec_driver_sql(
        "INSERT INTO move_ledger(id, intent, key, pairs_json, created_at) "
        "VALUES (?, ?, ?, '[{\"from\": \"a\", \"to\": \"b\"}]', '2026-01-01T00:00:00.000000+00:00')",
        (move_id, intent, key),
    )


def test_fresh_database_reaches_v16(tmp_path: Path) -> None:
    database_engine = sqlite_engine(tmp_path / "fresh.db")

    assert run_migrations(database_engine) == MIGRATIONS[-1].version
    assert migration_016().name == "move_ledger"
    ledger = applied_migrations(database_engine)
    assert {version: ledger[version] for version in SHIPPED_LEDGER} == SHIPPED_LEDGER
    # Version AND name, which is the pair `run_migrations` compares on a re-run.
    assert ledger[16] == "move_ledger"


def test_016_creates_the_one_table_and_its_indexes(tmp_path: Path) -> None:
    database_engine = sqlite_engine(tmp_path / "objects.db")
    run_migrations(database_engine)

    assert "move_ledger" in table_names(database_engine)
    assert set(MOVE_LEDGER_INDEXES) <= index_names(database_engine)
    assert set(columns(database_engine, "move_ledger")) == {
        "id",
        "intent",
        "key",
        "pairs_json",
        "landed",
        "applied",
        "undone",
        "superseded_by",
        "created_at",
    }


def test_a_v15_database_upgrades_without_touching_its_operation_row(tmp_path: Path) -> None:
    database_engine = make_v15_engine(tmp_path / "upgrade.db")
    with database_engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO operation(id, workflow, target, plan_json, plan_digest, "
            "idempotency_key, status, submitted_by, request_id, created_at) "
            "VALUES ('op-1', 'create-project', 'Row', '{}', 'digest', 'key-op-1', 'queued', "
            "'tester', '-', '2026-01-01T00:00:00.000000+00:00')"
        )

    assert run_migrations(database_engine) == MIGRATIONS[-1].version

    with database_engine.connect() as conn:
        operation = conn.exec_driver_sql(
            "SELECT id, workflow, status, idempotency_key FROM operation"
        ).fetchone()
        moves = conn.exec_driver_sql("SELECT COUNT(*) FROM move_ledger").fetchone()[0]
    assert tuple(operation) == ("op-1", "create-project", "queued", "key-op-1")
    assert moves == 0


def test_re_applying_016_is_a_no_op(tmp_path: Path) -> None:
    database_engine = sqlite_engine(tmp_path / "reapply.db")
    assert run_migrations(database_engine) == MIGRATIONS[-1].version

    # A second `run_migrations` sees 16 recorded and applies nothing.
    assert run_migrations(database_engine) == MIGRATIONS[-1].version
    # A direct apply on the same connection is harmless (`IF NOT EXISTS`) and
    # keeps the row it finds.
    with database_engine.begin() as conn:
        insert_move(conn, "m-1", "intent-1")
        _migration_016_move_ledger(conn)
    with database_engine.connect() as conn:
        rows = conn.exec_driver_sql("SELECT id, intent, key FROM move_ledger").fetchall()
    assert [tuple(row) for row in rows] == [("m-1", "intent-1", "Old/Row")]


def test_one_intent_is_one_row(tmp_path: Path) -> None:
    database_engine = sqlite_engine(tmp_path / "unique.db")
    run_migrations(database_engine)

    with database_engine.begin() as conn:
        insert_move(conn, "m-1", "intent-1")

    # The falsifier: a second move with one intent writing a second row.
    with pytest.raises(IntegrityError):
        with database_engine.begin() as conn:
            insert_move(conn, "m-2", "intent-1", key="Other/Row")


def test_the_flags_and_progress_default_to_zero(tmp_path: Path) -> None:
    database_engine = sqlite_engine(tmp_path / "defaults.db")
    run_migrations(database_engine)

    with database_engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO move_ledger(id, intent, key, pairs_json, created_at) "
            "VALUES ('m-1', 'intent-1', 'Old/Row', '[]', "
            "'2026-01-01T00:00:00.000000+00:00')"
        )

    with database_engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT landed, applied, undone, superseded_by FROM move_ledger"
        ).fetchone()
    assert tuple(row) == (0, 0, 0, None)
