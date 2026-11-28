import React from "react";

// Imported from the domain module, not the `../../lib/api` barrel: several
// suites replace that barrel with a fixed object, so a named import from it
// would come back `undefined` there (`useBundleDetect.ts` documents the same
// rule).
import {
  cancelOperation,
  undoOperation,
} from "../../lib/api/operations";
import { UnauthorizedError } from "../../lib/api/http";
import OperationProgress from "./OperationProgress";
import {
  doneLine,
  isActiveStatus,
  stateLine,
  statusLabel,
  stepLine,
  workflowLabel,
  type QueueRow,
} from "./operationView";
import type { OperationsQueueState } from "./useOperationsQueue";

/**
 * The operations queue's one surface: the filters, the rows and the one expanded
 * row's narration.
 *
 * The rows are view models (`QueueRow`) chosen in `operationView`, so this file
 * words nothing it did not receive. The queue hook owns the reads and the poll;
 * this component owns the selection (one expanded row at a time) and the two
 * writes a row offers - Cancel and Undo. Both writes are contribute, so a viewer
 * reads the queue and nothing else.
 *
 * Undo is offered by `OperationProgress` itself (it renders the button only while
 * the row's `undo_state` is `available`), and the server's refusal is shown
 * VERBATIM: a superseded 409 carries the reason the operator needs to read.
 */

type Props = {
  state: OperationsQueueState;
  /** The signed-in username: "Only mine" sends it as `submitted_by`. */
  me: string;
  /** Cancel and Undo are contribute; a viewer reads the queue and nothing else. */
  canContribute: boolean;
  /** The host's refetch signal after an undo or a cancel changed the library. */
  onChanged?: () => void;
  onUnauthorized?: () => void;
};

/** The six statuses the server answers, in the order the ledger walks them. */
const STATUSES = ["queued", "running", "cancelling", "cancelled", "done", "failed"];

/** The nine workflows the server runs, so the filter names them all. */
const WORKFLOWS = [
  "create-project",
  "split-and-create",
  "adopt",
  "extract-and-create",
  "extract",
  "library-scan",
  "import-link",
  "import-zip",
  "enrich",
];

const UNDO_FAILURE = "Could not undo the operation.";
const CANCEL_FAILURE = "Could not cancel the operation.";

function failureDetail(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

/** The server's timestamps are ISO strings; a row reads them in the operator's
 *  own locale rather than as a machine token. An unparsable value is shown
 *  verbatim, never as "Invalid Date". */
function formatTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

/** The count line above the rows. `activeCount` is null until a read succeeded,
 *  so a queue that has not loaded does not claim zero in flight. */
function countLine(count: number, activeCount: number | null): string {
  if (count === 0) return "No operations";
  const noun = count === 1 ? "operation" : "operations";
  const inFlight = activeCount !== null && activeCount > 0 ? ` -- ${activeCount} in flight` : "";
  return `${count} ${noun}${inFlight}`;
}

export default function OperationsQueue(props: Props): React.ReactElement {
  const { state, me, canContribute, onChanged, onUnauthorized } = props;

  // Only ONE row is expanded: two panels of step narrations would read as two
  // running operations.
  const [expandedId, setExpandedId] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [failure, setFailure] = React.useState<string | null>(null);

  async function undo(row: QueueRow): Promise<void> {
    setBusy(true);
    setFailure(null);
    try {
      await undoOperation(row.id);
      // The ledger changed, so the rows and the host's own data are both stale.
      await state.refresh();
      onChanged?.();
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized?.();
        return;
      }
      setFailure(failureDetail(err, UNDO_FAILURE));
    } finally {
      setBusy(false);
    }
  }

  async function cancel(row: QueueRow): Promise<void> {
    setBusy(true);
    setFailure(null);
    try {
      await cancelOperation(row.id);
      await state.refresh();
      onChanged?.();
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized?.();
        return;
      }
      setFailure(failureDetail(err, CANCEL_FAILURE));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      className="flex flex-col gap-3"
      data-operations-queue="true"
      aria-label="Operations"
    >
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <select
          aria-label="Filter by status"
          data-operations-filter-status="true"
          value={state.filters.state}
          onChange={event => state.setFilters({ state: event.target.value })}
          className="px-2 py-1 rounded-md border border-panel-strong bg-panel"
        >
          <option value="">All</option>
          {STATUSES.map(status => (
            <option key={status} value={status}>
              {statusLabel(status)}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter by workflow"
          data-operations-filter-workflow="true"
          value={state.filters.workflow}
          onChange={event => state.setFilters({ workflow: event.target.value })}
          className="px-2 py-1 rounded-md border border-panel-strong bg-panel"
        >
          <option value="">All</option>
          {WORKFLOWS.map(workflow => (
            <option key={workflow} value={workflow}>
              {workflowLabel(workflow)}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-1">
          <input
            type="checkbox"
            data-operations-filter-mine="true"
            checked={state.filters.submitted_by !== ""}
            onChange={event =>
              state.setFilters({ submitted_by: event.target.checked ? me : "" })
            }
          />
          Only mine
        </label>
      </div>

      {/* A live region, so a poll that changes the count does not interrupt a
          screen reader mid-sentence. */}
      <span aria-live="polite" data-operations-count="true" className="text-sm">
        {countLine(state.rows.length, state.activeCount)}
      </span>

      {state.loading && <p data-operations-loading="true">Loading operations...</p>}
      {state.error !== null && (
        <p role="alert" data-operations-error="true" className="review-detail-reason text-red-500">
          {state.error}
        </p>
      )}
      {!state.loading && state.error === null && state.rows.length === 0 && (
        // Never a blank pane: an empty queue says so.
        <p data-operations-empty="true" className="text-sm text-muted">
          No operations yet
        </p>
      )}

      <ul className="flex flex-col gap-2">
        {state.view.map(row => {
          const expanded = expandedId === row.id;
          const detailId = `operations-row-${row.id}`;
          return (
            <li
              key={row.id}
              data-operations-row={row.id}
              className="rounded-md border border-panel p-2 flex flex-col gap-1"
            >
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span data-operations-workflow={row.workflow}>{row.workflowLabel}</span>
                <span className="font-mono" data-operations-target={row.target}>
                  {row.target}
                </span>
                <span data-operations-submitter={row.submitter}>by {row.submitter}</span>
                <span data-operations-status={row.status}>{row.statusLabel}</span>
                {row.started !== null && (
                  <span data-operations-started={row.started}>
                    {formatTimestamp(row.started)}
                  </span>
                )}
                {row.finished !== null && (
                  <span data-operations-finished={row.finished}>
                    {formatTimestamp(row.finished)}
                  </span>
                )}
                {row.undoState !== "none" && (
                  <span data-operations-undo-state={row.undoState}>{`undo ${row.undoState}`}</span>
                )}
                <button
                  type="button"
                  className="text-xs px-2 py-1 rounded-md border border-panel-strong"
                  data-operations-expand="true"
                  aria-expanded={expanded}
                  aria-controls={detailId}
                  onClick={() => setExpandedId(previous => (previous === row.id ? null : row.id))}
                >
                  {row.active ? "Resume" : "Details"}
                </button>
              </div>

              {expanded && (
                <div id={detailId} data-operations-detail="true" className="flex flex-col gap-1">
                  {/* Cancel and Undo are gated on the CONTRIBUTE capability, never
                      on a client table of per-workflow levels: a workflow the server
                      caps higher answers with its own 403 sentence, which is shown
                      verbatim - so nothing here has to change when a workflow's level
                      does. */}
                  {canContribute && isActiveStatus(row.status) && (
                    <div className="review-detail-actions">
                      <button
                        type="button"
                        className="text-xs px-2.5 py-1.5 rounded-md border border-panel-strong disabled:opacity-60"
                        data-operations-cancel="true"
                        disabled={busy}
                        onClick={() => void cancel(row)}
                      >
                        Cancel
                      </button>
                    </div>
                  )}
                  <OperationProgress
                    operation={row.operation}
                    // A resumed row has no plan - re-planning a finished operation
                    // may legitimately refuse - so the dialog narrates what the
                    // row itself carries.
                    plan={null}
                    orderedSteps={[...row.operation.steps].sort(
                      (left, right) => left.index - right.index,
                    )}
                    // The queue owns its own failure sentence, so the dialog's
                    // per-request ones are empty here.
                    pollFailure={null}
                    undoFailure={null}
                    busy={busy}
                    onUndo={() => void undo(row)}
                    canUndo={canContribute}
                    dataPrefix="operations"
                    ariaLabel={`${workflowLabel(row.workflow)} progress`}
                    stateLine={status => stateLine(row.workflow, status)}
                    stepLine={stepLine}
                    doneLine={(operation, plan) => doneLine(row.workflow, operation, plan)}
                    undoLabel="Undo this operation"
                    cancelledLine="This operation was cancelled."
                  />
                </div>
              )}
            </li>
          );
        })}
      </ul>

      {failure !== null && (
        <p
          role="alert"
          data-operations-failure="true"
          className="review-detail-reason text-red-500"
        >
          {failure}
        </p>
      )}

      {state.hasMore && (
        <button
          type="button"
          className="text-xs px-2.5 py-1.5 rounded-md border border-panel-strong self-start"
          data-operations-load-more="true"
          onClick={() => void state.loadMore()}
        >
          Load more
        </button>
      )}
    </section>
  );
}
