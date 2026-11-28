// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";

// The hook imports the domain module directly (not the `../../lib/api` barrel),
// so the mock lives there. The original is spread back so the type surface and
// every other export (`isTerminal`, the label helpers) keep working.
const operationsApi = vi.hoisted(() => ({
  planOperation: vi.fn(),
  submitOperation: vi.fn(),
  getOperation: vi.fn(),
  undoOperation: vi.fn(),
}));
vi.mock("../../lib/api/operations", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api/operations")>()),
  planOperation: operationsApi.planOperation,
  submitOperation: operationsApi.submitOperation,
  getOperation: operationsApi.getOperation,
  undoOperation: operationsApi.undoOperation,
}));

import {
  getOperation,
  planOperation,
  submitOperation,
  undoOperation,
  type Operation,
  type OperationPlan,
  type OperationReview,
  type OperationStep,
  type OperationUndo,
} from "../../lib/api/operations";
import { UnauthorizedError } from "../../lib/api/http";
import { useOperationRun, type OperationRunOptions } from "./useOperationRun";

const planOperationMock = vi.mocked(planOperation);
const submitOperationMock = vi.mocked(submitOperation);
const getOperationMock = vi.mocked(getOperation);
const undoOperationMock = vi.mocked(undoOperation);

/** The hook's poll interval: the tests assert on the number, not on a symbol. */
const POLL_MS = 250;

/** The server's refusal, whole. A test that paraphrased it would be caught by
 *  the byte comparison against the hook's state. */
const REFUSAL =
  "the plan was computed against a library that moved: re-read the target and plan again";

/** A plan rejection, a poll rejection and a refused undo each carry their own
 *  sentence, so a hook that reused one for the other two is caught. */
const READ_REFUSAL = "the operation ledger is unavailable right now";
const UNDO_REFUSAL = "this operation was already undone by another operator";

/** A review's optional fields (`by_role`) are omitted on purpose: a client built
 *  against an older API must still read a review without the census. */
function makeReview(overrides: Partial<OperationReview> = {}): OperationReview {
  return {
    workflow: "split-and-create",
    target: "Container",
    moves: 1,
    creates: 1,
    adopts: 0,
    deletes: 0,
    members: 9,
    groups: [],
    cover: null,
    excluded: [],
    existing_project_id: null,
    existing_project_name: null,
    move_list: [{ source: "Container/Alpha", target: "Alpha", kind: "dir", action: "hoist" }],
    projects: [{ name: "Alpha", root: "Alpha", members: 9 }],
    ...overrides,
  };
}

function makePlan(overrides: Partial<OperationPlan> = {}): OperationPlan {
  return {
    plan_id: "plan-1",
    workflow: "split-and-create",
    target: "Container",
    steps: [],
    review: makeReview(),
    ...overrides,
  };
}

function makeStep(overrides: Partial<OperationStep> = {}): OperationStep {
  return {
    index: 0,
    op: "move",
    status: "pending",
    result: null,
    error: null,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

function makeOperation(overrides: Partial<Operation> = {}): Operation {
  return {
    id: "op-7",
    workflow: "split-and-create",
    target: "Container",
    status: "running",
    submitted_by: "u1",
    created_at: "2026-01-01T00:00:00Z",
    started_at: "2026-01-01T00:00:01Z",
    finished_at: null,
    error: null,
    undo_state: "available",
    superseded_by: null,
    steps: [],
    ...overrides,
  };
}

/** `members_removed` is omitted on purpose - it is the adopt-only field and an
 *  older server leaves the key out entirely. */
function makeUndo(overrides: Partial<OperationUndo> = {}): OperationUndo {
  return {
    operation_id: "op-7",
    undone: true,
    project_id: null,
    project_name: null,
    moves: 1,
    projects_removed: ["Alpha"],
    ...overrides,
  };
}

function makeOptions(overrides: Partial<OperationRunOptions> = {}): OperationRunOptions {
  return {
    workflow: "split-and-create",
    target: "Container",
    params: { shape: "split-top-folders" },
    enabled: true,
    planFailureFallback: "Could not plan the split",
    submitFailureFallback: "Could not queue the split",
    pollFailureFallback: "Could not read the operation",
    undoFailureFallback: "Could not undo the split",
    ...overrides,
  };
}

function renderRun(overrides: Partial<OperationRunOptions> = {}) {
  return renderHook((options: OperationRunOptions) => useOperationRun(options), {
    initialProps: makeOptions(overrides),
  });
}

/** A promise whose settlement the test drives. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

/** Lets every resolved mock's promise chain settle without moving the clock. */
async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

/** The steps out of index order in the payload, in order in the hook's view. */
const SHUFFLED_STEPS: OperationStep[] = [
  makeStep({ index: 2, op: "verify-members", status: "done", result: { members: 9 } }),
  makeStep({ index: 0, op: "move", status: "done", result: { moved: 1 } }),
  makeStep({ index: 1, op: "create-project", status: "done", result: { created: true } }),
];

beforeEach(() => {
  vi.resetAllMocks();
  planOperationMock.mockResolvedValue(makePlan());
  submitOperationMock.mockResolvedValue(makeOperation());
  getOperationMock.mockResolvedValue(makeOperation());
  undoOperationMock.mockResolvedValue(makeUndo());
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("useOperationRun planning", () => {
  it("plans nothing and returns no plan for a viewer", async () => {
    // Catches a viewer gate that still issues the READ: `enabled: false` must
    // leave `plan` null and cause no `planOperation` call at all.
    const { result } = renderRun({ enabled: false });
    await flush();

    expect(planOperationMock).not.toHaveBeenCalled();
    expect(result.current.plan).toBeNull();
    expect(result.current.busy).toBe(false);
  });

  it("plans the workflow, target and params on mount, and not again for an equal params literal", async () => {
    // Catches a params OBJECT in the plan's key: a parent that builds
    // `{ shape }` fresh every render would refetch the READ on every render.
    const { result, rerender } = renderRun();
    await waitFor(() => expect(result.current.plan).not.toBeNull());

    expect(planOperationMock).toHaveBeenCalledTimes(1);
    expect(planOperationMock).toHaveBeenCalledWith({
      workflow: "split-and-create",
      target: "Container",
      params: { shape: "split-top-folders" },
    });

    // A new literal, the same content: the serialised key has not changed.
    rerender(makeOptions({ params: { shape: "split-top-folders" } }));
    await flush();

    expect(planOperationMock).toHaveBeenCalledTimes(1);
    expect(result.current.plan?.plan_id).toBe("plan-1");
  });

  it("refetches the plan when the params content changes", async () => {
    // Catches a plan keyed on the workflow and target alone: a different shape
    // is a different plan and must be read again, not reused.
    planOperationMock
      .mockResolvedValueOnce(makePlan({ plan_id: "plan-1" }))
      .mockResolvedValueOnce(makePlan({ plan_id: "plan-2" }));
    const { result, rerender } = renderRun();
    await waitFor(() => expect(result.current.plan?.plan_id).toBe("plan-1"));

    rerender(makeOptions({ params: { shape: "one-per-folder" } }));
    await waitFor(() => expect(result.current.plan?.plan_id).toBe("plan-2"));

    expect(planOperationMock).toHaveBeenCalledTimes(2);
    expect(planOperationMock).toHaveBeenLastCalledWith({
      workflow: "split-and-create",
      target: "Container",
      params: { shape: "one-per-folder" },
    });
  });

  it("shows the server's plan refusal verbatim", async () => {
    // Catches a paraphrase (or a fallback) where the server's own sentence
    // belongs: the reason a plan was refused is the server's to say.
    planOperationMock.mockRejectedValue(new Error(REFUSAL));
    const { result } = renderRun();

    await waitFor(() => expect(result.current.planFailure).toBe(REFUSAL));
    expect(result.current.plan).toBeNull();
  });

  it("hands a 401 plan to the host and writes no sentence", async () => {
    // Catches a generic sentence left on screen for an expired session: the
    // host signs the operator out instead, and `onUnauthorized` fires once.
    const onUnauthorized = vi.fn();
    planOperationMock.mockRejectedValue(new UnauthorizedError());
    const { result } = renderRun({ onUnauthorized });

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    expect(result.current.planFailure).toBeNull();
    expect(result.current.plan).toBeNull();
  });
});

describe("useOperationRun submit", () => {
  it("submits each click with ONE key per mount and never without a plan", async () => {
    // Catches a key minted per click (two clicks queue two operations over the
    // same paths instead of replaying one intent), a key that is the plan id
    // (the two say different things), and a submit that fires with no plan.
    vi.useFakeTimers();
    const planGate = deferred<OperationPlan>();
    planOperationMock.mockReturnValue(planGate.promise);
    const { result } = renderRun();
    await flush();

    await act(async () => {
      await result.current.submit();
    });
    expect(submitOperationMock).not.toHaveBeenCalled();
    expect(result.current.operation).toBeNull();

    await act(async () => {
      planGate.resolve(makePlan());
    });
    await flush();
    expect(result.current.plan?.plan_id).toBe("plan-1");

    await act(async () => {
      await result.current.submit();
      await result.current.submit();
    });

    const calls = submitOperationMock.mock.calls;
    expect(calls).toHaveLength(2);
    const keys = calls.map(([input]) => input.idempotencyKey);
    expect(keys[0]).toEqual(expect.any(String));
    expect(keys[0].length).toBeGreaterThan(0);
    expect(new Set(keys).size).toBe(1);
    expect(keys[0]).not.toBe("plan-1");
    expect(calls[0][0].planId).toBe("plan-1");
    expect(result.current.operation?.id).toBe("op-7");
  });

  it("shows the server's submit refusal verbatim", async () => {
    // Catches a submit refusal swallowed (a click that queues nothing and says
    // nothing): the server's sentence explains the stale plan to the operator.
    const submitRefusal = "this plan is stale: the target changed since it was computed";
    submitOperationMock.mockRejectedValue(new Error(submitRefusal));
    const { result } = renderRun();
    await waitFor(() => expect(result.current.plan).not.toBeNull());

    await act(async () => {
      await result.current.submit();
    });

    expect(result.current.submitFailure).toBe(submitRefusal);
    expect(result.current.operation).toBeNull();
    expect(result.current.busy).toBe(false);
  });
});

describe("useOperationRun polling", () => {
  it("polls at 250 ms, orders the steps by index, and reports the change once", async () => {
    // Catches a poll on the wrong clock, a progress list in payload order (the
    // runner's index is the order), and a host told about the same terminal row
    // on every re-poll.
    vi.useFakeTimers();
    const onChanged = vi.fn();
    submitOperationMock.mockResolvedValue(makeOperation({ status: "running" }));
    getOperationMock
      .mockResolvedValueOnce(
        makeOperation({
          status: "running",
          steps: [
            makeStep({ index: 2, op: "verify-members", status: "pending" }),
            makeStep({ index: 0, op: "move", status: "done", result: { moved: 1 } }),
            makeStep({ index: 1, op: "create-project", status: "pending" }),
          ],
        }),
      )
      .mockResolvedValueOnce(makeOperation({ status: "done", steps: SHUFFLED_STEPS }));

    const { result } = renderRun({ onChanged });
    await flush();
    await act(async () => {
      await result.current.submit();
    });
    expect(result.current.operation?.status).toBe("running");
    expect(onChanged).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS - 1);
    });
    expect(getOperationMock).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(getOperationMock).toHaveBeenCalledTimes(1);
    expect(getOperationMock).toHaveBeenCalledWith("op-7");
    expect(result.current.orderedSteps.map(step => step.index)).toEqual([0, 1, 2]);
    expect(result.current.terminal).toBe(false);
    expect(onChanged).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    expect(getOperationMock).toHaveBeenCalledTimes(2);
    expect(result.current.terminal).toBe(true);
    expect(result.current.orderedSteps.map(step => step.index)).toEqual([0, 1, 2]);
    expect(result.current.orderedSteps.map(step => step.op)).toEqual([
      "move",
      "create-project",
      "verify-members",
    ]);
    expect(onChanged).toHaveBeenCalledTimes(1);

    // A terminal row arms no timer: five more seconds of clock change nothing.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(getOperationMock).toHaveBeenCalledTimes(2);
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("stops the timer when the runner unmounts mid-poll", async () => {
    // Catches a timer that outlives its row: a late read would write state into
    // an unmounted hook and keep a dead panel polling forever.
    vi.useFakeTimers();
    const { result, unmount } = renderRun();
    await flush();
    await act(async () => {
      await result.current.submit();
    });
    expect(result.current.operation?.status).toBe("running");

    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    expect(getOperationMock).not.toHaveBeenCalled();
  });

  it("keeps the server's poll refusal and arms no further poll", async () => {
    // Catches a failing read that repeats forever (a dead ledger hammered once
    // per 250 ms) and one that loses the server's sentence on the way.
    vi.useFakeTimers();
    getOperationMock.mockRejectedValue(new Error(READ_REFUSAL));
    const { result } = renderRun();
    await flush();
    await act(async () => {
      await result.current.submit();
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    await flush();

    expect(getOperationMock).toHaveBeenCalledTimes(1);
    expect(result.current.pollFailure).toBe(READ_REFUSAL);
    // The row is the server's record and the failed read wrote nothing to it.
    expect(result.current.operation?.status).toBe("running");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(getOperationMock).toHaveBeenCalledTimes(1);
  });
});

describe("useOperationRun undo", () => {
  it("answers the server's body, flips the row to undone and tells the host", async () => {
    // Catches an undo that discards the server's answer, leaves the button on a
    // row the server already folded, or withholds the host's refetch.
    vi.useFakeTimers();
    const onChanged = vi.fn();
    const answer = makeUndo();
    submitOperationMock.mockResolvedValue(makeOperation({ status: "done" }));
    undoOperationMock.mockResolvedValue(answer);
    const { result } = renderRun({ onChanged });
    await flush();
    await act(async () => {
      await result.current.submit();
    });
    // The terminal row reported itself once on submit; the undo's is its own.
    expect(onChanged).toHaveBeenCalledTimes(1);
    onChanged.mockClear();

    const returned = await act(async () => result.current.undo());

    expect(returned).toEqual(answer);
    expect(returned?.undone).toBe(true);
    expect(undoOperationMock).toHaveBeenCalledWith("op-7");
    expect(result.current.operation?.undo_state).toBe("undone");
    expect(result.current.undoFailure).toBeNull();
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(result.current.busy).toBe(false);
  });

  it("returns null and keeps the row when the undo is refused", async () => {
    // Catches a refusal read as success: an undo the server declined to perform
    // changed nothing, so `undo_state` stays and the sentence stays visible.
    vi.useFakeTimers();
    const onChanged = vi.fn();
    submitOperationMock.mockResolvedValue(makeOperation({ status: "done" }));
    undoOperationMock.mockRejectedValue(new Error(UNDO_REFUSAL));
    const { result } = renderRun({ onChanged });
    await flush();
    await act(async () => {
      await result.current.submit();
    });
    onChanged.mockClear();

    const returned = await act(async () => result.current.undo());

    expect(returned).toBeNull();
    expect(result.current.undoFailure).toBe(UNDO_REFUSAL);
    expect(result.current.operation?.undo_state).toBe("available");
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("hands a 401 undo to the host, returns null and writes no sentence", async () => {
    // Catches an expired session shown as an undo the server refused: the host
    // signs the operator out, and nothing is written into `undoFailure`.
    vi.useFakeTimers();
    const onUnauthorized = vi.fn();
    submitOperationMock.mockResolvedValue(makeOperation({ status: "done" }));
    undoOperationMock.mockRejectedValue(new UnauthorizedError());
    const { result } = renderRun({ onUnauthorized });
    await flush();
    await act(async () => {
      await result.current.submit();
    });

    const returned = await act(async () => result.current.undo());

    expect(returned).toBeNull();
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(result.current.undoFailure).toBeNull();
    expect(result.current.operation?.undo_state).toBe("available");
  });

  it("is a null no-op with no operation", async () => {
    // Catches an undo fired at a null row (an id-less request, or a fabricated
    // answer): with nothing to fold there is no request and no result.
    const { result } = renderRun({ enabled: false });
    await flush();

    expect(result.current.operation).toBeNull();
    const returned = await act(async () => result.current.undo());

    expect(returned).toBeNull();
    expect(undoOperationMock).not.toHaveBeenCalled();
    expect(result.current.undoFailure).toBeNull();
  });
});
