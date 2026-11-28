"""The in-place archive door as the `extract` workflow (Mf-y2u3 3/4).

`POST /import/archive` and `GET /import/archive/entries` are gone. The capability
did not change: `archive_service.plan_extraction` still reads the listing and
`archive_service.extract_out` still writes the twin, but the listing is the plan's
review and the write is a step in the shared queue, followed by a `register` step
that turns exactly the planned files into rows.

Every test here is written to fail if the capability is absent:

* **the review is the deleted listing** - `excluded` names the same junk members
  with the same reasons the entries door flagged, and `members` is the writable
  count, so a client can still see what an archive would write before it writes.
* **the run registers every planned file** - the extract step's own `files` and
  `bytes`, the register step's `registered` list, and the asset rows `GET /assets`
  serves.
* **a second extract is an answer, not an empty plan** - `already_present: True`
  and no second `archive.extract` row, because the primitive writes one only when
  it really wrote.
* **the door's refusals survive** - a partial collision is the failed step's own
  409 sentence with nothing published, and a non-zip or missing archive is
  refused at PLAN time with the primitive's own status and words.
* **undo removes only what the run added** - the empty folder rows its register
  step measured, never the files, and the response says the payload stays.
* **the level is the workflow's** - a contributor may plan and run it, a viewer is
  refused on both requests.
* **the primitive's audit row survives** - one `archive.extract` row for the
  operator's own action, beside the operation's submit row.

Harness: the idioms `tests/test_import_workflows.py` and
`tests/test_extract_and_create.py` established - one migrated database, a temp
storage root, auth off unless a test is about *who* may run an extract.
"""

import json
from pathlib import Path
from typing import Dict, Optional
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

import accounts
import asset_file_service
import asset_service
import audit
import auth
import db
import library_scan
import login_throttle as login_throttle_module
import main
import operations_runner
import operations_service
import permissions
from db import engine
from migrations import run_migrations
from models import (
    Asset,
    AssetFile,
    AuditEvent,
    Folder,
    MfaRecoveryCode,
    Operation,
    OperationLock,
    OperationStep,
    Project,
    ProjectAsset,
    User,
    UserSession,
)

JUNK = ("__MACOSX/._part.stl", ".DS_Store")
HIDDEN = ".hidden-note.stl"
STL_BYTES = b"solid cube\nendsolid cube\n"

BREAK_GLASS = {"username": "admin", "password": "super-secret"}
ADMIN_USERNAME = "root"
ADMIN_PASSWORD = "root-password-1"
CONTRIBUTOR_USERNAME = "contributor"
CONTRIBUTOR_PASSWORD = "contributor-password-1"
VIEWER_USERNAME = "viewer"
VIEWER_PASSWORD = "viewer-password-1"


# --- harness -----------------------------------------------------------------


def truncate_everything() -> None:
    """An empty ledger and an empty library.

    Audit rows are deliberately not removed: the append-only triggers refuse it,
    so each test addresses its own rows instead.
    """
    with Session(engine) as session:
        for model in (
            OperationStep,
            OperationLock,
            Operation,
            ProjectAsset,
            Project,
            AssetFile,
            Asset,
            Folder,
        ):
            session.exec(delete(model))
        session.execute(delete(MfaRecoveryCode))
        session.execute(delete(UserSession))
        session.execute(delete(User))
        session.commit()


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch):
    run_migrations(engine)
    truncate_everything()
    login_throttle_module.limiter.reset()
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    operations_runner.set_step_guard(None)
    yield
    operations_runner.set_step_guard(None)
    operations_runner.wait_idle(timeout=20)
    truncate_everything()
    login_throttle_module.limiter.reset()


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(accounts, "hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))


@pytest.fixture()
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "storage"
    thumbs = root / "thumbs"
    thumbs.mkdir(parents=True)
    monkeypatch.setattr(db, "STORAGE", root)
    monkeypatch.setattr(db, "THUMBS", thumbs)
    monkeypatch.setattr(main, "THUMBS", thumbs)
    monkeypatch.setattr(asset_service, "STORAGE", root)
    monkeypatch.setattr(asset_service, "THUMBS", thumbs)
    monkeypatch.setattr(asset_file_service, "STORAGE", root)
    monkeypatch.setattr(library_scan, "STORAGE", root)
    return root


@pytest.fixture()
def client() -> TestClient:
    with TestClient(main.app) as test_client:
        yield test_client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def sign_in(client: TestClient, token: str, username: str, password: str, role: str) -> str:
    created = client.post(
        "/users", json={"username": username, "password": password, "role": role}, headers=bearer(token)
    )
    assert created.status_code == 201, created.text
    logged_in = client.post("/login", json={"username": username, "password": password})
    assert logged_in.status_code == 200, logged_in.text
    return logged_in.json()["token"]


def plan_of(
    client: TestClient, target: str, *, headers: Optional[dict] = None, **params
) -> dict:
    response = client.post(
        "/operations/plan",
        json={"workflow": "extract", "target": target, "params": params},
        headers=headers or {},
    )
    assert response.status_code == 200, response.text
    return response.json()


def refused_plan(
    client: TestClient, target: str, *, headers: Optional[dict] = None, **params
) -> tuple[int, str]:
    response = client.post(
        "/operations/plan",
        json={"workflow": "extract", "target": target, "params": params},
        headers=headers or {},
    )
    detail = response.json().get("detail")
    assert isinstance(detail, str), response.text
    return response.status_code, detail


def submit_of(
    client: TestClient, plan: dict, key: Optional[str] = None, *, headers: Optional[dict] = None
) -> dict:
    response = client.post(
        "/operations",
        json={"plan_id": plan["plan_id"], "idempotency_key": key or uuid4().hex},
        headers=headers or {},
    )
    assert response.status_code == 202, response.text
    return response.json()


def operation_of(client: TestClient, operation_id: str, *, headers: Optional[dict] = None) -> dict:
    response = client.get(f"/operations/{operation_id}", headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()


def run_to_completion() -> None:
    operations_runner.dispatch_once()
    operations_runner.wait_idle(timeout=30)


def run_extract(
    client: TestClient,
    target: str,
    *,
    headers: Optional[dict] = None,
    expect_done: bool = True,
    **params,
) -> dict:
    plan = plan_of(client, target, headers=headers, **params)
    submitted = submit_of(client, plan, headers=headers)
    run_to_completion()
    body = operation_of(client, submitted["id"], headers=headers)
    if expect_done:
        assert body["status"] == "done", body
    return body


def step_of(operation: dict, op: str) -> dict:
    return next(step for step in operation["steps"] if step["op"] == op)


def write_zip(path: Path, entries: Dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


def nasty_archive(storage: Path, name: str = "Nasty.zip") -> Path:
    return write_zip(
        storage / name,
        {
            "part.stl": STL_BYTES,
            "a/b/nested.stl": b"nested",
            **{junk: b"junk" for junk in JUNK},
            HIDDEN: b"hidden",
        },
    )


def asset_paths() -> set[str]:
    with Session(engine) as session:
        return {asset.storage_path for asset in session.exec(select(Asset)) if asset.storage_path}


# --- 1: the review is the deleted entries listing ---------------------------


def test_the_extract_plan_is_the_deleted_entries_listing(
    client: TestClient, storage: Path
) -> None:
    """The plan's `excluded` is the junk the deleted GET used to flag, with reasons.

    This is the re-point of `test_zip_entries_door_flags_the_door_archive` and
    `test_the_archive_entries_door_flags_the_door_archive`: the listing is no
    longer a route, it is the review a client plans with, and `members` is the
    number of files the register step will turn into rows. The extract step
    writes the caller's selection, so the hidden member is still written - the
    register step is what leaves it out.
    """
    nasty_archive(storage)

    plan = plan_of(client, "Nasty.zip")

    review = plan["review"]
    assert review["workflow"] == "extract"
    assert review["target"] == "Nasty.zip"
    assert review["members"] == 2, review
    excluded = {entry["path"]: entry["reason"] for entry in review["excluded"]}
    assert set(excluded) == {*JUNK, HIDDEN}, review["excluded"]
    assert "OS metadata" in excluded[JUNK[0]]
    assert "OS metadata" in excluded[JUNK[1]]
    assert "hidden" in excluded[HIDDEN]
    assert review["cover"]["rel_path"] == "Nasty/part.stl"
    assert review["cover"]["asset_id"] is None
    assert [step["op"] for step in plan["steps"]] == ["extract", "register"]


# --- 2: the run writes and registers ---------------------------------------


def test_the_extract_writes_the_twin_and_the_register_step_names_every_file(
    client: TestClient, storage: Path
) -> None:
    """The 200-equivalent: files and bytes on disk, and every planned path a row."""
    nasty_archive(storage)

    operation = run_extract(client, "Nasty.zip")

    extracted = step_of(operation, "extract")["result"]
    assert extracted["destination"] == "Nasty"
    # The SELECTION, hidden member included: the deleted door wrote it too.
    assert extracted["files"] == 3
    assert extracted["bytes"] == len(STL_BYTES) + len(b"nested") + len(b"hidden")
    assert extracted["already_present"] is False
    assert extracted["scan_started"] is False
    assert (storage / "Nasty" / HIDDEN).read_bytes() == b"hidden"

    register = step_of(operation, "register")["result"]
    # The archive keeps the row a scan would have left at its own path; it is not
    # a member, which is why `count` names only the two registerable files. The
    # hidden member is on disk and has no row: written is not registered.
    assert register["registered"] == ["Nasty/part.stl", "Nasty/a/b/nested.stl", "Nasty.zip"]
    assert register["count"] == 2
    assert asset_paths() == {"Nasty/part.stl", "Nasty/a/b/nested.stl", "Nasty.zip"}

    listed = client.get("/assets")
    assert listed.status_code == 200, listed.text
    assert {asset["storage_path"] for asset in listed.json()} == asset_paths()


# --- 3: a second extract is the idempotent answer ---------------------------


def test_a_second_extract_is_the_idempotent_answer(
    client: TestClient, storage: Path
) -> None:
    """`already_present`, and no second `archive.extract` row: the primitive wrote nothing."""
    nasty_archive(storage)
    with Session(engine) as session:
        before = len(
            list(session.exec(select(AuditEvent).where(AuditEvent.action == audit.ARCHIVE_EXTRACT)))
        )

    first = run_extract(client, "Nasty.zip")
    assert step_of(first, "extract")["result"]["files"] == 3

    second = run_extract(client, "Nasty.zip")
    repeated = step_of(second, "extract")["result"]
    assert repeated["already_present"] is True
    assert repeated["files"] == 0

    with Session(engine) as session:
        after = len(
            list(session.exec(select(AuditEvent).where(AuditEvent.action == audit.ARCHIVE_EXTRACT)))
        )
    assert after - before == 1, "an extract that wrote nothing must record nothing"


# --- 4: a write-time refusal is the step error ------------------------------


def test_a_partially_colliding_extract_refuses_as_a_failed_step_with_the_doors_sentence(
    client: TestClient, storage: Path
) -> None:
    """The plan cannot see occupancy; the write refuses with the door's own 409 sentence."""
    nasty_archive(storage)
    (storage / "Nasty").mkdir()
    (storage / "Nasty" / "part.stl").write_bytes(b"foreign")

    operation = run_extract(client, "Nasty.zip", expect_done=False)

    assert operation["status"] == "failed"
    extract = step_of(operation, "extract")
    assert extract["status"] == "failed"
    assert (
        "refusing to extract: these paths already exist - move or delete them first: "
        "Nasty/part.stl"
    ) in extract["error"]
    assert not (storage / "Nasty" / "a" / "b" / "nested.stl").exists()
    assert (storage / "Nasty" / "part.stl").read_bytes() == b"foreign"
    assert asset_paths() == set()


# --- 5: plan-time refusals keep the primitive's status and sentence ---------


def test_a_non_zip_or_missing_archive_is_refused_at_plan_time(
    client: TestClient, storage: Path
) -> None:
    """The plan opens the archive for its listing, so the primitive refuses there."""
    (storage / "notes.txt").write_text("not a zip", encoding="utf-8")

    status, detail = refused_plan(client, "notes.txt")
    assert status == 415, detail
    assert detail == "Library file is not a zip"

    status, detail = refused_plan(client, "missing.zip")
    assert status == 404, detail
    assert detail == "'missing.zip' is not a file in the library"


# --- 6: undo prunes only what the register step created ---------------------


def test_undo_removes_only_the_folders_the_register_step_created_and_keeps_the_files(
    client: TestClient, storage: Path
) -> None:
    """The bytes and asset rows stay, and no empty folder row the run made survives.

    MEASURED: a normal extract's register step creates a folder row per directory
    on the way to a planned file, and every such row holds either an asset or a
    child row - so `folders_removed` is 0 on the happy path. The leg's delete
    condition is still exercised as an invariant: whatever it reports removed is
    gone, and no created folder that is now empty was left behind. This is the
    false state it catches - an undo that un-extracts, or one that deletes a
    folder that still holds library content.
    """
    nasty_archive(storage)
    operation = run_extract(client, "Nasty.zip")
    register = step_of(operation, "register")["result"]
    created_folders = set(register["created_folders"])
    assert created_folders, "the register step created the twin's folder rows"
    before_assets = asset_paths()

    undone = client.post(f"/operations/{operation['id']}/undo")
    assert undone.status_code == 200, undone.text
    body = undone.json()
    assert body["undone"] is True
    assert body["payload_residue"] and "stay" in body["payload_residue"]

    with Session(engine) as session:
        remaining = {folder.id for folder in session.exec(select(Folder))}
    removed = created_folders - remaining
    assert body["folders_removed"] == len(removed)
    with Session(engine) as session:
        for folder_id in remaining & created_folders:
            children = session.exec(select(Folder.id).where(Folder.parent_id == folder_id)).first()
            members = session.exec(select(Asset.id).where(Asset.folder_id == folder_id)).first()
            assert children is not None or members is not None, (
                f"undo left an empty folder row it created: {folder_id}"
            )

    assert asset_paths() == before_assets, "an undo never un-extracts"
    assert (storage / "Nasty" / "part.stl").read_bytes() == STL_BYTES
    assert (storage / "Nasty" / "a" / "b" / "nested.stl").read_bytes() == b"nested"


# --- 7: the level is the workflow's ----------------------------------------


def test_a_contributor_extracts_and_a_viewer_is_refused(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONTRIBUTE on plan and submit; the viewer is refused on both requests."""
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    nasty_archive(storage)
    assert operations_service.WORKFLOW_LEVELS["extract"] == permissions.CONTRIBUTE
    glass = client.post("/login", json=BREAK_GLASS)
    assert glass.status_code == 200, glass.text
    admin_token = sign_in(client, glass.json()["token"], ADMIN_USERNAME, ADMIN_PASSWORD, "admin")
    contributor = sign_in(
        client, admin_token, CONTRIBUTOR_USERNAME, CONTRIBUTOR_PASSWORD, accounts.CONTRIBUTOR_ROLE
    )
    viewer = sign_in(client, admin_token, VIEWER_USERNAME, VIEWER_PASSWORD, "viewer")

    planned = client.post(
        "/operations/plan",
        json={"workflow": "extract", "target": "Nasty.zip", "params": {}},
        headers=bearer(viewer),
    )
    assert planned.status_code == 403, planned.text
    assert planned.json()["detail"] == permissions.DENY_DETAIL[permissions.CONTRIBUTE]

    plan = plan_of(client, "Nasty.zip", headers=bearer(contributor))
    refused = client.post(
        "/operations",
        json={"plan_id": plan["plan_id"], "idempotency_key": "viewer-extract"},
        headers=bearer(viewer),
    )
    assert refused.status_code == 403, refused.text
    submitted = submit_of(client, plan, headers=bearer(contributor))
    run_to_completion()
    assert operation_of(client, submitted["id"], headers=bearer(contributor))["status"] == "done"


# --- 8: the primitive's own audit row survives -----------------------------


def test_extract_emits_one_archive_extract_row_and_one_operation_submit_row(
    client: TestClient, storage: Path
) -> None:
    """The R12 pin: a step body that lost the primitive's audit row would pass a status check."""
    target = f"Audit-{uuid4().hex}.zip"
    nasty_archive(storage, target)
    request_id = f"extract-{uuid4().hex}"
    plan = plan_of(client, target)
    response = client.post(
        "/operations",
        json={"plan_id": plan["plan_id"], "idempotency_key": "extract-audit"},
        headers={"X-Request-ID": request_id},
    )
    assert response.status_code == 202, response.text
    run_to_completion()

    with Session(engine) as session:
        submit = list(
            session.exec(
                select(AuditEvent).where(
                    AuditEvent.action == audit.OPERATION_SUBMIT,
                    AuditEvent.request_id == request_id,
                )
            )
        )
        extract_rows = list(
            session.exec(
                select(AuditEvent).where(
                    AuditEvent.action == audit.ARCHIVE_EXTRACT,
                    AuditEvent.target == target,
                )
            )
        )
    assert len(submit) == 1, "the submit is the route's own row"
    assert len(extract_rows) == 1, "the primitive wrote the operator's extract row"
    row = extract_rows[0]
    assert row.request_id == "-" and row.method == "RUNNER"
    assert json.loads(row.detail_json) == {
        "destination": Path(target).stem,
        "files": 3,
        "bytes": len(STL_BYTES) + len(b"nested") + len(b"hidden"),
        "scan_started": False,
    }
