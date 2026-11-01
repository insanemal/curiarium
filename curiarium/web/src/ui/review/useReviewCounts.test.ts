// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, renderHook } from "@testing-library/react";

import type { BundleDetectItem, BundleReport } from "../../lib/api/bundles";
import type { BundleDetectState } from "./useBundleDetect";
import { bundleBadgeCount, countAwaitingText, formatReviewCount, useReviewCounts } from "./useReviewCounts";

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

function makeReport(bundles: BundleDetectItem[] = [], truncated = false): BundleReport {
  return {
    root: "",
    truncated,
    counts: {
      total: bundles.length,
      high: 0,
      low: 0,
      ready: 0,
      blocked: 0,
      dismissed: 0,
      already_created: 0,
    },
    bundles,
    stale_dismissals: [],
  };
}

function makeState(report: BundleReport | null): BundleDetectState {
  return {
    report,
    loading: false,
    busy: false,
    error: null,
    lastRefreshedAt: null,
    showDismissed: false,
    setShowDismissed: () => undefined,
    refresh: async () => undefined,
    create: async () => undefined,
    dismiss: async () => undefined,
  };
}

afterEach(() => {
  cleanup();
});

describe("bundleBadgeCount", () => {
  it("returns null for an unknown report", () => {
    expect(bundleBadgeCount(null)).toBeNull();
  });

  it("returns null for a truncated walk even when the rows look complete", () => {
    // A withheld row must never look like a real zero-or-more count.
    expect(bundleBadgeCount(makeReport([makeBundle()], true))).toBeNull();
  });

  it("returns zero for a genuinely empty queue", () => {
    expect(bundleBadgeCount(makeReport())).toBe(0);
  });

  it("excludes dismissed rows", () => {
    expect(bundleBadgeCount(makeReport([makeBundle({ dismissed: true })]))).toBe(0);
  });

  it("excludes rows that already became a project", () => {
    expect(bundleBadgeCount(makeReport([makeBundle({ existing_project: "project-7" })]))).toBe(0);
  });

  it("counts blocked rows", () => {
    expect(bundleBadgeCount(makeReport([makeBundle({ status: "blocked" })]))).toBe(1);
  });

  it("excludes a dismissed and already-created row exactly once", () => {
    const settled = makeBundle({
      key: "Settled",
      dismissed: true,
      existing_project: "project-7",
    });
    expect(bundleBadgeCount(makeReport([settled, makeBundle()]))).toBe(1);
  });

  it("counts three undismissed rows when one is already created, and ignores a dismissed row", () => {
    const rows = [
      makeBundle({ key: "Open 1" }),
      makeBundle({ key: "Open 2" }),
      makeBundle({ key: "Created", existing_project: "project-7" }),
      makeBundle({ key: "Dismissed", dismissed: true }),
    ];
    expect(bundleBadgeCount(makeReport(rows))).toBe(2);
  });
});

describe("formatReviewCount", () => {
  it("renders an unknown count as an en dash", () => {
    expect(formatReviewCount(null)).toBe("\u2013");
  });

  it("renders a known count as its number", () => {
    expect(formatReviewCount(0)).toBe("0");
    expect(formatReviewCount(7)).toBe("7");
  });
});

describe("countAwaitingText", () => {
  // One wording for the nav label, its tooltip and the queue header: the same
  // number must not be described two ways in two places.
  it("says so when the count is unknown", () => {
    expect(countAwaitingText(null, "bundle")).toBe("Count unavailable");
  });

  it("uses the singular for one and the plural otherwise", () => {
    expect(countAwaitingText(1, "bundle")).toBe("1 bundle awaiting a decision");
    expect(countAwaitingText(0, "bundle")).toBe("0 bundles awaiting a decision");
    expect(countAwaitingText(12, "dismissal")).toBe("12 dismissals awaiting a decision");
  });
});

describe("useReviewCounts", () => {
  it("keeps the return object identity across a rerender with the same report", () => {
    const report = makeReport([makeBundle()]);
    const { result, rerender } = renderHook(
      ({ state }: { state: BundleDetectState }) => useReviewCounts(state),
      { initialProps: { state: makeState(report) } },
    );

    const initial = result.current;
    expect(initial.bundles).toBe(1);

    rerender({ state: makeState(report) });

    expect(result.current).toBe(initial);
  });
});
