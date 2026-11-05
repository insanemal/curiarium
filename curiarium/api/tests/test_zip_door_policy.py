"""The two-door acceptance for Mf-cdrq / Mf-znu1: one archive, four doors.

Every test here is measured against the same `DOOR_ARCHIVE` (see
`tests/zip_door_fixtures.py`): a payload at two depths plus the five pieces of OS
metadata a macOS or Windows archiver adds on its own - including payload-named
files *inside* `__MACOSX/` and `$RECYCLE.BIN/`, which is what makes "junk never
lands on disk" measurable instead of assumed.

There are two extract doors and two entries doors:

* ``POST /import/zip`` - bytes the server just fetched, extracted into managed
  assets (the upload door);
* ``POST /import/archive`` - an archive already in the library, extracted in
  place into its twin directory (the archive door);
* ``POST /import/zip/entries`` and ``GET /import/archive/entries`` - the two
  listings, which must report one verdict per member.

The archive door is **all-or-nothing**: a selection it refuses publishes nothing,
and a selection that spends no budget writes nothing at all. This module pins
that too, because the same junk member must cost the same nothing on both doors.

The harness is `tests/test_archive_import.py`'s (there is no shared conftest in
this suite): `isolated_storage` points every path-bound module at a temp root,
runs the migrations and deletes the rows a test created. The route's scan kick is
stubbed so assertions stay deterministic; the test that needs registration drives
`library_scan.scan_storage_root` itself, which is synchronous.
"""

from pathlib import Path
from typing import Dict
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select
from zip_door_fixtures import (
    DOOR_ARCHIVE,
    DOOR_JUNK,
    DOOR_JUNK_BYTES,
    DOOR_PAYLOAD,
    DOOR_PAYLOAD_BYTES,
    files_under,
    install_zip_download,
    write_door_archive,
)

import asset_file_service
import asset_service
import bundle_service
import db
import entry_policy
import library_scan
import main
import settings_service
from db import engine
from migrations import run_migrations
from models import Asset, AssetFile, AuditEvent, Folder, MfaRecoveryCode, Project, ProjectAsset, User, UserSession

# The OS-metadata vocabulary a path under the storage root may never contain: the
# junk *directories* and debris names, then the prefixes that make a segment and
# everything beneath it junk.
JUNK_SEGMENTS = ("__macosx", "$recycle.bin", ".ds_store", "thumbs.db")
JUNK_PREFIXES = ("._", ".spotlight-")


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
        projects_before = {project.id for project in session.exec(select(Project))}
        folders_before = {folder.id for folder in session.exec(select(Folder))}
        assets_before = {asset.id for asset in session.exec(select(Asset))}
    yield storage
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


# --- assertions shared by the extract doors --------------------------------


def tree(root: Path) -> Dict[str, bytes]:
    """Every path and (for a file) its exact bytes: enough to prove nothing moved."""
    return {
        str(path.relative_to(root)) or ".": path.read_bytes() if path.is_file() else b"<dir>"
        for path in [root, *root.rglob("*")]
    }


def assert_no_junk_under(root: Path) -> None:
    """No path under `root` carries an OS-metadata segment or a junk-named file.

    The basename half is the one that catches the payload-named junk
    (`__MACOSX/extra.stl`, `$RECYCLE.BIN/.../trashed.stl`): a scan prunes a
    dot-name by itself, so those two are the only members whose presence on disk
    proves the door wrote junk.
    """
    junk_names = {name.rsplit("/", 1)[-1].casefold() for name in DOOR_JUNK}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        segments = [segment.casefold() for segment in relative.split("/")]
        for segment in segments:
            assert segment not in JUNK_SEGMENTS, f"junk segment {segment!r} written on disk: {relative}"
            assert not segment.startswith(JUNK_PREFIXES), f"junk prefix written on disk: {relative}"
        assert segments[-1] not in junk_names, f"junk file written on disk: {relative}"


def new_rows(before_assets: set, before_folders: set) -> tuple[list[Asset], list[Folder]]:
    """Every row this test created, not the session's whole table."""
    with Session(engine) as session:
        assets = [asset for asset in session.exec(select(Asset)) if asset.id not in before_assets]
        folders = [folder for folder in session.exec(select(Folder)) if folder.id not in before_folders]
    return assets, folders


def extract_archive(client: TestClient, headers: dict, path: str = "Nasty.zip") -> dict:
    response = client.post("/import/archive", json={"path": path}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# --- the extract doors write the payload and nothing else ------------------


def test_the_zip_extract_door_writes_only_the_payload(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The upload door's regression: selecting everything imports the payload.

    Its junk half is Mf-ls9v's and already holds; the test stays because the two
    doors must be measured against one archive, and a vocabulary that drifted
    would show up as a different `failed` list here.
    """
    storage = isolated_storage
    install_zip_download(monkeypatch, tmp_path / "models.zip")
    headers = auth_header(client)

    response = client.post(
        "/import/zip",
        json={"url": "https://example.com/models.zip", "entries": sorted(DOOR_ARCHIVE)},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted(asset["filename"] for asset in body["assets"]) == ["nested.stl", "part.stl"]
    assert sorted(body["failed"]) == sorted(DOOR_JUNK)
    assert_no_junk_under(storage)


def test_the_archive_extract_door_writes_only_the_payload(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archive door's regression: seven members, two files, ten bytes."""
    storage = isolated_storage
    write_door_archive(storage / "Nasty.zip")
    headers = auth_header(client)
    stub_scan(monkeypatch)

    body = extract_archive(client, headers)

    assert body["files"] == 2
    assert body["bytes"] == DOOR_PAYLOAD_BYTES
    assert body["already_present"] is False
    assert files_under(storage) - {"Nasty.zip"} == {"Nasty/part.stl", "Nasty/a/b/nested.stl"}


# --- the entries doors agree, member for member ----------------------------


def test_the_zip_entries_door_flags_the_door_archive(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_zip_download(monkeypatch, tmp_path / "models.zip")
    headers = auth_header(client)

    response = client.post("/import/zip/entries", json={"url": "https://example.com/models.zip"}, headers=headers)

    assert response.status_code == 200, response.text
    entries = response.json()["entries"]
    assert {entry["path"] for entry in entries if entry["junk"]} == DOOR_JUNK
    assert {entry["path"] for entry in entries if not entry["junk"]} == DOOR_PAYLOAD


def test_the_archive_entries_door_flags_the_door_archive(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = isolated_storage
    write_door_archive(storage / "Nasty.zip")
    headers = auth_header(client)

    response = client.get("/import/archive/entries", params={"path": "Nasty.zip"}, headers=headers)

    assert response.status_code == 200, response.text
    entries = response.json()["entries"]
    assert {entry["path"] for entry in entries if entry["junk"]} == DOOR_JUNK
    assert {entry["path"] for entry in entries if not entry["junk"]} == DOOR_PAYLOAD


# --- symptom 1: the archive door writes junk on disk ----------------------


def test_the_archive_extract_leaves_no_junk_on_disk(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing on disk may carry a junk segment, whatever the scan would prune."""
    storage = isolated_storage
    write_door_archive(storage / "Nasty.zip")
    headers = auth_header(client)
    stub_scan(monkeypatch)

    body = extract_archive(client, headers)

    assert body["files"] == 2, body
    assert_no_junk_under(storage)
    assert files_under(storage) == {"Nasty.zip", "Nasty/part.stl", "Nasty/a/b/nested.stl"}


def test_a_real_scan_after_the_archive_extract_registers_no_junk_asset_or_folder(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registering scan is the half that turns junk files into junk assets.

    The positive control is the point: a scan that registers nothing would pass a
    "no junk rows" assertion, so the two payload assets must exist, with no extra
    junk-named one beside them. `__MACOSX/extra.stl` and `$RECYCLE.BIN/.../trashed.stl`
    are what a scan sees as assets; `.DS_Store`, `Thumbs.db` and `._part.stl`
    would be pruned as debris or hidden, so they are the weaker half.
    """
    storage = isolated_storage
    write_door_archive(storage / "Nasty.zip")
    headers = auth_header(client)
    stub_scan(monkeypatch)
    with Session(engine) as session:
        assets_before = {asset.id for asset in session.exec(select(Asset))}
        folders_before = {folder.id for folder in session.exec(select(Folder))}

    extract_archive(client, headers)

    state = library_scan.scan_storage_root()
    assert state.failed == 0, state.errors

    assets, folders = new_rows(assets_before, folders_before)
    filenames = {asset.filename for asset in assets}
    # The archive itself is a library file and a scan registers it - that is how a
    # bundle is detected at all. Everything else the scan found must be the
    # payload: exactly those three rows, so a scan that registered nothing fails.
    assert filenames == {"Nasty.zip", "part.stl", "nested.stl"}, "the payload is registered and the junk is not"
    assert filenames.isdisjoint({"extra.stl", "trashed.stl", ".DS_Store", "Thumbs.db", "._part.stl"})
    assert {folder.name for folder in folders}.isdisjoint({"__MACOSX", "$RECYCLE.BIN"})
    with Session(engine) as session:
        full_paths = ["/".join(asset_service.folder_segments(session, folder.id)) for folder in folders]
    assert all("__MACOSX" not in path and "$RECYCLE.BIN" not in path for path in full_paths), full_paths


# --- symptom 2: an oversized junk member refuses the whole extraction ------


def test_an_oversized_junk_member_never_refuses_the_archive_extraction(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two 20-byte junk members are over a 10-byte per-file cap; neither may 413.

    The declared-size preflight lives with the caps, and the caps live in the
    shared policy module `entry_policy`, which both doors read.
    """
    storage = isolated_storage
    write_door_archive(storage / "Nasty.zip")
    headers = auth_header(client)
    stub_scan(monkeypatch)
    # Both caps are read by `entry_policy`, not by the door or the service: that
    # is where the budget lives, so that is what a test patched.
    monkeypatch.setattr(entry_policy, "IMPORT_MAX_BYTES", 10)
    monkeypatch.setattr(entry_policy, "IMPORT_ZIP_MAX_BYTES", 1000)

    body = extract_archive(client, headers)

    assert body["files"] == 2, body
    assert body["bytes"] == DOOR_PAYLOAD_BYTES, body
    assert files_under(storage) == {"Nasty.zip", "Nasty/part.stl", "Nasty/a/b/nested.stl"}
    assert_no_junk_under(storage)


# --- symptom 3: a junk member spends no budget on either door --------------


def test_a_junk_member_spends_no_budget_on_either_door(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same selection, the same cap, both doors: payload 10, junk 60.

    A junk member that counted toward the 10-byte cumulative cap would refuse
    both doors, so the two requests below are one measurement of one rule.
    """
    storage = isolated_storage
    install_zip_download(monkeypatch, tmp_path / "models.zip")
    headers = auth_header(client)
    stub_scan(monkeypatch)
    monkeypatch.setattr(entry_policy, "IMPORT_MAX_BYTES", 1000)
    monkeypatch.setattr(entry_policy, "IMPORT_ZIP_MAX_BYTES", 10)
    write_door_archive(storage / "Nasty.zip")
    selection = sorted(DOOR_ARCHIVE)

    zip_response = client.post(
        "/import/zip",
        json={"url": "https://example.com/models.zip", "entries": selection},
        headers=headers,
    )
    archive_response = client.post(
        "/import/archive",
        json={"path": "Nasty.zip", "entries": selection},
        headers=headers,
    )
    zip_body = zip_response.json()
    archive_body = archive_response.json()

    assert zip_response.status_code == 200, f"zip door {zip_response.status_code}: {zip_body}"
    assert archive_response.status_code == 200, f"archive door {archive_response.status_code}: {archive_body}"
    assert archive_body["bytes"] == DOOR_PAYLOAD_BYTES, archive_body
    # Both doors agree on what the junk member cost: nothing. The zip door's own
    # accounting is the sum of the sizes it imported; they must be the same 10.
    assert archive_body["bytes"] == sum(asset["size"] for asset in zip_body["assets"]), (
        f"junk cost {DOOR_JUNK_BYTES} declared bytes on the archive door but not the zip door: "
        f"zip={zip_body} archive={archive_body}"
    )


# --- a selection that can write nothing writes nothing -------------------


def test_a_junk_only_archive_selection_writes_nothing_and_records_no_audit_row(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All-or-nothing includes "all of it was junk": no files, no rows, no audit.

    The two refusals beside it stay refusals, and the missing-entry refusal fires
    first: a selection naming a member the archive does not hold is a request
    defect, whichever other names it carries.
    """
    storage = isolated_storage
    write_door_archive(storage / "Nasty.zip")
    headers = auth_header(client)
    stub_scan(monkeypatch)
    request_id = str(uuid4())
    before = tree(storage)

    response = client.post(
        "/import/archive",
        json={"path": "Nasty.zip", "entries": sorted(DOOR_JUNK)},
        headers={**headers, "X-Request-ID": request_id},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["files"] == 0, body
    assert body["bytes"] == 0, body
    assert body["already_present"] is False, body
    assert body["scan_started"] is False, body
    with Session(engine) as session:
        assert list(session.exec(select(AuditEvent).where(AuditEvent.request_id == request_id))) == []
    assert tree(storage) == before, "a selection that writes nothing changes nothing"
    assert not (storage / "Nasty").exists(), "a junk-only selection creates no twin directory"

    empty = client.post("/import/archive", json={"path": "Nasty.zip", "entries": []}, headers=headers)
    assert empty.status_code == 400, empty.text
    assert empty.json()["detail"] == "No zip entries selected"

    ghost = client.post(
        "/import/archive", json={"path": "Nasty.zip", "entries": ["part.stl", "ghost.stl"]}, headers=headers
    )
    assert ghost.status_code == 400, ghost.text
    assert ghost.json()["detail"] == "Selection names entries that are not in the archive: ghost.stl"
    assert tree(storage) == before, "a refused selection changes nothing"


def test_the_two_nothing_written_outcomes_are_distinguishable(
    client: TestClient, isolated_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`files == 0` alone cannot tell them apart; `already_present` can.

    A junk-only selection writes nothing because it had nothing writable, so no
    destination existed and `already_present` stays False. An extract whose every
    destination already exists writes nothing for the opposite reason and answers
    True. The pair is the signature: `(0, False)` versus `(0, True)`.
    """
    storage = isolated_storage
    write_door_archive(storage / "Nasty.zip")
    headers = auth_header(client)
    stub_scan(monkeypatch)

    first = extract_archive(client, headers)
    assert first["files"] == len(DOOR_PAYLOAD), first

    repeated = extract_archive(client, headers)
    assert repeated["files"] == 0, repeated
    assert repeated["already_present"] is True, repeated
    assert repeated["scan_started"] is False, repeated

    junk_only = client.post(
        "/import/archive", json={"path": "Nasty.zip", "entries": sorted(DOOR_JUNK)}, headers=headers
    )
    assert junk_only.status_code == 200, junk_only.text
    junk_body = junk_only.json()
    assert junk_body["files"] == 0, junk_body
    assert junk_body["already_present"] is False, junk_body
    assert junk_body["scan_started"] is False, junk_body

    assert (junk_body["files"], junk_body["already_present"]) != (
        repeated["files"],
        repeated["already_present"],
    ), "the two nothing-written outcomes are indistinguishable through the response"


# --- the sentinel: all four doors ask the one predicate --------------------


@pytest.fixture
def sentinel_junk(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make `entry_policy.is_junk_entry` call `part.stl` OS metadata, and record every call.

    The predicate is the only thing that decides what is junk, so a door that
    reached a different copy (a local table, a per-door variant) would not see
    this and would import the file. The recorded calls prove the door asked.
    """
    real = entry_policy.is_junk_entry
    calls: list[str] = []

    def sentinel(path: str) -> bool:
        calls.append(path)
        return path == "part.stl" or real(path)

    monkeypatch.setattr(entry_policy, "is_junk_entry", sentinel)
    return calls


def test_the_sentinel_predicate_reaches_both_extract_doors(
    client: TestClient,
    isolated_storage: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sentinel_junk: list[str],
) -> None:
    """A payload file the predicate calls junk is refused by both extractors.

    If either door classified members with a table of its own it would import
    `part.stl`; if either merely consulted the predicate without the extracted
    bytes following it, the twin would hold the file anyway. Both are measured.
    """
    storage = isolated_storage
    install_zip_download(monkeypatch, tmp_path / "models.zip")
    headers = auth_header(client)
    stub_scan(monkeypatch)

    upload = client.post(
        "/import/zip",
        json={"url": "https://example.com/models.zip", "entries": sorted(DOOR_ARCHIVE)},
        headers=headers,
    )

    assert upload.status_code == 200, upload.text
    upload_body = upload.json()
    assert [asset["filename"] for asset in upload_body["assets"]] == ["nested.stl"]
    assert "part.stl" in upload_body["failed"]

    write_door_archive(storage / "Nasty.zip")
    archive_body = extract_archive(client, headers)

    assert archive_body["files"] == 1, archive_body
    assert archive_body["bytes"] == 5, archive_body
    twin = storage / "Nasty"
    assert files_under(twin) == {"a/b/nested.stl"}, files_under(twin)
    assert not (twin / "part.stl").exists()

    assert sentinel_junk, "neither door asked the predicate"
    assert "part.stl" in sentinel_junk, sentinel_junk


def test_the_sentinel_predicate_reaches_both_entries_doors(
    client: TestClient,
    isolated_storage: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sentinel_junk: list[str],
) -> None:
    """Both listings report the predicate's verdict, so neither has a table of its own."""
    storage = isolated_storage
    install_zip_download(monkeypatch, tmp_path / "models.zip")
    headers = auth_header(client)

    upload = client.post("/import/zip/entries", json={"url": "https://example.com/models.zip"}, headers=headers)
    assert upload.status_code == 200, upload.text
    uploaded = upload.json()["entries"]

    write_door_archive(storage / "Nasty.zip")
    listing = client.get("/import/archive/entries", params={"path": "Nasty.zip"}, headers=headers)
    assert listing.status_code == 200, listing.text
    listed = listing.json()["entries"]

    for entries in (uploaded, listed):
        assert {entry["path"] for entry in entries if entry["junk"]} == DOOR_JUNK | {"part.stl"}
        assert {entry["path"] for entry in entries if not entry["junk"]} == DOOR_PAYLOAD - {"part.stl"}

    assert "part.stl" in sentinel_junk, sentinel_junk
