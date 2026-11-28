"""split-and-create as a composition (Mf-g759).

The arrange is gone: `GET/POST /bundles/arrange` and its undo were deleted, and
the one workflow they enacted - hoist a container's product folders out of it and
write one project per product - is now `split-and-create`, a COMPOSITION of
`proposal_engine`'s moves, `move_service`'s pair questions, `move_service`'s move
and `bundle_service.create_project`, driven by the operations ledger.

Why this file exists: a composition is only worth calling one if each half's hard
promises survive the join. So every demonstration below is written so that a test
which would ALSO pass with the feature absent is not accepted:

* **the plan is both halves** - three `move_list` entries, three `projects`, and
  a named `excluded[]` for every registered file the split would leave behind; a
  review that dropped either list, or the loose file, fails.
* **one submit executes it** - the move step's `landed`/`ledger_id`, the three
  project rows and their members' post-move paths are read off the wire and the
  database; a run that never moved anything fails.
* **the step order is what makes the refusal structural** - a guard inside the
  first `create-project` step sees the hoisted tree already on disk and the
  container already gone, and a guard that occupies a hoist target makes the
  operation fail at step 0 with no project row; a creates-first implementation
  reds both.
* **undo returns the library to before the plan** - the tree digest, every asset
  path and the whole `folder` row set (ids and parents included) are compared
  against a pre-plan snapshot, and the operation's own projects go.
* **the deliberate inversions** - the arrange kept its project and left a
  membership it did not add; this workflow deletes the projects it created and
  takes such a membership with the project, because the operation's step rows are
  the record and the projects did not exist before the plan (tests 5 and 6).
* **refusals, each in the primitive's own words** - the shape, a product that is
  not a container, an unknown key, the library root, an archive, the declared
  cap, a running scan, a project already covering a hoist target; plus the stale
  plan and the replay.
* **the routes are gone everywhere** - no route, no OpenAPI path or component, no
  permission row, no audit action, and the ticket's grep finds the names only in
  prose.

Where every deleted arrange test's subject now lives - the "no refusal case
vanishes silently" evidence:

===============================  ============================================
deleted arrange test             its subject now lives in
===============================  ============================================
`test_a_wrapper_child_is_...`    one-project/wrapper shapes are
                                 `proposal_engine`'s: `test_proposal_engine*`
`test_a_grown_project_extends..` adopt: `test_adopt_service.py`
`test_the_adoption_preview_...`  adopt: `test_adopt_service.py`
`test_the_plan_tells_an_adopt..` adopt vs create vs move:
                                 `test_adopt_service.py` + `test_proposal_engine*`
`test_an_unregistered_growth_..` the refusal died with the arrange and is NOT
                                 re-homed: adopt only ever attaches registered
                                 rows (`test_adopt_service.py`), and the SPLIT
                                 drops it deliberately, named in test 12 below
`test_an_adoption_larger_than..` adopt: `test_adopt_service.py`
`test_a_split_hoists_every_...`  tests 1, 2, 3 and 5 here
`test_an_archive_beside_its_...` one-project/wrapper: `test_proposal_engine*`
`test_the_series_rename_keeps..` series layout: `test_proposal_engine*`
`test_a_destination_that_...`    `preflight_refusals` in `test_move_service.py`
                                 (`destination_exists`) + test 4 here
`test_a_cross_device_move_...`   `preflight_refusals` (`cross_device`)
`test_a_hidden_destination_...`  `preflight_refusals` (`hidden_destination`)
`test_a_destination_outside_..`  `preflight_refusals` (`outside_root`)
`test_a_missing_source_...`      `preflight_refusals` (`missing_source`)
`test_a_pinned_folder_row_...`   `preflight_refusals` (`pinned_row`)
`test_too_many_moves_is_...`     test 9 here (`MAX_SPLIT_MOVES`)
`test_a_stale_plan_id_is_...`    test 8 here
`test_a_running_scan_is_...`     test 9 here
`test_an_archive_row_is_not_...` test 9 here
`test_the_wire_says_from_and..`  test 15 here (`OperationMoveOut`)
`test_a_viewer_reads_the_...`    test 13 here (contributor splits, viewer 403)
`test_undo_restores_the_tree..`  test 5 here
`test_a_second_undo_is_a_409`    test 5 here
`test_undo_leaves_a_membership`  DELIBERATELY INVERTED by test 6 here
`test_a_partial_apply_is_...`    the landed prefix is `move_service.undo_ledger`'s
                                 own: `test_move_service.py`
                                 (`test_a_clobbered_pair_in_a_batch_is_refused...`
                                 and `test_undo_replays_the_ledger_backwards...`);
                                 the runner never leaves a half-applied step, so
                                 there is no workflow-level replay to test
`test_a_partial_apply_finishes`  same place: an incomplete intent resumes
                                 (`test_a_batch_resumes_after_a_clobber...`)
`test_an_ambiguous_fold_refuses` `test_move_service.py`
                                 (`test_undo_refuses_when_a_path_is_present_at_both_ends`)
`test_a_claim_mid_fold_is_...`   `test_move_service.py`
                                 (`test_undo_refuses_a_target_claimed_after_...`)
===============================  ============================================
"""

import errno
import hashlib
import io
import json
import re
import tokenize
from pathlib import Path
from typing import Optional
from uuid import uuid4
from zipfile import ZipFile

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
import move_service
import operations_runner
import operations_service
import permissions
import proposal_engine
from db import engine
from migrations import run_migrations
from models import (
    Asset,
    AssetFile,
    AuditEvent,
    Folder,
    MfaRecoveryCode,
    MoveLedger,
    Operation,
    OperationLock,
    OperationStep,
    Project,
    ProjectAsset,
    User,
    UserSession,
)

POOL = "OutOfMarbles"
CONTAINER = f"import/{POOL}"
STL_BYTES = b"solid cube\nendsolid cube\n"

BREAK_GLASS = {"username": "admin", "password": "super-secret"}
ADMIN_USERNAME = "root"
ADMIN_PASSWORD = "root-password-1"
CONTRIBUTOR_USERNAME = "contributor"
CONTRIBUTOR_PASSWORD = "contributor-password-1"
VIEWER_USERNAME = "viewer"
VIEWER_PASSWORD = "viewer-password-1"


def _tree(with_loose: bool = False) -> dict[str, bytes]:
    tree = {
        f"{CONTAINER}/Alpha/alpha.stl": b"solid alpha\nendsolid alpha\n",
        f"{CONTAINER}/Beta/beta.stl": b"solid beta\nendsolid beta\n",
        f"{CONTAINER}/Gamma/gamma.stl": b"solid gamma\nendsolid gamma\n",
    }
    if with_loose:
        tree[f"{CONTAINER}/loose.png"] = b"\x89PNG\r\n\x1a\n" + bytes(8)
    return tree


# --- harness -----------------------------------------------------------------


def truncate_everything() -> None:
    """An empty ledger, an empty library and no accounts: the test that reads made it."""
    with Session(engine) as session:
        for model in (
            OperationStep,
            OperationLock,
            Operation,
            MoveLedger,
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
    the next seed's name and a leftover operation would turn the next submit's 202
    into a 409 (the idempotency key is unique). Auth is off by default because
    only test 13 is about *who* may split; it turns auth back on for itself.
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
    """Argon2's floor parameters: only test 13 signs accounts in, and it signs three."""
    monkeypatch.setattr(accounts, "hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))


@pytest.fixture()
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp storage root holding the real files the plan's rows name."""
    root = tmp_path / "storage"
    for relative, body in _tree().items():
        write(root, relative, body)
    monkeypatch.setattr(db, "STORAGE", root)
    monkeypatch.setattr(bundle_service, "STORAGE", root)
    monkeypatch.setattr(library_scan, "STORAGE", root)
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


# --- reading the world -------------------------------------------------------


def plan_of(client: TestClient, target: str = CONTAINER, **params) -> dict:
    response = client.post(
        "/operations/plan",
        json={"workflow": "split-and-create", "target": target, "params": params},
    )
    assert response.status_code == 200, response.text
    return response.json()


def refused_plan(client: TestClient, target: str, **params) -> tuple[int, str]:
    """The status and the sentence a plan refuses with; the caller asserts both."""
    response = client.post(
        "/operations/plan",
        json={"workflow": "split-and-create", "target": target, "params": params},
    )
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


def run_split(client: TestClient, plan: Optional[dict] = None, key: Optional[str] = None) -> dict:
    """Submit `plan` (or a fresh one), run it and read the finished operation back."""
    submitted = submit_of(client, plan or plan_of(client), key)
    run_to_completion()
    body = operation_of(client, submitted["id"])
    assert body["status"] == "done", body
    return body


def tree_digest(root: Path, ignore: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """Every file's relative path and sha256, sorted; "nothing moved" as one comparison.

    `ignore` names files the TEST wrote after the snapshot - a refusal guard's own
    blocker - so the comparison stays about the library's bytes.
    """
    digest: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in ignore:
            continue
        digest.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    return digest


def folder_rows() -> list[tuple]:
    """Every folder row as the whole row that must survive an undo, ordered by id."""
    with Session(engine) as session:
        return sorted(
            (
                folder.id,
                folder.parent_id,
                folder.name,
                folder.storage_path,
                folder.tags_json,
            )
            for folder in session.exec(select(Folder))
        )


def asset_paths() -> dict[str, str]:
    with Session(engine) as session:
        return {asset.id: asset.storage_path or "" for asset in session.exec(select(Asset))}


def project_rows() -> list[Project]:
    with Session(engine) as session:
        return list(session.exec(select(Project)))


def bundle_key_of(project: Project) -> Optional[str]:
    metadata = json.loads(project.metadata_json or "{}")
    return (metadata.get("_bundle") or {}).get("key")


def membership_paths(project_id: str) -> list[str]:
    """The storage paths of one project's memberships, sorted."""
    with Session(engine) as session:
        asset_ids = list(
            session.exec(select(ProjectAsset.asset_id).where(ProjectAsset.project_id == project_id))
        )
        paths = [
            session.get(Asset, asset_id).storage_path or ""
            for asset_id in asset_ids
            if session.get(Asset, asset_id) is not None
        ]
    return sorted(paths)


def audit_rows(action: str, target: str) -> list[AuditEvent]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(AuditEvent)
                .where(AuditEvent.action == action, AuditEvent.target == target)
                .order_by(AuditEvent.created_at)
            )
        )


def create_project_row(name: str, key: str) -> str:
    """One project whose `_bundle` marker names `key`, as the detection door writes it."""
    project_id = str(uuid4())
    with Session(engine) as session:
        session.add(
            Project(
                id=project_id,
                name=name,
                metadata_json=json.dumps({"_bundle": {"key": key, "origin": "detection"}}),
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            )
        )
        session.commit()
    return project_id


# --- 1: the plan is both halves ----------------------------------------------


def test_the_out_of_marbles_plan_lists_three_hoists_three_projects_and_what_it_leaves_out(
    client: TestClient, storage: Path
) -> None:
    """Mutation: a review that shows the moves OR the projects, or drops `excluded[]`.

    The loose `loose.png` is why the fixture has one: without a file the split
    leaves behind, a review could drop `excluded[]` entirely and still pass. The
    counts are the second half - `creates == 3`, `adopts == 0`, `members == 3` read
    the same tree the step list does, so a plan that offered a create per hoist but
    counted the run twice fails.
    """
    # The fixture's base tree plus the one file at the container's own root, which
    # no hoisted project can ever hold - which is why the review must name it.
    for relative, body in _tree(with_loose=True).items():
        write(storage, relative, body)
    scan_and_seed(storage)

    plan = plan_of(client)

    assert [step["op"] for step in plan["steps"]] == [
        "move",
        "create-project",
        "verify-members",
        "create-project",
        "verify-members",
        "create-project",
        "verify-members",
    ]
    assert [step["index"] for step in plan["steps"]] == list(range(7))
    assert all("args" not in step for step in plan["steps"]), "args stay server-side"

    review = plan["review"]
    assert review["move_list"] == [
        {
            "source": f"{CONTAINER}/Alpha",
            "target": "import/Alpha",
            "kind": "dir",
            "action": "hoist",
        },
        {
            "source": f"{CONTAINER}/Beta",
            "target": "import/Beta",
            "kind": "dir",
            "action": "hoist",
        },
        {
            "source": f"{CONTAINER}/Gamma",
            "target": "import/Gamma",
            "kind": "dir",
            "action": "hoist",
        },
    ]
    assert review["projects"] == [
        {"name": "Alpha", "root": "import/Alpha", "members": 1},
        {"name": "Beta", "root": "import/Beta", "members": 1},
        {"name": "Gamma", "root": "import/Gamma", "members": 1},
    ]
    assert review["creates"] == 3
    assert review["moves"] == 3
    assert review["adopts"] == 0
    assert review["members"] == 3

    # The container's own file is named, with the planner's reason, and it is the
    # ONLY exclusion: a review that invented one for a member fails here.
    assert [entry["path"] for entry in review["excluded"]] == [f"{CONTAINER}/loose.png"]
    reason = review["excluded"][0]["reason"]
    assert isinstance(reason, str) and reason, "an exclusion without a reason is a silent drop"
    assert "not inside one of the folders the split hoists" in reason


# --- 2: one submit executes both halves --------------------------------------


def test_one_submit_moves_the_folders_and_creates_the_projects(
    client: TestClient, storage: Path
) -> None:
    """AC-4. Mutation: a run that creates the projects but never moves the bytes.

    Every assertion below is read off the wire or the database after ONE submit:
    the move step's `landed`/`ledger_id`/`moves`, the three files at their new
    paths, the container directory gone, the three project rows' `_bundle` keys,
    and each membership's `Asset.storage_path` being the POST-move path - a project
    whose memberships still name the pre-move path would pass a count-only test.
    """
    scan_and_seed(storage)

    body = run_split(client)

    assert [step["status"] for step in body["steps"]] == ["done"] * 7
    assert all(step["result"] is not None for step in body["steps"])
    move_result = body["steps"][0]["result"]
    assert move_result["landed"] == 3
    assert isinstance(move_result["ledger_id"], str) and move_result["ledger_id"]
    assert move_result["moves"] == [
        [f"{CONTAINER}/Alpha", "import/Alpha", "dir"],
        [f"{CONTAINER}/Beta", "import/Beta", "dir"],
        [f"{CONTAINER}/Gamma", "import/Gamma", "dir"],
    ]

    for child, filename in (("Alpha", "alpha.stl"), ("Beta", "beta.stl"), ("Gamma", "gamma.stl")):
        assert (storage / "import" / child / filename).is_file()
    assert not (storage / CONTAINER).exists(), "the emptied container directory is pruned"

    projects = project_rows()
    assert sorted(project.name for project in projects) == ["Alpha", "Beta", "Gamma"]
    assert sorted(bundle_key_of(project) for project in projects) == [
        "import/Alpha",
        "import/Beta",
        "import/Gamma",
    ]
    assert {project.name: membership_paths(project.id) for project in projects} == {
        "Alpha": ["import/Alpha/alpha.stl"],
        "Beta": ["import/Beta/beta.stl"],
        "Gamma": ["import/Gamma/gamma.stl"],
    }


# --- 3: the move step runs first ---------------------------------------------


def test_the_move_step_runs_before_any_project_exists(client: TestClient, storage: Path) -> None:
    """AC-4a. Fails if creates run before the move: the guard would see the old tree.

    The guard fires inside the FIRST `create-project` step, so it observes exactly
    what the runner has done by then. If the planner emitted the creates first (the
    detected-bundle arrange's order), the hoisted folders would still be at
    `import/OutOfMarbles/Alpha` and the container would still exist - each
    assertion below would raise, the step would be marked failed, and this test
    would red on `fired`/`done`/`failed` instead of passing.
    """
    scan_and_seed(storage)
    plan = plan_of(client)
    fired: list[int] = []

    def guard(_operation, step) -> None:
        if step.op != "create-project" or fired:
            return
        fired.append(step.step_index)
        assert (storage / "import/Alpha").is_dir(), "the first hoist is on disk before the first create"
        assert (storage / "import/Beta/beta.stl").is_file()
        assert (storage / "import/Gamma/gamma.stl").is_file()
        assert not (storage / CONTAINER).exists(), "the container is already gone when the creates start"

    operations_runner.set_step_guard(guard)
    body = run_split(client, plan)

    assert fired == [1], "the guard fired inside the first create-project step"
    assert body["status"] == "done"
    assert [step["status"] for step in body["steps"]] == ["done"] * 7
    assert body["error"] is None


# --- 4: a refusal at the move step stops the whole workflow ------------------


def test_a_file_at_a_hoist_target_fails_the_operation_at_step_zero(
    client: TestClient, storage: Path
) -> None:
    """AC-4b. Mutation: creating the projects first, so a move refusal leaves three of them.

    The blocker is written by the guard, AFTER the plan was taken and reviewed, so
    this is the race the plan cannot see. The mover's batch pre-flight is what makes
    the refusal structural: the step fails before a byte moves, no project exists,
    and the operation's `undo_state` is `none` because nothing landed.
    """
    scan_and_seed(storage)
    plan = plan_of(client)
    blocker = "import/Beta/blocker.stl"
    before = tree_digest(storage)

    def guard(_operation, step) -> None:
        if step.step_index != 0:
            return
        write(storage, blocker, b"do not touch\n")

    operations_runner.set_step_guard(guard)
    submitted = submit_of(client, plan)
    run_to_completion()
    body = operation_of(client, submitted["id"])

    assert body["status"] == "failed"
    assert "import/Beta" in body["error"]
    assert [step["status"] for step in body["steps"]] == ["failed"] + ["pending"] * 6
    assert project_rows() == [], "a refusal at the move step creates no project"
    # The blocker is the test's own byte, so the digest that proves "nothing moved"
    # ignores exactly that one path.
    assert tree_digest(storage, ignore=(blocker,)) == before
    assert (storage / blocker).read_bytes() == b"do not touch\n", "the blocker was not clobbered"
    assert body["undo_state"] == "none"


def test_a_move_that_dies_mid_bytes_is_compensated_before_it_is_reported(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: a mid-bytes failure that leaves half a split on disk.

    The move step asks `move_service.move` WITHOUT a session, so the ledger row is
    durable before a byte moves and a disk that fills up half way has a record. The
    step must then COMPENSATE from that record before it re-raises: a failed step
    whose bytes are still half-hoisted is a library nobody asked for, and the
    operation reports `failed` with no project created.

    The injected failure is the second byte move - the first has already landed and
    pruned the container, so the reversal has real work to do - and the wrapper
    raises exactly once so the compensation's own reversal can run.
    """
    scan_and_seed(storage)
    plan = plan_of(client)
    before = tree_digest(storage)
    original = move_service.move_bytes
    calls: list[int] = []

    def failing_move_bytes(source: Path, target: Path) -> bool:
        calls.append(1)
        if len(calls) == 2:
            raise OSError(errno.EIO, "no space left on device")
        return original(source, target)

    monkeypatch.setattr(move_service, "move_bytes", failing_move_bytes)
    submitted = submit_of(client, plan)
    run_to_completion()
    monkeypatch.setattr(move_service, "move_bytes", original)

    body = operation_of(client, submitted["id"])
    assert body["status"] == "failed"
    assert "no space left on device" in body["error"], "the original failure is the one reported"
    assert [step["status"] for step in body["steps"]] == ["failed"] + ["pending"] * 6
    assert project_rows() == [], "a failed move creates no project"
    assert tree_digest(storage) == before, "the landed pair was folded back"
    assert not (storage / "import/Alpha").exists(), "the compensation pruned the directory it made"
    with Session(engine) as session:
        rows = list(
            session.exec(
                select(MoveLedger).where(
                    MoveLedger.intent == operations_service.move_intent(body["id"])
                )
            )
        )
    assert len(rows) == 1
    assert rows[0].undone == 1, "the compensation is the ledger's own undo, not a second move"
    assert rows[0].applied == 0


# --- 5: undo returns the library to before the plan --------------------------


def test_undo_restores_the_tree_and_every_row_and_deletes_only_its_own_projects(
    client: TestClient, storage: Path
) -> None:
    """AC-5, plus the deliberate inversion of the deleted arrange's undo.

    The arrange DELIBERATELY KEPT the project it created - "`Project` owns no
    bytes, and keeping it is what makes a second undo reachable", its own words.
    This workflow DELIBERATELY DELETES the projects it created: the operation's own
    step rows are the record rather than a marker on the project, and undo must
    return the library to before the plan - the projects did not exist before it.
    The second undo is therefore still a 409, answered by `undo_state`.

    Mutations this reds: an undo that folds the bytes and leaves the rows; one that
    rewrites the children onto a missing container row (the `parent_id` half of the
    folder comparison); one that deletes ANY project rather than only its own; one
    that forgets to stamp the move ledger; a second undo that answers 200.
    """
    scan_and_seed(storage)
    before_digest = tree_digest(storage)
    before_assets = asset_paths()
    before_folders = folder_rows()
    container_id = next(
        folder[0] for folder in before_folders if folder[3] == CONTAINER
    )

    body = run_split(client)

    assert all(folder[3] != CONTAINER for folder in folder_rows()), "the emptied container row is retired"
    assert not (storage / CONTAINER).exists()
    created = [step["result"]["project_id"] for step in body["steps"] if step["op"] == "create-project"]
    assert len(created) == 3

    undone = client.post(f"/operations/{body['id']}/undo")

    assert undone.status_code == 200, undone.text
    payload = undone.json()
    assert payload["undone"] is True
    assert payload["moves"] == 3
    assert sorted(payload["projects_removed"]) == sorted(created)
    assert payload["project_id"] is None and payload["project_name"] is None

    assert tree_digest(storage) == before_digest
    assert asset_paths() == before_assets
    assert folder_rows() == before_folders, "the row set, ids and parent lines included"
    assert any(folder[0] == container_id for folder in folder_rows()), "the container row came back with its own id"
    assert project_rows() == []
    with Session(engine) as session:
        assert list(session.exec(select(ProjectAsset))) == []
    with Session(engine) as session:
        ledgers = list(
            session.exec(
                select(MoveLedger).where(MoveLedger.intent == operations_service.move_intent(body["id"]))
            )
        )
    assert len(ledgers) == 1
    assert ledgers[0].undone == 1, "the fold's own record says it happened"

    rows = audit_rows(audit.OPERATION_UNDO, body["id"])
    assert len(rows) == 1
    detail = json.loads(rows[0].detail_json)
    assert detail["moves"] == 3
    assert sorted(detail["projects_removed"]) == sorted(created)
    assert detail["projects_kept"] == []

    again = client.post(f"/operations/{body['id']}/undo")
    assert again.status_code == 409
    assert again.json()["detail"] == "the operation has already been undone"


def test_a_membership_added_after_the_split_goes_with_its_project(
    client: TestClient, storage: Path
) -> None:
    """The second deliberate inversion of `test_undo_leaves_a_membership_it_did_not_add`.

    The arrange kept a membership it did not add, because its unit of undo was the
    membership and its project had to survive. Here the operation's unit is the
    PROJECT: the projects did not exist before the plan, so undo removes them,
    and a membership somebody added afterwards goes with the project it belonged
    to. Mutation: an undo that deletes only the memberships its step rows name
    leaves the project (and the foreign membership) behind.
    """
    scan_and_seed(storage)
    body = run_split(client)
    alpha_id = body["steps"][1]["result"]["project_id"]
    extra_path = "elsewhere/extra.stl"
    write(storage, extra_path, b"solid extra\nendsolid extra\n")
    with Session(engine) as session:
        extra = Asset(
            name="extra",
            filename="extra.stl",
            mime="model/stl",
            size=len(STL_BYTES),
            storage_path=extra_path,
        )
        session.add(extra)
        session.flush()
        session.add(
            ProjectAsset(
                project_id=alpha_id,
                asset_id=extra.id,
                added_at="2026-01-01T00:00:00+00:00",
            )
        )
        session.commit()
        extra_id = extra.id
    assert membership_paths(alpha_id) == sorted(["import/Alpha/alpha.stl", extra_path])

    undone = client.post(f"/operations/{body['id']}/undo")

    assert undone.status_code == 200, undone.text
    assert alpha_id in undone.json()["projects_removed"]
    with Session(engine) as session:
        assert session.get(Project, alpha_id) is None, "the project is the undo's unit"
        assert (
            list(session.exec(select(ProjectAsset).where(ProjectAsset.project_id == alpha_id))) == []
        ), "the foreign membership went with its project"
        assert session.get(Asset, extra_id) is not None, "undo never deletes an asset row"


# --- 7: a later operation supersedes the split undo --------------------------


def test_a_later_operation_over_a_hoisted_folder_supersedes_the_split_undo(
    client: TestClient, storage: Path
) -> None:
    """AC-5, third clause. Mutation: a split that declares only its container.

    The split declared `import/Alpha` as one of its paths (a hoist target), so a
    later `create-project` over that folder overlaps and supersedes the undo. A
    plan that declared only the container would answer 200 here and fold bytes a
    second operation has since described.
    """
    scan_and_seed(storage)
    body = run_split(client)
    assert operation_of(client, body["id"])["undo_state"] == "available"

    later_plan = client.post(
        "/operations/plan", json={"workflow": "create-project", "target": "import/Alpha"}
    )
    assert later_plan.status_code == 200, later_plan.text
    later = run_split(client, later_plan.json())

    superseded = operation_of(client, body["id"])
    assert superseded["undo_state"] == "superseded"
    assert superseded["superseded_by"] == later["id"]

    refused = client.post(f"/operations/{body['id']}/undo")
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert detail["superseded_by"] == later["id"]
    assert detail["undo_state"] == "superseded"


# --- 8: the stale plan and the replay ----------------------------------------


def test_a_stale_split_plan_is_refused_and_writes_no_operation(
    client: TestClient, storage: Path
) -> None:
    """AC-8. Mutation: executing the plan the operator reviewed against a newer tree.

    The file appears between the review and the submit, so the recomputed review
    differs and the digest refuses. A submit that trusted the client's plan would
    queue a move list that no longer matches the member set shown.
    """
    scan_and_seed(storage)
    plan = plan_of(client)
    write(storage, f"{CONTAINER}/Alpha/arrived-later.stl", b"solid late\nendsolid late\n")
    library_scan.scan_storage_root()

    response = client.post(
        "/operations", json={"plan_id": plan["plan_id"], "idempotency_key": "stale-key"}
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "the library changed since this plan was reviewed; review it again"
    with Session(engine) as session:
        assert list(session.exec(select(Operation))) == []


def test_the_same_idempotency_key_answers_the_same_split_operation(
    client: TestClient, storage: Path
) -> None:
    """Mutation: a replay that queues a second split after the first one finished.

    The replay is answered from the key before the plan is recomputed, which is why
    the second call still answers 202 with the same id: the first run moved the
    library, so a recompute would refuse with the stale-plan 409 instead.
    """
    scan_and_seed(storage)
    plan = plan_of(client)
    first = submit_of(client, plan, key="one-key")
    run_to_completion()

    replay = submit_of(client, plan, key="one-key")

    assert replay["id"] == first["id"]
    assert replay["status"] == "done"
    with Session(engine) as session:
        assert len(list(session.exec(select(Operation)))) == 1


# --- 9: every shape the split cannot enact -----------------------------------


def test_the_plan_refuses_every_shape_the_split_cannot_enact(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One refusal per way the workflow cannot act, each in the refusal's own words.

    Mutations this reds, one per case: accepting a shape another door owns; planning
    a split of a folder that is already one product; planning against a key no scan
    produced; planning the library root; offering to move an archive instead of
    naming the extract door; an unbounded move list; planning while a scan rewrites
    the same rows; hoisting a folder onto a project that already covers it.
    """
    write(storage, "import/Solo/solo.stl", b"solid solo\nendsolid solo\n")
    # A real archive, because the engine only reads a zip it can open: at the
    # library root it is an `archive-only` row, which is the row the split refuses.
    # (A zip nested under `import/` is not a row at all - the scan does not descend
    # into a folder with no models - so the delete gate never sees one.)
    with ZipFile(storage / "Vault.zip", "w") as archive:
        archive.writestr("README.txt", b"Vault by maker on Thingiverse: https://www.thingiverse.com/thing:1\n")
        archive.writestr("part.stl", STL_BYTES)
    scan_and_seed(storage)

    status, detail = refused_plan(client, CONTAINER, shape="one-project")
    assert status == 409
    assert repr("one-project") in detail, "the refusal names the shape that is not a split shape"
    assert repr(proposal_engine.SPLIT_SHAPES[0]) in detail, "and the shapes that are"

    status, detail = refused_plan(client, "import/Solo")
    assert status == 409
    assert "this shape would change nothing" in detail, "a product is created, not split"

    status, detail = refused_plan(client, "import/NoSuchFolder")
    assert status == 409
    assert "unknown bundle" in detail
    assert "import/NoSuchFolder" in detail

    status, detail = refused_plan(client, "")
    assert status == 409
    assert "library-relative directory key" in detail

    status, detail = refused_plan(client, "Vault.zip")
    assert status == 409
    assert "POST /import/archive" in detail, "an archive is extracted first, never moved"

    with monkeypatch.context() as patch:
        patch.setattr(operations_service, "MAX_SPLIT_MOVES", 1)
        status, detail = refused_plan(client, CONTAINER)
    assert status == 409
    assert "above the declared cap of 1" in detail

    with monkeypatch.context() as patch:
        patch.setattr(operations_service.seams, "scan_status", lambda: {"running": True})
        status, detail = refused_plan(client, CONTAINER)
    assert status == 409
    assert "a library scan is running" in detail

    # Last, because it is the only case that leaves a row behind.
    create_project_row("Beta", "import/Beta")
    status, detail = refused_plan(client, CONTAINER)
    assert status == 409
    assert "import/Beta" in detail, "the refusal names the hoist target a project already covers"


# --- 10-12: the member rule, shared with create-project ----------------------


def test_a_file_already_in_another_project_is_excluded_with_its_reason(
    client: TestClient, storage: Path
) -> None:
    """Mutation: attaching a file that already belongs elsewhere, or counting it.

    `Beta` keeps a second registered file in the fixture so the folder is still
    creatable: the rule excludes the file, and the one-member-per-project create
    then refuses an EMPTY folder outright - which is a different sentence and would
    hide the exclusion this test is about.
    """
    write(storage, f"{CONTAINER}/Beta/notes.txt", b"layer notes\n")
    scan_and_seed(storage)
    elsewhere = create_project_row("Elsewhere", "somewhere/else")
    with Session(engine) as session:
        beta = session.exec(select(Asset).where(Asset.storage_path == f"{CONTAINER}/Beta/beta.stl")).one()
        session.add(
            ProjectAsset(project_id=elsewhere, asset_id=beta.id, added_at="2026-01-01T00:00:00+00:00")
        )
        session.commit()

    review = plan_of(client)["review"]

    excluded = [(entry["path"], entry["reason"]) for entry in review["excluded"]]
    # Exactly one entry, at the path the operator sees on disk now. A file the
    # MEMBER RULE dropped must not be named a second time at its post-move path
    # with the "left behind" reason: two contradictory reasons for one file is a
    # review the operator cannot act on.
    assert excluded == [
        (
            f"{CONTAINER}/Beta/beta.stl",
            "already a member of project 'Elsewhere'",
        )
    ]
    by_root = {project["root"]: project["members"] for project in review["projects"]}
    assert by_root["import/Beta"] == 1, "the counted member is the one NOT already claimed"
    assert by_root["import/Alpha"] == 1
    assert review["members"] == 3


def test_a_file_the_scan_flagged_missing_is_excluded(client: TestClient, storage: Path) -> None:
    """Mutation: counting a file the last scan says is gone, so the create under-attaches.

    The same shared rule as `create-project`'s, asked over the hoisted folder, and
    named once: a file the member rule dropped already carries its reason, so the
    "left behind" sweep must not name it a second time.
    """
    write(storage, f"{CONTAINER}/Alpha/notes.txt", b"layer notes\n")
    scan_and_seed(storage)
    with Session(engine) as session:
        asset = session.exec(select(Asset).where(Asset.storage_path == f"{CONTAINER}/Alpha/alpha.stl")).one()
        asset.missing_since = "2026-01-01T00:00:00+00:00"
        session.add(asset)
        session.commit()

    review = plan_of(client)["review"]

    excluded = [(entry["path"], entry["reason"]) for entry in review["excluded"]]
    # One entry only: the member rule's own reason, not also the "left behind"
    # sweep's - the same rule test 10 pins.
    assert excluded == [
        (
            f"{CONTAINER}/Alpha/alpha.stl",
            "the file is missing from disk (flagged by the last scan)",
        )
    ]
    by_root = {project["root"]: project["members"] for project in review["projects"]}
    assert by_root["import/Alpha"] == 1, "only the file the scan still sees is a member"
    assert review["members"] == 3


def test_an_unregistered_file_on_disk_is_silently_not_a_member(
    client: TestClient, storage: Path
) -> None:
    """A DELIBERATE drop of the arrange's `unregistered_delta` refusal.

    The arrange refused to plan when the disk held a file the library had never
    registered, and told the operator to rescan. The split does not: membership is
    the registered rows, a rescan prompt belongs to the rescan rather than to a
    plan, and the only honest symptom is the count. What must never happen is the
    file being counted as a member (the create would fail on a row that is not
    there) or being named as an exclusion (it is not the plan's to explain).

    Mutation: plan from the disk instead of the rows and `members` becomes 4, or
    the unregistered path appears in `excluded`.
    """
    scan_and_seed(storage)
    unregistered = f"{CONTAINER}/Alpha/never-scanned.stl"
    write(storage, unregistered, b"solid unseen\nendsolid unseen\n")

    review = plan_of(client)["review"]

    assert {project["root"]: project["members"] for project in review["projects"]} == {
        "import/Alpha": 1,
        "import/Beta": 1,
        "import/Gamma": 1,
    }
    assert review["members"] == 3
    assert unregistered not in [entry["path"] for entry in review["excluded"]]


# --- 13: who may split -------------------------------------------------------


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


def test_a_contributor_splits_and_a_viewer_is_refused(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-6. Mutation: classifying the split at WRITE, or letting a viewer plan it.

    The owner's rubric is "can they REMOVE data with this action": a split moves
    bytes and creates projects, so it is CONTRIBUTE - the workflow table and a real
    session both say so. The viewer's 403 is the permissions module's own sentence,
    not a generic one, and it comes before any lookup.
    """
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    scan_and_seed(storage)
    admin = client.post("/login", json=BREAK_GLASS)
    assert admin.status_code == 200, admin.text
    admin_token = sign_in(client, admin.json()["token"], ADMIN_USERNAME, ADMIN_PASSWORD, "admin")
    contributor = sign_in(
        client, admin_token, CONTRIBUTOR_USERNAME, CONTRIBUTOR_PASSWORD, "contributor"
    )
    viewer = sign_in(client, admin_token, VIEWER_USERNAME, VIEWER_PASSWORD, "viewer")

    assert operations_service.WORKFLOW_LEVELS["split-and-create"] == permissions.CONTRIBUTE

    planned = client.post(
        "/operations/plan",
        json={"workflow": "split-and-create", "target": CONTAINER},
        headers=bearer(contributor),
    )
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    submitted = client.post(
        "/operations", json={"plan_id": plan["plan_id"], "idempotency_key": "contributor"}, headers=bearer(contributor)
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
            json={"workflow": "split-and-create", "target": CONTAINER},
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


# --- 14: the arrange routes are gone ----------------------------------------


API_ROOT = Path(__file__).resolve().parents[1]
ARRANGE_TOKENS = re.compile(r"bundles/arrange|arrange_service|ARRANGE_")


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """The file's lines with comments and string literals blanked out, numbering kept.

    Prose that mentions a deleted module - a docstring explaining why
    `move_service` now owns a rule - is not a live reference, so it must not count
    as one. Blanking the tokens is exact where a per-line quote count would not be
    (an apostrophe in a docstring sentence is not a string delimiter).
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        for row in range(start_row, end_row + 1):
            if row < 1 or row > len(lines):
                continue
            left = start_col if row == start_row else 0
            right = end_col if row == end_row else len(lines[row - 1])
            lines[row - 1] = (
                lines[row - 1][:left] + " " * max(0, right - left) + lines[row - 1][right:]
            )
    return list(enumerate(lines, start=1))


def test_the_arrange_routes_are_gone_everywhere() -> None:
    """AC-2. Mutation: leaving a route, a schema, a permission row or an action behind.

    The grep half is the ticket's own check run in-process: no non-test module may
    name `bundles/arrange`, `arrange_service` or an `ARRANGE_` constant ANYWHERE
    except in prose, because prose is a comment or a docstring. The offending lines
    are printed in the failure message, so a reintroduced reference is named rather
    than counted.
    """
    gone = {
        ("GET", "/bundles/arrange"),
        ("POST", "/bundles/arrange"),
        ("POST", "/bundles/arrange/{arrange_id}/undo"),
    }
    routes = {
        (method, route.path)
        for route in main.app.routes
        for method in getattr(route, "methods", ())
    }
    assert gone.isdisjoint(routes), sorted(gone & routes)

    schema = main.app.openapi()
    assert not [path for path in schema["paths"] if path.startswith("/bundles/arrange")]
    assert not [name for name in schema["components"]["schemas"] if "BundleArrange" in name]

    assert not [
        key for key in permissions.ROUTE_PERMISSIONS if key[1].startswith("/bundles/arrange")
    ]
    assert gone.isdisjoint(set(audit.ROUTE_ACTIONS))
    assert "arrange.apply" not in audit.ACTIONS
    assert "arrange.undo" not in audit.ACTIONS

    offending: list[str] = []
    for path in sorted(API_ROOT.glob("**/*.py")):
        relative = path.relative_to(API_ROOT)
        if "tests" in relative.parts or "__pycache__" in relative.parts:
            continue
        original = path.read_text(encoding="utf-8").splitlines()
        for number, blanked in _code_lines(path):
            if ARRANGE_TOKENS.search(blanked):
                offending.append(f"{relative}:{number}: {original[number - 1].strip()}")
    assert offending == [], "a live arrange reference survived the deletion:\n" + "\n".join(offending)


# --- 15: the wire names both ends -------------------------------------------


def test_the_wire_names_both_ends_of_a_move(client: TestClient, storage: Path) -> None:
    """Mutation: serving `from`/`to` (or a `from_` alias) instead of `source`/`target`.

    This replaces the deleted arrange's `test_the_wire_says_from_and_the_openapi_
    renders_from`: the pair is now `OperationMoveOut`, the review is the only place
    it is served, and the four field names are the schema's whole surface.
    """
    scan_and_seed(storage)

    move = plan_of(client)["review"]["move_list"][0]

    assert set(move) == {"source", "target", "kind", "action"}
    assert move["source"] == f"{CONTAINER}/Alpha"
    assert move["target"] == "import/Alpha"

    properties = main.app.openapi()["components"]["schemas"]["OperationMoveOut"]["properties"]
    assert set(properties) == {"source", "target", "kind", "action"}
    assert "from_" not in properties and "from" not in properties
