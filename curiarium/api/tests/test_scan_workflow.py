"""The library scan as one `library-scan` operation, and the two routes it lost.

`GET /library/scan` and `POST /library/scan` are gone (Mf-y2u3): a scan is now
planned, submitted and driven by the same in-process runner as every other
workflow. The primitive did not change - `scan_storage_root` and the new
`scan_subtree` still do the walking - but its transport did, so every test here
is written to fail if the operation is not the record:

* **a queued scan is readable while it runs** - the walk is parked mid-step and
  the row already answers `running` with no result, so the operator has
  something to poll before the walk exists.
* **the step result IS the old wire shape** - the deleted `LibraryScanStatusOut`
  field set, exactly, because the client still reads those nine counters.
* **a scope is a folder, not a second walker** - a scoped run registers only its
  own folder and leaves its siblings unregistered.
* **the review names what is skipped** - an operator confirming a blank page is
  told why it is blank (hidden names, app folders, debris, symlinks, non-regular
  files, and that a scan never deletes).
* **the slot is shared with the primitive** - a second scan fails with the
  deleted route's own sentence, and a refused step never publishes itself as the
  current run.
* **the whole root declares the empty path** - which the prefix-lock rule treats
  as overlapping everything.
* **the workflow filter is the server's** - the newest scan is one query.
* **a whole-root scan is rate-limited, a folder scan is not** - one walk per
  window is charged to the library, and the refusal is a 429 with `Retry-After`
  that names the blocking submitter; a scoped scan never meets it.
* **who may run it** - a contributor is accepted at CONTRIBUTE, a viewer is
  refused, and the two deleted routes are absent from every matrix, the OpenAPI
  document and the app itself.
"""

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

import accounts
import audit
import auth
import bundle_service
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

BREAK_GLASS = {"username": "admin", "password": "super-secret"}
ADMIN_USERNAME = "root"
ADMIN_PASSWORD = "root-password-1"
CONTRIBUTOR_USERNAME = "contributor"
CONTRIBUTOR_PASSWORD = "contributor-password-1"
EDITOR_USERNAME = "editor"
EDITOR_PASSWORD = "editor-password-1"
VIEWER_USERNAME = "viewer"
VIEWER_PASSWORD = "viewer-password-1"

SCAN_WAIT_SECONDS = 30.0


# --- harness -----------------------------------------------------------------


def truncate_everything() -> None:
    """An empty ledger, an empty library and no accounts: the test that reads made it."""
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


def point_storage(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Point every path-bound module at `root`."""
    monkeypatch.setattr(db, "STORAGE", root)
    monkeypatch.setattr(bundle_service, "STORAGE", root)
    monkeypatch.setattr(library_scan, "STORAGE", root)


@pytest.fixture()
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty temp storage root the scan owns for one test."""
    root = tmp_path / "storage"
    root.mkdir()
    point_storage(monkeypatch, root)
    return root


@pytest.fixture()
def client() -> TestClient:
    with TestClient(main.app) as test_client:
        yield test_client


def _write(root: Path, rel_path: str, payload: bytes = b"solid scan\n") -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


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


def plan_scan(client: TestClient, target: str = "") -> dict:
    response = client.post(
        "/operations/plan", json={"workflow": "library-scan", "target": target, "params": {}}
    )
    assert response.status_code == 200, response.text
    return response.json()


def submit_of(client: TestClient, plan: dict, key: str = "scan-once") -> dict:
    response = client.post(
        "/operations", json={"plan_id": plan["plan_id"], "idempotency_key": key}
    )
    assert response.status_code == 202, response.text
    return response.json()


def operation_of(client: TestClient, operation_id: str) -> dict:
    response = client.get(f"/operations/{operation_id}")
    assert response.status_code == 200, response.text
    return response.json()


def run_to_completion() -> None:
    """Dispatch one pass and join the pool; the suite's background runner is off."""
    operations_runner.dispatch_once()
    operations_runner.wait_idle(timeout=SCAN_WAIT_SECONDS)


def run_scan(client: TestClient, target: str = "", key: str = "scan-once") -> dict:
    """Plan, submit and run one scan operation; the caller reads the finished row."""
    submitted = submit_of(client, plan_scan(client, target), key=key)
    run_to_completion()
    return operation_of(client, submitted["id"])


def _wait_for_scan_status(read_status, done, *, message: str) -> dict:
    deadline = time.monotonic() + SCAN_WAIT_SECONDS
    while True:
        payload = read_status()
        if done(payload):
            return payload
        if time.monotonic() >= deadline:
            raise AssertionError(f"{message}: last status was {payload}")
        time.sleep(0.01)


def park_register_tree(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    """Hold the walk inside `_register_tree` so a submitted operation can be read mid-run."""
    entered = threading.Event()
    release = threading.Event()
    real = library_scan._register_tree

    def parked(session, plan, state, seen):
        entered.set()
        assert release.wait(timeout=SCAN_WAIT_SECONDS)
        return real(session, plan, state, seen)

    monkeypatch.setattr(library_scan, "_register_tree", parked)
    return entered, release


# --- 1: readable while it runs ------------------------------------------------


def test_a_queued_scan_operation_is_readable_while_it_runs(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submitted scan is a row before it is a walk, and the row answers `running`."""
    _write(storage, "parked/one.stl")
    entered, release = park_register_tree(monkeypatch)
    submitted = submit_of(client, plan_scan(client))
    operation_id = submitted["id"]

    worker = threading.Thread(target=run_to_completion, daemon=True)
    worker.start()
    try:
        assert entered.wait(timeout=SCAN_WAIT_SECONDS), "the walk never reached the register step"
        row = operation_of(client, operation_id)
        assert row["status"] == "running"
        assert row["steps"][0]["status"] == "running"
        assert row["steps"][0]["result"] is None
    finally:
        release.set()
        worker.join(timeout=SCAN_WAIT_SECONDS)

    row = operation_of(client, operation_id)
    assert row["status"] == "done", row
    assert row["steps"][0]["result"]["registered"] > 0


# --- 2: the counters are the deleted wire shape -------------------------------


def test_a_scan_operation_reports_the_primitive_counters(
    client: TestClient, storage: Path
) -> None:
    """The step result keys are EXACTLY `ScanState.as_dict()`, which was the route's shape."""
    _write(storage, "counters/one.stl")
    row = run_scan(client)
    result = row["steps"][0]["result"]

    assert set(result) == {
        "running",
        "started_at",
        "finished_at",
        "registered",
        "known",
        "ignored",
        "missing",
        "failed",
        "errors",
    }
    assert set(result) == set(library_scan.ScanState().as_dict())
    assert result["running"] is False
    assert result["finished_at"] is not None
    assert result["registered"] == 1


# --- 3: a scope is a folder ---------------------------------------------------


def test_a_scoped_scan_operation_walks_only_its_scope(
    client: TestClient, storage: Path
) -> None:
    """One folder planned means one folder walked: the sibling stays unregistered."""
    for index in range(3):
        _write(storage, f"scanone/part-{index}.stl")
    for index in range(3):
        _write(storage, f"scantwo/part-{index}.stl")

    row = run_scan(client, target="scanone")
    result = row["steps"][0]["result"]

    assert result["registered"] == 3
    assert row["target"] == "scanone"
    with Session(engine) as session:
        under_sibling = session.exec(
            select(Asset).where(Asset.storage_path.like("scantwo/%"))
        ).all()
        under_scope = session.exec(
            select(Asset).where(Asset.storage_path.like("scanone/%"))
        ).all()
    assert under_sibling == []
    assert len(under_scope) == 3


# --- 4: the review is prose, not a count --------------------------------------


def test_the_scan_review_names_the_skip_rules(client: TestClient, storage: Path) -> None:
    """The false state this catches: an operator confirming a blank page.

    A review that only said "0 members" would leave the operator unable to tell a
    working scan of an empty folder from a broken walk. Every rule the walk
    enforces must be named, including the one that says nothing is deleted.
    """
    _write(storage, "scanone/one.stl")
    plan = plan_scan(client, "scanone")
    reasons = " ".join(entry["reason"] for entry in plan["review"]["excluded"])

    assert "hidden names" in reasons
    assert "the app's own folders" in reasons
    assert "OS debris" in reasons
    assert "symlinks" in reasons
    assert "non-regular files" in reasons
    assert "never deletes" in reasons


# --- 5/6: the one slot --------------------------------------------------------


def _hold_slot_with_a_parked_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event]:
    """Start a real background scan and park it inside the walk."""
    real_walk = library_scan._walk_storage
    entered = threading.Event()
    release = threading.Event()

    def parked_walk(root, **kwargs):
        entered.set()
        assert release.wait(timeout=SCAN_WAIT_SECONDS)
        yield from real_walk(root, **kwargs)

    monkeypatch.setattr(library_scan, "_walk_storage", parked_walk)
    assert library_scan.start_scan() is True
    assert entered.wait(timeout=SCAN_WAIT_SECONDS), "the background scan never reached the walk"
    return entered, release


def _release_slot(release: threading.Event) -> None:
    release.set()
    _wait_for_scan_status(
        library_scan.scan_status,
        lambda status: not status["running"],
        message="the held scan never finished",
    )


def test_a_second_scan_step_fails_with_the_doors_own_sentence(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal is the deleted route's sentence, carried on the step row."""
    _write(storage, "refused/one.stl")
    _entered, release = _hold_slot_with_a_parked_walk(monkeypatch)
    try:
        row = run_scan(client, key="refused-scan")
        assert row["status"] == "failed", row
        assert row["steps"][0]["error"] == library_scan.ALREADY_RUNNING_DETAIL
        assert row["steps"][0]["error"] == "A library scan is already running"
    finally:
        _release_slot(release)


def test_a_scan_never_claims_a_slot_it_did_not_get(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused step must not publish itself as the current run."""
    _write(storage, "refused/one.stl")
    _entered, release = _hold_slot_with_a_parked_walk(monkeypatch)
    try:
        before = library_scan.scan_status()
        run_scan(client, key="refused-scan")
        after = library_scan.scan_status()
        assert after == before
    finally:
        _release_slot(release)


# --- 7: the root declares the empty path --------------------------------------


def test_a_whole_root_scan_declares_the_empty_path(
    client: TestClient, storage: Path
) -> None:
    """The plan persists `['']`, which `paths_overlap` treats as overlapping everything."""
    submitted = submit_of(client, plan_scan(client))
    with Session(engine) as session:
        operation = session.get(Operation, submitted["id"])
        stored = json.loads(operation.plan_json)

    assert stored["paths"] == [""]
    assert operations_service.paths_overlap("", "anything") is True
    assert operations_service.paths_overlap("", "") is True


# --- 8: the workflow filter ---------------------------------------------------


def test_get_operations_filters_by_workflow(client: TestClient, storage: Path) -> None:
    """One query answers the newest scan; an unknown workflow is a 400, not an empty page.

    The scan is scoped: this test is about the `workflow` filter, and two root
    plans would put the second behind the whole-root cooldown instead.
    """
    _write(storage, "counters/one.stl")
    plan = plan_scan(client, "counters")
    first = submit_of(client, plan, key="scan-a")
    time.sleep(0.002)
    second = submit_of(client, plan, key="scan-b")

    rows = client.get("/operations?workflow=library-scan")
    assert rows.status_code == 200, rows.text
    assert [row["workflow"] for row in rows.json()] == ["library-scan", "library-scan"]
    assert [row["id"] for row in rows.json()] == [second["id"], first["id"]]
    assert client.get("/operations?workflow=nope").status_code == 400


# --- 9: the whole-root cooldown ----------------------------------------------


def shift_clock(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
    """Move the service clock forward by `seconds` from the real clock, never by sleeping."""
    real = operations_service.now()

    def shifted() -> str:
        return (datetime.fromisoformat(real) + timedelta(seconds=seconds)).isoformat(
            timespec="microseconds"
        )

    monkeypatch.setattr(operations_service, "now", shifted)


def _settle_row(operation_id: str) -> None:
    """Mark a submitted root row done so the dispatcher cannot run it.

    The row still charges the library's scan budget - the cooldown reads rows, not
    the dispatcher - but letting the dispatcher claim it would block the scoped
    row behind `['']` and register the sibling, which is the assertion under test.
    """
    with Session(engine) as session:
        operation = session.get(Operation, operation_id)
        assert operation is not None
        operation.status = operations_service.STATUS_DONE
        operation.started_at = operations_service.now()
        operation.finished_at = operations_service.now()
        session.add(operation)
        session.commit()


def test_a_second_whole_root_scan_inside_the_cooldown_is_refused(
    client: TestClient, storage: Path
) -> None:
    """The false state: a second walk of the whole root inside the window.

    The refusal lands before a row exists, so the queue does not grow with walks
    the library will only serialise one at a time anyway.
    """
    _write(storage, "scanone/one.stl")
    submit_of(client, plan_scan(client))
    before = client.get("/operations?workflow=library-scan").json()

    refused = client.post(
        "/operations/plan", json={"workflow": "library-scan", "target": "", "params": {}}
    )

    assert refused.status_code == 429, refused.text
    assert int(refused.headers["retry-after"]) > 0
    detail = refused.json()["detail"]
    assert "one whole-library scan per 10 minutes" in detail
    assert "allowed in" in detail
    assert "A folder scan has no cooldown" in detail
    after = client.get("/operations?workflow=library-scan").json()
    assert [row["id"] for row in after] == [row["id"] for row in before]


def test_a_whole_root_scan_is_allowed_again_once_the_window_has_passed(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The false state: a cooldown that never releases, so a legitimate re-scan is dead."""
    _write(storage, "scanone/one.stl")
    submit_of(client, plan_scan(client))
    shift_clock(monkeypatch, operations_service.LIBRARY_SCAN_COOLDOWN_SECONDS + 1)

    plan = plan_scan(client)
    submitted = submit_of(client, plan, key="scan-again")

    assert submitted["workflow"] == "library-scan"


def test_a_scoped_scan_is_never_rate_limited(
    client: TestClient, storage: Path
) -> None:
    """The false state: the whole-root cooldown refusing a folder walk too.

    A folder is bounded by itself and serialised by its declared path, so it is
    the alternative the whole-root refusal names - not a second victim of it.
    """
    for index in range(3):
        _write(storage, f"scanone/part-{index}.stl")
    for index in range(3):
        _write(storage, f"scantwo/part-{index}.stl")
    root = submit_of(client, plan_scan(client), key="root-scan")
    _settle_row(root["id"])

    plan_scan(client, "scantwo")
    scoped = plan_scan(client, "scanone")

    submitted = submit_of(client, scoped, key="scoped-scan")
    run_to_completion()
    row = operation_of(client, submitted["id"])

    assert row["steps"][0]["result"]["registered"] == 3
    with Session(engine) as session:
        sibling = session.exec(
            select(Asset).where(Asset.storage_path.like("scantwo/%"))
        ).all()
        under_scope = session.exec(
            select(Asset).where(Asset.storage_path.like("scanone/%"))
        ).all()
    assert sibling == []
    assert len(under_scope) == 3


def test_a_scan_cancelled_before_it_started_costs_nothing(
    client: TestClient, storage: Path
) -> None:
    """The false state: a scan cancelled before it walked anything still spending budget."""
    _write(storage, "scanone/one.stl")
    submitted = submit_of(client, plan_scan(client))

    cancelled = client.post(f"/operations/{submitted['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    plan = plan_scan(client)
    assert plan["workflow"] == "library-scan"


def test_a_scan_plan_refuses_params_it_does_not_read(
    client: TestClient, storage: Path
) -> None:
    """The false state: an unread param folded into the reviewed digest, so the review lies."""
    refused = client.post(
        "/operations/plan",
        json={"workflow": "library-scan", "target": "", "params": {"files": ["a"]}},
    )

    assert refused.status_code == 400, refused.text
    assert "files" in refused.json()["detail"]


# --- 10: who may run it -------------------------------------------------------

def test_a_contributor_may_run_a_scan_now_that_it_is_contribute(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan is CONTRIBUTE, so a contributor may start one; a viewer may not.

    The level moved in Mf-gciy because the rubric's answer is that a scan removes
    nothing, and the whole-root walk is paid for with the planner's cooldown. The
    viewer's plan is scoped: a second root plan would be refused by that cooldown,
    and this test is about the role decision, not the rate limit.
    """
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    admin = client.post("/login", json=BREAK_GLASS)
    assert admin.status_code == 200, admin.text
    admin_token = sign_in(client, admin.json()["token"], ADMIN_USERNAME, ADMIN_PASSWORD, "admin")
    contributor = sign_in(
        client, admin_token, CONTRIBUTOR_USERNAME, CONTRIBUTOR_PASSWORD, "contributor"
    )
    viewer = sign_in(client, admin_token, VIEWER_USERNAME, VIEWER_PASSWORD, "viewer")

    assert operations_service.WORKFLOW_LEVELS["library-scan"] == permissions.CONTRIBUTE

    _write(storage, "viewerscan/one.stl")

    planned = client.post(
        "/operations/plan",
        json={"workflow": "library-scan", "target": "", "params": {}},
        headers=bearer(contributor),
    )
    assert planned.status_code == 200, planned.text

    accepted = client.post(
        "/operations",
        json={"plan_id": planned.json()["plan_id"], "idempotency_key": "contributor-scan"},
        headers=bearer(contributor),
    )
    assert accepted.status_code == 202, accepted.text

    scoped = client.post(
        "/operations/plan",
        json={"workflow": "library-scan", "target": "viewerscan", "params": {}},
        headers=bearer(contributor),
    )
    assert scoped.status_code == 200, scoped.text

    viewer_planned = client.post(
        "/operations/plan",
        json={"workflow": "library-scan", "target": "viewerscan", "params": {}},
        headers=bearer(viewer),
    )
    assert viewer_planned.status_code == 403, viewer_planned.text
    assert viewer_planned.json()["detail"] == permissions.DENY_DETAIL[permissions.CONTRIBUTE]

    refused = client.post(
        "/operations",
        json={"plan_id": scoped.json()["plan_id"], "idempotency_key": "viewer-scan"},
        headers=bearer(viewer),
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == permissions.DENY_DETAIL[permissions.CONTRIBUTE]

    for method, path in (("GET", "/library/scan"), ("POST", "/library/scan")):
        assert (method, path) not in permissions.ROUTE_PERMISSIONS
        assert (method, path) not in audit.ROUTE_ACTIONS
        assert path not in main.app.openapi()["paths"]
    assert client.get("/library/scan", headers=bearer(contributor)).status_code == 404
    assert client.post("/library/scan", headers=bearer(contributor)).status_code == 404
