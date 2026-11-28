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

import json
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

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
import import_resolvers
import library_scan
import login_throttle as login_throttle_module
import main
import permissions
import proposal_engine
import settings_service
from db import engine
from migrations import run_migrations
from models import Asset, AssetFile, AuditEvent, Folder, MfaRecoveryCode, Project, ProjectAsset, User, UserSession
from schemas.bundles import BundleDetectItemOut, BundleRefusalOut, BundleReportOut, BundleRuleSetOut

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

# The owner's 5390184 export (Mf-nivh): its directory name is the only evidence,
# so the row starts with no creator, no licence and a derived Thingiverse URL.
DRAGON = "5390184_ARTICULATED_ice_dragon__fixed_"
DRAGON_KEY = f"import/{DRAGON}"
DRAGON_URL = "https://www.thingiverse.com/thing:5390184"
CC_BY_NC_SA_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"
THINGIVERSE_FIXTURES = Path(__file__).parent / "fixtures" / "thingiverse"


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


COLLECTION_KEY = "import/Collection"
AGREEING_KEY = "import/Agreeing"


def unknown_boundary_bundle(root: Path) -> Path:
    """A checkout the probe cannot read, with two product-shaped children.

    A fake `.git` plus two model-bearing children that each carry their own README
    is the split arm's strongest evidence. With the fix an unreadable boundary is
    one project carrying a note; without it, this folder is split.
    """
    bundle = root / "import" / "Unreadable"
    (bundle / ".git").mkdir(parents=True)
    for name in ("Widget One", "Widget Two"):
        write_text(bundle / name / "README.md", f"# {name}\n")
        for index in range(3):
            write_file(bundle / name / f"part{index}.stl", STL_BYTES)
    return bundle


def collection_bundle(root: Path) -> Path:
    """Three products in one folder: each top folder ships its own README and models.

    The detector reads the listing as one `generic` bundle (a model at its own depth
    plus an image and a text anywhere). The engine reads it as `split-top-folders`,
    20 + 30 + 10 = 60, against a `one-project` of 40 - 30 - 20 = -10: each child's own
    document is a claim to be a product, so the split is the ENGINE's recommendation
    while the app's row recommends something else. That is the dissent the row must
    serve, and it is why its confidence reads `high`.
    """
    bundle = root / "import" / "Collection"
    write_file(bundle / "loose.stl", STL_BYTES)
    write_file(bundle / "cover.jpg", b"j" * 100)
    write_text(bundle / "notes.txt", "print at 0.2mm\n")
    for name in ("Widget One", "Widget Two", "Widget Three"):
        write_text(bundle / name / "README.md", f"# {name}\n")
        for index in range(3):
            write_file(bundle / name / f"part{index}.stl", STL_BYTES)
    return bundle


def agreeing_bundle(root: Path) -> Path:
    """One product in one folder: a `generic` row the engine never dissents on.

    ONE model-bearing child, no product document anywhere, so the engine offers
    `one-project` and no split at all (a split needs two top folders carrying
    models). This is the row whose served matches must be exactly what they were
    before the engine was consulted.
    """
    bundle = root / "import" / "Agreeing"
    for index in range(3):
        write_file(bundle / "files" / f"part{index}.stl", STL_BYTES)
    write_file(bundle / "cover.jpg", b"j" * 100)
    write_text(bundle / "notes.txt", "print at 0.2mm\n")
    return bundle


# The wire shape of one match. Pinned by key set rather than by literal body, so the
# field list cannot drift silently in either direction.
MATCH_KEYS = {
    "shape",
    "rule",
    "confidence",
    "model_count",
    "image_count",
    "text_count",
    "other_count",
    "groups",
    "note",
    "creatable",
}


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
    # The engine's recommendation, in the wire's rule token: the ONE ranking
    # (Mf-1agz). `detector`/`confidence`/`metadata_shape` keep their names and now
    # describe `matches[0]`.
    assert row["detector"] == "engine:one-project"
    assert row["metadata_shape"] == "one-project"
    assert row["matches"][0]["rule"] == row["detector"]
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
    assert project["cover_asset_id"] == row["cover"]["asset_id"], (
        "since Mf-wsdo this says something stronger than "
        "\"the create used the row's cover\": the cover is one of the project's members"
    )
    # Every registered file under the key (Mf-7oow): the three models, the five
    # previews AND the two marker documents - the same ten the row above counts, so
    # membership and the row's facts cannot disagree.
    assert len(project_asset_ids(client, contributor, project_id)) == 10
    assert created["results"][0]["assets_added"] == 10


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


# --- scan fidelity: the report's own rule set and refused ledger (Mf-au4k) ---


def test_the_report_model_declares_every_key_the_service_computes(
    isolated_storage: Path,
) -> None:
    """The model and the service name the same keys, and the one drop is deliberate.

    `build_report` is called directly, so the response model is not in the loop: the
    dict it returns is the whole truth about what the service computes. The per-row
    `rule_set` stamp is LEFT OFF `BundleDetectItemOut` on purpose - one rule set per
    report, and the offline artifact keeps the per-row copy - so it is named in an
    assertion rather than tolerated as an unexplained extra. The two drift keys
    (`bundle_drift_state`/`bundle_drift_count`, Mf-8i04) ARE declared, so the
    model cannot silently drop them the way `rule_set` and `refused` once were.
    Mutation: delete either field from `BundleReportOut` and the set comparison
    reds; delete either drift key from `BundleDetectItemOut` and the row
    comparison reds; declare the per-row stamp on the row model and the
    named-difference assertion reds.
    """
    moai_bundle(isolated_storage)
    write_zip(
        isolated_storage / "import" / "models-only.zip",
        {"part-a.stl": b"s" * 40, "part-b.stl": b"s" * 41, "part-c.stl": b"s" * 42},
    )
    scan()

    report = bundle_service.build_report()

    assert report["refused"], "the fixture must refuse at least one path"
    assert report["rule_set"]["stamp"], "every report names the rule set it was taken at"
    assert set(report) == set(BundleReportOut.model_fields), "the model declares exactly the keys served"
    assert set(report["rule_set"]) == set(BundleRuleSetOut.model_fields)
    assert set(report["refused"][0]) == set(BundleRefusalOut.model_fields)
    assert set(report["bundles"][0]) - set(BundleDetectItemOut.model_fields) == {
        "rule_set"
    }, "the per-row stamp is deliberately off the model"
    assert set(BundleDetectItemOut.model_fields) - set(report["bundles"][0]) == set()


def test_the_detect_report_serves_the_rule_set_stamp_and_the_refusal_ledger(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """The live route keeps both fields: the walk's rule set and every path it refused.

    The stamp is compared to the constants the detector exports rather than to any
    literal, so a rule-table edit moves both sides together. Mutation: drop
    `rule_set`/`refused` from the response model and this reds - that is exactly the
    shape Mf-nivh found for `enrichment`.
    """
    moai_bundle(isolated_storage)
    write_zip(
        isolated_storage / "import" / "models-only.zip",
        {"part-a.stl": b"s" * 40, "part-b.stl": b"s" * 41, "part-c.stl": b"s" * 42},
    )
    scan()

    body = report_for(client, contributor)

    assert body["rule_set"] == {
        "version": proposal_engine.RULE_SET_VERSION,
        "stamp": proposal_engine.RULE_SET_STAMP,
    }
    assert body["refused"], "the walk names every path it did not offer"
    assert body["refused"] == bundle_service.build_report()["refused"], "the route never reshapes the ledger"
    for item in body["refused"]:
        assert item["kind"] in {"archive", "directory"}, item
        assert item["rule"] in bundle_service.REFUSAL_RULES, item
    assert [item["rel_path"] for item in body["refused"]] == sorted(
        item["rel_path"] for item in body["refused"]
    ), "the ledger is sorted by path"
    # A `.zip` holding models is its own ROW now (Mf-1agz), not a refusal; the
    # ledger names the directories the engine read and did not offer.
    assert "import/models-only.zip" in {row["key"] for row in body["bundles"]}
    assert {
        item["rel_path"] for item in body["refused"]
    } >= {f"{MOAI}/files", f"{MOAI}/images"}, "the row's own subdirectories are named as members"
    assert "import" in {item["rel_path"] for item in body["refused"]}, "the transparent drop zone is named too"


def test_an_empty_ledger_is_served_as_an_empty_list_not_omitted(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """Nothing on disk still serves `refused: []`: an empty answer is an answer.

    Mutation: drop `refused` from the model and the key disappears from the body;
    make it `Optional[...] = None` and this serves `null` instead of `[]` - either
    way a client can no longer tell "nothing was refused" from "no ledger here".
    Whether it is DEFAULTED rather than required is the fixture's required/default
    list, pinned by `test_openapi_contract.py`, not by this test.
    """
    body = report_for(client, contributor)

    assert "refused" in body, "an empty ledger is served, never omitted"
    assert body["refused"] == []
    assert body["rule_set"]["stamp"] == proposal_engine.RULE_SET_STAMP


# --- enrichment over HTTP (Mf-nivh) -----------------------------------------


def dragon_bundle(root: Path) -> Path:
    """The owner's export: one model under files/, no documents, the id in the name."""
    bundle = root / "import" / DRAGON
    write_file(bundle / "files" / "ARTICULATED_ice_dragon_fixed_p1.stl", STL_BYTES)
    return bundle


def fixture_html(name: str) -> str:
    """One captured Thingiverse page head from this module's own fixture tree."""
    return json.loads(THINGIVERSE_FIXTURES.joinpath(name).read_text(encoding="utf-8"))["html"]


def stub_thing_page(monkeypatch: pytest.MonkeyPatch, html: str) -> list[tuple[str, str | None]]:
    """Replace the fetch the service imports inside `enrich_bundles`; record its args."""
    calls: list[tuple[str, str | None]] = []

    def fake_fetch(thing_id: str, thingiverse_cookie: str | None) -> str:
        calls.append((thing_id, thingiverse_cookie))
        return html

    monkeypatch.setattr(import_resolvers, "fetch_thingiverse_thing_page", fake_fetch)
    return calls


def enrich(client: TestClient, actor: Actor, keys: list[str], **body) -> Response:
    return client.post("/bundles/enrich", json={"keys": keys, **body}, headers=bearer(actor.token))


def row_for(report: dict, key: str) -> dict:
    return {row["key"]: row for row in report["bundles"]}[key]


def test_the_owners_ice_dragon_gains_creator_and_licence(
    client: TestClient, contributor: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one acceptance path: a page fills the empty fields, then a create records them.

    Mutation: run the fetch during detection and the row is already attributed
    before the enrich; drop `enrichment` from the response model and the fields
    assertion reds; let the create ignore the row's values and the project's
    creator/licence/licence_url and `_bundle.enrichment` red.
    """
    dragon_bundle(isolated_storage)
    scan()

    before = row_for(report_for(client, contributor), DRAGON_KEY)
    assert before["creator"] is None and before["licence"] is None and before["licence_url"] is None
    assert before["detector"] == "engine:one-project"
    assert before["source_url"] == DRAGON_URL
    assert before["enrichment"] is None, "nothing on this row came from a page yet"

    calls = stub_thing_page(monkeypatch, fixture_html("thing-5390184.json"))
    response = enrich(client, contributor, [DRAGON_KEY])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["fetched"] == 1
    assert calls == [("5390184", None)]

    after = row_for(report_for(client, contributor), DRAGON_KEY)
    assert after["creator"] == "P1lotz"
    assert after["licence"] == "CC BY-NC-SA"
    assert after["licence_url"] == CC_BY_NC_SA_URL
    assert after["enrichment"]["fields"] == ["creator", "licence", "licence_url"]
    assert after["enrichment"]["source_url"] == DRAGON_URL
    assert after["detector"] == "engine:one-project", "enrichment never rewrites the ranking"

    created = client.post("/bundles/create", json={"items": [{"key": DRAGON_KEY}]}, headers=bearer(contributor.token))
    assert created.status_code == 200, created.text
    project_id = created.json()["results"][0]["project_id"]
    project = project_by_id(client, contributor, project_id)
    assert project["creator"] == "P1lotz"
    assert project["licence"] == "CC BY-NC-SA"
    assert project["licence_url"] == CC_BY_NC_SA_URL
    assert project["metadata_json"]["_bundle"]["enrichment"] == after["enrichment"]


def test_enrich_reports_a_429_as_a_readable_refusal(
    client: TestClient, contributor: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rate limit is one refused item, not a thrown request, and writes nothing.

    Mutation: retry the fetch and the call count becomes two; cache a refusal and
    the empty-map assertion reds; put the cookie (or its absence) in the audit
    detail and the last assertion reds.
    """
    dragon_bundle(isolated_storage)
    scan()
    calls: list[str] = []

    def rate_limited(thing_id: str, thingiverse_cookie: str | None) -> str:
        calls.append(thing_id)
        raise HTTPException(
            status_code=429,
            detail=(
                f"Thingiverse is rate limiting this server (HTTP 429) for thing {thing_id}. Set the "
                "THINGIVERSE_COOKIE environment variable to a Cookie header from a signed-in browser."
            ),
        )

    monkeypatch.setattr(import_resolvers, "fetch_thingiverse_thing_page", rate_limited)
    headers = {**bearer(contributor.token), "X-Request-ID": "enrich-429"}

    response = client.post("/bundles/enrich", json={"keys": [DRAGON_KEY], "thingiverse_cookie": "session=secret"}, headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["refused"] == 1 and body["fetched"] == 0
    result = body["results"][0]
    assert result["status"] == "refused"
    assert "THINGIVERSE_COOKIE" in result["reason"]
    assert calls == ["5390184"], "a refusal is never retried"
    assert bundle_service.get_enrichments() == {}, "a refusal caches nothing"
    assert row_for(report_for(client, contributor), DRAGON_KEY)["creator"] is None
    with Session(engine) as session:
        rows = list(session.exec(select(AuditEvent).where(AuditEvent.request_id == "enrich-429")))
        details = [row.detail_json or "" for row in session.exec(select(AuditEvent).where(AuditEvent.action == audit.BUNDLE_ENRICH))]
    assert rows == [], "a refusal changed nothing, so it writes no audit row"
    assert not any("secret" in detail for detail in details), "no enrich row ever carries the cookie"


def test_a_missing_thing_stays_untouched_and_says_so(
    client: TestClient, contributor: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200 page with no thing metadata is a refusal that names the id it asked about."""
    dragon_bundle(isolated_storage)
    scan()
    stub_thing_page(monkeypatch, fixture_html("thing-missing.json"))

    response = enrich(client, contributor, [DRAGON_KEY])

    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["status"] == "refused"
    assert "5390184" in result["reason"]
    assert bundle_service.get_enrichments() == {}
    row = row_for(report_for(client, contributor), DRAGON_KEY)
    assert row["creator"] is None and row["licence"] is None
    assert row["enrichment"] is None


def test_a_page_with_no_licence_leaves_the_row_licence_null(
    client: TestClient, contributor: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page that states an author and no licence fills the author only, never a guess."""
    dragon_bundle(isolated_storage)
    scan()
    page = (
        '<html><head><script type="application/ld+json">'
        '{"@context":"https://schema.org/","@type":"CreativeWork","author":{"@type":"Person","name":"Someone"}}'
        "</script></head></html>"
    )
    stub_thing_page(monkeypatch, page)

    response = enrich(client, contributor, [DRAGON_KEY])

    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["status"] == "fetched"
    assert result["creator"] == "Someone"
    assert result["licence"] is None
    row = row_for(report_for(client, contributor), DRAGON_KEY)
    assert row["creator"] == "Someone"
    assert row["licence"] is None
    assert row["licence_url"] is None
    assert row["enrichment"]["fields"] == ["creator"]


def test_a_report_with_an_enrichable_row_makes_no_outbound_call(
    client: TestClient, contributor: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report is a disk walk: a row that WANTS a page still opens no socket.

    Mutation: call the fetch from `build_report` and one of the three tripwires
    raises, so the 200 and the row assertions never run.
    """
    dragon_bundle(isolated_storage)
    scan()

    def refuse(*args, **kwargs):
        raise AssertionError("the detection report must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(import_resolvers, "fetch_thingiverse_thing_page", refuse)

    report = report_for(client, contributor)

    assert [row["key"] for row in report["bundles"]] == [DRAGON_KEY]
    assert report["bundles"][0]["source_url"] == DRAGON_URL


def test_enrich_refuses_more_than_twenty_five_keys(client: TestClient, contributor: Actor) -> None:
    """The bulk bound is a readable 400, before any walk or fetch."""
    response = enrich(client, contributor, [f"no/such/{index}" for index in range(26)])

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "enrich at most 25 bundles at a time"


def test_a_viewer_may_not_enrich(client: TestClient, viewer: Actor, isolated_storage: Path) -> None:
    """Enrichment is an outbound fetch plus a stored claim, so it needs contribute."""
    dragon_bundle(isolated_storage)
    scan()

    response = enrich(client, viewer, [DRAGON_KEY])

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == permissions.DENY_DETAIL[permissions.CONTRIBUTE]
    assert bundle_service.get_enrichments() == {}


def test_enrich_carries_the_cookie_through_the_same_plumbing(
    client: TestClient, contributor: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The body cookie is stripped by `resolve_thingiverse_cookie` and reaches the fetch.

    Mutation: pass `body.thingiverse_cookie` raw and the stub sees the whole
    `Cookie:` line; record it in the audit detail and the last assertion reds.
    """
    dragon_bundle(isolated_storage)
    scan()
    calls = stub_thing_page(monkeypatch, fixture_html("thing-5390184.json"))
    headers = {**bearer(contributor.token), "X-Request-ID": "enrich-cookie"}

    response = client.post(
        "/bundles/enrich",
        json={"keys": [DRAGON_KEY], "thingiverse_cookie": "Cookie: session=super-secret\nother=1"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert calls == [("5390184", "session=super-secret")]
    with Session(engine) as session:
        rows = list(session.exec(select(AuditEvent).where(AuditEvent.request_id == "enrich-cookie")))
    assert len(rows) == 1
    assert "super-secret" not in (rows[0].detail_json or "")


def test_an_enriched_project_is_never_edited_by_a_later_enrich(
    client: TestClient, contributor: Actor, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A human's edit outranks a later page: enrichment fills empties, never re-claims."""
    dragon_bundle(isolated_storage)
    scan()
    stub_thing_page(monkeypatch, fixture_html("thing-5390184.json"))
    assert enrich(client, contributor, [DRAGON_KEY]).json()["fetched"] == 1

    created = client.post("/bundles/create", json={"items": [{"key": DRAGON_KEY}]}, headers=bearer(contributor.token))
    project_id = created.json()["results"][0]["project_id"]
    renamed = client.patch(
        f"/project/{project_id}",
        json={"creator": "A Human"},
        headers=bearer(contributor.token),
    )
    assert renamed.status_code == 200, renamed.text

    def refuse(*args, **kwargs):
        raise AssertionError("a fresh record must answer without a fetch")

    monkeypatch.setattr(import_resolvers, "fetch_thingiverse_thing_page", refuse)

    response = enrich(client, contributor, [DRAGON_KEY])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cached"] == 1 and body["fetched"] == 0
    project = project_by_id(client, contributor, project_id)
    assert project["creator"] == "A Human", "the project row is the human's, not the page's"
    assert project["licence"] == "CC BY-NC-SA"
    row = row_for(report_for(client, contributor), DRAGON_KEY)
    assert row["creator"] == "P1lotz", "the report still shows what the page supplied"


# --- a compound row carries its group breakdown (Mf-8xrc) --------------------

STAGETOP_KEY = "import/Stagetop"
STAGETOP_EDITION = "StageTop Wave One - Personal Use"
OPENAPI_BUNDLES_FIXTURE = (
    Path(__file__).parent / "fixtures" / "openapi" / "components" / "bundles.json"
)


def stagetop_bundle(root: Path) -> Path:
    """The owner's Stagetop tree: one product in two editions, 60 model files.

    The counts are by `library_scan.MODEL_SUFFIXES`: `PlayTile - Blank` ships four
    `.stl` with four `.step` beside them, and `.step` counts, so the row reads 60
    and not 56. `PlayTiles/Accessory` is a `title-name` row on disk and is written
    here too. Nothing sits at the parent's own depth: the models live 45 in the
    first edition's ten part folders and 15 in `Wave 1.5`.
    """
    layout = {
        "Accessories": (5, 0),
        "Core": (3, 0),
        "Legs": (5, 0),
        "PlayTiles/PlayTile - 1x1": (4, 0),
        "PlayTiles/PlayTile - 2x2": (4, 0),
        "PlayTiles/PlayTile - 4x4": (3, 0),
        "PlayTiles/PlayTile - Blank": (4, 4),
        "PlayTiles/Accessory": (4, 0),
        "Rails/Rail - Elite": (5, 0),
        "Rails/Rail - Standard": (4, 0),
    }
    edition = root / "import" / "Stagetop" / STAGETOP_EDITION
    for folder, (stl_count, step_count) in layout.items():
        group = edition / folder
        for index in range(stl_count):
            write_file(group / f"part-{index}.stl", STL_BYTES)
        for index in range(step_count):
            write_file(group / f"part-{index}.step", b"ISO-10303-21;\n")
        write_file(group / "cover.jpg", b"j")
        write_text(group / "notes.txt", "notes\n")
    wave = root / "import" / "Stagetop" / "Wave 1.5"
    for index in range(15):
        write_file(wave / f"part-{index}.stl", STL_BYTES)
    write_file(wave / "cover.jpg", b"j")
    write_text(wave / "notes.txt", "notes\n")
    return root / "import" / "Stagetop"


def test_the_row_serves_the_engines_group_breakdown(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """The live route serves the engine's groups: one row, 60 models, 45 + 15.

    The engine's groups are hierarchical and the wire's `BundleGroupOut` is the flat
    three-field shape the queue renders and the create validates exclusions
    against, so the TOP level is what a row carries (Mf-1agz).

    Mutation: drop `groups` from `BundleDetectItemOut` and the response model silently
    drops the key (the Mf-nivh/Mf-au4k defect), so this reds.
    """
    stagetop_bundle(isolated_storage)
    moai_bundle(isolated_storage)
    scan()

    report = report_for(client, contributor)

    rows = [row for row in report["bundles"] if row["key"] == STAGETOP_KEY]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["detector"] == "engine:one-project"
    assert row["metadata_shape"] == "one-project"
    assert row["model_count"] == 60
    assert [(group["name"], group["model_count"]) for group in row["groups"]] == [
        (STAGETOP_EDITION, 45),
        ("Wave 1.5", 15),
    ], row["groups"]
    assert row["groups"] == row["matches"][0]["groups"], "the row and its match share one expression"
    # A plain row carries the engine's own top-level groups too: the fact sheet is
    # one measurement, and a group that is not `separable` is informational rather
    # than excludable.
    assert [group["name"] for group in row_for(report, MOAI)["groups"]] == ["files"]


def test_the_committed_openapi_fixture_declares_the_group_component() -> None:
    """The group is a pinned component with four required fields, and the row carries it.

    The rewrite run SKIPS, so a skipped regeneration would otherwise leave
    stale-but-valid JSON: this assertion is what catches it.
    """
    fixture = json.loads(OPENAPI_BUNDLES_FIXTURE.read_text(encoding="utf-8"))

    assert "BundleGroupOut" in fixture
    group = fixture["BundleGroupOut"]
    assert sorted(group["required"]) == ["foreign", "model_count", "name", "separable"]
    assert {name: prop["type"] for name, prop in group["properties"].items()} == {
        "name": "string",
        "model_count": "integer",
        "foreign": "boolean",
        "separable": "boolean",
    }
    assert "groups" in fixture["BundleDetectItemOut"]["properties"]
    create_item = fixture["BundleCreateItemIn"]
    assert "exclude_groups" not in create_item.get("required", []), "the field is additive, never required"
    assert create_item["properties"]["exclude_groups"]["type"] == "array"
    assert create_item["properties"]["exclude_groups"]["items"]["type"] == "string"


def test_the_route_serves_the_engines_whole_ranking(client: TestClient, contributor: Actor, isolated_storage: Path) -> None:
    """`matches` is the ENGINE's list on the wire, recommended first, in its order.

    Mutation: drop `matches` from ``BundleDetectItemOut`` and the response model
    silently drops the key - the Mf-nivh/Mf-au4k defect - so this reds. Mutation:
    append a legacy reading and a match appears whose rule is not ``engine:<shape>``.
    """
    moai_bundle(isolated_storage)
    stagetop_bundle(isolated_storage)
    collection_bundle(isolated_storage)
    scan()

    report = report_for(client, contributor)

    for row in report["bundles"]:
        matches = row["matches"]
        assert matches, row["key"]
        assert matches[0]["shape"] == row["metadata_shape"]
        assert matches[0]["confidence"] == row["confidence"]
        assert matches[0]["rule"] == row["detector"]
        assert matches[0]["groups"] == row["groups"]
        for match in matches:
            assert set(match) == MATCH_KEYS
            # The one owner of the vocabulary: nothing but the engine ranks, so every
            # rule is the engine's token and every shape the engine's own word.
            assert match["rule"] == "engine:" + match["shape"], match
            # A non-creatable match always says why; a creatable one carries a note
            # only when its boundary was unreadable (AC-4, Mf-ky16).
            if not match["creatable"]:
                assert match["note"].startswith("needs arranging first: "), match
            else:
                assert match["note"] is None or "could not read it" in match["note"], match
        # The engine's census is the row's census: one folder, one measurement.
        assert {(m["model_count"], m["image_count"], m["text_count"], m["other_count"]) for m in matches} == {
            (row["model_count"], row["image_count"], row["text_count"], row["other_count"])
        }
    # Three products in one folder: the engine recommends the split, and the
    # alternative one-project is the enactable one.
    collection = row_for(report, COLLECTION_KEY)
    assert collection["metadata_shape"] == "split-top-folders"
    assert [match["shape"] for match in collection["matches"]] == ["split-top-folders", "one-project"]
    assert [match["creatable"] for match in collection["matches"]] == [False, True]
    assert collection["matches"][0]["note"].startswith("needs arranging first: ")
    assert collection["matches"][1]["note"] is None
    # And a one-product folder offers exactly one reading.
    assert [match["shape"] for match in row_for(report, MOAI)["matches"]] == ["one-project"]


def test_a_creatable_match_carries_the_engines_note_when_the_boundary_is_unreadable(
    client: TestClient, contributor: Actor, isolated_storage: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A creatable `one-project` carries the engine's note when git could not read it.

    Mutation: force the unreadable-boundary recommendation without carrying its note
    and the operator enacts a project whose boundary nobody read, with no explanation
    - and with the split arms open it would be split instead. `subprocess.run` is
    patched so the fake `.git` reads the SAME on a host WITH git, where it would
    otherwise be a real "not a git repository" refusal that returns None.
    """
    unknown_boundary_bundle(isolated_storage)
    scan()

    def absent(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", absent)
    report = report_for(client, contributor)

    row = row_for(report, "import/Unreadable")
    match = row["matches"][0]
    assert match["shape"] == "one-project"
    assert match["creatable"] is True
    assert match["note"] == proposal_engine.unknown_boundary_note("git is not available")
    assert "could not read it" in match["note"]
    assert "git" in match["note"]
    assert {m["shape"] for m in row["matches"]} & {"split-top-folders", "split-model-folders"} == set()


def test_the_committed_openapi_fixture_declares_the_match_and_shape_fields() -> None:
    """The new wire shapes are pinned: a match component, a row list, an optional shape.

    The rewrite run SKIPS, so a skipped regeneration would otherwise leave
    stale-but-valid JSON: this assertion is what catches it.
    """
    fixture = json.loads(OPENAPI_BUNDLES_FIXTURE.read_text(encoding="utf-8"))

    assert "BundleMatchOut" in fixture
    match = fixture["BundleMatchOut"]
    assert sorted(match["required"]) == ["confidence", "rule", "shape"]
    assert match["properties"]["note"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    # Additive and defaulted permissively: a client from before this ticket keeps
    # working, because the default is the permissive value - a shape the server
    # cannot enact is the one that opts out. NOT required: the default is the one a
    # pre-existing payload already assumes.
    assert match["properties"]["creatable"]["type"] == "boolean"
    assert match["properties"]["creatable"]["default"] is True
    assert "creatable" not in match["required"], "the field is additive, never required"
    assert "matches" in fixture["BundleDetectItemOut"]["properties"]
    assert fixture["BundleDetectItemOut"]["properties"]["matches"]["items"]["$ref"] == (
        "#/components/schemas/BundleMatchOut"
    )
    create_item = fixture["BundleCreateItemIn"]
    assert "shape" not in create_item.get("required", []), "the field is additive, never required"
    assert create_item["properties"]["shape"]["anyOf"] == [{"type": "string"}, {"type": "null"}]


# --- the create follows the chosen shape (Mf-ao6r) --------------------------


def test_a_create_over_http_records_the_chosen_shape(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """The operator's shape crosses the router; the marker records it, not the row's pick.

    `import/Collection` is the row whose RECOMMENDATION is the split and whose
    alternative `one-project` is the enactable reading, so choosing it proves the
    field arrives: the marker says `one-project`, and the rule is the engine's own
    token.

    Mutation: drop `shape=item.shape` from the router's ``BundleCreateItem`` and the
    field is accepted and silently discarded - the Mf-ao6r no-op - so the marker
    records the split and this reds.
    """
    collection_bundle(isolated_storage)
    scan()
    row = row_for(report_for(client, contributor), COLLECTION_KEY)
    assert row["metadata_shape"] == "split-top-folders", "the recommendation is the other outcome, not this one"
    assert "one-project" in [match["shape"] for match in row["matches"]]

    response = client.post(
        "/bundles/create",
        json={"items": [{"key": COLLECTION_KEY, "shape": "one-project"}]},
        headers=bearer(contributor.token),
    )

    assert response.status_code == 200, response.text
    created = response.json()
    assert created["created"] == 1 and created["failed"] == 0, created
    project_id = created["results"][0]["project_id"]
    marker = project_by_id(client, contributor, project_id)["metadata_json"]["_bundle"]
    assert marker["shape"] == "one-project", "the chosen shape is what the marker records"
    assert marker["rule"] == "engine:one-project", "and the rule is the engine's own token"
    assert marker["detector"] == row["detector"], "the row's own fields are untouched by the choice"


def test_a_shape_less_create_over_http_records_the_recommended_shape(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """`shape` is an optional forward: omitting it still creates, on the row's shape.

    Stagetop is one product in two editions, so the engine recommends `one-project`
    and a shape-less create enacts exactly that.
    """
    stagetop_bundle(isolated_storage)
    scan()
    row = row_for(report_for(client, contributor), STAGETOP_KEY)
    assert row["metadata_shape"] == "one-project"

    response = client.post(
        "/bundles/create",
        json={"items": [{"key": STAGETOP_KEY}]},
        headers=bearer(contributor.token),
    )

    assert response.status_code == 200, response.text
    created = response.json()
    assert created["created"] == 1 and created["failed"] == 0, created
    project_id = created["results"][0]["project_id"]
    marker = project_by_id(client, contributor, project_id)["metadata_json"]["_bundle"]
    assert marker["shape"] == "one-project", "no shape sent means the recommendation, exactly as before"
    assert marker["rule"] == "engine:one-project"


def test_an_unoffered_shape_over_http_fails_and_creates_nothing(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """A shape the row never offered is a per-item failure over HTTP, not a silent pick."""
    moai_bundle(isolated_storage)
    scan()

    response = client.post(
        "/bundles/create",
        json={"items": [{"key": MOAI, "shape": "bogus"}]},
        headers=bearer(contributor.token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == 0 and body["failed"] == 1, body
    assert body["results"][0]["status"] == "failed"
    assert body["results"][0]["reason"] == "unknown shape"
    assert project_ids(client, contributor) == [], "an unoffered shape writes nothing"


# --- the recommendation is the engine's, and a split is an honest offer -------


def test_a_row_the_engine_reads_as_one_product_offers_exactly_that(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """One product in one folder: the served list is the engine's single option.

    Mutation: keep a legacy reading in the list - the `manual` scope, a detector
    shape - and this reds: the row offers one shape, it is the engine's, and it is
    enactable.
    """
    agreeing_bundle(isolated_storage)
    scan()

    row = row_for(report_for(client, contributor), AGREEING_KEY)

    assert row["detector"] == "engine:one-project"
    assert [match["shape"] for match in row["matches"]] == ["one-project"]
    assert [match["rule"] for match in row["matches"]] == ["engine:one-project"]
    assert all(match["creatable"] is True for match in row["matches"])
    assert all(match["note"] is None for match in row["matches"])


def test_the_dissent_walk_is_bounded_and_says_so(tmp_path: Path) -> None:
    """``measure`` counts the entries it visits and admits a partial census.

    The budget is checked per directory, so a walk may overshoot by one listing:
    the assertion is a band, not an equality. Mutation: drop the counter or the
    truncation flag and a 60-file folder walked with ``max_entries=20`` is
    presented as a complete census; bound the default (``None``) path too and the
    unbounded walk stops early instead of measuring the whole tree.
    """
    folder = tmp_path / "Flat"
    folder.mkdir()
    for index in range(60):
        (folder / f"part{index}.stl").write_bytes(b"solid part\n")

    bounded = proposal_engine.measure(str(folder), max_entries=20)
    assert bounded.truncated is True
    assert 20 <= bounded.entries <= 80, "one directory's listing clears the budget in one step"

    complete = proposal_engine.measure(str(folder))
    assert complete.truncated is False
    assert complete.entries == 60, "no budget means the whole tree, exactly as before"


def test_creating_a_split_recommendation_fails_with_needs_arrange_and_writes_nothing(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """AC-4: the honest split offer is refused at the door, before a project or a marker.

    The `split-and-create` operation (`POST /operations/plan` then
    `POST /operations`) is what enacts a split, so a
    CREATE of the split shape answers `failed`/`needs-arrange` and writes nothing at
    all: no project in the listing, no `_bundle` marker in any row, no assets added.
    A creatable create beside it still succeeds and still records its shape and rule,
    which is what makes the refusal a refusal of ONE shape rather than of the door.

    Mutation: drop the `creatable is False` guard from ``_create_one`` and the
    project below is written, so every "nothing was written" assertion reds.
    """
    collection_bundle(isolated_storage)
    agreeing_bundle(isolated_storage)
    scan()
    row = row_for(report_for(client, contributor), COLLECTION_KEY)
    assert any(
        match["shape"] == "split-top-folders" and match["creatable"] is False for match in row["matches"]
    ), "the fixture must offer the split this test refuses"

    refused = client.post(
        "/bundles/create",
        json={"items": [{"key": COLLECTION_KEY, "shape": "split-top-folders"}]},
        headers=bearer(contributor.token),
    )

    assert refused.status_code == 200, refused.text
    body = refused.json()
    assert (body["created"], body["failed"]) == (0, 1), body
    result = body["results"][0]
    assert result["status"] == "failed"
    assert result["reason"] == "needs-arrange"
    assert result["project_id"] is None
    assert result["assets_added"] == 0
    assert project_ids(client, contributor) == [], "a refused shape writes no project"
    with Session(engine) as session:
        markers = [
            project.id
            for project in session.exec(select(Project))
            if "_bundle" in json.loads(project.metadata_json or "{}")
        ]
    assert markers == [], "a refused shape writes no marker for a later arrange step to trip over"

    created = client.post(
        "/bundles/create", json={"items": [{"key": AGREEING_KEY}]}, headers=bearer(contributor.token)
    )

    assert created.status_code == 200, created.text
    made = created.json()
    assert (made["created"], made["failed"]) == (1, 0), made
    project_id = made["results"][0]["project_id"]
    marker = project_by_id(client, contributor, project_id)["metadata_json"]["_bundle"]
    assert marker["shape"] == "one-project", "the creatable shape is still created from its own offer"
    assert marker["rule"] == "engine:one-project"


# --- AUTO and the guided create (Mf-yz9e) -----------------------------------
#
# AUTO is one action on a high-confidence row the engine's own plan would not move.
# Every other band, shape and status is a `failed` with a reason, server-side, so a
# client can never decide a band. GUIDED writes exactly the metadata the operator
# accepted: a field absent from the map - a licence the operator dismissed - is
# never written, which is a server rule and not a client one.

VENDOR_KEY = "import/Moai statue -No overhang"
MEDIUM_KEY = "import/Widget kit"
PILE_KEY = "import/pile"
PDF_URL = "https://www.thingiverse.com/thing:1234567"
PDF_WHERE = "manual.pdf p2"


def write_pdf_pages(path: Path, pages: list[str]) -> None:
    """A minimal, uncompressed PDF whose pages carry the given text, in order.

    Hand-written like ``tests/test_proposal_engine_matrix.py``'s writer, extended to
    N pages: object 1 is the catalog, 2 the page tree, 3 the font, and every page
    takes a page object plus a content stream from 4 upwards. `pypdf` opens it and
    extracts each page's text, which is the point - the engine's page number in
    ``where`` ("manual.pdf p2") comes from reading the page, not from the suffix.
    """
    objects = {}
    kids = []
    number = 4
    for text in pages:
        page_number = number
        content_number = number + 1
        number += 2
        kids.append(page_number)
        lines = text.splitlines() or [""]
        escaped = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in lines]
        ops = "BT /F1 12 Tf 72 720 Td " + " ".join(f"({line}) Tj 0 -16 Td" for line in escaped) + " ET"
        stream = ops.encode("latin-1")
        objects[page_number] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents "
            + str(content_number).encode() + b" 0 R >>"
        )
        objects[content_number] = (
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = (
        b"<< /Type /Pages /Kids [" + b" ".join(f"{kid} 0 R".encode() for kid in kids)
        + b"] /Count " + str(len(kids)).encode() + b" >>"
    )
    objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    last = max(objects)
    for object_number in sorted(objects):
        offsets[object_number] = len(out)
        out += f"{object_number} 0 obj\n".encode() + objects[object_number] + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(last + 1).encode() + b"\n0000000000 65535 f \n"
    for object_number in range(1, last + 1):
        out += f"{offsets[object_number]:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size " + str(last + 1).encode() + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref).encode() + b"\n%%EOF\n"
    )
    write_file(path, bytes(out))


def vendor_bundle(root: Path) -> Path:
    """A HIGH documented product: a vendor README and LICENSE, two images, two part folders.

    MEASURED: one-project 50 `high` with `split-top-folders` 20 `low` as the second
    match, `arrange_moves(facts, "one-project") == []`, licence/creator/source URL
    `high` from LICENSE.txt and the licence `high` from README.txt, description
    `medium`. That is the row AUTO may run on: high, one-project, nothing to move.
    """
    bundle = root / "import" / "Moai statue -No overhang"
    write_text(bundle / "README.txt", README_TEXT)
    write_text(bundle / "LICENSE.txt", LICENSE_TEXT)
    write_file(bundle / "images" / "a.jpg", b"j" * 100)
    write_file(bundle / "images" / "b.jpg", b"j" * 900)
    for folder in ("Base", "Head"):
        for index in range(2):
            write_file(bundle / folder / f"part{index}.stl", STL_BYTES)
    return bundle


def medium_parts_bundle(root: Path) -> Path:
    """A product in two part folders with no document at all.

    MEASURED: `one-project` 45 `medium` - the band a client must not be able to
    promote, and the one AUTO refuses.
    """
    bundle = root / "import" / "Widget kit"
    for index in range(8):
        write_file(bundle / "Body" / f"part{index}.stl", STL_BYTES)
    for index in range(2):
        write_file(bundle / "Lid" / f"part{index}.stl", STL_BYTES)
    write_file(bundle / "cover.jpg", b"j" * 220)
    return bundle


def pdf_pile_bundle(root: Path) -> Path:
    """A LOW pile whose two-page manual names a Thingiverse URL on page TWO.

    MEASURED: row confidence `low`, `one-project` 40 `low`, and the manual's second
    page is the ONLY evidence - `metadata` carries `source_url` `medium` at
    "manual.pdf p2" and the provenance candidate is the same URL at the same place.
    """
    bundle = root / "import" / "pile"
    write_file(bundle / "x0.stl", STL_BYTES)
    write_file(bundle / "x1.stl", STL_BYTES)
    write_file(bundle / "spare" / "y.stl", STL_BYTES)
    write_pdf_pages(
        bundle / "manual.pdf",
        ["Assembly notes.", f"See the original at {PDF_URL} for updates."],
    )
    return bundle


def create_items(client: TestClient, actor: Actor, items: list[dict]) -> dict:
    response = client.post("/bundles/create", json={"items": items}, headers=bearer(actor.token))
    assert response.status_code == 200, response.text
    return response.json()


def bundle_audit_rows(key: str) -> list[dict]:
    with Session(engine) as session:
        rows = list(
            session.exec(
                select(AuditEvent).where(AuditEvent.action == audit.BUNDLE_CREATE)
            )
        )
    return [json.loads(row.detail_json or "{}") for row in rows if row.target == key]


def test_the_row_serves_the_metadata_suggestions_provenance_and_cover_faces(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """The row carries what a document claimed and WHERE, plus the faces a chooser may set.

    Mutation: drop `metadata_suggestions`/`provenance`/`cover_candidates`/`images`
    from `BundleDetectItemOut` and the response model silently drops them, so every
    assertion here reds even though the service computed them.
    """
    pdf_pile_bundle(isolated_storage)
    vendor_bundle(isolated_storage)
    scan()

    report = report_for(client, contributor)

    pile = row_for(report, PILE_KEY)
    assert {
        "field": "source_url",
        "value": PDF_URL,
        "where": PDF_WHERE,
        "confidence": "medium",
    } in pile["metadata_suggestions"], "the manual's second page is the only evidence there is"
    assert pile["provenance"]["candidates"][0]["where"] == PDF_WHERE
    assert pile["provenance"]["candidates"][0]["url"] == PDF_URL

    vendor = row_for(report, VENDOR_KEY)
    assert vendor["images"], "the vendor fixture has two registered previews"
    image_ids = {image["asset_id"] for image in vendor["images"]}
    assert vendor["cover"]["asset_id"] in image_ids, "the offered face must be one the chooser can see"
    registered = [entry for entry in vendor["cover_candidates"] if entry["asset_id"] is not None]
    assert registered, "a candidate backed by a registered asset is resolvable"
    assert all(entry["url"] for entry in registered), "a resolvable candidate serves its bytes"
    assert vendor["cover_candidates"][0]["rel_path"] == "images/b.jpg", "ranked by size, largest first"
    assert vendor["images"][0]["asset_id"] == vendor["cover_candidates"][0]["asset_id"]


def test_auto_creates_one_project_on_a_high_row(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """AUTO on a high row writes one project, the high metadata only, and says it was auto.

    Mutation: write every mined field instead of the `high` ones and the medium
    `description` appears; drop the marker's `operator` block and the record of the
    operator's decision is gone; drop the audit fields and the history loses the band.
    """
    vendor_bundle(isolated_storage)
    scan()

    body = create_items(client, contributor, [{"key": VENDOR_KEY, "auto": True}])

    assert (body["created"], body["failed"]) == (1, 0), body
    project_id = body["results"][0]["project_id"]
    project = project_by_id(client, contributor, project_id)
    assert project["name"] == "Moai statue -No overhang"
    assert project["licence"] == "CC BY-NC", "the licence is a HIGH field, so auto writes it"
    assert project["creator"] == "moai_maker"
    assert project["licence_url"] is not None, "also a HIGH field"
    assert project["description"] is None, "the mined description is MEDIUM and stays a suggestion"
    marker = project["metadata_json"]["_bundle"]
    assert marker["operator"]["auto"] is True
    assert marker["operator"]["cover"] == "engine"
    audit_rows = bundle_audit_rows(VENDOR_KEY)
    assert len(audit_rows) == 1
    assert audit_rows[0]["auto"] is True
    assert audit_rows[0]["accepted_fields"] == ["creator", "licence", "licence_url", "source_url"]


def test_auto_is_refused_for_medium_and_low_rows_server_side(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """AUTO on a medium or low row is a per-item `failed`, and nothing is written.

    Mutation: gate auto on the client only - i.e. drop the band check from
    ``_auto_refusal`` - and both rows create a project, so the empty listing reds.
    """
    medium_parts_bundle(isolated_storage)
    pdf_pile_bundle(isolated_storage)
    scan()

    body = create_items(
        client,
        contributor,
        [{"key": MEDIUM_KEY, "auto": True}, {"key": PILE_KEY, "auto": True}],
    )

    assert body["failed"] == 2 and body["created"] == 0, body
    assert [result["status"] for result in body["results"]] == ["failed", "failed"]
    assert [result["reason"] for result in body["results"]] == ["auto requires high confidence"] * 2
    assert project_ids(client, contributor) == [], "a refused auto writes nothing at all"


def test_auto_refuses_a_high_row_that_needs_arranging(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """A HIGH row whose own plan renames the folder is refused, not half-moved.

    `import/2493386_Moai_statue_-No_overhang` is high, one-project, and its folder
    name is not canonical, so `arrange_moves` returns exactly one rename. AUTO must
    never move a folder itself.

    Mutation: drop the `arrange_moves` gate and the create succeeds without the
    rename, leaving a project whose folder is not what the engine's plan said.
    """
    moai_bundle(isolated_storage)
    scan()
    row = row_for(report_for(client, contributor), MOAI)
    assert row["confidence"] == "high", "the fixture must be the HIGH row this test refuses"
    facts = proposal_engine.measure(str(isolated_storage / MOAI), MOAI)
    moves = proposal_engine.arrange_moves(facts, "one-project")
    assert len(moves) == 1 and moves[0]["action"] == "rename", moves

    body = create_items(client, contributor, [{"key": MOAI, "auto": True}])

    assert (body["created"], body["failed"]) == (0, 1), body
    reason = body["results"][0]["reason"]
    assert reason.startswith("auto needs arranging first:"), reason
    assert project_ids(client, contributor) == [], "a refused auto writes no project"


def test_a_second_auto_run_recognises_the_project_and_says_so(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """AUTO is idempotent: the second click skips with `already_created`.

    Mutation: bypass the dedupe on the auto path and the second run writes a second
    project, so both the status and the one-id listing red.
    """
    vendor_bundle(isolated_storage)
    scan()

    first = create_items(client, contributor, [{"key": VENDOR_KEY, "auto": True}])
    assert first["created"] == 1, first

    second = create_items(client, contributor, [{"key": VENDOR_KEY, "auto": True}])

    assert second["results"][0]["status"] == "skipped", second
    assert second["results"][0]["reason"] == "already_created"
    assert len(project_ids(client, contributor)) == 1


def test_guided_writes_only_the_metadata_the_operator_accepted(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """GUIDED writes the accepted fields and nothing else - a dismissal included.

    `licence` accepted, `creator` mapped to None (dismissed), everything else absent
    from the map. The row's own high `creator`/`licence_url` must NOT appear.

    Mutation: fall back to ``_enrichment`` for the fields absent from the map and
    `creator` becomes `moai_maker` and `licence_url` becomes the page's URL.
    """
    vendor_bundle(isolated_storage)
    scan()
    row = row_for(report_for(client, contributor), VENDOR_KEY)
    assert row["creator"] == "moai_maker" and row["licence_url"] is not None

    body = create_items(
        client,
        contributor,
        [{"key": VENDOR_KEY, "metadata": {"licence": "CC BY-NC", "creator": None}}],
    )

    assert (body["created"], body["failed"]) == (1, 0), body
    project = project_by_id(client, contributor, body["results"][0]["project_id"])
    assert project["licence"] == "CC BY-NC"
    assert project["creator"] is None, "the dismissed creator is never written"
    assert project["licence_url"] is None, "a field absent from the map is never written"
    marker = project["metadata_json"]["_bundle"]
    assert marker["operator"]["accepted"] == ["licence"]


def test_a_dismissed_licence_is_never_written(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """Only the creator was accepted, so the row's licence and its URL stay unwritten.

    Mutation: write the row's licence when it is absent from the accepted map and
    both assertions red.
    """
    vendor_bundle(isolated_storage)
    scan()

    body = create_items(
        client, contributor, [{"key": VENDOR_KEY, "metadata": {"creator": "moai_maker"}}]
    )

    assert (body["created"], body["failed"]) == (1, 0), body
    project = project_by_id(client, contributor, body["results"][0]["project_id"])
    assert project["creator"] == "moai_maker"
    assert project["licence"] is None, "a licence is never written without an explicit accept"
    assert project["licence_url"] is None


def test_the_operator_cover_overrides_the_engine_choice(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """The chooser's pick wins over the engine's largest image, and is recorded as `asset`.

    Mutation: ignore `item.cover` in ``_create_one`` and the project keeps the row's
    cover (the largest image), so the inequality reds.
    """
    vendor_bundle(isolated_storage)
    scan()
    row = row_for(report_for(client, contributor), VENDOR_KEY)
    images = {image["rel_path"]: image["asset_id"] for image in row["images"]}
    smallest = images[f"{VENDOR_KEY}/images/a.jpg"]
    assert row["cover"]["asset_id"] != smallest, "the engine's pick must not be the one chosen here"

    body = create_items(
        client,
        contributor,
        [{"key": VENDOR_KEY, "cover": {"mode": "asset", "asset_id": smallest}}],
    )

    assert (body["created"], body["failed"]) == (1, 0), body
    project = project_by_id(client, contributor, body["results"][0]["project_id"])
    assert project["cover_asset_id"] == smallest
    assert project["cover_asset_id"] != row["cover"]["asset_id"]
    marker = project["metadata_json"]["_bundle"]
    assert marker["operator"]["cover"] == "asset"


def test_the_operator_may_choose_no_cover_at_all(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """`mode: none` means no cover, which is NOT the same request as omitting the field.

    Mutation: treat `none` as "the engine decides" and the project keeps the row's
    largest image, so `cover_asset_id is None` reds.
    """
    vendor_bundle(isolated_storage)
    scan()
    row = row_for(report_for(client, contributor), VENDOR_KEY)
    assert row["cover"]["asset_id"] is not None, "the engine would have picked a face"

    body = create_items(client, contributor, [{"key": VENDOR_KEY, "cover": {"mode": "none"}}])

    assert (body["created"], body["failed"]) == (1, 0), body
    project = project_by_id(client, contributor, body["results"][0]["project_id"])
    assert project["cover_asset_id"] is None
    marker = project["metadata_json"]["_bundle"]
    assert marker["operator"]["cover"] == "none"


def test_a_cover_asset_outside_the_bundle_is_refused(
    client: TestClient, contributor: Actor, isolated_storage: Path
) -> None:
    """A cover asset that is not one of this row's files fails the item and writes nothing.

    Mutation: drop the membership resolution in ``_create_one`` and an unknown id
    reaches `create_project_from_members`, which refuses with a different reason
    (`cover asset is not a member of the project`) - or, worse, writes a dangling one.
    """
    vendor_bundle(isolated_storage)
    scan()

    body = create_items(
        client,
        contributor,
        [{"key": VENDOR_KEY, "cover": {"mode": "asset", "asset_id": "not-an-asset"}}],
    )

    assert (body["created"], body["failed"]) == (0, 1), body
    assert body["results"][0]["reason"] == "cover asset is not in this bundle"
    assert project_ids(client, contributor) == [], "a refused cover writes no project"
