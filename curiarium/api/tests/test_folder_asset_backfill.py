"""Asset-evidence folder path backfill (Mf-um7x).

Legacy folders migrated from the flattened v3 database carry
``storage_path IS NULL`` and no scan ever heals them, because healing only
happens for a directory a *newly registered* file touches and every legacy asset
is already known. ``folder_paths.backfill_paths_from_asset_dirs`` derives the
directory from the assets' own ``storage_path`` instead, purely from the ORM.

There is no shared conftest in this suite, so the harness below is deliberately a
copy of ``tests/test_bundle_service.py``'s ``isolated_storage``: it points every
module that binds a path at import time (``db``, ``main``, ``library_scan``,
``asset_service``, ``asset_file_service`` and ``bundle_service``) at a temporary
storage root, runs the migrations, and deletes the rows the test created on
teardown. The unit tests build rows only; the replica tests build a real
Thingiverse-shaped bundle on disk, scan it, and then flatten it into the legacy
shape - the same rows the v3 migration left behind.
"""

import logging
from pathlib import Path
from typing import Dict, List, Mapping, Optional

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import asset_file_service
import asset_service
import audit
import bundle_service
import db
import folder_paths
import library_scan
import main
import settings_service
from db import engine
from migrations import run_migrations
from models import Asset, AssetFile, Folder, Project, ProjectAsset

MOAI = "2493386_Moai_statue_-No_overhang"
README_TEXT = "Moai statue -No overhang\nCreated by Thingiverse user moai_maker\nLicensed under CC BY-NC\n"
LICENSE_TEXT = "Moai statue -No overhang by moai_maker on Thingiverse\n"
STL_BYTES = b"solid cube\nendsolid cube\n"
LEGACY_LABEL = "2-color_standing_cat_62536_files_twoColor1_2_7_10_53"


# --- infrastructure ---------------------------------------------------------


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
    template_before = settings_service.get_storage_template()
    with Session(engine) as session:
        projects_before = {project.id for project in session.exec(select(Project))}
        folders_before = {folder.id for folder in session.exec(select(Folder))}
        assets_before = {asset.id for asset in session.exec(select(Asset))}
    yield storage
    settings_service.set_storage_template(template_before)
    settings_service.set_setting(settings_service.BUNDLE_DISMISSALS_KEY, "")
    settings_service.set_setting(settings_service.BUNDLE_ENRICHMENTS_KEY, "")
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


def filesystem_snapshot(root: Path) -> tuple:
    """Paths and sizes of the whole tree; enough to prove nothing moved."""
    return tuple(
        sorted(
            (str(path.relative_to(root)) or ".", path.is_dir(), path.stat().st_size)
            for path in [root, *root.rglob("*")]
        )
    )


def storage_state(root: Path) -> Dict[str, tuple]:
    """Path -> kind/content map, so byte-level changes are directly comparable."""
    state: Dict[str, tuple] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            state[relative] = ("link", path.readlink().as_posix())
        elif path.is_dir():
            state[relative] = ("dir", None)
        else:
            state[relative] = ("file", path.read_bytes())
    return state


def write_file(path: Path, payload: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def moai_bundle(root: Path, name: str = MOAI) -> Path:
    """The Thingiverse README/LICENSE bundle: 5 previews, 3 models, no zip."""
    bundle = root / name
    write_text(bundle / "README.txt", README_TEXT)
    write_text(bundle / "LICENSE.txt", LICENSE_TEXT)
    for index, size in enumerate((100, 400, 200, 900, 300)):
        write_file(bundle / "images" / f"{index}.jpg", b"j" * size)
    for index in range(3):
        write_file(bundle / "files" / f"part{index}.stl", STL_BYTES)
    return bundle


def insert_folder_row(
    name: str, parent_id: Optional[str] = None, storage_path: Optional[str] = None
) -> str:
    folder = Folder(name=name, tags_json="[]", parent_id=parent_id, storage_path=storage_path)
    with Session(engine) as session:
        session.add(folder)
        session.commit()
        session.refresh(folder)
        return folder.id


def insert_asset_row(
    folder_id: Optional[str],
    storage_path: Optional[str],
    *,
    is_reference: int = 0,
    missing_since: Optional[str] = None,
) -> str:
    filename = (storage_path or "asset.stl").rsplit("/", 1)[-1]
    asset = Asset(
        name=Path(filename).stem,
        filename=filename,
        mime="model/stl",
        size=10,
        folder_id=folder_id,
        storage_path=storage_path,
        is_reference=is_reference,
        missing_since=missing_since,
    )
    with Session(engine) as session:
        session.add(asset)
        session.commit()
        session.refresh(asset)
        return asset.id


def stored_path(folder_id: str) -> Optional[str]:
    with Session(engine) as session:
        folder = session.get(Folder, folder_id)
        assert folder is not None
        return folder.storage_path


def backfill() -> Dict[str, str]:
    with Session(engine) as session:
        return folder_paths.backfill_paths_from_asset_dirs(session)


def scan() -> library_scan.ScanState:
    state = library_scan.scan_storage_root()
    assert state.failed == 0, state.errors
    return state


def audit_ctx() -> audit.AuditContext:
    return audit.AuditContext(
        actor=audit.Actor(username="admin", role="admin", kind="session"),
        request_id="test-request",
        method="POST",
        path="/bundles/create",
    )


def auth_header(client: TestClient) -> dict:
    """Bearer token, or empty when the app is running with auth disabled."""
    health = client.get("/health").json()
    if not health.get("auth_required"):
        return {}
    token = client.post("/login", json={"username": "admin", "password": "super-secret"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def row_for(report: Mapping, key: str) -> dict:
    matches = [row for row in report["bundles"] if row["key"] == key]
    assert len(matches) == 1, f"expected exactly one row for {key}: {report['bundles']}"
    return matches[0]


def folder_snapshot() -> List[tuple]:
    with Session(engine) as session:
        return sorted(
            (folder.id, folder.name, folder.parent_id, folder.storage_path)
            for folder in session.exec(select(Folder))
        )


def table_snapshot() -> tuple:
    with Session(engine) as session:
        folders = sorted(
            (folder.id, folder.name, folder.storage_path) for folder in session.exec(select(Folder))
        )
        assets = sorted(
            (asset.id, asset.folder_id, asset.storage_path) for asset in session.exec(select(Asset))
        )
    return folders, assets


def flatten_to_legacy(storage: Path, relative: str, label: str) -> str:
    """Turn the folder owning ``relative`` into the migrated v3 shape.

    Every folder row under the bundle loses its path, the owning row is renamed to
    a flattened label and reparented to root, and each asset keeps pointing at its
    row. That is exactly what migration from the old schema left behind.
    """
    prefix = relative.split("/", 1)[0].casefold() + "/"
    with Session(engine) as session:
        folders = list(session.exec(select(Folder)))
        target = next(
            (
                folder
                for folder in folders
                if folder.storage_path and folder.storage_path.casefold() == relative.casefold()
            ),
            None,
        )
        assert target is not None, f"no folder row owns {relative}"
        for folder in folders:
            if folder.storage_path and folder.storage_path.casefold().startswith(prefix):
                folder.storage_path = None
                session.add(folder)
        target.name = label
        target.parent_id = None
        session.add(target)
        session.commit()
        return target.id


# --- unit cases: derivation only, no filesystem -----------------------------


def test_children_sharing_one_directory_are_linked(isolated_storage: Path) -> None:
    folder_id = insert_folder_row(LEGACY_LABEL)
    insert_asset_row(folder_id, "Unsorted/coll/files/niche/one.stl")
    insert_asset_row(folder_id, "Unsorted/coll/files/niche/two.stl")

    assert backfill() == {folder_id: "Unsorted/coll/files/niche"}
    assert stored_path(folder_id) == "Unsorted/coll/files/niche"


def test_two_directories_stays_unlinked(
    isolated_storage: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder_id = insert_folder_row(LEGACY_LABEL)
    insert_asset_row(folder_id, "Unsorted/coll/files/one/part.stl")
    insert_asset_row(folder_id, "Unsorted/coll/files/two/other.stl")

    with caplog.at_level(logging.DEBUG, logger=folder_paths.log.name):
        assert backfill() == {}

    assert stored_path(folder_id) is None
    assert any(folder_id in record.getMessage() and "ambiguous_directories" in record.getMessage()
               for record in caplog.records)


def test_a_root_level_asset_makes_the_folder_ambiguous(
    isolated_storage: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The root is a directory value, so root + subdirectory is two directories."""
    folder_id = insert_folder_row(LEGACY_LABEL)
    insert_asset_row(folder_id, "loose.stl")
    insert_asset_row(folder_id, "dir/part.stl")

    with caplog.at_level(logging.DEBUG, logger=folder_paths.log.name):
        assert backfill() == {}

    assert stored_path(folder_id) is None
    assert any(folder_id in record.getMessage() and "ambiguous_directories" in record.getMessage()
               for record in caplog.records)


def test_a_folder_with_only_root_level_assets_stays_unlinked(
    isolated_storage: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Root scope is not a folder path, so a root-only folder has no directory."""
    folder_id = insert_folder_row(LEGACY_LABEL)
    insert_asset_row(folder_id, "loose.stl")

    with caplog.at_level(logging.DEBUG, logger=folder_paths.log.name):
        assert backfill() == {}

    assert stored_path(folder_id) is None
    assert any(folder_id in record.getMessage() and "root_scope" in record.getMessage()
               for record in caplog.records)


def test_no_child_assets_stays_unlinked(
    isolated_storage: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder_id = insert_folder_row(LEGACY_LABEL)

    with caplog.at_level(logging.DEBUG, logger=folder_paths.log.name):
        assert backfill() == {}

    assert stored_path(folder_id) is None
    assert any(folder_id in record.getMessage() and "no_live_asset" in record.getMessage()
               for record in caplog.records)


def test_already_claimed_path_stays_unlinked(
    isolated_storage: Path, caplog: pytest.LogCaptureFixture
) -> None:
    claimed_id = insert_folder_row("Claimed", storage_path="Taken")
    folder_id = insert_folder_row(LEGACY_LABEL)
    insert_asset_row(folder_id, "Taken/part.stl")

    with caplog.at_level(logging.DEBUG, logger=folder_paths.log.name):
        assert backfill() == {}

    assert stored_path(folder_id) is None
    assert stored_path(claimed_id) == "Taken"
    assert any(folder_id in record.getMessage() and "path_claimed" in record.getMessage()
               for record in caplog.records)


def test_two_folders_deriving_one_directory_both_stay_unlinked(
    isolated_storage: Path, caplog: pytest.LogCaptureFixture
) -> None:
    first_id = insert_folder_row("First")
    second_id = insert_folder_row("Second")
    insert_asset_row(first_id, "Shared/dir/a.stl")
    insert_asset_row(second_id, "Shared/dir/b.stl")

    with caplog.at_level(logging.DEBUG, logger=folder_paths.log.name):
        assert backfill() == {}

    assert stored_path(first_id) is None
    assert stored_path(second_id) is None
    for folder_id in (first_id, second_id):
        assert any(
            folder_id in record.getMessage() and "shared_directory" in record.getMessage()
            for record in caplog.records
        ), "both rows must be accounted for, not just one deterministic loser"


def test_a_directory_holding_another_folders_asset_is_not_claimed(
    isolated_storage: Path, caplog: pytest.LogCaptureFixture
) -> None:
    other_id = insert_folder_row("Other", storage_path="Other")
    folder_id = insert_folder_row(LEGACY_LABEL)
    insert_asset_row(folder_id, "Dir/a.stl")
    insert_asset_row(other_id, "Dir/b.stl")

    with caplog.at_level(logging.DEBUG, logger=folder_paths.log.name):
        assert backfill() == {}

    assert stored_path(folder_id) is None
    assert any(
        folder_id in record.getMessage() and "directory_shared_with_other_folder" in record.getMessage()
        for record in caplog.records
    )


def test_a_directory_containing_another_folders_path_is_not_claimed(
    isolated_storage: Path, caplog: pytest.LogCaptureFixture
) -> None:
    nested_id = insert_folder_row("Nested", storage_path="Big/dir/nested")
    folder_id = insert_folder_row(LEGACY_LABEL)
    insert_asset_row(folder_id, "Big/dir/part.stl")

    with caplog.at_level(logging.DEBUG, logger=folder_paths.log.name):
        assert backfill() == {}

    assert stored_path(folder_id) is None
    assert stored_path(nested_id) == "Big/dir/nested"
    assert any(
        folder_id in record.getMessage() and "contains_foreign_path" in record.getMessage()
        for record in caplog.records
    )


def test_a_child_of_an_unlinked_parent_stays_unlinked(
    isolated_storage: Path, caplog: pytest.LogCaptureFixture
) -> None:
    parent_id = insert_folder_row("Parent")
    child_id = insert_folder_row(LEGACY_LABEL, parent_id=parent_id)
    insert_asset_row(child_id, "Parent/Child/part.stl")

    with caplog.at_level(logging.DEBUG, logger=folder_paths.log.name):
        assert backfill() == {}

    assert stored_path(parent_id) is None
    assert stored_path(child_id) is None
    assert any(
        child_id in record.getMessage() and "parent_unlinked" in record.getMessage()
        for record in caplog.records
    )


def test_a_child_path_must_live_under_its_parents_path(
    isolated_storage: Path, caplog: pytest.LogCaptureFixture
) -> None:
    parent_id = insert_folder_row("Parent", storage_path="Parent")
    child_id = insert_folder_row(LEGACY_LABEL, parent_id=parent_id)
    insert_asset_row(child_id, "Elsewhere/Child/part.stl")

    with caplog.at_level(logging.DEBUG, logger=folder_paths.log.name):
        assert backfill() == {}

    assert stored_path(child_id) is None
    assert any(
        child_id in record.getMessage() and "not_under_parent" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize(
    ("storage_path", "reason"),
    [
        ("/abs/dir/part.stl", "invalid_directory"),
        ("../escape/part.stl", "invalid_directory"),
        ("a/../b/part.stl", "invalid_directory"),
        ("a//b/part.stl", "invalid_directory"),
        ("a\\b/part.stl", "invalid_directory"),
        ("part.stl", "root_scope"),
    ],
)
def test_absolute_and_parent_relative_paths_are_refused(
    isolated_storage: Path,
    caplog: pytest.LogCaptureFixture,
    storage_path: str,
    reason: str,
) -> None:
    folder_id = insert_folder_row(LEGACY_LABEL)
    insert_asset_row(folder_id, storage_path)

    with caplog.at_level(logging.DEBUG, logger=folder_paths.log.name):
        assert backfill() == {}

    assert stored_path(folder_id) is None
    assert any(
        folder_id in record.getMessage() and reason in record.getMessage()
        for record in caplog.records
    )


def test_reference_and_missing_assets_are_not_evidence(isolated_storage: Path) -> None:
    folder_id = insert_folder_row(LEGACY_LABEL)
    insert_asset_row(folder_id, "Unsorted/coll/ref.stl", is_reference=1)
    insert_asset_row(folder_id, "Unsorted/coll/gone.stl", missing_since="2026-01-01T00:00:00+00:00")

    assert backfill() == {}
    assert stored_path(folder_id) is None


def test_running_the_backfill_twice_writes_nothing_the_second_time(
    isolated_storage: Path,
) -> None:
    storage = isolated_storage
    write_file(storage / "Unsorted/coll/files/niche/part.stl", b"solid cube\n")
    folder_id = insert_folder_row(LEGACY_LABEL)
    insert_asset_row(folder_id, "Unsorted/coll/files/niche/part.stl")

    assert backfill() == {folder_id: "Unsorted/coll/files/niche"}
    before_tables = table_snapshot()
    before_files = filesystem_snapshot(storage)

    assert backfill() == {}

    assert table_snapshot() == before_tables
    assert filesystem_snapshot(storage) == before_files


def test_the_backfill_touches_no_file(isolated_storage: Path) -> None:
    storage = isolated_storage
    write_file(storage / "Unsorted/coll/files/niche/part.stl", b"solid cube\n")
    folder_id = insert_folder_row(LEGACY_LABEL)
    insert_asset_row(folder_id, "Unsorted/coll/files/niche/part.stl")
    before = storage_state(storage)

    assert backfill() == {folder_id: "Unsorted/coll/files/niche"}

    assert storage_state(storage) == before, "the backfill is metadata-only"


# --- replica cases: a real tree flattened into the legacy shape -------------


def test_a_rescan_links_a_legacy_folder_and_its_bundle_becomes_creatable(
    isolated_storage: Path,
) -> None:
    storage = isolated_storage
    bundle_relative = "Unsorted/coll/files/niche"
    moai_bundle(storage, bundle_relative)
    scan()

    leaf_id = flatten_to_legacy(storage, bundle_relative, LEGACY_LABEL)

    blocked = row_for(bundle_service.build_report(), bundle_relative)
    assert blocked["status"] == "blocked"
    assert blocked["blocked_reason"] == "unresolved_directory"
    assert stored_path(leaf_id) is None

    scan()

    assert stored_path(leaf_id) == bundle_relative
    ready = row_for(bundle_service.build_report(), bundle_relative)
    assert ready["status"] == "ready"
    assert ready["blocked_reason"] is None

    created = bundle_service.create_bundles(
        [bundle_service.BundleCreateItem(key=bundle_relative)], audit_ctx()
    )
    assert created["created"] == 1, created
    assert created["blocked"] == 0


def test_an_ambiguous_legacy_folder_stays_unlinked_and_is_visible(
    isolated_storage: Path,
) -> None:
    storage = isolated_storage
    bundle_relative = "Moai"
    write_text(storage / bundle_relative / "Moai_README" / "readme.txt", README_TEXT)
    write_text(storage / bundle_relative / "Moai_LICENSE" / "license.txt", LICENSE_TEXT)
    write_file(storage / bundle_relative / "part.stl", STL_BYTES)
    write_file(storage / bundle_relative / "preview.jpg", b"j" * 20)

    folder_id = insert_folder_row(LEGACY_LABEL)
    insert_asset_row(folder_id, "Moai/Moai_README/readme.txt")
    insert_asset_row(folder_id, "Moai/Moai_LICENSE/license.txt")
    insert_asset_row(None, "Moai/part.stl")
    insert_asset_row(None, "Moai/preview.jpg")

    scan()

    assert stored_path(folder_id) is None
    row = row_for(bundle_service.build_report(), bundle_relative)
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "unresolved_directory"

    with TestClient(main.app) as client:
        response = client.get("/folders", headers=auth_header(client))
        assert response.status_code == 200, response.text
        payload = {entry["id"]: entry for entry in response.json()}
    assert payload[folder_id]["storage_path"] is None, "the badge keys on this field"


def test_a_new_file_in_a_linked_legacy_directory_registers_cleanly(
    isolated_storage: Path,
) -> None:
    storage = isolated_storage
    bundle_relative = "Unsorted/coll/files/niche"
    moai_bundle(storage, bundle_relative)
    scan()
    leaf_id = flatten_to_legacy(storage, bundle_relative, LEGACY_LABEL)
    scan()
    assert stored_path(leaf_id) == bundle_relative

    write_file(storage / bundle_relative / "new_part.stl", STL_BYTES)
    state = scan()

    assert state.failed == 0, state.errors
    with Session(engine) as session:
        owners = [
            folder
            for folder in session.exec(
                select(Folder).where(Folder.storage_path.collate("NOCASE") == bundle_relative)
            )
        ]
        new_asset = session.exec(
            select(Asset).where(Asset.storage_path == f"{bundle_relative}/new_part.stl")
        ).one()
    assert len(owners) == 1, "adoption must reuse the healed row, not insert a duplicate"
    assert owners[0].id == leaf_id
    assert new_asset.folder_id == leaf_id


def test_scanning_twice_leaves_a_linked_legacy_folder_alone(
    isolated_storage: Path,
) -> None:
    storage = isolated_storage
    bundle_relative = "Unsorted/coll/files/niche"
    moai_bundle(storage, bundle_relative)
    scan()
    leaf_id = flatten_to_legacy(storage, bundle_relative, LEGACY_LABEL)
    scan()
    assert stored_path(leaf_id) == bundle_relative

    before = folder_snapshot()
    first = scan()
    second = scan()

    assert (first.registered, first.failed) == (0, 0)
    assert (second.registered, second.failed) == (0, 0)
    assert folder_snapshot() == before


def test_a_name_matched_row_cannot_collide_with_a_linked_legacy_row(
    isolated_storage: Path,
) -> None:
    """A name-matched NULL-path row must not take a directory another row owns.

    The backfill links a migrated legacy row to its directory, so a row that
    name-matches that directory can reach the heal branch with the path already
    taken. Healing it there would raise ``IntegrityError`` outside the per-asset
    savepoints and abort the whole scan, so the heal is skipped instead.
    """
    storage = isolated_storage
    bundle_relative = "Unsorted/coll/files/niche"
    parent_relative = "Unsorted/coll/files"
    moai_bundle(storage, bundle_relative)
    scan()

    # The row standing for the parent directory owns its path only until the
    # bundle is flattened, so capture its id now - this is the row the extra
    # name-matched folder hangs under.
    with Session(engine) as session:
        parent_id = session.exec(
            select(Folder.id).where(
                Folder.storage_path.collate("NOCASE") == parent_relative
            )
        ).one()

    leaf_id = flatten_to_legacy(storage, bundle_relative, LEGACY_LABEL)
    scan()
    assert stored_path(leaf_id) == bundle_relative

    # No assets, so the backfill leaves this row pathless.
    name_matched_id = insert_folder_row("niche", parent_id=parent_id)

    write_file(storage / bundle_relative / "new_part.stl", STL_BYTES)
    state = scan()

    assert state.failed == 0, state.errors
    with Session(engine) as session:
        owners = list(
            session.exec(
                select(Folder).where(
                    Folder.storage_path.collate("NOCASE") == bundle_relative
                )
            )
        )
        new_asset = session.exec(
            select(Asset).where(Asset.storage_path == f"{bundle_relative}/new_part.stl")
        ).one()
    assert len(owners) == 1, "the name-matched row must not take the directory"
    assert owners[0].id == leaf_id
    assert stored_path(leaf_id) == bundle_relative
    assert stored_path(name_matched_id) is None
    assert new_asset.folder_id == name_matched_id
