// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

// `ArchiveExtractAction` is the only module here that reaches the API barrel,
// so the mock carries just its two calls.
vi.mock("../../lib/api", () => ({
  listArchiveEntries: vi.fn(),
  extractArchive: vi.fn(),
}));

import { extractArchive, listArchiveEntries, type ArchiveExtractResult } from "../../lib/api";
import type { BundleDetectItem, BundleReport } from "../../lib/api/bundles";
import type { SettleOutcome } from "../../lib/scanSettle";
import BundleReviewView from "./BundleReviewView";
import { needsDecision } from "./bundleRows";
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
  cover: { asset_id: null, rel_path: "preview.png", reason: "largest-image-unregistered" },
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
  cover: { asset_id: "asset-3", rel_path: "images/vase.jpg", reason: "largest-image" },
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
  cover: { asset_id: null, rel_path: null, reason: "largest-image-unregistered" },
  assets_total: 1,
  status: "blocked",
  blocked_reason: "archive_only",
  archive: { rel_path: "Moai.zip", twin_verified: false },
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
    afterExtract: vi.fn(async (): Promise<SettleOutcome> => "settled"),
    ...overrides,
  };
}

function renderView(state: BundleDetectState, canContribute = true): void {
  render(<BundleReviewView state={state} canContribute={canContribute} />);
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

/** The row's bulk-selection glyph, which is how a selection is made by mouse. */
function selectionCheck(key: string): HTMLElement {
  const glyph = rowElement(key).querySelector<HTMLElement>(".review-check");
  if (!glyph) throw new Error(`no selection glyph for ${key}`);
  return glyph;
}

function detailTitle(): string | null {
  return within(detail()).getByRole("heading").textContent;
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
  listArchiveEntriesMock.mockReset();
  listArchiveEntriesMock.mockResolvedValue({ path: "Moai.zip", entries: ARCHIVE_ENTRIES });
  extractArchiveMock.mockReset();
  extractArchiveMock.mockResolvedValue(EXTRACT_RESULT);
});

afterEach(() => {
  cleanup();
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

  it("settles the scan and reports the extraction to the hook, so the row can turn ready", async () => {
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

    // The hook owns the refetch: a refresh here would race the walk that has to
    // register the extracted files before the row can read `ready`.
    expect(state.refresh).not.toHaveBeenCalled();
    expect(extractArchiveMock).toHaveBeenCalledWith({ path: "Moai.zip", entries: null });
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
