import React from "react";

// Imported from the domain module, not the `../../lib/api` barrel: several
// suites replace that barrel with a fixed object, so a named import from it
// would come back `undefined` there (`useBundleDetect.ts` documents the same
// rule).
import {
  getOperation,
  planOperation,
  splitActionLabel,
  submitOperation,
  undoOperation,
  type Operation,
  type OperationPlan,
  type OperationUndo,
} from "../../lib/api/operations";
import { UnauthorizedError } from "../../lib/api/http";
import { notifyInfo } from "../../lib/toast";
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

const POLL_MS = 250;

const PRIMARY_BUTTON = "text-xs px-2.5 py-1.5 rounded-md bg-accent disabled:opacity-60";
const SECONDARY_BUTTON =
  "text-xs px-2.5 py-1.5 rounded-md border border-panel-strong disabled:opacity-60";

/** The three statuses a runner never leaves: the poll stops on one of them. */
function isTerminal(status: string): boolean {
  return status === "done" || status === "failed" || status === "cancelled";
}

/** The thrown message, or a neutral fallback when the throw was not an Error.
 *  The API layer already turns the server's `detail` into `Error.message`, so
 *  this is the one place a failure sentence is chosen - and the client
 *  paraphrases nothing it did not decide. */
function failureDetail(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

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

/**
 * The split door, whole: the plan the server computed, ONE action labelled with
 * its outcome, and a popup that narrates the operation the action queued.
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

  const [plan, setPlan] = React.useState<OperationPlan | null>(null);
  const [planFailure, setPlanFailure] = React.useState<string | null>(null);
  const [submitFailure, setSubmitFailure] = React.useState<string | null>(null);
  const [pollFailure, setPollFailure] = React.useState<string | null>(null);
  const [undoFailure, setUndoFailure] = React.useState<string | null>(null);
  const [operation, setOperation] = React.useState<Operation | null>(null);
  const [busy, setBusy] = React.useState(false);

  // The host may hand this panel a fresh callback identity every render, so both
  // callbacks are read through refs: the plan is keyed by the bundle and the
  // shape alone, and a READ must not run again because a parent re-rendered.
  const unauthorizedRef = React.useRef(onUnauthorized);
  unauthorizedRef.current = onUnauthorized;
  const changedRef = React.useRef(onChanged);
  changedRef.current = onChanged;

  // ONE idempotency key per mount, minted on the first render and never again: a
  // second mint would let a double-click queue a second split, while a replay of
  // this key answers the operation the first click already queued.
  const idempotencyKey = React.useRef<string>();
  if (!idempotencyKey.current) idempotencyKey.current = crypto.randomUUID();
  const submitKey = idempotencyKey.current;

  // A plan is a READ: fetch it for the shape on offer, and again whenever the
  // operator's choice changes. A viewer issues no request at all.
  React.useEffect(() => {
    if (!canContribute) return;
    let live = true;
    setPlan(null);
    setPlanFailure(null);
    planOperation({
      workflow: "split-and-create",
      target: bundleKey,
      params: shape ? { shape } : {},
    })
      .then(next => {
        if (live) setPlan(next);
      })
      .catch((err: unknown) => {
        if (!live) return;
        // A 401 is the host's cue to sign the operator out, so the panel writes
        // no sentence of its own: a generic failure would strand them on a
        // session that is already gone.
        if (err instanceof UnauthorizedError) {
          unauthorizedRef.current?.();
          return;
        }
        setPlanFailure(failureDetail(err, "Failed to plan the split."));
      });
    return () => {
      live = false;
    };
  }, [bundleKey, shape, canContribute]);

  // Progress is a POLL, because the runner owns the row: 250 ms is fast enough
  // to feel live and slow enough that a long split is a handful of reads. The
  // timer is cleared on every re-arm and on unmount, and a terminal status arms
  // no timer at all.
  React.useEffect(() => {
    if (operation === null || isTerminal(operation.status)) return;
    const id = operation.id;
    const timer = setTimeout(() => {
      getOperation(id)
        .then(next => setOperation(next))
        .catch((err: unknown) => {
          if (err instanceof UnauthorizedError) {
            unauthorizedRef.current?.();
            return;
          }
          // The row is the server's record, so its sentence is the one to show -
          // and no further poll is armed, since one failed read of a dead ledger
          // would only repeat itself.
          setPollFailure(failureDetail(err, "Failed to read the operation."));
        });
    }, POLL_MS);
    return () => clearTimeout(timer);
  }, [operation]);

  // The host hears about a finished operation exactly once: a re-poll of the same
  // terminal row must not refetch the host's report a second time.
  const changedReported = React.useRef(false);
  const terminal = operation !== null && isTerminal(operation.status);
  React.useEffect(() => {
    if (!terminal || changedReported.current) return;
    changedReported.current = true;
    changedRef.current?.();
  }, [terminal]);

  // Every hook runs before this gate, so the early return breaks no hook order;
  // the effects each check the gate themselves because they are still armed.
  if (!canContribute) return null;

  async function submit(current: OperationPlan): Promise<void> {
    setBusy(true);
    setSubmitFailure(null);
    try {
      setOperation(
        await submitOperation({ planId: current.plan_id, idempotencyKey: submitKey }),
      );
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        unauthorizedRef.current?.();
        return;
      }
      // The server's own sentence already says why it refused (a stale plan, an
      // operation over those paths already queued), so it is shown verbatim.
      setSubmitFailure(failureDetail(err, "Failed to submit the split."));
    } finally {
      setBusy(false);
    }
  }

  function announceUndo(answer: OperationUndo): void {
    const removed = answer.projects_removed ?? [];
    notifyInfo("Split undone", removed.length > 0 ? { details: removed } : undefined);
  }

  async function undo(current: Operation): Promise<void> {
    setBusy(true);
    setUndoFailure(null);
    try {
      const answer = await undoOperation(current.id);
      // The answer IS the row's new state, so the button goes without another
      // read: `undone: false` means the server changed nothing, and the button
      // has to stay for the operator who will try again.
      setOperation(previous =>
        previous === null || !answer.undone
          ? previous
          : { ...previous, undo_state: "undone" },
      );
      announceUndo(answer);
      changedRef.current?.();
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        unauthorizedRef.current?.();
        return;
      }
      // The button stays mounted under the sentence: a dead button would swallow
      // the server's answer and invite the same refusal forever.
      setUndoFailure(failureDetail(err, "Failed to undo the split."));
    } finally {
      setBusy(false);
    }
  }

  // The popup reads the steps in the order the runner indexed them, whatever
  // order the payload happened to arrive in.
  const orderedSteps =
    operation === null ? [] : [...operation.steps].sort((left, right) => left.index - right.index);

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
            onClick={() => void submit(plan)}
          >
            {splitActionLabel(plan.review)}
          </button>
        </div>
      )}
      {operation !== null && plan !== null && (
        // Progress, in place - not a toast and not a modal the operator must
        // dismiss. The dialog narrates the runner's own steps in index order.
        <div role="dialog" aria-label="Split progress" data-split-dialog="true">
          {!isTerminal(operation.status) && (
            <span data-split-state="true">{stateLine(operation.status)}</span>
          )}
          {pollFailure !== null && (
            <span
              className="review-detail-reason text-red-500"
              role="alert"
              data-split-poll-failure="true"
            >
              {pollFailure}
            </span>
          )}
          <ul className="flex flex-col gap-1">
            {orderedSteps.map(step => (
              <li
                key={step.index}
                data-split-step="true"
                data-split-op={step.op}
                data-split-status={step.status}
              >
                {operationStatusLine(step)}
              </li>
            ))}
          </ul>
          {operation.status === "done" && (
            <span data-split-result="true">{doneLine(operation, plan)}</span>
          )}
          {operation.status === "failed" && operation.error !== null && (
            <span className="review-detail-reason text-red-500" role="alert" data-split-error="true">
              {operation.error}
            </span>
          )}
          {operation.status === "cancelled" && (
            <span data-split-cancelled="true">The split was cancelled.</span>
          )}
          {operation.undo_state === "available" && (
            <div className="review-detail-actions">
              <button
                type="button"
                className={SECONDARY_BUTTON}
                data-split-undo="true"
                disabled={busy}
                onClick={() => void undo(operation)}
              >
                Undo this split
              </button>
            </div>
          )}
          {undoFailure !== null && (
            <span
              className="review-detail-reason text-red-500"
              role="alert"
              data-split-undo-failure="true"
            >
              {undoFailure}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
