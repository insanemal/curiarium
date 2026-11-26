"""The arrange routes over HTTP: preview, apply, refusal and replay (Mf-v9eg).

The service's rules are pinned here through the real routes on a real tree: scan
the disk, preview a shape, apply it, and read the tree, the rows, the ledger and
the audit row back. The four evidence layouts are the ones the engine was built
from (a wrapper child, a grown compound, a split container, an archive beside its
twin), plus the numbered-series rename.

The harness is a copy of ``tests/test_bundles_api.py``'s (there is no shared
conftest in this suite): ``isolated_storage`` points every path-bound module at a
temp root, runs the migrations and deletes the rows a test created; the role
fixtures are copied from ``tests/test_contributor_role.py``.

Every test names, in its docstring, the mutation it exists to catch.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from zipfile import ZipFile

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from httpx import Response
from sqlmodel import Session, delete, select

import accounts
import arrange_service
import asset_file_service
import asset_service
import audit
import bundle_service
import db
import folder_service
import library_scan
import login_throttle as login_throttle_module
import main
import permissions
import project_service
import proposal_engine
import seams
import settings_service
from db import engine
from migrations import run_migrations
from models import Asset, AssetFile, AuditEvent, Folder, MfaRecoveryCode, Project, ProjectAsset, User, UserSession

ADMIN_USERNAME = "root"
ADMIN_PASSWORD = "root-password-1"
CONTRIBUTOR_USERNAME = "contributor"
CONTRIBUTOR_PASSWORD = "contributor-password-1"
VIEWER_USERNAME = "viewer"
VIEWER_PASSWORD = "viewer-password-1"
EDITOR_USERNAME = "editor"
EDITOR_PASSWORD = "editor-password-1"
BREAK_GLASS = {"username": "admin", "password": "super-secret"}

STL_BYTES = b"solid cube\nendsolid cube\n"
PDF_BYTES = b"%PDF-1.4 manual\n"
ZIP_BYTES = b"PK\x03\x04 not a real zip, only bytes to move\n"
THING_README = "Vault by maker on Thingiverse: https://www.thingiverse.com/thing:12345\nLicensed under CC BY-NC\n"

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


@pytest.fixture()
def editor(client: TestClient, admin: Actor) -> Actor:
    return sign_in(client, admin.token, EDITOR_USERNAME, EDITOR_PASSWORD, "editor")


# --- tree helpers -----------------------------------------------------------


def write_file(path: Path, payload: bytes = STL_BYTES) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def write_zip(path: Path, entries: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)


def write_tree(root: Path, spec: dict[str, bytes]) -> None:
    for relative, payload in spec.items():
        write_file(root / relative, payload)


def scan() -> None:
    """Register the tree exactly as the live box does; `ready` needs those rows."""
    state = library_scan.scan_storage_root()
    assert state.failed == 0, state.errors


def visible(relative: str) -> bool:
    """Whether a relative path is outside the app-owned directories."""
    return relative.split("/", 1)[0] not in folder_service.SYSTEM_DIRS


def relative_paths(root: Path) -> list[str]:
    """Every file and directory on disk, storage-relative, sorted, app dirs excluded."""
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if visible(path.relative_to(root).as_posix())
    )


def disk_directories(root: Path) -> set[str]:
    """Every user-visible directory on disk, storage-relative."""
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_dir() and visible(path.relative_to(root).as_posix())
    }


def digests(root: Path) -> dict[str, str]:
    """sha256 per file, keyed by relative path; the move must not change the bytes."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and visible(path.relative_to(root).as_posix())
    }


def digest_values(root: Path) -> list[str]:
    """Every file's digest as a sorted multiset, so a rename is not a corruption."""
    return sorted(digests(root).values())


def folder_paths(client: TestClient, actor: Actor) -> set[str]:
    response = client.get("/folders", headers=bearer(actor.token))
    assert response.status_code == 200, response.text
    return {row["storage_path"] for row in response.json() if row["storage_path"]}


def projects(client: TestClient, actor: Actor) -> dict[str, dict]:
    response = client.get("/projects", headers=bearer(actor.token))
    assert response.status_code == 200, response.text
    return {project["id"]: project for project in response.json()}


def asset_paths() -> set[str]:
    with Session(engine) as session:
        return {asset.storage_path for asset in session.exec(select(Asset)) if asset.storage_path}


def project_members(project_id: str) -> set[str]:
    with Session(engine) as session:
        statement = select(ProjectAsset.asset_id).where(ProjectAsset.project_id == project_id)
        return set(session.exec(statement))


def registered_asset_ids() -> set[str]:
    """Every registered asset id: the library as the latest scan left it."""
    with Session(engine) as session:
        return {asset.id for asset in session.exec(select(Asset))}


def arrange_events(action: str, arrange_id: str) -> list[AuditEvent]:
    with Session(engine) as session:
        rows = [
            event
            for event in session.exec(select(AuditEvent).where(AuditEvent.action == action))
            if event.detail_json and json.loads(event.detail_json).get("arrange_id") == arrange_id
        ]
    return rows


def arrange_id_of(client: TestClient, actor: Actor, key: str) -> str:
    """The ledger id of the one applied project created from `key`'s tree."""
    for project in projects(client, actor).values():
        bundle = project["metadata_json"].get("_bundle") or {}
        ledger = bundle.get("arrange")
        if ledger and ledger.get("key") == key:
            return ledger["id"]
    raise AssertionError(f"no project carries an arrange ledger for {key!r}")


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


def create_bundle(client: TestClient, actor: Actor, key: str) -> str:
    """Create one project through the ordinary door; the grown fixture starts here."""
    response = client.post("/bundles/create", json={"items": [{"key": key}]}, headers=bearer(actor.token))
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["created"] == 1, created
    return created["results"][0]["project_id"]


# --- the four evidence layouts ----------------------------------------------


def vault_child_tree(root: Path) -> None:
    """A wrapper child with the zip INSIDE it, plus a second model folder.

    `Vault` holds `Vault Archive_<date>/` (models, the manual and its own zip) and
    `Rhino models/`; a Thingiverse README/LICENSE pair at `Vault/` is what makes the
    PARENT the detected row while the wrapper child sits inside it.
    """
    write_tree(
        root,
        {
            "Vault/README.txt": THING_README.encode(),
            "Vault/LICENSE.txt": b"Vault by maker\n",
            "Vault/Vault Archive_18.11.2026/parts/part1.stl": STL_BYTES,
            "Vault/Vault Archive_18.11.2026/Instructions Manual.pdf": PDF_BYTES,
            "Vault/Vault Archive_18.11.2026/Vault Archive_18.11.2026.zip": ZIP_BYTES,
            "Vault/Rhino models/rhino.3dm": b"3dm bytes\n",
        },
    )


def stagetop_tree(root: Path, parts: int = 9) -> None:
    """Nine part folders inside `import/Stagetop`: the project about to grow."""
    for index in range(1, parts + 1):
        write_file(root / "import" / "Stagetop" / f"part {index:02d}" / "part.stl")
    write_file(root / "import" / "Stagetop" / "assembly.stl")


def grow_stagetop_tree(root: Path, first: int, last: int) -> None:
    """The NEW part folders that appear after the project was made: the growth."""
    for index in range(first, last + 1):
        write_file(root / "import" / "Stagetop" / f"part {index:02d}" / "part.stl")


STAGETOP_KEY = "import/Stagetop"
# The role vocabulary is the scan's, not a suffix table: each suffix below is only
# how the fixture spells one role, and `library_scan.classify_entry` decides.
STAGETOP_SUFFIXES = {
    "model": (".stl",),
    "image": (".png", ".jpg"),
    "text": (".txt",),
    "archive": (".zip",),
    "other": (".gcode",),
}
# The real Stagetop numbers: 60 files in six part folders at create time, 171 more
# later - 17 in each existing folder and 23 in each of three brand-new ones.
STAGETOP_CREATE_RECIPE = {"model": 6, "image": 2, "text": 1, "archive": 1}
STAGETOP_GROWTH_RECIPE = {"model": 10, "image": 3, "text": 2, "archive": 1, "other": 1}
STAGETOP_NEW_RECIPE = {"model": 14, "image": 3, "text": 2, "archive": 2, "other": 2}


def write_recipe(folder: Path, recipe: dict[str, int], *, tag: str) -> None:
    """Write one folder's files from a count-by-role recipe.

    The numbers live in the recipes, once, so a folder never silently changes the
    fixture's totals. `.gcode` is the "other" spelling on purpose: `.3dm` is an
    engine MODEL (`proposal_engine.MODEL_EXTS`) while the scan calls it OTHER, so a
    `.3dm` here would make the engine's model census disagree with the roles and
    turn AC-6's own 102 into 114.
    """
    for role, count in recipe.items():
        for index in range(1, count + 1):
            suffixes = STAGETOP_SUFFIXES[role]
            path = folder / f"{tag}-{role}-{index:02d}{suffixes[index % len(suffixes)]}"
            if role == "archive":
                write_zip(path, {"part.stl": STL_BYTES})
            else:
                write_file(path, STL_BYTES)


def stagetop_project_tree(root: Path) -> None:
    """The six part folders the Stagetop project is created from: 60 files."""
    for index in range(1, 7):
        write_recipe(root / STAGETOP_KEY / f"part {index:02d}", STAGETOP_CREATE_RECIPE, tag="create")


def grow_stagetop_project(root: Path) -> None:
    """The 171 files that arrive later across those six folders and three new ones.

    `Feet 5414731` is the deliberately separable group: its name carries a 7-digit
    vendor id, so `separable` AND `foreign` are both a real `True`, while `part 07`
    and `part 08` match `PART_NAME` and stay `separable: False`.
    """
    for index in range(1, 7):
        write_recipe(root / STAGETOP_KEY / f"part {index:02d}", STAGETOP_GROWTH_RECIPE, tag="grow")
    for name in ("part 07", "part 08", "Feet 5414731"):
        write_recipe(root / STAGETOP_KEY / name, STAGETOP_NEW_RECIPE, tag="new")


def marbles_tree(root: Path) -> None:
    """A container whose three product children each carry their own models/docs.

    The children put their models under TWO unnamed subfolders, so neither is a
    title-name row of its own and the container is the row (compound: one product
    in several parts) - the case where a split retires the container.
    """
    for product in ("Dragon Figurine", "Castle Figurine", "Robot Figurine"):
        write_file(root / "import" / "OutOfMarbles" / product / "alpha" / "a.stl")
        write_file(root / "import" / "OutOfMarbles" / product / "beta" / "b.stl")
        write_file(root / "import" / "OutOfMarbles" / product / "print-settings.json", b'{"layer":0.2}\n')


def archive_twin_tree(root: Path) -> None:
    """A zip BESIDE the extracted twin directory: provenance, not a product."""
    write_tree(
        root,
        {
            "Vault/README.txt": THING_README.encode(),
            "Vault/LICENSE.txt": b"Vault by maker\n",
            "Vault/Vault Archive_18.11.2026/parts/part1.stl": STL_BYTES,
            "Vault/Vault Archive_18.11.2026/Instructions Manual.pdf": PDF_BYTES,
            "Vault/Vault Archive_18.11.2026.zip": ZIP_BYTES,
        },
    )


def series_tree(root: Path) -> None:
    """A numbered tower beside three siblings that share the series title."""
    write_file(root / "import" / SERIES / "a.stl")
    for sibling in SERIES_SIBLINGS:
        write_file(root / "import" / sibling / "a.stl")


def test_a_wrapper_child_is_dropped_and_its_zip_filed_under_source(
    client: TestClient, admin: Actor, isolated_storage: Path
) -> None:
    """Mutation: copying instead of moving the wrapper's children, or losing the zip.

    A one-project arrange on a wrapper child must move every entry up one level and
    file the archive under `_source/`, with the bytes intact and the rows following.
    """
    vault_child_tree(isolated_storage)
    scan()
    before = digest_values(isolated_storage)

    plan = preview(client, admin, "Vault", "one-project")

    assert plan["refusals"] == []
    assert plan["report"]["creatable"] is False, "the row recommends root-docs, not one-project"
    assert [(move["from"], move["to"], move["kind"], move["action"]) for move in plan["moves"]] == [
        (
            "Vault/Vault Archive_18.11.2026/Instructions Manual.pdf",
            "Vault/Instructions Manual.pdf",
            "file",
            "hoist",
        ),
        ("Vault/Vault Archive_18.11.2026/parts", "Vault/parts", "dir", "hoist"),
        (
            "Vault/Vault Archive_18.11.2026/Vault Archive_18.11.2026.zip",
            "Vault/_source/Vault Archive_18.11.2026.zip",
            "file",
            "file-archive",
        ),
    ], "the wrapper's children hoist into the candidate; the zip is filed as provenance"
    assert plan["creates"] == [{"path": "Vault", "exists": True}, {"path": "Vault/_source", "exists": False}]
    assert the_only(plan["text"], "move ") == 3

    response = apply_plan(client, admin, plan)

    assert response.status_code == 200, response.text
    applied = response.json()
    assert applied["applied"] is True
    assert applied["already_applied"] is False
    assert applied["moves"] == 3
    assert applied["unseeable"] == [], "every directory on disk must have a row"
    assert applied["stale"] == [], "every asset row must name a file on disk"
    assert relative_paths(isolated_storage) == sorted(
        [
            "Vault",
            "Vault/Instructions Manual.pdf",
            "Vault/LICENSE.txt",
            "Vault/README.txt",
            "Vault/Rhino models",
            "Vault/Rhino models/rhino.3dm",
            "Vault/_source",
            "Vault/_source/Vault Archive_18.11.2026.zip",
            "Vault/parts",
            "Vault/parts/part1.stl",
        ]
    )
    assert digest_values(isolated_storage) == before, "a move that corrupts bytes"
    assert folder_paths(client, admin) == disk_directories(isolated_storage)
    assert "Vault/Vault Archive_18.11.2026" not in folder_paths(client, admin)

    project_id = applied["project_id"]
    bundle = projects(client, admin)[project_id]["metadata_json"]["_bundle"]
    ledger = bundle["arrange"]
    assert ledger["moves"] == [[move["from"], move["to"], move["kind"]] for move in plan["moves"]]
    assert ledger["applied"] is True
    assert ledger["undone"] is False
    assert ledger["done"] == 3
    for key in ("key", "detector", "groups", "rel_dir"):
        assert key in bundle, f"the arrange marker dropped _bundle[{key!r}]"

    events = arrange_events(audit.ARRANGE_APPLY, ledger["id"])
    assert len(events) == 1
    detail = json.loads(events[0].detail_json)
    assert events[0].target == "Vault"
    assert detail["shape"] == "one-project"
    assert detail["moves"] == ledger["moves"]
    assert detail["project_ids"] == [project_id]
    assert detail["created_project"] is True


def the_only(lines: list[str], needle: str) -> int:
    return sum(1 for line in lines if needle in line)


def test_a_grown_project_extends_its_membership_and_undoes_to_the_pre_arrange_set(
    client: TestClient, admin: Actor, isolated_storage: Path
) -> None:
    """Mutation: refusing `nothing_to_arrange` for a grown project, or making undo a 404.

    The ticket's "Stagetop grown project": the project exists, two new part folders
    arrive on disk and a rescan registers them, and the arrange extends the
    membership while moving nothing. The ledger's carrier (the project) is never
    deleted, so a second undo is a 409 rather than a 404.
    """
    stagetop_tree(isolated_storage)
    scan()
    project_id = create_bundle(client, admin, "import/Stagetop")
    before_members = project_members(project_id)
    assert len(before_members) == 10, "nine parts and the assembly share the project"

    grow_stagetop_tree(isolated_storage, first=10, last=11)
    scan()
    before_tree = digests(isolated_storage)
    assert project_members(project_id) == before_members, "a rescan registers the new parts, it does not attach them"

    plan = preview(client, admin, "import/Stagetop", "one-project")

    assert plan["moves"] == [], "the grown project needs no move"
    assert plan["refusals"] == [], "a grown project is the operation's own core case, not nothing_to_arrange"

    response = apply_plan(client, admin, plan)

    assert response.status_code == 200, response.text
    applied = response.json()
    assert applied["applied"] is True
    assert applied["moves"] == 0
    assert applied["project_ids"] == [project_id], "the grown project is extended, never doubled"
    assert applied["unseeable"] == []
    assert applied["stale"] == []
    after_members = project_members(project_id)
    added = after_members - before_members
    assert len(added) == 2, "the arrange attaches the two new part folders"
    assert digests(isolated_storage) == before_tree, "extending a membership never touches a byte"
    assert len(projects(client, admin)) == 1

    ledger = projects(client, admin)[project_id]["metadata_json"]["_bundle"]["arrange"]
    assert set(ledger["projects"][0]["members"]) == added, "the ledger snapshots exactly what it added"
    events = arrange_events(audit.ARRANGE_APPLY, applied["arrange_id"])
    assert len(events) == 1
    detail = json.loads(events[0].detail_json)
    assert detail["shape"] == "one-project"
    assert detail["moves"] == []
    assert detail["project_ids"] == [project_id]
    assert detail["created_project"] is False

    again = apply_plan(client, admin, plan)
    assert again.status_code == 200, again.text
    assert again.json()["already_applied"] is True
    assert project_members(project_id) == after_members, "a re-post never duplicates a membership"

    undo = client.post(f"/bundles/arrange/{applied['arrange_id']}/undo", headers=bearer(admin.token))

    assert undo.status_code == 200, undo.text
    undone = undo.json()
    assert undone["undone"] is True
    assert undone["moves"] == 0
    assert undone["project_removed"] is False
    assert undone["projects_removed"] == []
    assert project_members(project_id) == before_members, "exactly the memberships the arrange added are gone"
    assert digests(isolated_storage) == before_tree
    assert projects(client, admin).keys() == {project_id}, "the ledger's carrier stays, so the 409 is possible"

    second = client.post(f"/bundles/arrange/{applied['arrange_id']}/undo", headers=bearer(admin.token))
    assert second.status_code == 409, "a second undo answers 409, never 404: the kept project holds the ledger"
    assert "already undone" in second.json()["detail"]


def test_the_adoption_preview_names_171_files_in_nine_groups_and_the_apply_updates_them(
    client: TestClient, admin: Actor, isolated_storage: Path
) -> None:
    """Mutation: compute the pending delta and then THROW IT AWAY, so a 171-file
    adopt previews blank and the operator approves an operation nothing describes.

    Mutation: extend the memberships and leave the marker's `groups` at the six the
    project was created from, so a project holding 231 files renders 60. Mutation: a
    `by_role` or `groups` count that does not sum to `total`. Mutation: a ledger
    without `groups_before`, so undo cannot put the six groups back. Mutation: a
    plan digest that ignores the adopted ids.
    """
    stagetop_project_tree(isolated_storage)
    scan()
    project_id = create_bundle(client, admin, STAGETOP_KEY)
    before_members = project_members(project_id)
    assert len(before_members) == 60, "six part folders of ten files"
    before_groups = projects(client, admin)[project_id]["metadata_json"]["_bundle"]["groups"]
    assert [group["name"] for group in before_groups] == [f"part {index:02d}" for index in range(1, 7)]
    for group in before_groups:
        assert "included" in group and "attached" in group, group

    grow_stagetop_project(isolated_storage)
    scan()
    assert project_members(project_id) == before_members, (
        "a rescan registers the growth, it never attaches it"
    )
    before_tree = digests(isolated_storage)

    plan = preview(client, admin, STAGETOP_KEY, "add-to-project")

    assert plan["moves"] == [], "the folder is already inside the project"
    assert plan["creates"] == []
    assert plan["refusals"] == []
    adoptions = plan["adoptions"]
    assert adoptions["total"] == 171
    assert sum(group["files"] for group in adoptions["groups"]) == 171, (
        "the groups must PARTITION the total, never sample it"
    )
    assert set(adoptions["by_role"]) == {"model", "image", "text", "archive", "other"}, (
        "every role key is present, even at zero: a missing key reads as no answer"
    )
    assert adoptions["by_role"] == {"model": 102, "image": 27, "text": 18, "archive": 12, "other": 12}
    # The engine sorts roots by name CASEFOLD, so "Feet ..." leads and the parts
    # follow 01..08: the order is the report's, not this fixture's.
    assert [group["name"] for group in adoptions["groups"]] == [
        "Feet 5414731",
        "part 01",
        "part 02",
        "part 03",
        "part 04",
        "part 05",
        "part 06",
        "part 07",
        "part 08",
    ]
    by_name = {group["name"]: group for group in adoptions["groups"]}
    for name in [f"part {index:02d}" for index in range(1, 7)]:
        assert by_name[name]["new"] is False, f"{name} is a group the project already has"
    for name in ("part 07", "part 08", "Feet 5414731"):
        assert by_name[name]["new"] is True, f"{name} is a brand new group"
    assert by_name["Feet 5414731"]["separable"] is True, "a trailing vendor id can stand as its own project"
    assert by_name["Feet 5414731"]["foreign"] is True, "a 7-digit id is somebody else's product"
    assert by_name["part 07"]["separable"] is False, "a part name is never a project boundary"
    assert adoptions["sample_names"], "the preview must name something the operator recognises"
    assert all("/" not in name for name in adoptions["sample_names"]), "basenames only, never a path list"
    assert adoptions["target"] == {"root": STAGETOP_KEY, "project_id": project_id, "creates_project": False}
    assert adoptions["unregistered"] == 0

    # `measure()` counts a `.zip` in `archives` AND falls through to `others` for the
    # same file, so the on-disk total is the other four: adding `archives` here would
    # say 249 and the delta would name 18 files that are not missing.
    facts = proposal_engine.measure(str(isolated_storage / STAGETOP_KEY), STAGETOP_KEY)
    assert facts.models + facts.images + facts.docs + facts.others == 231, "60 held + 171 pending"

    other_shape = preview(client, admin, STAGETOP_KEY, "one-project")
    assert other_shape["adoptions"]["total"] == adoptions["total"], "the delta is the tree's, not the shape's"
    assert other_shape["plan_id"] != plan["plan_id"], "the digest covers the shape"

    response = apply_plan(client, admin, plan)

    assert response.status_code == 200, response.text
    applied = response.json()
    assert applied["project_ids"] == [project_id], "the grown project is extended, never doubled"
    assert applied["moves"] == 0
    after_members = project_members(project_id)
    added = after_members - before_members
    assert len(after_members) == 231
    assert len(added) == 171, "exactly the delta the preview named"

    bundle = projects(client, admin)[project_id]["metadata_json"]["_bundle"]
    assert bundle["key"] == STAGETOP_KEY
    assert bundle["rel_dir"] == STAGETOP_KEY
    assert bundle["detector"], "the adoption never rewrites the detector"
    assert bundle["shape"], "the adoption never rewrites the shape the project was created from"
    groups = bundle["groups"]
    assert len(groups) == 9, "the three new folders are groups now, not an afterthought"
    for group in groups:
        assert list(group) == ["name", "model_count", "foreign", "separable", "included", "attached"], (
            "the marker's key order is a contract: a group written without `included` never subtracts"
        )
        assert group["included"] is True
    # `attached` is MODEL-scoped by the marker's own vocabulary: 102 grown models plus
    # the 36 the create phase had, never all 231 members of the project.
    assert sum(group["attached"] for group in groups) == 138

    ledger = bundle["arrange"]
    assert set(ledger["projects"][0]["members"]) == added, "the ledger snapshots exactly what it added"
    assert ledger["projects"][0]["groups_before"] == before_groups, (
        "undo needs the snapshot it replaced, not a guess"
    )

    assert digests(isolated_storage) == before_tree, "an adoption moves no byte"

    undo = client.post(f"/bundles/arrange/{applied['arrange_id']}/undo", headers=bearer(admin.token))

    assert undo.status_code == 200, undo.text
    undone = undo.json()
    assert undone["members_removed"] == 171
    assert undone["moves"] == 0
    assert project_members(project_id) == before_members, "exactly the memberships the arrange added are gone"
    assert projects(client, admin)[project_id]["metadata_json"]["_bundle"]["groups"] == before_groups, (
        "undo puts the six groups back byte for byte"
    )
    assert digests(isolated_storage) == before_tree
    assert set(projects(client, admin)) == {project_id}, "the ledger's carrier stays"
    second = client.post(f"/bundles/arrange/{applied['arrange_id']}/undo", headers=bearer(admin.token))
    assert second.status_code == 409, "a second undo is a 409, never a 404"


def test_the_plan_tells_an_adopt_from_a_new_group_from_a_move(
    client: TestClient, admin: Actor, isolated_storage: Path
) -> None:
    """Mutation: one undifferentiated file list, so an adopt, a new group and a move
    read alike and the operator cannot tell what the plan is about to do.

    One preview object carries all three: `Rhino models` is a group the project
    already has (an adopt), `Vault Archive_18.11.2026` is a folder it never held (an
    adopt that creates a group), and the wrapper child hoisting out of itself is a
    MOVE - bytes, not membership.
    """
    write_tree(
        isolated_storage,
        {
            "Vault/README.txt": THING_README.encode(),
            "Vault/LICENSE.txt": b"Vault by maker\n",
            "Vault/Rhino models/rhino.stl": STL_BYTES,
        },
    )
    scan()
    project_id = create_bundle(client, admin, "Vault")
    before_groups = projects(client, admin)[project_id]["metadata_json"]["_bundle"]["groups"]
    assert [group["name"] for group in before_groups] == ["Rhino models"]
    before_members = project_members(project_id)

    write_tree(
        isolated_storage,
        {
            "Vault/Rhino models/rhino2.stl": STL_BYTES,
            "Vault/Vault Archive_18.11.2026/parts/part1.stl": STL_BYTES,
            "Vault/Vault Archive_18.11.2026/Instructions Manual.pdf": PDF_BYTES,
            "Vault/Vault Archive_18.11.2026/Vault Archive_18.11.2026.zip": ZIP_BYTES,
        },
    )
    scan()

    plan = preview(client, admin, "Vault", "one-project")

    assert plan["moves"], "the inner zip names the wrapper, so this plan moves bytes"
    assert any(move["action"] == "hoist" for move in plan["moves"]), (
        "the third case is a MOVE, not an adopt"
    )
    adoptions = plan["adoptions"]
    by_name = {group["name"]: group for group in adoptions["groups"]}
    assert by_name["Rhino models"]["new"] is False, "a file in a group the project already has is an adopt"
    assert by_name["Vault Archive_18.11.2026"]["new"] is True, (
        "a folder the project never held is an adopt that creates a group"
    )
    assert sum(group["files"] for group in adoptions["groups"]) == adoptions["total"]
    assert adoptions["total"] == len(registered_asset_ids() - before_members), (
        "the delta is every registered file the project does not hold"
    )


def test_an_unregistered_growth_refuses_and_names_the_rescan(
    client: TestClient, admin: Actor, isolated_storage: Path
) -> None:
    """Mutation: attaching the 60 the library knows about while 171 files sit on
    disk unregistered - a partial set that no preview ever named.

    The refusal is ONE: `unregistered_delta`, never also `nothing_to_arrange`, and
    the sentence carries the file count, the model share and the rescan that fixes
    it. Applying the same plan answers the same sentence and attaches nothing.
    """
    stagetop_project_tree(isolated_storage)
    scan()
    project_id = create_bundle(client, admin, STAGETOP_KEY)
    before_members = project_members(project_id)
    assert len(before_members) == 60

    grow_stagetop_project(isolated_storage)
    # Deliberately NO scan(): the disk holds 231 files, the library knows 60.

    plan = preview(client, admin, STAGETOP_KEY, "add-to-project")

    assert [refusal["reason"] for refusal in plan["refusals"]] == ["unregistered_delta"], (
        "one refusal, and nothing_to_arrange must not fire beside it"
    )
    detail = plan["refusals"][0]["detail"]
    assert "171" in detail, detail
    assert "102" in detail, detail
    assert "rescan" in detail, detail
    assert plan["adoptions"]["total"] == 0, "nothing unregistered is a member candidate"
    assert plan["adoptions"]["unregistered"] == 171

    response = apply_plan(client, admin, plan)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == detail, "the apply answers the preview's own sentence"
    assert project_members(project_id) == before_members, "nothing was attached"


def test_an_adoption_larger_than_one_query_batch_loses_no_member(
    client: TestClient, admin: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: insert the memberships in one unbounded batch, or stop after the
    first chunk - 171 > 4 means the loop must run 43 times, and a loop that stopped
    at the first chunk would leave 4 memberships behind while reporting success.
    """
    monkeypatch.setattr(project_service, "MEMBER_QUERY_BATCH", 4)
    stagetop_project_tree(isolated_storage)
    scan()
    project_id = create_bundle(client, admin, STAGETOP_KEY)
    before_members = project_members(project_id)

    grow_stagetop_project(isolated_storage)
    scan()

    plan = preview(client, admin, STAGETOP_KEY, "add-to-project")
    assert plan["adoptions"]["total"] == 171

    response = apply_plan(client, admin, plan)

    assert response.status_code == 200, response.text
    after_members = project_members(project_id)
    assert len(after_members - before_members) == 171
    assert len(after_members) == 231, "a chunked insert loses no row"


def test_a_split_hoists_every_product_and_retires_the_container(
    client: TestClient, admin: Actor, isolated_storage: Path
) -> None:
    """Mutation: a split that copies the children or leaves the empty container behind."""
    marbles_tree(isolated_storage)
    scan()
    before = digest_values(isolated_storage)

    plan = preview(client, admin, "import/OutOfMarbles", "split-top-folders")
    assert plan["refusals"] == []
    assert [move["to"] for move in plan["moves"]] == [
        "import/Castle Figurine",
        "import/Dragon Figurine",
        "import/Robot Figurine",
    ]
    assert [move["action"] for move in plan["moves"]] == ["hoist", "hoist", "hoist"], (
        "a split hoists each product out of the retired container"
    )

    response = apply_plan(client, admin, plan)

    assert response.status_code == 200, response.text
    applied = response.json()
    assert applied["applied"] is True
    assert applied["moves"] == 3
    assert len(applied["project_ids"]) == 3
    assert applied["unseeable"] == []
    assert applied["stale"] == []
    assert "import/OutOfMarbles" not in relative_paths(isolated_storage)
    assert relative_paths(isolated_storage) == sorted(
        ["import"]
        + [
            f"import/{product}/{part}"
            for product in ("Castle Figurine", "Dragon Figurine", "Robot Figurine")
            for part in ("", "alpha", "alpha/a.stl", "beta", "beta/b.stl", "print-settings.json")
            if part
        ]
        + ["import/Castle Figurine", "import/Dragon Figurine", "import/Robot Figurine"]
    )
    assert digest_values(isolated_storage) == before
    assert folder_paths(client, admin) == disk_directories(isolated_storage)
    assert len(projects(client, admin)) == 3
    assert "import/OutOfMarbles" not in folder_paths(client, admin), "the container row is retired"

    for project_id in applied["project_ids"]:
        bundle = projects(client, admin)[project_id]["metadata_json"]["_bundle"]
        ledger = bundle["arrange"]
        assert ledger["id"] == applied["arrange_id"]
        assert bundle["rel_dir"] in {entry["root"] for entry in ledger["projects"]}
        assert bundle["key"] == bundle["rel_dir"], "each split child owns its own product folder"


def test_an_archive_beside_its_twin_is_filed_under_source(
    client: TestClient, admin: Actor, isolated_storage: Path
) -> None:
    """Mutation: the packaging named as a product, or the zip left beside the product."""
    archive_twin_tree(isolated_storage)
    scan()
    before = digest_values(isolated_storage)

    plan = preview(client, admin, "Vault", "one-project")

    assert plan["refusals"] == []
    text = "\n".join(plan["text"])
    assert '"Vault/_source/Vault Archive_18.11.2026.zip"' in text, "the text names one product folder"
    assert 'drop the wrapper "Vault Archive_18.11.2026/"' in text
    assert len([line for line in plan["text"] if line.startswith("move ")]) == 3

    response = apply_plan(client, admin, plan)

    assert response.status_code == 200, response.text
    applied = response.json()
    assert applied["unseeable"] == []
    assert applied["stale"] == []
    assert relative_paths(isolated_storage) == sorted(
        [
            "Vault",
            "Vault/Instructions Manual.pdf",
            "Vault/LICENSE.txt",
            "Vault/README.txt",
            "Vault/_source",
            "Vault/_source/Vault Archive_18.11.2026.zip",
            "Vault/parts",
            "Vault/parts/part1.stl",
        ]
    )
    assert digest_values(isolated_storage) == before
    assert folder_paths(client, admin) == disk_directories(isolated_storage)


def test_the_series_rename_keeps_the_number_and_the_rows_follow(
    client: TestClient, admin: Actor, isolated_storage: Path
) -> None:
    """Mutation: stripping the numeric series prefix, or leaving the rows on the old path."""
    series_tree(isolated_storage)
    scan()
    before = digest_values(isolated_storage)
    old_rows = folder_paths(client, admin)
    assert SERIES_KEY in old_rows

    plan = preview(client, admin, SERIES_KEY, "one-project")

    assert plan["refusals"] == []
    assert plan["moves"] == [{"from": SERIES_KEY, "to": SERIES_TARGET, "kind": "dir", "action": "rename"}]
    assert plan["moves"][0]["to"].split("/")[-1].startswith("17_"), "the moves never strip the series number"
    assert any('rename to "17_Fates End - ClockworkMonster"' in line for line in plan["text"])

    response = apply_plan(client, admin, plan)

    assert response.status_code == 200, response.text
    applied = response.json()
    assert applied["unseeable"] == []
    assert applied["stale"] == []
    rows = folder_paths(client, admin)
    assert SERIES_TARGET in rows and SERIES_KEY not in rows
    assert f"{SERIES_TARGET}/a.stl" in asset_paths()
    assert f"{SERIES_KEY}/a.stl" not in asset_paths()
    assert rows == disk_directories(isolated_storage)
    assert digest_values(isolated_storage) == before
    ledger = projects(client, admin)[applied["project_id"]]["metadata_json"]["_bundle"]["arrange"]
    assert ledger["moves"] == [[SERIES_KEY, SERIES_TARGET, "dir"]]


# --- refusals ---------------------------------------------------------------


def assert_nothing_changed(
    client: TestClient,
    actor: Actor,
    storage: Path,
    before_tree: list[str],
    before_rows: set[str],
    response: Response,
    status: int,
    *,
    reason: Optional[str] = None,
    needle: str = "",
) -> dict:
    """One refusal: its status and sentence, and that disk and rows are untouched."""
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert needle in detail
    if reason is not None:
        assert reason in detail or reason.replace("_", " ") in detail
    assert relative_paths(storage) == before_tree
    assert folder_paths(client, actor) == before_rows
    assert projects(client, actor) == {}
    return {"detail": detail}


def refusal_plan(client: TestClient, actor: Actor, key: str, shape: str, reason: str) -> dict:
    plan = preview(client, actor, key, shape)
    assert [refusal["reason"] for refusal in plan["refusals"]] == [reason]
    return plan


def test_a_destination_that_already_exists_is_refused(
    client: TestClient, admin: Actor, isolated_storage: Path
) -> None:
    """Mutation: moving onto an occupied destination and clobbering it."""
    series_tree(isolated_storage)
    scan()
    plan = preview(client, admin, SERIES_KEY, "one-project")
    readme = isolated_storage / SERIES_TARGET / "keep.txt"
    write_file(readme, b"do not touch\n")
    before_tree = relative_paths(isolated_storage)
    before_rows = folder_paths(client, admin)

    response = apply_plan(client, admin, plan)

    assert_nothing_changed(
        client, admin, isolated_storage, before_tree, before_rows, response, 409, needle="already exists"
    )
    assert readme.read_bytes() == b"do not touch\n"


def test_a_cross_device_move_is_refused_before_a_byte_moves(
    client: TestClient, admin: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: falling back to a copy path, or reporting a cross-device failure after moving."""
    series_tree(isolated_storage)
    scan()
    monkeypatch.setattr(arrange_service, "_stat_device", lambda path: 1 if "17_ClockworkMonster" in str(path) else 2)
    before_tree = relative_paths(isolated_storage)
    before_rows = folder_paths(client, admin)

    plan = refusal_plan(client, admin, SERIES_KEY, "one-project", "cross_device")
    assert "nothing moved" in plan["refusals"][0]["detail"]

    response = apply_plan(client, admin, plan)

    assert_nothing_changed(
        client, admin, isolated_storage, before_tree, before_rows, response, 409, needle="nothing moved"
    )


def test_a_hidden_destination_is_a_400(
    client: TestClient, admin: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: moving library bytes into a hidden or app-owned directory."""
    series_tree(isolated_storage)
    scan()
    real = proposal_engine.arrange_moves
    monkeypatch.setattr(
        proposal_engine,
        "arrange_moves",
        lambda facts, chosen: (
            [{"from": SERIES_KEY, "to": ".hidden/ClockworkMonster", "kind": "dir", "action": "rename"}]
            if facts.rel == SERIES_KEY
            else real(facts, chosen)
        ),
    )
    before_tree = relative_paths(isolated_storage)
    before_rows = folder_paths(client, admin)

    plan = refusal_plan(client, admin, SERIES_KEY, "one-project", "hidden_destination")
    response = apply_plan(client, admin, plan)

    assert_nothing_changed(
        client, admin, isolated_storage, before_tree, before_rows, response, 400, needle=".hidden/ClockworkMonster"
    )


def test_a_destination_outside_the_storage_root_is_a_400(
    client: TestClient, admin: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: a plan whose target escapes the storage root."""
    series_tree(isolated_storage)
    scan()
    real = proposal_engine.arrange_moves
    monkeypatch.setattr(
        proposal_engine,
        "arrange_moves",
        lambda facts, chosen: (
            [
                {
                    "from": SERIES_KEY,
                    "to": "import/../../outside/ClockworkMonster",
                    "kind": "dir",
                    "action": "rename",
                }
            ]
            if facts.rel == SERIES_KEY
            else real(facts, chosen)
        ),
    )
    before_tree = relative_paths(isolated_storage)
    before_rows = folder_paths(client, admin)

    plan = refusal_plan(client, admin, SERIES_KEY, "one-project", "outside_root")
    response = apply_plan(client, admin, plan)

    assert_nothing_changed(
        client, admin, isolated_storage, before_tree, before_rows, response, 400, needle="outside the storage root"
    )


def test_a_missing_source_is_refused(
    client: TestClient, admin: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: a plan that name a source the disk does not hold."""
    series_tree(isolated_storage)
    scan()
    real = proposal_engine.arrange_moves
    monkeypatch.setattr(
        proposal_engine,
        "arrange_moves",
        lambda facts, chosen: (
            [{"from": f"{SERIES_KEY}/ghost", "to": "import/ghost", "kind": "dir", "action": "move"}]
            if facts.rel == SERIES_KEY
            else real(facts, chosen)
        ),
    )
    before_tree = relative_paths(isolated_storage)
    before_rows = folder_paths(client, admin)

    plan = refusal_plan(client, admin, SERIES_KEY, "one-project", "missing_source")
    response = apply_plan(client, admin, plan)

    assert_nothing_changed(
        client, admin, isolated_storage, before_tree, before_rows, response, 409, needle="not on disk"
    )


def test_a_pinned_folder_row_is_refused(client: TestClient, admin: Actor, isolated_storage: Path) -> None:
    """Mutation: converging a row the parent line pins elsewhere, so the scan can never heal it."""
    series_tree(isolated_storage)
    scan()
    with Session(engine) as session:
        source = session.exec(select(Folder).where(Folder.storage_path == SERIES_KEY)).one()
        sibling = session.exec(
            select(Folder).where(Folder.storage_path == f"import/{SERIES_SIBLINGS[0]}")
        ).one()
        source.parent_id = sibling.id
        session.add(source)
        session.commit()
    before_tree = relative_paths(isolated_storage)
    before_rows = folder_paths(client, admin)

    plan = refusal_plan(client, admin, SERIES_KEY, "one-project", "pinned_row")
    response = apply_plan(client, admin, plan)

    assert_nothing_changed(
        client, admin, isolated_storage, before_tree, before_rows, response, 409, needle=SERIES_KEY
    )


def test_too_many_moves_is_refused_with_the_cap_named(
    client: TestClient, admin: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: an unbounded plan that would move unbounded bytes."""
    series_tree(isolated_storage)
    scan()
    monkeypatch.setattr(arrange_service, "MAX_ARRANGE_MOVES", 0)
    before_tree = relative_paths(isolated_storage)
    before_rows = folder_paths(client, admin)

    plan = refusal_plan(client, admin, SERIES_KEY, "one-project", "too_many_moves")
    assert "declared cap of 0" in plan["refusals"][0]["detail"]
    response = apply_plan(client, admin, plan)

    assert_nothing_changed(
        client, admin, isolated_storage, before_tree, before_rows, response, 409, needle="declared cap of 0"
    )


def test_a_stale_plan_id_is_refused(client: TestClient, admin: Actor, isolated_storage: Path) -> None:
    """Mutation: applying a plan the tree has moved under since the preview."""
    series_tree(isolated_storage)
    scan()
    plan = preview(client, admin, SERIES_KEY, "one-project")
    body = {"key": plan["key"], "shape": plan["shape"], "plan_id": "0" * 64}
    before_tree = relative_paths(isolated_storage)
    before_rows = folder_paths(client, admin)

    response = client.post("/bundles/arrange", json=body, headers=bearer(admin.token))

    assert_nothing_changed(
        client, admin, isolated_storage, before_tree, before_rows, response, 409, needle="preview it again"
    )


def test_a_running_scan_is_refused(
    client: TestClient, admin: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation: arranging while a scan is rewriting the same rows."""
    series_tree(isolated_storage)
    scan()
    monkeypatch.setattr(seams, "scan_status", lambda: {"running": True})
    before_tree = relative_paths(isolated_storage)
    before_rows = folder_paths(client, admin)

    plan = refusal_plan(client, admin, SERIES_KEY, "one-project", "scan_running")
    response = apply_plan(client, admin, plan)

    assert_nothing_changed(
        client, admin, isolated_storage, before_tree, before_rows, response, 409, needle="scan is running"
    )


def test_an_archive_row_is_not_arranged(client: TestClient, admin: Actor, isolated_storage: Path) -> None:
    """Mutation: offering an archive move, which has no copy path and would need an extract."""
    write_zip(
        isolated_storage / "Box.zip",
        {
            "README.txt": THING_README.encode(),
            "LICENSE.txt": b"Vault by maker\n",
            "part.stl": STL_BYTES,
        },
    )
    scan()
    key = "Box.zip"
    before_tree = relative_paths(isolated_storage)
    before_rows = folder_paths(client, admin)

    plan = refusal_plan(client, admin, key, "one-project", "archive_not_extracted")
    assert "POST /import/archive" in plan["refusals"][0]["detail"]
    response = apply_plan(client, admin, plan)

    assert_nothing_changed(
        client,
        admin,
        isolated_storage,
        before_tree,
        before_rows,
        response,
        409,
        needle="extract it first",
    )


# --- the served surface -----------------------------------------------------


def test_the_wire_says_from_and_the_openapi_renders_from(
    client: TestClient, admin: Actor, isolated_storage: Path
) -> None:
    """Mutation: a `from_` field leaking onto the wire or into the generated schema."""
    series_tree(isolated_storage)
    scan()
    response = client.get(
        "/bundles/arrange", params={"key": SERIES_KEY, "shape": "one-project"}, headers=bearer(admin.token)
    )
    assert response.status_code == 200, response.text
    move = response.json()["moves"][0]
    assert set(move) == {"from", "to", "kind", "action"}
    assert move["action"] == "rename", "a series rename is the only token this plan can carry"
    assert "from_" not in move

    schema = client.get("/openapi.json").json()
    properties = schema["components"]["schemas"]["BundleArrangeMoveOut"]["properties"]
    assert "from" in properties and "from_" not in properties
    assert "action" in properties


def test_a_viewer_reads_the_preview_and_an_editor_may_apply(
    client: TestClient, viewer: Actor, contributor: Actor, editor: Actor, isolated_storage: Path
) -> None:
    """Mutation: the preview or the apply classified at the wrong permission level."""
    series_tree(isolated_storage)
    scan()

    plan = preview(client, viewer, SERIES_KEY, "one-project")

    denied = permissions.DENY_DETAIL[permissions.WRITE]
    for actor in (viewer, contributor):
        body = {"key": plan["key"], "shape": plan["shape"], "plan_id": plan["plan_id"]}
        arrange = client.post("/bundles/arrange", json=body, headers=bearer(actor.token))
        undo = client.post("/bundles/arrange/whatever/undo", headers=bearer(actor.token))
        assert arrange.status_code == 403, arrange.text
        assert arrange.json()["detail"] == denied
        assert undo.status_code == 403, undo.text
        assert undo.json()["detail"] == denied
    assert projects(client, viewer) == {}

    response = apply_plan(client, editor, plan)

    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True
