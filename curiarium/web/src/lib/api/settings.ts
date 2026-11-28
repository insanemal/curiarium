// Server settings, library scans and the mounted import. Distinct from
// `../settings`, the client-side settings cache.

import { authHeaders } from "../auth";
import { apiBase } from "./baseUrl";
import { UnauthorizedError, assertOk, readErrorMessage } from "./http";
import {
  listOperations,
  planOperation,
  submitOperation,
  type Operation,
  type OperationStep,
} from "./operations";

export type MountImportSettings = {
  enabled: boolean;
  copy_files: boolean;
  path?: string | null;
};

export type StorageSettings = {
  template: string;
  default_template: string;
  allowed_tokens: string[];
  sample_path: string;
  moved: number;
  skipped: number;
};

export async function getMountImportSettings(): Promise<MountImportSettings> {
  const res = await fetch(`${apiBase()}/settings/mount-import`, { headers: authHeaders() });
  assertOk(res, "Failed to load mount import settings");
  return res.json();
}

export async function updateMountImportSettings(payload: {
  enabled: boolean;
  copy_files: boolean;
}): Promise<MountImportSettings> {
  const res = await fetch(`${apiBase()}/settings/mount-import`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  assertOk(res, "Failed to update mount import settings");
  return res.json();
}

export async function getStorageSettings(): Promise<StorageSettings> {
  const res = await fetch(`${apiBase()}/settings/storage`, { headers: authHeaders() });
  assertOk(res, "Failed to load storage settings");
  return res.json();
}

export async function updateStorageSettings(payload: {
  template: string;
}): Promise<StorageSettings> {
  const res = await fetch(`${apiBase()}/settings/storage`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) {
    throw new Error(await readErrorMessage(res, "Failed to update storage settings"));
  }
  return res.json();
}

/**
 * Snapshot capability of one storage volume, as detected from the mounted
 * filesystem. Detection is advisory: nothing is written to disk and no
 * snapshot is taken by reporting it.
 */
export type StorageVolumeCapability = {
  roles: string[];
  path: string;
  fstype: string;
  mount_point: string;
  snapshot_method: string;
  usable: boolean;
  reason: string;
};

export type StorageCapabilities = {
  volumes: StorageVolumeCapability[];
};

export async function getStorageCapabilities(): Promise<StorageCapabilities> {
  const res = await fetch(`${apiBase()}/storage/capabilities`, { headers: authHeaders() });
  assertOk(res, "Failed to load storage capabilities");
  return res.json();
}

export type LibraryScanStatus = {
  running: boolean;
  started_at: number | null;
  finished_at: number | null;
  registered: number;
  known: number;
  ignored: number;
  missing: number;
  failed: number;
  errors: string[];
};

/** The sentence a scan the server refused reports on its OWN operation row.
 *
 *  The scan slot can be held by a walker that is not an operation (a mounted
 *  import, say), and the server then ends the operation `failed` with this
 *  sentence instead of queueing a second walk. That is evidence a scan is
 *  running, not evidence this one broke, so `scanStatusFromOperation` reads it
 *  as `running: true` and every caller compares against this one constant
 *  rather than re-spelling the server's sentence. */
export const SCAN_ALREADY_RUNNING = "A library scan is already running";

/** The counters and the failure list of one scan - the fields a step reports,
 *  as opposed to the times and `running` the operation row itself carries. */
type ScanTally = Pick<
  LibraryScanStatus,
  "registered" | "known" | "ignored" | "missing" | "failed" | "errors"
>;

/** Nothing counted and nothing failed: a fresh object every call, so a caller
 *  that sorts `errors` cannot reorder another caller's status. */
function zeroTally(): ScanTally {
  return { registered: 0, known: 0, ignored: 0, missing: 0, failed: 0, errors: [] };
}

/** `Date.parse` answers milliseconds and this wire shape has always been epoch
 *  seconds, so the one conversion lives here; an unparseable time reads as
 *  unknown rather than as 1970. */
function epochSeconds(iso: string | null): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : ms / 1000;
}

/** A count a step reported, or zero. `result` is the server's JSON of a shape
 *  the client only half-knows, so a missing or wrong-typed field reads as
 *  "nothing counted" instead of throwing inside the panel. */
function reportedCount(result: Record<string, unknown>, key: string): number {
  const value = result[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function reportedErrors(result: Record<string, unknown>): string[] {
  const value = result.errors;
  if (!Array.isArray(value)) return [];
  return value.filter((entry): entry is string => typeof entry === "string");
}

/** What the scan step reported, read off the first step that is no longer
 *  `pending` - the only step this workflow plans.
 *
 *  A `done` step's `result` IS the old wire shape, so its counts and failure
 *  list are the scan's answer verbatim, defensively read because they are the
 *  server's JSON and not the client's types. A `failed` step wrote no counts at
 *  all, so its counters stay zero and its own sentence is the whole report. */
function tallyFromStep(step: OperationStep | undefined): ScanTally {
  if (!step) return zeroTally();
  if (step.status === "failed") {
    return step.error ? { ...zeroTally(), errors: [step.error] } : zeroTally();
  }
  if (!step.result) return zeroTally();
  return {
    registered: reportedCount(step.result, "registered"),
    known: reportedCount(step.result, "known"),
    ignored: reportedCount(step.result, "ignored"),
    missing: reportedCount(step.result, "missing"),
    failed: reportedCount(step.result, "failed"),
    errors: reportedErrors(step.result),
  };
}

/**
 * The library scan as one operation row words it, or the empty answer when the
 * ledger holds no scan at all.
 *
 * The browser's only evidence about a scan is the row the server wrote, so
 * every field is read off it and nothing is guessed: `running` is the row's own
 * status (`queued` and `running` mean a walk holds the slot, and so does a row
 * the server refused with `SCAN_ALREADY_RUNNING`), the times are the row's in
 * epoch seconds, and the counts are the scan step's report. A client that
 * derived any of this itself would drift from the runner the moment the
 * workflow changed.
 */
export function scanStatusFromOperation(row: Operation | null): LibraryScanStatus {
  if (!row) return { running: false, started_at: null, finished_at: null, ...zeroTally() };
  const refused = row.status === "failed" && row.error === SCAN_ALREADY_RUNNING;
  return {
    // The refusal is the scan slot speaking, not this operation failing: the
    // thing the operator asked for is already happening, just not as this row.
    running: row.status === "queued" || row.status === "running" || refused,
    started_at: epochSeconds(row.started_at),
    finished_at: epochSeconds(row.finished_at),
    ...tallyFromStep(row.steps.find(step => step.status !== "pending")),
  };
}

/**
 * What the last library scan said, derived from the operations ledger.
 *
 * A scan is one `library-scan` operation now - the two `GET`/`POST
 * /library/scan` routes it used to answer on are gone - so the newest row of
 * that workflow is the whole record: the runner owns it, and polling this reads
 * the status the server wrote instead of one the browser assembled. An empty
 * ledger answers zeros rather than throwing, because "no scan has run" is a
 * fact and the panel must be able to say so.
 */
export async function getLibraryScanStatus(): Promise<LibraryScanStatus> {
  const rows = await listOperations({ workflow: "library-scan", limit: 1 });
  return scanStatusFromOperation(rows[0] ?? null);
}

/**
 * Asks for a library scan of `scope`, as a plan the server issues and then one
 * queued operation - the same two calls every other workflow makes, because the
 * scan no longer has a route of its own.
 *
 * An empty `scope` is the whole library: the one unbounded walk, and the only one
 * the server rate-limits - one per ten minutes, refused with a 429 whose
 * sentence is thrown here verbatim so the caller can show it. Any other `scope`
 * is a library-relative folder: a bounded walk with no cooldown, because the
 * folder bounds both the cost and the frequency.
 *
 * A refused plan or submit still throws (the server says why); what does not
 * throw is a scan that cannot take the slot, because the server accepts the
 * operation and ends it `failed` with `SCAN_ALREADY_RUNNING`, which the derived
 * status reports as `running: true` - a scan IS walking, just not this row.
 */
export async function startLibraryScan(scope = ""): Promise<LibraryScanStatus> {
  const plan = await planOperation({ workflow: "library-scan", target: scope });
  const operation = await submitOperation({
    planId: plan.plan_id,
    idempotencyKey: crypto.randomUUID(),
  });
  return scanStatusFromOperation(operation);
}

/**
 * One mounted import as one `mount-import` operation row words it, or the empty
 * answer when the ledger holds no such row.
 *
 * A walk is a system operation now - it has no route of its own and no run
 * history table - so every field is read off the row the server wrote and
 * nothing is guessed: `status` is the row's own, the outcome and counters are
 * the single step's report, and the times are epoch seconds. `status === null`
 * is the one value the wire cannot produce: it means the ledger holds no walk
 * at all, which the panel states rather than dressing as a zeroed run.
 */
export type MountImportStatus = {
  status: Operation["status"] | null;
  /** The row is `queued` or `running`: a walk holds the queue right now. The
   *  badge and the poll both read this, so the rule lives in one place. */
  running: boolean;
  outcome: "registered" | "skipped" | null;
  /** `copy` or `reference`; a plain string because the server may add modes. */
  mode: string | null;
  root: string | null;
  reason: string | null;
  /** The failed step's own sentence, else the operation's: the walk's words
   *  first, because the runner quotes the step it was executing. */
  error: string | null;
  /** Epoch seconds. */
  started_at: number | null;
  /** Epoch seconds; null until the walk finished. */
  finished_at: number | null;
  registered: number;
  known: number;
  ignored: number;
  failed: number;
  /** Oversize subset of `ignored`. */
  rejected: number;
  discovered: number;
  /** `known + ignored`. */
  skipped: number;
  errors: string[];
};

/** Everything the walk's single step reports - the outcome, the policy it ran
 *  under and the counters - as opposed to the status and `running` the
 *  operation row itself carries. */
type MountImportReport = Pick<
  MountImportStatus,
  | "outcome"
  | "mode"
  | "root"
  | "reason"
  | "registered"
  | "known"
  | "ignored"
  | "failed"
  | "rejected"
  | "discovered"
  | "skipped"
  | "errors"
>;

/** Nothing walked and nothing counted: a fresh object every call, so a caller
 *  that sorts `errors` cannot reorder another caller's report. */
function emptyMountImportReport(): MountImportReport {
  return {
    outcome: null,
    mode: null,
    root: null,
    reason: null,
    registered: 0,
    known: 0,
    ignored: 0,
    failed: 0,
    rejected: 0,
    discovered: 0,
    skipped: 0,
    errors: [],
  };
}

/** A string a step reported, or null. Same defensive read as `reportedCount`:
 *  the report is the server's JSON, so a missing or wrong-typed field reads as
 *  "not recorded" rather than throwing inside the panel. */
function reportedString(result: Record<string, unknown>, key: string): string | null {
  const value = result[key];
  return typeof value === "string" ? value : null;
}

/** One of the two outcomes the walk can answer, or null for a report that named
 *  neither (a walk still running, or one that failed before reporting). The
 *  client recognises exactly the two words the server writes. */
function reportedOutcome(result: Record<string, unknown>): "registered" | "skipped" | null {
  const value = result.outcome;
  if (value === "registered" || value === "skipped") return value;
  return null;
}

/** What the walk's step reported. The report carries its own epoch seconds, so
 *  a finished walk's timing needs no conversion at all.
 *
 * A `failed` step wrote no report, so its counters stay zero and its sentence
 * is the whole of what it said - the same rule the scan fold follows. */
function reportFromStep(step: OperationStep | undefined): MountImportReport {
  if (!step || step.status === "failed" || !step.result) return emptyMountImportReport();
  const result = step.result;
  return {
    outcome: reportedOutcome(result),
    mode: reportedString(result, "mode"),
    root: reportedString(result, "root"),
    reason: reportedString(result, "reason"),
    registered: reportedCount(result, "registered"),
    known: reportedCount(result, "known"),
    ignored: reportedCount(result, "ignored"),
    failed: reportedCount(result, "failed"),
    rejected: reportedCount(result, "rejected"),
    discovered: reportedCount(result, "discovered"),
    skipped: reportedCount(result, "skipped"),
    errors: reportedErrors(result),
  };
}

/** An epoch-seconds number the report carries, or null. The report's times are
 *  already seconds, so this is a guard rather than a conversion. */
function reportedSeconds(result: Record<string, unknown> | null, key: string): number | null {
  if (!result) return null;
  const value = result[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * The mounted import as one operation row words it, or the empty answer when the
 * ledger holds no walk at all.
 *
 * The browser's only evidence about a walk is the row the server wrote, so the
 * status, `running` and the failure sentence are the row's own, and the outcome,
 * mode, root, reason and counters are the single step's report. The report's own
 * epoch seconds are preferred over the row's ISO times because they are the
 * walk's own clock; a step that never reported falls back to the row's, and a
 * row that never started has no times to convert.
 */
export function mountImportStatusFromOperation(row: Operation | null): MountImportStatus {
  if (!row) {
    return {
      status: null,
      running: false,
      error: null,
      started_at: null,
      finished_at: null,
      ...emptyMountImportReport(),
    };
  }
  const step = row.steps[0];
  const report = reportFromStep(step);
  return {
    status: row.status,
    // A `cancelling` row is not counted as running: its walk has already been
    // told to stop, and the badge must not keep promising a walk that is on its
    // way out.
    running: row.status === "queued" || row.status === "running",
    error: step?.error ?? row.error,
    started_at:
      reportedSeconds(step?.result ?? null, "started_at") ?? epochSeconds(row.started_at),
    finished_at:
      reportedSeconds(step?.result ?? null, "finished_at") ?? epochSeconds(row.finished_at),
    ...report,
  };
}

/**
 * What the newest mounted import said, derived from the operations ledger.
 *
 * The walk is one `mount-import` operation now - the `GET /imports/runs` pair it
 * used to answer on is gone - so the newest row of that workflow is the whole
 * record: the runner owns it, and polling this reads the status the server wrote
 * instead of one the browser assembled. During a walk the step has not reported
 * yet, so the counters stay zero until it does; that is the ledger's shape, not a
 * missing read. An empty ledger answers the empty status rather than throwing,
 * because "no walk has happened" is a fact and the panel must be able to say so.
 */
export async function getMountImportStatus(): Promise<MountImportStatus> {
  const rows = await listOperations({ workflow: "mount-import", limit: 1 });
  return mountImportStatusFromOperation(rows[0] ?? null);
}
