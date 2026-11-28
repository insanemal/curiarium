"""The two ingest doors as operations: `import-link` and `import-zip` (Mf-y2u3 2/4).

`POST /import` and `POST /import/zip` are gone. The primitives did not change -
`import_service.import_asset_from_url` and `zip_service.extract_zip_entries_to_assets`
still do the work - but the transport did: a plan is the review the one-call
route never had, and the in-process runner executes exactly one step.

Every test here is written to fail if the capability is absent:

* **planning opens no socket** - the planner must not resolve DNS, because a
  fetching plan would download twice and make the digest network-dependent.
* **the paid/gated refusal survives** - at plan time with the door's own
  sentence, and again as a failed step when the resolved URL is the paid one.
* **the request is the client's, the steps are the server's** - `entries` is a
  list of strings or a 400, and a `params` key outside the import body is refused
  so nothing can ride in beside it.
* **attribution is the submitter's** - the row says `created_by=<submitter>` and
  `created_via=link-import`/`zip-import`, which is what `AssetOrigin.for_principal`
  used to read off the request.
* **the step result is the ledger** - exactly the ids and paths it created, and
  the entry names it could not write, because the undo and the operator's retry
  list are both read from it.
* **one bad member is one bad member** - a zip entry that fails alone leaves the
  others attached, where an all-or-nothing rewrite would lose them.
* **the door's sentences survive as step errors** - 415 for a non-zip, 400 for a
  corrupt one, neither with a status prefix and neither as a traceback.
* **undo removes what the run added, and only that** - the asset row and its
  bytes, the folder rows the mirrored tree created, and never a row that
  predated the plan.

Harness: the idioms `tests/test_extract_and_create.py` and
`tests/test_scan_workflow.py` established - one migrated database, a temp storage
root, the outbound fetch faked through the documented `main` patch points, auth
off unless a test is about *who* may start an import.
"""

import json
import socket
from pathlib import Path
from typing import Optional
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select
from test_asset_attribution import FakeImportResponse

import accounts
import asset_file_service
import asset_service
import audit
import auth
import db
import import_policy
import import_service
import library_scan
import login_throttle as login_throttle_module
import main
import operations_runner
import operations_service
import permissions
import url_utils
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

LINK_URL = "https://example.com/model.stl"
ZIP_URL = "https://example.com/models.zip"
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
    """An empty ledger, an empty library and no accounts.

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
    """Migrations, an empty world, auth off and no step guard, per test."""
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
    """Argon2's floor parameters: only the roles test signs accounts in."""
    monkeypatch.setattr(accounts, "hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))


@pytest.fixture()
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp storage root every path-bound module reads at call time."""
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


def plan_of(client: TestClient, workflow: str, target: str, *, headers: Optional[dict] = None, **params) -> dict:
    response = client.post(
        "/operations/plan",
        json={"workflow": workflow, "target": target, "params": params},
        headers=headers or {},
    )
    assert response.status_code == 200, response.text
    return response.json()


def refused_plan(
    client: TestClient, workflow: str, target: str, *, headers: Optional[dict] = None, **params
) -> tuple[int, str]:
    response = client.post(
        "/operations/plan",
        json={"workflow": workflow, "target": target, "params": params},
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
    """Dispatch one pass and join the pool; the suite's background runner is off."""
    operations_runner.dispatch_once()
    operations_runner.wait_idle(timeout=30)


def run_workflow(
    client: TestClient,
    workflow: str,
    target: str,
    *,
    headers: Optional[dict] = None,
    key: Optional[str] = None,
    **params,
) -> dict:
    """Plan, submit and run one import; the caller reads the finished row."""
    plan = plan_of(client, workflow, target, headers=headers, **params)
    submitted = submit_of(client, plan, key, headers=headers)
    run_to_completion()
    return operation_of(client, submitted["id"], headers=headers)


def step_of(operation: dict, op: str) -> dict:
    return next(step for step in operation["steps"] if step["op"] == op)


def install_link_import_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """`fetch`: the real persist path over a fake remote response, no socket."""

    def stub(url: str, body, *, origin=None):
        return import_service.persist_asset_from_response(FakeImportResponse(STL_BYTES), url, body, origin=origin)

    monkeypatch.setattr(main, "normalize_import_url", lambda url: url)
    monkeypatch.setattr(main, "import_asset_from_url", stub)


def write_zip(path: Path, entries: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


def install_zip_download(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    entries: Optional[dict] = None,
    *,
    payload: Optional[bytes] = None,
    filename: str = "models.zip",
) -> None:
    """`fetch-zip`: a fresh archive on every call, because the step unlinks its temp file."""

    def stub(url: str, body):
        if payload is not None:
            path.write_bytes(payload)
        else:
            write_zip(path, entries or {})
        return path, filename, "application/zip"

    monkeypatch.setattr(main, "normalize_import_url", lambda url: url)
    monkeypatch.setattr(main, "download_import_to_temp", stub)


# --- reading the world -------------------------------------------------------


def assets_of() -> list[Asset]:
    with Session(engine) as session:
        return list(session.exec(select(Asset)))


def folders_of() -> dict[str, Folder]:
    with Session(engine) as session:
        return {folder.id: folder for folder in session.exec(select(Folder))}


def folder_names() -> set[str]:
    return {folder.name for folder in folders_of().values()}


def audit_rows(action: str, target: str) -> list[AuditEvent]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(AuditEvent).where(AuditEvent.action == action, AuditEvent.target == target)
            )
        )


def undo_of(client: TestClient, operation_id: str) -> dict:
    response = client.post(f"/operations/{operation_id}/undo")
    assert response.status_code == 200, response.text
    return response.json()


# The whole `OperationUndoOut` wire shape. Every leg answers all of these keys -
# explicitly null where it has nothing to say - so a body that removed everything
# and told the client only `undone: True` reds here instead of passing.
UNDO_WIRE_KEYS = {
    "operation_id",
    "undone",
    "project_id",
    "project_name",
    "moves",
    "projects_removed",
    "members_removed",
    "asset_id",
    "filename",
    "assets_removed",
    "folders_removed",
    "entries_failed",
    "project_removed",
    "payload_residue",
}


# --- 1: planning opens no socket ---------------------------------------------


@pytest.mark.parametrize(
    ("workflow", "params"),
    [("import-link", {}), ("import-zip", {"entries": ["part.stl"]})],
)
def test_planning_an_import_opens_no_socket(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch, workflow: str, params: dict
) -> None:
    """The false state this catches: a planner that resolved the URL and fetched it.

    A fetching planner would download the file twice - once to grade the review,
    once to write the asset - and the plan digest would depend on the network
    rather than on the request, so two reviews of one URL could disagree.
    """

    def refuse(*args, **kwargs):
        raise AssertionError("planning an import must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(import_service, "safe_urlopen", refuse)
    monkeypatch.setattr(url_utils, "resolve_remote_url", refuse)

    plan = plan_of(client, workflow, LINK_URL, **params)

    assert plan["target"] == LINK_URL
    assert plan["steps"][0]["op"] == ("fetch" if workflow == "import-link" else "fetch-zip")


# --- 2: the paid/gated refusal, twice ----------------------------------------


def test_a_paid_url_is_refused_at_plan_time_and_again_as_a_failed_step(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The door's own sentence at both moments, and no connection at either."""

    def refuse(*args, **kwargs):
        raise AssertionError("a paid import must not open a socket")

    monkeypatch.setattr(import_service, "safe_urlopen", refuse)
    monkeypatch.setattr(url_utils, "resolve_remote_url", refuse)

    status, detail = refused_plan(
        client,
        "import-link",
        "https://makerworld.com/en/cart",
        makerworld_cookie="mw_session=SECRET",
    )
    assert status == 403
    assert detail == import_policy.PAID_IMPORT_DETAIL
    assert "SECRET" not in detail

    # The run-time half: the plan's URL passes the table, but the URL the fetch
    # actually resolves to is the paid one - the hop is only visible after it is
    # resolved, and the resolver refuses it before any byte is requested.
    monkeypatch.setattr(main, "normalize_import_url", lambda url: "https://makerworld.com/en/checkout")
    row = run_workflow(client, "import-link", "https://example.com/model.stl")

    assert row["status"] == "failed", row
    assert row["steps"][0]["error"] == import_policy.PAID_IMPORT_DETAIL


# --- 3: the client controls params, and only the import body -----------------


def test_params_entries_must_be_a_list_of_strings(client: TestClient, storage: Path) -> None:
    """A `str`, a mixed list and a number are all 400; a list of names is a plan."""
    for entries in ("part.stl", ["part.stl", 3], 3, {"part.stl": True}, None):
        status, detail = refused_plan(client, "import-zip", ZIP_URL, entries=entries)
        assert status == 400, entries
        assert detail == "entries must be a list of zip entry paths"

    plan = plan_of(client, "import-zip", ZIP_URL, entries=["part.stl", "a/b/nested.stl"])
    assert plan["review"]["creates"] == 2
    assert plan["review"]["members"] == 2


def test_a_params_key_outside_the_import_body_is_refused(client: TestClient, storage: Path) -> None:
    """The false state: a client smuggling a step list in beside the request."""
    status, detail = refused_plan(client, "import-link", LINK_URL, steps=[{"op": "fetch"}])

    assert status == 400
    assert "unknown import parameter(s) steps" in detail
    assert "title" in detail, "the refusal names what an import does accept"


# --- 4: attribution is the submitter's ---------------------------------------


def test_the_link_import_creates_one_asset_attributed_to_the_submitter(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_link_import_stub(monkeypatch)
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    admin = client.post("/login", json=BREAK_GLASS)
    assert admin.status_code == 200, admin.text
    token = sign_in(client, admin.json()["token"], CONTRIBUTOR_USERNAME, CONTRIBUTOR_PASSWORD, "contributor")

    plan = plan_of(client, "import-link", LINK_URL, headers=bearer(token))
    submitted = submit_of(client, plan, "link-attribution", headers=bearer(token))
    run_to_completion()
    row = operation_of(client, submitted["id"], headers=bearer(token))

    assert row["status"] == "done", row
    assert row["submitted_by"] == CONTRIBUTOR_USERNAME
    result = step_of(row, "fetch")["result"]
    assert set(result) == {"created", "asset_id", "path", "filename", "size", "url"}
    assert result["created"] is True
    assert result["filename"] == "model.stl"
    assert result["size"] == len(STL_BYTES)

    listed = client.get("/assets", headers=bearer(token)).json()
    stored = next(asset for asset in listed if asset["id"] == result["asset_id"])
    assert (stored["created_by"], stored["created_via"]) == (CONTRIBUTOR_USERNAME, "link-import")


# --- 4b: the session cookie never comes to rest in the ledger ---------------


@pytest.mark.parametrize(
    ("cookie_name", "secret"),
    [
        ("thingiverse_cookie", "tv-session-SECRET"),
        ("makerworld_cookie", "mw-session-SECRET"),
    ],
)
def test_the_site_cookie_is_scrubbed_from_the_persisted_plan(
    client: TestClient,
    storage: Path,
    monkeypatch: pytest.MonkeyPatch,
    cookie_name: str,
    secret: str,
) -> None:
    """The credential rides in the plan until the step takes it, and no longer.

    A plan IS the import request, so the cookie is in the step's args - and the
    ledger persists those args verbatim. The deleted door kept the value in memory
    only, so the false state this catches is a plan-shaped import that quietly
    parked a live site session in `operation_step.args_json` and `plan_json` for
    anyone who can read a row back. The other half is that the scrub must not cost
    the import its credential: the stub records the cookie the fetch really got.
    """
    received: list = []

    def stub(url: str, body, *, origin=None):
        received.append(getattr(body, cookie_name))
        return import_service.persist_asset_from_response(
            FakeImportResponse(STL_BYTES), url, body, origin=origin
        )

    monkeypatch.setattr(main, "normalize_import_url", lambda url: url)
    monkeypatch.setattr(main, "import_asset_from_url", stub)

    planned = client.post(
        "/operations/plan",
        json={"workflow": "import-link", "target": LINK_URL, "params": {cookie_name: secret}},
    )
    assert planned.status_code == 200, planned.text
    assert secret not in planned.text, "the review is not a place to echo a cookie"

    submitted = submit_of(client, planned.json(), f"cookie-{cookie_name}")
    run_to_completion()
    assert operation_of(client, submitted["id"])["status"] == "done"

    with Session(engine) as session:
        step = session.exec(
            select(OperationStep).where(OperationStep.operation_id == submitted["id"])
        ).one()
        operation = session.get(Operation, submitted["id"])
        step_args_json = step.args_json
        plan_json = operation.plan_json

    assert json.loads(step_args_json)["request"][cookie_name] == "-"
    assert "SECRET" not in step_args_json, step_args_json
    assert "SECRET" not in plan_json, "the persisted plan holds the intent's params too"
    assert received == [secret], "the scrub redacts the ledger, never the request"


# --- 5/6: the zip ledger, and per-entry isolation ----------------------------


def test_the_zip_import_reports_every_entry_it_created_and_every_one_it_could_not(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`created` names the two writable members, `failed` the junk one, and the rows exist."""
    entries = {
        "part.stl": STL_BYTES,
        "a/b/nested.stl": STL_BYTES,
        "__MACOSX/extra.stl": b"junk",
    }
    install_zip_download(monkeypatch, tmp_path / "models.zip", entries)

    row = run_workflow(client, "import-zip", ZIP_URL, entries=sorted(entries))

    assert row["status"] == "done", row
    result = step_of(row, "fetch-zip")["result"]
    assert result["failed"] == ["__MACOSX/extra.stl"]
    stored = {asset.id: asset for asset in assets_of()}
    assert set(result["created"]) == set(stored)
    assert sorted(stored[asset_id].filename for asset_id in result["created"]) == [
        "nested.stl",
        "part.stl",
    ]
    assert all(stored[asset_id].created_via == "zip-import" for asset_id in result["created"])


def test_a_failed_entry_leaves_the_other_entries_attached(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The false state: an all-or-nothing rewrite, where one bad member loses the rest.

    `LICENSE` has no extension, so `filename_for_model_name` refuses it as one
    failed entry; `part.stl` must still land, with bytes and a row.
    """
    entries = {"LICENSE": b"CC0\n", "part.stl": STL_BYTES}
    install_zip_download(monkeypatch, tmp_path / "models.zip", entries)

    row = run_workflow(client, "import-zip", ZIP_URL, entries=["LICENSE", "part.stl"])

    assert row["status"] == "done", row
    result = step_of(row, "fetch-zip")["result"]
    assert result["failed"] == ["LICENSE"]
    assert len(result["created"]) == 1
    survivor = assets_of()[0]
    assert survivor.id == result["created"][0]
    assert survivor.filename == "part.stl"
    assert survivor.size == len(STL_BYTES)
    assert (Path(storage) / (survivor.storage_path or "")).is_file()


# --- 7/8: the door's sentences survive as step errors ------------------------


def test_a_non_zip_url_planned_as_import_zip_fails_with_the_415_sentence(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The check is the FILENAME's suffix, and the sentence is the door's, prefix-free."""
    install_zip_download(monkeypatch, tmp_path / "models.txt", payload=b"not a zip", filename="models.txt")

    row = run_workflow(client, "import-zip", ZIP_URL, entries=["part.stl"])

    assert row["status"] == "failed", row
    assert row["steps"][0]["error"] == "Imported file is not a zip"
    assert assets_of() == []


def test_a_corrupt_zip_is_the_400_sentence_as_a_failed_step(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_zip_download(monkeypatch, tmp_path / "models.zip", payload=b"PK\x03\x04 truncated")

    row = run_workflow(client, "import-zip", ZIP_URL, entries=["part.stl"])

    assert row["status"] == "failed", row
    assert row["steps"][0]["error"] == "Invalid zip file"
    assert assets_of() == []


# --- 9: the step result is the undo's input ----------------------------------


def test_the_fetch_step_reports_exactly_what_it_created(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The link result names one asset; the zip result names ids, paths and its folders."""
    install_link_import_stub(monkeypatch)
    link = step_of(run_workflow(client, "import-link", LINK_URL), "fetch")["result"]
    assert set(link) == {"created", "asset_id", "path", "filename", "size", "url"}
    assert link["path"] == assets_of()[0].storage_path

    entries = {"part.stl": STL_BYTES, "a/b/nested.stl": STL_BYTES}
    install_zip_download(monkeypatch, tmp_path / "models.zip", entries)
    row = run_workflow(client, "import-zip", ZIP_URL, entries=sorted(entries))
    result = step_of(row, "fetch-zip")["result"]

    assert set(result) == {"created", "assets", "failed", "filename", "created_folders"}
    assert result["failed"] == []
    assert result["filename"] == "models.zip"
    assert {entry["asset_id"] for entry in result["assets"]} == set(result["created"])
    assert all(set(entry) == {"asset_id", "path", "filename", "size"} for entry in result["assets"])
    folders = folders_of()
    assert {folders[folder_id].name for folder_id in result["created_folders"]} == {"a", "b"}


# --- 10: the link undo -------------------------------------------------------


def test_undo_a_link_import_deletes_only_the_row_it_created(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The imported row and its bytes go; an asset the run did not make survives.

    And the wire says WHICH asset went: the false state is a leg that retired the
    asset correctly and answered the client `{operation_id, undone}`, leaving the
    operator to read the ledger by hand to learn what it removed.
    """
    install_link_import_stub(monkeypatch)
    upload = client.post("/upload", files={"file": ("seeded.stl", STL_BYTES, "model/stl")})
    assert upload.status_code == 200, upload.text
    seeded_id = upload.json()["id"]

    row = run_workflow(client, "import-link", LINK_URL)
    assert row["status"] == "done", row
    result = step_of(row, "fetch")["result"]
    imported = Path(storage) / (result["path"] or "")
    assert imported.is_file()

    body = undo_of(client, row["id"])

    assert set(body) == UNDO_WIRE_KEYS
    assert body["operation_id"] == row["id"]
    assert body["undone"] is True
    assert body["asset_id"] == result["asset_id"]
    assert body["filename"] == "model.stl"
    assert body["assets_removed"] == 1
    # Nothing to say: the link leg removes rows, not folders, and ran no archive.
    assert body["folders_removed"] is None
    assert body["entries_failed"] is None
    assert {asset.id for asset in assets_of()} == {seeded_id}
    assert not imported.exists(), "the undo removes the bytes as well as the row"
    events = audit_rows(audit.OPERATION_UNDO, row["id"])
    assert len(events) == 1
    assert json.loads(events[0].detail_json) == {
        "asset_id": result["asset_id"],
        "filename": "model.stl",
    }

    again = client.post(f"/operations/{row['id']}/undo")
    assert again.status_code == 409, again.text
    assert again.json()["detail"] == "the operation has already been undone"


# --- 11: the zip undo --------------------------------------------------------


def test_undo_a_zip_import_removes_its_assets_and_its_folders(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The mirrored rows go, and a folder the operator already had keeps its row.

    The wire answers the counts, not just `undone: True`: how many asset rows and
    how many mirrored folder rows this act removed, which is the only thing the
    operator can check the library against afterwards.
    """
    created = client.post("/folders", json={"name": "b"})
    assert created.status_code == 200, created.text
    pre_existing = created.json()["id"]

    entries = {"part.stl": STL_BYTES, "a/b/nested.stl": STL_BYTES}
    install_zip_download(monkeypatch, tmp_path / "models.zip", entries)
    row = run_workflow(client, "import-zip", ZIP_URL, entries=sorted(entries))
    assert row["status"] == "done", row
    result = step_of(row, "fetch-zip")["result"]
    assert len(result["created_folders"]) == 2, "a and a/b must be this run's own rows"
    paths = [Path(storage) / (entry["path"] or "") for entry in result["assets"]]
    assert all(path.is_file() for path in paths)

    body = undo_of(client, row["id"])

    assert set(body) == UNDO_WIRE_KEYS
    assert body["operation_id"] == row["id"]
    assert body["undone"] is True
    assert body["assets_removed"] == len(result["created"]) == 2
    assert body["folders_removed"] == len(result["created_folders"]) == 2
    assert body["entries_failed"] == [], "a run whose every entry landed says so"
    # Nothing to say: this leg retires a set, not one named asset.
    assert body["asset_id"] is None
    assert body["filename"] is None
    events = audit_rows(audit.OPERATION_UNDO, row["id"])
    assert len(events) == 1
    assert json.loads(events[0].detail_json) == {
        "assets_removed": result["created"],
        "folders_removed": result["created_folders"],
        "failed": [],
    }
    assert assets_of() == []
    assert all(not path.exists() for path in paths), "the bytes go with the rows"
    assert set(folders_of()) == {pre_existing}, "only the rows this run created are deleted"


def test_undo_a_zip_import_echoes_the_entries_the_run_could_not_write(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The operator's retry list survives the undo, on the wire.

    The false state: a run that failed `LICENSE` and wrote `part.stl` is undone, and
    the response says only `undone: True` - so the entries the operator meant to
    retry are gone with the operation the undo retired.
    """
    entries = {"LICENSE": b"CC0\n", "part.stl": STL_BYTES}
    install_zip_download(monkeypatch, tmp_path / "models.zip", entries)
    row = run_workflow(client, "import-zip", ZIP_URL, entries=["LICENSE", "part.stl"])
    assert row["status"] == "done", row
    assert step_of(row, "fetch-zip")["result"]["failed"] == ["LICENSE"]

    body = undo_of(client, row["id"])

    assert body["undone"] is True
    assert body["assets_removed"] == 1
    assert body["entries_failed"] == ["LICENSE"]
    assert assets_of() == []


# --- 12: who may run it ------------------------------------------------------


def test_a_contributor_runs_an_import_and_a_viewer_is_refused_on_plan_and_submit(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The level is CONTRIBUTE: a viewer cannot even reach the plan."""
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    glass = client.post("/login", json=BREAK_GLASS)
    assert glass.status_code == 200, glass.text
    sign_in(client, glass.json()["token"], ADMIN_USERNAME, ADMIN_PASSWORD, "admin")
    admin_session = client.post("/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    assert admin_session.status_code == 200, admin_session.text
    admin_token = admin_session.json()["token"]
    contributor = sign_in(client, admin_token, CONTRIBUTOR_USERNAME, CONTRIBUTOR_PASSWORD, "contributor")
    viewer = sign_in(client, admin_token, VIEWER_USERNAME, VIEWER_PASSWORD, "viewer")

    for workflow, params in (("import-link", {}), ("import-zip", {"entries": ["part.stl"]})):
        assert operations_service.WORKFLOW_LEVELS[workflow] == permissions.CONTRIBUTE

        planned = client.post(
            "/operations/plan",
            json={"workflow": workflow, "target": ZIP_URL, "params": params},
            headers=bearer(contributor),
        )
        assert planned.status_code == 200, planned.text
        plan_id = planned.json()["plan_id"]

        refused_plan_response = client.post(
            "/operations/plan",
            json={"workflow": workflow, "target": ZIP_URL, "params": params},
            headers=bearer(viewer),
        )
        assert refused_plan_response.status_code == 403, refused_plan_response.text
        assert refused_plan_response.json()["detail"] == permissions.DENY_DETAIL[permissions.CONTRIBUTE]

        refused_submit = client.post(
            "/operations",
            json={"plan_id": plan_id, "idempotency_key": f"viewer-{workflow}"},
            headers=bearer(viewer),
        )
        assert refused_submit.status_code == 403, refused_submit.text
        assert refused_submit.json()["detail"] == permissions.DENY_DETAIL[permissions.CONTRIBUTE]

        accepted = client.post(
            "/operations",
            json={"plan_id": plan_id, "idempotency_key": f"contributor-{workflow}"},
            headers=bearer(contributor),
        )
        assert accepted.status_code == 202, accepted.text


# --- 13: the stale plan guard ------------------------------------------------


def test_the_stale_plan_guard_still_holds(client: TestClient, storage: Path) -> None:
    """A plan whose review moved is refused; the same key still replays its row.

    The destination folder is renamed between plan and submit, so the step summary
    the plan carried no longer describes the library the submit recomputes - the
    409 the deleted door never had, because it had no plan.
    """
    created = client.post("/folders", json={"name": "dest"})
    assert created.status_code == 200, created.text
    folder_id = created.json()["id"]

    plan = plan_of(client, "import-link", LINK_URL, folder_id=folder_id)
    first = submit_of(client, plan, "stale-a")

    renamed = client.patch(f"/folder/{folder_id}", json={"name": "dest-renamed"})
    assert renamed.status_code == 200, renamed.text

    stale = client.post(
        "/operations",
        json={"plan_id": plan["plan_id"], "idempotency_key": "stale-b"},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"] == operations_service.STALE_PLAN_MESSAGE

    replay = client.post(
        "/operations",
        json={"plan_id": plan["plan_id"], "idempotency_key": "stale-a"},
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == first["id"]
    with Session(engine) as session:
        assert len(session.exec(select(Operation)).all()) == 1


def test_a_plan_review_names_the_import_prohibitions(client: TestClient, storage: Path) -> None:
    """The review is the only page an operator sees before the fetch."""
    link = plan_of(client, "import-link", LINK_URL)
    reasons = " ".join(entry["reason"] for entry in link["review"]["excluded"])
    assert link["review"]["creates"] == 1
    assert link["review"]["members"] == 0
    assert "fetched at RUN time" in reasons
    assert "paid or account-gated" in reasons
    assert "IMPORT_MAX_BYTES" in reasons
    assert "credentialed host" in reasons

    zip_plan = plan_of(client, "import-zip", ZIP_URL, entries=["a.stl", "b.stl"])
    zip_reasons = " ".join(entry["reason"] for entry in zip_plan["review"]["excluded"])
    assert "IMPORT_ZIP_MAX_BYTES" in zip_reasons
    assert "failed[]" in zip_reasons
    assert "does not fail the others" in zip_reasons
    assert "/import/zip/entries" in zip_reasons
    assert len(link["steps"]) == 1


def test_a_stale_folder_is_refused_at_plan_time(client: TestClient, storage: Path) -> None:
    """A plan refuses a folder that is gone, with the door's own sentence and a 409."""
    status, detail = refused_plan(client, "import-link", LINK_URL, folder_id="no-such-folder")

    assert status == 409
    assert detail == "Parent folder not found"


def test_a_root_import_declares_the_empty_path(client: TestClient, storage: Path) -> None:
    """A root import can write anywhere, so its prefix lock serialises honestly."""
    plan = plan_of(client, "import-link", LINK_URL)
    submitted = submit_of(client, plan, "root-declares-empty")
    with Session(engine) as session:
        stored = json.loads(session.get(Operation, submitted["id"]).plan_json)

    assert stored["paths"] == [""]
    assert operations_service.paths_overlap("", "anything") is True
