import { describe, expect, it } from "vitest";

import type { BundleDetectCounts, BundleDetectItem } from "../../lib/api/bundles";
import {
  archiveLine,
  confidenceLabel,
  countLabel,
  countsLine,
  coverLine,
  createdProjectLine,
  headline,
  isArchiveOnly,
  isBlocked,
  isCreatable,
  matchesFilter,
  matchesVisibility,
  needsDecision,
  statusLine,
  typedName,
} from "./bundleRows";

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
    existing_project_id: null,
    existing_project_name: null,
    dismissed: false,
    ...overrides,
  };
}

const readyHigh = makeBundle();
const blockedHigh = makeBundle({
  key: "Legacy_vase",
  rel_path: "Legacy_vase",
  title: "Legacy vase",
  status: "blocked",
  blocked_reason: "unresolved_directory",
});
const readyLow = makeBundle({ key: "Cable_clip", confidence: "low" });

function makeCounts(overrides: Partial<BundleDetectCounts> = {}): BundleDetectCounts {
  return {
    total: 0,
    high: 0,
    low: 0,
    ready: 0,
    blocked: 0,
    dismissed: 0,
    already_created: 0,
    ...overrides,
  };
}

describe("matchesFilter", () => {
  it("keeps every row for the all filter", () => {
    for (const row of [readyHigh, blockedHigh, readyLow]) {
      expect(matchesFilter(row, "all")).toBe(true);
    }
  });

  it("keeps only ready rows for the ready filter", () => {
    expect(matchesFilter(readyHigh, "ready")).toBe(true);
    expect(matchesFilter(blockedHigh, "ready")).toBe(false);
    expect(matchesFilter(readyLow, "ready")).toBe(true);
  });

  it("keeps only blocked rows for the blocked filter", () => {
    expect(matchesFilter(readyHigh, "blocked")).toBe(false);
    expect(matchesFilter(blockedHigh, "blocked")).toBe(true);
    expect(matchesFilter(readyLow, "blocked")).toBe(false);
  });

  it("keeps only low-confidence rows for the low filter", () => {
    expect(matchesFilter(readyHigh, "low")).toBe(false);
    expect(matchesFilter(blockedHigh, "low")).toBe(false);
    expect(matchesFilter(readyLow, "low")).toBe(true);
  });
});

describe("isBlocked", () => {
  it("treats a ready row as unblocked and any other status as blocked", () => {
    expect(isBlocked(makeBundle({ status: "ready" }))).toBe(false);
    expect(isBlocked(makeBundle({ status: "blocked" }))).toBe(true);
  });
});

describe("isCreatable", () => {
  it("accepts a ready row that is not already a project", () => {
    expect(isCreatable(makeBundle({ status: "ready", existing_project_id: null }))).toBe(true);
  });

  it("rejects a blocked row", () => {
    expect(isCreatable(makeBundle({ status: "blocked", existing_project_id: null }))).toBe(false);
  });

  it("rejects a ready row that already became a project", () => {
    expect(isCreatable(makeBundle({ status: "ready", existing_project_id: "project-7" }))).toBe(
      false,
    );
  });
});

describe("isArchiveOnly", () => {
  // `BundleArchive.rel_path` is non-nullable on the wire, so the "no path"
  // quadrant is forced here rather than widening the API type for a defensive
  // branch.
  const noPath = { rel_path: null as unknown as string, twin_verified: false };

  it("treats only an archive-only row with an archive path as extractable", () => {
    const archiveOnly = (archive: BundleDetectItem["archive"]) =>
      makeBundle({ status: "blocked", blocked_reason: "archive_only", archive });

    // The one quadrant that can be extracted: the label and the path agree.
    expect(isArchiveOnly(archiveOnly({ rel_path: "Moai.zip", twin_verified: false }))).toBe(true);
    // The label alone is not enough - there would be nothing to extract.
    expect(isArchiveOnly(archiveOnly(null))).toBe(false);
    expect(isArchiveOnly(archiveOnly(noPath))).toBe(false);
    // A path on a row the scan already resolved is not an extraction to offer.
    expect(
      isArchiveOnly(
        makeBundle({
          status: "ready",
          blocked_reason: null,
          archive: { rel_path: "Moai.zip", twin_verified: true },
        }),
      ),
    ).toBe(false);
  });
});

describe("matchesVisibility", () => {
  const pending = makeBundle();
  const blocked = makeBundle({ status: "blocked", blocked_reason: "unresolved_directory" });
  const dismissed = makeBundle({ dismissed: true });
  const created = makeBundle({ existing_project_id: "project-7" });
  const both = makeBundle({ dismissed: true, existing_project_id: "project-7" });

  it("keeps a pending row under every combination of toggles", () => {
    for (const showDismissed of [false, true]) {
      for (const showCreated of [false, true]) {
        expect(matchesVisibility(pending, { showDismissed, showCreated })).toBe(true);
        expect(matchesVisibility(blocked, { showDismissed, showCreated })).toBe(true);
      }
    }
  });

  it("hides a dismissed row until Show dismissed is on", () => {
    expect(matchesVisibility(dismissed, { showDismissed: false, showCreated: false })).toBe(false);
    expect(matchesVisibility(dismissed, { showDismissed: false, showCreated: true })).toBe(false);
    expect(matchesVisibility(dismissed, { showDismissed: true, showCreated: false })).toBe(true);
    expect(matchesVisibility(dismissed, { showDismissed: true, showCreated: true })).toBe(true);
  });

  it("hides a created row until Show created is on", () => {
    expect(matchesVisibility(created, { showDismissed: false, showCreated: false })).toBe(false);
    expect(matchesVisibility(created, { showDismissed: true, showCreated: false })).toBe(false);
    expect(matchesVisibility(created, { showDismissed: false, showCreated: true })).toBe(true);
    expect(matchesVisibility(created, { showDismissed: true, showCreated: true })).toBe(true);
  });

  it("needs both toggles for a row that is dismissed and already created", () => {
    expect(matchesVisibility(both, { showDismissed: false, showCreated: false })).toBe(false);
    expect(matchesVisibility(both, { showDismissed: true, showCreated: false })).toBe(false);
    expect(matchesVisibility(both, { showDismissed: false, showCreated: true })).toBe(false);
    expect(matchesVisibility(both, { showDismissed: true, showCreated: true })).toBe(true);
  });
});

describe("needsDecision", () => {
  it("is matchesVisibility with both toggles off, for every row shape", () => {
    const rows = [
      readyHigh,
      blockedHigh,
      readyLow,
      makeBundle({ dismissed: true }),
      makeBundle({ existing_project_id: "project-7" }),
      makeBundle({ dismissed: true, existing_project_id: "project-7" }),
    ];
    for (const row of rows) {
      expect(needsDecision(row)).toBe(
        matchesVisibility(row, { showDismissed: false, showCreated: false }),
      );
    }
  });

  it("keeps a pending row, blocked or not, and drops the settled ones", () => {
    expect(needsDecision(readyHigh)).toBe(true);
    expect(needsDecision(blockedHigh)).toBe(true);
    expect(needsDecision(makeBundle({ dismissed: true }))).toBe(false);
    expect(needsDecision(makeBundle({ existing_project_id: "project-7" }))).toBe(false);
    expect(needsDecision(makeBundle({ dismissed: true, existing_project_id: "project-7" }))).toBe(
      false,
    );
    // A nameless project is still settled: the id, not the name, is the identity
    // that keeps it out of the queue (the Mf-gyxg behaviour).
    expect(
      needsDecision({
        ...makeBundle(),
        existing_project_id: "project-7",
        existing_project_name: null,
      }),
    ).toBe(false);
  });
});

describe("statusLine", () => {
  it("calls a ready row ready", () => {
    expect(statusLine(makeBundle({ status: "ready" }))).toBe("Ready");
  });

  it("translates each known blocked reason", () => {
    expect(statusLine(makeBundle({ status: "blocked", blocked_reason: "unresolved_directory" }))).toBe(
      "Blocked - no matching folder on disk",
    );
    expect(statusLine(makeBundle({ status: "blocked", blocked_reason: "not_scanned" }))).toBe(
      "Blocked - no model file registered by a scan",
    );
    expect(statusLine(makeBundle({ status: "blocked", blocked_reason: "archive_only" }))).toBe(
      "Blocked - zip only - extract the archive in place, then the scan registers it",
    );
  });

  it("passes an unknown blocked reason through unchanged", () => {
    expect(statusLine(makeBundle({ status: "blocked", blocked_reason: "future-reason" }))).toBe(
      "Blocked - future-reason",
    );
  });

  it("falls back to a bare blocked line when the reason is missing", () => {
    expect(statusLine(makeBundle({ status: "blocked", blocked_reason: null }))).toBe("Blocked - blocked");
  });
});

describe("createdProjectLine", () => {
  const projectId = "3f2a6c1e-8f4b-4c7d-9a1e-2b5c8d0f4a61";

  it("says nothing for a row that never became a project", () => {
    expect(createdProjectLine(makeBundle())).toBeNull();
    // A name without an id is not a project: the id is the identity.
    expect(createdProjectLine(makeBundle({ existing_project_name: "Moai statue" }))).toBeNull();
  });

  it("names the project when the wire carries a usable name", () => {
    expect(
      createdProjectLine(
        makeBundle({ existing_project_id: projectId, existing_project_name: "Moai statue" }),
      ),
    ).toBe("Already created as project Moai statue");
  });

  it("trims a padded name before speaking it", () => {
    expect(
      createdProjectLine(
        makeBundle({ existing_project_id: projectId, existing_project_name: "  Moai statue  " }),
      ),
    ).toBe("Already created as project Moai statue");
  });

  it("says the name is unavailable rather than printing the id or nothing", () => {
    for (const existing_project_name of [null, "", "   "]) {
      const line = createdProjectLine(
        makeBundle({ existing_project_id: projectId, existing_project_name }),
      );
      expect(line).toBe("Already created as a project - its name is unavailable.");
      expect(line).not.toBe("");
      expect(line).not.toContain(projectId);
    }
  });
});

describe("countLabel", () => {
  it("uses the singular form for exactly one", () => {
    expect(countLabel(1, "model", "models")).toBe("1 model");
  });

  it("uses the plural form for zero and many", () => {
    expect(countLabel(0, "model", "models")).toBe("0 models");
    expect(countLabel(2, "model", "models")).toBe("2 models");
  });
});

describe("countsLine", () => {
  it("lists the asset counts and the registered-asset tail", () => {
    expect(countsLine(makeBundle())).toBe(
      "3 models, 5 images, 2 text files, 0 other files - 8 registered assets",
    );
  });

  it("uses singular nouns for a one-of-everything bundle", () => {
    const row = makeBundle({
      model_count: 1,
      image_count: 1,
      text_count: 1,
      other_count: 1,
      assets_total: 1,
    });
    expect(countsLine(row)).toBe(
      "1 model, 1 image, 1 text file, 1 other file - 1 registered asset",
    );
  });
});

describe("coverLine", () => {
  it("says there is no cover when the bundle has none", () => {
    const row = makeBundle({ cover: { asset_id: null, rel_path: null, reason: "largest-image-unregistered" } });
    expect(coverLine(row)).toBe("Cover: none");
  });

  it("names a registered cover", () => {
    expect(coverLine(makeBundle())).toBe("Cover images/hero.jpg");
  });

  it("marks an unregistered largest image", () => {
    const row = makeBundle({
      cover: { asset_id: null, rel_path: "preview.png", reason: "largest-image-unregistered" },
    });
    expect(coverLine(row)).toBe("Cover preview.png (not registered)");
  });
});

describe("archiveLine", () => {
  it("returns null when there is no archive twin", () => {
    expect(archiveLine(makeBundle({ archive: null }))).toBeNull();
  });

  it("says whether the twin is verified", () => {
    expect(
      archiveLine(makeBundle({ archive: { rel_path: "Moai.zip", twin_verified: true } })),
    ).toBe("Archive Moai.zip - twin verified");
    expect(
      archiveLine(makeBundle({ archive: { rel_path: "Moai.zip", twin_verified: false } })),
    ).toBe("Archive Moai.zip - twin not verified");
  });
});

describe("typedName", () => {
  it("falls back to the server's suggestion when nothing was typed", () => {
    expect(typedName({}, makeBundle({ name_suggestion: "Moai statue" }))).toBe("Moai statue");
  });

  it("trims the operator's typed name", () => {
    expect(typedName({ "Moai statue": "  Renamed Moai  " }, makeBundle())).toBe("Renamed Moai");
  });
});

describe("confidenceLabel", () => {
  it("names high and low confidence", () => {
    expect(confidenceLabel("high")).toBe("High confidence");
    expect(confidenceLabel("low")).toBe("Low confidence");
  });
});

describe("headline", () => {
  it("says no bundles were found", () => {
    expect(headline(makeCounts())).toBe("0 bundles found - 0 high confidence, 0 low");
  });

  it("uses the singular for exactly one bundle", () => {
    expect(headline(makeCounts({ total: 1, high: 1 }))).toBe(
      "1 bundle found - 1 high confidence, 0 low",
    );
  });

  it("uses the plural for two bundles", () => {
    expect(headline(makeCounts({ total: 2, high: 1, low: 1 }))).toBe(
      "2 bundles found - 1 high confidence, 1 low",
    );
  });
});
