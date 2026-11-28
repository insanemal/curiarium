import React from "react";

// Imported from the domain module, not the `../../lib/api` barrel: several
// suites replace that barrel with a fixed object, so a named import from it
// would come back `undefined` there (`useBundleDetect.ts` documents the same
// rule).
import {
  getOperation,
  planOperation,
  submitOperation,
  undoOperation,
  type Operation,
  type OperationPlan,
  type OperationStep,
  type OperationUndo,
} from "../../lib/api/operations";
import { UnauthorizedError } from "../../lib/api/http";

/**
 * One state machine, two doors. The split panel and the project page's adopt
 * control both queue ONE server-computed operation: they ask for a plan, submit
 * that plan's `plan_id` with one idempotency key, poll the row the server makes
 * and fold it back with an undo when the server offers one. Two copies of this
 * machine would drift in exactly the ways its tests pin - a per-render key, a
 * poll that outlives its row, an undo that outlives its answer - so the door
 * supplies only its WORDS and the workflow it is opening.
 *
 * The plan is a READ keyed by the workflow, the target and the serialised
 * params; submit, poll and undo are the runner's, and every sentence shown comes
 * back as state rather than being spoken here.
 */

const POLL_MS = 250;

/** The three statuses a runner never leaves: the poll stops on one of them. */
export function isTerminal(status: string): boolean {
  return status === "done" || status === "failed" || status === "cancelled";
}

/** The thrown message, or a neutral fallback when the throw was not an Error.
 *  The API layer already turns the server's `detail` into `Error.message`, so
 *  this is the one place a failure sentence is chosen - and the client
 *  paraphrases nothing it did not decide. */
function failureDetail(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

export type OperationRunOptions = {
  workflow: string;
  target: string;
  /** Params the planner needs; omitted = {} and it is NOT part of the plan's key. */
  params?: Record<string, unknown>;
  /** False means: plan nothing, submit nothing (a viewer). */
  enabled: boolean;
  /** The host's completion signal after a run finishes or is undone. */
  onChanged?: () => void;
  /** Session expiry is the host's news, not this hook's. */
  onUnauthorized?: () => void;
  /**
   * A row the server says is already in flight for this exact (workflow,
   * target). Non-null means: do not plan, do not submit - seed `operation` from
   * it and poll it, so the popup the operator sees is the run they started
   * before the reload.
   */
  adopt?: Operation | null;
  /** The four fallback sentences, so each door keeps its own words. */
  planFailureFallback: string;
  submitFailureFallback: string;
  pollFailureFallback: string;
  undoFailureFallback: string;
};

export type OperationRun = {
  plan: OperationPlan | null;
  planFailure: string | null;
  submitFailure: string | null;
  pollFailure: string | null;
  undoFailure: string | null;
  operation: Operation | null;
  busy: boolean;
  /** The runner's steps in INDEX order, whatever order the payload arrived in. */
  orderedSteps: OperationStep[];
  terminal: boolean;
  submit: () => Promise<void>;
  /** Folds the run back; answers the server's body, or null when it refused/401. */
  undo: () => Promise<OperationUndo | null>;
  /** Puts the door back on its review after a finished run, so a trigger door can
   *  offer its control again. The plan stays in hand (a submit re-computes it and
   *  refuses a stale one with 409) and the mount's ONE idempotency key makes a
   *  replay answer the operation this mount already queued, never a second one. */
  reset: () => void;
};

export function useOperationRun(options: OperationRunOptions): OperationRun {
  const {
    workflow,
    target,
    params,
    enabled,
    adopt,
    onChanged,
    onUnauthorized,
    planFailureFallback,
    submitFailureFallback,
    pollFailureFallback,
    undoFailureFallback,
  } = options;

  const [plan, setPlan] = React.useState<OperationPlan | null>(null);
  const [planFailure, setPlanFailure] = React.useState<string | null>(null);
  const [submitFailure, setSubmitFailure] = React.useState<string | null>(null);
  const [pollFailure, setPollFailure] = React.useState<string | null>(null);
  const [undoFailure, setUndoFailure] = React.useState<string | null>(null);
  const [operation, setOperation] = React.useState<Operation | null>(null);
  const [busy, setBusy] = React.useState(false);

  // The host may hand this hook fresh callback identities every render, so both
  // callbacks are read through refs: the plan is keyed by the workflow, the
  // target and the shape alone, and a READ must not run again because a parent
  // re-rendered.
  const unauthorizedRef = React.useRef(onUnauthorized);
  unauthorizedRef.current = onUnauthorized;
  const changedRef = React.useRef(onChanged);
  changedRef.current = onChanged;
  // The four sentences are the door's, not the request's: a fresh string never
  // refetches, and an answer that arrives late still speaks its own door's words.
  const fallbacksRef = React.useRef({
    planFailureFallback,
    submitFailureFallback,
    pollFailureFallback,
    undoFailureFallback,
  });
  fallbacksRef.current = {
    planFailureFallback,
    submitFailureFallback,
    pollFailureFallback,
    undoFailureFallback,
  };

  // ONE idempotency key per mount, minted on the first render and never again: a
  // second mint would let a double-click queue a second operation, while a replay
  // of this key answers the operation the first click already queued.
  const idempotencyKey = React.useRef<string>();
  if (!idempotencyKey.current) idempotencyKey.current = crypto.randomUUID();
  const submitKey = idempotencyKey.current;

  // The params object is read through a ref, while only its serialisation is part
  // of the plan's key: a parent that builds a fresh `{ shape }` literal every
  // render must not refetch a READ that has not changed.
  const paramsRef = React.useRef(params ?? {});
  paramsRef.current = params ?? {};
  const paramsKey = JSON.stringify(params ?? {});

  // The host hears about a finished operation exactly once PER RUN: a re-poll of
  // the same terminal row must not refetch the host's report a second time, and a
  // second run submitted from this mount (the adopt door's undo-then-adopt) is a
  // new completion the host does have to hear about. Declared up here because
  // adopting a row primes it below.
  const changedReported = React.useRef(false);

  // The adopted row, read through a ref so the seeding effect is keyed on its ID
  // alone: a host that hands a fresh row object every render must not re-seed an
  // operation the operator is already watching.
  const adoptRef = React.useRef<Operation | null>(adopt ?? null);
  adoptRef.current = adopt ?? null;
  const adoptId = adoptRef.current?.id ?? null;

  // ONE seeding per adopted id. The effect is declared BEFORE the plan effect so
  // that a row handed over on mount marks itself adopted before the plan READ
  // can fire, and `submit` can see it. `changedReported` is primed to the row's
  // own terminality: a row that was already finished when this panel opened is
  // not news for the host, while one that finishes later reports exactly once.
  const adoptedId = React.useRef<string | null>(null);
  React.useEffect(() => {
    const next = adoptRef.current;
    if (next === null) {
      // A host that withdrew the row (a new target's lookup found nothing) takes
      // the adoption with it; otherwise the next target would inherit this one's
      // submit block and never plan.
      if (adoptedId.current !== null) {
        adoptedId.current = null;
        setOperation(null);
      }
      return;
    }
    if (adoptedId.current === next.id) return;
    adoptedId.current = next.id;
    changedReported.current = isTerminal(next.status);
    setOperation(next);
  }, [adoptId]);

  // A plan is a READ: fetch it for the workflow and target on offer, and again
  // whenever the params change. A viewer issues no request at all, and neither
  // does a resumed panel: the consumed row already says what is running, so a
  // re-plan would only ask the server to describe work it is already doing.
  React.useEffect(() => {
    if (!enabled) return;
    if (adoptedId.current !== null) return;
    let live = true;
    setPlan(null);
    setPlanFailure(null);
    planOperation({ workflow, target, params: paramsRef.current })
      .then(next => {
        if (live) setPlan(next);
      })
      .catch((err: unknown) => {
        if (!live) return;
        // A 401 is the host's cue to sign the operator out, so the hook writes no
        // sentence of its own: a generic failure would strand them on a session
        // that is already gone.
        if (err instanceof UnauthorizedError) {
          unauthorizedRef.current?.();
          return;
        }
        setPlanFailure(failureDetail(err, fallbacksRef.current.planFailureFallback));
      });
    return () => {
      live = false;
    };
  }, [workflow, target, paramsKey, enabled]);

  // Progress is a POLL, because the runner owns the row: 250 ms is fast enough
  // to feel live and slow enough that a long run is a handful of reads. The timer
  // is cleared on every re-arm and on unmount, and a terminal status arms no
  // timer at all.
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
          setPollFailure(failureDetail(err, fallbacksRef.current.pollFailureFallback));
        });
    }, POLL_MS);
    return () => clearTimeout(timer);
  }, [operation]);

  // The host hears about a finished operation exactly once PER RUN: a re-poll of
  // the same terminal row must not refetch the host's report a second time, and a
  // second run submitted from this mount (the adopt door's undo-then-adopt) is a
  // new completion the host does have to hear about.
  const terminal = operation !== null && isTerminal(operation.status);
  React.useEffect(() => {
    if (!terminal || changedReported.current) return;
    changedReported.current = true;
    changedRef.current?.();
  }, [terminal]);

  async function submit(): Promise<void> {
    // A resumed row is not re-submitted: the operation exists, and a second
    // submit would queue another one over the same paths.
    if (adoptedId.current !== null) return;
    if (plan === null) return;
    setBusy(true);
    setSubmitFailure(null);
    try {
      const submitted = await submitOperation({ planId: plan.plan_id, idempotencyKey: submitKey });
      changedReported.current = false;
      setOperation(submitted);
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        unauthorizedRef.current?.();
        return;
      }
      // The server's own sentence already says why it refused (a stale plan, an
      // operation over those paths already queued), so it is shown verbatim.
      setSubmitFailure(failureDetail(err, submitFailureFallback));
    } finally {
      setBusy(false);
    }
  }

  async function undo(): Promise<OperationUndo | null> {
    if (operation === null) return null;
    const id = operation.id;
    setBusy(true);
    setUndoFailure(null);
    try {
      const answer = await undoOperation(id);
      // The answer IS the row's new state, so the button goes without another
      // read: `undone: false` means the server changed nothing, and the button
      // has to stay for the operator who will try again.
      if (answer.undone) {
        setOperation(previous =>
          previous === null ? previous : { ...previous, undo_state: "undone" },
        );
        // Only a fold the server confirmed is a change the host has to refetch:
        // `undone: false` means it changed nothing.
        changedRef.current?.();
      }
      return answer;
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        unauthorizedRef.current?.();
        return null;
      }
      // The button stays mounted under the sentence: a dead button would swallow
      // the server's answer and invite the same refusal forever.
      setUndoFailure(failureDetail(err, undoFailureFallback));
      return null;
    } finally {
      setBusy(false);
    }
  }

  function reset(): void {
    // The ADOPTION is deliberately kept: a resumed row is the server's record, and
    // dropping it would let the door offer a confirm over a live operation.
    if (adoptedId.current !== null) return;
    changedReported.current = false;
    setOperation(null);
    setSubmitFailure(null);
    setPollFailure(null);
    setUndoFailure(null);
  }

  // The steps read in the order the runner indexed them, whatever order the
  // payload happened to arrive in.
  const orderedSteps =
    operation === null ? [] : [...operation.steps].sort((left, right) => left.index - right.index);

  return {
    plan,
    planFailure,
    submitFailure,
    pollFailure,
    undoFailure,
    operation,
    busy,
    orderedSteps,
    terminal,
    submit,
    undo,
    reset,
  };
}
