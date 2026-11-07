"""Extract-and-create: one project from the entries an archive promised (Mf-0ck5).

The live failure this closes: an operator pressed *Extract and create* on an
`archive_only` row and got "The project could not be created: unknown bundle",
because the flow captured the archive's bundle key before extracting and asked
detection to honour it afterwards - and the extraction had changed the identity
detection was asked about. The owner's correction was to stop asking: "we should
match the extracted files to what we knew we were going to extract. And create the
project using those files once they appear. They should bypass all the other
rules."

So this suite pins the comparison rather than the guess. Everything runs through
the real doors - `POST /import/archive` writes the bytes, a synchronous
`library_scan.scan_storage_root` registers them, `POST /import/archive/project`
creates - over a real `ZipFile` whose names are the ones the library actually
holds (`import/01_Fates End - Wizard Tower.zip`, with its models in
`01_Fates End - Wizard Tower/01_Wizard/`). There is no imagined payload here and
no fixture row that the server does not really send.

The harness is `tests/test_archive_import.py`'s: `isolated_storage` points every
path-bound module at a temp root, runs the migrations and deletes the rows a test
created; the route's scan kick is stubbed and any test that needs registration
drives the scan itself, which is synchronous.
"""

import ast
import hashlib
import json
from io import BytesIO
from pathlib import Path
from typing import Dict, List
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import event
from sqlmodel import Session, delete, select

import archive_project_service
import asset_file_service
import asset_service
import audit
import bundle_detect
import bundle_service
import db
import library_scan
import main
import project_service
import settings_service
from db import engine
from migrations import run_migrations
from models import Asset, AssetFile, AuditEvent, Folder, MfaRecoveryCode, Project, ProjectAsset, User, UserSession

STL_BYTES = b"solid wizard tower\nendsolid wizard tower\n"
README_TEXT = "Wizard Tower v5.4 by fates_end\nLicensed under CC BY-NC\n"
LICENSE_TEXT = "Wizard Tower v5.4 by fates_end on Thingiverse: https://www.thingiverse.com/thing:5432100\n"

# The archive from the live report, names and all.
ARCHIVE = "import/01_Fates End - Wizard Tower.zip"
TWIN = "import/01_Fates End - Wizard Tower"
ROOT = "01_Fates End - Wizard Tower"
MODEL_NAMES = [
    "wizard_tower_base.stl",
    "wizard_tower_shaft_a.stl",
    "wizard_tower_shaft_b.stl",
    "wizard_tower_shaft_c.stl",
    "wizard_tower_battlement.stl",
    "wizard_tower_roof.stl",
    "wizard_spire.stl",
    "wizard_staff.stl",
    "wizard_figure.stl",
    "wizard_tower_door.stl",
]
# The whole listing, in the order it is written into the archive: 21 members the
# scan can register, of which 14 are models in two subdirectories, 3 images, one
# nested archive and one document.
TOWER_ENTRIES: List[str] = [
    f"{ROOT}/README.txt",
    f"{ROOT}/LICENSE.txt",
    *[f"{ROOT}/01_Wizard/{name}" for name in MODEL_NAMES],
    f"{ROOT}/02_Grounding/grounding_base.stl",
    f"{ROOT}/02_Grounding/grounding_rocks_a.stl",
    f"{ROOT}/02_Grounding/grounding_rocks_b.stl",
    f"{ROOT}/02_Grounding/grounding_tree.stl",
    f"{ROOT}/images/preview.png",
    f"{ROOT}/images/box_art.png",
    f"{ROOT}/images/building_step_1.png",
    f"{ROOT}/docs/building_guide.txt",
    f"{ROOT}/nested/accessories.zip",
]
PLANNED = len(TOWER_ENTRIES)  # 21


# --- infrastructure ---------------------------------------------------------


def truncate_accounts() -> None:
    with Session(engine) as session:
        session.execute(delete(MfaRecoveryCode))
        session.execute(delete(UserSession))
        session.execute(delete(User))
        session.commit()


@pytest.fixture(autouse=True)
def clean_accounts():
    """Start and finish every test without accounts, so the break-glass login works."""
    run_migrations(engine)
    truncate_accounts()
    yield
    truncate_accounts()


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
    with Session(engine) as session:
        # Projects first on the way out: `idx_project_name_unique` is NOCASE, so a
        # leaked row would 409 the next test's create.
        projects_before = {project.id for project in session.exec(select(Project))}
        folders_before = {folder.id for folder in session.exec(select(Folder))}
        assets_before = {asset.id for asset in session.exec(select(Asset))}
    yield storage
    settings_service.set_setting(settings_service.BUNDLE_DISMISSALS_KEY, "")
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


@pytest.fixture
def client() -> TestClient:
    with TestClient(main.app) as test_client:
        yield test_client


def auth_header(client: TestClient) -> dict:
    """Bearer token, or empty when the app is running with auth disabled."""
    health = client.get("/health").json()
    if not health.get("auth_required"):
        return {}
    token = client.post("/login", json={"username": "admin", "password": "super-secret"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def stub_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the route's scan kick without a background thread."""
    monkeypatch.setattr(main, "start_scan", lambda: True)


# --- fixtures on disk, in the shapes the library really holds ---------------


def png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), (30, 120, 200)).save(buffer, "PNG")
    return buffer.getvalue()


def nested_zip_bytes() -> bytes:
    """A real `.zip` as an archive member, because a nested archive is a real file."""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as nested:
        nested.writestr("accessories/wand.stl", b"solid wand\nendsolid wand\n")
    return buffer.getvalue()


def tower_payload() -> Dict[str, bytes]:
    """Every member's bytes, keyed by its path inside the archive."""
    payload: Dict[str, bytes] = {}
    for name in TOWER_ENTRIES:
        if name.endswith(".zip"):
            payload[name] = nested_zip_bytes()
        elif name.endswith(".png"):
            payload[name] = png_bytes()
        elif name.endswith(".stl"):
            payload[name] = STL_BYTES
        elif name.endswith("README.txt"):
            payload[name] = README_TEXT.encode()
        elif name.endswith("LICENSE.txt"):
            payload[name] = LICENSE_TEXT.encode()
        else:
            payload[name] = b"print at 0.2mm\n"
    return payload


def write_zip(path: Path, entries: Dict[str, bytes], directories: List[str] = ()) -> Path:
    """One real archive on disk under the temp storage root."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as archive:
        for name in directories:
            archive.writestr(name, b"")
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


def staged_archive(storage: Path, entries: Dict[str, bytes], directories: List[str] = ()) -> Path:
    return write_zip(storage / ARCHIVE, entries, directories)


def extract(client: TestClient, headers: dict, path: str = ARCHIVE, entries: List[str] | None = None) -> dict:
    response = client.post("/import/archive", json={"path": path, "entries": entries}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def scan() -> None:
    """Register what is on disk. Synchronous, so a test cannot race the walk."""
    state = library_scan.scan_storage_root()
    assert state.failed == 0, state.errors


def create(client: TestClient, headers: dict, path: str = ARCHIVE, request_id: str = "", **body) -> dict:
    sent = {**headers, "X-Request-ID": request_id} if request_id else headers
    response = client.post("/import/archive/project", json={"path": path, **body}, headers=sent)
    assert response.status_code == 200, response.text
    return response.json()


def full_run(client: TestClient, headers: dict, monkeypatch: pytest.MonkeyPatch, storage: Path) -> dict:
    """The whole flow: write the archive, extract it, let the scan register it, create."""
    staged_archive(storage, tower_payload(), directories=[f"{ROOT}/"])
    stub_scan(monkeypatch)
    assert extract(client, headers)["destination"] == TWIN
    scan()
    return create(client, headers)


def member_paths(project_id: str) -> List[str]:
    """The registered `storage_path` of every member, sorted."""
    with Session(engine) as session:
        rows = session.exec(
            select(Asset.storage_path)
            .join(ProjectAsset, ProjectAsset.asset_id == Asset.id)
            .where(ProjectAsset.project_id == project_id)
        ).all()
    return sorted(rows)


def projects() -> List[Project]:
    with Session(engine) as session:
        return list(session.exec(select(Project)))


def marker_of(project_id: str) -> dict:
    with Session(engine) as session:
        project = session.get(Project, project_id)
        assert project is not None
        return archive_project_service.archive_project_marker_of(project.metadata_json)


def events_for(value: str) -> List[AuditEvent]:
    """The rows one request wrote. `AuditEvent` is not undone between tests, so an
    audit assertion is scoped to its own request id, as `tests/test_audit_log.py`
    scopes its sweep."""
    with Session(engine) as session:
        return list(
            session.exec(
                select(AuditEvent).where(
                    AuditEvent.request_id == value,
                    AuditEvent.action == audit.ARCHIVE_PROJECT_CREATE,
                )
            )
        )


def only_row(value: str) -> AuditEvent:
    rows = events_for(value)
    assert len(rows) == 1, [row.action for row in rows]
    return rows[0]


# --- AC-1: the planned entries become one project, no rule consulted --------


def test_the_planned_entries_become_one_project_with_every_asset_attached(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ticket's one action, on the archive from the live report.

    21 planned entries, 21 registered assets, one project - and the members are
    the files under the twin's own subdirectories, which is where the extracted
    tree actually puts them.
    """
    headers = auth_header(client)
    body = full_run(client, headers, monkeypatch, isolated_storage)

    assert body["status"] == "created"
    assert body["archive"] == ARCHIVE
    assert body["twin"] == TWIN
    assert body["planned"] == PLANNED
    assert body["resolved"] == PLANNED
    assert body["failed"] == []
    project = body["project"]
    assert project is not None
    assert project["name"] == ROOT, "the default name is the twin directory's own"
    assert project["asset_count"] == PLANNED
    assert project["origin"] == "own"

    expected = sorted(f"{TWIN}/{name}" for name in TOWER_ENTRIES)
    assert member_paths(project["id"]) == expected
    # The models are one directory deeper than the twin, which is exactly the
    # shape that made the old flow answer `unknown bundle`.
    assert f"{TWIN}/{ROOT}/01_Wizard/wizard_tower_base.stl" in member_paths(project["id"])


def test_the_cover_is_the_first_member_the_library_calls_a_model(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cover is a courtesy, so it is picked by mime, never by a detector's rules."""
    headers = auth_header(client)
    body = full_run(client, headers, monkeypatch, isolated_storage)

    cover_id = body["project"]["cover_asset_id"]
    with Session(engine) as session:
        cover = session.get(Asset, cover_id)
        assert cover is not None
        assert cover.mime == "model/stl"
        assert cover.storage_path == f"{TWIN}/{ROOT}/01_Wizard/wizard_tower_base.stl"


def test_a_plan_larger_than_one_query_batch_loses_no_member(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolve query is batched, and a chunk boundary cannot drop a member.

    `_resolve` reads the expected paths back in batches of
    `archive_project_service.RESOLVE_QUERY_BATCH` because SQLite caps how many host
    variables one statement may bind. The pin is on the batching, not on the
    constant's value: an archive one batch plus seven members wide spans at least
    two chunks however the constant is later tuned, so a merge that dropped a
    batch - or re-keyed rows onto the wrong batch - shows up here as a `partial`
    refusal rather than as a project with every member.

    A real archive of that size is used rather than a monkeypatched-down constant,
    because the number of variables the driver will actually bind is the thing that
    broke; the 507 tiny `.stl` members it costs are a second of runtime, not a slow
    test.
    """
    headers = auth_header(client)
    total = archive_project_service.RESOLVE_QUERY_BATCH + 7
    bulk_root = "01_Bulk"
    entries = [f"{bulk_root}/part_{index:04d}.stl" for index in range(total)]
    staged_archive(isolated_storage, {name: STL_BYTES for name in entries})
    stub_scan(monkeypatch)

    assert extract(client, headers)["files"] == total
    scan()
    body = create(client, headers)

    assert body["status"] == "created", "a chunk that resolved nothing would refuse as partial"
    assert body["planned"] == total
    assert body["resolved"] == total
    assert body["failed"] == []
    assert body["project"] is not None
    assert body["project"]["asset_count"] == total
    assert member_paths(body["project"]["id"]) == sorted(f"{TWIN}/{name}" for name in entries)


def test_no_bundle_rule_is_consulted_on_this_path(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1's second half: "none of them may be consulted on this path".

    Two independent proofs, because a comment can drift and an import can be
    added by the next refactor:

    * the door's transitive import closure contains no bundle module at all -
      and not `archive_service`, which would pull `bundle_detect` in through its
      twin-pairing check;
    * every rule function that could answer "what is this folder" explodes if it
      is called, and the create still succeeds.
    """
    headers = auth_header(client)
    staged_archive(isolated_storage, tower_payload())
    stub_scan(monkeypatch)
    extract(client, headers)
    scan()

    forbidden = {"bundle_detect", "bundle_service", "bundle_drift", "archive_service"}
    for module_name in sorted(reachable_modules("archive_project_service")):
        assert module_name not in forbidden, f"{module_name} is in the door's import closure"

    def explode(*_args, **_kwargs):
        raise AssertionError("a bundle rule was consulted on the extract-and-create path")

    for name in (
        "archive_owned_decision",
        "archive_owned_facts",
        "common_entry_root",
        "is_counted_model",
        "is_bundle_member",
        "twin_key",
        "rule_set",
        "rule_set_tables",
        "duplicate_groups",
        "generic_doc_candidates",
    ):
        monkeypatch.setattr(bundle_detect, name, explode)
    for name in ("scan_bundles", "iter_bundle_candidates", "create_bundles", "find_existing_project"):
        monkeypatch.setattr(bundle_service, name, explode)

    body = create(client, headers)
    assert body["status"] == "created"
    assert body["resolved"] == PLANNED


def reachable_modules(entry_point: str) -> set:
    """Every local module reachable from `entry_point` through its imports.

    Walked with `ast` rather than by importing, so a module that would explode on
    import - or one already sitting in `sys.modules` because another test imported
    it - cannot hide the edge. Sub-packages are walked too: `schemas` is a package,
    and a detector import smuggled into a schema module is exactly the drift this
    guard is for.

    Relative imports are edges like any other, so they are resolved against the
    importing module's own package rather than skipped. They have to be: the schema
    barrel reaches its ~fifteen submodules through nothing but `from . import ...`
    and `from .bundles import *`, so a walker blind to level-1 imports would leave
    every one of them un-walked and a detector import put there would pass - which
    is the exact drift the docstring of this module claims to refuse.
    """
    api_root = Path(archive_project_service.__file__).parent
    modules = {path.stem for path in api_root.glob("*.py")}
    packages = {path.name for path in api_root.iterdir() if (path / "__init__.py").is_file()}
    known = modules | packages

    def source(name: str) -> Path | None:
        """The file a dotted, `api_root`-relative module name is written in."""
        candidate = api_root.joinpath(*name.split(".")) if name else api_root
        module_path = candidate.with_suffix(".py")
        if module_path.is_file():
            return module_path
        package_init = candidate / "__init__.py"
        return package_init if package_init.is_file() else None

    def resolve(name: str, level: int, module: str | None) -> str:
        """An `ImportFrom` target as a dotted name local to `api_root`.

        `level` 1 is the package containing the importing module and every further
        level walks up one more; a package `__init__` sits one level shallower than
        a plain module, because its own directory *is* that package. A target that
        resolves outside the local tree simply lands outside `known` and is not
        followed - the walk reports what it can see, it never crashes on what it
        cannot.
        """
        parts = name.split(".") if name else []
        if parts and name not in packages:
            parts = parts[:-1]
        for _up in range(level - 1):
            if parts:
                parts.pop()
        if module:
            parts.extend(module.split("."))
        return ".".join(parts)

    seen: set = set()
    stack = [entry_point]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        path = source(name)
        if path is None:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            found: List[str] = []
            if isinstance(node, ast.Import):
                found = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    if node.module:
                        found = [node.module, *[f"{node.module}.{alias.name}" for alias in node.names]]
                else:
                    base = resolve(name, node.level, node.module)
                    found = [base, *[f"{base}.{alias.name}" for alias in node.names]]
            for target in found:
                head = target.split(".")[0]
                if head in known:
                    # A real submodule is walked as itself; anything deeper is a
                    # name inside a module, whose edge belongs to that module.
                    stack.append(target if source(target) is not None else head)
    return seen


# --- the three facts that must still refuse ---------------------------------


def test_a_partial_extraction_creates_no_project_and_says_how_many_entries_failed(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-3: the server's own listing said 21 would be written; only some were.

    The extraction is all-or-nothing for the entries it was *given*, so the real
    partial is the operator extracting a subset and then asking for the project:
    the door compares the whole listing against what is registered and refuses.
    """
    headers = auth_header(client)
    staged_archive(isolated_storage, tower_payload())
    stub_scan(monkeypatch)
    subset = [name for name in TOWER_ENTRIES if name.startswith(f"{ROOT}/01_Wizard/")]
    assert extract(client, headers, entries=subset)["files"] == len(subset)
    scan()

    body = create(client, headers)

    assert body["status"] == "partial"
    assert body["planned"] == PLANNED
    assert body["resolved"] == len(subset)
    assert len(body["failed"]) == PLANNED - len(subset)
    assert body["failed"] == sorted(name for name in TOWER_ENTRIES if name not in subset)
    assert f"{ROOT}/images/preview.png" in body["failed"]
    assert body["project"] is None, "a partial extraction creates nothing at all"
    assert projects() == [], "and leaves no empty project behind"


def test_a_partial_extraction_still_creates_once_the_rest_arrives(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal must be retriable, not a dead end: nothing was written to undo."""
    headers = auth_header(client)
    staged_archive(isolated_storage, tower_payload())
    stub_scan(monkeypatch)
    subset = TOWER_ENTRIES[:3]
    extract(client, headers, entries=subset)
    scan()
    assert create(client, headers)["status"] == "partial"

    extract(client, headers, entries=TOWER_ENTRIES[3:])
    scan()
    body = create(client, headers)

    assert body["status"] == "created"
    assert body["resolved"] == PLANNED
    assert len(projects()) == 1


def test_a_settle_that_resolves_nothing_names_the_twin_and_creates_nothing(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-4: the files are on disk, but no scan ever registered them.

    This is the settle that never happened. The door reads registered assets and
    nothing else, so it creates nothing and names the directory to go and look in.
    """
    headers = auth_header(client)
    staged_archive(isolated_storage, tower_payload())
    stub_scan(monkeypatch)
    result = extract(client, headers)
    assert result["files"] == PLANNED, "the bytes really were written"
    assert (isolated_storage / TWIN / ROOT / "01_Wizard" / "wizard_tower_base.stl").is_file()

    body = create(client, headers)

    assert body["status"] == "nothing_resolved"
    assert body["twin"] == TWIN, "the message the operator reads is built from this"
    assert body["planned"] == PLANNED
    assert body["resolved"] == 0
    assert len(body["failed"]) == PLANNED
    assert body["project"] is None
    assert projects() == []


def test_an_archive_that_was_never_extracted_resolves_nothing_too(
    client: TestClient, isolated_storage: Path
) -> None:
    """The other zero: nothing was extracted at all, so nothing can resolve."""
    headers = auth_header(client)
    staged_archive(isolated_storage, tower_payload())

    body = create(client, headers)

    assert body["status"] == "nothing_resolved"
    assert body["twin"] == TWIN
    assert body["resolved"] == 0
    assert projects() == []


def test_a_second_run_on_the_same_archive_is_already_created_never_a_duplicate(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-2, keyed by archive path: the second click answers, it does not create."""
    headers = auth_header(client)
    first = full_run(client, headers, monkeypatch, isolated_storage)
    project_id = first["project"]["id"]

    second = create(client, headers)

    assert second["status"] == "already_created"
    assert second["project"]["id"] == project_id
    assert [project.id for project in projects()] == [project_id]
    assert len(member_paths(project_id)) == PLANNED, "and re-attaches nothing"


def test_the_repeat_answer_survives_the_archive_being_gone(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotency is keyed by the archive path, so the lookup precedes the read.

    An extraction consumes its archive: the operator deletes it, or the twin is
    what the library keeps. A door that opened the zip first would answer 404
    here, and a 404 on the operator's own retry reads like their mistake.
    """
    headers = auth_header(client)
    first = full_run(client, headers, monkeypatch, isolated_storage)
    (isolated_storage / ARCHIVE).unlink()

    body = create(client, headers)

    assert body["status"] == "already_created"
    assert body["project"]["id"] == first["project"]["id"]


def test_a_different_archive_is_a_different_project(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keying by archive path must not become keying by name."""
    headers = auth_header(client)
    first = full_run(client, headers, monkeypatch, isolated_storage)

    other = "import/02_Fates End - Wizard Tower.zip"
    staged_archive(isolated_storage, tower_payload(), directories=[])
    write_zip(isolated_storage / other, tower_payload())
    extract(client, headers, path=other)
    scan()
    body = create(client, headers, path=other)

    assert body["status"] == "created"
    assert body["project"]["id"] != first["project"]["id"]
    assert len(projects()) == 2, "the second archive is its own project, not a duplicate"


# --- design point 5: the marker a later reader can tell the story from ------


def test_the_marker_records_the_archive_the_counts_the_groups_and_the_fingerprint(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator-declared project must be tellable from a detected one."""
    headers = auth_header(client)
    body = full_run(client, headers, monkeypatch, isolated_storage)
    marker = marker_of(body["project"]["id"])

    assert marker["archive"] == ARCHIVE
    assert marker["twin"] == TWIN
    assert marker["planned"] == PLANNED
    assert marker["resolved"] == PLANNED
    assert marker["common_root"] == ROOT, "the listing's own single root directory"
    assert marker["groups"] == [
        {"name": "01_Wizard", "count": 10},
        {"name": "02_Grounding", "count": 4},
        {"name": "docs", "count": 1},
        {"name": "images", "count": 3},
        {"name": "nested", "count": 1},
    ], "groups are the common root's subdirectories, not a detector's answer"
    assert marker["fingerprint"] == hashlib.sha256("\n".join(sorted(TOWER_ENTRIES)).encode()).hexdigest()[:16]

    with Session(engine) as session:
        project = session.get(Project, body["project"]["id"])
        stored = json.loads(project.metadata_json)
    assert set(stored) == {"_archive_project"}, "the marker reserves its own key and writes no other"
    assert "_bundle" not in stored, "this project was declared, not detected"


def test_a_listing_with_no_single_root_has_no_common_root_and_no_groups(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flat archive is not a mis-shapen one: the root is simply the twin itself."""
    headers = auth_header(client)
    flat = {f"model_{index}.stl": STL_BYTES for index in range(3)}
    flat["notes.txt"] = b"print at 0.2mm\n"
    staged_archive(isolated_storage, flat)
    stub_scan(monkeypatch)
    extract(client, headers)
    scan()

    body = create(client, headers)

    assert body["status"] == "created"
    assert body["resolved"] == 4
    marker = marker_of(body["project"]["id"])
    assert marker["common_root"] is None
    assert marker["groups"] == []


def test_groups_come_from_the_listing_even_when_the_scan_named_the_folders_differently(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The groups are the listing's, so they survive whatever the folder rows say."""
    headers = auth_header(client)
    body = full_run(client, headers, monkeypatch, isolated_storage)
    marker = marker_of(body["project"]["id"])

    with Session(engine) as session:
        folders = {
            folder.storage_path
            for folder in session.exec(select(Folder)).all()
            if folder.storage_path and folder.storage_path.startswith(TWIN)
        }
    assert folders, "the scan made its own folder rows from the same tree"
    assert [group["name"] for group in marker["groups"]] == ["01_Wizard", "02_Grounding", "docs", "images", "nested"]


# --- the plan's risk: a dotfile must not refuse the door forever ------------


def test_junk_hidden_and_directory_members_cannot_refuse_the_door_forever(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Mac-made archive is the common case, not the edge case.

    `__MACOSX/`, `.DS_Store`, `Thumbs.db`, a dotfile and a directory entry are all
    written by real extraction tools. None of them can ever become a registered
    asset, because the scan prunes them - so if any of them counted as "planned",
    every archive with one would answer `partial` forever.
    """
    headers = auth_header(client)
    payload = tower_payload()
    payload.update(
        {
            "__MACOSX/._wizard_tower_base.stl": b"AppleDouble\n",
            ".DS_Store": b"\x00\x00",
            f"{ROOT}/Thumbs.db": b"\x00\x00",
            f"{ROOT}/.hidden/handy.stl": STL_BYTES,
            f"{ROOT}/.gitignore": b"*\n",
        }
    )
    staged_archive(isolated_storage, payload, directories=[f"{ROOT}/", f"{ROOT}/01_Wizard/"])
    stub_scan(monkeypatch)
    extract(client, headers)
    scan()

    body = create(client, headers)

    assert body["status"] == "created"
    assert body["planned"] == PLANNED, "the unregistrable members are not planned members"
    assert body["resolved"] == PLANNED
    assert body["failed"] == []


# --- one transaction -------------------------------------------------------


def test_a_name_collision_creates_nothing_rather_than_an_empty_project(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single commit is the whole promise: `create_project`+`add_assets` is two.

    `idx_project_name_unique` is NOCASE, and it only speaks at flush time - which
    is exactly why the members have to be in the same transaction as the project.
    """
    headers = auth_header(client)
    staged_archive(isolated_storage, tower_payload())
    stub_scan(monkeypatch)
    extract(client, headers)
    scan()
    with Session(engine) as session:
        project_service.create_project(session, ROOT.lower(), None, None)

    response = client.post("/import/archive/project", json={"path": ARCHIVE}, headers=headers)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Project name already exists"
    assert len(projects()) == 1, "no second project, not even an empty one"
    with Session(engine) as session:
        assert session.exec(select(ProjectAsset)).all() == []


def test_an_unknown_member_id_refuses_the_whole_create_without_leaving_a_project() -> None:
    """`add_assets` can afford a per-id failure; a creating door cannot."""
    with Session(engine) as session:
        asset = Asset(name="one", filename="one.stl", mime="model/stl", size=1, storage_path="loose/one.stl")
        session.add(asset)
        session.commit()
        asset_id = asset.id

    with Session(engine) as session, pytest.raises(Exception) as refused:
        project_service.create_project_with_members(session, "Stranded", None, None, [asset_id, "nope"])
    assert getattr(refused.value, "status_code", None) == 404

    with Session(engine) as session:
        assert session.exec(select(Project).where(Project.name == "Stranded")).all() == []
        assert session.exec(select(ProjectAsset)).all() == []


def test_the_member_lookup_still_attaches_every_member_on_a_smaller_query_batch(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existence check is batched, and a chunk boundary cannot drop a member.

    `_require_assets` reads the member ids back in batches of
    `project_service.MEMBER_QUERY_BATCH` for the same reason `_resolve` batches:
    SQLite caps the host variables one statement may bind, so one ``IN`` list over
    a huge archive's members raises `too many SQL variables` and reaches the
    operator as a 500 from this very request. The pin is on the batching, not on
    the constant's value: the constant is driven down to 4 while the archive from
    the live report has 21 members, so the lookup spans six chunks - and a merge
    that dropped one would 404 the create rather than attach all 21. The queries
    themselves are counted too, because a lookup that quietly ignored the constant
    would pass a pure outcome assertion.
    """
    headers = auth_header(client)
    batch = 4
    monkeypatch.setattr(project_service, "MEMBER_QUERY_BATCH", batch)
    member_queries: List[str] = []

    def watch(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        # `asset.id IN (...)` is this one lookup: `_resolve`'s query is on
        # `asset.storage_path`, and the cover's read is a plain `= ?`.
        if "asset.id IN" in statement:
            member_queries.append(statement)

    event.listen(engine, "before_cursor_execute", watch)
    try:
        body = full_run(client, headers, monkeypatch, isolated_storage)
    finally:
        event.remove(engine, "before_cursor_execute", watch)

    assert body["status"] == "created", "a chunk whose ids went unseen would 404 the create"
    assert len(member_queries) == (PLANNED + batch - 1) // batch, (
        "one query per batch, however small the batch is made - a single query over all "
        f"{PLANNED} ids is exactly what the SQLite variable cap forbids"
    )
    assert body["planned"] == PLANNED
    assert body["resolved"] == PLANNED
    assert body["failed"] == []
    assert body["project"] is not None
    assert body["project"]["asset_count"] == PLANNED
    assert member_paths(body["project"]["id"]) == sorted(f"{TWIN}/{name}" for name in TOWER_ENTRIES)


def test_a_member_missing_from_a_later_batch_is_refused_and_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batching must not soften the check: a miss in a later chunk is still a 404.

    Ten ids over a batch of four is three chunks, and the unknown one sits in the
    third. The two earlier chunks finding everything they hold is no reason to
    pass, and the refusal still names the id it could not find - the same 404 the
    single-query version wrote.
    """
    monkeypatch.setattr(project_service, "MEMBER_QUERY_BATCH", 4)
    with Session(engine) as session:
        asset_ids = []
        for index in range(9):
            asset = Asset(
                name=f"part {index}",
                filename=f"part{index}.stl",
                mime="model/stl",
                size=1,
                storage_path=f"chunked/part{index}.stl",
            )
            session.add(asset)
            session.commit()
            asset_ids.append(asset.id)

    with Session(engine) as session, pytest.raises(Exception) as refused:
        project_service.create_project_with_members(
            session, "LaterBatchMissing", None, None, [*asset_ids, "nope-in-the-third-batch"]
        )
    assert getattr(refused.value, "status_code", None) == 404
    assert "nope-in-the-third-batch" in getattr(refused.value, "detail", "")

    with Session(engine) as session:
        assert session.exec(select(Project).where(Project.name == "LaterBatchMissing")).all() == []
        assert session.exec(select(ProjectAsset)).all() == []


def test_members_join_in_the_order_the_listing_gave_them() -> None:
    """The order is the listing's, and the listing is the evidence."""
    with Session(engine) as session:
        assets = []
        for index in range(3):
            asset = Asset(
                name=f"part {index}",
                filename=f"part{index}.stl",
                mime="model/stl",
                size=1,
                storage_path=f"ordered/part{index}.stl",
            )
            session.add(asset)
            session.commit()
            assets.append(asset.id)

    with Session(engine) as session:
        project = project_service.create_project_with_members(session, "Ordered", None, None, list(reversed(assets)))
        project_id = project.id

    with Session(engine) as session:
        rows = session.exec(
            select(ProjectAsset).where(ProjectAsset.project_id == project_id).order_by(ProjectAsset.position)
        ).all()
    assert [row.asset_id for row in rows] == list(reversed(assets))
    assert [row.position for row in rows] == [0, 1, 2]
    assert {row.role for row in rows} == {"member"}


# --- the two doors must agree about where the files live --------------------


def test_the_twin_name_matches_what_the_extract_door_actually_wrote(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`archive_project_service` recomputes the twin instead of importing it.

    It has to: importing `archive_service` would put `bundle_detect` in this
    door's closure. The price is a second naming rule, so the agreement with the
    door that wrote the bytes is pinned rather than assumed - the response's own
    `destination` is the authority here, not a name this test invented.
    """
    headers = auth_header(client)
    for archive, expected in (
        (ARCHIVE, TWIN),
        ("Moai.zip", "Moai"),
        ("import/vendors/Fates End - Wizard Tower.zip", "import/vendors/Fates End - Wizard Tower"),
    ):
        write_zip(isolated_storage / archive, {f"{ROOT}/part.stl": STL_BYTES})
        stub_scan(monkeypatch)
        assert extract(client, headers, path=archive)["destination"] == expected
        scan()
        body = create(client, headers, path=archive)
        assert body["twin"] == expected
        assert body["status"] == "created"


def test_an_archive_with_no_usable_twin_name_refuses_like_the_extract_door_does(
    client: TestClient, isolated_storage: Path
) -> None:
    """The one refusal the twin rule can raise, and it agrees with the other door.

    A hidden destination would be pruned by the scan, so neither door will write
    one; both say the same sentence, which is what an operator reading either
    refusal needs.
    """
    headers = auth_header(client)
    hidden = "import/.Wizard.zip"
    write_zip(isolated_storage / hidden, {"part.stl": STL_BYTES})

    extract_response = client.post("/import/archive", json={"path": hidden}, headers=headers)
    door = client.post("/import/archive/project", json={"path": hidden}, headers=headers)

    assert extract_response.status_code == 400
    assert door.status_code == 400, door.text
    assert door.json()["detail"] == extract_response.json()["detail"]


# --- the door's own refusals -------------------------------------------------


def test_a_selection_that_names_an_entry_the_archive_does_not_hold_is_a_400(
    client: TestClient, isolated_storage: Path
) -> None:
    """One selection means the same thing at both doors, including its refusals."""
    headers = auth_header(client)
    staged_archive(isolated_storage, tower_payload())

    response = client.post(
        "/import/archive/project",
        json={"path": ARCHIVE, "entries": [f"{ROOT}/README.txt", f"{ROOT}/nope.stl"]},
        headers=headers,
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"].startswith("Selection names entries that are not in the archive:")
    assert f"{ROOT}/nope.stl" in response.json()["detail"]


def test_a_path_the_library_does_not_hold_and_a_file_that_is_not_an_archive_refuse(
    client: TestClient, isolated_storage: Path
) -> None:
    headers = auth_header(client)
    staged_archive(isolated_storage, tower_payload())

    missing = client.post("/import/archive/project", json={"path": "import/never-arrived.zip"}, headers=headers)
    assert missing.status_code == 404, missing.text
    traversal = client.post("/import/archive/project", json={"path": "../etc/passwd.zip"}, headers=headers)
    assert traversal.status_code == 400, traversal.text
    reserved = client.post("/import/archive/project", json={"path": "thumbs/inside.zip"}, headers=headers)
    assert reserved.status_code == 400, reserved.text
    (isolated_storage / "import/notes.txt").write_bytes(b"not an archive\n")
    not_a_zip = client.post("/import/archive/project", json={"path": "import/notes.txt"}, headers=headers)
    assert not_a_zip.status_code == 415, not_a_zip.text
    assert not projects()


def test_a_selection_whose_only_members_are_junk_resolves_nothing(
    client: TestClient, isolated_storage: Path
) -> None:
    """Nothing in the selection can ever be an asset: say so, and create nothing."""
    headers = auth_header(client)
    staged_archive(isolated_storage, {"__MACOSX/._x.stl": b"AppleDouble\n", ".DS_Store": b"\x00"})

    body = create(client, headers)

    assert body["status"] == "nothing_resolved"
    assert body["planned"] == 0
    assert body["resolved"] == 0
    assert body["twin"] == TWIN
    assert projects() == []


# --- the operator's words ---------------------------------------------------


def test_the_typed_name_is_used_trimmed_and_whitespace_falls_back_to_the_twin(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's name wins; blank is not a name, so the twin's is."""
    headers = auth_header(client)
    staged_archive(isolated_storage, tower_payload())
    stub_scan(monkeypatch)
    extract(client, headers)
    scan()

    named = create(client, headers, name="  Wizard v5.4  ")
    assert named["project"]["name"] == "Wizard v5.4"

    # A second create on that archive is already-created, so the fallback needs its
    # own archive rather than a second click on this one.
    second = "import/02_Wizard v5.4.zip"
    write_zip(isolated_storage / second, tower_payload())
    extract(client, headers, path=second)
    scan()
    blank = create(client, headers, path=second, name="   ")
    assert blank["status"] == "created"
    assert blank["project"]["name"] == "02_Wizard v5.4", "the fallback is that twin's own name"


# --- the record -------------------------------------------------------------


def test_every_verdict_records_one_audit_row(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The click is the evidence, so a refused run is in the history too.

    Three clicks, three rows - including the refusal that created nothing, which a
    history of successes only would leave indistinguishable from a click that
    never happened.
    """
    headers = auth_header(client)
    staged_archive(isolated_storage, tower_payload())
    stub_scan(monkeypatch)

    refused, created, replayed = (f"verdict-{uuid4().hex}" for _ in range(3))

    assert create(client, headers, request_id=refused)["status"] == "nothing_resolved"
    refusal = only_row(refused)
    assert json.loads(refusal.detail_json)["project_id"] is None

    extract(client, headers)
    scan()

    body = create(client, headers, request_id=created)
    assert body["status"] == "created"
    row = only_row(created)
    assert row.action == audit.ARCHIVE_PROJECT_CREATE
    assert row.target == ARCHIVE
    assert row.method == "POST"
    assert row.actor_role == "admin"
    assert {row.action} <= audit.ACTIONS
    detail = json.loads(row.detail_json)
    assert detail == {
        "status": "created",
        "twin": TWIN,
        "planned": PLANNED,
        "resolved": PLANNED,
        "project_id": body["project"]["id"],
    }

    assert create(client, headers, request_id=replayed)["status"] == "already_created"
    assert json.loads(only_row(replayed).detail_json)["project_id"] == body["project"]["id"]


def test_the_audit_row_names_the_refusal_when_nothing_was_created(
    client: TestClient, isolated_storage: Path
) -> None:
    """The other refusal, on its own: a click with nothing registered behind it."""
    headers = auth_header(client)
    staged_archive(isolated_storage, tower_payload())
    value = f"refusal-{uuid4().hex}"

    create(client, headers, request_id=value)

    row = only_row(value)
    assert row.target == ARCHIVE
    detail = json.loads(row.detail_json)
    assert detail["status"] == "nothing_resolved"
    assert detail["project_id"] is None
    assert detail["twin"] == TWIN
