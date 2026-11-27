"""The membership delta: adopt adds only what a project is missing, undo takes back only that.

`adopt_service` is the one owner of "what does this project hold now", so these
tests drive it directly, without a route in front of it: the four primitives are
the missing-only insert, the group measurement (returned, never written), the
ledger entry, and the undo of one recorded entry.

The harness is a copy of ``tests/test_arrange_undo.py``'s: a temp storage root, a
migrated database, and a teardown that removes every row the test created, because
the database and the engine are session-wide.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlmodel import Session, select

import adopt_service
import db
import library_scan
import project_service
import proposal_engine
from db import engine
from migrations import run_migrations
from models import Asset, AssetFile, Folder, Project, ProjectAsset

KEY = "import/17_ClockworkMonster"
STAMP = "2026-01-01T00:00:00+00:00"
STL_BYTES = b"solid cube\nendsolid cube\n"


# --- harness ----------------------------------------------------------------


@dataclass(frozen=True)
class Vault:
    """The temp storage root and the ids that predate the test."""

    root: Path
    projects: frozenset
    folders: frozenset
    assets: frozenset
    memberships: frozenset

    def digest(self) -> list[tuple[str, str]]:
        """Every file's relative path and sha256, in one sorted snapshot."""
        return sorted(
            (path.relative_to(self.root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
            for path in self.root.rglob("*")
            if path.is_file()
        )

    def project_rows(self) -> int:
        with Session(engine) as session:
            return len(session.exec(select(Project)).all())


@pytest.fixture()
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Vault:
    """Point the path-bound modules at a temp root and undo the rows a test made."""
    storage = tmp_path / "storage"
    (storage / "thumbs").mkdir(parents=True)
    monkeypatch.setattr(db, "STORAGE", storage)
    monkeypatch.setattr(db, "THUMBS", storage / "thumbs")
    monkeypatch.setattr(library_scan, "STORAGE", storage)
    run_migrations(engine)
    with Session(engine) as session:
        state = Vault(
            root=storage,
            projects=frozenset(project.id for project in session.exec(select(Project))),
            folders=frozenset(folder.id for folder in session.exec(select(Folder))),
            assets=frozenset(asset.id for asset in session.exec(select(Asset))),
            memberships=frozenset((row.project_id, row.asset_id) for row in session.exec(select(ProjectAsset))),
        )
    yield state
    with Session(engine) as session:
        for membership in list(session.exec(select(ProjectAsset))):
            if (membership.project_id, membership.asset_id) not in state.memberships:
                session.delete(membership)
        for project in list(session.exec(select(Project))):
            if project.id not in state.projects:
                session.delete(project)
        for asset_file in list(session.exec(select(AssetFile))):
            if asset_file.asset_id not in state.assets:
                session.delete(asset_file)
        for asset in list(session.exec(select(Asset))):
            if asset.id not in state.assets:
                session.delete(asset)
        for folder in list(session.exec(select(Folder))):
            if folder.id not in state.folders:
                session.delete(folder)
        session.commit()


# --- row helpers ------------------------------------------------------------


def make_project(session: Session, *, marker: dict, name: str = "Clockwork Monster") -> str:
    project = Project(
        name=name,
        metadata_json=json.dumps({"_bundle": marker}),
        created_at=STAMP,
        updated_at=STAMP,
    )
    session.add(project)
    session.commit()
    return project.id


def make_assets(session: Session, *names: str) -> list[str]:
    assets = [
        Asset(
            name=name,
            filename=f"{name}.stl",
            mime="model/stl",
            size=len(STL_BYTES),
            storage_path=f"{KEY}/{name}.stl",
        )
        for name in names
    ]
    session.add_all(assets)
    session.commit()
    return [asset.id for asset in assets]


def row_count(session: Session, project_id: str) -> int:
    return len(memberships(session, project_id))


def memberships(session: Session, project_id: str) -> set[str]:
    return set(session.exec(select(ProjectAsset.asset_id).where(ProjectAsset.project_id == project_id)))


def marker_of(session: Session, project_id: str) -> dict:
    project = session.get(Project, project_id)
    assert project is not None
    return project_service.bundle_marker_of(project.metadata_json)


def write_marker_groups(session: Session, project_id: str, groups: list[dict]) -> None:
    """What the arrange's commit 1 does: refreshed groups and the ledger, one write."""
    project = session.get(Project, project_id)
    assert project is not None
    bundle = {**marker_of(session, project_id), "groups": groups}
    project.metadata_json = project_service.merge_project_marker(project.metadata_json, {"_bundle": bundle})
    session.add(project)
    session.commit()


def facts_with(*groups: dict) -> proposal_engine.Facts:
    """Hand-built facts: a measurement without the filesystem walk behind it."""
    facts = proposal_engine.Facts("ClockworkMonster", "/unused", KEY)
    facts.groups = list(groups)
    return facts


def group(name: str, models: int = 1) -> dict:
    return {"name": name, "models": models, "foreign": False, "separable": True}


def fixture_tree(root: Path) -> None:
    for name in ("a", "b"):
        path = root / KEY / f"{name}.stl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(STL_BYTES)


# --- the missing-only rule --------------------------------------------------


def test_a_second_adopt_reports_what_it_found_and_writes_nothing(vault: Vault) -> None:
    """Mutation: inserting every id again, so the second adopt reports `added` as the whole set."""
    with Session(engine) as session:
        project_id = make_project(session, marker={"key": KEY})
        member_ids = make_assets(session, "a", "b")

        first = adopt_service.adopt(session, project_id, member_ids)
        rows_after_first = row_count(session, project_id)
        second = adopt_service.adopt(session, project_id, member_ids)
        rows_after_second = row_count(session, project_id)

    assert first.added == tuple(member_ids)
    assert second.added == (), "the same set adopted twice adds nothing the second time"
    assert second.already_members == tuple(member_ids)
    assert rows_after_first == rows_after_second == len(member_ids)


def test_adopt_adds_only_the_missing_memberships(vault: Vault) -> None:
    """Mutation: writing the overlap too, so the second `added` names a and b beside c."""
    with Session(engine) as session:
        project_id = make_project(session, marker={"key": KEY})
        first_a, first_b, third = make_assets(session, "a", "b", "c")

        adopt_service.adopt(session, project_id, [first_a, first_b])
        second = adopt_service.adopt(session, project_id, [first_a, first_b, third])

        assert second.added == (third,)
        assert set(second.already_members) == {first_a, first_b}
        assert row_count(session, project_id) == 3


# --- undo -------------------------------------------------------------------


def test_adopt_undo_removes_exactly_the_recorded_memberships(vault: Vault) -> None:
    """Mutation: undo deleting the project row (the arrange's ledger carrier) or the operator's later membership."""
    with Session(engine) as session:
        project_id = make_project(session, marker={"key": KEY})
        adopted_a, adopted_b, operator_added = make_assets(session, "a", "b", "operator")
        outcome = adopt_service.adopt(session, project_id, [adopted_a, adopted_b])
        entry = adopt_service.entry_of(outcome, root=KEY, created=False)
        session.add(ProjectAsset(project_id=project_id, asset_id=operator_added, added_at=STAMP))
        session.commit()

        removed = adopt_service.undo_entry(session, entry)
        session.commit()

        assert removed == 2
        assert memberships(session, project_id) == {operator_added}, "the operator's row is not in the entry"
        assert session.get(Project, project_id) is not None, "a project owns no path; undo never deletes it"


# --- the group snapshot -----------------------------------------------------


def test_adopt_undo_restores_the_group_snapshot(vault: Vault) -> None:
    """Mutation: undo leaving the refreshed groups in the marker, or writing `groups: None` instead of removing the key."""
    old_groups = [{"name": "old"}]
    with Session(engine) as session:
        project_id = make_project(session, marker={"key": KEY, "groups": old_groups})
        member_ids = make_assets(session, "a", "b")
        outcome = adopt_service.adopt(
            session,
            project_id,
            member_ids,
            key=KEY,
            root_rel=KEY,
            attached=[(member_ids[0], f"{KEY}/a.stl", len(STL_BYTES))],
            facts=facts_with(group("new", models=2)),
        )
        entry = adopt_service.entry_of(outcome, root=KEY, created=False)

        assert outcome.groups_before == old_groups
        assert entry["groups_before"] == old_groups
        assert outcome.groups_after != outcome.groups_before

        write_marker_groups(session, project_id, outcome.groups_after)
        assert marker_of(session, project_id)["groups"] == outcome.groups_after

        adopt_service.undo_entry(session, entry)
        session.commit()
        assert marker_of(session, project_id)["groups"] == old_groups, "the snapshot goes back verbatim"

    with Session(engine) as session:
        project_id = make_project(session, marker={"key": KEY}, name="Clockwork Monster Two")
        member_ids = make_assets(session, "c")
        outcome = adopt_service.adopt(
            session,
            project_id,
            member_ids,
            key=KEY,
            root_rel=KEY,
            attached=[(member_ids[0], f"{KEY}/c.stl", len(STL_BYTES))],
            facts=facts_with(group("new", models=1)),
        )
        entry = adopt_service.entry_of(outcome, root=KEY, created=False)

        assert outcome.groups_before is None, "no `groups` key before the adopt is a recorded None"
        assert entry["groups_before"] is None

        write_marker_groups(session, project_id, outcome.groups_after)
        adopt_service.undo_entry(session, entry)
        session.commit()
        assert "groups" not in marker_of(session, project_id), "a recorded None REMOVES the key, it does not write None"


# --- the three refusals to write --------------------------------------------


def test_adopt_never_moves_bytes(vault: Vault) -> None:
    """Mutation: an adopt that touches the storage tree or creates a project row while it re-members."""
    fixture_tree(vault.root)
    with Session(engine) as session:
        project_id = make_project(session, marker={"key": KEY, "groups": [{"name": "old"}]})
        member_ids = make_assets(session, "a", "b")
        before = (vault.digest(), vault.project_rows())

        adopt_service.adopt(
            session,
            project_id,
            member_ids,
            key=KEY,
            root_rel=KEY,
            attached=[(member_ids[0], f"{KEY}/a.stl", len(STL_BYTES))],
            facts=facts_with(group("new")),
        )

        after = (vault.digest(), vault.project_rows())

    assert after == before, "membership is a row relation; the disk is move_service's"


def test_a_moving_plan_measures_nothing(vault: Vault) -> None:
    """Mutation: measuring a plan that renames the folder, recording group paths the moves are about to change."""
    marker = {"key": KEY, "groups": [{"name": "old"}]}
    with Session(engine) as session:
        project_id = make_project(session, marker=marker)
        member_ids = make_assets(session, "a", "b")

        outcome = adopt_service.adopt(
            session,
            project_id,
            member_ids,
            key=KEY,
            root_rel="import/17_Fates End - ClockworkMonster",
            attached=[(member_ids[0], f"{KEY}/a.stl", len(STL_BYTES))],
            facts=facts_with(group("new")),
        )

        assert (outcome.groups_before, outcome.groups_after) == (None, None)
        assert outcome.measured is False
        assert marker_of(session, project_id) == marker, "the marker is untouched"


def test_no_facts_measures_nothing(vault: Vault) -> None:
    """Mutation: `entry_of` recording `groups_before: None` for an adopt that never measured, so undo would delete a marker's groups."""
    with Session(engine) as session:
        project_id = make_project(session, marker={"key": KEY, "groups": [{"name": "old"}]})
        member_ids = make_assets(session, "a")

        outcome = adopt_service.adopt(session, project_id, member_ids, key=KEY, root_rel=KEY, attached=(), facts=None)
        entry = adopt_service.entry_of(outcome, root=KEY, created=False)

        assert outcome.measured is False
        assert (outcome.groups_before, outcome.groups_after) == (None, None)
        assert "groups_before" not in entry, "an absent key and a recorded None mean different things to undo"
        assert adopt_service.groups_restore(entry) is None
