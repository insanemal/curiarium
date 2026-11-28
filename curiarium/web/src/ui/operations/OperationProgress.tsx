import React from "react";

import type { Operation, OperationPlan, OperationStep } from "../../lib/api/operations";
import { isTerminal } from "./useOperationRun";

/**
 * The progress dialog for one server-computed operation, shared by every door.
 *
 * It invents no status, count or sentence: every line comes from props, and the
 * `data-*` attributes are the door's namespace (`dataPrefix`), so the split door
 * still renders `data-split-*` and a second door can bring its own without a
 * second copy of this markup.
 */

const SECONDARY_BUTTON =
  "text-xs px-2.5 py-1.5 rounded-md border border-panel-strong disabled:opacity-60";

type Props = {
  operation: Operation;
  plan: OperationPlan;
  orderedSteps: OperationStep[];
  pollFailure: string | null;
  undoFailure: string | null;
  busy: boolean;
  onUndo: () => void;
  /** The door's attribute namespace: "split" yields data-split-dialog etc. */
  dataPrefix: string;
  ariaLabel: string;
  stateLine: (status: string) => string;
  stepLine: (step: OperationStep) => string;
  doneLine: (operation: Operation, plan: OperationPlan) => string;
  cancelledLine: string;
  undoLabel: string;
};

export default function OperationProgress(props: Props): React.ReactElement {
  const {
    operation,
    plan,
    orderedSteps,
    pollFailure,
    undoFailure,
    busy,
    onUndo,
    dataPrefix,
    ariaLabel,
    stateLine,
    stepLine,
    doneLine,
    cancelledLine,
    undoLabel,
  } = props;

  const flagged = (name: string) => ({ [`data-${dataPrefix}-${name}`]: "true" });
  // The step tags carry the runner's own tokens as well, so their names stay
  // mechanical too.
  const stepFlags = (step: OperationStep) => ({
    [`data-${dataPrefix}-step`]: "true",
    [`data-${dataPrefix}-op`]: step.op,
    [`data-${dataPrefix}-status`]: step.status,
  });

  return (
    // Progress, in place - not a toast and not a modal the operator must dismiss.
    // The dialog narrates the runner's own steps in index order.
    <div role="dialog" aria-label={ariaLabel} {...flagged("dialog")}>
      {!isTerminal(operation.status) && (
        <span {...flagged("state")}>{stateLine(operation.status)}</span>
      )}
      {pollFailure !== null && (
        <span
          className="review-detail-reason text-red-500"
          role="alert"
          {...flagged("poll-failure")}
        >
          {pollFailure}
        </span>
      )}
      <ul className="flex flex-col gap-1">
        {orderedSteps.map(step => (
          <li key={step.index} {...stepFlags(step)}>
            {stepLine(step)}
          </li>
        ))}
      </ul>
      {operation.status === "done" && (
        <span {...flagged("result")}>{doneLine(operation, plan)}</span>
      )}
      {operation.status === "failed" && operation.error !== null && (
        <span className="review-detail-reason text-red-500" role="alert" {...flagged("error")}>
          {operation.error}
        </span>
      )}
      {operation.status === "cancelled" && <span {...flagged("cancelled")}>{cancelledLine}</span>}
      {operation.undo_state === "available" && (
        <div className="review-detail-actions">
          <button
            type="button"
            className={SECONDARY_BUTTON}
            {...flagged("undo")}
            disabled={busy}
            onClick={onUndo}
          >
            {undoLabel}
          </button>
        </div>
      )}
      {undoFailure !== null && (
        <span
          className="review-detail-reason text-red-500"
          role="alert"
          {...flagged("undo-failure")}
        >
          {undoFailure}
        </span>
      )}
    </div>
  );
}
