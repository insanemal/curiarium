"""Folder creation, read-back and delete preview (Mf-avo9 split)."""

from typing import List, Optional

from pydantic import BaseModel

from .assets import SnapshotNoticeOut

__all__ = [
  "FolderDeletePreviewOut",
  "FolderIn",
  "FolderOut",
]


class FolderIn(BaseModel):
  name: str
  tags: List[str] = []
  parent_id: Optional[str] = None


class FolderDeletePreviewOut(BaseModel):
  folders: int
  assets: int
  files: int
  bytes: int
  in_trash: bool
  # Conservative: True whenever the delete would rename bytes into .trash,
  # including a NULL-path folder whose name-derived directory exists in storage.
  would_move_bytes: bool
  # The row's own directory identity (NULL for a migrated legacy row), so the
  # UI can badge a delete that would move bytes it cannot name.
  storage_path: Optional[str] = None
  snapshot: Optional[SnapshotNoticeOut] = None


class FolderOut(BaseModel):
  id: str
  name: str
  tags: List[str]
  parent_id: Optional[str]
  storage_path: Optional[str] = None
