// One review action - list, extract, settle, create - kept free of React so its
// order is unit-testable without a DOM.
//
// Two measured server facts shape the flow:
//
// - The extract is all-or-nothing. `POST /import/archive` either writes every
//   entry it took or writes none, so a failure leaves nothing half-written:
//   there is no partial extraction for the client to clean up, which is why
//   `ExtractAndCreateDeps` has no rollback member at all.
// - The extract's scan is asynchronous. `archive_service` ends with
//   `scan_started = bool(seams.start_scan())`, and `library_scan.start_scan()`
//   only starts a daemon thread and returns at once, so the extracted files are
//   invisible to the bundle report until the walk registers them. That is why
//   the create waits on `settle`, and why a scan that never proves itself
//   abandons the run rather than creating a project the server would refuse as
//   still-blocked.
//
// `deps.create` returning `null` means the hook has no response to report - the
// queue was left, or the request failed and the hook's own error line already
// speaks it - so this module adds no sentence of its own for it. That is why
// `null` maps to `cancelled` rather than to `create-failed`.

import type {
  BundleCreateItem,
  BundleCreateItemResult,
  BundleCreateResponse,
} from "../../lib/api/bundles";
import type { ArchiveEntry, ArchiveExtractResult } from "../../lib/api/imports";
import type { SettleOutcome } from "../../lib/scanSettle";
import { BLOCKED_REASON_LABELS } from "./bundleRows";

export type ExtractAndCreatePhase = "listing" | "extracting" | "scanning" | "creating";

/** The one row the action runs on. `name` is the operator's typed name, already
 *  trimmed; null/absent means the server's name suggestion is used. */
export type ExtractAndCreateTarget = { key: string; name?: string | null };

export type ExtractAndCreateDeps = {
  listEntries: (path: string) => Promise<{ path: string; entries: ArchiveEntry[] }>;
  extract: (payload: { path: string; entries?: string[] | null }) => Promise<ArchiveExtractResult>;
  settle: (results: ArchiveExtractResult[]) => Promise<SettleOutcome>;
  /** null means "no response arrived": the queue was left, or the request
   *  failed and the hook's own error line already speaks it. */
  create: (items: BundleCreateItem[]) => Promise<BundleCreateResponse | null>;
  /** Fire-and-forget cover prime, exactly as the existing create doors do. */
  beforeCreate?: () => void;
  onPhase?: (phase: ExtractAndCreatePhase) => void;
};

export type NothingWrittenCause = "extract-failed" | "junk-only";

export type ExtractAndCreateOutcome =
  | { kind: "listing-failed"; detail: string | null }
  | { kind: "nothing-written"; cause: NothingWrittenCause; entries: number; detail: string | null }
  | { kind: "settle-abandoned"; entries: number }
  | { kind: "cancelled" }
  | { kind: "created"; name: string | null }
  | { kind: "already-created"; name: string | null }
  | { kind: "still-blocked"; reason: string; wrote: boolean }
  | { kind: "create-failed"; detail: string | null };

/** The server's own sentence, or null when it gave none: an error with an empty
 *  message is no message, and rendering `: ` would read as a truncation. */
function errorDetail(err: unknown): string | null {
  return err instanceof Error && err.message.trim() ? err.message : null;
}

/** `: reason` or nothing, so one template speaks with and without the server's
 *  detail and never leaves a dangling colon. */
function detailSuffix(detail: string | null): string {
  return detail ? `: ${detail}` : "";
}

/** "1 entry" / "2 entries", so "0 of 1 entry" cannot read as a bug. */
function entryLabel(count: number): string {
  return `${count} ${count === 1 ? "entry" : "entries"}`;
}

/** Runs a cosmetic callback without letting it derail the action: progress and
 *  the cover prime cannot change what was written, so their failure is dropped
 *  rather than turned into a rejection that would speak over the real verdict. */
function notify(action: () => void): void {
  try {
    action();
  } catch {
    // Dropped on purpose; see above.
  }
}

/** What one create result means to the operator. The unknown-status branch is
 *  deliberate: a status this build does not know did not create anything it can
 *  vouch for, so it is reported as a failure rather than claimed as a success. */
function outcomeForResult(
  result: BundleCreateItemResult,
  wrote: boolean,
): ExtractAndCreateOutcome {
  switch (result.status) {
    case "created":
      return { kind: "created", name: result.name };
    case "skipped":
      if (result.reason === "already_created") {
        return { kind: "already-created", name: result.name };
      }
      return { kind: "create-failed", detail: result.reason };
    case "blocked":
      // The label lookup belongs to `outcomeLine`, not here: the outcome carries
      // the server's raw reason and whether this run wrote anything, so every
      // consumer sees the same facts.
      return { kind: "still-blocked", reason: result.reason ?? "blocked", wrote };
    case "failed":
      return { kind: "create-failed", detail: result.reason };
    default:
      return { kind: "create-failed", detail: result.reason };
  }
}

/**
 * Extracts one archive in place, waits for the scan to register the files, then
 * creates one project from them.
 *
 * Never rejects: every failure mode is an outcome, because the caller is a view
 * that must say something after a one-way write. The order is the contract -
 * no phase is skipped silently and no create happens without proof that the
 * files are visible.
 */
export async function runExtractAndCreate(run: {
  archivePath: string;
  item: ExtractAndCreateTarget;
  deps: ExtractAndCreateDeps;
}): Promise<ExtractAndCreateOutcome> {
  const { archivePath, item, deps } = run;

  notify(() => deps.onPhase?.("listing"));
  let entries: number;
  try {
    const listing = await deps.listEntries(archivePath);
    entries = listing.entries.length;
  } catch (err) {
    // Nothing was extracted, so no count may be claimed.
    return { kind: "listing-failed", detail: errorDetail(err) };
  }

  notify(() => deps.onPhase?.("extracting"));
  let extracted: ArchiveExtractResult;
  try {
    // `entries: null` takes the whole archive: the one-step door has no entry
    // chooser, so the archive's own contents decide what becomes the project.
    extracted = await deps.extract({ path: archivePath, entries: null });
  } catch (err) {
    return {
      kind: "nothing-written",
      cause: "extract-failed",
      entries,
      detail: errorDetail(err),
    };
  }

  if (extracted.files === 0 && !extracted.already_present) {
    return { kind: "nothing-written", cause: "junk-only", entries, detail: null };
  }

  // Skip the wait only when this request wrote nothing, which is measured, not
  // assumed. `already_present` is `_already_present`, a stat of every target:
  // that early return answers `files=0, already_present=True, scan_started=False`
  // (`archive_service.py:144-151`), so no file was written and no scan was
  // started. `settleAfterExtract` returns `"settled"` without any network read
  // when nothing was written (`scanSettle.ts:124-125`), so skipping the wait
  // here is the same answer, not a shortcut around a proof. The create response
  // stays the only readiness authority either way.
  if (extracted.files > 0) {
    notify(() => deps.onPhase?.("scanning"));
    let settleOutcome: SettleOutcome;
    try {
      settleOutcome = await deps.settle([extracted]);
    } catch {
      // The settle loop never throws by contract. A rejection here means the
      // proof never arrived, and without proof the create must not run.
      settleOutcome = "timeout";
    }
    if (settleOutcome === "timeout") return { kind: "settle-abandoned", entries };
    if (settleOutcome === "cancelled") return { kind: "cancelled" };
  }

  notify(() => deps.beforeCreate?.());
  notify(() => deps.onPhase?.("creating"));
  let response: BundleCreateResponse | null;
  try {
    response = await deps.create([createItem(item)]);
  } catch (err) {
    return { kind: "create-failed", detail: errorDetail(err) };
  }

  if (response === null) return { kind: "cancelled" };

  // Exactly one item is sent and the server echoes the request's key
  // (`bundle_service.py:2325`), so a results list that does not name this row
  // answers nothing about it. Falling back to `results[0]` would let another
  // row's verdict speak for this one.
  const result = response.results.find(candidate => candidate.key === item.key) ?? null;
  if (result === null) {
    return { kind: "create-failed", detail: "The server returned no result for this bundle." };
  }
  return outcomeForResult(result, extracted.files > 0);
}

/** The one item the one-step door sends. The server's compound branch builds
 *  the project's groups from the row itself, so this never sends
 *  `exclude_groups`, never `name_only`, and never one item per group. */
export function createItem(item: ExtractAndCreateTarget): BundleCreateItem {
  const name = (item.name ?? "").trim();
  return name ? { key: item.key, name } : { key: item.key };
}

export function phaseLine(phase: ExtractAndCreatePhase): string {
  switch (phase) {
    case "listing":
      return "Reading the archive's file list…";
    case "extracting":
      return "Extracting the archive…";
    case "scanning":
      return "Waiting for the library scan to register the extracted files…";
    case "creating":
      return "Creating the project…";
  }
}

/** The sentence the operator reads after the run, or null when there is nothing
 *  to say. Only `cancelled` is silent: the queue was left, or the session was
 *  revoked, so there is no verdict to give and the shell already speaks it. */
export function outcomeLine(outcome: ExtractAndCreateOutcome): string | null {
  switch (outcome.kind) {
    case "listing-failed": {
      // The detail branch supplies its own colon and the silent branch the full
      // stop, so the sentence never ends with two.
      const detail = outcome.detail ? `: ${outcome.detail}` : ".";
      return `Could not read this archive's file list${detail} Nothing was extracted, so no project was created.`;
    }
    case "nothing-written": {
      if (outcome.cause === "extract-failed") {
        return `Nothing was written: 0 of ${entryLabel(outcome.entries)} reached the library, because the extraction failed${detailSuffix(outcome.detail)}. The server extracts all-or-nothing and leaves nothing half-written, so no project was created.`;
      }
      if (outcome.entries === 0) {
        return "Nothing was written: the archive holds no entries at all, so nothing will register and no project was created.";
      }
      return `Nothing was written: 0 of ${entryLabel(outcome.entries)} reached the library, because the archive held no extractable files. Nothing will register from it, so no project was created.`;
    }
    case "settle-abandoned":
      return "The extracted files are in the library, but the library scan did not settle, so no project was created. Refresh this queue when the scan finishes.";
    case "created": {
      const name = (outcome.name ?? "").trim();
      if (!name) return "Created the project from this archive.";
      return `Created project ${name}.`;
    }
    case "already-created": {
      const name = (outcome.name ?? "").trim();
      if (!name) {
        return "This bundle already has a project (its name is unavailable) - this run created no second project.";
      }
      return `Already created as project ${name} - this run created no second project.`;
    }
    case "still-blocked": {
      // The table's `archive_only` label is an instruction to re-extract, which
      // is exactly what this run just did, so that reason gets its own sentence
      // rather than a contradiction.
      let head: string;
      if (outcome.reason === "archive_only") {
        head =
          "No project was created: the queue still reads this row as zip-only, so the scan has not paired the extracted folder with the archive.";
      } else {
        head = `No project was created: ${BLOCKED_REASON_LABELS[outcome.reason] ?? outcome.reason}.`;
      }
      // One clause, and it is measured: `wrote` is `extracted.files > 0`, false
      // on the `already_present` route where this run wrote nothing at all.
      if (!outcome.wrote) return `${head} This run wrote nothing to the library.`;
      return `${head} The extraction stands - the files are in the library.`;
    }
    case "create-failed":
      // "The row is ready" is a claim only a server verdict can support: every
      // `failed` and every unexplained `skipped` came back past the blocked
      // check. A thrown create never reaches here in the app - the hook answers
      // `null` for it and its own error line speaks (see the module comment).
      return `The project could not be created${detailSuffix(outcome.detail)}. The extraction stands: the files are in the library and the row is ready, so Create project retries it, and nothing was rolled back.`;
    case "cancelled":
      return null;
  }
}
