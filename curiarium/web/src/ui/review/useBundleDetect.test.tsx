// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";

vi.mock("../../lib/api/bundles", () => ({
  getBundleReport: vi.fn(),
  createBundles: vi.fn(),
  setBundleDismissed: vi.fn(),
}));

import {
  createBundles,
  getBundleReport,
  setBundleDismissed,
  type BundleDetectItem,
  type BundleReport,
} from "../../lib/api/bundles";
import { UnauthorizedError } from "../../lib/api/http";
import { useBundleDetect } from "./useBundleDetect";

const getBundleReportMock = vi.mocked(getBundleReport);
const createBundlesMock = vi.mocked(createBundles);
const setBundleDismissedMock = vi.mocked(setBundleDismissed);

function makeBundle(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return {
    key: "Moai statue",
    rel_path: "Moai statue",
    title: "Moai statue",
    name_suggestion: "Moai statue",
    creator: "sculpteo",
    licence: "CC BY",
    licence_url: "https://creativecommons.org/licenses/by/4.0/",
    source_url: "https://www.thingiverse.com/thing:2493386",
    detector: "thingiverse-readme",
    confidence: "high",
    metadata_shape: "root-docs",
    description_excerpt: "A Moai statue.",
    model_count: 3,
    image_count: 5,
    text_count: 2,
    other_count: 0,
    cover: { asset_id: "asset-1", rel_path: "images/hero.jpg", reason: "largest-image" },
    assets_total: 8,
    status: "ready",
    blocked_reason: null,
    archive: null,
    existing_project: null,
    dismissed: false,
    ...overrides,
  };
}

const emptyCounts = {
  total: 0,
  high: 0,
  low: 0,
  ready: 0,
  blocked: 0,
  dismissed: 0,
  already_created: 0,
};

function makeReport(bundles: BundleDetectItem[] = []): BundleReport {
  return {
    root: "",
    truncated: false,
    counts: { ...emptyCounts },
    bundles,
    stale_dismissals: [],
  };
}

const report = makeReport([makeBundle()]);

/** A promise whose settlement the test drives, for ordering two responses. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

beforeEach(() => {
  getBundleReportMock.mockReset();
  getBundleReportMock.mockResolvedValue(report);
  createBundlesMock.mockReset();
  createBundlesMock.mockResolvedValue({ created: 1, skipped: 0, blocked: 0, failed: 0, results: [] });
  setBundleDismissedMock.mockReset();
  setBundleDismissedMock.mockResolvedValue({ dismissed: ["Moai statue"], stale_dismissals: [] });
});

afterEach(() => {
  cleanup();
  // Restores the Storage.prototype.setItem spy even when an assertion threw,
  // so a red run cannot leak a spy into the tests after it.
  vi.restoreAllMocks();
});

describe("useBundleDetect", () => {
  it("fetches once with the full walk when the queue opens", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }));

    await waitFor(() => expect(result.current.report).toEqual(report));

    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
    expect(getBundleReportMock).toHaveBeenCalledWith({ includeDismissed: true, limit: 1000 });
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("stays inert while the role cannot write", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: false }));

    // Give a request every chance to fire before asserting it never did.
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(getBundleReportMock).not.toHaveBeenCalled();
    expect(result.current.report).toBeNull();
  });

  it("walks the library once under StrictMode, and the report still lands", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }), {
      wrapper: React.StrictMode,
    });

    await waitFor(() => expect(result.current.report).toEqual(report));

    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
  });

  it("lets the newest request win, so a superseded response cannot land", async () => {
    const first = deferred<BundleReport>();
    const second = deferred<BundleReport>();
    getBundleReportMock
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const older = makeReport([makeBundle({ key: "Older", title: "Older walk" })]);
    const newer = makeReport([makeBundle({ key: "Newer", title: "Newer walk" })]);

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(1));

    // A manual refresh starts while the first walk is still on the wire.
    act(() => {
      void result.current.refresh();
    });
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(2));

    // The newer request answers first...
    await act(async () => {
      second.resolve(newer);
    });
    await waitFor(() => expect(result.current.report).toEqual(newer));

    // ...and the older one's late answer is dropped.
    await act(async () => {
      first.resolve(older);
    });

    expect(result.current.report).toEqual(newer);
  });

  it("refetches on every re-entry, and never polls", async () => {
    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useBundleDetect({ enabled }),
      { initialProps: { enabled: false } },
    );
    expect(getBundleReportMock).not.toHaveBeenCalled();

    rerender({ enabled: true });
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(1));

    rerender({ enabled: false });
    rerender({ enabled: true });
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(2));

    // No timer, no poll: the count stays at two while the queue stays open.
    await new Promise(resolve => setTimeout(resolve, 50));
    expect(getBundleReportMock).toHaveBeenCalledTimes(2);
  });

  it("creates and then refetches the report", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    await act(async () => {
      await result.current.create([{ key: "Moai statue", name: "Moai" }]);
    });

    expect(createBundlesMock).toHaveBeenCalledWith([{ key: "Moai statue", name: "Moai" }]);
    expect(getBundleReportMock).toHaveBeenCalledTimes(2);
    expect(result.current.busy).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("dismisses and then refetches the report", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    await act(async () => {
      await result.current.dismiss(["Moai statue"], true);
    });

    expect(setBundleDismissedMock).toHaveBeenCalledWith(["Moai statue"], true);
    expect(getBundleReportMock).toHaveBeenCalledTimes(2);
    expect(result.current.busy).toBe(false);
  });

  it("keeps the show-dismissed toggle", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    expect(result.current.showDismissed).toBe(false);

    act(() => {
      result.current.setShowDismissed(true);
    });

    expect(result.current.showDismissed).toBe(true);
  });

  it("keeps the show-created toggle", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    expect(result.current.showCreated).toBe(false);

    act(() => {
      result.current.setShowCreated(true);
    });

    expect(result.current.showCreated).toBe(true);
  });

  // The persistence decision, pinned for both toggles: session state only. A
  // persisted Show created would put the settled rows back in the queue for
  // whoever ever flipped it, which is the bug this queue avoids.
  it.each([
    ["showDismissed", "setShowDismissed"],
    ["showCreated", "setShowCreated"],
  ] as const)(
    "keeps %s across queue re-entry and writes no localStorage key",
    async (field, setter) => {
      const setItem = vi.spyOn(Storage.prototype, "setItem");
      const { result, rerender } = renderHook(
        ({ enabled }: { enabled: boolean }) => useBundleDetect({ enabled }),
        { initialProps: { enabled: false } },
      );
      expect(result.current[field]).toBe(false);

      act(() => {
        result.current[setter](true);
      });
      expect(result.current[field]).toBe(true);

      rerender({ enabled: true });
      await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(1));
      rerender({ enabled: false });
      rerender({ enabled: true });

      // Still on after the round trip, because leaving the queue is not a reset.
      expect(result.current[field]).toBe(true);
      // Only a page reload resets it, because nothing was ever stored.
      expect(setItem).not.toHaveBeenCalled();
    },
  );

  it("turns a failed report into an error and never throws", async () => {
    getBundleReportMock.mockRejectedValue(new Error("boom"));

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));

    await waitFor(() => expect(result.current.error).toBe("Bundle detection is unavailable."));
    expect(result.current.report).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it("turns a failed create into an error that keeps the server's message", async () => {
    createBundlesMock.mockRejectedValue(new Error("A project with that name already exists"));

    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    await act(async () => {
      await result.current.create([{ key: "Moai statue", name: "Moai" }]);
    });

    expect(result.current.error).toBe("A project with that name already exists");
    expect(result.current.busy).toBe(false);
    // A failed mutation must not refetch over the error it just recorded.
    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
  });

  it("reports an unauthorized failure to the caller", async () => {
    getBundleReportMock.mockRejectedValue(new UnauthorizedError());
    const onUnauthorized = vi.fn();

    const { result } = renderHook(() => useBundleDetect({ enabled: true, onUnauthorized }));

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    expect(result.current.error).toBe("Bundle detection is unavailable.");
  });

  it("stamps lastRefreshedAt only on a successful fetch", async () => {
    const { result } = renderHook(() => useBundleDetect({ enabled: true }));
    expect(result.current.lastRefreshedAt).toBeNull();

    await waitFor(() => expect(result.current.report).toEqual(report));
    expect(result.current.lastRefreshedAt).toEqual(expect.any(Number));
    const stamped = result.current.lastRefreshedAt;

    createBundlesMock.mockRejectedValue(new Error("nope"));
    await act(async () => {
      await result.current.create([{ key: "Moai statue", name: "Moai" }]);
    });

    expect(result.current.lastRefreshedAt).toBe(stamped);
  });
});
