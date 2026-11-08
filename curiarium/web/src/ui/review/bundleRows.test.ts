import { describe, expect, it } from "vitest";

import {
  ENRICH_MAX_KEYS,
  type BundleDetectCounts,
  type BundleDetectItem,
  type BundleEnrichment,
} from "../../lib/api/bundles";
import {
  ENRICH_NONE_REASON,
  EXTRACT_AND_CREATE_HINT,
  EXTRACT_AND_CREATE_LABEL,
  EXTRACT_AND_CREATE_ROW_TITLE,
  NO_SOURCE_PAGE_REASON,
  ROW_STATE_CREATED,
  ROW_STATE_DISMISSED,
  ROW_STATE_IN_PROJECT,
  archiveExtractTitle,
  archiveLine,
  bulkEnrichTitle,
  canEnrich,
  canExtractAndCreate,
  canRecheck,
  confidenceLabel,
  countLabel,
  countsLine,
  coverLine,
  createdProjectLine,
  enrichSelectionLabel,
  enrichUnavailableLine,
  enrichmentLine,
  extractPathFor,
  hasThingSource,
  headline,
  isArchiveOnly,
  isBlocked,
  isCreatable,
  matchesFilter,
  matchesVisibility,
  needsDecision,
  needsEnrichment,
  newPartsLine,
  rowBucket,
  rowStateChip,
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

  it("keeps a medium row for the medium chip and not for the low one", () => {
    const readyMedium = makeBundle({ key: "Dragon_5390184", confidence: "medium" });

    expect(matchesFilter(readyMedium, "medium")).toBe(true);
    expect(matchesFilter(readyHigh, "medium")).toBe(false);
    expect(matchesFilter(readyLow, "medium")).toBe(false);

    // The discriminating pair: folding `medium` into the low leg (or the reverse)
    // makes one of these two assertions fail.
    expect(matchesFilter(readyMedium, "low")).toBe(false);
    expect(matchesFilter(readyLow, "medium")).toBe(false);
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

  it("never re-offers a create for a grown bundle", () => {
    // A folder that grew is still the same folder: a second project for it is
    // exactly what the marker prevents, whatever the new parts say.
    expect(
      isCreatable(
        makeBundle({
          status: "ready",
          existing_project_id: "project-7",
          bundle_drift_state: "known",
          bundle_drift_count: 3,
        }),
      ),
    ).toBe(false);
  });
});

describe("extractPathFor and isArchiveOnly", () => {
  it("gives the ticket's live row its own path, because the row IS the archive", () => {
    // The wire sends `archive: null` for a zip-only row; a predicate that demands
    // a twin shuts every door on the one row that has one.
    const live = makeBundle({
      key: "import/01_Fates End - Wizard Tower.zip",
      rel_path: "import/01_Fates End - Wizard Tower.zip",
      status: "blocked",
      blocked_reason: "archive_only",
      archive: null,
    });
    expect(isArchiveOnly(live)).toBe(true);
    expect(extractPathFor(live)).toBe("import/01_Fates End - Wizard Tower.zip");
  });

  it("names the twin on a refused pair, whose own key is a directory", () => {
    // A `pair_only` row: the container rule refused the directory, so its key
    // cannot end `.zip` and the twin is the only possible source.
    const pairOnly = makeBundle({
      key: "import/endorphin",
      rel_path: "import/endorphin",
      status: "blocked",
      blocked_reason: "archive_only",
      archive: { rel_path: "import/endorphin.zip", twin_verified: true },
    });
    expect(isArchiveOnly(pairOnly)).toBe(true);
    expect(extractPathFor(pairOnly)).toBe("import/endorphin.zip");
  });

  it("reads the suffix case-insensitively, mirroring the server's walk", () => {
    const upper = makeBundle({
      key: "import/Moai.ZIP",
      rel_path: "import/Moai.ZIP",
      status: "blocked",
      blocked_reason: "archive_only",
      archive: null,
    });
    expect(isArchiveOnly(upper)).toBe(true);
    expect(extractPathFor(upper)).toBe("import/Moai.ZIP");
  });

  it("refuses a blocked row whose twin is attached but whose reason is not the archive's", () => {
    // The twin is attached whatever the status, so the reason gate is the only
    // thing keeping an unresolved directory's provenance out of the door.
    const unresolved = makeBundle({
      key: "Legacy_vase",
      rel_path: "Legacy_vase",
      status: "blocked",
      blocked_reason: "unresolved_directory",
      archive: { rel_path: "Legacy_vase.zip", twin_verified: true },
    });
    expect(isArchiveOnly(unresolved)).toBe(false);
    expect(extractPathFor(unresolved)).toBeNull();
  });

  it("refuses a ready row that names a twin", () => {
    const ready = makeBundle({
      status: "ready",
      blocked_reason: null,
      archive: { rel_path: "Moai.zip", twin_verified: true },
    });
    expect(isArchiveOnly(ready)).toBe(false);
    expect(extractPathFor(ready)).toBeNull();
  });

  it("refuses an empty twin when the row's own path is not an archive", () => {
    // The twin can no longer be the only source: a row that is not itself a zip
    // still needs a real path behind the label.
    const emptyTwin = makeBundle({
      status: "blocked",
      blocked_reason: "archive_only",
      archive: { rel_path: "", twin_verified: false },
    });
    expect(isArchiveOnly(emptyTwin)).toBe(false);
    expect(extractPathFor(emptyTwin)).toBeNull();
  });
});

describe("canExtractAndCreate", () => {
  const zipOnly = makeBundle({
    key: "import/Moai.zip",
    rel_path: "import/Moai.zip",
    status: "blocked",
    blocked_reason: "archive_only",
    archive: null,
  });
  const pairOnly = makeBundle({
    key: "import/endorphin",
    rel_path: "import/endorphin",
    status: "blocked",
    blocked_reason: "archive_only",
    archive: { rel_path: "import/endorphin.zip", twin_verified: true },
  });

  it("accepts the zip-only row that is the archive and has no project", () => {
    expect(canExtractAndCreate(zipOnly)).toBe(true);
  });

  it("accepts a refused pair, whose twin is the archive", () => {
    expect(canExtractAndCreate(pairOnly)).toBe(true);
  });

  it("refuses every row the one-step door could not serve", () => {
    // A ready row is not an archive waiting for its extraction.
    expect(canExtractAndCreate(makeBundle())).toBe(false);
    // A finished row's one create would only be answered `already_created`.
    expect(canExtractAndCreate(makeBundle({ ...zipOnly, existing_project_id: PROJECT_ID }))).toBe(
      false,
    );
    // Another blocked reason is not the archive door, even with a twin attached.
    expect(
      canExtractAndCreate(
        makeBundle({
          status: "blocked",
          blocked_reason: "unresolved_directory",
          archive: { rel_path: "Moai.zip", twin_verified: false },
        }),
      ),
    ).toBe(false);
  });
});

describe("archiveExtractTitle", () => {
  const zipOnly = makeBundle({
    key: "import/Moai.zip",
    rel_path: "import/Moai.zip",
    status: "blocked",
    blocked_reason: "archive_only",
    archive: null,
  });

  it("points a one-step row at the detail panel's whole job", () => {
    expect(archiveExtractTitle(zipOnly)).toBe(EXTRACT_AND_CREATE_ROW_TITLE);
    expect(archiveExtractTitle(zipOnly)).toContain(EXTRACT_AND_CREATE_LABEL);
  });

  it("keeps the old sentence byte for byte on every other row", () => {
    expect(archiveExtractTitle(makeBundle())).toBe("Extract this archive in place (Enter)");
    expect(archiveExtractTitle(makeBundle({ ...zipOnly, existing_project_id: PROJECT_ID }))).toBe(
      "Extract this archive in place (Enter)",
    );
  });
});

describe("extract-and-create words", () => {
  it("names the door, and its hint says what plain Extract leaves out", () => {
    expect(EXTRACT_AND_CREATE_LABEL).toBe("Extract and create project");
    expect(EXTRACT_AND_CREATE_HINT).toContain("waits for the library scan");
    // Plain Extract runs the wait too, so the hint must not claim it stops at
    // the extraction: the project is the only step it leaves out.
    expect(EXTRACT_AND_CREATE_HINT).toContain(
      "The plain Extract button does the extraction and the wait, and stops before the project.",
    );
    expect(EXTRACT_AND_CREATE_HINT).not.toContain("stops after the extraction");
    // No quoted control name: the plain door's visible text and its accessible
    // name disagree, so a quote would name a control nobody hears.
    expect(EXTRACT_AND_CREATE_HINT).not.toContain("'Extract archive'");
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

  it("says 'Already in' and names the shared-files evidence for an asset match", () => {
    expect(
      createdProjectLine(
        makeBundle({
          existing_project_id: projectId,
          existing_project_name: "Fates End - ClockworkMonster Tower",
          existing_project_match: "assets",
        }),
      ),
    ).toBe("Already in Fates End - ClockworkMonster Tower (matched by shared files)");
  });

  it("says the name is unavailable for an asset match rather than printing the id or nothing", () => {
    for (const existing_project_name of [null, "", "   "]) {
      const line = createdProjectLine(
        makeBundle({
          existing_project_id: projectId,
          existing_project_name,
          existing_project_match: "assets",
        }),
      );
      expect(line).toBe(
        "Already in a project (matched by shared files) - its name is unavailable.",
      );
      expect(line).not.toBe("");
      expect(line).not.toContain(projectId);
    }
  });

  it("trims a padded name before speaking it for an asset match", () => {
    expect(
      createdProjectLine(
        makeBundle({
          existing_project_id: projectId,
          existing_project_name: "  Fates End - ClockworkMonster Tower  ",
          existing_project_match: "assets",
        }),
      ),
    ).toBe("Already in Fates End - ClockworkMonster Tower (matched by shared files)");
  });

  it("keeps the old line for the marker, source URL and name identities", () => {
    // The copy is unchanged for the three identities the app either knows or
    // guesses: only shared files need the weaker "Already in" phrasing.
    for (const existing_project_match of ["marker", "source_url", "name"] as const) {
      expect(
        createdProjectLine(
          makeBundle({
            existing_project_id: projectId,
            existing_project_name: "Moai statue",
            existing_project_match,
          }),
        ),
      ).toBe("Already created as project Moai statue");
    }
  });

  it("keeps the old line when the payload omits the match, so undefined is not assets", () => {
    expect(
      createdProjectLine(
        makeBundle({ existing_project_id: projectId, existing_project_name: "Moai statue" }),
      ),
    ).toBe("Already created as project Moai statue");
  });
});

describe("newPartsLine", () => {
  const projectId = "3f2a6c1e-8f4b-4c7d-9a1e-2b5c8d0f4a61";
  const created = (overrides: Partial<BundleDetectItem> = {}) =>
    makeBundle({ existing_project_id: projectId, existing_project_name: "Moai statue", ...overrides });

  it("says nothing for a row that never became a project", () => {
    expect(newPartsLine(makeBundle({ bundle_drift_state: "known", bundle_drift_count: 3 }))).toBeNull();
  });

  it("says nothing for a project with no marker", () => {
    expect(newPartsLine(created({ bundle_drift_state: "not_applicable" }))).toBeNull();
  });

  it("says nothing when the folder has not grown, so a quiet queue stays quiet", () => {
    expect(newPartsLine(created({ bundle_drift_state: "known", bundle_drift_count: 0 }))).toBeNull();
  });

  it("says nothing for a payload from before the field existed", () => {
    // Neither field: an older server, which is not_applicable rather than a 0.
    expect(newPartsLine(created())).toBeNull();
  });

  it("counts the new parts in the plural", () => {
    expect(newPartsLine(created({ bundle_drift_state: "known", bundle_drift_count: 3 }))).toBe(
      "3 new parts since this was created",
    );
  });

  it("counts one new part in the singular", () => {
    expect(newPartsLine(created({ bundle_drift_state: "known", bundle_drift_count: 1 }))).toBe(
      "1 new part since this was created",
    );
  });

  it("renders an en dash when the server cannot tell, never a zero", () => {
    const line = newPartsLine(created({ bundle_drift_state: "unknown" }));
    expect(line).toBe("\u2013 new parts since this was created");
    expect(line).not.toContain("0");
  });
});

describe("a row already in a project by shared files", () => {
  const assetMatched = makeBundle({
    existing_project_id: "project-7",
    existing_project_name: "Fates End - ClockworkMonster Tower",
    existing_project_match: "assets",
  });

  it("is hidden by default, as the created bucket is", () => {
    expect(matchesVisibility(assetMatched, { showDismissed: false, showCreated: false })).toBe(
      false,
    );
  });

  it("is visible once Show created is on", () => {
    expect(matchesVisibility(assetMatched, { showDismissed: false, showCreated: true })).toBe(true);
  });

  it("does not count as needing a decision", () => {
    expect(needsDecision(assetMatched)).toBe(false);
  });

  it("is not creatable, so it is never re-offered as new work", () => {
    expect(isCreatable(assetMatched)).toBe(false);
  });
});

describe("rowStateChip", () => {
  const projectId = "3f2a6c1e-8f4b-4c7d-9a1e-2b5c8d0f4a61";
  const assetMatched = makeBundle({
    existing_project_id: projectId,
    existing_project_name: "Fates End - ClockworkMonster Tower",
    existing_project_match: "assets",
  });

  it("says In project for shared files, and never claims the app created it", () => {
    const chip = rowStateChip(assetMatched);
    expect(chip.label).toBe("In project");
    expect(chip.label).not.toBe("Created");
    expect(chip.title).toBe(
      "Already in Fates End - ClockworkMonster Tower (matched by shared files)",
    );
  });

  it("says In project with the nameless sentence when the project has no name", () => {
    const chip = rowStateChip(
      makeBundle({
        existing_project_id: projectId,
        existing_project_name: null,
        existing_project_match: "assets",
      }),
    );
    expect(chip.label).toBe("In project");
    expect(chip.title).toBe(
      "Already in a project (matched by shared files) - its name is unavailable.",
    );
  });

  it("keeps Created for the three identities that are the app's own record or a guess", () => {
    for (const existing_project_match of ["marker", "source_url", "name"] as const) {
      expect(
        rowStateChip(
          makeBundle({
            existing_project_id: projectId,
            existing_project_name: "Moai statue",
            existing_project_match,
          }),
        ),
      ).toEqual({ label: "Created", title: null });
    }
  });

  it("keeps Created when the payload omits the match, so undefined is not assets", () => {
    // One fixture with the key absent, one with it explicitly null: both mean
    // "the server did not say", and neither may borrow the assets words.
    expect(rowStateChip(makeBundle({ existing_project_id: projectId }))).toEqual({
      label: "Created",
      title: null,
    });
    expect(
      rowStateChip(makeBundle({ existing_project_id: projectId, existing_project_match: null })),
    ).toEqual({ label: "Created", title: null });
  });

  it("says Dismissed for a dismissal and nothing at all for a fresh row", () => {
    expect(rowStateChip(makeBundle({ dismissed: true }))).toEqual({
      label: "Dismissed",
      title: null,
    });
    expect(rowStateChip(makeBundle())).toEqual({ label: "", title: null });
    expect(rowStateChip(makeBundle({ dismissed: true, existing_project_id: projectId }))).toEqual({
      label: "Created",
      title: null,
    });
  });

  it("the three chip words are distinct", () => {
    expect(new Set([ROW_STATE_CREATED, ROW_STATE_IN_PROJECT, ROW_STATE_DISMISSED]).size).toBe(3);
  });
});

describe("rowBucket", () => {
  const projectId = "3f2a6c1e-8f4b-4c7d-9a1e-2b5c8d0f4a61";

  it("puts a row with a project in the created bucket, whatever the identity", () => {
    for (const existing_project_match of [
      "marker",
      "source_url",
      "name",
      "assets",
      undefined,
    ] as const) {
      expect(rowBucket(makeBundle({ existing_project_id: projectId, existing_project_match }))).toBe(
        "created",
      );
    }
  });

  it("puts a dismissal in the dismissed bucket", () => {
    expect(rowBucket(makeBundle({ dismissed: true }))).toBe("dismissed");
  });

  it("puts a fresh row in no bucket", () => {
    expect(rowBucket(makeBundle())).toBeNull();
  });

  it("keeps a project ahead of a dismissal", () => {
    expect(rowBucket(makeBundle({ dismissed: true, existing_project_id: projectId }))).toBe("created");
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
    const row = makeBundle({
      cover: {
        asset_id: null,
        rel_path: null,
        reason: "largest-image-unregistered",
        url: null,
        thumb_url: null,
      },
    });
    expect(coverLine(row)).toBe("Cover: none");
  });

  it("names a registered cover", () => {
    expect(coverLine(makeBundle())).toBe("Cover images/hero.jpg");
  });

  it("marks an unregistered largest image", () => {
    const row = makeBundle({
      cover: {
        asset_id: null,
        rel_path: "preview.png",
        reason: "largest-image-unregistered",
        url: null,
        thumb_url: null,
      },
    });
    expect(coverLine(row)).toBe("Cover preview.png (not registered)");
  });

  it("names a derived model preview", () => {
    const row = makeBundle({
      cover: {
        asset_id: "asset-9",
        rel_path: "model.stl",
        reason: "model-preview",
        url: "/file/asset-9/model.stl",
        thumb_url: null,
      },
    });
    expect(coverLine(row)).toBe("Cover model.stl (model preview)");
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
  it("names each confidence level", () => {
    expect(confidenceLabel("high")).toBe("High confidence");
    expect(confidenceLabel("medium")).toBe("Medium confidence");
    expect(confidenceLabel("low")).toBe("Low confidence");
    // An unknown value still reads as the lowest level we know.
    expect(confidenceLabel("future-level")).toBe("Low confidence");
  });
});

describe("headline", () => {
  it("says no bundles were found", () => {
    expect(headline(makeCounts())).toBe("0 bundles found - 0 high confidence, 0 medium, 0 low");
  });

  it("uses the singular for exactly one bundle", () => {
    expect(headline(makeCounts({ total: 1, high: 1 }))).toBe(
      "1 bundle found - 1 high confidence, 0 medium, 0 low",
    );
  });

  it("uses the plural for two bundles", () => {
    expect(headline(makeCounts({ total: 2, high: 1, low: 1 }))).toBe(
      "2 bundles found - 1 high confidence, 0 medium, 1 low",
    );
  });

  it("says 0 medium for a payload that omits the medium count", () => {
    // Built without the `medium` key at all, which is exactly what a report
    // written before the medium confidence looks like. Dropping the `?? 0` in
    // `headline` makes the middle number read `undefined`.
    const line = headline(makeCounts({ total: 1, high: 1 }));
    expect(line).toBe("1 bundle found - 1 high confidence, 0 medium, 0 low");
    expect(line).not.toContain("undefined");
  });

  it("reads the middle number from the medium count", () => {
    expect(headline(makeCounts({ total: 4, high: 1, medium: 2, low: 1 }))).toBe(
      "4 bundles found - 1 high confidence, 2 medium, 1 low",
    );
  });
});

const PROJECT_ID = "3f2a6c1e-8f4b-4c7d-9a1e-2b5c8d0f4a61";

function enrichment(overrides: Partial<BundleEnrichment> = {}): BundleEnrichment {
  return {
    source_url: "https://www.thingiverse.com/thing:5390184",
    fetched_at: "2026-11-05T12:00:00+00:00",
    fields: ["creator", "licence"],
    ...overrides,
  };
}

describe("hasThingSource", () => {
  it("accepts a row that names its vendor page", () => {
    expect(hasThingSource(makeBundle())).toBe(true);
  });

  it("rejects a row without a source URL", () => {
    expect(hasThingSource(makeBundle({ source_url: null }))).toBe(false);
  });
});

describe("needsEnrichment", () => {
  it("accepts a row missing either field", () => {
    expect(needsEnrichment(makeBundle({ creator: null, licence: null }))).toBe(true);
    expect(needsEnrichment(makeBundle({ creator: null, licence: "CC BY" }))).toBe(true);
    expect(needsEnrichment(makeBundle({ creator: "sculpteo", licence: null }))).toBe(true);
  });

  it("rejects a row whose creator and licence are both known", () => {
    expect(needsEnrichment(makeBundle())).toBe(false);
  });
});

describe("canEnrich", () => {
  const GAP = { creator: null, licence: null } as const;
  const cases: [string, BundleDetectItem, boolean][] = [
    // The happy path: a source to read and a gap to fill.
    ["a source and a gap", makeBundle(GAP), true],
    // A LOW hash-named row carries no server-issued key, so there is no page.
    ["no source URL", makeBundle({ ...GAP, source_url: null }), false],
    // Already answered: a second bulk pass would be a request with nothing to
    // fill. `null` is the server saying this row took nothing from the page,
    // which is the same as no record at all, so it stays enrichable.
    ["an existing page record", makeBundle({ ...GAP, enrichment: enrichment() }), false],
    ["an explicit null page record", makeBundle({ ...GAP, enrichment: null }), true],
    // An already-created row's values are never used again.
    ["an existing project", makeBundle({ ...GAP, existing_project_id: PROJECT_ID }), false],
    // Nothing the page could supply.
    ["both fields present", makeBundle({ creator: "sculpteo", licence: "CC BY" }), false],
  ];

  it.each(cases)("answers %s", (_label, row, expected) => {
    expect(canEnrich(row)).toBe(expected);
  });
});

describe("canRecheck", () => {
  it("accepts a live row the page already answered", () => {
    expect(canRecheck(makeBundle({ enrichment: enrichment() }))).toBe(true);
  });

  it("rejects a row that has not been enriched yet", () => {
    expect(canRecheck(makeBundle())).toBe(false);
    expect(canRecheck(makeBundle({ enrichment: null }))).toBe(false);
  });

  it("rejects a row without a source URL", () => {
    expect(canRecheck(makeBundle({ enrichment: enrichment(), source_url: null }))).toBe(false);
  });

  it("rejects an already-created row", () => {
    expect(canRecheck(makeBundle({ enrichment: enrichment(), existing_project_id: PROJECT_ID }))).toBe(
      false,
    );
  });
});

describe("enrichmentLine", () => {
  const fetched = (fields: string[]): BundleDetectItem =>
    makeBundle({ enrichment: enrichment({ fields }) });

  it("names all three fields", () => {
    expect(enrichmentLine(fetched(["creator", "licence", "licence_url"]))).toBe(
      "From the source page: creator, licence, licence URL (fetched 2026-11-05)",
    );
  });

  it("names only the fields this row took", () => {
    expect(enrichmentLine(fetched(["creator", "licence_url"]))).toBe(
      "From the source page: creator, licence URL (fetched 2026-11-05)",
    );
  });

  it("names a single field", () => {
    expect(enrichmentLine(fetched(["licence"]))).toBe(
      "From the source page: licence (fetched 2026-11-05)",
    );
  });

  it("still dates a page that supplied nothing", () => {
    expect(enrichmentLine(fetched([]))).toBe("From the source page (fetched 2026-11-05)");
  });

  it("names the fields and says the date is unknown when the record has no stamp", () => {
    // A record written by an older build, or a cache entry whose stamp was lost,
    // still says what the page said. The date is said to be unknown, not
    // dropped and not invented, and the sentence must not print `null`.
    const line = enrichmentLine(makeBundle({ enrichment: enrichment({ fetched_at: null }) }));
    expect(line).toBe("From the source page: creator, licence (fetched date unknown)");
    expect(line).not.toContain("null");
  });

  it("still says the date is unknown for a stamp-less page that supplied nothing", () => {
    const line = enrichmentLine(
      makeBundle({ enrichment: enrichment({ fetched_at: null, fields: [] }) }),
    );
    expect(line).toBe("From the source page (fetched date unknown)");
    expect(line).not.toContain("null");
  });

  it("returns null for a row the page never answered", () => {
    expect(enrichmentLine(makeBundle({ enrichment: null }))).toBeNull();
    // `undefined` is "the server did not say", and it must read as no line.
    expect(enrichmentLine(makeBundle())).toBeNull();
  });
});

describe("enrichSelectionLabel", () => {
  it("counts the enrichable selection when it fits", () => {
    expect(enrichSelectionLabel(1, 1)).toBe("Enrich selected (1)");
    expect(enrichSelectionLabel(ENRICH_MAX_KEYS, ENRICH_MAX_KEYS)).toBe("Enrich selected (25)");
  });

  it("names the truncation once the selection exceeds the server's cap", () => {
    expect(enrichSelectionLabel(ENRICH_MAX_KEYS + 1, ENRICH_MAX_KEYS + 1)).toBe(
      "Enrich selected (25 of 26)",
    );
    expect(enrichSelectionLabel(30, 30)).toBe("Enrich selected (25 of 30)");
  });

  it("names the shortfall when part of the selection cannot be enriched", () => {
    expect(enrichSelectionLabel(1, 3)).toBe("Enrich selected (1 of 3)");
    // Nothing is sendable at all: the label must still say three were selected,
    // so a reader never sees a bare `(0)` and mistakes it for an empty choice.
    expect(enrichSelectionLabel(0, 3)).toBe("Enrich selected (0 of 3)");
    // The one cell where the cap shortfall and the unenrichable shortfall
    // collide: 25 rows are sendable and 25 is also the cap, so the words are
    // true under either cause.
    expect(enrichSelectionLabel(25, 30)).toBe("Enrich selected (25 of 30)");
    expect(enrichSelectionLabel(0, 0)).toBe("Enrich selected (0)");
  });
});

describe("enrichUnavailableLine", () => {
  const GAP = { creator: null, licence: null, licence_url: null } as const;

  // Three source-less detector shapes: none names a vendor page, so the row
  // offers no enrichment door for one and the same reason.
  const sourceLess: [string, BundleDetectItem][] = [
    [
      "a generic bundle",
      makeBundle({
        ...GAP,
        source_url: null,
        detector: "generic-bundle",
        confidence: "low",
        metadata_shape: "generic",
      }),
    ],
    [
      "an archive bundle",
      makeBundle({
        ...GAP,
        key: "import/Moai.zip",
        rel_path: "import/Moai.zip",
        source_url: null,
        detector: "archive-bundle",
        confidence: "low",
        status: "blocked",
        blocked_reason: "archive_only",
        archive: null,
      }),
    ],
    [
      "a title-name row",
      makeBundle({ ...GAP, source_url: null, detector: "title-name", confidence: "low" }),
    ],
  ];

  it.each(sourceLess)("says why a %s has no door", (_label, row) => {
    expect(enrichUnavailableLine(row)).toBe(NO_SOURCE_PAGE_REASON);
  });

  it("stays silent for a low-confidence row that does name a source page", () => {
    // The gate is `hasThingSource`, not the detector or the confidence: the
    // same generic-bundle shape with a URL gains the door, so the sentence goes.
    const row = makeBundle({
      ...GAP,
      detector: "generic-bundle",
      confidence: "low",
      metadata_shape: "generic",
    });
    expect(enrichUnavailableLine(row)).toBeNull();
  });

  const silent: [string, BundleDetectItem][] = [
    // A page to read: the door exists, so there is nothing to explain.
    ["the row names a source page", makeBundle({ ...GAP })],
    // Both fields are filled: another line already says so.
    ["neither creator nor licence is empty", makeBundle({ source_url: null })],
    [
      "the page already answered",
      makeBundle({
        ...GAP,
        source_url: null,
        enrichment: {
          source_url: "https://www.thingiverse.com/thing:5390184",
          fetched_at: "2026-11-05T12:00:00+00:00",
          fields: ["creator"],
        },
      }),
    ],
    // A settled project says so in `createdProjectLine` instead.
    ["the row is already a project", makeBundle({ ...GAP, source_url: null, existing_project_id: PROJECT_ID })],
  ];

  it.each(silent)("stays silent when %s", (_label, row) => {
    expect(enrichUnavailableLine(row)).toBeNull();
  });

  it("never speaks where canEnrich would have agreed", () => {
    for (const [, row] of [...sourceLess, ...silent]) {
      if (enrichUnavailableLine(row) !== null) {
        expect(canEnrich(row)).toBe(false);
        expect(hasThingSource(row)).toBe(false);
      }
    }
  });
});

describe("bulkEnrichTitle", () => {
  it("says nothing when the label already says everything", () => {
    expect(bulkEnrichTitle(3, 3)).toBeUndefined();
    expect(bulkEnrichTitle(0, 0)).toBeUndefined();
    expect(bulkEnrichTitle(ENRICH_MAX_KEYS, ENRICH_MAX_KEYS)).toBeUndefined();
  });

  it("names the cap when the request has to leave rows behind", () => {
    const cap = bulkEnrichTitle(30, 30);
    expect(cap).toBeDefined();
    expect(cap).toContain(String(ENRICH_MAX_KEYS));
    expect(cap).toContain("later pass");
    expect(cap).not.toBe(enrichSelectionLabel(30, 30));
    // The same branch, further past the cap: 26 sendable, 30 selected.
    expect(bulkEnrichTitle(ENRICH_MAX_KEYS + 1, 30)).toContain(String(ENRICH_MAX_KEYS));
  });

  it("gives the wholly unenrichable selection the none reason verbatim", () => {
    // A disabled control's title is not reliably surfaced as a tooltip and the
    // control is not focusable, so the title alone is unreachable: the same
    // sentence is rendered beside the button, and the `(0 of 3)` count form
    // still has to stand alone.
    expect(bulkEnrichTitle(0, 3)).toBe(ENRICH_NONE_REASON);
  });

  it("names both counts for a partial shortfall without accusing every row", () => {
    const partial = bulkEnrichTitle(1, 3);
    expect(partial).toContain("1 of the 3");
    // An already-enriched row in the selection must not be told it lacks a page,
    // so the single-cause sentence must not appear: the four gates are listed
    // instead. Compared against the constant, not the phrase, because the
    // honest disjunction says `no source page` as one gate of four.
    expect(partial).not.toContain(NO_SOURCE_PAGE_REASON);

    const collided = bulkEnrichTitle(ENRICH_MAX_KEYS, 30);
    expect(collided).toContain("25 of the 30");
    expect(collided).not.toContain(NO_SOURCE_PAGE_REASON);
  });
});
