// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// App's own imports from the barrel, as a fixed object: the review queue
// reaches the API through `useBundleDetect`'s domain module instead.
//
// `listProjects` is a hoisted spy rather than `async () => []`: the three
// project-list witnesses below have to control what it answers per call (boot
// seed, then seed plus the created row) or leave it UNRESOLVED, and a fixture
// whose list already contains the new row would make those witnesses green
// without any of the wiring they exist to test.
const { listProjectsSpy } = vi.hoisted(() => ({ listProjectsSpy: vi.fn() }));

vi.mock("../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  FORBIDDEN_MESSAGE: "Your role does not allow this action.",
  apiHealth: vi.fn(async () => ({ ok: true, auth_required: true })),
  fileUrl: vi.fn((rel: string) => rel),
  getApiBase: vi.fn(() => "http://localhost:8000"),
  getAssetCounts: vi.fn(async () => ({ total: 0, unfiled: 0, missing: 0 })),
  getMe: vi.fn(async () => ({
    id: "u1",
    username: "contributor",
    email: null,
    role: "contributor",
    disabled: false,
    kind: "local",
  })),
  listProjects: listProjectsSpy,
  logout: vi.fn(async () => {}),
  refreshToken: vi.fn(),
}));

vi.mock("../lib/api/bundles", () => ({
  // A value, not a type: `BundleReviewView` reads the cap to slice the bulk
  // request, so the mock has to answer it or the render throws.
  ENRICH_MAX_KEYS: 25,
  getBundleReport: vi.fn(),
  createBundles: vi.fn(),
  setBundleDismissed: vi.fn(),
}));

// A viewer's Review entry resolves to the imports queue, so its run read has to
// answer without touching the network.
vi.mock("../lib/api/settings", () => ({
  getImportRuns: vi.fn(async () => ({ runs: [], total: 0, limit: 1, offset: 0 })),
  getImportRun: vi.fn(),
  getLibraryScanStatus: vi.fn(),
  startLibraryScan: vi.fn(),
}));

vi.mock("../lib/settings", () => ({
  loadSettings: () => ({
    slicer: { enabled: false, selected: "orca" },
    engraving: { enabled: false, selected: "lightburn" },
    theme: { selected: "light" },
    makerworld: { cookie: "" },
    thingiverse: { cookie: "" },
    imports: { rememberCookies: false },
    network: { publicUrl: "" },
    previews: { mode: "automatic" },
  }),
  saveSettings: () => {},
  resolveTheme: () => "light",
}));

// The review entry point lives in the header now, so each rail only has to
// mount the pane the mode assertions look for. App's props are accepted and
// ignored: nothing in this file asserts the rails' contents.
vi.mock("./Sidebar", async () => {
  const react = await import("react");
  return { default: () => react.createElement("aside", { "aria-label": "Files" }) };
});

// The stateful shape `App.mode.test.tsx` uses, for the reason the brief names:
// a stub that prints a constant cannot show a NEW row arriving, which is the
// whole assertion here. It keeps the `"Project view"` text every other test in
// this file leans on, and it counts its own mounts so "no reload" is measured.
// The read-outs are `data-testid` paragraphs rather than roles because App keeps
// an unvisited pane `hidden`, and a `hidden` subtree is invisible to `getByRole`.
vi.mock("./ProjectView", async () => {
  const react = await import("react");
  function ProjectViewStandIn({
    projects,
    selectedProjectId,
  }: {
    projects?: Array<{ id: string; name: string; asset_count: number; cover_thumb_url: string | null }> | null;
    selectedProjectId?: string | null;
  }) {
    const rows = projects ?? [];
    return react.createElement(
      "div",
      null,
      react.createElement("p", null, "Project view"),
      react.createElement(
        "p",
        { "data-testid": "project-ids" },
        rows.map(project => project.id).join(","),
      ),
      react.createElement(
        "p",
        { "data-testid": "project-names" },
        rows.map(project => project.name).join(","),
      ),
      react.createElement(
        "p",
        { "data-testid": "project-fields" },
        rows
          .map(
            project =>
              `${project.id}/${project.asset_count}/${project.cover_thumb_url ?? "no-cover"}`,
          )
          .join("|"),
      ),
      react.createElement("p", { "data-testid": "open-project" }, selectedProjectId ?? "none"),
    );
  }
  return { default: ProjectViewStandIn };
});

// The files-mode pane is a stub here, so the picker's append is exercised through
// a button that calls the callback App hands the grid. The real grid→picker
// threading is pinned in `AssetGrid.project.test.tsx` and the real picker in
// `ProjectPickerDialog.test.tsx`: three links, each tested through its own real
// implementation, none of them stubbed for a second test.
const pickerCreatedRow = vi.hoisted(() => ({
  id: "p-from-picker",
  name: "Picked by hand",
}));

vi.mock("./asset-grid/AssetGrid", async () => {
  const react = await import("react");
  return {
    default: ({ onProjectCreated }: { onProjectCreated?: (project: unknown) => void }) =>
      react.createElement(
        "div",
        null,
        "Asset grid",
        react.createElement(
          "button",
          {
            type: "button",
            "data-testid": "simulate-picker-append",
            onClick: () => onProjectCreated?.({ ...pickerCreatedRow }),
          },
          "Report a project the picker created",
        ),
      ),
  };
});
vi.mock("./settings/Settings", async () => {
  const react = await import("react");
  return { default: () => react.createElement("div", null, "Settings view") };
});
vi.mock("./AuditLog", () => ({ default: () => null }));
vi.mock("./UploadBar", () => ({ default: () => null }));
vi.mock("./UploadQueuePanel", () => ({ default: () => null }));
vi.mock("./Login", () => ({ default: () => null }));

import App from "./App";
import { getMe } from "../lib/api";
import type { Project } from "../lib/api/projects";
import {
  createBundles,
  getBundleReport,
  setBundleDismissed,
  type BundleCreateItemResult,
  type BundleCreateResponse,
  type BundleDetectItem,
  type BundleReport,
} from "../lib/api/bundles";
import { storeToken } from "../lib/auth";
import { MODE_STORAGE_KEY, type LibraryMode } from "../lib/mode";
import { BACK_TO_MODE_LABEL } from "./views";

const getBundleReportMock = vi.mocked(getBundleReport);
const createBundlesMock = vi.mocked(createBundles);
const setBundleDismissedMock = vi.mocked(setBundleDismissed);

/** jsdom may resolve `window.localStorage` to undefined; install a real one. */
function installMemoryStorage(): void {
  const store = new Map<string, string>();
  const storage: Storage = {
    get length(): number {
      return store.size;
    },
    clear(): void {
      store.clear();
    },
    getItem(key: string): string | null {
      return store.has(key) ? store.get(key)! : null;
    },
    key(index: number): string | null {
      return Array.from(store.keys())[index] ?? null;
    },
    removeItem(key: string): void {
      store.delete(key);
    },
    setItem(key: string, value: string): void {
      store.set(key, value);
    },
  };
  Object.defineProperty(window, "localStorage", { value: storage, configurable: true });
}

function makeBundle(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return {
    key: "Moai",
    rel_path: "Moai",
    title: "Moai statue",
    name_suggestion: "Moai statue",
    creator: "sculpteo",
    licence: "CC BY",
    licence_url: null,
    source_url: null,
    detector: "thingiverse-readme",
    confidence: "high",
    metadata_shape: "root-docs",
    description_excerpt: null,
    model_count: 1,
    image_count: 1,
    text_count: 0,
    other_count: 0,
    cover: {
      asset_id: null,
      rel_path: null,
      reason: "largest-image-unregistered",
      url: null,
      thumb_url: null,
    },
    assets_total: 2,
    status: "ready",
    blocked_reason: null,
    archive: null,
    existing_project_id: null,
    existing_project_name: null,
    dismissed: false,
    ...overrides,
  };
}

function makeReport(bundles: BundleDetectItem[]): BundleReport {
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
  };
}

/** One bundle awaiting a decision, one dismissed: the badge must say 1. */
const FIRST_REPORT = makeReport([
  makeBundle(),
  makeBundle({ key: "Old", title: "Old bundle", dismissed: true }),
]);

/** The same walk after Moai became a project: nothing awaits a decision. */
const SECOND_REPORT = makeReport([
  // A name alone was the lie this ticket removes: the wire names the id and
  // derives the name, never a friendly string with no identity behind it.
  makeBundle({
    existing_project_id: "3f2a6c1e-8f4b-4c7d-9a1e-2b5c8d0f4a61",
    existing_project_name: "Moai statue",
  }),
  makeBundle({ key: "Old", title: "Old bundle", dismissed: true }),
]);

/** The same walk after Moai was dismissed: nothing awaits a decision either. */
const DISMISSED_REPORT = makeReport([
  makeBundle({ dismissed: true }),
  makeBundle({ key: "Old", title: "Old bundle", dismissed: true }),
]);

/** The header's Review entry exists only once `/me` answers, so this waits for the role.
 *
 *  `initialProjects` is load-bearing for the project-list witnesses: passing a boot
 *  seed is what makes App's list effect refuse to refetch until something BUMPS
 *  `assetsVersion` past the seed's stamp. Render App once and never remount it -
 *  a remount would show the new row without any of the wiring.
 */
async function renderApp(boot: AppBoot = {}): Promise<void> {
  render(<App initialMode={boot.initialMode} initialProjects={boot.initialProjects} />);
  await screen.findByRole("button", { name: "Review" });
}

type AppBoot = { initialMode?: LibraryMode; initialProjects?: Project[] | null };

function makeProject(id: string, name: string): Project {
  return {
    id,
    name,
    description: null,
    creator: null,
    source_url: null,
    licence: null,
    licence_url: null,
    origin: "own",
    cover_asset_id: null,
    cover_thumb_url: null,
    asset_count: 3,
    total_size: 4096,
    metadata_json: {},
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

/** What the projects pane can actually see, read out of the stand-in. */
function projectViewReadout(): {
  ids: string[];
  names: string[];
  fields: string[];
  open: string;
} {
  const text = (testId: string): string =>
    document.querySelector(`[data-testid="${testId}"]`)?.textContent ?? "";
  const ids = text("project-ids");
  const names = text("project-names");
  const fields = text("project-fields");
  return {
    ids: ids ? ids.split(",") : [],
    names: names ? names.split(",") : [],
    fields: fields ? fields.split("|") : [],
    open: text("open-project"),
  };
}

/** The files pane's stand-in button that calls the append callback App threads
 *  to the grid. Found by attribute, because `getByRole` skips a pane App is
 *  keeping `hidden` - and the append has to work from either mode. */
function clickPickerAppend(): void {
  const node = document.querySelector('[data-testid="simulate-picker-append"]');
  if (!node) throw new Error("no simulated picker append");
  fireEvent.click(node);
}

/** One `POST /bundles/create` answer, shaped the way the wire answers: counters
 *  plus one result per key, with the `project_id` the server sends. */
function createResponseFor(items: Array<{ key: string }>): BundleCreateResponse {
  const results: BundleCreateItemResult[] = items.map(item => ({
    key: item.key,
    status: "created",
    project_id: `project-${item.key.toLowerCase()}`,
    name: "Moai statue",
    assets_added: 2,
    skipped_assets: 0,
    failed_assets: [],
    reason: null,
  }));
  return { created: results.length, skipped: 0, blocked: 0, failed: 0, results };
}

/** Tick the row at `index` and press the bulk door. */
function createSelectedRow(index = 0): void {
  fireEvent.keyDown(screen.getAllByRole("option")[index], { key: " " });
  fireEvent.click(screen.getByRole("button", { name: /^Create selected/ }));
}

function openQueueButton(): HTMLElement {
  return screen.getByRole("button", { name: "Review" });
}

function badgeText(): string {
  // The tab's text also carries its label, so read only the count span.
  return screen.getByTestId("review-count-bundles").querySelector(".sidebar-count")?.textContent ?? "";
}

/** Resolves after real timers have had a chance to fire anything they hold. */
function settle(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

beforeEach(() => {
  installMemoryStorage();
  storeToken("access-token");
  listProjectsSpy.mockReset();
  listProjectsSpy.mockResolvedValue([]);
  getBundleReportMock.mockReset();
  getBundleReportMock.mockResolvedValue(FIRST_REPORT);
  createBundlesMock.mockReset();
  createBundlesMock.mockResolvedValue({
    created: 1,
    skipped: 0,
    blocked: 0,
    failed: 0,
    results: [],
  });
  setBundleDismissedMock.mockReset();
  setBundleDismissedMock.mockResolvedValue({ dismissed: ["Moai"], stale_dismissals: [] });
});

afterEach(() => {
  cleanup();
});

describe("App review destination", () => {
  it("does not walk the library before Review is entered", async () => {
    await renderApp();
    await settle(30);

    expect(getBundleReportMock).not.toHaveBeenCalled();
    // The count belongs to the queue tab inside Review, so the library has no
    // badge to paint until the destination is entered.
    expect(screen.queryAllByTestId("review-count-bundles")).toHaveLength(0);
  });

  it("opens the queue with exactly one full-report fetch and paints the badge from it", async () => {
    await renderApp();

    fireEvent.click(openQueueButton());

    expect(await screen.findByRole("heading", { name: "Bundles to confirm" })).toBeTruthy();
    expect(screen.getAllByRole("tablist")).toHaveLength(1);
    await screen.findByRole("listbox", { name: "Bundles to confirm" });
    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
    expect(getBundleReportMock).toHaveBeenCalledWith({ includeDismissed: true, limit: 1000 });
    // Undismissed and not already a project: the dismissed row does not count.
    await waitFor(() => expect(badgeText()).toBe("1"));
  });

  it("does not poll while the operator sits in the queue", async () => {
    await renderApp();
    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });
    expect(getBundleReportMock).toHaveBeenCalledTimes(1);

    await settle(60);

    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
  });

  it("refetches once after a create and updates the badge from the new report", async () => {
    getBundleReportMock
      .mockResolvedValueOnce(FIRST_REPORT)
      .mockResolvedValueOnce(SECOND_REPORT);
    await renderApp();
    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });
    await waitFor(() => expect(badgeText()).toBe("1"));

    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    await waitFor(() => expect(createBundlesMock).toHaveBeenCalledTimes(1));
    expect(createBundlesMock.mock.calls[0][0]).toEqual([{ key: "Moai", name: "Moai statue" }]);
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(badgeText()).toBe("0"));
    // Both remaining rows are settled - one created, one dismissed - and the
    // queue has nothing pending, so it says which kinds are hidden rather than
    // claiming either one alone. No listbox, no created row.
    expect(
      await screen.findByText("Every detected bundle is dismissed or already has a project."),
    ).toBeTruthy();
    expect(screen.queryAllByRole("option")).toHaveLength(0);

    fireEvent.click(screen.getByRole("checkbox", { name: "Show created" }));

    expect(await screen.findByText("Already created as project Moai statue")).toBeTruthy();
  });

  it("refetches once after a dismiss and updates the badge from the new report", async () => {
    getBundleReportMock
      .mockResolvedValueOnce(FIRST_REPORT)
      .mockResolvedValueOnce(DISMISSED_REPORT);
    await renderApp();
    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });
    await waitFor(() => expect(badgeText()).toBe("1"));

    // Space is the row glyph's keyboard equivalent: it selects for the bulk bar.
    fireEvent.keyDown(screen.getAllByRole("option")[0], { key: " " });
    fireEvent.click(screen.getByRole("button", { name: /^Dismiss selected/ }));

    await waitFor(() => expect(setBundleDismissedMock).toHaveBeenCalledTimes(1));
    expect(setBundleDismissedMock).toHaveBeenCalledWith(["Moai"], true);
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(badgeText()).toBe("0"));
  });

  it("gives a viewer no bundle tab and never walks the library", async () => {
    vi.mocked(getMe).mockResolvedValueOnce({
      id: "u1",
      username: "viewer",
      email: null,
      role: "viewer",
      disabled: false,
      kind: "local",
    });
    await renderApp();

    // The stored queue is still `bundles` - App's default - and the viewer has
    // no bundle tab, so `resolveQueue` redirects the entry to the first queue
    // they may see. Entering Review must therefore never arm the bundle walk.
    fireEvent.click(openQueueButton());

    await waitFor(() => expect(screen.getByRole("tabpanel").id).toBe("review-tabpanel-imports"));
    expect(screen.queryByTestId("review-count-bundles")).toBeNull();
    // the imports tab controls the panel that actually rendered.
    expect(
      screen.getByRole("tab", { name: /^Imports running/ }).getAttribute("aria-controls"),
    ).toBe("review-tabpanel-imports");
    expect(screen.queryByRole("listbox", { name: "Bundles to confirm" })).toBeNull();
    expect(getBundleReportMock).not.toHaveBeenCalled();
  });

  it("walks the library exactly once under StrictMode", async () => {
    render(
      <React.StrictMode>
        <App />
      </React.StrictMode>,
    );
    await screen.findByRole("button", { name: "Review" });

    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });

    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
  });

  it("refetches once on re-entry, not on every render", async () => {
    await renderApp();
    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });
    expect(getBundleReportMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Back to files" }));
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Bundles to confirm" })).toBeNull(),
    );

    fireEvent.click(openQueueButton());
    await screen.findByRole("heading", { name: "Bundles to confirm" });
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(2));
  });

  it("keeps the queue open across a Files/Projects switch", async () => {
    await renderApp();
    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });

    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));

    expect(window.localStorage.getItem(MODE_STORAGE_KEY)).toBe("projects");
    expect(screen.getByRole("heading", { name: "Bundles to confirm" })).toBeTruthy();
    // the header is rendered once, outside both panes: the queue keeps one row.
    expect(screen.getAllByRole("tablist")).toHaveLength(1);
    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
  });

  it("returns to the library from the queue", async () => {
    await renderApp();
    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });

    fireEvent.click(screen.getByRole("button", { name: "Back to files" }));

    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Bundles to confirm" })).toBeNull(),
    );
    expect(screen.getByText("Asset grid")).toBeTruthy();
    expect(screen.queryAllByRole("tablist")).toHaveLength(0);
  });

  it("leaving the Review destination lands on the header's Review entry, not <body>", async () => {
    await renderApp();
    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });

    // A queue tab holds focus inside the destination, the way a keyboard user
    // arrives here with the arrow keys.
    screen.getByRole("tab", { name: /^Bundles/ }).focus();

    // "Back to files" lives inside the destination, so the click unmounts the
    // very control that was clicked: without App's effect focus falls to
    // <body> and a keyboard user restarts at the top of the document.
    fireEvent.click(screen.getByRole("button", { name: "Back to files" }));

    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Review" }));
  });

  it("does not steal focus from a header control that stays mounted", async () => {
    await renderApp();
    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });

    const settings = screen.getByRole("button", { name: "Settings" });
    // jsdom's fireEvent.click does not move focus the way a browser does, so
    // the keyboard state is set up explicitly rather than implied by the click.
    settings.focus();
    fireEvent.click(settings);

    expect(await screen.findByText("Settings view")).toBeTruthy();
    // The button is still mounted and still holds focus; a refocus that ran on
    // every departure from Review would yank focus out of the operator's hands.
    expect(document.activeElement).toBe(settings);
  });
});

describe("App project list hand-off", () => {
  // The owner's report this block exists for: "when I import a project from the
  // Review screen, then click Back to library ... the Projects list doesn't have
  // the added stuff. I have to refresh the whole thing."
  //
  // Two fixtures make these witnesses fail when the wiring is removed rather than
  // when the mood changes:
  //
  // - The boot list is passed as `initialProjects`, which stamps `projectsAsOf` at
  //   the version the seed is valid for. App's list effect then REFUSES to fetch
  //   until something bumps `assetsVersion` past it, so a created row can only
  //   arrive through a notification the create actually sent.
  // - `listProjects` is a spy whose answer is set per test. A mock that already
  //   contained the new row would make every assertion below green with the
  //   threading deleted, which is the hazard this idiom exists to avoid.
  const seed = () => makeProject("p-seed", "Seed project");
  const created = () => makeProject("project-moai", "Moai statue");
  const made = (id: string, name: string) => makeProject(id, name);

  /** Open the queue with the projects pane already visited once.
   *
   *  Visiting it first is what makes the count of list requests mean something:
   *  with the pane mounted and the boot seed still current, a mode switch fetches
   *  nothing, so the ONLY thing left that can produce a request is a create.
   */
  async function openQueueWithProjectsVisited(): Promise<void> {
    await renderApp({ initialMode: "files", initialProjects: [seed()] });
    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));
    expect(await screen.findByText("Project view")).toBeTruthy();
    expect(listProjectsSpy).not.toHaveBeenCalled();
    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });
  }

  it("a project created from the queue reaches the Projects list without a reload", async () => {
    // T1 - the BULK bundle door, `POST /bundles/create`, the surface both the
    // brief and the ticket missed: `useBundleDetect.create` used to type itself
    // `Promise<void>` and throw the response away, so nothing could report from
    // it and nobody noticed.
    //
    // It is also partly a LANDING test, which is honest to say up front: a
    // single-row click lands, so deleting App's `setActiveView("library")` reds
    // this test too (the pane is never mounted while the queue stays open). The
    // bulk case - bump without landing - is pinned by the next test but one.
    // The list answers WITH the new row - and that is exactly the fixture that
    // would be dangerous if it could be reached without a request. It cannot:
    // the boot seed came in as a prop and stamped `projectsAsOf`, so until a
    // create bumps `assetsVersion` the effect refuses to call this mock at all
    // and the pane keeps showing the seed alone. Delete the threading and the
    // call count below is 0, not 1, and the card never appears.
    listProjectsSpy.mockResolvedValue([seed(), created()]);
    await openQueueWithProjectsVisited();
    createBundlesMock.mockResolvedValue(createResponseFor([{ key: "Moai" }]));
    // jsdom will not let a test redefine `location.reload`, so the instrument is
    // the document itself: a real reload replaces every node in it.
    const mainBefore = document.querySelector("main");
    const bodyBefore = document.body;
    expect(mainBefore).not.toBeNull();

    createSelectedRow();

    await waitFor(() => expect(createBundlesMock).toHaveBeenCalledTimes(1));
    // One notification, therefore exactly one list request - and no arguments:
    // it is the plain `GET /projects` the effect already issues.
    await waitFor(() => expect(listProjectsSpy).toHaveBeenCalledTimes(1));
    expect(listProjectsSpy.mock.calls[0]).toEqual([]);
    await waitFor(() => expect(projectViewReadout().ids).toContain("project-moai"));
    expect(projectViewReadout().names).toContain("Moai statue");
    // The row is the server's, with the fields only the server can compute.
    expect(projectViewReadout().fields).toContain("project-moai/3/no-cover");
    // "Without a reload", measured rather than assumed. The owner's workaround was
    // a full page reload, and a reload tears the document down: every node is
    // replaced and the boot sequence runs again. The same `<main>` element across
    // the click, the same document, and `location.reload` never touched is the
    // difference between being told and being claimed.
    expect(document.querySelector("main")).toBe(mainBefore);
    expect(document.body).toBe(bodyBefore);
    expect(screen.getByText("Project view")).toBeTruthy();
  });

  it("lands on the project the queue created, and leaves the queue", async () => {
    // T3 - the landing. See the assertion below for what was measured when
    // `setActiveView("library")` is deleted: the FIRST red is the queue-heading
    // assertion, and the pane read-outs go red with it (App renders the panes only
    // under `activeView === "library"`, so with the queue still open there is no
    // pane to read at all). The heading is what names the cause.
    listProjectsSpy.mockResolvedValue([seed(), created()]);
    await openQueueWithProjectsVisited();
    createBundlesMock.mockResolvedValue(createResponseFor([{ key: "Moai" }]));

    createSelectedRow();
    await waitFor(() => expect(createBundlesMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(listProjectsSpy).toHaveBeenCalledTimes(1));

    // THE discriminator, and it comes first on purpose. `handleModeChange` ends
    // with `setActiveView(view => view === "review" ? view : "library")`, so
    // reusing `handleProjectImported` verbatim from the queue records the project
    // (mode set, `openProjectId` set, list bumped) and keeps the queue on screen.
    // Measured, not asserted: with that one line deleted this is the first
    // assertion to fail - `expected <h1 class="review-queue-title"> to be null` -
    // and the pane read-outs below fail too, because App renders them only under
    // `activeView === "library"`. So the heading is the assertion that says WHY.
    // (Trim it and the test still dies, later and for the wrong reason.)
    expect(screen.queryByRole("heading", { name: "Bundles to confirm" })).toBeNull();
    expect(screen.queryByRole("listbox", { name: "Bundles to confirm" })).toBeNull();

    // Then the three facts the queue-kept state would also satisfy, so this test
    // says the whole gesture and not just the departure.
    expect(projectViewReadout().open).toBe("project-moai");
    expect(screen.getByRole("radio", { name: "Projects" }).getAttribute("aria-checked")).toBe(
      "true",
    );
    expect(projectViewReadout().ids).toContain("project-moai");
  });

  it("a five-row create from the queue reaches the Projects list too, without landing", async () => {
    // The OTHER half of the queue's notification, and the half that the three
    // single-row tests cannot reach: a multi-row create bumps `assetsVersion`
    // through App's `onProjectsChanged` instead of landing through
    // `onProjectImported`. Delete that one prop from App's `<BundleReviewView>`
    // and every other test in this file stays green while the owner's bulk
    // gesture - create five, go back to a Projects list that never heard of them -
    // comes straight back.
    listProjectsSpy.mockResolvedValue([seed(), created(), made("project-cable", "Cable clip")]);
    await openQueueWithProjectsVisited();
    createBundlesMock.mockResolvedValue(
      createResponseFor([{ key: "Moai" }, { key: "Cable_clip" }]),
    );

    // Both creatable rows, by the bulk bar's own select-all.
    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));
    fireEvent.click(screen.getByRole("button", { name: /^Create selected/ }));

    await waitFor(() => expect(createBundlesMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(listProjectsSpy).toHaveBeenCalledTimes(1));
    expect(listProjectsSpy.mock.calls[0]).toEqual([]);

    // The owner's next click, because a bump lands nowhere on its own: back out of
    // the queue and into the projects pane. Without the prop above the request
    // count stays 0 here and the list keeps the boot seed - the report this ticket
    // was written from.
    fireEvent.click(screen.getByRole("button", { name: "Back to projects" }));
    await waitFor(() => expect(projectViewReadout().ids).toContain("project-cable"));
    // Bumped, not landed: two projects were made and neither is "the" one.
    expect(projectViewReadout().open).toBe("none");
    expect(screen.getByRole("radio", { name: "Projects" }).getAttribute("aria-checked")).toBe(
      "true",
    );
  });

  it("appends the row the project picker reported, with no list request to take it from", async () => {
    // T2 (the App link; the grid link is pinned in `AssetGrid.project.test.tsx`
    // and the picker link in `ProjectPickerDialog.test.tsx`). The picker already
    // notified App through `onChanged` -> `onProjectsChanged`, so what is new is
    // the append beside it - and with the list request left UNRESOLVED, a refetch
    // cannot be where the card came from.
    listProjectsSpy.mockReturnValue(new Promise<Project[]>(() => {}));
    await renderApp({ initialMode: "files", initialProjects: [seed()] });

    clickPickerAppend();
    // The pane is mounted only once the mode was visited, and the append has to
    // survive the visit: the row was in App's state before the pane ever existed.
    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));

    await waitFor(() => expect(projectViewReadout().ids).toContain("p-from-picker"));
    expect(listProjectsSpy).not.toHaveBeenCalled();

    // One card for one server row: App's handler ignores a row whose id it already
    // holds, because append-plus-append is the only way to list one project twice.
    clickPickerAppend();
    expect(projectViewReadout().ids.filter(id => id === "p-from-picker")).toHaveLength(1);
    expect(projectViewReadout().ids).toEqual(["p-seed", "p-from-picker"]);
  });

  it("names the back control after the mode it returns to, and never says library", async () => {
    await renderApp({ initialMode: "files" });
    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });

    expect(screen.getByRole("button", { name: "Back to files" }).textContent).toBe(
      "Back to files",
    );
    // The word is gone from the header control, in both modes.
    expect(screen.queryByRole("button", { name: "Back to library" })).toBeNull();
    expect(screen.queryByText("Back to library")).toBeNull();

    // The control names the axis it actually leaves: switching mode renames it,
    // because the label reads `mode`, not a fixed string.
    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));
    expect(screen.getByRole("button", { name: "Back to projects" }).textContent).toBe(
      "Back to projects",
    );
    expect(screen.queryByRole("button", { name: "Back to files" })).toBeNull();
  });

  it("pins the back-control table against the mode list, so no mode arrives unnamed", () => {
    // `Record<LibraryMode, string>` is the load-bearing half (tsc refuses a missing
    // mode); this is the runtime belt for a build that widens `LibraryMode` with a
    // cast. It restates the two modes because `lib/mode.ts` keeps its own list
    // private - the pair is asserted against each other, not copied as prose.
    expect(Object.keys(BACK_TO_MODE_LABEL).sort()).toEqual(["files", "projects"]);
    for (const [mode, label] of Object.entries(BACK_TO_MODE_LABEL)) {
      expect(label.startsWith("Back to ")).toBe(true);
      expect(label.toLowerCase()).toContain(mode);
      expect(label.toLowerCase()).not.toContain("library");
    }
  });
});
