import React from "react";

// Imported from the domain module, not the `../../lib/api` barrel: several
// suites replace that barrel with a fixed object, so a named import from it
// would come back `undefined` there (`useBundleDetect.ts` documents the same
// rule).
import {
  adoptActionLabel,
  type Operation,
  type OperationPlan,
  type OperationStep,
} from "../../lib/api/operations";
import { notifyInfo } from "../../lib/toast";
import OperationProgress from "../operations/OperationProgress";
import { useOperationRun } from "../operations/useOperationRun";
import { excludedLine, operationStatusLine } from "../review/bundleRows";

type Props = {
  /** The project whose folder grew; the server resolves the delta from its marker. */
  projectId: string;
  /** The host's completion signal: refetch the project, its members and the drift. */
  onChanged?: () => void;
  /** Session expiry is the host's news, not this control's. */
  onUnauthorized?: () => void;
  /**
   * Whether this control currently has a run to show. The drift notice that
   * hosts it is keyed on the drift COUNT, which the run itself drives to zero -
   * so without this the host would unmount the control the instant the adopt
   * finished, taking the result line and the undo button with it. `true` while a
   * submitted operation is on screen, `false` again once it has been undone.
   */
  onActiveChange?: (active: boolean) => void;
};

const PRIMARY_BUTTON = "text-xs px-2.5 py-1.5 rounded-md bg-accent disabled:opacity-60";

/** The census is printed in the order the scan walks the roles, so two grown
 *  folders never disagree about where a count sits. A role outside this list
 *  (a newer server's) has no line here - the client writes no words for it. */
const ROLE_ORDER = ["model", "image", "text", "archive", "other"];

/** One step's four phrases: the runner's own vocabulary, one per status. */
type StepPhrases = { done: string; running: string; failed: string; pending: string };

/** The steps an adopt runs, each said as what it did rather than as the token
 *  the runner wrote. `verify-members` is absent on purpose: it narrates the
 *  same work as the split's, so it falls through to `operationStatusLine`. */
const STEP_PHRASES: Record<string, StepPhrases> = {
  adopt: {
    done: "added the files",
    running: "adding the files\u2026",
    failed: "the files were not added",
    pending: "waiting to add the files",
  },
  "refresh-groups": {
    done: "refreshed the project's groups",
    running: "refreshing the project's groups\u2026",
    failed: "the project's groups were not refreshed",
    pending: "waiting to refresh the project's groups",
  },
};

/** One role's line: the count and the noun a human would use. The text role is
 *  a DOCUMENT to a reader, and every other role takes a plain plural. */
function roleLine(role: string, count: number): string {
  const noun = role === "text" ? "document" : role;
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/** One step's phrase for its status. A status outside the three finished ones
 *  reads as still-waiting, which is what anything not yet done means. */
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

function adoptStepLine(step: OperationStep): string {
  const phrases = STEP_PHRASES[step.op];
  if (phrases === undefined) return operationStatusLine(step);
  return stepPhrase(step.status, phrases);
}

/** The dialog's line about where the runner is, for the statuses that are not
 *  an outcome. The server's own token is named rather than guessed at. */
function adoptStateLine(status: string): string {
  switch (status) {
    case "queued":
      return "The adopt is queued.";
    case "running":
      return "The adopt is running.";
    case "cancelling":
      return "The adopt is being cancelled.";
    default:
      return `The adopt is ${status}.`;
  }
}

/** How many files landed, in words. The PLAN's own count is the number the
 *  button promised, so the done line repeats it rather than tallying rows. */
function adoptDoneLine(_operation: Operation, plan: OperationPlan): string {
  const members = plan.review.members;
  return `Done: ${members} ${members === 1 ? "file" : "files"} added`;
}

/**
 * The project page's grown-folder action: the drift notice's ONE action,
 * labelled with the outcome ("Add 171 files in 9 groups") and never with the
 * subsystem.
 *
 * It submits the server's plan, narrates the runner in the shared progress
 * dialog and offers the undo the server says is still meaningful. The envelope
 * is deliberately small: the state machine lives in
 * `ui/operations/useOperationRun`, so this file is the ADOPT's door - its
 * preview and its words - and not a second implementation.
 *
 * It does NOT replace the manual attach picker: that stays for choosing
 * individual files, while this action adopts the whole delta the marker names.
 */
export default function AdoptGrownParts(props: Props): React.ReactElement {
  const { projectId, onChanged, onUnauthorized, onActiveChange } = props;

  const run = useOperationRun({
    workflow: "adopt",
    target: projectId,
    enabled: true,
    onChanged,
    onUnauthorized,
    planFailureFallback: "Failed to plan the adopt.",
    submitFailureFallback: "Failed to submit the adopt.",
    pollFailureFallback: "Failed to read the operation.",
    undoFailureFallback: "Failed to undo the adopt.",
  });
  const { plan, planFailure, submitFailure, operation, busy, orderedSteps, pollFailure, undoFailure } =
    run;

  // The host hears whether the control has something to show, so it can keep the
  // notice mounted across the drift going to zero. An undone operation has
  // nothing left to offer, so it reports inactive and the host may close the
  // notice (the undo's own `onChanged` refetches the drift, which brings the
  // action back).
  const active = operation !== null && operation.undo_state !== "undone";
  React.useEffect(() => {
    onActiveChange?.(active);
  }, [active, onActiveChange]);

  async function undo(): Promise<void> {
    const answer = await run.undo();
    if (answer === null) return;
    notifyInfo(
      "Adopt undone",
      answer.members_removed
        ? { details: [`${answer.members_removed} files removed`] }
        : undefined,
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {planFailure !== null && (
        <span className="review-detail-reason text-red-500" role="alert" data-adopt-failure="true">
          {planFailure}
        </span>
      )}
      {plan !== null && operation === null && (
        // The plan as INFORMATION: the mix the one action would add and the
        // files the server leaves out. Nothing here is a tab stop but the
        // action.
        <div
          className="flex flex-col gap-1"
          role="group"
          aria-label="Adopt preview"
          data-adopt-plan="true"
        >
          {/* The census in the scan's role order, zeros skipped: they exist so
              the server's sum is checkable, not so every page prints five. A
              review from a server that predates the census renders none. */}
          {ROLE_ORDER.filter(role => ((plan.review.by_role ?? {})[role] ?? 0) > 0).map(role => {
            const count = (plan.review.by_role ?? {})[role] ?? 0;
            return (
              <span key={role} data-adopt-role={role} data-adopt-count={count}>
                {roleLine(role, count)}
              </span>
            );
          })}
          {/* The server's refusal of one file, verbatim: the client decided none
              of these and writes no words for one. */}
          {plan.review.excluded.map(entry => (
            <span key={entry.path} data-adopt-excluded="true">
              {excludedLine(entry)}
            </span>
          ))}
        </div>
      )}
      {submitFailure !== null && (
        <span
          className="review-detail-reason text-red-500"
          role="alert"
          data-adopt-submit-failure="true"
        >
          {submitFailure}
        </span>
      )}
      {/* The ONE action, labelled with the outcome the plan promises. A plan
          that adds no member has nothing to do, so it gets no button at all. */}
      {plan !== null && operation === null && plan.review.members > 0 && (
        <button
          type="button"
          className={PRIMARY_BUTTON}
          data-adopt-action-button="true"
          disabled={busy}
          onClick={() => void run.submit()}
        >
          {adoptActionLabel(plan.review)}
        </button>
      )}
      {operation !== null && plan !== null && (
        <OperationProgress
          operation={operation}
          plan={plan}
          orderedSteps={orderedSteps}
          pollFailure={pollFailure}
          undoFailure={undoFailure}
          busy={busy}
          onUndo={undo}
          dataPrefix="adopt"
          ariaLabel="Adopt progress"
          stateLine={adoptStateLine}
          stepLine={adoptStepLine}
          doneLine={adoptDoneLine}
          cancelledLine="The adopt was cancelled."
          undoLabel="Undo this adopt"
        />
      )}
    </div>
  );
}
