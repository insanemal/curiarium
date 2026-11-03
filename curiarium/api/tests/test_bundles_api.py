"""The bundle routes over HTTP: the report a viewer may read, the create a contributor may run.

Two things are proved here that the service tests cannot: the served surface (three
routes, a viewer refused on both POSTs) and the one acceptance path - a real
Thingiverse-shaped bundle on disk becomes exactly one project with its provenance
and its members. The route *classification* and the audit *coverage* are proved by
the sweeps in ``tests/test_permissions_matrix.py`` and ``tests/test_audit_log.py``,
which enumerate from the matrix and from ``audit.ROUTE_ACTIONS``; no route list is
hand-written here.

The harness is a copy of ``tests/test_projects_api.py``'s (there is no shared
conftest in this suite): ``isolated_storage`` points every module that binds a path
at import time at a temp root, runs the migrations and deletes the rows a test
created; the role fixtures are copied from ``tests/test_contributor_role.py``.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from httpx import Response
from sqlmodel import Session, delete, select

import accounts
import asset_file_service
import asset_service
import bundle_service
import db
import library_scan
import login_throttle as login_throttle_module
import main
import permissions
import settings_service
from db import engine
from migrations import run_migrations
from models import Asset, AssetFile, Folder, MfaRecoveryCode, Project, ProjectAsset, User, UserSession

ADMIN_USERNAME = "root"
ADMIN_PASSWORD = "root-password-1"
CONTRIBUTOR_USERNAME = "contributor"
CONTRIBUTOR_PASSWORD = "contributor-password-1"
VIEWER_USERNAME = "viewer"
VIEWER_PASSWORD = "viewer-password-1"
BREAK_GLASS = {"username": "admin", "password": "super-secret"}

MOAI = "2493386_Moai_statue_-No_overhang"
THING_URL = "https://www.thingiverse.com/thing:2493386"
README_TEXT = "Moai statue -No overhang\nCreated by Thingiverse user moai_maker\nLicensed under CC BY-NC\n"
LICENSE_TEXT = f"Moai statue -No overhang by moai_maker on Thingiverse: {THING_URL}\n"
STL_BYTES = b"solid cube\nendsolid cube\n"
# The previews, in write order. The fourth is the largest, so it is the cover.
IMAGE_SIZES = (100, 400, 200, 900, 300)


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
    with Session(engine) as session:
        # Projects first: `idx_project_name_unique` is case-insensitive, so a
        # leaked row would 409 the next test's project.
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


def ok(response: Response) -> Response:
    """Seed requests are infrastructure: anything but 200 means the harness broke."""
    assert response.status_code == 200, response.text
    return response


def sign_in(client: TestClient, token: str, username: str, password: str, role: str) -> Actor:
    """Create `username` with `role` and sign it in, through the real routes."""
    user = create_user(client, token, username, password, role)
    session = login(client, username, password)
    return Actor(token=session["token"], id=user["id"], role=role)


@pytest.fixture()
def admin(client: TestClient) -> Actor:
    """The break-glass admin promoted to a real account, because the API needs one."""
    glass = login(client, **BREAK_GLASS)
    user = create_user(client, glass["token"], ADMIN_USERNAME, ADMIN_PASSWORD, "admin")
    return Actor(token=login(client, ADMIN_USERNAME, ADMIN_PASSWORD)["token"], id=user["id"], role="admin")


@pytest.fixture()
def contributor(client: TestClient, admin: Actor) -> Actor:
    return sign_in(client, admin.token, CONTRIBUTOR_USERNAME, CONTRIBUTOR_PASSWORD, "contributor")


@pytest.fixture()
def viewer(client: TestClient, admin: Actor) -> Actor:
    return sign_in(client, admin.token, VIEWER_USERNAME, VIEWER_PASSWORD, "viewer")


# --- fixtures on disk -------------------------------------------------------


def write_file(path: Path, payload: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_zip(path: Path, entries: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)


def moai_bundle(root: Path) -> Path:
    """The Thingiverse README/LICENSE bundle: 5 previews, 3 models, no zip."""
    bundle = root / MOAI
    write_text(bundle / "README.txt", README_TEXT)
    write_text(bundle / "LICENSE.txt", LICENSE_TEXT)
    for index, size in enumerate(IMAGE_SIZES):
        write_file(bundle / "images" / f"{index}.jpg", b"j" * size)
    for index in range(3):
        write_file(bundle / "files" / f"part{index}.stl", STL_BYTES)
    return bundle


def scan() -> None:
    """Register the tree exactly as the live box does; `ready` needs those rows."""
    state = library_scan.scan_storage_root()
    assert state.failed == 0, state.errors


def report_for(client: TestClient, actor: Actor, **params) -> dict:
    response = client.get("/bundles/detect", params=params, headers=bearer(actor.token))
    assert response.status_code == 200, response.text
    return response.json()


def project_asset_ids(client: TestClient, actor: Actor, project_id: str) -> list[str]:
    response = client.get(f"/project/{project_id}/assets", headers=bearer(actor.token))
    assert response.status_code == 200, response.text
    return [asset["id"] for asset in response.json()]


def project_ids(client: TestClient, actor: Actor) -> list[str]:
    """Every project id, read back through the listing; empty means nothing was made."""
    response = client.get("/projects", headers=bearer(actor.token))
    assert response.status_code == 200, response.text
    return [project["id"] for project in response.json()]


def project_by_id(client: TestClient, actor: Actor, project_id: str) -> dict:
    response = client.get("/projects", headers=bearer(actor.token))
    assert response.status_code == 200, response.text
    return {project["id"]: project for project in response.json()}[project_id]


# --- the acceptance paths ---------------------------------------------------


def test_create_from_the_report_returns_one_project(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """The report names the bundle; one create makes one project with its provenance."""
    moai_bundle(isolated_storage)
    scan()

    report = report_for(client, contributor)

    assert report["counts"]["total"] == 1
    row = report["bundles"][0]
    assert row["key"] == MOAI
    assert row["detector"] == "thingiverse-readme"
    assert row["confidence"] == "high"
    assert row["status"] == "ready"
    assert row["model_count"] == 3
    assert row["cover"]["rel_path"] == f"{MOAI}/images/3.jpg", "the largest preview is the cover"
    assert row["cover"]["reason"] == "largest-image"

    response = client.post("/bundles/create", json={"items": [{"key": MOAI}]}, headers=bearer(contributor.token))

    assert response.status_code == 200, response.text
    created = response.json()
    assert created["created"] == 1
    project_id = created["results"][0]["project_id"]
    project = project_by_id(client, contributor, project_id)
    assert project["name"] == "Moai statue -No overhang"
    assert project["creator"] == "moai_maker"
    assert project["licence"] == "CC BY-NC"
    assert project["licence_url"] == "https://creativecommons.org/licenses/by-nc/4.0/"
    assert project["source_url"] == THING_URL
    assert project["origin"] == "imported"
    assert project["cover_asset_id"] == row["cover"]["asset_id"]
    assert len(project_asset_ids(client, contributor, project_id)) == 3
    assert created["results"][0]["assets_added"] == 3


def test_a_viewer_can_read_the_report_but_cannot_create_or_dismiss(
    client: TestClient, viewer: Actor, isolated_storage: Path
) -> None:
    """Reading a dry-run report is a READ; both writes need contribute."""
    moai_bundle(isolated_storage)
    scan()

    assert report_for(client, viewer)["counts"]["total"] == 1

    refused = permissions.DENY_DETAIL[permissions.CONTRIBUTE]
    create = client.post("/bundles/create", json={"items": [{"key": MOAI}]}, headers=bearer(viewer.token))
    dismiss = client.post(
        "/bundles/dismiss",
        json={"keys": [MOAI], "dismissed": True},
        headers=bearer(viewer.token),
    )

    assert create.status_code == 403, create.text
    assert create.json()["detail"] == refused
    assert dismiss.status_code == 403, dismiss.text
    assert dismiss.json()["detail"] == refused
    assert project_ids(client, viewer) == [], "a refused create writes nothing"
    assert bundle_service.get_dismissals() == {}, "a refused dismiss writes nothing"


def test_an_unknown_key_creates_nothing_and_never_fails(client: TestClient, contributor: Actor) -> None:
    """The client's key is never trusted: an unknown one is a per-item failure, not a 500."""
    response = client.post(
        "/bundles/create",
        json={"items": [{"key": "does/not/exist"}]},
        headers=bearer(contributor.token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == 0 and body["failed"] == 1
    assert body["results"][0]["reason"] == "unknown bundle"
    assert project_ids(client, contributor) == []


# --- duplicate archives (Mf-z4vv) -------------------------------------------


def test_a_duplicate_archive_is_reported_and_never_a_bundle_row(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """A byte-identical copy is reclaimable bytes in the report, never a second row."""
    moai_bundle(isolated_storage)
    entries = {
        "README.txt": README_TEXT.encode(),
        "LICENSE.txt": LICENSE_TEXT.encode(),
        **{f"images/{index}.jpg": b"j" * size for index, size in enumerate(IMAGE_SIZES)},
        **{f"files/part{index}.stl": STL_BYTES for index in range(3)},
    }
    write_zip(isolated_storage / f"{MOAI}.zip", entries)
    shutil.copyfile(isolated_storage / f"{MOAI}.zip", isolated_storage / f"{MOAI} (1).zip")
    scan()

    report = report_for(client, contributor)

    size = (isolated_storage / f"{MOAI}.zip").stat().st_size
    assert len(report["duplicates"]) == 1
    duplicate = report["duplicates"][0]
    assert duplicate["paths"] == [f"{MOAI}.zip", f"{MOAI} (1).zip"]
    assert duplicate["reclaimable_bytes"] == duplicate["size"] == size
    assert len(duplicate["sha256"]) == 64
    assert all(character in "0123456789abcdef" for character in duplicate["sha256"])
    assert f"{MOAI} (1).zip" not in {row["key"] for row in report["bundles"]}
    assert [row["key"] for row in report["bundles"]] == [MOAI]
    assert report["duplicates_total_bytes"] == size
    assert f"{MOAI}.zip" in report["duplicates_text"]
    assert f"{MOAI} (1).zip" in report["duplicates_text"]
    assert "duplicate" in report["duplicates_text"]
