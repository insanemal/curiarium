// One review action - list, extract, settle, create - kept free of React so its
// order is unit-testable without a DOM.
//
// The operator's click is the whole justification for it: they ticked this
// archive and pressed *Extract and create*, which already states that these
// entries are one thing. So the fourth phase asks nobody's permission. It calls
// `POST /import/archive/project`, which compares the entry list the server itself
// published against the assets that extraction registered and creates the project
// from those ids in one transaction - no bundle key is resolved and no detection
// rule is consulted (Mf-0ck5; the flow this replaces captured a bundle key, asked
// detection to honour it after the extraction had changed the identity, and was
// answered "unknown bundle").
//
// Three measured server facts still shape the flow:
//
// - The extract is all-or-nothing for the entries it was given.
//   `POST /import/archive` either writes every one of them or writes none, so
//   there is no half-written tree for the client to clean up, which is why
//   `ExtractAndCreateDeps` has no rollback member at all.
// - The extract's scan is asynchronous. `archive_service` ends with
//   `scan_started = bool(seams.start_scan())`, and `library_scan.start_scan()`
//   only starts a daemon thread and returns at once, so the extracted files are
//   invisible to the asset table until the walk registers them. That is why the
//   create waits on `settle`.
// - The settle is a wait, not an authority. When it times out this run still
//   calls the create: the door's own comparison of planned against registered
//   paths is the readiness authority, and it answers "15 of 21" or "none of them,
//   look in import/Moai" - which is more true, and more actionable, than the
//   "the scan did not settle, try later" this flow used to say. A `cancelled`
//   settle still creates nothing: that is the operator walking away, and it stays
//   the one exit that writes nothing.
//
// `deps.create` returning `null` means the hook has no response to report - the
// queue was left, or the request failed and the caller's own error line already
// speaks it - so this module adds no sentence of its own for it. That is why
// `null` maps to `cancelled` rather than to `create-failed`.

import type {
  ArchiveEntry,
  ArchiveExtractResult,
  ArchiveProjectResult,
} from "../../lib/api/imports";
import type { SettleOutcome } from "../../lib/scanSettle";
import { planArchive, type ArchivePlan } from "../archive/archivePlan";

export type ExtractAndCreatePhase = "listing" | "extracting" | "scanning" | "creating";

/** The one row the action runs on. `name` is the operator's typed name, already
 *  trimmed; null/absent means the server names the project after the twin. */
export type ExtractAndCreateTarget = { name?: string | null };

export type ExtractAndCreateDeps = {
  listEntries: (path: string) => Promise<{ path: string; entries: ArchiveEntry[] }>;
  extract: (payload: { path: string; entries?: string[] | null }) => Promise<ArchiveExtractResult>;
  settle: (results: ArchiveExtractResult[]) => Promise<SettleOutcome>;
  /** The extract-and-create door. null means "no response arrived": the queue was
   *  left, or the request failed and the caller's own error line speaks it. */
  create: (payload: { path: string; name?: string }) => Promise<ArchiveProjectResult | null>;
  /** Fire-and-forget cover prime, exactly as the existing create doors do. */
  beforeCreate?: () => void;
  onPhase?: (phase: ExtractAndCreatePhase) => void;
  /** The listing read back as a plan, so the queue can say what the archive holds
   *  while the run is in flight - the same narration the confirm step gives. */
  onPlan?: (plan: ArchivePlan) => void;
};

type NothingWrittenCause = "extract-failed" | "junk-only";

export type ExtractAndCreateOutcome =
  | { kind: "listing-failed"; detail: string | null }
  | { kind: "nothing-written"; cause: NothingWrittenCause; entries: number; detail: string | null }
  | { kind: "cancelled" }
  | { kind: "created"; name: string | null }
  | { kind: "already-created"; name: string | null }
  /** The listing promised N entries and fewer than that are registered assets. */
  | { kind: "partial"; planned: number; resolved: number; failed: string[] }
  /** None of the planned entries became a registered asset; `twin` is where they
   *  would have been, and the only place the operator can go and look. */
  | { kind: "nothing-resolved"; planned: number; twin: string }
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

/** `"import/Moai"` → `"import/Moai/"`, so a directory reads as a directory. */
function directoryLabel(path: string): string {
  const trimmed = (path || "").replace(/\/+$/, "");
  return trimmed ? `${trimmed}/` : "/";
}

/** Runs a cosmetic callback without letting it derail the action: progress, the
 *  plan narration and the cover prime cannot change what was written, so their
 *  failure is dropped rather than turned into a rejection that would speak over
 *  the real verdict. */
function notify(action: () => void): void {
  try {
    action();
  } catch {
    // Dropped on purpose; see above.
  }
}

/** What one door verdict means to the operator. The unknown-status branch is
 *  deliberate: a status this build does not know did not create anything it can
 *  vouch for, so it is reported as a failure rather than claimed as a success. */
function outcomeForResult(result: ArchiveProjectResult): ExtractAndCreateOutcome {
  switch (result.status) {
    case "created":
      return { kind: "created", name: result.project?.name ?? null };
    case "already_created":
      return { kind: "already-created", name: result.project?.name ?? null };
    case "partial":
      return { kind: "partial", planned: result.planned, resolved: result.resolved, failed: result.failed };
    case "nothing_resolved":
      return { kind: "nothing-resolved", planned: result.planned, twin: result.twin };
    default:
      return { kind: "create-failed", detail: null };
  }
}

/**
 * Extracts one archive in place, waits for the library scan to register the
 * files, then creates one project from them.
 *
 * Never rejects: every failure mode is an outcome, because the caller is a view
 * that must say something after a one-way write. The order is the contract - no
 * phase is skipped silently, and the create is the only thing that decides
 * whether a project exists.
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
    // The listing is read before anything is written, so the same knowledge the
    // confirm step uses can be said aloud while the run is in flight.
    notify(() => deps.onPlan?.(planArchive(listing.entries)));
  } catch (err) {
    // Nothing was extracted, so no count may be claimed.
    return { kind: "listing-failed", detail: errorDetail(err) };
  }

  notify(() => deps.onPhase?.("extracting"));
  let extracted: ArchiveExtractResult;
  try {
    // `entries: null` takes the whole archive: the one-step door has no entry
    // chooser, so the archive's own contents decide what becomes the project -
    // and the create door reads the very same listing off the archive itself.
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
  // (`archive_service._already_present`), so no file was written and no scan was
  // started. `settleAfterExtract` returns `"settled"` without any network read
  // when nothing was written (`scanSettle.ts:124-125`), so skipping the wait
  // here is the same answer, not a shortcut around a proof.
  if (extracted.files > 0) {
    notify(() => deps.onPhase?.("scanning"));
    let settleOutcome: SettleOutcome;
    try {
      settleOutcome = await deps.settle([extracted]);
    } catch {
      // The settle loop never throws by contract. A rejection here means the wait
      // itself broke, which is a timeout in every sense that matters.
      settleOutcome = "timeout";
    }
    // A timeout does not abandon the create any more. The door compares the
    // planned paths against registered assets and refuses a partial or an empty
    // result itself, so waiting out the walk is a courtesy to the operator, not
    // the proof that makes the create safe. `cancelled` is different in kind: the
    // queue is gone, so there is nothing left to report a verdict to.
    if (settleOutcome === "cancelled") return { kind: "cancelled" };
  }

  notify(() => deps.beforeCreate?.());
  notify(() => deps.onPhase?.("creating"));
  let response: ArchiveProjectResult | null;
  try {
    response = await deps.create(createPayload(archivePath, item));
  } catch (err) {
    return { kind: "create-failed", detail: errorDetail(err) };
  }

  if (response === null) return { kind: "cancelled" };
  return outcomeForResult(response);
}

/** The one payload the one-step door sends. The archive path is the whole
 *  identity - idempotency is keyed on it, never on a bundle key that may not
 *  exist - and `name` is omitted entirely when the operator typed nothing, so
 *  the server's twin-derived default is what names the project. */
export function createPayload(
  archivePath: string,
  item: ExtractAndCreateTarget,
): { path: string; name?: string } {
  const name = (item.name ?? "").trim();
  return name ? { path: archivePath, name } : { path: archivePath };
}

/** The listing as a plan. `planArchive` is total over any listing, and `notify`
 *  keeps a throw in a cosmetic callback from changing what was written. */
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
    case "created": {
      const name = (outcome.name ?? "").trim();
      if (!name) return "Created the project from this archive.";
      return `Created project ${name}.`;
    }
    case "already-created": {
      const name = (outcome.name ?? "").trim();
      if (!name) {
        return "This archive already has a project (its name is unavailable) - this run created no second project.";
      }
      return `Already created as project ${name} - this run created no second project.`;
    }
    case "partial": {
      // Every number here is the server's: `planned` is the size of the listing it
      // published, `resolved` the set it found, and `failed` names the difference.
      // Nothing was written, so the retry after the operator acts needs no undo.
      const missing = outcome.planned - outcome.resolved;
      return `No project was created: the archive's file list promised ${entryLabel(outcome.planned)} and ${entryLabel(outcome.resolved)} are registered in the library, so ${entryLabel(missing)} never arrived${namedFailures(outcome.failed)}. Nothing was written and nothing was rolled back - extract the rest, or let the library scan finish, and this runs again.`;
    }
    case "nothing-resolved": {
      if (outcome.planned === 0) {
        return "No project was created: this archive holds no files the library can register, so there is nothing to make a project from.";
      }
      // Nothing is in flight to wait for. The extract door ends with
      // `seams.start_scan()`, but `create_project_from_extract` deliberately starts
      // no walk - it writes nothing at all, and a whole-library scan as a side
      // effect of a door whose verdict says "nothing was created" would be an
      // unreported write-adjacent action. So the advice is to START a scan, not to
      // wait for one. The control is named because it is reachable from here: the
      // `LibraryScanPanel` "Rescan storage folder" button is mounted by
      // `MissingFilesReviewView`, which is the Missing files queue of this same
      // Review destination - a tab beside the one this line is spoken in, and a
      // queue every role that can press the door can see. (`POST /library/scan` is
      // WRITE, so the button itself is editor-and-above, like every other way to
      // start a scan; a contributor is pointed at the same place to ask for one.)
      return `No project was created: none of the ${entryLabel(outcome.planned)} the archive lists is a registered file in the library. They would be under "${directoryLabel(outcome.twin)}" - check where they are, start a library scan (this run started none; Review's Missing files queue has the "Rescan storage folder" control), then this runs again.`;
    }
    case "create-failed":
      // What is claimed here is only what this run measured: the extraction
      // wrote files (or refused) and the server said this. Whether the row is
      // creatable is not this run's business - no bundle rule was consulted, so
      // none can be the reason a create was refused.
      return `The project could not be created${detailSuffix(outcome.detail)}. The extraction stands: the files are in the library, and nothing was rolled back.`;
    case "cancelled":
      return null;
  }
}

/** ` ("a.stl", "b.stl")` for the first few, or `""`: the operator can act on the
 *  names, and a long list would bury the sentence it is inside. */
function namedFailures(failed: string[]): string {
  if (failed.length === 0) return "";
  const shown = failed.slice(0, 3).map(name => `"${name}"`).join(", ");
  const more = failed.length > 3 ? ` and ${failed.length - 3} more` : "";
  return ` (${shown}${more})`;
}
