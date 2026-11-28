"""Regression tests for Mf-vesa: the session cookie is scoped to the public
rate-limit workarounds, and a paid/gated URL never reaches the network.

Each test pins one half of the policy:

* ``test_cookie_attaches_only_for_credentialed_hosts`` - a cookie goes to the two
  credentialed resolvers and to nothing else, look-alike hosts included.
* ``test_credentialed_registry_is_closed`` - the privileged host set is an
  enumerated registry, so a third credentialed source is a reviewed edit.
* ``test_paid_and_gated_urls_are_refused`` / ``_free_public_urls_are_not_refused``
  - the UX check matches whole path segments and query keys, so "reorder" and
  "paid_note" stay free while "cart" does not.
* ``test_paid_import_issues_no_request_and_no_cookie`` and
  ``test_open_import_response_refuses_purchase_state_at_any_depth`` - the guard
  runs before any connection exists, in every surviving import route and inside
  the fetch helper that HTML-discovered download URLs re-enter.
* ``test_a_paid_import_is_refused_when_it_is_planned`` and
  ``test_a_paid_import_is_refused_again_when_the_step_runs`` - the two doors that
  became the `import-link`/`import-zip` operations (Mf-y2u3 part 2/4) refuse at
  plan time with the same sentence, and again as a failed step when the resolved
  URL is the paid one.
* ``test_import_request_has_no_purchase_or_payment_fields`` - the app has no
  purchase state to store, request or replay.
* ``test_import_path_never_logs_the_cookie`` - the modules holding the cookie
  value have no logger at all; ``test_failed_paid_free_import_never_echoes_the_cookie``
  pins the error path that turns an upstream failure into a response body.
"""

import logging
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, delete

import import_policy
import import_service
import main
import operations_runner
import url_utils
from db import engine
from migrations import run_migrations
from models import Operation, OperationLock, OperationStep
from schemas import ImportProjectRequest, ImportRequest, ImportZipEntriesRequest, ImportZipExtractRequest

IMPORT_FIELDS = {"url", "title", "notes", "tags", "folder_id", "filename", "makerworld_cookie", "thingiverse_cookie"}

# The same credentialed body against a checkout URL, for every import route.
PAID_PAYLOAD = {"url": "https://makerworld.com/en/cart", "makerworld_cookie": "mw_session=SECRET"}

API_ROOT = Path(__file__).resolve().parents[1]
COOKIE_PATH_MODULES = (
    "import_service.py",
    "import_resolvers.py",
    "routers/imports.py",
    "import_policy.py",
    "safe_http.py",
)


def _truncate_ledger() -> None:
    with Session(engine) as session:
        for model in (OperationStep, OperationLock, Operation):
            session.exec(delete(model))
        session.commit()


@pytest.fixture(autouse=True)
def empty_ledger():
    """Leave no operation behind: another module's `dispatch_once` would run it.

    The two plan/submit tests below write real rows into the suite's shared
    database, and a queued row nobody finished would be picked up by the next
    module that drives the runner - against a URL this module only ever refused.
    """
    run_migrations(engine)
    _truncate_ledger()
    yield
    operations_runner.wait_idle(timeout=20)
    _truncate_ledger()


def auth_header(client: TestClient) -> dict:
    """Bearer token, or empty when the app is running with auth disabled."""
    health = client.get("/health").json()
    if not health.get("auth_required"):
        return {}
    token = client.post("/login", json={"username": "admin", "password": "super-secret"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "url,expects_cookie",
    [
        ("https://makerworld.com/en/models/1", True),
        ("https://www.makerworld.com/en/models/1", True),
        ("https://thingiverse.com/thing:1234", True),
        ("https://www.thingiverse.com/thing:1234", True),
        ("https://printables.com/model/1", False),
        ("https://evil-makerworld.com/en/models/1", False),
        ("https://makerworld.com.evil.io/en/models/1", False),
        ("https://notthingiverse.com/thing:1234", False),
        ("https://example.com/cube.stl", False),
        ("https://cdn.example.com/files/cube.stl", False),
        ("http://93.184.216.34/cube.stl", False),
    ],
)
def test_cookie_attaches_only_for_credentialed_hosts(monkeypatch, url, expects_cookie):
    # Both resolvers fall back to these env vars, so a contaminated env would
    # attach a cookie on a host that must never see one and flip the assertion.
    monkeypatch.delenv("MAKERWORLD_COOKIE", raising=False)
    monkeypatch.delenv("THINGIVERSE_COOKIE", raising=False)
    body = ImportRequest(url=url, makerworld_cookie="mw_session=SECRET", thingiverse_cookie="cf_clearance=SECRET")
    headers = import_service.import_request_headers(url, body)
    assert ("Cookie" in headers) is expects_cookie
    if not expects_cookie:
        assert not any("SECRET" in value for value in headers.values())


def test_credentialed_registry_is_closed():
    # Adding a third credentialed source must be a deliberate, reviewed edit:
    # this assertion is what makes it show up in review.
    assert set(import_policy.CREDENTIALED_HOST_MATCHERS) == {"makerworld", "thingiverse"}
    assert import_policy.credentialed_source_for("makerworld.com") == "makerworld"
    assert import_policy.credentialed_source_for("WWW.MakerWorld.com") == "makerworld"
    assert import_policy.credentialed_source_for("www.thingiverse.com") == "thingiverse"
    assert import_policy.credentialed_source_for("printables.com") is None
    assert import_policy.credentialed_source_for("evil-makerworld.com") is None
    assert import_policy.credentialed_source_for("makerworld.com.evil.io") is None
    assert import_policy.credentialed_source_for("") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://gumroad.com/l/xyz",
        "https://shop.gumroad.com/l/xyz",
        "https://makerworld.com/en/cart",
        "https://www.thingiverse.com/checkout",
        "https://example.com/orders/123",
        "https://example.com/purchase",
        "https://example.com/x?checkout=1",
        "https://example.com/cube.stl?order_id=9",
        "makerworld.com/en/payments",
    ],
)
def test_paid_and_gated_urls_are_refused(url):
    assert import_policy.purchase_state_reason(url)
    with pytest.raises(HTTPException) as excinfo:
        import_policy.refuse_paid_or_gated(url)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == import_policy.PAID_IMPORT_DETAIL
    assert "supply these files yourself" in excinfo.value.detail


@pytest.mark.parametrize(
    "url",
    [
        "https://makerworld.com/en/models/1",
        "https://www.thingiverse.com/thing:1234",
        "https://www.printables.com/model/1",
        "https://cdn.example.com/files/cube.stl",
        "https://example.com/reorder/cube.stl",
        "https://example.com/cube.stl?paid_note=free",
        "https://example.invalid/cube.stl",
    ],
)
def test_free_public_urls_are_not_refused(url):
    # Whole-segment matching: "reorder" and "paid_note" contain a purchase word
    # but are not one, so they must not trip the guard.
    assert import_policy.purchase_state_reason(url) is None


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/import/inspect", PAID_PAYLOAD),
        ("/import/project", PAID_PAYLOAD),
        ("/import/zip/entries", PAID_PAYLOAD),
    ],
)
def test_paid_import_issues_no_request_and_no_cookie(monkeypatch, path, payload):
    # Every import route must fail before any credentialed request exists.
    def refuse_to_open(*args, **kwargs):
        raise AssertionError("a paid import must not open a connection")

    monkeypatch.setattr(import_service, "safe_urlopen", refuse_to_open)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        res = client.post(path, json=payload, headers=auth_header(client))
    assert res.status_code == 403
    assert import_policy.PAID_IMPORT_DETAIL in res.text
    assert "SECRET" not in res.text


@pytest.mark.parametrize(
    "workflow,params",
    [("import-link", {}), ("import-zip", {"entries": ["cube.stl"]})],
)
def test_a_paid_import_is_refused_when_it_is_planned(monkeypatch, workflow, params):
    """The door's sentence at plan time: a plan is the first thing a client asks for."""

    def refuse_to_open(*args, **kwargs):
        raise AssertionError("a paid import must not open a connection")

    monkeypatch.setattr(import_service, "safe_urlopen", refuse_to_open)
    monkeypatch.setattr(url_utils, "resolve_remote_url", refuse_to_open)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        res = client.post(
            "/operations/plan",
            json={
                "workflow": workflow,
                "target": PAID_PAYLOAD["url"],
                "params": {**params, "makerworld_cookie": PAID_PAYLOAD["makerworld_cookie"]},
            },
            headers=auth_header(client),
        )
    assert res.status_code == 403
    assert import_policy.PAID_IMPORT_DETAIL in res.text
    assert "SECRET" not in res.text


@pytest.mark.parametrize(
    "workflow,params",
    [("import-link", {}), ("import-zip", {"entries": ["cube.stl"]})],
)
def test_a_paid_import_is_refused_again_when_the_step_runs(monkeypatch, workflow, params):
    """The run-time half: the plan's URL is free, the resolved one is not.

    The hop is only visible after resolution, so the resolver applies the same
    table - and the step carries the refusal as its own sentence, without a status
    prefix and without a connection.
    """

    def refuse_to_open(*args, **kwargs):
        raise AssertionError("a paid import must not open a connection")

    monkeypatch.setattr(main, "normalize_import_url", lambda url: PAID_PAYLOAD["url"])
    monkeypatch.setattr(import_service, "safe_urlopen", refuse_to_open)
    monkeypatch.setattr(url_utils, "resolve_remote_url", refuse_to_open)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        planned = client.post(
            "/operations/plan",
            json={
                "workflow": workflow,
                "target": "https://example.com/cube.stl",
                "params": {**params, "makerworld_cookie": "S"},
            },
            headers=auth_header(client),
        )
        assert planned.status_code == 200, planned.text
        submitted = client.post(
            "/operations",
            json={"plan_id": planned.json()["plan_id"], "idempotency_key": f"paid-step-{workflow}"},
            headers=auth_header(client),
        )
        assert submitted.status_code == 202, submitted.text
        operations_runner.dispatch_once()
        operations_runner.wait_idle(timeout=30)
        row = client.get(f"/operations/{submitted.json()['id']}", headers=auth_header(client)).json()

    assert row["status"] == "failed", row
    assert row["steps"][0]["error"] == import_policy.PAID_IMPORT_DETAIL


def test_open_import_response_refuses_purchase_state_at_any_depth(monkeypatch):
    # HTML-discovered download URLs re-enter this function, so the guard has to
    # live above the fetch, not only in the router.
    def refuse_to_open(*args, **kwargs):
        raise AssertionError("a paid import must not open a connection")

    monkeypatch.setattr(import_service, "safe_urlopen", refuse_to_open)
    url = "https://makerworld.com/en/checkout"
    body = ImportRequest(url=url, makerworld_cookie="S")
    with pytest.raises(HTTPException) as excinfo:
        import_service.open_import_response(url, body)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == import_policy.PAID_IMPORT_DETAIL


def test_a_redirect_to_a_paid_url_is_refused_and_closes_its_body(monkeypatch):
    # The requested URL is free; only the hop is paid. The documented promise is
    # that no paid page is fetched, and the hop is visible only after the first
    # request, so the already-open body must be closed on refusal.
    class PaidHopResponse:
        """A free URL's response that redirected to a checkout page."""

        def __init__(self) -> None:
            self.closed = False
            # Non-HTML, so reaching the body handling would end the refusal.
            self.headers = {"Content-Type": "application/octet-stream"}

        def geturl(self) -> str:
            return "https://makerworld.com/en/checkout"

        def close(self) -> None:
            self.closed = True

    def skip_url_validation(url: str) -> str:
        """Skip URL validation, which resolves DNS - a network call."""
        return url

    response = PaidHopResponse()

    def return_the_redirected_response(*args, **kwargs):
        return response

    monkeypatch.setattr(import_service, "validate_remote_url", skip_url_validation)
    monkeypatch.setattr(import_service, "safe_urlopen", return_the_redirected_response)
    body = ImportRequest(url="https://cdn.example.com/cube.stl", makerworld_cookie="S")
    with pytest.raises(HTTPException) as excinfo:
        import_service.open_import_response("https://cdn.example.com/cube.stl", body)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == import_policy.PAID_IMPORT_DETAIL
    assert response.closed is True


def test_import_request_has_no_purchase_or_payment_fields():
    # The app has no purchase state to store, request or replay; this fails
    # loudly if a payment field is ever added to the import body.
    assert set(ImportRequest.model_fields) == IMPORT_FIELDS
    assert len(ImportRequest.model_fields) == 8, "the base import body keeps its eight fields"
    # The project-import body subclasses the same base, so it inherits the sweep.
    for request_model in (ImportProjectRequest, ImportZipEntriesRequest, ImportZipExtractRequest):
        added = set(request_model.model_fields) - IMPORT_FIELDS
        for field in added:
            assert not any(marker in field for marker in ("pay", "purch", "order", "price")), field


def test_import_path_never_logs_the_cookie(caplog):
    # Other modules (asset_service, audit) do log, which is why the behavioural
    # half asserts on the captured records rather than on the code alone.
    caplog.set_level(logging.DEBUG)
    secret_body = ImportRequest(
        url="https://makerworld.com/en/models/1",
        makerworld_cookie="mw_session=SECRET123",
        thingiverse_cookie="cf_clearance=SECRET123",
    )
    for url in ("https://makerworld.com/en/models/1", "https://www.thingiverse.com/thing:1234"):
        headers = import_service.import_request_headers(url, secret_body)
        assert "Cookie" in headers
    assert "SECRET123" not in caplog.text
    # Static guarantee: the module that holds the value has no logger to log it,
    # so adding one fails this test loudly.
    for name in COOKIE_PATH_MODULES:
        source = (API_ROOT / name).read_text().lower()
        assert "logging" not in source, name
        assert "logger" not in source, name


def test_failed_paid_free_import_never_echoes_the_cookie(monkeypatch, caplog):
    # A failed credentialed fetch must neither log nor echo the credential: the
    # resolver turns HTTPError.reason into the refusal detail, which is the
    # realistic way a value from the request could reach the record.
    caplog.set_level(logging.DEBUG)

    def skip_url_validation(url: str) -> str:
        """Skip URL validation, which resolves DNS - a network call."""
        return url

    def fail_the_fetch(*args, **kwargs):
        raise HTTPError("https://makerworld.com/en/models/1", 403, "Forbidden", {}, None)

    monkeypatch.setattr(main, "normalize_import_url", skip_url_validation)
    monkeypatch.setattr(import_service, "validate_remote_url", skip_url_validation)
    monkeypatch.setattr(import_service, "safe_urlopen", fail_the_fetch)
    payload = {
        "url": "https://makerworld.com/en/models/1",
        "makerworld_cookie": "mw_session=SECRET123",
        "thingiverse_cookie": "cf_clearance=SECRET123",
    }
    with TestClient(main.app, raise_server_exceptions=False) as client:
        planned = client.post(
            "/operations/plan",
            json={
                "workflow": "import-link",
                "target": payload["url"],
                "params": {
                    "makerworld_cookie": payload["makerworld_cookie"],
                    "thingiverse_cookie": payload["thingiverse_cookie"],
                },
            },
            headers=auth_header(client),
        )
        assert planned.status_code == 200, planned.text
        submitted = client.post(
            "/operations",
            json={"plan_id": planned.json()["plan_id"], "idempotency_key": "cookie-echo"},
            headers=auth_header(client),
        )
        assert submitted.status_code == 202, submitted.text
        operations_runner.dispatch_once()
        operations_runner.wait_idle(timeout=30)
        res = client.get(f"/operations/{submitted.json()['id']}", headers=auth_header(client))
    assert res.status_code == 200
    # The upstream reason still reaches the operator, so the assertions above
    # cannot pass because the failure happened somewhere else entirely.
    assert "Failed to fetch URL: Forbidden" in res.text
    assert "SECRET123" not in res.text
    assert "SECRET123" not in caplog.text
