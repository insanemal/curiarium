// @vitest-environment jsdom
import React from "react";
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getStatus = vi.fn();
const startScan = vi.fn();

// The hook imports from the domain modules, not the `lib/api` barrel, so these
// are the two mocks that matter. `UnauthorizedError` is mocked to one class and
// the tests reject with an instance of THAT class, or `instanceof` in the hook
// would compare against a different constructor and the 401 test would pass
// for the wrong reason.
vi.mock("../../lib/api/http", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {
    constructor(message = "Unauthorized") {
      super(message);
      this.name = "UnauthorizedError";
    }
  },
}));

vi.mock("../../lib/api/settings", () => ({
  getLibraryScanStatus: () => getStatus(),
  startLibraryScan: () => startScan(),
}));

import { UnauthorizedError } from "../../lib/api/http";
import type { LibraryScanStatus } from "../../lib/api/settings";
import { scanOutcome, useLibraryScan } from "./useLibraryScan";

const FINISHED_AT = 1_700_000_010;

function makeStatus(overrides: Partial<LibraryScanStatus> = {}): LibraryScanStatus {
  return {
    running: false,
    started_at: 1_700_000_000,
    finished_at: FINISHED_AT,
    registered: 3,
    known: 1,
    ignored: 0,
    missing: 0,
    failed: 0,
    errors: [],
    ...overrides,
  };
}

/**
 * Fresh objects per call, exactly as the API returns them: the poll chain
 * re-arms on the new `scan` identity, so one shared fixture would look like a
 * dying chain the real server can never produce.
 */
const runningStatus = (): LibraryScanStatus => makeStatus({ running: true, finished_at: null });
const finishedStatus = (): LibraryScanStatus => makeStatus();

/** The status read keeps answering with a fresh copy of the same payload. */
function readReturns(status: () => LibraryScanStatus): void {
  getStatus.mockImplementation(async () => status());
}

/** A promise the test resolves by hand, so a pending request can be observed. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

/** Flush the microtask queue inside act, so a settled read lands in state. */
async function settle(): Promise<void> {
  await act(async () => {});
}

/** Advance the fake clock by one poll interval inside act. */
async function tick(ms = 1_600): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  getStatus.mockReset();
  startScan.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useLibraryScan entry read", () => {
  it("does not read the status before the missing queue is opened", async () => {
    readReturns(finishedStatus);

    const { rerender } = renderHook(({ enabled }) => useLibraryScan({ enabled }), {
      initialProps: { enabled: false },
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000);
    });
    rerender({ enabled: false });
    await settle();

    expect(getStatus).not.toHaveBeenCalled();
  });

  it("reads the status once on entry", async () => {
    readReturns(finishedStatus);

    const { result } = renderHook(() => useLibraryScan({ enabled: true }));
    await settle();

    expect(getStatus).toHaveBeenCalledTimes(1);
    expect(result.current.loaded).toBe(true);
  });

  it("reads the entry status exactly once under StrictMode", async () => {
    readReturns(finishedStatus);

    const wrapper = ({ children }: { children: React.ReactNode }) =>
      React.createElement(React.StrictMode, null, children);

    renderHook(() => useLibraryScan({ enabled: true }), { wrapper });
    await settle();

    expect(getStatus).toHaveBeenCalledTimes(1);
  });

  it("refetches on re-entry", async () => {
    readReturns(finishedStatus);

    const { rerender } = renderHook(({ enabled }) => useLibraryScan({ enabled }), {
      initialProps: { enabled: true },
    });
    await settle();

    rerender({ enabled: false });
    await settle();
    rerender({ enabled: true });
    await settle();

    expect(getStatus).toHaveBeenCalledTimes(2);
  });
});

describe("useLibraryScan polling", () => {
  it("polls every 1.5 seconds while a scan is running and stops when it finishes", async () => {
    readReturns(runningStatus);

    renderHook(() => useLibraryScan({ enabled: true }));
    await settle();
    expect(getStatus).toHaveBeenCalledTimes(1);

    await tick();
    await tick();
    // Entry read plus one read per tick: the chain re-arms on each running reply.
    expect(getStatus).toHaveBeenCalledTimes(3);

    // The pending poll is what notices the scan finished; after that read the
    // chain must not re-arm at all.
    readReturns(finishedStatus);
    await tick();
    const settled = getStatus.mock.calls.length;
    expect(settled).toBe(4);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(getStatus).toHaveBeenCalledTimes(settled);
  });

  it("keeps polling after a failed read instead of dying", async () => {
    // The entry read succeeds, so the failure is a poll: a swallowed read must
    // still leave a timer behind, or one hiccup would silently stop the queue
    // from ever noticing that the scan finished.
    getStatus
      .mockImplementationOnce(async () => runningStatus())
      .mockRejectedValueOnce(new Error("network down"))
      .mockImplementation(async () => runningStatus());

    renderHook(() => useLibraryScan({ enabled: true }));
    await settle();
    expect(getStatus).toHaveBeenCalledTimes(1);

    await tick();
    expect(getStatus).toHaveBeenCalledTimes(2);

    await tick();
    expect(getStatus).toHaveBeenCalledTimes(3);

    await tick();
    expect(getStatus.mock.calls.length).toBeGreaterThan(3);
  });
});

describe("useLibraryScan announcements", () => {
  it("announces a finished scan once to both change signals", async () => {
    getStatus.mockImplementationOnce(async () => runningStatus());
    readReturns(finishedStatus);
    startScan.mockImplementation(async () => finishedStatus());
    const onAssetsChanged = vi.fn();
    const onFoldersChanged = vi.fn();

    const { result } = renderHook(() =>
      useLibraryScan({ enabled: true, onAssetsChanged, onFoldersChanged }),
    );
    await settle();
    expect(onFoldersChanged).not.toHaveBeenCalled();

    await tick();
    expect(onFoldersChanged).toHaveBeenCalledTimes(1);
    expect(onAssetsChanged).toHaveBeenCalledTimes(1);

    // Refetching the SAME finished scan (a manual rescan reporting the same
    // `finished_at`) must not announce it a second time.
    await act(async () => {
      await result.current.run();
    });
    await settle();

    expect(onFoldersChanged).toHaveBeenCalledTimes(1);
    expect(onAssetsChanged).toHaveBeenCalledTimes(1);
  });

  it("does not re-announce a scan that finished before the queue was opened", async () => {
    readReturns(finishedStatus);
    const onAssetsChanged = vi.fn();
    const onFoldersChanged = vi.fn();

    renderHook(() => useLibraryScan({ enabled: true, onAssetsChanged, onFoldersChanged }));
    await settle();
    await tick();
    await tick();

    expect(onFoldersChanged).not.toHaveBeenCalled();
    expect(onAssetsChanged).not.toHaveBeenCalled();
  });
});

describe("useLibraryScan failure handling", () => {
  it("swallows an advisory read failure and leaves the outcome unknown", async () => {
    getStatus.mockRejectedValue(new Error("status read failed"));

    const { result } = renderHook(() => useLibraryScan({ enabled: true }));
    await settle();

    expect(result.current.error).toBeNull();
    expect(result.current.loaded).toBe(false);
    expect(result.current.scan).toBeNull();
    expect(scanOutcome(result.current.scan)).toBe("unknown");
  });

  it("signs out and stops on a 401", async () => {
    getStatus.mockRejectedValue(new UnauthorizedError());
    const onUnauthorized = vi.fn();

    const { result } = renderHook(() =>
      useLibraryScan({ enabled: true, onUnauthorized }),
    );
    await settle();
    await tick();
    await tick();

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBeNull();
    expect(result.current.loaded).toBe(false);
    expect(getStatus).toHaveBeenCalledTimes(1);
  });
});

describe("useLibraryScan run", () => {
  it("starts a scan and surfaces the already-running message", async () => {
    readReturns(finishedStatus);
    startScan.mockRejectedValue(new Error("A scan is already running"));

    const { result } = renderHook(() => useLibraryScan({ enabled: true }));
    await settle();

    await act(async () => {
      await result.current.run();
    });

    expect(result.current.error).toBe("A scan is already running");
    expect(result.current.busy).toBe(false);
    expect(startScan).toHaveBeenCalledTimes(1);
  });

  it("sets busy for the start request only", async () => {
    readReturns(runningStatus);
    const pending = deferred<LibraryScanStatus>();
    startScan.mockReturnValue(pending.promise);

    const { result } = renderHook(() => useLibraryScan({ enabled: true }));
    await settle();

    // A status read is not a user action: it must never flip the button label.
    await tick();
    expect(result.current.busy).toBe(false);

    let started: Promise<void> = Promise.resolve();
    act(() => {
      started = result.current.run();
    });
    expect(result.current.busy).toBe(true);

    await act(async () => {
      pending.resolve(finishedStatus());
      await started;
    });
    expect(result.current.busy).toBe(false);
  });
});

describe("adopt (a scan the card's Re-scan started)", () => {
  it("adopts a status the card read, so the outcome follows that scan", async () => {
    readReturns(finishedStatus);

    const { result } = renderHook(() => useLibraryScan({ enabled: true }));
    await settle();

    act(() => result.current.adopt(makeStatus({ finished_at: 200, failed: 2 })));

    expect(result.current.scan?.failed).toBe(2);
    expect(result.current.loaded).toBe(true);
    expect(scanOutcome(result.current.scan)).toBe("failed");
  });

  it("does not announce a scan the card started a second time", async () => {
    readReturns(finishedStatus);
    const onAssetsChanged = vi.fn();
    const onFoldersChanged = vi.fn();

    const { result } = renderHook(() =>
      useLibraryScan({ enabled: true, onAssetsChanged, onFoldersChanged }),
    );
    await settle();

    act(() => result.current.adopt(makeStatus({ finished_at: 200 })));
    await settle();

    // The card path refetches the grid and the counts itself.
    expect(onFoldersChanged).not.toHaveBeenCalled();
    expect(onAssetsChanged).not.toHaveBeenCalled();
  });

  it("does not arm a second poll for a scan the card owns", async () => {
    readReturns(finishedStatus);

    const { result } = renderHook(() => useLibraryScan({ enabled: true }));
    await settle();
    expect(getStatus).toHaveBeenCalledTimes(1);

    act(() => result.current.adopt(runningStatus()));
    await tick(10_000);

    expect(getStatus).toHaveBeenCalledTimes(1);
  });
});

// The rule the queue's empty state depends on: ONLY a clean scan may claim a
// clean disk, so every "we cannot tell" input has to answer `unknown`.
describe("scanOutcome (only a clean scan may claim a clean disk)", () => {
  it("is unknown when no status was read at all", () => {
    expect(scanOutcome(null)).toBe("unknown");
  });

  it("is unknown while a scan is running", () => {
    expect(scanOutcome(runningStatus())).toBe("unknown");
  });

  it("is unknown when no scan has finished", () => {
    expect(scanOutcome(makeStatus({ running: false, finished_at: null }))).toBe("unknown");
  });

  it("is clean only for a finished scan with no failures", () => {
    expect(scanOutcome(makeStatus({ failed: 0 }))).toBe("clean");
  });

  it("is failed when the finished scan could not check every file", () => {
    expect(scanOutcome(makeStatus({ failed: 1 }))).toBe("failed");
  });
});
