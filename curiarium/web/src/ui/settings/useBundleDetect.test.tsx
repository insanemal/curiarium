// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";

vi.mock("../../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  getBundleReport: vi.fn(),
  createBundles: vi.fn(),
  setBundleDismissed: vi.fn(),
}));

import {
  UnauthorizedError,
  createBundles,
  getBundleReport,
  setBundleDismissed,
  type BundleDetectItem,
  type BundleReport,
} from "../../lib/api";
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
});

describe("useBundleDetect", () => {
  it("fetches once with dismissed rows included when the section opens", async () => {
    const { result } = renderHook(() => useBundleDetect({ section: "imports", enabled: true }));

    await waitFor(() => expect(result.current.report).toEqual(report));

    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
    expect(getBundleReportMock).toHaveBeenCalledWith({ includeDismissed: true });
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("stays inert while the role cannot write", async () => {
    const { result } = renderHook(() => useBundleDetect({ section: "imports", enabled: false }));

    // Give a request every chance to fire before asserting it never did.
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(getBundleReportMock).not.toHaveBeenCalled();
    expect(result.current.report).toBeNull();
  });

  it("stays inert in every other settings section", async () => {
    const { result } = renderHook(() => useBundleDetect({ section: "storage", enabled: true }));

    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(getBundleReportMock).not.toHaveBeenCalled();
  });

  it("fetches again when the section is reopened, and never polls", async () => {
    const { rerender } = renderHook(
      ({ section }: { section: "storage" | "imports" }) =>
        useBundleDetect({ section, enabled: true }),
      { initialProps: { section: "storage" as "storage" | "imports" } },
    );
    expect(getBundleReportMock).not.toHaveBeenCalled();

    rerender({ section: "imports" });
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(1));

    // No timer, no poll: the count stays at one while the section stays open.
    await new Promise(resolve => setTimeout(resolve, 50));
    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
  });

  it("creates and then refetches the report", async () => {
    const { result } = renderHook(() => useBundleDetect({ section: "imports", enabled: true }));
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
    const { result } = renderHook(() => useBundleDetect({ section: "imports", enabled: true }));
    await waitFor(() => expect(result.current.report).toEqual(report));

    await act(async () => {
      await result.current.dismiss(["Moai statue"], true);
    });

    expect(setBundleDismissedMock).toHaveBeenCalledWith(["Moai statue"], true);
    expect(getBundleReportMock).toHaveBeenCalledTimes(2);
    expect(result.current.busy).toBe(false);
  });

  it("keeps the show-dismissed toggle", async () => {
    const { result } = renderHook(() => useBundleDetect({ section: "imports", enabled: true }));
    expect(result.current.showDismissed).toBe(false);

    act(() => {
      result.current.setShowDismissed(true);
    });

    expect(result.current.showDismissed).toBe(true);
  });

  it("turns a failed report into an error and never throws", async () => {
    getBundleReportMock.mockRejectedValue(new Error("boom"));

    const { result } = renderHook(() => useBundleDetect({ section: "imports", enabled: true }));

    await waitFor(() => expect(result.current.error).toBe("Bundle detection is unavailable."));
    expect(result.current.report).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it("turns a failed create into an error that keeps the server's message", async () => {
    createBundlesMock.mockRejectedValue(new Error("A project with that name already exists"));

    const { result } = renderHook(() => useBundleDetect({ section: "imports", enabled: true }));
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

    const { result } = renderHook(() =>
      useBundleDetect({ section: "imports", enabled: true, onUnauthorized }),
    );

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    expect(result.current.error).toBe("Bundle detection is unavailable.");
  });
});
