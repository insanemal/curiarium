"""Scan a large synthetic tree as one `library-scan` operation while reads continue.

``CURIARIUM_SCAN_STRESS`` sets how many files the tree holds. The default
5000 keeps the normal suite fast; the ticket's acceptance run is

    CURIARIUM_SCAN_STRESS=50000 python -m pytest tests/test_z_library_scan_stress.py -q

The scan is no longer a route of its own (Mf-y2u3): it is planned, submitted and
then polled as an operation row, with the runner driving the walk. Everything is
written to a pytest temp root, so nothing here touches a real mount. The
database is shared for the whole session, so the rows are bulk deleted by
storage-path prefix in ``finally`` and again before the scan starts.
"""
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

import library_scan
import main
import operations_runner
from db import engine
from models import Asset, Folder, Operation, OperationLock, OperationStep

SCAN_FILES = int(os.getenv("CURIARIUM_SCAN_STRESS", "5000"))
FILES_PER_DIR = 100
ROOT_DIR_PREFIX = "zz-scan-stress"
DEADLINE_SECONDS = 900
TERMINAL = {"done", "failed", "cancelled"}


def _auth_header(client: TestClient) -> dict:
    health = client.get("/health").json()
    if not health.get("auth_required"):
        return {}
    token = client.post("/login", json={"username": "admin", "password": "super-secret"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _build_tree(root: Path, files: int) -> int:
    """Write `files` .stl files over ~files/100 directories; return the tree size."""
    for index in range(files):
        path = root / f"{ROOT_DIR_PREFIX}-{index // FILES_PER_DIR:04d}" / f"part-{index:05d}.stl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"solid stress\n")
    return files


def _cleanup() -> None:
    """Bulk-delete this test's assets and folders, whatever state the run ended in."""
    with Session(engine) as session:
        session.execute(delete(Asset).where(Asset.storage_path.like(f"{ROOT_DIR_PREFIX}-%")))
        session.commit()
    with Session(engine) as session:
        for folder in list(session.exec(select(Folder))):
            if folder.name.startswith(ROOT_DIR_PREFIX):
                session.delete(folder)
        session.commit()


def _delete_operation(operation_id: str) -> None:
    """Remove the operation row this test made, steps and locks first."""
    with Session(engine) as session:
        session.execute(delete(OperationStep).where(OperationStep.operation_id == operation_id))
        session.execute(delete(OperationLock).where(OperationLock.operation_id == operation_id))
        session.execute(delete(Operation).where(Operation.id == operation_id))
        session.commit()


def _drain_runner() -> None:
    """The suite's runner is off; drive exactly this queued operation to the end."""
    operations_runner.dispatch_once()
    operations_runner.wait_idle(timeout=DEADLINE_SECONDS)


def test_scan_stays_accurate_and_responsive_under_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    expected_folders = (SCAN_FILES + FILES_PER_DIR - 1) // FILES_PER_DIR
    _build_tree(storage, SCAN_FILES)
    monkeypatch.setattr(library_scan, "STORAGE", storage)

    started = time.monotonic()
    operation_id: str | None = None
    try:
        with TestClient(main.app) as client:
            # the lifespan migrated the schema; start from a clean slate in case
            # an earlier run died before its cleanup
            _cleanup()
            headers = _auth_header(client)
            plan = client.post(
                "/operations/plan",
                json={"workflow": "library-scan", "target": ""},
                headers=headers,
            )
            assert plan.status_code == 200, plan.text
            submitted = client.post(
                "/operations",
                json={"plan_id": plan.json()["plan_id"], "idempotency_key": "stress-scan"},
                headers=headers,
            )
            assert submitted.status_code == 202, submitted.text
            operation_id = submitted.json()["id"]

            worker = threading.Thread(target=_drain_runner, daemon=True)
            worker.start()
            polls = 0
            while True:
                status = client.get(f"/operations/{operation_id}", headers=headers)
                assert status.status_code == 200, status.text
                payload = status.json()
                # Reads must keep working while the scan writes: no 5xx, no
                # "database is locked" leaking out of the API.
                assets = client.get("/assets?limit=5", headers=headers)
                assert assets.status_code == 200, assets.text
                polls += 1
                if payload["status"] in TERMINAL:
                    break
                assert time.monotonic() - started < DEADLINE_SECONDS
                time.sleep(0.05)
            worker.join(timeout=DEADLINE_SECONDS)
            assert not worker.is_alive()

        assert payload["status"] == "done", payload
        result = payload["steps"][0]["result"]
        assert result["registered"] == SCAN_FILES
        assert result["known"] == 0
        assert result["failed"] == 0
        assert result["errors"] == []
        assert result["registered"] + result["known"] + result["failed"] == SCAN_FILES

        with Session(engine) as session:
            stored = session.exec(
                select(Asset.id).where(Asset.storage_path.like(f"{ROOT_DIR_PREFIX}-%"))
            ).all()
            folders = [folder.name for folder in session.exec(select(Folder)).all()]
        assert len(stored) == SCAN_FILES
        scanned_folders = [name for name in folders if name.startswith(ROOT_DIR_PREFIX)]
        assert len(scanned_folders) == expected_folders == len(set(scanned_folders))
        assert polls >= 1
    finally:
        _cleanup()
        if operation_id is not None:
            _delete_operation(operation_id)
