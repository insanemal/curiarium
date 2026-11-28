import { useEffect, useRef, useState, type ReactElement } from "react";
// Domain modules, never the `../../lib/api` barrel: several suites replace that
// barrel with a fixed object, so a named import from it would come back
// `undefined` there (`ui/review/useBundleDetect.ts` documents the same rule).
import type { ArchiveExtractResult } from "../../lib/api/imports";
import type { Operation } from "../../lib/api/operations";
import { tabbableIn } from "../../lib/focusTrap";
import { notifyInfo } from "../../lib/toast";
import OperationProgress from "../operations/OperationProgress";
import { isTerminal, useOperationRun } from "../operations/useOperationRun";
// The narration of the ledger's own steps, shared with the extract-and-create
// panel: one `register` step must read as the same sentence in both doors.
import {
  excludedLine,
  extractGroupLine,
  extractUndoLine,
  operationStatusLine,
} from "../review/bundleRows";

type Props = {
  /** Contribute capability: extraction writes new bytes into the library. Required. */
  canContribute: boolean;
  /** Library-relative path of the archive, e.g. "Moai.zip". */
  path: string;
  /** Human label for the accessible names, e.g. "Moai.zip". */
  label: string;
  /** "detail" for the asset preview footer, "row" for a compact settings-card row. */
  variant?: "detail" | "row";
  /**
   * Refuse every control this instance renders, at every stage: the caller has
   * another write in flight on the same archive and this one would race it.
   */
  disabled?: boolean;
  /** Fires after a successful extract so the caller can refresh what it shows. */
  onExtracted?: (result: ArchiveExtractResult) => void;
};

/**
 * `planning` waits for the plan (a READ the server answers on its own), `confirm`
 * and `extracting` share the confirmation panel, and `done` puts the trigger back
 * beside the operation's own narration.
 */
type Stage = "idle" | "planning" | "confirm" | "extracting" | "done" | "error";

const DETAIL_BUTTON = "px-3 py-2 rounded-md border border-panel-strong text-sm disabled:opacity-60";
// A compact bordered row button, for a dense list row or a settings card.
const ROW_BUTTON =
  "text-xs px-2 py-1 rounded-md border border-panel-strong self-start disabled:opacity-60";

/**
 * What this action can Tab through: only buttons, so `DRAWER_TABBABLE_SELECTOR`
 * (which also carries `summary` and `a[href]`) would let a Tab wrap onto a
 * member this control never renders.
 */
const ACTION_TABBABLE_SELECTOR = 'button:not([tabindex="-1"])';

/**
 * The stage's one focus home: its first enabled control, or the container
 * itself while every control in the stage is disabled (`planning`,
 * `extracting`) - the same fallback `ZipImportModal` uses for its overlay.
 * A disabled control is not a home: it refuses focus, and a browser has
 * already blurred it.
 */
function focusActionHome(container: HTMLDivElement): void {
  (tabbableIn(container, ACTION_TABBABLE_SELECTOR)[0] ?? container).focus();
}

function countLabel(count: number): string {
  return count === 1 ? "1 file" : `${count} files`;
}

/**
 * The extract step's own report, re-assembled into the shape the client already
 * models. The server writes exactly these six fields there (it is the
 * `ArchiveExtractResult` the deleted route used to answer), so this only guards
 * the reader against a step that has not reported yet: null means no report, not
 * a zero.
 */
function extractResult(operation: Operation): ArchiveExtractResult | null {
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

/**
 * What the extraction came to, read off the extract step's RESULT rather than
 * guessed from the status. Two different nothings (a destination that was
 * already there, and an archive whose entries all turned out to be junk), and one
 * completed write - which says "registered", not "a scan has started": the
 * `register` step after it wrote the rows in-process, and no walk is queued.
 */
function doneLine(operation: Operation): string {
  const result = extractResult(operation);
  if (result === null) return "The extraction finished without a report.";
  if (result.already_present) return "Already extracted - nothing to do.";
  if (result.files === 0) return "Nothing was written - the archive held no extractable files.";
  return `Extracted ${countLabel(result.files)}. The files are registered.`;
}

/** The progress dialog's line about where the runner is, for the statuses that
 *  are not an outcome. The server's own token is the only thing that says this,
 *  so the sentence names it rather than inventing a stage the client cannot see. */
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

/**
 * Extracts a `.zip` that is already in the library, beside itself: the `extract`
 * operation, whole.
 *
 * The listing this control used to read is gone with its route: the plan the
 * server computes IS the review, so the confirm stage renders what the server
 * measured - how many members, which groups they fall into, and every member it
 * would leave out with its own reason - and the write is that plan's `plan_id`
 * handed back. The state machine lives in `ui/operations/useOperationRun` and the
 * dialog in `ui/operations/OperationProgress`, so this file is the door's WORDS
 * and its stages, not a third implementation.
 *
 * The server enforces the capability (`require_contribute`); hiding the control
 * below contributor is UX, not security.
 */
export default function ArchiveExtractAction({
  canContribute,
  path,
  label,
  variant = "detail",
  disabled = false,
  onExtracted,
}: Props): ReactElement | null {
  const [stage, setStage] = useState<Stage>("idle");
  const [error, setError] = useState("");

  const containerRef = useRef<HTMLDivElement | null>(null);
  const previousStageRef = useRef<Stage | null>(null);

  const run = useOperationRun({
    workflow: "extract",
    target: path,
    params: {},
    enabled: canContribute,
    planFailureFallback: `Could not plan the extraction of ${path}.`,
    submitFailureFallback: `Could not submit the extraction of ${path}.`,
    pollFailureFallback: `Could not read the extraction of ${path}.`,
    undoFailureFallback: `Could not undo the extraction of ${path}.`,
  });
  const { plan, planFailure, submitFailure, operation } = run;

  const button = variant === "row" ? ROW_BUTTON : DETAIL_BUTTON;
  const primaryButton = `${button} bg-accent hover:bg-accent-strong`;

  function open(): void {
    setError("");
    // The plan is a READ that the hook already issued: it is normally in hand
    // before the operator clicks, and a click that beats it waits in `planning`.
    if (plan !== null) {
      setStage("confirm");
      return;
    }
    if (planFailure !== null) {
      setError(planFailure);
      setStage("error");
      return;
    }
    setStage("planning");
  }

  async function confirmExtract(): Promise<void> {
    setStage("extracting");
    // The plan IS the argument: submit only sends its `plan_id` back, and the
    // hook refuses to submit without one. The mount's ONE idempotency key rides
    // along, so a second press from the finished stage replays the operation the
    // first one queued instead of writing a second extraction.
    await run.submit();
  }

  function cancel(): void {
    setStage("idle");
  }

  // `planning` is a wait, not a stage the operator chose: the plan lands and the
  // confirm opens, or the server's refusal becomes the error.
  useEffect(() => {
    if (stage !== "planning") return;
    if (plan !== null) {
      setStage("confirm");
      return;
    }
    if (planFailure !== null) {
      setError(planFailure);
      setStage("error");
    }
  }, [stage, plan, planFailure]);

  // A refused submit queued nothing, so this is the error stage and not a
  // narration of a run: the server's own sentence is what the operator reads.
  useEffect(() => {
    if (stage !== "extracting" || submitFailure === null) return;
    setError(submitFailure);
    setStage("error");
  }, [stage, submitFailure]);

  // The row is the record: once it stops moving, the run is over.
  useEffect(() => {
    if (stage !== "extracting" || operation === null) return;
    if (!isTerminal(operation.status)) return;
    setStage("done");
  }, [stage, operation]);

  // The host's completion signal, once per operation: the mapped step result is
  // exactly what the deleted route answered, so its refresh keeps working. The
  // latest callback is read through a ref, because a host that hands a fresh
  // identity every render must not turn one operation into two reports.
  const reportedRef = useRef<string | null>(null);
  const onExtractedRef = useRef(onExtracted);
  onExtractedRef.current = onExtracted;
  useEffect(() => {
    if (operation === null || operation.status !== "done") return;
    if (reportedRef.current === operation.id) return;
    reportedRef.current = operation.id;
    const result = extractResult(operation);
    if (result !== null) onExtractedRef.current?.(result);
  }, [operation]);

  // Recovery: every stage advance swaps this control's own controls, so the
  // button that was just pressed leaves the DOM - or is still there with
  // `disabled` on it, which a browser blurs and jsdom does not - and focus
  // drops to <body>. <body> is outside the enclosing dialog's Tab trap and out
  // of reach of its Escape handler, so re-enter on the new stage's first real
  // control. A caret the user moved themselves, anywhere in the document, is
  // left alone; the initial mount is skipped because this action also mounts in
  // surfaces that own no focus of their own (a bulk-dialog batch remount, an
  // aside), where grabbing it would yank a caret the user never lost.
  useEffect(() => {
    const previous = previousStageRef.current;
    previousStageRef.current = stage;
    if (previous === null || previous === stage) return;
    const container = containerRef.current;
    if (!container) return; // Below contributor nothing at all is rendered.
    const active = document.activeElement;
    const holdsLiveCaret =
      active instanceof HTMLElement &&
      active !== document.body &&
      // Our own container is only ever this effect's busy home, never a caret
      // a user chose: leaving focus there would lose confirm -> error.
      active !== container &&
      active.isConnected &&
      !active.hasAttribute("disabled");
    if (holdsLiveCaret) return;
    focusActionHome(container);
  }, [stage]);

  async function undo(): Promise<void> {
    const answer = await run.undo();
    // The extracted files deliberately stay (the server documents the residue),
    // so the news is the undo's own answer rather than a claim of erasure.
    if (answer !== null) notifyInfo("Extract undone", { details: [extractUndoLine(answer)] });
  }

  // Every control here writes into the library, so a role below contributor gets
  // no control at all rather than a disabled button advertising a capability it
  // lacks. The early return sits after every hook so the hook order never changes.
  if (!canContribute) return null;

  const review = plan?.review ?? null;
  const extracting = stage === "extracting";

  // A progress dialog is one component: the door supplies its namespace and its
  // words, never a second copy of the markup.
  const progress =
    operation !== null && plan !== null ? (
      <div role="status" aria-live="polite">
        <OperationProgress
          operation={operation}
          plan={plan}
          orderedSteps={run.orderedSteps}
          pollFailure={run.pollFailure}
          undoFailure={run.undoFailure}
          busy={run.busy}
          onUndo={() => void undo()}
          dataPrefix="extract"
          ariaLabel="Extract progress"
          stateLine={stateLine}
          stepLine={operationStatusLine}
          doneLine={doneLine}
          cancelledLine="The extraction was cancelled."
          undoLabel="Undo this extraction"
        />
      </div>
    ) : null;

  if ((stage === "confirm" || stage === "extracting") && review !== null) {
    return (
      // tabindex="-1": programmatically focusable, never in a trap's Tab cycle.
      <div className="flex flex-col gap-2" ref={containerRef} tabIndex={-1}>
        {/* What the plan measured, before the operator approves it: the member
            count (the listing's replacement), then the groups it fell into. */}
        <p className="text-sm text-muted" data-extract-summary="true">
          {`${countLabel(review.members)} — extract into a new folder beside the archive?`}
        </p>
        <ul className="flex flex-col gap-1">
          {review.groups.map(group => (
            <li
              key={group.name}
              className="text-sm text-muted"
              data-extract-group="true"
              data-extract-group-name={group.name}
            >
              {extractGroupLine(group)}
            </li>
          ))}
        </ul>
        {/* Every member the plan would leave out, with the server's own reason
            beside it verbatim: this is the capability the deleted listing had, and
            a junk member is still visible here - excluded, and said why. */}
        {review.excluded.map(entry => (
          <p
            key={entry.path}
            className="review-detail-reason text-sm text-muted"
            data-extract-excluded="true"
          >
            {excludedLine(entry)}
          </p>
        ))}
        <div className="flex gap-2">
          <button
            type="button"
            className={primaryButton}
            disabled={disabled || extracting}
            onClick={() => void confirmExtract()}
          >
            {extracting ? "Extracting..." : `Extract ${countLabel(review.members)}`}
          </button>
          <button
            type="button"
            className={button}
            disabled={disabled || extracting}
            onClick={cancel}
          >
            Cancel
          </button>
        </div>
        {progress}
      </div>
    );
  }

  return (
    // tabindex="-1": programmatically focusable, never in a trap's Tab cycle.
    <div className="flex flex-col gap-1" ref={containerRef} tabIndex={-1}>
      <button
        type="button"
        aria-label={`Extract ${label}`}
        className={primaryButton}
        disabled={disabled || stage === "planning"}
        onClick={open}
      >
        {stage === "planning" ? "Planning..." : "Extract archive"}
      </button>
      {stage === "error" && <p className="text-sm text-red-400">{error}</p>}
      {progress}
    </div>
  );
}
