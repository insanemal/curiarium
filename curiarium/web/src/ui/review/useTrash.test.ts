// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";

// Domain modules, not the `../../lib/api` barrel: the hook itself imports from
// them, and a barrel mock that forgot one export would make every assertion
// here vacuous.
vi.mock("../../lib/api/trash", () => ({
  getTrash: vi.fn(),
  purgeTrash: vi.fn(),
}));

// The hook matches `err instanceof UnauthorizedError`, so the test has to
// construct the same class the hook sees.
vi.mock("../../lib/api/http", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
}));

import { UnauthorizedError } from "../../lib/api/http";
import { getTrash, purgeTrash, type TrashList } from "../../lib/api/trash";
import { useTrash } from "./useTrash";

const getTrashMock = vi.mocked(getTrash);
const purgeTrashMock = vi.mocked(purgeTrash);

const LIST: TrashList = {
  entries: [
    {
      path: ".trash/20261027-abc-old-folder",
      name: "old-folder",
      size: 2048,
      mtime: "2026-10-27T10:00:00Z",
    },
  ],
  total_size: 2048,
};

const EMPTY: TrashList = { entries: [], total_size: 0 };

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

beforeEach(() => {
  vi.useFakeTimers();
  getTrashMock.mockReset();
  purgeTrashMock.mockReset();
  getTrashMock.mockResolvedValue(LIST);
  purgeTrashMock.mockResolvedValue({ purged: 1, bytes: 2048, failed: [] });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("useTrash activation", () => {
  it("reads nothing while the queue is never opened", async () => {
    const { result } = renderHook(() => useTrash({ enabled: false }));

    // Ten minutes of clock: every chance to fire a stray timer or fetch.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600_000);
    });

    expect(getTrashMock).not.toHaveBeenCalled();
    expect(result.current.trash).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it("reads exactly once per activation and reports when it settled", async () => {
    const { result } = renderHook(() => useTrash({ enabled: true }));
    expect(result.current.loading).toBe(true);

    await settle();

    expect(getTrashMock).toHaveBeenCalledTimes(1);
    expect(result.current.trash).toEqual(LIST);
    expect(result.current.loading).toBe(false);
    expect(result.current.lastRefreshedAt).toEqual(expect.any(Number));
  });

  it("reads exactly once under React.StrictMode", async () => {
    const { result } = renderHook(() => useTrash({ enabled: true }), {
      wrapper: React.StrictMode,
    });

    await settle();

    expect(getTrashMock).toHaveBeenCalledTimes(1);
    expect(result.current.trash).toEqual(LIST);
  });

  it("refetches when the queue is left and opened again", async () => {
    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useTrash({ enabled }),
      { initialProps: { enabled: true } },
    );
    await settle();
    expect(getTrashMock).toHaveBeenCalledTimes(1);

    rerender({ enabled: false });
    await settle();
    rerender({ enabled: true });
    await settle();

    expect(getTrashMock).toHaveBeenCalledTimes(2);
  });

  it("never polls, however long the queue stays open", async () => {
    renderHook(() => useTrash({ enabled: true }));
    await settle();
    expect(getTrashMock).toHaveBeenCalledTimes(1);

    // `GET /trash` walks the whole storage tree to size each entry, so a timer
    // here would re-walk it forever. One read, then silence.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    expect(getTrashMock).toHaveBeenCalledTimes(1);
  });
});

describe("useTrash response ordering", () => {
  it("lets the newest read win when the older one answers last", async () => {
    const older = deferred<TrashList>();
    const newer = deferred<TrashList>();
    getTrashMock
      .mockResolvedValueOnce(LIST)
      .mockReturnValueOnce(older.promise)
      .mockReturnValueOnce(newer.promise);

    const { result } = renderHook(() => useTrash({ enabled: true }));
    await settle();

    // Two refreshes overlap before either answers.
    act(() => {
      void result.current.refresh();
    });
    await settle();
    act(() => {
      void result.current.refresh();
    });
    await settle();

    await act(async () => {
      newer.resolve(EMPTY);
    });
    expect(result.current.trash).toEqual(EMPTY);

    // The superseded read lands late and must not repaint.
    await act(async () => {
      older.resolve(LIST);
    });

    expect(result.current.trash).toEqual(EMPTY);
  });
});

describe("useTrash read failures", () => {
  it("keeps the rows and the refresh age when a refresh fails", async () => {
    const { result } = renderHook(() => useTrash({ enabled: true }));
    await settle();
    expect(result.current.trash).toEqual(LIST);
    const refreshedAt = result.current.lastRefreshedAt;

    getTrashMock.mockRejectedValueOnce(new Error("Failed to load trash"));
    await act(async () => {
      await result.current.refresh();
    });

    expect(result.current.trash).toEqual(LIST);
    expect(result.current.error).toBe("Failed to load trash");
    expect(result.current.lastRefreshedAt).toBe(refreshedAt);
    expect(result.current.refreshing).toBe(false);
  });

  it("signs out on a 401 instead of reporting an error", async () => {
    getTrashMock.mockRejectedValueOnce(new UnauthorizedError());
    const onUnauthorized = vi.fn();

    const { result } = renderHook(() => useTrash({ enabled: true, onUnauthorized }));
    await settle();

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBeNull();
    expect(result.current.trash).toBeNull();
    expect(result.current.lastRefreshedAt).toBeNull();
  });
});

describe("useTrash purge", () => {
  it("keeps the rows and sets only purgeError when the purge fails", async () => {
    const { result } = renderHook(() => useTrash({ enabled: true }));
    await settle();
    expect(result.current.trash).toEqual(LIST);

    purgeTrashMock.mockRejectedValueOnce(new Error("Multi-factor authentication required"));

    let ok = true;
    await act(async () => {
      ok = await result.current.purge();
    });

    expect(ok).toBe(false);
    expect(result.current.trash).toEqual(LIST);
    // One channel for two failures is what let a failed read render as a failed
    // purge and a failed purge as an empty trash: `error` stays untouched.
    expect(result.current.error).toBeNull();
    expect(result.current.purgeError).toBe("Multi-factor authentication required");
    expect(result.current.purging).toBe(false);
  });

  it("records the result and refetches exactly once after a successful purge", async () => {
    const { result } = renderHook(() => useTrash({ enabled: true }));
    await settle();
    const resultBefore = { purged: 1, bytes: 2048, failed: [] };
    purgeTrashMock.mockResolvedValueOnce(resultBefore);

    let ok = false;
    await act(async () => {
      ok = await result.current.purge();
    });

    expect(ok).toBe(true);
    expect(result.current.purgeResult).toEqual(resultBefore);
    expect(result.current.purgeError).toBeNull();
    // The entry read plus the one post-purge read.
    expect(getTrashMock).toHaveBeenCalledTimes(2);
  });

  it("signs out on a 401 instead of reporting the purge as failed", async () => {
    const onUnauthorized = vi.fn();
    const { result } = renderHook(() => useTrash({ enabled: true, onUnauthorized }));
    await settle();
    expect(result.current.trash).toEqual(LIST);

    purgeTrashMock.mockRejectedValueOnce(new UnauthorizedError());

    let ok = true;
    await act(async () => {
      ok = await result.current.purge();
    });

    expect(ok).toBe(false);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(result.current.purgeError).toBeNull();
    expect(result.current.error).toBeNull();
    expect(result.current.trash).toEqual(LIST);
  });
});
