"""The deleted enrichment door as the `enrich` workflow (Mf-y2u3 4/4).

`POST /bundles/enrich` is gone. The capability did not change:
`bundle_service.enrich_bundles` still reads the vendor pages a bundle names, fills
only the empty provenance fields, writes the cache once per call and reports a
refusal per bundle. What changed is the transport: the plan is the review the
one-call route never had, and the one step in the shared queue is the fetch.

Every test here is written to fail if the capability is absent:

* **the review names the rules** - fill only empties, never overwrite, the
  licence-only-from-the-page rule, the licence/URL pair, a refusal is a result,
  the cache, `refresh` and the 25-bundle cap.
* **planning opens no socket and walks nothing** - the planner keeps to the
  request's shape, because the primitive is the only fetch.
* **the step result is the deleted route's wire shape** - counters plus one
  result per key, verbatim.
* **a refusal is a result, never a failed operation** - one rate-limited page
  does not discard the others and no row is cached for it.
* **the cache answers before the network** - a fresh record makes no request at
  all, and `refresh` re-reads it.
* **undo removes what the run cached, and only that** - the truthful inverse of a
  step whose only persisted effect is the cache, and a later enrich's newer
  record is that operation's, not this one's.
* **the cookie never comes to rest** - the persisted step args and plan hold the
  redaction, never the value.
* **the level is the workflow's** - a contributor may plan and run it, a viewer
  is refused on both requests.

Harness: the idioms `tests/test_import_workflows.py` and
`tests/test_extract_workflow.py` established - one migrated database, a temp
storage root, the outbound fetch faked through the documented
`import_resolvers` patch point, auth off unless a test is about *who* may run it.
The bundle fixtures and the committed Thingiverse page heads are the ones
`tests/test_bundles_api.py` uses.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

import accounts
import asset_file_service
import asset_service
import audit
import auth
import bundle_service
import db
import import_resolvers
import library_scan
import login_throttle as login_throttle_module
import main
import operations_runner
import operations_service
import permissions
import settings_service
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

STL_BYTES = b"solid cube\nendsolid cube\n"

# The owner's 5390184 export (Mf-nivh): its directory name is the only evidence,
# so the row starts with no creator, no licence and a derived Thingiverse URL.
DRAGON = "5390184_ARTICULATED_ice_dragon__fixed_"
DRAGON_KEY = f"import/{DRAGON}"
DRAGON_URL = "https://www.thingiverse.com/thing:5390184"
CC_BY_NC_SA_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"
THINGIVERSE_FIXTURES = Path(__file__).parent / "fixtures" / "thingiverse"

BREAK_GLASS = {"username": "admin", "password": "super-secret"}
ADMIN_USERNAME = "root"
ADMIN_PASSWORD = "root-password-1"
CONTRIBUTOR_USERNAME = "contributor"
CONTRIBUTOR_PASSWORD = "contributor-password-1"
VIEWER_USERNAME = "viewer"
VIEWER_PASSWORD = "viewer-password-1"


# --- harness -----------------------------------------------------------------


def truncate_everything() -> None:
    """An empty ledger and an empty library.

    Audit rows are deliberately not removed: the append-only triggers refuse it,
    so each test addresses its own rows instead.
    """
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
    run_migrations(engine)
    truncate_everything()
    settings_service.set_setting(settings_service.BUNDLE_DISMISSALS_KEY, "")
    settings_service.set_setting(settings_service.BUNDLE_ENRICHMENTS_KEY, "")
    login_throttle_module.limiter.reset()
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    operations_runner.set_step_guard(None)
    yield
    operations_runner.set_step_guard(None)
    operations_runner.wait_idle(timeout=20)
    truncate_everything()
    settings_service.set_setting(settings_service.BUNDLE_DISMISSALS_KEY, "")
    settings_service.set_setting(settings_service.BUNDLE_ENRICHMENTS_KEY, "")
    login_throttle_module.limiter.reset()


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(accounts, "hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))


@pytest.fixture()
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp storage root every path-bound module reads at call time."""
    root = tmp_path / "storage"
    thumbs = root / "thumbs"
    thumbs.mkdir(parents=True)
    monkeypatch.setattr(db, "STORAGE", root)
    monkeypatch.setattr(db, "THUMBS", thumbs)
    monkeypatch.setattr(main, "THUMBS", thumbs)
    monkeypatch.setattr(asset_service, "STORAGE", root)
    monkeypatch.setattr(asset_service, "THUMBS", thumbs)
    monkeypatch.setattr(asset_file_service, "STORAGE", root)
    monkeypatch.setattr(library_scan, "STORAGE", root)
    monkeypatch.setattr(bundle_service, "STORAGE", root)
    return root


@pytest.fixture()
def client() -> TestClient:
    with TestClient(main.app) as test_client:
        yield test_client


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


def plan_of(
    client: TestClient,
    keys: list[str],
    *,
    headers: Optional[dict] = None,
    expect: int = 200,
    **params,
) -> dict:
    response = client.post(
        "/operations/plan",
        json={"workflow": "enrich", "target": "", "params": {"keys": keys, **params}},
        headers=headers or {},
    )
    assert response.status_code == expect, response.text
    return response.json()


def submit_of(
    client: TestClient,
    plan: dict,
    key: Optional[str] = None,
    *,
    headers: Optional[dict] = None,
    expect: int = 202,
) -> dict:
    response = client.post(
        "/operations",
        json={"plan_id": plan["plan_id"], "idempotency_key": key or uuid4().hex},
        headers=headers or {},
    )
    assert response.status_code == expect, response.text
    return response.json()


def operation_of(client: TestClient, operation_id: str, *, headers: Optional[dict] = None) -> dict:
    response = client.get(f"/operations/{operation_id}", headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()


def run_to_completion() -> None:
    operations_runner.dispatch_once()
    operations_runner.wait_idle(timeout=30)


def run_enrich(
    client: TestClient,
    keys: list[str],
    *,
    headers: Optional[dict] = None,
    key: Optional[str] = None,
    **params,
) -> dict:
    """Plan, submit and run one enrich; the finished operation is the caller's to read."""
    plan = plan_of(client, keys, headers=headers, **params)
    submitted = submit_of(client, plan, key, headers=headers)
    run_to_completion()
    body = operation_of(client, submitted["id"], headers=headers)
    assert body["status"] == "done", body
    return body


def undo_of(client: TestClient, operation_id: str) -> dict:
    response = client.post(f"/operations/{operation_id}/undo")
    assert response.status_code == 200, response.text
    return response.json()


def step_of(operation: dict, op: str) -> dict:
    return next(step for step in operation["steps"] if step["op"] == op)


def result_of(operation: dict) -> dict:
    return step_of(operation, "enrich")["result"]


def report_for(client: TestClient) -> dict:
    response = client.get("/bundles/detect")
    assert response.status_code == 200, response.text
    return response.json()


def row_for(report: dict, key: str) -> dict:
    return {row["key"]: row for row in report["bundles"]}[key]


def scan() -> None:
    state = library_scan.scan_storage_root()
    assert state.failed == 0, state.errors


def fixture_html(name: str) -> str:
    return json.loads(THINGIVERSE_FIXTURES.joinpath(name).read_text(encoding="utf-8"))["html"]


def stub_thing_page(monkeypatch: pytest.MonkeyPatch, html: str) -> list[tuple[str, Optional[str]]]:
    """Replace the fetch the primitive imports inside `enrich_bundles`; record its args."""
    calls: list[tuple[str, Optional[str]]] = []

    def fake_fetch(thing_id: str, thingiverse_cookie: Optional[str]) -> str:
        calls.append((thing_id, thingiverse_cookie))
        return html

    monkeypatch.setattr(import_resolvers, "fetch_thingiverse_thing_page", fake_fetch)
    return calls


def write_file(path: Path, payload: bytes = STL_BYTES) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def dragon_bundle(root: Path) -> Path:
    """The owner's export: one model under files/, no documents, the id in the name."""
    bundle = root / "import" / DRAGON
    write_file(bundle / "files" / "ARTICULATED_ice_dragon_fixed_p1.stl")
    return bundle


def documented_bundle(root: Path, name: str, text: str) -> Path:
    """A bundle whose README is the evidence: the URL it states is the row's source."""
    bundle = root / "import" / name
    write_file(bundle / "files" / "part.stl")
    write_text(bundle / "README.txt", text)
    return bundle


def frozen_clock(moment: datetime):
    """A stand-in whose `now` is fixed, so two runs can carry two cache stamps."""

    class FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return moment

    return FrozenDatetime


def enrich_rows(action: str = audit.BUNDLE_ENRICH) -> list[AuditEvent]:
    with Session(engine) as session:
        return list(session.exec(select(AuditEvent).where(AuditEvent.action == action)))


def undo_rows(operation_id: str) -> list[AuditEvent]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(AuditEvent).where(
                    AuditEvent.action == audit.OPERATION_UNDO,
                    AuditEvent.target == operation_id,
                )
            )
        )


# --- 1: the workflow fills only the gaps ------------------------------------


def test_the_workflow_fills_only_the_gaps_from_the_page(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run fills creator, licence and URL; the report shows them and a create records them.

    Mutation: fetch during detection and the before-report is already attributed;
    let the create recompute the provenance instead of copying the row and the
    marker's `enrichment` reds; add a key to the provenance block and the shape
    assertion reds.
    """
    dragon_bundle(storage)
    scan()
    before = row_for(report_for(client), DRAGON_KEY)
    assert before["enrichment"] is None, "nothing on this row came from a page yet"
    before_rows = {row.id for row in enrich_rows()}

    calls = stub_thing_page(monkeypatch, fixture_html("thing-5390184.json"))
    operation = run_enrich(client, [DRAGON_KEY])

    assert operation["workflow"] == "enrich" and operation["target"] == ""
    result = result_of(operation)
    assert result["fetched"] == 1
    assert calls == [("5390184", None)]
    item = result["results"][0]
    assert item["status"] == "fetched"
    assert item["filled"] == ["creator", "licence", "licence_url"]

    after = row_for(report_for(client), DRAGON_KEY)
    assert after["creator"] == "P1lotz"
    assert after["licence"] == "CC BY-NC-SA"
    assert after["licence_url"] == CC_BY_NC_SA_URL
    assert set(after["enrichment"]) == {"source_url", "fetched_at", "fields"}

    # The primitive's own row survives the move to operations, written under the
    # runner's context: the deleted route's sweep cannot cover `bundle.enrich`
    # any more, so the workflow's test owns it.
    written = [row for row in enrich_rows() if row.id not in before_rows]
    assert len(written) == 1
    assert written[0].target == DRAGON_KEY
    assert (written[0].method, written[0].request_id) == ("RUNNER", "-")
    assert json.loads(written[0].detail_json) == {
        "thing_id": "5390184",
        "source_url": DRAGON_URL,
        "fields": ["creator", "licence", "licence_url"],
    }

    created = client.post("/bundles/create", json={"items": [{"key": DRAGON_KEY}]})
    assert created.status_code == 200, created.text
    project = client.get("/projects").json()
    marker = {row["id"]: row for row in project}[created.json()["results"][0]["project_id"]]
    assert marker["creator"] == "P1lotz"
    assert marker["licence"] == "CC BY-NC-SA"
    assert set(marker["metadata_json"]["_bundle"]["enrichment"]) == {
        "source_url",
        "fetched_at",
        "fields",
    }


# --- 2: a human's value and the licence pair survive -----------------------


def test_a_human_creator_is_never_overwritten_and_the_licence_pair_is_never_orphaned(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A README value is not the page's to replace, and a licence without a URL keeps both.

    Mutation: let the record win over a truthy row value and the creator changes to
    P1lotz; take the page's licence URL while the row already holds a licence and
    the pair assertion reds.
    """
    documented_bundle(
        storage,
        "Dragon",
        "Created by Thingiverse user Ada Lovelace\n"
        "Licensed under Custom License\n"
        f"{DRAGON_URL}\n",
    )
    scan()
    before = row_for(report_for(client), "import/Dragon")
    assert before["creator"] == "Ada Lovelace"
    assert before["licence"] == "Custom License"
    assert before["licence_url"] is None, "an unknown token gets no link the table cannot back"

    stub_thing_page(monkeypatch, fixture_html("thing-5390184.json"))
    item = result_of(run_enrich(client, ["import/Dragon"]))["results"][0]

    assert item["status"] == "fetched"
    assert item["filled"] == [], "a page never overwrites a value the row already states"
    assert item["creator"] == "Ada Lovelace"
    assert item["licence"] == "Custom License"
    assert item["licence_url"] is None

    created = client.post("/bundles/create", json={"items": [{"key": "import/Dragon"}]})
    project_id = created.json()["results"][0]["project_id"]
    renamed = client.patch(f"/project/{project_id}", json={"creator": "A Human"})
    assert renamed.status_code == 200, renamed.text

    run_enrich(client, ["import/Dragon"], refresh=True)

    project = {row["id"]: row for row in client.get("/projects").json()}[project_id]
    assert project["creator"] == "A Human", "a human's edit is never re-claimed"
    assert project["licence"] == "Custom License"
    assert project["licence_url"] is None, "the licence pair is only ever written together"
    row = row_for(report_for(client), "import/Dragon")
    assert row["creator"] == "Ada Lovelace", "the report still shows the row's own value"
    assert row["licence"] == "Custom License"


# --- 3: a refusal is a result, never a failed step -------------------------


def test_a_404_is_a_refused_result_and_never_a_failed_step(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operation ends `done`, the per-bundle answer carries the vendor's sentence.

    Mutation: let the HTTPException escape the primitive and the step fails the
    whole operation, so the 200-page bundles beside it lose their answer too.
    """
    dragon_bundle(storage)
    scan()
    sentence = (
        "Thingiverse has no such thing: it answered HTTP 404 for thing 5390184."
    )
    before_rows = len(enrich_rows())

    def missing(thing_id: str, thingiverse_cookie: Optional[str]) -> str:
        raise HTTPException(status_code=404, detail=sentence)

    monkeypatch.setattr(import_resolvers, "fetch_thingiverse_thing_page", missing)

    operation = run_enrich(client, [DRAGON_KEY])

    result = result_of(operation)
    assert result["refused"] == 1 and result["fetched"] == 0
    assert result["results"][0]["status"] == "refused"
    assert result["results"][0]["reason"] == sentence
    assert bundle_service.get_enrichments() == {}, "a refusal caches nothing"
    assert len(enrich_rows()) == before_rows, "a refusal changed nothing, so it writes no enrich row"


# --- 4 and 5: the cache answers, and `refresh` ignores it ------------------


def test_a_fresh_cache_answers_cached_with_no_request(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second run is answered from the cache, so the raising stub is never called."""
    dragon_bundle(storage)
    scan()
    stub_thing_page(monkeypatch, fixture_html("thing-5390184.json"))
    assert result_of(run_enrich(client, [DRAGON_KEY]))["fetched"] == 1

    def refuse(*args, **kwargs):
        raise AssertionError("a fresh record must answer without a fetch")

    monkeypatch.setattr(import_resolvers, "fetch_thingiverse_thing_page", refuse)

    result = result_of(run_enrich(client, [DRAGON_KEY]))

    assert result["cached"] == 1 and result["fetched"] == 0
    assert result["results"][0]["status"] == "cached"
    assert result["results"][0]["filled"] == ["creator", "licence", "licence_url"]


def test_refresh_forces_the_request(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`refresh` re-reads a page the cache already answered; mutation: honour the TTL anyway."""
    dragon_bundle(storage)
    scan()
    calls = stub_thing_page(monkeypatch, fixture_html("thing-5390184.json"))
    assert result_of(run_enrich(client, [DRAGON_KEY]))["fetched"] == 1

    result = result_of(run_enrich(client, [DRAGON_KEY], refresh=True))

    assert result["fetched"] == 1 and result["cached"] == 0
    assert len(calls) == 2, "the page was read again despite a fresh record"


# --- 6: a key that is not a live bundle ------------------------------------


def test_a_key_that_is_not_a_live_bundle_is_unknown_or_a_plan_refusal(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan never walks; the run answers `unknown` and fetches nothing.

    Mutation: validate liveness at plan time and the plan's tripwire raises;
    fetch an unknown key and the second tripwire does.
    """
    key = "import/NotThere"
    with pytest.MonkeyPatch.context() as mp:

        def refuse_scan(*args, **kwargs):
            raise AssertionError("planning an enrich must not walk the disk")

        mp.setattr(bundle_service, "scan_bundles", refuse_scan)
        plan = plan_of(client, [key])

    assert plan["review"]["workflow"] == "enrich"
    assert plan["review"]["target"] == ""

    def refuse_fetch(*args, **kwargs):
        raise AssertionError("an unknown key must not be fetched")

    monkeypatch.setattr(import_resolvers, "fetch_thingiverse_thing_page", refuse_fetch)
    submitted = submit_of(client, plan)
    run_to_completion()

    result = result_of(operation_of(client, submitted["id"]))
    assert result["unknown"] == 1
    assert result["results"][0]["status"] == "unknown"
    assert result["results"][0]["reason"] == "not a live bundle"


# --- 7: no internet at all -------------------------------------------------


def test_a_fresh_record_makes_no_request_at_all(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demonstration's "no internet" case: the fetch stub's call count stays 0."""
    dragon_bundle(storage)
    scan()
    stub_thing_page(monkeypatch, fixture_html("thing-5390184.json"))
    assert result_of(run_enrich(client, [DRAGON_KEY]))["fetched"] == 1

    calls = stub_thing_page(monkeypatch, fixture_html("thing-5390184.json"))
    result = result_of(run_enrich(client, [DRAGON_KEY]))

    assert result["cached"] == 1 and result["fetched"] == 0
    assert calls == []


# --- 8: the undo's real inverse -------------------------------------------


def test_undo_removes_only_what_this_run_cached(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache record this run wrote goes; a later run's newer record is not its to remove.

    MEASURED: the step writes no library row, so the truthful inverse is the cache
    and nothing else. Two bundles share one thing page (same URL, different paths),
    so the second run replaces the first run's cache record without superseding
    its operation - the `fetched_at` stamp is what tells the two runs apart.

    Mutation: drop the cache entry without comparing the stamp and the first
    operation's undo removes the second run's record; report only `undone: True`
    and the counts assertion reds; drop a `cached` result's record and part three
    reds.
    """
    documented_bundle(storage, "One", f"{DRAGON_URL}\n")
    documented_bundle(storage, "Two", f"{DRAGON_URL}\n")
    scan()
    stub_thing_page(monkeypatch, fixture_html("thing-5390184.json"))

    first = run_enrich(client, ["import/One"])
    assert result_of(first)["fetched"] == 1
    assert len(bundle_service.get_enrichments()) == 1

    later = datetime.now(timezone.utc) + timedelta(hours=1)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(bundle_service, "datetime", frozen_clock(later))
        second = run_enrich(client, ["import/Two"], refresh=True)
    assert result_of(second)["fetched"] == 1
    newer = bundle_service.get_enrichments()["thingiverse:5390184"]
    assert newer["fetched_at"] == later.isoformat(timespec="seconds")

    body = undo_of(client, first["id"])

    assert body["records_dropped"] == 0
    assert body["records_kept"] == 1, "the newer record belongs to the later operation"
    assert bundle_service.get_enrichments() == {"thingiverse:5390184": newer}
    assert len(undo_rows(first["id"])) == 1

    body = undo_of(client, second["id"])

    assert body["records_dropped"] == 1
    assert body["records_kept"] == 0
    assert bundle_service.get_enrichments() == {}
    assert len(undo_rows(second["id"])) == 1

    third = run_enrich(client, ["import/One"])
    cached = run_enrich(client, ["import/One"])
    assert result_of(third)["fetched"] == 1
    assert result_of(cached)["cached"] == 1

    body = undo_of(client, cached["id"])

    assert body["records_dropped"] == 0
    assert body["records_kept"] == 0, "a cached-only run wrote nothing to take back"
    assert len(bundle_service.get_enrichments()) == 1, "the record the earlier run wrote stays"
    assert len(undo_rows(cached["id"])) == 1


# --- 9: the cookie never comes to rest ------------------------------------


def test_the_site_cookie_is_scrubbed_from_the_persisted_plan(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fetch gets the cookie; the persisted step args and plan never hold it.

    Mutation: skip `_scrub_cookies` and the value stays in `args_json` and
    `plan_json`; scrub only the step and the plan's intent is the leak.
    """
    dragon_bundle(storage)
    scan()
    calls = stub_thing_page(monkeypatch, fixture_html("thing-5390184.json"))
    plan = plan_of(
        client, [DRAGON_KEY], thingiverse_cookie="Cookie: session=super-secret\nother=1"
    )
    submitted = submit_of(client, plan)
    run_to_completion()

    assert calls == [("5390184", "session=super-secret")]
    with Session(engine) as session:
        step = session.exec(
            select(OperationStep).where(OperationStep.operation_id == submitted["id"])
        ).one()
        operation = session.get(Operation, submitted["id"])
        args_json = step.args_json
        plan_json = operation.plan_json
    assert '"thingiverse_cookie": "-"' in args_json
    assert '"thingiverse_cookie": "-"' in plan_json
    assert "super-secret" not in args_json
    assert "super-secret" not in plan_json


# --- 10: the level is the workflow's --------------------------------------


def test_a_contributor_enriches_and_a_viewer_is_refused(
    client: TestClient, storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CONTRIBUTE on plan and submit; the viewer is refused on both requests."""
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    dragon_bundle(storage)
    scan()
    stub_thing_page(monkeypatch, fixture_html("thing-5390184.json"))
    assert operations_service.WORKFLOW_LEVELS["enrich"] == permissions.CONTRIBUTE
    glass = client.post("/login", json=BREAK_GLASS)
    assert glass.status_code == 200, glass.text
    admin_token = sign_in(client, glass.json()["token"], ADMIN_USERNAME, ADMIN_PASSWORD, "admin")
    contributor = sign_in(
        client, admin_token, CONTRIBUTOR_USERNAME, CONTRIBUTOR_PASSWORD, accounts.CONTRIBUTOR_ROLE
    )
    viewer = sign_in(client, admin_token, VIEWER_USERNAME, VIEWER_PASSWORD, "viewer")

    planned = client.post(
        "/operations/plan",
        json={"workflow": "enrich", "target": "", "params": {"keys": [DRAGON_KEY]}},
        headers=bearer(viewer),
    )
    assert planned.status_code == 403, planned.text
    assert planned.json()["detail"] == permissions.DENY_DETAIL[permissions.CONTRIBUTE]

    plan = plan_of(client, [DRAGON_KEY], headers=bearer(contributor))
    refused = client.post(
        "/operations",
        json={"plan_id": plan["plan_id"], "idempotency_key": "viewer-enrich"},
        headers=bearer(viewer),
    )
    assert refused.status_code == 403, refused.text
    submitted = submit_of(client, plan, headers=bearer(contributor))
    run_to_completion()
    assert operation_of(client, submitted["id"], headers=bearer(contributor))["status"] == "done"


# --- 11: the review names the rules ---------------------------------------


def test_the_plan_review_names_the_enrich_rules(client: TestClient) -> None:
    """The review's `excluded` is the deleted door's contract, as prose for a human.

    Mutation: drop any rule from `_enrich_exclusions` and its keyword goes.
    """
    plan = plan_of(client, [DRAGON_KEY], thingiverse_cookie="session=secret")

    assert plan["target"] == ""
    assert [step["op"] for step in plan["steps"]] == ["enrich"]
    assert plan["steps"][0]["summary"] == "read the source page(s) for 1 bundle(s)"
    reasons = " ".join(entry["reason"] for entry in plan["review"]["excluded"])
    for needle in (
        "never overwritten",
        "only ever comes from the page",
        "ONE pair",
        "never a failed step",
        "fresh cache record answers without a request",
        "re-reads a page the cache already answered",
        "at most 25 bundles",
    ):
        assert needle in reasons, needle

    refreshed = plan_of(client, [DRAGON_KEY], refresh=True)
    assert refreshed["steps"][0]["summary"].endswith("again, ignoring the cache")


def test_an_empty_key_list_is_a_refusal_not_a_plan_with_nothing_to_do(client: TestClient) -> None:
    """Zeroness: the deleted door answered an empty request with an empty result; a plan refuses."""
    plan = plan_of(client, [], expect=400)
    assert plan["detail"] == "keys must be a list of bundle keys"


def test_a_param_outside_the_enrich_request_is_refused(client: TestClient) -> None:
    """The request is the client's, the steps are the server's: nothing rides in beside it."""
    raised = plan_of(client, [DRAGON_KEY], expect=400, entries=["part.stl"])
    assert raised["detail"] == (
        "unknown enrich parameter(s) entries: an enrich takes keys, refresh, thingiverse_cookie"
    )
