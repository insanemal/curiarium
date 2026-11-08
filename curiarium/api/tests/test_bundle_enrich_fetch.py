"""The one outbound call enrichment makes (Mf-nivh).

Nothing here touches a database or a socket: ``import_resolvers.safe_urlopen`` is
the seam every test replaces - the name the module actually calls, not
``safe_http.safe_urlopen`` - and the response is a stub, never a server. One test
installs socket tripwires, so "the stub answered" cannot be a coincidence.

Every refusal is pinned by the two things an operator reads: the status and the
detail. The three cookie refusals reuse ``import_resolvers.THINGIVERSE_COOKIE_REMEDY``,
so they name the Settings paste AND the environment variable - the gap that once
left this path pointing only at ``THINGIVERSE_COOKIE`` is closed (``Mm-folh``).
Each refusal asserts the constant itself rather than its wording, which is what
keeps this path from drifting away from the import path: the text can be
reworded and both move together. The import path's own pins live in
``test_import_cookie_messages.py``.

The only files read are the Thingiverse page fixtures inside this module's tree,
so the in-container build context carries them (AGENTS.md guardrail 3).
"""

import json
import logging
import socket
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest
from fastapi import HTTPException

import import_resolvers
import safe_http
from config import IMPORT_BROWSER_USER_AGENT, IMPORT_HTML_MAX_BYTES
from schemas import ImportRequest

FIXTURE_DIR = Path(__file__).parent.joinpath("fixtures/thingiverse")
THING_ID = "5390184"


def fixture(name: str) -> dict:
    """One page fixture: ``source_url``, ``html`` and its capture notes."""
    return json.loads(FIXTURE_DIR.joinpath(name).read_text())


class StubResponse:
    """Just enough of an ``http.client.HTTPResponse`` for the fetch function."""

    def __init__(self, body: bytes, headers: Message) -> None:
        self._body = body
        self.headers = headers

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]

    def __enter__(self) -> "StubResponse":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


class StubFetch:
    """Records the request it was handed and answers with the scripted outcome."""

    def __init__(self, body: bytes = b"", *, charset: str = "utf-8", error: Exception | None = None) -> None:
        self.body = body
        self.charset = charset
        self.error = error
        self.request = None
        self.timeout = None
        self.max_redirects = None
        self.calls = 0

    def __call__(self, request, *, timeout, max_redirects):
        self.calls += 1
        self.request = request
        self.timeout = timeout
        self.max_redirects = max_redirects
        if self.error is not None:
            raise self.error
        headers = Message()
        headers["Content-Type"] = f"text/html; charset={self.charset}"
        return StubResponse(self.body, headers)


def stub(monkeypatch: pytest.MonkeyPatch, **kwargs) -> StubFetch:
    """Replace the name ``import_resolvers`` calls; never the ``safe_http`` one."""
    fetch = StubFetch(**kwargs)
    monkeypatch.setattr(import_resolvers, "safe_urlopen", fetch)
    return fetch


def http_error(code: int) -> HTTPError:
    return HTTPError(f"https://www.thingiverse.com/thing:{THING_ID}", code, f"HTTP {code}", None, None)


# --- what the request is ---------------------------------------------------


def test_the_fetch_reads_the_public_thing_page_with_one_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The URL, the clock and the redirect budget are the contract of one call.

    Mutation: point the URL at the v2 API (403 without a token) or allow a
    redirect and this fails - and so would the live page.
    """
    fetch = stub(monkeypatch, body=b"<html></html>")

    import_resolvers.fetch_thingiverse_thing_page(THING_ID, None)

    assert fetch.request.full_url == f"https://www.thingiverse.com/thing:{THING_ID}"
    assert fetch.timeout == import_resolvers.ENRICH_TIMEOUT_SECONDS == 10
    assert fetch.max_redirects == import_resolvers.ENRICH_MAX_REDIRECTS == 0
    assert fetch.calls == 1, "a page is fetched once; a refusal is never retried"


def test_the_browser_user_agent_and_the_cookie_reach_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cookie is a header when given and absent when not, exactly like the import door."""
    fetch = stub(monkeypatch, body=b"<html></html>")

    import_resolvers.fetch_thingiverse_thing_page(THING_ID, "session=abc")
    with_cookie = dict(fetch.request.header_items())

    import_resolvers.fetch_thingiverse_thing_page(THING_ID, None)
    without_cookie = dict(fetch.request.header_items())

    assert with_cookie["User-agent"] == IMPORT_BROWSER_USER_AGENT
    assert with_cookie["Cookie"] == "session=abc"
    assert "Cookie" not in without_cookie, "an unauthenticated page must not carry an empty cookie"


def test_a_page_decodes_with_the_charset_it_announces(monkeypatch: pytest.MonkeyPatch) -> None:
    """A vendor page is not always UTF-8; replacement keeps a bad byte from raising."""
    stub(monkeypatch, body="Cr\u00e9ateur".encode("iso-8859-1"), charset="iso-8859-1")

    assert import_resolvers.fetch_thingiverse_thing_page(THING_ID, None) == "Cr\u00e9ateur"


# --- the refusals ----------------------------------------------------------


def test_a_rate_limit_names_the_shared_cookie_remedy(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 is the measured failure of unauthenticated traffic, so the remedy is
    the cookie.

    Mutation: hand-write the remedy here again and this fails on the constant
    guard - which is the point. Pinning ``THINGIVERSE_COOKIE_REMEDY`` rather
    than its literal text is what stops this path drifting from the import
    path: a reworded constant moves both, a reworded literal moves one.
    """
    stub(monkeypatch, error=http_error(429))

    with pytest.raises(HTTPException) as caught:
        import_resolvers.fetch_thingiverse_thing_page(THING_ID, None)

    assert caught.value.status_code == 429
    assert "429" in caught.value.detail
    assert THING_ID in caught.value.detail
    assert import_resolvers.THINGIVERSE_COOKIE_REMEDY in caught.value.detail
    assert "THINGIVERSE_COOKIE" in caught.value.detail
    assert "Settings > Import sources" in caught.value.detail
    assert "restart" in caught.value.detail


@pytest.mark.parametrize("code", [401, 403])
def test_a_refused_page_is_a_403_that_names_the_shared_cookie_remedy(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """A Cloudflare challenge answers 403 (and sometimes 401): both mean "no page for you".

    The detail names both cookie surfaces through the shared constant, so an
    operator whose cookie lives in Settings is not sent to a server they cannot
    reach.
    """
    stub(monkeypatch, error=http_error(code))

    with pytest.raises(HTTPException) as caught:
        import_resolvers.fetch_thingiverse_thing_page(THING_ID, None)

    assert caught.value.status_code == 403
    assert f"HTTP {code}" in caught.value.detail
    assert "challenge" in caught.value.detail
    assert import_resolvers.THINGIVERSE_COOKIE_REMEDY in caught.value.detail
    assert "Settings > Import sources" in caught.value.detail


def test_a_missing_thing_is_a_404_that_names_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 is the vendor saying the id is not a thing; the operator needs the id."""
    stub(monkeypatch, error=http_error(404))

    with pytest.raises(HTTPException) as caught:
        import_resolvers.fetch_thingiverse_thing_page(THING_ID, None)

    assert caught.value.status_code == 404
    assert THING_ID in caught.value.detail


@pytest.mark.parametrize("code", [500, 503, 520, 410])
def test_an_unmapped_status_is_a_502_and_never_a_raw_httperror(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """A 5xx (or a 4xx with no reading of its own) is a gateway failure, not a 500.

    Mutation: let the raw ``HTTPError`` escape and ``enrich_bundles`` - which
    catches only ``HTTPException`` - turns one vendor 503 into a 500 for the whole
    batch, discarding the other pages' answers and the single cache write.
    """
    stub(monkeypatch, error=http_error(code))

    with pytest.raises(HTTPException) as caught:
        import_resolvers.fetch_thingiverse_thing_page(THING_ID, None)

    assert caught.value.status_code == 502
    assert f"HTTP {code}" in caught.value.detail
    assert THING_ID in caught.value.detail
    assert "Nothing was changed" in caught.value.detail


def test_a_redirect_is_refused_because_the_canonical_url_never_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``safe_urlopen(max_redirects=0)`` reports the hop as a 400; this call says why.

    Mutation: allow one redirect and the detail turns back into the generic
    "Too many redirects while fetching URL", which tells an operator nothing.
    """
    stub(monkeypatch, error=HTTPException(status_code=400, detail="Too many redirects while fetching URL"))

    with pytest.raises(HTTPException) as caught:
        import_resolvers.fetch_thingiverse_thing_page(THING_ID, None)

    assert caught.value.status_code == 400
    assert THING_ID in caught.value.detail
    assert "redirect" in caught.value.detail.lower()
    assert "does not follow" in caught.value.detail


@pytest.mark.parametrize(
    "error",
    [URLError("dns is down"), TimeoutError("no answer in 10 s"), ConnectionResetError("peer reset")],
    ids=["urlerror", "timeout", "oserror"],
)
def test_a_network_failure_is_a_502_and_says_nothing_changed(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """A hung or unreachable vendor is a gateway failure, never a 500 and never a partial write."""
    stub(monkeypatch, error=error)

    with pytest.raises(HTTPException) as caught:
        import_resolvers.fetch_thingiverse_thing_page(THING_ID, None)

    assert caught.value.status_code == 502
    assert THING_ID in caught.value.detail
    assert "nothing was changed" in caught.value.detail.lower()


def test_an_unexpected_error_is_left_to_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    """500 is honest for a bug: a ``ValueError`` from our own code must not be dressed as a vendor refusal."""
    stub(monkeypatch, error=ValueError("a bug"))

    with pytest.raises(ValueError):
        import_resolvers.fetch_thingiverse_thing_page(THING_ID, None)


def test_a_page_larger_than_the_import_cap_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The body is read ``IMPORT_HTML_MAX_BYTES + 1``; the extra byte is the refusal."""
    stub(monkeypatch, body=b"x" * (IMPORT_HTML_MAX_BYTES + 1))

    with pytest.raises(HTTPException) as caught:
        import_resolvers.fetch_thingiverse_thing_page(THING_ID, None)

    assert caught.value.status_code == 502
    assert "larger than the import cap" in caught.value.detail
    assert str(IMPORT_HTML_MAX_BYTES) in caught.value.detail


def test_the_no_metadata_detail_names_the_id_the_remedy_and_the_outcome() -> None:
    """A 200 page with no thing metadata is the measured "missing thing" answer.

    Mutation: drop the challenge-page half and an operator facing a Cloudflare
    interstitial is told the thing was deleted, which is a wrong diagnosis.
    """
    detail = import_resolvers.thingiverse_no_metadata_detail(THING_ID)

    assert THING_ID in detail
    assert "no creator or licence" in detail
    assert "may not exist" in detail
    assert "challenge" in detail
    assert import_resolvers.THINGIVERSE_COOKIE_REMEDY in detail
    assert "Settings > Import sources" in detail
    assert "Nothing was changed" in detail


# --- the leak pins ---------------------------------------------------------


@pytest.mark.parametrize("code", [429, 401, 403])
def test_no_refusal_carries_the_cookie_it_was_given(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    """The cookie must reach the wire and nowhere a human reads.

    The two fetch refusals are the ones that could genuinely leak, because the
    cookie is in scope there; this drives the real seam with a fake value and
    proves the header really went out before pinning the detail clean - an
    assertion that would otherwise pass vacuously. The no-metadata detail takes
    no cookie, so its check is structural: nothing can put one there.
    """
    fetch = stub(monkeypatch, error=http_error(code))

    with pytest.raises(HTTPException) as caught:
        import_resolvers.fetch_thingiverse_thing_page(THING_ID, "session=fake-cookie")

    assert dict(fetch.request.header_items())["Cookie"] == "session=fake-cookie"
    assert "session=fake-cookie" not in caught.value.detail
    assert "session=fake-cookie" not in import_resolvers.thingiverse_no_metadata_detail(THING_ID)


def test_the_cookie_never_reaches_a_log_record(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A tripwire, not a description of today's behaviour.

    Nothing in ``import_resolvers`` or ``bundle_service`` logs right now, so
    this cannot pass by catching an existing line; it fails the day someone adds
    a debug line carrying the request headers. The wire assertion first keeps it
    from being vacuous.
    """
    caplog.set_level(logging.DEBUG)
    # Positive control: without it this could pass against a log that never
    # captured anything, which is the shape of a vacuous leak pin. It borrows the
    # module's own logger name, so propagation from a future leak is exercised
    # rather than the root handler alone.
    logging.getLogger("import_resolvers").debug("capture-is-live")
    fetch = stub(monkeypatch, error=http_error(429))

    with pytest.raises(HTTPException):
        import_resolvers.fetch_thingiverse_thing_page(THING_ID, "session=fake-cookie")

    assert "capture-is-live" in caplog.text
    assert dict(fetch.request.header_items())["Cookie"] == "session=fake-cookie"
    assert "session=fake-cookie" not in caplog.text


# --- the cookie resolver ---------------------------------------------------


def test_resolve_thingiverse_cookie_reads_either_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both request models carry the field, so the resolver is structural, not nominal.

    Mutation: annotate the parameter ``ImportRequest`` and the future
    ``BundleEnrichRequest`` is a type lie even though the code runs.
    """
    monkeypatch.delenv("THINGIVERSE_COOKIE", raising=False)
    import_request = ImportRequest(url="https://www.thingiverse.com/thing:1", thingiverse_cookie="Cookie: a=b; c=d")
    enrich_request = SimpleNamespace(thingiverse_cookie="c=d")

    assert import_resolvers.resolve_thingiverse_cookie(import_request) == "a=b; c=d"
    assert import_resolvers.resolve_thingiverse_cookie(enrich_request) == "c=d"


def test_resolve_thingiverse_cookie_falls_back_to_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The paste wins when present; the environment is the deployment-wide default.

    A whitespace-only paste is no cookie at all rather than the environment's:
    that is the shipped behaviour, pinned here so this step does not quietly
    change it while widening the annotation.
    """
    monkeypatch.setenv("THINGIVERSE_COOKIE", "env=1")

    assert import_resolvers.resolve_thingiverse_cookie(SimpleNamespace(thingiverse_cookie=None)) == "env=1"
    assert import_resolvers.resolve_thingiverse_cookie(SimpleNamespace(thingiverse_cookie="  ")) is None


# --- the tripwire ----------------------------------------------------------


def test_the_stub_is_what_is_called_and_no_socket_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test in this file may reach the network, and this is the proof.

    The stub replaces the name ``import_resolvers`` bound at import time, so
    replacing ``safe_http.safe_urlopen`` must NOT affect this call - that is the
    difference between stubbing the seam and stubbing the wrong module.
    """
    page = fixture("thing-5390184.json")

    def refuse(*args, **kwargs):
        raise AssertionError("this test must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(safe_http, "safe_urlopen", refuse)
    fetch = stub(monkeypatch, body=page["html"].encode("utf-8"))

    html = import_resolvers.fetch_thingiverse_thing_page(THING_ID, "session=abc")

    assert fetch.calls == 1
    assert "P1lotz" in html
