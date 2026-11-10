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
import dataclasses
import hashlib
import json
import mimetypes
import posixpath
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Sequence, Tuple
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
import file_utils
import library_scan
import main
import project_create_service
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


def write_tree(storage: Path, payload: Dict[str, bytes], omit: Sequence[str] = ()) -> List[str]:
    """Write payload bytes into the twin by hand, exactly as an ordinary unzip would.

    No `/import/archive` call is involved: this is the tree that arrived by `rsync`,
    by a snapshot restore, or by the operator's own `unzip` beside the zip - the
    state a plain library scan then registers. `omit` drops known members so a test
    can build a tree that is short by a number it chose rather than by luck.

    Returns every file's library-relative path, sorted, so an assertion compares
    against the tree the test built instead of against a literal it hoped for.
    """
    omitted = set(omit)
    unknown = omitted - set(payload)
    assert not unknown, f"omitting {sorted(unknown)} asks for members the payload does not hold"
    written: List[str] = []
    for name, content in payload.items():
        if name in omitted:
            continue
        target = storage / TWIN / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        written.append(posixpath.join(TWIN, name))
    return sorted(written)


def files_under(storage: Path, rel: str) -> List[str]:
    """Every file under `rel`, read off disk, as sorted library-relative paths.

    A fixture that contradicts the wire is this repo's recurring defect, so the walk
    is the evidence and the expectation is what gets compared against it. Nothing is
    filtered here - dotfiles and all - because a tree a test did not intend to write
    is exactly what this function is for.
    """
    base = storage / rel
    if not base.is_dir():
        return []
    return sorted(posixpath.join(rel, path.relative_to(base).as_posix()) for path in base.rglob("*") if path.is_file())


def fingerprint_tree(storage: Path, rel: str) -> Dict[str, Tuple[str, int]]:
    """Every file under `rel` as `{library relative path: (sha256, st_mtime_ns)}`.

    Both halves are load-bearing and neither implies the other: the hash says the
    bytes are the same, which a rewrite of identical bytes satisfies perfectly; the
    mtime is the thing a rewrite cannot keep.
    """
    base = storage / rel
    tree: Dict[str, Tuple[str, int]] = {}
    for path in sorted(base.rglob("*")):
        if path.is_file():
            key = posixpath.join(rel, path.relative_to(base).as_posix())
            tree[key] = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    return tree


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
    """This door's payload as one reader returns it.

    `project_create_service.archive_extract_payload_of` is the only reader of it:
    the door writes the payload and no longer knows how to find it again. It reads
    `_bundle["archive_extract"]` and falls back to the legacy `_archive_project`
    block, so rows written before the namespace existed still tell their story.
    """
    with Session(engine) as session:
        project = session.get(Project, project_id)
        assert project is not None
        return project_create_service.archive_extract_payload_of(project.metadata_json)


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


def extract_rows_for(value: str) -> List[AuditEvent]:
    """The `archive.extract` rows one request id carries - the OTHER door's action.

    `events_for` can only see this door's own action, so it cannot tell a create that
    extracted from a create that only compared. A create that started extracting on
    the operator's behalf would record through the same `audit.AuditContext`, i.e.
    under the create request's own id, so an empty list here says the extract door
    never ran inside this request rather than merely that some row is missing.
    """
    with Session(engine) as session:
        return list(
            session.exec(
                select(AuditEvent).where(
                    AuditEvent.request_id == value,
                    AuditEvent.action == audit.ARCHIVE_EXTRACT,
                )
            )
        )


def extract_row_count() -> int:
    """How many `archive.extract` rows the table holds, however old.

    `AuditEvent` is not undone between tests, so a test in the middle of this file
    cannot assert a global zero - earlier tests really did extract. It can assert
    that the count standing before its own clicks is the count still standing after,
    which is the same claim about this test and immune to its neighbours.
    """
    with Session(engine) as session:
        return len(list(session.exec(select(AuditEvent).where(AuditEvent.action == audit.ARCHIVE_EXTRACT))))


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


def test_the_cover_predicate_is_an_extension_decision_with_no_mime_anywhere() -> None:
    """Which member gets a face is answered from the path, and from nothing else.

    Three things are pinned here at once, none of which an end-to-end run can show:

    * every suffix the library calls a model is chosen - all five, each with a
      documents-only member sitting in front of it, and with no `mime` anywhere in
      the room. A row's mime is a string some host wrote, so the door cannot hold
      it as evidence;
    * a project with only documents keeps having no face, which is what it has
      always had - the extension rule must not start inventing one out of a
      `.txt`;
    * `ResolvedMember` carries no `mime` field at all. That is structural, not a
      behavioural preference: with no column on the member, "a document whose
      upload door was handed `Content-Type: model/stl` hijacks the face" is not
      merely untested here, it is unrepresentable - there is nowhere to put the
      value a client declared.

    The exact-suffix-set assertion makes a silent widening or narrowing of
    `file_utils.MODEL_SUFFIXES` visible in this test rather than in production:
    the set is pinned against the web copies by `scripts/check_model_table.py`,
    and the two assertions have to agree.
    """
    assert file_utils.MODEL_SUFFIXES == {".stl", ".3mf", ".step", ".stp", ".obj"}
    assert "mime" not in {field.name for field in dataclasses.fields(archive_project_service.ResolvedMember)}

    def member(name: str) -> archive_project_service.ResolvedMember:
        """One planned entry that resolved - the only three columns it can carry."""
        return archive_project_service.ResolvedMember(
            asset_id=f"asset-{name}",
            entry=f"{ROOT}/{name}",
            rel_path=f"{TWIN}/{ROOT}/{name}",
        )

    documents = [member("README.txt"), member("box_art.png")]
    for suffix in sorted(file_utils.MODEL_SUFFIXES):
        chosen = archive_project_service._cover_asset_id([*documents, member(f"part{suffix}")])
        assert chosen == f"asset-part{suffix}", f"{suffix} is a model the door must name without a mime"

    # `.3mf` spelled out, because it is the member that fails on the *host* too:
    # `/etc/mime.types` there answers `application/vnd.ms-3mfdocument`, which no
    # `model/` prefix ever matched, so this case was broken before the container
    # symptom was ever noticed.
    assert archive_project_service._cover_asset_id([*documents, member("pack.3mf")]) == "asset-pack.3mf"

    assert archive_project_service._cover_asset_id(documents) is None, "documents alone must not grow a face"
    assert archive_project_service._cover_asset_id([]) is None
    # Storage paths come off disk as written, so the rule cannot be case-sensitive.
    assert archive_project_service._cover_asset_id([*documents, member("part.STL")]) == "asset-part.STL"


def test_the_cover_survives_a_host_that_knows_no_model_mime(
    client, isolated_storage, monkeypatch
) -> None:
    """The `python:3.12-slim` condition, reproduced in-repo. The regression test.

    What shipped: every project created by *Extract and create* on the published
    image got a blank card. The deployed base is `python:3.12-slim`, which carries
    no `/etc/mime.types`, so `mimetypes.guess_type("x.stl")` answers `(None, None)`
    and the scan stored every model as `application/octet-stream`; the cover test
    was a `model/` prefix on that stored column, so it matched nothing.

    This test is the reason it belongs on the host: the full container gate caught
    it, and `verify-fast` structurally could not, because this host's
    `/etc/mime.types` made the same suite green - the identical assertions pass or
    fail on the accident of the machine reading them. Reproducing the condition is
    therefore two patchings, and the asymmetry between them is the actual test:

    * the scan-side table is emptied and `mimetypes.guess_type` is blinded, so the
      row really is `application/octet-stream` - asserted below, because a test
      that silently got a real mime would prove nothing at all;
    * the door-side frozen `file_utils.MODEL_SUFFIXES` is left completely alone,
      because the door's job is to answer without the mime column. Blinding that
      too would test the wrong thing and would pass on a broken door.
    """
    headers = auth_header(client)
    monkeypatch.setattr(file_utils, "MODEL_MIME_BY_SUFFIX", {})
    monkeypatch.setattr(mimetypes, "guess_type", lambda *_args, **_kwargs: (None, None))

    body = full_run(client, headers, monkeypatch, isolated_storage)

    assert body["status"] == "created"
    assert body["resolved"] == PLANNED, "mime-blindness must not cost a member"
    with Session(engine) as session:
        cover = session.get(Asset, body["project"]["cover_asset_id"])
        assert cover is not None, "blank card — the python:3.12-slim symptom"
        assert cover.mime == "application/octet-stream", "the row has to be mime-blind or nothing was reproduced"
        assert cover.storage_path == f"{TWIN}/{ROOT}/01_Wizard/wizard_tower_base.stl"


def test_the_scan_names_a_model_mime_whatever_the_host_thinks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The table wins over the environment, which is the point of having one.

    Green here in *this* environment is the assertion, not a formality. Three of
    the five values are not what this host's `/etc/mime.types` says - `.3mf` is
    `application/vnd.ms-3mfdocument` there and `.step`/`.stp` are `application/p21`,
    neither of which is a `model/*` value - and four are not what the 3.12 stdlib
    table says at all, because the stdlib ships no `model/*` entries and the
    published image has no `/etc/mime.types` to add them. So a pass means the
    library's own table answered first on purpose; a green that only meant
    "`mimetypes` agreed" would let this test stay green while the container broke.

    The two non-model lines are the other half: a table consulted before
    `mimetypes` must not swallow anything it does not own, or `.txt` stops being
    `text/plain` and a file with no extension stops being `application/octet-stream`.
    They run against a genuine stdlib guesser rather than whatever the module
    attribute currently holds, because the container simulation some runs install
    blinds `guess_type` wholesale - and a real `python:3.12-slim` still answers
    `text/plain` for `.txt`, which is a stdlib built-in entry; it is only the model
    types the stdlib lacks. Restoring the real one here is what the image does, not
    a way of dodging the simulation.
    """
    monkeypatch.setattr(mimetypes, "guess_type", mimetypes.MimeTypes().guess_type)

    for suffix, expected in file_utils.MODEL_MIME_BY_SUFFIX.items():
        assert file_utils.guess_mime_from_path(f"parts/part{suffix}") == expected

    # Last suffix wins, so a prepared bundle's report keeps its real type.
    assert file_utils.guess_mime_from_path("parts/prepared.gcode.3mf") == "model/3mf"

    # Not in the table: straight through to the stdlib, and to its own default.
    assert file_utils.guess_mime_from_path("parts/notes.txt") == "text/plain"
    assert file_utils.guess_mime_from_path("parts/no-extension") == "application/octet-stream"


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
    for name in ("scan_bundles", "iter_bundle_candidates", "create_bundles"):
        # `find_existing_project` used to be in this list, and used to belong in it:
        # it was a `bundle_service` function and the archive door had its own
        # `_find_by_archive` precisely so it would never call one. Mf-yhjo moved the
        # dedupe to `project_create_service`, where it is a CREATE rule and not a
        # bundle rule, and this door now asks it - that ask is how a folder the
        # detector already made into a project stops becoming a second project.
        # What this test still forbids is the detector's own vocabulary, and it
        # still explodes on every one of them.
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
    # Mf-yhjo: one namespace for every creating door, and the door is told apart by
    # `origin` rather than by a namespace of its own. `_archive_project` is still
    # READ (so pre-namespace rows are still found) and never written.
    assert set(stored) == {"_bundle"}, "the marker reserves the one key and writes no other"
    assert stored["_bundle"]["origin"] == "archive_extract", "declared by an operator, not detected"
    assert "_archive_project" not in stored, "the legacy block is read, never written"


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


# --- the tree that arrived without the extract door -------------------------

# Three members left out of the twin on purpose, so the `partial` answer is checked
# against three different kinds of member - a model, an image, a nested archive -
# rather than three spellings of one, and so the count it reports is one this test
# chose rather than one it counted after the fact.
MISSING_ON_DISK = [
    f"{ROOT}/02_Grounding/grounding_tree.stl",
    f"{ROOT}/images/preview.png",
    f"{ROOT}/nested/accessories.zip",
]

# The destinations left standing under the twin when the extract door meets a
# partially occupied tree: two loose files at the root and three models in a
# subdirectory, so the blockers span both shapes the preflight has to walk.
OCCUPIED_ON_DISK = [
    f"{ROOT}/README.txt",
    f"{ROOT}/LICENSE.txt",
    f"{ROOT}/02_Grounding/grounding_base.stl",
    f"{ROOT}/02_Grounding/grounding_rocks_a.stl",
    f"{ROOT}/02_Grounding/grounding_rocks_b.stl",
]


def test_a_tree_the_scan_registered_creates_with_the_extract_door_never_opened(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ticket note said this could not happen. Measured: it does, and it is correct.

    Shape: the archive sits in the library - unread by any door until the create, but
    listable, which is all the create reads - and its payload was written into the
    twin by something that was not this API. `rsync`, a snapshot restore, an
    operator's own `unzip`. Registration is this file's own `scan()`: the real,
    synchronous `library_scan.scan_storage_root`, not the stub. The extract door is
    never opened - `extract()` does not appear in this test - and the two audit
    assertions at the bottom are what turns that claim into evidence.

    Measured on the live report's own archive: `created`, planned 21, resolved 21,
    `failed` empty, one project row, and its members are exactly the files the disk
    walk finds - compared against the tree this test built, not against a literal.
    No `archive.extract` row exists under this run's request id, and the table's
    total extract count is the same number it was before the first click.

    Why that is the right answer rather than a hole: this door is a set comparison
    of the listing's `<twin>/<entry>` paths against registered `Asset.storage_path`.
    It never asks who wrote the bytes, and it cannot - `archive_service` is not in
    its import closure at all (`test_no_bundle_rule_is_consulted_on_this_path`).
    Requiring the extract door to have run would make every library that arrived by
    `rsync` uncreateable, which is the bug this suite exists to keep dead.

    Mutation this refuses: a create that insists the extract door ran first - a
    marker written by the extract, a per-request token, "resolve from what the
    extract returned", or a plain `import archive_service` plus a call to it. The
    first three answer `nothing_resolved` here; the last grows an `archive.extract`
    row under this request's id, and both of the audit assertions below trip.
    """
    headers = auth_header(client)
    payload = tower_payload()
    staged_archive(isolated_storage, payload)
    tree = write_tree(isolated_storage, payload)
    # The fixture is checked against the wire before any answer is trusted: the door
    # compares against the listing, so a tree that quietly disagreed with the archive
    # would make every assertion below a tautology about the wrong payload.
    assert tree == sorted(posixpath.join(TWIN, name) for name in TOWER_ENTRIES)
    assert len(tree) == PLANNED

    stub_scan(monkeypatch)
    scan()
    before = extract_row_count()
    request = f"scan-arrived-{uuid4().hex}"
    body = create(client, headers, request_id=request)

    assert body["status"] == "created"
    assert body["planned"] == PLANNED
    assert body["resolved"] == PLANNED
    assert body["failed"] == []
    assert body["project"] is not None
    assert body["project"]["asset_count"] == PLANNED
    assert member_paths(body["project"]["id"]) == tree, "the members are the files on disk, all of them"
    assert len(projects()) == 1, "one project, and nothing else"

    # The row for this click exists, so the request id really did reach the audit
    # table - without that, the two assertions below would be vacuously true.
    assert only_row(request).action == audit.ARCHIVE_PROJECT_CREATE
    assert extract_rows_for(request) == []
    assert extract_row_count() == before, "not one extract row was written by this test"


def test_a_tree_the_scan_registered_short_by_three_files_refuses_as_partial_and_creates_nothing(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same door, three members short: it counts them and it names them.

    Shape is the previous test's - payload written beside the zip, no extract door -
    minus `MISSING_ON_DISK`. The scan registers what it can see, so the create is
    left holding 18 of the 21 paths the listing promised.

    Measured: `partial` (``archive_project_service.STATUS_PARTIAL``, not a status
    code - this door has no policy to refuse on), planned 21, resolved 18, the
    missing count is 21 - 18 = 3, `failed` is exactly the three names in sorted
    order, `project` is null, and `projects()` is empty.

    The asymmetry with the extract door is the point of the pair: the same
    partially-present tree is a 409 over there (`test_a_partially_occupied_twin...`)
    and an informative `partial` over here. This door creates nothing on a partial
    because presenting 18 files as the Wizard Tower would be the lie it exists to
    stop - and because nothing was written, the operator's next click after adding
    the three files creates cleanly (see
    `test_a_partial_extraction_still_creates_once_the_rest_arrives`).

    Mutation this refuses: creating over the survivors (`asset_count` 18 on a real
    project), reporting the count but not the names, or - the silent one - letting
    an unregistrable-looking member drop out of `planned` so 18/18 reads as complete
    and a project ships without its roof.
    """
    headers = auth_header(client)
    payload = tower_payload()
    staged_archive(isolated_storage, payload)
    tree = write_tree(isolated_storage, payload, omit=MISSING_ON_DISK)
    assert tree == sorted(posixpath.join(TWIN, name) for name in TOWER_ENTRIES if name not in MISSING_ON_DISK)
    assert len(tree) == PLANNED - 3

    stub_scan(monkeypatch)
    scan()
    body = create(client, headers)

    assert body["status"] == archive_project_service.STATUS_PARTIAL
    assert body["planned"] == PLANNED, "the listing still promised all 21"
    assert body["resolved"] == len(tree) == 18
    assert PLANNED - body["resolved"] == len(MISSING_ON_DISK), "the count it reports is the count that is missing"
    assert body["failed"] == sorted(MISSING_ON_DISK)
    assert body["project"] is None
    assert projects() == [], "and it leaves no empty project behind"


def test_the_extract_door_touches_no_byte_and_no_mtime_when_the_tree_is_already_there(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every destination exists: the door reads the plan, changes nothing, says so.

    Shape: a complete tree under the twin - byte-for-byte the archive's own payload,
    asserted before the click, arrived at by writing the bytes directly so that
    nothing else in this test can have written them - then the one-step flow's own
    first call, `POST /import/archive`, and then the scan and the create.

    Measured: the extract answers 200 with `already_present=True`, `files=0`,
    `bytes=0`, `scan_started=False` and the right `destination`; the create answers
    `created`; and all 21 files carry the same sha256 *and* the same `st_mtime_ns`
    as before the click - both checked per file, on every file, not sampled. That no
    `archive.extract` row was written is the same signature from the server's side:
    a request that changes nothing records nothing.

    `st_mtime_ns` is the load-bearing half. A future "optimisation" that re-streamed
    the members and let the no-clobber move sort it out, or that rewrote identical
    bytes for the sake of one code path, leaves every hash untouched - only the
    clock can see it. And the hash is still worth keeping beside it, because a
    rewrite that also preserved mtimes would be the worse bug.

    Mutation this refuses: dropping the `_already_present` short-circuit so the door
    stages and publishes over a finished tree, which surfaces as a 409 rather than
    as an idempotent no-op; or a re-extract that republishes identical bytes and
    passes a bytes-only assertion.
    """
    headers = auth_header(client)
    payload = tower_payload()
    staged_archive(isolated_storage, payload)
    tree = write_tree(isolated_storage, payload)
    assert tree == sorted(posixpath.join(TWIN, name) for name in TOWER_ENTRIES), "complete means complete"

    stub_scan(monkeypatch)
    before = fingerprint_tree(isolated_storage, TWIN)
    assert set(before) == set(tree) and len(before) == PLANNED, "the snapshot covers the whole tree"
    extract_before = extract_row_count()

    result = extract(client, headers)

    assert result["destination"] == TWIN
    assert result["already_present"] is True, "every destination existed, so this request was a no-op"
    assert result["files"] == 0
    assert result["bytes"] == 0
    assert result["scan_started"] is False, "nothing landed, so there is nothing for a scan to register"

    scan()
    body = create(client, headers)

    assert body["status"] == "created"
    assert body["planned"] == PLANNED
    assert body["resolved"] == PLANNED
    assert member_paths(body["project"]["id"]) == tree
    assert extract_row_count() == extract_before, "a request that changed nothing recorded nothing"

    after = fingerprint_tree(isolated_storage, TWIN)
    assert set(after) == set(before), "the no-op added or removed a file"
    for path, (digest, mtime_ns) in before.items():
        assert after[path][0] == digest, f"{path} no longer holds the bytes the archive holds"
        assert after[path][1] == mtime_ns, f"{path} was rewritten - identical bytes, new clock"


def test_a_partially_occupied_twin_is_a_409_at_the_extract_door_and_a_partial_at_this_one(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PIN, not endorsement: two doors, two different verdicts on one tree.

    Shape: five of the 21 planned destinations are standing under the twin
    (`OCCUPIED_ON_DISK`: two loose files plus three models) and sixteen are not. Both
    doors are then asked about that same tree.

    Measured, and deliberately left exactly as it is:

    * `POST /import/archive` answers **409** -
      `refusing to extract: these paths already exist - move or delete them first: `
      `<the five blockers, sorted, comma-joined>`. That is `archive_service`'s
      all-or-nothing contract: `_already_present` (definition `:364`, consulted at
      `:141`) no-ops only when *every* destination exists, and `_refuse_occupied`
      raises at `:380` as soon as *one* exists, naming every blocker so the operator
      can clear them all in one pass. Its three sibling 409s - `:390` for a path that
      exists but is not a directory, `:425` for a directory that cannot be created, and
      `:195` for a name claimed after the preflight - are the same contract, and none of
      them is the one hit here. Nothing is written and no `archive.extract` row is
      recorded; the tree on disk is the tree that was there before the click.
    * `POST /import/archive/project` on the same tree answers **partial** - planned
      21, resolved 5, `failed` the other sixteen names, no project.

    This test asserts the asymmetry, not a preference. A "top-up extract" that filled
    only the missing destinations would be a change of contract for `archive_service`
    - one that quietly writes into a directory some other process has half-filled -
    and if anyone makes it, the 409 assertion here fails loudly and forces that
    decision to be made on purpose rather than landing silently. Do not "fix" the
    409 here; the door under test is not this one, and nothing in this file changes
    it.

    Mutation this refuses on the create's side: `create` growing the extract door's
    all-or-nothing reflex - a `_resolve` that returns nothing unless every planned path
    resolved. Measured against that mutation the create answers `nothing_resolved`, so
    the operator with sixteen files still to copy gets a verdict that names nothing in
    place of the one that names all sixteen.
    """
    headers = auth_header(client)
    payload = tower_payload()
    staged_archive(isolated_storage, payload)
    tree = write_tree(isolated_storage, payload, omit=[name for name in TOWER_ENTRIES if name not in OCCUPIED_ON_DISK])
    assert tree == sorted(posixpath.join(TWIN, name) for name in OCCUPIED_ON_DISK)
    assert len(tree) == 5

    stub_scan(monkeypatch)
    extract_before = extract_row_count()

    response = client.post("/import/archive", json={"path": ARCHIVE}, headers=headers)

    blockers = sorted(posixpath.join(TWIN, name) for name in OCCUPIED_ON_DISK)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == (
        "refusing to extract: these paths already exist - move or delete them first: " + ", ".join(blockers)
    ), "one 409 naming every blocker, so the operator clears them in one pass"
    assert files_under(isolated_storage, TWIN) == tree, "a refused extract publishes nothing"
    assert extract_row_count() == extract_before, "and records nothing"

    scan()
    body = create(client, headers)

    assert body["status"] == archive_project_service.STATUS_PARTIAL
    assert body["planned"] == PLANNED
    assert body["resolved"] == len(OCCUPIED_ON_DISK)
    assert body["failed"] == sorted(name for name in TOWER_ENTRIES if name not in OCCUPIED_ON_DISK)
    assert body["project"] is None
    assert projects() == []


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


# --- the dedupe the doors share (Mf-yhjo) ------------------------------------
#
# `project_create_service.find_existing_project` answers "does this already exist?" for
# every door that makes a project, and it answers it from four identities. Two archives
# that share a basename are the case where one of those four - the name - would be a
# lie: `import/a/thing.zip` and `import/b/thing.zip` are two products the operator
# filed in two folders, and this door names a project after `posixpath.basename` of the
# twin, so BOTH of them are called `thing`. An archive door that handed that name to the
# shared dedupe would merge the second product into the first and tell the operator
# they had already made it.


THING_ZIPS = ("a/thing.zip", "b/thing.zip")
THING_ENTRY_NAMES = ("thing/part-1.stl", "thing/part-2.stl", "thing/preview.png")
# The one name BOTH twins get: `posixpath.basename("a/thing") == posixpath.basename("b/thing")`.
THING_NAME = "thing"


def thing_payload(copy: str) -> Dict[str, bytes]:
    """The same three entries, tagged with which copy of the archive they came from.

    Same NAMES in both archives, different BYTES, so the two products cannot be
    confused by content either: each extraction registers its own three assets.
    """
    return {name: f"{copy}: {name} was printed twice\n".encode() for name in THING_ENTRY_NAMES}


def thing_member_paths(folder: str) -> List[str]:
    """The three paths one twin holds - the entry path, under `<folder>/<stem>/`."""
    return sorted(f"{folder}/{THING_NAME}/{name}" for name in THING_ENTRY_NAMES)


def thing_member_ids(folder: str) -> List[str]:
    """The registered ASSET IDs the ``<folder>/thing`` twin holds.

    The containment leg is fed ids, not paths, so asking it needs this one lookup.
    """
    paths = thing_member_paths(folder)
    with Session(engine) as session:
        return list(
            session.exec(select(Asset.id).where(Asset.storage_path.in_(paths)))  # type: ignore[attr-defined]
        )


def dedupe_answer_for(path: str, twin: str, name: str = "", assets: Sequence[str] = ()):
    """What the ONE dedupe says about one archive, asked with the identities this door holds.

    ``name`` defaults to the empty string because that is what the door hands - a door
    that holds no name must not match on one. The one place this test asks with a name is
    labelled as asking a question the door does not ask.

    ``assets`` defaults to the empty tuple for the same reason: containment is a leg a
    door has to ASK for (``Dedupe.assets``), and this door never asks for it. Where a
    test passes ids here it is measuring the leg directly, one step behind the door, to
    show what the door is declining to ask - not asking on the door's behalf.
    """
    with Session(engine) as session:
        return project_create_service.find_existing_project(
            session, key=twin, archive=path, name=name, assets=assets
        )


def create_status(client: TestClient, headers: dict, path: str) -> Tuple[int, object]:
    """The raw verdict of one create click: status code and body, nothing asserted.

    The module's `create` helper pins 200, which is right for every test in this file
    until Mf-yhjo put a second door in front of the same dedupe. This is the one place
    the code itself is the thing under test.
    """
    response = client.post("/import/archive/project", json={"path": path}, headers=headers)
    return response.status_code, response.json()


def test_an_archive_whose_name_contains_a_backslash_is_still_found_on_the_second_click(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``\\`` is a legal character in a Linux filename, and this door owns that fact.

    ``backslash/a\\b.zip`` below is ONE file whose name contains a backslash, not a
    directory: legal on any filesystem, and exactly what a Windows-authored archive
    leaves in a library. Two failures hang on how the dedupe spells it, and both were
    measured against the version of this code that folded the ASK
    (``_normalised`` did ``.replace("\\", "/")``) while the door stored what
    ``posixpath.normpath`` left alone:

    * the second click on the SAME file MISSED, so it wrote a second ``Project`` where
      base answered ``already_created`` - an archive the operator had already turned
      into a product became two products (observed as a 409 from the name index until
      the second click was given its own name; see the mutation note below for why that
      distinction is the test's own);
    * the fold ran the other way as well: click the DIFFERENT file ``backslash/a/b.zip``
      after the backslash one has a project, and the folded spellings met in the middle -
      that click answered ``already_created`` carrying the project made from
      ``backslash/a\\b.zip``. One archive's click reported success against another
      archive's product - a merge no operator asked for and no marker undoes.

    Base could not fail either way because it ran BOTH sides through one rule:
    ``archive_project_service._find_by_archive`` normalised the stored marker value as
    well as the ask. The shared dedupe keeps the falsy guard (blank is not an identity)
    and does no canonicalisation at all, which is what these two assertions are.

    Mutation, all three measured:

    * fold the ASK only (``.replace("\\", "/")`` back in ``_normalised``) or fold the
      STORED side only (fold ``payload["archive"]`` in ``_read_marker``) - either one
      breaks the symmetry, and the second click above answers ``created`` with a NEW id
      where it must answer ``already_created`` carrying the first one;
    * fold BOTH sides, which is the "just normalise everywhere" repair that looks like
      it fixes the first pair and quietly creates the second bug: the second click goes
      green and the THIRD assertion reds - the click on the DIFFERENT file ``a/b.zip``
      answers ``already_created`` carrying the project made from ``a\\b.zip``.

    That third mutation is the reason this function canonicalises nothing at all rather
    than canonicalising in one place: symmetric folding makes two different files one
    product, and no operator can undo a merge made on a spelling.
    """
    headers = auth_header(client)
    stub_scan(monkeypatch)
    backslash = "backslash/a\\b.zip"
    forward = "backslash/a/b.zip"
    write_zip(isolated_storage / "backslash" / "a\\b.zip", thing_payload("backslash"))
    write_zip(isolated_storage / "backslash" / "a" / "b.zip", thing_payload("forward"))
    # The bytes already on disk under the name the extract door WOULD have filed them
    # under: `_twin_rel` sanitises the stem (`a\\b` -> `a_b`) while the door stores the
    # archive path verbatim, and that is the whole asymmetry this test is about. Going
    # through `POST /import/archive` is not an option - that door refuses to pair a
    # backslash archive with a sanitised twin at all, with a 400 that tells the operator
    # to rename - so the files are written here and registered by the scan.
    for folder, copy in (("a_b", "backslash"), ("a/b", "forward")):
        for name, content in thing_payload(copy).items():
            landing = isolated_storage / "backslash" / folder / name
            landing.parent.mkdir(parents=True, exist_ok=True)
            landing.write_bytes(content)
    scan()

    first = create(client, headers, backslash)

    assert (first["status"], (first["planned"], first["resolved"])) == ("created", (3, 3)), first
    first_id = first["project"]["id"]

    # A distinct name on the second click, so this measures the DEDUPE and not the name
    # index. Both CLICKS of this one archive recompute the same twin (`_twin_rel` of the
    # same path is ``backslash/a_b`` both times), so the two clicks want the same project
    # NAME and a dedupe that misses dies on ``idx_project_name_unique`` with a 409 - the
    # test reports the index instead of the answer. That is a real outcome, and it is the
    # argument ``test_two_archives_that_share_a_basename_in_different_folders_are_two_projects``
    # below makes about a 409 not being ``already_created``; it is simply not the outcome
    # under observation here. Asked with a name the first project does not have, a miss
    # has nowhere to hide: it becomes a second product.
    again = create(client, headers, backslash, name="the backslash archive, clicked twice")

    assert (again["status"], again["project"]["id"]) == ("already_created", first_id), (
        "the same file, clicked twice: a folded ask does not equal the stored spelling, "
        "and the second click writes a second product"
    )

    other = create(client, headers, forward)

    assert other["status"] == "created", (
        "the click on `a/b.zip` answered `already_created` carrying the project made "
        "from `a\\b.zip`: folding both sides meets two different files in the middle and "
        "makes one product of them"
    )
    assert other["project"]["id"] != first_id
    assert len(projects()) == 2, "two files the operator filed as two products"


def test_two_archives_that_share_a_basename_in_different_folders_are_two_projects(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two `.zip`s named `thing`, in two folders: two products, never one project.

    Both archives hold the same three entry names, and both twins are therefore called
    ``thing`` - which is exactly the shape a NAME leg in the shared dedupe would merge.
    This door hands no name to ``Dedupe`` (``archive_project_service``: "a name leg here
    would merge ``a/thing.zip`` with ``b/thing.zip``") and the assertions below are that
    claim measured:

    * both extractions register their OWN three assets, six rows in all - the two
      products are distinct files, not two names for one set of bytes;
    * the first create is ``created``, named ``thing``;
    * asked directly, the ONE dedupe finds **nothing** for the second archive with the
      two identities the door holds (its twin as the key, the archive as itself), and it
      also finds nothing on the assets leg - the second archive's member ids are nobody's
      members;
    * the second product is created too, with a DIFFERENT project id, and its three
      members are the ``b/`` copies: two ``Project`` rows, each holding its own files.

    One measured deviation from the ticket's wording, stated rather than hidden: the
    second click cannot be created under the name ``thing`` as well, because
    ``idx_project_name_unique`` is a NOCASE UNIQUE index on ``project.name`` and the
    second INSERT is refused by the NAME INDEX (a 409 ``Project name already exists``),
    not by the dedupe. That refusal is asserted below, before the named create, because
    the difference is the whole point: a 409 is the operator telling the database twice
    that two things are called ``thing``, which is a naming problem they can settle by
    typing a second name; ``already_created`` would be the dedupe claiming two different
    products are one, which is a lie no operator can undo. So the name leg is refused
    here on both counts, and the 409 is the observable proof that the dedupe did NOT
    merge them.

    Mutation: add ``name=project_name`` to ``archive_project_service``'s
    ``Dedupe(key=twin_rel, archive=normalized)`` - the fifth identity the module docstring
    refuses - and the second click answers ``200 already_created`` carrying the FIRST
    project's id instead of the 409 the name index owes it, which reds the ``(code,
    detail)`` assertion below. The probe above is that same fact read without waiting for
    the mutation, so the reason is in the file rather than in a commit message.
    """
    headers = auth_header(client)
    stub_scan(monkeypatch)
    for folder in ("a", "b"):
        write_zip(isolated_storage / folder / "thing.zip", thing_payload(folder))

    for folder in ("a", "b"):
        assert extract(client, headers, f"{folder}/thing.zip")["destination"] == f"{folder}/thing"
    scan()

    with Session(engine) as session:
        registered = session.exec(select(Asset.storage_path)).all()
    assert sorted(thing_member_paths("a") + thing_member_paths("b")) == sorted(
        path for path in registered if not path.endswith(".zip")
    ), (
        "six registered files: two copies of one listing, not one listing seen twice - "
        "the two `.zip`s register as assets too, and they are the only things filtered here"
    )

    first = create(client, headers, THING_ZIPS[0])

    assert first["status"] == "created"
    assert (first["planned"], first["resolved"]) == (3, 3)
    assert first["twin"] == "a/thing"
    first_id = first["project"]["id"]
    assert first["project"]["name"] == THING_NAME, "both twins are called `thing` - the merge risk, named"
    assert member_paths(first_id) == thing_member_paths("a")

    assert dedupe_answer_for(THING_ZIPS[1], "b/thing") is None, (
        "the ONE dedupe, asked with the two identities this door holds, sees no project "
        "behind the second archive: no key and no twin"
    )
    assert dedupe_answer_for(THING_ZIPS[1], "b/thing", assets=thing_member_ids("b")) is None, (
        "and the same asked one step behind the door, with the containment leg the door "
        "does NOT hold fed in by hand: the second archive's three files are nobody's "
        "members, so even a leg this door declines to ask could not merge them"
    )

    # The same question with the identity this door deliberately does NOT hold. Asked
    # directly, because the claim is about what a name leg would do, and a witness that
    # only shows up under a mutation is a witness nobody reads.
    would_merge = dedupe_answer_for(THING_ZIPS[1], "b/thing", name=THING_NAME)

    assert would_merge is not None and would_merge.match == "name" and would_merge.project.id == first_id, (
        "handed the name BOTH twins have, the shared dedupe merges the second product "
        "into the first and calls it already made - which is the sentence this door is "
        "not allowed to say about two archives the operator filed in two folders"
    )

    code, body = create_status(client, headers, THING_ZIPS[1])

    assert (code, body.get("status"), body.get("detail")) == (
        409,
        None,
        "Project name already exists",
    ), (
        "refused by the NOCASE name index, NOT answered `already_created`: the dedupe "
        "did not merge two products, it let the second one reach the insert"
    )

    second = create(client, headers, THING_ZIPS[1], name="thing (the other folder)")

    assert second["status"] == "created", "the same product under a second name is still its own project"
    second_id = second["project"]["id"]
    assert second_id != first_id
    assert (second["planned"], second["resolved"]) == (3, 3)
    assert second["twin"] == "b/thing"
    assert member_paths(second_id) == thing_member_paths("b")

    rows = projects()
    assert len(rows) == 2, "TWO rows - the merged answer would have left one"
    assert {project.name for project in rows} == {THING_NAME, "thing (the other folder)"}
    assert member_paths(first_id) == thing_member_paths("a"), "the first product kept its own files"
