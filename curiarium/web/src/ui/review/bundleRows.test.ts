import { describe, expect, it } from "vitest";

import {
  ENRICH_MAX_KEYS,
  type BundleDetectCounts,
  type BundleDetectItem,
  type BundleEnrichment,
} from "../../lib/api/bundles";
import {
  ARCHIVE_ONLY_FOLDER_LABEL,
  BLOCKED_REASON_LABELS,
  EXTRACT_AND_CREATE_HINT,
  EXTRACT_AND_CREATE_LABEL,
  EXTRACT_AND_CREATE_ROW_TITLE,
  NO_PAYLOAD_DOOR_REASON,
  NO_SOURCE_PAGE_REASON,
  NOT_SCANNED_REGISTERED_LABEL,
  ROW_STATE_CREATED,
  ROW_STATE_DISMISSED,
  ROW_STATE_IN_PROJECT,
  archiveExtractTitle,
  archiveLine,
  blockedReasonLabel,
  bulkEnrichNoneLine,
  bulkEnrichTitle,
  canEnrich,
  canExtractAndCreate,
  canOfferEnrich,
  canOfferRecheck,
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
  noPayloadDoor,
  payloadOnDisk,
  rowBucket,
  rowStateChip,
  statusLine,
  typedName,
} from "./bundleRows";
// The namespace import is the deletion witness: a re-added `ENRICH_NONE_REASON`
// would still satisfy every named import, but not this `in` check.
import * as bundleRows from "./bundleRows";

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
    expect(
      statusLine(makeBundle({ status: "blocked", blocked_reason: "not_scanned", assets_total: 0 })),
    ).toBe("Blocked - no model file registered by a scan");
    // `makeBundle`'s default `rel_path` is `Moai statue`, a DIRECTORY, so the
    // honest sentence for this row is the folder one. What used to be asserted
    // here called that folder a zip - the exact lie this ticket is about - so the
    // corrected expectation is the fix, not a regression.
    expect(statusLine(makeBundle({ status: "blocked", blocked_reason: "archive_only" }))).toBe(
      "Blocked - folder only - this row is a folder, not an archive",
    );
  });

  it("says which of the two a not_scanned row is once the report calls it non-empty", () => {
    // THE ORPHAN SHAPE - see `orphanRow` for the measured union it is built from.
    // The builder's default `assets_total` is 8, which is precisely the lie this
    // ticket exists to fix: "no model file registered by a scan" next to a counts
    // line that says `8 registered assets`. The corrected sentence is the fix, not
    // a regression, and it is the same arm of the same predicate the door offers
    // read (`payloadOnDisk`), so the two cannot disagree about the row.
    const orphan = orphanRow();
    expect(payloadOnDisk(orphan)).toBe(true);
    expect(blockedReasonLabel(orphan)).toBe(NOT_SCANNED_REGISTERED_LABEL);
    expect(statusLine(orphan)).toBe(`Blocked - ${NOT_SCANNED_REGISTERED_LABEL}`);
    expect(statusLine(orphan)).not.toBe("Blocked - no model file registered by a scan");
    // No count is claimed in a status cell, and no remedy is promised: the arm
    // offers none. Mirrors the `ARCHIVE_ONLY_FOLDER_LABEL` witnesses.
    expect(NOT_SCANNED_REGISTERED_LABEL).not.toMatch(/\d/);
    expect(NOT_SCANNED_REGISTERED_LABEL).not.toContain("\u2013");
    expect(NOT_SCANNED_REGISTERED_LABEL).not.toMatch(/extract|create|scan it|re-?scan|click/i);
    // The empty leg keeps the plain lookup byte for byte (the test above pins the
    // sentence; this pins that the new arm did not swallow it).
    expect(statusLine(notScannedRow())).toBe("Blocked - no model file registered by a scan");
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

describe("blockedReasonLabel", () => {
  // Written out rather than imported, so a rename of the label in `bundleRows.ts`
  // fails here instead of moving both sides of the comparison together.
  const ZIP_SENTENCE = "zip only - extract the archive in place, then the scan registers it";
  const zipRow = (rel_path: string) =>
    makeBundle({
      key: rel_path,
      rel_path,
      status: "blocked",
      blocked_reason: "archive_only",
      archive: null,
    });

  it("keeps the zip sentence byte for byte for a row that IS an archive", () => {
    // The shape the zip sentence was written for: the server builds a zip-only
    // candidate with `archive: null`, so the row's own path is the archive and
    // "extract the archive in place" is true of the thing on disk.
    const row = zipRow("import/01_Fates End - Wizard Tower.zip");
    expect(blockedReasonLabel(row)).toBe(ZIP_SENTENCE);
    expect(statusLine(row)).toBe(`Blocked - ${ZIP_SENTENCE}`);
  });

  it("reads the same suffix predicate the door reads, case included", () => {
    // `Foo.ZIP` is a zip to the server's walk and to `extractPathFor`, so the
    // sentence must call it a zip too. Both sides ask `isArchivePath`, which is
    // the whole point: a badge and a sentence that cannot disagree.
    const row = zipRow("import/Moai.ZIP");
    expect(isArchiveOnly(row)).toBe(true);
    expect(blockedReasonLabel(row)).toBe(ZIP_SENTENCE);
  });

  it("refuses to call the ticket's directory row a zip", () => {
    // The live wire row: `rel_path` is a folder and the twin zip sits beside it,
    // so the Extract door next to this sentence extracts the TWIN, never this
    // folder. The sentence has to be true about the row it is printed on.
    const folder = makeBundle({
      key: "import/01_Fates End - Wizard Tower",
      rel_path: "import/01_Fates End - Wizard Tower",
      status: "blocked",
      blocked_reason: "archive_only",
      archive: { rel_path: "import/01_Fates End - Wizard Tower.zip", twin_verified: true },
    });
    expect(isArchiveOnly(folder)).toBe(true);
    expect(blockedReasonLabel(folder)).toBe(ARCHIVE_ONLY_FOLDER_LABEL);
    expect(statusLine(folder)).toBe(`Blocked - ${ARCHIVE_ONLY_FOLDER_LABEL}`);
    expect(statusLine(folder)).not.toBe(`Blocked - ${ZIP_SENTENCE}`);
    // No count is claimed in a status cell: the en dash belongs to the one count
    // formatter, and an unknown number must never become a false zero here.
    expect(ARCHIVE_ONLY_FOLDER_LABEL).not.toMatch(/\d/);
    expect(ARCHIVE_ONLY_FOLDER_LABEL).not.toContain("\u2013");
  });

  it("says the folder sentence for a directory row with no twin and no door", () => {
    // The other directory shape: `extractPathFor` answers null, so nothing beside
    // this sentence offers extraction at all. One sentence serves both shapes only
    // because it promises no remedy - the door's own title is where a remedy is
    // promised, and there is no door to over-promise here.
    const twinless = makeBundle({
      key: "import/endorphin",
      rel_path: "import/endorphin",
      status: "blocked",
      blocked_reason: "archive_only",
      archive: null,
    });
    expect(isArchiveOnly(twinless)).toBe(false);
    expect(extractPathFor(twinless)).toBeNull();
    expect(statusLine(twinless)).toBe(`Blocked - ${ARCHIVE_ONLY_FOLDER_LABEL}`);
  });

  it("passes an unknown reason through unchanged", () => {
    // A reason from a newer server still reads as its own name rather than as the
    // archive's sentence, whatever the row's path looks like.
    expect(
      blockedReasonLabel(makeBundle({ status: "blocked", blocked_reason: "future-reason" })),
    ).toBe("future-reason");
  });

  it("says blocked with no reason at all", () => {
    // The cell still has to say something: `Blocked - blocked` rather than the
    // dashed stub an empty lookup would leave behind.
    expect(blockedReasonLabel(makeBundle({ status: "blocked", blocked_reason: null }))).toBe(
      "blocked",
    );
    expect(statusLine(makeBundle({ status: "blocked", blocked_reason: null }))).toBe(
      "Blocked - blocked",
    );
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

/** --------------------------------------------------------------------------
 *  MEASURED FIXTURES.
 *
 *  POINTER: `BundleReviewView.test.tsx` transcribes the same measured rows for
 *  its own specimen - a shared module is outside this ticket's touch-set, so the
 *  two blocks are kept in step by hand.
 *
 *  Every field below is quoted from a recorded report of the real corpus kept in
 *  the lane at `.plans/measurements/` - `2026-11-10-bundles-mfquv4-rows-after.json`
 *  (the `rows` array: `rel_path`, `status`, `blocked_reason`, `assets_total`,
 *  `model_count`, `detector`, `confidence`, `cover_reason`, `cover_asset`,
 *  `existing_project_match`) and `2026-11-10-bundles-mfquv4-after.json` (the full
 *  rows: `counts.image|text|other`, `shape`, `twin_verified`,
 *  `existing_project_name`) - and each fixture names the row it came from. Two
 *  fields are stated rather than measured, because the measurements do not carry
 *  them and the ticket's specimen has to be a row the enrichment gates actually
 *  ask about:
 *
 *  - `creator`/`licence`/`licence_url`/`description_excerpt` are null on a
 *    `generic-bundle` row: that detector is a folder-shape guess, so no vendor
 *    page ever answered it. Rows whose detector IS a page read
 *    (`thingiverse-readme`, `thingiverse-sibling-docs`) keep the builder's filled
 *    pair, which is what a page read leaves behind;
 *  - `title`/`name_suggestion` are the measured `rel_path`'s basename.
 *
 *  Where a measurement records a project match it also implies a project id, which
 *  it does not print; those fixtures carry `PROJECT_ID` and say so. `rule_set` is
 *  deliberately absent from every fixture here - it is not a field of
 *  `BundleDetectItem`. */

/** `import/endorphin`: a `blocked`/`archive_only` directory with its verified twin
 *  beside it, `assets_total 8`, counts 1/1/0/0, `generic-bundle`, `low`, shape
 *  `generic`, cover `largest-image` and `cover_asset: true` (the builder's
 *  registered `largest-image` cover is kept, which is that shape). Both doors
 *  exist for this row, so it is the negative leg of every door predicate here. */
function endorphinRow(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return makeBundle({
    key: "import/endorphin",
    rel_path: "import/endorphin",
    title: "endorphin",
    name_suggestion: "endorphin",
    creator: null,
    licence: null,
    licence_url: null,
    description_excerpt: null,
    source_url: null,
    detector: "generic-bundle",
    confidence: "low",
    metadata_shape: "generic",
    model_count: 1,
    image_count: 1,
    text_count: 0,
    other_count: 0,
    assets_total: 8,
    status: "blocked",
    blocked_reason: "archive_only",
    archive: { rel_path: "import/endorphin.zip", twin_verified: true },
    ...overrides,
  });
}

/** `import/Q`: `archive_only` with the verified twin `import/Q.zip`,
 *  `assets_total 6`, counts 3/1/2/0, `generic-bundle`, `low`, shape `generic`. */
function queueQRow(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return makeBundle({
    key: "import/Q",
    rel_path: "import/Q",
    title: "Q",
    name_suggestion: "Q",
    creator: null,
    licence: null,
    licence_url: null,
    description_excerpt: null,
    source_url: null,
    detector: "generic-bundle",
    confidence: "low",
    metadata_shape: "generic",
    model_count: 3,
    image_count: 1,
    text_count: 2,
    other_count: 0,
    assets_total: 6,
    status: "blocked",
    blocked_reason: "archive_only",
    archive: { rel_path: "import/Q.zip", twin_verified: true },
    ...overrides,
  });
}

/** `import/X`: `archive_only` with the NOT-verified twin `import/X.zip`,
 *  `assets_total 7`, counts 1/0/0/0, `generic-bundle`, `low`, shape `generic`.
 *  Its measured cover is a `model-preview`; the builder's registered cover is
 *  kept because no measurement prints a cover path for this row. */
function queueXRow(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return makeBundle({
    key: "import/X",
    rel_path: "import/X",
    title: "X",
    name_suggestion: "X",
    creator: null,
    licence: null,
    licence_url: null,
    description_excerpt: null,
    source_url: null,
    detector: "generic-bundle",
    confidence: "low",
    metadata_shape: "generic",
    model_count: 1,
    image_count: 0,
    text_count: 0,
    other_count: 0,
    assets_total: 7,
    status: "blocked",
    blocked_reason: "archive_only",
    archive: { rel_path: "import/X.zip", twin_verified: false },
    ...overrides,
  });
}

/** `import/Pile`: `archive_only` with the verified twin
 *  `import/Pile Download.zip`, `assets_total 8`, counts 1/1/0/0,
 *  `generic-bundle`, `low`, shape `generic`, AND already claimed by a project
 *  through shared files (`existing_project_match: "assets"`,
 *  `existing_project_name: "Project of the pile's own part"`; the id is
 *  `PROJECT_ID`, the id that match implies). */
function pileRow(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return makeBundle({
    key: "import/Pile",
    rel_path: "import/Pile",
    title: "Pile",
    name_suggestion: "Pile",
    creator: null,
    licence: null,
    licence_url: null,
    description_excerpt: null,
    source_url: null,
    detector: "generic-bundle",
    confidence: "low",
    metadata_shape: "generic",
    model_count: 1,
    image_count: 1,
    text_count: 0,
    other_count: 0,
    assets_total: 8,
    status: "blocked",
    blocked_reason: "archive_only",
    archive: { rel_path: "import/Pile Download.zip", twin_verified: true },
    existing_project_id: PROJECT_ID,
    existing_project_name: "Project of the pile's own part",
    existing_project_match: "assets",
    ...overrides,
  });
}

/** `import/Not Scanned Thing`: the measured `not_scanned` row - `assets_total 0`,
 *  counts 1/1/1/0, `generic-bundle`, `low`, shape `generic`, `source_url` null,
 *  `archive` null, and an UNregistered cover (`cover_asset: false`,
 *  `cover_reason: "largest-image-unregistered"`). The empty leg of `not_scanned`. */
function notScannedRow(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return makeBundle({
    key: "import/Not Scanned Thing",
    rel_path: "import/Not Scanned Thing",
    title: "Not Scanned Thing",
    name_suggestion: "Not Scanned Thing",
    creator: null,
    licence: null,
    licence_url: null,
    description_excerpt: null,
    source_url: null,
    detector: "generic-bundle",
    confidence: "low",
    metadata_shape: "generic",
    model_count: 1,
    image_count: 1,
    text_count: 1,
    other_count: 0,
    cover: {
      asset_id: null,
      rel_path: null,
      reason: "largest-image-unregistered",
      url: null,
      thumb_url: null,
    },
    assets_total: 0,
    status: "blocked",
    blocked_reason: "not_scanned",
    archive: null,
    ...overrides,
  });
}

/** `import/Q/Moai_statue`: the page-read child of `import/Q` - `ready`,
 *  `thingiverse-readme`, `high`, shape `root-docs`, `assets_total 6`, counts
 *  3/1/2/0, and the vendor page of the measured `2493386` folder. */
function moaiStatueRow(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return makeBundle({
    key: "import/Q/Moai_statue",
    rel_path: "import/Q/Moai_statue",
    title: "Moai_statue",
    name_suggestion: "Moai_statue",
    source_url: "https://www.thingiverse.com/thing:2493386",
    detector: "thingiverse-readme",
    confidence: "high",
    metadata_shape: "root-docs",
    model_count: 3,
    image_count: 1,
    text_count: 2,
    other_count: 0,
    assets_total: 6,
    status: "ready",
    blocked_reason: null,
    archive: null,
    ...overrides,
  });
}

/** `Unresolved Thing`: `blocked`/`unresolved_directory`, `assets_total 0`,
 *  `thingiverse-sibling-docs`, `high`, shape `sibling-docs`, counts 1/1/2/0, and
 *  the vendor page the sibling docs name. An unregistered cover. */
function unresolvedThingRow(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return makeBundle({
    key: "Unresolved Thing",
    rel_path: "Unresolved Thing",
    title: "Unresolved Thing",
    name_suggestion: "Unresolved Thing",
    source_url: "https://www.thingiverse.com/thing:2493386",
    detector: "thingiverse-sibling-docs",
    confidence: "high",
    metadata_shape: "sibling-docs",
    model_count: 1,
    image_count: 1,
    text_count: 2,
    other_count: 0,
    cover: {
      asset_id: null,
      rel_path: null,
      reason: "largest-image-unregistered",
      url: null,
      thumb_url: null,
    },
    assets_total: 0,
    status: "blocked",
    blocked_reason: "unresolved_directory",
    archive: null,
    ...overrides,
  });
}

/** `import/lone_export.zip`: the row that IS an archive - `blocked`/`archive_only`,
 *  `assets_total 0`, `archive: null` (the wire sends none for a zip-only row),
 *  `thingiverse-readme`, `high`, shape `root-docs`, counts 3/5/3/0, and a page it
 *  read. An unregistered cover, as measured. */
function loneExportRow(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return makeBundle({
    key: "import/lone_export.zip",
    rel_path: "import/lone_export.zip",
    title: "lone_export.zip",
    name_suggestion: "lone_export.zip",
    source_url: "https://www.thingiverse.com/thing:2493386",
    detector: "thingiverse-readme",
    confidence: "high",
    metadata_shape: "root-docs",
    model_count: 3,
    image_count: 5,
    text_count: 3,
    other_count: 0,
    cover: {
      asset_id: null,
      rel_path: null,
      reason: "largest-image-unregistered",
      url: null,
      thumb_url: null,
    },
    assets_total: 0,
    status: "blocked",
    blocked_reason: "archive_only",
    archive: null,
    ...overrides,
  });
}

/** `import/G`: the pair-only row Mf-quv4 turned `ready` - which thereby LOST its
 *  Extract door, because `extractPathFor` asks the blocked reason first and a
 *  ready row's twin zip is provenance, not a target. `ready`, `blocked_reason`
 *  null, `assets_total 6`, counts 3/1/1/0, `generic-bundle`, `low`, shape
 *  `generic`, archive `{ import/G.zip, twin verified }`. */
function pairOnlyGRow(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return makeBundle({
    key: "import/G",
    rel_path: "import/G",
    title: "G",
    name_suggestion: "G",
    creator: null,
    licence: null,
    licence_url: null,
    description_excerpt: null,
    source_url: null,
    detector: "generic-bundle",
    confidence: "low",
    metadata_shape: "generic",
    model_count: 3,
    image_count: 1,
    text_count: 1,
    other_count: 0,
    assets_total: 6,
    status: "ready",
    blocked_reason: null,
    archive: { rel_path: "import/G.zip", twin_verified: true },
    ...overrides,
  });
}

/** `import/twin`: a `ready` row carrying an UNVERIFIED twin - provenance on a row
 *  that is already workable, so it is no door. `assets_total 3`. */
function twinRow(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return makeBundle({
    key: "import/twin",
    rel_path: "import/twin",
    title: "twin",
    name_suggestion: "twin",
    detector: "thingiverse-readme",
    confidence: "high",
    metadata_shape: "root-docs",
    model_count: 1,
    image_count: 0,
    text_count: 2,
    other_count: 0,
    assets_total: 3,
    status: "ready",
    blocked_reason: null,
    archive: { rel_path: "import/twin.zip", twin_verified: false },
    ...overrides,
  });
}

/** `import/xbox360_shell`: `ready` with `assets_total 33`, `compound-bundle`,
 *  `low`, shape `compound`, counts 31/0/2/0, and an existing project claimed by
 *  the app's own marker (`existing_project_match: "marker"`,
 *  `existing_project_name: "xbox360 shell"`; the id is `PROJECT_ID`, the id that
 *  match implies). Its measured compound group is left out - no predicate here
 *  reads it, and half-quoting it would be worse than omitting it. */
function xbox360Row(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return makeBundle({
    key: "import/xbox360_shell",
    rel_path: "import/xbox360_shell",
    title: "xbox360_shell",
    name_suggestion: "xbox360_shell",
    source_url: null,
    detector: "compound-bundle",
    confidence: "low",
    metadata_shape: "compound",
    model_count: 31,
    image_count: 0,
    text_count: 2,
    other_count: 0,
    assets_total: 33,
    status: "ready",
    blocked_reason: null,
    archive: null,
    existing_project_id: PROJECT_ID,
    existing_project_name: "xbox360 shell",
    existing_project_match: "marker",
    ...overrides,
  });
}

/** `import/Pile/Alpha`: `ready` with `assets_total 3`, `generic-bundle`, `low`,
 *  shape `generic`, counts 1/1/1/0, claimed by a project through shared files
 *  (`existing_project_name: "Project of the two children"`; the id is
 *  `PROJECT_ID`, the id that match implies). */
function pileAlphaRow(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return makeBundle({
    key: "import/Pile/Alpha",
    rel_path: "import/Pile/Alpha",
    title: "Alpha",
    name_suggestion: "Alpha",
    creator: null,
    licence: null,
    licence_url: null,
    description_excerpt: null,
    source_url: null,
    detector: "generic-bundle",
    confidence: "low",
    metadata_shape: "generic",
    model_count: 1,
    image_count: 1,
    text_count: 1,
    other_count: 0,
    assets_total: 3,
    status: "ready",
    blocked_reason: null,
    archive: null,
    existing_project_id: PROJECT_ID,
    existing_project_name: "Project of the two children",
    existing_project_match: "assets",
    ...overrides,
  });
}

/** THE ORPHAN SHAPE HAS NO MEASURED INSTANCE. The corpus never produced a
 *  `not_scanned` row the report calls non-empty, so this fixture is the DECLARED
 *  union of the named measured rows above - field by field, nothing invented
 *  wholesale:
 *
 *  - `status` "blocked", `blocked_reason` "not_scanned", `archive` null, the
 *    counts 1/1/1/0 and `metadata_shape` "generic" from `import/Not Scanned Thing`;
 *  - `detector` "thingiverse-readme", `confidence` "high" and `source_url`
 *    `https://www.thingiverse.com/thing:2493386` from `import/Q/Moai_statue`;
 *  - `assets_total: 8` and the registered `largest-image` cover (the builder's
 *    cover, which is that measured shape) from `import/endorphin`;
 *  - `creator`/`licence`/`licence_url` null, because the ticket's specimen is a
 *    row the server would still happily enrich - that is what makes the refusal
 *    the ticket's subject rather than a metadata fact.
 *
 *  It is the shape Mf-quv4 left behind: a folder that owns no registered MODEL
 *  while a scan has registered files under its key. */
function orphanRow(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return makeBundle({
    key: "import/Not Scanned Thing",
    rel_path: "import/Not Scanned Thing",
    title: "Not Scanned Thing",
    name_suggestion: "Not Scanned Thing",
    creator: null,
    licence: null,
    licence_url: null,
    description_excerpt: null,
    // from `import/Q/Moai_statue`
    source_url: "https://www.thingiverse.com/thing:2493386",
    detector: "thingiverse-readme",
    confidence: "high",
    // from `import/Not Scanned Thing`
    metadata_shape: "generic",
    model_count: 1,
    image_count: 1,
    text_count: 1,
    other_count: 0,
    status: "blocked",
    blocked_reason: "not_scanned",
    archive: null,
    // from `import/endorphin`: a non-empty report, and a registered cover
    assets_total: 8,
    cover: {
      asset_id: "asset-1",
      rel_path: "images/hero.jpg",
      reason: "largest-image",
      url: "/file/asset-1/hero.jpg",
      thumb_url: "/thumb/asset-1.jpg?v=1",
    },
    ...overrides,
  });
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

  // The second cause this ticket adds: a row that DOES name a page, whose payload
  // no door can reach. The sentence is the refusal text of `noPayloadDoor`, the
  // very predicate both offer predicates read - Mf-ny89 part C's rule for the
  // blocked-reason label, extended to the button set.
  it("says the payload refusal for the orphan, which names a page and has files", () => {
    const orphan = orphanRow();
    expect(hasThingSource(orphan)).toBe(true);
    expect(payloadOnDisk(orphan)).toBe(true);
    expect(noPayloadDoor(orphan)).toBe(true);
    expect(enrichUnavailableLine(orphan)).toBe(NO_PAYLOAD_DOOR_REASON);
    // No digit, and no remedy the queue does not offer - the same witnesses the
    // other refusal sentence carries. The remedy wording lives in the door titles, never here:
    // this sentence names the two absent doors and stops, which is what lets the bulk bar speak
    // it too, over a selection it cannot tell anybody what to do about.
    expect(NO_PAYLOAD_DOOR_REASON).not.toMatch(/\d/);
    expect(NO_PAYLOAD_DOOR_REASON).not.toMatch(/extract the archive|re-check|click|press/i);
    expect(enrichUnavailableLine(orphan)).not.toBe(NO_SOURCE_PAGE_REASON);
  });

  it("still says the missing page first, where both causes hold", () => {
    // The measured `import/endorphin` row: no source page AND (once its twin is
    // taken away) no door at all. Cause one wins, because the branch that reads
    // it comes first and the row's own page is the fact an operator acts on.
    const endorphin = endorphinRow();
    expect(hasThingSource(endorphin)).toBe(false);
    expect(enrichUnavailableLine(endorphin)).toBe(NO_SOURCE_PAGE_REASON);
    expect(enrichUnavailableLine(endorphinRow({ archive: null }))).toBe(NO_SOURCE_PAGE_REASON);
  });

  it("stays silent for a page-naming row whose payload is not on disk", () => {
    // Measured `Unresolved Thing`: source page, `assets_total 0` - the door is
    // refused by the row's own blocked reason, which the status cell already says.
    expect(enrichUnavailableLine(unresolvedThingRow())).toBeNull();
    // Same row with a real metadata gap, so the offer predicate itself is asked:
    // still silent, because `noPayloadDoor` is the only thing that may speak here.
    expect(enrichUnavailableLine(unresolvedThingRow({ creator: null, licence: null }))).toBeNull();
  });

  it("stays silent for a page-naming row whose doors are both shut by the row itself", () => {
    // A settled project says so in `createdProjectLine`; an answered page says so
    // in `enrichmentLine`. Neither situation is a payload refusal, and the
    // sentence must not be recruited to say "something is missing".
    const created = moaiStatueRow({ existing_project_id: PROJECT_ID });
    expect(enrichUnavailableLine(created)).toBeNull();
    expect(enrichUnavailableLine(moaiStatueRow({ enrichment: enrichment() }))).toBeNull();
  });
});

describe("bulkEnrichNoneLine", () => {
  // The two causes the bar may speak, each carried by a row that speaks nothing else. The
  // no-page row is deliberately `import/Not Scanned Thing` rather than one of the `archive_only`
  // fixtures: blocked, no page, and no door of its own, so nothing but "one cause or silence"
  // can answer for it - a row with a twin would let the guards silence it by accident.
  const noPageRow = notScannedRow(); // no page, a gap to fill, no create, no extraction
  const orphan = orphanRow(); // names a page, files, no door

  it("speaks the one cause a whole selection shares", () => {
    // Two measured `archive_only` rows, both page-less and both with a gap: the bar has an
    // honest selection-wide sentence, and neither guard applies to it - the page guard asks
    // only whether a selected row names a page, and neither does.
    const rows = [endorphinRow(), queueQRow()];
    expect(bulkEnrichNoneLine(rows)).toBe(NO_SOURCE_PAGE_REASON);
    // The single-row specimen keeps working: one row is a selection of one cause.
    expect(bulkEnrichNoneLine([endorphinRow()])).toBe(NO_SOURCE_PAGE_REASON);
    expect(bulkEnrichNoneLine([orphanRow()])).toBe(NO_PAYLOAD_DOOR_REASON);
  });

  it("says nothing at all when the selected rows do not agree on a cause", () => {
    // THE MIS-ATTRIBUTION WITNESS. `import/Not Scanned Thing` says "no vendor page", the orphan
    // says "no door for these files" - each TRUE of itself, and either one false about the
    // other. Reciting whichever one the queue happened to put first is the paragraph this ticket
    // deleted, in miniature, so both queue orders are silent (a `find`-style "first non-null"
    // prints the page sentence one way and the payload sentence the other, and neither is
    // redressable by the guards: neither of these rows is creatable or extractable, and the
    // page sentence here is contradicted by the orphan naming a page).
    expect(bulkEnrichNoneLine([noPageRow, orphanRow()])).toBeNull();
    expect(bulkEnrichNoneLine([orphanRow(), noPageRow])).toBeNull();
    // Not vacuously: each row on its own does have a sentence to say.
    expect(enrichUnavailableLine(noPageRow)).toBe(NO_SOURCE_PAGE_REASON);
    expect(enrichUnavailableLine(orphan)).toBe(NO_PAYLOAD_DOOR_REASON);
  });

  it("does not claim no create while a selected row is creatable", () => {
    // The orphan is the only row with a sentence; `import/G` is the bar's other selected row.
    // Its page, creator and licence are stated (the measurement carries none), which is what
    // makes it silent on its own doors - and a silent row is precisely the case the guard, not
    // the one-cause rule, has to answer. Nothing here is sendable, so the bar really would
    // speak if the guard did not stop it.
    const settledFields = { creator: "sculpteo", licence: "CC BY" } as const;
    const creatable = pairOnlyGRow(settledFields);
    expect(isCreatable(creatable)).toBe(true);
    expect(enrichUnavailableLine(creatable)).toBeNull();
    expect(canOfferEnrich(creatable)).toBe(false);
    expect(bulkEnrichNoneLine([orphanRow(), creatable])).toBeNull();
  });

  it("does not claim no extraction while a selected row has an archive door", () => {
    // `import/endorphin` again, this time with its metadata already filled in (stated, as
    // above) so it says nothing for itself while its twin zip remains a live extraction.
    const extractable = endorphinRow({ creator: "sculpteo", licence: "CC BY" });
    expect(isArchiveOnly(extractable)).toBe(true);
    expect(enrichUnavailableLine(extractable)).toBeNull();
    expect(canOfferEnrich(extractable)).toBe(false);
    expect(bulkEnrichNoneLine([orphanRow(), extractable])).toBeNull();
  });

  it("does not claim no vendor page while a selected row names one", () => {
    // `import/Q/Moai_statue` with its page record: silent for itself (another line already
    // says the page answered), but it DOES name a page, so the page sentence may not be spoken
    // over it - and one-cause-or-silence cannot catch this, because it speaks nothing.
    const answered = moaiStatueRow({ enrichment: enrichment() });
    expect(hasThingSource(answered)).toBe(true);
    expect(enrichUnavailableLine(answered)).toBeNull();
    expect(canOfferEnrich(answered)).toBe(false);
    expect(bulkEnrichNoneLine([noPageRow, answered])).toBeNull();
  });

  it("stays silent when no selected row has a sentence, though nothing is sendable", () => {
    // The branch a `find(...) ?? null` never exercises, because it also answers null: two
    // settled rows - `import/Pile/Alpha` and `import/xbox360_shell`, both with real measured
    // fields and neither enrichable. Nothing is sendable, so the bar is in its
    // "explain the refusal" state, and every row in it is silent: the honest answer is that the
    // bar has no sentence, so it renders none.
    const rows = [pileAlphaRow(), xbox360Row()];
    for (const row of rows) {
      expect(enrichUnavailableLine(row)).toBeNull();
      expect(canOfferEnrich(row)).toBe(false);
    }
    expect(bulkEnrichNoneLine(rows)).toBeNull();
  });

  it("stays silent for an empty selection", () => {
    expect(bulkEnrichNoneLine([])).toBeNull();
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

  it("says nothing at all when nothing is sendable, because there is no count to explain", () => {
    // The paragraph this ticket deleted used to be the title here. A disabled
    // control's title is not reliably surfaced as a tooltip and the control is not
    // focusable, so it was unreachable anyway - and it guessed four gates for a
    // selection it had not looked at. The bar now renders the selected rows' own
    // sentence (`enrichUnavailableLine`, decided per row by the predicates that
    // refused each door), so the title stays silent.
    expect(bulkEnrichTitle(0, 3)).toBeUndefined();
    expect(bulkEnrichTitle(0, 1)).toBeUndefined();
    expect(bulkEnrichTitle(0, 0)).toBeUndefined();
  });

  it("names both counts for a partial shortfall without accusing every row", () => {
    const partial = bulkEnrichTitle(1, 3);
    expect(partial).toContain("1 of the 3");
    // An already-enriched row in the selection must not be told it lacks a page,
    // so the single-cause sentence must not appear: the four gates are listed
    // instead. Compared against the constant, not the phrase, because the
    // honest disjunction says `no source page` as one gate of four.
    expect(partial).not.toContain(NO_SOURCE_PAGE_REASON);
    // The guessed universal is gone from the count branch entirely: this sentence
    // speaks the count and nothing else, so it can name no gate at all.
    expect(bulkEnrichTitle(1, 3)).not.toMatch(/source page|creator|licence|project/);

    const collided = bulkEnrichTitle(ENRICH_MAX_KEYS, 30);
    expect(collided).toContain("25 of the 30");
    expect(collided).not.toContain(NO_SOURCE_PAGE_REASON);
  });
});

describe("the deleted bulk paragraph", () => {
  it("is gone, and no new blocked-reason vocabulary took its place", () => {
    // A re-added `ENRICH_NONE_REASON` would still satisfy every named import in
    // this file, so the witness reads the module's own export table.
    expect("ENRICH_NONE_REASON" in bundleRows).toBe(false);
    // The ticket adds no new status and no new blocked reason: the three the
    // server can answer are the only keys this client translates.
    expect(Object.keys(BLOCKED_REASON_LABELS).sort()).toEqual([
      "archive_only",
      "not_scanned",
      "unresolved_directory",
    ]);
  });
});

describe("payloadOnDisk", () => {
  it("says the measured empty row holds no payload", () => {
    // `import/Not Scanned Thing` measured `assets_total: 0`.
    expect(payloadOnDisk(notScannedRow())).toBe(false);
  });

  it("says the measured on-disk row holds one", () => {
    // `import/endorphin` measured `assets_total: 8`.
    expect(payloadOnDisk(endorphinRow())).toBe(true);
  });

  it("treats a payload that omits the count as no payload", () => {
    // An absent count is not a payload: the comparison is `> 0`, so a payload from
    // before the field existed answers false rather than throwing or lying.
    const withoutCount = { ...endorphinRow() } as Partial<BundleDetectItem>;
    delete withoutCount.assets_total;
    expect(payloadOnDisk(withoutCount as BundleDetectItem)).toBe(false);
  });
});

describe("noPayloadDoor", () => {
  // The full truth table, over measured rows only. `true` is the row this ticket
  // exists for: files the report counts, no create, no extraction.
  const truthTable: [string, BundleDetectItem, boolean][] = [
    // `import/endorphin`: files AND a verified twin to extract - a door exists.
    ["import/endorphin, whose twin zip is the door", endorphinRow(), false],
    // `import/Q` and `import/X`: files AND a twin (verified, and not verified).
    ["import/Q, whose verified twin is the door", queueQRow(), false],
    ["import/X, whose unverified twin is still the door", queueXRow(), false],
    // `import/Pile`: files, already claimed by a project, AND a twin.
    ["import/Pile, already claimed and still extractable", pileRow(), false],
    // `import/G`: the report counts files and the twin is no door, but the row is
    // `ready`, so Create is live.
    ["import/G, which Mf-quv4 turned ready", pairOnlyGRow(), false],
    // `import/lone_export.zip` and `Unresolved Thing`: no files at all, so the
    // predicate never gets to ask about doors.
    ["import/lone_export.zip, an empty zip row", loneExportRow(), false],
    ["Unresolved Thing, an empty blocked row", unresolvedThingRow(), false],
    // The two already-created rows: files on disk, no create (settled), no archive.
    ["import/xbox360_shell, created by its marker", xbox360Row(), true],
    ["import/Pile/Alpha, created by shared files", pileAlphaRow(), true],
    // The orphan (declared union of measured rows - see its fixture).
    ["the registered not_scanned orphan", orphanRow(), true],
  ];

  it.each(truthTable)("answers %s", (_label, row, expected) => {
    expect(noPayloadDoor(row)).toBe(expected);
  });

  it("flips on each of the three facts it asks", () => {
    // The orphan's three facts, taken away one at a time.
    expect(noPayloadDoor(orphanRow({ assets_total: 0 }))).toBe(false); // not on disk
    expect(noPayloadDoor(orphanRow({ status: "ready", blocked_reason: null }))).toBe(false); // creates
    // ...and the third leg really is the extraction. `import/endorphin` with its
    // twin removed is the measured directory shape that has files and no door at
    // all - which is precisely the row the twin is there to serve.
    expect(extractPathFor(endorphinRow())).toBe("import/endorphin.zip");
    expect(extractPathFor(endorphinRow({ archive: null }))).toBeNull();
    expect(noPayloadDoor(endorphinRow())).toBe(false);
    expect(noPayloadDoor(endorphinRow({ archive: null }))).toBe(true);
  });

  it("is a claim about the report, never a write decision", () => {
    // The same key answers differently under two reports, which is exactly why no
    // create, extract or attach may read this predicate.
    expect(payloadOnDisk(notScannedRow())).toBe(false);
    expect(payloadOnDisk(notScannedRow({ assets_total: 8 }))).toBe(true);
    // And `isCreatable` stays the server-shaped answer, untouched by the count.
    expect(isCreatable(endorphinRow({ status: "ready", blocked_reason: null }))).toBe(true);
    expect(isCreatable(endorphinRow({ status: "ready", blocked_reason: null, assets_total: 0 }))).toBe(
      true,
    );
  });
});

describe("canOfferEnrich / canOfferRecheck", () => {
  it("refuses the orphan the server would still happily enrich", () => {
    // THE TICKET. `canEnrich` is the server-shaped question and is unchanged: the
    // refusal is layered on top of it, once, in the offer predicate the buttons read.
    const orphan = orphanRow();
    expect(canEnrich(orphan)).toBe(true);
    expect(canOfferEnrich(orphan)).toBe(false);
    expect(noPayloadDoor(orphan)).toBe(true);
  });

  it("gives the door back to the same row the report calls empty", () => {
    // `assets_total: 0` is the measured `import/Not Scanned Thing` value: with no
    // payload to strand there is nothing to refuse, and the row is work again.
    const empty = orphanRow({ assets_total: 0 });
    expect(canEnrich(empty)).toBe(true);
    expect(canOfferEnrich(empty)).toBe(true);
    expect(noPayloadDoor(empty)).toBe(false);
  });

  it("refuses the re-check door the same way, and only there", () => {
    const answered = orphanRow({ enrichment: enrichment() });
    expect(canRecheck(answered)).toBe(true);
    expect(canOfferRecheck(answered)).toBe(false);
    expect(canOfferEnrich(answered)).toBe(false); // the page already answered

    const answeredEmpty = orphanRow({ enrichment: enrichment(), assets_total: 0 });
    expect(canRecheck(answeredEmpty)).toBe(true);
    expect(canOfferRecheck(answeredEmpty)).toBe(true);

    // A settled project is refused by `canRecheck` itself, not by the layer: the
    // layer adds a refusal, it never removes one.
    const created = orphanRow({ enrichment: enrichment(), existing_project_id: PROJECT_ID });
    expect(canRecheck(created)).toBe(false);
    expect(canOfferRecheck(created)).toBe(false);
  });

  it("refuses nothing for a row that has a create", () => {
    const creatable = orphanRow({ status: "ready", blocked_reason: null });
    expect(noPayloadDoor(creatable)).toBe(false);
    expect(canOfferEnrich(creatable)).toBe(true);
    // The re-check door on the same shape needs the page record `canRecheck` asks
    // for; the payload layer then refuses nothing, because Create is live.
    const answeredCreatable = orphanRow({
      status: "ready",
      blocked_reason: null,
      enrichment: enrichment(),
    });
    expect(canRecheck(answeredCreatable)).toBe(true);
    expect(canOfferRecheck(answeredCreatable)).toBe(true);
    // A measured row with a twin keeps its enrichment offer too: the door exists.
    expect(canOfferEnrich(endorphinRow())).toBe(false); // no source page at all
    expect(canOfferEnrich(endorphinRow({ source_url: "https://www.thingiverse.com/thing:2493386" }))).toBe(
      true,
    );
  });
});

describe("enrichment is never the sole live action on an on-disk row", () => {
  // THE THEOREM. For every row shape this file knows: a row whose payload is on
  // disk and which the queue offers for enrichment ALWAYS has a create or an
  // extraction beside it. Table-driven over the file's own fixtures plus the
  // measured ones, so a new fixture is caught by the same law.
  const rows: [string, BundleDetectItem][] = [
    ["the ready builder row", readyHigh],
    ["the blocked builder row", blockedHigh],
    ["the low builder row", readyLow],
    ["a source-less generic bundle", makeBundle({ source_url: null, detector: "generic-bundle", confidence: "low" })],
    ["an already-created row", makeBundle({ existing_project_id: PROJECT_ID })],
    ["a dismissed row", makeBundle({ dismissed: true })],
    ["a zip-only row", makeBundle({ rel_path: "import/Moai.zip", status: "blocked", blocked_reason: "archive_only", archive: null })],
    ["an answered page", makeBundle({ enrichment: enrichment() })],
    ["import/endorphin", endorphinRow()],
    ["import/Q", queueQRow()],
    ["import/X", queueXRow()],
    ["import/Pile", pileRow()],
    ["import/Not Scanned Thing", notScannedRow()],
    ["import/Q/Moai_statue", moaiStatueRow()],
    ["import/Q/Moai_statue with a gap", moaiStatueRow({ creator: null, licence: null })],
    ["Unresolved Thing", unresolvedThingRow()],
    ["import/lone_export.zip", loneExportRow()],
    ["import/G", pairOnlyGRow()],
    ["import/G once it names a page", pairOnlyGRow({ source_url: "https://www.thingiverse.com/thing:2493386" })],
    ["import/twin", twinRow()],
    ["import/xbox360_shell", xbox360Row()],
    ["import/Pile/Alpha", pileAlphaRow()],
    ["the registered not_scanned orphan", orphanRow()],
    ["the empty not_scanned row", notScannedRow({ source_url: "https://www.thingiverse.com/thing:2493386", detector: "thingiverse-readme", confidence: "high" })],
  ];

  it.each(rows)("never strands %s", (_label, row) => {
    if (payloadOnDisk(row) && canOfferEnrich(row)) {
      expect(isCreatable(row) || extractPathFor(row) !== null).toBe(true);
    }
    // The same law in its contrapositive form, so a row that HAS no door can never
    // be offered, whatever else its fields say.
    expect(canOfferEnrich(row) && noPayloadDoor(row)).toBe(false);
    expect(canOfferRecheck(row) && noPayloadDoor(row)).toBe(false);
  });

  it("is not a vacuous table: rows do reach the antecedent", () => {
    // Without this, the witness above could pass on an empty set forever.
    const offered = rows.filter(([, row]) => payloadOnDisk(row) && canOfferEnrich(row));
    expect(offered.length).toBeGreaterThan(0);
    const labels = offered.map(([label]) => label);
    expect(labels).toContain("import/Q/Moai_statue with a gap");
    expect(labels).toContain("import/G once it names a page");
  });

  it("keeps import/G's lost extract door lost, with zero new branches", () => {
    // The row Mf-quv4 turned `ready` thereby LOST its Extract door, and it stays
    // lost: extraction from a verified twin is a proven no-op (`files: 0`,
    // `already_present`, pinned by Mf-ny89 part B), and a door that cannot succeed
    // must not exist. The queue needs no new branch to say so - the row is
    // creatable, so a live action already stands beside enrichment.
    const row = pairOnlyGRow();
    expect(isCreatable(row)).toBe(true);
    expect(extractPathFor(row)).toBeNull();
    expect(isArchiveOnly(row)).toBe(false);
    expect(canExtractAndCreate(row)).toBe(false);
    expect(payloadOnDisk(row)).toBe(true);
    // Measured `import/G` is a `generic-bundle` row and names no page, so plain
    // enrichment is not even in play; the same row once it DOES name a page is
    // where the theorem is asked, and the lost door stays lost there too.
    expect(canOfferEnrich(row)).toBe(false);
    const withPage = pairOnlyGRow({ source_url: "https://www.thingiverse.com/thing:2493386" });
    expect(canOfferEnrich(withPage)).toBe(true);
    expect(isCreatable(withPage)).toBe(true);
    expect(extractPathFor(withPage)).toBeNull();
    expect(noPayloadDoor(withPage)).toBe(false);
  });
});
