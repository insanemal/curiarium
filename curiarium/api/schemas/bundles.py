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
  "BundleEnrichItemOut",
  "BundleEnrichRequest",
  "BundleEnrichResponse",
  "BundleEnrichmentOut",
  "BundleGroupOut",
  "BundleRefusalOut",
  "BundleReportOut",
  "BundleRuleSetOut",
]


class BundleCoverOut(BaseModel):
  """The cover a created project would get, and why that one was chosen."""

  asset_id: Optional[str] = None
  rel_path: Optional[str] = None
  # largest-image | largest-image-unregistered | model-preview | none
  reason: str
  # Set exactly when asset_id is: the served paths for the cover's bytes.
  url: Optional[str] = None
  thumb_url: Optional[str] = None


class BundleArchiveOut(BaseModel):
  """A twin zip beside the bundle folder: provenance, never a member."""

  rel_path: str
  twin_verified: bool


class BundleGroupOut(BaseModel):
  """One model-bearing group inside a compound folder, and whether it is somebody else's thing."""

  name: str
  model_count: int
  foreign: bool


class BundleDuplicateGroupOut(BaseModel):
  """Byte-identical archives: one bundle, the rest reclaimable bytes."""

  sha256: str
  size: int
  paths: List[str]
  reclaimable_bytes: int


class BundleEnrichmentOut(BaseModel):
  """Which of the row's fields came from the vendor page, and when."""

  source_url: Optional[str] = None
  fetched_at: Optional[str] = None
  fields: List[str] = []


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
  # high | medium | low, orthogonal to `status`: a legacy bundle is high confidence and blocked.
  confidence: str
  metadata_shape: str
  description_excerpt: Optional[str] = None
  model_count: int = 0
  image_count: int = 0
  text_count: int = 0
  other_count: int = 0
  # Only a compound row fills this (`compound-bundle` / low: one product in
  # several parts). Additive and defaulted, so every pre-existing payload still
  # validates.
  groups: List[BundleGroupOut] = []
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
  # Which identity found it: marker | source_url | name | assets. On the wire
  # because the four are not equally certain - a marker is proof the app made
  # the project from this bundle, a source URL is strong, a name is a guess, and
  # asset containment proves the FILES are the same but nothing about intent.
  existing_project_match: Optional[str] = None
  dismissed: bool = False
  # A None means nothing on this row came from a page. Declared HERE or the
  # response model silently drops it - the way `rule_set` and `refused` were
  # lost before.
  enrichment: Optional[BundleEnrichmentOut] = None


class BundleDetectCountsOut(BaseModel):
  """The headline numbers, computed over the rows the report actually shows."""

  total: int = 0
  high: int = 0
  medium: int = 0
  low: int = 0
  ready: int = 0
  blocked: int = 0
  dismissed: int = 0
  already_created: int = 0


class BundleRuleSetOut(BaseModel):
  """The rule set one report was decided against: its revision, and what its tables were."""

  version: str
  stamp: str


class BundleRefusalOut(BaseModel):
  """One path the walk saw and did not offer, with the rule that refused it."""

  rel_path: str
  rule: str
  # archive | directory - what the path IS on disk, so a consumer can tell a
  # missing row from a missing folder without a second walk.
  kind: str


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
  # The rule set this whole report was decided against. REQUIRED, not defaulted:
  # every producer always emits it, so a default would only mask a producer that
  # forgot. Declared HERE or the response model silently drops it.
  rule_set: BundleRuleSetOut
  # Every path the walk refused to offer, one reason each. An empty list is a
  # legitimate answer ("nothing was refused"), never an omission.
  refused: List[BundleRefusalOut] = []


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


class BundleEnrichRequest(BaseModel):
  """The bundles whose vendor pages to read.

  `refresh` re-reads a page the cache already answered. The cookie is the same
  plumbing the import door uses and is never stored or audited.
  """

  model_config = ConfigDict(extra="forbid")
  keys: List[str] = []
  thingiverse_cookie: Optional[str] = None
  refresh: bool = False


class BundleEnrichItemOut(BaseModel):
  """What happened to one bundle: its status, its values and the reason for any refusal."""

  key: str
  # fetched | cached | refused | unavailable | unknown
  status: str
  source_url: Optional[str] = None
  creator: Optional[str] = None
  licence: Optional[str] = None
  licence_url: Optional[str] = None
  filled: List[str] = []
  fetched_at: Optional[str] = None
  reason: Optional[str] = None


class BundleEnrichResponse(BaseModel):
  """Per-status counters plus one result per requested bundle."""

  fetched: int = 0
  cached: int = 0
  refused: int = 0
  unavailable: int = 0
  unknown: int = 0
  results: List[BundleEnrichItemOut] = []
