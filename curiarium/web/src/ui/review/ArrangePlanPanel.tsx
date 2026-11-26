import React from "react";

// Imported from the domain module, not the `../../lib/api` barrel: several
// suites replace that barrel with a fixed object, so a named import from it
// would come back `undefined` there (`useBundleDetect.ts` documents the same
// rule).
import {
  arrangeBundles,
  getArrangePlan,
  undoArrange,
  type BundleArrangeApplied,
  type BundleArrangeMove,
  type BundleArrangePlan,
} from "../../lib/api/bundles";
import { UnauthorizedError } from "../../lib/api/http";
import { notifyInfo } from "../../lib/toast";
import {
  arrangeActionLabel,
  arrangeMoveCount,
  arrangeMoveCountLabel,
  arrangeMoveLine,
  arrangeRefusalLine,
} from "./bundleRows";

type Props = {
  /** The storage-relative key the plan is measured against. */
  bundleKey: string;
  /** The shape whose moves this door would make. */
  shape: string;
  /** The write gate: a viewer renders nothing here and issues no request. */
  canContribute: boolean;
  /** The host's completion signal: refetch whatever the host owns. */
  onChanged?: () => void;
  /** Session expiry is the host's news, not this panel's. */
  onUnauthorized?: () => void;
};

type Failure = { at: "preview" | "apply" | "undo"; detail: string };

type Applied = {
  arrangeId: string;
  moves: number;
  unseeable: string[];
  stale: string[];
  alreadyApplied: boolean;
};

const PRIMARY_BUTTON = "text-xs px-2.5 py-1.5 rounded-md bg-accent disabled:opacity-60";
const SECONDARY_BUTTON =
  "text-xs px-2.5 py-1.5 rounded-md border border-panel-strong disabled:opacity-60";

/** The thrown message, or a neutral fallback when the throw was not an Error.
 *  The API layer already turns the server's `detail` into `Error.message`, so
 *  this is the one place a failure sentence is chosen - and the client
 *  paraphrases nothing it did not decide. */
function failureDetail(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

/**
 * The arrange door, whole: preview, apply and undo in one self-contained
 * component mounted from two screens (the review queue's detail pane and a
 * project's drift notice). It owns its own request state rather than reaching
 * into a hook, so neither host has to thread an arrange action down to it.
 *
 * Nothing is fetched on mount - a preview is a disk walk, and the click is the
 * only trigger. A viewer renders nothing at all: no wrapper, no layout track,
 * no request.
 */
export default function ArrangePlanPanel({
  bundleKey,
  shape,
  canContribute,
  onChanged,
  onUnauthorized,
}: Props): React.ReactElement | null {
  const [busy, setBusy] = React.useState(false);
  const [plan, setPlan] = React.useState<BundleArrangePlan | null>(null);
  const [applied, setApplied] = React.useState<Applied | null>(null);
  // One failure slot for the three steps: `at` names which sentence is on
  // screen, and every message is the server's own.
  const [failure, setFailure] = React.useState<Failure | null>(null);

  if (!canContribute) return null;

  async function preview(): Promise<void> {
    setBusy(true);
    try {
      const next = await getArrangePlan(bundleKey, shape);
      setPlan(next);
      setFailure(null);
    } catch (err) {
      // A 401 is the host's cue to sign the operator out, so the panel writes no
      // sentence of its own: a generic failure would strand them on a session
      // that is already gone.
      if (err instanceof UnauthorizedError) {
        onUnauthorized?.();
        return;
      }
      setFailure({
        at: "preview",
        detail: failureDetail(err, "Failed to preview the arrangement."),
      });
    } finally {
      setBusy(false);
    }
    // A preview writes nothing, so it refetches nothing: the host's report is
    // the one already on screen.
  }

  function announceApplied(
    result: BundleArrangeApplied,
    previewedMoves: BundleArrangeMove[],
  ): void {
    if (result.already_applied) {
      notifyInfo("This arrangement had already been applied");
      return;
    }
    // The details name the moves only when the landed count matches the
    // previewed ones one for one; otherwise the toast would list lines that did
    // not all happen.
    const details =
      result.moves === previewedMoves.length ? previewedMoves.map(arrangeMoveLine) : undefined;
    notifyInfo(`Arranged: ${arrangeMoveCountLabel(result.moves)}`, details ? { details } : undefined);
  }

  async function apply(current: BundleArrangePlan): Promise<void> {
    setBusy(true);
    try {
      const result = await arrangeBundles({
        key: bundleKey,
        shape: current.shape,
        planId: current.plan_id,
      });
      // The apply consumes the plan: its `plan_id` names a tree that has just
      // moved, so the preview must not stay on screen as if it were still the
      // offer.
      const previewedMoves = current.moves ?? [];
      setPlan(null);
      setFailure(null);
      setApplied({
        arrangeId: result.arrange_id,
        moves: result.moves,
        unseeable: result.unseeable,
        stale: result.stale,
        alreadyApplied: result.already_applied,
      });
      announceApplied(result, previewedMoves);
      onChanged?.();
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized?.();
        return;
      }
      // The server's sentence already says the tree changed and to preview
      // again, so it is rendered verbatim and the preview is offered again
      // beneath it - no second sentence saying the same thing.
      setPlan(null);
      setFailure({ at: "apply", detail: failureDetail(err, "Failed to arrange the bundle.") });
    } finally {
      setBusy(false);
    }
  }

  function cancel(): void {
    setPlan(null);
    setFailure(null);
  }

  async function undo(current: Applied): Promise<void> {
    setBusy(true);
    try {
      const result = await undoArrange(current.arrangeId);
      setApplied(null);
      setFailure(null);
      notifyInfo(`Put back: ${arrangeMoveCountLabel(result.moves)}`);
      onChanged?.();
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized?.();
        return;
      }
      // The applied group stays so the undo sentence can stand where its button
      // was: a dead button would swallow the server's answer.
      setFailure({ at: "undo", detail: failureDetail(err, "Failed to undo the arrangement.") });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      {plan !== null && (
        <div
          className="review-detail-facts"
          role="group"
          aria-label="Arrangement preview"
          data-arrange-plan="true"
        >
          <span data-arrange-moves="true">
            {`Arrangement: ${arrangeMoveCountLabel(arrangeMoveCount(plan))}`}
          </span>
          {/* The moves come from the WIRE ARRAY, never from `plan.text`: the
              prose is a rendering of these records, and a client that read it
              back would be showing a sentence as if it were the operation. A
              real list so a screen reader reads one move per item. */}
          <ul className="flex flex-col gap-1">
            {(plan.moves ?? []).map((move, index) => (
              <li
                key={`${index}-${move.from}-${move.to}`}
                data-arrange-move="true"
                data-arrange-action={move.action}
              >
                <span className="font-mono">{arrangeMoveLine(move)}</span>{" "}
                <span className="text-muted">{arrangeActionLabel(move.action)}</span>
              </li>
            ))}
          </ul>
          {/* The engine's own lines, copied and never rebuilt: a second
              formatter here would let the two drift. */}
          {plan.text.map((line, index) => (
            <span key={`${index}-${line}`} data-arrange-text="true">
              {line}
            </span>
          ))}
          {plan.creates.length > 0 && (
            <>
              <span>Creates these folders</span>
              {plan.creates.map(create => (
                <span key={create.path} className="font-mono" data-arrange-create="true">
                  {create.path}
                </span>
              ))}
            </>
          )}
          {/* The server's refusal, verbatim. The client decided none of these,
              so it writes no words for one - and while one stands the plan
              cannot be applied. */}
          {(plan.refusals ?? []).map(refusal => (
            <span
              key={`${refusal.status}-${refusal.reason}`}
              className="review-detail-reason text-red-500"
              data-arrange-refusal="true"
            >
              {arrangeRefusalLine(refusal)}
            </span>
          ))}
        </div>
      )}
      {failure !== null && failure.at !== "undo" && (
        <span
          className="review-detail-reason text-red-500"
          role="alert"
          data-arrange-plan-failure={failure.at === "apply" ? "true" : undefined}
          data-arrange-preview-failure={failure.at === "preview" ? "true" : undefined}
        >
          {failure.detail}
        </span>
      )}
      <div className="review-detail-actions">
        {plan === null && (
          <button
            type="button"
            className={SECONDARY_BUTTON}
            data-arrange-preview="true"
            disabled={busy}
            onClick={() => void preview()}
          >
            Preview the moves
          </button>
        )}
        {plan !== null && (
          <>
            <button
              type="button"
              className={PRIMARY_BUTTON}
              data-arrange-apply="true"
              // A plan the server already objects to answers 409, so it may not
              // look like a working button. A plan with zero moves and zero
              // creates is still applicable: a grown project's plan legitimately
              // arranges nothing new.
              disabled={busy || (plan.refusals ?? []).length > 0}
              onClick={() => void apply(plan)}
            >
              Apply this arrangement
            </button>
            <button
              type="button"
              className={SECONDARY_BUTTON}
              data-arrange-cancel="true"
              disabled={busy}
              onClick={cancel}
            >
              Cancel
            </button>
          </>
        )}
        {applied !== null && failure?.at !== "undo" && (
          <button
            type="button"
            className={SECONDARY_BUTTON}
            data-arrange-undo="true"
            disabled={busy}
            onClick={() => void undo(applied)}
          >
            Undo this arrange
          </button>
        )}
        {applied !== null && failure?.at === "undo" && (
          <span
            className="review-detail-reason text-red-500"
            role="alert"
            data-arrange-undo-failure="true"
          >
            {failure.detail}
          </span>
        )}
      </div>
      {applied !== null && (
        <div className="review-detail-facts" role="group" aria-label="Arrangement result">
          <span data-arrange-applied="true">
            {`Arranged: ${arrangeMoveCountLabel(applied.moves)}`}
          </span>
          {/* The server's self-report, said out loud and by name. A silent pass
              would leave a directory the walk cannot see - or a row that names
              no file - for the next reader to find on their own. */}
          {applied.unseeable.length > 0 && (
            <span className="review-detail-reason" data-arrange-unseeable="true">
              {`Written but unseen by the library: ${applied.unseeable.join(", ")}`}
            </span>
          )}
          {applied.stale.length > 0 && (
            <span className="review-detail-reason" data-arrange-stale="true">
              {`Asset rows that name no file: ${applied.stale.join(", ")}`}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
