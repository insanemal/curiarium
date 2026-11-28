"""The bundle detection report and the two confirmed actions (Mf-vwqf).

The report is computed from disk on every read, so nothing here is stored: the
response is a snapshot of what the storage root looks like right now. Every class
is prefixed `Bundle` because the OpenAPI fixtures pin bare component refs and a
generic `CountsOut` would collide with a future domain.
"""

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict

from .batch import BatchFailure

__all__ = [
  "BundleArchiveOut",
  "BundleCoverCandidateOut",
  "BundleCoverChoiceIn",
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
  "BundleEnrichmentOut",
  "BundleGroupOut",
  "BundleImageOut",
  "BundleMatchOut",
  "BundleProvenanceAlsoInOut",
  "BundleProvenanceCandidateOut",
  "BundleProvenanceOut",
  "BundleProvenanceUnresolvedOut",
  "BundleRefusalOut",
  "BundleReportOut",
  "BundleRuleSetOut",
  "BundleSuggestionOut",
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
  # REQUIRED, never defaulted: a defaulted False would silently switch every
  # checkbox off. This is a SERVICE fact - was this directory a row of its own
  # before R7 absorbed it, i.e. can excluding it leave a creatable row behind -
  # which the pure detector cannot know, so `BundleFacts.groups`/`BundleGroup`
  # deliberately stay three-field and the rule-set stamp is untouched.
  separable: bool


class BundleMatchOut(BaseModel):
  """One shape a listing satisfies: the rule that offered it and its counts.

  ``matches[0]`` on a row is the recommended shape and equals the row's own
  `detector`/`confidence`/`metadata_shape`; the rest are the alternatives. The
  manual entry (shape `manual`) is always present and last unless the row itself
  recommends it, and `note` carries the refusal word a manual offer was built
  from.

  A match the server cannot enact yet is still offered, because withholding it
  would hide the reading that disagrees with the detector - an honest reading is
  worth more than a short list. ``creatable=False`` plus a ``note`` saying why
  is the contract: the client must not let the operator pick such a shape, and
  the create path refuses it with ``needs-arrange``. ``creatable`` means "this
  shape is enactable once the row is ready; the row's own ``status`` still gates
  the create".
  """

  shape: str
  rule: str
  confidence: str
  model_count: int = 0
  image_count: int = 0
  text_count: int = 0
  other_count: int = 0
  groups: List[BundleGroupOut] = []
  note: Optional[str] = None
  # Defaulted True on purpose: every shape the detector offers is creatable, so
  # the permissive value is the one a client that predates this field assumes.
  creatable: bool = True


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


class BundleSuggestionOut(BaseModel):
  """One value the engine mined from a document, with WHERE it came from.

  A SUGGESTION, never a fact: nothing is written unless the operator accepts it.
  `where` is the document and page the engine read, e.g. "manual.pdf p2".
  """

  field: str  # title | creator | licence | licence_url | source_url | description
  value: str
  where: str
  confidence: str  # the engine's band for THIS field: high | medium | low


class BundleProvenanceAlsoInOut(BaseModel):
  """A second sighting of a provider subject the engine already listed."""

  where: str
  confidence: str


class BundleProvenanceCandidateOut(BaseModel):
  """A provider URL the engine found in a document, and where it saw it."""

  url: str
  provider: str
  subject_id: Optional[str] = None
  where: str
  confidence: str
  # The engine keeps every weaker sighting of the same subject, bounded, so the
  # strongest location is `where` above and the rest are here. Additive and
  # defaulted, so a payload from before this field still validates.
  also_in: List[BundleProvenanceAlsoInOut] = []


class BundleProvenanceUnresolvedOut(BaseModel):
  """A provider URL with no model id (a truncated extraction), never an invented id."""

  text: str
  where: str
  reason: str


class BundleProvenanceOut(BaseModel):
  """The provider URLs the engine mined, for the guided panel's source field."""

  candidates: List[BundleProvenanceCandidateOut] = []
  unresolved: List[BundleProvenanceUnresolvedOut] = []
  confidence: str = "suggested"


class BundleCoverCandidateOut(BaseModel):
  """One image the engine's walk would accept as a face, bundle-relative.

  `asset_id`/`url`/`thumb_url` are set exactly when the same file is also a
  REGISTERED asset of this row, so the chooser can only offer a face it can set.
  """

  rel_path: str
  reason: str  # largest-image | model-preview
  bytes: int = 0
  asset_id: Optional[str] = None
  url: Optional[str] = None
  thumb_url: Optional[str] = None


class BundleImageOut(BaseModel):
  """One registered image of the row: the chooser's selectable faces."""

  asset_id: str
  rel_path: str
  url: str
  thumb_url: Optional[str] = None


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
  # The `archive` role's count, from this row's own facts: the nested zips the
  # detector counted. It exists ONLY here - never on `BundleMatchOut`, never in
  # `bundle_serialize.COUNT_FIELDS` - so a before/after pair of report artifacts
  # stays comparable across the change that added it. Optional and defaulted, so a
  # pre-existing payload still validates.
  archive_count: Optional[int] = None
  # Only a compound row fills this (`compound-bundle` / low: one product in
  # several parts). Additive and defaulted, so every pre-existing payload still
  # validates.
  groups: List[BundleGroupOut] = []
  # Every shape this listing satisfies, recommended first and the manual scope
  # last. ADDITIVE and defaulted, so a pre-existing payload still validates -
  # and declared HERE or the response model silently drops it, the same trap
  # `rule_set` and `refused` fell into.
  matches: List[BundleMatchOut] = []
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
  # Drift on an already-created row (Mf-8i04): `not_applicable` | `known` |
  # `unknown`, and the honest count only when `known`. Defaulted, so every row
  # that is not marker-matched says nothing extra and every pre-existing payload
  # still validates. Declared HERE or the response model silently drops both -
  # the same trap `rule_set` and `refused` fell into. The server never formats a
  # number and the state is its word for which of the three cases it is: the
  # client renders `known`-0 as no notice at all and `unknown` as an en dash.
  bundle_drift_state: str = "not_applicable"
  bundle_drift_count: Optional[int] = None
  dismissed: bool = False
  # A None means nothing on this row came from a page. Declared HERE or the
  # response model silently drops it - the way `rule_set` and `refused` were
  # lost before.
  enrichment: Optional[BundleEnrichmentOut] = None
  # What a document CLAIMED and where, for the guided panel's accept/edit/dismiss
  # list. Every one of the four below is ADDITIVE and defaulted, so a pre-existing
  # payload still validates - and each is declared HERE or the response model
  # silently drops it, the same trap `rule_set` and `refused` fell into.
  metadata_suggestions: List[BundleSuggestionOut] = []
  # None means the row is a `.zip`, whose facts carry no provenance at all.
  provenance: Optional[BundleProvenanceOut] = None
  cover_candidates: List[BundleCoverCandidateOut] = []
  images: List[BundleImageOut] = []


class BundleDetectCountsOut(BaseModel):
  """The headline numbers, computed over the rows the report actually shows."""

  # The rows this report HAS. `total` keeps that meaning (Mf-1agz): it is not the
  # number the wire carried.
  total: int = 0
  high: int = 0
  medium: int = 0
  low: int = 0
  ready: int = 0
  blocked: int = 0
  dismissed: int = 0
  already_created: int = 0
  # What the wire actually carried, and how many rows the `limit` kept back
  # (Mf-1agz). Defaulted, so every pre-existing payload still validates; a report
  # bound that said only `truncated: true` made the operator guess whether the row
  # they were looking for was one of the ones dropped.
  offered: int = 0
  withheld: int = 0


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


class BundleCoverChoiceIn(BaseModel):
  """The operator's cover decision on the guided panel.

  `mode` is `asset` (set this registered image) or `none` (no cover). OMITTING
  the whole field means the engine's own choice stands, which is why the field
  is optional rather than a nullable asset id: an absent choice and a deliberate
  "no cover" must not be the same request.
  """

  model_config = ConfigDict(extra="forbid")
  mode: str
  asset_id: Optional[str] = None


class BundleCreateItemIn(BaseModel):
  """One confirmed bundle; a key the fresh report does not know is `failed`."""

  model_config = ConfigDict(extra="forbid")
  key: str
  name: Optional[str] = None
  name_only: bool = False
  # The compound row's foreign groups the operator says are NOT this product.
  # Additive and defaulted, so a pre-existing payload still validates; each name
  # must be a group the report marked `separable`, else the item fails.
  exclude_groups: List[str] = []
  # One of the row's `matches` shapes; None means the row's recommended shape.
  # An unoffered shape answers `failed`/`unknown shape`.
  shape: Optional[str] = None
  # AUTO (Mf-yz9e): one action, high confidence only. The server refuses every
  # other band and every shape it cannot do safely; a client never decides a band.
  auto: bool = False
  # The cover the operator chose; absent means the engine's choice.
  cover: Optional[BundleCoverChoiceIn] = None
  # The metadata the operator ACCEPTED, field -> value (an edit) or None (a
  # dismiss). When present, fields absent from this map are NEVER written, so a
  # licence the operator did not accept is never written. Absent = legacy create.
  metadata: Optional[Dict[str, Optional[str]]] = None


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
