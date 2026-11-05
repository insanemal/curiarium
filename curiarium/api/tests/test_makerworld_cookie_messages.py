"""The MakerWorld download-API refusals, and the remedy they share with Thingiverse.

Both sites answer a dead credential the same way, so both remedies are the same
sentence with one variable renamed. That is the whole point of this module: a
future reader who edits one site's wording, drops the Settings pointer, or lets a
vendor JSON body replace the remedy sees red here. The anonymous column is the
other half - a cookie-less import is not the credential's fault, so it must still
answer ``None`` and keep the caller's ``find_download_url`` rescue.

Nothing opens a socket: ``import_resolvers.safe_urlopen`` is replaced (the name
the module actually calls), and one test installs socket tripwires so "the stub
answered" cannot be a coincidence.
"""

import io
import json
import socket
from email.message import Message
from pathlib import Path
from typing import Callable, Optional
from urllib.error import HTTPError, URLError

import pytest

import import_resolvers

PAGE_URL = "https://makerworld.com/en/models/1-benchy"
INSTANCE_ID = "987654"
MODEL_ID = "1"
MODEL_DOWNLOAD_URL = "https://makerworld.com/api/v1/models/1/download"
INSTANCE_URL = "https://makerworld.com/api/v1/design-service/instance/987654/f3mf?type=download&fileType=3mfstl"
CANDIDATES = (
    "https://makerworld.com/api/v1/models/1",
    "https://makerworld.com/api/v1/models/1/files",
    "https://makerworld.com/api/v1/model/1",
    "https://makerworld.com/api/v1/model/1/files",
)


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
    """A refusal whose body is readable, or (with ``None``) deliberately unreadable.

    ``None`` as the fp makes ``err.read()`` raise, which is how the anonymous
    column proves the return happens before the body is ever touched.
    """
    headers = Message()
    headers["Content-Type"] = "application/json"
    fp = None if body is None else io.BytesIO(body)
    return HTTPError(PAGE_URL, code, f"HTTP {code}", headers, fp)


def http_error_and_body(code: int, body: bytes) -> tuple[HTTPError, io.BytesIO]:
    """A refusal whose body stream the test can inspect afterwards.

    Where ``http_error`` proves a read would have raised, this one proves the read
    never happened: an untouched stream still sits at offset 0.
    """
    headers = Message()
    headers["Content-Type"] = "application/json"
    fp = io.BytesIO(body)
    return HTTPError(PAGE_URL, code, f"HTTP {code}", headers, fp), fp


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
    """Answers each request from a script, by TYPE, recording every request it saw.

    Dispatching on type rather than truthiness is the point: ``{}`` is scripted on
    purpose and is falsy as data. Both the request and its URL are kept because the
    anonymous candidates must be proved to carry no Cookie header.
    """

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.requests = []
        self.urls: list[str] = []

    def __call__(self, request, *, timeout):
        self.requests.append(request)
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


def tripwired_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket in these tests is a bug: the stub is the only fetcher."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("this test must not open a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def call_instance(cookie: Optional[str], identifier: str = INSTANCE_ID) -> Optional[str]:
    """The default-instance download answer, as the resolver asks for it."""
    return import_resolvers.fetch_makerworld_instance_download_url(identifier, PAGE_URL, cookie)


def call_model(cookie: Optional[str], identifier: str = MODEL_ID) -> Optional[str]:
    """The model download answer, as the resolver asks for it."""
    return import_resolvers.fetch_makerworld_model_download_url(identifier, PAGE_URL, None, cookie)


# Both endpoints, the subject each refusal names and the one URL it asks for, so
# no refusal assertion is ever proved on one endpoint alone.
ENDPOINTS = [
    pytest.param(call_instance, "the design's default instance 987654", INSTANCE_URL, id="instance"),
    pytest.param(call_model, "design 1", MODEL_DOWNLOAD_URL, id="model"),
]

# 401 is reported as 403 on purpose: the web client throws UnauthorizedError on a
# 401 before it reads `detail`, so a 401 would hide the remedy.
REFUSAL_CODES = [
    pytest.param(401, 403, id="401-reported-as-403"),
    pytest.param(403, 403, id="403-carried-through"),
    pytest.param(429, 429, id="429-carried-through"),
]


# --- the remedy and the source pin -----------------------------------------


def test_the_two_remedies_are_one_sentence_with_one_word_swapped() -> None:
    """The two sites cannot drift: MakerWorld's is Thingiverse's with the one name changed."""
    assert import_resolvers.MAKERWORLD_COOKIE_REMEDY.replace(
        "MAKERWORLD_COOKIE", "THINGIVERSE_COOKIE"
    ) == import_resolvers.THINGIVERSE_COOKIE_REMEDY
    assert import_resolvers.MAKERWORLD_COOKIE_REMEDY != import_resolvers.THINGIVERSE_COOKIE_REMEDY


def test_both_remedies_are_the_exact_sentence_the_card_promises() -> None:
    """The whole sentence is pinned, so a reworded clause is a failure, not a surprise."""
    assert import_resolvers.THINGIVERSE_COOKIE_REMEDY == (
        "Paste the Cookie header from a signed-in browser session under Settings > Import sources, "
        "or set the THINGIVERSE_COOKIE environment variable on the server and restart the API. "
        "Nothing was changed."
    )
    assert import_resolvers.MAKERWORLD_COOKIE_REMEDY == (
        "Paste the Cookie header from a signed-in browser session under Settings > Import sources, "
        "or set the MAKERWORLD_COOKIE environment variable on the server and restart the API. "
        "Nothing was changed."
    )


@pytest.mark.parametrize(
    "remedy",
    [
        pytest.param(import_resolvers.THINGIVERSE_COOKIE_REMEDY, id="thingiverse"),
        pytest.param(import_resolvers.MAKERWORLD_COOKIE_REMEDY, id="makerworld"),
    ],
)
def test_the_remedy_says_under_settings_exactly_once(remedy: str) -> None:
    """The card says "under Settings"; "in Settings" would send the operator to a page that is not there."""
    assert remedy.count("under Settings > Import sources") == 1
    assert " in Settings > Import sources" not in remedy


def test_the_sentence_lives_in_one_module_once() -> None:
    """The sentence is a template, so it must be written once and rendered twice.

    Production modules only: ``tests/`` is never scanned, which is why this pin
    survives the test module naming the same sentence to prove what it renders.
    """
    frame = "Paste the Cookie header from a signed-in browser session under Settings > Import sources"
    api_dir = Path(import_resolvers.__file__).resolve().parent
    holding = sorted(path.name for path in api_dir.glob("*.py") if frame in path.read_text(encoding="utf-8"))

    assert holding == ["import_resolvers.py"]
    source = (api_dir / "import_resolvers.py").read_text(encoding="utf-8")
    assert source.count(frame) == 1
    assert frame in import_resolvers.THINGIVERSE_COOKIE_REMEDY
    assert frame in import_resolvers.MAKERWORLD_COOKIE_REMEDY


# --- the credentialed refusals ---------------------------------------------


@pytest.mark.parametrize("fetch, subject, url", ENDPOINTS)
@pytest.mark.parametrize("code, expected_status", REFUSAL_CODES)
def test_a_credentialed_download_api_refusal_names_the_variable_and_the_paste(
    monkeypatch: pytest.MonkeyPatch,
    fetch: Callable[[Optional[str]], Optional[str]],
    subject: str,
    url: str,
    code: int,
    expected_status: int,
) -> None:
    """Every dead-cookie answer carries the remedy, the subject, the code and nothing secret.

    401 maps to 403 because the web client throws ``UnauthorizedError`` on a 401
    before it reads ``detail``, so a 401 would hide the remedy the operator needs.
    """
    stub_fetch = stub(monkeypatch, http_error(code, b'{"error": "Unauthorized"}'))
    tripwired_sockets(monkeypatch)

    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        fetch("mw_session=abc")

    detail = caught.value.detail
    assert isinstance(caught.value, import_resolvers.CredentialRefusal)
    assert caught.value.status_code == expected_status
    assert detail.endswith(import_resolvers.MAKERWORLD_COOKIE_REMEDY)
    assert "MAKERWORLD_COOKIE" in detail
    assert "under Settings > Import sources" in detail
    assert "Nothing was changed." in detail
    assert subject in detail
    assert f"(HTTP {code})" in detail
    assert "only a rate-limit workaround for free models" in detail, "the card's paid promise, not a claim a cookie can help there"
    assert "paid or account-gated files are never fetched" in detail
    assert detail.index("paid or account-gated files are never fetched") < detail.index("Paste the Cookie header"), (
        "the hedge is a qualification, so it must not read as an afterthought to the remedy"
    )
    assert "direct download link" not in detail
    assert "mw_session=abc" not in detail
    assert stub_fetch.request.full_url == url


@pytest.mark.parametrize("fetch, _subject, _url", ENDPOINTS)
def test_a_vendor_body_is_quoted_and_the_remedy_still_follows(
    monkeypatch: pytest.MonkeyPatch,
    fetch: Callable[[Optional[str]], Optional[str]],
    _subject: str,
    _url: str,
) -> None:
    """The vendor's word is quoted, but it never displaces the remedy that follows."""
    stub(monkeypatch, http_error(403, b'{"error": "Unauthorized"}'))

    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        fetch("mw_session=abc")

    detail = caught.value.detail
    assert "Unauthorized" in detail
    assert detail.endswith(import_resolvers.MAKERWORLD_COOKIE_REMEDY)


@pytest.mark.parametrize("fetch, _subject, _url", ENDPOINTS)
def test_an_unreadable_error_body_still_names_the_block_and_the_remedy(
    monkeypatch: pytest.MonkeyPatch,
    fetch: Callable[[Optional[str]], Optional[str]],
    _subject: str,
    _url: str,
) -> None:
    """A body this server cannot read is not proof the cookie is dead, but the remedy is the same step."""
    stub(monkeypatch, http_error(403, None))

    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        fetch("mw_session=abc")

    detail = caught.value.detail
    assert "MakerWorld blocked the request." in detail
    assert ".." not in detail, "the fallback carries no period: the lead supplies the only one"
    assert detail.endswith(import_resolvers.MAKERWORLD_COOKIE_REMEDY)


# --- the anonymous column ---------------------------------------------------


@pytest.mark.parametrize("fetch, _subject, _url", ENDPOINTS)
@pytest.mark.parametrize("code, _expected_status", REFUSAL_CODES)
def test_the_same_refusal_without_a_cookie_answers_none_before_reading_the_body(
    monkeypatch: pytest.MonkeyPatch,
    fetch: Callable[[Optional[str]], Optional[str]],
    _subject: str,
    _url: str,
    code: int,
    _expected_status: int,
) -> None:
    """The proof that the no-cookie return precedes ``err.read()``.

    Two halves, because either alone is vacuous. The unreadable body makes a read
    raise (swallowed by the ``except Exception`` around it, so it proves nothing on
    its own), and the readable body's stream must still sit at offset 0 afterwards,
    which is only true when nothing consumed it. That is what keeps a cookie-less
    MakerWorld import on today's ``find_download_url`` rescue and the generic 400.
    """
    stub_fetch = stub(monkeypatch, http_error(code, None))

    assert fetch(None) is None
    assert stub_fetch.calls == 1, "the request is still made without a cookie"

    error, fp = http_error_and_body(code, b'{"error": "Unauthorized"}')
    stub(monkeypatch, error)

    assert fetch(None) is None
    assert fp.tell() == 0, "the no-cookie branch must answer before the body is read"


@pytest.mark.parametrize("fetch, _subject, _url", ENDPOINTS)
def test_an_anonymous_429_never_raises(
    monkeypatch: pytest.MonkeyPatch,
    fetch: Callable[[Optional[str]], Optional[str]],
    _subject: str,
    _url: str,
) -> None:
    """Raising here would violate ``_refuse_credential``'s doctrine and forfeit the caller's rescue."""
    stub(monkeypatch, http_error(429, b'{"error": "Too Many Requests"}'))

    assert fetch(None) is None


# --- what a refusal must not claim -----------------------------------------


@pytest.mark.parametrize("fetch, _subject, _url", ENDPOINTS)
@pytest.mark.parametrize("code", [pytest.param(404, id="not-found"), pytest.param(500, id="server-error")])
def test_a_status_outside_the_refusals_answers_none(
    monkeypatch: pytest.MonkeyPatch,
    fetch: Callable[[Optional[str]], Optional[str]],
    _subject: str,
    _url: str,
    code: int,
) -> None:
    """404 and 500 say nothing about the cookie, so the caller decides what they mean."""
    stub(monkeypatch, http_error(code, None))

    assert fetch("mw_session=abc") is None


@pytest.mark.parametrize("fetch, _subject, _url", ENDPOINTS)
def test_a_transport_failure_is_never_called_a_dead_cookie(
    monkeypatch: pytest.MonkeyPatch,
    fetch: Callable[[Optional[str]], Optional[str]],
    _subject: str,
    _url: str,
) -> None:
    """A server that could not be reached says nothing about the cookie: today's ``None`` stands."""
    script(monkeypatch, URLError("unreachable"))

    assert fetch("mw_session=abc") is None


@pytest.mark.parametrize("fetch, _subject, url", ENDPOINTS)
def test_an_unreadable_200_body_is_not_a_credential_verdict(
    monkeypatch: pytest.MonkeyPatch,
    fetch: Callable[[Optional[str]], Optional[str]],
    _subject: str,
    url: str,
) -> None:
    """A challenge page is an unreadable answer, not a dead credential: answer ``None``."""
    fetch_stub = script(monkeypatch, StubResponse(b"<html>challenge</html>", "text/html"))

    assert fetch("mw_session=abc") is None
    assert fetch_stub.urls == [url]


# --- the resolver's own shapes ---------------------------------------------


def test_a_resolution_with_no_derivable_id_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """No model id and no instance id means no request, so no refusal can be about the cookie."""
    fetch_stub = script(monkeypatch)

    assert (
        import_resolvers.resolve_makerworld_download_url(
            "<html></html>", "https://makerworld.com/en/explore", "mw_session=abc"
        )
        is None
    )
    assert fetch_stub.urls == []


INSTANCE_PAGE = """
<html><head>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"design":{"id":4321,"defaultInstanceId":987654}}}}
</script>
</head></html>
"""


def test_an_instance_refusal_stops_before_the_model_endpoint_and_the_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The instance branch runs first, so its refusal is the only request the resolver makes.

    The script holds exactly one answer, so a fall-through to the model endpoint or
    to the four anonymous candidates would raise here instead of answering.
    """
    fetch_stub = script(monkeypatch, http_error(403, b'{"error": "Unauthorized"}'))

    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        import_resolvers.resolve_makerworld_download_url(INSTANCE_PAGE, PAGE_URL, "mw_session=abc")

    assert "the design's default instance 987654" in caught.value.detail
    assert caught.value.detail.endswith(import_resolvers.MAKERWORLD_COOKIE_REMEDY)
    assert fetch_stub.urls == [INSTANCE_URL]


def test_a_200_with_no_download_url_answers_none_so_the_caller_keeps_its_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole resolver, not just the two fetch helpers: no URL means today's generic 400.

    The four candidates are the cookie-free ones, so every one of their requests
    must arrive with no Cookie header at all.
    """
    fetch_stub = script(
        monkeypatch,
        StubResponse(b"{}", "application/json"),
        StubResponse(b"{}", "application/json"),
        StubResponse(b"{}", "application/json"),
        StubResponse(b"{}", "application/json"),
        StubResponse(b"{}", "application/json"),
    )

    assert import_resolvers.resolve_makerworld_download_url("<html></html>", PAGE_URL, "mw_session=abc") is None
    assert fetch_stub.urls == [MODEL_DOWNLOAD_URL, *CANDIDATES]
    for request in fetch_stub.requests[1:]:
        assert request.get_header("Cookie") is None


# --- what must never leak or bury the remedy -------------------------------


@pytest.mark.parametrize("fetch, _subject, _url", ENDPOINTS)
def test_the_cookie_value_never_reaches_the_detail(
    monkeypatch: pytest.MonkeyPatch,
    fetch: Callable[[Optional[str]], Optional[str]],
    _subject: str,
    _url: str,
) -> None:
    """The remedy may NAME the variable; echoing the secret into a UI toast would leak it."""
    stub_fetch = stub(monkeypatch, http_error(403, b""))

    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        fetch("mw_session=supersecret")

    assert "supersecret" not in caught.value.detail
    assert stub_fetch.request.get_header("Cookie") == "mw_session=supersecret", "otherwise this passes vacuously"


@pytest.mark.parametrize("fetch, _subject, _url", ENDPOINTS)
def test_a_huge_vendor_body_cannot_bury_the_remedy(
    monkeypatch: pytest.MonkeyPatch,
    fetch: Callable[[Optional[str]], Optional[str]],
    _subject: str,
    _url: str,
) -> None:
    """The vendor text is bounded to 300 characters so the remedy is never pushed off the screen."""
    stub(monkeypatch, http_error(403, json.dumps({"error": "x" * 1000}).encode()))

    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        fetch("mw_session=abc")

    detail = caught.value.detail
    assert "x" * 300 in detail
    assert "x" * 301 not in detail
    assert detail.endswith(import_resolvers.MAKERWORLD_COOKIE_REMEDY)


@pytest.mark.parametrize("fetch", [pytest.param(call_instance, id="instance"), pytest.param(call_model, id="model")])
def test_the_id_in_the_message_is_capped_at_64_characters(
    monkeypatch: pytest.MonkeyPatch,
    fetch: Callable[[Optional[str], str], Optional[str]],
) -> None:
    """A page-supplied id is quoted in a message, so it is bounded; the request keeps the raw one."""
    long_id = "1" * 100
    stub_fetch = stub(monkeypatch, http_error(403, b'{"error": "Unauthorized"}'))

    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        fetch("mw_session=abc", long_id)

    detail = caught.value.detail
    assert long_id not in detail
    assert long_id[:64] in detail
    assert long_id in stub_fetch.request.full_url, "the id is bounded in the message only; the request keeps the raw one"


BOUND_BODIES = [
    pytest.param("Forbidden.", "Forbidden.", id="one-period"),
    pytest.param("Forbidden..", None, id="two-periods"),
    pytest.param("x" * 299 + ".", "x" * 299, id="bound-lands-on-period"),
    pytest.param("x" * 298 + ".." + "y" * 40, None, id="period-run-inside-the-bound"),
    pytest.param("...", "MakerWorld blocked the request.", id="nothing-but-periods-falls-back"),
    pytest.param("", "MakerWorld blocked the request.", id="blank-error-field-is-not-a-message"),
]


@pytest.mark.parametrize("fetch, _subject, _url", ENDPOINTS)
@pytest.mark.parametrize("message, fragment", BOUND_BODIES)
def test_the_bound_comes_before_the_period_strip(
    monkeypatch: pytest.MonkeyPatch,
    fetch: Callable[[Optional[str]], Optional[str]],
    _subject: str,
    _url: str,
    message: str,
    fragment: Optional[str],
) -> None:
    """The bound is taken on the raw vendor string before periods are stripped from it.

    The "period-run-inside-the-bound" arm is the one that fails if the period
    strip happens BEFORE the bound: the trailing ``y`` run means stripping does
    nothing first, so only binding first removes the ``..`` the truncation exposed.
    """
    stub(monkeypatch, http_error(403, json.dumps({"error": message}).encode()))

    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        fetch("mw_session=abc")

    detail = caught.value.detail
    assert ".." not in detail
    if fragment is not None:
        assert fragment in detail
    assert detail.endswith(import_resolvers.MAKERWORLD_COOKIE_REMEDY)
