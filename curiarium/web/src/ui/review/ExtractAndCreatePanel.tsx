import React from "react";

// Imported from the domain module, not the `../../lib/api` barrel: several
// suites replace that barrel with a fixed object, so a named import from it
// would come back `undefined` there (`useBundleDetect.ts` documents the same
// rule).
import type { OperationUndo } from "../../lib/api/operations";
import { notifyInfo } from "../../lib/toast";
import OperationProgress from "../operations/OperationProgress";
import { useOperationRun } from "../operations/useOperationRun";
import {
  excludedLine,
  extractActionLabel,
  extractArchiveHomeLine,
  extractCoverLine,
  extractDoneLine,
  extractExistingLine,
  extractGroupLine,
  extractSummaryLine,
  extractUndoLine,
  operationStatusLine,
} from "./bundleRows";

type Props = {
  /** The archive's library-relative path; the operation is keyed on it. */
  archivePath: string;
  /** The write gate: a viewer renders nothing and issues no request. */
  canContribute: boolean;
  /** The operator's typed name, already trimmed; "" lets the server name the
   *  project after the archive. */
  name?: string;
  /** The host's completion signal: refetch whatever the host owns. */
  onChanged?: () => void;
  /** Session expiry is the host's news, not this panel's. */
  onUnauthorized?: () => void;
};

const PRIMARY_BUTTON = "text-xs px-2.5 py-1.5 rounded-md bg-accent disabled:opacity-60";

/** The dialog's line about where the runner is, for the statuses that are not an
 *  outcome. The server's own token is the only thing that says this, so the
 *  sentence names it rather than inventing a stage the client cannot see. */
function stateLine(status: string): string {
  switch (status) {
    case "queued":
      return "The extraction is queued.";
    case "running":
      return "The extraction is running.";
    case "cancelling":
      return "The extraction is being cancelled.";
    default:
      return `The extraction is ${status}.`;
  }
}

/** The news an undo answers with, in the host's toast. The extracted files
 *  deliberately stay after an undo (the server documents the residue), so the
 *  sentence says so instead of implying the tree was erased. */
function announceUndo(answer: OperationUndo): void {
  notifyInfo("Extract undone", { details: [extractUndoLine(answer)] });
}

/**
 * The extract-and-create door, whole: the plan the server computed, ONE action
 * labelled with its outcome, and a popup that narrates the operation it queued.
 *
 * The state machine lives in `ui/operations/useOperationRun` and the dialog in
 * `ui/operations/OperationProgress`, so this file is the door's PREVIEW and its
 * strings, not a third implementation. The server computes every step: this panel
 * asks for a plan on mount and can only send that plan's `plan_id` back.
 *
 * A zero-step plan is the server saying a project already exists. It is rendered
 * as a finished sentence and gets NO action at all, because there is nothing to
 * approve.
 *
 * A viewer renders nothing at all: no wrapper, no layout track, no request.
 */
export default function ExtractAndCreatePanel(props: Props): React.ReactElement | null {
  const { archivePath, canContribute, name, onChanged, onUnauthorized } = props;
  const trimmedName = (name ?? "").trim();

  const run = useOperationRun({
    workflow: "extract-and-create",
    target: archivePath,
    params: trimmedName ? { name: trimmedName } : {},
    enabled: canContribute,
    onChanged,
    onUnauthorized,
    planFailureFallback: "Failed to plan the extraction.",
    submitFailureFallback: "Failed to submit the extraction.",
    pollFailureFallback: "Failed to read the operation.",
    undoFailureFallback: "Failed to undo the extraction.",
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

  const review = plan?.review ?? null;
  const alreadyCreated = review !== null && review.creates === 0;
  const homeLine = review === null ? null : extractArchiveHomeLine(review);
  const coverLine = review === null ? null : extractCoverLine(review.cover);

  return (
    <div className="flex flex-col gap-2">
      {planFailure !== null && (
        <span className="review-detail-reason text-red-500" role="alert" data-extract-failure="true">
          {planFailure}
        </span>
      )}
      {plan !== null && review !== null && operation === null && (
        // The plan as INFORMATION: what the one action would do, not a step the
        // operator has to take. Nothing here is a tab stop except the action.
        <div
          className="review-detail-facts"
          role="group"
          aria-label="Extract preview"
          data-extract-plan="true"
        >
          {alreadyCreated ? (
            <span data-extract-existing="true">{extractExistingLine(review)}</span>
          ) : (
            <>
              <span data-extract-summary="true">{extractSummaryLine(review)}</span>
              {/* The groups come from the WIRE ARRAY, never from a prose
                  rendering of it: a real list, so a screen reader reads one group
                  per item. The `""` bucket is the root's loose files. */}
              <ul className="flex flex-col gap-1">
                {review.groups.map(group => (
                  <li
                    key={group.name}
                    data-extract-group="true"
                    data-extract-group-name={group.name}
                  >
                    {extractGroupLine(group)}
                  </li>
                ))}
              </ul>
              {homeLine !== null && (
                <span data-extract-home="true" className="font-mono">
                  {homeLine}
                </span>
              )}
              {coverLine !== null && <span data-extract-cover="true">{coverLine}</span>}
            </>
          )}
          {/* The server's refusal of one file, verbatim: the client decided none
              of these and writes no words for one. */}
          {review.excluded.map(entry => (
            <span key={entry.path} className="review-detail-reason" data-extract-excluded="true">
              {excludedLine(entry)}
            </span>
          ))}
        </div>
      )}
      {submitFailure !== null && (
        <span
          className="review-detail-reason text-red-500"
          role="alert"
          data-extract-submit-failure="true"
        >
          {submitFailure}
        </span>
      )}
      {/* The ONE action, labelled with the outcome the plan promises. A zero-step
          plan has nothing to do, so it gets no button at all. */}
      {plan !== null && review !== null && operation === null && !alreadyCreated && (
        <div className="review-detail-actions">
          <button
            type="button"
            className={PRIMARY_BUTTON}
            data-extract-action-button="true"
            disabled={busy}
            onClick={() => void run.submit()}
          >
            {extractActionLabel(review)}
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
          dataPrefix="extract"
          ariaLabel="Extract progress"
          stateLine={stateLine}
          stepLine={operationStatusLine}
          doneLine={extractDoneLine}
          cancelledLine="The extraction was cancelled."
          undoLabel="Undo this extraction"
        />
      )}
    </div>
  );
}
