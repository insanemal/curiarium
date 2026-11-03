"""OS metadata in a zip is junk: listed as junk, pre-unticked and never imported (Mf-ls9v).

Before this ticket the entries listing called every member a file, so a Finder or
Explorer archive pre-ticked `__MACOSX/` sidecars, `.DS_Store`, `Thumbs.db` and
`desktop.ini` for import. Worse, a root `.DS_Store` has no extension, so
`filename_for_model_name` raised its 400 and the per-entry classifier re-raised
every 400 - the whole import aborted instead of losing one entry. Now one
predicate (``zip_junk.is_junk_entry``) decides what is junk, the listing reports
the verdict, and the extractor refuses a junk entry before it can create a
folder, an asset row or an audit row.

The tests here are the backend half of AC-1..AC-4 and AC-6; AC-5 (the modal's
default ticks) lives with the web tests. Storage and the database are shared for
the whole session, so every test works in a temporary storage root and removes
the rows it created.
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

import asset_file_service
import asset_service
import db
import library_scan
import main
import schemas
import settings_service
import zip_service
from db import engine
from migrations import run_migrations
from models import Asset, AssetFile, Folder, MfaRecoveryCode, Project, ProjectAsset, User, UserSession
from zip_junk import is_junk_entry

# The nasty archive the AC tests share: three real files at three depths and the
# five pieces of OS metadata a macOS or Windows archiver adds on its own.
NASTY_PAYLOAD = {"part.stl", "a/b/nested.stl", "a/b/deep/inner.stl"}
NASTY_JUNK = {
    "__MACOSX/._part.stl",
    "__MACOSX/a/._nested.stl",
    ".DS_Store",
    "a/.DS_Store",
    "a/b/Thumbs.db",
}
# Declared sizes: the five junk members alone are 120 bytes, which is what makes
# the cumulative-cap test below meaningful.
NASTY_ENTRIES: Dict[str, bytes] = {
    "part.stl": bytes(range(32, 132)),
    "a/b/nested.stl": bytes(range(32, 37)),
    "a/b/deep/inner.stl": bytes(range(32, 37)),
    "__MACOSX/._part.stl": bytes(range(32, 56)),
    "__MACOSX/a/._nested.stl": bytes(range(32, 56)),
    ".DS_Store": bytes(range(32, 56)),
    "a/.DS_Store": bytes(range(32, 56)),
    "a/b/Thumbs.db": bytes(range(32, 56)),
}


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
    """Point storage at a temp root, restore the template and undo every row made."""
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


def write_zip(path: Path, entries: Dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def make_nasty_zip(path: Path) -> Path:
    return write_zip(path, NASTY_ENTRIES)


def extract_request(entries: List[str]) -> schemas.ImportZipExtractRequest:
    return schemas.ImportZipExtractRequest(url="upload://archive.zip", entries=entries)


def extract(path: Path, entries: List[str]) -> Tuple[List[Asset], List[str]]:
    return zip_service.extract_zip_entries_to_assets(path, entries, extract_request(entries))


def files_under(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}


def state() -> dict:
    """Every row family junk could add to, as plain sets, measured not assumed."""
    with Session(engine) as session:
        return {
            "assets": {asset.id for asset in session.exec(select(Asset))},
            "folders": {folder.id for folder in session.exec(select(Folder))},
            "projects": {project.id for project in session.exec(select(Project))},
            "members": {(member.project_id, member.asset_id) for member in session.exec(select(ProjectAsset))},
            "covers": {project.id: project.cover_asset_id for project in session.exec(select(Project))},
        }


def folder_paths(folder_ids: set[str]) -> set[str]:
    with Session(engine) as session:
        return {"/".join(asset_service.folder_segments(session, folder_id)) for folder_id in folder_ids}


def install_zip_download(monkeypatch: pytest.MonkeyPatch, archive: Path) -> None:
    """Serve a fresh copy of `archive` to every route call.

    The import route unlinks the temp file in its own `finally`, so a single
    pre-written path would be gone for a second request in the same test; the
    fake rewrites it instead.
    """
    monkeypatch.setattr(main, "normalize_import_url", lambda url: url)
    monkeypatch.setattr(
        main,
        "download_import_to_temp",
        lambda url, body: (make_nasty_zip(archive), "models.zip", "application/zip"),
    )


# --- the predicate ----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("__MACOSX/._part.stl", True),
        ("__MACOSX/a/._nested.stl", True),
        (".DS_Store", True),
        ("a/.DS_Store", True),
        ("a/b/Thumbs.db", True),
        ("desktop.ini", True),
        ("._part.stl", True),
        (".Spotlight-V100/x", True),
        ("$RECYCLE.BIN/S-1-5/x.stl", True),
        ("A/B/.DS_STORE", True),
        ("x\\__MACOSX\\y.stl", True),
        ("part.stl", False),
        ("a/b/nested.stl", False),
        ("notes.txt", False),
        # A name that merely ends like a basename, or a prefix inside a name, is
        # a real file: matching is per whole segment, never a substring.
        ("model.ds_store.stl", False),
        ("_part.stl", False),
        ("a/b", False),
        ("my__MACOSX.stl", False),
        ("thumbs.dbx", False),
        # Total on garbage: a route can hand this an empty selection member.
        ("", False),
        (None, False),
    ],
)
def test_predicate_table(path: Optional[str], expected: bool) -> None:
    """The exact vocabulary, both directions, including both separators and case."""
    assert is_junk_entry(path) is expected


# --- AC-1: junk is one failed entry and can never become a row -------------


def test_all_eight_selected_import_three_assets_and_five_failed_entries(tmp_path: Path) -> None:
    """The regression itself: selecting everything yields the payload and nothing else.

    Measured by reverting the two halves of the fix one at a time. At base
    `2d5f4dc` this test raises: `.DS_Store` has no extension,
    `filename_for_model_name` answers 400 and the old classifier
    (`if exc.status_code != 409: raise`) re-raised it for the whole request. With
    the classifier fixed but the junk guard removed (`entry_name in plan.junk`)
    it does not raise - it imports six assets, the three payload files plus
    `._part`, `._nested` and `Thumbs`, the junk members that happen to carry an
    extension - and leaves two `failed`. So the guard line, not the classifier,
    is what this test pins; the classifier's own evidence is the extensionless
    `LICENSE` test below.
    """
    archive = make_nasty_zip(tmp_path / "nasty.zip")
    before = state()

    assets, failed = extract(archive, sorted(NASTY_PAYLOAD | NASTY_JUNK))

    assert sorted(asset.name for asset in assets) == ["inner", "nested", "part"]
    assert sorted(asset.filename for asset in assets) == ["inner.stl", "nested.stl", "part.stl"]
    assert sorted(failed) == sorted(NASTY_JUNK)

    after = state()
    with Session(engine) as session:
        filenames = {asset.filename for asset in session.exec(select(Asset))}
    assert filenames.isdisjoint({".DS_Store", "Thumbs.db", "._part.stl", "._nested.stl"})
    # Exactly the three folders the payload's own paths need: no `__MACOSX` row.
    assert folder_paths(after["folders"] - before["folders"]) == {"a", "a/b", "a/b/deep"}
    assert after["projects"] == before["projects"]
    assert after["members"] == before["members"]
    assert after["covers"] == before["covers"]


# --- AC-2: one bad entry is still just one bad entry ----------------------


def test_root_ds_store_beside_a_real_file_imports_the_real_file(tmp_path: Path) -> None:
    """A root `.DS_Store` no longer aborts the import; it is the only failure."""
    archive = make_nasty_zip(tmp_path / "nasty.zip")

    assets, failed = extract(archive, [".DS_Store", "part.stl"])

    assert [asset.filename for asset in assets] == ["part.stl"]
    assert failed == [".DS_Store"]


def test_an_extensionless_payload_entry_is_reported_not_aborted(tmp_path: Path) -> None:
    """`LICENSE` is not junk, but it still has no extension: one failed entry.

    This is the AssetFilenameError half: without the class-based classifier the
    400 it carries would re-raise and the selection would lose `part.stl` too.
    Measured: reverting the classifier to `status_code != 409` fails this test
    and nothing else in this file, because the junk guard already keeps every
    junk entry away from `filename_for_model_name`.
    """
    archive = write_zip(tmp_path / "with-license.zip", {"LICENSE": b"CC0\n", "part.stl": NASTY_ENTRIES["part.stl"]})

    assets, failed = extract(archive, ["LICENSE", "part.stl"])

    assert [asset.filename for asset in assets] == ["part.stl"]
    assert failed == ["LICENSE"]


# --- AC-3: a junk-only selection is a 200 that writes nothing -------------


def test_a_junk_only_selection_answers_200_and_writes_nothing(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = isolated_storage
    install_zip_download(monkeypatch, tmp_path / "models.zip")
    headers = auth_header(client)
    before, files_before = state(), files_under(storage)

    response = client.post(
        "/import/zip",
        json={"url": "https://example.com/models.zip", "entries": sorted(NASTY_JUNK)},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["assets"] == []
    assert sorted(response.json()["failed"]) == sorted(NASTY_JUNK)
    assert state() == before
    assert files_under(storage) == files_before


def test_junk_does_not_count_toward_the_cumulative_cap(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The junk members declare 120 bytes; the cap is 60, and only payload counts.

    Without `junk` being excluded from `declared_total` the first request is a
    413; with it, the junk-only request is a 200 while a payload selection over
    the same cap still 413s.
    """
    install_zip_download(monkeypatch, tmp_path / "models.zip")
    headers = auth_header(client)
    monkeypatch.setattr(zip_service, "IMPORT_ZIP_MAX_BYTES", 60)

    junk_only = client.post(
        "/import/zip",
        json={"url": "https://example.com/models.zip", "entries": sorted(NASTY_JUNK)},
        headers=headers,
    )
    assert junk_only.status_code == 200, junk_only.text
    assert junk_only.json()["assets"] == []

    over_cap = client.post(
        "/import/zip",
        json={"url": "https://example.com/models.zip", "entries": ["part.stl"]},
        headers=headers,
    )
    assert over_cap.status_code == 413, over_cap.text
    assert "IMPORT_ZIP_MAX_MB" in over_cap.json()["detail"]


# --- AC-4a: a whole-request 400 is still raised ----------------------------


def test_a_storage_template_failure_aborts_the_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 400 from the storage template is NOT one failed entry.

    The classifier must match `AssetFilenameError` by class: this exception is
    also a 400 with a different meaning, so `status_code in (400, 409)` would
    swallow it into `failed` and this test would see no raise at all.
    """

    def explode(asset: Asset, session: Session) -> str:
        raise HTTPException(status_code=400, detail="Storage template exploded")

    monkeypatch.setattr(asset_service, "render_storage_path", explode)
    archive = make_nasty_zip(tmp_path / "nasty.zip")
    before = state()

    outcome: Optional[Tuple[List[Asset], List[str]]] = None
    with pytest.raises(HTTPException) as raised:
        outcome = extract(archive, ["part.stl"])

    assert raised.value.status_code == 400
    assert raised.value.detail == "Storage template exploded"
    # Nothing came back, so there is no `failed` list to hide the entry in.
    assert outcome is None
    assert state() == before


# --- AC-6: both entries doors agree on what is junk -----------------------


def test_both_entries_doors_flag_the_same_junk(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One verdict, two doors: the upload seam and the in-library file.

    The first door answers from bytes the server has fetched to a temp file; the
    second from a real `.zip` in the storage root. Both go through
    `list_zip_entries`, so a junk set that disagreed would mean two vocabularies.
    """
    storage = isolated_storage
    install_zip_download(monkeypatch, tmp_path / "models.zip")
    headers = auth_header(client)

    upload = client.post("/import/zip/entries", json={"url": "https://example.com/models.zip"}, headers=headers)
    assert upload.status_code == 200, upload.text
    uploaded = upload.json()["entries"]

    write_zip(storage / "nasty.zip", NASTY_ENTRIES)
    listing = client.get("/import/archive/entries", params={"path": "nasty.zip"}, headers=headers)
    assert listing.status_code == 200, listing.text
    listed = listing.json()["entries"]

    assert {entry["path"] for entry in uploaded if entry["junk"]} == NASTY_JUNK
    assert {entry["path"] for entry in listed if entry["junk"]} == NASTY_JUNK
    assert {entry["path"] for entry in uploaded if not entry["junk"]} == NASTY_PAYLOAD
    assert {entry["path"] for entry in listed if not entry["junk"]} == NASTY_PAYLOAD
