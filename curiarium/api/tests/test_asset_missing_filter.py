"""Missing-file filtering on the listing and the facets (Mf-1tx3).

A scan flags a row by stamping `missing_since` (a nullable TEXT timestamp) and
clears the stamp when the file reappears. These tests pin the explicit `missing`
flag on `GET /assets`, the two facets and the sidebar counts: unlike the
`unfiled` flag it composes with every other filter, because a missing file can
sit in a folder, in a project and carry tags. Only the listing's own scope is
shared by the facets - an unscoped facet next to a scoped listing is a lying
control.

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
    missing_since: Optional[str] = None,
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
        storage_path=f"Missing/{unique}/{filename}",
        missing_since=missing_since,
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


def database_missing_count() -> int:
    """The flagged-row count, re-derived from the rows themselves."""
    with Session(engine) as session:
        return session.exec(
            select(func.count()).select_from(Asset).where(Asset.missing_since.is_not(None))
        ).one()


def clear_missing_flag(asset_id: str) -> None:
    """Clear the stamp the way the scan does when the file reappears."""
    with Session(engine) as session:
        asset = session.get(Asset, asset_id)
        asset.missing_since = None
        session.add(asset)
        session.commit()


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


# --- missing filter ---------------------------------------------------------


def test_missing_lists_only_flagged_rows(isolated_storage: Path) -> None:
    with TestClient(app) as client:
        headers = auth_header(client)
        probe = f"missingprobe{uuid4().hex[:12]}"

        flagged = insert_asset(f"{probe} flagged", None, missing_since="2024-01-01T00:00:00+00:00")
        present = insert_asset(f"{probe} present", None)

        only_missing = listed(client, headers, "/assets", missing="true", q=probe)
        assert {asset["id"] for asset in only_missing} == {flagged}

        # Without the flag both come back, so the flag is what did it.
        everything = listed(client, headers, "/assets", q=probe)
        assert {asset["id"] for asset in everything} == {flagged, present}


def test_a_cleared_flag_leaves_the_list(isolated_storage: Path) -> None:
    with TestClient(app) as client:
        headers = auth_header(client)
        probe = f"missingprobe{uuid4().hex[:12]}"
        asset_id = insert_asset(f"{probe} returns", None, missing_since="2024-01-01T00:00:00+00:00")

        assert {asset["id"] for asset in listed(client, headers, "/assets", missing="true", q=probe)} == {
            asset_id
        }

        # The "file reappeared" case at the API layer: clearing the stamp takes
        # the row out of the queue without deleting anything.
        clear_missing_flag(asset_id)

        assert listed(client, headers, "/assets", missing="true", q=probe) == []
        assert {asset["id"] for asset in listed(client, headers, "/assets", q=probe)} == {asset_id}


def test_missing_composes_with_every_other_filter(isolated_storage: Path) -> None:
    with TestClient(app) as client:
        headers = auth_header(client)
        probe = f"missingprobe{uuid4().hex[:12]}"
        folder_id = create_folder(client, headers, f"{probe} shelf")

        filed_tag = f"{probe}-filed-tag"
        root_tag = f"{probe}-root-tag"
        present_tag = f"{probe}-present-tag"
        filed = insert_asset(
            f"{probe} filed",
            folder_id,
            creator=f"{probe} Filed",
            tags=[filed_tag],
            missing_since="2024-01-01T00:00:00+00:00",
        )
        root = insert_asset(
            f"{probe} root",
            None,
            creator=f"{probe} Root",
            tags=[root_tag],
            missing_since="2024-01-02T00:00:00+00:00",
        )
        insert_asset(f"{probe} present", folder_id, creator=f"{probe} Present", tags=[present_tag])

        assert {asset["id"] for asset in listed(client, headers, "/assets", missing="true", q=probe)} == {
            filed,
            root,
        }

        # A missing file can sit in a folder, so this pair is not a 400 (unlike
        # unfiled + folder_id, which is a contradiction).
        response = client.get(
            "/assets", params={"missing": "true", "folder_id": folder_id, "q": probe}, headers=headers
        )
        assert response.status_code == 200, response.text
        assert {asset["id"] for asset in response.json()} == {filed}

        # Root mode composes: missing AND unfiled is exactly the flagged root rows.
        assert {asset["id"] for asset in listed(client, headers, "/assets", missing="true", unfiled="true", q=probe)} == {
            root
        }

        # A narrower q still works alongside the flag.
        assert {asset["id"] for asset in listed(client, headers, "/assets", missing="true", q=f"{probe} root")} == {
            root
        }

        # Tags compose too: the present row shares the folder, not the flag.
        assert {asset["id"] for asset in listed(client, headers, "/assets", missing="true", tags=filed_tag)} == {
            filed
        }

        # The facets describe the flagged set, not the whole library: a tag or
        # creator that lives only on a present row must not appear.
        tags = listed(client, headers, "/tags", missing="true", q=probe)
        assert filed_tag in tags
        assert root_tag in tags
        assert present_tag not in tags

        creators = listed(client, headers, "/creators", missing="true", q=probe)
        assert creators == [f"{probe} Filed", f"{probe} Root"]
        assert f"{probe} Present" not in creators


def test_the_missing_count_is_the_definition(isolated_storage: Path) -> None:
    with TestClient(app) as client:
        headers = auth_header(client)
        baseline = counts(client, headers)
        probe = f"missingprobe{uuid4().hex[:12]}"

        insert_asset(f"{probe} one", None, missing_since="2024-01-01T00:00:00+00:00")
        insert_asset(f"{probe} two", None, missing_since="2024-02-01T00:00:00+00:00")
        insert_asset(f"{probe} present", None)

        after = counts(client, headers)
        assert after["total"] == baseline["total"] + 3
        assert after["unfiled"] == baseline["unfiled"] + 3
        assert after["missing"] == baseline["missing"] + 2

        # The same numbers as the rows themselves: `missing` is the rows whose
        # stamp is set, re-derived straight from the database.
        assert (after["total"], after["unfiled"]) == database_counts()
        assert after["missing"] == database_missing_count()

        # The route declares no filters, so every junk parameter - including the
        # new one - is ignored and the labels cannot describe a filtered picture.
        junk = counts(
            client, headers, missing="true", q=probe, unfiled="true", tags=f"{probe}-tag"
        )
        assert junk == after

        # An unrelated call cannot make the number drift.
        assert counts(client, headers)["missing"] == after["missing"]


def test_missing_pages_without_duplicates_or_gaps(isolated_storage: Path) -> None:
    flagged_total = 7  # odd on purpose: the last page is short
    present_total = 4
    with TestClient(app) as client:
        headers = auth_header(client)
        probe = f"missingprobe{uuid4().hex[:12]}"

        flagged_ids = {
            insert_asset(f"{probe} flagged {index:02d}", None, missing_since="2024-01-01T00:00:00+00:00")
            for index in range(flagged_total)
        }
        present_ids = {insert_asset(f"{probe} present {index:02d}", None) for index in range(present_total)}

        seen: list[str] = []
        offset = 0
        pages = 0
        while True:
            response = client.get(
                "/assets",
                params={"missing": "true", "q": probe, "limit": 4, "offset": offset},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            page = response.json()
            pages += 1
            seen.extend(asset["id"] for asset in page)

            has_more = response.headers["X-Has-More"]
            next_offset = int(response.headers["X-Next-Offset"])
            if has_more == "false":
                break
            assert has_more == "true"
            assert next_offset == offset + len(page)
            offset = next_offset

        assert pages == 2
        assert len(seen) == flagged_total
        assert len(set(seen)) == flagged_total, "a page boundary duplicated a row"
        assert set(seen) == flagged_ids, "a page boundary skipped a row"
        assert not (set(seen) & present_ids), "a present row leaked into the missing queue"

        # One unpaged call agrees with the walk.
        unpaged = listed(client, headers, "/assets", missing="true", q=probe)
        assert {asset["id"] for asset in unpaged} == flagged_ids
