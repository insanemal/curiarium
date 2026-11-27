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
  "OperationOut",
  "OperationPlanOut",
  "OperationPlanRequest",
  "OperationPlanStepOut",
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
  """The result of undoing a finished operation."""

  operation_id: str
  undone: bool
  project_id: Optional[str] = None
  project_name: Optional[str] = None
