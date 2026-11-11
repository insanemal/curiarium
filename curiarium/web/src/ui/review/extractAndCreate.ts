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
import type { Project } from "../../lib/api/projects";
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
  /** The server's own re-read row for the project this run created
   *  (`routers/imports.py` serialises it through `project_service.project_with_stats`
   *  after the write). Carried as the wire carries it, `project` and nothing
   *  derived from it: a second representation of the same fact - the `name`
   *  field this variant used to hold - is how one wire fact grows two answers
   *  and the two drift. It is also what lets the view hand App a real id instead
   *  of a client-fabricated Project. */
  | { kind: "created"; project: Project | null }
  /** The project that already existed, again the server's row. This run wrote
   *  nothing, which is why the view reports it and does not land on it. */
  | { kind: "already-created"; project: Project | null }
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
      return { kind: "created", project: result.project };
    case "already_created":
      return { kind: "already-created", project: result.project };
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
      // The name is read from the row the server returned, never from a copy of
      // it kept on the outcome.
      const name = (outcome.project?.name ?? "").trim();
      if (!name) return "Created the project from this archive.";
      return `Created project ${name}.`;
    }
    case "already-created": {
      const name = (outcome.project?.name ?? "").trim();
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

// ---------------------------------------------------------------------------
// Many archives, one click.
//
// The bulk door is the same run repeated in queue order, plus the aggregate
// sentence that has to be spoken before the first byte is written. Two rules
// shape all of it:
//
// - One archive at a time, never in parallel. The extract door writes one tree
//   per archive and the settle waits on a library-wide walk, so overlapping two
//   archives would race two walks and leave "which archive did this file come
//   from" unanswerable in the one place the operator reads it - their own queue.
//   A `for` loop awaiting one run at a time is therefore the contract, not a
//   conservative choice; parallelism is forbidden by rule 1 of the ticket.
// - One classifier. Every listing goes through `planArchive`, the same one the
//   single-archive confirm step uses. `archivePlanLines` is per-archive prose
//   (sizes, roots, junk) and summing it would lie, so the aggregate line is built
//   from the summed `ArchivePlan` numbers instead - and `archivePlanLines` is
//   never called from here.
//
// The listing is the expensive read (it opens the zip on the server) and it is
// needed twice: once to say what the click would write, once by the run itself.
// `planExtractAndCreateMany` keeps the entries it read on the listed member, and
// `runExtractAndCreateMany` hands those back through the memoised `listEntries`
// rather than reading again. The client cannot see an archive change under it
// between confirm and run either way - the server re-reads the listing inside the
// door, which is the only readiness authority this flow has.

/** One row of the confirmed queue: the archive path the door is keyed on, the
 *  label the queue already shows, and the operator's typed name (null when they
 *  typed nothing, so the server's twin-derived default names the project).
 *
 *  The label is carried beside the path because a verdict has to name the archive
 *  the operator ticked, and a bucket-relative path is a poor name in a sentence
 *  about many of them. */
export type BulkArchiveTarget = { path: string; label: string; name: string | null };

/** One archive after the plan pass: either its listing and its plan, or the
 *  reason neither exists. The union is what stops a failed read from being summed
 *  as a zero - there is no `plan` to add, so there is nothing to count. */
export type BulkListedArchive = BulkArchiveTarget &
  (
    | { entries: ArchiveEntry[]; plan: ArchivePlan; detail: null }
    | { entries: null; plan: null; detail: string | null }
  );

/** The readable archives' plans, added member by member. Bytes are summed for the
 *  same reason `ArchivePlan.bytes` exists: it is what the click spends, not what
 *  the library will hold. */
export type BulkPlanTotals = {
  files: number;
  bytes: number;
  models: number;
  images: number;
  documents: number;
  nestedArchives: number;
  junk: number;
  unregistered: number;
};

/** The confirmed click: every target, in queue order, with the aggregate of the
 *  ones that could be read. */
export type BulkPlan = { archives: BulkListedArchive[]; totals: BulkPlanTotals };

/** What one archive's turn in the queue came to. Reuses the single-archive
 *  outcome whole, so no sentence here can disagree with the one that door already
 *  speaks. */
export type BulkArchiveVerdict = BulkArchiveTarget & { outcome: ExtractAndCreateOutcome };

export type BulkRunResult = {
  /** The confirmed targets, in order - the plan's rows without their listings, so
   *  the caller can describe the click without carrying the plan around. */
  targets: BulkArchiveTarget[];
  /** One per archive visited, in order. The tail after a `cancelled` has none:
   *  it was never touched, which is exactly what `stopped` reports. */
  verdicts: BulkArchiveVerdict[];
  created: number;
  alreadyCreated: number;
  /** The targets whose extraction measurably wrote files, which is not the same
   *  as the targets that were visited: an archive can be read, extracted and still
   *  create nothing. */
  written: BulkArchiveTarget[];
  /** The created project, ONLY when the click named exactly one archive and it
   *  created one. Landing on an arbitrary member of a many-archive click would
   *  hide the others behind one row. */
  landing: Project | null;
  /** The queue was gone mid-run: the loop broke on `cancelled` and never visited
   *  the tail. */
  stopped: boolean;
};

/** `1 model` / `3 models`, or nothing at all when the count is zero - the same
 *  shape `archivePlan`'s own `part` uses, restated here rather than imported so
 *  the only imports from that module stay `planArchive` and `ArchivePlan`. */
function counted(count: number, one: string, many: string): string | null {
  if (count === 0) return null;
  return `${count} ${count === 1 ? one : many}`;
}

/** The readable archives' plans, added up. An unreadable archive contributes
 *  nothing at all - not a zero, which would be a claim that it holds nothing. */
function sumPlans(archives: readonly BulkListedArchive[]): BulkPlanTotals {
  const totals: BulkPlanTotals = {
    files: 0,
    bytes: 0,
    models: 0,
    images: 0,
    documents: 0,
    nestedArchives: 0,
    junk: 0,
    unregistered: 0,
  };
  for (const archive of archives) {
    const plan = archive.plan;
    if (plan === null) continue;
    totals.files += plan.files;
    totals.bytes += plan.bytes;
    totals.models += plan.models;
    totals.images += plan.images;
    totals.documents += plan.documents;
    totals.nestedArchives += plan.nestedArchives;
    totals.junk += plan.junk;
    totals.unregistered += plan.unregistered;
  }
  return totals;
}

/**
 * Reads every archive's listing, one at a time, and says what the whole click
 * would write.
 *
 * The catch is per archive on purpose: one unreadable zip must not cost the
 * operator the other two, and the queue has to be able to name the one that
 * failed rather than say "the plan failed". Nothing is written here - this is the
 * confirm step's read, so a plan that is never confirmed costs one listing per
 * archive and no bytes.
 *
 * Never rejects: a listing failure is a value, not an exception, and the cosmetic
 * progress line is dropped if it throws.
 */
export async function planExtractAndCreateMany(run: {
  targets: readonly BulkArchiveTarget[];
  listEntries: (path: string) => Promise<{ path: string; entries: ArchiveEntry[] }>;
  onProgress?: (line: string) => void;
}): Promise<BulkPlan> {
  const archives: BulkListedArchive[] = [];
  const total = run.targets.length;

  for (let index = 0; index < run.targets.length; index += 1) {
    const target = run.targets[index];
    let listed: BulkListedArchive;
    try {
      const listing = await run.listEntries(target.path);
      // The entries are kept, not just counted: the run hands them back through
      // its `listEntries`, so the confirm and the write read the same zip once.
      listed = { ...target, entries: listing.entries, plan: planArchive(listing.entries), detail: null };
    } catch (err) {
      listed = { ...target, entries: null, plan: null, detail: errorDetail(err) };
    }
    archives.push(listed);
    // 1-based and counting archives *attempted*, so a failed read still advances
    // the line: the operator is being told where the pass has got to, not how
    // much of it succeeded.
    notify(() => run.onProgress?.(`Reading ${index + 1} of ${total} archive file lists…`));
  }

  return { archives, totals: sumPlans(archives) };
}

/**
 * The confirm step's aggregate sentence, plus one line per archive that needs
 * saying. Never a claim the listings do not support: an archive that could not be
 * read is named and excluded, not counted as empty.
 */
export function bulkPlanLines(plan: BulkPlan): string[] {
  const total = plan.archives.length;
  if (total === 0) return [];

  const lines: string[] = [];
  const readable = plan.archives.filter(archive => archive.plan !== null).length;
  if (readable === 0) {
    // Nothing readable means nothing to promise, and no partial sum may stand in
    // for the payload: the click would write nothing at all.
    lines.push(`None of the ${total} archives could be read, so this click would write nothing.`);
  } else {
    const parts = [
      counted(plan.totals.models, "model", "models"),
      counted(plan.totals.images, "image", "images"),
      counted(plan.totals.nestedArchives, "nested archive", "nested archives"),
      counted(plan.totals.documents, "document", "documents"),
      counted(plan.totals.files, "file", "files"),
    ].filter((part): part is string => part !== null);
    if (parts.length === 0) {
      // Every readable archive holds only junk or directory members, so there
      // is no count to promise: the same truth `bulkArchiveLine` tells per
      // archive, told once for the click. Without this the header would render
      // its colon and then nothing. The subject counts readable archives, not
      // targets: in a mixed plan an unreadable archive is unread, not empty,
      // and it gets its own failure line below - counting it here would claim
      // it holds nothing extractable, which the listing never said.
      const subject = readable === 1 ? "this archive" : `these ${readable} archives`;
      lines.push(`Nothing in ${subject} can be extracted, so this click would write nothing.`);
    } else {
      lines.push(`Extracting ${total} ${total === 1 ? "archive" : "archives"}: ${parts.join(", ")} in all.`);
    }
  }

  // Archive order, so the lines read in the same order as the queue above them.
  for (const archive of plan.archives) {
    const line = bulkArchiveLine(archive);
    if (line !== null) lines.push(line);
  }
  return lines;
}

/** What one archive needs said before it is written, or null when the header
 *  already told the truth about it.
 *
 *  `files === 0` comes first because it is the loudest: the click would hand the
 *  library nothing and make no project from this row. The nested-archive warning
 *  is the same one `archivePlanLines` gives in the single-archive confirm step,
 *  in the same shape - it is the moment the operator can still choose differently,
 *  and it must not be said differently here than there. */
function bulkArchiveLine(archive: BulkListedArchive): string | null {
  if (archive.plan === null) {
    // The detail is the server's own sentence; the parentheses keep it from
    // running into the one this module writes around it.
    const detail = archive.detail ? ` (${archive.detail})` : "";
    return `Could not read the file list of "${archive.label}"${detail} - it will be skipped and nothing in it will be written.`;
  }
  if (archive.plan.files === 0) {
    return `"${archive.label}" holds nothing extractable, so extracting it writes nothing and no project will be made from it.`;
  }
  if (archive.plan.modelsOnlyInsideNestedArchive) {
    return nestedOnlyLine(archive.label, archive.plan.nestedArchivePaths);
  }
  return null;
}

/** `"a.zip"` / `"a.zip" and "b.zip"` / `"a.zip" and "b.zip" and 3 more`, named by
 *  basename the way the single-archive warning names them: the operator recognises
 *  the file, not its bucket-relative path. */
function nestedOnlyLine(label: string, paths: readonly string[]): string {
  const named = paths
    .slice(0, 2)
    .map(path => `"${path.split("/").pop()}"`)
    .join(" and ");
  const more = paths.length > 2 ? ` and ${paths.length - 2} more` : "";
  const subject = paths.length === 1 ? "the only archive inside is" : "the only archives inside are";
  return `"${label}" packs no model of its own: ${subject} ${named}${more}, which extraction releases whole.`;
}

/**
 * Runs the confirmed queue, one archive at a time, and reports what each one came
 * to.
 *
 * Everything the single-archive run decided still decides: the same
 * `runExtractAndCreate` is awaited once per archive, so the phases, the settle
 * skip, the all-or-nothing extract and the door's own verdict are unchanged. This
 * function only supplies the per-archive wiring and carries the failure forward
 * instead of out - a failed archive is a verdict, and the next archive still runs.
 *
 * It breaks for exactly one outcome: `cancelled`. That verdict means the queue is
 * gone (the session was left or the response never arrived), so continuing would
 * POST projects whose verdicts nobody is left to read.
 *
 * Never rejects: every dependency failure is already an outcome, and the cosmetic
 * callbacks are wrapped in `notify` so a paint cannot derail a write.
 */
export async function runExtractAndCreateMany(run: {
  plan: BulkPlan;
  /** The single-archive dependencies, minus the listing: the listing was read by
   *  the plan pass and is handed back below, so the run makes zero listing calls. */
  deps: Pick<ExtractAndCreateDeps, "extract" | "settle" | "create">;
  /** Fire-and-forget cover prime, per archive, exactly as the create doors do. */
  beforeCreate?: (archive: BulkArchiveTarget) => void;
  onProgress?: (line: string) => void;
  onVerdict?: (verdict: BulkArchiveVerdict) => void;
}): Promise<BulkRunResult> {
  const archives = run.plan.archives;
  const total = archives.length;
  const targets: BulkArchiveTarget[] = archives.map(archive => ({
    path: archive.path,
    label: archive.label,
    name: archive.name,
  }));

  const verdicts: BulkArchiveVerdict[] = [];
  const written: BulkArchiveTarget[] = [];
  let created = 0;
  let alreadyCreated = 0;
  let landing: Project | null = null;
  let stopped = false;

  for (let index = 0; index < archives.length; index += 1) {
    const archive = archives[index];
    const target: BulkArchiveTarget = { path: archive.path, label: archive.label, name: archive.name };
    let outcome: ExtractAndCreateOutcome;

    if (archive.plan === null) {
      // The listing already failed in the plan pass, so there is nothing to
      // extract and nothing to ask the door: one listing call, zero writes.
      outcome = { kind: "listing-failed", detail: archive.detail };
    } else {
      const entries = archive.entries;
      // Per-archive, because "did this run write anything here" is a fact about
      // this archive: the extract reports `files: 0` for an archive that was
      // already fully present, which is a visit, not a write.
      let wrote = false;
      outcome = await runExtractAndCreate({
        archivePath: archive.path,
        item: { name: archive.name },
        deps: {
          extract: async payload => {
            const result = await run.deps.extract(payload);
            if (result.files > 0) wrote = true;
            return result;
          },
          settle: run.deps.settle,
          create: run.deps.create,
          // The memoised listing: the file list the operator confirmed is the file
          // list this run plans from, so the run makes no listing call of its own.
          listEntries: async path => ({ path, entries }),
          beforeCreate: () => notify(() => run.beforeCreate?.(target)),
          onPhase: phase => notify(() => run.onProgress?.(bulkPhaseLine(index + 1, total, phase))),
        },
      });
      if (wrote) written.push(target);
    }

    const verdict: BulkArchiveVerdict = { ...target, outcome };
    verdicts.push(verdict);
    if (outcome.kind === "created") {
      created += 1;
      // One archive, one project: that is the only shape where "the project this
      // click made" has a single answer. Otherwise the caller stays in the queue.
      if (total === 1) landing = outcome.project;
    } else if (outcome.kind === "already-created") {
      alreadyCreated += 1;
    }
    notify(() => run.onVerdict?.(verdict));

    if (outcome.kind === "cancelled") {
      // The queue is gone. The untouched tail gets no verdict, because nothing was
      // visited there and a verdict is a claim about what happened.
      stopped = true;
      break;
    }
  }

  return { targets, verdicts, created, alreadyCreated, written, landing, stopped };
}

/** `"<label> - <outcomeLine>"` per verdict, in queue order. `outcomeLine` is used
 *  verbatim - the bulk door must not re-word a verdict the single door already
 *  speaks - and a `cancelled` verdict contributes no line, exactly as it does
 *  there. */
export function bulkVerdictLines(verdicts: readonly BulkArchiveVerdict[]): string[] {
  const lines: string[] = [];
  for (const verdict of verdicts) {
    const line = outcomeLine(verdict.outcome);
    if (line === null) continue;
    lines.push(`${verdict.label} - ${line}`);
  }
  return lines;
}

/** The one-line total, or null when nothing was created: a bulk click that made
 *  no project says so per archive, and a summary claiming "Created 0 projects"
 *  would read as a failure of the run rather than as its verdicts. */
export function bulkSummaryLine(result: Pick<BulkRunResult, "created">): string | null {
  if (result.created === 0) return null;
  return result.created === 1 ? "Created 1 project." : `Created ${result.created} projects.`;
}

/** How much of the queue a stopped run never reached, or null when it ran to the
 *  end. The count is measured against the confirmed targets, not the live
 *  selection: the operator is owed the size of the click they made. */
export function bulkStopLine(
  result: Pick<BulkRunResult, "stopped" | "targets" | "verdicts">,
): string | null {
  if (!result.stopped) return null;
  const untouched = result.targets.length - result.verdicts.length;
  return `Stopped with ${result.verdicts.length} of ${result.targets.length} archives done - ${untouched} ${untouched === 1 ? "archive was" : "archives were"} never touched.`;
}

/** One archive's phase, said over the existing phase line so the bulk narration
 *  and the single-archive narration cannot drift apart. */
export function bulkPhaseLine(index: number, total: number, phase: ExtractAndCreatePhase): string {
  return `Archive ${index} of ${total} - ${phaseLine(phase)}`;
}
