// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within, type RenderResult } from "@testing-library/react";

// Both surfaces must answer identically: `ArchiveExtractAction` reaches the
// barrel and the one-step door reaches the domain module, and a test that mocks
// them with two different pairs would pass while the app imported two
// different functions.
const archiveApi = vi.hoisted(() => ({
  listArchiveEntries: vi.fn(),
  extractArchive: vi.fn(),
}));
vi.mock("../../lib/api", () => ({
  listArchiveEntries: archiveApi.listArchiveEntries,
  extractArchive: archiveApi.extractArchive,
}));
vi.mock("../../lib/api/imports", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api/imports")>()),
  listArchiveEntries: archiveApi.listArchiveEntries,
  extractArchive: archiveApi.extractArchive,
}));

// The cover's derived half renders a snapshot. Hoisted so the props can be
// asserted (a null renderer would prove nothing), and lazy-resolved the same way
// `ui/asset-grid/preview.test.tsx` does it. `generateModelSnapshot` is part of
// the factory because `bundleCovers` imports it for its default renderer: a
// missing key would make the fire-and-forget create path throw.
const modelSnapshotProps = vi.hoisted(() => [] as Array<Record<string, unknown>>);

vi.mock("../ModelViewer", () => ({
  ModelSnapshot: (props: Record<string, unknown>) => {
    modelSnapshotProps.push(props);
    // A marker, not `null`: the props alone cannot tell the row cell from the
    // detail fact (they are identical), so the two sites are only countable if
    // each one leaves something in the DOM to find.
    return <span data-model-snapshot="" />;
  },
  generateModelSnapshot: vi.fn(),
}));

// The split door and the guided panel import their domain modules directly, so
// their calls are mocked there rather than on the barrel. The originals are
// spread back so the suite's other imports keep working.
const bundleApi = vi.hoisted(() => ({
  createBundles: vi.fn(),
  enrichBundles: vi.fn(),
}));
vi.mock("../../lib/api/bundles", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api/bundles")>()),
  createBundles: bundleApi.createBundles,
  enrichBundles: bundleApi.enrichBundles,
}));

const operationsApi = vi.hoisted(() => ({
  planOperation: vi.fn(),
  submitOperation: vi.fn(),
  getOperation: vi.fn(),
  undoOperation: vi.fn(),
}));
vi.mock("../../lib/api/operations", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api/operations")>()),
  planOperation: operationsApi.planOperation,
  submitOperation: operationsApi.submitOperation,
  getOperation: operationsApi.getOperation,
  undoOperation: operationsApi.undoOperation,
}));

import { extractArchive, listArchiveEntries, type ArchiveExtractResult } from "../../lib/api";
import type { LibraryScanStatus } from "../../lib/api/settings";
import {
  ENRICH_MAX_KEYS,
  type BundleCreateItemResult,
  type BundleCreateResponse,
  type BundleDetectItem,
  type BundleDuplicateGroup,
  type BundleMatch,
  type BundleReport,
} from "../../lib/api/bundles";
import {
  getOperation,
  planOperation,
  submitOperation,
  undoOperation,
  type Operation,
  type OperationPlan,
  type OperationReview,
  type OperationStep,
  type OperationUndo,
} from "../../lib/api/operations";
import type { SettleOutcome } from "../../lib/scanSettle";
import { UnauthorizedError } from "../../lib/api/http";
import { getToasts, resetToasts } from "../../lib/toast";
import Toaster from "../Toaster";
import BundleReviewView from "./BundleReviewView";
import {
  EXTRACT_AND_CREATE_ROW_TITLE,
  NO_PAYLOAD_DOOR_REASON,
  NO_SOURCE_PAGE_REASON,
  SHAPE_LABELS,
  SHAPE_MANUAL,
  bulkEnrichTitle,
  isArchiveOnly,
  needsDecision,
} from "./bundleRows";
import type { BundleDetectState } from "./useBundleDetect";
import type { LibraryScanState } from "./useLibraryScan";

const listArchiveEntriesMock = vi.mocked(listArchiveEntries);
const extractArchiveMock = vi.mocked(extractArchive);
const planOperationMock = vi.mocked(planOperation);
const submitOperationMock = vi.mocked(submitOperation);
const undoOperationMock = vi.mocked(undoOperation);
const getOperationMock = vi.mocked(getOperation);

const ARCHIVE_ENTRIES = [
  { path: "Moai.stl", size: 2048 },
  { path: "readme.txt", size: 512 },
];

const EXTRACT_RESULT: ArchiveExtractResult = {
  path: "import/Moai.zip",
  destination: "import/Moai",
  files: 2,
  bytes: 2560,
  already_present: false,
  scan_started: true,
};

/** The matches every fixture serves, from the one helper that knows the wire's
 *  order: the shape the row was detected as first (the recommendation), `manual`
 *  last. A row with groups is offered as `compound`, because that is the shape
 *  its groups belong to. No fixture spells a `matches` list out, so a test that
 *  wants a different set is visibly overriding rather than mutating a copy. */
function servedMatches(row: BundleDetectItem): BundleMatch[] {
  const recommended = row.groups && row.groups.length > 0 ? "compound" : row.metadata_shape;
  const counts = {
    model_count: row.model_count,
    image_count: row.image_count,
    text_count: row.text_count,
    other_count: row.other_count,
  };
  return [
    {
      shape: recommended,
      rule: row.detector,
      confidence: row.confidence,
      ...counts,
      ...(row.groups ? { groups: row.groups } : {}),
      note: null,
    },
    { shape: "manual", rule: "manual", confidence: row.confidence, ...counts, note: null },
  ];
}

function makeBundle(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  const row: BundleDetectItem = {
    key: "Moai",
    rel_path: "Moai",
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
  // Built AFTER the overrides merged, so the matches speak about the fields the
  // fixture ended up with. A test that needs no `matches` at all deletes it, the
  // way the payload-shape witnesses delete `assets_total`.
  return { ...row, matches: overrides.matches ?? servedMatches(row) };
}

/** A row the detail pane offers its one-step create on: high confidence, ready,
 *  and a shape the server's AUTO run accepts. Only such a row renders
 *  `Create project`; every other live row is a guided review. A row with groups
 *  still recommends `compound` (that is where `exclude_groups` is built), so the
 *  AUTO fixture is about the BAND and the door, not the chosen shape. */
function autoRow(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return makeBundle({ ...overrides, metadata_shape: "one-project" });
}

const moai = makeBundle();
const cableClip = makeBundle({
  key: "Cable_clip",
  rel_path: "Cable_clip",
  title: "Cable clip",
  name_suggestion: "Cable clip",
  creator: null,
  licence: null,
  licence_url: null,
  source_url: null,
  detector: "generic-bundle",
  confidence: "low",
  metadata_shape: "generic",
  description_excerpt: null,
  model_count: 1,
  image_count: 1,
  text_count: 1,
  other_count: 0,
  cover: {
    asset_id: null,
    rel_path: "preview.png",
    reason: "largest-image-unregistered",
    url: null,
    thumb_url: null,
  },
  assets_total: 0,
});
const legacyVase = makeBundle({
  key: "Legacy_vase",
  rel_path: "Legacy_vase",
  title: "Legacy vase",
  name_suggestion: "Legacy vase",
  creator: null,
  licence: null,
  licence_url: null,
  source_url: null,
  detector: "thingiverse-sibling-docs",
  metadata_shape: "sibling-docs",
  description_excerpt: null,
  model_count: 1,
  image_count: 2,
  text_count: 0,
  cover: {
    asset_id: "asset-3",
    rel_path: "images/vase.jpg",
    reason: "largest-image",
    url: "/file/asset-3/vase.jpg",
    thumb_url: "/thumb/asset-3.jpg?v=2",
  },
  assets_total: 3,
  status: "blocked",
  blocked_reason: "unresolved_directory",
});
/** A zip-only bundle: the row IS the archive, so the wire sends `archive: null`
 *  and its own path ends `.zip`. The nested path is a shape the zip-only pass can
 *  emit - an archive in `import/` whose extracted twin is not beside it - and the
 *  parent split from the ready `Moai` row is what makes it a row of its own: a
 *  zip a directory was paired with is consumed as that directory's twin. */
const moaiZip = makeBundle({
  key: "import/Moai.zip",
  rel_path: "import/Moai.zip",
  title: "Moai zip",
  name_suggestion: "Moai zip",
  creator: null,
  licence: null,
  licence_url: null,
  source_url: null,
  detector: "generic-bundle",
  confidence: "low",
  metadata_shape: "generic",
  description_excerpt: null,
  model_count: 0,
  image_count: 0,
  text_count: 0,
  other_count: 1,
  cover: {
    asset_id: null,
    rel_path: null,
    reason: "largest-image-unregistered",
    url: null,
    thumb_url: null,
  },
  assets_total: 1,
  status: "blocked",
  blocked_reason: "archive_only",
  archive: null,
});

/** Two zip-only rows, the same shape, so a row change swaps `path` and `label`
 *  without changing which children render: the extract action stays the same
 *  instance unless something keys it. */
const ALPHA_KEY = "Alpha.zip";
const BETA_KEY = "Beta.zip";
const ALPHA_ENTRIES = [
  { path: "alpha.stl", size: 1024 },
  { path: "alpha.txt", size: 256 },
];
const BETA_ENTRIES = [
  { path: "beta.stl", size: 1024 },
  { path: "beta.txt", size: 256 },
  { path: "beta.jpg", size: 512 },
];
const alphaZip = makeBundle({
  key: ALPHA_KEY,
  rel_path: ALPHA_KEY,
  title: "Alpha",
  name_suggestion: "Alpha",
  creator: null,
  licence: null,
  licence_url: null,
  source_url: null,
  detector: "generic-bundle",
  confidence: "low",
  metadata_shape: "generic",
  description_excerpt: null,
  model_count: 0,
  image_count: 0,
  text_count: 0,
  other_count: 1,
  cover: {
    asset_id: null,
    rel_path: null,
    reason: "largest-image-unregistered",
    url: null,
    thumb_url: null,
  },
  assets_total: 1,
  status: "blocked",
  blocked_reason: "archive_only",
  archive: null,
});
const betaZip = makeBundle({
  key: BETA_KEY,
  rel_path: BETA_KEY,
  title: "Beta",
  name_suggestion: "Beta",
  creator: null,
  licence: null,
  licence_url: null,
  source_url: null,
  detector: "generic-bundle",
  confidence: "low",
  metadata_shape: "generic",
  description_excerpt: null,
  model_count: 0,
  image_count: 0,
  text_count: 0,
  other_count: 1,
  cover: {
    asset_id: null,
    rel_path: null,
    reason: "largest-image-unregistered",
    url: null,
    thumb_url: null,
  },
  assets_total: 1,
  status: "blocked",
  blocked_reason: "archive_only",
  archive: null,
});
/** A third archive of the identical shape, so the many-archive notice can name
 *  more than two verdicts - and so the three verdicts it names (created,
 *  partial, already-created) are one render's worth of the same door. */
const GAMMA_KEY = "Gamma.zip";
const gammaZip = makeBundle({
  key: GAMMA_KEY,
  rel_path: GAMMA_KEY,
  title: "Gamma",
  name_suggestion: "Gamma",
  creator: null,
  licence: null,
  licence_url: null,
  source_url: null,
  detector: "generic-bundle",
  confidence: "low",
  metadata_shape: "generic",
  description_excerpt: null,
  model_count: 0,
  image_count: 0,
  text_count: 0,
  other_count: 1,
  cover: {
    asset_id: null,
    rel_path: null,
    reason: "largest-image-unregistered",
    url: null,
    thumb_url: null,
  },
  assets_total: 1,
  status: "blocked",
  blocked_reason: "archive_only",
  archive: null,
});

/** A medium row: the folder name carried a Thingiverse id, so the source URL is
 *  derived from it and no creator or licence is known. */
const mediumDragon = makeBundle({
  key: "Dragon_5390184",
  rel_path: "Dragon_5390184",
  title: "Articulated ice dragon",
  name_suggestion: "Articulated ice dragon",
  creator: null,
  licence: null,
  licence_url: null,
  source_url: "https://www.thingiverse.com/thing:5390184",
  detector: "thingiverse-id-name",
  confidence: "medium",
  metadata_shape: "id-name",
  description_excerpt: null,
  model_count: 1,
  image_count: 0,
  text_count: 0,
  other_count: 0,
  cover: {
    asset_id: null,
    rel_path: "dragon.stl",
    reason: "largest-image-unregistered",
    url: null,
    thumb_url: null,
  },
  assets_total: 0,
});

/** A compound row: one product in several parts, one of them somebody else's
 *  thing. `separable` is the service's answer to "was this a directory of its
 *  own before the compound rule absorbed it?", and only a separable group may be
 *  excluded - the server refuses the rest. */
const compound = makeBundle({
  key: "StageTop",
  rel_path: "StageTop",
  title: "StageTop Wave One",
  name_suggestion: "StageTop Wave One",
  confidence: "low",
  groups: [
    {
      name: "StageTop Wave One - Personal Use",
      model_count: 45,
      foreign: false,
      separable: true,
    },
    { name: "Gearbox", model_count: 3, foreign: true, separable: true },
  ],
});

/** The same shape with no foreign part, so nothing here may be excluded. */
const compoundOwn = makeBundle({
  key: "Vase",
  rel_path: "Vase",
  title: "Vase",
  name_suggestion: "Vase",
  groups: [
    { name: "Body", model_count: 2, foreign: false, separable: true },
    { name: "Lid", model_count: 1, foreign: false, separable: true },
  ],
});

/** A foreign group the server would NOT accept an exclusion for: it was never a
 *  row of its own, so the checkbox must not exist at all. */
const compoundNotSeparable = makeBundle({
  key: "Borrowed",
  rel_path: "Borrowed",
  title: "Borrowed",
  name_suggestion: "Borrowed",
  groups: [{ name: "Gearbox", model_count: 3, foreign: true, separable: false }],
});

/** A row whose served readings disagree about cardinality: the recommendation
 *  imports the whole folder as one project, and the proposal engine's dissenting
 *  readings (`split-top-folders` / `split-model-folders`) are offered VISIBLY but
 *  with `creatable: false`, because the server refuses them with `needs-arrange`
 *  and writes nothing. Their `note` is the server's own reason, copied verbatim:
 *  the client invents no copy for a refusal it did not decide. */
const SPLIT_NOTE =
  "needs arranging first: one project per top folder; fragments a single product if it was one";
const splitTopFolders = makeBundle({
  key: "Multi",
  rel_path: "Multi",
  title: "Multi folder",
  name_suggestion: "Multi folder",
  creator: null,
  licence: null,
  licence_url: null,
  source_url: null,
  detector: "generic-bundle",
  confidence: "low",
  metadata_shape: "generic",
  description_excerpt: null,
  matches: [
    {
      shape: "generic",
      rule: "generic-bundle",
      confidence: "low",
      model_count: 3,
      image_count: 0,
      text_count: 0,
      other_count: 0,
      note: null,
    },
    {
      shape: "split-top-folders",
      rule: "engine:split-top-folders",
      confidence: "low",
      model_count: 3,
      image_count: 0,
      text_count: 0,
      other_count: 0,
      note: SPLIT_NOTE,
      creatable: false,
    },
    {
      shape: SHAPE_MANUAL,
      rule: "manual",
      confidence: "low",
      model_count: 3,
      image_count: 0,
      text_count: 0,
      other_count: 0,
      note: null,
    },
  ],
});

/** The compound row the pane's `Create project` body is reachable on. Only an
 *  AUTO row renders that door now, and AUTO is decided by the BAND and the
 *  detector's shape - not the chosen reading - so the fixture is high/one-project
 *  while the served recommendation stays `compound` (from its groups), which is
 *  where `exclude_groups` is built. The exclusion wiring is what these tests
 *  cover; the guided panel does not carry it. */
const autoCompound = autoRow({ ...compound, confidence: "high" });

/** The same repin for the chooser rows: a `generic`/low folder is a guided
 *  review now, so the tests about a chosen reading refusing the create door need
 *  the AUTO band that door renders on. The served readings are untouched. */
const autoSplitTopFolders = autoRow({ ...splitTopFolders, confidence: "high" });

function makeReport(
  bundles: BundleDetectItem[],
  overrides: Partial<BundleReport> = {},
): BundleReport {
  const ready = bundles.filter(row => row.status === "ready").length;
  return {
    root: "",
    truncated: false,
    counts: {
      total: bundles.length,
      // The wire always carries the pair now: this fixture has every row on it,
      // so nothing is withheld unless a test overrides the numbers.
      offered: bundles.length,
      withheld: 0,
      high: bundles.filter(row => row.confidence === "high").length,
      medium: bundles.filter(row => row.confidence === "medium").length,
      low: bundles.filter(row => row.confidence === "low").length,
      ready,
      blocked: bundles.length - ready,
      dismissed: bundles.filter(row => row.dismissed).length,
      already_created: bundles.filter(row => row.existing_project_id).length,
    },
    bundles,
    stale_dismissals: [],
    ...overrides,
  };
}

const DUPLICATE_GROUPS: BundleDuplicateGroup[] = [
  { sha256: "a".repeat(64), size: 2048, paths: ["Moai.zip", "Moai (1).zip"], reclaimable_bytes: 2048 },
  { sha256: "b".repeat(64), size: 1024, paths: ["Ender3.zip", "Ender3 (1).zip"], reclaimable_bytes: 1024 },
];

// Deliberately disagrees with duplicates_total_bytes (3072): a copy built from
// the group fields would not equal this, so only a verbatim copy can pass.
const DUPLICATES_TEXT = [
  "# duplicate archives: 2 group(s), 5120 bytes reclaimable",
  `# sha256 ${"a".repeat(64)} size 2048 reclaimable 2048`,
  "keep\t2048\tMoai.zip",
  "duplicate\t2048\tMoai (1).zip",
  `# sha256 ${"b".repeat(64)} size 1024 reclaimable 1024`,
  "keep\t1024\tEnder3.zip",
  "duplicate\t1024\tEnder3 (1).zip",
].join("\n");

function withDuplicates(overrides: Partial<BundleReport> = {}): BundleReport {
  return makeReport([moai], {
    duplicates: DUPLICATE_GROUPS,
    duplicates_total_bytes: 3072,
    duplicates_text: DUPLICATES_TEXT,
    ...overrides,
  });
}

/** The duplicate notice, found by its own attribute: it is not a landmark. */
function duplicatePanel(): HTMLElement {
  const node = document.querySelector("[data-duplicate-report]");
  if (!node) throw new Error("no duplicate panel");
  return node as HTMLElement;
}

const FIDELITY_STAMP = "c".repeat(64);
const FIDELITY_RULE_SET = { version: "2026-11-04.2", stamp: FIDELITY_STAMP };
const FIDELITY_REFUSALS = [
  { rel_path: "import/models-only.zip", rule: "R3:trio-no-image", kind: "archive" },
  { rel_path: "import/stray-folder", rule: "R1:no-markers", kind: "directory" },
];

function withFidelity(overrides: Partial<BundleReport> = {}): BundleReport {
  return makeReport([moai], {
    rule_set: FIDELITY_RULE_SET,
    refused: FIDELITY_REFUSALS,
    ...overrides,
  });
}

/** The fidelity notice, found by its own attribute: it is not a landmark. */
function scanFidelityPanel(): HTMLElement {
  const node = document.querySelector("[data-scan-fidelity]");
  if (!node) throw new Error("no scan fidelity panel");
  return node as HTMLElement;
}

function makeState(overrides: Partial<BundleDetectState> = {}): BundleDetectState {
  return {
    report: makeReport([moai, cableClip, legacyVase, moaiZip]),
    loading: false,
    busy: false,
    error: null,
    lastRefreshedAt: null,
    showDismissed: false,
    setShowDismissed: vi.fn(),
    showCreated: false,
    setShowCreated: vi.fn(),
    refresh: vi.fn(async () => {}),
    // `null` is "the server gave no verdict", which is what the create doors
    // report nothing from. A fixture that wanted the hand-off to fire passes its
    // own `create` - see the project list hand-off block.
    create: vi.fn(async (): Promise<BundleCreateResponse | null> => null),
    dismiss: vi.fn(async () => {}),
    enrich: vi.fn(async () => {}),
    notice: null,
    afterExtract: vi.fn(async (): Promise<SettleOutcome> => "settled"),
    waitAfterExtract: vi.fn(async (): Promise<SettleOutcome> => "settled"),
    ...overrides,
  };
}

function renderView(
  state: BundleDetectState,
  canContribute = true,
  options: {
    canScan?: boolean;
    onScan?: () => void;
    onUnauthorized?: () => void;
    onProjectsChanged?: () => void;
    onFoldersChanged?: () => void;
  } = {},
): RenderResult {
  return render(
    <BundleReviewView
      state={state}
      canContribute={canContribute}
      scan={makeScanState()}
      canScan={options.canScan ?? false}
      onScan={options.onScan ?? vi.fn()}
      onProjectsChanged={options.onProjectsChanged}
      onFoldersChanged={options.onFoldersChanged}
      onUnauthorized={options.onUnauthorized}
    />,
  );
}

/** A sanitised scan state for the detail-pane action; no test here reads it. */
function makeScanState(): LibraryScanState {
  const scan: LibraryScanStatus = {
    running: false,
    started_at: 1_700_000_000,
    finished_at: 1_700_000_010,
    registered: 3,
    known: 1,
    ignored: 0,
    missing: 0,
    failed: 0,
    errors: [],
  };
  return {
    scan,
    busy: false,
    error: null,
    loaded: true,
    run: vi.fn(async () => {}),
    adopt: vi.fn(),
  };
}

/** The scan props a direct render needs; no writer, so no action renders. */
function scanProps(): { scan: LibraryScanState; canScan: boolean; onScan: () => void } {
  return { scan: makeScanState(), canScan: false, onScan: vi.fn() };
}

function listbox(): HTMLElement {
  return screen.getByRole("listbox", { name: "Bundles to confirm" });
}

/** The queue header's own count, the number the badge paints. */
function headerCount(): string {
  const node = document.querySelector(".review-queue-count");
  if (!node) throw new Error("no queue count");
  return node.textContent ?? "";
}

function detail(): HTMLElement {
  return screen.getByRole("complementary", { name: "Bundle details" });
}

/** The detail pane's compound-row breakdown; only a row carrying groups renders it. */
function groupsBlock(): HTMLElement {
  return screen.getByRole("group", { name: "Groups in this bundle" });
}

function rowKeys(): string[] {
  return Array.from(listbox().querySelectorAll("[data-bundle-row]")).map(
    node => node.getAttribute("data-bundle-row") ?? "",
  );
}

function rowElement(key: string): HTMLElement {
  const row = listbox().querySelector<HTMLElement>(`[data-bundle-row="${key}"]`);
  if (!row) throw new Error(`no row for ${key}`);
  return row;
}

/** The blocked-reason cell of a row: the mouse's door to the extract dialog. */
function statusCell(key: string): HTMLElement {
  const cell = rowElement(key).querySelector<HTMLElement>(".review-row-status");
  if (!cell) throw new Error(`no status cell for ${key}`);
  return cell;
}

/** The row's own words for who made the project, which is not the queue's
 *  bucket verdict: the two are allowed to disagree. */
function stateCell(key: string): HTMLElement {
  const cell = rowElement(key).querySelector<HTMLElement>(".review-row-state");
  if (!cell) throw new Error(`no state cell for ${key}`);
  return cell;
}

/** The row's bulk-selection glyph, which is how a selection is made by mouse. */
function selectionCheck(key: string): HTMLElement {
  const glyph = rowElement(key).querySelector<HTMLElement>(".review-check");
  if (!glyph) throw new Error(`no selection glyph for ${key}`);
  return glyph;
}

function detailTitle(): string | null {
  return within(detail()).getByRole("heading").textContent;
}

/** One answer per path, so the confirmation's count names the archive that was
 *  listed. The file-wide mock answers every path the same way, which would leave
 *  a stale panel and a fresh one reading identically. */
function installArchiveListings(): void {
  listArchiveEntriesMock.mockImplementation(async (path: string) => ({
    path,
    entries: path === "Beta.zip" ? BETA_ENTRIES : ALPHA_ENTRIES,
  }));
}

/** Both reveal toggles need live setters, exactly as the hook supplies them. */
function VisibilityHarness({ initial }: { initial: BundleDetectState }) {
  const [showDismissed, setShowDismissed] = React.useState(initial.showDismissed);
  const [showCreated, setShowCreated] = React.useState(initial.showCreated);
  return (
    <BundleReviewView
      {...scanProps()}
      state={{
        ...initial,
        showDismissed,
        setShowDismissed: (value: boolean) => {
          initial.setShowDismissed(value);
          setShowDismissed(value);
        },
        showCreated,
        setShowCreated: (value: boolean) => {
          initial.setShowCreated(value);
          setShowCreated(value);
        },
      }}
      canContribute
    />
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  resetToasts();
  modelSnapshotProps.length = 0;
  listArchiveEntriesMock.mockReset();
  listArchiveEntriesMock.mockResolvedValue({ path: "Moai.zip", entries: ARCHIVE_ENTRIES });
  extractArchiveMock.mockReset();
  extractArchiveMock.mockResolvedValue(EXTRACT_RESULT);
  // Both operation doors plan on mount, so a test that reaches one without
  // saying what the server answers gets a real plan rather than an undefined
  // `.then`. The suite that cares asserts the call; the others are unaffected.
  planOperationMock.mockReset();
  planOperationMock.mockImplementation(async input =>
    input.workflow === "extract-and-create"
      ? extractPlanFor(input.target)
      : makeOperationPlan(),
  );
  submitOperationMock.mockReset();
  getOperationMock.mockReset();
  undoOperationMock.mockReset();
});

afterEach(() => {
  cleanup();
  Reflect.deleteProperty(navigator, "clipboard");
});

describe("BundleReviewView states", () => {
  it("says detection is running before the first report, with no rows", () => {
    renderView(makeState({ report: null, loading: true }));

    const loading = screen.getByText("Detecting vendor bundles…");
    expect(loading.className).toBe("review-loading");
    expect(loading.getAttribute("aria-busy")).toBe("true");
    expect(screen.queryByRole("listbox", { name: "Bundles to confirm" })).toBeNull();
  });

  it("offers a retry when the first fetch failed and there is nothing to show", () => {
    const state = makeState({ report: null, loading: false, error: "Bundle detection is unavailable." });
    renderView(state);

    expect(screen.getByRole("alert").textContent).toContain("Bundle detection is unavailable.");
    expect(screen.queryByRole("listbox", { name: "Bundles to confirm" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(state.refresh).toHaveBeenCalledTimes(1);
  });

  it("keeps the server's mutation message above the rows and offers no retry", () => {
    renderView(makeState({ error: "Failed to create the selected bundles." }));

    expect(screen.getByRole("alert").textContent).toContain("Failed to create the selected bundles.");
    expect(rowKeys()).toEqual(["Moai", "Cable_clip", "Legacy_vase", "import/Moai.zip"]);
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("says nothing was detected when the report is genuinely empty", () => {
    renderView(makeState({ report: makeReport([]) }));

    expect(screen.getByText("No vendor bundles were detected.")).toBeTruthy();
    expect(screen.queryByText("Every detected bundle is dismissed.")).toBeNull();
  });

  it("tells the operator a dismissal filter hid every row, not that detection found none", () => {
    const dismissed = makeBundle({ key: "Dismissed_one", title: "Dismissed one", dismissed: true });
    const state = makeState({ report: makeReport([dismissed]) });
    render(<VisibilityHarness initial={state} />);

    expect(screen.getByText("Every detected bundle is dismissed.")).toBeTruthy();
    expect(screen.queryByText("No vendor bundles were detected.")).toBeNull();
    expect(screen.queryByRole("listbox", { name: "Bundles to confirm" })).toBeNull();
    expect(screen.getByText("Turn on Show dismissed to see them.")).toBeTruthy();

    // The hint has to be actionable, so the toggle stays on screen here.
    fireEvent.click(screen.getByRole("checkbox", { name: "Show dismissed" }));
    expect(rowKeys()).toEqual(["Dismissed_one"]);
  });

  it("says no bundles match a filter and clears it on request", () => {
    renderView(makeState({ report: makeReport([moai, cableClip]) }));

    fireEvent.click(screen.getByRole("button", { name: "Blocked" }));
    expect(screen.getByText("No bundles match this filter.")).toBeTruthy();
    expect(rowKeys()).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.queryByText("No bundles match this filter.")).toBeNull();
    expect(rowKeys()).toEqual(["Moai", "Cable_clip"]);
  });

  it("says how many bundles the report bound left off the wire", () => {
    const report = makeReport([moai, cableClip], { truncated: true });
    // The report HAS five rows and the wire carried two of them: the notice says
    // what the operator is looking at, not merely that something is missing.
    report.counts.offered = 5;
    report.counts.withheld = 3;

    renderView(makeState({ report }));

    expect(screen.getByText("Showing 2 of 5 \u2014 3 bundles are not in this list.")).toBeTruthy();
    // The old guess at a fixed sentence is gone, not merely outnumbered.
    expect(
      screen.queryByText("The walk stopped at the report limit; some bundles may be missing."),
    ).toBeNull();
  });

  it("says nothing about the bound when the report is not truncated", () => {
    renderView(makeState({ report: makeReport([moai, cableClip]) }));

    expect(screen.queryByText(/bundles are not in this list\./)).toBeNull();
  });

  it("counts stale dismissals in the singular and the plural", () => {
    const { unmount } = render(
      <BundleReviewView
        {...scanProps()}
        state={makeState({ report: makeReport([moai], { stale_dismissals: ["Gone"] }) })}
        canContribute
      />,
    );
    expect(screen.getByText("1 dismissal no longer match a bundle on disk.")).toBeTruthy();
    unmount();

    renderView(makeState({ report: makeReport([moai], { stale_dismissals: ["Gone", "Also_gone"] }) }));
    expect(screen.getByText("2 dismissals no longer match a bundle on disk.")).toBeTruthy();
  });
});

describe("BundleReviewView filters", () => {
  it("shows exactly the rows each filter names", () => {
    renderView(makeState());

    expect(rowKeys()).toEqual(["Moai", "Cable_clip", "Legacy_vase", "import/Moai.zip"]);
    expect(screen.getByRole("button", { name: "All" }).getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: "Ready" }));
    expect(rowKeys()).toEqual(["Moai", "Cable_clip"]);
    expect(screen.getByRole("button", { name: "Ready" }).getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: "Blocked" }));
    expect(rowKeys()).toEqual(["Legacy_vase", "import/Moai.zip"]);

    fireEvent.click(screen.getByRole("button", { name: "Low confidence" }));
    expect(rowKeys()).toEqual(["Cable_clip", "import/Moai.zip"]);

    expect(screen.getByText("Showing 2 of 4")).toBeTruthy();
  });

  it("shows exactly the medium rows for the medium chip", () => {
    renderView(makeState({ report: makeReport([moai, mediumDragon, cableClip]) }));

    fireEvent.click(screen.getByRole("button", { name: "Medium confidence" }));
    expect(rowKeys()).toEqual(["Dragon_5390184"]);
    expect(
      screen.getByRole("button", { name: "Medium confidence" }).getAttribute("aria-pressed"),
    ).toBe("true");

    // The low chip is its own set: a filter that folded the two together would
    // return both rows here and prove nothing about the medium chord.
    fireEvent.click(screen.getByRole("button", { name: "Low confidence" }));
    expect(rowKeys()).toEqual(["Cable_clip"]);
  });

  it("reveals dismissed rows once Show dismissed is on", () => {
    const dismissed = makeBundle({ key: "Dismissed_one", title: "Dismissed one", dismissed: true });
    const state = makeState({ report: makeReport([moai, dismissed]) });
    render(<VisibilityHarness initial={state} />);

    expect(rowKeys()).toEqual(["Moai"]);

    fireEvent.click(screen.getByRole("checkbox", { name: "Show dismissed" }));

    expect(state.setShowDismissed).toHaveBeenCalledWith(true);
    expect(rowKeys()).toEqual(["Moai", "Dismissed_one"]);
    expect(screen.getByText("Showing 2 of 2")).toBeTruthy();
  });
});

describe("BundleReviewView confidence", () => {
  it("gives each confidence its own colour", () => {
    renderView(makeState({ report: makeReport([moai, mediumDragon, cableClip]) }));

    // The cell's own class, scoped to its row: its `title` is now the actable
    // reason for a row with a door, so only the text/content identifies the cell.
    const cell = (key: string): HTMLElement => {
      const node = rowElement(key).querySelector<HTMLElement>(".review-row-confidence");
      if (!node) throw new Error(`no confidence cell for ${key}`);
      return node;
    };
    const high = cell(moai.key);
    const medium = cell(mediumDragon.key);
    const low = cell(cableClip.key);

    expect(high.className).toContain("text-emerald-400");
    expect(medium.className).toContain("text-amber-400");
    // Low is deliberately the muted zinc, not amber: amber now means "medium,
    // worth a look", so amber on ~117 low rows would drown that signal.
    expect(low.className).toContain("text-zinc-500");

    // Three distinct colours, not three spans sharing one: a two-way
    // `confidenceClass` collapses two of these into one string.
    const classNames = [high, medium, low].map(node => node.className);
    expect(new Set(classNames).size).toBe(3);
  });
});

describe("BundleReviewView badge agreement", () => {
  it("counts exactly the rows it shows, from the same predicate", () => {
    const pending = makeBundle();
    const dismissed = makeBundle({ key: "Old", title: "Old bundle", dismissed: true });
    const created = makeBundle({
      key: "Settled",
      title: "Settled bundle",
      existing_project_id: "3f2a6c1e-8f4b-4c7d-9a1e-2b5c8d0f4a61",
      existing_project_name: "Settled project",
    });
    const report = makeReport([pending, dismissed, created]);
    renderView(makeState({ report }));

    // The view's own predicate, on the report's own array: the assertion pins
    // the shared rule rather than a second copy of it.
    const expected = report.bundles.filter(needsDecision).map(row => row.key);

    expect(expected).toEqual(["Moai"]);
    // What the operator sees: the header's number and the list it heads.
    const rendered = rowKeys();
    expect(headerCount()).toBe(String(rendered.length));
    // ...and both are the shared predicate's answer on the same array.
    expect(rendered).toEqual(expected);
    expect(headerCount()).toBe(String(expected.length));
  });
});

describe("BundleReviewView empty states", () => {
  const COPY = {
    none: "No vendor bundles were detected.",
    created: "Every detected bundle has a project.",
    dismissed: "Every detected bundle is dismissed.",
    mixed: "Every detected bundle is dismissed or already has a project.",
    filtered: "No bundles match this filter.",
  };

  const TOGGLE_NAMES = ["Show dismissed", "Show created"];

  const dismissedRow = makeBundle({ key: "Old", title: "Old bundle", dismissed: true });
  const createdRow = makeBundle({
    key: "Settled",
    title: "Settled bundle",
    existing_project_id: "3f2a6c1e-8f4b-4c7d-9a1e-2b5c8d0f4a61",
    existing_project_name: "Settled project",
  });

  type Case = {
    name: string;
    state: BundleDetectState;
    copy: string;
    /** The reveal toggles on screen: the way out, and nothing else. */
    toggles: string[];
    /** A filter button to press after mounting, for the filter-empty case. */
    filter?: string;
  };

  const cases: Case[] = [
    {
      name: "nothing detected",
      state: makeState({ report: makeReport([]) }),
      copy: COPY.none,
      toggles: [],
    },
    {
      name: "every bundle has a project",
      state: makeState({ report: makeReport([createdRow]) }),
      copy: COPY.created,
      toggles: ["Show created"],
    },
    {
      name: "every bundle is dismissed",
      state: makeState({ report: makeReport([dismissedRow]) }),
      copy: COPY.dismissed,
      toggles: ["Show dismissed"],
    },
    {
      name: "some dismissed and some created",
      state: makeState({ report: makeReport([dismissedRow, createdRow]) }),
      copy: COPY.mixed,
      toggles: ["Show created", "Show dismissed"],
    },
    {
      name: "the filter hid every row",
      state: makeState({ report: makeReport([moai, cableClip]) }),
      copy: COPY.filtered,
      // The filter branch sits inside the normal toolbar, so both toggles stay.
      toggles: ["Show dismissed", "Show created"],
      filter: "Blocked",
    },
  ];

  it("gives each state its own words", () => {
    expect(new Set(Object.values(COPY)).size).toBe(Object.values(COPY).length);
  });

  for (const testCase of cases) {
    it(`says "${testCase.copy}" when ${testCase.name}, and nothing else`, () => {
      renderView(testCase.state);
      if (testCase.filter) fireEvent.click(screen.getByRole("button", { name: testCase.filter }));

      expect(screen.getByText(testCase.copy)).toBeTruthy();
      for (const other of Object.values(COPY)) {
        if (other === testCase.copy) continue;
        expect(screen.queryByText(other)).toBeNull();
      }
      // The instruction always comes with the control it names, and a branch
      // shows no toggle that is not its way out.
      const shown = TOGGLE_NAMES.filter(
        name => screen.queryByRole("checkbox", { name }) !== null,
      );
      expect(shown.sort()).toEqual([...testCase.toggles].sort());
    });
  }
});

describe("BundleReviewView existing-project chips", () => {
  const projectId = "3f2a6c1e-8f4b-4c7d-9a1e-2b5c8d0f4a61";
  const assetRow = makeBundle({
    key: "Clockwork",
    rel_path: "Clockwork",
    title: "Clockwork",
    existing_project_id: projectId,
    existing_project_name: "Fates End - ClockworkMonster Tower",
    existing_project_match: "assets",
  });

  it("says the bundle is in a project, not that the app created it", () => {
    const state = makeState({ report: makeReport([assetRow]) });
    render(<VisibilityHarness initial={state} />);

    fireEvent.click(screen.getByRole("checkbox", { name: "Show created" }));

    expect(stateCell("Clockwork").textContent).toBe("In project");
    expect(stateCell("Clockwork").textContent).not.toBe("Created");
    // The hover title is the detail line's own sentence, so the chip cannot
    // abbreviate one claim while the panel makes another.
    expect(stateCell("Clockwork").getAttribute("title")).toBe(
      "Already in Fates End - ClockworkMonster Tower (matched by shared files)",
    );
    expect(
      within(detail()).getByText(
        "Already in Fates End - ClockworkMonster Tower (matched by shared files)",
      ),
    ).toBeTruthy();
  });

  it("still says Created for the marker identity beside an asset row", () => {
    const markedRow = makeBundle({
      key: "Moai_marked",
      rel_path: "Moai_marked",
      title: "Moai marked",
      existing_project_id: projectId,
      existing_project_name: "Moai statue",
      existing_project_match: "marker",
    });
    const state = makeState({ report: makeReport([markedRow, assetRow]) });
    render(<VisibilityHarness initial={state} />);

    fireEvent.click(screen.getByRole("checkbox", { name: "Show created" }));

    // One render, two identities: the chip must follow the match, not the id.
    expect(stateCell("Moai_marked").textContent).toBe("Created");
    expect(stateCell("Clockwork").textContent).toBe("In project");
  });

  it("keeps an asset-matched row in the Show created bucket, off the count and out of the badge", () => {
    renderView(makeState({ report: makeReport([assetRow]) }));

    // The words changed; the bucket did not. The row is still settled, so the
    // default view is the created empty state and never the mixed one.
    expect(screen.queryByRole("listbox", { name: "Bundles to confirm" })).toBeNull();
    expect(screen.getByText("Every detected bundle has a project.")).toBeTruthy();
    expect(screen.getByText("Turn on Show created to see them.")).toBeTruthy();
    expect(
      screen.queryByText("Every detected bundle is dismissed or already has a project."),
    ).toBeNull();
    expect(headerCount()).toBe("0");
  });
});

describe("BundleReviewView writes", () => {
  it("creates the active bundle under the typed name, or with no name when cleared", () => {
    const state = makeState({ report: makeReport([autoRow()]) });
    renderView(state);

    const input = screen.getByLabelText("Name for Moai statue") as HTMLInputElement;
    expect(input.value).toBe("Moai statue");

    fireEvent.change(input, { target: { value: "typed" } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    expect(state.create).toHaveBeenCalledTimes(1);
    // The detail pane's one-step door is AUTO now: the flag rides with the item,
    // and the server decides whether this band/shape may run unattended.
    expect(state.create).toHaveBeenCalledWith([{ key: "Moai", name: "typed", auto: true }]);

    vi.mocked(state.create).mockClear();
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    expect(state.create).toHaveBeenCalledWith([{ key: "Moai", auto: true }]);
  });

  it("creates only the creatable selected rows and counts only those", () => {
    const created = makeBundle({
      key: "Legacy_created",
      title: "Legacy created",
      existing_project_id: "7c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f",
      existing_project_name: "Legacy created",
    });
    const state = makeState({ report: makeReport([moai, cableClip, legacyVase, created]) });
    renderView(state);

    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));
    // REPINNED from `(2)`: `Legacy_vase` is `unresolved_directory`, which no longer
    // refuses a create - a blocked status is a readiness fact now, and only
    // `archive_only` names another door. The settled row is still left out.
    expect(screen.getByRole("button", { name: /^Create selected/ }).textContent).toBe(
      "Create selected (3)",
    );
    // The already-created row is settled, so it is not shown, let alone selected.
    expect(screen.getByText("3 bundles selected")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /^Create selected/ }));

    const items = vi.mocked(state.create).mock.calls[0][0];
    expect(items.map(item => item.key)).toEqual(["Moai", "Cable_clip", "Legacy_vase"]);
    // No row carries a shape: the operator never touched the chooser, so the
    // server's recommendation stands and the body is the one it always was.
    for (const item of items) expect(item).not.toHaveProperty("shape");
  });

  it("creates every visible low-confidence creatable row as name-only", () => {
    const lowButCreated = makeBundle({
      key: "Low_created",
      title: "Low created",
      confidence: "low",
      existing_project_id: "8d2e3f4a-5b6c-4d7e-9f80-1a2b3c4d5e6f",
      existing_project_name: "Low created",
    });
    const lowReady = makeBundle({ key: "Sword_hilt", title: "Sword hilt", confidence: "low" });
    const state = makeState({
      report: makeReport([moai, cableClip, lowReady, legacyVase, moaiZip, lowButCreated]),
    });
    renderView(state);

    fireEvent.click(screen.getByRole("button", { name: /^Create low as name-only/ }));

    // Blocked rows (import/Moai.zip) and already-created ones are not creatable, so
    // the button's count and its payload are the same two rows.
    expect(state.create).toHaveBeenCalledWith([
      { key: "Cable_clip", name_only: true },
      { key: "Sword_hilt", name_only: true },
    ]);
  });

  it("never sends a medium row through the low bulk bar", () => {
    const lowReady = makeBundle({ key: "Sword_hilt", title: "Sword hilt", confidence: "low" });
    const state = makeState({
      report: makeReport([moai, cableClip, mediumDragon, lowReady, legacyVase, moaiZip]),
    });
    renderView(state);

    // The medium row is ready and creatable, so only the confidence test in
    // `lowCreatable` keeps it out of the count and the payload.
    expect(screen.getByRole("button", { name: /^Create low as name-only/ }).textContent).toBe(
      "Create low as name-only (2)",
    );

    fireEvent.click(screen.getByRole("button", { name: /^Create low as name-only/ }));

    // The bulk bar sends `name_only: true`, which writes no source URL - and a
    // medium row's entire point is the URL derived from the id in its folder
    // name. Such a row is created from the detail panel, where the enrichment is
    // written, so widening `lowCreatable` would silently throw that URL away.
    expect(state.create).toHaveBeenCalledWith([
      { key: "Cable_clip", name_only: true },
      { key: "Sword_hilt", name_only: true },
    ]);
    const items = vi.mocked(state.create).mock.calls[0][0];
    expect(items.map(item => item.key)).not.toContain("Dragon_5390184");
  });

  it("dismisses the selection, and the detail panel's own row with the true flag", () => {
    const state = makeState({ report: makeReport([moai, cableClip]) });
    renderView(state);

    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));
    fireEvent.click(screen.getByRole("button", { name: /^Dismiss selected/ }));
    expect(state.dismiss).toHaveBeenCalledWith(["Moai", "Cable_clip"], true);

    vi.mocked(state.dismiss).mockClear();
    fireEvent.click(within(detail()).getByRole("button", { name: "Dismiss" }));
    expect(state.dismiss).toHaveBeenCalledWith(["Moai"], true);
  });

  it("undoes a dismissal from the detail panel with the false flag", () => {
    const state = makeState({
      report: makeReport([makeBundle({ dismissed: true })]),
      showDismissed: true,
    });
    renderView(state);

    fireEvent.click(within(detail()).getByRole("button", { name: "Undo dismiss" }));

    expect(state.dismiss).toHaveBeenCalledWith(["Moai"], false);
  });

  it("offers archive extraction only on the zip-only row", () => {
    renderView(makeState({ report: makeReport([moaiZip]) }));
    expect(within(detail()).getByRole("button", { name: "Extract Moai zip" })).toBeTruthy();
    cleanup();

    renderView(makeState({ report: makeReport([legacyVase]) }));
    expect(within(detail()).queryByRole("button", { name: /^Extract / })).toBeNull();
    cleanup();

    renderView(makeState({ report: makeReport([moaiZip]) }), false);
    expect(within(detail()).queryByRole("button", { name: /^Extract / })).toBeNull();
  });

  it("reports the extraction to the hook and leaves the refetch to it", async () => {
    const state = makeState({ report: makeReport([moaiZip]) });
    renderView(state);

    fireEvent.click(within(detail()).getByRole("button", { name: "Extract Moai zip" }));
    await waitFor(() => expect(listArchiveEntriesMock).toHaveBeenCalledWith("import/Moai.zip"));
    fireEvent.click(await screen.findByRole("button", { name: "Extract 2 files" }));

    await waitFor(() => expect(state.afterExtract).toHaveBeenCalledTimes(1));
    expect(state.afterExtract).toHaveBeenCalledWith([EXTRACT_RESULT]);
    await waitFor(() =>
      expect(screen.queryByText(/waiting for the library scan to register/)).toBeNull(),
    );

    // This view's half is the report. The row actually turning `ready` is the
    // hook's half, pinned by `useBundleDetect.test.tsx`'s "settles the library
    // scan and then refetches, so an extracted bundle turns ready".
    // The hook owns the refetch: a refresh here would race the walk that has to
    // register the extracted files before the row can read `ready`.
    expect(state.refresh).not.toHaveBeenCalled();
    expect(extractArchiveMock).toHaveBeenCalledWith({ path: "import/Moai.zip", entries: null });
  });

  it("promises no scan and reports nothing when the extract wrote no files", async () => {
    // The server's junk-only early return: no bytes, no scan, and
    // `already_present: false` because no destination existed to be already there.
    extractArchiveMock.mockResolvedValue({
      path: "import/Moai.zip",
      destination: "import/Moai",
      files: 0,
      bytes: 0,
      already_present: false,
      scan_started: false,
    });
    const state = makeState({ report: makeReport([moaiZip]) });
    renderView(state);

    fireEvent.click(within(detail()).getByRole("button", { name: "Extract Moai zip" }));
    await waitFor(() => expect(listArchiveEntriesMock).toHaveBeenCalledWith("import/Moai.zip"));
    fireEvent.click(await screen.findByRole("button", { name: "Extract 2 files" }));

    // The dialog's own done line is the anchor that says the round trip finished.
    await screen.findByText("Nothing was written - the archive held no extractable files.");
    // A junk-only extract registers nothing, so the wait would be a fiction and
    // the notice would promise a refetch that no scan will ever justify.
    expect(screen.queryByText(/waiting for the library scan to register/)).toBeNull();
    expect(state.afterExtract).not.toHaveBeenCalled();
  });

  it("shows a read-only view with rows and details but no write control", () => {
    renderView(makeState({ report: makeReport([moai, moaiZip]) }), false);

    expect(rowKeys()).toEqual(["Moai", "import/Moai.zip"]);
    expect(within(detail()).getByRole("heading").textContent).toBe("Moai statue");
    expect(within(detail()).getByText("Creator sculpteo")).toBeTruthy();

    expect(screen.queryByRole("button", { name: /^Create/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Dismiss/ })).toBeNull();
    expect(screen.queryByLabelText("Name for Moai statue")).toBeNull();
    expect(screen.queryByRole("group", { name: "Bundle actions" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Select all shown" })).toBeNull();
    // The filters and both reveal toggles are read controls and stay.
    expect(screen.getByRole("button", { name: "Blocked" })).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "Show dismissed" })).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "Show created" })).toBeTruthy();
  });

  it("hides an already-created bundle and reports its project once Show created is on", () => {
    // The wire sends the existing project's uuid AND its name, so the panel can
    // name the project without ever putting the uuid on screen.
    const projectId = "3f2a6c1e-8f4b-4c7d-9a1e-2b5c8d0f4a61";
    const created = makeBundle({
      key: "Moai_created",
      title: "Moai created",
      existing_project_id: projectId,
      existing_project_name: "Moai statue",
    });
    const state = makeState({ report: makeReport([created]) });
    render(<VisibilityHarness initial={state} />);

    // Created rows are permanently done, so they are hidden by default - and
    // the queue says why rather than claiming nothing was detected. With no
    // rows there is no list, and no detail panel for the hidden row either.
    expect(screen.queryByRole("listbox", { name: "Bundles to confirm" })).toBeNull();
    expect(screen.queryByRole("complementary", { name: "Bundle details" })).toBeNull();
    expect(screen.getByText("Every detected bundle has a project.")).toBeTruthy();
    expect(screen.getByText("Turn on Show created to see them.")).toBeTruthy();

    fireEvent.click(screen.getByRole("checkbox", { name: "Show created" }));

    expect(rowKeys()).toEqual(["Moai_created"]);
    expect(within(listbox()).getByText("Created")).toBeTruthy();
    expect(within(detail()).getByText("Already created as project Moai statue")).toBeTruthy();
    // The identity is never rendered: a uuid on screen is the bug this ticket
    // removes, so the panel must not contain it anywhere.
    expect(within(detail()).queryByText(new RegExp(projectId))).toBeNull();
    // There is nothing to create, so neither the name field nor the button is
    // offered; dismissing is still a real decision, so that stays.
    expect(within(detail()).queryByRole("button", { name: "Create project" })).toBeNull();
    expect(within(detail()).queryByLabelText("Name for Moai created")).toBeNull();
    expect(within(detail()).getByRole("button", { name: "Dismiss" })).toBeTruthy();
  });
});

describe("BundleReviewView guided import door", () => {
  /** The server's sentence when it refuses AUTO. */
  const AUTO_REFUSAL = "auto needs arranging first: 1 move(s); review it instead";

  /** A 200 whose one result refused AUTO: the create door's per-item verdict. */
  function refusalResponse(): BundleCreateResponse {
    return {
      created: 0,
      skipped: 0,
      blocked: 0,
      failed: 1,
      results: [
        {
          key: "Moai",
          status: "failed",
          project_id: null,
          name: null,
          assets_added: 0,
          skipped_assets: 0,
          failed_assets: [],
          reason: AUTO_REFUSAL,
        },
      ],
    };
  }

  it("offers Create and Review for a high row", () => {
    renderView(makeState({ report: makeReport([autoRow()]) }));

    const create = within(detail()).getByRole("button", { name: "Create project" });
    expect(create.getAttribute("data-guided-auto")).toBe("true");
    const review = within(detail()).getByRole("button", { name: "Review first" });
    expect(review.getAttribute("data-guided-open")).toBe("true");
  });

  it("offers only Review and import for a medium row", () => {
    renderView(makeState({ report: makeReport([autoRow({ confidence: "medium" })]) }));

    const review = within(detail()).getByRole("button", { name: "Review and import" });
    expect(review.getAttribute("data-guided-open")).toBe("true");
    // No plain create for a band the server will not run unattended: "do it
    // anyway" is the panel, never a silent auto.
    expect(within(detail()).queryByRole("button", { name: "Create project" })).toBeNull();
  });

  it("opens the guided panel and sends auto on Create", async () => {
    const state = makeState({ report: makeReport([autoRow()]) });
    renderView(state);

    fireEvent.click(within(detail()).getByRole("button", { name: "Review first" }));
    expect(within(detail()).getByText("What the app parsed")).toBeTruthy();

    fireEvent.click(within(detail()).getByRole("button", { name: "Create project" }));

    await waitFor(() => expect(state.create).toHaveBeenCalledTimes(1));
    expect(vi.mocked(state.create).mock.calls[0][0][0]).toMatchObject({ key: "Moai", auto: true });
  });

  it("falls back to the guided panel when the server refuses auto", async () => {
    const state = makeState({
      report: makeReport([autoRow()]),
      create: vi.fn(async () => refusalResponse()),
    });
    renderView(state);

    fireEvent.click(within(detail()).getByRole("button", { name: "Create project" }));

    // The refusal is not a silent no-op: the panel that can finish the job is
    // open and the server's own sentence stands above the action row.
    await waitFor(() => expect(within(detail()).getByText("What the app parsed")).toBeTruthy());
    expect(detail().querySelector("[data-auto-failure]")?.textContent).toBe(AUTO_REFUSAL);
  });
});

describe("BundleReviewView source-page enrichment", () => {
  const ENRICHED_AT = "2026-11-05T12:00:00+00:00";

  /** A medium row: the folder name carried the Thingiverse id, so the source
   *  URL exists while both fields are still empty - the first pass's row. */
  function enrichableRow(index: number): BundleDetectItem {
    return makeBundle({
      key: `Dragon_${index}`,
      rel_path: `Dragon_${index}`,
      title: `Dragon ${index}`,
      name_suggestion: `Dragon ${index}`,
      creator: null,
      licence: null,
      licence_url: null,
      source_url: `https://www.thingiverse.com/thing:${index}`,
      confidence: "medium",
    });
  }

  function answeredRow(fields: string[]): BundleDetectItem {
    return makeBundle({
      key: "Dragon_5390184",
      rel_path: "Dragon_5390184",
      title: "Articulated ice dragon",
      name_suggestion: "Articulated ice dragon",
      creator: "P1lotz",
      licence: "CC BY-NC-SA",
      licence_url: "https://creativecommons.org/licenses/by-nc-sa/4.0/",
      source_url: "https://www.thingiverse.com/thing:5390184",
      confidence: "medium",
      enrichment: {
        source_url: "https://www.thingiverse.com/thing:5390184",
        fetched_at: ENRICHED_AT,
        fields,
      },
    });
  }

  it("offers the first pass for a row with a source and a gap, and sends only that row", () => {
    const state = makeState({ report: makeReport([mediumDragon]) });
    renderView(state);

    const button = within(detail()).getByRole("button", { name: "Enrich from source page" });
    expect(button.getAttribute("data-bundle-enrich")).toBe("true");
    // The title has to say what the action will and will not do: it reads the
    // page but never overwrites a value the row already has.
    expect(button.getAttribute("title")).toContain("only empty fields are filled");

    fireEvent.click(button);
    expect(state.enrich).toHaveBeenCalledTimes(1);
    // Exactly one argument: a first pass sends no options at all.
    expect(state.enrich).toHaveBeenCalledWith(["Dragon_5390184"]);
  });

  it("offers a re-check once the page has answered, and it forces a refetch", () => {
    const state = makeState({ report: makeReport([answeredRow(["creator", "licence"])]) });
    renderView(state);

    // The two doors are exclusive: a row the page already answered belongs to
    // the re-check, not the first pass, or the label would understate the call.
    expect(within(detail()).queryByRole("button", { name: "Enrich from source page" })).toBeNull();
    fireEvent.click(within(detail()).getByRole("button", { name: "Re-check source page" }));

    expect(state.enrich).toHaveBeenCalledWith(["Dragon_5390184"], { refresh: true });
  });

  it("renders no enrichment door for a row that names no source page, and gives the reason instead", () => {
    renderView(makeState({ report: makeReport([cableClip]) }));

    expect(detail().querySelector("[data-bundle-enrich]")).toBeNull();
    expect(screen.queryByRole("button", { name: /source page/ })).toBeNull();

    const reason = detail().querySelector<HTMLElement>("[data-bundle-enrich-reason]");
    expect(reason?.textContent).toBe(NO_SOURCE_PAGE_REASON);
    // Not a live region: a third one in the aside would re-announce this on
    // every ArrowDown.
    expect(reason?.getAttribute("role")).toBeNull();
    expect(reason?.getAttribute("aria-live")).toBeNull();
  });

  it("counts only the enrichable selected rows in the bulk label", () => {
    const state = makeState({ report: makeReport([moai, mediumDragon, cableClip]) });
    renderView(state);

    const none = screen.getByRole("button", {
      name: "Enrich selected (0)",
    }) as HTMLButtonElement;
    expect(none.getAttribute("data-bundle-enrich-selected")).toBe("true");
    expect(none.disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));

    // Moai has both fields (no gap) and Cable_clip has no source, so only the
    // dragon is enrichable even though three rows are selected.
    const mixed = screen.getByRole("button", {
      name: "Enrich selected (1 of 3)",
    }) as HTMLButtonElement;
    expect(mixed.getAttribute("title")).toBe(bulkEnrichTitle(1, 3));
    expect(mixed.getAttribute("title")).toContain("1 of the 3");
    expect(mixed.disabled).toBe(false);
    // The visible sentence belongs to the wholly-unenrichable case only: a
    // partial shortfall is spoken by the counts, not by a paragraph. And now that
    // the sentence is per row, a selection that HAS a sendable row speaks nothing:
    // neither of the two refusal sentences may appear while a request is live.
    expect(screen.queryByText(NO_SOURCE_PAGE_REASON)).toBeNull();
    expect(screen.queryByText(NO_PAYLOAD_DOOR_REASON)).toBeNull();

    fireEvent.click(mixed);
    expect(state.enrich).toHaveBeenCalledWith(["Dragon_5390184"]);
  });

  it("says a wholly unenrichable selection is (0 of 3) rather than a bare zero", () => {
    // All three refuse at the same gate - none names a source page - while their
    // row status differs (an archive when a zip could be extracted, a generic
    // folder, an unresolved directory). One gate, three row shapes: the count is
    // not a quirk of one detector.
    const state = makeState({ report: makeReport([moaiZip, cableClip, legacyVase]) });
    renderView(state);

    // Nothing is selected yet: the bar says so, and the sentence that explains a
    // wholly-unenrichable selection must not speak while the count says none. The
    // sentence is per row now, so neither of the two causes speaks in the bar
    // either - the scoped query is what separates the bar from the detail pane,
    // which has been saying `NO_SOURCE_PAGE_REASON` about the active row all along.
    expect(screen.getByText("No bundles selected")).toBeTruthy();
    const bar = () => screen.getByRole("group", { name: "Bundle actions" });
    expect(within(bar()).queryByText(NO_SOURCE_PAGE_REASON)).toBeNull();
    expect(within(bar()).queryByText(NO_PAYLOAD_DOOR_REASON)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));

    const none = screen.getByRole("button", {
      name: "Enrich selected (0 of 3)",
    }) as HTMLButtonElement;
    expect(none.disabled).toBe(true);
    // The paragraph this ticket deleted is gone from the disabled button too: the
    // title speaks a count or nothing, and here there is no count to speak - so
    // the attribute is not merely empty, it is absent (`bulkEnrichTitle` returned
    // undefined and React rendered no attribute at all).
    expect(none.getAttribute("title")).toBeNull();
    expect(none.hasAttribute("title")).toBe(false);

    // A disabled control's title is not reliably surfaced as a tooltip, and the
    // control is not focusable, so the same sentence is also rendered visibly -
    // but as plain text: `.review-selection` already speaks the count, and this
    // must not become a live region too. The three rows name no source page, so
    // the honest per-row sentence is the one about the page - which is also what
    // the detail pane says about the active row, hence the scoped query.
    const reason = within(bar()).getByText(NO_SOURCE_PAGE_REASON);
    expect(reason.getAttribute("data-bundle-enrich-none-reason")).toBe("true");
    expect(reason.getAttribute("aria-live")).toBeNull();
    expect(reason.getAttribute("role")).toBeNull();
    // Last in the bar, and `basis-full` gives it a line of its own; nothing
    // follows it, so no button is pushed down with it.
    expect(reason.parentElement?.lastElementChild).toBe(reason);
    expect(reason.className).toContain("basis-full");

    fireEvent.click(none);
    expect(state.enrich).not.toHaveBeenCalled();
  });

  it("keeps the reason beside the extract door it does not displace", () => {
    renderView(makeState({ report: makeReport([moaiZip]) }));

    expect(detail().querySelector("[data-bundle-enrich-reason]")?.textContent).toBe(
      NO_SOURCE_PAGE_REASON,
    );
    // The zip-only door is still a real action, so the sentence must not shove
    // it aside: both stand, one for the door that is absent, one for the door
    // that is present.
    expect(within(detail()).getByRole("button", { name: "Extract Moai zip" })).toBeTruthy();
  });

  it("flips both the reason and the door when ArrowDown leaves a source-less row", () => {
    renderView(makeState({ report: makeReport([moaiZip, mediumDragon]) }));

    // import/Moai.zip is the active row: it has no page, so the reason stands and the
    // door does not.
    expect(detail().querySelector("[data-bundle-enrich-reason]")?.textContent).toBe(
      NO_SOURCE_PAGE_REASON,
    );
    expect(within(detail()).queryByRole("button", { name: "Enrich from source page" })).toBeNull();

    fireEvent.keyDown(rowElement("import/Moai.zip"), { key: "ArrowDown" });

    // The dragon does name a page, so the sentence goes and the door appears:
    // the two claims are the same predicate read from both sides.
    expect(detailTitle()).toBe("Articulated ice dragon");
    expect(detail().querySelector("[data-bundle-enrich-reason]")).toBeNull();
    expect(within(detail()).getByRole("button", { name: "Enrich from source page" })).toBeTruthy();
  });

  it("gives a viewer neither the door nor the reason", () => {
    renderView(makeState({ report: makeReport([cableClip]) }), false);

    // The sentence lives inside `canContribute`: it stands where the door would,
    // and a viewer has no door to be missing. Pinned so a later ticket has to
    // relocate it on purpose rather than lose it by accident.
    expect(detail().querySelector("[data-bundle-enrich-reason]")).toBeNull();
    expect(within(detail()).queryByRole("button", { name: /source page/ })).toBeNull();
  });

  it("caps the bulk request at 25 and says how many it leaves behind", () => {
    const rows = Array.from({ length: ENRICH_MAX_KEYS + 5 }, (_, index) =>
      enrichableRow(index + 1),
    );
    const state = makeState({ report: makeReport(rows) });
    renderView(state);

    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));
    fireEvent.click(screen.getByRole("button", { name: "Enrich selected (25 of 30)" }));

    expect(state.enrich).toHaveBeenCalledTimes(1);
    expect(vi.mocked(state.enrich).mock.calls[0][0]).toEqual(
      rows.slice(0, ENRICH_MAX_KEYS).map(row => row.key),
    );
  });

  it("puts the page's provenance after the detector's own line", () => {
    renderView(
      makeState({
        report: makeReport([answeredRow(["creator", "licence", "licence_url"])]),
      }),
    );

    const line = detail().querySelector<HTMLElement>("[data-bundle-enrichment]");
    expect(line?.textContent).toBe(
      "From the source page: creator, licence, licence URL (fetched 2026-11-05)",
    );

    // The order is the point: the detector's guess is stated first, then what
    // the vendor's page supplied, so the two claims cannot be read as one.
    const facts = Array.from(detail().querySelectorAll(".review-detail-facts > span"));
    const detectedAt = facts.findIndex(node => node.textContent?.startsWith("Detected by"));
    expect(detectedAt).toBeGreaterThan(-1);
    expect(facts.indexOf(line as HTMLElement)).toBeGreaterThan(detectedAt);
  });

  it("keeps the server's refusal sentence verbatim in the alert", () => {
    const reason = "The site refused to be read. Set THINGIVERSE_COOKIE and try again.";
    renderView(makeState({ report: makeReport([mediumDragon]), error: reason }));

    expect(screen.getByRole("alert").textContent).toBe(reason);
  });

  it("shows a not-news notice as a status line, not an alert", () => {
    renderView(makeState({ notice: "Already checked from the source page" }));

    const notice = screen.getByText("Already checked from the source page");
    expect(notice.getAttribute("role")).toBe("status");
    expect(notice.className).toBe("review-notice");
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

/** --------------------------------------------------------------------------
 *  MEASURED FIXTURES for this file's ticket specimen. POINTER: `bundleRows.test.ts`
 *  transcribes the same measured rows for the unit-level pins - a shared module is
 *  outside this ticket's touch-set, so the two blocks are kept in step by hand.
 *
 *  Every field is quoted from
 *  the recorded reports in the lane at `.plans/measurements/` -
 *  `2026-11-10-bundles-mfquv4-rows-after.json` (`rel_path`, `status`,
 *  `blocked_reason`, `assets_total`, `model_count`, `detector`, `confidence`,
 *  `cover_reason`, `cover_asset`, `existing_project_match`) and
 *  `2026-11-10-bundles-mfquv4-after.json` (`counts.image|text|other`, `shape`,
 *  `twin_verified`, `existing_project_name`) - and each fixture names its row.
 *  `creator`/`licence`/`licence_url`/`description_excerpt` are null on a
 *  `generic-bundle` row because that detector is a folder-shape guess no vendor
 *  page ever answered; `title`/`name_suggestion` are the measured `rel_path`'s
 *  basename; a row whose measured `existing_project_match` implies an id carries
 *  `MEASURED_PROJECT_ID`, which the measurement itself does not print. */
const MEASURED_PROJECT_ID = "3f2a6c1e-8f4b-4c7d-9a1e-2b5c8d0f4a61";

/** `import/endorphin` - `blocked`/`archive_only`, `assets_total 8`, counts 1/1/0/0,
 *  `generic-bundle`, `low`, shape `generic`, verified twin
 *  `import/endorphin.zip`, `source_url` null, registered `largest-image` cover
 *  (`cover_asset: true`, `cover_reason: "largest-image"` - the builder's cover
 *  already is that shape). */
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

/** `import/Q` - `archive_only`, `assets_total 6`, counts 3/1/2/0,
 *  `generic-bundle`, `low`, shape `generic`, verified twin `import/Q.zip`. */
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

/** `import/X` - `archive_only`, `assets_total 7`, counts 1/0/0/0,
 *  `generic-bundle`, `low`, shape `generic`, twin `import/X.zip` NOT verified. */
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

/** `import/Pile` - `archive_only` with the verified twin
 *  `import/Pile Download.zip`, `assets_total 8`, counts 1/1/0/0,
 *  `generic-bundle`, `low`, shape `generic`, AND claimed by a project through
 *  shared files (`existing_project_match: "assets"`,
 *  `existing_project_name: "Project of the pile's own part"`, id
 *  `MEASURED_PROJECT_ID`). Rendered with Show created on, which is the only way
 *  this row reaches the queue at all. */
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
    existing_project_id: MEASURED_PROJECT_ID,
    existing_project_name: "Project of the pile's own part",
    existing_project_match: "assets",
    ...overrides,
  });
}

/** `import/xbox360_shell` - `ready` with `assets_total 33`, `compound-bundle`,
 *  `low`, shape `compound`, counts 31/0/2/0, and an existing project claimed by
 *  the app's own marker (`existing_project_match: "marker"`,
 *  `existing_project_name: "xbox360 shell"`, id `MEASURED_PROJECT_ID`). The
 *  measured compound group is left out - no words here read it. Transcribed from
 *  `bundleRows.test.ts`. */
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
    existing_project_id: MEASURED_PROJECT_ID,
    existing_project_name: "xbox360 shell",
    existing_project_match: "marker",
    ...overrides,
  });
}

/** `import/Pile/Alpha` - `ready` with `assets_total 3`, `generic-bundle`, `low`,
 *  shape `generic`, counts 1/1/1/0, claimed by a project through shared files
 *  (`existing_project_name: "Project of the two children"`, id
 *  `MEASURED_PROJECT_ID`). Transcribed from `bundleRows.test.ts`. */
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
    existing_project_id: MEASURED_PROJECT_ID,
    existing_project_name: "Project of the two children",
    existing_project_match: "assets",
    ...overrides,
  });
}

/** THE ORPHAN SHAPE HAS NO MEASURED INSTANCE: no recorded report of this corpus
 *  ever produced a `not_scanned` row the report calls non-empty. This fixture is
 *  the DECLARED union of the named measured rows, field by field:
 *
 *  - `status` "blocked", `blocked_reason` "not_scanned", `archive` null, counts
 *    1/1/1/0 and `metadata_shape` "generic" from `import/Not Scanned Thing`;
 *  - `detector` "thingiverse-readme", `confidence` "high" and `source_url`
 *    `https://www.thingiverse.com/thing:2493386` from `import/Q/Moai_statue`;
 *  - `assets_total: 8` and the registered `largest-image` cover from
 *    `import/endorphin`;
 *  - `creator`/`licence`/`licence_url` null, because the ticket's specimen is a
 *    row the server would still happily enrich. */
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
    source_url: "https://www.thingiverse.com/thing:2493386",
    detector: "thingiverse-readme",
    confidence: "high",
    metadata_shape: "generic",
    model_count: 1,
    image_count: 1,
    text_count: 1,
    other_count: 0,
    status: "blocked",
    blocked_reason: "not_scanned",
    archive: null,
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

/** The row with files and NO door at all, now that a blocked status no longer
 *  refuses a create: an `archive_only` DIRECTORY with no twin. Extract has no
 *  target and Create refuses (that reason names the other door), so
 *  `NO_PAYLOAD_DOOR_REASON` is still true of it - and it names a page with a
 *  metadata gap, so no earlier cause speaks for it. */
function danglingRow(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return makeBundle({
    key: "import/endorphin",
    rel_path: "import/endorphin",
    title: "endorphin",
    name_suggestion: "endorphin",
    creator: null,
    licence: null,
    licence_url: null,
    description_excerpt: null,
    source_url: "https://www.thingiverse.com/thing:2493386",
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
    archive: null,
    ...overrides,
  });
}

/** The first words of the paragraph this ticket deleted. Hard-coded rather than
 *  imported, because the constant is gone from the module and only a verbatim
 *  fragment can witness that neither bar nor pane still speaks it. */
const DELETED_BULK_PARAGRAPH = "None of the selected bundles can be enriched";

describe("BundleReviewView offers only the actions that can succeed", () => {
  function bulkBar(): HTMLElement {
    return screen.getByRole("group", { name: "Bundle actions" });
  }

  function enrichButton(): HTMLButtonElement {
    return screen.getByRole("button", {
      name: /^Enrich selected/,
    }) as HTMLButtonElement;
  }

  it("refuses to offer enrichment as the only live action on an on-disk row", () => {
    const dangling = danglingRow();
    renderView(makeState({ report: makeReport([dangling]) }));

    // The row is in the queue and the report says its key holds files.
    expect(rowKeys()).toContain(dangling.key);
    // No enrichment door of either kind, so no click can reach a refusal.
    expect(detail().querySelector("[data-bundle-enrich]")).toBeNull();
    expect(within(detail()).queryByRole("button", { name: "Enrich from source page" })).toBeNull();
    expect(within(detail()).queryByRole("button", { name: "Re-check source page" })).toBeNull();
    // The reason span speaks the refusal of the very predicate that gated the
    // button - not the old universal paragraph about four gates this row fails none of.
    // REPINNED from the orphan: a `not_scanned` row can be created now, so the
    // only row this refusal is still true of is an `archive_only` folder with no
    // twin and nothing to extract.
    const reason = detail().querySelector<HTMLElement>("[data-bundle-enrich-reason]");
    expect(reason?.textContent).toBe(NO_PAYLOAD_DOOR_REASON);
    expect(screen.queryByText(DELETED_BULK_PARAGRAPH)).toBeNull();
    // Not a live region, exactly as the other refusal sentence: the queue already
    // speaks through the selection count and the notice.
    expect(reason?.getAttribute("aria-live")).toBeNull();
    expect(reason?.getAttribute("role")).toBeNull();
  });

  it("offers enrichment on the blocked row that now has a guided import", () => {
    // REPINNED: the orphan used to be refused both doors. Its import is live now,
    // so the theorem this block exists for holds the other way round - enrichment
    // is never the ONLY live action, because the import door stands beside it.
    // A `not_scanned` row is not a band/shape AUTO may run unattended, so that
    // door is the guided panel's.
    const orphan = orphanRow();
    renderView(makeState({ report: makeReport([orphan]) }));

    expect(within(detail()).getByRole("button", { name: "Enrich from source page" })).toBeTruthy();
    const gate = within(detail()).getByRole("button", {
      name: "Review and import",
    }) as HTMLButtonElement;
    expect(gate.disabled).toBe(false);
    expect(gate.getAttribute("data-guided-open")).toBe("true");
    // Nothing is refused, so the pane says nothing about a missing door.
    expect(detail().querySelector("[data-bundle-enrich-reason]")).toBeNull();
    expect(screen.queryByText(NO_PAYLOAD_DOOR_REASON)).toBeNull();
  });

  it("keeps the guided import live and the extract door absent on that same row", () => {
    const orphan = orphanRow();
    renderView(makeState({ report: makeReport([orphan]) }));

    // REPINNED: `not_scanned` is a readiness fact, not an import refusal, so the
    // primary door is live - and the row's own cell must not call it blocked.
    const gate = within(detail()).getByRole("button", {
      name: "Review and import",
    }) as HTMLButtonElement;
    expect(gate.disabled).toBe(false);
    expect(statusCell(orphan.key).textContent).not.toContain("Blocked - ");
    // And no extraction in place, no one-step: `extractPathFor` answers null, so
    // neither control renders.
    expect(detail().querySelector("[data-extract-plan]")).toBeNull();
    expect(within(detail()).queryByRole("button", { name: /^Extract/ })).toBeNull();
  });

  it("counts nothing in the bulk bar, disables the button, and says why in the row's words", () => {
    // REPINNED from the orphan, which the bar can now send: the row whose words
    // the bar speaks is the doorless one.
    const dangling = danglingRow();
    renderView(makeState({ report: makeReport([dangling]) }));

    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));

    const button = enrichButton();
    expect(button.textContent).toBe("Enrich selected (0 of 1)");
    expect(button.disabled).toBe(true);
    // The deleted paragraph is gone from the title too: a count or nothing.
    expect(button.hasAttribute("title")).toBe(false);

    const none = within(bulkBar()).getByText(NO_PAYLOAD_DOOR_REASON);
    expect(none.getAttribute("data-bundle-enrich-none-reason")).toBe("true");
    expect(none.getAttribute("aria-live")).toBeNull();
    expect(none.getAttribute("role")).toBeNull();
    expect(none.className).toContain("basis-full");
    // Last in the bar, so nothing is pushed down with it.
    expect(none.parentElement?.lastElementChild).toBe(none);
    // And the sentence is not the page one: this row DOES name a page.
    expect(within(bulkBar()).queryByText(NO_SOURCE_PAGE_REASON)).toBeNull();
  });

  it("counts the orphan the bar can now send, and speaks nothing", () => {
    // REPINNED: the create door makes the orphan sendable, so the bar counts it
    // and has no refusal to explain. The deleted paragraph stays gone either way.
    renderView(makeState({ report: makeReport([orphanRow()]) }));

    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));

    const button = enrichButton();
    expect(button.textContent).toBe("Enrich selected (1)");
    expect(button.disabled).toBe(false);
    expect(button.hasAttribute("title")).toBe(false);
    expect(bulkBar().querySelector("[data-bundle-enrich-none-reason]")).toBeNull();
    expect(screen.queryByText(DELETED_BULK_PARAGRAPH)).toBeNull();
  });

  it("says nothing in the bar when the selected rows do not agree on a cause", () => {
    // THE MIS-ATTRIBUTION WITNESS, rendered. `Legacy_vase` names no page, so its own sentence
    // is the page one; the doorless row DOES name a page whose payload no door can reach, so
    // its own sentence is the payload one. Each is true of its row and false of the other, so
    // a bar that recites the first one it found in queue order is the paragraph this ticket
    // deleted in miniature. Both queue orders are asked because which sentence such a bar would
    // have spoken is exactly what the order decided - and neither order is saved by the two
    // guards, since neither row is creatable or extractable and only the doorless row names a
    // page. REPINNED from the orphan: that row can be created now, so it is not a doorless
    // specimen any more.
    const noPage = legacyVase;
    const withPage = danglingRow();
    for (const rows of [[noPage, withPage], [withPage, noPage]]) {
      const view = renderView(makeState({ report: makeReport(rows) }));

      fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));

      const button = enrichButton();
      expect(button.textContent).toBe("Enrich selected (0 of 2)");
      expect(button.disabled).toBe(true);
      // No node at all - not an empty span, and not one row's verdict about both. The two
      // single-row cases above pin that each of these rows DOES speak on its own.
      expect(bulkBar().querySelector("[data-bundle-enrich-none-reason]")).toBeNull();
      view.unmount();
    }
  });

  it("renders no sentence node at all when every selected row is silent", () => {
    // The branch a "first non-null line" cannot distinguish: two settled rows - 
    // `import/Pile/Alpha` and `import/xbox360_shell`, each with real measured fields. Nothing is
    // sendable, so the bar is in its explain-the-refusal state, and every row in it has no
    // sentence of its own (a settled project says itself in `createdProjectLine`). The honest
    // answer is that the bar has nothing to say, so it renders nothing - while the button stays
    // where it was, still disabled, still counting the selection.
    const rows = [pileAlphaRow(), xbox360Row()];
    renderView(makeState({ report: makeReport(rows), showCreated: true }));

    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));

    expect(bulkBar().querySelector("[data-bundle-enrich-none-reason]")).toBeNull();
    expect(bulkBar().querySelector(".review-detail-reason")).toBeNull();
    const button = enrichButton();
    expect(button.textContent).toBe("Enrich selected (0 of 2)");
    expect(button.disabled).toBe(true);
    // The title stays absent too: `bulkEnrichTitle` has no count to speak here either.
    expect(button.hasAttribute("title")).toBe(false);
  });

  it("keeps Dismiss live - hiding the row is never company for the refusal", () => {
    // REPINNED from the orphan: that row now has a live create, so the row this
    // sentence is about is the doorless one.
    const dangling = danglingRow();
    const state = makeState({ report: makeReport([dangling]) });
    renderView(state);

    const dismiss = within(detail()).getByRole("button", {
      name: "Dismiss",
    }) as HTMLButtonElement;
    expect(dismiss.disabled).toBe(false);
    fireEvent.click(dismiss);
    expect(state.dismiss).toHaveBeenCalledWith([dangling.key], true);

    // The bulk Dismiss is live too, while the Enrich button beside it stays shut.
    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));
    expect(enrichButton().disabled).toBe(true);
    const dismissSelected = screen.getByRole("button", {
      name: "Dismiss selected (1)",
    }) as HTMLButtonElement;
    expect(dismissSelected.disabled).toBe(false);
  });

  it("gives the Enrich door back to the same row the report calls empty", () => {
    // THE DISCRIMINATOR: one field, `assets_total`, from the measured
    // `import/Not Scanned Thing` value of 0 to `import/endorphin`'s 8. Everything
    // else - page, gap, status, reason - is identical to the row above, and the
    // door returns.
    const empty = orphanRow({ assets_total: 0 });
    renderView(makeState({ report: makeReport([empty]) }));

    expect(within(detail()).getByRole("button", { name: "Enrich from source page" })).toBeTruthy();
    expect(detail().querySelector("[data-bundle-enrich-reason]")).toBeNull();
    expect(screen.queryByText(NO_PAYLOAD_DOOR_REASON)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));
    const button = enrichButton();
    expect(button.textContent).toBe("Enrich selected (1)");
    expect(button.disabled).toBe(false);
    // Nothing sendable means nothing to explain, so the bar renders no span at all
    // rather than an empty one.
    expect(bulkBar().querySelector("[data-bundle-enrich-none-reason]")).toBeNull();
  });

  it("keeps every measured on-disk archive_only row extractable and never enrichable", () => {
    // The four rows the corpus really produced with files AND `archive_only`.
    // Their Extract door stays live - the twin is a real action - and none of them
    // names a page, so the enrichment door is refused by the page gate with the
    // sentence that says so, never by the payload rule. The expected sentence is
    // part of the table: `import/Pile` is already settled, and a settled project
    // says so in `createdProjectLine`, so its enrichment line stays silent.
    const cases: [BundleDetectItem, string | null][] = [
      [endorphinRow(), NO_SOURCE_PAGE_REASON],
      [queueQRow(), NO_SOURCE_PAGE_REASON],
      [queueXRow(), NO_SOURCE_PAGE_REASON],
      [pileRow(), null],
    ];
    // `import/Pile` measured an existing project match, so it needs the created
    // rows revealed to be in the queue at all.
    renderView(
      makeState({ report: makeReport(cases.map(([row]) => row)), showCreated: true }),
    );

    expect(rowKeys()).toEqual(cases.map(([row]) => row.key));
    for (const [row, expectedReason] of cases) {
      expect(isArchiveOnly(row)).toBe(true);
      fireEvent.click(rowElement(row.key));

      // The detail pane's AT-safe extract button, named for this row's title.
      expect(within(detail()).getByRole("button", { name: `Extract ${row.title}` })).toBeTruthy();
      // No enrichment door for a row whose files a door can reach.
      expect(detail().querySelector("[data-bundle-enrich]")).toBeNull();
      const reason = detail().querySelector<HTMLElement>("[data-bundle-enrich-reason]");
      if (expectedReason === null) {
        expect(reason).toBeNull();
      } else {
        // The cause that really applies is the missing page - the payload refusal
        // would have accused a row that HAS a door of having none.
        expect(reason?.textContent).toBe(expectedReason);
      }
    }
  });
});

describe("BundleReviewView archive extraction", () => {
  it("opens the extract flow from an archive-only row's blocked reason", () => {
    renderView(makeState());

    // The detail target before the click: a click that reaches the row would
    // move it to import/Moai.zip, so this is what proves `stopPropagation` works.
    const before = detailTitle();
    expect(before).toBe("Moai statue");

    fireEvent.click(statusCell("import/Moai.zip"));

    expect(screen.getByRole("dialog", { name: "Extract Moai zip" })).toBeTruthy();
    expect(detailTitle()).toBe(before);
  });

  it("extracts the archive a real-shaped row IS, then waits for the scan", async () => {
    // The wire shape for a zip-only row: `archive: null`, and the row's own path
    // is the archive. Every door above is driven by this fixture; this one proves
    // the bulk door hands the API that path rather than a twin that does not exist.
    const state = makeState({ report: makeReport([moaiZip]) });
    renderView(state);

    fireEvent.click(selectionCheck(moaiZip.key));
    const door = screen.getByRole("button", {
      name: "Extract selected archives (1)",
    }) as HTMLButtonElement;
    expect(door.disabled).toBe(false);
    fireEvent.click(door);

    expect(screen.getByRole("dialog", { name: "Extract Moai zip" })).toBeTruthy();
    // The dialog holds the same idle action the detail pane does: its trigger
    // lists the archive, and the confirmation is the step that writes.
    fireEvent.click(
      within(screen.getByRole("dialog", { name: "Extract Moai zip" })).getByRole("button", {
        name: "Extract Moai zip",
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Extract 2 files" }));

    await waitFor(() =>
      expect(extractArchiveMock).toHaveBeenCalledWith({
        path: "import/Moai.zip",
        entries: null,
      }),
    );
    await waitFor(() => expect(state.afterExtract).toHaveBeenCalledWith([EXTRACT_RESULT]));
  });

  it("keeps the door on a refused pair, which names its archive as a twin", async () => {
    // A directory the container rule refuses ships `blocked_reason: archive_only`
    // WITH its twin zip in `archive`. Its key is a directory, so the twin is the
    // only possible target.
    const refusedPair = makeBundle({
      key: "import/endorphin",
      rel_path: "import/endorphin",
      title: "Endorphin pile",
      status: "blocked",
      blocked_reason: "archive_only",
      archive: { rel_path: "import/endorphin.zip", twin_verified: true },
    });
    renderView(makeState({ report: makeReport([refusedPair]) }));

    expect(rowElement("import/endorphin").getAttribute("aria-keyshortcuts")).toBe("Enter");
    fireEvent.click(within(detail()).getByRole("button", { name: "Extract Endorphin pile" }));
    await waitFor(() =>
      expect(listArchiveEntriesMock).toHaveBeenCalledWith("import/endorphin.zip"),
    );
  });

  it("offers no extract door on a blocked row that is not an archive", () => {
    // The twin is attached whatever the status, so an unresolved directory can
    // name one; extracting it is not this row's remedy and must stay shut.
    const unresolvedWithTwin = makeBundle({
      ...legacyVase,
      archive: { rel_path: "Legacy_vase.zip", twin_verified: true },
    });
    renderView(makeState({ report: makeReport([unresolvedWithTwin]) }));

    const key = unresolvedWithTwin.key;
    expect(rowElement(key).getAttribute("aria-keyshortcuts")).toBeNull();
    expect(statusCell(key).getAttribute("title")).toBeNull();
    expect(within(detail()).queryByRole("button", { name: /^Extract / })).toBeNull();
    fireEvent.click(statusCell(key));
    expect(screen.queryByRole("dialog", { name: /^Extract / })).toBeNull();
    cleanup();

    // A ready row names a twin too - as provenance for the folder. Same answer:
    // the reason gate, not the mere presence of a path, decides the door.
    const readyWithTwin = makeBundle({
      archive: { rel_path: "Moai.zip", twin_verified: true },
    });
    renderView(makeState({ report: makeReport([readyWithTwin]) }));

    expect(rowElement(readyWithTwin.key).getAttribute("aria-keyshortcuts")).toBeNull();
    expect(statusCell(readyWithTwin.key).getAttribute("title")).toBeNull();
    expect(within(detail()).queryByRole("button", { name: /^Extract / })).toBeNull();
  });

  it("disables a zero-count primary action, and its click writes nothing", () => {
    const state = makeState({ report: makeReport([moaiZip]) });
    renderView(state);

    const create = screen.getByRole("button", { name: "Create selected (0)" }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);
    // The primary constant carries a `disabled:` variant, so a zero-count action
    // reads as shut rather than merely inert. Assert the variant's PRESENCE rather
    // than copying the constant, and never the exact utility: the point is that
    // dropping the styling fails a test.
    expect(create.className.split(" ").some(name => name.startsWith("disabled:"))).toBe(true);
    fireEvent.click(create);
    expect(state.create).not.toHaveBeenCalled();

    // The zero-count secondary action carries the same variant, so the bulk bar
    // cannot ship one family styled and one merely inert.
    const extractNone = screen.getByRole("button", {
      name: "Extract selected archives (0)",
    }) as HTMLButtonElement;
    expect(extractNone.disabled).toBe(true);
    expect(extractNone.className.split(" ").some(name => name.startsWith("disabled:"))).toBe(
      true,
    );

    // The same selection proves the contrast: a blocked row can be dismissed, so
    // the bar is not disabling everything it draws.
    fireEvent.click(selectionCheck(moaiZip.key));
    expect(
      (screen.getByRole("button", { name: "Dismiss selected (1)" }) as HTMLButtonElement).disabled,
    ).toBe(false);
    expect(
      (screen.getByRole("button", { name: "Create selected (0)" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("opens the extract flow from the keyboard with Enter on an archive-only row", () => {
    renderView(makeState());

    // Enter on a ready row opens nothing: only the archive-only row advertises
    // the shortcut, and the two halves must agree about that.
    fireEvent.keyDown(rowElement("Moai"), { key: "Enter" });
    expect(screen.queryByRole("dialog", { name: /^Extract / })).toBeNull();

    // The handler keys off the active row, which End moves - exactly how the
    // existing arrow/Space tests drive the listbox.
    fireEvent.keyDown(rowElement("Moai"), { key: "End" });
    expect(detailTitle()).toBe("Moai zip");
    fireEvent.keyDown(rowElement("import/Moai.zip"), { key: "Enter" });

    expect(screen.getByRole("dialog", { name: "Extract Moai zip" })).toBeTruthy();
    // Advertised without a tab stop: the option says Enter, and nothing else does.
    expect(rowElement("import/Moai.zip").getAttribute("aria-keyshortcuts")).toBe("Enter");
    expect(rowElement("Moai").getAttribute("aria-keyshortcuts")).toBeNull();
  });

  it("gives a viewer no affordance on the blocked reason", () => {
    renderView(makeState(), false);

    fireEvent.click(rowElement("import/Moai.zip"));
    expect(detailTitle()).toBe("Moai zip");

    const cell = statusCell("import/Moai.zip");
    expect(cell.getAttribute("title")).toBeNull();
    fireEvent.click(cell);

    expect(screen.queryByRole("dialog", { name: /^Extract / })).toBeNull();
    expect(rowElement("import/Moai.zip").getAttribute("aria-keyshortcuts")).toBeNull();
    expect(within(detail()).queryByRole("button", { name: /^Extract / })).toBeNull();
  });

  it("says what the queue is waiting for while the scan registers the archive", async () => {
    let settle: (outcome: SettleOutcome) => void = () => {};
    const afterExtract = vi.fn(
      () =>
        new Promise<SettleOutcome>(resolve => {
          settle = resolve;
        }),
    );
    const state = makeState({ report: makeReport([moaiZip]), afterExtract });
    renderView(state);

    fireEvent.click(within(detail()).getByRole("button", { name: "Extract Moai zip" }));
    await waitFor(() => expect(listArchiveEntriesMock).toHaveBeenCalledWith("import/Moai.zip"));
    fireEvent.click(await screen.findByRole("button", { name: "Extract 2 files" }));

    const notice = await screen.findByText(/waiting for the library scan to register/);
    expect(notice.getAttribute("role")).toBe("status");
    expect(afterExtract).toHaveBeenCalledWith([EXTRACT_RESULT]);

    settle("settled");
    await waitFor(() =>
      expect(screen.queryByText(/waiting for the library scan to register/)).toBeNull(),
    );
    // A settle that finished is not a slow one, so the queue says nothing.
    expect(screen.queryByText(/taking longer than expected/)).toBeNull();

    cleanup();

    // The walk can give up: the notice has to stop promising and say so.
    renderView(
      makeState({
        report: makeReport([moaiZip]),
        afterExtract: vi.fn(async (): Promise<SettleOutcome> => "timeout"),
      }),
    );
    fireEvent.click(within(detail()).getByRole("button", { name: "Extract Moai zip" }));
    await waitFor(() => expect(listArchiveEntriesMock).toHaveBeenCalledWith("import/Moai.zip"));
    fireEvent.click(await screen.findByRole("button", { name: "Extract 2 files" }));

    const timedOut = await screen.findByText(/taking longer than expected/);
    expect(timedOut.getAttribute("role")).toBe("status");

    // That line tells the operator to refresh the queue, so taking its advice
    // has to clear it: a notice that outlives its own instruction is a lie.
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() =>
      expect(screen.queryByText(/taking longer than expected/)).toBeNull(),
    );
  });

  it("offers the bulk extract only for a selection of archive-only rows", () => {
    renderView(makeState());

    // Nothing selected: the door is shut and its count says zero.
    const none = screen.getByRole("button", {
      name: "Extract selected archives (0)",
    }) as HTMLButtonElement;
    expect(none.disabled).toBe(true);

    // One archive-only row: the button opens the same dialog the row does.
    fireEvent.click(selectionCheck("import/Moai.zip"));
    const one = screen.getByRole("button", {
      name: "Extract selected archives (1)",
    }) as HTMLButtonElement;
    expect(one.disabled).toBe(false);
    fireEvent.click(one);
    expect(screen.getByRole("dialog", { name: "Extract Moai zip" })).toBeTruthy();
    cleanup();

    // Mixed: two rows are selected, but only the zip-only one is extractable.
    renderView(makeState());
    fireEvent.click(selectionCheck("import/Moai.zip"));
    fireEvent.click(selectionCheck("Moai"));
    expect(screen.getByText("2 bundles selected")).toBeTruthy();
    const mixed = screen.getByRole("button", {
      name: "Extract selected archives (1)",
    }) as HTMLButtonElement;
    expect(mixed.disabled).toBe(false);
    cleanup();

    // No zip-only row in the selection: the same button counts nothing.
    renderView(makeState());
    fireEvent.click(selectionCheck("Moai"));
    const ready = screen.getByRole("button", {
      name: "Extract selected archives (0)",
    }) as HTMLButtonElement;
    expect(ready.disabled).toBe(true);
  });

  it("drops a stale confirmation when the row changes to another archive", async () => {
    installArchiveListings();
    renderView(makeState({ report: makeReport([alphaZip, betaZip]) }));

    fireEvent.click(rowElement(ALPHA_KEY));
    fireEvent.click(within(detail()).getByRole("button", { name: "Extract Alpha" }));
    await screen.findByRole("button", { name: "Extract 2 files" });

    fireEvent.click(rowElement(BETA_KEY));

    // The confirmation belonged to Alpha. Left standing it would read as
    // Beta's own - one archive confirmed, another extracted - so the action has
    // to be back at idle, offering the new row's trigger instead.
    expect(detailTitle()).toBe("Beta");
    expect(within(detail()).queryByRole("button", { name: /^Extract \d+ files$/ })).toBeNull();
    expect(within(detail()).getByRole("button", { name: "Extract Beta" })).toBeTruthy();
  });

  it("writes the archive the new row names, never the one the row left behind", async () => {
    installArchiveListings();
    renderView(makeState({ report: makeReport([alphaZip, betaZip]) }));

    fireEvent.click(rowElement(ALPHA_KEY));
    fireEvent.click(within(detail()).getByRole("button", { name: "Extract Alpha" }));
    await screen.findByRole("button", { name: "Extract 2 files" });
    // Alpha was listed only to reach its confirmation - nothing was written.
    expect(listArchiveEntriesMock).toHaveBeenCalledTimes(1);
    expect(listArchiveEntriesMock).toHaveBeenCalledWith("Alpha.zip");

    fireEvent.click(rowElement(BETA_KEY));
    // The row change is where the old bug fired: a confirmation still armed for
    // Alpha was the one on screen, so the write it led to named Alpha.
    expect(extractArchiveMock).not.toHaveBeenCalled();

    fireEvent.click(within(detail()).getByRole("button", { name: "Extract Beta" }));
    fireEvent.click(await screen.findByRole("button", { name: "Extract 3 files" }));

    await waitFor(() => expect(extractArchiveMock).toHaveBeenCalledTimes(1));
    expect(extractArchiveMock).toHaveBeenCalledWith({ path: "Beta.zip", entries: null });

    // Alpha.zip is legitimately listed on the way to its confirmation, so the
    // "never Alpha" claim is scoped to the write and to every listing after the
    // row change - the call pinned above makes the first of those the only one.
    expect(extractArchiveMock.mock.calls.map(([request]) => request.path)).toEqual(["Beta.zip"]);
    const listedAfterRowChange = listArchiveEntriesMock.mock.calls.slice(1).map(([path]) => path);
    expect(listedAfterRowChange).toEqual(["Beta.zip"]);
  });

  it("does not take focus when the remount drops the stale confirmation", async () => {
    installArchiveListings();
    renderView(makeState({ report: makeReport([alphaZip, betaZip]) }));

    fireEvent.click(rowElement(ALPHA_KEY));
    fireEvent.click(within(detail()).getByRole("button", { name: "Extract Alpha" }));
    await screen.findByRole("button", { name: "Extract 2 files" });

    // In a browser the confirmation panel's own button held the caret. The
    // remount must not chase it: `previousStageRef` starts null, so the mount
    // is skipped.
    (document.activeElement as HTMLElement).blur();
    expect(document.activeElement).toBe(document.body);

    fireEvent.click(rowElement(BETA_KEY));

    expect(document.activeElement).toBe(document.body);
    expect(within(detail()).getByRole("button", { name: "Extract Beta" })).toBeTruthy();
  });

  it("leaves the operator's caret on the row that changed", async () => {
    installArchiveListings();
    renderView(makeState({ report: makeReport([alphaZip, betaZip]) }));

    fireEvent.click(rowElement(ALPHA_KEY));
    fireEvent.click(within(detail()).getByRole("button", { name: "Extract Alpha" }));
    await screen.findByRole("button", { name: "Extract 2 files" });

    // `fireEvent.click` does not move focus in jsdom, so the caret has to be
    // placed by hand - this is the row the operator was reading when the panel
    // reset itself.
    const betaRow = rowElement(BETA_KEY);
    betaRow.focus();
    fireEvent.click(betaRow);

    expect(document.activeElement).toBe(betaRow);
  });

  it("returns focus from the aside's own extract trigger to the row that replaces the archive", async () => {
    // The queue as the settle refetch leaves it: the zip-only row is gone and
    // the extracted directory has registered as a ready row.
    function RefetchHarness() {
      const [bundles, setBundles] = React.useState([moaiZip]);
      const state = makeState({
        report: makeReport(bundles),
        afterExtract: vi.fn(async (): Promise<SettleOutcome> => {
          setBundles([moai]);
          return "settled";
        }),
      });
      return <BundleReviewView state={state} canContribute {...scanProps()} />;
    }
    render(<RefetchHarness />);

    const trigger = within(detail()).getByRole("button", { name: "Extract Moai zip" });
    // `fireEvent.click` does not move focus in jsdom, so the operator's caret
    // has to be placed by hand - and asserted, or this test would pass through
    // the listbox's own recovery and prove nothing about the aside door.
    trigger.focus();
    expect(detail().contains(document.activeElement)).toBe(true);

    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole("button", { name: "Extract 2 files" }));

    await waitFor(() => expect(detailTitle()).toBe("Moai statue"));
    expect(document.activeElement).not.toBe(document.body);
    expect(document.activeElement?.getAttribute("data-bundle-row")).toBe("Moai");
  });

  it("returns focus to the row that opened the extract dialog when it closes", () => {
    renderView(makeState());

    fireEvent.click(rowElement("import/Moai.zip"));
    fireEvent.click(statusCell("import/Moai.zip"));
    expect(screen.getByRole("dialog", { name: "Extract Moai zip" })).toBeTruthy();

    // The precondition: the dialog put the caret on a real control of its own.
    // Without this the Escape below could be firing anywhere.
    const close = within(screen.getByRole("dialog", { name: "Extract Moai zip" }))
      .getByRole("button", { name: "Close" });
    expect(document.activeElement).toBe(close);

    fireEvent.keyDown(close, { key: "Escape" });

    expect(screen.queryByRole("dialog", { name: "Extract Moai zip" })).toBeNull();
    expect(document.activeElement).toBe(rowElement("import/Moai.zip"));
  });

  it("reports a failed extraction that landed after the row changed, naming the one that failed", async () => {
    // A file-wide mock answers every path identically, which would let a panel
    // listing the wrong archive still show the expected count.
    installArchiveListings();
    const message = "Refusing to overwrite: beta.stl";
    let rejectExtract: (reason: unknown) => void = () => {};
    extractArchiveMock.mockReturnValue(
      new Promise<ArchiveExtractResult>((_resolve, reject) => {
        rejectExtract = reject;
      }),
    );

    const view = render(
      <>
        <BundleReviewView
          {...scanProps()}
          state={makeState({ report: makeReport([alphaZip, betaZip]) })}
          canContribute
        />
        <Toaster />
      </>,
    );

    fireEvent.click(rowElement(BETA_KEY));
    fireEvent.click(within(detail()).getByRole("button", { name: "Extract Beta" }));
    // Three entries, not two: Beta is the archive that was actually listed.
    fireEvent.click(await screen.findByRole("button", { name: "Extract 3 files" }));

    // The write is in flight before the row moves, so the rejection below has no
    // live instance left to paint into.
    await waitFor(() =>
      expect(extractArchiveMock).toHaveBeenCalledWith({ path: "Beta.zip", entries: null }),
    );

    fireEvent.click(rowElement(ALPHA_KEY));
    expect(detailTitle()).toBe("Alpha");

    await act(async () => {
      rejectExtract(new Error(message));
    });

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(`Extract Beta.zip failed: ${message}`);
    expect(alert.textContent).not.toContain("Alpha.zip");
    expect(getToasts()).toHaveLength(1);
    // The aside is showing Alpha now, so a line about Beta can only be a lie.
    expect(within(detail()).queryByText(/Extract Beta\.zip failed/)).toBeNull();

    // The settle refetch drops the archive that failed: the toast is all that is
    // left of that press, and it must not go away with the row.
    view.rerender(
      <>
        <BundleReviewView state={makeState({ report: makeReport([alphaZip]) })} canContribute {...scanProps()} />
        <Toaster />
      </>,
    );
    expect(rowElement(ALPHA_KEY)).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain(
      `Extract Beta.zip failed: ${message}`,
    );
    expect(getToasts()).toHaveLength(1);
    expect(within(detail()).queryByText(/Extract Beta\.zip failed/)).toBeNull();
  });
});

describe("BundleReviewView keyboard and focus", () => {
  it("keeps exactly one tab stop in the listbox", () => {
    renderView(makeState());

    const options = Array.from(listbox().querySelectorAll("[role='option']"));
    expect(options.length).toBe(4);
    expect(options.filter(option => option.getAttribute("tabindex") === "0").length).toBe(1);
    expect(options[0].getAttribute("tabindex")).toBe("0");
  });

  it("moves the active row and the detail panel with the arrow keys and Home/End", () => {
    renderView(makeState());

    expect(detailTitle()).toBe("Moai statue");

    fireEvent.keyDown(rowElement("Moai"), { key: "ArrowDown" });
    expect(detailTitle()).toBe("Cable clip");
    expect(document.activeElement?.getAttribute("data-bundle-row")).toBe("Cable_clip");

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "End" });
    expect(detailTitle()).toBe("Moai zip");

    // Clamped, no wrap: the last row stays the last row.
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "ArrowDown" });
    expect(detailTitle()).toBe("Moai zip");

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Home" });
    expect(detailTitle()).toBe("Moai statue");

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "ArrowUp" });
    expect(detailTitle()).toBe("Moai statue");
    expect(document.activeElement?.getAttribute("data-bundle-row")).toBe("Moai");
  });

  it("toggles the active row's selection with Space without moving the detail panel", () => {
    renderView(makeState());

    fireEvent.keyDown(rowElement("Moai"), { key: "ArrowDown" });
    expect(detailTitle()).toBe("Cable clip");
    expect(rowElement("Cable_clip").getAttribute("aria-selected")).toBe("false");

    fireEvent.keyDown(rowElement("Cable_clip"), { key: " " });

    expect(rowElement("Cable_clip").getAttribute("aria-selected")).toBe("true");
    expect(detailTitle()).toBe("Cable clip");
    expect(screen.getByText("1 bundle selected")).toBeTruthy();
  });

  it("moves focus to the row that replaces a vanished one on a programmatic refetch", () => {
    const { rerender } = render(
      <BundleReviewView state={makeState({ report: makeReport([moai, cableClip]) })} canContribute {...scanProps()} />,
    );

    rowElement("Moai").focus();
    expect(document.activeElement?.getAttribute("data-bundle-row")).toBe("Moai");

    // Programmatic-refetch path: a mouse click on a write button moves focus to
    // that button first, so this is not a claim that a click never steals the
    // operator's focus - it is the case where the row vanishes without one.
    rerender(
      <BundleReviewView
        {...scanProps()}
        state={makeState({ report: makeReport([cableClip, legacyVase]) })}
        canContribute
      />,
    );

    expect(rowKeys()).toEqual(["Cable_clip", "Legacy_vase"]);
    expect(document.activeElement?.getAttribute("data-bundle-row")).toBe("Cable_clip");
  });

  it("moves focus to the row that replaces a dismissed one", async () => {
    const remaining = [cableClip, legacyVase];
    function RefetchHarness() {
      const [bundles, setBundles] = React.useState([moai, cableClip, legacyVase]);
      const state = makeState({
        report: makeReport(bundles),
        dismiss: vi.fn(async (keys: string[]) => {
          setBundles(bundles.filter(row => !keys.includes(row.key)));
        }),
      });
      return <BundleReviewView state={state} canContribute {...scanProps()} />;
    }
    render(<RefetchHarness />);

    rowElement("Moai").focus();
    expect(document.activeElement?.getAttribute("data-bundle-row")).toBe("Moai");

    fireEvent.click(within(detail()).getByRole("button", { name: "Dismiss" }));

    await waitFor(() => expect(rowKeys()).toEqual(remaining.map(row => row.key)));
    expect(document.activeElement?.getAttribute("data-bundle-row")).toBe("Cable_clip");
  });

  it("moves focus to the queue itself when the aside's extract empties the queue", async () => {
    function EmptyingHarness() {
      const [bundles, setBundles] = React.useState([moaiZip]);
      const state = makeState({
        report: makeReport(bundles),
        afterExtract: vi.fn(async (): Promise<SettleOutcome> => {
          setBundles([]);
          return "settled";
        }),
      });
      return <BundleReviewView state={state} canContribute {...scanProps()} />;
    }
    render(<EmptyingHarness />);

    const trigger = within(detail()).getByRole("button", { name: "Extract Moai zip" });
    trigger.focus();
    expect(detail().contains(document.activeElement)).toBe(true);

    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole("button", { name: "Extract 2 files" }));

    // Nothing survived to take the row's place: neither the listbox nor the
    // detail pane renders, so the queue's own region is the only home. It is
    // only a home because it accepts programmatic focus.
    await waitFor(() => expect(screen.queryByRole("listbox", { name: "Bundles to confirm" })).toBeNull());
    const queue = screen.getByRole("region", { name: "Bundles to confirm" });
    expect(document.activeElement).toBe(queue);
    expect(document.activeElement).not.toBe(document.body);
  });

  it("does not pull a filter chip's caret to the queue after an aside recovery", async () => {
    function RefetchHarness() {
      const [bundles, setBundles] = React.useState([moaiZip]);
      const state = makeState({
        report: makeReport(bundles),
        afterExtract: vi.fn(async (): Promise<SettleOutcome> => {
          setBundles([moai]);
          return "settled";
        }),
      });
      return <BundleReviewView state={state} canContribute {...scanProps()} />;
    }
    render(<RefetchHarness />);

    // First the aside's own recovery, which is what leaves a stale flag behind
    // if the ladder does not consume it.
    const trigger = within(detail()).getByRole("button", { name: "Extract Moai zip" });
    trigger.focus();
    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole("button", { name: "Extract 2 files" }));
    await waitFor(() => expect(detailTitle()).toBe("Moai statue"));
    expect(document.activeElement?.getAttribute("data-bundle-row")).toBe("Moai");

    // Now the operator picks a filter that hides the one row left. jsdom does
    // not focus on click, so the chip is focused by hand first. `Moai` is a
    // high-confidence ready row, so `Medium confidence` empties the filtered
    // list while `rows` stays non-empty - the branch that renders the toolbar,
    // and with it this chip.
    const chip = screen.getByRole("button", { name: "Medium confidence" });
    chip.focus();
    expect(document.activeElement).toBe(chip);
    fireEvent.click(chip);

    await screen.findByText("No bundles match this filter.");
    // The layout - and with it the chip that was clicked - is still mounted: a
    // filter that hides every row is not an empty queue.
    expect(screen.queryByRole("button", { name: "Medium confidence" })).not.toBeNull();
    expect(document.activeElement).toBe(chip);
  });
});

describe("BundleReviewView detail panel", () => {
  it("renders the whole row: counts, cover, archive, metadata, licence and excerpt", () => {
    const populated = makeBundle({ archive: { rel_path: "Moai.zip", twin_verified: true } });
    renderView(makeState({ report: makeReport([populated]) }));

    const panel = detail();
    expect(within(panel).getByRole("heading", { name: "Moai statue" })).toBeTruthy();
    expect(within(panel).getByText("Ready")).toBeTruthy();
    expect(within(panel).getByText("Moai")).toBeTruthy();
    expect(
      within(panel).getByText(
        "3 models, 5 images, 2 documents, 0 other files, \u2013 archives - 8 registered assets",
      ),
    ).toBeTruthy();
    expect(within(panel).getByText("Cover images/hero.jpg")).toBeTruthy();
    expect(within(panel).getByText("Archive Moai.zip - twin verified")).toBeTruthy();
    expect(within(panel).getByText("Creator sculpteo")).toBeTruthy();
    expect(within(panel).getByText("Licence CC BY")).toBeTruthy();
    expect(
      within(panel).getByText("Source https://www.thingiverse.com/thing:2493386"),
    ).toBeTruthy();
    expect(within(panel).getByText("Detected by thingiverse-readme (root-docs)")).toBeTruthy();
    expect(within(panel).getByText("A Moai statue.")).toBeTruthy();
  });

  it("asks for a selection when the filter hid every row", () => {
    renderView(makeState({ report: makeReport([moai]) }));

    fireEvent.click(screen.getByRole("button", { name: "Blocked" }));

    expect(within(detail()).getByText("Select a bundle to see its details.")).toBeTruthy();
  });
});

describe("BundleReviewView compound groups", () => {
  it("renders every group's name and part count, and no checkbox when none is foreign", () => {
    renderView(makeState({ report: makeReport([compoundOwn]) }));

    const block = groupsBlock();
    expect(within(block).getByText("Body - 2 parts")).toBeTruthy();
    // Singular on purpose: 1 must read "1 part", not "1 parts".
    expect(within(block).getByText("Lid - 1 part")).toBeTruthy();
    // Nothing is somebody else's thing, so the operator has nothing to decide:
    // a checkbox here would offer an exclusion the server has no reason to take.
    expect(within(block).queryByRole("checkbox")).toBeNull();
  });

  it("names a foreign group as unrelated and offers one checked exclusion", () => {
    renderView(makeState({ report: makeReport([compound]) }));

    const block = groupsBlock();
    expect(within(block).getByText("Gearbox - 3 parts")).toBeTruthy();
    expect(within(block).getByText("unrelated")).toBeTruthy();

    // Exactly one, and only the foreign group gets it: the row's own part is not
    // the operator's to remove.
    const boxes = within(block).getAllByRole("checkbox");
    expect(boxes).toHaveLength(1);
    expect(within(block).getByRole("checkbox", { name: "Include Gearbox" })).toBeTruthy();
    // Checked by default: excluding is opt-in, so an untouched row includes
    // every group.
    expect((boxes[0] as HTMLInputElement).checked).toBe(true);
  });

  it("offers no checkbox for a foreign group the server would refuse to exclude", () => {
    renderView(makeState({ report: makeReport([compoundNotSeparable]) }));

    const block = groupsBlock();
    expect(within(block).getByText("Gearbox - 3 parts")).toBeTruthy();
    expect(within(block).getByText("unrelated")).toBeTruthy();
    // Not separable means it was never a row of its own, so an exclusion would
    // come back `not_separable_group`; the control must not exist to ask for it.
    expect(within(block).queryByRole("checkbox")).toBeNull();
    expect(screen.queryByRole("checkbox", { name: "Include Gearbox" })).toBeNull();
  });

  it("creates the project without an unchecked foreign group", () => {
    const state = makeState({ report: makeReport([autoCompound]) });
    renderView(state);

    fireEvent.click(screen.getByRole("checkbox", { name: "Include Gearbox" }));
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    // The name is part of the body as it always was; the exclusion is the only
    // new key, and it carries the group's own name verbatim.
    expect(state.create).toHaveBeenCalledWith([
      { key: "StageTop", name: "StageTop Wave One", exclude_groups: ["Gearbox"], auto: true },
    ]);
  });

  it("omits exclude_groups entirely when nothing was unchecked", () => {
    const state = makeState({ report: makeReport([autoCompound]) });
    renderView(state);

    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    const item = vi.mocked(state.create).mock.calls[0][0][0];
    // Absent, not present-and-empty: the server reads the missing field as
    // "include every group", and the old body stays byte-identical (modulo the
    // AUTO flag this door now carries).
    expect(item).not.toHaveProperty("exclude_groups");
    expect(item).toEqual({ key: "StageTop", name: "StageTop Wave One", auto: true });
  });

  it("renders no group block for an empty list and sends the old body", () => {
    const state = makeState({ report: makeReport([autoRow({ groups: [] })]) });
    renderView(state);

    expect(screen.queryByRole("group", { name: "Groups in this bundle" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    expect(state.create).toHaveBeenCalledWith([{ key: "Moai", name: "Moai statue", auto: true }]);
  });

  it("keeps bulk doors on include-every-group even after an exclusion was toggled", () => {
    const state = makeState({ report: makeReport([compound]) });
    renderView(state);

    fireEvent.click(screen.getByRole("checkbox", { name: "Include Gearbox" }));
    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));
    fireEvent.click(screen.getByRole("button", { name: /^Create selected/ }));

    const bulkItems = vi.mocked(state.create).mock.calls[0][0];
    // Bulk create means "include every group": the exclusion lives in the detail
    // pane, and carrying it here would silently drop a part the operator never
    // excluded from this door.
    for (const item of bulkItems) expect(item).not.toHaveProperty("exclude_groups");

    vi.mocked(state.create).mockClear();
    fireEvent.click(screen.getByRole("button", { name: /^Create low as name-only/ }));

    expect(state.create).toHaveBeenCalledWith([{ key: "StageTop", name_only: true }]);
  });
});

describe("BundleReviewView import-as chooser", () => {
  function importAs(): HTMLElement {
    return within(detail()).getByRole("radiogroup", { name: "Import as" });
  }

  it("offers exactly one chooser, in the detail pane, in the served order", () => {
    renderView(makeState({ report: makeReport([moai]) }));

    expect(within(detail()).getAllByRole("radiogroup", { name: "Import as" })).toHaveLength(1);
    // Never in the list: an option must stay free of focusable controls.
    expect(listbox().querySelector("[role='radiogroup']")).toBeNull();

    const radios = within(importAs()).getAllByRole("radio") as HTMLInputElement[];
    expect(radios.map(radio => radio.value)).toEqual(["root-docs", SHAPE_MANUAL]);
    // A VISIBLE label per option, in human words rather than the wire tokens.
    expect(radios.map(radio => radio.labels?.[0]?.textContent)).toEqual([
      SHAPE_LABELS["root-docs"],
      SHAPE_LABELS[SHAPE_MANUAL],
    ]);
  });

  it("checks the recommended shape and shows that shape's counts", () => {
    renderView(makeState({ report: makeReport([moai]) }));

    const checked = within(importAs()).getByRole("radio", { checked: true }) as HTMLInputElement;
    expect(checked.value).toBe("root-docs");
    expect(detail().querySelector("[data-bundle-shape-counts]")?.textContent).toBe(
      "3 models, 5 images, 2 texts, 0 other",
    );
  });

  it("follows the choice with that shape's counts and note", () => {
    // The chosen match's own numbers, so the operator can see what `manual` would
    // import - including the parts the recommendation leaves out.
    const row = makeBundle({
      key: "StageTop",
      rel_path: "StageTop",
      title: "StageTop Wave One",
      name_suggestion: "StageTop Wave One",
      matches: [
        {
          shape: "compound",
          rule: "compound-bundle",
          confidence: "low",
          model_count: 45,
          image_count: 0,
          text_count: 1,
          other_count: 0,
          note: null,
        },
        {
          shape: SHAPE_MANUAL,
          rule: "manual",
          confidence: "low",
          model_count: 48,
          image_count: 2,
          text_count: 1,
          other_count: 3,
          note: "Everything under the folder, including the unrelated part.",
        },
      ],
    });
    renderView(makeState({ report: makeReport([row]) }));

    expect(detail().querySelector("[data-bundle-shape-counts]")?.textContent).toBe(
      "45 models, 0 images, 1 texts, 0 other",
    );

    fireEvent.click(within(importAs()).getByRole("radio", { name: SHAPE_LABELS[SHAPE_MANUAL] }));

    expect(detail().querySelector("[data-bundle-shape-counts]")?.textContent).toBe(
      "48 models, 2 images, 1 texts, 3 other",
    );
    expect(
      within(detail()).getByText("Everything under the folder, including the unrelated part."),
    ).toBeTruthy();
  });

  it("shows a reading the server cannot enact as a visible, selectable radio carrying its note", () => {
    // Catches a chooser that DROPS a non-enactable reading (the operator loses
    // the cardinality question) and one that DISABLES it: the split door is the
    // only way to make that reading true, and a disabled radio makes the door
    // unreachable from any payload the server actually emits.
    renderView(makeState({ report: makeReport([splitTopFolders]) }));

    const radios = within(importAs()).getAllByRole("radio") as HTMLInputElement[];
    expect(radios.map(radio => radio.value)).toEqual([
      "generic",
      "split-top-folders",
      SHAPE_MANUAL,
    ]);

    const split = within(importAs()).getByRole("radio", {
      name: SHAPE_LABELS["split-top-folders"],
    }) as HTMLInputElement;
    // Not busy, so the reading is a normal control the operator may choose.
    expect(split.disabled).toBe(false);
    expect(split.checked).toBe(false);

    // The recommendation still stands until the operator makes the choice.
    const checked = within(importAs()).getByRole("radio", { checked: true }) as HTMLInputElement;
    expect(checked.value).toBe("generic");

    // The reason is the SERVER's own `note`, visible beside the option and on the
    // wrapper's title so it is reachable on hover.
    const blocked = importAs().querySelector("[data-bundle-shape-blocked='true']");
    expect(blocked).not.toBeNull();
    expect(within(detail()).getByText(SPLIT_NOTE)).toBeTruthy();
    expect(blocked?.getAttribute("title")).toBe(SPLIT_NOTE);
    // Its own counts line still speaks about the recommendation until chosen.
    expect(detail().querySelector("[data-bundle-shape-counts]")?.textContent).toBe(
      "3 models, 0 images, 0 texts, 0 other",
    );
  });

  it("selects that reading, shuts the create door and offers the split door", async () => {
    // The contract change: choosing a reading the server cannot enact must not
    // silently create the recommendation. It selects, the Create button refuses
    // with the server's own reason, and the split door takes its place.
    const state = makeState({ report: makeReport([autoSplitTopFolders]) });
    renderView(state);

    const split = within(importAs()).getByRole("radio", {
      name: SHAPE_LABELS["split-top-folders"],
    }) as HTMLInputElement;
    fireEvent.click(split);

    expect(split.checked).toBe(true);
    expect(
      (within(importAs()).getByRole("radio", { checked: true }) as HTMLInputElement).value,
    ).toBe("split-top-folders");

    const create = within(detail()).getByRole("button", {
      name: "Create project",
    }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);
    expect(create.getAttribute("title")).toBe(SPLIT_NOTE);

    // The bulk door sends the same row, so it refuses for the same reason.
    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));
    const createSelected = screen.getByRole("button", {
      name: /^Create selected/,
    }) as HTMLButtonElement;
    expect(createSelected.disabled).toBe(true);
    expect(createSelected.getAttribute("title")).toBe(SPLIT_NOTE);

    // The split door takes the create door's place, and its one action is live
    // once the plan names something to do.
    await waitFor(() => expect(splitPreview()).toBeTruthy());
    expect(splitActionButton().disabled).toBe(false);
  });

  it("never sends a shape the server cannot enact", () => {
    // Catches a createItems that trusts a stored choice: the server answers
    // `needs-arrange` and writes nothing, so the body must never carry that
    // shape. The click is made first, so a build that only shades the disabled
    // control is caught here too: a disabled button fires no handler.
    const state = makeState({ report: makeReport([autoSplitTopFolders]) });
    renderView(state);

    fireEvent.click(
      within(importAs()).getByRole("radio", { name: SHAPE_LABELS["split-top-folders"] }),
    );
    const create = within(detail()).getByRole("button", {
      name: "Create project",
    }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);
    fireEvent.click(create);

    expect(state.create).not.toHaveBeenCalled();
  });

  it("keeps the create door shut when a refetch marks the stored choice unenactable", () => {
    // The real hole the guard closes: the operator picks a split reading while the
    // server still offers it, a refetch marks that same reading `creatable: false`,
    // and the stored choice must now shut the create door rather than snap back to
    // the recommendation and create something the operator did not choose.
    const enactable = makeBundle({
      ...autoSplitTopFolders,
      matches: autoSplitTopFolders.matches?.map(match =>
        match.shape === "split-top-folders" ? { ...match, creatable: true } : match,
      ),
    });
    const view = renderView(makeState({ report: makeReport([enactable]) }));

    fireEvent.click(
      within(importAs()).getByRole("radio", { name: SHAPE_LABELS["split-top-folders"] }),
    );
    expect(
      (within(importAs()).getByRole("radio", { checked: true }) as HTMLInputElement).value,
    ).toBe("split-top-folders");

    const refetched = makeState({ report: makeReport([autoSplitTopFolders]) });
    view.rerender(
      <BundleReviewView
        state={refetched}
        canContribute
        scan={makeScanState()}
        canScan={false}
        onScan={vi.fn()}
      />,
    );

    // The choice is still offered, so it still stands - and the create door
    // refuses it with the server's own note.
    expect(
      (within(importAs()).getByRole("radio", { checked: true }) as HTMLInputElement).value,
    ).toBe("split-top-folders");
    const create = within(detail()).getByRole("button", {
      name: "Create project",
    }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);
    expect(create.getAttribute("title")).toBe(SPLIT_NOTE);
    fireEvent.click(create);

    expect(refetched.create).not.toHaveBeenCalled();
  });

  it("offers manual on every row, high confidence and archive-only included", () => {
    renderView(makeState({ report: makeReport([moai]) }));
    expect(
      within(importAs()).getByRole("radio", { name: SHAPE_LABELS[SHAPE_MANUAL] }),
    ).toBeTruthy();
    cleanup();

    renderView(makeState({ report: makeReport([moaiZip]) }));
    expect(
      within(importAs()).getByRole("radio", { name: SHAPE_LABELS[SHAPE_MANUAL] }),
    ).toBeTruthy();
  });

  it("sends the chosen shape and never an exclusion when manual is picked", () => {
    const state = makeState({ report: makeReport([autoCompound]) });
    renderView(state);

    // An exclusion the operator made on the recommended compound shape...
    fireEvent.click(screen.getByRole("checkbox", { name: "Include Gearbox" }));
    fireEvent.click(within(importAs()).getByRole("radio", { name: SHAPE_LABELS[SHAPE_MANUAL] }));
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    // ...must not travel with a manual import: there are no groups to leave out.
    expect(state.create).toHaveBeenCalledWith([
      { key: "StageTop", name: "StageTop Wave One", shape: SHAPE_MANUAL, auto: true },
    ]);
    const item = vi.mocked(state.create).mock.calls[0][0][0];
    expect(item).not.toHaveProperty("exclude_groups");
  });

  it("keeps the group chooser only while compound is the chosen shape", () => {
    renderView(makeState({ report: makeReport([compound]) }));

    // The fixture's recommended shape is compound, so the chooser stands.
    expect(
      within(importAs()).getByRole("radio", {
        name: SHAPE_LABELS.compound,
        checked: true,
      }),
    ).toBeTruthy();
    expect(screen.getByRole("group", { name: "Groups in this bundle" })).toBeTruthy();

    fireEvent.click(within(importAs()).getByRole("radio", { name: SHAPE_LABELS[SHAPE_MANUAL] }));

    expect(screen.queryByRole("group", { name: "Groups in this bundle" })).toBeNull();
    expect(screen.queryByRole("checkbox", { name: "Include Gearbox" })).toBeNull();

    // Choosing the recommendation again puts it back: one piece of state, and the
    // chooser is reversible.
    fireEvent.click(within(importAs()).getByRole("radio", { name: SHAPE_LABELS.compound }));
    expect(screen.getByRole("group", { name: "Groups in this bundle" })).toBeTruthy();
  });

  it("gains no focusable control in the option, and the listbox keeps one tab stop", () => {
    renderView(makeState());

    const options = Array.from(listbox().querySelectorAll("[role='option']"));
    expect(options.length).toBe(4);
    for (const option of options) {
      // Not a button, not a radio, not the glyph: the chooser lives in the pane.
      expect(
        option.querySelector("button, input, [role='radio'], [role='checkbox'], [role='switch']"),
      ).toBeNull();
    }
    expect(options.filter(option => option.getAttribute("tabindex") === "0")).toHaveLength(1);
  });

  it("treats a payload with no matches as the one recommendation, and renders no wrapper", () => {
    // A cached payload from before the field was served: one shape, so there is
    // nothing to choose and no empty wrapper is left behind.
    const legacy = { ...moai } as Partial<BundleDetectItem>;
    delete legacy.matches;
    renderView(makeState({ report: makeReport([legacy as BundleDetectItem]) }));

    expect(within(detail()).queryByRole("radiogroup", { name: "Import as" })).toBeNull();
    expect(detail().querySelector("[data-bundle-shape-counts]")).toBeNull();
    // The pane still renders its facts: nothing was disturbed.
    expect(within(detail()).getByText("Ready")).toBeTruthy();
  });

  it("keeps the guided door live on a creatable blocked row and shut on the archive-only row", () => {
    renderView(makeState({ report: makeReport([legacyVase]) }));

    // `unresolved_directory` is a readiness fact now: the import door is live, and
    // the row does not call itself blocked beside it. A generic low row is a
    // guided review, so that live door is the panel's.
    expect(statusCell(legacyVase.key).textContent).toBe("no matching folder on disk");
    expect(
      (within(detail()).getByRole("button", { name: "Review and import" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);
    cleanup();

    renderView(makeState({ report: makeReport([moaiZip]) }));

    // The zip-only row offers its own door instead of a create. This fixture's
    // recommendation sets no `creatable: false`, so the words follow that door:
    // the sentence says what the row is and the refusal word is not spoken beside
    // a live Extract button.
    expect(statusCell(moaiZip.key).textContent).toBe(
      "zip only - extract the archive in place, then the scan registers it",
    );
    expect(statusCell(moaiZip.key).textContent).not.toContain("Blocked");
    expect(
      (within(detail()).getByRole("button", { name: "Create project" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(within(detail()).getByRole("button", { name: "Extract Moai zip" })).toBeTruthy();
  });
});

describe("BundleReviewView covers", () => {
  function coverRow(cover: BundleDetectItem["cover"]): BundleDetectItem {
    return makeBundle({ key: "Cover", title: "Cover bundle", rel_path: "Cover", cover });
  }

  it("renders a derived cover thumbnail from the served thumb url", () => {
    const row = coverRow({
      asset_id: "asset-7",
      rel_path: "images/hero.jpg",
      reason: "largest-image",
      url: "/file/asset-7/hero.jpg",
      thumb_url: "/thumb/asset-7.jpg?v=3",
    });
    const { container } = render(
      <BundleReviewView state={makeState({ report: makeReport([row]) })} canContribute {...scanProps()} />,
    );

    const images = Array.from(container.querySelectorAll("img"));
    expect(images.length).toBeGreaterThan(0);
    for (const image of images) {
      // `fileUrl()` prefixes the API base, so the path is contained, not equal.
      expect(image.getAttribute("src")).toContain("/thumb/asset-7.jpg?v=3");
      expect(image.getAttribute("alt")).toBe("");
      expect(image.getAttribute("aria-hidden")).toBe("true");
    }
  });

  it("renders the model snapshot as the row cover when only a preview is derivable", async () => {
    const row = coverRow({
      asset_id: "asset-9",
      rel_path: "model.stl",
      reason: "model-preview",
      url: "/file/asset-9/model.stl",
      thumb_url: null,
    });
    renderView(makeState({ report: makeReport([row]) }));

    await waitFor(() => expect(modelSnapshotProps.length).toBeGreaterThan(0));
    // The row cell and the detail fact are the two mounted sites, so every call
    // has to carry the same derivation.
    for (const props of modelSnapshotProps) {
      expect(props).toMatchObject({ ext: "stl", assetId: "asset-9", showStatus: false });
      expect(props.url).toContain("/file/asset-9/model.stl");
    }
    // The loop above is satisfied by one call, so it cannot prove both sites are
    // mounted. The props are identical at both, so the containers are the only
    // thing that tells them apart - and either site going missing turns this red.
    expect(rowElement("Cover").querySelector(".review-thumb [data-model-snapshot]")).not.toBeNull();
    expect(detail().querySelector(".review-thumb [data-model-snapshot]")).not.toBeNull();
  });

  it("renders no image element for a bundle with no cover", () => {
    const row = coverRow({
      asset_id: null,
      rel_path: null,
      reason: "none",
      url: null,
      thumb_url: null,
    });
    const { container } = render(
      <BundleReviewView state={makeState({ report: makeReport([row]) })} canContribute {...scanProps()} />,
    );

    expect(container.querySelector("img")).toBeNull();
    expect(modelSnapshotProps).toHaveLength(0);
    expect(within(detail()).getByText("Cover: none")).toBeTruthy();
  });

  it("keeps the cover cell in the row for a bundle with no cover", () => {
    const row = coverRow({
      asset_id: null,
      rel_path: null,
      reason: "none",
      url: null,
      thumb_url: null,
    });
    renderView(makeState({ report: makeReport([row]) }));

    // jsdom does no layout, so the structure that guarantees the six-track grid
    // is what gets pinned: a `null` CoverThumb is not a grid item at all, which
    // left the leading track empty and shifted every later cell one track left.
    expect(rowElement("Cover").querySelector(".review-thumb")).not.toBeNull();
  });

  it("gives a no-cover row the same six cells as a model-preview row", () => {
    const noCover = coverRow({
      asset_id: null,
      rel_path: null,
      reason: "none",
      url: null,
      thumb_url: null,
    });
    const preview = makeBundle({
      key: "Preview",
      title: "Preview bundle",
      rel_path: "Preview",
      cover: {
        asset_id: "asset-9",
        rel_path: "model.stl",
        reason: "model-preview",
        url: "/file/asset-9/model.stl",
        thumb_url: null,
      },
    });
    renderView(makeState({ report: makeReport([noCover, preview]) }));

    const cells = rowElement("Cover").children.length;
    expect(cells).toBe(6);
    expect(rowElement("Preview").children.length).toBe(cells);
  });
});

describe("BundleReviewView duplicate archives", () => {
  it("announces the duplicate groups and their reclaimable size", () => {
    renderView(makeState({ report: withDuplicates() }));

    const panel = duplicatePanel();
    expect(within(panel).getByText("2 duplicate groups - 3.0 KB reclaimable")).toBeTruthy();
    // The notice is a reading aid, and says so where the operator reads it.
    expect(panel.textContent).toContain("Nothing is deleted or moved here");
    // Collapsed by default: the summary is the notice, the paths are on request.
    expect(within(panel).queryByText("Moai (1).zip")).toBeNull();
  });

  it("copies the server's duplicate text verbatim, not one it rebuilt", async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    renderView(makeState({ report: withDuplicates() }));

    fireEvent.click(screen.getByRole("button", { name: "Copy report" }));

    // `DUPLICATES_TEXT` contradicts `duplicates_total_bytes`, so a client-side
    // rebuild from the group fields could not match it.
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(DUPLICATES_TEXT));
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(getToasts()[0].tone).toBe("info");
  });

  it("toasts an error instead of pretending it copied", async () => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    Object.defineProperty(document, "execCommand", { configurable: true, value: () => false });
    renderView(makeState({ report: withDuplicates() }));

    fireEvent.click(screen.getByRole("button", { name: "Copy report" }));

    await waitFor(() => expect(getToasts()[0]?.tone).toBe("error"));
    Reflect.deleteProperty(document, "execCommand");
  });

  it("adds no element at all when the report has no duplicates", () => {
    // Byte-for-byte: the whole queue's markup with the panel present, minus the
    // panel's own serialisation, must equal the markup with no panel at all.
    // A stray wrapper, an empty `<div>` or a leftover placeholder cannot pass.
    renderView(makeState({ report: withDuplicates() }));
    const panelHtml = duplicatePanel().outerHTML;
    const queue = document.querySelector(".review-queue") as HTMLElement;
    const withHtml = queue.innerHTML;
    expect(withHtml).toContain(panelHtml);
    cleanup();

    // The second fixture is the payload an older server sends: the field omitted.
    const withoutDuplicates = [
      makeReport([moai], { duplicates: [], duplicates_total_bytes: 0, duplicates_text: "" }),
      makeReport([moai]),
    ];
    for (const report of withoutDuplicates) {
      renderView(makeState({ report }));
      expect(document.querySelector("[data-duplicate-report]")).toBeNull();
      expect((document.querySelector(".review-queue") as HTMLElement).innerHTML).toBe(
        withHtml.replace(panelHtml, ""),
      );
      cleanup();
    }
  });

  it("renders an en dash, never a false zero, when the walk was cut short", () => {
    renderView(
      makeState({
        report: makeReport([moai], {
          duplicates: [],
          duplicates_total_bytes: 0,
          duplicates_text: "",
          truncated: true,
        }),
      }),
    );

    // The panel exists - silence would be the under-reporting the ticket forbids.
    const text = duplicatePanel().textContent ?? "";
    expect(text).toContain("\u2013");
    expect(text).not.toContain("0 duplicate groups");
    expect(text).not.toContain("0 B reclaimable");
    // No text from the server, so no copy advertised.
    expect(screen.queryByRole("button", { name: "Copy report" })).toBeNull();
  });

  it("says the walk was cut short rather than under-reporting", () => {
    renderView(makeState({ report: withDuplicates({ truncated: true }) }));

    const panel = duplicatePanel();
    expect(
      within(panel).getByText(
        "The walk stopped at the report limit, so this list is a lower bound: some duplicate groups may be missing.",
      ),
    ).toBeTruthy();
    const text = panel.textContent ?? "";
    expect(text).toContain("Duplicate groups: \u2013");
    expect(text).toContain("reclaimable size \u2013");
    // An exact-looking "(2)" beside a `–` total would read as the whole list.
    expect(within(panel).getByRole("button", { name: "Show the 2 listed groups" })).toBeTruthy();
    expect(within(panel).queryByRole("button", { name: "Show groups (2)" })).toBeNull();
  });

  it("offers no destructive control", () => {
    renderView(makeState({ report: withDuplicates() }));
    fireEvent.click(screen.getByRole("button", { name: "Show groups (2)" }));

    const names = screen.getAllByRole("button").map(button => button.textContent ?? "");
    expect(names.length).toBeGreaterThan(0);
    // Not even a disabled one: a control that names a delete is a claim that
    // something here can delete.
    expect(names.filter(name => /delete|remove|move|rename|prune|clean|purge/i.test(name))).toEqual(
      [],
    );
  });

  it("expands to the groups' sizes, copy counts and paths", () => {
    renderView(makeState({ report: withDuplicates() }));

    fireEvent.click(screen.getByRole("button", { name: "Show groups (2)" }));

    const panel = duplicatePanel();
    expect(within(panel).getByText("2.0 KB - 2 copies - 2.0 KB reclaimable")).toBeTruthy();
    expect(within(panel).getByText("Moai.zip")).toBeTruthy();
    expect(within(panel).getByText("Moai (1).zip")).toBeTruthy();
    expect(within(panel).getByText("Ender3 (1).zip")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Hide groups" }));
    expect(within(panel).queryByText("Moai (1).zip")).toBeNull();
  });

  it("offers no copy when the server sent no report text", () => {
    renderView(makeState({ report: withDuplicates({ duplicates_text: "" }) }));

    // Nothing to copy means no button: a copy that copies nothing would only
    // toast a lie.
    expect(screen.queryByRole("button", { name: "Copy report" })).toBeNull();
    expect(
      within(duplicatePanel()).getByText("2 duplicate groups - 3.0 KB reclaimable"),
    ).toBeTruthy();
  });
});

describe("BundleReviewView scan fidelity", () => {
  it("announces the refused count and the rule-set stamp beside the duplicate report", () => {
    renderView(
      makeState({
        report: withDuplicates({ rule_set: FIDELITY_RULE_SET, refused: FIDELITY_REFUSALS }),
      }),
    );

    const panel = scanFidelityPanel();
    expect(within(panel).getByText("2 paths refused")).toBeTruthy();
    // The full stamp stays reachable in the sr-only span; the visible line only
    // carries the version and a prefix, which is enough to spot a change.
    expect(panel.textContent).toContain(FIDELITY_STAMP);
    expect(panel.textContent).toContain(FIDELITY_RULE_SET.version);
    expect(panel.textContent).toContain(FIDELITY_STAMP.slice(0, 8));
    // Both notices hold their own place: the new one did not displace the old.
    expect(
      within(document.querySelector("[data-duplicate-report]") as HTMLElement).getByText(
        "2 duplicate groups - 3.0 KB reclaimable",
      ),
    ).toBeTruthy();
  });

  it("adds no element at all when the server said nothing about the scan", () => {
    // Byte-for-byte: the whole queue's markup with the panel present, minus the
    // panel's own serialisation, must equal the markup with no panel at all. A
    // stray wrapper, an empty `<div>` or a leftover placeholder cannot pass.
    // Each fixture is compared against its OWN baseline, because a truncated
    // report legitimately renders other notices too.
    const shapes: Array<Partial<BundleReport>> = [{}, { truncated: true }];
    for (const shape of shapes) {
      renderView(makeState({ report: withFidelity(shape) }));
      const panelHtml = scanFidelityPanel().outerHTML;
      const withHtml = (document.querySelector(".review-queue") as HTMLElement).innerHTML;
      expect(withHtml).toContain(panelHtml);
      cleanup();

      // The second fixture is the payload an older server sends: the fields omitted.
      renderView(makeState({ report: makeReport([moai], shape) }));
      expect(document.querySelector("[data-scan-fidelity]")).toBeNull();
      expect((document.querySelector(".review-queue") as HTMLElement).innerHTML).toBe(
        withHtml.replace(panelHtml, ""),
      );
      cleanup();
    }
  });

  it("renders zero as zero rather than a dash", () => {
    renderView(makeState({ report: withFidelity({ refused: [] }) }));

    // A real zero is the server's answer, and the notice exists because it said so.
    const panel = scanFidelityPanel();
    expect(panel.textContent).toContain("0 paths refused");
    expect(within(panel).queryByText(`Paths refused: \u2013`)).toBeNull();
  });

  it("offers no control that acts on a path", () => {
    renderView(makeState({ report: withFidelity() }));

    const panel = scanFidelityPanel();
    expect(panel.querySelectorAll("button, a")).toHaveLength(0);
    // Not even a disabled one: a control that names a delete is a claim that
    // something here can delete.
    const names = screen.getAllByRole("button").map(button => button.textContent ?? "");
    expect(names.length).toBeGreaterThan(0);
    expect(names.filter(name => /delete|remove|move|rename|prune|clean|purge/i.test(name))).toEqual(
      [],
    );
  });
});

describe("BundleReviewView extract and create door", () => {
  /** The panel's preview, found by the attribute the shared operation panels use. */
  function extractPanel(): HTMLElement | null {
    return detail().querySelector<HTMLElement>("[data-extract-plan]");
  }

  function extractAction(): HTMLButtonElement | null {
    return detail().querySelector<HTMLButtonElement>("[data-extract-action-button]");
  }

  it("mounts the panel on a zip-only row, and only there", async () => {
    // Catches a viewer gate that still plans, and a door left on a row that has
    // nothing to extract: the panel is the only extract-and-create door left.
    renderView(makeState({ report: makeReport([moaiZip]) }));
    await waitFor(() => expect(extractPanel()).toBeTruthy());
    expect(planOperationMock).toHaveBeenCalledWith({
      workflow: "extract-and-create",
      target: "import/Moai.zip",
      params: { name: "Moai zip" },
    });
    cleanup();

    // A ready row needs no extraction, so the door is not offered.
    planOperationMock.mockClear();
    renderView(makeState({ report: makeReport([moai]) }));
    expect(detail().querySelector("[data-extract-plan]")).toBeNull();
    expect(planOperationMock).not.toHaveBeenCalledWith(
      expect.objectContaining({ workflow: "extract-and-create" }),
    );
    cleanup();

    // A zip-only row that already became a project has nothing left to do.
    const alreadyCreated = makeBundle({
      ...moaiZip,
      existing_project_id: "p1",
      existing_project_name: "Moai zip",
    });
    renderView(makeState({ report: makeReport([alreadyCreated]), showCreated: true }));
    expect(detailTitle()).toBe("Moai zip");
    expect(detail().querySelector("[data-extract-plan]")).toBeNull();
    cleanup();

    // A viewer never sees a write door, and must not cause a plan request.
    planOperationMock.mockClear();
    renderView(makeState({ report: makeReport([moaiZip]) }), false);
    expect(detail().querySelector("[data-extract-plan]")).toBeNull();
    expect(planOperationMock).not.toHaveBeenCalledWith(
      expect.objectContaining({ workflow: "extract-and-create" }),
    );
  });

  it("keeps plain Extract beside the panel, and the bulk label byte-identical", async () => {
    renderView(makeState({ report: makeReport([moaiZip]) }));
    await waitFor(() => expect(extractPanel()).toBeTruthy());

    // Both doors in the aside at once: the panel is added, not swapped in for
    // plain Extract, which extracts and waits without creating a project.
    expect(within(detail()).getByRole("button", { name: "Extract Moai zip" })).toBeTruthy();
    expect(extractAction()?.textContent).toBe("Extract and create 'Moai' from 2 files");

    // The row's blocked-reason cell points at the whole job and keeps its click
    // behaviour: it is a text edit, not a new control.
    expect(statusCell("import/Moai.zip").getAttribute("title")).toBe(EXTRACT_AND_CREATE_ROW_TITLE);

    // The bulk bar's own door and its words are untouched.
    expect(
      (screen.getByRole("button", { name: "Extract selected archives (0)" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    fireEvent.click(selectionCheck("import/Moai.zip"));
    expect(screen.getByRole("button", { name: "Extract selected archives (1)" })).toBeTruthy();
  });

  it("reports a completed run to the host: the queue is re-walked and the list bumped", async () => {
    // The one-step door's landing machinery is gone with the route it called, so
    // the panel only says "something changed": the host re-walks the queue and
    // bumps the project list once.
    planOperationMock.mockResolvedValue(makeExtractPlan());
    submitOperationMock.mockResolvedValue(
      makeOperation({
        status: "done",
        steps: [
          makeOperationStep({ index: 0, op: "extract" }),
          makeOperationStep({ index: 1, op: "move" }),
          makeOperationStep({ index: 2, op: "scan" }),
          makeOperationStep({
            index: 3,
            op: "create-project",
            result: { project_id: "p1", name: "Moai", status: "created", created: true },
          }),
        ],
      }),
    );
    const onProjectsChanged = vi.fn();
    const state = makeState({ report: makeReport([moaiZip]) });
    render(
      <BundleReviewView
        {...scanProps()}
        state={state}
        canContribute
        onProjectsChanged={onProjectsChanged}
      />,
    );
    await waitFor(() => expect(extractAction()).toBeTruthy());

    fireEvent.click(extractAction() as HTMLButtonElement);

    await waitFor(() => expect(state.refresh).toHaveBeenCalledTimes(1));
    expect(onProjectsChanged).toHaveBeenCalledTimes(1);
    // The dialog narrates the operation rather than replacing it with a notice.
    expect(detail().querySelector("[data-extract-result]")?.textContent).toBe(
      "Done: project 'Moai' created",
    );
  });

  it("forwards a 401 from the panel's plan read to the shell's sign-out", async () => {
    // Catches a view that mounts the panel without the shell's sign-out: every
    // other door on this screen signs the operator out, so this one must too.
    const onUnauthorized = vi.fn();
    planOperationMock.mockImplementation(async input => {
      if (input.workflow === "extract-and-create") throw new UnauthorizedError();
      return makeOperationPlan();
    });
    renderView(makeState({ report: makeReport([moaiZip]) }), true, { onUnauthorized });

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    // The session's news is the shell's; the panel writes no sentence of its own.
    expect(detail().querySelector("[data-extract-failure]")).toBeNull();
  });
});

describe("BundleReviewView new parts on an already-created row", () => {
  const projectId = "3f2a6c1e-8f4b-4c7d-9a1e-2b5c8d0f4a61";

  /** A created row is hidden until Show created is on, so the harness starts
   *  there: the sentence lives in the detail panel of a row a reader opened. */
  function renderCreated(overrides: Partial<BundleDetectItem>) {
    const row = makeBundle({
      key: "Moai_created",
      title: "Moai created",
      existing_project_id: projectId,
      existing_project_name: "Moai statue",
      ...overrides,
    });
    render(<VisibilityHarness initial={makeState({ report: makeReport([row]), showCreated: true })} />);
    return row;
  }

  /** The sentence's own span, found by its attribute: it sits beside the
   *  created line rather than replacing any pinned string. */
  function newPartsSpan(): HTMLElement | null {
    return document.querySelector("[data-bundle-new-parts]");
  }

  it("reports the new parts beside the unchanged created line, with no create", () => {
    renderCreated({ bundle_drift_state: "known", bundle_drift_count: 3 });

    expect(newPartsSpan()?.textContent).toBe("3 new parts since this was created");
    // The existing sentence is untouched and still present: this is a second
    // fact about the same row, not a replacement.
    expect(within(detail()).getByText("Already created as project Moai statue")).toBeTruthy();
    // A second project for one folder is exactly what the marker prevents.
    expect(within(detail()).queryByRole("button", { name: "Create project" })).toBeNull();
    expect(within(detail()).queryByLabelText("Name for Moai created")).toBeNull();
  });

  it("stays quiet when the folder has not grown", () => {
    renderCreated({ bundle_drift_state: "known", bundle_drift_count: 0 });

    expect(newPartsSpan()).toBeNull();
    expect(within(detail()).queryByText(/new parts since this was created/)).toBeNull();
  });

  it("renders an en dash when the server cannot tell, never a zero", () => {
    renderCreated({ bundle_drift_state: "unknown", bundle_drift_count: null });

    // U+2013, and the sentence counts nothing rather than claiming a zero.
    expect(newPartsSpan()?.textContent).toBe("\u2013 new parts since this was created");
    expect(newPartsSpan()?.textContent).not.toContain("0");
  });
});

describe("BundleReviewView project list hand-off", () => {
  // App owns the project list, and these two callbacks are the only way the queue
  // can say "a project now exists that your list has never seen". The owner's
  // gesture: create a project from the queue, then find the Projects list
  // unchanged until a full page reload.
  //
  // `POST /bundles/create` answers one result per key, and `project_id` is
  // non-null on the `already_created` skip too (`bundle_service.py:2990-2996`), so
  // the doors' filter is on `status === "created"` alone - not on "has an id",
  // which is the same question asked the wrong way (the name-collision skip at
  // `:2987` answers `project_id: null`). The skip witnesses below therefore carry
  // a REAL id: silence about a row that has one is the whole point, and a fixture
  // that emptied `project_id` would test nothing.

  function createdItem(overrides: Partial<BundleCreateItemResult> = {}): BundleCreateItemResult {
    return {
      key: "Moai",
      status: "created",
      project_id: "project-moai",
      name: "Moai statue",
      assets_added: 3,
      skipped_assets: 0,
      failed_assets: [],
      reason: null,
      ...overrides,
    };
  }

  /** Counters derived from the rows, the way the server counts them, so a
   *  fixture cannot state a total its own rows contradict. */
  function createResponse(results: BundleCreateItemResult[]): BundleCreateResponse {
    const count = (status: string) => results.filter(row => row.status === status).length;
    return {
      created: count("created"),
      skipped: count("skipped"),
      blocked: count("blocked"),
      failed: count("failed"),
      results,
    };
  }

  /** Mount the view with the two hand-off props, and hand back the state the
   *  doors actually hold - the spies have to be the ones the view calls. */
  function handoff(
    overrides: Partial<BundleDetectState> = {},
    props: { onProjectsChanged?: () => void; onProjectImported?: (id: string) => void } = {},
    report?: BundleReport,
  ): BundleDetectState {
    const state = makeState(report ? { report, ...overrides } : overrides);
    render(
      <BundleReviewView
        {...scanProps()}
        state={state}
        canContribute
        onProjectsChanged={props.onProjectsChanged}
        onProjectImported={props.onProjectImported}
      />,
    );
    return state;
  }

  function tickRow(index: number): void {
    fireEvent.keyDown(screen.getAllByRole("option")[index], { key: " " });
  }

  function createSelectedButton(): HTMLElement {
    return screen.getByRole("button", { name: /^Create selected/ });
  }

  /** Let every queued microtask and timer callback run, so "nothing was reported"
   *  is read after the doors had their turn rather than beside it. */
  async function flush(): Promise<void> {
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 0));
    });
  }

  it("keeps working for a host that passes no callbacks at all", async () => {
    // Both props are optional: every pre-existing caller (and the ~200 tests
    // above) mounts without them, so pressing a create door must not throw.
    const state = handoff({ create: vi.fn(async () => createResponse([createdItem()])) });
    tickRow(0);
    expect(createSelectedButton().textContent).toBe("Create selected (1)");
    fireEvent.click(createSelectedButton());

    await waitFor(() => expect(state.create).toHaveBeenCalledTimes(1));
    await flush();
    // The payload is unchanged by the hand-off: one item per ticked row, the
    // server's suggested name filled in, exactly as the doors sent it before.
    expect(state.create).toHaveBeenCalledWith([{ key: "Moai", name: "Moai statue" }]);
    // Re-walking the queue is the hook's own job (its `create` refetches), not the
    // door's, so the view calls `refresh` for the extract door and never here.
    expect(state.refresh).not.toHaveBeenCalled();
  });

  it("lands on the one project the bulk door created", async () => {
    const onProjectImported = vi.fn();
    const onProjectsChanged = vi.fn();
    const state = handoff(
      { create: vi.fn(async () => createResponse([createdItem()])) },
      { onProjectImported, onProjectsChanged },
    );
    tickRow(0);
    fireEvent.click(createSelectedButton());

    await waitFor(() => expect(state.create).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onProjectImported).toHaveBeenCalledTimes(1));
    expect(onProjectImported).toHaveBeenCalledWith("project-moai");
    // Landing already bumps `assetsVersion` (App's `handleProjectImported`), so a
    // click that lands reports once, not twice.
    expect(onProjectsChanged).not.toHaveBeenCalled();
  });

  it("lands only when one click made one project: five created is a bump, not a landing", async () => {
    const onProjectImported = vi.fn();
    const onProjectsChanged = vi.fn();
    const state = handoff(
      {
        create: vi.fn(async () =>
          createResponse(
            ["a", "b", "c", "d", "e"].map(letter =>
              createdItem({ key: `K_${letter}`, project_id: `project-${letter}` }),
            ),
          ),
        ),
      },
      { onProjectImported, onProjectsChanged },
    );
    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));
    fireEvent.click(createSelectedButton());

    await waitFor(() => expect(state.create).toHaveBeenCalledTimes(1));
    await flush();
    await waitFor(() => expect(onProjectsChanged).toHaveBeenCalledTimes(1));
    // Landing on an arbitrary one of five is worse than not landing at all: the
    // list changes, the view does not.
    expect(onProjectImported).not.toHaveBeenCalled();
  });

  it("asks the verdict, not the request: a create the server refused answers null and reports nothing", async () => {
    const onProjectImported = vi.fn();
    const onProjectsChanged = vi.fn();
    const state = handoff(
      { create: vi.fn(async () => null) },
      { onProjectImported, onProjectsChanged },
    );
    tickRow(0);
    fireEvent.click(createSelectedButton());

    await waitFor(() => expect(state.create).toHaveBeenCalledTimes(1));
    await flush();
    expect(onProjectImported).not.toHaveBeenCalled();
    expect(onProjectsChanged).not.toHaveBeenCalled();
  });

  it("reports nothing the click did not create: skips, blocked and failed rows stay silent", async () => {
    const onProjectImported = vi.fn();
    const onProjectsChanged = vi.fn();
    const state = handoff(
      {
        create: vi.fn(async () =>
          createResponse([
            // A real `project_id` on the already-created skip, exactly as
            // `bundle_service.py:2990-2996` answers it: reporting this would append
            // - or open - a project that already existed before the click.
            createdItem({
              status: "skipped",
              project_id: "project-already-there",
              reason: "already_created",
            }),
            createdItem({
              key: "Cable_clip",
              status: "blocked",
              project_id: null,
              reason: "unresolved_directory",
            }),
            createdItem({
              key: "Legacy_vase",
              status: "failed",
              project_id: null,
              reason: "the door refused",
            }),
          ]),
        ),
      },
      { onProjectImported, onProjectsChanged },
    );
    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));
    fireEvent.click(createSelectedButton());

    await waitFor(() => expect(state.create).toHaveBeenCalledTimes(1));
    await flush();
    expect(onProjectImported).not.toHaveBeenCalled();
    expect(onProjectsChanged).not.toHaveBeenCalled();
  });

  it("does not land on a skip that carries an id, even for a single-row click", async () => {
    const onProjectImported = vi.fn();
    const onProjectsChanged = vi.fn();
    const state = handoff(
      {
        create: vi.fn(async () =>
          createResponse([
            createdItem({ status: "skipped", project_id: "project-already-there" }),
          ]),
        ),
      },
      { onProjectImported, onProjectsChanged },
    );
    tickRow(0);
    fireEvent.click(createSelectedButton());

    await waitFor(() => expect(state.create).toHaveBeenCalledTimes(1));
    await flush();
    expect(onProjectImported).not.toHaveBeenCalled();
    expect(onProjectsChanged).not.toHaveBeenCalled();
  });

  it("lands from the detail pane's Create project on the one row it asked for", async () => {
    const onProjectImported = vi.fn();
    const state = handoff(
      { create: vi.fn(async () => createResponse([createdItem()])) },
      { onProjectImported },
      makeReport([autoRow()]),
    );
    fireEvent.click(within(detail()).getByRole("button", { name: "Create project" }));

    await waitFor(() => expect(state.create).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onProjectImported).toHaveBeenCalledWith("project-moai"));
  });

  it("treats the name-only bulk door as a change to the list, never a landing", async () => {
    const onProjectImported = vi.fn();
    const onProjectsChanged = vi.fn();
    const state = handoff(
      {
        create: vi.fn(async () =>
          createResponse([
            createdItem({ key: "Cable_clip", project_id: "project-cable" }),
            createdItem({ key: "Sword_hilt", project_id: "project-sword" }),
          ]),
        ),
      },
      { onProjectImported, onProjectsChanged },
    );
    fireEvent.click(screen.getByRole("button", { name: /^Create low as name-only/ }));

    await waitFor(() => expect(state.create).toHaveBeenCalledTimes(1));
    await flush();
    await waitFor(() => expect(onProjectsChanged).toHaveBeenCalledTimes(1));
    expect(onProjectImported).not.toHaveBeenCalled();
  });

});

describe("BundleReviewView bulk extract and create", () => {
  /** The two bulk archive doors, read as the operator sees them: the same count
   *  and the same gate, from the same `extractableSelected` array. */
  function bothDoors(): { disabled: [boolean, boolean]; counts: [string, string] } {
    const plain = screen.getByRole("button", {
      name: /^Extract selected archives \(/,
    }) as HTMLButtonElement;
    const bulk = screen.getByRole("button", {
      name: /^Extract and create \(/,
    }) as HTMLButtonElement;
    const count = (node: HTMLButtonElement) => node.textContent?.match(/\((\d+)\)$/)?.[1] ?? "";
    return { disabled: [plain.disabled, bulk.disabled], counts: [count(plain), count(bulk)] };
  }

  /** The inline confirm, found by its own attribute: it is not a landmark and not
   *  a dialog - it lives in the bulk bar it was opened from. */
  function confirmPanel(): HTMLElement | null {
    return document.querySelector<HTMLElement>("[data-bulk-extract-and-create-confirm]");
  }

  /** The queue-level verdicts: one live region holding the summary and the named
   *  verdicts. */
  function bulkNotice(): HTMLElement | null {
    return document.querySelector<HTMLElement>("[data-bulk-extract-and-create-notice]");
  }

  /** One archive's finished operation, with the create step the server reports. */
  function createdOperation(
    target: string,
    projectId: string,
    name: string,
    status = "created",
  ): Operation {
    return makeOperation({
      id: `op:${target}`,
      target,
      status: "done",
      steps: [
        makeOperationStep({ index: 0, op: "extract" }),
        makeOperationStep({ index: 1, op: "move" }),
        makeOperationStep({ index: 2, op: "scan" }),
        makeOperationStep({
          index: 3,
          op: "create-project",
          result: { project_id: projectId, name, status, created: status === "created" },
        }),
      ],
    });
  }

  it("states the aggregate payload and writes nothing until confirmed", async () => {
    installArchiveListings();
    const state = makeState({ report: makeReport([alphaZip, betaZip]) });
    renderView(state);
    planOperationMock.mockClear();

    fireEvent.click(selectionCheck(ALPHA_KEY));
    fireEvent.click(selectionCheck(BETA_KEY));

    const open = screen.getByRole("button", {
      name: "Extract and create (2)",
    }) as HTMLButtonElement;
    expect(open.disabled).toBe(false);
    fireEvent.click(open);

    await waitFor(() => expect(confirmPanel()).toBeTruthy());

    // ALPHA holds 1 model and 1 document; BETA 1 model, 1 image and 1 document.
    // One sentence, from the summed plans of the same classifier the plain
    // Extract panel uses - never a sum of per-archive prose.
    expect((confirmPanel() as HTMLElement).textContent).toContain(
      "Extracting 2 archives: 2 models, 1 image, 2 documents, 5 files in all.",
    );
    // One listing per archive, read once, before anything is queued. (The detail
    // pane's own panel may have planned for the active row; that is a READ.)
    expect(listArchiveEntriesMock).toHaveBeenCalledTimes(2);
    expect(
      planOperationMock.mock.calls.filter(([input]) => input.workflow === "extract-and-create"),
    ).toEqual([]);
    expect(submitOperationMock).not.toHaveBeenCalled();
    expect(state.waitAfterExtract).not.toHaveBeenCalled();
  });

  it("moves both bulk archive buttons together", () => {
    renderView(makeState({ report: makeReport([moai, moaiZip, cableClip]) }));

    expect(bothDoors()).toEqual({ disabled: [true, true], counts: ["0", "0"] });

    fireEvent.click(selectionCheck(moaiZip.key));
    expect(bothDoors()).toEqual({ disabled: [false, false], counts: ["1", "1"] });

    // A ready row beside the archive is selected too, but it is not extractable:
    // the count and the gate follow `isArchiveOnly`, not the selection length.
    fireEvent.click(selectionCheck(moai.key));
    expect(bothDoors()).toEqual({ disabled: [false, false], counts: ["1", "1"] });

    // Ready rows alone: the same zero, and the same shut door, on both.
    fireEvent.click(selectionCheck(moaiZip.key));
    fireEvent.click(selectionCheck(cableClip.key));
    expect(bothDoors()).toEqual({ disabled: [true, true], counts: ["0", "0"] });
  });

  it("gives a viewer neither bulk archive button", () => {
    renderView(makeState({ report: makeReport([moaiZip]) }), false);

    expect(screen.queryByRole("button", { name: /^Extract selected archives/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Extract and create \(/ })).toBeNull();
  });

  it("drives one operation per archive and reports Created 2 projects", async () => {
    installArchiveListings();
    planOperationMock.mockImplementation(async input => extractPlanFor(input.target));
    submitOperationMock.mockImplementation(async input => {
      const target = input.planId.replace("plan-extract:", "");
      return createdOperation(target, target === ALPHA_KEY ? "p-alpha" : "p-beta", target.replace(/\.zip$/, ""));
    });
    const onProjectsChanged = vi.fn();
    const onProjectImported = vi.fn();
    const state = makeState({ report: makeReport([alphaZip, betaZip]) });
    render(
      <BundleReviewView
        {...scanProps()}
        state={state}
        canContribute
        onProjectsChanged={onProjectsChanged}
        onProjectImported={onProjectImported}
      />,
    );

    fireEvent.click(selectionCheck(ALPHA_KEY));
    fireEvent.click(selectionCheck(BETA_KEY));
    fireEvent.click(screen.getByRole("button", { name: "Extract and create (2)" }));
    await waitFor(() => expect(confirmPanel()).toBeTruthy());
    // The detail pane's own panel may have planned for the active row already:
    // what this click drives is what happens from here on.
    const plannedBefore = planOperationMock.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Extract and create 2 projects" }));

    await waitFor(() => expect(bulkNotice()?.textContent).toContain("Created 2 projects."));

    // One plan and one submit per archive, in queue order, each keyed on its own
    // path - the ledger does the work the dead route used to.
    expect(
      planOperationMock.mock.calls
        .slice(plannedBefore)
        .filter(([input]) => input.workflow === "extract-and-create")
        .map(([input]) => input.target),
    ).toEqual([ALPHA_KEY, BETA_KEY]);
    expect(submitOperationMock.mock.calls.map(([input]) => input.planId)).toEqual([
      `plan-extract:${ALPHA_KEY}`,
      `plan-extract:${BETA_KEY}`,
    ]);
    // Two archives is never a landing: there is no single project to land on, so
    // the list is bumped and the operator stays in the queue.
    expect(onProjectImported).not.toHaveBeenCalled();
    expect(onProjectsChanged).toHaveBeenCalledTimes(1);
    // Exactly one whole-library walk at the end; the bulk path never settles or
    // extracts through the hook.
    expect(state.refresh).toHaveBeenCalledTimes(1);
    expect(state.afterExtract).not.toHaveBeenCalled();
    expect(state.waitAfterExtract).not.toHaveBeenCalled();
    expect(state.create).not.toHaveBeenCalled();
  });

  it("lands on the project when the click named exactly one archive", async () => {
    installArchiveListings();
    planOperationMock.mockImplementation(async input => extractPlanFor(input.target));
    submitOperationMock.mockResolvedValue(createdOperation(ALPHA_KEY, "p-alpha", "Alpha"));
    const onProjectsChanged = vi.fn();
    const onProjectImported = vi.fn();
    const state = makeState({ report: makeReport([alphaZip]) });
    render(
      <BundleReviewView
        {...scanProps()}
        state={state}
        canContribute
        onProjectsChanged={onProjectsChanged}
        onProjectImported={onProjectImported}
      />,
    );

    fireEvent.click(selectionCheck(ALPHA_KEY));
    fireEvent.click(screen.getByRole("button", { name: "Extract and create (1)" }));
    await waitFor(() => expect(confirmPanel()).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Extract and create 1 project" }));

    // Single-archive parity: the id is the create step's own project_id.
    await waitFor(() => expect(onProjectImported).toHaveBeenCalledWith("p-alpha"));
    // Landing already bumps the list (`handleProjectImported`), and leaving the
    // queue would throw a walk away, so neither the bump nor the walk happens.
    expect(onProjectsChanged).not.toHaveBeenCalled();
    expect(state.refresh).not.toHaveBeenCalled();
  });

  it("the notice names every archive it acted on", async () => {
    installArchiveListings();
    planOperationMock.mockImplementation(async input => {
      if (input.target === GAMMA_KEY) {
        // The server's own zero-step answer: this archive already has a project,
        // so nothing is submitted for it.
        return makeExtractPlan(GAMMA_KEY, {
          steps: [],
          review: {
            ...makeExtractPlan(GAMMA_KEY).review,
            moves: 0,
            creates: 0,
            existing_project_id: "p-gamma",
            existing_project_name: "Gamma",
            move_list: [],
            projects: [],
          },
        });
      }
      return extractPlanFor(input.target);
    });
    submitOperationMock.mockImplementation(async input => {
      const target = input.planId.replace("plan-extract:", "");
      if (target === BETA_KEY) {
        // The server's own sentence for a run that could not finish.
        return makeOperation({
          id: `op:${target}`,
          target,
          status: "failed",
          error: "the archive was deleted mid-run",
        });
      }
      return createdOperation(target, "p-alpha", "Alpha");
    });
    const state = makeState({ report: makeReport([alphaZip, betaZip, gammaZip]) });
    renderView(state);

    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));
    fireEvent.click(screen.getByRole("button", { name: "Extract and create (3)" }));
    await waitFor(() => expect(confirmPanel()).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Extract and create 3 projects" }));

    await waitFor(() => expect(bulkNotice()?.textContent).toContain("Created 1 project."));

    // ONE container: a summary, then one line per archive, in queue order - not
    // one live region per verdict, which would interrupt itself.
    expect(document.querySelectorAll("[data-bulk-extract-and-create-notice]")).toHaveLength(1);
    const notice = bulkNotice() as HTMLElement;
    expect(notice.getAttribute("role")).toBe("status");
    expect(notice.getAttribute("aria-live")).toBe("polite");
    // The already-existing verdict wrote nothing, which is a notice, not an alarm.
    expect(notice.getAttribute("role")).not.toBe("alert");

    const text = notice.textContent ?? "";
    expect(text).toContain("Alpha - Created project Alpha.");
    expect(text).toContain(
      "Beta - The project could not be created: the archive was deleted mid-run. Nothing was rolled back.",
    );
    expect(text).toContain(
      "Gamma - Already created as project Gamma - this run created no second project.",
    );
    // Gamma never reached the ledger: the zero-step plan is the server's answer.
    expect(submitOperationMock.mock.calls.map(([input]) => input.planId)).toEqual([
      `plan-extract:${ALPHA_KEY}`,
      `plan-extract:${BETA_KEY}`,
    ]);
    expect(state.create).not.toHaveBeenCalled();
    expect(state.waitAfterExtract).not.toHaveBeenCalled();
    expect(state.afterExtract).not.toHaveBeenCalled();
    expect(state.refresh).toHaveBeenCalledTimes(1);
  });

  it("derives the confirm stale when the selection changes under it", async () => {
    installArchiveListings();
    renderView(makeState({ report: makeReport([alphaZip, betaZip]) }));

    fireEvent.click(selectionCheck(ALPHA_KEY));
    const bulk = screen.getByRole("button", {
      name: "Extract and create (1)",
    }) as HTMLButtonElement;
    fireEvent.click(bulk);
    await waitFor(() => expect(confirmPanel()).toBeTruthy());

    expect(document.querySelector("[data-bulk-extract-and-create-stale]")).toBeNull();
    expect(
      (screen.getByRole("button", { name: "Extract and create 1 project" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false);

    // A second archive ticked under the open summary: the words on screen no
    // longer describe what the button would write.
    fireEvent.click(selectionCheck(BETA_KEY));

    expect(document.querySelector("[data-bulk-extract-and-create-stale]")).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Extract and create 1 project" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);

    // Escape is the confirm's keyboard cancel, and nothing has been written.
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(confirmPanel()).toBeNull());
    expect(document.activeElement).toBe(bulk);
    expect(submitOperationMock).not.toHaveBeenCalled();
  });

  it("does not let Escape abandon a run that is already writing", async () => {
    installArchiveListings();
    planOperationMock.mockImplementation(async input => extractPlanFor(input.target));
    // The submit is held open, so the run is mid-flight: the archive may already
    // be being written and the progress line is the only door state on screen.
    let finish: (value: Operation) => void = () => {};
    submitOperationMock.mockReturnValue(
      new Promise<Operation>(resolve => {
        finish = resolve;
      }),
    );
    const onProjectImported = vi.fn();
    const state = makeState({ report: makeReport([alphaZip]) });
    render(
      <BundleReviewView
        {...scanProps()}
        state={state}
        canContribute
        onProjectImported={onProjectImported}
      />,
    );

    fireEvent.click(selectionCheck(ALPHA_KEY));
    fireEvent.click(screen.getByRole("button", { name: "Extract and create (1)" }));
    await waitFor(() => expect(confirmPanel()).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Extract and create 1 project" }));

    await waitFor(() =>
      expect(document.querySelector("[data-bulk-extract-and-create-progress]")).toBeTruthy(),
    );
    // The confirm is gone because the click was answered, not cancelled: there is
    // no Cancel left in the bar to take the write back.
    expect(confirmPanel()).toBeNull();
    const bar = document.querySelector<HTMLElement>("[data-bulk-extract-and-create-bar]");
    expect(bar).toBeTruthy();
    expect(within(bar as HTMLElement).queryByRole("button", { name: "Cancel" })).toBeNull();
    expect(submitOperationMock).toHaveBeenCalledTimes(1);

    // Escape belongs to the confirm and the planning pass; a run that is already
    // writing projects is deliberately not cancellable.
    fireEvent.keyDown(document, { key: "Escape" });
    expect(document.querySelector("[data-bulk-extract-and-create-progress]")).toBeTruthy();
    expect(state.refresh).not.toHaveBeenCalled();

    await act(async () => {
      finish(createdOperation(ALPHA_KEY, "p-alpha", "Alpha"));
    });
    await waitFor(() => expect(bulkNotice()?.textContent).toContain("Created project Alpha."));
    // One archive is a landing: the id goes to the host and the queue is not
    // re-walked, exactly as the one-archive bulk landing pins.
    expect(onProjectImported).toHaveBeenCalledWith("p-alpha");
    expect(state.refresh).not.toHaveBeenCalled();
  });
});

describe("BundleReviewView storage scan on an unresolved row", () => {
  // The row's folder is not registered, so a scan is the one action that can
  // change the verdict. The control lives in the detail pane: the listbox keeps
  // exactly one tab stop and no option gains a focusable control.
  it("offers the scan in the detail pane for an unregistered folder", () => {
    const onScan = vi.fn();
    renderView(makeState({ report: makeReport([legacyVase]) }), true, {
      canScan: true,
      onScan,
    });

    fireEvent.click(rowElement("Legacy_vase"));

    const action = within(detail()).getByRole("button", { name: "Rescan storage folder" });
    expect(detail().querySelector('[data-scan-surface="detail"]')).toBeTruthy();
    expect(within(detail()).getAllByRole("button", { name: "Rescan storage folder" })).toHaveLength(
      1,
    );

    fireEvent.click(action);
    expect(onScan).toHaveBeenCalledTimes(1);
  });

  it("keeps the list row free of controls and the listbox at one tab stop", () => {
    renderView(makeState({ report: makeReport([legacyVase]) }), true, { canScan: true });

    const rows = Array.from(listbox().querySelectorAll("[data-bundle-row]"));
    expect(rows).toHaveLength(1);
    for (const row of rows) {
      expect(row.querySelectorAll("button, input, select, textarea, a[href]")).toHaveLength(0);
    }

    // The one-tab-stop invariant this file pins everywhere: a single roving
    // option carries tabindex 0.
    const options = Array.from(listbox().querySelectorAll("[role='option']"));
    expect(options.filter(option => option.getAttribute("tabindex") === "0")).toHaveLength(1);

    // The action is in the pane, not absent.
    expect(within(detail()).getByRole("button", { name: "Rescan storage folder" })).toBeTruthy();
  });

  it("shows a contributor no scan action, because the scan is WRITE", () => {
    renderView(makeState({ report: makeReport([legacyVase]) }), true, { canScan: false });

    fireEvent.click(rowElement("Legacy_vase"));

    expect(within(detail()).queryByRole("button", { name: "Rescan storage folder" })).toBeNull();
    expect(detail().querySelector('[data-scan-surface="detail"]')).toBeNull();
  });
});

/** A row whose EVERY served reading needs arranging first, so the shape the pane
 *  falls back to is `creatable: false` - the one state the split door exists for.
 *  `activeShape` answers the first offered reading when nothing on offer can be
 *  enacted (`bundleRows`), and that reading is the one a plan is built for. */
const MODEL_SPLIT_NOTE =
  "needs arranging first: one project per model folder; fragments a single product if it was one";
const arrangingRow = makeBundle({
  key: "Container",
  rel_path: "Container",
  title: "Container folder",
  name_suggestion: "Container folder",
  creator: null,
  licence: null,
  licence_url: null,
  source_url: null,
  detector: "generic-bundle",
  confidence: "low",
  metadata_shape: "generic",
  description_excerpt: null,
  matches: [
    {
      shape: "split-top-folders",
      rule: "engine:split-top-folders",
      confidence: "low",
      model_count: 2,
      image_count: 0,
      text_count: 0,
      other_count: 0,
      note: SPLIT_NOTE,
      creatable: false,
    },
    {
      shape: "split-model-folders",
      rule: "engine:split-model-folders",
      confidence: "low",
      model_count: 2,
      image_count: 0,
      text_count: 0,
      other_count: 0,
      note: MODEL_SPLIT_NOTE,
      creatable: false,
    },
  ],
});

/** The Mf-jsw5 payload: the RECOMMENDATION itself is the shape the server cannot
 *  enact, and the enactable reading is offered second. `activeShape` prefers the
 *  first enactable reading while nothing is chosen, so this is the one row where
 *  clicking the recommendation's own radio has to STORE the choice for the split
 *  door to be reachable at all. */
const recommendedRefused = makeBundle({
  key: "Multi",
  rel_path: "Multi",
  title: "Multi folder",
  name_suggestion: "Multi folder",
  creator: null,
  licence: null,
  licence_url: null,
  source_url: null,
  detector: "generic-bundle",
  confidence: "high",
  metadata_shape: "one-project",
  description_excerpt: null,
  matches: [
    {
      shape: "split-top-folders",
      rule: "engine:split-top-folders",
      confidence: "high",
      model_count: 3,
      image_count: 0,
      text_count: 0,
      other_count: 0,
      note: SPLIT_NOTE,
      creatable: false,
    },
    {
      shape: "one-project",
      rule: "generic-bundle",
      confidence: "high",
      model_count: 3,
      image_count: 0,
      text_count: 0,
      other_count: 0,
      note: null,
    },
  ],
});

/** One split the server planned. Both halves of the intent ride ONE payload -
 *  `move_list` is the moves and `projects` the creates - because one action
 *  approves both; the remaining review fields are the wire's own, so a fixture
 *  that skipped one would not be the payload the panel reads. */
function makeOperationReview(overrides: Partial<OperationReview> = {}): OperationReview {
  return {
    workflow: "split-and-create",
    target: "Container",
    moves: 3,
    creates: 3,
    adopts: 0,
    deletes: 0,
    members: 27,
    groups: [],
    cover: null,
    excluded: [{ path: "Container/notes.psd", reason: "does not hold a model" }],
    existing_project_id: null,
    existing_project_name: null,
    move_list: [
      { source: "Container/Alpha", target: "Alpha", kind: "dir", action: "hoist" },
      { source: "Container/Beta", target: "Beta", kind: "dir", action: "hoist" },
      { source: "Container/Gamma", target: "Gamma", kind: "dir", action: "hoist" },
    ],
    projects: [
      { name: "Alpha", root: "Alpha", members: 9 },
      { name: "Beta", root: "Beta", members: 9 },
      { name: "Gamma", root: "Gamma", members: 9 },
    ],
    ...overrides,
  };
}

/** A plan the server issued: `plan_id` is the whole submit argument, so a test
 *  that post-fixed it into a step list would not be proving anything. */
function makeOperationPlan(overrides: Partial<OperationPlan> = {}): OperationPlan {
  return {
    plan_id: "plan-1",
    workflow: "split-and-create",
    target: "Container",
    steps: [{ index: 0, op: "move", summary: "hoist 3 folders" }],
    review: makeOperationReview(),
    ...overrides,
  };
}

/** The plan the extract-and-create door is issued from. The project's name is
 *  derived from the archive path the way the server derives its default, so a
 *  fixture reads as the server's own answer rather than a label a test invented. */
function makeExtractPlan(
  target = "import/Moai.zip",
  overrides: Partial<OperationPlan> = {},
): OperationPlan {
  const name = target.split("/").pop()?.replace(/\.zip$/, "") ?? "Archive";
  return {
    plan_id: `plan-extract:${target}`,
    workflow: "extract-and-create",
    target,
    steps: [
      { index: 0, op: "extract", summary: "extract 2 files" },
      { index: 1, op: "move", summary: "file the archive" },
      { index: 2, op: "scan", summary: "register 2 files" },
      { index: 3, op: "create-project", summary: "create 1 project" },
    ],
    review: {
      workflow: "extract-and-create",
      target,
      moves: 1,
      creates: 1,
      adopts: 0,
      deletes: 0,
      members: 2,
      groups: [{ name: "", members: 2, models: 1 }],
      cover: { asset_id: null, rel_path: "Moai/moai.stl", reason: "first model" },
      excluded: [],
      existing_project_id: null,
      existing_project_name: null,
      move_list: [
        { source: target, target: `${name}/_source/${name}.zip`, kind: "file", action: "file-archive" },
      ],
      projects: [{ name, root: name, members: 2 }],
    },
    ...overrides,
  };
}

/** The same plan, named after the archive, for a bulk run's per-archive answer. */
function extractPlanFor(target: string): OperationPlan {
  return makeExtractPlan(target);
}

function makeOperationStep(overrides: Partial<OperationStep> = {}): OperationStep {
  return {
    index: 0,
    op: "move",
    status: "done",
    result: null,
    error: null,
    started_at: "2026-01-01T00:00:01Z",
    finished_at: "2026-01-01T00:00:02Z",
    ...overrides,
  };
}

/** The runner's finished record, with its undo still available - so no poll is
 *  armed and the undo control is on offer. */
function makeOperation(overrides: Partial<Operation> = {}): Operation {
  return {
    id: "op-7",
    workflow: "split-and-create",
    target: "Container",
    status: "done",
    submitted_by: "u1",
    created_at: "2026-01-01T00:00:00Z",
    started_at: "2026-01-01T00:00:01Z",
    finished_at: "2026-01-01T00:00:09Z",
    error: null,
    undo_state: "available",
    superseded_by: null,
    steps: [
      makeOperationStep({ index: 0, op: "move" }),
      makeOperationStep({ index: 1, op: "create-project", result: { created: true } }),
      makeOperationStep({ index: 2, op: "create-project", result: { created: true } }),
      makeOperationStep({ index: 3, op: "create-project", result: { created: true } }),
    ],
    ...overrides,
  };
}

/** The verdict of an undo: the operation's new state, plus the projects the
 *  undo itself removed. */
function makeUndone(overrides: Partial<OperationUndo> = {}): OperationUndo {
  return {
    operation_id: "op-7",
    undone: true,
    project_id: null,
    project_name: null,
    moves: 3,
    projects_removed: ["Alpha", "Beta", "Gamma"],
    ...overrides,
  };
}

/** The plan as INFORMATION: the moves and the projects the one action would make.
 *  Nothing inside it is a tab stop - the action is. */
function splitPreview(): HTMLElement {
  return within(detail()).getByRole("group", { name: "Split preview" });
}

/** The ONE action, found by its data attribute because its label names the
 *  OUTCOME ("Split into 3 projects") rather than the subsystem. */
function splitActionButton(): HTMLButtonElement {
  const button = detail().querySelector<HTMLButtonElement>("[data-split-action-button]");
  if (!button) throw new Error("no split action");
  return button;
}

function splitUndoButton(): HTMLButtonElement | null {
  return detail().querySelector<HTMLButtonElement>("[data-split-undo]");
}

describe("the split door", () => {
  it("forwards a 401 from the plan read to the shell's sign-out", async () => {
    // Catches a view that mounts the door without the shell's sign-out: the 401
    // would die inside the panel and leave the operator on an expired session,
    // while every other door on this screen signs them out.
    const onUnauthorized = vi.fn();
    planOperationMock.mockRejectedValue(new UnauthorizedError());
    renderView(makeState({ report: makeReport([arrangingRow]) }), true, { onUnauthorized });

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    // The session's news is the shell's; the panel writes no sentence of its own.
    expect(within(detail()).queryByRole("alert")).toBeNull();
  });

  it("plans for a shape the server cannot enact yet, and lists the wire's moves", async () => {
    // Catches a pane that renders nothing for the refused reading: the plan is a
    // READ on mount, and the wire's move array (not a prose rendering of it) is
    // what the operator decides on.
    planOperationMock.mockResolvedValue(makeOperationPlan());
    renderView(makeState({ report: makeReport([arrangingRow]) }));

    await waitFor(() => expect(splitPreview()).toBeTruthy());
    expect(planOperationMock).toHaveBeenCalledWith({
      workflow: "split-and-create",
      target: "Container",
      params: { shape: "split-top-folders" },
    });

    const panel = splitPreview();
    const moves = Array.from(panel.querySelectorAll<HTMLElement>("[data-split-move]"));
    // One list item per WIRE move, each naming its action.
    expect(moves.map(node => node.getAttribute("data-split-action"))).toEqual([
      "hoist",
      "hoist",
      "hoist",
    ]);
    expect(within(moves[0]).getByText('move "Container/Alpha" -> "Alpha"')).toBeTruthy();
    expect(within(moves[2]).getByText('move "Container/Gamma" -> "Gamma"')).toBeTruthy();
    // The projects the same action would create, with the plan's own member count.
    expect(
      Array.from(panel.querySelectorAll("[data-split-project]")).map(node => node.textContent),
    ).toEqual(["Alpha from 9 files", "Beta from 9 files", "Gamma from 9 files"]);
    // The server's refusal of one file, verbatim: the client decided none of this.
    expect(panel.querySelector("[data-split-excluded]")?.textContent).toBe(
      "Container/notes.psd: does not hold a model",
    );
  });

  it("is reachable from a real payload by choosing the refused reading", async () => {
    // The payload the server actually emits: `generic` plus a `creatable: false`
    // `split-top-folders` plus `manual`. Until that reading is chosen the door is
    // not offered - and once it is, the panel must plan for THAT shape.
    planOperationMock.mockResolvedValue(makeOperationPlan({ target: "Multi" }));
    renderView(makeState({ report: makeReport([splitTopFolders]) }));

    // An enactable recommendation means no split is offered, so nothing is read.
    expect(detail().querySelector("[data-split-plan]")).toBeNull();
    expect(planOperationMock).not.toHaveBeenCalled();

    const importAs = within(detail()).getByRole("radiogroup", { name: "Import as" });
    fireEvent.click(
      within(importAs).getByRole("radio", { name: SHAPE_LABELS["split-top-folders"] }),
    );
    expect(
      (within(importAs).getByRole("radio", { checked: true }) as HTMLInputElement).value,
    ).toBe("split-top-folders");

    await waitFor(() => expect(splitPreview()).toBeTruthy());
    expect(planOperationMock).toHaveBeenCalledWith({
      workflow: "split-and-create",
      target: "Multi",
      params: { shape: "split-top-folders" },
    });
  });

  it("keeps a chosen recommended creatable:false shape checked and plans for it", async () => {
    // Dropping the stored choice for a recommendation made `activeShape` fall back
    // to the first enactable shape, so the radio snapped back to `one-project` and
    // the split door could never open (Mf-jsw5). The recommendation here is the
    // refused reading, which is exactly the row the old rule broke.
    planOperationMock.mockResolvedValue(makeOperationPlan({ target: "Multi" }));
    const state = makeState({ report: makeReport([recommendedRefused]) });
    renderView(state);

    // Nothing is planned before the choice: the pane offers the enactable reading.
    expect(detail().querySelector("[data-split-plan]")).toBeNull();
    expect(planOperationMock).not.toHaveBeenCalled();

    const importAs = within(detail()).getByRole("radiogroup", { name: "Import as" });
    fireEvent.click(
      within(importAs).getByRole("radio", { name: SHAPE_LABELS["split-top-folders"] }),
    );

    // (a) The radio stays where the operator put it.
    expect(
      (within(importAs).getByRole("radio", { checked: true }) as HTMLInputElement).value,
    ).toBe("split-top-folders");

    // (b) The door behind it opens, for the chosen shape.
    await waitFor(() => expect(splitPreview()).toBeTruthy());
    expect(planOperationMock).toHaveBeenCalledWith({
      workflow: "split-and-create",
      target: "Multi",
      params: { shape: "split-top-folders" },
    });

    // (c) No create was attempted for a shape the server refuses.
    const create = within(detail()).getByRole("button", {
      name: "Create project",
    }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);
    fireEvent.click(create);
    expect(state.create).not.toHaveBeenCalled();
  });

  it("offers one action labelled with the outcome, not a preview/apply pair", async () => {
    // Catches a door rebuilt as a ladder: the moves are information, so the one
    // thing to press says what it will MAKE.
    planOperationMock.mockResolvedValue(makeOperationPlan());
    renderView(makeState({ report: makeReport([arrangingRow]) }));

    await waitFor(() => expect(splitPreview()).toBeTruthy());
    expect(splitActionButton().textContent).toBe("Split into 3 projects");
    expect(within(detail()).queryByRole("button", { name: "Preview the moves" })).toBeNull();
    expect(within(detail()).queryByRole("button", { name: "Apply this arrangement" })).toBeNull();
  });

  it("submits the served plan id with one idempotency key and narrates the run", async () => {
    // Catches a submit that sends the row key or a second key: the plan id is the
    // whole approval, and one key per intent is what makes a double-click a
    // replay rather than a second split.
    planOperationMock.mockResolvedValue(makeOperationPlan());
    submitOperationMock.mockResolvedValue(makeOperation());
    renderView(makeState({ report: makeReport([arrangingRow]) }));

    await waitFor(() => expect(splitPreview()).toBeTruthy());
    fireEvent.click(splitActionButton());

    await waitFor(() => expect(splitUndoButton()).toBeTruthy());
    const submitted = submitOperationMock.mock.calls[0][0];
    expect(submitted.planId).toBe("plan-1");
    expect(submitted.idempotencyKey).toEqual(expect.any(String));

    // The popup narrates the runner's own steps, in index order.
    const dialog = detail().querySelector<HTMLElement>("[data-split-dialog]");
    if (!dialog) throw new Error("no split dialog");
    expect(dialog.getAttribute("role")).toBe("dialog");
    expect(
      Array.from(dialog.querySelectorAll("[data-split-step]")).map(node => node.textContent),
    ).toEqual([
      "moved the folders",
      "created a project",
      "created a project",
      "created a project",
    ]);
    expect(dialog.querySelector("[data-split-result]")?.textContent).toBe("Done: 3 projects");
    // The consumed plan is gone: it named a tree that has just moved.
    expect(detail().querySelector("[data-split-plan]")).toBeNull();
  });

  it("bumps the project list and the folder tree exactly once when the split lands", async () => {
    // The split door is the single completion signal for its own event: the server
    // rewrites `storage_path` and creates `Folder` rows in that one operation, and
    // a second bump would make these counts two.
    planOperationMock.mockResolvedValue(makeOperationPlan());
    submitOperationMock.mockResolvedValue(makeOperation());
    const onProjectsChanged = vi.fn();
    const onFoldersChanged = vi.fn();
    renderView(makeState({ report: makeReport([arrangingRow]) }), true, {
      onProjectsChanged,
      onFoldersChanged,
    });

    await waitFor(() => expect(splitPreview()).toBeTruthy());
    fireEvent.click(splitActionButton());
    await waitFor(() => expect(splitUndoButton()).toBeTruthy());

    expect(onProjectsChanged).toHaveBeenCalledTimes(1);
    expect(onFoldersChanged).toHaveBeenCalledTimes(1);
  });

  it("offers the undo while the run is undoable, and undoes it by the operation id", async () => {
    // Catches an undo driven by the row key or the plan id: the ledger is found by
    // the operation's own id, and a second split has its own.
    planOperationMock.mockResolvedValue(makeOperationPlan());
    submitOperationMock.mockResolvedValue(makeOperation({ undo_state: "available" }));
    undoOperationMock.mockResolvedValue(makeUndone());
    renderView(makeState({ report: makeReport([arrangingRow]) }));

    await waitFor(() => expect(splitPreview()).toBeTruthy());
    fireEvent.click(splitActionButton());
    await waitFor(() => expect(splitUndoButton()).toBeTruthy());

    fireEvent.click(splitUndoButton() as HTMLButtonElement);

    await waitFor(() => expect(undoOperationMock).toHaveBeenCalledWith("op-7"));
    // The answer IS the row's new state, so the button goes without another read:
    // there is nothing left of this split to fold back a second time.
    await waitFor(() => expect(splitUndoButton()).toBeNull());
  });

  it("offers no undo while the server says the run is not undoable", async () => {
    // `undo_state` is the server's answer, never a guess from the status: a run a
    // later operation superseded must not be offered back.
    planOperationMock.mockResolvedValue(makeOperationPlan());
    submitOperationMock.mockResolvedValue(makeOperation({ undo_state: "unavailable" }));
    renderView(makeState({ report: makeReport([arrangingRow]) }));

    await waitFor(() => expect(splitPreview()).toBeTruthy());
    fireEvent.click(splitActionButton());

    await waitFor(() =>
      expect(detail().querySelector("[data-split-result]")?.textContent).toBe("Done: 3 projects"),
    );
    expect(splitUndoButton()).toBeNull();
  });

  it("offers a viewer none of these controls, and asks for no plan", () => {
    // Catches a door left outside the `canContribute` gate: a READ-only role would
    // start a plan request and then be offered a write.
    renderView(makeState({ report: makeReport([arrangingRow]) }), false);

    expect(detail().querySelector("[data-split-plan]")).toBeNull();
    expect(detail().querySelector("[data-split-action-button]")).toBeNull();
    expect(detail().querySelector("[data-split-dialog]")).toBeNull();
    expect(planOperationMock).not.toHaveBeenCalled();
  });

  it("still sends no shape for an untouched row whose recommendation is enactable", async () => {
    // The other half of the Mf-jsw5 fix: storing a refused recommendation must not
    // start storing the ENACTABLE one. A row nobody chose a shape for keeps the
    // original, byte-identical create body - no `shape` key at all.
    const state = makeState({ report: makeReport([autoSplitTopFolders]) });
    renderView(state);

    const importAs = within(detail()).getByRole("radiogroup", { name: "Import as" });
    expect(
      (within(importAs).getByRole("radio", { checked: true }) as HTMLInputElement).value,
    ).toBe("generic");

    fireEvent.click(within(detail()).getByRole("button", { name: "Create project" }));

    expect(state.create).toHaveBeenCalledTimes(1);
    const items = vi.mocked(state.create).mock.calls[0][0];
    expect(items[0]).toMatchObject({ key: "Multi", auto: true });
    expect(items[0]).not.toHaveProperty("shape");
  });
});
