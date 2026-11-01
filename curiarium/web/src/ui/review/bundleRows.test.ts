import { describe, expect, it } from "vitest";

import type { BundleDetectCounts, BundleDetectItem } from "../../lib/api/bundles";
import {
  archiveLine,
  confidenceLabel,
  countLabel,
  countsLine,
  coverLine,
  headline,
  isBlocked,
  isCreatable,
  matchesFilter,
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
    existing_project: null,
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
    expect(isCreatable(makeBundle({ status: "ready", existing_project: null }))).toBe(true);
  });

  it("rejects a blocked row", () => {
    expect(isCreatable(makeBundle({ status: "blocked", existing_project: null }))).toBe(false);
  });

  it("rejects a ready row that already became a project", () => {
    expect(isCreatable(makeBundle({ status: "ready", existing_project: "project-7" }))).toBe(false);
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
