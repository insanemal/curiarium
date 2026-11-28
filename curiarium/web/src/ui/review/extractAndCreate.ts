// The many-archive extract step, kept free of React so its order is unit-testable
// without a DOM.
//
// Every archive becomes ONE server-computed operation: `plan` says what the
// extract-and-create workflow would do, `submit` queues that plan, and `read`
// polls the row the runner writes until it reaches a terminal status. The client
// therefore plans, approves and narrates - it never extracts, moves or creates
// anything itself, and the `POST /import/archive/project` route this flow used to
// call no longer exists.
//
// Two measured server facts still shape the flow:
//
// - One archive at a time, never in parallel. Each run files one archive and
//   writes one tree, and the server's queue serialises the moves anyway; a
//   `for` loop awaiting one run at a time is therefore the contract, not a
//   conservative choice.
// - The listing read by the confirm step is PROSE ONLY. It says what the click
//   would write before anything is queued (`planExtractAndCreateMany`); the
//   operation plans from the archive itself, so the run makes no listing call of
//   its own.
//
// `UnauthorizedError` is the one rejection: a session that has gone ends on a
// fresh sign-in rather than a sentence on an expired one, exactly as the shared
// hook treats it. Every other failure is an outcome, because the caller is a view
// that must say something after a one-way write.

import type { ArchiveEntry } from "../../lib/api/imports";
import { UnauthorizedError } from "../../lib/api/http";
import type { Operation, OperationPlan } from "../../lib/api/operations";
import { planArchive, type ArchivePlan } from "../archive/archivePlan";

/** The three statuses a runner never leaves: the poll stops on one of them. Kept
 *  local rather than imported from the hook so this module stays React-free. */
const TERMINAL_STATUSES = ["done", "failed", "cancelled"];

/** Fast enough to feel live, slow enough that a long run is a handful of reads -
 *  the same interval the shared hook polls at. */
const POLL_MS = 250;

function isTerminal(status: string): boolean {
  return TERMINAL_STATUSES.includes(status);
}

/** The thrown message, or null when the throw carried none: an error with an
 *  empty message is no message, and rendering `: ` would read as a truncation. */
function errorDetail(err: unknown): string | null {
  return err instanceof Error && err.message.trim() ? err.message : null;
}

/** `: reason` or nothing, so one template speaks with and without the server's
 *  detail and never leaves a dangling colon. */
function detailSuffix(detail: string | null): string {
  return detail ? `: ${detail}` : "";
}

/** Runs a cosmetic callback without letting it derail the action: a progress
 *  paint cannot change what was queued, so its failure is dropped rather than
 *  turned into a rejection that would speak over the real verdict. */
function notify(action: () => void): void {
  try {
    action();
  } catch {
    // Dropped on purpose; see above.
  }
}

/** The two stages this flow narrates. The runner's own pipeline (extract, move,
 *  scan, create-project) is narrated by its step rows, not by the client. */
export type ExtractAndCreatePhase = "planning" | "running";

/** What the client calls to drive one archive. Injected so this module never
 *  reaches the network itself and a test can answer without a server. */
export type ExtractRunDeps = {
  plan: (input: {
    workflow: string;
    target: string;
    params?: Record<string, unknown>;
  }) => Promise<OperationPlan>;
  submit: (input: { planId: string; idempotencyKey: string }) => Promise<Operation>;
  read: (operationId: string) => Promise<Operation>;
  /** The wait between polls, injected so a test does not sit on a wall clock. */
  pause?: (milliseconds: number) => Promise<void>;
  /** One idempotency key per archive intent, injected for a deterministic test. */
  newKey?: () => string;
};

/** What one archive's turn in the queue came to. `listing-failed` is decided
 *  before any request: the confirm step could not read the zip, so nothing was
 *  planned for it. */
export type ExtractRunOutcome =
  | { kind: "listing-failed"; detail: string | null }
  /** The project the server's create step reported. `name`/`project_id` are the
   *  step result's own fields, which is why nothing here is derived from a row
   *  this run did not read. */
  | { kind: "created"; project_id: string | null; name: string | null }
  /** The project that already existed when the runner reached the create step. */
  | { kind: "already-created"; project_id: string | null; name: string | null }
  | { kind: "failed"; detail: string | null }
  | { kind: "cancelled" };

/** The default wait between polls. A no-op under fake timers would spin, so the
 *  injected `pause` is what a test uses; this is the production one. */
function wait(milliseconds: number): Promise<void> {
  return new Promise(resolve => {
    setTimeout(resolve, milliseconds);
  });
}

/** The sentence the operator reads after one archive's turn, or null when there
 *  is nothing to say. Only `cancelled` is silent: the server's runner stopped, so
 *  there is no verdict to give and the shell already speaks it. */
export function outcomeLine(outcome: ExtractRunOutcome): string | null {
  switch (outcome.kind) {
    case "listing-failed": {
      // The detail branch supplies its own colon and the silent branch the full
      // stop, so the sentence never ends with two.
      const detail = outcome.detail ? `: ${outcome.detail}` : ".";
      return `Could not read this archive's file list${detail} Nothing was extracted, so no project was created.`;
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
    case "failed":
      // What is claimed here is only what the run measured: the operation was
      // refused or ended without a create step that said it created anything.
      return `The project could not be created${detailSuffix(outcome.detail)}. Nothing was rolled back.`;
    case "cancelled":
      return null;
  }
}

/** One plan step's own report, read as untrusted JSON. `undefined` for a key the
 *  step did not send, so a missing name is never the string "undefined". */
function resultString(result: Record<string, unknown> | null, key: string): string | null {
  const value = result?.[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

/** What a finished operation says about the project it made. The create step is
 *  the authority: `created` is a project this run made, `already_created` is the
 *  race the server lost, and anything else created nothing this run can vouch
 *  for. */
function outcomeForOperation(operation: Operation): ExtractRunOutcome {
  if (operation.status === "cancelled") return { kind: "cancelled" };
  if (operation.status !== "done") return { kind: "failed", detail: operation.error };

  const create = operation.steps.find(step => step.op === "create-project");
  const status = resultString(create?.result ?? null, "status");
  const project_id = resultString(create?.result ?? null, "project_id");
  const name = resultString(create?.result ?? null, "name");
  if (status === "already_created") return { kind: "already-created", project_id, name };
  if (status === "created") return { kind: "created", project_id, name };
  // A done operation whose create step reported neither status made no project
  // this run can claim, so it is reported as the failure it is - with the server's
  // own sentence when it gave one.
  return { kind: "failed", detail: operation.error };
}

/** Reads the row until the runner leaves a terminal status. */
async function readUntilTerminal(
  operationId: string,
  deps: ExtractRunDeps,
  submitted: Operation,
): Promise<Operation> {
  let current = submitted;
  const pause = deps.pause ?? wait;
  while (!isTerminal(current.status)) {
    await pause(POLL_MS);
    current = await deps.read(operationId);
  }
  return current;
}

/**
 * Plans, submits and follows ONE archive's `extract-and-create` operation.
 *
 * Never rejects except with `UnauthorizedError`, which the caller must hand to
 * the shell. A plan with zero creates means the project already exists: the
 * server says so itself, so nothing is submitted and the existing project's name
 * is reported rather than a create the client would have to invent.
 */
export async function runExtractAndCreate(run: {
  archivePath: string;
  /** The operator's typed name, already trimmed; null means the server names the
   *  project after the archive. */
  name: string | null;
  deps: ExtractRunDeps;
  onPhase?: (phase: ExtractAndCreatePhase) => void;
}): Promise<ExtractRunOutcome> {
  const { archivePath, name, deps } = run;

  notify(() => run.onPhase?.("planning"));
  let plan: OperationPlan;
  try {
    // `entries` is deliberately absent: the browser has no entry picker for this
    // workflow, so the whole archive is the subject.
    plan = await deps.plan({
      workflow: "extract-and-create",
      target: archivePath,
      params: name ? { name } : {},
    });
  } catch (err) {
    if (err instanceof UnauthorizedError) throw err;
    return { kind: "failed", detail: errorDetail(err) };
  }

  // Zero creates is the server's own answer that a project already exists. It is
  // a finished plan, not a failure, and submitting it would queue a no-op.
  if (plan.review.creates === 0) {
    return {
      kind: "already-created",
      project_id: plan.review.existing_project_id,
      name: plan.review.existing_project_name,
    };
  }

  notify(() => run.onPhase?.("running"));
  let submitted: Operation;
  try {
    const newKey = deps.newKey ?? (() => crypto.randomUUID());
    submitted = await deps.submit({ planId: plan.plan_id, idempotencyKey: newKey() });
  } catch (err) {
    if (err instanceof UnauthorizedError) throw err;
    return { kind: "failed", detail: errorDetail(err) };
  }

  try {
    const finished = isTerminal(submitted.status)
      ? submitted
      : await readUntilTerminal(submitted.id, deps, submitted);
    return outcomeForOperation(finished);
  } catch (err) {
    if (err instanceof UnauthorizedError) throw err;
    // The row is the server's record, so its sentence is the one to show. The
    // project may still have landed: this only says the follow-up read failed.
    return { kind: "failed", detail: errorDetail(err) };
  }
}

/** The sentence for a phase, so the bulk narration has one line per stage. */
export function phaseLine(phase: ExtractAndCreatePhase): string {
  switch (phase) {
    case "planning":
      return "Planning the extraction…";
    case "running":
      return "Running the extraction…";
  }
}

// ---------------------------------------------------------------------------
// Many archives, one click.
//
// The bulk door plans every archive's listing first, so the operator can read
// what the click would write before anything is queued. Two rules shape it:
//
// - One archive at a time, never in parallel (see the header).
// - One classifier. Every listing goes through `planArchive`, the same one the
//   single-archive confirm step uses. `archivePlanLines` is per-archive prose
//   (sizes, roots, junk) and summing it would lie, so the aggregate line is built
//   from the summed `ArchivePlan` numbers instead.

/** One row of the confirmed queue: the archive path the operation is keyed on,
 *  the label the queue already shows, and the operator's typed name (null when
 *  they typed nothing, so the server's twin-derived default names the project).
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

/** What one archive's turn in the queue came to, named by the row it belongs to. */
export type BulkArchiveVerdict = BulkArchiveTarget & { outcome: ExtractRunOutcome };

export type BulkRunResult = {
  /** The confirmed targets, in order - the plan's rows without their listings, so
   *  the caller can describe the click without carrying the plan around. */
  targets: BulkArchiveTarget[];
  /** One per archive visited, in order. The tail after a stop has none: it was
   *  never touched, which is exactly what `stopped` reports. */
  verdicts: BulkArchiveVerdict[];
  created: number;
  alreadyCreated: number;
  /** The created project's id, ONLY when the click named exactly one archive and
   *  it created one. Landing on an arbitrary member of a many-archive click would
   *  hide the others behind one row. No row is carried: the operations route
   *  reports the step's project id, not a whole project. */
  landing: string | null;
  /** The run stopped before the end of the queue - the session went, so the tail
   *  was never visited. */
  stopped: boolean;
  /** The session expired mid-run: the host's sign-out, never a sentence here. */
  unauthorized: boolean;
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
      listed = {
        ...target,
        entries: listing.entries,
        plan: planArchive(listing.entries),
        detail: null,
      };
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
      // Every readable archive holds only junk or directory members, so there is
      // no count to promise: the same truth `bulkArchiveLine` tells per archive,
      // told once for the click. The subject counts readable archives, not
      // targets: in a mixed plan an unreadable archive is unread, not empty, and
      // it gets its own failure line below.
      const subject = readable === 1 ? "this archive" : `these ${readable} archives`;
      lines.push(`Nothing in ${subject} can be extracted, so this click would write nothing.`);
    } else {
      lines.push(
        `Extracting ${total} ${total === 1 ? "archive" : "archives"}: ${parts.join(", ")} in all.`,
      );
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
 *  already told the truth about it. */
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
 * Each archive is planned, submitted and followed by `runExtractAndCreate`, so
 * the server's queue - not this loop - is what orders the writes. A failed
 * archive is a verdict, and the next archive still runs; only a lost session
 * stops the loop, because the tail would queue operations nobody is left to read.
 */
export async function runExtractAndCreateMany(run: {
  plan: BulkPlan;
  deps: ExtractRunDeps;
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
  let created = 0;
  let alreadyCreated = 0;
  let landing: string | null = null;
  let stopped = false;
  let unauthorized = false;

  for (let index = 0; index < archives.length; index += 1) {
    const archive = archives[index];
    const target: BulkArchiveTarget = {
      path: archive.path,
      label: archive.label,
      name: archive.name,
    };
    let outcome: ExtractRunOutcome;

    if (archive.plan === null) {
      // The listing already failed in the plan pass, so there is nothing to plan
      // an operation from: one listing call, zero writes.
      outcome = { kind: "listing-failed", detail: archive.detail };
    } else {
      try {
        outcome = await runExtractAndCreate({
          archivePath: archive.path,
          name: archive.name,
          deps: run.deps,
          onPhase: phase => notify(() => run.onProgress?.(bulkPhaseLine(index + 1, total, phase))),
        });
      } catch (err) {
        // A 401 is the host's news, never a verdict: the untouched tail gets no
        // line, because nothing was visited there and a verdict is a claim about
        // what happened.
        if (err instanceof UnauthorizedError) {
          stopped = true;
          unauthorized = true;
          break;
        }
        outcome = { kind: "failed", detail: errorDetail(err) };
      }
    }

    const verdict: BulkArchiveVerdict = { ...target, outcome };
    verdicts.push(verdict);
    if (outcome.kind === "created") {
      created += 1;
      // One archive, one project: that is the only shape where "the project this
      // click made" has a single answer. Otherwise the caller stays in the queue.
      if (total === 1) landing = outcome.project_id;
    } else if (outcome.kind === "already-created") {
      alreadyCreated += 1;
    }
    notify(() => run.onVerdict?.(verdict));

    if (outcome.kind === "cancelled") {
      // The runner stopped the operation. The untouched tail gets no verdict,
      // because nothing was visited there.
      stopped = true;
      break;
    }
  }

  return { targets, verdicts, created, alreadyCreated, landing, stopped, unauthorized };
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
