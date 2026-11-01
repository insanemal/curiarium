// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";

// Domain modules, not the `../../lib/api` barrel: the hook itself imports from
// the domain modules, and a barrel mock that forgot one export would make every
// assertion here vacuous.
vi.mock("../../lib/api/settings", () => ({
  getImportRuns: vi.fn(),
  getImportRun: vi.fn(),
}));

// The hook matches `err instanceof UnauthorizedError`, so the test has to
// construct the same class the hook sees.
vi.mock("../../lib/api/http", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
}));

import { UnauthorizedError } from "../../lib/api/http";
import {
  getImportRun,
  getImportRuns,
  type ImportRunDetail,
  type ImportRunList,
  type ImportRunSummary,
} from "../../lib/api/settings";
import { useImportRuns } from "./useImportRuns";

const getImportRunsMock = vi.mocked(getImportRuns);
const getImportRunMock = vi.mocked(getImportRun);

const POLL_MS = 1500;
const ENTRY_ERROR = "Import run history is unavailable.";

function makeSummary(overrides: Partial<ImportRunSummary> = {}): ImportRunSummary {
  return {
    id: "run-1",
    status: "succeeded",
    mode: "copy",
    root: "/mnt/models",
    reason: null,
    started_at: 1_700_000_000,
    finished_at: 1_700_000_042,
    registered: 4,
    known: 2,
    ignored: 1,
    failed: 0,
    rejected: 0,
    discovered: 7,
    skipped: 3,
    ...overrides,
  };
}

function makeDetail(overrides: Partial<ImportRunDetail> = {}): ImportRunDetail {
  return { ...makeSummary(), errors: [], ...overrides };
}

function makeList(runs: ImportRunSummary[]): ImportRunList {
  return { runs, total: runs.length, limit: 1, offset: 0 };
}

const running = makeDetail({ id: "run-running", status: "running", finished_at: null });
const succeeded = makeDetail({ id: "run-succeeded" });

/** A promise whose settlement the test drives, for ordering two responses. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

/**
 * Lets every pending promise chain settle without advancing the clock. The
 * entry fetch is `list -> detail -> setState`, so two microtask yields are the
 * minimum that lands it inside one act().
 */
async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

/** Advances the poll clock and lets the request it starts settle. */
async function tick(ms = POLL_MS): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  getImportRunsMock.mockReset();
  getImportRunMock.mockReset();
  getImportRunsMock.mockResolvedValue(makeList([succeeded]));
  getImportRunMock.mockResolvedValue(succeeded);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("useImportRuns polling (AC-1)", () => {
  it("polls every 1.5s while the newest run is running", async () => {
    getImportRunsMock.mockResolvedValue(makeList([running]));
    getImportRunMock.mockResolvedValue(running);

    renderHook(() => useImportRuns({ enabled: true }));
    await settle();

    // One entry pair: the list first, then the newest run's detail.
    expect(getImportRunsMock).toHaveBeenCalledTimes(1);
    expect(getImportRunMock).toHaveBeenCalledTimes(1);

    for (let ticked = 1; ticked <= 3; ticked += 1) {
      await tick();
      // Each poll is one list plus one detail: the count is unambiguous because
      // the entry pair above is stated explicitly.
      expect(getImportRunsMock).toHaveBeenCalledTimes(1 + ticked);
      expect(getImportRunMock).toHaveBeenCalledTimes(1 + ticked);
    }
  });

  it("keeps the badge honest after a failed poll, then clears the error on the next success", async () => {
    getImportRunsMock
      .mockResolvedValueOnce(makeList([running]))
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValue(makeList([running]));
    getImportRunMock.mockResolvedValue(running);

    const { result } = renderHook(() => useImportRuns({ enabled: true }));
    await settle();
    expect(result.current.error).toBeNull();

    await tick();
    // The failure changed no run state. Without the `pollTick` re-arm the chain
    // would die here and the badge would stick at 1 forever.
    expect(result.current.error).toBe(ENTRY_ERROR);
    expect(getImportRunsMock).toHaveBeenCalledTimes(2);
    expect(getImportRunMock).toHaveBeenCalledTimes(1);

    await tick();
    expect(getImportRunsMock).toHaveBeenCalledTimes(3);
    expect(getImportRunMock).toHaveBeenCalledTimes(2);
    expect(result.current.error).toBeNull();
  });
});

describe("useImportRuns idle does not poll (AC-6)", () => {
  it("makes zero requests when the queue is never opened", async () => {
    renderHook(() => useImportRuns({ enabled: false }));

    // Ten minutes of clock: every chance to fire a stray timer.
    await tick(600_000);

    expect(getImportRunsMock).not.toHaveBeenCalled();
    expect(getImportRunMock).not.toHaveBeenCalled();
  });

  it("makes one entry pair and then nothing for a succeeded run", async () => {
    renderHook(() => useImportRuns({ enabled: true }));
    await settle();

    expect(getImportRunsMock).toHaveBeenCalledTimes(1);
    expect(getImportRunMock).toHaveBeenCalledTimes(1);

    await tick(60_000);

    expect(getImportRunsMock).toHaveBeenCalledTimes(1);
    expect(getImportRunMock).toHaveBeenCalledTimes(1);
  });

  it("stops polling once a running run flips to succeeded", async () => {
    getImportRunsMock.mockResolvedValue(makeList([running]));
    getImportRunMock
      .mockResolvedValueOnce(running)
      .mockResolvedValue(succeeded);

    const { result } = renderHook(() => useImportRuns({ enabled: true }));
    await settle();
    expect(result.current.run?.status).toBe("running");

    // The poll that returns the finished detail is the last one.
    await tick();
    expect(result.current.run?.status).toBe("succeeded");
    expect(getImportRunsMock).toHaveBeenCalledTimes(2);
    expect(getImportRunMock).toHaveBeenCalledTimes(2);

    await tick(60_000);
    expect(getImportRunsMock).toHaveBeenCalledTimes(2);
    expect(getImportRunMock).toHaveBeenCalledTimes(2);
  });
});

describe("useImportRuns activation", () => {
  it("refetches on every re-entry", async () => {
    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useImportRuns({ enabled }),
      { initialProps: { enabled: false } },
    );
    expect(getImportRunsMock).not.toHaveBeenCalled();

    rerender({ enabled: true });
    await settle();
    expect(getImportRunsMock).toHaveBeenCalledTimes(1);

    rerender({ enabled: false });
    await settle();
    rerender({ enabled: true });
    await settle();

    expect(getImportRunsMock).toHaveBeenCalledTimes(2);
    expect(getImportRunMock).toHaveBeenCalledTimes(2);
  });

  it("fetches the entry pair exactly once under StrictMode", async () => {
    const { result } = renderHook(() => useImportRuns({ enabled: true }), {
      wrapper: React.StrictMode,
    });

    await settle();

    expect(getImportRunsMock).toHaveBeenCalledTimes(1);
    expect(getImportRunMock).toHaveBeenCalledTimes(1);
    expect(result.current.run).toEqual(succeeded);
  });

  it("fetches no detail for a list with no runs", async () => {
    getImportRunsMock.mockResolvedValue(makeList([]));

    const { result } = renderHook(() => useImportRuns({ enabled: true }));
    await settle();

    expect(getImportRunsMock).toHaveBeenCalledTimes(1);
    expect(getImportRunMock).not.toHaveBeenCalled();
    expect(result.current.run).toBeNull();
    expect(result.current.loaded).toBe(true);
  });
});

describe("useImportRuns response ordering", () => {
  it("lets the newest request win when the older one answers last", async () => {
    const older = deferred<ImportRunDetail>();
    const newer = deferred<ImportRunDetail>();
    getImportRunsMock.mockResolvedValue(makeList([running]));
    getImportRunMock
      .mockResolvedValueOnce(running)
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);

    const { result } = renderHook(() => useImportRuns({ enabled: true }));
    await settle();

    // Two manual refreshes overlap before either detail answers.
    act(() => {
      void result.current.refresh();
    });
    await settle();
    act(() => {
      void result.current.refresh();
    });
    await settle();

    await act(async () => {
      newer.resolve(succeeded);
    });
    expect(result.current.run).toEqual(succeeded);

    // The superseded response lands late and must not paint over the newer one.
    await act(async () => {
      older.resolve(running);
    });

    expect(result.current.run).toEqual(succeeded);
  });
});

describe("useImportRuns failure handling", () => {
  it("reports an unauthorized poll once, records no error and stops the chain", async () => {
    getImportRunsMock
      .mockResolvedValueOnce(makeList([running]))
      .mockRejectedValueOnce(new UnauthorizedError());
    getImportRunMock.mockResolvedValue(running);
    const onUnauthorized = vi.fn();

    const { result } = renderHook(() =>
      useImportRuns({ enabled: true, onUnauthorized }),
    );
    await settle();
    expect(getImportRunsMock).toHaveBeenCalledTimes(1);

    await tick();

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    // A revoked session is not a poll failure: no error string for the panel.
    expect(result.current.error).toBeNull();
    expect(getImportRunsMock).toHaveBeenCalledTimes(2);

    // Armed is cleared, so the next poll never fires.
    await tick(60_000);
    expect(getImportRunsMock).toHaveBeenCalledTimes(2);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("turns an unauthorized entry fetch into a sign-out without an error string", async () => {
    getImportRunsMock.mockRejectedValue(new UnauthorizedError());
    const onUnauthorized = vi.fn();

    const { result } = renderHook(() =>
      useImportRuns({ enabled: true, onUnauthorized }),
    );
    await settle();

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBeNull();
    expect(getImportRunMock).not.toHaveBeenCalled();
  });

  it("keeps an entry failure visible and retryable through refresh", async () => {
    getImportRunsMock.mockRejectedValue(new Error("boom"));

    const { result } = renderHook(() => useImportRuns({ enabled: true }));
    await settle();

    expect(result.current.error).toBe(ENTRY_ERROR);
    expect(result.current.loaded).toBe(false);
    expect(result.current.loading).toBe(false);

    getImportRunsMock.mockResolvedValue(makeList([succeeded]));
    await act(async () => {
      await result.current.refresh();
    });

    expect(result.current.error).toBeNull();
    expect(result.current.loaded).toBe(true);
    expect(result.current.run).toEqual(succeeded);
  });
});

describe("useImportRuns in-flight flags", () => {
  it("shows refreshing only for the operator's refresh, never for a background poll", async () => {
    const entry = deferred<ImportRunList>();
    getImportRunsMock.mockReturnValueOnce(entry.promise).mockResolvedValue(makeList([running]));
    getImportRunMock.mockResolvedValue(running);

    const { result } = renderHook(() => useImportRuns({ enabled: true }));
    // The entry fetch is a background load: no operator is waiting on a button.
    expect(result.current.refreshing).toBe(false);

    await act(async () => {
      entry.resolve(makeList([running]));
    });
    await settle();
    expect(result.current.refreshing).toBe(false);

    const manual = deferred<ImportRunList>();
    getImportRunsMock.mockReturnValueOnce(manual.promise);
    act(() => {
      void result.current.refresh();
    });
    expect(result.current.refreshing).toBe(true);

    await act(async () => {
      manual.resolve(makeList([running]));
    });
    await settle();
    expect(result.current.refreshing).toBe(false);

    // A poll of a running run settles without ever touching the flag.
    await tick();
    expect(result.current.refreshing).toBe(false);
    expect(getImportRunsMock).toHaveBeenCalledTimes(3);
  });

  it("reports loaded and lastRefreshedAt only once a fetch succeeded", async () => {
    const entry = deferred<ImportRunList>();
    getImportRunsMock.mockReturnValueOnce(entry.promise);
    getImportRunMock.mockResolvedValue(succeeded);

    const { result } = renderHook(() => useImportRuns({ enabled: true }));

    expect(result.current.loaded).toBe(false);
    expect(result.current.lastRefreshedAt).toBeNull();
    expect(result.current.loading).toBe(true);

    await act(async () => {
      entry.resolve(makeList([succeeded]));
    });
    await settle();

    expect(result.current.loaded).toBe(true);
    expect(result.current.loading).toBe(false);
    expect(result.current.lastRefreshedAt).toEqual(expect.any(Number));
  });

  it("early-returns from refresh until the hook has activated once", async () => {
    const { result } = renderHook(() => useImportRuns({ enabled: false }));

    await act(async () => {
      await result.current.refresh();
    });

    expect(getImportRunsMock).not.toHaveBeenCalled();
  });
});
