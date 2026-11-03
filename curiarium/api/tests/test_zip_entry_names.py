"""Extracted zip entries keep their own names; the archive title names the project.

``extract_zip_entries_to_assets`` used to hand the request's single ``body.title``
to every entry's ``create_asset_record``, so `part.stl`, `a/b/nested.stl` and
`a/b/deep/inner.stl` all collapsed into three assets named after the archive.
Each entry now keeps its own filename stem, verbatim - vendor stems such as
`moai-Small-NO_OVERHANG` and `twoColor1_2_7_10_53` are the accepted result, not a
prettifying bug - and the title names a new project only, never a file, whether
the archive holds one entry or many.

Storage and the database are shared for the whole session, so every test works in
a temporary storage root and removes the rows it created.
"""

from pathlib import Path
from typing import List, Optional, Tuple
from zipfile import ZipFile

import pytest
from sqlmodel import Session, select

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
from models import Asset, AssetFile, Folder, Project, ProjectAsset

STL_BYTES = b"solid cube\nendsolid cube\n"


# --- infrastructure ---------------------------------------------------------


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


def make_zip(tmp_path: Path, names: List[str]) -> Path:
    path = tmp_path / "archive.zip"
    with ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, STL_BYTES)
    return path


def extract_request(entries: List[str], title: Optional[str] = None) -> schemas.ImportZipExtractRequest:
    return schemas.ImportZipExtractRequest(url="upload://archive.zip", entries=entries, title=title)


def extract_entries(tmp_path: Path, entries: List[str], title: Optional[str] = None) -> Tuple[List[Asset], List[str]]:
    """Extract every entry of a fresh archive, as the import route calls the door."""
    zip_path = make_zip(tmp_path, entries)
    return zip_service.extract_zip_entries_to_assets(zip_path, entries, extract_request(entries, title=title))


def folder_path_of(asset: Asset) -> str:
    """The asset's folder as a slash path, `Unassigned` for a root asset."""
    with Session(engine) as session:
        return "/".join(asset_service.folder_segments(session, asset.folder_id))


# --- per-entry names --------------------------------------------------------


def test_entries_keep_their_own_names_with_a_title_set(tmp_path: Path) -> None:
    """The regression itself: one title, three entries, three different names."""
    assets, failed = extract_entries(
        tmp_path, ["part.stl", "a/b/nested.stl", "a/b/deep/inner.stl"], title="T nested"
    )

    assert failed == []
    assert len(assets) == 3
    assert {asset.name for asset in assets} == {"part", "nested", "inner"}
    assert {asset.filename for asset in assets} == {"part.stl", "nested.stl", "inner.stl"}
    assert all(asset.title == asset.name for asset in assets)
    assert {asset.name: folder_path_of(asset) for asset in assets} == {
        "part": "Unassigned",
        "nested": "a/b",
        "inner": "a/b/deep",
    }


def test_vendor_stems_are_kept_verbatim(tmp_path: Path) -> None:
    """No title-casing and no prettifying: the archive's own stems are the names."""
    entries = [
        "files/moai-Small-NO_OVERHANG.stl",
        "files/moai-Medium-NO_OVERHANG.stl",
        "files/moai-Big-NO_OVERHANG.stl",
    ]

    assets, failed = extract_entries(tmp_path, entries, title="Moai statue -No overhang")

    assert failed == []
    assert {asset.name for asset in assets} == {
        "moai-Small-NO_OVERHANG",
        "moai-Medium-NO_OVERHANG",
        "moai-Big-NO_OVERHANG",
    }
    assert {folder_path_of(asset) for asset in assets} == {"files"}
    assert all(" " not in asset.filename for asset in assets)


def test_same_basename_in_different_folders_both_import(tmp_path: Path) -> None:
    """The name is unique per folder, not per archive, so both `part`s are real."""
    assets, failed = extract_entries(tmp_path, ["a/part.stl", "b/part.stl"], title="T two folders")

    assert failed == []
    assert {(asset.name, folder_path_of(asset)) for asset in assets} == {("part", "a"), ("part", "b")}
    assert {asset.filename for asset in assets} == {"part.stl"}


def test_a_conflicting_entry_fails_without_aborting_the_selection(tmp_path: Path) -> None:
    """A 409 stays per-entry: the other selected entry is still extracted."""
    # The occupant only has to win the name; the collision is decided on the row.
    asset_service.create_asset_record("taken.stl", "model/stl", None, None, [], None)

    assets, failed = extract_entries(tmp_path, ["taken.stl", "good.stl"], title="T conflict")

    assert failed == ["taken.stl"]
    assert len(assets) == 1
    assert assets[0].name == "good"
    assert assets[0].filename == "good.stl"


def test_a_single_entry_archive_keeps_its_own_name(tmp_path: Path) -> None:
    """There is no "may take the title" case: even one entry keeps its own stem."""
    assets, failed = extract_entries(tmp_path, ["only.stl"], title="T single")

    assert failed == []
    assert len(assets) == 1
    assert assets[0].name == "only"
    assert assets[0].filename == "only.stl"
