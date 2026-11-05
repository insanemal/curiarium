// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within, type RenderResult } from "@testing-library/react";

// `ArchiveExtractAction` is the only module here that reaches the API barrel,
// so the mock carries just its two calls.
vi.mock("../../lib/api", () => ({
  listArchiveEntries: vi.fn(),
  extractArchive: vi.fn(),
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
  ENRICH_NONE_REASON,
  NO_SOURCE_PAGE_REASON,
  bulkEnrichTitle,
  needsDecision,
} from "./bundleRows";
import type { BundleDetectState } from "./useBundleDetect";

const listArchiveEntriesMock = vi.mocked(listArchiveEntries);
const extractArchiveMock = vi.mocked(extractArchive);

const ARCHIVE_ENTRIES = [
  { path: "Moai.stl", size: 2048 },
  { path: "readme.txt", size: 512 },
];

const EXTRACT_RESULT: ArchiveExtractResult = {
  path: "Moai.zip",
  destination: "Moai",
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
/** A zip-only bundle: nothing but the archive is on disk to register. */
const moaiZip = makeBundle({
  key: "Moai_zip",
  rel_path: "Moai_zip",
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
  archive: { rel_path: "Moai.zip", twin_verified: false },
});

/** Two zip-only rows, the same shape, so a row change swaps `path` and `label`
 *  without changing which children render: the extract action stays the same
 *  instance unless something keys it. */
const ALPHA_KEY = "Alpha_zip";
const BETA_KEY = "Beta_zip";
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
  archive: { rel_path: "Alpha.zip", twin_verified: false },
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
  archive: { rel_path: "Beta.zip", twin_verified: false },
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
    expect(rowKeys()).toEqual(["Moai", "Cable_clip", "Legacy_vase", "Moai_zip"]);
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

    expect(rowKeys()).toEqual(["Moai", "Cable_clip", "Legacy_vase", "Moai_zip"]);
    expect(screen.getByRole("button", { name: "All" }).getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: "Ready" }));
    expect(rowKeys()).toEqual(["Moai", "Cable_clip"]);
    expect(screen.getByRole("button", { name: "Ready" }).getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: "Blocked" }));
    expect(rowKeys()).toEqual(["Legacy_vase", "Moai_zip"]);

    fireEvent.click(screen.getByRole("button", { name: "Low confidence" }));
    expect(rowKeys()).toEqual(["Cable_clip", "Moai_zip"]);

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

    // Blocked rows (Moai_zip) and already-created ones are not creatable, so
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

  it("offers archive extraction only for a zip-only bundle that has an archive", () => {
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
    await waitFor(() => expect(listArchiveEntriesMock).toHaveBeenCalledWith("Moai.zip"));
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
    expect(extractArchiveMock).toHaveBeenCalledWith({ path: "Moai.zip", entries: null });
  });

  it("promises no scan and reports nothing when the extract wrote no files", async () => {
    // The server's junk-only early return: no bytes, no scan, and
    // `already_present: false` because no destination existed to be already there.
    extractArchiveMock.mockResolvedValue({
      path: "Moai.zip",
      destination: "Moai",
      files: 0,
      bytes: 0,
      already_present: false,
      scan_started: false,
    });
    const state = makeState({ report: makeReport([moaiZip]) });
    renderView(state);

    fireEvent.click(within(detail()).getByRole("button", { name: "Extract Moai zip" }));
    await waitFor(() => expect(listArchiveEntriesMock).toHaveBeenCalledWith("Moai.zip"));
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

    expect(rowKeys()).toEqual(["Moai", "Moai_zip"]);
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

    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));

    const none = screen.getByRole("button", {
      name: "Enrich selected (0 of 3)",
    }) as HTMLButtonElement;
    expect(none.disabled).toBe(true);
    expect(none.getAttribute("title")).toBe(ENRICH_NONE_REASON);

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

    // Moai_zip is the active row: it has no page, so the reason stands and the
    // door does not.
    expect(detail().querySelector("[data-bundle-enrich-reason]")?.textContent).toBe(
      NO_SOURCE_PAGE_REASON,
    );
    expect(within(detail()).queryByRole("button", { name: "Enrich from source page" })).toBeNull();

    fireEvent.keyDown(rowElement("Moai_zip"), { key: "ArrowDown" });

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

describe("BundleReviewView archive extraction", () => {
  it("opens the extract flow from an archive-only row's blocked reason", () => {
    renderView(makeState());

    // The detail target before the click: a click that reaches the row would
    // move it to Moai_zip, so this is what proves `stopPropagation` works.
    const before = detailTitle();
    expect(before).toBe("Moai statue");

    fireEvent.click(statusCell("Moai_zip"));

    expect(screen.getByRole("dialog", { name: "Extract Moai zip" })).toBeTruthy();
    expect(detailTitle()).toBe(before);
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
    fireEvent.keyDown(rowElement("Moai_zip"), { key: "Enter" });

    expect(screen.getByRole("dialog", { name: "Extract Moai zip" })).toBeTruthy();
    // Advertised without a tab stop: the option says Enter, and nothing else does.
    expect(rowElement("Moai_zip").getAttribute("aria-keyshortcuts")).toBe("Enter");
    expect(rowElement("Moai").getAttribute("aria-keyshortcuts")).toBeNull();
  });

  it("gives a viewer no affordance on the blocked reason", () => {
    renderView(makeState(), false);

    fireEvent.click(rowElement("Moai_zip"));
    expect(detailTitle()).toBe("Moai zip");

    const cell = statusCell("Moai_zip");
    expect(cell.getAttribute("title")).toBeNull();
    fireEvent.click(cell);

    expect(screen.queryByRole("dialog", { name: /^Extract / })).toBeNull();
    expect(rowElement("Moai_zip").getAttribute("aria-keyshortcuts")).toBeNull();
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
    await waitFor(() => expect(listArchiveEntriesMock).toHaveBeenCalledWith("Moai.zip"));
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
    await waitFor(() => expect(listArchiveEntriesMock).toHaveBeenCalledWith("Moai.zip"));
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
    fireEvent.click(selectionCheck("Moai_zip"));
    const one = screen.getByRole("button", {
      name: "Extract selected archives (1)",
    }) as HTMLButtonElement;
    expect(one.disabled).toBe(false);
    fireEvent.click(one);
    expect(screen.getByRole("dialog", { name: "Extract Moai zip" })).toBeTruthy();
    cleanup();

    // Mixed: two rows are selected, but only the zip-only one is extractable.
    renderView(makeState());
    fireEvent.click(selectionCheck("Moai_zip"));
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

    fireEvent.click(rowElement("Moai_zip"));
    fireEvent.click(statusCell("Moai_zip"));
    expect(screen.getByRole("dialog", { name: "Extract Moai zip" })).toBeTruthy();

    // The precondition: the dialog put the caret on a real control of its own.
    // Without this the Escape below could be firing anywhere.
    const close = within(screen.getByRole("dialog", { name: "Extract Moai zip" }))
      .getByRole("button", { name: "Close" });
    expect(document.activeElement).toBe(close);

    fireEvent.keyDown(close, { key: "Escape" });

    expect(screen.queryByRole("dialog", { name: "Extract Moai zip" })).toBeNull();
    expect(document.activeElement).toBe(rowElement("Moai_zip"));
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
