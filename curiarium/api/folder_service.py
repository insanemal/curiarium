"""Folder CRUD: directory materialisation and the matching transactional rows.

Filesystem is always the *first* side effect and the database row is committed
second, so a rollback can undo the directory it created. A rename is the only
operation that touches existing bytes, and it always runs inside one transaction
with the row updates that describe the move.
"""

import errno
import logging
import os
import stat
from pathlib import Path
from typing import Optional, Sequence

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

import audit
import folder_paths
from models import Asset, Folder

log = logging.getLogger(__name__)

# Errnos a directory rename returns when the destination exists: POSIX reports
# ENOTEMPTY for a non-empty directory and ENOTDIR for a file, and Windows reports
# EEXIST for either. Some POSIX filesystems also use EEXIST for the directory
# case. All three mean "occupied", which is the 409 the pre-check would have
# raised.
_DESTINATION_OCCUPIED_ERRNOS = frozenset({errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR})


def validate_parent_folder(session: Session, parent_id: Optional[str], folder_id: Optional[str] = None) -> Optional[str]:
    """Ensure a parent folder exists and does not introduce cycles."""
    if not parent_id:
        return None
    parent = session.get(Folder, parent_id)
    if not parent:
        raise HTTPException(status_code=400, detail="Parent folder not found")
    if folder_id and parent_id == folder_id:
        raise HTTPException(status_code=400, detail="Folder cannot be its own parent")

    ancestor = parent
    visited = set([folder_id]) if folder_id else set()
    while ancestor and ancestor.parent_id:
        if ancestor.parent_id in visited:
            raise HTTPException(status_code=400, detail="Invalid parent: would create a cycle")
        if folder_id and ancestor.parent_id == folder_id:
            raise HTTPException(status_code=400, detail="Invalid parent: would create a cycle")
        visited.add(ancestor.parent_id)
        ancestor = session.get(Folder, ancestor.parent_id)
    return parent_id


def storage_root() -> Path:
    """Resolve the storage root at call time so tests can monkeypatch it."""
    import db

    return Path(db.STORAGE).resolve()


def _absolute(relative: str) -> Path:
    """Resolve a relative storage path, refusing anything outside the storage root."""
    root = storage_root()
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise HTTPException(status_code=400, detail="Folder path resolved outside the storage directory")
    return candidate


def prune_empty_storage_dirs(start: Path) -> None:
    """Remove empty directories from `start` upwards, stopping at the storage root.

    Own copy of `asset_service.prune_empty_storage_dirs`: that one compares
    against the `STORAGE` path bound at import time, so under a monkeypatched
    storage root its rollback would silently do nothing.
    """
    root = storage_root()
    current = start
    while True:
        try:
            resolved = current.resolve()
        except OSError:
            return
        if resolved == root or root not in resolved.parents:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def remove_created_directory(path: Path) -> None:
    """Undo a directory created for a folder whose row did not persist.

    The leaf must be removed first: pruning from `path.parent` while `path` still
    exists removes nothing, which is what an earlier draft got wrong.
    """
    try:
        path.rmdir()
    except OSError:
        return
    prune_empty_storage_dirs(path.parent)


def apply_folder_paths(session: Session, folders: dict[str, Folder], paths: dict[str, str]) -> None:
    """Write derived relative paths onto the loaded folder rows."""
    for folder_id, relative in paths.items():
        folders[folder_id].storage_path = relative
        session.add(folders[folder_id])


def plan_asset_rewrites(session: Session, old_relative: str, new_relative: str) -> list[tuple[Asset, str]]:
    """Pair each asset inside `old_relative` with its path below `new_relative`.

    The prefix filter is done in Python rather than with LIKE: a sanitised
    segment may legally contain `%` or `_`, and the folder prefix is
    case-sensitive here. `storage_path == old_relative` is never a match (a file
    cannot live at a directory path), and assets outside the prefix are left
    alone because the storage template may not contain `{folder}` at all.
    """
    prefix = f"{old_relative}/"
    rewrites: list[tuple[Asset, str]] = []
    for asset in session.exec(select(Asset)):
        if asset.storage_path and asset.storage_path.startswith(prefix):
            rewrites.append((asset, new_relative + asset.storage_path[len(old_relative):]))
    return rewrites


def apply_asset_path_rewrites(session: Session, rewrites: list[tuple[Asset, str]]) -> None:
    """Write the planned asset paths onto their rows."""
    for asset, new_path in rewrites:
        asset.storage_path = new_path
        session.add(asset)


def _child_named(session: Session, parent_id: Optional[str], name: str) -> Optional[Folder]:
    """The existing child of `parent_id` this name already means, or None.

    Matched on the row's name OR on the last segment of its directory, because a
    deduped directory leaf (`Foo (2)`) and its row name (`Foo`) are two spellings
    of the same child and an incoming path may use either.
    """
    wanted = (name or "").casefold()
    for child in session.exec(select(Folder).where(Folder.parent_id == parent_id)):
        if (child.name or "").casefold() == wanted:
            return child
        leaf = (child.storage_path or "").rsplit("/", 1)[-1]
        if leaf.casefold() == wanted:
            return child
    return None


def get_or_create_child(parent_id: Optional[str], name: str) -> tuple[Folder, bool]:
    """The child folder `name` means under `parent_id`, creating it if needed.

    Returns `(folder, created)`, where `created` is True only when this call made
    the row and its directory. The lookup matches the row's name and the leaf of
    its directory (`_child_named`), so a path that spells a deduped `Foo (2)`
    either way finds the one child instead of creating a second one beside it.

    Race-safe: two requests can pass the lookup together, and the loser of the
    `create_folder` 409 re-reads the child and returns it rather than failing.
    `create_folder` commits, so the row is durable before the caller inserts the
    asset that lives in it.
    """
    from db import engine

    with Session(engine) as session:
        existing = _child_named(session, parent_id, name)
        if existing is not None:
            return existing, False
        try:
            return create_folder(session, name, "[]", parent_id), True
        except HTTPException as error:
            if error.status_code != 409:
                raise
            existing = _child_named(session, parent_id, name)
            if existing is None:
                raise
            return existing, False


def _drop_repeated_base_segments(segments: list[str], accepted: set[str], at_most: int) -> list[str]:
    """Drop the leading incoming segments that merely spell the base again.

    Measured necessity: without this, uploading `elecrow-files/x.stl` into the
    folder named `elecrow-files` created `elecrow-files (2)/elecrow-files/`,
    because the base's directory leaf is deduped `(2)` while its row name is not,
    so a leading segment equal to either spelling is the base itself spelled
    again. A base you are inside can be spelled at most once, which is what
    `at_most` bounds the drops to.
    """
    remaining = list(segments)
    for _ in range(at_most):
        if not remaining or remaining[0].casefold() not in accepted:
            break
        remaining.pop(0)
    return remaining


def resolve_folder_chain(base_folder_id: Optional[str], segments: Sequence[str]) -> tuple[Optional[str], list[str]]:
    """Turn a received path into real folders and return its deepest folder id.

    Returns `(leaf_folder_id, created_folder_ids)`, the second being what a
    failed request hands to `delete_created_folders` to undo.

    The base folder is converged first (its NULL-path ancestors are persisted),
    so the accepted spellings of "where we already are" are real directory
    names. A leading segment that spells the base - its row name or the leaf of
    its directory, deduped or not - is dropped rather than made into a child:
    see `_drop_repeated_base_segments` for the measurement that demands it.
    With no base folder nothing is dropped, because nothing is being repeated.

    Empty `segments` returns the base untouched and never touches the disk.
    """
    from db import engine

    if not segments:
        return base_folder_id, []

    accepted: set[str] = set()
    base_segments: list[str] = []
    if base_folder_id:
        with Session(engine) as session:
            relative = folder_paths.converge_ancestor_paths(session, base_folder_id)
            base = session.get(Folder, base_folder_id)
            base_name = base.name if base is not None else ""
            session.commit()
        base_segments = [segment for segment in relative.split("/") if segment]
        accepted = {base_name.casefold()} | {segment.casefold() for segment in base_segments}

    remaining = _drop_repeated_base_segments(list(segments), accepted, len(base_segments))
    current = base_folder_id
    created: list[str] = []
    for segment in remaining:
        folder, made = get_or_create_child(current, segment)
        current = folder.id
        if made:
            created.append(folder.id)
    return current, created


def delete_created_folders(folder_ids: Sequence[str]) -> None:
    """Best-effort undo for a request that created folders and then failed.

    A failed request must not leave a directory the tree can show but nothing
    owns, so each row is deleted in its own committed session and its directory
    removed, deepest first (reverse creation order) so a parent is pruned only
    after its children are gone. Every id is attempted and every failure is a
    warning: this runs while another exception is on its way out and must never
    replace it.
    """
    from db import engine

    for folder_id in reversed(list(folder_ids)):
        relative = ""
        try:
            with Session(engine) as session:
                folder = session.get(Folder, folder_id)
                if folder is None:
                    continue
                relative = folder.storage_path or ""
                session.delete(folder)
                session.commit()
            if relative:
                remove_created_directory(_absolute(relative))
        except (OSError, HTTPException) as error:
            log.warning("failed to undo created folder %s (%s): %s", folder_id, relative, error)


def create_folder(session: Session, name: str, tags_json: str, parent_id: Optional[str]) -> Folder:
    """Create the directory for a new folder, then its row."""
    parent_id = validate_parent_folder(session, parent_id)
    parent_relative = folder_paths.converge_ancestor_paths(session, parent_id)
    segment = folder_paths.allocate_segment(name, *folder_paths.sibling_scope(session, parent_id, set()))
    relative = folder_paths.join_relative(parent_relative, segment)
    target = _absolute(relative)
    if target.exists():
        raise HTTPException(status_code=409, detail=f"Storage path already exists: {relative}")
    try:
        target.mkdir(parents=True)
    except FileExistsError:
        # TOCTOU: the tree appeared between the exists() check and mkdir()
        raise HTTPException(status_code=409, detail=f"Storage path already exists: {relative}") from None

    folder = Folder(name=name, tags_json=tags_json, parent_id=parent_id, storage_path=relative)
    try:
        session.add(folder)
        session.commit()
        session.refresh(folder)
    except IntegrityError:
        # Filesystem first, row second: a failed insert must not leave a directory behind.
        session.rollback()
        remove_created_directory(target)
        raise HTTPException(status_code=409, detail=f"Storage path already exists: {relative}") from None
    except BaseException:
        session.rollback()
        remove_created_directory(target)
        raise
    return folder


def _stage_folder_changes(
    session: Session,
    folder: Folder,
    previous_name: str,
    previous_parent_id: Optional[str],
    name: str,
    parent_id: Optional[str],
    audit_ctx: Optional[audit.AuditContext],
) -> None:
    """Stage one event per field this patch actually changed.

    Tags are an exempt metadata edit. A tags-only PATCH therefore stages nothing,
    and a rename and a reparent in one request stage two rows, one per fact.
    """
    events = []
    if name != previous_name:
        events.append(audit.Event(audit.FOLDER_RENAME, folder.id, {"from": previous_name, "to": name}))
    if parent_id != previous_parent_id:
        events.append(audit.Event(audit.FOLDER_MOVE, folder.id, {"from": previous_parent_id, "to": parent_id}))
    audit.stage(session, audit_ctx, *events)


def move_folder(
    session: Session,
    folder: Folder,
    name: str,
    tags_json: str,
    parent_id: Optional[str],
    *,
    audit_ctx: Optional[audit.AuditContext],
) -> Folder:
    """Rename and/or reparent a folder, keeping disk and rows in step."""
    previous_name = folder.name
    previous_parent_id = folder.parent_id
    parent_id = validate_parent_folder(session, parent_id, folder.id)
    old_relative = folder_paths.converge_ancestor_paths(session, folder.id)
    # `validate_parent_folder` already rejected cycles, so the new parent can
    # never live inside this folder's own subtree.
    new_parent_relative = folder_paths.converge_ancestor_paths(session, parent_id)
    subtree_ids = {subtree_folder.id for subtree_folder in folder_paths.folder_subtree(session, folder.id)}

    if name != folder.name:
        base_segment = folder_paths.folder_path_segment(name)
        sanitized = False
    else:
        # A same-name PATCH must never re-derive a segment: a deduped folder named
        # `Foo (2)` would otherwise be renamed to `Foo` by a tags-only edit.
        base_segment = old_relative.rsplit("/", 1)[-1] if old_relative else folder_paths.folder_path_segment(name)
        sanitized = True

    new_segment = folder_paths.allocate_segment(
        base_segment,
        *folder_paths.sibling_scope(session, parent_id, subtree_ids),
        sanitized=sanitized,
    )
    new_relative = folder_paths.join_relative(new_parent_relative, new_segment)

    folder.name = name
    folder.tags_json = tags_json
    folder.parent_id = parent_id
    session.add(folder)

    if not old_relative or new_relative == old_relative:
        # Metadata-only edit: no filesystem call at all, so unrelated files cannot move.
        try:
            _stage_folder_changes(session, folder, previous_name, previous_parent_id, name, parent_id, audit_ctx)
            session.commit()
            session.refresh(folder)
        except IntegrityError:
            session.rollback()
            raise HTTPException(status_code=409, detail=f"Storage path already exists: {new_relative}") from None
        return folder

    source = _absolute(old_relative)
    try:
        source_exists = stat.S_ISDIR(source.stat().st_mode)
    except FileNotFoundError:
        source_exists = False

    folders = {subtree_folder.id: subtree_folder for subtree_folder in folder_paths.folder_subtree(session, folder.id)}

    if not source_exists:
        # Deliberate convergence: 004 legacy rows whose path was never on disk, and
        # scan/zip-created folders whose directory lives somewhere else. Only rows
        # change here; nothing is created, moved or deleted. For scan/zip folders
        # the database then claims a path where nothing exists, which later-phase
        # work owns.
        try:
            apply_folder_paths(
                session,
                folders,
                folder_paths.derive_subtree_paths(session, folder, new_parent_relative, new_segment),
            )
            apply_asset_path_rewrites(session, plan_asset_rewrites(session, old_relative, new_relative))
            _stage_folder_changes(session, folder, previous_name, previous_parent_id, name, parent_id, audit_ctx)
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(status_code=409, detail=f"Storage path already exists: {new_relative}") from None
        except BaseException:
            session.rollback()
            raise
        session.refresh(folder)
        return folder

    target = _absolute(new_relative)
    if target.exists():
        raise HTTPException(status_code=409, detail=f"Storage path already exists: {new_relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(source, target)
    except FileNotFoundError:
        # TOCTOU: the source vanished between the stat() and the rename
        raise HTTPException(status_code=409, detail=f"Storage path already exists: {new_relative}") from None
    except OSError as error:
        if error.errno in _DESTINATION_OCCUPIED_ERRNOS:
            # TOCTOU: the destination appeared between the exists() check and
            # the rename. POSIX rename would silently replace an empty
            # directory; that cannot lose bytes, and the unique storage_path
            # index is the record-level guard, so only the error cases are
            # translated. Anything else is a real filesystem failure and keeps
            # its 500.
            raise HTTPException(status_code=409, detail=f"Storage path already exists: {new_relative}") from None
        raise

    try:
        apply_folder_paths(
            session,
            folders,
            folder_paths.derive_subtree_paths(session, folder, new_parent_relative, new_segment),
        )
        apply_asset_path_rewrites(session, plan_asset_rewrites(session, old_relative, new_relative))
        _stage_folder_changes(session, folder, previous_name, previous_parent_id, name, parent_id, audit_ctx)
        session.commit()
    except BaseException as error:
        session.rollback()
        # The directory already moved while the rows did not commit; a crash in
        # this window leaves disk and database out of sync, so the undo below is
        # best effort.
        try:
            os.rename(target, source)
        except OSError:
            log.exception("Failed to undo folder rename %s -> %s", source, target)
        if isinstance(error, IntegrityError):
            raise HTTPException(status_code=409, detail=f"Storage path already exists: {new_relative}") from None
        raise

    # A reparent can leave the vacated parent chain empty, and the library scan
    # turns every directory into a folder, so an empty leftover would reappear as
    # a folder of its own. Best effort, exactly like `asset_service.prune_empty_storage_dirs`.
    prune_empty_storage_dirs(source.parent)
    session.refresh(folder)
    return folder
