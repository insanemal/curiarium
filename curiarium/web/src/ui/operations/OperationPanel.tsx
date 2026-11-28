import React from "react";

// Imported from the domain module, not the `../../lib/api` barrel: several
// suites replace that barrel with a fixed object, so a named import from it
// would come back `undefined` there (`useBundleDetect.ts` documents the same
// rule).
import {
  listOperations,
  type Operation,
  type OperationReview as Review,
  type OperationUndo,
} from "../../lib/api/operations";
import { UnauthorizedError } from "../../lib/api/http";
import { tabbableIn } from "../../lib/focusTrap";
import OperationProgress from "./OperationProgress";
import OperationReview from "./OperationReview";
import { isTerminal, useOperationRun, type OperationRun } from "./useOperationRun";
import {
  actionLabel,
  doneLine,
  inFlightFor,
  stateLine,
  stepLine,
  workflowLabel,
} from "./operationView";

/**
 * One door for any workflow: the review the server computed, the ONE action that
 * submits it, and the progress popup that follows.
 *
 * The door supplies only its workflow, its target, its params and (optionally)
 * its own words; everything else is shared. `variant` covers the two shapes the
 * app already has: the review shown inline, or a trigger the operator presses
 * first. `render` lets a door add controls it owns (the archive entry picker).
 *
 * The in-flight lookup is a SERVER read on mount, so it holds across a reload: a
 * row the server already has for this exact (workflow, target) is adopted instead
 * of planning a second operation over the same paths.
 */

/** One workflow's seven sentences, so each door keeps its own words. */
export type OperationWords = {
  /** Why the plan could not be read. */
  planFailure: string;
  /** Why the submit was refused (a stale plan, work already queued). */
  submitFailure: string;
  /** Why one progress read failed; the row itself is unchanged. */
  pollFailure: string;
  /** Why the undo was refused. */
  undoFailure: string;
  /** The dialog's line for a cancelled run. */
  cancelledLine: string;
  /** The undo button's label. */
  undoLabel: string;
  /** The dialog's accessible name. */
  ariaLabel: string;
};

/** One workflow's default words, before a door overrides any of them. */
type WordShape = {
  /** The workflow as a noun phrase in the four failure sentences. */
  subject: string;
  cancelledLine: string;
  undoLabel: string;
  ariaLabel: string;
};

const WORD_SHAPES: Record<string, WordShape> = {
  "create-project": {
    subject: "the project",
    cancelledLine: "The create was cancelled.",
    undoLabel: "Undo this create",
    ariaLabel: "Create project progress",
  },
  "split-and-create": {
    subject: "the split",
    cancelledLine: "The split was cancelled.",
    undoLabel: "Undo this split",
    ariaLabel: "Split progress",
  },
  adopt: {
    subject: "the adopt",
    cancelledLine: "The adopt was cancelled.",
    undoLabel: "Undo this adopt",
    ariaLabel: "Adopt progress",
  },
  "extract-and-create": {
    subject: "the extraction",
    cancelledLine: "The extraction was cancelled.",
    undoLabel: "Undo this extraction",
    ariaLabel: "Extract and create progress",
  },
  extract: {
    subject: "the extraction",
    cancelledLine: "The extraction was cancelled.",
    undoLabel: "Undo this extraction",
    ariaLabel: "Extract progress",
  },
  "library-scan": {
    subject: "the scan",
    cancelledLine: "The scan was cancelled.",
    undoLabel: "Undo this scan",
    ariaLabel: "Scan progress",
  },
  "import-link": {
    subject: "the import",
    cancelledLine: "The import was cancelled.",
    undoLabel: "Undo this import",
    ariaLabel: "Import progress",
  },
  "import-zip": {
    subject: "the import",
    cancelledLine: "The import was cancelled.",
    undoLabel: "Undo this import",
    ariaLabel: "Import progress",
  },
  enrich: {
    subject: "the enrichment",
    cancelledLine: "The enrichment was cancelled.",
    undoLabel: "Undo this enrichment",
    ariaLabel: "Enrichment progress",
  },
};

/**
 * The default sentences for one workflow. They are the sentences its own panel
 * used before it moved to this door, so a door that re-points here must not
 * reword its failures. An unknown workflow still gets readable defaults, built
 * from its own key and never from a neighbour's noun.
 */
export function wordsFor(workflow: string): OperationWords {
  const shape = WORD_SHAPES[workflow];
  if (shape === undefined) {
    const label = workflowLabel(workflow);
    return {
      planFailure: `Failed to plan ${label}.`,
      submitFailure: `Failed to submit ${label}.`,
      pollFailure: "Failed to read the operation.",
      undoFailure: `Failed to undo ${label}.`,
      cancelledLine: `${label} was cancelled.`,
      undoLabel: "Undo this operation",
      ariaLabel: `${label} progress`,
    };
  }
  return {
    planFailure: `Failed to plan ${shape.subject}.`,
    submitFailure: `Failed to submit ${shape.subject}.`,
    pollFailure: "Failed to read the operation.",
    undoFailure: `Failed to undo ${shape.subject}.`,
    cancelledLine: shape.cancelledLine,
    undoLabel: shape.undoLabel,
    ariaLabel: shape.ariaLabel,
  };
}

/**
 * Whether the plan has nothing for the operator to approve, so it must be shown
 * with NO action: a zero-step plan is the server's own answer that there is
 * nothing left to do, and a button over it would promise work that will not
 * happen. Only the workflows whose review can say that are listed; the rest
 * always have an action.
 */
export function nothingToDo(workflow: string, review: Review): boolean {
  switch (workflow) {
    case "split-and-create":
      return review.projects.length === 0;
    case "adopt":
      return review.members === 0;
    case "extract-and-create":
      return review.creates === 0;
    default:
      return false;
  }
}

/** How many rows the in-flight lookup asks for: more than enough to find the one
 *  active row for a target, and bounded so a busy ledger stays one small read. */
const IN_FLIGHT_LIMIT = 20;

/** What the in-flight lookup answers, and whether it has answered at all. */
type InFlightLookup = {
  /** The one active row for this exact (workflow, target), or null. */
  operation: Operation | null;
  /** False until the read settled. The plan READ waits for it, so a resumed panel
   *  never asks the server to re-plan work it is already running. */
  settled: boolean;
};

/**
 * The operation the server already has in flight for this exact (workflow,
 * target), or null.
 *
 * One read on mount, evaluated through `inFlightFor`, so the answer survives a
 * reload rather than being a client guess. A failed lookup is NOT the door's
 * failure: the review outranks it, so nothing is shown and the operator can still
 * start the work. A 401 is the host's cue to sign the operator out.
 */
function useInFlightOperation(
  workflow: string,
  target: string,
  enabled: boolean,
  onUnauthorized?: () => void,
): InFlightLookup {
  const [lookup, setLookup] = React.useState<InFlightLookup>({
    operation: null,
    settled: false,
  });
  const unauthorizedRef = React.useRef(onUnauthorized);
  unauthorizedRef.current = onUnauthorized;

  React.useEffect(() => {
    if (!enabled) return;
    // A new workflow or target invalidates the old answer: adopting the previous
    // target's row would block this door over work that belongs to another one.
    setLookup({ operation: null, settled: false });
    let live = true;
    listOperations({ workflow, target, limit: IN_FLIGHT_LIMIT })
      .then(rows => {
        if (live) setLookup({ operation: inFlightFor(rows, workflow, target), settled: true });
      })
      .catch((err: unknown) => {
        if (!live) return;
        if (err instanceof UnauthorizedError) {
          unauthorizedRef.current?.();
          return;
        }
        setLookup({ operation: null, settled: true });
      });
    return () => {
      live = false;
    };
  }, [workflow, target, enabled]);

  return lookup;
}

type Props = {
  workflow: string;
  target: string;
  params?: Record<string, unknown>;
  canContribute: boolean;
  words?: Partial<OperationWords>;
  /** "always" shows the review inline (the default); "trigger" shows a button
   *  first and opens the review from it (the archive door's two-stage shape). */
  variant?: "always" | "trigger";
  /** The trigger button's label, for variant="trigger". */
  triggerLabel?: string;
  onChanged?: () => void;
  onUnauthorized?: () => void;
  /** Called once per operation that reaches `done`, with the row: the archive
   *  door maps the extract step's report out of it. */
  onResult?: (operation: Operation) => void;
  /** Extra controls the door owns, rendered under the review (the entry picker). */
  render?: (run: OperationRun) => React.ReactNode;
  /** The door's attribute namespace: "split" yields data-split-* etc. */
  dataPrefix?: string;
  /** Fired after a CONFIRMED undo, with the server's answer, so the door can
   *  announce what was folded back. `undone: false` is not a change and does not
   *  fire. */
  onUndo?: (answer: OperationUndo) => void;
  /** Every control refused at every stage (a caller has another write in flight on
   *  the same archive). The review is still shown; the action and trigger are
   *  disabled. */
  disabled?: boolean;
  /** The door's own class names for the trigger button, so the archive door keeps
   *  its compact row look beside its full detail look. */
  triggerClassName?: string;
  /** The row as it changes, so a host that keys its own mount on "is something on
   *  screen" can follow it. */
  onOperation?: (operation: Operation | null) => void;
};

const PRIMARY_BUTTON = "text-xs px-2.5 py-1.5 rounded-md bg-accent disabled:opacity-60";

/** What this panel can Tab through. Deliberately not `DRAWER_TABBABLE_SELECTOR`:
 *  that one also carries `summary` and `a[href]`, which this panel never renders,
 *  so a Tab could wrap onto a member that does not exist. */
const PANEL_TABBABLE_SELECTOR =
  'button:not([tabindex="-1"]), input:not([tabindex="-1"]), select:not([tabindex="-1"]), [role="listbox"]';

export default function OperationPanel(props: Props): React.ReactElement | null {
  const {
    workflow,
    target,
    params,
    canContribute,
    words,
    variant = "always",
    triggerLabel,
    onChanged,
    onUnauthorized,
    onResult,
    render,
    dataPrefix,
    onUndo,
    disabled = false,
    triggerClassName,
    onOperation,
  } = props;

  const copy: OperationWords = { ...wordsFor(workflow), ...words };
  const prefix = dataPrefix ?? "operation";
  const [revealed, setRevealed] = React.useState(false);
  const containerRef = React.useRef<HTMLDivElement | null>(null);

  // Every hook runs before the viewer gate, so the hook order never changes. The
  // lookup and the plan are two independent READS issued on mount: the plan must
  // not wait for the lookup, because a lookup that is slow (or that the host's
  // test harness answers late) would leave the operator staring at nothing while
  // a READ they already paid for was held back. The lookup only decides what the
  // runner ADOPTS, and until it answers the review is simply the honest shape.
  const lookup = useInFlightOperation(workflow, target, canContribute, onUnauthorized);
  const inFlight = lookup.operation;
  const run = useOperationRun({
    workflow,
    target,
    params,
    enabled: canContribute,
    adopt: inFlight,
    onChanged,
    onUnauthorized,
    planFailureFallback: copy.planFailure,
    submitFailureFallback: copy.submitFailure,
    pollFailureFallback: copy.pollFailure,
    undoFailureFallback: copy.undoFailure,
  });

  const { plan, planFailure, submitFailure, operation, busy, orderedSteps, pollFailure, undoFailure } =
    run;

  // The host hears about the row as it changes (null when nothing is on screen),
  // so a host that keys its own mount on "is something running" can follow the
  // same identity the panel is narrating.
  const onOperationRef = React.useRef(onOperation);
  onOperationRef.current = onOperation;
  React.useEffect(() => {
    onOperationRef.current?.(operation);
  }, [operation]);

  // The host hears about a completed operation once per ID: the archive door maps
  // the extract step's report out of the row, and a re-poll of the same row must
  // not map it twice.
  const reportedRef = React.useRef<string | null>(null);
  const onResultRef = React.useRef(onResult);
  onResultRef.current = onResult;
  React.useEffect(() => {
    if (operation === null || operation.status !== "done") return;
    if (reportedRef.current === operation.id) return;
    reportedRef.current = operation.id;
    onResultRef.current?.(operation);
  }, [operation]);

  const stage =
    operation !== null
      ? `progress:${operation.status}`
      : plan !== null
        ? busy
          ? "submitting"
          : "review"
        : revealed
          ? "planning"
          : "trigger";
  const previousStageRef = React.useRef(stage);
  React.useEffect(() => {
    if (previousStageRef.current === stage) return;
    previousStageRef.current = stage;
    const container = containerRef.current;
    if (container === null) return;
    const active = document.activeElement;
    // A live control the operator is using keeps the caret: only a caret the stage
    // change orphaned moves on to the new stage - one that fell to the document, or
    // one sitting on the panel's own placeholder home, or one left on a control the
    // new stage DISABLED (a browser blurs that to <body>, where neither a host's
    // Escape handler nor the dialog's trap can reach it; jsdom leaves it focused,
    // so the case has to be named rather than inferred from the document).
    const orphaned =
      active === null ||
      active === document.body ||
      active === container ||
      (active instanceof HTMLElement && active.matches(":disabled"));
    if (!orphaned) return;
    // The stage's ONE action is its home when it has one: a door's own controls
    // (the entry picker) sit BEFORE the action in document order, and landing on
    // one of those would make the operator walk past the button the stage exists
    // for.
    const action = container.querySelector<HTMLElement>(
      `[data-${prefix}-action-button]:not([disabled])`,
    );
    (action ?? tabbableIn(container, PANEL_TABBABLE_SELECTOR)[0] ?? container).focus();
  }, [stage, prefix]);

  // Every control here writes into the library, so a role below contributor gets
  // no control at all. The early return sits after every hook.
  if (!canContribute) return null;

  const flag = (name: string): Record<string, string> => ({ [`data-${prefix}-${name}`]: "true" });

  // `useOperationRun.undo` already reports a CONFIRMED fold to `onChanged`
  // (`undone: false` means the server changed nothing), so this only forwards the
  // click: calling the host again here would refetch the library twice per undo.
  async function undo(): Promise<void> {
    const answer = await run.undo();
    // `undone: false` is not a change, so it is not news for the door.
    if (answer !== null && answer.undone) onUndo?.(answer);
  }

  // The stage's own focus home, so a door that swaps one control for another
  // never drops the caret on <body>: a host's Escape handler lives on an ancestor,
  // and a browser (and jsdom) blurs a removed control to the document, which is
  // outside every handler the host installed. Only a caret that the removal
  // actually orphaned is moved - one the operator parked inside the panel stays
  // where it is, and one the host owns is left alone.
  const extra = render?.(run) ?? null;
  // Every control the panel offers is refused while a request is in flight, and
  // while the caller says another write is already running on this target.
  const controlsOff = busy || disabled;
  // A plan with nothing to approve gets no button: the review still states why.
  const label =
    plan === null
      ? null
      : nothingToDo(workflow, plan.review)
        ? null
        : actionLabel(workflow, plan.review, params);

  // A trigger door's control comes back once the run has ENDED, beside the
  // narration: the operator may want a second extract of the same archive, and a
  // door that offered its control exactly once would have lost the capability the
  // old stage machine kept. A one-per-mount idempotency key makes a replay answer
  // the operation this mount already queued.
  const ended = operation !== null && isTerminal(operation.status);
  const showTrigger =
    variant === "trigger" &&
    inFlight === null &&
    ((!revealed && operation === null) || ended);
  const showReview =
    inFlight === null &&
    operation === null &&
    plan !== null &&
    (variant === "always" || revealed);

  return (
    <div ref={containerRef} tabIndex={-1} className="flex flex-col gap-2">
      {planFailure !== null && (
        <span className="review-detail-reason text-red-500" role="alert" {...flag("plan-failure")}>
          {planFailure}
        </span>
      )}
      {submitFailure !== null && (
        <span
          className="review-detail-reason text-red-500"
          role="alert"
          {...flag("submit-failure")}
        >
          {submitFailure}
        </span>
      )}

      {/* A row for this exact target is already in flight: it is shown INSTEAD of
          the review, and no confirm is offered - a second submit would queue a
          second operation over the same paths. */}
      {inFlight !== null && (
        <p className="text-sm" {...flag("in-flight")}>
          {`An operation for this target is already in flight (submitted by ${inFlight.submitted_by}) - showing it instead of starting a second one.`}
        </p>
      )}

      {showTrigger && (
        <button
          type="button"
          className={triggerClassName ?? PRIMARY_BUTTON}
          {...flag("trigger")}
          disabled={controlsOff}
          onClick={() => {
            setRevealed(true);
            // A finished run's trigger puts the door back on its review; a fresh
            // one only opens it.
            if (ended) run.reset();
          }}
        >
          {triggerLabel ?? workflowLabel(workflow)}
        </button>
      )}

      {/* The plan is a READ the click does not wait for: between the press and the
          answer the door shows that it heard, instead of nothing at all. */}
      {variant === "trigger" &&
        revealed &&
        plan === null &&
        planFailure === null &&
        operation === null &&
        inFlight === null && (
          <button type="button" className={triggerClassName ?? PRIMARY_BUTTON} disabled {...flag("planning")}>
            Planning...
          </button>
        )}

      {/* A submit is a request the review's button already disabled: the status
          line is what says the press was heard before the row exists. */}
      {busy && operation === null && plan !== null && (
        <p role="status" className="text-sm text-muted" {...flag("submitting")}>
          Submitting...
        </p>
      )}

      {!showTrigger && showReview && plan !== null && (
        <OperationReview
          plan={plan}
          actionLabel={label}
          busy={controlsOff}
          onConfirm={() => void run.submit()}
          dataPrefix={prefix}
        >
          {extra}
        </OperationReview>
      )}

      {operation !== null && (
        <OperationProgress
          operation={operation}
          plan={plan}
          orderedSteps={orderedSteps}
          pollFailure={pollFailure}
          undoFailure={undoFailure}
          busy={busy}
          onUndo={() => void undo()}
          canUndo
          dataPrefix={prefix}
          ariaLabel={copy.ariaLabel}
          stateLine={status => stateLine(workflow, status)}
          stepLine={stepLine}
          doneLine={(operation, plan) => doneLine(workflow, operation, plan)}
          cancelledLine={copy.cancelledLine}
          undoLabel={copy.undoLabel}
        />
      )}

      {/* Once the review is gone, the door's controls sit beside the popup so a
          selection can still be changed mid-run. */}
      {operation !== null && extra}
    </div>
  );
}
