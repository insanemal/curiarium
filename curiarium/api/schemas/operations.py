"""Operations: plans, submissions, the queue and undo (Mf-5t30).

The server computes every plan. A client sends a `workflow` and a `target` and
gets back a `plan_id`; `POST /operations` accepts that `plan_id` and an
`idempotency_key` and nothing else, so a client cannot invent a step list. Every
class is prefixed `Operation` because the OpenAPI fixtures pin bare component
refs and a generic `GroupOut` or `StepOut` would collide with a future domain.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

__all__ = [
  "OperationCoverOut",
  "OperationExcludedOut",
  "OperationGroupOut",
  "OperationMoveOut",
  "OperationOut",
  "OperationPlanOut",
  "OperationPlanRequest",
  "OperationPlanStepOut",
  "OperationProjectOut",
  "OperationReviewOut",
  "OperationStepOut",
  "OperationSubmitRequest",
  "OperationUndoOut",
]


class OperationGroupOut(BaseModel):
  """One first-level directory under the target, and what it holds."""

  name: str
  members: int
  models: int


class OperationCoverOut(BaseModel):
  """The cover a create would pick, and why; `asset_id` is None for no cover."""

  asset_id: Optional[str] = None
  rel_path: Optional[str] = None
  reason: str


class OperationExcludedOut(BaseModel):
  """One file or directory the plan leaves out, with a human reason."""

  path: str
  reason: str


class OperationMoveOut(BaseModel):
  """One move a plan would make, both ends storage-root-relative.

  ``source``/``target`` rather than ``from``/``to`` because ``from`` is a Python
  keyword; unlike the deleted arrange model this does not alias, because the
  operations review is the only place the pair is served and the wire reads
  better naming both ends symmetrically.

  ``kind`` is what the path IS (``file``/``dir``); ``action`` is why the move
  exists (``hoist`` for a split). The client renders the line from these records
  and never from prose.
  """

  source: str
  target: str
  kind: str
  action: str


class OperationProjectOut(BaseModel):
  """One project a plan would create: its name, the path it lives at, its size.

  ``members`` is the count of files the plan would attach, after every exclusion,
  so it is the number the create step will actually write - not a folder's file
  count.
  """

  name: str
  root: str
  members: int


class OperationReviewOut(BaseModel):
  """The human-readable summary of what a plan would do, before it runs."""

  workflow: str
  target: str
  moves: int
  creates: int
  adopts: int
  deletes: int
  members: int
  groups: List[OperationGroupOut] = []
  cover: Optional[OperationCoverOut] = None
  excluded: List[OperationExcludedOut] = []
  existing_project_id: Optional[str] = None
  existing_project_name: Optional[str] = None
  # The other half of a plan that moves bytes: the moves themselves and the
  # projects they feed. Empty for a workflow that makes neither, so a
  # `create-project` review answers with two empty lists rather than omitting the
  # keys a client may read unconditionally.
  move_list: List[OperationMoveOut] = []
  projects: List[OperationProjectOut] = []
  # The census of the members the plan would add, one key per role the scan can
  # assign (model/image/text/archive/other, zeros included), so
  # `sum(by_role.values()) == members` and the client can render the mix without
  # counting rows.
  by_role: Dict[str, int] = {}


class OperationPlanStepOut(BaseModel):
  """One step of a plan; `args` stay server-side, so only the shape is shown."""

  index: int
  op: str
  summary: str


class OperationPlanRequest(BaseModel):
  """What to do and where.

  `extra="forbid"` is the proof a client cannot smuggle a step list in alongside
  `workflow` and `target`.
  """

  model_config = ConfigDict(extra="forbid")
  workflow: str
  target: str
  params: Dict[str, Any] = {}


class OperationPlanOut(BaseModel):
  """A computed plan: the opaque id to submit, plus the steps and review."""

  plan_id: str
  workflow: str
  target: str
  steps: List[OperationPlanStepOut]
  review: OperationReviewOut


class OperationSubmitRequest(BaseModel):
  """A submit names a plan this server issued and an idempotency key; no steps."""

  model_config = ConfigDict(extra="forbid")
  plan_id: str
  idempotency_key: str


class OperationStepOut(BaseModel):
  """One step as read back; `args_json` is never exposed."""

  index: int
  op: str
  status: str
  result: Optional[Dict[str, Any]] = None
  error: Optional[str] = None
  started_at: Optional[str] = None
  finished_at: Optional[str] = None


class OperationOut(BaseModel):
  """One operation and its steps, newest state as stored."""

  id: str
  workflow: str
  target: str
  status: str
  submitted_by: str
  created_at: str
  started_at: Optional[str] = None
  finished_at: Optional[str] = None
  error: Optional[str] = None
  undo_state: str
  superseded_by: Optional[str] = None
  steps: List[OperationStepOut] = []


class OperationUndoOut(BaseModel):
  """The result of undoing a finished operation, shaped by its workflow.

  A `create-project` undo answers the project it deleted and leaves every
  workflow-specific key null; a `split-and-create` undo answers ``moves`` (how
  many landed moves were folded back) and ``projects_removed`` (the projects it
  created and then deleted), and leaves the project pair null. An `adopt` undo
  answers the project it RESTORED plus how many memberships it took back
  (``members_removed``); `create-project` and `split-and-create` leave it null.
  An `import-link` undo answers the asset it retired (``asset_id`` and
  ``filename``) and how many asset rows it removed (``assets_removed``); an
  `import-zip` undo answers how many assets and mirrored folder rows it removed
  (``assets_removed``/``folders_removed``) and echoes the entries the run itself
  could not write (``entries_failed``), so the operator's retry list survives.
  An `extract` undo answers the folder rows it pruned and ``payload_residue``,
  the sentence that says the extracted files stay; an `attach-project` undo
  answers the project it left or removed (``project_removed``) and how many
  memberships it took back.
  Optional on both sides so neither body gains a fabricated zero: a leg says only
  what its own run wrote, and everything it has nothing to say about stays null.
  """

  operation_id: str
  undone: bool
  project_id: Optional[str] = None
  project_name: Optional[str] = None
  moves: Optional[int] = None
  projects_removed: Optional[List[str]] = None
  members_removed: Optional[int] = None
  asset_id: Optional[str] = None
  filename: Optional[str] = None
  assets_removed: Optional[int] = None
  folders_removed: Optional[int] = None
  entries_failed: Optional[List[str]] = None
  project_removed: Optional[bool] = None
  payload_residue: Optional[str] = None
