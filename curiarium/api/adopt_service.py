"""The ONE owner for the membership delta: adding files to a project, and taking them back.

The detected-bundle arrange grew this logic two ways - a create-or-extend path
inserted only the memberships a project was missing, in bounded chunks, and a
marker refresh measured the project's groups from the engine's facts and wrote
them through `bundle_service.marker_groups`. The arrange itself is gone: its door
is `operations_service`'s `split-and-create` workflow plus the create-project
primitive, and both reach this logic through `adopt`, so the answer to "what does
this project hold now" has one implementation instead of one per door.

What this module owns:

  * the missing-only rule. `adopt` inserts the memberships the project does not
    already hold and reports the rest as `already_members`, so a second adopt over
    the same set is a no-op that says `added=()` rather than a second write. The
    unit of work is one chunk of `project_service.MEMBER_QUERY_BATCH` rows, read
    off the module at call time, so a Vault-sized adopt never stages one unbounded
    batch and a test can pin the boundary.
  * the group refresh, as a MEASUREMENT: `adopt` returns `(groups_before,
    groups_after)` and writes nothing to the marker. The caller persists it where
    its own ledger lives - the arrange's ledger is inside the project marker, and
    its commit 1 writes both the ledger and the refreshed groups in one write, so
    splitting that across two modules would be two writes that can disagree.
  * the undo of one recorded entry: exactly the memberships the entry names, plus
    the group snapshot it carries. A membership an operator added afterwards is
    not in the entry and survives, which is what makes undo an answer about this
    adopt rather than about the project.

What it deliberately does NOT do:

  * It never moves bytes and never touches the filesystem. Membership is a row
    relation; the disk is `move_service`'s.
  * It never creates or deletes a `Project` row. Deleting one would delete the
    carrier of the arrange's ledger, which is what makes a second undo a 409.
  * It does not decide WHICH files are members. That is the door's rule derived
    from the key (`bundle_service.members_of` for the bundle and operations doors,
    the projected path set for the arrange), and the ids handed in are written
    verbatim, in the caller's order.
  * It writes no audit row. A membership delta is reported by the door's own event
    (`arrange.apply`, or the operation's own lifecycle events), and this module
    knows nothing about which door asked.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

from sqlmodel import Session, func, select

import bundle_service
import library_scan
import project_create_service
import project_service
from models import Asset, Project, ProjectAsset

log = logging.getLogger(__name__)

# What an adopt records about itself, and the only shape its undo reads. The
# arrange persists exactly this as `projects[*]` in its ledger, which is why the
# keys are the ledger's keys and not a second vocabulary.
ENTRY_ID = "id"
ENTRY_MEMBERS = "members"
ENTRY_GROUPS_BEFORE = "groups_before"


def _now() -> str:
    """ISO-8601 UTC with the column precision the rest of the app writes."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class AdoptOutcome:
    """What one adopt decided: the delta it wrote and the group measurement it made.

    `groups_before`/`groups_after` are `None` when the adopt did not measure - a
    caller that passed no facts, or a plan that renames the folder, whose group
    paths on disk are about to change and would be recorded wrong.
    """

    project_id: str
    added: Tuple[str, ...]
    already_members: Tuple[str, ...]
    groups_before: Optional[List[dict]] = None
    groups_after: Optional[List[dict]] = None

    @property
    def measured(self) -> bool:
        return self.groups_after is not None


@dataclass(frozen=True)
class GroupFacts:
    """The engine's group list, carried from a reviewed plan to the runner.

    `measure_groups` reads exactly one attribute - `facts.groups` - from whatever
    it is handed. A `proposal_engine.Facts` is the plan-time answer; this is the
    same attribute carried on the step's args, so the runner refreshes the groups
    the operator REVIEWED instead of measuring the disk a second time.
    """

    groups: List[dict]


def unregistered_under(session: Session, root: str) -> int:
    """How many files on disk under `root` have no registered `Asset` row.

    This is the disk-minus-registered count the adopt planner refuses on, because
    adopting a folder whose registered set is behind the disk would silently leave
    those files out and call it "already complete" on the next plan. The fix is a
    rescan, so the refusal names the scan.

    One disk walk and one SQL statement over the exact prefix, never a per-file
    query. A missing directory answers 0 rather than raising: a folder the walk
    cannot read has no files this count can be honest about.
    """
    prefix = root + "/"
    registered = set(
        session.exec(
            select(Asset.storage_path).where(
                Asset.storage_path.is_not(None),
                func.substr(Asset.storage_path, 1, len(prefix)) == prefix,
            )
        )
    )
    storage = Path(bundle_service.STORAGE)
    unregistered = 0
    try:
        for directory, _dirs, files in os.walk(storage / root):
            for name in files:
                # The scan's own basename rule: it never registers a dot file.
                if name.startswith("."):
                    continue
                relative = Path(directory, name).relative_to(storage).as_posix()
                if relative not in registered:
                    unregistered += 1
    except OSError:
        return 0
    return unregistered


def adopt(
    session: Session,
    project_id: str,
    member_ids: Sequence[str],
    *,
    key: Optional[str] = None,
    root_rel: Optional[str] = None,
    attached: Sequence[Tuple[str, str, int]] = (),
    facts: Optional[object] = None,
) -> AdoptOutcome:
    """Add only the memberships `project_id` is missing, then measure its groups.

    Order is the contract: the memberships land first, so a group count measured
    afterwards describes what the project actually holds. `attached` is the
    caller's candidate list `(asset id, storage path, size)`; only the counted
    MODELs under `key + "/"` are bucketed by the first path segment after the key,
    which is the marker's own vocabulary for `attached` and the same rule the
    create path uses - a project's `attached` therefore means one thing whether
    the project was created or adopted.

    The measurement is skipped when the caller gave no facts, or when
    `root_rel != key`: a plan that renames or hoists the folder is about to change
    the very paths the engine's groups are relative to, so recording them would
    write folders that do not exist once the moves land. A project row that has
    gone is `None`-measured rather than an error: the caller's own ledger is the
    thing that must not be left half-written by a race with a delete.

    The commit is the caller's to make - except the membership chunk commits
    itself, which is `adopt`'s own rule and belongs to no helper: one flush per
    chunk of `project_service.MEMBER_QUERY_BATCH` rows rather than one write
    transaction for the whole set, so a Vault-sized adopt never holds an unbounded
    transaction open while it walks its chunks.
    """
    held = set(session.exec(select(ProjectAsset.asset_id).where(ProjectAsset.project_id == project_id)))
    ordered = list(dict.fromkeys(member_ids))
    added = [asset_id for asset_id in ordered if asset_id not in held]
    already = tuple(asset_id for asset_id in ordered if asset_id in held)
    stamp = _now()
    batch = max(1, int(project_service.MEMBER_QUERY_BATCH))
    for offset in range(0, len(added), batch):
        for asset_id in added[offset : offset + batch]:
            session.add(ProjectAsset(project_id=project_id, asset_id=asset_id, added_at=stamp))
        session.flush()
    if added:
        session.commit()
    before, after = measure_groups(session, project_id, key=key, root_rel=root_rel, attached=attached, facts=facts)
    return AdoptOutcome(
        project_id=project_id,
        added=tuple(added),
        already_members=already,
        groups_before=before,
        groups_after=after,
    )


def measure_groups(
    session: Session,
    project_id: str,
    *,
    key: Optional[str],
    root_rel: Optional[str],
    attached: Sequence[Tuple[str, str, int]],
    facts: Optional[object],
) -> Tuple[Optional[List[dict]], Optional[List[dict]]]:
    """`(before_groups, groups_after)` for this project and folder, or `(None, None)`.

    `root_rel != key` means the plan renames or hoists the folder: the engine's
    group paths are relative to a name the disk no longer has once the moves land,
    so a refresh would record folders that do not exist. Only a membership-only
    plan - the one whose layout is still the current one - rewrites the marker's
    groups. That is why this takes both spellings rather than trusting the caller
    to have compared them.
    """
    if facts is None or not key or root_rel != key:
        return (None, None)
    project = session.get(Project, project_id)
    if project is None:
        return (None, None)
    bundle = project_service.bundle_marker_of(project.metadata_json)
    before = bundle.get("groups")
    prefix = f"{key}/"
    counted = [
        project_create_service.MemberCandidate(asset_id, path)
        for asset_id, path, size in attached
        if library_scan.is_counted_model(path, size) and path.startswith(prefix)
    ]
    # `attached_by_group` is the same bucketing the create path asks, so an adopted
    # project's `attached` and a created one's are one implementation, not two that
    # agree today.
    after = bundle_service.marker_groups(
        bundle_service.wire_groups(facts.groups),
        attached=bundle_service.attached_by_group(key, counted, [member.asset_id for member in counted]),
        before=before if isinstance(before, list) else None,
    )
    return (before if isinstance(before, list) else None, after)


def entry_of(outcome: AdoptOutcome, *, root: str, created: bool) -> dict:
    """The caller's ledger entry for this adopt, in the shape `undo_entry` reads.

    `root`/`created` are the caller's facts - the arrange's ledger records which
    root the memberships landed under and whether this arrange created the project
    or extended one - and an adopt that measured nothing records no
    `groups_before` key at all, because a key present with a `None` value means
    something different to undo (remove the marker's groups) than an absent one
    (never touched them).
    """
    entry = {
        ENTRY_ID: outcome.project_id,
        "root": root,
        "created": created,
        ENTRY_MEMBERS: list(outcome.added),
    }
    if outcome.measured:
        entry[ENTRY_GROUPS_BEFORE] = outcome.groups_before
    return entry


def groups_restore(entry: Mapping) -> Optional[dict]:
    """The marker update that puts a project's groups back, or `None` if untouched.

    A recorded `None` is not "write nothing": it means the marker had no `groups`
    key before the adopt, so undo's marker write must REMOVE the key. The caller
    maps that `None` to its own removal rule, because the removal is the caller's
    write, not this module's. That rule lived in the arrange's ledger
    (`arrange_service._write_ledger` removed the key), and the arrange is deleted
    (`Mf-g759`): `operations_service`'s `split-and-create` undo deletes the
    projects it created, so a created project's marker goes with the row rather
    than being rewritten.
    """
    if ENTRY_GROUPS_BEFORE not in entry:
        return None
    return {"groups": entry.get(ENTRY_GROUPS_BEFORE)}


def undo_entry(session: Session, entry: Mapping) -> int:
    """Remove exactly the memberships this entry recorded as added, and nothing else.

    `projects[*].members` is the snapshot of what the adopt ADDED: every member of
    a project it created, and only the missing ones for a project it extended. A
    membership the operator added afterwards is not in the snapshot, and one that
    predates the adopt was never snapshotted, so both survive.

    The group snapshot the entry carries is put back here as well: the caller
    writes its ledger in the same breath, and a restore split across two writers
    would be a marker the undo left half-restored. `None` removes the key, per
    `groups_restore`.
    """
    project_id = entry.get(ENTRY_ID)
    if not isinstance(project_id, str) or not project_id:
        return 0
    removed = 0
    for asset_id in entry.get(ENTRY_MEMBERS) or []:
        if not isinstance(asset_id, str):
            continue
        membership = session.exec(
            select(ProjectAsset).where(
                ProjectAsset.project_id == project_id, ProjectAsset.asset_id == asset_id
            )
        ).first()
        if membership is None:
            continue
        session.delete(membership)
        removed += 1
    session.flush()
    restore = groups_restore(entry)
    if restore is not None:
        project = session.get(Project, project_id)
        if project is not None:
            bundle = dict(project_service.bundle_marker_of(project.metadata_json))
            if restore["groups"] is None:
                bundle.pop("groups", None)
            else:
                bundle["groups"] = restore["groups"]
            project.metadata_json = project_service.merge_project_marker(
                project.metadata_json, {"_bundle": bundle}
            )
            project.updated_at = _now()
            session.add(project)
            session.flush()
    return removed
