"""The two Thingiverse refusals an import can hit, and the remedy they carry.

Both messages name the *same* two surfaces, because the import path really has
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
from urllib.error import HTTPError

import pytest
from fastapi import HTTPException

import import_resolvers

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
