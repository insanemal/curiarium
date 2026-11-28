import React from "react";

// Imported from the domain module, not the `../../lib/api` barrel: several
// suites replace that barrel with a fixed object, so a named import from it
// would come back `undefined` there (`useBundleDetect.ts` documents the same
// rule).
import {
  splitActionLabel,
  type Operation,
  type OperationPlan,
  type OperationUndo,
} from "../../lib/api/operations";
import { notifyInfo } from "../../lib/toast";
import OperationProgress from "../operations/OperationProgress";
import { useOperationRun } from "../operations/useOperationRun";
import { excludedLine, operationActionLabel, operationMoveLine, operationStatusLine } from "./bundleRows";

type Props = {
  /** The storage-relative key whose top folders the split hoists. */
  bundleKey: string;
  /** The shape the operator chose; "" means the server's own default. */
  shape: string;
  /** The write gate: a viewer renders nothing and issues no request. */
  canContribute: boolean;
  /** The host's completion signal: refetch whatever the host owns. */
  onChanged?: () => void;
  /** Session expiry is the host's news, not this panel's. */
  onUnauthorized?: () => void;
};

const PRIMARY_BUTTON = "text-xs px-2.5 py-1.5 rounded-md bg-accent disabled:opacity-60";

/** The dialog's line about where the runner is, for the statuses that are not
 *  an outcome. The server's own token is the only thing that says this, so the
 *  sentence names it rather than inventing a stage the client cannot see. */
function stateLine(status: string): string {
  switch (status) {
    case "queued":
      return "The split is queued.";
    case "running":
      return "The split is running.";
    case "cancelling":
      return "The split is being cancelled.";
    default:
      return `The split is ${status}.`;
  }
}

/** How many projects the split made, in words. `Done: 3 projects` comes from the
 *  PLAN's own project list - the request the operator approved - because that is
 *  the count the button promised; the server's create steps are the second
 *  opinion, used only when the plan names no project at all. */
function doneLine(operation: Operation, plan: OperationPlan): string {
  const planned = plan.review.projects.length;
  const created = operation.steps.filter(
    step => step.op === "create-project" && step.result?.created === true,
  ).length;
  const total = planned > 0 ? planned : created;
  return `Done: ${total} ${total === 1 ? "project" : "projects"}`;
}

/** One project's line: its name and how many files it would hold. */
function projectLine(project: OperationPlan["review"]["projects"][number]): string {
  const files = project.members === 1 ? "file" : "files";
  return `${project.name} from ${project.members} ${files}`;
}

/** The news an undo answers with: what it folded back, in the host's toast. */
function announceUndo(answer: OperationUndo): void {
  const removed = answer.projects_removed ?? [];
  notifyInfo("Split undone", removed.length > 0 ? { details: removed } : undefined);
}

/**
 * The split door, whole: the plan the server computed, ONE action labelled with
 * its outcome, and a popup that narrates the operation the action queued.
 *
 * The state machine now lives in `ui/operations/useOperationRun` and the dialog
 * in `ui/operations/OperationProgress`, so this file is the SPLIT's door - its
 * preview and its strings - and not a second implementation: the project page's
 * adopt control opens the same machine with its own words.
 *
 * The server computes every step: this panel asks for a plan on mount and can
 * only send that plan's `plan_id` back. It never invents a step, a count or a
 * status - the moves are information the operator reads, not a ladder they have
 * to climb, and the popup's lines come from the runner's own step records.
 *
 * ONE action walks every door the server named: the moves and the creates are
 * two halves of a single intent, so one button approves both. The popup that
 * follows is progress rather than a gate - nothing there has to be pressed.
 *
 * A viewer renders nothing at all: no wrapper, no layout track, no request.
 */
export default function SplitAndCreatePanel(props: Props): React.ReactElement | null {
  const { bundleKey, shape, canContribute, onChanged, onUnauthorized } = props;

  const run = useOperationRun({
    workflow: "split-and-create",
    target: bundleKey,
    params: shape ? { shape } : {},
    enabled: canContribute,
    onChanged,
    onUnauthorized,
    planFailureFallback: "Failed to plan the split.",
    submitFailureFallback: "Failed to submit the split.",
    pollFailureFallback: "Failed to read the operation.",
    undoFailureFallback: "Failed to undo the split.",
  });
  const { plan, planFailure, submitFailure, operation, busy, orderedSteps, pollFailure, undoFailure } =
    run;

  // Every hook runs before this gate, so the early return breaks no hook order;
  // the hook checks the gate itself because its effects are still armed.
  if (!canContribute) return null;

  async function undo(): Promise<void> {
    const answer = await run.undo();
    if (answer !== null) announceUndo(answer);
  }

  return (
    <div className="flex flex-col gap-2">
      {planFailure !== null && (
        <span className="review-detail-reason text-red-500" role="alert" data-split-failure="true">
          {planFailure}
        </span>
      )}
      {plan !== null && operation === null && (
        // The plan as INFORMATION: what the one action would do, not a step the
        // operator has to take. Nothing here is a tab stop except the action.
        <div
          className="review-detail-facts"
          role="group"
          aria-label="Split preview"
          data-split-plan="true"
        >
          {/* The moves come from the WIRE ARRAY, never from a prose rendering of
              it: a real list, so a screen reader reads one move per item. */}
          <ul className="flex flex-col gap-1">
            {plan.review.move_list.map((move, index) => (
              <li
                key={`${index}-${move.source}-${move.target}`}
                data-split-move="true"
                data-split-action={move.action}
              >
                <span className="font-mono">{operationMoveLine(move)}</span>{" "}
                <span className="text-muted">{operationActionLabel(move.action)}</span>
              </li>
            ))}
          </ul>
          {/* The projects the same action would create. `members` is the plan's
              own count, so the line says how much each project would hold. */}
          <ul className="flex flex-col gap-1">
            {plan.review.projects.map(project => (
              <li key={project.root} data-split-project="true" data-split-root={project.root}>
                {projectLine(project)}
              </li>
            ))}
          </ul>
          {/* The server's refusal of one file, verbatim: the client decided none
              of these and writes no words for one. */}
          {plan.review.excluded.map(entry => (
            <span key={entry.path} className="review-detail-reason" data-split-excluded="true">
              {excludedLine(entry)}
            </span>
          ))}
        </div>
      )}
      {submitFailure !== null && (
        <span
          className="review-detail-reason text-red-500"
          role="alert"
          data-split-submit-failure="true"
        >
          {submitFailure}
        </span>
      )}
      {/* The ONE action, labelled with the outcome the plan promises. A plan that
          names no project has nothing to do, so it gets no button at all. */}
      {plan !== null && operation === null && plan.review.projects.length > 0 && (
        <div className="review-detail-actions">
          <button
            type="button"
            className={PRIMARY_BUTTON}
            data-split-action-button="true"
            disabled={busy}
            onClick={() => void run.submit()}
          >
            {splitActionLabel(plan.review)}
          </button>
        </div>
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
          dataPrefix="split"
          ariaLabel="Split progress"
          stateLine={stateLine}
          stepLine={operationStatusLine}
          doneLine={doneLine}
          cancelledLine="The split was cancelled."
          undoLabel="Undo this split"
        />
      )}
    </div>
  );
}
