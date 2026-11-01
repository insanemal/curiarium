"""Library-root ("unfiled") filtering and the sidebar counts (Mf-nff4).

An absent `folder_id` means "no folder filter", so the API could not express
"assets at the library root" at all. These tests pin the explicit `unfiled` flag
on the listing and both facets, the deliberate 400 when it is combined with
`folder_id`, and `GET /assets/counts`, which must stay whole-library so the
sidebar labels cannot claim a filtered picture.

The suite shares a session-level database, so every listing is narrowed by a
per-test query prefix and every row name carries a uuid. The counts route takes
no filters by design, so its assertions are deltas against a baseline plus a
direct comparison with the rows in the database.
"""

import json
from pathlib import Path
from typing import Optional
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlmodel import Session, select

import asset_file_service
import asset_service
import db
import library_scan
import settings_service
from db import engine
from main import app
from migrations import run_migrations
from models import Asset, Folder

# --- infrastructure ---------------------------------------------------------


def auth_header(client: TestClient) -> dict:
    """Bearer token, or empty when the app is running with auth disabled."""
    health = client.get("/health").json()
    if not health.get("auth_required"):
        return {}
    token = client.post("/login", json={"username": "admin", "password": "super-secret"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def insert_asset(
    name: str,
    folder_id: Optional[str],
    *,
    creator: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> str:
    unique = uuid4().hex
    filename = f"{name}.stl"
    asset = Asset(
        filename=filename,
        mime="model/stl",
        size=10,
        name=name,
        creator=creator,
        tags_json=json.dumps(tags or []),
        folder_id=folder_id,
        storage_path=f"Unfiled/{unique}/{filename}",
    )
    with Session(engine) as session:
        session.add(asset)
        session.commit()
        session.refresh(asset)
        return asset.id


def create_folder(client: TestClient, headers: dict, name: str, parent_id: Optional[str] = None) -> str:
    response = client.post(
        "/folders", json={"name": name, "tags": [], "parent_id": parent_id}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def listed(client: TestClient, headers: dict, path: str, **params) -> list:
    response = client.get(path, params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def counts(client: TestClient, headers: dict, **params) -> dict:
    response = client.get("/assets/counts", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def database_counts() -> tuple[int, int]:
    """The same two numbers, read straight from the shared database."""
    with Session(engine) as session:
        total = session.exec(select(func.count()).select_from(Asset)).one()
        unfiled = session.exec(select(func.count()).select_from(Asset).where(Asset.folder_id.is_(None))).one()
    return total, unfiled


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point storage at a temp root, restore settings and undo every row made."""
    storage = tmp_path / "storage"
    (storage / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(db, "STORAGE", storage)
    monkeypatch.setattr(db, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(library_scan, "STORAGE", storage)
    monkeypatch.setattr(asset_service, "STORAGE", storage)
    monkeypatch.setattr(asset_file_service, "STORAGE", storage)
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


# --- unfiled filter ---------------------------------------------------------


def test_unfiled_lists_only_root_assets_and_scopes_the_facets(isolated_storage: Path) -> None:
    with TestClient(app) as client:
        headers = auth_header(client)
        probe = f"unfiledprobe{uuid4().hex[:12]}"
        parent_id = create_folder(client, headers, f"{probe} parent")
        child_id = create_folder(client, headers, f"{probe} child", parent_id=parent_id)

        root_tag = f"{probe}-root-tag"
        filed_tag = f"{probe}-filed-tag"
        root_one = insert_asset(f"{probe} root one", None, creator=f"{probe} Root", tags=[root_tag])
        root_two = insert_asset(f"{probe} root two", None, creator=f"{probe} Root")
        filed = insert_asset(f"{probe} filed", parent_id, creator=f"{probe} Filed", tags=[filed_tag])
        nested = insert_asset(f"{probe} nested", child_id, creator=f"{probe} Nested", tags=[f"{probe}-nested-tag"])

        found = listed(client, headers, "/assets", unfiled="true", q=probe)
        found_ids = {asset["id"] for asset in found}
        assert found_ids == {root_one, root_two}
        assert filed not in found_ids
        assert nested not in found_ids

        # The facets describe the same root-only pile as the grid: a tag or
        # creator that lives only on a filed (even nested) row must not appear.
        tags = listed(client, headers, "/tags", unfiled="true", q=probe)
        assert root_tag in tags
        assert filed_tag not in tags
        assert f"{probe}-nested-tag" not in tags

        creators = listed(client, headers, "/creators", unfiled="true", q=probe)
        assert creators == [f"{probe} Root"]
        assert f"{probe} Filed" not in creators
        assert f"{probe} Nested" not in creators


def test_absent_filters_list_everything_and_folder_id_stays_direct_children(isolated_storage: Path) -> None:
    with TestClient(app) as client:
        headers = auth_header(client)
        probe = f"unfiledprobe{uuid4().hex[:12]}"
        parent_id = create_folder(client, headers, f"{probe} parent")
        child_id = create_folder(client, headers, f"{probe} child", parent_id=parent_id)

        root = insert_asset(f"{probe} root", None)
        direct = insert_asset(f"{probe} direct", parent_id)
        nested = insert_asset(f"{probe} nested", child_id)

        # No `folder_id` and no `unfiled` is still "every row", root and nested.
        everything = listed(client, headers, "/assets", q=probe)
        assert {asset["id"] for asset in everything} == {root, direct, nested}

        # A folder lists its direct children only: the child folder's asset is a
        # row of its own level, not of its parent's.
        children = listed(client, headers, "/assets", folder_id=parent_id, q=probe)
        assert {asset["id"] for asset in children} == {direct}


def test_unfiled_and_folder_id_are_a_deliberate_400_on_every_scoped_route(isolated_storage: Path) -> None:
    with TestClient(app) as client:
        headers = auth_header(client)
        folder_id = create_folder(client, headers, f"unfiledprobe{uuid4().hex[:12]}")

        for path in ("/assets", "/tags", "/creators"):
            response = client.get(
                path, params={"unfiled": "true", "folder_id": folder_id}, headers=headers
            )
            assert response.status_code == 400, f"{path}: {response.status_code} {response.text}"
            assert response.json()["detail"] == "unfiled and folder_id cannot be combined"


# --- counts -----------------------------------------------------------------


def test_counts_are_whole_library_and_root_and_ignore_every_filter(isolated_storage: Path) -> None:
    with TestClient(app) as client:
        headers = auth_header(client)
        baseline = counts(client, headers)

        probe = f"unfiledprobe{uuid4().hex[:12]}"
        parent_id = create_folder(client, headers, f"{probe} parent")
        child_id = create_folder(client, headers, f"{probe} child", parent_id=parent_id)
        insert_asset(f"{probe} root one", None)
        insert_asset(f"{probe} root two", None)
        insert_asset(f"{probe} filed", parent_id)
        insert_asset(f"{probe} nested", child_id)

        after = counts(client, headers)
        assert after["total"] == baseline["total"] + 4
        assert after["unfiled"] == baseline["unfiled"] + 2

        # The same two numbers as the rows themselves, so the route cannot drift
        # from the definition: total is the whole library, unfiled is the root.
        assert (after["total"], after["unfiled"]) == database_counts()

        # The route declares no filters; junk query parameters are ignored, so
        # a caller cannot make the sidebar labels describe a filtered picture.
        junk = counts(client, headers, q=probe, tags=f"{probe}-tag", folder_id=parent_id, unfiled="true")
        assert junk == after


def test_moving_a_root_asset_into_a_folder_decreases_the_unfiled_count(isolated_storage: Path) -> None:
    with TestClient(app) as client:
        headers = auth_header(client)
        baseline = counts(client, headers)
        probe = f"unfiledprobe{uuid4().hex[:12]}"
        folder_id = create_folder(client, headers, f"{probe} shelf")
        asset_id = insert_asset(f"{probe} root", None)

        before_move = counts(client, headers)
        assert before_move["unfiled"] == baseline["unfiled"] + 1
        assert before_move["total"] == baseline["total"] + 1

        response = client.post(f"/asset/{asset_id}/folder", json={"folder_id": folder_id}, headers=headers)
        assert response.status_code == 200, response.text

        after_move = counts(client, headers)
        assert after_move["unfiled"] == before_move["unfiled"] - 1
        assert after_move["total"] == before_move["total"]
