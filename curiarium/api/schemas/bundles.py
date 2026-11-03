"""The bundle detection report and the two confirmed actions (Mf-vwqf).

The report is computed from disk on every read, so nothing here is stored: the
response is a snapshot of what the storage root looks like right now. Every class
is prefixed `Bundle` because the OpenAPI fixtures pin bare component refs and a
generic `CountsOut` would collide with a future domain.
"""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict

from .batch import BatchFailure

__all__ = [
  "BundleArchiveOut",
  "BundleCoverOut",
  "BundleCreateItemIn",
  "BundleCreateItemOut",
  "BundleCreateRequest",
  "BundleCreateResponse",
  "BundleDetectCountsOut",
  "BundleDetectItemOut",
  "BundleDismissRequest",
  "BundleDismissResponse",
  "BundleDuplicateGroupOut",
  "BundleReportOut",
]


class BundleCoverOut(BaseModel):
  """The cover a created project would get, and why that one was chosen."""

  asset_id: Optional[str] = None
  rel_path: Optional[str] = None
  # largest-image | largest-image-unregistered | none
  reason: str


class BundleArchiveOut(BaseModel):
  """A twin zip beside the bundle folder: provenance, never a member."""

  rel_path: str
  twin_verified: bool


class BundleDuplicateGroupOut(BaseModel):
  """Byte-identical archives: one bundle, the rest reclaimable bytes."""

  sha256: str
  size: int
  paths: List[str]
  reclaimable_bytes: int


class BundleDetectItemOut(BaseModel):
  """One detected bundle: what it is, what it counts, and whether it can be created."""

  key: str
  rel_path: str
  title: str
  name_suggestion: str
  creator: Optional[str] = None
  licence: Optional[str] = None
  licence_url: Optional[str] = None
  source_url: Optional[str] = None
  detector: str
  # high | low, orthogonal to `status`: a legacy bundle is high confidence and blocked.
  confidence: str
  metadata_shape: str
  description_excerpt: Optional[str] = None
  model_count: int = 0
  image_count: int = 0
  text_count: int = 0
  other_count: int = 0
  cover: BundleCoverOut
  assets_total: int = 0
  # ready | blocked
  status: str
  blocked_reason: Optional[str] = None
  archive: Optional[BundleArchiveOut] = None
  # The pair is derived from one Project row on every read: the id is the
  # identity every check keys on, the name is what the panel says. A name the
  # project no longer has (blank/whitespace) is reported as None, never as the
  # id - the client owns the honest nameless line.
  existing_project_id: Optional[str] = None
  existing_project_name: Optional[str] = None
  dismissed: bool = False


class BundleDetectCountsOut(BaseModel):
  """The headline numbers, computed over the rows the report actually shows."""

  total: int = 0
  high: int = 0
  low: int = 0
  ready: int = 0
  blocked: int = 0
  dismissed: int = 0
  already_created: int = 0


class BundleReportOut(BaseModel):
  """A dry-run view of the storage root. Reading it writes nothing."""

  root: str
  truncated: bool = False
  counts: BundleDetectCountsOut
  bundles: List[BundleDetectItemOut] = []
  # Byte-identical archives are one bundle: this list is the copies, never rows.
  duplicates: List[BundleDuplicateGroupOut] = []
  duplicates_total_bytes: int = 0
  duplicates_text: str = ""
  # Dismissal keys that match no live bundle: a renamed directory un-dismisses.
  stale_dismissals: List[str] = []


class BundleCreateItemIn(BaseModel):
  """One confirmed bundle; a key the fresh report does not know is `failed`."""

  model_config = ConfigDict(extra="forbid")
  key: str
  name: Optional[str] = None
  name_only: bool = False


class BundleCreateRequest(BaseModel):
  """The confirmed selection. Partial success is the contract, like `/assets/batch/*`."""

  model_config = ConfigDict(extra="forbid")
  items: List[BundleCreateItemIn] = []


class BundleCreateItemOut(BaseModel):
  """What happened to one item: created, skipped, blocked or failed."""

  key: str
  # created | skipped | blocked | failed
  status: str
  project_id: Optional[str] = None
  name: Optional[str] = None
  assets_added: int = 0
  skipped_assets: int = 0
  failed_assets: List[BatchFailure] = []
  reason: Optional[str] = None


class BundleCreateResponse(BaseModel):
  """Per-status counters plus one result per requested item."""

  created: int = 0
  skipped: int = 0
  blocked: int = 0
  failed: int = 0
  results: List[BundleCreateItemOut] = []


class BundleDismissRequest(BaseModel):
  """A toggle, so a dismissal always has a visible undo."""

  model_config = ConfigDict(extra="forbid")
  keys: List[str] = []
  dismissed: bool = True


class BundleDismissResponse(BaseModel):
  """The dismissal map after the change, plus the keys that match no bundle."""

  dismissed: List[str] = []
  stale_dismissals: List[str] = []
