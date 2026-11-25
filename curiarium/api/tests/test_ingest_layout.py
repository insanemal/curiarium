"""Every door that writes bytes leaves a directory the tree can show (Mf-xqvc).

The rule, verbatim: *if a directory exists on disk that is not a folder row, the
tree must show it or the app must not create it.* A directory nothing owns is a
place the Files tree cannot reach: its bytes are on the volume and invisible in
the UI, and no delete, rename or move can ever act on them.

The measuring stick is ``unseeable_directories``: every directory under the
storage root that is neither the storage root, nor a system directory
(``thumbs``, ``bundles``, ``.incoming``, ``.trash``), nor the ``storage_path`` of
some folder row. Each door - the ordinary upload, the linked-zip import and the
in-library archive extract - is driven here and the measurement is asserted
empty.

The archive door is deliberately two-step and is pinned that way instead of
being pretended atomic: ``archive_service.extract_out`` writes a twin tree with
no rows on purpose, and the storage scan registers it afterwards. The test
asserts the offenders before the scan are exactly the twin subtree, then runs the
real scan and asserts the measurement is clean.

Every test works in a temporary storage root (the database is shared for the
whole session, so rows and the template are restored at teardown).
"""

import zipfile
from pathlib import Path
from typing import List, Optional

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import archive_service
import asset_service
import db
import library_scan
import seams
import settings_service
import zip_service
from db import engine
from main import app
from migrations import run_migrations
from models import Asset, Folder
from schemas.imports import ImportZipExtractRequest

# Directories the library owns for itself: they hold derived or in-flight bytes,
# never a user's folder, so the Files tree neither shows them nor should.
SYSTEM_DIRS = {"thumbs", "bundles", ".incoming", ".trash"}

ELECROW = "elecrow-7-inch-screen-console-model_files"
ELECROW_BASENAMES = ["body-v201.stl", "body-v202.stl", "screen-bracket.stl", "stand.stl"]


# --- infrastructure ---------------------------------------------------------


def auth_header(client: TestClient) -> dict:
    """Bearer token, or empty when the app is running with auth disabled."""
    health = client.get("/health").json()
    if not health.get("auth_required"):
        return {}
    token = client.post("/login", json={"username": "admin", "password": "super-secret"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def registered_dirs(session: Session) -> set:
    """Every directory a folder row claims."""
    return {folder.storage_path for folder in session.exec(select(Folder)) if folder.storage_path}


def unseeable_directories(root: Path) -> List[str]:
    """Storage-relative directories on disk that no folder row names.

    Sorted, so a failure prints the offenders in a stable order. A path whose
    first segment is a system directory is skipped together with everything
    under it.
    """
    with Session(engine) as session:
        registered = registered_dirs(session)
    offenders: List[str] = []
    for path in root.rglob("*"):
        if not path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.split("/")[0] in SYSTEM_DIRS:
            continue
        if relative not in registered:
            offenders.append(relative)
    return sorted(offenders)


def create_folder(client: TestClient, headers: dict, name: str, parent_id: Optional[str] = None) -> dict:
    response = client.post("/folders", json={"name": name, "parent_id": parent_id}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def upload(client: TestClient, headers: dict, filename: str, folder_id: Optional[str] = None) -> dict:
    response = client.post(
        "/upload",
        files={"file": (filename, b"solid payload\n", "model/stl")},
        data={"folder_id": folder_id} if folder_id else None,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def read_asset(asset_id: str) -> Asset:
    with Session(engine) as session:
        asset = session.get(Asset, asset_id)
        assert asset is not None
        session.expunge(asset)
        return asset


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every storage binding at a temp root and undo every row made here."""
    storage = tmp_path / "storage"
    (storage / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(db, "STORAGE", storage)
    monkeypatch.setattr(db, "THUMBS", storage / "thumbs")
    # These modules bind STORAGE at import time, so their globals are patched too.
    monkeypatch.setattr(asset_service, "STORAGE", storage)
    monkeypatch.setattr(library_scan, "STORAGE", storage)
    run_migrations(engine)
    template_before = settings_service.get_storage_template()
    with Session(engine) as session:
        folders_before = {folder.id for folder in session.exec(select(Folder))}
        assets_before = {asset.id for asset in session.exec(select(Asset))}
    yield storage
    settings_service.set_storage_template(template_before)
    with Session(engine) as session:
        for asset in list(session.exec(select(Asset))):
            if asset.id not in assets_before:
                session.delete(asset)
        for folder in list(session.exec(select(Folder))):
            if folder.id not in folders_before:
                session.delete(folder)
        session.commit()


# --- the ordinary upload door ----------------------------------------------


def test_a_plain_upload_creates_no_directory_the_tree_cannot_show(isolated_storage: Path) -> None:
    with TestClient(app) as client:
        headers = auth_header(client)
        folder = create_folder(client, headers, "Props")
        for basename in ELECROW_BASENAMES:
            asset = upload(client, headers, basename, folder["id"])
            assert asset["storage_path"] == f"Props/{basename}"

    assert unseeable_directories(isolated_storage) == []


def test_an_elecrow_folder_upload_lands_flat_in_one_directory(isolated_storage: Path) -> None:
    """A browser sending bare basenames puts every file directly in the folder."""
    with TestClient(app) as client:
        headers = auth_header(client)
        folder = create_folder(client, headers, ELECROW)
        for basename in ELECROW_BASENAMES:
            asset = upload(client, headers, basename, folder["id"])
            assert asset["storage_path"] == f"{ELECROW}/{basename}"

    directory = isolated_storage / ELECROW
    assert directory.is_dir()
    assert [path for path in directory.rglob("*") if path.is_dir()] == []
    # Exactly one directory, the folder's own: no per-file directories beside it.
    assert [path.name for path in isolated_storage.iterdir() if path.is_dir() and path.name not in SYSTEM_DIRS] == [ELECROW]
    assert unseeable_directories(isolated_storage) == []


def test_a_path_carrying_upload_creates_only_visible_directories(isolated_storage: Path) -> None:
    """The 2026-11-24 shape: the browser sends the folder prefix in the filename."""
    with TestClient(app) as client:
        headers = auth_header(client)
        folder = create_folder(client, headers, ELECROW)
        for basename in ELECROW_BASENAMES:
            asset = upload(client, headers, f"{ELECROW}/{basename}", folder["id"])
            assert asset["storage_path"] == f"{ELECROW}/{basename}"

    assert unseeable_directories(isolated_storage) == []


def test_a_deduped_folder_writes_where_its_row_points(isolated_storage: Path) -> None:
    """`Foo (2)`'s directory leaf is the row's path, not the row's name.

    The old renderer walked folder *names*, so a new file in the second `Foo`
    was written into `Foo/` - the first folder's directory, described by no row.
    """
    with TestClient(app) as client:
        headers = auth_header(client)
        first = create_folder(client, headers, "Foo")
        second = create_folder(client, headers, "Foo")
        assert first["storage_path"] == "Foo"
        assert second["storage_path"] == "Foo (2)"

        asset = upload(client, headers, "cube.stl", second["id"])

    assert asset["storage_path"] == "Foo (2)/cube.stl"
    assert (isolated_storage / "Foo (2)" / "cube.stl").is_file()
    assert unseeable_directories(isolated_storage) == []


def test_an_ancestor_name_in_the_upload_path_is_not_silently_shortened(isolated_storage: Path) -> None:
    """Only the base folder may be dropped from an incoming path, never an ancestor.

    The drop is bounded to the base's own identity. A leading segment that names
    an *ancestor* of the base (here ``Parts`` above ``Parts/elecrow``) is a real
    child folder the sender asked for, and dropping it would be the silent
    rename this ticket exists to kill.
    """
    with TestClient(app) as client:
        headers = auth_header(client)
        parts = create_folder(client, headers, "Parts")
        elecrow = create_folder(client, headers, "elecrow", parts["id"])
        assert elecrow["storage_path"] == "Parts/elecrow"

        lone_ancestor = upload(client, headers, "Parts/x.stl", elecrow["id"])
        base_by_row_name = upload(client, headers, "elecrow/z.stl", elecrow["id"])
        base_by_full_path = upload(client, headers, "Parts/elecrow/y.stl", elecrow["id"])

    # The new pin: this used to land at 'Parts/elecrow/x.stl'.
    assert lone_ancestor["storage_path"] == "Parts/elecrow/Parts/x.stl"
    assert base_by_row_name["storage_path"] == "Parts/elecrow/z.stl"
    assert base_by_full_path["storage_path"] == "Parts/elecrow/y.stl"
    assert unseeable_directories(isolated_storage) == []


def test_a_nested_deduped_leaf_can_be_spelled_either_way(isolated_storage: Path) -> None:
    """One segment, not the whole chain, is what the deduped-leaf spelling drops.

    The base here is the second ``Foo``: row name ``Foo``, directory leaf
    ``Foo (2)``. Both spellings name the base itself, so exactly one leading
    segment goes; the file still lands inside ``Foo (2)``.
    """
    with TestClient(app) as client:
        headers = auth_header(client)
        outer = create_folder(client, headers, "Foo")
        first = create_folder(client, headers, "Foo", outer["id"])
        base = create_folder(client, headers, "Foo", outer["id"])
        assert first["storage_path"] == "Foo/Foo"
        assert base["storage_path"] == "Foo/Foo (2)"

        by_row_name = upload(client, headers, "Foo/a.stl", base["id"])
        by_deduped_leaf = upload(client, headers, "Foo (2)/b.stl", base["id"])

    assert by_row_name["storage_path"] == "Foo/Foo (2)/a.stl"
    assert by_deduped_leaf["storage_path"] == "Foo/Foo (2)/b.stl"
    assert unseeable_directories(isolated_storage) == []


# --- the linked-zip import door --------------------------------------------


def write_zip(path: Path, members: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return path


def test_the_zip_door_registers_every_directory_it_writes(isolated_storage: Path, tmp_path: Path) -> None:
    """`POST /import/zip` needs a URL, so this drives the function the route calls.

    That function is the whole of the door's behaviour - bytes, folder rows,
    asset rows - so the measurement is the same one the route would produce.
    """
    zip_path = write_zip(
        tmp_path / "elecrow.zip",
        {
            "elecrow-files/body-v201.stl": "solid body\n",
            "elecrow-files/sub/x.stl": "solid x\n",
            "__MACOSX/._junk": "junk\n",
        },
    )
    body = ImportZipExtractRequest(url="https://example.invalid/elecrow.zip", entries=[])

    assets, failed = zip_service.extract_zip_entries_to_assets(zip_path, None, body)

    assert [asset.storage_path for asset in assets] == ["elecrow-files/body-v201.stl", "elecrow-files/sub/x.stl"]
    assert failed == ["__MACOSX/._junk"]
    assert unseeable_directories(isolated_storage) == []


# --- the in-library archive extract door -----------------------------------


def test_the_archive_extract_door_becomes_visible_after_a_scan(
    isolated_storage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This door is two-step by contract: extract writes, the scan registers.

    The scan is run here by the test rather than by `seams.start_scan`, so the
    window it documents is measurable instead of a race.
    """
    storage = isolated_storage
    monkeypatch.setattr(seams, "start_scan", lambda: False)
    write_zip(
        storage / "elecrow.zip",
        {"body-v201.stl": "solid body\n", "sub/x.stl": "solid x\n"},
    )

    outcome = archive_service.extract_out("elecrow.zip", None, None)

    assert outcome.destination == "elecrow"
    assert outcome.files == 2
    assert outcome.scan_started is False
    # Before the scan the twin tree is on disk and nothing owns it.
    assert unseeable_directories(storage) == ["elecrow", "elecrow/sub"]

    library_scan.scan_storage_root()

    assert unseeable_directories(storage) == []
