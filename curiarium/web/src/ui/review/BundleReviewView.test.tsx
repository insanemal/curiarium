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
  createProjectFromExtract: vi.fn(),
}));
vi.mock("../../lib/api", () => ({
  listArchiveEntries: archiveApi.listArchiveEntries,
  extractArchive: archiveApi.extractArchive,
}));
vi.mock("../../lib/api/imports", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api/imports")>()),
  listArchiveEntries: archiveApi.listArchiveEntries,
  extractArchive: archiveApi.extractArchive,
  createProjectFromExtract: archiveApi.createProjectFromExtract,
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

import { extractArchive, listArchiveEntries, type ArchiveExtractResult } from "../../lib/api";
import { createProjectFromExtract, type ArchiveProjectResult } from "../../lib/api/imports";
import {
  ENRICH_MAX_KEYS,
  type BundleDetectItem,
  type BundleDuplicateGroup,
  type BundleReport,
} from "../../lib/api/bundles";
import type { SettleOutcome } from "../../lib/scanSettle";
import { getToasts, resetToasts } from "../../lib/toast";
import Toaster from "../Toaster";
import BundleReviewView from "./BundleReviewView";
import {
  EXTRACT_AND_CREATE_HINT,
  EXTRACT_AND_CREATE_ROW_TITLE,
  NO_PAYLOAD_DOOR_REASON,
  NO_SOURCE_PAGE_REASON,
  bulkEnrichTitle,
  isArchiveOnly,
  needsDecision,
} from "./bundleRows";
import type { BundleDetectState } from "./useBundleDetect";

const listArchiveEntriesMock = vi.mocked(listArchiveEntries);
const extractArchiveMock = vi.mocked(extractArchive);
const createProjectFromExtractMock = vi.mocked(createProjectFromExtract);

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

function makeBundle(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return {
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
    create: vi.fn(async () => {}),
    dismiss: vi.fn(async () => {}),
    enrich: vi.fn(async () => {}),
    notice: null,
    afterExtract: vi.fn(async (): Promise<SettleOutcome> => "settled"),
    ...overrides,
  };
}

function renderView(state: BundleDetectState, canContribute = true): RenderResult {
  return render(<BundleReviewView state={state} canContribute={canContribute} />);
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
  createProjectFromExtractMock.mockReset();
  createProjectFromExtractMock.mockResolvedValue({
    status: "created",
    archive: "import/Moai.zip",
    twin: "import/Moai",
    planned: 2,
    resolved: 2,
    failed: [],
    project: {
      id: "p1",
      name: "Moai zip",
      description: null,
      creator: null,
      source_url: null,
      licence: null,
      licence_url: null,
      origin: "own",
      cover_asset_id: null,
      cover_thumb_url: null,
      asset_count: 2,
      total_size: 2560,
      metadata_json: {},
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
    },
  });
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

  it("warns that a truncated walk may be missing bundles", () => {
    renderView(makeState({ report: makeReport([moai], { truncated: true }) }));

    expect(
      screen.getByText("The walk stopped at the report limit; some bundles may be missing."),
    ).toBeTruthy();
  });

  it("counts stale dismissals in the singular and the plural", () => {
    const { unmount } = render(
      <BundleReviewView
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

    // The confidence span carries `title={confidenceLabel(...)}`, so the label is
    // the stable handle on the right cell.
    const high = screen.getByTitle("High confidence");
    const medium = screen.getByTitle("Medium confidence");
    const low = screen.getByTitle("Low confidence");

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
    const state = makeState({ report: makeReport([moai]) });
    renderView(state);

    const input = screen.getByLabelText("Name for Moai statue") as HTMLInputElement;
    expect(input.value).toBe("Moai statue");

    fireEvent.change(input, { target: { value: "typed" } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    expect(state.create).toHaveBeenCalledTimes(1);
    expect(state.create).toHaveBeenCalledWith([{ key: "Moai", name: "typed" }]);

    vi.mocked(state.create).mockClear();
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    expect(state.create).toHaveBeenCalledWith([{ key: "Moai" }]);
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
    expect(screen.getByRole("button", { name: /^Create selected/ }).textContent).toBe(
      "Create selected (2)",
    );
    // The already-created row is settled, so it is not shown, let alone selected.
    expect(screen.getByText("3 bundles selected")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /^Create selected/ }));

    const items = vi.mocked(state.create).mock.calls[0][0];
    expect(items.map(item => item.key)).toEqual(["Moai", "Cable_clip"]);
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
    const orphan = orphanRow();
    renderView(makeState({ report: makeReport([orphan]) }));

    // The row is in the queue and the report says its key holds files.
    expect(rowKeys()).toContain(orphan.key);
    // No enrichment door of either kind, so no click can reach a refusal.
    expect(detail().querySelector("[data-bundle-enrich]")).toBeNull();
    expect(within(detail()).queryByRole("button", { name: "Enrich from source page" })).toBeNull();
    expect(within(detail()).queryByRole("button", { name: "Re-check source page" })).toBeNull();
    // The reason span speaks the refusal of the very predicate that gated the
    // button - not the old universal paragraph about four gates this row fails none of.
    const reason = detail().querySelector<HTMLElement>("[data-bundle-enrich-reason]");
    expect(reason?.textContent).toBe(NO_PAYLOAD_DOOR_REASON);
    expect(screen.queryByText(DELETED_BULK_PARAGRAPH)).toBeNull();
    // Not a live region, exactly as the other refusal sentence: the queue already
    // speaks through the selection count and the notice.
    expect(reason?.getAttribute("aria-live")).toBeNull();
    expect(reason?.getAttribute("role")).toBeNull();
  });

  it("keeps the create shut and the extract door absent on that same row", () => {
    renderView(makeState({ report: makeReport([orphanRow()]) }));

    // Blocked, so the primary door is the row's honest state: present, disabled.
    const create = within(detail()).getByRole("button", {
      name: "Create project",
    }) as HTMLButtonElement;
    expect(create.disabled).toBe(true);
    // And no extraction in place, no one-step: `extractPathFor` answers null, so
    // neither control renders. The row really has no door for its files.
    expect(detail().querySelector("[data-extract-and-create]")).toBeNull();
    expect(within(detail()).queryByRole("button", { name: /^Extract/ })).toBeNull();
  });

  it("counts nothing in the bulk bar, disables the button, and says why in the row's words", () => {
    const orphan = orphanRow();
    renderView(makeState({ report: makeReport([orphan]) }));

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

  it("says nothing in the bar when the selected rows do not agree on a cause", () => {
    // THE MIS-ATTRIBUTION WITNESS, rendered. `Legacy_vase` names no page, so its own sentence
    // is the page one; the orphan DOES name a page whose payload no door can reach, so its own
    // sentence is the payload one. Each is true of its row and false of the other, so a bar that
    // recites the first one it found in queue order is the paragraph this ticket deleted in
    // miniature. Both queue orders are asked because which sentence such a bar would have spoken
    // is exactly what the order decided - and neither order is saved by the two guards, since
    // neither row is creatable or extractable and only the orphan names a page.
    const noPage = legacyVase;
    const withPage = orphanRow();
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
    const orphan = orphanRow();
    const state = makeState({ report: makeReport([orphan]) });
    renderView(state);

    const dismiss = within(detail()).getByRole("button", {
      name: "Dismiss",
    }) as HTMLButtonElement;
    expect(dismiss.disabled).toBe(false);
    fireEvent.click(dismiss);
    expect(state.dismiss).toHaveBeenCalledWith([orphan.key], true);

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
      return <BundleReviewView state={state} canContribute />;
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
        <BundleReviewView state={makeState({ report: makeReport([alphaZip]) })} canContribute />
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
      <BundleReviewView state={makeState({ report: makeReport([moai, cableClip]) })} canContribute />,
    );

    rowElement("Moai").focus();
    expect(document.activeElement?.getAttribute("data-bundle-row")).toBe("Moai");

    // Programmatic-refetch path: a mouse click on a write button moves focus to
    // that button first, so this is not a claim that a click never steals the
    // operator's focus - it is the case where the row vanishes without one.
    rerender(
      <BundleReviewView
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
      return <BundleReviewView state={state} canContribute />;
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
      return <BundleReviewView state={state} canContribute />;
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
      return <BundleReviewView state={state} canContribute />;
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
      within(panel).getByText("3 models, 5 images, 2 text files, 0 other files - 8 registered assets"),
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
    const state = makeState({ report: makeReport([compound]) });
    renderView(state);

    fireEvent.click(screen.getByRole("checkbox", { name: "Include Gearbox" }));
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    // The name is part of the body as it always was; the exclusion is the only
    // new key, and it carries the group's own name verbatim.
    expect(state.create).toHaveBeenCalledWith([
      { key: "StageTop", name: "StageTop Wave One", exclude_groups: ["Gearbox"] },
    ]);
  });

  it("omits exclude_groups entirely when nothing was unchecked", () => {
    const state = makeState({ report: makeReport([compound]) });
    renderView(state);

    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    const item = vi.mocked(state.create).mock.calls[0][0][0];
    // Absent, not present-and-empty: the server reads the missing field as
    // "include every group", and the old body stays byte-identical.
    expect(item).not.toHaveProperty("exclude_groups");
    expect(item).toEqual({ key: "StageTop", name: "StageTop Wave One" });
  });

  it("renders no group block for an empty list and sends the old body", () => {
    const state = makeState({ report: makeReport([makeBundle({ groups: [] })]) });
    renderView(state);

    expect(screen.queryByRole("group", { name: "Groups in this bundle" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    expect(state.create).toHaveBeenCalledWith([{ key: "Moai", name: "Moai statue" }]);
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
      <BundleReviewView state={makeState({ report: makeReport([row]) })} canContribute />,
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
      <BundleReviewView state={makeState({ report: makeReport([row]) })} canContribute />,
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

describe("BundleReviewView extract and create", () => {
  /** One door verdict, shaped the way `ArchiveProjectOut` answers. The project is
   *  only present on the two verdicts that have one, so a fixture cannot claim a
   *  created project alongside `partial`. */
  function makeVerdict(overrides: Partial<ArchiveProjectResult> = {}): ArchiveProjectResult {
    const status = overrides.status ?? "created";
    const hasProject = status === "created" || status === "already_created";
    return {
      status,
      archive: "import/Moai.zip",
      twin: "import/Moai",
      planned: 2,
      resolved: hasProject ? 2 : 0,
      failed: [],
      project: hasProject
        ? {
            id: "p1",
            name: "Moai zip",
            description: null,
            creator: null,
            source_url: null,
            licence: null,
            licence_url: null,
            origin: "own",
            cover_asset_id: null,
            cover_thumb_url: null,
            asset_count: 2,
            total_size: 2560,
            metadata_json: {},
            created_at: "2026-01-01T00:00:00Z",
            updated_at: "2026-01-01T00:00:00Z",
          }
        : null,
      ...overrides,
    };
  }

  /** The narration, found by the row key it belongs to. Its own attribute, so it
   *  cannot be confused with the button's. */
  function narration(key: string): HTMLElement | null {
    return document.querySelector<HTMLElement>(`[data-extract-and-create-notice="${key}"]`);
  }

  function oneStepButton(): HTMLElement | null {
    return within(detail()).queryByRole("button", { name: "Extract and create project" });
  }

  it("offers one action on a zip-only row, and only there", () => {
    renderView(makeState({ report: makeReport([moaiZip]) }));
    expect(oneStepButton()).toBeTruthy();
    cleanup();

    // A ready row needs no extraction, so the door is not offered.
    renderView(makeState({ report: makeReport([moai]) }));
    expect(oneStepButton()).toBeNull();
    cleanup();

    // A zip-only row that already became a project has nothing left to do.
    const alreadyCreated = makeBundle({
      ...moaiZip,
      existing_project_id: "p1",
      existing_project_name: "Moai zip",
    });
    renderView(makeState({ report: makeReport([alreadyCreated]), showCreated: true }));
    expect(detailTitle()).toBe("Moai zip");
    expect(oneStepButton()).toBeNull();
    cleanup();

    // A viewer never sees a write door.
    renderView(makeState({ report: makeReport([moaiZip]) }), false);
    expect(oneStepButton()).toBeNull();
  });

  it("runs listing, extracting, scanning and creating, then names the project", async () => {
    const state = makeState({ report: makeReport([moaiZip]) });
    const view = renderView(state);

    fireEvent.click(within(detail()).getByRole("button", { name: "Extract and create project" }));

    // The first phase is painted before anything has answered, so the operator
    // is never looking at a silent button.
    expect(narration("import/Moai.zip")?.textContent).toBe("Reading the archive's file list…");

    await waitFor(() =>
      expect(narration("import/Moai.zip")?.textContent).toBe("Created project Moai zip."),
    );

    expect(listArchiveEntriesMock).toHaveBeenCalledWith("import/Moai.zip");
    expect(extractArchiveMock).toHaveBeenCalledWith({ path: "import/Moai.zip", entries: null });
    expect(state.afterExtract).toHaveBeenCalledTimes(1);
    expect(state.afterExtract).toHaveBeenCalledWith([EXTRACT_RESULT]);
    expect(createProjectFromExtractMock).toHaveBeenCalledTimes(1);
    // One archive and the operator's typed name: no bundle key, because the door
    // is keyed by the archive path and asks detection for nothing.
    expect(createProjectFromExtractMock).toHaveBeenCalledWith({ path: "import/Moai.zip", name: "Moai zip" });
    // The bulk doors are untouched by the one-step door: they still go through
    // the hook's void `create`.
    expect(state.create).not.toHaveBeenCalled();
    // A project now exists, so the queue is re-walked: the door writes no bundle
    // report of its own, and the row should stop reading as zip-only.
    await waitFor(() => expect(state.refresh).toHaveBeenCalledTimes(1));

    // The four dependencies are reached in the phase order, not merely at all.
    const calls = [
      listArchiveEntriesMock.mock.invocationCallOrder[0],
      extractArchiveMock.mock.invocationCallOrder[0],
      vi.mocked(state.afterExtract).mock.invocationCallOrder[0],
      createProjectFromExtractMock.mock.invocationCallOrder[0],
    ];
    expect(calls).toEqual([...calls].sort((a, b) => a - b));

    // The create hides its own row (`showCreated` off), so the narration lives
    // in the queue's notices and must survive that hiding: a one-way write
    // leaves a sentence behind even when the row it was about is gone.
    view.rerender(
      <BundleReviewView
        state={makeState({
          report: makeReport([
            moai,
            { ...moaiZip, existing_project_id: "p1", existing_project_name: "Moai zip" },
          ]),
        })}
        canContribute
      />,
    );
    expect(rowKeys()).not.toContain("import/Moai.zip");
    expect(narration("import/Moai.zip")?.textContent).toBe("Created project Moai zip.");
    // Queue level, not aside level: the paragraph must not live in the panel
    // whose row just vanished.
    expect(detail().contains(narration("import/Moai.zip"))).toBe(false);
  });

  it("reports already-created rather than making a second project", async () => {
    createProjectFromExtractMock.mockResolvedValue(makeVerdict({ status: "already_created" }));
    const state = makeState({ report: makeReport([moaiZip]) });
    renderView(state);

    fireEvent.click(within(detail()).getByRole("button", { name: "Extract and create project" }));

    await waitFor(() =>
      expect(narration("import/Moai.zip")?.textContent).toContain("Already created"),
    );
    expect(narration("import/Moai.zip")?.textContent).toContain("no second project");
    expect(state.create).not.toHaveBeenCalled();
  });

  it("still calls the door when the scan refuses to settle, and says what it said", async () => {
    // The settle is a courtesy, not the proof (Mf-0ck5): the door compares the
    // planned entries against registered assets itself, so a walk that never
    // proved itself answers "none of them is registered, look in import/Moai/"
    // instead of the old "the scan did not settle, try later".
    createProjectFromExtractMock.mockResolvedValue(
      makeVerdict({ status: "nothing_resolved", planned: 2, resolved: 0, failed: ["Moai.stl", "readme.txt"] }),
    );
    const state = makeState({
      report: makeReport([moaiZip]),
      afterExtract: vi.fn(async (): Promise<SettleOutcome> => "timeout"),
    });
    renderView(state);

    fireEvent.click(within(detail()).getByRole("button", { name: "Extract and create project" }));

    await waitFor(() =>
      expect(narration("import/Moai.zip")?.textContent).toContain("none of the 2 entries"),
    );
    expect(narration("import/Moai.zip")?.textContent).toContain("import/Moai/");
    expect(createProjectFromExtractMock).toHaveBeenCalledTimes(1);
    expect(state.refresh).not.toHaveBeenCalled();
  });

  it("says what the archive holds while the one-step run is in flight", async () => {
    // The door has no confirm panel of its own, so the listing it read is said
    // aloud here - the same classifier the plain Extract panel renders.
    let settle: (outcome: SettleOutcome) => void = () => {};
    const state = makeState({
      report: makeReport([moaiZip]),
      afterExtract: () => new Promise<SettleOutcome>(resolve => { settle = resolve; }),
    });
    renderView(state);

    fireEvent.click(within(detail()).getByRole("button", { name: "Extract and create project" }));
    await waitFor(() =>
      expect(narration("import/Moai.zip")?.textContent).toContain("Waiting for the library scan"),
    );

    const plan = document.querySelector('[data-extract-and-create-plan="import/Moai.zip"]');
    expect(plan?.textContent).toContain("It holds 1 model, 1 document.");
    expect(plan?.textContent).toContain("The entries sit loose at the top of the new folder.");
    settle("settled");
  });

  it("clears the verdict when the queue is refreshed, as its own sentence asks", async () => {
    createProjectFromExtractMock.mockResolvedValue(
      makeVerdict({ status: "partial", planned: 2, resolved: 1, failed: ["readme.txt"] }),
    );
    const state = makeState({ report: makeReport([moaiZip]) });
    renderView(state);

    fireEvent.click(within(detail()).getByRole("button", { name: "Extract and create project" }));

    await waitFor(() =>
      expect(narration("import/Moai.zip")?.textContent).toContain("1 entry never arrived"),
    );
    // That sentence tells the operator to refresh once the scan finishes, so
    // taking its advice has to clear it.
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(narration("import/Moai.zip")).toBeNull());
    expect(state.refresh).toHaveBeenCalledTimes(1);
  });

  it("creates nothing when the archive held no extractable files", async () => {
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

    fireEvent.click(within(detail()).getByRole("button", { name: "Extract and create project" }));

    await waitFor(() =>
      expect(narration("import/Moai.zip")?.textContent).toContain("Nothing will register"),
    );
    expect(state.afterExtract).not.toHaveBeenCalled();
    expect(createProjectFromExtractMock).not.toHaveBeenCalled();
  });

  it("keeps plain Extract beside the one-step door, and the bulk label byte-identical", () => {
    renderView(makeState({ report: makeReport([moaiZip]) }));

    // Both doors in the aside at once: the one-step button is added, not
    // swapped in for plain Extract, which runs the same extraction and wait.
    expect(within(detail()).getByRole("button", { name: "Extract Moai zip" })).toBeTruthy();
    const oneStep = within(detail()).getByRole("button", { name: "Extract and create project" });
    expect(oneStep.getAttribute("title")).toBe(EXTRACT_AND_CREATE_HINT);

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

  /** A zip-only row that also names a source page, so the detail pane renders
   *  its enrich door beside the one-step door and the gate can be seen on
   *  both. */
  function enrichableZip(): BundleDetectItem {
    return makeBundle({
      ...moaiZip,
      source_url: "https://www.thingiverse.com/thing:2493386",
    });
  }

  it("locks every other mutating door while the one-step run is in flight", async () => {
    let settle: (outcome: SettleOutcome) => void = () => {};
    const afterExtract = vi.fn(
      () =>
        new Promise<SettleOutcome>(resolve => {
          settle = resolve;
        }),
    );
    const state = makeState({
      report: makeReport([moai, enrichableZip()]),
      afterExtract,
    });
    renderView(state);

    // The ready row is selected first, so the bulk create door is live on its
    // own account and disabling it below can only be the run's doing.
    fireEvent.click(selectionCheck("Moai"));
    fireEvent.click(rowElement("import/Moai.zip"));
    expect(detailTitle()).toBe("Moai zip");
    const bulkCreate = screen.getByRole("button", {
      name: "Create selected (1)",
    }) as HTMLButtonElement;
    expect(bulkCreate.disabled).toBe(false);

    fireEvent.click(within(detail()).getByRole("button", { name: "Extract and create project" }));
    await waitFor(() =>
      expect(narration("import/Moai.zip")?.textContent).toContain("Waiting for the library scan"),
    );

    // `busy` is false here: this is the scan wait, not the final create, and
    // the run still owns the queue's writes for the whole of it.
    expect(state.busy).toBe(false);
    expect((oneStepButton() as HTMLButtonElement).disabled).toBe(true);
    expect(
      (within(detail()).getByRole("button", { name: "Dismiss" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (within(detail()).getByRole("button", { name: "Enrich from source page" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(bulkCreate.disabled).toBe(true);
    expect(
      (screen.getByRole("button", { name: /^Dismiss selected/ }) as HTMLButtonElement).disabled,
    ).toBe(true);

    // The ready row is a second detail target, so its own Create project door
    // is gated by the run too: it is not merely disabled by its own row.
    fireEvent.click(rowElement("Moai"));
    expect(detailTitle()).toBe("Moai statue");
    expect(
      (within(detail()).getByRole("button", { name: "Create project" }) as HTMLButtonElement).disabled,
    ).toBe(true);

    settle("settled");
    await waitFor(() =>
      expect(narration("import/Moai.zip")?.textContent).toBe("Created project Moai zip."),
    );

    // The run has answered, so every door it held comes back.
    expect(
      (within(detail()).getByRole("button", { name: "Create project" }) as HTMLButtonElement).disabled,
    ).toBe(false);
    expect(bulkCreate.disabled).toBe(false);
    fireEvent.click(rowElement("import/Moai.zip"));
    expect((oneStepButton() as HTMLButtonElement).disabled).toBe(false);
  });

  it("cannot dismiss the row being processed, so a dismissed row cannot still create", async () => {
    let settle: (outcome: SettleOutcome) => void = () => {};
    const afterExtract = vi.fn(
      () =>
        new Promise<SettleOutcome>(resolve => {
          settle = resolve;
        }),
    );
    const state = makeState({ report: makeReport([moaiZip]), afterExtract });
    renderView(state);

    fireEvent.click(within(detail()).getByRole("button", { name: "Extract and create project" }));
    await waitFor(() =>
      expect(narration("import/Moai.zip")?.textContent).toContain("Waiting for the library scan"),
    );

    // The very row the run is processing: a dismissal written now would hide
    // it while the run's create still lands, so nothing may be written.
    fireEvent.click(within(detail()).getByRole("button", { name: "Dismiss" }));
    expect(state.dismiss).not.toHaveBeenCalled();

    settle("settled");
    await waitFor(() =>
      expect(narration("import/Moai.zip")?.textContent).toBe("Created project Moai zip."),
    );
    expect(createProjectFromExtractMock).toHaveBeenCalledTimes(1);
  });

  it("shuts the plain extract door while the one-step run is in flight", async () => {
    let settle: (outcome: SettleOutcome) => void = () => {};
    const afterExtract = vi.fn(
      () =>
        new Promise<SettleOutcome>(resolve => {
          settle = resolve;
        }),
    );
    const state = makeState({
      report: makeReport([moaiZip]),
      afterExtract,
    });
    renderView(state);

    /** The detail pane's own AT-safe extract door, named for the archive. */
    function detailExtract(): HTMLButtonElement {
      return within(detail()).getByRole("button", {
        name: "Extract Moai zip",
      }) as HTMLButtonElement;
    }

    // Both plain doors are live before the run, so what the assertions below
    // see can only be the run's doing.
    expect(detailExtract().disabled).toBe(false);
    expect(rowElement("import/Moai.zip").getAttribute("aria-keyshortcuts")).toBe("Enter");
    expect(statusCell("import/Moai.zip").getAttribute("title")).toBe(EXTRACT_AND_CREATE_ROW_TITLE);

    fireEvent.click(within(detail()).getByRole("button", { name: "Extract and create project" }));
    await waitFor(() =>
      expect(narration("import/Moai.zip")?.textContent).toContain("Waiting for the library scan"),
    );

    // (a) A second extract landing now would run a second settle and a second
    // refetch, so the detail pane's own extract action refuses at every stage.
    expect(detailExtract().disabled).toBe(true);

    // (b) The row's blocked-reason cell is the mouse's door. It keeps its
    // sentence but loses the door: a click on it opens nothing, and it no
    // longer reads as a control.
    const cell = statusCell("import/Moai.zip");
    expect(cell.getAttribute("title")).toBeNull();
    expect(cell.className).toBe("review-row-status");
    fireEvent.click(cell);
    expect(screen.queryByRole("dialog", { name: /^Extract / })).toBeNull();

    // (c) Enter on the option is that same door from the keyboard: it opens
    // nothing, and the option stops advertising the shortcut it cannot honour.
    fireEvent.keyDown(rowElement("import/Moai.zip"), { key: "Enter" });
    expect(screen.queryByRole("dialog", { name: /^Extract / })).toBeNull();
    expect(rowElement("import/Moai.zip").getAttribute("aria-keyshortcuts")).toBeNull();

    settle("settled");
    await waitFor(() =>
      expect(narration("import/Moai.zip")?.textContent).toBe("Created project Moai zip."),
    );

    // The run has answered, so the door comes back - and it is a door again,
    // not a cell that looks live while refusing.
    expect(detailExtract().disabled).toBe(false);
    expect(rowElement("import/Moai.zip").getAttribute("aria-keyshortcuts")).toBe("Enter");
    expect(statusCell("import/Moai.zip").getAttribute("title")).toBe(EXTRACT_AND_CREATE_ROW_TITLE);
    fireEvent.click(statusCell("import/Moai.zip"));
    expect(screen.getByRole("dialog", { name: "Extract Moai zip" })).toBeTruthy();
  });

  it("shuts the confirm panel it already had open, so no second extract can fire", async () => {
    let settle: (outcome: SettleOutcome) => void = () => {};
    const afterExtract = vi.fn(
      () =>
        new Promise<SettleOutcome>(resolve => {
          settle = resolve;
        }),
    );
    const state = makeState({
      report: makeReport([moaiZip]),
      afterExtract,
    });
    renderView(state);

    // A panel the operator already opened and whose count is on screen: its
    // "Extract 2 files" button is the very second extract the run must refuse,
    // because it would run a second settle and a second refetch.
    fireEvent.click(within(detail()).getByRole("button", { name: "Extract Moai zip" }));
    const confirmButton = (await screen.findByRole("button", {
      name: "Extract 2 files",
    })) as HTMLButtonElement;
    expect(confirmButton.disabled).toBe(false);

    fireEvent.click(within(detail()).getByRole("button", { name: "Extract and create project" }));
    await waitFor(() =>
      expect(narration("import/Moai.zip")?.textContent).toContain("Waiting for the library scan"),
    );

    const extractsBefore = extractArchiveMock.mock.calls.length;
    expect(confirmButton.disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    fireEvent.click(confirmButton);

    expect(extractArchiveMock.mock.calls.length).toBe(extractsBefore);
    expect(afterExtract).toHaveBeenCalledTimes(1);

    settle("settled");
    await waitFor(() =>
      expect(narration("import/Moai.zip")?.textContent).toBe("Created project Moai zip."),
    );
    // The panel is a door again once the run has answered.
    expect(confirmButton.disabled).toBe(false);
  });

  it("still selects the row when the shut blocked-reason cell is clicked", async () => {
    let settle: (outcome: SettleOutcome) => void = () => {};
    const afterExtract = vi.fn(
      () =>
        new Promise<SettleOutcome>(resolve => {
          settle = resolve;
        }),
    );
    renderView(
      makeState({
        report: makeReport([moai, moaiZip]),
        afterExtract,
      }),
    );

    fireEvent.click(rowElement("import/Moai.zip"));
    fireEvent.click(within(detail()).getByRole("button", { name: "Extract and create project" }));
    await waitFor(() =>
      expect(narration("import/Moai.zip")?.textContent).toContain("Waiting for the library scan"),
    );

    fireEvent.click(rowElement("Moai"));
    expect(detailTitle()).toBe("Moai statue");

    // The shut cell opens nothing, and the click still reaches the row: moving
    // the detail target is a read, so the lock must not swallow it.
    fireEvent.click(statusCell("import/Moai.zip"));

    expect(detailTitle()).toBe("Moai zip");
    expect(screen.queryByRole("dialog", { name: /^Extract / })).toBeNull();

    settle("settled");
    await waitFor(() =>
      expect(narration("import/Moai.zip")?.textContent).toBe("Created project Moai zip."),
    );
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
