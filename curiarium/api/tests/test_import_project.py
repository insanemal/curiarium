"""Mf-56z9: an import URL that lands in a project, or adds to one.

Three invariants are pinned here, each because breaking it is silent:

* a FREE page always parses as `gated=False` - a licence key or a price string is
  not paid-ness, and a false positive would refuse a free import;
* a gated import stores the project and its page details but downloads nothing
  and issues no second outbound request;
* a generic "no downloadable file" failure stays exactly today's error and
  creates no project, so the gated path cannot swallow ordinary failures.

The database is shared for the whole session, so every row a test creates is
deleted at teardown. No test opens a socket: the seam that would fetch is
monkeypatched on `main`, which is the patch surface the routes resolve at call
time.
"""

import io
import json
import socket
import struct
import zlib
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import asset_file_service
import asset_service
import audit
import bundle_service
import db
import import_resolvers
import import_service
import library_scan
import main
import permissions
import project_import
import project_service
import settings_service
from db import engine
from import_metadata import PageMetadata, parse_page_metadata
from import_policy import GATED_IMPORT_DETAIL
from import_service import ImportAttempt, ImportPage, ZipAttempt
from migrations import run_migrations
from models import Asset, AssetFile, Folder, Project, ProjectAsset
from schemas import ImportProjectRequest
from url_utils import canonical_url, source_key

STL_BYTES = b"solid cube\nendsolid cube\n"

# Inline pages. The `__NEXT_DATA__` shape mirrors what the three sites ship, and
# `og:title`/`meta[name=author]`/`rel=license` are the generic fallbacks.
MAKERWORLD_FREE_PAGE = """
<html><head>
<title>MakerWorld</title>
<meta property="og:title" content="Benchy Boat">
<meta name="author" content="Generic Author">
<link rel="license" href="https://creativecommons.org/licenses/by/4.0/">
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"design":{"name":"Benchy Boat","creator":{"name":"Overlay Creator"},"licenceType":"CC BY","price":0}}}}
</script>
</head><body><a rel="license" href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a></body></html>
"""

MAKERWORLD_PAID_PAGE = """
<html><head>
<meta property="og:title" content="Paid Vase">
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"design":{"name":"Paid Vase","is_purchasable":true}}}}
</script>
</head></html>
"""

MAKERWORLD_URL = "https://makerworld.com/en/models/1-benchy"
MAKERWORLD_MODEL_DOWNLOAD_URL = "https://makerworld.com/api/v1/models/1/download"
MAKERWORLD_ANONYMOUS_CANDIDATES = (
    "https://makerworld.com/api/v1/models/1",
    "https://makerworld.com/api/v1/models/1/files",
    "https://makerworld.com/api/v1/model/1",
    "https://makerworld.com/api/v1/model/1/files",
)

# A free page that names its file, so a fall-through past the refusal has
# somewhere to go: find_download_url finds this href and re-enters the fetch,
# which the scripted page fetch answers with an AssertionError.
MAKERWORLD_PAGE_WITH_A_FILE_LINK = """
<html><head>
<title>MakerWorld</title>
<meta property="og:title" content="Benchy Boat">
<meta name="author" content="Generic Author">
<link rel="license" href="https://creativecommons.org/licenses/by/4.0/">
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"design":{"name":"Benchy Boat","creator":{"name":"Overlay Creator"},"licenceType":"CC BY","price":0}}}}
</script>
</head><body><a href="https://cdn.makerworld.com/assets/cube.stl">cube.stl</a></body></html>
"""

PRINTABLES_FREE_PAGE = """
<html><head>
<meta property="og:title" content="Free Planter">
<meta name="author" content="Generic Planter Author">
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"model":{"name":"Free Planter","designer":{"displayName":"Planter Maker"},"licenceType":"CC-BY-NC"}}}}
</script>
</head></html>
"""

PRINTABLES_PAID_PAGE = """
<html><head>
<meta property="og:title" content="Premium Planter">
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"model":{"name":"Premium Planter","is_premium":true}}}}
</script>
</head></html>
"""

THINGIVERSE_FREE_PAGE = """
<html><head>
<meta property="og:title" content="Free Thing">
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"thing":{"name":"Free Thing","creator":{"name":"Thing Maker"}}}}}
</script>
</head></html>
"""

THINGIVERSE_PAID_PAGE = """
<html><head>
<meta property="og:title" content="Paid Thing">
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"thing":{"name":"Paid Thing","requires_purchase":true}}}}
</script>
</head></html>
"""

# A free page that names its file, so a fall-through past the refusal has
# somewhere to go: find_download_url finds this href and re-enters the fetch,
# which the scripted page fetch answers with an AssertionError.
THINGIVERSE_PAGE_WITH_A_FILE_LINK = """
<html><head>
<meta property="og:title" content="Free Thing">
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"thing":{"name":"Free Thing","creator":{"name":"Thing Maker"}}}}}
</script>
</head><body><a href="https://cdn.thingiverse.com/assets/cube.stl">cube.stl</a></body></html>
"""

THINGIVERSE_PAGE_WITHOUT_A_THING_ID = """
<html><head><title>Explore</title></head><body>nothing to download here</body></html>
"""

THINGIVERSE_URL = "https://www.thingiverse.com/thing:5390184"
THINGIVERSE_AUTH_VIEW_URL = "https://www.thingiverse.com/api/v2/auth/view"
THINGIVERSE_FILES_URL = "https://www.thingiverse.com/api/v2/things/5390184/files"


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point storage at a temp root and undo every row this test made."""
    storage = tmp_path / "storage"
    (storage / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(db, "STORAGE", storage)
    monkeypatch.setattr(db, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(main, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(library_scan, "STORAGE", storage)
    monkeypatch.setattr(asset_service, "STORAGE", storage)
    monkeypatch.setattr(asset_service, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(asset_file_service, "STORAGE", storage)
    run_migrations(engine)
    template_before = settings_service.get_storage_template()
    with Session(engine) as session:
        projects_before = {project.id for project in session.exec(select(Project))}
        folders_before = {folder.id for folder in session.exec(select(Folder))}
        assets_before = {asset.id for asset in session.exec(select(Asset))}
    yield storage
    settings_service.set_storage_template(template_before)
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


def auth_header(client: TestClient) -> dict:
    """Bearer token, or empty when the app is running with auth disabled."""
    health = client.get("/health").json()
    if not health.get("auth_required"):
        return {}
    token = client.post("/login", json={"username": "admin", "password": "super-secret"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def make_asset(filename: str = "cube.stl") -> Asset:
    """A real asset row with real bytes, as the import seam would have produced."""
    asset = asset_service.create_asset_record(filename, "model/stl", None, None, [], None)
    dest = asset_service.managed_asset_path(asset)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(STL_BYTES)
    finalized = asset_service.finalize_asset_record(asset.id, len(STL_BYTES), "model/stl")
    assert finalized is not None
    return finalized


def page(url: str, html: str) -> ImportPage:
    """One page both as the fetcher reports it and as the parser sees it."""
    return ImportPage(url=url, html=html)


def project_row(project_id: str) -> Project:
    with Session(engine) as session:
        row = session.get(Project, project_id)
        assert row is not None
        return row


def member_ids(project_id: str) -> set[str]:
    with Session(engine) as session:
        return set(session.exec(select(ProjectAsset.asset_id).where(ProjectAsset.project_id == project_id)).all())


def project_count() -> int:
    with Session(engine) as session:
        return len(list(session.exec(select(Project))))


def stub_attempt(monkeypatch: pytest.MonkeyPatch, attempt: ImportAttempt | ZipAttempt) -> list[str]:
    """Answer every attempt seam with `attempt`, recording each call's URL."""
    calls: list[str] = []

    def resolve(url: str, body, *, origin=None) -> ImportAttempt | ZipAttempt:
        calls.append(url)
        return attempt

    monkeypatch.setattr(main, "import_asset_attempt", resolve)
    monkeypatch.setattr(main, "download_zip_attempt", resolve)
    return calls


class ScriptedResponse:
    """A 200 the real fetcher can consume: headers, geturl, a bounded read, close."""

    def __init__(self, body: bytes, content_type: str, url: str) -> None:
        self.body = body
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]

    def geturl(self) -> str:
        return self.url

    def close(self) -> None:
        return None

    def __enter__(self) -> "ScriptedResponse":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def script_page(monkeypatch: pytest.MonkeyPatch, html: str, url: str) -> list[str]:
    """Answer the page fetch exactly once; a second call is an unscripted fall-through."""
    calls: list[str] = []

    def fetch(request, *, timeout):
        calls.append(request.full_url)
        if len(calls) > 1:
            raise AssertionError(f"a refused import must not fetch again: {request.full_url}")
        return ScriptedResponse(html.encode(), "text/html; charset=utf-8", url)

    monkeypatch.setattr(import_service, "safe_urlopen", fetch)
    return calls


def script_resolver(monkeypatch: pytest.MonkeyPatch, *outcomes: object) -> list[str]:
    """Answer a host resolver's own fetch, recording each URL it asked for."""
    urls: list[str] = []
    queue = list(outcomes)

    def fetch(request, *, timeout):
        urls.append(request.full_url)
        if not queue:
            raise AssertionError(f"unscripted resolver fetch of {request.full_url}")
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(import_resolvers, "safe_urlopen", fetch)
    return urls


def resolver_http_error(code: int, body: bytes) -> HTTPError:
    """A vendor refusal with a readable JSON body, so parse_json_error_message runs.

    The URL it carries is the error's filename only; a door test asserts the URL the
    resolver actually asked for, never this one.
    """
    headers = Message()
    headers["Content-Type"] = "application/json"
    return HTTPError(THINGIVERSE_URL, code, f"HTTP {code}", headers, io.BytesIO(body))


def pin_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve any host to a public address so a door test needs no resolver.

    ``_resolve_import`` validates the URL before it fetches and that validation is
    a real ``getaddrinfo``; the fetch itself is scripted, so nothing connects. A
    public address keeps the SSRF guard's own decision out of these tests.
    """

    def fake_getaddrinfo(host: str, port, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


# --- page metadata ----------------------------------------------------------


def test_free_makerworld_page_parses_its_details_and_is_not_gated() -> None:
    """A free page must never parse as gated: that would refuse the import."""
    metadata = parse_page_metadata(MAKERWORLD_FREE_PAGE, "https://makerworld.com/en/models/1-benchy")

    assert metadata.gated is False
    assert metadata.gated_reason is None
    assert metadata.title == "Benchy Boat"
    assert metadata.creator == "Overlay Creator", "the __NEXT_DATA__ overlay wins over the generic meta"
    assert metadata.licence == "CC BY 4.0"
    assert metadata.licence_url == "https://creativecommons.org/licenses/by/4.0/"


def test_paid_makerworld_page_names_its_flag() -> None:
    metadata = parse_page_metadata(MAKERWORLD_PAID_PAGE, "https://makerworld.com/en/models/2-paid")

    assert metadata.gated is True
    assert "is_purchasable" in (metadata.gated_reason or "")


def test_free_printables_page_parses_its_details_and_is_not_gated() -> None:
    metadata = parse_page_metadata(PRINTABLES_FREE_PAGE, "https://www.printables.com/model/3-planter")

    assert metadata.gated is False
    assert metadata.title == "Free Planter"
    assert metadata.creator == "Planter Maker"


def test_paid_printables_page_names_its_flag() -> None:
    metadata = parse_page_metadata(PRINTABLES_PAID_PAGE, "https://www.printables.com/model/4-planter")

    assert metadata.gated is True
    assert "is_premium" in (metadata.gated_reason or "")


@pytest.mark.parametrize(
    "flag,canonical",
    [("isPremium", "is_premium"), ("isPurchasable", "is_purchasable")],
)
def test_a_camel_case_gated_flag_is_gated_with_its_canonical_reason(flag: str, canonical: str) -> None:
    # The real __NEXT_DATA__ payload is camelCase ("isPremium"), which the
    # snake_case fixtures above never exercise. The stored reason stays
    # snake_case, exactly as the paired snake_case test yields it.
    next_data = {"props": {"pageProps": {"model": {"name": "CamelCase Model", flag: True}}}}
    html = (
        '<html><head><meta property="og:title" content="CamelCase Model">'
        '<script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(next_data)}"
        "</script></head></html>"
    )
    metadata = parse_page_metadata(html, "https://www.printables.com/model/8-camelcase")

    assert metadata.gated is True
    assert canonical in (metadata.gated_reason or "")


def test_free_thingiverse_page_parses_its_details_and_is_not_gated() -> None:
    metadata = parse_page_metadata(THINGIVERSE_FREE_PAGE, "https://www.thingiverse.com/thing:5")

    assert metadata.gated is False
    assert metadata.title == "Free Thing"
    assert metadata.creator == "Thing Maker"


def test_paid_thingiverse_page_names_its_flag() -> None:
    metadata = parse_page_metadata(THINGIVERSE_PAID_PAGE, "https://www.thingiverse.com/thing:6")

    assert metadata.gated is True
    assert "requires_purchase" in (metadata.gated_reason or "")


def test_a_price_or_licence_key_never_makes_a_page_gated() -> None:
    """The regression this exists to prevent: a free page refused as paid."""
    html = """
    <html><head>
    <script id="__NEXT_DATA__" type="application/json">
    {"props":{"pageProps":{"design":{"name":"Free","licenceType":"CC BY","price":{"amount":0}}}}}
    </script>
    <script type="application/ld+json">
    {"@type":"Product","name":"Free","offers":{"price":"9.99"},"license":"CC BY 4.0"}
    </script>
    </head></html>
    """
    metadata = parse_page_metadata(html, "https://makerworld.com/en/models/7-free")

    assert metadata.gated is False
    assert metadata.gated_reason is None


def test_json_ld_supplies_title_creator_and_a_licence_url() -> None:
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@graph":[{"@type":"CreativeWork","name":"LD Model","author":{"name":"LD Author"},
    "license":"https://example.com/licence"}]}
    </script>
    </head></html>
    """
    metadata = parse_page_metadata(html, "https://example.com/model")

    assert metadata.title == "LD Model"
    assert metadata.creator == "LD Author"
    assert metadata.licence_url == "https://example.com/licence"
    assert metadata.licence is None


def test_an_empty_page_is_metadata_only_and_not_gated() -> None:
    assert parse_page_metadata("", "https://example.com") == PageMetadata()


# --- source keys ------------------------------------------------------------


def test_source_key_survives_a_slug_rename_for_every_supported_host() -> None:
    assert source_key("https://makerworld.com/en/models/123-old-name") == source_key(
        "https://makerworld.com/en/models/123-new-name"
    ) == "makerworld:123"
    assert source_key("https://www.printables.com/model/123-old-name") == source_key(
        "https://www.printables.com/model/123-new-name"
    ) == "printables:123"
    assert source_key("https://www.thingiverse.com/thing:123-old-name") == source_key(
        "https://www.thingiverse.com/thing:123-new-name"
    ) == "thingiverse:123"


def test_canonical_url_collapses_the_noise_but_stays_displayable() -> None:
    noisy = "https://www.MakerWorld.com:443/en/models/123-cube/?utm_source=news&b=2&a=1&fbclid=x#gallery"

    assert canonical_url(noisy) == "https://makerworld.com/en/models/123-cube?a=1&b=2"


def test_source_keys_are_total_on_garbage() -> None:
    assert canonical_url("") == ""
    assert source_key("") == ""
    for raw in ("not a url", "http://host:badport/x", "https://[::1/x", "   "):
        # Total: these must answer, not raise - one garbage stored row must not
        # break an unrelated import.
        assert isinstance(source_key(raw), str)
        assert isinstance(canonical_url(raw), str)


def test_canonical_url_adds_a_scheme_to_a_bare_host() -> None:
    assert canonical_url("makerworld.com/en/models/1-x") == "https://makerworld.com/en/models/1-x"


# --- gated imports ----------------------------------------------------------


def test_gated_import_stores_metadata_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gated page creates the project, records `_files`, downloads nothing."""
    target = page("https://makerworld.com/en/models/2-paid", MAKERWORLD_PAID_PAGE)
    calls = stub_attempt(monkeypatch, ImportAttempt(page=target, error=HTTPException(status_code=400, detail="no file")))

    # Belt and braces: the attempt is stubbed, so any real fetch is a test bug.
    def no_network(*_args, **_kwargs):
        raise AssertionError("a gated import must not open a connection")

    monkeypatch.setattr(import_service, "safe_urlopen", no_network)

    outcome = project_import.import_into_project(
        "https://makerworld.com/en/models/2-paid", ImportProjectRequest(url="https://makerworld.com/en/models/2-paid")
    )

    assert calls == ["https://makerworld.com/en/models/2-paid"], "exactly one attempt, no second outbound call"
    assert outcome.gated is True
    assert outcome.message == GATED_IMPORT_DETAIL
    assert outcome.created is True
    assert outcome.assets == [] and outcome.added == []
    row = project_row(outcome.project_id)
    assert row.origin == "imported"
    assert json.loads(row.metadata_json) == {"_files": "user-supplied"}
    assert row.name == "Paid Vase"
    assert member_ids(outcome.project_id) == set()


def test_a_403_without_a_read_page_keeps_the_original_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """No page, no metadata: a 403 there may be bot protection, not a paywall.

    Nothing was read, so there is nothing to import and nothing that proves the
    model is gated; the import answers exactly what it answered before.
    """
    stub_attempt(monkeypatch, ImportAttempt(error=HTTPException(status_code=403, detail="forbidden")))
    before = project_count()

    with pytest.raises(HTTPException) as excinfo:
        project_import.import_into_project(
            "https://example.com/private", ImportProjectRequest(url="https://example.com/private")
        )

    assert excinfo.value.status_code == 403
    assert project_count() == before, "an unread page must not create a project"


def test_a_403_on_a_read_page_is_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The site refusing the files while serving the page is the real detector."""
    target = page("https://www.printables.com/model/9-paid-vase", PRINTABLES_FREE_PAGE)
    stub_attempt(monkeypatch, ImportAttempt(page=target, error=HTTPException(status_code=403, detail="forbidden")))

    outcome = project_import.import_into_project(
        "https://www.printables.com/model/9-paid-vase",
        ImportProjectRequest(url="https://www.printables.com/model/9-paid-vase"),
    )

    assert outcome.gated is True
    row = project_row(outcome.project_id)
    assert json.loads(row.metadata_json) == {"_files": "user-supplied"}
    assert row.source_url == "https://printables.com/model/9-paid-vase"


def test_a_stale_explicit_project_is_404ed_before_anything_is_downloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad target must not leave the just-imported asset orphaned in the library."""
    calls = stub_attempt(monkeypatch, ImportAttempt(asset=make_asset(), page=None))

    with pytest.raises(HTTPException) as excinfo:
        project_import.import_into_project(
            "https://example.com/cube.stl",
            ImportProjectRequest(url="https://example.com/cube.stl", project_id="nope"),
        )

    assert excinfo.value.status_code == 404
    assert calls == [], "the target is proved to exist before the fetch, not after"


def test_a_gated_import_does_not_mark_a_project_that_already_has_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_files` says "this project has no files", so it must not land on one that does."""
    target = page("https://makerworld.com/en/models/7-paid", MAKERWORLD_PAID_PAGE)
    stub_attempt(monkeypatch, ImportAttempt(page=target, error=HTTPException(status_code=403, detail="no file")))
    project_id, _created = project_service.find_or_create_import_project(
        project_id=None,
        suggested_name="Already has files",
        source_url="https://makerworld.com/en/models/7-paid",
    )
    project_service.add_assets(project_id, [make_asset("member.stl").id])

    outcome = project_import.import_into_project(
        "https://makerworld.com/en/models/7-paid",
        ImportProjectRequest(url="https://makerworld.com/en/models/7-paid", project_id=project_id),
    )

    assert outcome.gated is True
    assert json.loads(project_row(project_id).metadata_json) == {}


def test_a_free_pages_generic_failure_stays_the_same_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 400 on a free page must re-raise and create no project (no regression)."""
    target = page("https://makerworld.com/en/models/1-benchy", MAKERWORLD_FREE_PAGE)
    stub_attempt(monkeypatch, ImportAttempt(page=target, error=HTTPException(status_code=400, detail="No downloadable model file found")))
    before = project_count()

    with pytest.raises(HTTPException) as excinfo:
        project_import.import_into_project(
            "https://makerworld.com/en/models/1-benchy",
            ImportProjectRequest(url="https://makerworld.com/en/models/1-benchy"),
        )

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "No downloadable model file found"
    assert project_count() == before, "a failed import must not leave an empty project"


# --- happy paths ------------------------------------------------------------


def test_single_file_import_creates_the_project_and_adds_the_asset(monkeypatch: pytest.MonkeyPatch) -> None:
    asset = make_asset()
    target = page("https://makerworld.com/en/models/321-cube?utm_source=news", MAKERWORLD_FREE_PAGE)
    stub_attempt(monkeypatch, ImportAttempt(asset=asset, page=target))

    outcome = project_import.import_into_project(
        "https://makerworld.com/en/models/321-cube?utm_source=news",
        ImportProjectRequest(url="https://makerworld.com/en/models/321-cube?utm_source=news"),
    )

    assert outcome.created is True
    assert outcome.gated is False
    assert outcome.added == [asset.id] and outcome.failed == []
    row = project_row(outcome.project_id)
    assert row.origin == "imported"
    assert row.name == "Benchy Boat"
    assert row.creator == "Overlay Creator"
    assert row.licence == "CC BY 4.0"
    assert row.source_url == "https://makerworld.com/en/models/321-cube", "tracking is dropped from the stored URL"
    assert member_ids(outcome.project_id) == {asset.id}


def test_importing_the_same_page_again_reuses_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dedupe compares the source key, not the URL text: a renamed slug reuses it."""
    first_asset = make_asset("first.stl")
    stub_attempt(
        monkeypatch,
        ImportAttempt(asset=first_asset, page=page("https://makerworld.com/en/models/321-cube", MAKERWORLD_FREE_PAGE)),
    )
    first = project_import.import_into_project(
        "https://makerworld.com/en/models/321-cube", ImportProjectRequest(url="https://makerworld.com/en/models/321-cube")
    )
    before = project_count()

    second_asset = make_asset("second.stl")
    stub_attempt(
        monkeypatch,
        ImportAttempt(asset=second_asset, page=page("https://makerworld.com/en/models/321-renamed", MAKERWORLD_FREE_PAGE)),
    )
    second = project_import.import_into_project(
        "https://makerworld.com/en/models/321-renamed",
        ImportProjectRequest(url="https://makerworld.com/en/models/321-renamed"),
    )

    assert second.created is False
    assert second.project_id == first.project_id
    assert project_count() == before, "no duplicate project for the same model id"
    assert member_ids(second.project_id) == {first_asset.id, second_asset.id}


def test_an_explicit_project_wins_and_its_creator_is_not_overwritten(monkeypatch: pytest.MonkeyPatch) -> None:
    with Session(engine) as session:
        chosen = project_service.create_project(
            session,
            "My Chosen Project",
            None,
            None,
            creator="User Creator",
            source_url="https://elsewhere.example/thing",
        )
    asset = make_asset()
    stub_attempt(
        monkeypatch,
        ImportAttempt(asset=asset, page=page("https://makerworld.com/en/models/9-other", MAKERWORLD_FREE_PAGE)),
    )

    outcome = project_import.import_into_project(
        "https://makerworld.com/en/models/9-other",
        ImportProjectRequest(url="https://makerworld.com/en/models/9-other", project_id=chosen.id),
    )

    assert outcome.project_id == chosen.id
    assert outcome.created is False
    row = project_row(chosen.id)
    assert row.creator == "User Creator", "an existing non-empty creator is never overwritten"
    assert row.name == "My Chosen Project"
    assert row.source_url == "https://elsewhere.example/thing", "the user's source is kept too"
    assert member_ids(chosen.id) == {asset.id}


def test_project_name_is_taken_from_the_request_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    asset = make_asset()
    stub_attempt(
        monkeypatch,
        ImportAttempt(asset=asset, page=page("https://makerworld.com/en/models/55-cube", MAKERWORLD_FREE_PAGE)),
    )

    outcome = project_import.import_into_project(
        "https://makerworld.com/en/models/55-cube",
        ImportProjectRequest(url="https://makerworld.com/en/models/55-cube", project_name="Chosen Name"),
    )

    assert project_row(outcome.project_id).name == "Chosen Name"


def test_a_title_does_not_name_the_project_for_a_single_file_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """The title names the asset on the single-file door, not the project."""
    target = page("https://makerworld.com/en/models/321-cube", MAKERWORLD_FREE_PAGE)
    stub_attempt(monkeypatch, ImportAttempt(asset=make_asset(), page=target))

    outcome = project_import.import_into_project(
        "https://makerworld.com/en/models/321-cube",
        ImportProjectRequest(url="https://makerworld.com/en/models/321-cube", title="Asset title"),
    )

    # The asset itself is named on the seam path this test stubs out, so this
    # pins only the project name.
    assert project_row(outcome.project_id).name == "Benchy Boat"


# --- zip selection ----------------------------------------------------------


def make_zip(tmp_path: Path, names: list[str], *, payloads: dict[str, bytes] | None = None) -> Path:
    path = tmp_path / "download.zip"
    with ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, (payloads or {}).get(name, STL_BYTES))
    return path


def test_selected_zip_entries_become_members(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    zip_path = make_zip(tmp_path, ["a.stl", "b.stl", "readme.txt"])
    target = page("https://www.printables.com/model/77-planter", PRINTABLES_FREE_PAGE)
    stub_attempt(monkeypatch, ZipAttempt(tmp_path=zip_path, filename="download.zip", mime="application/zip", page=target))

    outcome = project_import.import_into_project(
        "https://www.printables.com/model/77-planter",
        ImportProjectRequest(url="https://www.printables.com/model/77-planter", entries=["a.stl", "b.stl"]),
    )

    assert len(outcome.added) == 2
    assert len(outcome.assets) == 2
    assert outcome.failed == []
    assert member_ids(outcome.project_id) == {asset.id for asset in outcome.assets}
    assert not zip_path.exists(), "the downloaded temp file is the import's to delete"


def test_a_zip_import_title_names_the_project_not_the_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The archive title names the project; every entry keeps its own filename stem."""
    zip_path = make_zip(tmp_path, ["part.stl", "a/nested.stl"])
    target = page("https://www.printables.com/model/88-planter", PRINTABLES_FREE_PAGE)
    stub_attempt(monkeypatch, ZipAttempt(tmp_path=zip_path, filename="download.zip", mime="application/zip", page=target))

    outcome = project_import.import_into_project(
        "https://www.printables.com/model/88-planter",
        ImportProjectRequest(
            url="https://www.printables.com/model/88-planter",
            entries=["part.stl", "a/nested.stl"],
            title="Archive title",
        ),
    )

    assert project_row(outcome.project_id).name == "Archive title"
    assert {asset.name for asset in outcome.assets} == {"part", "nested"}
    assert {asset.filename for asset in outcome.assets} == {"part.stl", "nested.stl"}


def test_a_gated_zip_import_still_takes_the_title_for_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gated branch writes no files, but it does create the project the title names."""
    target = page("https://www.printables.com/model/89-paid", PRINTABLES_PAID_PAGE)
    stub_attempt(
        monkeypatch,
        ZipAttempt(
            filename="download.zip",
            mime="application/zip",
            page=target,
            error=HTTPException(status_code=403, detail="forbidden"),
        ),
    )

    outcome = project_import.import_into_project(
        "https://www.printables.com/model/89-paid",
        ImportProjectRequest(
            url="https://www.printables.com/model/89-paid",
            entries=["part.stl"],
            title="Archive title",
        ),
    )

    assert outcome.gated is True
    assert outcome.assets == []
    assert project_row(outcome.project_id).name == "Archive title"


def test_project_name_still_beats_the_title(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An explicit project name wins over the title, which only fills the gap."""
    zip_path = make_zip(tmp_path, ["part.stl", "a/nested.stl"])
    target = page("https://www.printables.com/model/88-planter", PRINTABLES_FREE_PAGE)
    stub_attempt(monkeypatch, ZipAttempt(tmp_path=zip_path, filename="download.zip", mime="application/zip", page=target))

    outcome = project_import.import_into_project(
        "https://www.printables.com/model/88-planter",
        ImportProjectRequest(
            url="https://www.printables.com/model/88-planter",
            entries=["part.stl", "a/nested.stl"],
            project_name="Chosen Name",
            title="Archive title",
        ),
    )

    assert project_row(outcome.project_id).name == "Chosen Name"


def test_a_non_zip_download_is_a_415_and_leaves_no_temp_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tmp_file = tmp_path / "download.stl"
    tmp_file.write_bytes(STL_BYTES)
    stub_attempt(
        monkeypatch,
        ZipAttempt(
            tmp_path=tmp_file,
            filename="download.stl",
            mime="model/stl",
            page=page("https://example.com/model", MAKERWORLD_FREE_PAGE),
        ),
    )

    with pytest.raises(HTTPException) as excinfo:
        project_import.import_into_project(
            "https://example.com/model", ImportProjectRequest(url="https://example.com/model", entries=["a.stl"])
        )

    assert excinfo.value.status_code == 415
    assert not tmp_file.exists()


def test_a_corrupt_zip_is_a_400_and_leaves_no_temp_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    not_a_zip = tmp_path / "download.zip"
    not_a_zip.write_bytes(b"definitely not a zip")
    stub_attempt(
        monkeypatch,
        ZipAttempt(tmp_path=not_a_zip, filename="download.zip", mime="application/zip", page=page("https://example.com/model", MAKERWORLD_FREE_PAGE)),
    )

    with pytest.raises(HTTPException) as excinfo:
        project_import.import_into_project(
            "https://example.com/model", ImportProjectRequest(url="https://example.com/model", entries=["a.stl"])
        )

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "Invalid zip file"
    assert not not_a_zip.exists()


# --- zip-as-project: cover and marker (Mf-qbji) ------------------------------


def png_bytes(width: int, height: int) -> bytes:
    """A real, decodable PNG: extraction thumbnails images, so bytes must decode."""
    raw_row = b"\x00" + b"\xff\xff\xff" * width
    raw = raw_row * height

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def import_zip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    names: list[str],
    entries: list[str],
    url: str,
    payloads: dict[str, bytes] | None = None,
    project_id: str | None = None,
) -> project_import.ImportOutcome:
    """Run one zip-as-project import through the stubbed attempt seam."""
    zip_path = make_zip(tmp_path, names, payloads=payloads)
    target = page(url, PRINTABLES_FREE_PAGE)
    stub_attempt(monkeypatch, ZipAttempt(tmp_path=zip_path, filename="download.zip", mime="application/zip", page=target))
    return project_import.import_into_project(
        url, ImportProjectRequest(url=url, entries=entries, project_id=project_id)
    )


def test_a_zip_project_gets_a_cover_from_its_largest_registered_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The zip door asks the detection cover rule over the rows it just created."""
    url = "https://www.printables.com/model/201-vase"
    outcome = import_zip(
        monkeypatch,
        tmp_path,
        names=["small.png", "large.png", "part1.stl", "part2.stl"],
        entries=["small.png", "large.png", "part1.stl", "part2.stl"],
        url=url,
        payloads={"small.png": png_bytes(1, 1), "large.png": png_bytes(16, 16)},
    )

    images = {asset.filename: asset for asset in outcome.assets if asset.filename.endswith(".png")}
    small, large = images["small.png"], images["large.png"]
    assert large.size > small.size, "the fixture must make the 16x16 PNG the larger one"

    row = project_row(outcome.project_id)
    assert row.cover_asset_id == large.id
    assert json.loads(row.metadata_json)["_bundle"]["cover"] == {
        "asset_id": large.id,
        "rel_path": large.storage_path,
        "reason": "largest-image",
        "group": None,
    }


def test_a_zip_with_no_image_gets_no_cover_rather_than_a_broken_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The images-only ruling: no image, no cover - not a fabricated one.

    This deliberately does NOT use the model-preview leg the detection door would
    use for the same files, so a models-only archive gets a blank card here.
    """
    url = "https://www.printables.com/model/202-nut"
    outcome = import_zip(
        monkeypatch,
        tmp_path,
        names=["PartC/nut.stl", "PartD/bolt.stl"],
        entries=["PartC/nut.stl", "PartD/bolt.stl"],
        url=url,
    )

    row = project_row(outcome.project_id)
    assert row.cover_asset_id is None
    assert json.loads(row.metadata_json)["_bundle"]["cover"] == {
        "asset_id": None,
        "rel_path": None,
        "reason": "none",
        "group": None,
    }


def test_the_two_doors_agree_on_the_image_leg_and_only_there(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Parity pin: the same rule answers both doors, except the model-preview leg."""
    image_url = "https://www.printables.com/model/203-vase"
    image_outcome = import_zip(
        monkeypatch,
        tmp_path,
        names=["small.png", "large.png", "part1.stl"],
        entries=["small.png", "large.png", "part1.stl"],
        url=image_url,
        payloads={"small.png": png_bytes(1, 1), "large.png": png_bytes(16, 16)},
    )
    image_marker = json.loads(project_row(image_outcome.project_id).metadata_json)["_bundle"]["cover"]
    detection = bundle_service.cover_for_registered(bundle_service.registered_from_rows(image_outcome.assets), None)

    assert detection["asset_id"] == image_marker["asset_id"]
    assert detection["rel_path"] == image_marker["rel_path"]
    assert detection["reason"] == image_marker["reason"]

    model_url = "https://www.printables.com/model/204-nut"
    model_outcome = import_zip(
        monkeypatch,
        tmp_path,
        names=["PartC/nut.stl", "PartD/bolt.stl"],
        entries=["PartC/nut.stl", "PartD/bolt.stl"],
        url=model_url,
    )
    model_marker = json.loads(project_row(model_outcome.project_id).metadata_json)["_bundle"]["cover"]
    detection = bundle_service.cover_for_registered(bundle_service.registered_from_rows(model_outcome.assets), None)

    assert detection["reason"] == bundle_service.COVER_MODEL_PREVIEW
    assert model_marker["reason"] == bundle_service.COVER_NONE


def test_the_zip_project_marker_names_the_door_its_key_and_its_groups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The marker is the detection shape with honest nulls and THIS door's name."""
    # Group names are the archive's TOP folders because that is what
    # `resolve_zip_folder_id` groups the project by, so the marker's groups and
    # the project's own sections cannot disagree.
    url = "https://www.printables.com/model/205-thing"
    outcome = import_zip(
        monkeypatch,
        tmp_path,
        names=["Thing/one.stl", "Thing/two.stl"],
        entries=["Thing/one.stl", "Thing/two.stl"],
        url=url,
    )

    expected_key = project_import.ZIP_PROJECT_KEY_PREFIX + canonical_url(url)
    marker = json.loads(project_row(outcome.project_id).metadata_json)["_bundle"]
    assert {k: v for k, v in marker.items() if k != "cover"} == {
        "key": expected_key,
        "source": project_import.ZIP_PROJECT_SOURCE,
        "detector": None,
        "confidence": None,
        "rel_dir": "Thing",
        "archive": None,
        "twin_verified": None,
        "shape": "compound",
        "groups": [{"name": "Thing", "model_count": 2, "foreign": False, "included": True, "attached": 2}],
    }
    assert marker["cover"] == {"asset_id": None, "rel_path": None, "reason": bundle_service.COVER_NONE, "group": None}

    mixed_url = "https://www.printables.com/model/206-mixed"
    mixed = import_zip(
        monkeypatch,
        tmp_path,
        names=["PartA/bracket.stl", "PartB/lid.stl", "previewA.png"],
        entries=["PartA/bracket.stl", "PartB/lid.stl", "previewA.png"],
        url=mixed_url,
        payloads={"previewA.png": png_bytes(4, 4)},
    )
    mixed_marker = json.loads(project_row(mixed.project_id).metadata_json)["_bundle"]
    assert mixed_marker["rel_dir"] is None, "a mixed-root archive has no honest walk root"
    assert mixed_marker["shape"] == "compound"
    assert mixed_marker["groups"] == [
        {"name": "PartA", "model_count": 1, "foreign": False, "included": True, "attached": 1},
        {"name": "PartB", "model_count": 1, "foreign": False, "included": True, "attached": 1},
    ]
    assert mixed_marker["cover"]["reason"] == bundle_service.COVER_LARGEST_IMAGE
    assert mixed_marker["cover"]["group"] is None, "the root-level preview belongs to no group"


def test_a_re_import_never_clobbers_a_human_cover_or_a_detection_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fill only what is empty: a user cover and a detection marker both survive."""
    human_url = "https://www.printables.com/model/207-human"
    human_cover = make_asset("human-cover.stl")
    with Session(engine) as session:
        seeded = project_service.create_project(session, "Human cover", None, human_cover.id, source_url=human_url)
    human = import_zip(
        monkeypatch,
        tmp_path,
        names=["human.png", "human-part.stl"],
        entries=["human.png", "human-part.stl"],
        url=human_url,
        payloads={"human.png": png_bytes(8, 8)},
    )
    assert human.created is False
    assert human.project_id == seeded.id
    assert project_row(seeded.id).cover_asset_id == human_cover.id, "a human cover is never overwritten"

    detection_url = "https://www.printables.com/model/208-detected"
    seeded_metadata = json.dumps(
        {"_bundle": {"key": "import/17_ClockworkMonster", "detector": "folder", "groups": [{"name": "a", "model_count": 1}]}}
    )
    with Session(engine) as session:
        detected = project_service.create_project(
            session,
            "Detected project",
            None,
            None,
            source_url=detection_url,
            metadata={"_bundle": json.loads(seeded_metadata)["_bundle"]},
        )
    detected_outcome = import_zip(
        monkeypatch,
        tmp_path,
        names=["detected.png", "detected-part.stl"],
        entries=["detected.png", "detected-part.stl"],
        url=detection_url,
        payloads={"detected.png": png_bytes(8, 8)},
    )
    detected_row = project_row(detected.id)
    assert detected_outcome.created is False
    assert detected_row.metadata_json == seeded_metadata, "a detection marker is someone else's record"
    assert detected_row.cover_asset_id is not None, "an empty cover is still filled"

    empty_url = "https://www.printables.com/model/209-empty"
    with Session(engine) as session:
        empty = project_service.create_project(session, "Empty project", None, None, source_url=empty_url)
    empty_outcome = import_zip(
        monkeypatch,
        tmp_path,
        names=["empty.png", "empty-part.stl"],
        entries=["empty.png", "empty-part.stl"],
        url=empty_url,
        payloads={"empty.png": png_bytes(8, 8)},
    )
    empty_row = project_row(empty.id)
    assert empty_outcome.created is False
    assert empty_row.cover_asset_id is not None
    assert json.loads(empty_row.metadata_json)["_bundle"]["key"] == project_import.ZIP_PROJECT_KEY_PREFIX + canonical_url(empty_url)

    # (d) This door's OWN marker is a free slot: a second import of the same page
    # refreshes it rather than treating it as someone else's. What must never
    # move is the identity - the key names the page, not the selection.
    again = import_zip(
        monkeypatch,
        tmp_path,
        names=["empty.png", "empty-part.stl", "extra.stl"],
        entries=["empty.png", "empty-part.stl", "extra.stl"],
        url=empty_url,
        payloads={"empty.png": png_bytes(8, 8)},
    )
    again_row = project_row(again.project_id)
    assert again.created is False
    assert json.loads(again_row.metadata_json)["_bundle"]["source"] == project_import.ZIP_PROJECT_SOURCE
    assert json.loads(again_row.metadata_json)["_bundle"]["key"] == project_import.ZIP_PROJECT_KEY_PREFIX + canonical_url(empty_url)


def test_a_group_records_what_was_selected_and_what_was_attached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The two numbers are the scan-count/attach-set split, and nothing else.

    `model_count` counts what the SELECTION named and `attached` counts what the
    extractor actually wrote. Only an extraction failure can separate them on
    this door, so the requested-but-absent third entry is what pins the split.
    """
    url = "https://www.printables.com/model/214-split"
    outcome = import_zip(
        monkeypatch,
        tmp_path,
        names=["Thing/one.stl", "Thing/two.stl"],
        entries=["Thing/one.stl", "Thing/two.stl", "Thing/missing.stl"],
        url=url,
    )

    marker = json.loads(project_row(outcome.project_id).metadata_json)["_bundle"]
    assert marker["groups"] == [
        {"name": "Thing", "model_count": 3, "foreign": False, "included": True, "attached": 2}
    ]
    assert outcome.failed == ["Thing/missing.stl"]


def test_an_explicit_project_target_gains_an_empty_cover_and_the_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A chosen target with a stock `{}` slot is empty, so the import fills it."""
    url = "https://www.printables.com/model/215-target"
    with Session(engine) as session:
        seeded = project_service.create_project(session, "Chosen target", None, None, source_url=url)
    assert project_row(seeded.id).metadata_json == "{}"

    outcome = import_zip(
        monkeypatch,
        tmp_path,
        names=["preview.png", "part.stl"],
        entries=["preview.png", "part.stl"],
        url=url,
        payloads={"preview.png": png_bytes(4, 4)},
        project_id=seeded.id,
    )

    row = project_row(seeded.id)
    image = next(asset for asset in outcome.assets if asset.filename == "preview.png")
    assert outcome.created is False
    assert outcome.project_id == seeded.id
    assert row.name == "Chosen target"
    assert row.cover_asset_id == image.id
    assert json.loads(row.metadata_json)["_bundle"]["key"] == project_import.ZIP_PROJECT_KEY_PREFIX + canonical_url(url)


def test_the_readers_still_read_the_new_marker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The marker is the shape every existing reader already parses."""
    url = "https://www.printables.com/model/210-thing"
    outcome = import_zip(
        monkeypatch,
        tmp_path,
        names=["Thing/one.stl", "Thing/two.stl"],
        entries=["Thing/one.stl", "Thing/two.stl"],
        url=url,
    )
    raw = project_row(outcome.project_id).metadata_json
    marker = json.loads(raw)["_bundle"]

    assert bundle_service._bundle_marker(raw) == marker
    with Session(engine) as session:
        assert bundle_service._compound_splits(session).get(marker["key"]) is None
        assert (
            bundle_service.find_existing_project(
                session, key="import/17_ClockworkMonster", source_url=None, name="", assets=()
            )
            is None
        ), "an identity key is not a directory key, so no detection lookup false-matches it"

    with TestClient(main.app) as client:
        headers = auth_header(client)
        response = client.get("/projects", headers=headers)

    assert response.status_code == 200, response.text
    listed = next(project for project in response.json() if project["id"] == outcome.project_id)
    assert listed["metadata_json"]["_bundle"]["key"] == marker["key"]


def test_the_whole_zip_as_one_asset_path_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The single-asset door records neither a cover nor a marker."""
    url = "https://www.printables.com/model/211-bundle"
    target = page(url, PRINTABLES_FREE_PAGE)
    # The cover rule keys on the storage path SUFFIX, so the fixture must carry
    # one the rule would pick - a `.zip` path could never fire it.
    asset = make_asset("preview.png")
    stub_attempt(monkeypatch, ImportAttempt(asset=asset, page=target))

    outcome = project_import.import_into_project(url, ImportProjectRequest(url=url))

    row = project_row(outcome.project_id)
    assert member_ids(outcome.project_id) == {asset.id}
    assert row.cover_asset_id is None
    assert row.metadata_json == "{}"


def test_a_gated_zip_import_records_no_cover_and_no_bundle_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gated zip stores no files, so it has nothing to cover and no marker to claim."""
    url = "https://www.printables.com/model/212-paid"
    target = page(url, PRINTABLES_PAID_PAGE)
    stub_attempt(
        monkeypatch,
        ZipAttempt(
            filename="download.zip",
            mime="application/zip",
            page=target,
            error=HTTPException(status_code=403, detail="forbidden"),
        ),
    )

    outcome = project_import.import_into_project(url, ImportProjectRequest(url=url, entries=["part.stl"]))

    row = project_row(outcome.project_id)
    assert outcome.gated is True
    assert row.cover_asset_id is None
    assert json.loads(row.metadata_json) == {"_files": "user-supplied"}


def test_no_storage_path_can_contain_the_identity_keys_separator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The no-false-match claim rests on the renderer refusing `:` in every segment."""
    url = "https://www.printables.com/model/213-colons"
    outcome = import_zip(
        monkeypatch,
        tmp_path,
        names=["Part: A/br:acket.stl", "prev:iew.png"],
        entries=["Part: A/br:acket.stl", "prev:iew.png"],
        url=url,
        payloads={"prev:iew.png": png_bytes(2, 2)},
    )

    assert len(outcome.assets) == 2
    for asset in outcome.assets:
        assert ":" not in asset.storage_path
    assert asset_service._INVALID_SEGMENT_RE.search(":") is not None
    assert ":" not in asset_service.sanitize_path_segment("zip-project:x", "item")
    with Session(engine) as session:
        assert ":" not in asset_service.render_storage_path(outcome.assets[0], session)


# --- collisions and totality ------------------------------------------------


def test_a_name_collision_suffixes_the_name_instead_of_409ing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The files are already imported: a propagated 409 would orphan them."""
    with Session(engine) as session:
        project_service.create_project(session, "benchy boat", None, None)
    asset = make_asset()
    stub_attempt(
        monkeypatch,
        ImportAttempt(asset=asset, page=page("https://makerworld.com/en/models/321-cube", MAKERWORLD_FREE_PAGE)),
    )

    outcome = project_import.import_into_project(
        "https://makerworld.com/en/models/321-cube", ImportProjectRequest(url="https://makerworld.com/en/models/321-cube")
    )

    assert outcome.created is True
    assert project_row(outcome.project_id).name == "Benchy Boat (2)"
    assert member_ids(outcome.project_id) == {asset.id}, "the imported asset is not orphaned"


def test_a_garbage_stored_source_does_not_break_an_unrelated_import(monkeypatch: pytest.MonkeyPatch) -> None:
    with Session(engine) as session:
        project_service.create_project(session, "Legacy Project", None, None, source_url="not a url")
    asset = make_asset()
    stub_attempt(
        monkeypatch,
        ImportAttempt(asset=asset, page=page("https://makerworld.com/en/models/777-cube", MAKERWORLD_FREE_PAGE)),
    )

    outcome = project_import.import_into_project(
        "https://makerworld.com/en/models/777-cube", ImportProjectRequest(url="https://makerworld.com/en/models/777-cube")
    )

    assert outcome.created is True
    assert project_row(outcome.project_id).name == "Benchy Boat"


# --- route wiring -----------------------------------------------------------


def test_the_route_is_contribute_and_audited() -> None:
    assert permissions.ROUTE_PERMISSIONS[("POST", "/import/project")] == permissions.CONTRIBUTE
    assert audit.ROUTE_ACTIONS[("POST", "/import/project")] == (audit.ASSET_CREATE,)


def test_the_route_answers_with_the_project_and_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    target = page("https://makerworld.com/en/models/2-paid", MAKERWORLD_PAID_PAGE)
    monkeypatch.setattr(main, "normalize_import_url", lambda raw: raw)
    stub_attempt(monkeypatch, ImportAttempt(page=target, error=HTTPException(status_code=403, detail="forbidden")))

    with TestClient(main.app) as client:
        headers = auth_header(client)
        response = client.post(
            "/import/project",
            json={"url": "https://makerworld.com/en/models/2-paid"},
            headers=headers,
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["gated"] is True
    assert body["created"] is True
    assert body["message"] == GATED_IMPORT_DETAIL
    assert body["assets"] == []
    assert body["project"]["origin"] == "imported"
    assert body["project"]["source_url"] == "https://makerworld.com/en/models/2-paid"
    assert body["project"]["metadata_json"] == {"_files": "user-supplied"}
    assert body["project"]["asset_count"] == 0


# --- a dead credential is not a gated model (Mm-alfd) ----------------------


def test_a_credential_refusal_on_a_free_page_raises_and_creates_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The remedy must reach the operator, not GATED_IMPORT_DETAIL."""
    target = page(THINGIVERSE_URL, THINGIVERSE_FREE_PAGE)
    refusal = import_resolvers.CredentialRefusal(
        status_code=403,
        detail="Thingiverse answered the view-token request without a token. " + import_resolvers.THINGIVERSE_COOKIE_REMEDY,
    )
    stub_attempt(monkeypatch, ImportAttempt(page=target, error=refusal))
    before = project_count()

    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        project_import.import_into_project(THINGIVERSE_URL, ImportProjectRequest(url=THINGIVERSE_URL))

    assert caught.value is refusal
    assert project_count() == before, "a refused credential must leave no project behind"


def test_a_credential_refusal_on_a_paid_page_still_takes_the_details_only_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page's own flag is checked first, so a paid page keeps its outcome."""
    target = page(THINGIVERSE_URL, THINGIVERSE_PAID_PAGE)
    refusal = import_resolvers.CredentialRefusal(
        status_code=403,
        detail="Thingiverse refused the download API for thing 5390184 (HTTP 403). "
        + import_resolvers.THINGIVERSE_COOKIE_REMEDY,
    )
    stub_attempt(monkeypatch, ImportAttempt(page=target, error=refusal))

    outcome = project_import.import_into_project(THINGIVERSE_URL, ImportProjectRequest(url=THINGIVERSE_URL))

    assert outcome.gated is True
    assert outcome.message == GATED_IMPORT_DETAIL
    assert json.loads(project_row(outcome.project_id).metadata_json) == {"_files": "user-supplied"}


def test_a_tokenless_view_token_at_the_door_refuses_the_cookie_and_creates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AC at the door: the real _resolve_import, the real raise, no project.

    The page HTML names a real file, so a fall-through past the refusal would
    fetch a second time and trip the scripted page fetch. The resolver's recorded
    URLs prove the import stopped at the token step.
    """
    pin_public_dns(monkeypatch)
    page_calls = script_page(monkeypatch, THINGIVERSE_PAGE_WITH_A_FILE_LINK, THINGIVERSE_URL)
    resolver_urls = script_resolver(monkeypatch, ScriptedResponse(b"{}", "application/json", THINGIVERSE_URL))
    captured: dict[str, ImportAttempt] = {}
    real_attempt = import_service.import_asset_attempt

    def capture(url: str, body, *, origin=None) -> ImportAttempt:
        captured["attempt"] = real_attempt(url, body, origin=origin)
        return captured["attempt"]

    monkeypatch.setattr(main, "import_asset_attempt", capture)
    before = project_count()

    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        project_import.import_into_project(
            THINGIVERSE_URL, ImportProjectRequest(url=THINGIVERSE_URL, thingiverse_cookie="session=abc")
        )

    assert isinstance(captured["attempt"].error, import_resolvers.CredentialRefusal)
    assert caught.value is captured["attempt"].error
    assert caught.value.status_code == 403
    assert caught.value.detail.endswith(import_resolvers.THINGIVERSE_COOKIE_REMEDY)
    assert "Settings > Import sources" in caught.value.detail
    assert "signed-out or expired" in caught.value.detail, "this is the no-token row, not an unreadable body"
    assert "direct download link" not in caught.value.detail
    assert page_calls == [THINGIVERSE_URL]
    assert resolver_urls == [THINGIVERSE_AUTH_VIEW_URL]
    assert project_count() == before


def test_a_free_page_whose_files_api_refuses_creates_no_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row F at the door: the files step refuses, so nothing is written."""
    pin_public_dns(monkeypatch)
    page_calls = script_page(monkeypatch, THINGIVERSE_PAGE_WITH_A_FILE_LINK, THINGIVERSE_URL)
    resolver_urls = script_resolver(
        monkeypatch,
        ScriptedResponse(b'{"access": "tok"}', "application/json", THINGIVERSE_URL),
        resolver_http_error(403, b'{"error": "Unauthorized"}'),
    )
    before = project_count()

    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        project_import.import_into_project(
            THINGIVERSE_URL, ImportProjectRequest(url=THINGIVERSE_URL, thingiverse_cookie="session=abc")
        )

    assert caught.value.status_code == 403
    assert "refused the download API for thing 5390184" in caught.value.detail
    assert caught.value.detail.endswith(import_resolvers.THINGIVERSE_COOKIE_REMEDY)
    assert "direct download link" not in caught.value.detail
    assert page_calls == [THINGIVERSE_URL]
    assert resolver_urls == [THINGIVERSE_AUTH_VIEW_URL, THINGIVERSE_FILES_URL]
    assert project_count() == before, "a files-step refusal must not leave an empty project"


def test_a_thingiverse_url_with_no_thing_id_still_answers_the_direct_link_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R16: with no thing id the cookie is not implicated, so today's 400 stands."""
    pin_public_dns(monkeypatch)
    url = "https://www.thingiverse.com/explore"
    page_calls = script_page(monkeypatch, THINGIVERSE_PAGE_WITHOUT_A_THING_ID, url)
    resolver_urls = script_resolver(monkeypatch)
    before = project_count()

    with pytest.raises(HTTPException) as caught:
        project_import.import_into_project(url, ImportProjectRequest(url=url, thingiverse_cookie="session=abc"))

    assert caught.value.status_code == 400
    assert caught.value.detail == "No downloadable model file found. Use a direct download link."
    assert page_calls == [url]
    assert resolver_urls == [], "no thing id means no resolver request at all"
    assert project_count() == before


@pytest.mark.parametrize(
    "code, expected_status",
    [
        pytest.param(401, 403, id="401-reported-as-403"),
        pytest.param(403, 403, id="403-carried-through"),
        pytest.param(429, 429, id="429-carried-through"),
    ],
)
def test_a_credentialed_download_api_refusal_at_the_door_skips_every_fallback(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    expected_status: int,
) -> None:
    """The MakerWorld AC at the door: the real resolver raises, and nothing past it runs.

    The page names a real file, so a fall-through past the refusal would fetch a
    second time and trip the scripted page fetch. Only the model download URL is
    recorded: the model fallback, the four anonymous candidates and the
    `find_download_url` href rescue never ran. All three statuses travel the whole
    route, because the remap and the carry-through are not the resolver's alone.
    """
    pin_public_dns(monkeypatch)
    page_calls = script_page(monkeypatch, MAKERWORLD_PAGE_WITH_A_FILE_LINK, MAKERWORLD_URL)
    resolver_urls = script_resolver(monkeypatch, resolver_http_error(code, b'{"error": "Unauthorized"}'))
    before = project_count()

    with pytest.raises(import_resolvers.CredentialRefusal) as caught:
        project_import.import_into_project(
            MAKERWORLD_URL, ImportProjectRequest(url=MAKERWORLD_URL, makerworld_cookie="mw_session=abc")
        )

    assert caught.value.status_code == expected_status
    assert caught.value.detail.endswith(import_resolvers.MAKERWORLD_COOKIE_REMEDY)
    assert "refused the download API for design 1" in caught.value.detail
    assert f"(HTTP {code})" in caught.value.detail, "the vendor's own code stays visible, even when 401 is reported as 403"
    assert "mw_session=abc" not in caught.value.detail
    assert page_calls == [MAKERWORLD_URL]
    assert resolver_urls == [MAKERWORLD_MODEL_DOWNLOAD_URL]
    assert project_count() == before


def test_the_anonymous_column_at_the_door_keeps_the_generic_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cookie-less import is not the credential's fault, so today's 400 is byte-identical.

    The environment variable is cleared first: a stray MAKERWORLD_COOKIE in the
    test runner's environment must not quietly turn this column credentialed.
    """
    monkeypatch.delenv("MAKERWORLD_COOKIE", raising=False)
    pin_public_dns(monkeypatch)
    page_calls = script_page(monkeypatch, MAKERWORLD_FREE_PAGE, MAKERWORLD_URL)
    resolver_urls = script_resolver(
        monkeypatch,
        resolver_http_error(403, b'{"error": "Unauthorized"}'),
        resolver_http_error(404, b""),
        resolver_http_error(404, b""),
        resolver_http_error(404, b""),
        resolver_http_error(404, b""),
    )
    before = project_count()

    with pytest.raises(HTTPException) as caught:
        project_import.import_into_project(MAKERWORLD_URL, ImportProjectRequest(url=MAKERWORLD_URL))

    assert caught.value.status_code == 400
    assert not isinstance(caught.value, import_resolvers.CredentialRefusal)
    assert caught.value.detail == "No downloadable model file found. Use a direct download link."
    assert page_calls == [MAKERWORLD_URL]
    assert resolver_urls == [MAKERWORLD_MODEL_DOWNLOAD_URL, *MAKERWORLD_ANONYMOUS_CANDIDATES]
    assert project_count() == before


def test_a_makerworld_credential_refusal_on_a_paid_page_keeps_the_details_only_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page's own flag is checked first, so a paid page keeps its details-only outcome."""
    target = page(MAKERWORLD_URL, MAKERWORLD_PAID_PAGE)
    refusal = import_resolvers.CredentialRefusal(
        status_code=403,
        detail=import_resolvers._makerworld_cookie_detail(
            "MakerWorld refused the download API for design 1 (HTTP 403): Unauthorized."
        ),
    )
    stub_attempt(monkeypatch, ImportAttempt(page=target, error=refusal))

    outcome = project_import.import_into_project(
        MAKERWORLD_URL, ImportProjectRequest(url=MAKERWORLD_URL, makerworld_cookie="mw_session=abc")
    )

    assert outcome.gated is True
    assert outcome.message == GATED_IMPORT_DETAIL
    assert json.loads(project_row(outcome.project_id).metadata_json) == {"_files": "user-supplied"}
