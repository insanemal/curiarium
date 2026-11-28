// The operations ledger: server-computed plans, one shared queue, a server-side
// runner. Field names are the server's snake_case verbatim - the payloads are
// served straight from the API.
//
// The server computes every plan. A client cannot invent a step list: planning
// answers a `plan_id` the server issued, and submitting sends that id back with
// an idempotency key and nothing else. The queue is ONE shared queue attributed
// to the submitting user, not a queue per user, so two operations over the same
// paths serialise instead of racing; the runner that drains it lives on the
// server. The client therefore polls `getOperation` for progress and never
// invents a step or a status of its own - the row is the record of the intent,
// and the steps on it are what the runner actually did.
//
// A `split-and-create` review carries both halves of its intent - `move_list`
// (the hoists) and `projects` (the creates) - because the operator approves both
// in one action: two payloads would let a click approve half a plan.

import { authHeaders } from "../auth";
import { apiBase } from "./baseUrl";
import { UnauthorizedError, assertOk, readErrorMessage } from "./http";

/** One file or directory the plan would move. Both paths are relative to the
 *  storage root, `kind` is what the path IS on disk (`file | dir`) and `action`
 *  is WHY the move exists (`hoist | move | file-archive | rename`). */
export type OperationMove = {
  source: string;
  target: string;
  kind: string;
  action: string;
};

/** One project the plan would create, with the members it would hold. */
export type OperationProject = {
  name: string;
  root: string;
  members: number;
};

/** One group inside the target, and how many of its files are model-bearing. */
export type OperationGroup = {
  name: string;
  members: number;
  models: number;
};

/** The cover a created project would get, and why that one was chosen.
 *  `asset_id`/`rel_path` are set exactly when the server found a face to set. */
export type OperationCover = {
  asset_id: string | null;
  rel_path: string | null;
  reason: string;
};

/** One file the plan would leave out, and the server's own reason - rendered
 *  verbatim, because the client did not decide the exclusion. */
export type OperationExcluded = {
  path: string;
  reason: string;
};

/** One member of an archive the plan read: its path, its bytes, and the
 *  server's `junk` verdict. These are the rows the deleted
 *  `GET /import/archive/entries` listing served, in the same order, so an entry
 *  picker can tick members by size and disable the ones the extractor will not
 *  register. */
export type OperationEntry = {
  path: string;
  size: number;
  junk: boolean;
};

/** Everything the operator approves in one action. The counts are the plan's;
 *  `move_list` is the moves half and `projects` the creates half, so a
 *  split-and-create review is one payload naming both. */
export type OperationReview = {
  workflow: string;
  target: string;
  moves: number;
  creates: number;
  adopts: number;
  deletes: number;
  members: number;
  groups: OperationGroup[];
  /** The census of the members a plan would add: one key per scan role
   *  (model/image/text/archive/other, zeros included), so
   *  `sum(by_role.values()) === members` and the mix reads without counting
   *  rows. Optional because the server sends it for every plan but a client
   *  built against an older API must still be able to read a review: a missing
   *  census reads as no census, never as five fabricated zeros. */
  by_role?: Record<string, number>;
  /** The archive's own listing, for the two extract workflows only. Optional
   *  for the same reason as `by_role`: a client built against an older API must
   *  still read a review, and a missing listing reads as no listing, never as
   *  an empty archive. */
  entries?: OperationEntry[];
  cover: OperationCover | null;
  excluded: OperationExcluded[];
  existing_project_id: string | null;
  existing_project_name: string | null;
  move_list: OperationMove[];
  projects: OperationProject[];
};

/** One step of the plan as the server names it. `summary` is the server's own
 *  sentence: two renderings of one step would drift, so the client shows this. */
export type OperationPlanStep = {
  index: number;
  op: string;
  summary: string;
};

/** A plan the server computed and will recompute at submit. `plan_id` is the
 *  whole check: a library that moved since this was issued refuses the submit
 *  rather than running something nobody reviewed. */
export type OperationPlan = {
  plan_id: string;
  workflow: string;
  target: string;
  steps: OperationPlanStep[];
  review: OperationReview;
};

/** One step's outcome on the ledger. `result` is the step's own report as the
 *  server wrote it (untrusted, shape per `op`), and `error` is set exactly when
 *  the step failed - the pair is what says a step ran, not a status guess. */
export type OperationStep = {
  index: number;
  op: string;
  status: string;
  result: Record<string, unknown> | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
};

/** One operation row: the durable record of an intent, plus the steps the
 *  runner wrote against it. `undo_state` and `superseded_by` are the server's
 *  own answer about whether an undo is still meaningful - a superseded operation
 *  is one a later operation already changed the same paths after. */
export type Operation = {
  id: string;
  workflow: string;
  target: string;
  status: string;
  submitted_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  undo_state: string;
  superseded_by: string | null;
  steps: OperationStep[];
};

/** The verdict of an undo. `moves` counts the moves that were folded back (null
 *  when the workflow has no move ledger) and `projects_removed` names only the
 *  projects the undo itself deleted, so a caller never reads the response as
 *  "the whole thing was erased". */
export type OperationUndo = {
  operation_id: string;
  undone: boolean;
  project_id: string | null;
  project_name: string | null;
  moves: number | null;
  projects_removed: string[] | null;
  /** An `adopt` undo answers the project it restored and how many memberships
   *  it took back; the other workflows leave it null, and an older server omits
   *  the key entirely - both read as "not measured", never as zero. */
  members_removed?: number | null;
  /** The asset rows an `import-link` or `import-zip` undo removed. */
  assets_removed?: number | null;
  /** The mirrored folder rows an `import-zip` undo removed. */
  folders_removed?: number | null;
  /** The entries an `import-zip` run itself could not write, echoed back so the
   *  operator's retry list survives the undo. */
  entries_failed?: string[] | null;
  /** Whether an `attach-project` undo left or removed the project it targeted. */
  project_removed?: boolean | null;
  /** The `extract` undo's sentence saying the extracted files stay. */
  payload_residue?: string | null;
  /** The enrichment-cache records an `enrich` undo dropped. */
  records_dropped?: number | null;
  /** The enrichment-cache records an `enrich` undo kept because a later enrich
   *  had replaced them. */
  records_kept?: number | null;
};

/** What the workflow WOULD do: a dry run the server computes and writes nothing
 *  for. It answers the `plan_id` the submit needs, so the plan is fetched here
 *  and never assembled client-side. */
export async function planOperation(input: {
  workflow: string;
  target: string;
  params?: Record<string, unknown>;
}): Promise<OperationPlan> {
  const res = await fetch(`${apiBase()}/operations/plan`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      workflow: input.workflow,
      target: input.target,
      params: input.params ?? {},
    }),
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) {
    throw new Error(await readErrorMessage(res, "Failed to plan the operation"));
  }
  return res.json();
}

/** Queues the plan the operator reviewed. The client cannot invent a step list:
 *  the `plan_id` is server-issued, and one idempotency key belongs to one intent
 *  - a replay of the same key answers the same operation rather than queueing a
 *  second one. The server recomputes the plan at submit, so a stale `plan_id`
 *  is refused (409) instead of running something nobody read. */
export async function submitOperation(input: {
  planId: string;
  idempotencyKey: string;
}): Promise<Operation> {
  const res = await fetch(`${apiBase()}/operations`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ plan_id: input.planId, idempotency_key: input.idempotencyKey }),
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) {
    throw new Error(await readErrorMessage(res, "Failed to submit the operation"));
  }
  return res.json();
}

/** Reads one operation with its steps. A READ: the runner owns the row, and
 *  polling this is the only way the client learns progress - it never derives a
 *  status the server did not write. */
export async function getOperation(operationId: string): Promise<Operation> {
  const res = await fetch(`${apiBase()}/operations/${encodeURIComponent(operationId)}`, {
    headers: authHeaders(),
  });
  assertOk(res, "Failed to read the operation");
  return res.json();
}

/** Pages the ledger, newest first. Every filter is the server's own (`workflow`,
 *  `state`, `target`, `submitted_by`, and a `limit`/`offset` page), so "the
 *  latest scan operation" or "the operation for THIS target" is one query
 *  rather than a client scan of a truncated page. A READ like `getOperation`,
 *  and the same rule applies: the rows are what the runner wrote, so a caller
 *  reads a status off them rather than inventing one. */
export async function listOperations(
  input: {
    workflow?: string;
    state?: string;
    target?: string;
    submitted_by?: string;
    limit?: number;
    offset?: number;
  } = {},
): Promise<Operation[]> {
  const qs = new URLSearchParams();
  if (input.workflow) qs.set("workflow", input.workflow);
  if (input.state) qs.set("state", input.state);
  // `!= null`, never truthiness: `""` is a legal target (the library root), and
  // dropping it would widen the query to every target instead of narrowing it.
  if (input.target != null) qs.set("target", input.target);
  if (input.submitted_by != null) qs.set("submitted_by", input.submitted_by);
  if (input.limit != null) qs.set("limit", String(input.limit));
  if (input.offset != null) qs.set("offset", String(input.offset));
  const query = qs.toString();
  const res = await fetch(`${apiBase()}/operations${query ? `?${query}` : ""}`, {
    headers: authHeaders(),
  });
  assertOk(res, "Failed to load operations");
  return res.json();
}

/** Folds one finished operation back. The request carries no body at all: the id
 *  in the path IS the argument, and the ledger the run wrote says what to undo.
 *  The server answers 409 with its own `detail` for every case it refuses - an
 *  already-undone or superseded operation, or one that changed nothing. */
export async function undoOperation(operationId: string): Promise<OperationUndo> {
  const res = await fetch(`${apiBase()}/operations/${encodeURIComponent(operationId)}/undo`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) {
    throw new Error(await readErrorMessage(res, "Failed to undo the operation"));
  }
  return res.json();
}

/** Asks a queued operation to stop now and a running one to stop after the step
 *  it is in. The server's 409 sentence is shown verbatim: it refused for a reason
 *  the operator needs to read (an operation that already finished, or one that
 *  was never queued). */
export async function cancelOperation(operationId: string): Promise<Operation> {
  const res = await fetch(`${apiBase()}/operations/${encodeURIComponent(operationId)}/cancel`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) {
    throw new Error(await readErrorMessage(res, "Failed to cancel the operation"));
  }
  return res.json();
}
