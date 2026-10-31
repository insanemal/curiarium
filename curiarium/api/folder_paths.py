"""Pure path-derivation rules for folders: no filesystem, no HTTP.

Migrations 1-4 are immutable, so `migrations._folder_path_segment` and
`migrations._effective_folder_parent` are a frozen API: they can never change to
match new runtime behaviour. The runtime therefore imports them instead of
keeping a second copy that could silently drift away from what migration 004
wrote into the database. `tests/test_folder_crud_filesystem.py` pins the parity
between these helpers and migration 004's backfill.

Everything here only reads and mutates ORM state; creating, renaming and pruning
directories is `folder_service`'s job.
"""

import logging
from collections import Counter
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from migrations import _effective_folder_parent as effective_folder_parent
from migrations import _folder_path_segment as folder_path_segment
from models import Asset, Folder

log = logging.getLogger(__name__)

__all__ = [
    "allocate_segment",
    "backfill_paths_from_asset_dirs",
    "converge_ancestor_paths",
    "derive_subtree_paths",
    "effective_folder_parent",
    "folder_path_segment",
    "folder_subtree",
    "join_relative",
    "preview_converged_path",
    "sibling_scope",
]


def sibling_scope(session: Session, parent_id: Optional[str], exclude_ids: set[str]) -> tuple[set[str], set[str]]:
    """Return the (used, reserved) directory-name scopes of a parent folder.

    `used` holds the directory names already taken in that scope and `reserved`
    holds every non-excluded child's derived name, so a newcomer never takes the
    literal name of a sibling. Both sets are casefolded.

    `used` deliberately diverges from migration 004 for children whose
    `storage_path` is NULL: those contribute their *derived* name instead of
    nothing. Every folder created through the API has a NULL path until it is
    converged, so without the divergence two same-named siblings would both want
    the same directory name and the second one would die on the unique index.
    """
    children = session.exec(select(Folder).where(Folder.parent_id == parent_id))
    used: set[str] = set()
    reserved: set[str] = set()
    for child in children:
        if child.id in exclude_ids:
            continue
        derived = folder_path_segment(child.name).casefold()
        reserved.add(derived)
        if child.storage_path:
            used.add(child.storage_path.rsplit("/", 1)[-1].casefold())
        else:
            used.add(derived)
    return used, reserved


def allocate_segment(name: str, used: set[str], reserved: set[str], sanitized: bool = False) -> str:
    """Pick a directory segment that is unique inside its parent scope.

    This is migration 004's allocation loop verbatim, so a runtime rename lands
    on exactly the name the backfill would have chosen. The suffix is appended
    *after* the 120-character truncation, so a segment may reach 124 characters;
    that is also what makes `Foo` plus a literal `Foo (2)` push a newcomer to
    `Foo (3)`.
    """
    segment = name if sanitized else folder_path_segment(name)
    candidate = segment
    suffix = 2
    while candidate.casefold() in used or (
        candidate.casefold() != segment.casefold() and candidate.casefold() in reserved
    ):
        candidate = f"{segment} ({suffix})"
        suffix += 1
    return candidate


def join_relative(parent_relative: str, segment: str) -> str:
    """Append a segment to a parent's relative storage path."""
    return f"{parent_relative}/{segment}" if parent_relative else segment


def folder_subtree(session: Session, folder_id: str) -> list[Folder]:
    """Return `folder_id`'s subtree breadth-first, every parent before its children.

    All folders are loaded once. A dangling `parent_id` (a parent row that no
    longer exists) is treated as root scope, which is how migration 004's
    `effective_folder_parent` resolves it too. Children are ordered by
    (casefold(name), id) so the result never depends on row order, and a cycle
    cannot make the walk loop forever.
    """
    folders = list(session.exec(select(Folder)))
    known_ids = {folder.id for folder in folders}
    children: dict[Optional[str], list[Folder]] = {}
    for folder in folders:
        parent_key = folder.parent_id if folder.parent_id in known_ids else None
        children.setdefault(parent_key, []).append(folder)
    for siblings in children.values():
        siblings.sort(key=lambda sibling: ((sibling.name or "").casefold(), sibling.id))

    root = next((folder for folder in folders if folder.id == folder_id), None)
    if root is None:
        return []

    ordered: list[Folder] = []
    seen: set[str] = set()
    queue = [root]
    while queue:
        current = queue.pop(0)
        if current.id in seen:
            continue
        seen.add(current.id)
        ordered.append(current)
        queue.extend(children.get(current.id, []))
    return ordered


def _ancestor_chain(session: Session, folder_id: Optional[str]) -> list[Folder]:
    """Load `folder_id`'s ancestor line root-first, ending at a gap or a cycle.

    The whole folder table is read once, so a deep tree costs one query. A
    missing `folder_id` yields an empty chain; a repeated id ends the walk, so a
    `parent_id` cycle terminates instead of looping forever.
    """
    if not folder_id:
        return []

    folders = {folder.id: folder for folder in session.exec(select(Folder))}
    chain: list[Folder] = []
    seen: set[str] = set()
    current = folders.get(folder_id)
    while current is not None and current.id not in seen:
        seen.add(current.id)
        chain.append(current)
        current = folders.get(current.parent_id) if current.parent_id else None
    chain.reverse()
    return chain


def _chain_relative_paths(session: Session, chain: list[Folder]) -> dict[str, str]:
    """Derive every chain folder's storage-relative path, root first.

    Read-only: a folder with a path keeps it verbatim and a path-less one is
    allocated against its parent's scope, but no attribute is assigned and no
    row is written. The parent's path comes from the map already computed for
    the chain, so the result is exactly what `converge_ancestor_paths` persists.
    """
    paths: dict[str, str] = {
        folder.id: folder.storage_path for folder in chain if folder.storage_path
    }
    for folder in chain:
        if folder.id in paths:
            continue
        parent_relative = paths.get(folder.parent_id, "") if folder.parent_id else ""
        # A folder is excluded from its own scope: otherwise its own NULL name
        # segment counts as `used` and pushes it to `Name (2)`.
        segment = allocate_segment(folder.name, *sibling_scope(session, folder.parent_id, {folder.id}))
        paths[folder.id] = join_relative(parent_relative, segment)
    return paths


def converge_ancestor_paths(session: Session, folder_id: Optional[str]) -> str:
    """Give every path-less ancestor of `folder_id` a `storage_path`, root first.

    Returns `folder_id`'s relative path, or "" when there is no folder. Persist
    only: nothing is created, moved or renamed on disk. Folders that already have
    a path keep it exactly.
    """
    chain = _ancestor_chain(session, folder_id)
    if not chain:
        # `folder_id` no longer exists (deleted while we were looking at it).
        return ""

    paths = _chain_relative_paths(session, chain)
    for folder in chain:
        computed = paths[folder.id]
        if folder.storage_path != computed:
            folder.storage_path = computed
            session.add(folder)
    return chain[-1].storage_path or ""


def preview_converged_path(session: Session, folder_id: Optional[str]) -> str:
    """The path `converge_ancestor_paths` would return, without persisting it.

    Read-only on purpose: a folder delete preview calls this, so it must never
    converge an ancestor, write a row or mutate an ORM object. Returns "" when
    the folder does not exist.
    """
    chain = _ancestor_chain(session, folder_id)
    if not chain:
        return ""
    return _chain_relative_paths(session, chain)[chain[-1].id]


def derive_subtree_paths(
    session: Session,
    folder: Folder,
    new_parent_relative: str,
    new_segment: str,
) -> dict[str, str]:
    """Map every folder in `folder`'s subtree to its new relative path.

    The walk follows `folder_subtree`'s breadth-first order so a parent's path is
    always known before its children's. Siblings that move in the same operation
    are not reflected in the database yet, so segments are allocated against a
    per-scope overlay seeded once from `sibling_scope`; every derived or reused
    segment is added to that overlay immediately. This is the runtime equivalent
    of migration 004's in-loop `sibling_used.add(...)`: without it two NULL-path
    casefold-colliding siblings would each derive `Name (2)` and the rename would
    die on `idx_folder_storage_path_unique`.

    Descendants that already have a path keep their last segment verbatim - the
    directory on disk keeps that exact name, so it must never be re-sanitised or
    re-truncated. A reused segment could in principle collide in the new scope;
    such a collision can only come from pre-existing bad data and surfaces as an
    IntegrityError, which the caller maps to a 409.
    """
    subtree = folder_subtree(session, folder.id)
    subtree_ids = {subtree_folder.id for subtree_folder in subtree}
    overlay: dict[Optional[str], tuple[set[str], set[str]]] = {}

    def overlay_for(parent_id: Optional[str]) -> tuple[set[str], set[str]]:
        if parent_id not in overlay:
            overlay[parent_id] = sibling_scope(session, parent_id, subtree_ids)
        return overlay[parent_id]

    paths: dict[str, str] = {}
    for current in subtree:
        parent_key = current.parent_id
        if current.id == folder.id:
            parent_relative = new_parent_relative
            segment = new_segment
        else:
            parent_relative = paths[current.parent_id]
            if current.storage_path:
                segment = current.storage_path.rsplit("/", 1)[-1]
            else:
                used, reserved = overlay_for(parent_key)
                segment = allocate_segment(current.name, used, reserved)
        paths[current.id] = join_relative(parent_relative, segment)
        overlay_for(parent_key)[0].add(segment.casefold())
    return paths


def _directory_of(storage_path: str) -> str:
    """The POSIX directory a storage-relative file path lives in."""
    return storage_path.rsplit("/", 1)[0] if "/" in storage_path else ""


def _is_clean_relative_dir(value: str) -> bool:
    """A storage-relative POSIX directory with no escape, empty or Windows segment."""
    if not value or value.startswith("/") or "\\" in value:
        return False
    return all(segment not in ("", ".", "..") for segment in value.split("/"))


def _pathless_folder(folder: Folder) -> bool:
    """Whether a folder still needs its directory identity (NULL or blank)."""
    return not (folder.storage_path or "").strip()


def backfill_paths_from_asset_dirs(session: Session) -> dict[str, str]:
    """Give path-less folders the directory their own assets already live in.

    A folder migrated from the flattened v3 database has ``storage_path IS NULL``
    and can never be healed by the scan's per-directory resolver, because every
    legacy asset is already known and the walk only resolves folders for a file
    it registers in this run. This is the other half: derive the directory from
    the assets' own ``storage_path`` column, which is the only surviving record of
    where the bytes lie. No filesystem call, no ``stat``, no create/rename/move/
    delete - the volume may not even be mounted on this host.

    A folder is linked only when every rule below accepts it:

    * it owns at least one live managed asset (``is_reference = 0`` and
      ``missing_since IS NULL``) and all of those assets sit in exactly one
      directory;
    * that directory is a clean storage-relative POSIX path and no existing row
      already claims it case-insensitively;
    * its effective parent is linked (or has a path of its own) and the directory
      is strictly under that parent's path;
    * no two rows would derive the same casefolded directory;
    * no asset directly in the directory belongs to a different folder, and the
      directory contains no other folder's known path outside its own subtree.

    The storage root is one of those directories, spelled "". An asset sitting
    directly at the root therefore makes its folder ambiguous rather than
    invisible: the folder's children would otherwise look like they all live in
    some subdirectory while one of them does not. A folder whose only evidence
    is the root has no directory of its own at all (reason ``root_scope``),
    because root scope is not a folder path and a folder claiming one would hand
    a delete a directory that holds every other root-level asset.

    Every rule is subtractive, so the answer does not depend on row order. Two
    rows that would derive the same casefolded directory are *both* left NULL: a
    deterministic winner would give one row a delete whose ``rm -r`` moves the
    other row's bytes into the trash. A second call returns ``{}`` and writes
    nothing, because only NULL/blank rows are ever candidates.

    Returns the linked ``{folder_id: relative_directory}`` mapping.
    """
    folders = list(session.exec(select(Folder)))
    pathless = [folder for folder in folders if _pathless_folder(folder)]
    if not pathless:
        # Re-run fast path: nothing to heal, so the asset table is not touched.
        return {}

    pathless_ids = {folder.id for folder in pathless}
    claimed = {
        folder.storage_path.casefold(): folder.id
        for folder in folders
        if not _pathless_folder(folder)
    }
    by_id = {folder.id: folder for folder in folders}

    # One indexed read (idx_asset_folder_id) is the whole evidence base: a
    # managed, live asset's storage_path is the directory its folder stands for.
    dirs: dict[str, set[str]] = {}
    evidence = session.exec(
        select(Asset.folder_id, Asset.storage_path).where(
            Asset.folder_id.in_(pathless_ids),
            Asset.storage_path.is_not(None),
            Asset.is_reference == 0,
            Asset.missing_since.is_(None),
        )
    ).all()
    for folder_id, storage_path in evidence:
        # Recorded unconditionally, including "" for the storage root: dropping
        # root-level evidence would let a folder whose assets straddle the root
        # and a subdirectory look like it lives in exactly one directory.
        dirs.setdefault(folder_id, set()).add(_directory_of(storage_path or ""))

    reasons: dict[str, str] = {}
    candidates: dict[str, str] = {}
    for folder in pathless:
        found = dirs.get(folder.id)
        if not found:
            reasons[folder.id] = "no_live_asset"
            continue
        if len(found) != 1:
            reasons[folder.id] = "ambiguous_directories"
            continue
        directory = next(iter(found))
        if not directory:
            reasons[folder.id] = "root_scope"
            continue
        if not _is_clean_relative_dir(directory):
            reasons[folder.id] = "invalid_directory"
            continue
        if directory.casefold() in claimed:
            reasons[folder.id] = "path_claimed"
            continue
        candidates[folder.id] = directory

    # Two rows that would derive the same casefolded directory are BOTH dropped.
    # A deterministic winner would hand one row a path whose folder delete
    # trashes the whole directory - including the other row's bytes - so neither
    # may claim it. The operator has to separate the rows by hand.
    by_directory: dict[str, list[str]] = {}
    for folder_id, directory in candidates.items():
        by_directory.setdefault(directory.casefold(), []).append(folder_id)
    for folder_ids in by_directory.values():
        if len(folder_ids) < 2:
            continue
        for folder_id in folder_ids:
            reasons[folder_id] = "shared_directory"
            del candidates[folder_id]

    # Root-first hierarchy walk. The order is folder_subtree's: children grouped
    # by effective parent, siblings by (casefold(name), id), breadth-first from
    # root scope, so the answer never depends on row order.
    parents = {folder.id: folder.parent_id for folder in folders}
    effective = {
        folder_id: effective_folder_parent(folder_id, parents[folder_id], parents)
        for folder_id in parents
    }
    children: dict[Optional[str], list[Folder]] = {}
    for folder in folders:
        children.setdefault(effective[folder.id], []).append(folder)
    for siblings in children.values():
        siblings.sort(key=lambda sibling: ((sibling.name or "").casefold(), sibling.id))

    ordered: list[Folder] = []
    seen: set[str] = set()
    queue = list(children.get(None, []))
    while queue:
        current = queue.pop(0)
        if current.id in seen:
            continue
        seen.add(current.id)
        ordered.append(current)
        queue.extend(children.get(current.id, []))

    assigned: dict[str, str] = {}
    for folder in ordered:
        directory = candidates.get(folder.id)
        if directory is None:
            continue
        parent_id = effective[folder.id]
        if parent_id is not None:
            parent_path = assigned.get(parent_id)
            if parent_path is None:
                parent = by_id.get(parent_id)
                existing = parent.storage_path if parent is not None else None
                parent_path = existing if existing and existing.strip() else None
            if parent_path is None:
                reasons[folder.id] = "parent_unlinked"
                continue
            if not directory.startswith(parent_path + "/"):
                reasons[folder.id] = "not_under_parent"
                continue
        assigned[folder.id] = directory

    for folder_id in candidates:
        if folder_id not in assigned and folder_id not in reasons:
            reasons[folder_id] = "unreachable"

    # Keep the hierarchy's answer for the containment pass below: "assigned
    # earlier" means the walk's assignment, not the surviving set, so the pass
    # stays order-independent even after a row is dropped here.
    hierarchy_paths = dict(assigned)

    # Exclusivity pass 1: a directory an asset of another folder lives in is not
    # this folder's directory. One keyset-paginated read of every asset keeps
    # memory at O(candidates): only candidate directory names are retained.
    # `library_scan` imports this module, so the page size is imported lazily
    # instead of duplicated here.
    from library_scan import BATCH_SIZE

    candidate_keys = {
        directory.casefold(): folder_id for folder_id, directory in assigned.items()
    }
    owners: dict[str, set[str]] = {key: set() for key in candidate_keys}
    if candidate_keys:
        last_id = ""
        while True:
            page = session.exec(
                select(Asset.id, Asset.folder_id, Asset.storage_path)
                .where(Asset.storage_path.is_not(None), Asset.id > last_id)
                .order_by(Asset.id)
                .limit(BATCH_SIZE)
            ).all()
            if not page:
                break
            for asset_id, folder_id, storage_path in page:
                last_id = asset_id
                if folder_id is None:
                    continue
                key = _directory_of(storage_path or "").casefold()
                if key in owners:
                    owners[key].add(folder_id)

    for key, owner_ids in owners.items():
        folder_id = candidate_keys[key]
        if any(owner_id != folder_id for owner_id in owner_ids):
            reasons[folder_id] = "directory_shared_with_other_folder"
            del assigned[folder_id]

    # Exclusivity pass 2: a candidate that contains another folder's known path
    # would swallow that folder's directory on a delete. Paths inside the row's
    # own subtree are its descendants, so they are expected and allowed.
    known_paths: dict[str, str] = {}
    for folder in folders:
        if not _pathless_folder(folder):
            known_paths[folder.id] = folder.storage_path
    for folder_id, directory in hierarchy_paths.items():
        known_paths.setdefault(folder_id, directory)

    actual_children: dict[Optional[str], list[str]] = {}
    for folder in folders:
        actual_children.setdefault(folder.parent_id, []).append(folder.id)

    def subtree_ids(root_id: str) -> set[str]:
        found: set[str] = set()
        stack = list(actual_children.get(root_id, []))
        while stack:
            child_id = stack.pop()
            if child_id == root_id or child_id in found:
                continue
            found.add(child_id)
            stack.extend(actual_children.get(child_id, []))
        return found

    for folder_id in list(assigned):
        prefix = assigned[folder_id] + "/"
        own_subtree = subtree_ids(folder_id)
        for other_id, other_path in known_paths.items():
            if other_id == folder_id or other_id in own_subtree:
                continue
            if other_path.startswith(prefix):
                reasons[folder_id] = "contains_foreign_path"
                del assigned[folder_id]
                break

    # Persist in the same root-first order, one savepoint per row. A concurrent
    # HTTP folder create can take a path between the read and the write; that
    # must fail only this row, never abort the whole scan.
    linked: dict[str, str] = {}
    for folder in ordered:
        directory = assigned.get(folder.id)
        if directory is None:
            continue
        taken = False
        try:
            with session.begin_nested():
                clash = session.exec(
                    select(Folder.id).where(
                        Folder.storage_path.collate("NOCASE") == directory
                    )
                ).first()
                if clash is not None:
                    taken = True
                else:
                    folder.storage_path = directory
                    session.add(folder)
                    session.flush()
        except IntegrityError:
            # idx_folder_storage_path_unique lost a race: the savepoint rolled
            # back, the row stays NULL and the next scan retries.
            folder.storage_path = None
            reasons[folder.id] = "unique_conflict"
            log.warning(
                "folder path backfill: folder %s lost the race for %s",
                folder.id, directory,
            )
            continue
        if taken:
            reasons[folder.id] = "path_taken_concurrently"
            continue
        linked[folder.id] = directory

    if linked:
        session.commit()

    counts = Counter(reasons.values())
    summary = ", ".join(f"{reason}={counts[reason]}" for reason in sorted(counts)) or "none"
    log.info(
        "folder path backfill: linked %d of %d pathless folder(s); unlinked reasons: %s",
        len(linked), len(pathless), summary,
    )
    for folder_id, directory in sorted(linked.items()):
        log.debug("folder path backfill: folder %s linked to %s", folder_id, directory)
    for folder_id, reason in sorted(reasons.items()):
        log.debug("folder path backfill: folder %s unlinked (%s)", folder_id, reason)
    if not linked:
        sample = "; ".join(
            f"{folder_id}={reasons.get(folder_id, 'unknown')}"
            for folder_id in sorted(pathless_ids)[:20]
        )
        # A permanently ambiguous legacy library is an accepted steady state, so
        # the no-candidate case is informational; only a stage that had candidates
        # and dropped all of them is worth a warning on every scan.
        log.log(
            logging.WARNING if candidates else logging.INFO,
            "folder path backfill linked none of %d pathless folder(s); first %d: %s",
            len(pathless), min(20, len(pathless)), sample,
        )
    return linked
