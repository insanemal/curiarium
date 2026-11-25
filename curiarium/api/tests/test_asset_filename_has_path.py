"""A stored filename carrying a path is a 400 at the doors that rename (F1).

``file_utils.sanitize_filename`` refuses a value carrying a separator instead of
flattening it, which is right for a name the caller just sent. But
``asset_service.filename_for_model_name`` renders the *new* name and appends the
extension it takes from the *stored* one, and ``pathlib`` on POSIX treats a
backslash as an ordinary character: ``Path("thing.stl\\v2").suffix`` is
``".stl\\v2"``. A row whose filename is legal POSIX but carries a backslash
therefore handed the renderer a path it had to refuse, and a bare ``ValueError``
reached the door as a 500 on two routes that answered 200 before. It is the
row's own filename that is wrong, so the door names it and answers 400.

Every test works in a temporary storage root (``asset_service`` binds ``STORAGE``
at import time, so its global is patched too); the database is shared for the
whole test session, so rows and the storage template are restored at teardown.
"""

from pathlib import Path
from typing import Optional

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import asset_service
import db
import library_scan
import settings_service
from db import engine
from main import app
from migrations import run_migrations
from models import Asset, Folder

# The poisoned value as a POSIX filename sees it: a backslash is not a separator.
BACKSLASH_FILENAME = "thing.stl\\v2"


# --- infrastructure ---------------------------------------------------------


def auth_header(client: TestClient) -> dict:
    """Bearer token, or empty when the app is running with auth disabled."""
    health = client.get("/health").json()
    if not health.get("auth_required"):
        return {}
    token = client.post("/login", json={"username": "admin", "password": "super-secret"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def write_placeholder_file(path: Path, payload: bytes = b"placeholder") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def insert_asset_row(name: str, folder_id: Optional[str], storage_path: Optional[str], *, filename: str) -> str:
    asset = Asset(
        filename=filename,
        mime="model/stl",
        size=10,
        name=name,
        folder_id=folder_id,
        storage_path=storage_path,
    )
    with Session(engine) as session:
        session.add(asset)
        session.commit()
        session.refresh(asset)
        return asset.id


def read_asset(asset_id: str) -> Asset:
    with Session(engine) as session:
        asset = session.get(Asset, asset_id)
        assert asset is not None
        session.expunge(asset)
        return asset


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point storage at a temp root, restore the template and undo every row made."""
    storage = tmp_path / "storage"
    (storage / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(db, "STORAGE", storage)
    monkeypatch.setattr(db, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(library_scan, "STORAGE", storage)
    monkeypatch.setattr(asset_service, "STORAGE", storage)
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


# --- the two rename doors ---------------------------------------------------


def test_rename_refuses_a_row_whose_own_filename_carries_a_path(isolated_storage: Path) -> None:
    """The stored value is what is wrong, so the 400 names it, not the new name."""
    storage = isolated_storage
    asset_id = insert_asset_row("Backslashed model", None, f"Somewhere/{BACKSLASH_FILENAME}", filename=BACKSLASH_FILENAME)
    write_placeholder_file(storage / "Somewhere" / BACKSLASH_FILENAME, b"stl bytes")

    # raise_server_exceptions=False turns a regression into a 500 instead of a raise.
    with TestClient(app, raise_server_exceptions=False) as client:
        headers = auth_header(client)
        response = client.post(f"/asset/{asset_id}/rename", json={"filename": "New Name.stl"}, headers=headers)

    assert response.status_code == 400, response.text
    # `!r` is deliberate: it is what makes the backslash unambiguous in the body.
    assert repr(BACKSLASH_FILENAME) in response.json()["detail"]
    assert read_asset(asset_id).filename == BACKSLASH_FILENAME
    assert (storage / "Somewhere" / BACKSLASH_FILENAME).read_bytes() == b"stl bytes"


def test_meta_rename_refuses_a_row_whose_own_filename_carries_a_path(isolated_storage: Path) -> None:
    storage = isolated_storage
    asset_id = insert_asset_row("Backslashed model", None, f"Somewhere/{BACKSLASH_FILENAME}", filename=BACKSLASH_FILENAME)
    write_placeholder_file(storage / "Somewhere" / BACKSLASH_FILENAME, b"stl bytes")

    with TestClient(app, raise_server_exceptions=False) as client:
        headers = auth_header(client)
        response = client.post(f"/asset/{asset_id}/meta", json={"name": "Another Name"}, headers=headers)

    assert response.status_code == 400, response.text
    # `!r` is deliberate: it is what makes the backslash unambiguous in the body.
    assert repr(BACKSLASH_FILENAME) in response.json()["detail"]
    assert read_asset(asset_id).filename == BACKSLASH_FILENAME
    assert (storage / "Somewhere" / BACKSLASH_FILENAME).read_bytes() == b"stl bytes"


def test_a_healthy_row_still_renames_on_both_doors(isolated_storage: Path) -> None:
    """The control: the same two doors answer 200 when the stored name is clean."""
    storage = isolated_storage
    asset_id = insert_asset_row("Good model", None, "Somewhere/good.stl", filename="good.stl")
    write_placeholder_file(storage / "Somewhere/good.stl", b"good bytes")

    with TestClient(app, raise_server_exceptions=False) as client:
        headers = auth_header(client)
        renamed = client.post(f"/asset/{asset_id}/rename", json={"filename": "Renamed.stl"}, headers=headers)
        assert renamed.status_code == 200, renamed.text

        edited = client.post(f"/asset/{asset_id}/meta", json={"name": "Meta renamed"}, headers=headers)
        assert edited.status_code == 200, edited.text

    assert read_asset(asset_id).filename == "Meta renamed.stl"
    assert (storage / "Somewhere" / "Meta renamed.stl").read_bytes() == b"good bytes"
