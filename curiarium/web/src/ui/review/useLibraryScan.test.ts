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
import { RESCAN_TIMEOUT_MS } from "../asset-grid/assetGridMeta";
import { scanOutcome, useLibraryScan } from "./useLibraryScan";

const FINISHED_AT = 1_700_000_010;

/** The server's sentence for a scan that could not take the slot. Spelled here
 *  because this file mocks `lib/api/settings`, whose own `SCAN_ALREADY_RUNNING`
 *  the hook never imports. */
const REFUSAL = "A library scan is already running";

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

  it("stops polling a running scan once the off-view deadline passes", async () => {
    // The status read is advisory, so a dead API keeps answering neither a
    // status nor an error the operator can see. The bound is on that STALL:
    // nothing stamps `lastReadAt`, so once `RESCAN_TIMEOUT_MS` has passed since
    // the last read that succeeded, the loop stops re-arming.
    getStatus
      .mockResolvedValueOnce(runningStatus())
      .mockRejectedValue(new Error("network down"));

    renderHook(() => useLibraryScan({ enabled: true }));
    await settle();
    expect(getStatus).toHaveBeenCalledTimes(1);

    // One interval in, a poll fails and the chain re-arms on `pollTick`.
    await tick();
    expect(getStatus).toHaveBeenCalledTimes(2);

    // One jump past the deadline: the last timer stamped inside it arms, the
    // read after it does not, and the stall is over the bound from then on.
    await tick(RESCAN_TIMEOUT_MS + 1_600);
    const readsAtDeadline = getStatus.mock.calls.length;
    expect(readsAtDeadline).toBeGreaterThan(2);

    await tick(RESCAN_TIMEOUT_MS);
    expect(getStatus).toHaveBeenCalledTimes(readsAtDeadline);
  });

  it("keeps following a scan that walks longer than the deadline", async () => {
    // A 15-minute rsync of a big storage folder is a HEALTHY scan. The deadline
    // bounds a dead API, not a slow scan, so every successful running read must
    // keep the chain alive past `RESCAN_TIMEOUT_MS` - a duration cap would lose
    // the finish signal and leave the tree stale after the scan ends, which is
    // the bug this ticket fixes.
    readReturns(runningStatus);

    renderHook(() => useLibraryScan({ enabled: true }));
    await settle();
    expect(getStatus).toHaveBeenCalledTimes(1);

    // Twelve minutes of walk time, advanced in one-minute steps: a healthy
    // read must keep stamping the bound, so the loop follows the scan right
    // across `RESCAN_TIMEOUT_MS` instead of capping its duration.
    for (let minute = 0; minute < 12; minute += 1) await tick(60_000);
    const reads = getStatus.mock.calls.length;
    expect(reads).toBeGreaterThan(10);

    // And it is still growing: the chain never stopped following the scan.
    await tick();
    expect(getStatus.mock.calls.length).toBeGreaterThan(reads);
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

    // Five further intervals of the same finished status: an announced
    // completion may never be announced again.
    for (let interval = 0; interval < 5; interval += 1) await tick();
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

  it("announces nothing when a finished scan registered and flagged nothing", async () => {
    // A healing-only run: the asset-evidence backfill can relink a migrated
    // folder's `storage_path` with nothing registered, and the tree's names are
    // unaffected. Both zero is a run with nothing to tell either signal about.
    getStatus.mockImplementationOnce(async () => runningStatus());
    readReturns(() => makeStatus({ registered: 0, missing: 0 }));
    const onAssetsChanged = vi.fn();
    const onFoldersChanged = vi.fn();

    const { rerender } = renderHook(
      ({ enabled }) => useLibraryScan({ enabled, onAssetsChanged, onFoldersChanged }),
      { initialProps: { enabled: true } },
    );
    await settle();

    await tick();
    expect(onFoldersChanged).not.toHaveBeenCalled();
    expect(onAssetsChanged).not.toHaveBeenCalled();

    // `announced` advanced even though no signal fired, so a later re-read of
    // the same `finished_at` cannot announce it either.
    rerender({ enabled: false });
    await settle();
    rerender({ enabled: true });
    await settle();
    await tick();

    expect(getStatus).toHaveBeenCalledTimes(3);
    expect(onFoldersChanged).not.toHaveBeenCalled();
    expect(onAssetsChanged).not.toHaveBeenCalled();
  });

  it("signals only the assets for a run that newly flagged vanished files", async () => {
    // A run with nothing registered still flags missing files: swallowing it
    // would leave the grid showing rows the scan just marked.
    getStatus.mockImplementationOnce(async () => runningStatus());
    readReturns(() => makeStatus({ registered: 0, missing: 3 }));
    const onAssetsChanged = vi.fn();
    const onFoldersChanged = vi.fn();

    renderHook(() => useLibraryScan({ enabled: true, onAssetsChanged, onFoldersChanged }));
    await settle();
    await tick();

    expect(onAssetsChanged).toHaveBeenCalledTimes(1);
    expect(onFoldersChanged).not.toHaveBeenCalled();
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
  it("reports a refused scan as a scan that already holds the slot", async () => {
    readReturns(finishedStatus);
    // The server no longer refuses the START: it accepts the operation and ends
    // it `failed` with its own sentence, so `startLibraryScan` RESOLVES with a
    // status that says a scan is already running. Spelling that here as a
    // resolved status (not a rejection) is the assertion: the hook must not
    // invent an error line for a scan that is demonstrably walking.
    startScan.mockResolvedValue(
      makeStatus({
        running: true,
        started_at: null,
        finished_at: null,
        errors: [REFUSAL],
      }),
    );

    const { result } = renderHook(() => useLibraryScan({ enabled: true }));
    await settle();

    await act(async () => {
      await result.current.run();
    });

    expect(startScan).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBeNull();
    expect(result.current.scan?.running).toBe(true);
    expect(result.current.scan?.errors).toEqual([REFUSAL]);
    expect(result.current.busy).toBe(false);
  });

  it("shows the message when the plan or the submit is refused", async () => {
    readReturns(finishedStatus);
    startScan.mockRejectedValue(new Error("Failed to plan the operation"));

    const { result } = renderHook(() => useLibraryScan({ enabled: true }));
    await settle();

    await act(async () => {
      await result.current.run();
    });

    expect(result.current.error).toBe("Failed to plan the operation");
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
