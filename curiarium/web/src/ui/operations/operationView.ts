// The one vocabulary the operations surfaces speak. Pure: no JSX, no React, and
// no request. The plan and the operation are the server's, and every function
// here only says what one of their lines reads as - never what the server should
// have done. One owner for every sentence also means two doors cannot word the
// same step or the same count differently.

import type { ArchiveExtractResult } from "../../lib/api/imports";
import type {
  Operation,
  OperationCover,
  OperationExcluded,
  OperationGroup,
  OperationMove,
  OperationPlan,
  OperationReview,
  OperationStep,
  OperationUndo,
} from "../../lib/api/operations";

/** `1 file` / `9 files`, so a count of one does not read as a bug. */
function fileCount(count: number): string {
  return counted(count, "file", "files");
}

/** `1 move` / `3 moves`, with the noun chosen by the count. */
function counted(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** One move, with both paths quoted: a path may hold spaces, and the separator
 *  between the two is the only thing that says which half is which. */
export function operationMoveLine(move: OperationMove): string {
  return `move "${move.source}" -> "${move.target}"`;
}

/** The word for WHY a move exists. The four tokens this client knows each get
 *  their own reading; any other token (a newer server's) is shown verbatim,
 *  because the client invents no word for a token it does not know - and never
 *  a dash, which would make a real move read as an absent one. */
export function operationActionLabel(action: string): string {
  switch (action) {
    case "hoist":
      return "hoist";
    case "move":
      return "move";
    case "file-archive":
      return "file archive";
    case "rename":
      return "rename";
    default:
      return action;
  }
}

/** One file the plan would leave out: the server's path and the server's own
 *  reason beside it, VERBATIM. The client decided no exclusion, so it writes no
 *  words for one - a reason paraphrased here would drift from the sentence the
 *  plan actually carries. */
export function excludedLine(entry: OperationExcluded): string {
  return `${entry.path}: ${entry.reason}`;
}

/** The four phrases one step's narration is chosen from: the runner's own
 *  vocabulary, one phrase per status a step can be in. */
type StepPhrases = { done: string; running: string; failed: string; pending: string };

/** One step's phrase for its status. A status outside the three finished ones
 *  reads as still-waiting, which is what anything not yet done means to the
 *  operator - the client invents no fifth word for it. */
function stepPhrase(status: string, phrases: StepPhrases): string {
  switch (status) {
    case "done":
      return phrases.done;
    case "running":
      return phrases.running;
    case "failed":
      return phrases.failed;
    default:
      return phrases.pending;
  }
}

/** The line a running operation's step reads as, driven by `op` and `status`
 *  alone - never by a count, which only the server's own result carries. A step
 *  whose `op` this client does not know is narrated with its two tokens
 *  verbatim: the client invents no word for a step it does not know. */
export function operationStatusLine(step: OperationStep): string {
  switch (step.op) {
    case "extract":
      return stepPhrase(step.status, {
        done: "extracted the archive",
        running: "extracting the archive\u2026",
        failed: "the extraction was refused",
        pending: "waiting to extract the archive",
      });
    case "move":
      return stepPhrase(step.status, {
        done: "moved the folders",
        running: "moving the folders\u2026",
        failed: "the move was refused",
        pending: "waiting to move the folders",
      });
    case "create-project":
      return stepPhrase(step.status, {
        done: "created a project",
        running: "creating a project\u2026",
        failed: "the create was refused",
        pending: "waiting to create a project",
      });
    case "verify-members":
      return stepPhrase(step.status, {
        done: "checked the project's files",
        running: "checking the project's files\u2026",
        failed: "the project's files did not match",
        pending: "waiting to check the files",
      });
    case "register":
      // The extract-and-create runner's third step registers exactly the files
      // the plan named, so the phrase is about registration rather than about a
      // scan of the whole library.
      return stepPhrase(step.status, {
        done: "registered the extracted files",
        running: "registering the extracted files\u2026",
        failed: "the files could not be registered",
        pending: "waiting to register the files",
      });
    case "mount-import":
      // One step, and it is the walk itself: the mounted folder is walked and
      // what it holds is registered, so both halves belong in the phrase.
      return stepPhrase(step.status, {
        done: "walked the mounted folder and registered what it holds",
        running: "walking the mounted folder\u2026",
        failed: "the mounted folder could not be walked",
        pending: "waiting to walk the mounted folder",
      });
    default:
      return `${step.op} ${step.status}`;
  }
}

/** The dialog's line for one step, delegated so the Progress dialog's `stepLine`
 *  prop and this module's narration cannot drift. */
export function stepLine(step: OperationStep): string {
  return operationStatusLine(step);
}

/** One group the plan would write. The `""` bucket is the archive's own root
 *  files, not a folder: the server keeps it so `sum(groups) === members` holds
 *  structurally, and calling it a directory would point the operator at a folder
 *  that does not exist. */
export function extractGroupLine(group: OperationGroup): string {
  const label = group.name === "" ? "loose files" : group.name;
  const models = `${group.models} ${group.models === 1 ? "model" : "models"}`;
  return `${label} - ${fileCount(group.members)}, ${models}`;
}

/** How much the plan would write, said once above the groups. */
export function extractSummaryLine(review: OperationReview): string {
  return `Would write ${fileCount(review.members)}.`;
}

/** One project the plan would create: its name and the members it would hold.
 *  The split door's wording, moved here so the shared review surface and the
 *  door cannot word the same project differently. */
export function projectLine(project: OperationPlan["review"]["projects"][number]): string {
  return `${project.name} from ${fileCount(project.members)}`;
}

/** Where the archive would be filed. `move_list` carries exactly one move for
 *  this workflow, and a plan with none is an archive already in its place - the
 *  server said so by naming no move, so no sentence is invented for a target. */
export function extractArchiveHomeLine(review: OperationReview): string | null {
  const move = review.move_list[0];
  if (move === undefined) return null;
  return `The archive moves to "${move.target}".`;
}

/** The planned cover, by path. The asset id is resolved later by the server, so
 *  only the path is shown - a null path means the plan named no cover at all. */
export function extractCoverLine(cover: OperationCover | null): string | null {
  if (cover === null || cover.rel_path === null) return null;
  return `The project's cover is "${cover.rel_path}".`;
}

/** The zero-step plan's sentence: the server found a project already, so there is
 *  nothing to approve. The name is the server's `existing_project_name`; a null
 *  name is reported as unavailable rather than guessed from the archive path. */
export function extractExistingLine(review: OperationReview): string {
  const name = (review.existing_project_name ?? "").trim();
  if (!name) return "This archive already has a project, so there is nothing to create.";
  return `The project "${name}" already exists, so there is nothing to create.`;
}

/** The one action's label, named for the OUTCOME and never for the subsystem.
 *  Counts and the project name come from the plan the server issued, so the
 *  button promises exactly what the review measured. A zero-step plan gets no
 *  label at all, because it gets no button. */
export function extractActionLabel(review: OperationReview): string {
  const name = (review.projects[0]?.name ?? review.existing_project_name ?? "").trim();
  const subject = name ? `'${name}'` : "a project";
  return `Extract and create ${subject} from ${fileCount(review.members)}`;
}

/** The dialog's done line, read from the CREATE STEP's own result. The server can
 *  lose a race at run time, so a step that answers `already_created` must say so
 *  instead of claiming a create the runner did not perform. */
export function extractDoneLine(operation: Operation, plan: OperationPlan): string {
  const create = operation.steps.find(step => step.op === "create-project");
  if (create === undefined) return "Done: no project was created";
  const status = create.result?.status;
  const resultName = create.result?.name;
  const plannedName = plan.review.projects[0]?.name ?? plan.review.existing_project_name;
  const name = typeof resultName === "string" && resultName ? resultName : (plannedName ?? "");
  const subject = name ? `project '${name}'` : "the project";
  if (status === "already_created") {
    return `Done: ${subject} already existed - nothing new was created`;
  }
  return `Done: ${subject} created`;
}

/** The news an undo answers with. The extracted files deliberately stay (the
 *  server documents the residue), so the sentence says so rather than implying an
 *  undo erased the tree. Every number is the server's own answer. */
export function extractUndoLine(answer: OperationUndo): string {
  if (!answer.undone) return "Nothing was undone.";
  const removed = answer.projects_removed ?? [];
  const moves = answer.moves ?? 0;
  const projects =
    removed.length === 0
      ? "no projects removed"
      : `removed ${removed.length} ${removed.length === 1 ? "project" : "projects"}`;
  return `Undone: ${moves} ${moves === 1 ? "move" : "moves"} folded back, ${projects}. The extracted files stay in the library.`;
}

/**
 * The extract step's own report, re-assembled into the shape the deleted route
 * answered. The server writes exactly these six fields there (it is the
 * `ArchiveExtractResult` the deleted route used to answer), so this only guards
 * the reader against a step that has not reported yet: null means no report, not
 * a zero.
 */
export function extractResult(operation: Operation): ArchiveExtractResult | null {
  const step = operation.steps.find(candidate => candidate.op === "extract");
  if (step === undefined || step.result === null) return null;
  const result = step.result;
  return {
    path: typeof result.path === "string" ? result.path : operation.target,
    destination: typeof result.destination === "string" ? result.destination : "",
    files: typeof result.files === "number" ? result.files : 0,
    bytes: typeof result.bytes === "number" ? result.bytes : 0,
    already_present: result.already_present === true,
    scan_started: result.scan_started === true,
  };
}

/** One step's raw report by `op`, or null when the runner wrote none. The report
 *  is the server's JSON of a shape the client only half-knows, so every reader
 *  below guards the field it takes - and null stays "no report", never a zero. */
function stepResult(operation: Operation, op: string): Record<string, unknown> | null {
  const step = operation.steps.find(candidate => candidate.op === op);
  return step?.result ?? null;
}

/** A count a step reported, or zero. */
function stepCount(result: Record<string, unknown>, key: string): number {
  const value = result[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

/** A sentence a step reported, or the empty string. */
function stepText(result: Record<string, unknown>, key: string): string {
  const value = result[key];
  return typeof value === "string" ? value.trim() : "";
}

/** The adopt action's label, named for the OUTCOME and never for the subsystem.
 *  Counts come from the plan the server issued. The `""` bucket is the folder's
 *  own root files, not a group: counting it would promise a group the operator
 *  cannot see. */
function adoptActionLabel(review: OperationReview): string {
  const groups = review.groups.filter(group => group.name !== "").length;
  const files = fileCount(review.members);
  if (groups === 0) return `Add ${files}`;
  if (groups === 1) return `Add ${files} in 1 group`;
  return `Add ${files} in ${groups} groups`;
}

/**
 * ONE outcome label per workflow, singular or plural derived from the review's
 * own counts - never a hardcoded count, never a raw workflow key.
 *
 * `params` is the third argument because enrich's working set is its bundle
 * keys, which the review deliberately does not carry: enrich adds no files, so
 * its `members` is 0 and a count read from the review would be a fabrication.
 * The count therefore comes from the params the panel is about to submit, and
 * absent keys fall back to a sentence that names no number.
 */
export function actionLabel(
  workflow: string,
  review: OperationReview,
  params: Record<string, unknown> = {},
): string {
  switch (workflow) {
    case "create-project":
      return "Create project";
    case "split-and-create":
      return review.projects.length <= 1
        ? "Split into 1 project"
        : `Split into ${review.projects.length} projects`;
    case "adopt":
      return adoptActionLabel(review);
    case "extract":
      return `Extract ${fileCount(review.members)}`;
    case "extract-and-create":
      return extractActionLabel(review);
    case "library-scan":
      return review.target === "" ? "Scan the whole library" : `Scan '${review.target}'`;
    case "import-link":
      return "Import from the page";
    case "import-zip":
      return `Import ${fileCount(review.members)} from the zip`;
    case "enrich": {
      const keys = params.keys;
      if (Array.isArray(keys) && keys.length > 0) {
        return `Read ${counted(keys.length, "vendor page", "vendor pages")}`;
      }
      return "Read the source page";
    }
    case "mount-import":
      // The walk's scope is the configured mount, not a library path, so the
      // label names the folder the operator configured rather than a count the
      // plan cannot know - the mount is walked, nothing is enumerated first.
      return "Walk the mounted folder";
    default:
      return `Run ${workflow}`;
  }
}

/**
 * The plan's own counts, in one order, so two workflows never disagree about
 * where a number sits. Zeros are skipped, so a review prints only the facts it
 * actually carries. `creates` is deliberately absent: it counts projects for one
 * workflow and asset rows for another, so a line built from it would mean two
 * things depending on which review it read.
 */
export function reviewFacts(review: OperationReview): string[] {
  const facts: string[] = [];
  if (review.members > 0) facts.push(fileCount(review.members));
  if (review.groups.length > 0) facts.push(counted(review.groups.length, "group", "groups"));
  if (review.moves > 0) facts.push(counted(review.moves, "move", "moves"));
  if (review.projects.length > 0) {
    facts.push(counted(review.projects.length, "project", "projects"));
  }
  if (review.adopts > 0) facts.push(counted(review.adopts, "adopted file", "adopted files"));
  if (review.deletes > 0) facts.push(counted(review.deletes, "deletion", "deletions"));
  const existing = (review.existing_project_name ?? "").trim();
  if (existing) facts.push(`Existing project "${existing}"`);
  return facts;
}

/**
 * The plan's own step summaries in index order. This is the "what will happen"
 * list, and it is what makes the surface uniform for the workflows whose review
 * is thin: a library scan cannot count files at plan time, and enrich names one
 * working set rather than a set of files.
 */
export function planSteps(plan: OperationPlan): string[] {
  return [...plan.steps]
    .sort((left, right) => left.index - right.index)
    .map(step => step.summary);
}

/** Whether a status is one the runner is still working through: a queued,
 *  running or cancelling row is in flight, and a terminal row is not. */
export function isActiveStatus(status: string): boolean {
  return status === "queued" || status === "running" || status === "cancelling";
}

/** The status as a word. A token this client does not know reads verbatim rather
 *  than being flattened into a neighbour it is not. */
export function statusLabel(status: string): string {
  switch (status) {
    case "queued":
      return "Queued";
    case "running":
      return "Running";
    case "cancelling":
      return "Cancelling";
    case "done":
      return "Done";
    case "failed":
      return "Failed";
    case "cancelled":
      return "Cancelled";
    default:
      return status;
  }
}

/** The workflow as a sentence. An unknown workflow reads verbatim: the client
 *  invents no word for a workflow it does not know, and never hides the key. */
export function workflowLabel(workflow: string): string {
  switch (workflow) {
    case "create-project":
      return "Create a project";
    case "split-and-create":
      return "Split into projects";
    case "adopt":
      return "Adopt grown files";
    case "extract-and-create":
      return "Extract and create";
    case "extract":
      return "Extract an archive";
    case "library-scan":
      return "Library scan";
    case "import-link":
      return "Import a link";
    case "import-zip":
      return "Import a zip";
    case "enrich":
      return "Enrich from the source page";
    case "mount-import":
      return "Mounted import";
    default:
      return workflow;
  }
}

/** The noun a workflow's status sentence is built around. Unknown workflows fall
 *  back to their own key, so the sentence still names what is running. */
function operationNoun(workflow: string): string {
  switch (workflow) {
    case "create-project":
      return "create";
    case "split-and-create":
      return "split";
    case "adopt":
      return "adopt";
    case "extract-and-create":
      return "extraction";
    case "extract":
      return "extraction";
    case "library-scan":
      return "scan";
    case "import-link":
      return "import";
    case "import-zip":
      return "import";
    case "enrich":
      return "enrichment";
    case "mount-import":
      return "walk";
    default:
      return workflow;
  }
}

/** The progress dialog's line about where the runner is, for the statuses that
 *  are not an outcome. Per workflow, so "The split is queued." names the split
 *  rather than borrowing another door's noun. */
export function stateLine(workflow: string, status: string): string {
  const noun = operationNoun(workflow);
  switch (status) {
    case "queued":
      return `The ${noun} is queued.`;
    case "running":
      return `The ${noun} is running.`;
    case "cancelling":
      return `The ${noun} is being cancelled.`;
    default:
      return `The ${noun} is ${status}.`;
  }
}

/** One finished operation's outcome line. Tolerates `plan === null`, which is
 *  what a resumed row has: a finished split can legitimately refuse a re-plan,
 *  and refusing to narrate it would defeat the resume. Every missing number
 *  falls back to the operation's own step result and then to a sentence that
 *  names no number, so a done line never throws and never lies. */
export function doneLine(
  workflow: string,
  operation: Operation,
  plan: OperationPlan | null,
): string {
  switch (workflow) {
    case "split-and-create": {
      if (plan !== null) {
        return `Done: ${counted(plan.review.projects.length, "project", "projects")}`;
      }
      const created = operation.steps.filter(
        step => step.op === "create-project" && step.result?.created === true,
      ).length;
      return `Done: ${counted(created, "project", "projects")}`;
    }
    case "adopt": {
      if (plan !== null) return `Done: ${fileCount(plan.review.members)} added`;
      const adopt = operation.steps.find(step => step.op === "adopt");
      const members = adopt?.result?.members;
      if (typeof members === "number") return `Done: ${fileCount(members)} added`;
      return "Done: the files were added.";
    }
    case "extract": {
      const result = extractResult(operation);
      if (result === null) return "The extraction finished without a report.";
      if (result.already_present) return "Already extracted - nothing to do.";
      if (result.files === 0) {
        return "Nothing was written - the archive held no extractable files.";
      }
      return `Extracted ${fileCount(result.files)}. The files are registered.`;
    }
    case "extract-and-create":
      if (plan !== null) return extractDoneLine(operation, plan);
      return "Done: the extraction finished.";
    case "create-project": {
      const create = operation.steps.find(step => step.op === "create-project");
      const status = create?.result?.status;
      const resultName = create?.result?.name;
      const plannedName =
        plan?.review.projects[0]?.name ?? plan?.review.existing_project_name ?? "";
      const name = typeof resultName === "string" && resultName ? resultName : plannedName;
      if (name) {
        if (status === "already_created") {
          return `Done: project '${name}' already existed - nothing new was created`;
        }
        return `Done: project '${name}' created`;
      }
      if (status === "already_created") {
        return "Done: the project already existed - nothing new was created";
      }
      return "Done: the project was created";
    }
    case "library-scan":
      return "Done: the scan finished.";
    case "mount-import": {
      const report = stepResult(operation, "mount-import");
      if (report === null) return "Done: the walk finished without a report.";
      // A skipped walk never looked at the mount, so its reason IS the outcome
      // and printing counters beside it would claim a walk that did not happen.
      if (report.outcome === "skipped") {
        const why = stepText(report, "reason");
        if (why) return `Done: the walk skipped the mounted folder - ${why}`;
        return "Done: the walk skipped the mounted folder.";
      }
      const registered = stepCount(report, "registered");
      const failed = stepCount(report, "failed");
      if (registered === 0 && failed === 0) {
        return "Done: the walk found nothing to register in the mounted folder.";
      }
      if (failed > 0) {
        return `Done: registered ${fileCount(registered)}, ${counted(
          failed,
          "file",
          "files",
        )} could not be read.`;
      }
      return `Done: registered ${fileCount(registered)} from the mounted folder.`;
    }
    case "import-link":
    case "import-zip":
      if (plan !== null && plan.review.members > 0) {
        return `Done: imported ${fileCount(plan.review.members)}.`;
      }
      return "Done: the import finished.";
    case "enrich":
      return "Done: the source page was read.";
    default:
      return "Done.";
  }
}

/** The news an undo answers with, per workflow. Every number is the server's own
 *  answer, and each workflow reads only the keys its own restore writes - a leg
 *  that says nothing stays null rather than becoming a fabricated zero. */
export function undoLine(workflow: string, answer: OperationUndo): string {
  switch (workflow) {
    case "extract":
    case "extract-and-create":
      return extractUndoLine(answer);
    case "adopt":
      if (answer.undone === false) return "Nothing was undone.";
      return `Undone: ${counted(answer.members_removed ?? 0, "file", "files")} removed.`;
    case "split-and-create": {
      if (!answer.undone) return "Nothing was undone.";
      const moves = answer.moves ?? 0;
      const projects = (answer.projects_removed ?? []).length;
      return `Undone: ${counted(moves, "move", "moves")} folded back, ${counted(
        projects,
        "project",
        "projects",
      )} removed.`;
    }
    case "create-project": {
      if (!answer.undone) return "Nothing was undone.";
      const name = answer.project_name;
      if (name) return `Undone: project '${name}' removed.`;
      return "Undone: the project was removed.";
    }
    case "import-link":
    case "import-zip":
      if (!answer.undone) return "Nothing was undone.";
      return `Undone: ${counted(answer.assets_removed ?? 0, "asset", "assets")} removed.`;
    case "library-scan":
      return "Nothing was undone.";
    default:
      return answer.undone ? "Undone." : "Nothing was undone.";
  }
}

/** One row of the operations queue, said in the one vocabulary. `resumable` is
 *  true for an in-flight row the queue may offer to resume: an operation that
 *  already ended is never resumable. */
export type QueueRow = {
  id: string;
  workflow: string;
  workflowLabel: string;
  target: string;
  submitter: string;
  status: string;
  statusLabel: string;
  created: string;
  started: string | null;
  finished: string | null;
  undoState: string;
  active: boolean;
  resumable: boolean;
  /** The row itself, so an expanded row can narrate its own steps. */
  operation: Operation;
};

/** The queue's view models, in the server's order (newest first). The order is
 *  preserved rather than re-sorted: the server already answered the order the
 *  ledger stores. */
export function queueRows(operations: Operation[]): QueueRow[] {
  return operations.map(operation => {
    const active = isActiveStatus(operation.status);
    return {
      id: operation.id,
      workflow: operation.workflow,
      workflowLabel: workflowLabel(operation.workflow),
      target: operation.target,
      submitter: operation.submitted_by,
      status: operation.status,
      statusLabel: statusLabel(operation.status),
      created: operation.created_at,
      started: operation.started_at,
      finished: operation.finished_at,
      undoState: operation.undo_state,
      active,
      resumable: active && operation.undo_state !== "undone",
      operation,
    };
  });
}

/** The newest in-flight operation for one workflow and target, or null.
 *
 *  The lookup is what makes "an operation is already in flight for this target"
 *  one server read rather than a client guess. A terminal row is NEVER returned:
 *  resuming a finished operation would offer a confirm over work already done.
 *  The newest row wins (created_at descending, then id descending), and the list
 *  is sorted defensively rather than trusting the caller's order. */
export function inFlightFor(
  operations: Operation[],
  workflow: string,
  target: string,
): Operation | null {
  const matches = operations
    .filter(
      operation =>
        operation.workflow === workflow &&
        operation.target === target &&
        isActiveStatus(operation.status),
    )
    .sort((left, right) => {
      if (left.created_at !== right.created_at) {
        return left.created_at < right.created_at ? 1 : -1;
      }
      if (left.id === right.id) return 0;
      return left.id < right.id ? 1 : -1;
    });
  return matches[0] ?? null;
}
