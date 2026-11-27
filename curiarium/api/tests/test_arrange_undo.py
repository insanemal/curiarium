"""Undo of an arrange: the fold backwards, the row restore, and the two refusals.

The ledger is the only source of the move list, so undo can reverse exactly what
an apply did - including a partial one, whose landed prefix is `moves[:done]`.
These tests drive the real routes on a real tree and compare the tree and the rows
against a snapshot taken before the apply.

The harness is a copy of ``tests/test_bundles_api.py``'s, as in
``tests/test_arrange.py``.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import Response
from sqlmodel import Session, delete, select

import accounts
import asset_file_service
import asset_service
import audit
import bundle_service
import db
import folder_service
import library_scan
import login_throttle as login_throttle_module
import main
import move_service
import settings_service
from db import engine
from migrations import run_migrations
from models import Asset, AssetFile, AuditEvent, Folder, MfaRecoveryCode, Project, ProjectAsset, User, UserSession

ADMIN_USERNAME = "root"
ADMIN_PASSWORD = "root-password-1"
BREAK_GLASS = {"username": "admin", "password": "super-secret"}

STL_BYTES = b"solid cube\nendsolid cube\n"

SERIES = "17_ClockworkMonster"
SERIES_KEY = f"import/{SERIES}"
SERIES_TARGET = "import/17_Fates End - ClockworkMonster"
SERIES_SIBLINGS = (
    "01_Fates End - Wizard Tower",
    "02_Fates End - Dragon Tower",
    "03_Fates End - Clock Tower",
)


# --- harness ----------------------------------------------------------------


def truncate_accounts() -> None:
    with Session(engine) as session:
        session.execute(delete(MfaRecoveryCode))
        session.execute(delete(UserSession))
        session.execute(delete(User))
        session.commit()


@pytest.fixture(autouse=True)
def clean_accounts():
    """Migrate once, then start and finish every test without accounts."""
    run_migrations(engine)
    truncate_accounts()
    login_throttle_module.limiter.reset()
    yield
    truncate_accounts()
    login_throttle_module.limiter.reset()


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Argon2's floor parameters, in this file only: every test signs accounts in."""
    monkeypatch.setattr(accounts, "hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every path-bound module at a temp root and undo the rows a test made."""
    storage = tmp_path / "storage"
    (storage / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(db, "STORAGE", storage)
    monkeypatch.setattr(db, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(main, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(library_scan, "STORAGE", storage)
    monkeypatch.setattr(asset_service, "STORAGE", storage)
    monkeypatch.setattr(asset_service, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(asset_file_service, "STORAGE", storage)
    monkeypatch.setattr(bundle_service, "STORAGE", storage)
    run_migrations(engine)
    template_before = settings_service.get_storage_template()
    with Session(engine) as session:
        projects_before = {project.id for project in session.exec(select(Project))}
        folders_before = {folder.id for folder in session.exec(select(Folder))}
        assets_before = {asset.id for asset in session.exec(select(Asset))}
    yield storage
    settings_service.set_storage_template(template_before)
    settings_service.set_setting(settings_service.BUNDLE_DISMISSALS_KEY, "")
    settings_service.set_setting(settings_service.BUNDLE_ENRICHMENTS_KEY, "")
    with Session(engine) as session:
        for membership in list(session.exec(select(ProjectAsset))):
            if membership.project_id not in projects_before:
                session.delete(membership)
        for project in list(session.exec(select(Project))):
            if project.id not in projects_before:
                session.delete(project)
        for asset_file in list(session.exec(select(AssetFile))):
            if asset_file.asset_id not in assets_before:
                session.delete(asset_file)
        for asset in list(session.exec(select(Asset))):
            if asset.id not in assets_before:
                session.delete(asset)
        for folder in list(session.exec(select(Folder))):
            if folder.id not in folders_before:
                session.delete(folder)
        session.commit()


@pytest.fixture()
def client() -> TestClient:
    with TestClient(main.app) as test_client:
        yield test_client


@dataclass(frozen=True)
class Actor:
    """One signed-in account: its token, its id and its role."""

    token: str
    id: str
    role: str


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(client: TestClient, username: str, password: str) -> dict:
    response = client.post("/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def create_user(client: TestClient, token: str, username: str, password: str, role: str) -> dict:
    response = client.post(
        "/users",
        json={"username": username, "password": password, "role": role},
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture()
def admin(client: TestClient) -> Actor:
    """The break-glass admin promoted to a real account, because the API needs one."""
    glass = login(client, **BREAK_GLASS)
    user = create_user(client, glass["token"], ADMIN_USERNAME, ADMIN_PASSWORD, "admin")
    return Actor(token=login(client, ADMIN_USERNAME, ADMIN_PASSWORD)["token"], id=user["id"], role="admin")


# --- tree helpers -----------------------------------------------------------


def write_file(path: Path, payload: bytes = STL_BYTES) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def scan() -> None:
    state = library_scan.scan_storage_root()
    assert state.failed == 0, state.errors


def visible(relative: str) -> bool:
    return relative.split("/", 1)[0] not in folder_service.SYSTEM_DIRS


def relative_paths(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if visible(path.relative_to(root).as_posix())
    )


def digest_values(root: Path) -> list[str]:
    """Every file's sha256 as a sorted multiset: a rename is not a corruption."""
    return sorted(
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and visible(path.relative_to(root).as_posix())
    )


def folder_paths(client: TestClient, actor: Actor) -> set[str]:
    response = client.get("/folders", headers=bearer(actor.token))
    assert response.status_code == 200, response.text
    return {row["storage_path"] for row in response.json() if row["storage_path"]}


def asset_paths() -> set[str]:
    with Session(engine) as session:
        return {asset.storage_path for asset in session.exec(select(Asset)) if asset.storage_path}


def project_ids(client: TestClient, actor: Actor) -> list[str]:
    response = client.get("/projects", headers=bearer(actor.token))
    assert response.status_code == 200, response.text
    return [project["id"] for project in response.json()]


def project_members(project_id: str) -> set[str]:
    with Session(engine) as session:
        return set(
            session.exec(select(ProjectAsset.asset_id).where(ProjectAsset.project_id == project_id))
        )


def ledger_of(project_id: str) -> dict:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        assert project is not None
        bundle = json.loads(project.metadata_json or "{}").get("_bundle") or {}
        return bundle.get("arrange") or {}


def arrange_id_for_plan(client: TestClient, actor: Actor, plan_id: str) -> str:
    for project_id in project_ids(client, actor):
        ledger = ledger_of(project_id)
        if ledger.get("plan") == plan_id:
            return ledger["id"]
    raise AssertionError(f"no project carries the ledger for plan {plan_id!r}")


def ledger_for_plan(client: TestClient, actor: Actor, plan_id: str) -> dict:
    for project_id in project_ids(client, actor):
        ledger = ledger_of(project_id)
        if ledger.get("plan") == plan_id:
            return ledger
    raise AssertionError(f"no project carries the ledger for plan {plan_id!r}")


def preview(client: TestClient, actor: Actor, key: str, shape: str) -> dict:
    response = client.get("/bundles/arrange", params={"key": key, "shape": shape}, headers=bearer(actor.token))
    assert response.status_code == 200, response.text
    return response.json()


def apply_plan(client: TestClient, actor: Actor, plan: dict) -> Response:
    return client.post(
        "/bundles/arrange",
        json={"key": plan["key"], "shape": plan["shape"], "plan_id": plan["plan_id"]},
        headers=bearer(actor.token),
    )


def undo_arrange(client: TestClient, actor: Actor, arrange_id: str) -> Response:
    return client.post(f"/bundles/arrange/{arrange_id}/undo", headers=bearer(actor.token))


def series_tree(root: Path) -> None:
    write_file(root / "import" / SERIES / "a.stl")
    for sibling in SERIES_SIBLINGS:
        write_file(root / "import" / sibling / "a.stl")


def big_split_tree(root: Path, parts: int = 8) -> None:
    """A container of many part folders: eight directory moves, nothing else."""
    for index in range(1, parts + 1):
        write_file(root / "import" / "Big Kit" / f"part {index:02d}" / "alpha" / "a.stl")
        write_file(root / "import" / "Big Kit" / f"part {index:02d}" / "beta" / "b.stl")


def flaky_move(monkeypatch: pytest.MonkeyPatch, fail_on: int) -> tuple[dict, object]:
    """Make the ONE mover fail on its `fail_on`-th call, counting calls.

    `move_service.move` is the primitive the arrange's move loop asks, one pair per
    call, so the counter still counts MOVES and `fail_on=6` still stops the sixth
    with five landed - the same injected failure at the same point as before the
    primitive existed, which is what keeps every count in this file unchanged.

    Returns the counter and the real mover: the caller restores the attribute
    with `monkeypatch.setattr` rather than `monkeypatch.undo()`, because that
    fixture object also holds `isolated_storage`'s path patches.
    """
    real = move_service.move
    calls = {"count": 0}

    def move(pairs, **kwargs):
        calls["count"] += 1
        if calls["count"] == fail_on:
            raise HTTPException(status_code=409, detail="Storage path already exists: simulated")
        return real(pairs, **kwargs)

    monkeypatch.setattr(move_service, "move", move)
    return calls, real


# --- undo -------------------------------------------------------------------


def test_undo_restores_the_tree_and_the_rows_byte_for_byte(
    client: TestClient, admin: Actor, isolated_storage: Path
) -> None:
    """Mutation: an undo that reverses the rows but leaves the bytes where the arrange put them."""
    series_tree(isolated_storage)
    scan()
    before_tree = relative_paths(isolated_storage)
    before_digests = digest_values(isolated_storage)
    before_rows = folder_paths(client, admin)
    before_assets = asset_paths()
    plan = preview(client, admin, SERIES_KEY, "one-project")
    applied = apply_plan(client, admin, plan).json()
    assert relative_paths(isolated_storage) != before_tree, "the apply must move something"

    response = undo_arrange(client, admin, applied["arrange_id"])

    assert response.status_code == 200, response.text
    undone = response.json()
    assert undone["undone"] is True
    assert undone["key"] == SERIES_KEY
    assert undone["moves"] == 1
    assert undone["project_removed"] is False, "a project owns no path and no bytes; it is never deleted"
    assert undone["projects_removed"] == []
    assert undone["unseeable"] == []
    assert undone["stale"] == []
    assert relative_paths(isolated_storage) == before_tree
    assert digest_values(isolated_storage) == before_digests
    assert folder_paths(client, admin) == before_rows
    assert asset_paths() == before_assets
    assert project_ids(client, admin) == [applied["project_id"]], "the ledger's carrier stays"
    assert project_members(applied["project_id"]) == set(), "the memberships the arrange added are gone"

    with Session(engine) as session:
        events = [
            event
            for event in session.exec(select(AuditEvent).where(AuditEvent.action == audit.ARRANGE_UNDO))
            if event.detail_json and json.loads(event.detail_json).get("arrange_id") == applied["arrange_id"]
        ]
    assert len(events) == 1
    detail = json.loads(events[0].detail_json)
    assert events[0].target == SERIES_KEY
    assert detail["moves"] == 1
    assert detail["memberships_removed"] == 1
    assert detail["projects_removed"] == []


def test_a_second_undo_is_a_409(client: TestClient, admin: Actor, isolated_storage: Path) -> None:
    """Mutation: undo deleting the ledger's carrier, so the repeat answers 404 instead of 409."""
    series_tree(isolated_storage)
    scan()
    plan = preview(client, admin, SERIES_KEY, "one-project")
    applied = apply_plan(client, admin, plan).json()
    assert undo_arrange(client, admin, applied["arrange_id"]).status_code == 200
    after_first = relative_paths(isolated_storage)

    response = undo_arrange(client, admin, applied["arrange_id"])

    assert response.status_code == 409, "the ledger lives in the kept project, so the repeat is a 409 not a 404"
    assert "already undone" in response.json()["detail"]
    assert relative_paths(isolated_storage) == after_first


def test_undo_leaves_a_membership_it_did_not_add(
    client: TestClient, admin: Actor, isolated_storage: Path
) -> None:
    """Mutation: removing a membership the arrange did not add, or deleting the ledger with it."""
    series_tree(isolated_storage)
    scan()
    plan = preview(client, admin, SERIES_KEY, "one-project")
    applied = apply_plan(client, admin, plan).json()
    # A file added to the project after the arrange: it is not in the ledger's
    # `members` snapshot, so the undo must leave it a member and leave the project.
    write_file(isolated_storage / "extra" / "extra.stl")
    with Session(engine) as session:
        asset = Asset(
            name="extra",
            filename="extra.stl",
            mime="model/stl",
            size=len(STL_BYTES),
            storage_path="extra/extra.stl",
        )
        session.add(asset)
        session.flush()
        session.add(ProjectAsset(project_id=applied["project_id"], asset_id=asset.id, added_at="2026-01-01T00:00:00+00:00"))
        session.commit()
        extra_id = asset.id

    response = undo_arrange(client, admin, applied["arrange_id"])

    assert response.status_code == 200, response.text
    undone = response.json()
    assert undone["project_removed"] is False
    assert undone["projects_removed"] == []
    assert undone["moves"] == 1
    assert project_ids(client, admin) == [applied["project_id"]]
    assert project_members(applied["project_id"]) == {extra_id}, "only the snapshot's membership is gone"
    ledger = ledger_of(applied["project_id"])
    assert ledger["undone"] is True
    assert ledger["applied"] is True, "the ledger records that the apply did happen"

    second = undo_arrange(client, admin, applied["arrange_id"])
    assert second.status_code == 409, "the marker says undone, so the second undo refuses"
    assert "already undone" in second.json()["detail"]


def test_a_partial_apply_is_replayable_backwards(
    client: TestClient, admin: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: a partial apply whose landed prefix is not recorded (done == 0)."""
    big_split_tree(isolated_storage)
    scan()
    before_tree = relative_paths(isolated_storage)
    before_digests = digest_values(isolated_storage)
    before_rows = folder_paths(client, admin)
    plan = preview(client, admin, "import/Big Kit", "split-top-folders")
    assert len(plan["moves"]) == 8
    calls, real_move = flaky_move(monkeypatch, fail_on=6)

    response = apply_plan(client, admin, plan)

    assert response.status_code == 409, response.text
    assert "5 of 8 moves landed" in response.json()["detail"]
    assert calls["count"] == 6
    ledger = ledger_for_plan(client, admin, plan["plan_id"])
    assert ledger["done"] == 5
    assert ledger["applied"] is False
    plan_projects = project_ids(client, admin)
    assert len(plan_projects) == 8, "a split creates one project per product root"

    monkeypatch.setattr(move_service, "move", real_move)
    undo = undo_arrange(client, admin, ledger["id"])

    assert undo.status_code == 200, undo.text
    undone = undo.json()
    assert undone["moves"] == 5
    assert undone["project_removed"] is False, "no project row is ever deleted"
    assert undone["unseeable"] == []
    assert undone["stale"] == []
    assert relative_paths(isolated_storage) == before_tree
    assert digest_values(isolated_storage) == before_digests
    assert folder_paths(client, admin) == before_rows
    assert project_ids(client, admin) == plan_projects, "the three carriers stay so the ledger is reachable"
    assert all(project_members(project_id) == set() for project_id in plan_projects)


def test_a_partial_apply_finishes_forward_on_a_re_post(
    client: TestClient, admin: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: a re-post that re-plans instead of resuming from the ledger's `done`."""
    big_split_tree(isolated_storage)
    scan()
    before_digests = digest_values(isolated_storage)
    plan = preview(client, admin, "import/Big Kit", "split-top-folders")
    _calls, real_move = flaky_move(monkeypatch, fail_on=6)
    failed = apply_plan(client, admin, plan)
    assert failed.status_code == 409, failed.text
    ledger = ledger_for_plan(client, admin, plan["plan_id"])
    assert ledger["done"] == 5
    monkeypatch.setattr(move_service, "move", real_move)

    response = apply_plan(client, admin, plan)

    assert response.status_code == 200, response.text
    applied = response.json()
    assert applied["already_applied"] is False
    assert applied["applied"] is True
    assert applied["moves"] == 8
    assert applied["unseeable"] == []
    assert applied["stale"] == []
    assert "import/Big Kit" not in relative_paths(isolated_storage)
    assert relative_paths(isolated_storage) == sorted(
        ["import"]
        + [f"import/part {index:02d}" for index in range(1, 9)]
        + [
            f"import/part {index:02d}/{part}"
            for index in range(1, 9)
            for part in ("alpha", "alpha/a.stl", "beta", "beta/b.stl")
        ]
    )
    assert digest_values(isolated_storage) == before_digests
    assert folder_paths(client, admin) == {
        "import",
        *[f"import/part {index:02d}" for index in range(1, 9)],
        *[
            f"import/part {index:02d}/{part}"
            for index in range(1, 9)
            for part in ("alpha", "beta")
        ],
    }
    resumed = ledger_for_plan(client, admin, plan["plan_id"])
    assert resumed["applied"] is True
    assert resumed["done"] == 8


def test_an_ambiguous_fold_refuses_and_leaves_the_tree_alone(
    client: TestClient, admin: Actor, isolated_storage: Path
) -> None:
    """Mutation: guessing which end of a move to trust when both ends exist."""
    series_tree(isolated_storage)
    scan()
    plan = preview(client, admin, SERIES_KEY, "one-project")
    applied = apply_plan(client, admin, plan).json()
    # A hand-move back: the source exists again beside the destination.
    write_file(isolated_storage / SERIES_KEY / "a.stl")
    before_tree = relative_paths(isolated_storage)
    before_rows = folder_paths(client, admin)

    response = undo_arrange(client, admin, applied["arrange_id"])

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert SERIES_KEY in detail
    assert "both ends" in detail
    assert relative_paths(isolated_storage) == before_tree
    assert folder_paths(client, admin) == before_rows


def test_a_claim_mid_fold_is_refused_and_the_fold_is_not_recorded(
    client: TestClient, admin: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: undo trusting `has_source` when the arbiter says the old name was taken.

    The falsifier: `move_service.move_bytes` returns False for the pair under fold,
    which is what `os.link` answers when the old name `has_source` just called free
    was claimed in the window between the check and the link. Ignoring that False (as
    this undo did) folds nothing, prunes, restores every row onto the old name and
    stamps `undone: True` - a row naming a file that does not exist, and a second
    undo answering "already undone", so the lie is permanent.

    Second instance of the defect class closed in
    ``tests/test_move_service.py::test_undo_refuses_a_target_claimed_after_its_check_instead_of_lying``;
    same hole, one call site away, on the arrange's own undo path.
    """
    series_tree(isolated_storage)
    scan()
    plan = preview(client, admin, SERIES_KEY, "one-project")
    applied = apply_plan(client, admin, plan).json()
    # The harness's moves are directory moves (`kind == "dir"`), so the pair under
    # fold - the one `undo` hands the arbiter - is `SERIES_KEY` -> `SERIES_TARGET`,
    # not a lone file inside them.
    source_relative = SERIES_KEY
    target_relative = SERIES_TARGET
    assert (isolated_storage / target_relative).exists(), "the forward move put the bytes here"
    before_tree = relative_paths(isolated_storage)
    before_rows = asset_paths()
    real_move_bytes = move_service.move_bytes

    def claimed(source: Path, target: Path) -> bool:
        # Free to `has_source`'s read, occupied by the time the arbiter runs: the
        # window a check-then-act cannot close.
        if Path(target) == isolated_storage / source_relative:
            return False
        return real_move_bytes(source, target)

    monkeypatch.setattr(move_service, "move_bytes", claimed)

    response = undo_arrange(client, admin, applied["arrange_id"])

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert source_relative in detail
    assert "both ends" in detail and "refusing to guess" in detail
    assert relative_paths(isolated_storage) == before_tree, "the refusal moves nothing"
    assert (isolated_storage / target_relative).exists(), "the bytes stay where the forward move left them"
    assert not (isolated_storage / source_relative).exists()
    assert asset_paths() == before_rows, "a refused fold restores no row"
    assert all((isolated_storage / path).exists() for path in before_rows), "every row still names a file that exists"
    assert not ledger_for_plan(client, admin, plan["plan_id"]).get("undone"), "the fold is not recorded, so a retry can still try"
