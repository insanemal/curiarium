"""Adopt as a registry workflow (Mf-ytig): the grown-project path Mf-g759 lost.

Mf-g759 deleted the detected-bundle arrange and with it the one door a grown
project had: "this folder has more files than I hold - add them". The split kept
the create half; the EXTEND half left no route at all, so the only way to grow a
project was the manual `POST /project/{id}/assets` attach, which attaches
memberships and never refreshes the marker's `groups`. `adopt` is that door,
rebuilt as a composition of `adopt_service`'s missing-only membership delta, the
engine's own group measurement, and `project_service.set_bundle_groups`, driven by
the operations ledger exactly like `split-and-create`.

Why this file exists: the arrange's grown-project tests were deleted with it, so
nothing below this workflow proves the promise any more. Every demonstration here
is therefore written so that a test which would ALSO pass with the feature absent
is not accepted:

* **the workflow is a registry entry, not a client's step list** - `adopt` is in
  `WORKFLOWS`, its three steps have no client-supplied `args` on the wire, and a
  submit body that carries a `steps` array is a 422; a server that accepted a
  plan assembled by the client fails.
* **the 171/9 truth is read off the wire** - the owner's real Stagetop folder: 9
  groups, 19 model files each, a marker that records only the 6 groups the
  project was made with and NO memberships. The review must answer 171 members,
  9 groups whose member counts sum to 171, and an all-`model` role census; the
  run must answer `added == 171` and 6 -> 9 groups. A planner that re-used the
  marker's stale group list, or counted memberships it already had, fails.
* **every exclusion is named, and the excluded files are absent from the step** -
  a nested project, another project's member, a scan-flagged missing file and an
  operator-excluded group each answer the sentence their owner wrote, and none of
  their asset ids reaches the adopt step's `member_ids`; a review that silently
  dropped a file fails. The operator's `included: False` is CARRIED through the
  refresh, not defaulted back to included.
* **the disk-minus-registered refusal names the scan** - a folder that grew on
  disk without a rescan is refused before any row exists; adopting past it would
  leave the unregistered files out and call the project "complete" next time.
* **one submit runs it, and the marker is the engine's measurement** - the three
  steps run in order, the refresh writes the group list a fresh
  `proposal_engine.measure` + `bundle_service.wire_groups` produces, and the
  marker's identity keys stay byte-identical; a marker rewritten from the
  hand-written review shape fails.
* **undo is the entry, byte for byte** - exactly one `operation.undo` audit row,
  171 memberships removed, the pre-adopt group list restored verbatim and
  `undo_state == "undone"`; a second undo is the shared 409.
* **undo lapses when a later operation touches the same root** - a later
  create-project replay supersedes it, and the refusal names the id.
* **the roles are the owner's rubric** - a contributor adopts, a viewer is
  refused with the permissions module's own sentence.
* **the manual attach route is untouched** - it still attaches and still writes
  no groups; that is exactly the defect adopt exists to replace, and the
  demonstration keeps it honest rather than pretending it was fixed.
* **refusals never write a row** - unknown project, no marker, a stale walk root
  and a running scan all answer 409 with no `Operation` row; every refusal
  happens before the ledger hears about it.
* **the honest grown project adopts only the delta** - create the project the
  real way over 6 groups, grow the folder to 9, and the adopt's review is
  3 * 19, not 171. The create-project door records no `groups` in its marker, so
  the honest measurement is `None` before and 9 after; the 6-before shape is
  AC-2's seeded marker, which is the state the old attach route left.

Harness: the same idioms as `tests/test_split_and_create.py` - one migrated
database, a temp storage root holding the real files, `scan_and_seed` for the
rows the walk would write, auth off unless a test is about *who* may adopt.
"""

import json
from pathlib import Path
from typing import Optional
from uuid import uuid4

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
import project_service
import proposal_engine
import seams
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

KEY = "import/Stagetop"
# The owner's real top-level Stagetop folder names: the fixture keeps the names
# because the engine's group vocabulary is the names on disk, and renames would
# make the marker's `groups` a different list from the one the library has.
GROUPS = (
    "Community Wave",
    "PRUSA USERS ONLY",
    "StageTop 2 Ready Pack",
    "StageTop Wave One - Personal Use",
    "Wave 1.5",
    "Wave 2",
    "Wave 3",
    "Wave 3.5",
    "Wave 4",
)
BEFORE_GROUPS = GROUPS[:6]  # the groups the folder had when the project was made
NEW_GROUPS = GROUPS[6:]  # the three the folder grew by
PER_GROUP = 19

DETECTOR = "engine:one-project"
STAMP = "2026-01-01T00:00:00+00:00"
STL_BYTES = b"solid cube\nendsolid cube\n"

BREAK_GLASS = {"username": "admin", "password": "super-secret"}
ADMIN_USERNAME = "root"
ADMIN_PASSWORD = "root-password-1"
CONTRIBUTOR_USERNAME = "contributor"
CONTRIBUTOR_PASSWORD = "contributor-password-1"
VIEWER_USERNAME = "viewer"
VIEWER_PASSWORD = "viewer-password-1"


def _tree() -> dict[str, bytes]:
    """The Stagetop shape: 9 groups x 19 model files, 171 registered assets."""
    return {
        f"{KEY}/{group}/part-{index}.stl": STL_BYTES
        for group in GROUPS
        for index in range(PER_GROUP)
    }


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
    """Migrations, an empty world, auth off and no step guard, per test.

    The database is one file for the whole run, so a leftover project would 409
    the next seed and a leftover operation would turn the next submit's 202 into a
    409 (the idempotency key is unique). Auth is off by default because only test
    8 is about *who* may adopt; it turns auth back on for itself.
    """
    run_migrations(engine)
    truncate_everything()
    login_throttle_module.limiter.reset()
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    operations_runner.set_step_guard(None)
    yield
    operations_runner.set_step_guard(None)
    operations_runner.wait_idle(timeout=15)
    truncate_everything()
    login_throttle_module.limiter.reset()


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Argon2's floor parameters: only test 8 signs accounts in, and it signs three."""
    monkeypatch.setattr(accounts, "hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))


def point_storage(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Point every path-bound module at `root`; the fixture and test 13 share this."""
    monkeypatch.setattr(db, "STORAGE", root)
    monkeypatch.setattr(bundle_service, "STORAGE", root)
    monkeypatch.setattr(library_scan, "STORAGE", root)


@pytest.fixture()
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp storage root holding the whole 9-group tree the plan's rows name."""
    root = tmp_path / "storage"
    write_tree(root, GROUPS)
    point_storage(monkeypatch, root)
    return root


@pytest.fixture()
def client() -> TestClient:
    with TestClient(main.app) as test_client:
        yield test_client


def write(root: Path, relative: str, body: bytes = STL_BYTES) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def write_tree(root: Path, groups: tuple[str, ...]) -> None:
    """Write the model files `_tree()` names for each group in `groups` (a subset of GROUPS)."""
    for relative, body in _tree().items():
        group = relative[len(KEY) + 1 :].split("/", 1)[0]
        if group in groups:
            write(root, relative, body)


def scan_and_seed(root: Path) -> None:
    """Register the tree exactly as the live box does, then fill any folder row the walk skipped.

    `library_scan.scan_storage_root` writes the asset rows and backfills a folder
    row for every directory that holds a registered file; a directory the backfill
    cannot reach still needs a row (the plan snapshots the rows it may retire), so
    one is created here with an explicit id, parent line intact.
    """
    state = library_scan.scan_storage_root()
    assert state.failed == 0, state.errors
    with Session(engine) as session:
        existing = {folder.storage_path or "" for folder in session.exec(select(Folder))}
        directories = sorted(
            (
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_dir() and path.relative_to(root).as_posix().split("/", 1)[0] != "thumbs"
            ),
            key=lambda relative: (relative.count("/"), relative),
        )
        for relative in directories:
            if relative in existing:
                continue
            parent_id = None
            if "/" in relative:
                parent = session.exec(select(Folder).where(Folder.storage_path == relative.rsplit("/", 1)[0])).first()
                parent_id = parent.id if parent is not None else None
            session.add(
                Folder(
                    id=str(uuid4()),
                    name=relative.rsplit("/", 1)[-1],
                    tags_json="[]",
                    parent_id=parent_id,
                    storage_path=relative,
                )
            )
            session.flush()
            existing.add(relative)
        session.commit()


# --- seeding the project -----------------------------------------------------


def marker_of(groups: tuple[str, ...], **overrides) -> dict:
    """The marker a 6-group create wrote: every group 19 models, all included."""
    marker = {
        "key": KEY,
        "detector": DETECTOR,
        "confidence": "high",
        "shape": "one-project",
        "rule": DETECTOR,
        "rel_dir": KEY,
        "groups": [
            {
                "name": name,
                "model_count": PER_GROUP,
                "foreign": False,
                "separable": True,
                "included": True,
                "attached": PER_GROUP,
            }
            for name in groups
        ],
    }
    marker.update(overrides)
    return marker


def make_project(session: Session, marker: dict, name: str = "Stagetop", cover_asset_id: Optional[str] = None) -> str:
    """One project row carrying `marker` and NO memberships.

    The 171 number in the review needs exactly this state: the project holds none
    of the folder's files while its marker records the 6 groups it was made with.
    That is the state the OLD "Add them" route left - it attached memberships and
    never refreshed `_bundle.groups` - and the ticket's "adopts 171 files and still
    renders its old groups" names it. It is deliberately NOT a create-then-grow
    history: no route ever refreshed the marker, so the marker is stale by
    construction, which is the state this workflow must repair.
    """
    project = Project(
        name=name,
        metadata_json=json.dumps({"_bundle": marker}),
        cover_asset_id=cover_asset_id,
        created_at=STAMP,
        updated_at=STAMP,
    )
    session.add(project)
    session.commit()
    return project.id


# --- reading the world -------------------------------------------------------


def plan_adopt(client: TestClient, project_id: str) -> dict:
    response = client.post("/operations/plan", json={"workflow": "adopt", "target": project_id})
    assert response.status_code == 200, response.text
    return response.json()


def run_workflow(client: TestClient, workflow: str, target: str, **params) -> dict:
    """Plan, submit and run one operation; the caller reads the finished body."""
    planned = client.post(
        "/operations/plan", json={"workflow": workflow, "target": target, "params": params}
    )
    assert planned.status_code == 200, planned.text
    submitted = submit_of(client, planned.json())
    run_to_completion()
    body = operation_of(client, submitted["id"])
    assert body["status"] == "done", body
    return body


def refused_plan(client: TestClient, workflow: str, target: str) -> tuple[int, str]:
    """The status and the sentence a plan refuses with; the caller asserts both."""
    response = client.post("/operations/plan", json={"workflow": workflow, "target": target})
    detail = response.json().get("detail")
    assert isinstance(detail, str), response.text
    return response.status_code, detail


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
    """Dispatch one pass and join the pool; the runner is a thread pool, not a thread."""
    operations_runner.dispatch_once()
    operations_runner.wait_idle(timeout=15)


def step_of(operation: dict, op: str) -> dict:
    return next(step for step in operation["steps"] if step["op"] == op)


def members(project_id: str) -> set[str]:
    with Session(engine) as session:
        return set(
            session.exec(select(ProjectAsset.asset_id).where(ProjectAsset.project_id == project_id))
        )


def marker(project_id: str) -> dict:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        assert project is not None
        return project_service.bundle_marker_of(project.metadata_json)


def registered_ids() -> set[str]:
    """Every registered asset id under the fixture key, read through one session."""
    with Session(engine) as session:
        return set(session.exec(select(Asset.id).where(Asset.storage_path.is_not(None))))


def first_asset_ids(count: int) -> list[str]:
    """The first `count` registered ids in path order, for the manual attach route."""
    with Session(engine) as session:
        return list(
            session.exec(
                select(Asset.id).where(Asset.storage_path.is_not(None)).order_by(Asset.storage_path).limit(count)
            )
        )


def audit_rows(action: str, target_id: str) -> list[AuditEvent]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(AuditEvent)
                .where(AuditEvent.action == action, AuditEvent.target == target_id)
                .order_by(AuditEvent.created_at)
            )
        )


def operation_rows() -> int:
    with Session(engine) as session:
        return len(session.exec(select(Operation)).all())


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def sign_in(client: TestClient, token: str, username: str, password: str, role: str) -> str:
    """Create `username` with `role` through the real route and return its bearer token."""
    created = client.post(
        "/users",
        json={"username": username, "password": password, "role": role},
        headers=bearer(token),
    )
    assert created.status_code == 201, created.text
    logged_in = client.post("/login", json={"username": username, "password": password})
    assert logged_in.status_code == 200, logged_in.text
    return logged_in.json()["token"]


def seed_stagetop(storage: Path) -> str:
    """Register the 9-group tree and make the stale 6-group project the old door left."""
    scan_and_seed(storage)
    with Session(engine) as session:
        return make_project(session, marker_of(BEFORE_GROUPS))


def run_adopt(client: TestClient, project_id: str) -> dict:
    """The AC-2 state, one submit, one run; every later test starts from this."""
    plan = plan_adopt(client, project_id)
    submitted = submit_of(client, plan)
    run_to_completion()
    body = operation_of(client, submitted["id"])
    assert body["status"] == "done", body
    return body


# --- 1: the workflow is a registry entry, not a client's step list -----------


def test_adopt_is_a_registry_workflow_whose_steps_a_client_cannot_author(
    client: TestClient, storage: Path
) -> None:
    """AC-1. Mutation: keeping `adopt` a declaration, or accepting a plan the client assembled.

    `WORKFLOWS` is the set of workflows that actually RUN, so a declaration-only
    entry here fails; the three steps are the composition's own, and none of them
    carries `args` on the wire, because `args` stay server-side. The submit route
    still answers 202 for a body with only the plan id and a key, and refuses a
    body that tries to hand in a step list - `extra="forbid"` is what makes that a
    422 rather than a silently ignored field.
    """
    project_id = seed_stagetop(storage)

    # Seven, since Mf-y2u3 landed `library-scan` and then the two ingest doors:
    # `WORKFLOWS` is the set that RUNS, and a workflow added here without a planner
    # is caught by its own suite.
    assert set(operations_service.WORKFLOWS) == {
        "create-project",
        "split-and-create",
        "adopt",
        "extract-and-create",
        "library-scan",
        "import-link",
        "import-zip",
    }

    plan = plan_adopt(client, project_id)
    assert [step["op"] for step in plan["steps"]] == ["adopt", "refresh-groups", "verify-members"]
    for step in plan["steps"]:
        assert set(step) == {"index", "op", "summary"}, "args stay server-side"
    assert plan["review"]["workflow"] == "adopt"

    submitted = submit_of(client, plan)
    assert submitted["workflow"] == "adopt"
    assert submitted["status"] == "queued"

    refused = client.post(
        "/operations",
        json={
            "plan_id": plan["plan_id"],
            "idempotency_key": "smuggle",
            "steps": [{"index": 0, "op": "adopt", "summary": "adopt everything"}],
        },
    )
    assert refused.status_code == 422, refused.text


# --- 2: the Stagetop shape ---------------------------------------------------


def test_the_stagetop_shape_is_171_files_in_9_groups(client: TestClient, storage: Path) -> None:
    """AC-2. Mutation: reviewing the marker's stale 6 groups, or counting 6 * 19.

    The project holds NONE of the folder's 171 files while its marker records the
    6 groups it was made with, and the disk has grown to 9. The review must count
    the folder, not the marker: 171 members in 9 groups, all models, nothing moved,
    created or deleted, no exclusions. The run then proves the same shape from the
    other side - `added == 171`, and the measurement sees 6 groups before and 9
    after, with the three new names present and disjoint from the six old ones.
    """
    project_id = seed_stagetop(storage)
    assert members(project_id) == set(), "the project holds none of the folder's files"

    plan = plan_adopt(client, project_id)
    review = plan["review"]

    assert review["members"] == 171
    assert len(review["groups"]) == 9
    assert sum(group["members"] for group in review["groups"]) == 171
    assert review["by_role"] == {"model": 171, "image": 0, "text": 0, "archive": 0, "other": 0}
    assert review["adopts"] == 171
    assert review["moves"] == review["creates"] == review["deletes"] == 0
    assert review["excluded"] == []

    submitted = submit_of(client, plan)
    run_to_completion()
    body = operation_of(client, submitted["id"])
    assert body["status"] == "done", body

    adopted = step_of(body, "adopt")["result"]
    assert len(adopted["groups_before"]) == 6
    assert len(adopted["groups_after"]) == 9
    after_names = {group["name"] for group in adopted["groups_after"]}
    before_names = {group["name"] for group in adopted["groups_before"]}
    assert set(NEW_GROUPS) <= after_names
    assert set(NEW_GROUPS).isdisjoint(before_names)
    assert adopted["added"] == 171


# --- 3: every exclusion is named ---------------------------------------------


def test_every_exclusion_is_named_with_its_owners_reason(client: TestClient, storage: Path) -> None:
    """AC-3. Mutation: dropping an excluded file silently, or defaulting `included` back to True.

    Four kinds of exclusion, one per owner: a group that is already its own
    project, a file another project already holds, a file the last scan flagged
    missing, and a group the operator excluded at create time. Each reason is the
    sentence its owner wrote, read verbatim; and every excluded path's asset ids
    are absent from the adopt step's `member_ids`, so a review that named a file
    the run would also attach fails. The refresh must CARRY `included: False` for
    "Wave 4" by name, or an operator's exclusion is resurrected by every adopt.
    """
    scan_and_seed(storage)
    seed = marker_of(GROUPS)
    for group in seed["groups"]:
        if group["name"] == "Wave 4":
            group["included"] = False
    with Session(engine) as session:
        project_id = make_project(session, seed)

        # (a) the nested group is already its own project.
        make_project(session, {"key": f"{KEY}/Community Wave"}, name="Community Wave")

        # (b) one file of another group already belongs to another project.
        other = Project(name="Other Build", metadata_json="{}", created_at=STAMP, updated_at=STAMP)
        session.add(other)
        session.commit()
        claimed = session.exec(
            select(Asset).where(Asset.storage_path == f"{KEY}/PRUSA USERS ONLY/part-0.stl")
        ).one()
        session.add(ProjectAsset(project_id=other.id, asset_id=claimed.id, added_at=STAMP))

        # (c) one file of a third group is gone as far as the last scan is concerned.
        missing = session.exec(
            select(Asset).where(Asset.storage_path == f"{KEY}/StageTop 2 Ready Pack/part-0.stl")
        ).one()
        missing.missing_since = STAMP
        session.add(missing)
        session.commit()

    plan = plan_adopt(client, project_id)
    review = plan["review"]

    reasons = [entry["reason"] for entry in review["excluded"]]
    assert "this directory is already its own project" in reasons
    assert "already a member of project 'Other Build'" in reasons
    assert "the file is missing from disk (flagged by the last scan)" in reasons
    assert "this group is excluded from the project" in reasons

    # The step's own member list, read through `recompute_plan` because `args`
    # never reach the wire. Every excluded path's ids are absent from it.
    replay = operations_service.recompute_plan(plan["plan_id"])
    member_ids = set(replay.steps[0]["args"]["member_ids"])
    with Session(engine) as session:
        assets = list(session.exec(select(Asset).where(Asset.storage_path.is_not(None))))

    def ids_at(path: str) -> set[str]:
        return {
            asset.id
            for asset in assets
            if asset.storage_path == path or asset.storage_path.startswith(path + "/")
        }

    for entry in review["excluded"]:
        assert not (ids_at(entry["path"]) & member_ids), entry["path"]

    excluded_ids = registered_ids() - member_ids
    assert review["members"] == 171 - len(excluded_ids) == len(member_ids)

    submitted = submit_of(client, plan)
    run_to_completion()
    body = operation_of(client, submitted["id"])
    assert body["status"] == "done", body

    wave_four = next(group for group in marker(project_id)["groups"] if group["name"] == "Wave 4")
    assert wave_four["included"] is False, "the refresh carries the operator's flag, it does not default it"


# --- 4: the disk-minus-registered refusal ------------------------------------


def test_a_partially_unregistered_folder_refuses_and_names_the_scan(
    client: TestClient, storage: Path
) -> None:
    """AC-4. Mutation: adopting past a file on disk with no row, leaving it out silently.

    One extra `.stl` under a group with no rescan means the registered set is
    behind the disk. An adopt planned now would attach 171 files and leave the
    172nd out forever - the next plan would even call the project complete - so
    the refusal names the exact condition and the fix, and no `Operation` row is
    written: the ledger never hears about a plan that cannot be honest.
    """
    project_id = seed_stagetop(storage)
    write(storage, f"{KEY}/Wave 3/part-19.stl")

    status, detail = refused_plan(client, "adopt", project_id)

    assert status == 409
    assert "on disk but not in the library" in detail
    assert "rescan the library" in detail
    assert operation_rows() == 0
    assert client.get("/operations").json() == []


# --- 5: one submit runs it ---------------------------------------------------


def test_one_submit_runs_it_and_refreshes_the_marker(client: TestClient, storage: Path) -> None:
    """AC-5. Mutation: writing the reviewed wire shape into the marker instead of the engine's measurement.

    The three steps run in index order and all finish. The adopt step reports
    171 added; the refresh reports the project id, 9 groups and its own read-back
    verification. The marker then carries the group list a LIVE
    `measure` + `wire_groups` produces in the same name order - not the
    hand-written snapshot the review's census was built from - while `key`,
    `detector` and `shape` stay byte-identical to before. A marker rewritten from
    the review's own shape, or one whose identity keys moved, fails.
    """
    project_id = seed_stagetop(storage)
    before = marker(project_id)
    assert members(project_id) == set()

    submitted = submit_of(client, plan_adopt(client, project_id))
    run_to_completion()
    body = operation_of(client, submitted["id"])

    assert body["status"] == "done", body
    assert [step["status"] for step in body["steps"]] == ["done", "done", "done"]
    assert [step["index"] for step in body["steps"]] == [0, 1, 2]
    assert step_of(body, "adopt")["result"]["added"] == 171
    refresh = step_of(body, "refresh-groups")["result"]
    assert sorted(refresh) == ["groups", "project_id", "verified"]
    assert refresh == {"project_id": project_id, "groups": 9, "verified": True}

    assert len(members(project_id)) == 171

    after = marker(project_id)
    assert len(after["groups"]) == 9
    wire = bundle_service.wire_groups(proposal_engine.measure(str(storage / KEY), KEY).groups)
    assert [group["name"] for group in after["groups"]] == [group["name"] for group in wire]
    wire_by_name = {group["name"]: group for group in wire}
    for entry in after["groups"]:
        fresh = wire_by_name[entry["name"]]
        assert (entry["name"], entry["model_count"], entry["foreign"], entry["separable"]) == (
            fresh["name"],
            fresh["model_count"],
            fresh["foreign"],
            fresh["separable"],
        )
    assert after["key"] == before["key"]
    assert after["detector"] == before["detector"]
    assert after["shape"] == before["shape"]
    assert body["undo_state"] == "available"


# --- 6: undo ------------------------------------------------------------------


def test_undo_restores_memberships_and_groups_byte_for_byte(client: TestClient, storage: Path) -> None:
    """AC-6. Mutation: undo leaving a membership, or a marker group list that is merely equal.

    The entry is the ledger: undo takes back exactly the 171 memberships it
    recorded, restoring the pre-adopt set (empty) exactly, and puts the group
    snapshot back verbatim - compared as canonical JSON, so a list that is
    "morally" equal but reordered or re-typed fails. Every other `_bundle` key is
    unchanged, `undo_state` flips to `undone`, exactly ONE `operation.undo` audit
    row exists for the operation, and a second undo is the shared 409.
    """
    project_id = seed_stagetop(storage)
    before = marker(project_id)
    before_members = members(project_id)
    before_groups_dump = json.dumps(before["groups"], sort_keys=True)

    submitted = submit_of(client, plan_adopt(client, project_id))
    run_to_completion()
    assert operation_of(client, submitted["id"])["status"] == "done"

    undone = client.post(f"/operations/{submitted['id']}/undo")

    assert undone.status_code == 200, undone.text
    body = undone.json()
    assert body["members_removed"] == 171
    assert body["undone"] is True
    assert members(project_id) == before_members
    assert json.dumps(marker(project_id)["groups"], sort_keys=True) == before_groups_dump
    after = marker(project_id)
    for key, value in before.items():
        if key == "groups":
            continue
        assert after[key] == value, key
    assert operation_of(client, submitted["id"])["undo_state"] == "undone"
    assert len(audit_rows(audit.OPERATION_UNDO, submitted["id"])) == 1

    again = client.post(f"/operations/{submitted['id']}/undo")
    assert again.status_code == 409
    assert again.json()["detail"] == "the operation has already been undone"


# --- 7: supersede ------------------------------------------------------------


def test_undo_lasts_until_a_later_operation_over_the_same_root_supersedes_it(
    client: TestClient, storage: Path
) -> None:
    """AC-7. Mutation: a later operation over the same root leaving the earlier undo `available`.

    The adopt's undo is a promise about the folder as the adopt left it. A later
    create-project replay over the same key supersedes it when that operation
    finishes; the adopt then reports `undo_state == "superseded"` and
    `superseded_by` names the later id, and its undo is the shared 409 whose
    detail carries both, with the reason naming the id. The project's cover is
    seeded first so the replay's own verify-cover step passes - that is a real
    operation, not a mocked one.
    """
    project_id = seed_stagetop(storage)
    adopt = run_adopt(client, project_id)

    with Session(engine) as session:
        registered = bundle_service.registered_assets(session, KEY)
        project = session.get(Project, project_id)
        assert project is not None
        project.cover_asset_id = bundle_service.cover_for_registered(registered, None)["asset_id"]
        session.add(project)
        session.commit()

    later = run_workflow(client, "create-project", KEY)

    adopt_after = operation_of(client, adopt["id"])
    assert adopt_after["undo_state"] == "superseded"
    assert adopt_after["superseded_by"] == later["id"]

    refused = client.post(f"/operations/{adopt['id']}/undo")
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["superseded_by"] == later["id"]
    assert detail["undo_state"] == "superseded"
    assert later["id"] in detail["reason"]


# --- 8: who may adopt --------------------------------------------------------


def test_a_contributor_adopts_and_a_viewer_is_refused(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-8. Mutation: classifying adopt at WRITE, or letting a viewer plan and submit it.

    The owner's rubric is "can they REMOVE data with this action": adopt only
    writes memberships and the marker, so it is CONTRIBUTE. A real contributor
    session plans, submits, runs and undoes it; the viewer is refused at both the
    plan and the submit with the permissions module's own sentence, exactly.
    """
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    project_id = seed_stagetop(storage)
    admin = client.post("/login", json=BREAK_GLASS)
    assert admin.status_code == 200, admin.text
    admin_token = sign_in(client, admin.json()["token"], ADMIN_USERNAME, ADMIN_PASSWORD, "admin")
    contributor = sign_in(client, admin_token, CONTRIBUTOR_USERNAME, CONTRIBUTOR_PASSWORD, "contributor")
    viewer = sign_in(client, admin_token, VIEWER_USERNAME, VIEWER_PASSWORD, "viewer")

    assert operations_service.WORKFLOW_LEVELS["adopt"] == permissions.CONTRIBUTE

    planned = client.post(
        "/operations/plan",
        json={"workflow": "adopt", "target": project_id},
        headers=bearer(contributor),
    )
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    submitted = client.post(
        "/operations",
        json={"plan_id": plan["plan_id"], "idempotency_key": "contributor"},
        headers=bearer(contributor),
    )
    assert submitted.status_code == 202, submitted.text
    run_to_completion()
    operation_id = submitted.json()["id"]
    assert client.get(f"/operations/{operation_id}", headers=bearer(contributor)).json()["status"] == "done"
    assert client.post(f"/operations/{operation_id}/undo", headers=bearer(contributor)).status_code == 200

    denied = permissions.DENY_DETAIL[permissions.CONTRIBUTE]
    for response in (
        client.post(
            "/operations/plan",
            json={"workflow": "adopt", "target": project_id},
            headers=bearer(viewer),
        ),
        client.post(
            "/operations",
            json={"plan_id": plan["plan_id"], "idempotency_key": "viewer"},
            headers=bearer(viewer),
        ),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == denied


# --- 9: idempotency and the completed project --------------------------------


def test_a_second_submit_is_idempotent_and_a_fresh_plan_after_the_run_refuses(
    client: TestClient, storage: Path
) -> None:
    """AC-9. Mutation: a second row for one idempotency key, or a fresh plan that re-adopts everything.

    The same key and the same plan answer the same operation id and leave exactly
    one `Operation` row; the membership count is what one run wrote. Once the
    project holds every file, planning again is the planner's own 409 - "already
    holds every file" - and it writes no row: a refusal is a refusal, not a
    queued no-op.
    """
    project_id = seed_stagetop(storage)
    plan = plan_adopt(client, project_id)

    first = submit_of(client, plan, "same-key")
    second = submit_of(client, plan, "same-key")

    assert first["id"] == second["id"]
    with Session(engine) as session:
        rows = list(session.exec(select(Operation).where(Operation.idempotency_key == "same-key")))
    assert len(rows) == 1

    run_to_completion()
    assert operation_of(client, first["id"])["status"] == "done"
    assert len(members(project_id)) == 171
    rows_before = operation_rows()

    status, detail = refused_plan(client, "adopt", project_id)
    assert status == 409
    assert "already holds every file" in detail
    assert operation_rows() == rows_before


# --- 10: a marker with no walk root ------------------------------------------


def test_a_marker_with_no_walk_root_is_refused_and_the_notice_agrees(
    client: TestClient,
) -> None:
    """AC-10. Mutation: walking an identity key as a path, or making the refusal disagree with the notice.

    A zip-project's key is an identity, not a path, and the marker carries a
    `source`; with no `rel_dir` there is nothing to walk. The drift notice says
    exactly that - `unknown`, a null count, no ids - and the plan refuses naming
    the marker's own key, which is the operator's handle for the row. A planner
    that invented a zero, or a notice that claimed `known`, fails.
    """
    identity_marker = {"key": "zip-project:https://example.invalid/x", "source": "https://example.invalid/x"}
    with Session(engine) as session:
        project_id = make_project(session, dict(identity_marker), name="Zip Project")

    notice = client.get(f"/project/{project_id}/bundle-drift")
    assert notice.status_code == 200, notice.text
    assert notice.json() == {"state": "unknown", "count": None, "asset_ids": []}

    status, detail = refused_plan(client, "adopt", project_id)
    assert status == 409
    assert "zip-project:https://example.invalid/x" in detail
    assert operation_rows() == 0


# --- 11: the manual attach route is unchanged --------------------------------


def test_the_manual_attach_route_is_unchanged_and_writes_no_groups(
    client: TestClient, storage: Path
) -> None:
    """AC-11. Mutation: "fixing" the manual route, or claiming adopt replaced it.

    The old route still attaches memberships and still does NOT refresh
    `_bundle.groups` - the marker is byte-identical afterwards. That is the defect
    `adopt` exists to replace, kept here as a witness: the web project page's
    attach control is a UI concern and is NOT asserted by this API test, but the
    route it calls must keep behaving exactly as it did.
    """
    project_id = seed_stagetop(storage)
    before = json.dumps(marker(project_id), sort_keys=True)
    ids = first_asset_ids(3)

    attached = client.post(f"/project/{project_id}/assets", json={"asset_ids": ids})

    assert attached.status_code == 200, attached.text
    body = attached.json()
    assert set(body) == {"added", "skipped", "failed"}
    assert body["added"] == ids
    assert body["skipped"] == []
    assert body["failed"] == []
    assert len(members(project_id)) == 3
    assert json.dumps(marker(project_id), sort_keys=True) == before


# --- 12: refusals write no row ----------------------------------------------


def test_every_refusal_happens_before_a_row_exists(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-12. Mutation: inserting the `Operation` row before the planner has refused.

    Four refusals, each asserted with its own sentence AND a zero `Operation`
    count: an unknown project id, a project with no bundle marker, a marker whose
    walk root holds no registered row, and a library scan in flight. The ledger
    only ever hears about a plan that can run.
    """
    scan_and_seed(storage)

    status, detail = refused_plan(client, "adopt", "no-such-project")
    assert status == 409
    assert "unknown project" in detail
    assert operation_rows() == 0

    with Session(engine) as session:
        plain = Project(name="Hand Made", metadata_json="{}", created_at=STAMP, updated_at=STAMP)
        session.add(plain)
        session.commit()
        plain_id = plain.id
    status, detail = refused_plan(client, "adopt", plain_id)
    assert status == 409
    assert "has no bundle marker" in detail
    assert operation_rows() == 0

    with Session(engine) as session:
        stale_id = make_project(
            session,
            {"key": KEY, "detector": DETECTOR, "rel_dir": "import/Nothing Here"},
            name="Stale",
        )
    status, detail = refused_plan(client, "adopt", stale_id)
    assert status == 409
    assert "names no folder this library still holds" in detail
    assert operation_rows() == 0

    with Session(engine) as session:
        live_id = make_project(session, marker_of(BEFORE_GROUPS))
    monkeypatch.setattr(seams, "scan_status", lambda: {"running": True})
    status, detail = refused_plan(client, "adopt", live_id)
    assert status == 409
    assert "a library scan is running" in detail
    assert operation_rows() == 0


# --- 13: the honest grown project -------------------------------------------


def test_the_grown_project_adopts_only_the_delta(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-13. Mutation: an adopt over a project that grew that re-attaches, and re-counts, everything.

    The honest story, built the real way: 6 groups on disk, one `create-project`
    through the routes makes the project over all 114 files, then the owner writes
    the last 3 groups and rescans. The review is the DELTA - 3 * 19 - and after the
    run the project holds all 171 with `already_members == 0`; an adopt that
    planned the whole folder would review 171 and attach 57 again.

    `groups_before` is `None` here, and that is the measured truth of THIS route:
    the `create-project` step's args carry no `groups` (its marker is the identity
    pair plus the detector), so the project had no `groups` key before the adopt
    and the measurement correctly records a `None` - which `undo` reads as "remove
    the key". The 6-before/9-after shape is AC-2's marker, which DOES record the
    6 groups because the hand-written seed is the state the old attach route left;
    asserting 6 here would require changing the create-project workflow, which
    Mf-ytig does not do.
    """
    root = tmp_path / "storage"
    point_storage(monkeypatch, root)
    write_tree(root, BEFORE_GROUPS)
    scan_and_seed(root)

    created = run_workflow(client, "create-project", KEY)
    step = step_of(created, "create-project")
    project_id = step["result"]["project_id"]
    assert len(members(project_id)) == 6 * PER_GROUP

    write_tree(root, NEW_GROUPS)
    scan_and_seed(root)

    plan = plan_adopt(client, project_id)
    assert plan["review"]["members"] == 3 * PER_GROUP

    submitted = submit_of(client, plan)
    run_to_completion()
    body = operation_of(client, submitted["id"])
    assert body["status"] == "done", body

    adopted = step_of(body, "adopt")["result"]
    assert adopted["groups_before"] is None, "the create-project door records no `groups` to measure before"
    assert len(adopted["groups_after"]) == 9
    assert adopted["already_members"] == 0
    assert len(members(project_id)) == 171
