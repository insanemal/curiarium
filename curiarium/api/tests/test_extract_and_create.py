"""Extract-and-create as a registry workflow, and the death of the archive door.

`POST /import/archive/project` did five things in one opaque call: it extracted,
moved, resolved, created and reported. This workflow is the owner's shape of the
same job - "an extraction phase and then the existing create-project after moving
the parent archive into the extracted path" - as four visible steps in one
operation:

    extract -> move (the archive under <twin>/_source) -> scan -> create-project

The deleted route could select a subset of entries, list them before writing,
report per-entry failures without aborting the rest, refuse a partial extraction,
answer already-created by archive identity, strip the vendor wrapper, record
provenance in the marker and choose a cover. Every one of those capabilities is
reached below through the workflow's plan/params/review, or pinned as a NEGATIVE
where the old route never had it (the wrapper strip: `archive_project_service`
never imported `proposal_engine`, so capability six never existed to lose).

Why every test here is written to fail if the feature is absent:

* **four steps, once, in order** - the Wizard-Tower shape: 20 payload entries
  under `01_Wizard/`, two junk members and a hidden member beside them, one submit
  that puts every planned file on disk, files the archive under
  `<twin>/_source/<archive>.zip`, registers the tree and creates ONE project with
  20 members and the first model as its cover. A planner that skipped the scan, a
  create that re-resolved from the key, or a move that left the archive beside the
  twin all fail it.
* **the extract door's refusals, not copies of them** - a pre-occupied destination
  refuses AT the extract step with the extract route's own sentence, nothing is
  extracted, no rows exist and the tree is byte-identical.
* **the plan is the listing** - `GET /import/archive/entries` still publishes the
  archive's members, and the plan's `members` is that listing's planned subset.
* **already-created survives its own side effects** - a second plan is ZERO steps
  naming the project, and it keeps answering after the archive was filed under
  `_source` and deleted; a same-key replay answers the same operation.
* **undo folds the move, keeps the payload** - the archive is back, the asset row
  is back, only the project this run created is gone, and the extracted bytes stay
  (the documented residue).
* **the old route is gone everywhere** - routes, OpenAPI, the permission matrix,
  the audit vocabulary and a tokenised grep with comments and strings blanked.

Harness: the idioms `tests/test_adopt_workflow.py` and
`tests/test_split_and_create.py` established - one migrated database, a temp
storage root holding a real zip, the archive registered exactly as the live scan
registers it, auth off unless a test is about *who* may run it.
"""

import ast
import hashlib
import importlib.util
import json
import tokenize
from pathlib import Path
from typing import Optional
from uuid import uuid4
from zipfile import ZIP_STORED, ZipFile

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

import accounts
import archive_service
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
import project_create_service
import project_service
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

API_ROOT = Path(__file__).resolve().parents[1]

ARCHIVE = "import/01_Fates End - Wizard Tower.zip"
TWIN = "import/01_Fates End - Wizard Tower"
ARCHIVE_NAME = "01_Fates End - Wizard Tower.zip"
SOURCE_DIR = f"{TWIN}/_source"
FILED = f"{SOURCE_DIR}/{ARCHIVE_NAME}"
PAYLOAD_ROOT = "01_Wizard"

# The owner's real archive's 20 payload members, in the archive's own order: three
# images, fifteen models, one text note and one nested archive (an opaque member
# whose role is `archive`, never something this path opens).
PNGS = ("_FileName_Key.png", "Wizard_RENDER.png", "Wizard_VisualReference.png")
STLS = (
    "Wizard_Tray_DiceHold_v1.4.stl",
    "AddOn_Wizard_Tray_Lg_Smo_Circle.4.stl",
    "AddOn_Wizard_Tray_Lg_Tx_Circle.stl",
    "Wizard_Dec_Roof_v5.4.stl",
    "Wizard_Dec_v5.4.stl",
    "Wizard_Dec2_Mid_v5.5.stl",
    "Wizard_Tray_OL_v1.3.stl",
    "Wizard_Dice_roof_v5.4.stl",
    "Wizard_Tray_Sm_Tx_v1.4.stl",
    "Wizard_Tray_Lg_Tx_v1.4.stl",
    "Wizard_Tray_Lg_Sm_v1.3.stl",
    "Wizard_Dice_v5.4.stl",
    "Wizard_DT2_Btm_v5.5.stl",
    "Wizard_Dec2_Btm_v5.5.stl",
    "Wizard_DT2_Mid_v5.5.stl",
)
TEXT_NOTE = "Wizard_Dice_v5.4_fixes.txt"
NESTED = "01_Wizard_v5.4.zip"

PAYLOAD = tuple(f"{PAYLOAD_ROOT}/{name}" for name in (*PNGS, *STLS, TEXT_NOTE, NESTED))
# The two junk members the real archive does not happen to carry, plus one hidden
# name that is NOT junk (so the hidden branch is measured, not the junk branch
# wearing its clothes). The fixture keeps the real 20 payload entries, so every
# count below is the owner's shape.
JUNK = (f"__MACOSX/._{STLS[0]}", f"{PAYLOAD_ROOT}/.DS_Store")
HIDDEN = f"{PAYLOAD_ROOT}/.hidden-note.stl"
FIRST_MODEL = f"{PAYLOAD_ROOT}/{STLS[0]}"

STL_BYTES = b"solid cube\nendsolid cube\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n-not-really"
TEXT_BYTES = b"v5.4 fixes: none\n"

BREAK_GLASS = {"username": "admin", "password": "super-secret"}
ADMIN_USERNAME = "root"
ADMIN_PASSWORD = "root-password-1"
CONTRIBUTOR_USERNAME = "contributor"
CONTRIBUTOR_PASSWORD = "contributor-password-1"
VIEWER_USERNAME = "viewer"
VIEWER_PASSWORD = "viewer-password-1"


# --- the archive and its tree ------------------------------------------------


def payload_bytes(name: str) -> bytes:
    if name.endswith(".stl"):
        return STL_BYTES
    if name.endswith(".png"):
        return PNG_BYTES
    if name.endswith(".txt"):
        return TEXT_BYTES
    return b"PK\x05\x06" + b"\x00" * 18  # an empty zip, carried as one opaque member


def archive_members() -> dict[str, bytes]:
    """The archive's bytes, in the real archive's order, with the excluded members last."""
    members = {entry: payload_bytes(entry) for entry in PAYLOAD}
    members.update({name: b"junk-bytes" for name in JUNK})
    members[HIDDEN] = STL_BYTES
    return members


def write_archive(root: Path, relative: str = ARCHIVE, members: Optional[dict] = None) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", ZIP_STORED) as archive:
        for name, body in (members or archive_members()).items():
            archive.writestr(name, body)
    return path


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
    """A temp storage root holding the one archive, at its real library-relative path."""
    root = tmp_path / "storage"
    write_archive(root)
    point_storage(monkeypatch, root)
    return root


@pytest.fixture()
def client() -> TestClient:
    with TestClient(main.app) as test_client:
        yield test_client


def register_archive() -> None:
    """Register the archive exactly as the live box does - a real scan, no hand rows."""
    state = library_scan.scan_storage_root()
    assert state.failed == 0, state.errors


def tree_digest(root: Path) -> dict[str, tuple[int, int, str]]:
    """Every file under `root` by relative path: size, mtime (ns) and sha256."""
    digest: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            body = path.read_bytes()
            digest[path.relative_to(root).as_posix()] = (
                len(body),
                path.stat().st_mtime_ns,
                hashlib.sha256(body).hexdigest(),
            )
    return digest


# --- reading the world -------------------------------------------------------


def plan_extract(client: TestClient, target: str = ARCHIVE, **params) -> dict:
    response = client.post(
        "/operations/plan", json={"workflow": "extract-and-create", "target": target, "params": params}
    )
    assert response.status_code == 200, response.text
    return response.json()


def refused_plan(client: TestClient, target: str, **params) -> tuple[int, str]:
    response = client.post(
        "/operations/plan", json={"workflow": "extract-and-create", "target": target, "params": params}
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
    operations_runner.wait_idle(timeout=30)


def run_workflow(client: TestClient, target: str = ARCHIVE, **params) -> dict:
    """Plan, submit and run one operation; the caller reads the finished body."""
    plan = plan_extract(client, target, **params)
    submitted = submit_of(client, plan)
    run_to_completion()
    body = operation_of(client, submitted["id"])
    assert body["status"] == "done", json.dumps(body, indent=2)
    return body


def step_of(operation: dict, op: str) -> dict:
    return next(step for step in operation["steps"] if step["op"] == op)


def ops(operation: dict) -> list[str]:
    return [step["op"] for step in operation["steps"]]


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


def projects() -> list[Project]:
    with Session(engine) as session:
        return list(session.exec(select(Project)))


def operation_rows() -> int:
    with Session(engine) as session:
        return len(session.exec(select(Operation)).all())


def membership_rows() -> int:
    with Session(engine) as session:
        return len(session.exec(select(ProjectAsset)).all())


def assets_by_path() -> dict[str, Asset]:
    with Session(engine) as session:
        return {asset.storage_path: asset for asset in session.exec(select(Asset)) if asset.storage_path}


def folder_rows() -> set[str]:
    with Session(engine) as session:
        return {folder.storage_path or "" for folder in session.exec(select(Folder))}


def audit_rows(action: str, target: str) -> list[AuditEvent]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(AuditEvent)
                .where(AuditEvent.action == action, AuditEvent.target == target)
                .order_by(AuditEvent.created_at)
            )
        )


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


# --- 1: the four steps, the Wizard-Tower shape, one operation -----------------


def test_the_four_steps_run_in_order_and_leave_the_tree_the_review_promised(
    client: TestClient, storage: Path
) -> None:
    """AC-1. Mutation: a planner that skipped the scan, a create that re-resolved from the key.

    One submit must: write all twenty planned files, file the archive under
    `<twin>/_source`, register the tree, and create ONE project with twenty
    members and the first model as its face - with every step visible, in order,
    on one operation row.
    """
    register_archive()
    assert assets_by_path()[ARCHIVE] is not None

    plan = plan_extract(client)
    assert [step["op"] for step in plan["steps"]] == ["extract", "move", "scan", "create-project"]
    review = plan["review"]
    assert review["workflow"] == "extract-and-create"
    assert review["members"] == 20
    assert review["creates"] == 1
    assert review["moves"] == 1
    assert review["existing_project_id"] is None

    # Every excluded member is NAMED with the server's reason: two junk members and
    # one hidden name. A plan that silently dropped them would say 20 and mean 23.
    reasons = {entry["path"]: entry["reason"] for entry in review["excluded"]}
    assert set(reasons) == {*JUNK, HIDDEN}
    assert "OS metadata" in reasons[JUNK[0]]
    assert "OS metadata" in reasons[JUNK[1]]
    assert "hidden" in reasons[HIDDEN]

    # The listing's own shape: the 20 files sit directly in `01_Wizard`, so they are
    # the loose bucket, and sum(groups) == members structurally.
    assert review["groups"] == [{"name": "", "members": 20, "models": 15}]
    assert sum(group["members"] for group in review["groups"]) == review["members"]
    # The cover is a PATH at plan time: no asset id exists until the scan runs.
    assert review["cover"]["asset_id"] is None
    assert review["cover"]["rel_path"] == f"{TWIN}/{FIRST_MODEL}"
    # The archive's new home is on the wire, in the one move the plan declares.
    assert review["move_list"] == [
        {"source": ARCHIVE, "target": FILED, "kind": "file", "action": "file-archive"}
    ]

    body = run_workflow(client)
    assert ops(body) == ["extract", "move", "scan", "create-project"]
    assert [step["status"] for step in body["steps"]] == ["done", "done", "done", "done"]
    ledger_id = step_of(body, "move")["result"]["ledger_id"]
    assert isinstance(ledger_id, str) and ledger_id

    # Every planned file is on disk under the twin, and the archive is under _source
    # and gone from its old path.
    for entry in PAYLOAD:
        assert (storage / TWIN / entry).is_file(), entry
    assert (storage / FILED).is_file()
    assert not (storage / ARCHIVE).exists()
    assert not any((storage / TWIN / name).exists() for name in (*JUNK, HIDDEN))

    # One project, twenty members, the cover the plan named (resolved by the scan).
    rows = projects()
    assert len(rows) == 1
    created = rows[0]
    assert created.name == "01_Fates End - Wizard Tower"
    held = members(created.id)
    assert len(held) == 20
    assert held == {
        assets_by_path()[f"{TWIN}/{entry}"].id for entry in PAYLOAD
    }
    assert created.cover_asset_id == assets_by_path()[f"{TWIN}/{FIRST_MODEL}"].id
    assert step_of(body, "create-project")["result"]["status"] == "created"
    assert step_of(body, "create-project")["result"]["created"] is True

    # The provenance block: the archive it read, planned vs resolved, the listing's
    # fingerprint, its common root and its groups.
    bundle_marker = marker(created.id)
    assert bundle_marker["origin"] == "archive_extract"
    assert bundle_marker["key"] == project_create_service.archive_identity(ARCHIVE)
    assert bundle_marker["rel_dir"] == TWIN
    payload = bundle_marker["archive_extract"]
    assert payload["archive"] == ARCHIVE
    assert payload["twin"] == TWIN
    assert payload["planned"] == 20
    assert payload["resolved"] == 20
    assert payload["common_root"] == PAYLOAD_ROOT
    assert payload["groups"] == [{"name": "", "count": 20}]
    assert payload["fingerprint"] == archive_service.listing_fingerprint(PAYLOAD)

    # The archive's own asset row moved with it: the move rewrote the row the scan
    # made, which is what an undo has to put back.
    by_path = assets_by_path()
    assert FILED in by_path and ARCHIVE not in by_path
    assert by_path[FILED].id is not None


def test_the_plan_is_the_entries_door_listing(client: TestClient, storage: Path) -> None:
    """AC-1/parity-2. Mutation: a plan that counts something other than the listing.

    The entries door still publishes what the archive holds (READ, untouched). The
    plan's `members` must be that listing minus the members no extract would write,
    which is the only "list them before writing" the operator needs.
    """
    register_archive()
    listing = client.get("/import/archive/entries", params={"path": ARCHIVE})
    assert listing.status_code == 200, listing.text
    published = listing.json()["entries"]
    assert len(published) == len(PAYLOAD) + len(JUNK) + 1

    plan = plan_extract(client)
    excluded = {entry["path"] for entry in plan["review"]["excluded"]}
    assert plan["review"]["members"] == len(published) - len(excluded)
    assert plan["review"]["members"] == 20


def test_a_subset_selection_writes_and_attaches_only_the_subset(client: TestClient, storage: Path) -> None:
    """AC-2. Mutation: an extract step that ignores `params.entries`.

    The operator's pick is the file set: only those entries land, and the project
    holds exactly them - a create that re-resolved the twin's registered files
    would attach every file the archive holds.
    """
    register_archive()
    subset = (PAYLOAD[0], PAYLOAD[3], PAYLOAD[4], PAYLOAD[18])  # two images, two models
    plan = plan_extract(client, entries=list(subset))
    assert plan["review"]["members"] == len(subset)
    assert plan["review"]["cover"]["rel_path"] == f"{TWIN}/{subset[1]}"

    body = run_workflow(client, entries=list(subset))
    assert body["status"] == "done", body
    written = sorted(
        path.relative_to(storage / TWIN).as_posix()
        for path in (storage / TWIN).rglob("*")
        if path.is_file() and "_source" not in path.parts
    )
    assert written == sorted(subset)
    held = members(projects()[0].id)
    assert len(held) == len(subset)
    assert held == {assets_by_path()[f"{TWIN}/{entry}"].id for entry in subset}


# --- 3: per-entry failure refuses at the extract step ------------------------


def test_a_pre_occupied_destination_refuses_at_the_extract_step_and_creates_nothing(
    client: TestClient, storage: Path
) -> None:
    """AC-3. Mutation: an extract that half-writes, or a create that runs anyway.

    The extract door is all-or-nothing: a destination that already exists refuses
    the WHOLE request with that door's own 409 sentence, before a byte is written.
    Nothing is created, no rows exist, and the tree is byte-identical - which is
    the only thing that makes a retry safe.
    """
    register_archive()
    blocked = f"{TWIN}/{PAYLOAD[3]}"
    (storage / blocked).parent.mkdir(parents=True, exist_ok=True)
    (storage / blocked).write_bytes(b"someone else's bytes")
    before = tree_digest(storage)

    plan = plan_extract(client)
    submitted = submit_of(client, plan)
    run_to_completion()
    body = operation_of(client, submitted["id"])

    assert body["status"] == "failed"
    extract = step_of(body, "extract")
    assert extract["status"] == "failed"
    # The extract route's own sentence, not a paraphrase and not `409: ...`.
    assert extract["error"].startswith("refusing to extract: these paths already exist")
    assert blocked in extract["error"]
    assert body["error"] == extract["error"]
    # The runner's vocabulary for "never started": three untouched steps.
    assert [step["status"] for step in body["steps"][1:]] == ["pending", "pending", "pending"]
    assert projects() == []
    assert membership_rows() == 0
    assert tree_digest(storage) == before
    # Nothing else was written: the one prepared file is the only payload path.
    assert [entry for entry in PAYLOAD if (storage / TWIN / entry).exists()] == [PAYLOAD[3]]
    assert not (storage / FILED).exists()


def test_a_planned_entry_that_never_registers_fails_the_scan_step_naming_every_miss(
    client: TestClient, storage: Path
) -> None:
    """AC-4 (partial resolution). Mutation: a scan that accepts whatever registered.

    A member the scan never registers means the archive's promise was not kept.
    The step refuses once, naming EVERY miss by the ENTRY the operator saw, and
    nothing is created - a create over the survivors would present a partial
    extraction as a whole model.
    """
    register_archive()
    # A file the extract wrote disappears between the extract and the walk, so the
    # scan cannot register it while every other planned file registers normally.
    # The seam is the runner's own guard, which runs BEFORE the step body, so the
    # loss is the test's and not a second implementation of the extract.
    def drop_one(operation: Operation, step: OperationStep) -> None:
        if step.op == "scan":
            (storage / TWIN / FIRST_MODEL).unlink()

    operations_runner.set_step_guard(drop_one)
    plan = plan_extract(client)
    submitted = submit_of(client, plan)
    run_to_completion()
    operations_runner.set_step_guard(None)
    body = operation_of(client, submitted["id"])

    assert body["status"] == "failed"
    scan = step_of(body, "scan")
    assert scan["status"] == "failed"
    assert "1 of 20 planned file(s) did not register" in scan["error"]
    assert FIRST_MODEL in scan["error"]
    assert projects() == []
    assert membership_rows() == 0
    # The archive is already filed: the move step ran before the scan.
    assert (storage / FILED).is_file()


def test_a_re_plan_after_a_scan_failure_answers_the_two_remaining_steps(
    client: TestClient, storage: Path
) -> None:
    """The archive is filed, the project does not exist: plan [scan, create-project], not 404.

    Mutation: a planner that only knows the declared path would 404 on a file its
    own previous run moved - a dead end after the first bad scan.
    """
    register_archive()

    def fail_the_scan(operation: Operation, step: OperationStep) -> None:
        if step.op == "scan":
            raise RuntimeError("the walk was interrupted")

    operations_runner.set_step_guard(fail_the_scan)
    plan = plan_extract(client)
    submitted = submit_of(client, plan)
    run_to_completion()
    operations_runner.set_step_guard(None)
    failed = operation_of(client, submitted["id"])
    assert failed["status"] == "failed", failed
    assert step_of(failed, "scan")["status"] == "failed"
    assert (storage / FILED).is_file()
    assert projects() == []

    replanned = plan_extract(client)
    assert [step["op"] for step in replanned["steps"]] == ["scan", "create-project"]
    assert replanned["review"]["moves"] == 0
    assert replanned["review"]["move_list"] == []
    assert replanned["review"]["members"] == 20

    second = submit_of(client, replanned)
    run_to_completion()
    body = operation_of(client, second["id"])
    assert body["status"] == "done", body
    assert len(projects()) == 1
    assert len(members(projects()[0].id)) == 20


# --- 4: already created, three ways -----------------------------------------


def test_a_second_plan_is_a_zero_step_answer_that_survives_filing_and_deletion(
    client: TestClient, storage: Path
) -> None:
    """AC-4. Mutation: a dedupe asked AFTER the zip is opened.

    The archive is filed under `_source` by the run, so a planner that opened the
    declared path first would 404 on its own successful run. Then the filed copy is
    deleted outright: the answer is the marker's, so it still answers.
    """
    register_archive()
    run_workflow(client)
    assert (storage / FILED).is_file()
    assert not (storage / ARCHIVE).exists()

    replanned = plan_extract(client)
    assert replanned["steps"] == []
    assert replanned["review"]["creates"] == 0
    assert replanned["review"]["members"] == 0
    assert replanned["review"]["existing_project_id"] == projects()[0].id
    assert replanned["review"]["existing_project_name"] == projects()[0].name

    (storage / FILED).unlink()
    still = plan_extract(client)
    assert still["steps"] == []
    assert still["review"]["existing_project_id"] == projects()[0].id


def test_a_same_key_replay_answers_the_same_operation(client: TestClient, storage: Path) -> None:
    """AC-4. Mutation: recomputing the plan before asking the key (Mf-215n's defect).

    After the run the same plan would recompute differently - `creates` is 0 now -
    so a submit that recomputed first would answer 409 to its own retry.
    """
    register_archive()
    plan = plan_extract(client)
    submitted = submit_of(client, plan, key="one-click")
    run_to_completion()
    assert operation_of(client, submitted["id"])["status"] == "done"

    replay = submit_of(client, plan, key="one-click")
    assert replay["id"] == submitted["id"]
    assert operation_rows() == 1
    assert len(projects()) == 1


# --- 5: undo -----------------------------------------------------------------


def test_undo_restores_the_archive_and_its_row_and_removes_only_its_own_project(
    client: TestClient, storage: Path
) -> None:
    """AC-5. Mutation: an undo that also un-extracts, or that deletes by name.

    The ledger folds the move back (bytes, then the asset row), the emptied
    `_source` row is deleted, the project this run created is deleted - and the
    extracted payload deliberately STAYS, byte for byte, because no ledger records
    those bytes and un-extracting would delete registered assets.
    """
    register_archive()
    body = run_workflow(client)
    project_id = projects()[0].id
    extracted = {
        entry: (storage / TWIN / entry).read_bytes() for entry in PAYLOAD
    }

    response = client.post(f"/operations/{body['id']}/undo")
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["undone"] is True
    assert answer["moves"] == 1
    assert answer["projects_removed"] == [project_id]

    # The archive is back where it was, and its asset row came with it.
    assert (storage / ARCHIVE).is_file()
    assert not (storage / FILED).exists()
    by_path = assets_by_path()
    assert ARCHIVE in by_path and FILED not in by_path
    # The `_source` folder row this run created is gone; the twin's row is not.
    rows = folder_rows()
    assert SOURCE_DIR not in rows
    assert TWIN in rows
    # Only this run's project is gone.
    assert projects() == []
    assert membership_rows() == 0
    # The payload residue: every extracted byte is still there, unchanged.
    for entry, original in extracted.items():
        assert (storage / TWIN / entry).read_bytes() == original

    # Exactly one `operation.undo` row, and it says what was left behind.
    rows = audit_rows(audit.OPERATION_UNDO, body["id"])
    assert len(rows) == 1
    detail = json.loads(rows[0].detail_json)
    assert detail["moves"] == 1
    assert detail["projects_removed"] == [project_id]
    assert detail["payload_residue"] == operations_service.EXTRACT_PAYLOAD_RESIDUE

    # The operation's own state, and the shared refusal for a second undo.
    undone = operation_of(client, body["id"])
    assert undone["undo_state"] == "undone"
    again = client.post(f"/operations/{body['id']}/undo")
    assert again.status_code == 409


def test_an_undo_keeps_a_project_it_did_not_create(client: TestClient, storage: Path) -> None:
    """AC-5. Mutation: an undo that deletes by `existing_project_id`.

    A project that appeared between the review and the run answers `already_created`
    at the create step; the undo must leave it alone, because deleting it would
    destroy somebody else's work.
    """
    register_archive()
    plan = plan_extract(client)
    submitted = submit_of(client, plan)

    # The race: someone else's project already covers the twin when the create runs.
    with Session(engine) as session:
        foreign = Project(
            name="Made elsewhere",
            metadata_json=json.dumps(
                {"_bundle": {"key": TWIN, "rel_dir": TWIN, "origin": "detection"}}
            ),
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        session.add(foreign)
        session.commit()
        foreign_id = foreign.id

    run_to_completion()
    body = operation_of(client, submitted["id"])
    assert body["status"] == "done", body
    create = step_of(body, "create-project")["result"]
    assert create["status"] == "already_created"
    assert create["created"] is False
    assert create["project_id"] == foreign_id

    answer = client.post(f"/operations/{body['id']}/undo")
    assert answer.status_code == 200, answer.text
    assert answer.json()["projects_removed"] == []
    assert [project.id for project in projects()] == [foreign_id]


def test_a_later_operation_over_the_same_paths_supersedes_the_undo(
    client: TestClient, storage: Path
) -> None:
    """AC-5. Mutation: an undo that outlives a later run over its own paths.

    The second operation declares the same archive and twin, so the first one's
    undo is retired - and the refusal names the operation that retired it.
    """
    register_archive()
    first = run_workflow(client)
    second_plan = plan_extract(client)
    assert second_plan["steps"] == []
    second = submit_of(client, second_plan)
    run_to_completion()
    assert operation_of(client, second["id"])["status"] == "done"

    assert operation_of(client, first["id"])["undo_state"] == "superseded"
    refused = client.post(f"/operations/{first['id']}/undo")
    assert refused.status_code == 409
    assert second["id"] in json.dumps(refused.json()["detail"])


def test_a_two_step_re_plan_undo_removes_its_project_and_leaves_the_filed_archive(
    client: TestClient, storage: Path
) -> None:
    """The documented limitation. Mutation: claiming a re-plan can fold an earlier run's move.

    The archive was filed by the FAILED operation, not by this one; a two-step
    re-plan owns no ledger, so its undo removes its own project and says the
    payload (and the archive) stay. The first operation - which owns the move - is
    superseded the moment the re-plan finishes, so this is a stated residue rather
    than a surprise.
    """

    def fail_the_scan(operation: Operation, step: OperationStep) -> None:
        if step.op == "scan":
            raise RuntimeError("the walk was interrupted")

    register_archive()
    operations_runner.set_step_guard(fail_the_scan)
    first = plan_extract(client)
    submitted = submit_of(client, first)
    run_to_completion()
    operations_runner.set_step_guard(None)
    assert operation_of(client, submitted["id"])["status"] == "failed"

    second = run_workflow(client)
    assert ops(second) == ["scan", "create-project"]
    project_id = projects()[0].id
    answer = client.post(f"/operations/{second['id']}/undo")
    assert answer.status_code == 200, answer.text
    assert answer.json()["moves"] == 0
    assert answer.json()["projects_removed"] == [project_id]
    assert projects() == []
    # Named, not hidden: the archive the earlier operation filed is still filed.
    assert (storage / FILED).is_file()
    assert not (storage / ARCHIVE).exists()
    assert len(audit_rows(audit.OPERATION_UNDO, second["id"])) == 1


# --- 6: a stale plan --------------------------------------------------------


def test_a_plan_over_an_archive_that_changed_is_refused_and_writes_no_operation(
    client: TestClient, storage: Path
) -> None:
    """AC-6. Mutation: a digest that does not cover the listing.

    The listing's fingerprint travels in the plan and the create's marker, so an
    archive that gained a member between the review and the submit moves the
    digest and the submit is refused with the one sentence. No operation row, no
    bytes: an unreviewed tree cannot be written.
    """
    register_archive()
    plan = plan_extract(client)
    assert plan["review"]["members"] == 20

    extra = {**archive_members(), f"{PAYLOAD_ROOT}/Added_Later.stl": STL_BYTES}
    write_archive(storage, members=extra)

    response = client.post(
        "/operations", json={"plan_id": plan["plan_id"], "idempotency_key": uuid4().hex}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == operations_service.STALE_PLAN_MESSAGE
    assert operation_rows() == 0
    assert not (storage / TWIN).exists()


# --- 7: the parity table's negative ----------------------------------------


def test_the_vendor_wrapper_is_not_stripped_because_this_route_never_stripped_one(
    client: TestClient, storage: Path
) -> None:
    """AC-7, capability 6 as a NEGATIVE. Mutation: inventing a wrapper strip to satisfy prose.

    `archive_project_service` never imported `proposal_engine` and its destination
    was always `<parent>/<sanitised stem>`: it wrote nothing itself. So no route
    on this path ever hoisted `01_Wizard/` out of the twin, and this workflow does
    not invent that behaviour. The wrapper survives, the group census sees it, and
    the project holds the wrapped paths.

    The closure evidence for the deleted module is in the ticket note and in the
    commit: `git show` the pre-deletion file and look for `proposal_engine` - there
    is none, and there is no `strip`/`hoist` in it at all.
    """
    register_archive()
    run_workflow(client)
    for entry in PAYLOAD:
        assert entry.startswith(f"{PAYLOAD_ROOT}/")
        assert (storage / TWIN / entry).is_file()
    # Nothing was hoisted above the twin: the twin's only children are the wrapper
    # directory and `_source`.
    assert sorted(path.name for path in (storage / TWIN).iterdir()) == sorted(
        [PAYLOAD_ROOT, "_source"]
    )
    held = members(projects()[0].id)
    assert held == {assets_by_path()[f"{TWIN}/{entry}"].id for entry in PAYLOAD}


# --- 8: the old route is gone everywhere -----------------------------------


def _app_route_pairs() -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for route in main.app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if path is None:
            continue
        for method in methods:
            pairs.add((method, path))
    return pairs


def _tokenised(path: Path) -> str:
    """The file's code with comments and strings BLANKED: names and operators only.

    A docstring is a string, and a route path is a string; what survives is what a
    live reference would have to be - an identifier or an attribute. That is why a
    historical prose mention of the deleted door does not fail this, and a live
    import would.
    """
    pieces: list[str] = []
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING, tokenize.ENCODING):
                continue
            pieces.append(token.string)
    return "\n".join(pieces)


def test_the_old_route_is_gone_from_the_routes_the_wire_and_the_vocabulary(
    client: TestClient,
) -> None:
    """AC-8. Mutation: leaving a shim, or a schema/action row behind the deleted handler."""
    pairs = _app_route_pairs()
    assert ("POST", "/import/archive/project") not in pairs
    assert ("GET", "/import/archive/project") not in pairs
    # Not 405 with an allow header: the path is not declared at all.
    assert client.post("/import/archive/project", json={"path": ARCHIVE}).status_code == 404
    assert client.get("/import/archive/project").status_code == 404

    schema = client.get("/openapi.json").json()
    assert "/import/archive/project" not in schema["paths"]
    components = schema["components"]["schemas"]
    assert "ArchiveProjectOut" not in components
    assert "ArchiveProjectRequest" not in components

    assert ("POST", "/import/archive/project") not in permissions.ROUTE_PERMISSIONS
    assert ("POST", "/import/archive/project") not in audit.ROUTE_ACTIONS
    assert "archive.project.create" not in audit.ACTIONS
    assert not hasattr(audit, "ARCHIVE_PROJECT_CREATE")
    assert importlib.util.find_spec("archive_project_service") is None
    assert not (API_ROOT / "archive_project_service.py").exists()

    # A tokenised grep over every API module: comments and strings blanked, so a
    # live identifier or attribute is the only way to fail.
    offenders: list[str] = []
    for path in sorted(API_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = _tokenised(path)
        if "archive_project" in text:
            offenders.append(str(path.relative_to(API_ROOT)))
    assert offenders == []


# --- 9: the counts, reconciled ---------------------------------------------


def test_the_deleted_route_is_reconciled_in_every_count() -> None:
    """AC-9. The before/after numbers, measured rather than asserted.

    Before this commit: ROUTE_PERMISSIONS 109 (11/39/27/13/18), GET 43, ACTIONS 60,
    ROUTE_ACTIONS 51, EXEMPT 15, WORKFLOWS 3. The deletion removes exactly one
    route (one permission row, one audit route row, one action) and the workflow
    set gains its fourth entry.
    """
    assert len(permissions.ROUTE_PERMISSIONS) == 108
    by_level: dict[str, int] = {}
    for level in permissions.ROUTE_PERMISSIONS.values():
        by_level[level] = by_level.get(level, 0) + 1
    assert by_level == {"public": 11, "read": 39, "contribute": 27, "write": 13, "admin": 18}
    assert sum(1 for method, _path in permissions.ROUTE_PERMISSIONS if method == "GET") == 43
    assert len(audit.ACTIONS) == 59
    assert len(audit.ROUTE_ACTIONS) == 50
    assert len(audit.EXEMPT_ROUTES) == 15
    assert operations_service.WORKFLOWS == (
        "create-project",
        "split-and-create",
        "adopt",
        "extract-and-create",
    )
    assert operations_service.WORKFLOW_LEVELS["extract-and-create"] == permissions.CONTRIBUTE


# --- 10: who may run it ----------------------------------------------------


def test_a_contributor_runs_it_and_a_viewer_is_refused_on_both_requests(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-10. Mutation: classifying the workflow at WRITE, or trusting the route's level.

    The owner's rubric: contribute unless the action can REMOVE data. This workflow
    writes files and creates a project; it removes nothing the operator did not put
    there, and its undo is its own. A real contributor session plans, submits, runs
    and undoes it; the viewer is refused at the plan and at the submit with the
    permissions module's own sentence.
    """
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    register_archive()
    admin = client.post("/login", json=BREAK_GLASS)
    assert admin.status_code == 200, admin.text
    admin_token = sign_in(client, admin.json()["token"], ADMIN_USERNAME, ADMIN_PASSWORD, "admin")
    contributor = sign_in(client, admin_token, CONTRIBUTOR_USERNAME, CONTRIBUTOR_PASSWORD, "contributor")
    viewer = sign_in(client, admin_token, VIEWER_USERNAME, VIEWER_PASSWORD, "viewer")

    planned = client.post(
        "/operations/plan",
        json={"workflow": "extract-and-create", "target": ARCHIVE, "params": {}},
        headers=bearer(contributor),
    )
    assert planned.status_code == 200, planned.text
    submitted = client.post(
        "/operations",
        json={"plan_id": planned.json()["plan_id"], "idempotency_key": "contributor"},
        headers=bearer(contributor),
    )
    assert submitted.status_code == 202, submitted.text
    run_to_completion()
    operation_id = submitted.json()["id"]
    assert (
        client.get(f"/operations/{operation_id}", headers=bearer(contributor)).json()["status"] == "done"
    )
    assert (
        client.post(f"/operations/{operation_id}/undo", headers=bearer(contributor)).status_code == 200
    )

    denied = permissions.DENY_DETAIL[permissions.CONTRIBUTE]
    for response in (
        client.post(
            "/operations/plan",
            json={"workflow": "extract-and-create", "target": ARCHIVE, "params": {}},
            headers=bearer(viewer),
        ),
        client.post(
            "/operations",
            json={"plan_id": planned.json()["plan_id"], "idempotency_key": "viewer"},
            headers=bearer(viewer),
        ),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == denied


# --- 11: entry_policy is executed, never copied ----------------------------


def _module_tree(name: str) -> ast.Module:
    return ast.parse((API_ROOT / name).read_text(encoding="utf-8"))


def _names_in(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found


def _calls_in(tree: ast.Module, module: str, function: str) -> int:
    count = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == function
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == module
        ):
            count += 1
    return count


def test_the_extract_step_executes_the_entry_policy_instead_of_restating_it() -> None:
    """AC-11. Mutation: a planner or runner that re-implements the selection.

    `entry_policy`'s vocabulary (`plan_selection`, `is_junk_entry`, `stream_entry`,
    `entry_member_segments`) belongs to the extractor. Neither the planner nor the
    runner may name it: the planner asks `archive_service.plan_extraction`, and the
    extract step calls `archive_service.extract_out` - the same door the extract
    route calls, so the budget, the junk rule and the no-clobber arbiter are that
    door's by construction.
    """
    forbidden = {"plan_selection", "is_junk_entry", "stream_entry", "entry_member_segments"}
    for name in ("operations_service.py", "operations_runner.py"):
        tree = _module_tree(name)
        assert not (forbidden & _names_in(tree)), name
        assert "entry_policy" not in {
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        }, name

    runner = _module_tree("operations_runner.py")
    assert _calls_in(runner, "archive_service", "extract_out") == 1
    assert _calls_in(runner, "library_scan", "scan_storage_root") == 1
    service = _module_tree("operations_service.py")
    # Two call sites, one primitive: the declared path and the filed copy.
    assert _calls_in(service, "archive_service", "plan_extraction") == 2
    # The old firewall's claim is not restated anywhere: the workflow deliberately
    # goes through bundle_service.create_project, the same verb every bundle door
    # uses.
    assert "test_archive_project.py" not in (bundle_service.create_project.__doc__ or "")
    assert "test_archive_project.py" not in (project_create_service.__doc__ or "")


# --- the two refusals the plan owns before anything runs -------------------


def test_a_running_scan_refuses_the_plan_and_writes_nothing(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: planning over a scan that is about to register the tree anyway."""
    register_archive()
    monkeypatch.setattr(seams, "scan_status", lambda: {"running": True, "state": "running"})
    status, detail = refused_plan(client, ARCHIVE)
    assert status == 409
    assert "a library scan is running" in detail
    assert operation_rows() == 0


def test_an_archive_with_nothing_plannable_is_refused_before_an_operation_exists(
    client: TestClient, storage: Path
) -> None:
    """Mutation: planning the zero-member create that the create verb would refuse anyway."""
    junk_only = {
        f"__MACOSX/._{STLS[0]}": b"junk",
        f"{PAYLOAD_ROOT}/.DS_Store": b"junk",
        f"{PAYLOAD_ROOT}/.hidden.stl": STL_BYTES,
    }
    write_archive(storage, members=junk_only)
    register_archive()
    status, detail = refused_plan(client, ARCHIVE)
    assert status == 409
    assert "nothing to create" in detail
    assert operation_rows() == 0
    assert not (storage / TWIN).exists()


def test_a_member_the_scan_skips_is_named_in_the_plan_rather_than_planned(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third exclusion branch, which the two vocabularies currently overlap on.

    Junk and hidden names are excluded by their own rules; `is_skipped_name` is the
    belt for the day `IGNORED_FILENAMES` gains an entry `entry_policy` does not call
    junk. Patched to prove the branch exists and names the file, because an
    exclusion that cannot be named is a plan that has quietly lost a file.
    """
    register_archive()
    skipped = f"{PAYLOAD_ROOT}/{TEXT_NOTE}"
    real = library_scan.is_skipped_name
    monkeypatch.setattr(
        library_scan,
        "is_skipped_name",
        lambda name, **kwargs: True if name == TEXT_NOTE else real(name, **kwargs),
    )
    plan = plan_extract(client)
    reasons = {entry["path"]: entry["reason"] for entry in plan["review"]["excluded"]}
    assert reasons[skipped] == "a file name the library scan skips"
    assert plan["review"]["members"] == 19


def test_a_broken_zip_is_refused_with_the_extract_door_s_own_sentence(
    client: TestClient, storage: Path
) -> None:
    """Mutation: a planner that advertises a listing it never read.

    The plan opens the archive through `archive_service.plan_extraction`, so a
    file that is named `.zip` and is not one is refused with the extractor's own
    400 instead of a plan that promises files nothing could write. (The extract
    door's 415 branch - a non-zip SUFFIX - is unreachable here: the twin-pairing
    refusal answers first, which is the more useful sentence.)
    """
    (storage / "import").mkdir(parents=True, exist_ok=True)
    (storage / "import/broken.zip").write_bytes(b"not a zip at all")
    status, detail = refused_plan(client, "import/broken.zip")
    assert status == 400
    assert detail == "Invalid zip file"
    assert operation_rows() == 0


# --- what the deleted suite pinned and the composition must keep -------------


def test_a_folder_the_detector_made_answers_the_archive_workflow(
    client: TestClient, storage: Path
) -> None:
    """Mf-yhjo's direction, kept: the twin's project IS this archive's project.

    The detection door writes its marker key as the folder's path, and this door's
    second identity is exactly that twin - so the `.zip` beside a folder that is
    already a project answers a ZERO-STEP plan naming it, rather than becoming a
    second product with a second name. A planner that only asked the archive leg
    would answer 20 members and create a duplicate.
    """
    register_archive()
    with Session(engine) as session:
        foreign = Project(
            name="Fates End - Wizard Tower",
            metadata_json=json.dumps(
                {"_bundle": {"key": TWIN, "detector": "engine:one-project", "rel_dir": TWIN}}
            ),
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        session.add(foreign)
        session.commit()
        foreign_id = foreign.id

    plan = plan_extract(client)
    assert plan["steps"] == []
    assert plan["review"]["existing_project_id"] == foreign_id
    assert plan["review"]["existing_project_name"] == "Fates End - Wizard Tower"
    assert projects()[0].id == foreign_id


def test_the_run_records_one_submit_and_one_done_row_and_no_verdict_row(
    client: TestClient, storage: Path
) -> None:
    """The audit record moved with the door: operation lifecycle rows, not a verdict row.

    Mutation: leaving `archive.project.create` in the vocabulary. The workflow's
    record is one `operation.submit` and one `operation.done` per run; a refused
    PLAN writes nothing at all, which is the honest reduction the ticket names.
    """
    register_archive()
    body = run_workflow(client)
    assert len(audit_rows(audit.OPERATION_SUBMIT, body["id"])) == 1
    assert len(audit_rows(audit.OPERATION_DONE, body["id"])) == 1
    with Session(engine) as session:
        assert (
            session.exec(select(AuditEvent).where(AuditEvent.action == "archive.project.create")).all()
            == []
        )


def test_an_already_extracted_tree_is_an_idempotent_extract_then_registers_and_creates(
    client: TestClient, storage: Path
) -> None:
    """Mutation: an extract step that refuses a tree it already wrote.

    The extract door answers `files=0, already_present=True` and writes nothing when
    every destination is there; the scan then registers the tree and the create runs.
    This is the state a previous run that died after its extract leaves.
    """
    register_archive()
    for entry in PAYLOAD:
        destination = storage / TWIN / entry
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload_bytes(entry))
    payload_before = {
        entry: tree_digest(storage)[f"{TWIN}/{entry}"] for entry in PAYLOAD
    }

    body = run_workflow(client)
    extract = step_of(body, "extract")["result"]
    assert (extract["files"], extract["already_present"]) == (0, True)
    # The extract wrote nothing: no payload byte, mtime or size moved.
    assert {entry: tree_digest(storage)[f"{TWIN}/{entry}"] for entry in PAYLOAD} == payload_before
    assert len(members(projects()[0].id)) == 20


def test_two_archives_sharing_a_basename_are_two_projects(
    client: TestClient, storage: Path
) -> None:
    """Mutation: deduping on the archive's NAME rather than its library path.

    `a/thing.zip` and `b/thing.zip` are two archives that happen to share a name;
    the identity is the path, and their twins are two directories.
    """
    for parent in ("import/a", "import/b"):
        write_archive(storage, relative=f"{parent}/thing.zip", members={f"{PAYLOAD_ROOT}/part.stl": STL_BYTES})
    register_archive()

    run_workflow(client, target="import/a/thing.zip", name="Thing A")
    run_workflow(client, target="import/b/thing.zip", name="Thing B")
    assert sorted(project.name for project in projects()) == ["Thing A", "Thing B"]
    assert (storage / "import/a/thing/_source/thing.zip").is_file()
    assert (storage / "import/b/thing/_source/thing.zip").is_file()


def test_an_archive_with_no_single_root_has_no_common_root_and_groups_by_first_segment(
    client: TestClient, storage: Path
) -> None:
    """The listing helpers moved to `archive_service`; their behaviour did not.

    Two top-level roots mean no common root, so every entry's own first segment is
    its group - and the review still counts every member exactly once.
    """
    members = {
        "alpha/one.stl": STL_BYTES,
        "alpha/two.stl": STL_BYTES,
        "beta/three.stl": STL_BYTES,
        "loose.stl": STL_BYTES,
    }
    write_archive(storage, relative="import/mixed.zip", members=members)
    register_archive()
    plan = plan_extract(client, target="import/mixed.zip")
    assert plan["review"]["members"] == 4
    assert archive_service.listing_root(members) is None
    assert {group["name"]: group["members"] for group in plan["review"]["groups"]} == {
        "": 1,
        "alpha": 2,
        "beta": 1,
    }
    assert sum(group["members"] for group in plan["review"]["groups"]) == 4


def test_the_typed_name_is_trimmed_and_whitespace_falls_back_to_the_twin(
    client: TestClient, storage: Path
) -> None:
    """Mutation: a name that reaches the create verb untrimmed or blank."""
    register_archive()
    typed = plan_extract(client, name="  Wizard v5.4  ")
    assert typed["review"]["projects"][0]["name"] == "Wizard v5.4"

    blank = plan_extract(client, name="   ")
    assert blank["review"]["projects"][0]["name"] == "01_Fates End - Wizard Tower"


def test_a_selection_that_names_an_entry_the_archive_does_not_hold_is_a_400(
    client: TestClient, storage: Path
) -> None:
    """The extract door's own sentence, before an operation can exist."""
    register_archive()
    status, detail = refused_plan(client, ARCHIVE, entries=[f"{PAYLOAD_ROOT}/Not_Here.stl"])
    assert status == 400
    assert detail.startswith("Selection names entries that are not in the archive:")
    assert operation_rows() == 0


def test_the_folder_still_finds_the_filed_archives_project(
    client: TestClient, storage: Path
) -> None:
    """Mutation: relying on the report's zip-beside-folder pairing after the move.

    Filing the archive under `<twin>/_source` leaves no `.zip` beside the folder, so
    detection can no longer pair the two. The project's twin is the marker's
    `rel_dir`, and the dedupe's key leg reads it for an archive-extract marker - or the
    folder door would fall to the assets identity, and to a duplicate project the
    moment the folder grows.
    """
    register_archive()
    run_workflow(client)
    project_id = projects()[0].id
    with Session(engine) as session:
        found = project_create_service.find_existing_project(session, key=TWIN)
        assert found is not None and found.project.id == project_id
        assert project_create_service.find_existing_project(session, key="import/Somewhere Else") is None
