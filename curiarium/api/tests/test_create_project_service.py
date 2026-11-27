"""The create-project primitive: one path in, one dedupe, one write (Mf-215n).

`project_create_service.create_project` is the coarse verb for a door whose subject
is a library PATH. These tests pin what it owns and what it refuses to own:

* the member set is every REGISTERED file under the key - models, images,
  documents, unknowns and nested archives - with each member's role read off the
  scan's own classifier. Mutation: reintroduce a suffix table at this seam and the
  document stops being a member;
* an excluded group's files are not members, and the marker says the group is not
  included, because validation and the marker must agree about the same list;
* the dedupe is ONE pass over the identities this verb holds, so a second create
  from the same path is `already_created` carrying the project the first one made.
  Mutation: drop the key leg and a retry writes a second project over one folder;
* it moves no bytes and writes no audit row. The first is a promise to the
  filesystem; the second is a fact about `audit.ACTIONS`, which has no
  `project.create` entry and which this ticket may not add - the door that asked
  audits.

The undo of a create is the OPERATION's, not this module's: `tests/test_operations.py
::test_undo_deletes_the_project_and_never_the_files` is that proof (the project row
is deleted, every member file's digest is unchanged). A primitive that created and
then deleted projects would be a second owner of the operation's undo.
"""

import hashlib
import json
from pathlib import Path
from typing import Dict

import pytest
from sqlmodel import Session, select

import bundle_service
import db
import library_scan
import project_create_service
from db import engine
from migrations import run_migrations
from models import Asset, Project, ProjectAsset

KEY = "import/Flexi Cat"
FILES = {
    "import/Flexi Cat/cat.stl": b"model bytes",
    "import/Flexi Cat/preview.png": b"image bytes",
    "import/Flexi Cat/README.txt": b"read me",
    "import/Flexi Cat/docs/manual.txt": b"the manual",
    "import/Flexi Cat/parts/body.stl": b"body bytes",
}


# --- harness ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp storage root with five registered files, and every row removed after."""
    storage = tmp_path / "storage"
    (storage / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(db, "STORAGE", storage)
    monkeypatch.setattr(db, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(library_scan, "STORAGE", storage)
    run_migrations(engine)
    with Session(engine) as session:
        assets_before = {asset.id for asset in session.exec(select(Asset))}
        projects_before = {project.id for project in session.exec(select(Project))}
    for relative, payload in FILES.items():
        path = storage / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    with Session(engine) as session:
        for relative, payload in FILES.items():
            session.add(
                Asset(
                    name=Path(relative).stem,
                    filename=Path(relative).name,
                    mime="model/stl",
                    size=len(payload),
                    storage_path=relative,
                )
            )
        session.commit()
    yield storage
    with Session(engine) as session:
        for project in list(session.exec(select(Project))):
            if project.id not in projects_before:
                for membership in list(session.exec(select(ProjectAsset).where(ProjectAsset.project_id == project.id))):
                    session.delete(membership)
                session.delete(project)
        for asset in list(session.exec(select(Asset))):
            if asset.id not in assets_before:
                session.delete(asset)
        session.commit()


def tree_digest(root: Path) -> Dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def project_rows() -> list:
    with Session(engine) as session:
        return list(session.exec(select(Project)))


def members_of(project_id: str) -> set:
    with Session(engine) as session:
        return {membership.asset_id for membership in session.exec(select(ProjectAsset).where(ProjectAsset.project_id == project_id))}


def marker_of(project_id: str) -> dict:
    with Session(engine) as session:
        project = session.get(Project, project_id)
    return json.loads(project.metadata_json or "{}").get("_bundle", {})


def asset_id_at(relative: str) -> str:
    with Session(engine) as session:
        asset = session.exec(select(Asset).where(Asset.storage_path == relative)).first()
    assert asset is not None, relative
    return asset.id


# --- idempotency -----------------------------------------------------------


def test_a_second_create_from_one_path_is_already_created(seeded: Path) -> None:
    """AC-2: two creates, one path -> one project row, one membership set, no second write."""
    before_tree = tree_digest(seeded)
    with Session(engine) as session:
        first = bundle_service.create_project(session, key=KEY, name="Flexi Cat")
        first_members = members_of(first.project_id)

    assert first.status == project_create_service.STATUS_CREATED
    assert len(project_rows()) == 1

    with Session(engine) as session:
        second = bundle_service.create_project(session, key=KEY, name="Flexi Cat")

    assert second.status == project_create_service.STATUS_ALREADY_CREATED, (
        "one path is one identity; a retry must answer with the project the first call made"
    )
    assert second.project_id == first.project_id
    assert second.match == project_create_service.MATCH_MARKER, (
        "the marker key is the proof, and it answers before the name is ever compared"
    )
    assert len(project_rows()) == 1, "not a second project over one folder"
    assert members_of(first.project_id) == first_members
    assert tree_digest(seeded) == before_tree


def test_the_key_identity_outranks_a_different_name(seeded: Path) -> None:
    """Mutation: ask the name first and a renamed retry writes a second project."""
    with Session(engine) as session:
        first = bundle_service.create_project(session, key=KEY, name="Flexi Cat")
    with Session(engine) as session:
        second = bundle_service.create_project(session, key=KEY, name="Something else")

    assert second.status == project_create_service.STATUS_ALREADY_CREATED
    assert second.project_id == first.project_id
    assert len(project_rows()) == 1


# --- the member set --------------------------------------------------------


def test_the_member_set_is_every_registered_file_under_the_path(seeded: Path) -> None:
    """Models, images, documents and a nested group's file are all members."""
    with Session(engine) as session:
        outcome = bundle_service.create_project(session, key=KEY, name="Flexi Cat")

    assert outcome.status == project_create_service.STATUS_CREATED
    assert members_of(outcome.project_id) == {asset_id_at(relative) for relative in FILES}


def test_an_excluded_group_is_not_a_member_and_the_marker_says_so(seeded: Path) -> None:
    """Validation and the marker read the same list, so they cannot disagree."""
    docs = asset_id_at("import/Flexi Cat/docs/manual.txt")
    body = asset_id_at("import/Flexi Cat/parts/body.stl")
    with Session(engine) as session:
        outcome = bundle_service.create_project(
            session,
            key=KEY,
            name="Flexi Cat",
            groups=[
                {"name": "docs", "model_count": 0, "foreign": False, "separable": True},
                {"name": "parts", "model_count": 1, "foreign": False, "separable": True},
            ],
            excluded_groups=("docs",),
        )

    assert outcome.status == project_create_service.STATUS_CREATED
    members = members_of(outcome.project_id)
    assert docs not in members
    assert body in members, "an exclusion subtracts one group, never the row"
    groups = {group["name"]: group for group in marker_of(outcome.project_id)["groups"]}
    assert groups["docs"]["included"] is False, (
        "the marker's `included` is what `bundle_drift.excluded_prefixes` subtracts; absent, it never subtracts"
    )
    assert groups["docs"]["attached"] == 0
    assert groups["parts"]["included"] is True
    assert groups["parts"]["attached"] == 1, (
        "the attached count is `bundle_service.attached_by_group`, the same bucketing the create and adopt paths use"
    )


def test_the_marker_names_the_path_it_was_created_from(seeded: Path) -> None:
    """A marker that does not name its key cannot be found by the dedupe, ever."""
    with Session(engine) as session:
        outcome = bundle_service.create_project(session, key=KEY, name="Flexi Cat")

    marker = marker_of(outcome.project_id)
    assert marker["key"] == KEY
    assert marker["rel_dir"] == KEY
    assert marker["origin"] == project_create_service.ORIGIN_DETECTION


# --- what it refuses -------------------------------------------------------


def test_the_library_root_is_a_caller_error(seeded: Path) -> None:
    """`""` is not a path: a create over the whole library is not something to guess at."""
    with Session(engine) as session:
        with pytest.raises(ValueError, match="library-relative path"):
            bundle_service.create_project(session, key="   ", name="Everything")

    assert project_rows() == []


def test_a_path_with_no_registered_files_creates_nothing(seeded: Path) -> None:
    """An empty member set refuses rather than leaving a project with no files."""
    with Session(engine) as session:
        outcome = bundle_service.create_project(session, key="import/Empty Row", name="Empty")

    assert outcome.status == project_create_service.STATUS_REFUSED
    assert outcome.reason == "no member files resolved"
    assert outcome.project_id is None
    assert project_rows() == []


def test_a_caller_supplied_member_set_is_written_verbatim(seeded: Path) -> None:
    """The operations step executes the reviewed set, so a door may hand its own in.

    Mutation: re-resolve from the key when `members` is given, and the runner
    attaches files the operator never approved.
    """
    wanted = [project_create_service.MemberCandidate(asset_id_at("import/Flexi Cat/cat.stl"), "import/Flexi Cat/cat.stl")]
    with Session(engine) as session:
        outcome = bundle_service.create_project(session, key=KEY, name="Just the model", members=wanted)

    assert outcome.status == project_create_service.STATUS_CREATED
    assert members_of(outcome.project_id) == {wanted[0].asset_id}
