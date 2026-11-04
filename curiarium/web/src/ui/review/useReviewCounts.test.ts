// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, renderHook } from "@testing-library/react";

import type { AssetCounts } from "../../lib/api/assets";
import type { BundleDetectItem, BundleReport } from "../../lib/api/bundles";
import type { ImportRunDetail } from "../../lib/api/settings";
import type { TrashEntry, TrashList } from "../../lib/api/trash";
import { needsDecision } from "./bundleRows";
import type { BundleDetectState } from "./useBundleDetect";
import type { ImportRunsState } from "./useImportRuns";
import {
  bundleBadgeCount,
  countAwaitingText,
  describeQueueCount,
  formatReviewCount,
  importsBadgeCount,
  missingBadgeCount,
  trashBadgeCount,
  useReviewCounts,
} from "./useReviewCounts";

function makeRun(overrides: Partial<ImportRunDetail> = {}): ImportRunDetail {
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
    errors: [],
    ...overrides,
  };
}

/**
 * The full state shape, so a test states only what it is about. The default is
 * "nothing loaded yet": the imports count is unknown, not zero.
 */
function importsState(overrides: Partial<ImportRunsState> = {}): ImportRunsState {
  return {
    run: null,
    loading: false,
    refreshing: false,
    loaded: false,
    error: null,
    lastRefreshedAt: null,
    refresh: async () => {},
    ...overrides,
  };
}

/** The whole-library counts App already fetched; only `missing` is ours. */
function makeCounts(overrides: Partial<AssetCounts> = {}): AssetCounts {
  return { total: 40, unfiled: 5, missing: 3, ...overrides };
}

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
    cover: {
      asset_id: "asset-1",
      rel_path: "images/hero.jpg",
      reason: "largest-image",
      url: "/file/asset-1/hero.jpg",
      thumb_url: "/thumb/asset-1.jpg?v=1",
    },
    assets_total: 8,
    status: "ready",
    blocked_reason: null,
    archive: null,
    existing_project_id: null,
    existing_project_name: null,
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

function makeTrashEntry(name: string): TrashEntry {
  return { path: `.trash/${name}`, name, size: 1024, mtime: "2026-11-01T10:00:00Z" };
}

/** A trash listing sized like `GET /trash` returns; `null` is "no read yet". */
function makeTrash(entries: TrashEntry[] = []): TrashList {
  return {
    entries,
    total_size: entries.reduce((sum, entry) => sum + entry.size, 0),
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
    showCreated: false,
    setShowCreated: () => undefined,
    refresh: async () => undefined,
    create: async () => undefined,
    dismiss: async () => undefined,
    // Extraction settling is the queue view's concern; this hook's fixtures
    // report a settled scan so no timer is ever armed here.
    afterExtract: async () => "settled" as const,
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
    const created = makeBundle({
      existing_project_id: "project-7",
      existing_project_name: "Project 7",
    });
    expect(bundleBadgeCount(makeReport([created]))).toBe(0);
  });

  it("counts blocked rows", () => {
    expect(bundleBadgeCount(makeReport([makeBundle({ status: "blocked" })]))).toBe(1);
  });

  it("excludes a dismissed and already-created row exactly once", () => {
    const settled = makeBundle({
      key: "Settled",
      dismissed: true,
      existing_project_id: "project-7",
      existing_project_name: "Project 7",
    });
    expect(bundleBadgeCount(makeReport([settled, makeBundle()]))).toBe(1);
  });

  it("counts three undismissed rows when one is already created, and ignores a dismissed row", () => {
    const rows = [
      makeBundle({ key: "Open 1" }),
      makeBundle({ key: "Open 2" }),
      makeBundle({
        key: "Created",
        existing_project_id: "project-7",
        existing_project_name: "Project 7",
      }),
      makeBundle({ key: "Dismissed", dismissed: true }),
    ];
    expect(bundleBadgeCount(makeReport(rows))).toBe(2);
  });

  it("equals the count of the queue's own predicate on a mixed report", () => {
    // The badge IS the list's rule: one predicate, one array, one number. A
    // truncated report is the one deliberate exception (see `null` above).
    const report = makeReport([
      makeBundle({ key: "Pending" }),
      makeBundle({ key: "Blocked", status: "blocked" }),
      makeBundle({
        key: "Created",
        existing_project_id: "project-7",
        existing_project_name: "Project 7",
      }),
      makeBundle({ key: "Dismissed", dismissed: true }),
      makeBundle({
        key: "Settled",
        dismissed: true,
        existing_project_id: "project-7",
        existing_project_name: "Project 7",
      }),
    ]);

    expect(bundleBadgeCount(report)).toBe(report.bundles.filter(needsDecision).length);
    expect(bundleBadgeCount(report)).toBe(2);
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

describe("importsBadgeCount (AC-8)", () => {
  it("returns null before the first successful fetch, even with no run", () => {
    // `run === null` cannot tell "the first fetch failed" from "no run has ever
    // happened", so `loaded` is what decides between a dash and a zero.
    expect(importsBadgeCount(importsState())).toBeNull();
  });

  it("returns 1 while the newest run is running", () => {
    expect(
      importsBadgeCount(
        importsState({ loaded: true, run: makeRun({ status: "running", finished_at: null }) }),
      ),
    ).toBe(1);
  });

  it("returns 0 for a loaded queue that has never had a run", () => {
    expect(importsBadgeCount(importsState({ loaded: true, run: null }))).toBe(0);
  });

  it("returns 0 once the loaded run has succeeded, so a zero means idle", () => {
    expect(importsBadgeCount(importsState({ loaded: true, run: makeRun() }))).toBe(0);
  });
});

describe("describeQueueCount", () => {
  it("keeps the bundle wording unchanged", () => {
    expect(describeQueueCount("bundles", null)).toBe("Count unavailable");
    expect(describeQueueCount("bundles", 0)).toBe("0 bundles awaiting a decision");
    expect(describeQueueCount("bundles", 1)).toBe("1 bundle awaiting a decision");
    expect(describeQueueCount("bundles", 12)).toBe("12 bundles awaiting a decision");
  });

  it("states the imports fact, because imports count a boolean not a queue", () => {
    expect(describeQueueCount("imports", null)).toBe("Count unavailable");
    expect(describeQueueCount("imports", 0)).toBe("No import currently running");
    expect(describeQueueCount("imports", 1)).toBe("1 import running");
    expect(describeQueueCount("imports", 2)).toBe("2 imports running");
  });

  // The missing queue counts decisions outstanding, like bundles: each flagged
  // file needs somebody to restore it or forget it.
  it("words the missing count as decisions outstanding", () => {
    expect(describeQueueCount("missing", 3)).toBe("3 missing files awaiting a decision");
    expect(describeQueueCount("missing", 1)).toBe("1 missing file awaiting a decision");
    expect(describeQueueCount("missing", null)).toBe("Count unavailable");
  });

  // A trashed entry awaits nothing, so the missing queue's "awaiting a decision"
  // wording must not be reused; "The trash is empty" would also lie when a purge
  // just failed and left entries behind.
  it("words the trash count as entries held, not decisions outstanding", () => {
    expect(describeQueueCount("trash", null)).toBe("Count unavailable");
    expect(describeQueueCount("trash", 0)).toBe("No entries in the trash");
    expect(describeQueueCount("trash", 1)).toBe("1 entry in the trash");
    expect(describeQueueCount("trash", 2)).toBe("2 entries in the trash");
  });

  it("uses the singular for one trashed entry and the plural otherwise", () => {
    expect(describeQueueCount("trash", 1)).toBe("1 entry in the trash");
    expect(describeQueueCount("trash", 12)).toBe("12 entries in the trash");
  });

  it("does not fall through to the imports wording", () => {
    // `imports` states a fact for zero, so a missing zero that borrowed that
    // branch would read "No import currently running" - a different queue.
    expect(describeQueueCount("missing", 0)).toBe("0 missing files awaiting a decision");
    expect(describeQueueCount("missing", 0)).not.toBe("No import currently running");
  });
});

describe("missingBadgeCount (Mf-1tx3)", () => {
  it("returns null before the counts fetch lands, never zero", () => {
    expect(missingBadgeCount(null)).toBeNull();
  });

  it("returns a genuine zero when the last scan flagged nothing", () => {
    expect(missingBadgeCount(makeCounts({ missing: 0 }))).toBe(0);
  });

  it("returns the number of flagged files", () => {
    expect(missingBadgeCount(makeCounts({ missing: 7 }))).toBe(7);
  });
});

describe("trashBadgeCount", () => {
  it("returns null when no read has succeeded, never zero", () => {
    expect(trashBadgeCount(null)).toBeNull();
  });

  it("returns a genuine zero for a listing with no entries", () => {
    expect(trashBadgeCount(makeTrash())).toBe(0);
  });

  it("counts every entry in the listing", () => {
    expect(trashBadgeCount(makeTrash([makeTrashEntry("a.ply"), makeTrashEntry("b.ply")]))).toBe(2);
  });
});

describe("useReviewCounts", () => {
  it("keeps the return object identity across a rerender with the same counts", () => {
    const report = makeReport([makeBundle()]);
    const counts = makeCounts({ missing: 2 });
    type Args = {
      bundle: BundleDetectState;
      imports: ImportRunsState;
      counts: AssetCounts;
      trash: TrashList;
    };
    const { result, rerender } = renderHook(
      ({ bundle, imports, counts, trash }: Args) =>
        useReviewCounts(bundle, imports, counts, trash),
      {
        initialProps: {
          bundle: makeState(report),
          imports: importsState({ loaded: true }),
          counts,
          trash: makeTrash([makeTrashEntry("a.ply"), makeTrashEntry("b.ply")]),
        },
      },
    );

    const initial = result.current;
    // Every count is exercised: the memo must depend on all four primitives.
    expect(initial).toEqual({ bundles: 1, imports: 0, missing: 2, trash: 2 });

    // Fresh state objects with the same numbers must not churn the nav.
    rerender({
      bundle: makeState(report),
      imports: importsState({ loaded: true }),
      counts: makeCounts({ missing: 2 }),
      // A fresh listing object with the same entries must not churn the nav either.
      trash: makeTrash([makeTrashEntry("a.ply"), makeTrashEntry("b.ply")]),
    });

    expect(result.current).toBe(initial);
  });

  it("recomputes when the imports run starts running", () => {
    const report = makeReport([makeBundle()]);
    type Args = { imports: ImportRunsState };
    const { result, rerender } = renderHook(
      ({ imports }: Args) => useReviewCounts(makeState(report), imports, null, null),
      { initialProps: { imports: importsState({ loaded: true }) } },
    );

    expect(result.current.imports).toBe(0);

    rerender({
      imports: importsState({ loaded: true, run: makeRun({ status: "running", finished_at: null }) }),
    });

    expect(result.current.imports).toBe(1);
    expect(result.current.bundles).toBe(1);
  });

  it("recomputes the missing count when the asset counts change", () => {
    type Args = { counts: AssetCounts | null };
    const { result, rerender } = renderHook(
      ({ counts }: Args) => useReviewCounts(makeState(null), importsState(), counts, null),
      { initialProps: { counts: null as AssetCounts | null } },
    );

    // No counts fetch has landed: a dash, never a zero.
    expect(result.current.missing).toBeNull();

    rerender({ counts: makeCounts({ missing: 7 }) });
    expect(result.current.missing).toBe(7);

    rerender({ counts: makeCounts({ missing: 0 }) });
    expect(result.current.missing).toBe(0);
  });

  it("recomputes the trash count once the listing arrives", () => {
    type Args = { trash: TrashList | null };
    const { result, rerender } = renderHook(
      ({ trash }: Args) => useReviewCounts(makeState(null), importsState(), null, trash),
      { initialProps: { trash: null as TrashList | null } },
    );

    // The queue has not been opened: a dash, never a zero.
    expect(result.current.trash).toBeNull();

    rerender({ trash: makeTrash() });
    expect(result.current.trash).toBe(0);

    rerender({ trash: makeTrash([makeTrashEntry("a.ply")]) });
    expect(result.current.trash).toBe(1);
  });
});
