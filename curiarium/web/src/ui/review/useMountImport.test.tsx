// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";

// Domain modules, not the `../../lib/api` barrel: the hook itself imports from
// the domain modules, and a barrel mock that forgot one export would make every
// assertion here vacuous.
vi.mock("../../lib/api/settings", () => ({
  getMountImportStatus: vi.fn(),
}));

// The hook matches `err instanceof UnauthorizedError`, so the test has to
// construct the same class the hook sees.
vi.mock("../../lib/api/http", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
}));

import { UnauthorizedError } from "../../lib/api/http";
import { getMountImportStatus, type MountImportStatus } from "../../lib/api/settings";
import { useMountImport } from "./useMountImport";

const getMountImportStatusMock = vi.mocked(getMountImportStatus);

const POLL_MS = 1500;
const ENTRY_ERROR = "The mount import status is unavailable.";

function makeStatus(overrides: Partial<MountImportStatus> = {}): MountImportStatus {
  return {
    status: "done",
    running: false,
    outcome: "registered",
    mode: "copy",
    root: "/mnt/models",
    reason: null,
    error: null,
    started_at: 1_700_000_000,
    finished_at: 1_700_000_042,
    registered: 4,
    known: 2,
    ignored: 1,
    failed: 0,
    rejected: 0,
    discovered: 7,
    skipped: 3,
    errors: [],
    ...overrides,
  };
}

const queued = makeStatus({ status: "queued", outcome: null, finished_at: null, running: true });
const running = makeStatus({ status: "running", outcome: null, finished_at: null, running: true });
const succeeded = makeStatus();

/** A promise whose settlement the test drives, for ordering two responses. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

/** Lets every pending promise chain settle without advancing the clock. */
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
  getMountImportStatusMock.mockReset();
  getMountImportStatusMock.mockResolvedValue(succeeded);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("useMountImport polling (AC-1)", () => {
  it("polls every 1.5s while the newest row is running", async () => {
    getMountImportStatusMock.mockResolvedValue(running);

    renderHook(() => useMountImport({ enabled: true }));
    await settle();

    expect(getMountImportStatusMock).toHaveBeenCalledTimes(1);

    for (let ticked = 1; ticked <= 3; ticked += 1) {
      await tick();
      expect(getMountImportStatusMock).toHaveBeenCalledTimes(1 + ticked);
    }
  });

  it("polls a queued row too, because nothing is walking yet", async () => {
    getMountImportStatusMock.mockResolvedValueOnce(queued).mockResolvedValue(running);

    const { result } = renderHook(() => useMountImport({ enabled: true }));
    await settle();
    expect(result.current.status?.status).toBe("queued");

    await tick();
    expect(getMountImportStatusMock).toHaveBeenCalledTimes(2);
    expect(result.current.status?.status).toBe("running");
  });

  it("keeps the badge honest after a failed poll, then clears the error on the next success", async () => {
    getMountImportStatusMock
      .mockResolvedValueOnce(running)
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValue(running);

    const { result } = renderHook(() => useMountImport({ enabled: true }));
    await settle();
    expect(result.current.error).toBeNull();

    await tick();
    // The failure changed no row state. Without the `pollTick` re-arm the chain
    // would die here and the badge would stick at 1 forever.
    expect(result.current.error).toBe(ENTRY_ERROR);
    expect(getMountImportStatusMock).toHaveBeenCalledTimes(2);
    // The last good row is still the one the panel shows.
    expect(result.current.status).toEqual(running);

    await tick();
    expect(getMountImportStatusMock).toHaveBeenCalledTimes(3);
    expect(result.current.error).toBeNull();
  });
});

describe("useMountImport idle does not poll (AC-6)", () => {
  it("makes zero requests when the queue is never opened", async () => {
    renderHook(() => useMountImport({ enabled: false }));

    // Ten minutes of clock: every chance to fire a stray timer.
    await tick(600_000);

    expect(getMountImportStatusMock).not.toHaveBeenCalled();
  });

  it("makes one entry read and then nothing for a finished walk", async () => {
    renderHook(() => useMountImport({ enabled: true }));
    await settle();

    expect(getMountImportStatusMock).toHaveBeenCalledTimes(1);

    await tick(60_000);

    expect(getMountImportStatusMock).toHaveBeenCalledTimes(1);
  });

  it("stops polling once a running row flips to done", async () => {
    getMountImportStatusMock.mockResolvedValueOnce(running).mockResolvedValue(succeeded);

    const { result } = renderHook(() => useMountImport({ enabled: true }));
    await settle();
    expect(result.current.status?.status).toBe("running");

    // The poll that returns the finished row is the last one.
    await tick();
    expect(result.current.status?.status).toBe("done");
    expect(getMountImportStatusMock).toHaveBeenCalledTimes(2);

    await tick(60_000);
    expect(getMountImportStatusMock).toHaveBeenCalledTimes(2);
  });
});

describe("useMountImport activation", () => {
  it("refetches on every re-entry", async () => {
    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useMountImport({ enabled }),
      { initialProps: { enabled: false } },
    );
    expect(getMountImportStatusMock).not.toHaveBeenCalled();

    rerender({ enabled: true });
    await settle();
    expect(getMountImportStatusMock).toHaveBeenCalledTimes(1);

    rerender({ enabled: false });
    await settle();
    rerender({ enabled: true });
    await settle();

    expect(getMountImportStatusMock).toHaveBeenCalledTimes(2);
  });

  it("fetches the entry read exactly once under StrictMode", async () => {
    const { result } = renderHook(() => useMountImport({ enabled: true }), {
      wrapper: React.StrictMode,
    });

    await settle();

    expect(getMountImportStatusMock).toHaveBeenCalledTimes(1);
    expect(result.current.status).toEqual(succeeded);
  });

  it("lands the empty status for a ledger with no walk", async () => {
    const empty = makeStatus({
      status: null,
      outcome: null,
      mode: null,
      root: null,
      started_at: null,
      finished_at: null,
      discovered: 0,
      registered: 0,
      known: 0,
      ignored: 0,
      failed: 0,
      rejected: 0,
      skipped: 0,
    });
    getMountImportStatusMock.mockResolvedValue(empty);

    const { result } = renderHook(() => useMountImport({ enabled: true }));
    await settle();

    expect(getMountImportStatusMock).toHaveBeenCalledTimes(1);
    // "No walk has happened" is a fact, so it is loaded - not a dash.
    expect(result.current.loaded).toBe(true);
    expect(result.current.status).toEqual(empty);
  });
});

describe("useMountImport response ordering", () => {
  it("lets the newest request win when the older one answers last", async () => {
    const older = deferred<MountImportStatus>();
    const newer = deferred<MountImportStatus>();
    getMountImportStatusMock
      .mockResolvedValueOnce(running)
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);

    const { result } = renderHook(() => useMountImport({ enabled: true }));
    await settle();

    // Two manual refreshes overlap before either answers.
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
    expect(result.current.status).toEqual(succeeded);

    // The superseded response lands late and must not paint over the newer one.
    await act(async () => {
      older.resolve(running);
    });

    expect(result.current.status).toEqual(succeeded);
  });
});

describe("useMountImport failure handling", () => {
  it("reports an unauthorized poll once, records no error and stops the chain", async () => {
    getMountImportStatusMock
      .mockResolvedValueOnce(running)
      .mockRejectedValueOnce(new UnauthorizedError());
    const onUnauthorized = vi.fn();

    const { result } = renderHook(() => useMountImport({ enabled: true, onUnauthorized }));
    await settle();
    expect(getMountImportStatusMock).toHaveBeenCalledTimes(1);

    await tick();

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    // A revoked session is not a poll failure: no error string for the panel.
    expect(result.current.error).toBeNull();
    expect(getMountImportStatusMock).toHaveBeenCalledTimes(2);

    // Armed is cleared, so the next poll never fires.
    await tick(60_000);
    expect(getMountImportStatusMock).toHaveBeenCalledTimes(2);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("turns an unauthorized entry fetch into a sign-out without an error string", async () => {
    getMountImportStatusMock.mockRejectedValue(new UnauthorizedError());
    const onUnauthorized = vi.fn();

    const { result } = renderHook(() => useMountImport({ enabled: true, onUnauthorized }));
    await settle();

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBeNull();
    expect(result.current.status).toBeNull();
  });

  it("keeps an entry failure visible and retryable through refresh", async () => {
    getMountImportStatusMock.mockRejectedValue(new Error("boom"));

    const { result } = renderHook(() => useMountImport({ enabled: true }));
    await settle();

    expect(result.current.error).toBe(ENTRY_ERROR);
    expect(result.current.loaded).toBe(false);
    expect(result.current.loading).toBe(false);

    getMountImportStatusMock.mockResolvedValue(succeeded);
    await act(async () => {
      await result.current.refresh();
    });

    expect(result.current.error).toBeNull();
    expect(result.current.loaded).toBe(true);
    expect(result.current.status).toEqual(succeeded);
  });
});

describe("useMountImport in-flight flags", () => {
  it("shows refreshing only for the operator's refresh, never for a background poll", async () => {
    const entry = deferred<MountImportStatus>();
    getMountImportStatusMock.mockReturnValueOnce(entry.promise).mockResolvedValue(running);

    const { result } = renderHook(() => useMountImport({ enabled: true }));
    // The entry fetch is a background load: no operator is waiting on a button.
    expect(result.current.refreshing).toBe(false);

    await act(async () => {
      entry.resolve(running);
    });
    await settle();
    expect(result.current.refreshing).toBe(false);

    const manual = deferred<MountImportStatus>();
    getMountImportStatusMock.mockReturnValueOnce(manual.promise);
    act(() => {
      void result.current.refresh();
    });
    expect(result.current.refreshing).toBe(true);

    await act(async () => {
      manual.resolve(running);
    });
    await settle();
    expect(result.current.refreshing).toBe(false);

    // A poll of a running walk settles without ever touching the flag.
    await tick();
    expect(result.current.refreshing).toBe(false);
    expect(getMountImportStatusMock).toHaveBeenCalledTimes(3);
  });

  it("reports loaded and lastRefreshedAt only once a fetch succeeded", async () => {
    const entry = deferred<MountImportStatus>();
    getMountImportStatusMock.mockReturnValueOnce(entry.promise);

    const { result } = renderHook(() => useMountImport({ enabled: true }));

    expect(result.current.loaded).toBe(false);
    expect(result.current.lastRefreshedAt).toBeNull();
    expect(result.current.loading).toBe(true);

    await act(async () => {
      entry.resolve(succeeded);
    });
    await settle();

    expect(result.current.loaded).toBe(true);
    expect(result.current.loading).toBe(false);
    expect(result.current.lastRefreshedAt).toEqual(expect.any(Number));
  });

  it("early-returns from refresh until the hook has activated once", async () => {
    const { result } = renderHook(() => useMountImport({ enabled: false }));

    await act(async () => {
      await result.current.refresh();
    });

    expect(getMountImportStatusMock).not.toHaveBeenCalled();
  });
});
