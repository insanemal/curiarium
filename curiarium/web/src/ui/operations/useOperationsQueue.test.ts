// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";

// The hook reads the domain module directly (never the `../../lib/api` barrel),
// so the mock lives there. The original is spread back so the type surface keeps
// working.
const operationsApi = vi.hoisted(() => ({ listOperations: vi.fn() }));
vi.mock("../../lib/api/operations", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api/operations")>()),
  listOperations: operationsApi.listOperations,
}));

import { listOperations, type Operation } from "../../lib/api/operations";
import { UnauthorizedError } from "../../lib/api/http";
import { PAGE_SIZE, useOperationsQueue } from "./useOperationsQueue";

const listOperationsMock = vi.mocked(listOperations);

/** The queue's poll interval: the tests assert on the number, not on a symbol. */
const POLL_MS = 1500;

function makeOperation(overrides: Partial<Operation> = {}): Operation {
  return {
    id: "op-1",
    workflow: "split-and-create",
    target: "Container",
    status: "running",
    submitted_by: "u1",
    created_at: "2026-01-01T00:00:00Z",
    started_at: "2026-01-01T00:00:01Z",
    finished_at: null,
    error: null,
    undo_state: "none",
    superseded_by: null,
    steps: [],
    ...overrides,
  };
}

/** A full page of rows, so `hasMore` is true for the next page. */
function makePage(count: number, prefix = "op"): Operation[] {
  return Array.from({ length: count }, (_unused, index) =>
    makeOperation({ id: `${prefix}-${index}` }),
  );
}

/** A promise whose settlement the test drives, for ordering two responses. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

/** Lets every resolved mock's promise chain settle without moving the clock. */
async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

/** Advances the poll clock and lets the read it starts settle. */
async function tick(ms = POLL_MS): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.resetAllMocks();
  listOperationsMock.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("useOperationsQueue activation", () => {
  it("issues no request while disabled and exactly one page 0 on enable", async () => {
    // Catches a queue that reads the ledger on every route, and one that issues
    // two entry reads for the same activation.
    const { result, rerender } = renderHook(
      (props: { enabled: boolean }) => useOperationsQueue(props),
      { initialProps: { enabled: false } },
    );
    await settle();

    expect(listOperationsMock).not.toHaveBeenCalled();
    expect(result.current.loading).toBe(false);

    rerender({ enabled: true });
    await settle();

    expect(listOperationsMock).toHaveBeenCalledTimes(1);
    expect(listOperationsMock).toHaveBeenCalledWith(
      expect.objectContaining({
        state: "",
        workflow: "",
        submitted_by: "",
        limit: PAGE_SIZE,
        offset: 0,
      }),
    );
    expect(result.current.loaded).toBe(true);
    expect(result.current.loading).toBe(false);
  });

  it("sends every filter to the server as its own query field", async () => {
    // Catches a filter that is shown but not asked for, and a "Only mine" that
    // never reaches `submitted_by`.
    const { result } = renderHook(() => useOperationsQueue({ enabled: true }));
    await settle();

    act(() => {
      result.current.setFilters({ state: "failed", workflow: "extract", submitted_by: "u1" });
    });
    await settle();

    expect(listOperationsMock).toHaveBeenLastCalledWith(
      expect.objectContaining({
        state: "failed",
        workflow: "extract",
        submitted_by: "u1",
        offset: 0,
      }),
    );
  });
});

describe("useOperationsQueue pages", () => {
  it("re-reads page 0 and replaces the rows when a filter changes", async () => {
    // Catches a filter change that pages on from the old offset (settling, say,
    // on offset 50 of a completely different query) or appends to the old rows.
    listOperationsMock
      .mockResolvedValueOnce(makePage(PAGE_SIZE, "first"))
      .mockResolvedValueOnce([makeOperation({ id: "op-more" })])
      .mockResolvedValueOnce([makeOperation({ id: "op-done", status: "done" })]);
    const { result } = renderHook(() => useOperationsQueue({ enabled: true }));
    await settle();

    await act(async () => {
      await result.current.loadMore();
    });
    expect(result.current.rows).toHaveLength(PAGE_SIZE + 1);

    act(() => {
      result.current.setFilters({ state: "done" });
    });
    await settle();

    const calls = listOperationsMock.mock.calls;
    const lastCall = calls[calls.length - 1]?.[0];
    expect(lastCall).toEqual(expect.objectContaining({ state: "done", offset: 0 }));
    expect(result.current.rows.map(row => row.id)).toEqual(["op-done"]);
  });

  it("reads offset 50 for Load more and appends rather than replacing", async () => {
    // Catches a Load more that re-reads page 0 (the list jumps back to the top)
    // and one that drops the page the operator was reading.
    listOperationsMock
      .mockResolvedValueOnce(makePage(PAGE_SIZE, "first"))
      .mockResolvedValueOnce(makePage(3, "second"));
    const { result } = renderHook(() => useOperationsQueue({ enabled: true }));
    await settle();
    expect(result.current.hasMore).toBe(true);

    await act(async () => {
      await result.current.loadMore();
    });

    expect(listOperationsMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ offset: PAGE_SIZE }),
    );
    expect(result.current.rows).toHaveLength(PAGE_SIZE + 3);
    expect(result.current.rows[0].id).toBe("first-0");
    expect(result.current.rows[result.current.rows.length - 1]?.id).toBe("second-2");
    // A short last page is the only honest end-of-list signal the route leaves.
    expect(result.current.hasMore).toBe(false);
  });
});

describe("useOperationsQueue polling", () => {
  it("polls only while a row is active, and stops once every row is terminal", async () => {
    // Catches a queue that hammers a settled ledger, and one that freezes while
    // an operation is still moving.
    listOperationsMock
      .mockResolvedValueOnce([makeOperation({ id: "op-1", status: "running" })])
      .mockResolvedValueOnce([makeOperation({ id: "op-1", status: "done", finished_at: "2026-01-01T00:01:00Z" })]);
    const { result } = renderHook(() => useOperationsQueue({ enabled: true }));
    await settle();

    await tick(POLL_MS - 1);
    expect(listOperationsMock).toHaveBeenCalledTimes(1);

    await tick(1);
    expect(listOperationsMock).toHaveBeenCalledTimes(2);
    expect(result.current.rows[0].status).toBe("done");

    await tick(POLL_MS * 4);
    expect(listOperationsMock).toHaveBeenCalledTimes(2);
  });

  it("re-arms after a failed poll and clears the error on the next success", async () => {
    // Catches a poll chain that dies on the first failure (the queue stuck
    // mid-flight with a sentence nobody can clear) and an error left behind.
    listOperationsMock
      .mockResolvedValueOnce([makeOperation({ id: "op-1", status: "running" })])
      .mockRejectedValueOnce(new Error("the ledger is unavailable right now"))
      .mockResolvedValueOnce([makeOperation({ id: "op-1", status: "running" })]);
    const { result } = renderHook(() => useOperationsQueue({ enabled: true }));
    await settle();

    await tick(POLL_MS);
    expect(result.current.error).toBe("the ledger is unavailable right now");

    await tick(POLL_MS);
    expect(listOperationsMock).toHaveBeenCalledTimes(3);
    expect(result.current.error).toBeNull();
  });

  it("never paints a response a newer request superseded", async () => {
    // Catches a queue without a sequence guard: the mount read landing last would
    // paint the rows of a filter the operator already left.
    const slow = deferred<Operation[]>();
    const fast = deferred<Operation[]>();
    listOperationsMock.mockReturnValueOnce(slow.promise).mockReturnValueOnce(fast.promise);
    const { result } = renderHook(() => useOperationsQueue({ enabled: true }));
    await settle();

    act(() => {
      result.current.setFilters({ state: "done" });
    });
    await settle();
    expect(listOperationsMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      fast.resolve([makeOperation({ id: "op-fast", status: "done" })]);
    });
    await settle();
    expect(result.current.rows.map(row => row.id)).toEqual(["op-fast"]);

    await act(async () => {
      slow.resolve([makeOperation({ id: "op-slow", status: "done" })]);
    });
    await settle();
    expect(result.current.rows.map(row => row.id)).toEqual(["op-fast"]);
  });
});

describe("useOperationsQueue failures", () => {
  it("hands a 401 to the host and writes no sentence", async () => {
    // Catches a revoked session shown as a queue failure - the operator would
    // retry a read that can only succeed after signing in again.
    const onUnauthorized = vi.fn();
    listOperationsMock.mockRejectedValue(new UnauthorizedError());
    const { result } = renderHook(() => useOperationsQueue({ enabled: true, onUnauthorized }));
    await settle();

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBeNull();
    expect(result.current.loaded).toBe(false);
    expect(result.current.activeCount).toBeNull();

    await tick(POLL_MS * 4);
    expect(listOperationsMock).toHaveBeenCalledTimes(1);
  });
});
