"""The operations core (Mf-5t30): plan/review, queue, locks, resume, cancel, undo.

Why this file exists: the substrate below every future workflow is only worth
anything if its hard promises are measured, not asserted. So each demonstration
below is written so that a test which would ALSO pass with the feature absent is
not accepted:

* **plan/review** - the steps, the counts and every `excluded[]` reason are read
  off the wire for one real row; a response that dropped `excluded` fails.
* **idempotency, both ways** - the same key with the same plan answers the same
  operation id AND leaves exactly one row and one audit row (a fresh row per
  call would fail the row count); the same key with a different plan answers 409
  whose detail names both digests (a 202 would fail). A replay is answered from
  the key first, so it still answers the same operation after the operation
  itself moved the review the digest was taken over.
* **lock contention, both ways** - two operations declaring one path: one
  dispatch pass promotes exactly one (2 would fail) and the other holds NO lock
  row; two operations on disjoint paths both reach a `threading.Barrier(2)`
  inside their first step, which a serialised pair could never do (it would time
  out and fail both operations).
* **resume** - step 0's `result_json` is byte-identical across a fresh lifespan
  (a re-run rewrites it as `already_created`), and a step caught `running` is
  failed naming the step and its remaining count rather than re-run.
* **cancel between steps** - step 1 is blocked on an Event, so the cancel can
  only be honoured between steps: the running step completes, step 2 is
  `skipped`, the operation is `cancelled`.
* **undo then supersede** - the project row and its memberships go, the member
  files stay on disk, the asset rows are untouched, one `operation.undo` row is
  written; then a later operation over the same paths makes that undo a 409 that
  names the superseding id.
* **retention** - 205 old + 3 young terminal rows and 1 queued row: exactly 5 go,
  the queued row survives, and the pruned operation's audit row still answers
  `GET /audit?target=<id>`.

Auth is disabled in this file (monkeypatched, the same mode `test_permissions_matrix`
sweeps) because none of these demonstrations is about *who* may call the route -
the eleven role sweeps own that, parametrised from the matrix. What is proven
here about permissions is that the six routes are classified and declared, that
the workflow->level table is the owner's rubric (Mf-215n: contribute unless the
action can REMOVE data), and that a caller cannot smuggle a step list in.
"""

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

import audit
import auth
import db
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
    Operation,
    OperationLock,
    OperationStep,
    Project,
    ProjectAsset,
)

WIDGET = "Widget"
GADGET = "Gadget"

# One tree with two groups (`""` for the row's own files and `docs/`), one image
# so the cover rule has a real face to pick, and one nested directory.
TREE = {
    f"{WIDGET}/part.stl": b"solid part\nendsolid part\n",
    f"{WIDGET}/other.stl": b"solid other\nendsolid other\n",
    f"{WIDGET}/render.png": b"\x89PNG\r\n\x1a\n" + bytes(96),
    f"{WIDGET}/docs/manual.pdf": b"%PDF-1.4\n% fake\n",
}
SECOND_TREE = {
    f"{GADGET}/frame.stl": b"solid frame\nendsolid frame\n",
}


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="microseconds")


# --- harness -----------------------------------------------------------------


def truncate_operations() -> None:
    """An empty ledger and an empty library: the test that reads a row made it.

    The database is one file for the whole run, so a leftover asset would collide
    on the unique `storage_path` index and a leftover project would 409 the next
    seed's name.
    """
    with Session(engine) as session:
        session.exec(delete(OperationStep))
        session.exec(delete(OperationLock))
        session.exec(delete(Operation))
        session.exec(delete(ProjectAsset))
        session.exec(delete(Project))
        session.exec(delete(AssetFile))
        session.exec(delete(Asset))
        session.commit()


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch):
    """Migrations, an empty ledger, a temp storage root and auth off, per test.

    Every row is created by the test that reads it: the database is one file for
    the whole run, so a leftover operation would turn the next submit's 202 into
    a 409 (the idempotency key is unique) and a leftover project would 409 the
    next seed's name.
    """
    run_migrations(engine)
    truncate_operations()
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    operations_runner.set_step_guard(None)
    yield
    operations_runner.set_step_guard(None)
    operations_runner.wait_idle(timeout=15)
    truncate_operations()


@pytest.fixture()
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp storage root holding the real files the plan's rows name."""
    root = tmp_path / "storage"
    for relative, body in {**TREE, **SECOND_TREE}.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    for module in (db,):
        monkeypatch.setattr(module, "STORAGE", root)
    return root


def seed_tree(root: Path, key: str = WIDGET) -> list[str]:
    """Register one directory tree's files as `asset` rows; returns the ids."""
    ids: list[str] = []
    with Session(engine) as session:
        for relative in sorted(TREE if key == WIDGET else SECOND_TREE):
            path = root / relative
            asset = Asset(
                id=str(uuid4()),
                name=path.stem,
                filename=path.name,
                mime="model/stl" if path.suffix == ".stl" else "application/octet-stream",
                size=len(path.read_bytes()),
                storage_path=relative,
            )
            session.add(asset)
            ids.append(asset.id)
        session.commit()
    return ids


@pytest.fixture()
def client() -> TestClient:
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture()
def seeded(storage: Path) -> list[str]:
    return seed_tree(storage, WIDGET)


def plan_of(client: TestClient, target: str = WIDGET, **extra) -> dict:
    body = {"workflow": "create-project", "target": target, **extra}
    response = client.post("/operations/plan", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def submit_of(client: TestClient, plan: dict, key: Optional[str] = None) -> dict:
    response = client.post(
        "/operations", json={"plan_id": plan["plan_id"], "idempotency_key": key or uuid4().hex}
    )
    assert response.status_code == 202, response.text
    return response.json()


def operation_of(client: TestClient, operation_id: str) -> dict:
    response = client.get(f"/operations/{operation_id}")
    assert response.status_code == 200, response.text
    return response.json()


def run_to_completion() -> None:
    """Dispatch and wait; the runner is a thread pool, so waiting is explicit."""
    operations_runner.dispatch_once()
    operations_runner.wait_idle(timeout=15)


def poll(client: TestClient, operation_id: str, wanted: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = operation_of(client, operation_id)
        if body["status"] == wanted:
            return body
        time.sleep(0.02)
    raise AssertionError(f"operation {operation_id} never reached {wanted}: {operation_of(client, operation_id)}")


def audit_rows(action: str, target: str = "") -> list[AuditEvent]:
    with Session(engine) as session:
        statement = select(AuditEvent).where(AuditEvent.action == action)
        if target:
            statement = statement.where(AuditEvent.target == target)
        return list(session.exec(statement.order_by(AuditEvent.created_at)))


def block_at(step_index: int, reached: threading.Event, release: threading.Event):
    """A step guard that reports it arrived and then blocks until released."""

    def guard(_operation, step) -> None:
        if step.step_index == step_index:
            reached.set()
            assert release.wait(timeout=10), "the test never released the blocked step"

    return guard


# --- plan and review ---------------------------------------------------------


def test_a_plan_names_its_steps_its_counts_and_its_exclusions(client: TestClient, seeded: list[str]) -> None:
    plan = plan_of(client)

    assert [step["op"] for step in plan["steps"]] == [
        "create-project",
        "verify-members",
        "verify-cover",
    ]
    assert [step["index"] for step in plan["steps"]] == [0, 1, 2]
    # `args` stay server-side: a plan that leaked them would let a client build
    # its own step list out of the review it was shown.
    assert all("args" not in step for step in plan["steps"])

    review = plan["review"]
    assert review["workflow"] == "create-project"
    assert review["target"] == WIDGET
    assert review["moves"] == 0
    assert review["deletes"] == 0
    assert review["creates"] == 1
    assert review["adopts"] == 4
    assert review["members"] == 4
    assert review["excluded"] == []
    assert review["existing_project_id"] is None
    # Two groups: the row's own files and the `docs/` directory.
    assert sorted(group["name"] for group in review["groups"]) == ["", "docs"]
    own = next(group for group in review["groups"] if group["name"] == "")
    assert own["members"] == 3
    assert own["models"] == 2
    # The image wins the cover over the models, and the reason says so.
    assert review["cover"]["reason"] == "largest-image"
    assert review["cover"]["rel_path"] == f"{WIDGET}/render.png"


def test_a_create_project_review_carries_empty_move_and_project_lists(
    client: TestClient, seeded: list[str]
) -> None:
    """The two additive review keys a split fills, answered empty by a create.

    A client may read `review.move_list` and `review.projects` unconditionally, so
    a create-project review must answer two empty lists rather than omit the keys.
    Mutation: drop the defaults from `OperationReviewOut` and a reader of either
    key breaks on the workflow that makes neither.
    """
    review = plan_of(client)["review"]

    assert review["move_list"] == []
    assert review["projects"] == []
    # And the create's own two lists are still populated: the split's keys are
    # additive, not a replacement for `groups`/`cover`.
    assert review["groups"]
    assert review["cover"] is not None


def test_a_plan_excludes_a_child_directory_that_is_already_its_own_project(
    client: TestClient, seeded: list[str]
) -> None:
    with Session(engine) as session:
        session.add(
            Project(
                id=str(uuid4()),
                name="Widget Docs",
                metadata_json=json.dumps({"_bundle": {"key": f"{WIDGET}/docs", "origin": "detection"}}),
                created_at=_iso(0),
                updated_at=_iso(0),
            )
        )
        session.commit()

    review = plan_of(client)["review"]

    assert [entry["path"] for entry in review["excluded"]] == [f"{WIDGET}/docs"]
    assert review["excluded"][0]["reason"] == "this directory is already its own project"
    # The exclusion is not cosmetic: the child's file is no longer a member.
    assert review["members"] == 3
    assert review["adopts"] == 3


def test_a_plan_excludes_a_file_the_last_scan_flagged_missing(
    client: TestClient, seeded: list[str]
) -> None:
    with Session(engine) as session:
        asset = session.exec(select(Asset).where(Asset.storage_path == f"{WIDGET}/other.stl")).one()
        asset.missing_since = _iso(0)
        session.add(asset)
        session.commit()

    review = plan_of(client)["review"]

    excluded = {entry["path"]: entry["reason"] for entry in review["excluded"]}
    assert excluded == {
        f"{WIDGET}/other.stl": "the file is missing from disk (flagged by the last scan)"
    }
    assert review["members"] == 3


def test_a_plan_excludes_a_file_that_already_belongs_to_another_project(
    client: TestClient, seeded: list[str]
) -> None:
    other_asset = next(iter(seeded))
    with Session(engine) as session:
        project = Project(id=str(uuid4()), name="Elsewhere", created_at=_iso(0), updated_at=_iso(0))
        session.add(project)
        session.add(
            ProjectAsset(project_id=project.id, asset_id=other_asset, added_at=_iso(0))
        )
        session.commit()

    review = plan_of(client)["review"]

    assert review["excluded"] == [
        {
            "path": sorted(asset.storage_path for asset in selected_assets())[0],
            "reason": "already a member of project 'Elsewhere'",
        }
    ]
    assert review["members"] == 3


def selected_assets() -> list[Asset]:
    with Session(engine) as session:
        return list(session.exec(select(Asset).order_by(Asset.storage_path)))


def test_a_plan_is_refused_for_an_unknown_workflow_and_an_unknown_row(client: TestClient, seeded: list[str]) -> None:
    unknown_workflow = client.post(
        "/operations/plan", json={"workflow": "reticulate-splines", "target": WIDGET}
    )
    assert unknown_workflow.status_code == 400
    assert "unknown workflow" in unknown_workflow.json()["detail"]

    no_such_row = client.post("/operations/plan", json={"workflow": "create-project", "target": "Nope"})
    assert no_such_row.status_code == 409
    assert no_such_row.json()["detail"] == "no registered files under 'Nope'"

    root = client.post("/operations/plan", json={"workflow": "create-project", "target": " / "})
    assert root.status_code == 409
    assert "library-relative directory key" in root.json()["detail"]


def test_a_plan_reports_an_existing_project_instead_of_offering_a_second_create(
    client: TestClient, seeded: list[str]
) -> None:
    with Session(engine) as session:
        session.add(
            Project(
                id=str(uuid4()),
                name=WIDGET,
                metadata_json=json.dumps({"_bundle": {"key": WIDGET, "origin": "detection"}}),
                created_at=_iso(0),
                updated_at=_iso(0),
            )
        )
        session.commit()

    review = plan_of(client)["review"]

    assert review["creates"] == 0
    assert review["existing_project_name"] == WIDGET
    assert review["existing_project_id"] is not None


def test_a_client_cannot_smuggle_a_step_list_in(client: TestClient, seeded: list[str]) -> None:
    plan = plan_of(client)

    smuggled_plan = client.post(
        "/operations/plan",
        json={"workflow": "create-project", "target": WIDGET, "steps": [{"op": "rm-rf"}]},
    )
    assert smuggled_plan.status_code == 422, smuggled_plan.text

    smuggled_submit = client.post(
        "/operations",
        json={"plan_id": plan["plan_id"], "idempotency_key": uuid4().hex, "steps": [{"op": "rm-rf"}]},
    )
    assert smuggled_submit.status_code == 422, smuggled_submit.text


def test_a_stale_plan_is_refused_with_a_readable_reason(client: TestClient, seeded: list[str]) -> None:
    plan = plan_of(client)

    with Session(engine) as session:
        asset = session.exec(select(Asset).where(Asset.storage_path == f"{WIDGET}/part.stl")).one()
        asset.storage_path = f"{WIDGET}/parts/part.stl"
        session.add(asset)
        session.commit()

    stale = client.post("/operations", json={"plan_id": plan["plan_id"], "idempotency_key": uuid4().hex})

    assert stale.status_code == 409
    assert stale.json()["detail"] == operations_service.STALE_PLAN_MESSAGE
    with Session(engine) as session:
        assert session.exec(select(Operation)).all() == [], "a stale plan must write nothing"


def test_a_plan_id_that_was_not_issued_by_this_server_is_a_400(client: TestClient, seeded: list[str]) -> None:
    for plan_id in ("not-a-plan", "", "eyJ2IjoxfQ.deadbeef"):
        response = client.post("/operations", json={"plan_id": plan_id, "idempotency_key": uuid4().hex})
        assert response.status_code == 400, (plan_id, response.text)
        assert response.json()["detail"] == operations_service.BAD_PLAN_MESSAGE


# --- idempotency -------------------------------------------------------------


def test_the_same_key_and_plan_answer_the_same_operation_once(client: TestClient, seeded: list[str]) -> None:
    plan = plan_of(client)
    key = uuid4().hex

    first = submit_of(client, plan, key)
    second = submit_of(client, plan, key)

    assert first["id"] == second["id"]
    assert first["status"] == "queued" == second["status"]
    with Session(engine) as session:
        assert len(session.exec(select(Operation)).all()) == 1
        assert len(session.exec(select(OperationStep)).all()) == 3
    assert len(audit_rows(audit.OPERATION_SUBMIT, first["id"])) == 1, "a replay must not write a second row"


def test_the_same_key_with_a_different_plan_is_a_409_naming_both_digests(
    client: TestClient, storage: Path, seeded: list[str]
) -> None:
    seed_tree(storage, GADGET)
    widget = plan_of(client, WIDGET)
    gadget = plan_of(client, GADGET)
    key = uuid4().hex
    submit_of(client, widget, key)

    conflict = client.post("/operations", json={"plan_id": gadget["plan_id"], "idempotency_key": key})

    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    with Session(engine) as session:
        stored = session.exec(select(Operation)).one()
    assert stored.plan_digest in detail
    assert "create-project" in detail
    assert f"'{WIDGET}'" in detail and f"'{GADGET}'" in detail


def test_the_key_is_asked_before_the_plan_is_recomputed(
    client: TestClient, storage: Path, seeded: list[str]
) -> None:
    plan = plan_of(client)
    key = uuid4().hex
    first = submit_of(client, plan, key)

    # One more registered file moves the digest the submitted plan id carries.
    body = b"solid extra\nendsolid extra\n"
    (storage / WIDGET / "extra.stl").write_bytes(body)
    with Session(engine) as session:
        session.add(
            Asset(
                id=str(uuid4()),
                name="extra",
                filename="extra.stl",
                mime="model/stl",
                size=len(body),
                storage_path=f"{WIDGET}/extra.stl",
            )
        )
        session.commit()

    with pytest.raises(HTTPException) as stale:
        operations_service.recompute_plan(plan["plan_id"])
    assert stale.value.status_code == 409
    assert stale.value.detail == operations_service.STALE_PLAN_MESSAGE

    replay = submit_of(client, plan, key)

    assert replay["id"] == first["id"]
    assert replay["status"] == first["status"]

    refused = client.post("/operations", json={"plan_id": plan["plan_id"], "idempotency_key": uuid4().hex})

    assert refused.status_code == 409
    assert refused.json()["detail"] == operations_service.STALE_PLAN_MESSAGE
    with Session(engine) as session:
        assert len(session.exec(select(Operation)).all()) == 1
        assert len(session.exec(select(OperationStep)).all()) == 3
    assert len(audit_rows(audit.OPERATION_SUBMIT, first["id"])) == 1, "a replay must not write a second row"


def test_a_replay_of_a_finished_operation_answers_it_and_starts_no_second_one(
    client: TestClient, seeded: list[str]
) -> None:
    plan = plan_of(client)
    key = uuid4().hex
    first = submit_of(client, plan, key)
    run_to_completion()

    assert operation_of(client, first["id"])["status"] == "done"
    with Session(engine) as session:
        assert len(session.exec(select(Project)).all()) == 1
        project_id = session.exec(select(Project)).one().id
        members = {(row.project_id, row.asset_id) for row in session.exec(select(ProjectAsset))}
    assert {project for project, _ in members} == {project_id}, "the seeded members belong to that one project"

    replay = submit_of(client, plan, key)

    assert replay["id"] == first["id"]
    assert replay["status"] == "done"
    with Session(engine) as session:
        assert len(session.exec(select(Operation)).all()) == 1, "a replay must not queue a second operation"
        assert len(session.exec(select(OperationStep)).all()) == 3
        assert len(session.exec(select(Project)).all()) == 1, "a replay must not create a second project"
        replayed_members = {(row.project_id, row.asset_id) for row in session.exec(select(ProjectAsset))}
    assert replayed_members == members, "a replay must not add or drop a membership"
    assert len(audit_rows(audit.OPERATION_SUBMIT, first["id"])) == 1

    # The review now names the project this operation created, so the plan the
    # replay re-submitted would be refused as stale - which is why the key has
    # to be asked before the plan is recomputed.
    with pytest.raises(HTTPException) as stale:
        operations_service.recompute_plan(plan["plan_id"])
    assert stale.value.status_code == 409
    assert stale.value.detail == operations_service.STALE_PLAN_MESSAGE


def test_the_same_key_with_a_different_plan_is_a_409_after_the_first_one_finished(
    client: TestClient, storage: Path, seeded: list[str]
) -> None:
    seed_tree(storage, GADGET)
    key = uuid4().hex
    first = submit_of(client, plan_of(client, WIDGET), key)
    run_to_completion()
    gadget = plan_of(client, GADGET)

    conflict = client.post("/operations", json={"plan_id": gadget["plan_id"], "idempotency_key": key})

    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    with Session(engine) as session:
        stored = session.get(Operation, first["id"]).plan_digest
    assert stored in detail
    assert gadget["plan_id"].rsplit(".", 1)[1] in detail
    assert "create-project" in detail
    assert f"'{WIDGET}'" in detail and f"'{GADGET}'" in detail


def test_a_blank_idempotency_key_is_refused(client: TestClient, seeded: list[str]) -> None:
    plan = plan_of(client)
    response = client.post("/operations", json={"plan_id": plan["plan_id"], "idempotency_key": "  "})
    assert response.status_code == 400
    assert response.json()["detail"] == "idempotency_key is required"


# --- path locks --------------------------------------------------------------


def test_two_operations_over_one_path_queue_and_the_loser_holds_no_lock(
    client: TestClient, seeded: list[str]
) -> None:
    first = submit_of(client, plan_of(client))
    second = submit_of(client, plan_of(client))

    reached = threading.Event()
    release = threading.Event()
    operations_runner.set_step_guard(block_at(0, reached, release))
    try:
        assert operations_runner.dispatch_once() == 1, "both operations were dispatched over one path"
        assert reached.wait(timeout=10)
        assert operations_runner.dispatch_once() == 0, "a blocked operation was dispatched again"

        assert operation_of(client, first["id"])["status"] == "running"
        assert operation_of(client, second["id"])["status"] == "queued"
        with Session(engine) as session:
            held = session.exec(select(OperationLock)).all()
        assert [(row.path, row.operation_id) for row in held] == [(WIDGET, first["id"])], (
            "the queued operation must keep no lock rows of its own"
        )
    finally:
        release.set()
    operations_runner.wait_idle(timeout=15)

    assert operation_of(client, first["id"])["status"] == "done"
    assert operation_of(client, second["id"])["status"] == "queued"
    with Session(engine) as session:
        assert session.exec(select(OperationLock)).all() == [], "a finished operation must release its locks"

    run_to_completion()
    assert operation_of(client, second["id"])["status"] == "done"


def test_two_operations_over_disjoint_paths_run_at_the_same_time(
    client: TestClient, storage: Path, seeded: list[str]
) -> None:
    seed_tree(storage, GADGET)
    widget = submit_of(client, plan_of(client, WIDGET))
    gadget = submit_of(client, plan_of(client, GADGET))

    barrier = threading.Barrier(2, timeout=10)

    def guard(_operation, step) -> None:
        if step.step_index == 0:
            # Only two workers inside their first step at once can satisfy this;
            # a serialised pair raises BrokenBarrierError and both operations fail.
            barrier.wait()

    operations_runner.set_step_guard(guard)
    try:
        assert operations_runner.dispatch_once() == 2, "disjoint operations were serialised"
        operations_runner.wait_idle(timeout=15)
    finally:
        operations_runner.set_step_guard(None)

    for operation in (widget, gadget):
        body = operation_of(client, operation["id"])
        assert body["status"] == "done", body
        assert body["error"] is None


# --- cancel ------------------------------------------------------------------


def test_cancel_between_steps_finishes_the_step_and_skips_the_rest(client: TestClient, seeded: list[str]) -> None:
    operation = submit_of(client, plan_of(client))

    reached = threading.Event()
    release = threading.Event()
    operations_runner.set_step_guard(block_at(1, reached, release))
    try:
        assert operations_runner.dispatch_once() == 1
        assert reached.wait(timeout=10), "step 1 never started"

        cancelled = client.post(f"/operations/{operation['id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelling"
    finally:
        release.set()
    operations_runner.wait_idle(timeout=15)

    body = poll(client, operation["id"], "cancelled")
    assert [step["status"] for step in body["steps"]] == ["done", "done", "skipped"]
    assert body["undo_state"] == "available", "a cancel must leave the steps that ran undoable"
    assert body["error"] is None
    rows = audit_rows(audit.OPERATION_CANCEL, operation["id"])
    assert len(rows) == 1
    assert rows[0].actor == audit.ACTOR_DISABLED_USERNAME
    assert json.loads(rows[0].detail_json) == {"previous": "running", "result": "cancelling"}

    again = client.post(f"/operations/{operation['id']}/cancel")
    assert again.status_code == 409
    assert "already" in again.json()["detail"]


def test_cancel_of_a_queued_operation_finishes_it_without_running_anything(
    client: TestClient, seeded: list[str]
) -> None:
    operation = submit_of(client, plan_of(client))

    response = client.post(f"/operations/{operation['id']}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert [step["status"] for step in response.json()["steps"]] == ["pending", "pending", "pending"]
    with Session(engine) as session:
        assert session.exec(select(Project)).all() == []
    rows = audit_rows(audit.OPERATION_CANCEL, operation["id"])
    assert len(rows) == 1
    # Both cancel branches write one shape: the state the caller found and the
    # state it is now, both plain strings.
    assert json.loads(rows[0].detail_json) == {"previous": "queued", "result": "cancelled"}


# --- resume ------------------------------------------------------------------


def test_resume_does_not_repeat_a_finished_step(client: TestClient, seeded: list[str]) -> None:
    operation = submit_of(client, plan_of(client))
    run_to_completion()
    done = operation_of(client, operation["id"])
    assert done["status"] == "done"
    assert json.loads(json.dumps(done["steps"][0]["result"])).get("created") is True
    first_result = done["steps"][0]["result"]
    project_id = first_result["project_id"]

    # Interrupted BETWEEN steps: step 0 finished, steps 1 and 2 never started.
    with Session(engine) as session:
        stored = session.get(Operation, operation["id"])
        stored.status = operations_service.STATUS_RUNNING
        stored.finished_at = None
        stored.undo_state = operations_service.UNDO_NONE
        stored.error = None
        session.add(stored)
        for index in (1, 2):
            step = session.exec(
                select(OperationStep).where(
                    OperationStep.operation_id == operation["id"],
                    OperationStep.step_index == index,
                )
            ).one()
            step.status = operations_service.STEP_PENDING
            step.result_json = None
            step.started_at = None
            step.finished_at = None
            session.add(step)
        session.commit()

    # A FRESH lifespan is the repair: `resume_on_startup` runs inside it.
    with TestClient(main.app):
        pass

    body = operation_of(client, operation["id"])
    assert body["status"] == "done", body
    assert body["steps"][0]["result"] == first_result, "the finished step was re-run"
    with Session(engine) as session:
        step_zero = session.exec(
            select(OperationStep).where(
                OperationStep.operation_id == operation["id"],
                OperationStep.step_index == 0,
            )
        ).one()
        assert json.loads(step_zero.result_json)["created"] is True
        projects = session.exec(select(Project)).all()
    assert [project.id for project in projects] == [project_id], "a re-run must not add a second project"


def test_resume_fails_a_step_that_was_caught_running_and_never_repeats_it(
    client: TestClient, seeded: list[str]
) -> None:
    operation = submit_of(client, plan_of(client))
    run_to_completion()
    body = operation_of(client, operation["id"])
    assert body["status"] == "done"

    # Interrupted INSIDE step 1: its outcome is unknown, so it must not be re-run.
    with Session(engine) as session:
        stored = session.get(Operation, operation["id"])
        stored.status = operations_service.STATUS_RUNNING
        stored.finished_at = None
        stored.undo_state = operations_service.UNDO_NONE
        session.add(stored)
        for index, status in ((1, operations_service.STEP_RUNNING), (2, operations_service.STEP_PENDING)):
            step = session.exec(
                select(OperationStep).where(
                    OperationStep.operation_id == operation["id"],
                    OperationStep.step_index == index,
                )
            ).one()
            step.status = status
            step.result_json = None
            step.started_at = _iso(0) if status == operations_service.STEP_RUNNING else None
            step.finished_at = None
            session.add(step)
        session.commit()

    with TestClient(main.app):
        pass

    repaired = operation_of(client, operation["id"])
    assert repaired["status"] == "failed"
    assert "verify-members" in repaired["error"]
    assert "1 step(s) not started" in repaired["error"]
    assert [step["status"] for step in repaired["steps"]] == ["done", "failed", "pending"]
    assert repaired["steps"][1]["error"] == "interrupted"
    assert repaired["undo_state"] == "available"
    assert len(audit_rows(audit.OPERATION_FAILED, operation["id"])) == 1


def test_resume_leaves_a_finished_operation_alone(client: TestClient, seeded: list[str]) -> None:
    operation = submit_of(client, plan_of(client))
    run_to_completion()
    before = operation_of(client, operation["id"])

    with TestClient(main.app):
        pass

    assert operation_of(client, operation["id"]) == before


# --- undo and supersede -------------------------------------------------------


def test_undo_deletes_the_project_and_never_the_files(
    client: TestClient, storage: Path, seeded: list[str]
) -> None:
    operation = submit_of(client, plan_of(client))
    run_to_completion()
    body = operation_of(client, operation["id"])
    assert body["status"] == "done"
    project_id = body["steps"][0]["result"]["project_id"]

    undone = client.post(f"/operations/{operation['id']}/undo")

    assert undone.status_code == 200, undone.text
    assert undone.json()["undone"] is True
    assert undone.json()["project_id"] == project_id
    assert operation_of(client, operation["id"])["undo_state"] == "undone"
    with Session(engine) as session:
        assert session.get(Project, project_id) is None
        assert session.exec(select(ProjectAsset).where(ProjectAsset.project_id == project_id)).all() == []
        assert len(session.exec(select(Asset)).all()) == len(seeded), "undo must not touch the asset rows"
    for relative in TREE:
        assert (storage / relative).exists(), f"undo deleted {relative} from disk"
    rows = audit_rows(audit.OPERATION_UNDO, operation["id"])
    assert len(rows) == 1
    assert json.loads(rows[0].detail_json)["project_id"] == project_id

    again = client.post(f"/operations/{operation['id']}/undo")
    assert again.status_code == 409
    assert again.json()["detail"] == "the operation has already been undone"


def test_the_create_project_undo_body_gains_only_the_two_additive_null_keys(
    client: TestClient, seeded: list[str]
) -> None:
    """The `OperationUndoOut` change is purely additive, and asserted rather than loosened.

    A create answers only the project it deleted, and leaves EVERY
    workflow-specific key null: `moves` and `projects_removed` (a split's),
    `members_removed` (an adopt's) and `asset_id`/`filename`/`assets_removed`/
    `folders_removed`/`entries_failed` (the two import legs') and
    `project_removed`/`payload_residue` (the attach-project and in-place extract
    legs') and `records_dropped`/`records_kept` (the enrich leg's). The key set
    below is
    EXACT, not a subset: a body that dropped `project_id`, or one that invented a
    zero for `moves`, `members_removed` or `assets_removed`, reds here with the
    whole shape in front of the reviewer.
    """
    operation = submit_of(client, plan_of(client))
    run_to_completion()

    undone = client.post(f"/operations/{operation['id']}/undo")

    assert undone.status_code == 200, undone.text
    body = undone.json()
    assert set(body) == {
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
        "records_dropped",
        "records_kept",
    }
    assert body["undone"] is True
    assert body["project_id"] is not None and body["project_name"] is not None
    assert body["moves"] is None
    assert body["projects_removed"] is None
    assert body["members_removed"] is None
    assert body["asset_id"] is None
    assert body["filename"] is None
    assert body["assets_removed"] is None
    assert body["folders_removed"] is None
    assert body["entries_failed"] is None
    assert body["project_removed"] is None
    assert body["payload_residue"] is None
    assert body["records_dropped"] is None
    assert body["records_kept"] is None


def test_a_later_operation_over_the_same_paths_supersedes_the_earlier_undo(
    client: TestClient, seeded: list[str]
) -> None:
    first = submit_of(client, plan_of(client))
    run_to_completion()
    assert operation_of(client, first["id"])["undo_state"] == "available"

    second = submit_of(client, plan_of(client))
    run_to_completion()
    assert operation_of(client, second["id"])["status"] == "done"

    superseded = operation_of(client, first["id"])
    assert superseded["undo_state"] == "superseded"
    assert superseded["superseded_by"] == second["id"]

    refused = client.post(f"/operations/{first['id']}/undo")
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["superseded_by"] == second["id"]
    assert detail["undo_state"] == "superseded"
    assert second["id"] in detail["reason"]


def test_undo_of_a_failed_operation_covers_what_ran(client: TestClient, seeded: list[str]) -> None:
    operation = submit_of(client, plan_of(client))

    def guard(_operation, step) -> None:
        if step.step_index == 1:
            raise RuntimeError("the member set was not what the plan promised")

    operations_runner.set_step_guard(guard)
    run_to_completion()
    operations_runner.set_step_guard(None)

    failed = operation_of(client, operation["id"])
    assert failed["status"] == "failed"
    assert failed["error"] == "the member set was not what the plan promised"
    assert [step["status"] for step in failed["steps"]] == ["done", "failed", "pending"]
    assert failed["undo_state"] == "available"
    project_id = failed["steps"][0]["result"]["project_id"]

    undone = client.post(f"/operations/{operation['id']}/undo")

    assert undone.status_code == 200, undone.text
    with Session(engine) as session:
        assert session.get(Project, project_id) is None


def test_undo_is_refused_before_the_operation_finishes(client: TestClient, seeded: list[str]) -> None:
    operation = submit_of(client, plan_of(client))

    response = client.post(f"/operations/{operation['id']}/undo")

    assert response.status_code == 409
    assert response.json()["detail"] == "the operation has not finished"


# --- the queue's filters ------------------------------------------------------


def _queue_row(
    operation_id: str,
    *,
    workflow: str,
    target: str,
    submitted_by: str,
    status: str = "done",
    created: Optional[str] = None,
) -> str:
    """Insert one queue row and return its id with the workflow, target and submitter named.

    `_operation_row` hardcodes all three, so a test about a filter OVER them cannot
    use it: every row it builds would answer the same question whatever the filter
    did.
    """
    stamp = created or _iso(1)
    with Session(engine) as session:
        session.add(
            Operation(
                id=operation_id,
                workflow=workflow,
                target=target,
                plan_json=json.dumps({"paths": [], "steps": [], "review": {}}),
                plan_digest="d" * 64,
                idempotency_key=f"key-{operation_id}",
                status=status,
                submitted_by=submitted_by,
                request_id="-",
                created_at=stamp,
                finished_at=stamp if status in operations_service.TERMINAL_STATUSES else None,
            )
        )
        session.commit()
    return operation_id


def test_the_queue_filters_by_target(client: TestClient) -> None:
    """A same-workflow row on ANOTHER target must not answer a target filter.

    False state caught: a queue that ignored `target` and returned the whole page,
    so a client's "resume the operation for THIS target" would pick the Beta row
    for an Alpha question - the one answer that cannot be discovered to be wrong.
    """
    alpha_create = _queue_row(
        str(uuid4()), workflow="create-project", target="Alpha", submitted_by="alice"
    )
    beta_create = _queue_row(
        str(uuid4()), workflow="create-project", target="Beta", submitted_by="alice"
    )
    alpha_adopt = _queue_row(str(uuid4()), workflow="adopt", target="Alpha", submitted_by="bob")

    response = client.get("/operations", params={"target": "Alpha"})

    assert response.status_code == 200, response.text
    ids = {row["id"] for row in response.json()}
    assert ids == {alpha_create, alpha_adopt}
    assert beta_create not in ids, "a same-workflow row on another target answered the filter"

    narrowed = client.get("/operations", params={"target": "Alpha", "workflow": "adopt"})
    assert [row["id"] for row in narrowed.json()] == [alpha_adopt]


def test_the_queue_filters_by_submitter(client: TestClient) -> None:
    """Two users, one target: "only mine" must not be a claim about a page.

    False state caught: a submitter filter that is really a client-side scan of a
    truncated page, which would either miss the caller's own row or show another
    user's row for the same target.
    """
    alice_row = _queue_row(str(uuid4()), workflow="adopt", target="Alpha", submitted_by="alice")
    bob_row = _queue_row(str(uuid4()), workflow="adopt", target="Alpha", submitted_by="bob")

    mine = client.get("/operations", params={"submitted_by": "bob"})

    assert mine.status_code == 200, mine.text
    assert {row["id"] for row in mine.json()} == {bob_row}
    assert alice_row not in {row["id"] for row in mine.json()}, (
        "another user's row for the same target answered the submitter filter"
    )

    both = client.get("/operations", params={"submitted_by": "bob", "target": "Alpha"})
    assert [row["id"] for row in both.json()] == [bob_row]

    nobody = client.get("/operations", params={"submitted_by": "nobody"})
    assert nobody.status_code == 200 and nobody.json() == [], (
        "an unknown submitter must answer the empty page, not the whole one"
    )


def test_the_new_filters_compose_with_state_and_the_page(client: TestClient) -> None:
    """Filters narrow BEFORE the page, and the empty string is the library root.

    False state caught: a filter applied to the rows after `offset`/`limit` sliced
    the unfiltered table, which answers a shorter page (or another row entirely)
    while still looking filtered. The old guards are measured beside it because a
    new parameter must not weaken the validation the route already had.
    """
    newest = _queue_row(
        str(uuid4()), workflow="create-project", target="Alpha", submitted_by="a", created=_iso(5)
    )
    queued = _queue_row(
        str(uuid4()),
        workflow="create-project",
        target="Alpha",
        submitted_by="a",
        status="queued",
        created=_iso(4),
    )
    second = _queue_row(
        str(uuid4()), workflow="adopt", target="Alpha", submitted_by="a", created=_iso(3)
    )
    failed = _queue_row(
        str(uuid4()),
        workflow="adopt",
        target="Alpha",
        submitted_by="a",
        status="failed",
        created=_iso(2),
    )
    third = _queue_row(
        str(uuid4()), workflow="adopt", target="Alpha", submitted_by="a", created=_iso(1)
    )

    page = client.get(
        "/operations", params={"target": "Alpha", "state": "done", "limit": 1, "offset": 1}
    )

    assert page.status_code == 200, page.text
    assert [row["id"] for row in page.json()] == [second], (
        "the second DONE row under this target is what page 2 of the filtered set holds"
    )
    assert {newest, queued, failed, third}.isdisjoint({row["id"] for row in page.json()})

    unknown_workflow = client.get("/operations", params={"workflow": "nope"})
    assert unknown_workflow.status_code == 400
    assert unknown_workflow.json()["detail"] == "unknown workflow"
    unknown_state = client.get("/operations", params={"state": "nope"})
    assert unknown_state.status_code == 400
    assert unknown_state.json()["detail"] == "unknown state"

    # `""` is a legal target - the library root - so it must be a filter, not an
    # absent one: a truthiness check here would answer BOTH rows below.
    root_row = _queue_row(str(uuid4()), workflow="library-scan", target="", submitted_by="a")
    _queue_row(str(uuid4()), workflow="library-scan", target="Alpha", submitted_by="a")

    root = client.get("/operations?target=")

    assert root.status_code == 200, root.text
    assert [row["id"] for row in root.json()] == [root_row]


# --- retention ---------------------------------------------------------------


def test_prune_keeps_the_newest_200_old_rows_and_every_young_one(
    client: TestClient, seeded: list[str]
) -> None:
    old_ids = [str(uuid4()) for _ in range(205)]
    young_ids = [str(uuid4()) for _ in range(3)]
    queued_id = str(uuid4())
    with Session(engine) as session:
        for operation_id in old_ids:
            session.add(
                _operation_row(operation_id, "done", created=_iso(40), finished=_iso(40))
            )
        for operation_id in young_ids:
            session.add(_operation_row(operation_id, "done", created=_iso(1), finished=_iso(1)))
        session.add(_operation_row(queued_id, "queued", created=_iso(60), finished=None))
        for operation_id in old_ids + young_ids:
            session.add(
                OperationStep(
                    operation_id=operation_id,
                    step_index=0,
                    op="create-project",
                    status=operations_service.STEP_DONE,
                )
            )
            session.add(
                OperationLock(path=f"Row/{operation_id}", operation_id=operation_id, created_at=_iso(40))
            )
        session.commit()

    # The audit table is the permanent record: one row on an operation about to be pruned.
    audit.record(
        audit.Event(audit.OPERATION_SUBMIT, old_ids[0], {"workflow": "create-project"}),
        ctx=audit.runner_context(),
    )

    deleted = operations_service.prune()

    assert deleted == 5, "205 old terminal rows against a 200 row window is exactly 5"
    with Session(engine) as session:
        remaining = {row.id for row in session.exec(select(Operation))}
    assert queued_id in remaining, "a queued row must never be pruned"
    assert set(young_ids) <= remaining
    assert len(remaining) == 200 + 3 + 1
    # The oldest five of the old set are the ones that went.
    assert set(old_ids[:5]).isdisjoint(remaining)
    assert set(old_ids[5:]) <= remaining
    with Session(engine) as session:
        orphans = session.exec(
            select(OperationStep).where(OperationStep.operation_id.in_(old_ids[:5]))
        ).all()
        locks = session.exec(
            select(OperationLock).where(OperationLock.operation_id.in_(old_ids[:5]))
        ).all()
    assert orphans == [] and locks == [], "a pruned operation must take its steps and locks with it"

    kept = client.get("/audit", params={"target": old_ids[0]})
    assert kept.status_code == 200, kept.text
    assert kept.json()["total"] == 1, "the audit row is the permanent record of a pruned operation"


def _operation_row(operation_id: str, status: str, *, created: str, finished: Optional[str]) -> Operation:
    return Operation(
        id=operation_id,
        workflow="create-project",
        target="Row",
        plan_json=json.dumps({"paths": ["Row"], "steps": [], "review": {}}),
        plan_digest="d" * 64,
        idempotency_key=f"key-{operation_id}",
        status=status,
        submitted_by="tester",
        request_id="-",
        created_at=created,
        finished_at=finished,
    )


# --- audit and permissions ---------------------------------------------------


def test_submit_records_the_caller_the_operation_and_the_request_id(
    client: TestClient, seeded: list[str]
) -> None:
    plan = plan_of(client)
    response = client.post(
        "/operations",
        json={"plan_id": plan["plan_id"], "idempotency_key": uuid4().hex},
        headers={"X-Request-ID": "operations-submit-1"},
    )
    assert response.status_code == 202
    operation_id = response.json()["id"]

    rows = audit_rows(audit.OPERATION_SUBMIT, operation_id)
    assert len(rows) == 1
    row = rows[0]
    # Auth is off in this file, so the actor is the fixed `local` principal - the
    # same answer every other audited route gives in that deployment mode.
    assert row.actor == audit.ACTOR_DISABLED_USERNAME
    assert row.request_id == "operations-submit-1"
    assert row.method == "POST" and row.path == "/operations"
    assert json.loads(row.detail_json)["steps"] == 3


def test_a_failed_audit_write_on_submit_leaves_no_operation_row(
    client: TestClient, seeded: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_of(client)

    def explode(_session, _row) -> None:
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(audit, "_add", explode)

    with pytest.raises(RuntimeError, match="audit insert failed"):
        client.post("/operations", json={"plan_id": plan["plan_id"], "idempotency_key": uuid4().hex})

    monkeypatch.undo()
    with Session(engine) as session:
        assert session.exec(select(Operation)).all() == [], "a failed audit write must roll the operation back"
        assert session.exec(select(OperationStep)).all() == []


def test_the_six_routes_are_classified_and_declared_as_the_ticket_decided() -> None:
    expected = {
        ("POST", "/operations/plan"): permissions.CONTRIBUTE,
        ("POST", "/operations"): permissions.CONTRIBUTE,
        ("GET", "/operations/{operation_id}"): permissions.READ,
        ("GET", "/operations"): permissions.READ,
        ("POST", "/operations/{operation_id}/cancel"): permissions.CONTRIBUTE,
        ("POST", "/operations/{operation_id}/undo"): permissions.CONTRIBUTE,
    }
    for key, level in expected.items():
        assert permissions.ROUTE_PERMISSIONS[key] == level, key

    declared = {
        (method, route.path): permissions.declared_levels(route)
        for route in permissions.router_routes(*main.API_ROUTERS)
        for method in route.methods
        if route.path.startswith("/operations")
    }
    assert declared == {key: [level] for key, level in expected.items()}


def test_the_queue_is_readable_by_a_viewer_and_the_three_mutations_are_contribute() -> None:
    """The level split the owner decided, read off the matrix.

    Mf-215n's amendment replaced the earlier editor-only rule for submit, cancel
    and undo: an operation is contribute unless its workflow can REMOVE data, so
    the route declares the minimum and the handler enforces the workflow's own
    level. Viewers read the queue and may not mutate it. The eleven role sweeps
    in `test_permissions_matrix.py` are what actually call each route as each
    role, so no role test is hand-rolled here; this pins the decision the sweeps
    are driven by, so a future route that moves level fails with the intent in
    front of the reviewer.
    """
    read_keys = {key for key, level in permissions.ROUTE_PERMISSIONS.items() if level == permissions.READ}
    contribute_keys = {
        key for key, level in permissions.ROUTE_PERMISSIONS.items() if level == permissions.CONTRIBUTE
    }
    write_keys = {key for key, level in permissions.ROUTE_PERMISSIONS.items() if level == permissions.WRITE}

    assert {("GET", "/operations"), ("GET", "/operations/{operation_id}")} <= read_keys
    assert {
        ("POST", "/operations/plan"),
        ("POST", "/operations"),
        ("POST", "/operations/{operation_id}/cancel"),
        ("POST", "/operations/{operation_id}/undo"),
    } <= contribute_keys
    assert {
        ("GET", "/operations"),
        ("GET", "/operations/{operation_id}"),
        ("POST", "/operations/plan"),
        ("POST", "/operations"),
        ("POST", "/operations/{operation_id}/cancel"),
        ("POST", "/operations/{operation_id}/undo"),
    }.isdisjoint(write_keys)


def test_the_workflow_level_table_is_the_owner_rubric() -> None:
    """The whole table, pinned by equality so any change fails here with a reason.

    The owner's rubric (Mf-215n amendment, 2026-11-27) is "can they REMOVE data
    with this action": an operation is contribute unless it can REMOVE data, so
    only a data-removing workflow may be write. A row for a workflow that does
    not exist yet is a declaration for the record.
    """
    assert operations_service.WORKFLOW_LEVELS == {
        # Contribute: none of these removes data with this action.
        "create-project": permissions.CONTRIBUTE,
        "adopt": permissions.CONTRIBUTE,
        "extract-and-create": permissions.CONTRIBUTE,
        "import-folder": permissions.CONTRIBUTE,
        "import-zip": permissions.CONTRIBUTE,
        "import-link": permissions.CONTRIBUTE,
        "extract": permissions.CONTRIBUTE,
        "enrich": permissions.CONTRIBUTE,
        "split-and-create": permissions.CONTRIBUTE,
        "relocate": permissions.CONTRIBUTE,
        # Write: it removes nothing, but it is an existing route whose capacity
        # consequence is an open question with the owner (Mf-215n).
        "library-scan": permissions.WRITE,
    }


def test_a_workflow_with_no_table_entry_is_refused_rather_than_defaulted_to_contribute() -> None:
    """Fail closed: an unclassified workflow is a bug, so it is refused outright.

    Defaulting to contribute would let an unknown workflow run at the loosest
    mutating level; the refusal names the WRITE requirement instead.
    """
    with pytest.raises(HTTPException) as excinfo:
        operations_service.level_for_workflow("no-such-workflow")

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == permissions.DENY_DETAIL[permissions.WRITE]


def test_a_declared_workflow_without_a_planner_yet_resolves_to_contribute() -> None:
    """The owner rubric, table-levelled: organising and relocating are contribute.

    Honest scope, in two halves. `relocate` and `import-folder` are still
    DECLARATIONS FOR THE RECORD - they are not in `operations_service.WORKFLOWS`, so
    their level is a table-level assertion and nothing more. `adopt`,
    `split-and-create`, `extract-and-create`, `extract`, `library-scan`,
    `import-link`, `import-zip` and `enrich` left that set when their planners
    landed (`adopt` in Mf-ytig, `split-and-create` in Mf-g759,
    `extract-and-create` in Mf-hebx, `extract` in Mf-y2u3 3/4, `library-scan` and
    the two ingest doors in Mf-y2u3 otherwise, `enrich` in Mf-y2u3 4/4): they are
    real workflows now and are asserted end to end, roles included, in
    `tests/test_adopt_workflow.py`, `tests/test_split_and_create.py`,
    `tests/test_extract_and_create.py`, `tests/test_extract_workflow.py`,
    `tests/test_scan_workflow.py`, `tests/test_import_workflows.py` and
    `tests/test_enrich_workflow.py`. Both halves fail the moment somebody changes
    a level, and the first fails the moment a declaration starts claiming to run.
    """
    assert set(operations_service.WORKFLOWS) == {
        "create-project",
        "split-and-create",
        "adopt",
        "extract-and-create",
        "extract",
        "library-scan",
        "import-link",
        "import-zip",
        "enrich",
    }
    for workflow in ("relocate", "import-folder"):
        assert workflow not in operations_service.WORKFLOWS, (
            f"{workflow} has a planner now; move it out of the declaration-only list"
        )
    for workflow in ("adopt", "split-and-create", "extract-and-create", "extract", "relocate"):
        assert operations_service.level_for_workflow(workflow) == permissions.CONTRIBUTE, workflow
