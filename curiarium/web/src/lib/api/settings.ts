// Server settings, library scans and import-run history. Distinct from
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
 * One mounted-import run as the API reports it. `status` stays a plain string
 * because the server may add outcomes; the UI treats unknown values as neutral.
 */
export type ImportRunSummary = {
  id: string;
  status: string;
  mode: string | null;
  root: string | null;
  reason: string | null;
  /** Epoch seconds. */
  started_at: number;
  /** Epoch seconds; null while the run is still walking. */
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
};

export type ImportRunDetail = ImportRunSummary & { errors: string[] };

export type ImportRunList = {
  runs: ImportRunSummary[];
  total: number;
  limit: number;
  offset: number;
};

/** Newest-first page of mounted-import runs. */
export async function getImportRuns(limit = 20, offset = 0): Promise<ImportRunList> {
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  const res = await fetch(`${apiBase()}/imports/runs?${qs.toString()}`, { headers: authHeaders() });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) {
    throw new Error(await readErrorMessage(res, "Failed to load mount import runs"));
  }
  return res.json();
}

/** One run with its per-file errors (already bounded server-side). */
export async function getImportRun(id: string): Promise<ImportRunDetail> {
  const res = await fetch(`${apiBase()}/imports/runs/${encodeURIComponent(id)}`, {
    headers: authHeaders(),
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) {
    throw new Error(await readErrorMessage(res, "Failed to load the mount import run"));
  }
  return res.json();
}
