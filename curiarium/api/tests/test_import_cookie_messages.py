"""The Thingiverse refusals an import can hit, and the one remedy they carry.

Every shape below composes the *same* remedy, and a transport failure is the one
answer that carries none, because a server it could not reach says nothing about
the cookie. The remedy names both surfaces because the import path really has
both: ``UploadBar`` sends the pasted Cookie header as ``thingiverse_cookie`` and
``resolve_thingiverse_cookie`` prefers that paste over ``os.getenv``'s
``THINGIVERSE_COOKIE``. Naming only the variable strands the operator who can use
Settings; naming only Settings strands the operator who cannot reach the UI.

This module is the anti-regression guard for that correctness: the Settings
pointer is right and must survive, so a future reader who deletes it - or who
lets a vendor JSON body replace the remedy - sees red here. Nothing opens a
socket: ``import_resolvers.safe_urlopen`` is replaced (the name the module
actually calls), and one test installs socket tripwires so "the stub answered"
cannot be a coincidence.
"""

import io
import socket
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest
from fastapi import HTTPException

import import_resolvers
from config import IMPORT_HTML_MAX_BYTES

# One thing page: both refusals are reached with the same URL.
PAGE_URL = "https://www.thingiverse.com/thing:5390184"


class StubFetch:
    """Raises the scripted ``HTTPError`` - the only outcome these refusals need."""

    def __init__(self, error: HTTPError) -> None:
        self.error = error
        self.request = None
        self.calls = 0

    def __call__(self, request, *, timeout):
        self.calls += 1
        self.request = request
        raise self.error


def stub(monkeypatch: pytest.MonkeyPatch, error: HTTPError) -> StubFetch:
    """Replace the name ``import_resolvers`` calls; never the ``safe_http`` one."""
    fetch = StubFetch(error)
    monkeypatch.setattr(import_resolvers, "safe_urlopen", fetch)
    return fetch


def http_error(code: int, body: bytes | None = None) -> HTTPError:
    """A vendor refusal whose body is really readable, so the JSON branch runs.

    The sibling module passes ``None`` as the fp; that makes ``err.read()`` raise
    and exercises the no-body branch by accident. Here an ``io.BytesIO`` body is
    the point of several tests.
    """
    headers = Message()
    headers["Content-Type"] = "application/json"
    fp = None if body is None else io.BytesIO(body)
    return HTTPError(PAGE_URL, code, f"HTTP {code}", headers, fp)


def refusal(monkeypatch: pytest.MonkeyPatch, error: HTTPError) -> HTTPException:
    """Call the view-token fetch and hand back the refusal it raised."""
    stub(monkeypatch, error)
    with pytest.raises(HTTPException) as caught:
        import_resolvers.fetch_thingiverse_view_token(PAGE_URL, None)
    return caught.value


class StubResponse:
    """A 200 answer: real headers, and a read that honours the size it is asked for."""

    def __init__(self, body: bytes, content_type: str) -> None:
        self.body = body
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.url = PAGE_URL

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> "StubResponse":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


class ScriptedFetch:
    """Answers each request from a script, by TYPE, recording every URL it saw.

    Dispatching on type rather than truthiness is the point: ``{}`` and
    ``{"files": []}`` are scripted on purpose and are both falsy as data.
    """

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.urls: list[str] = []

    def __call__(self, request, *, timeout):
        self.urls.append(request.full_url)
        if not self.outcomes:
            raise AssertionError(f"unscripted fetch of {request.full_url}")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def script(monkeypatch: pytest.MonkeyPatch, *outcomes: object) -> ScriptedFetch:
    """Replace the name ``import_resolvers`` calls, answering from the script."""
    fetch = ScriptedFetch(*outcomes)
    monkeypatch.setattr(import_resolvers, "safe_urlopen", fetch)
    return fetch


AUTH_VIEW_URL = "https://www.thingiverse.com/api/v2/auth/view"
FILES_URL = "https://www.thingiverse.com/api/v2/things/5390184/files"
THING_URL = "https://www.thingiverse.com/api/v2/things/5390184"


def tripwired_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket in these tests is a bug: the stub is the only fetcher."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("this test must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


# --- the auth refusal ------------------------------------------------------


def test_a_vendor_json_body_is_kept_and_the_remedy_still_follows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The vendor text is quoted, but the remedy is appended rather than displaced.

    Mutation: ``detail = message or (...)`` - the shipped shape - and the JSON
    body swallows the remedy, leaving an operator with "Unauthorized" and no step.
    """
    error = refusal(monkeypatch, http_error(403, b'{"error": "Unauthorized"}'))

    assert error.status_code == 403
    assert "Unauthorized" in error.detail
    assert "THINGIVERSE_COOKIE" in error.detail
    assert "Settings" in error.detail
    assert "Nothing was changed" in error.detail


def test_an_empty_body_names_the_block_and_both_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 with no JSON has nothing to quote, so the generic sentence carries the meaning."""
    error = refusal(monkeypatch, http_error(429, b""))

    assert error.status_code == 429
    assert "Thingiverse blocked the request." in error.detail
    assert "THINGIVERSE_COOKIE" in error.detail
    assert "Settings" in error.detail


def test_a_status_outside_the_refusals_produces_no_detail_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """500 is not "paste a cookie": the fetch answers ``None`` and the caller decides."""
    stub(monkeypatch, http_error(500))

    assert import_resolvers.fetch_thingiverse_view_token(PAGE_URL, None) is None


# --- the no-cookie refusal -------------------------------------------------


def test_the_no_cookie_import_url_403_names_the_variable_the_paste_and_the_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second refusal shares the remedy word for word, so the two cannot drift.

    Mutation: keep the bare "requires a session cookie" sentence and an operator
    knows what failed but not what to do about it.
    """
    monkeypatch.setattr(import_resolvers, "fetch_thingiverse_view_token", lambda *args: None)

    with pytest.raises(HTTPException) as caught:
        import_resolvers.resolve_thingiverse_download_url(PAGE_URL, None)

    detail = caught.value.detail
    assert caught.value.status_code == 403
    assert "requires a session cookie" in detail
    assert "THINGIVERSE_COOKIE" in detail
    assert "Settings" in detail
    assert "Nothing was changed" in detail


# --- what the remedy must never do -----------------------------------------


@pytest.mark.parametrize(
    "body",
    [b"", b'{"error": "Unauthorized"}'],
    ids=["empty-body", "vendor-json"],
)
def test_the_cookie_value_never_reaches_the_detail(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    """The remedy may NAME the variable; echoing the secret into a UI toast would leak it."""
    stub(monkeypatch, http_error(403, body))

    with pytest.raises(HTTPException) as caught:
        import_resolvers.fetch_thingiverse_view_token(PAGE_URL, "session=supersecret")

    assert "supersecret" not in caught.value.detail


def test_a_huge_vendor_body_cannot_bury_the_remedy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The vendor text is bounded to 300 characters so the remedy is never pushed off the screen.

    Mutation: drop the slice and a chatty upstream turns the message into a wall
    the operator scrolls past - with the remedy somewhere below the fold.
    """
    error = refusal(monkeypatch, http_error(403, b'{"error": "' + b"x" * 1000 + b'"}'))

    assert "x" * 300 in error.detail
    assert "x" * 301 not in error.detail
    assert "THINGIVERSE_COOKIE" in error.detail


# --- the tripwire ----------------------------------------------------------


def test_the_stub_is_what_is_called_and_no_socket_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test in this file may reach the network, and this is the proof."""

    def refuse(*args, **kwargs):
        raise AssertionError("this test must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    fetch = stub(monkeypatch, http_error(403, b'{"error": "Unauthorized"}'))

    with pytest.raises(HTTPException):
        import_resolvers.fetch_thingiverse_view_token(PAGE_URL, "session=abc")

    assert fetch.calls == 1
    assert fetch.request.full_url == "https://www.thingiverse.com/api/v2/auth/view"


# --- a 200 the server cannot use -------------------------------------------


def test_a_tokenless_200_names_the_stale_cookie_and_the_shared_remedy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The AC's headline shape: a 200 that carries no token means a dead cookie.

    Mutation: treat it as "no token, fall through" and the operator is told to
    use a direct download link for a page that is not broken - the remedy for a
    different problem entirely.
    """
    fetch = script(monkeypatch, StubResponse(b"{}", "application/json"))

    tripwired_sockets(monkeypatch)
    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        import_resolvers.fetch_thingiverse_view_token(PAGE_URL, "session=abc")

    detail = caught.value.detail
    assert caught.value.status_code == 403
    assert detail.endswith(import_resolvers.THINGIVERSE_COOKIE_REMEDY)
    assert "Settings > Import sources" in detail
    assert "signed-out or expired" in detail
    assert "direct download link" not in detail
    assert "session=abc" not in detail
    assert fetch.urls == [AUTH_VIEW_URL]


def test_a_tokenless_200_without_a_cookie_still_answers_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cookie_sent=False column for the headline row.

    This is the one row the six-shape table cannot prove: its 200 is a valid
    object, so it never reaches the shared table, and without this pin dropping
    the gate on it leaves the whole suite green. With no cookie it must fall
    through to today's find_download_url rescue instead of refusing, or every
    anonymous import of a public page loses its rescue.
    """
    fetch = script(monkeypatch, StubResponse(b"{}", "application/json"))

    assert import_resolvers.fetch_thingiverse_view_token(PAGE_URL, None) is None
    assert fetch.urls == [AUTH_VIEW_URL], "the request is still made without a cookie"


VIEW_TOKEN_SHAPES = [
    pytest.param(b"", "application/json", "with an empty body", id="empty-body"),
    pytest.param(b"<html>challenge</html>", "text/html", "cannot read", id="challenge-page"),
    pytest.param(b"[1, 2, 3]", "application/json", "cannot read", id="json-that-is-not-an-object"),
    pytest.param(b"not json", "application/json", "cannot read", id="unparseable"),
    pytest.param(b"\xff\xfe\x00", "application/json", "cannot read", id="undecodable-bytes"),
    pytest.param(b"x" * (IMPORT_HTML_MAX_BYTES + 1), "application/json", "larger than the import cap", id="over-the-cap"),
]


@pytest.mark.parametrize("body, content_type, lead", VIEW_TOKEN_SHAPES)
def test_a_cookie_that_goes_unchecked_is_never_silent(
    monkeypatch: pytest.MonkeyPatch, body: bytes, content_type: str, lead: str
) -> None:
    """Every unusable answer becomes the remedy when a cookie was actually sent.

    Six shapes, one column: the shape only decides the LEAD, never whether the
    operator is told. The ban on "signed-out or expired" is the other half - a
    body this server could not read is not proof the cookie is dead.
    """
    fetch = script(monkeypatch, StubResponse(body, content_type))

    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        import_resolvers.fetch_thingiverse_view_token(PAGE_URL, "session=abc")

    detail = caught.value.detail
    assert caught.value.status_code == 403
    assert lead in detail, "the shape picks the lead; only the class is shared"
    assert detail.endswith(import_resolvers.THINGIVERSE_COOKIE_REMEDY)
    assert "Settings > Import sources" in detail
    assert "THINGIVERSE_COOKIE" in detail
    assert "Nothing was changed." in detail
    assert "signed-out or expired" not in detail
    assert "direct download link" not in detail
    assert "session=abc" not in detail
    assert fetch.urls == [AUTH_VIEW_URL]


@pytest.mark.parametrize("body, content_type, _lead", VIEW_TOKEN_SHAPES)
def test_the_same_shapes_without_a_cookie_still_answer_none(
    monkeypatch: pytest.MonkeyPatch, body: bytes, content_type: str, _lead: str
) -> None:
    """The cookie_sent=False column, and the mutation that proves the gate is real.

    Drop the gate and this column raises instead of answering None, which would
    take today's find_download_url rescue away from every anonymous import.
    """
    fetch = script(monkeypatch, StubResponse(body, content_type))

    assert import_resolvers.fetch_thingiverse_view_token(PAGE_URL, None) is None
    assert fetch.urls == [AUTH_VIEW_URL], "the request is still made without a cookie"


# --- what a refusal must not claim -----------------------------------------


@pytest.mark.parametrize(
    "outcome, claim",
    [
        pytest.param(http_error(500), "Nothing was changed.", id="unread-status"),
        pytest.param(URLError("unreachable"), "nothing was changed.", id="unreachable"),
    ],
)
def test_a_transport_failure_is_never_called_a_dead_cookie(
    monkeypatch: pytest.MonkeyPatch, outcome: Exception, claim: str
) -> None:
    """A 500 or a URLError is not evidence about the cookie, so it names no remedy.

    Both still promise the operator that nothing was written, because both raises
    precede any write - the same promise the remedy carries, said without it.
    """
    script(monkeypatch, outcome)

    with pytest.raises(HTTPException) as caught:
        import_resolvers.fetch_thingiverse_view_token(PAGE_URL, "session=abc")

    detail = caught.value.detail
    assert caught.value.status_code == 502
    assert not isinstance(caught.value, import_resolvers.CredentialRefusal)
    assert import_resolvers.THINGIVERSE_COOKIE_REMEDY not in detail
    assert "THINGIVERSE_COOKIE" not in detail
    assert claim in detail
    assert "direct download link" not in detail
    assert "session=abc" not in detail


def test_a_guard_refusal_from_the_url_opener_is_never_relabelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """`safe_urlopen`'s own HTTPException is a policy refusal, not a credential verdict.

    There is no broad `except` around that call, so the guard's exception and its
    detail reach the operator exactly as they were raised.
    """
    guard = HTTPException(status_code=400, detail="Remote host is not allowed")
    script(monkeypatch, guard)

    with pytest.raises(HTTPException) as caught:
        import_resolvers.fetch_thingiverse_view_token(PAGE_URL, "session=abc")

    assert caught.value is guard
    assert not isinstance(caught.value, import_resolvers.CredentialRefusal)
    assert "THINGIVERSE_COOKIE" not in caught.value.detail


@pytest.mark.parametrize(
    "outcome",
    [pytest.param(http_error(500), id="unread-status"), pytest.param(URLError("unreachable"), id="unreachable")],
)
def test_a_transport_failure_without_a_cookie_keeps_answering_none(
    monkeypatch: pytest.MonkeyPatch, outcome: Exception
) -> None:
    """Today's behaviour for a cookie-less server: the caller's rescue still runs."""
    script(monkeypatch, outcome)

    assert import_resolvers.fetch_thingiverse_view_token(PAGE_URL, None) is None


# --- the files step --------------------------------------------------------


@pytest.mark.parametrize("code", [401, 403, 429])
def test_a_files_api_refusal_names_the_thing_it_was_reading(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    """A 200 token then a refusal: the reachable "no file" shape, and it is the cookie's fault.

    All three statuses are scripted because the status is carried through
    unchanged (`err.code or 400`), so a 401 stays a 401 - the web client's
    handling of that status is Ma-3vov's, not this ticket's.
    """
    fetch = script(
        monkeypatch,
        StubResponse(b'{"access": "tok"}', "application/json"),
        http_error(code, b'{"error": "Unauthorized"}'),
    )

    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        import_resolvers.resolve_thingiverse_download_url(PAGE_URL, "session=abc")

    detail = caught.value.detail
    assert caught.value.status_code == code
    assert "refused the download API for thing 5390184" in detail
    assert f"(HTTP {code})" in detail
    assert "Unauthorized" in detail
    assert detail.endswith(import_resolvers.THINGIVERSE_COOKIE_REMEDY)
    assert "direct download link" not in detail
    assert "session=abc" not in detail
    assert fetch.urls == [AUTH_VIEW_URL, FILES_URL]


def test_a_huge_files_step_refusal_cannot_bury_the_remedy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 300-character bound is the files step's too, not only the view token's."""
    script(
        monkeypatch,
        StubResponse(b'{"access": "tok"}', "application/json"),
        http_error(403, b'{"error": "' + b"x" * 1000 + b'"}'),
    )

    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        import_resolvers.resolve_thingiverse_download_url(PAGE_URL, "session=abc")

    assert "x" * 300 in caught.value.detail
    assert "x" * 301 not in caught.value.detail
    assert caught.value.detail.endswith(import_resolvers.THINGIVERSE_COOKIE_REMEDY)


def test_the_same_files_refusal_without_a_cookie_stays_the_vendor_word_for_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a cookie the vendor's word is all there is, and it must not change."""
    script(
        monkeypatch,
        StubResponse(b'{"access": "tok"}', "application/json"),
        http_error(403, b'{"error": "Unauthorized"}'),
    )

    with pytest.raises(HTTPException) as caught:
        import_resolvers.resolve_thingiverse_download_url(PAGE_URL, None)

    assert caught.value.status_code == 403
    assert caught.value.detail == "Unauthorized"
    assert not isinstance(caught.value, import_resolvers.CredentialRefusal)
    assert "5390184" not in caught.value.detail
    assert import_resolvers.THINGIVERSE_COOKIE_REMEDY not in caught.value.detail


def test_a_token_that_finds_no_file_url_still_answers_the_zip_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The AC's second shape, pinned for what it really does.

    A response with a token can never reach the stale-cookie wording: the two API
    candidates are tried and the zip URL answers unconditionally. That is why the
    wording is not pinned here - this is the shape, and all three URLs are
    recorded so no other door was tried.
    """
    fetch = script(
        monkeypatch,
        StubResponse(b'{"access": "tok"}', "application/json"),
        StubResponse(b'{"files": []}', "application/json"),
        StubResponse(b"{}", "application/json"),
    )

    url = import_resolvers.resolve_thingiverse_download_url(PAGE_URL, "session=abc")

    assert url == "https://www.thingiverse.com/thing:5390184/zip"
    assert fetch.urls == [AUTH_VIEW_URL, FILES_URL, THING_URL]


# --- the refusal that must survive -----------------------------------------


def test_the_auth_refusal_is_a_credential_refusal_and_keeps_its_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row 1 gains a class, not a word: the detail is byte-identical to before."""
    stub(monkeypatch, http_error(403, b'{"error": "Unauthorized"}'))

    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        import_resolvers.fetch_thingiverse_view_token(PAGE_URL, "session=abc")

    assert caught.value.status_code == 403
    assert caught.value.detail == "Unauthorized " + import_resolvers.THINGIVERSE_COOKIE_REMEDY


def test_the_same_auth_refusal_without_a_cookie_is_not_a_credential_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no cookie sent there is nothing to blame on the credential, so the class stays off."""
    stub(monkeypatch, http_error(403, b'{"error": "Unauthorized"}'))

    with pytest.raises(HTTPException) as caught:
        import_resolvers.fetch_thingiverse_view_token(PAGE_URL, None)

    assert not isinstance(caught.value, import_resolvers.CredentialRefusal)
    assert caught.value.detail.endswith(import_resolvers.THINGIVERSE_COOKIE_REMEDY)


def test_a_thingiverse_url_with_no_thing_id_implicates_no_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    """R16: no thing id means no attempt and no refusal - nothing here is about the cookie."""
    fetch = script(monkeypatch)

    assert import_resolvers.resolve_thingiverse_download_url("https://www.thingiverse.com/explore", "session=abc") is None
    assert fetch.urls == []
