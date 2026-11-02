// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// App's own imports from the barrel, as a fixed object. `lib/uploadTree` and
// `lib/uploadQueue` reach `createFolder`/`uploadAsset` *through this barrel*, so
// the folder-scan upload path is only exercised if the mocks below carry them.
vi.mock("../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  FORBIDDEN_MESSAGE: "Your role does not allow this action.",
  apiHealth: vi.fn(async () => ({ ok: true, auth_required: true })),
  createFolder: vi.fn(),
  fileUrl: vi.fn((rel: string) => rel),
  getApiBase: vi.fn(() => "http://localhost:8000"),
  getAssetCounts: vi.fn(async () => ({ total: 0, unfiled: 0, missing: 0 })),
  getMe: vi.fn(async () => ({
    id: "u1",
    username: "editor",
    email: null,
    role: "editor",
    disabled: false,
    kind: "local",
  })),
  listProjects: vi.fn(async () => []),
  logout: vi.fn(async () => {}),
  refreshToken: vi.fn(),
  uploadAsset: vi.fn(),
}));

// An editor's Review entry resolves to the bundle queue, so `useBundleDetect`
// arms on entry. The report is empty on purpose: this file asserts imports
// reads only, and no queue assertion may shift because a neighbour fetched.
vi.mock("../lib/api/bundles", () => ({
  getBundleReport: vi.fn(async () => ({
    root: "",
    truncated: false,
    counts: {
      total: 0,
      high: 0,
      low: 0,
      ready: 0,
      blocked: 0,
      dismissed: 0,
      already_created: 0,
    },
    bundles: [],
    stale_dismissals: [],
  })),
  createBundles: vi.fn(),
  setBundleDismissed: vi.fn(),
}));

// Domain modules: the real hooks import from these, not the barrel.
vi.mock("../lib/api/settings", () => ({
  getImportRuns: vi.fn(),
  getImportRun: vi.fn(),
  // App arms `useLibraryScan`; its pair comes from this domain module.
  getLibraryScanStatus: vi.fn(),
  startLibraryScan: vi.fn(),
}));

// `useFolderScan` imports `createFolder` from the domain module (the root folder
// it creates before uploading); the barrel's copy above creates nested ones.
vi.mock("../lib/api/folders", () => ({
  createFolder: vi.fn(),
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

// The review entry point lives in the header, so each rail only has to mount the
// pane the mode assertions look for. Every prop App passes is accepted and
// ignored; the folder version stays an observable, because it is what the two
// change signals a folder-scan upload fires are read through.
vi.mock("./Sidebar", async () => {
  const react = await import("react");
  return {
    default: ({ foldersVersion }: { foldersVersion?: number }) =>
      react.createElement("aside", {
        "aria-label": "Files",
        "data-testid": "files-sidebar",
        "data-folders-version": String(foldersVersion ?? 0),
      }),
  };
});

vi.mock("./ProjectView", async () => {
  const react = await import("react");
  return { default: () => react.createElement("div", null, "Project view") };
});

// `assetsVersion` is App's "assets changed" generation; the upload must bump it.
vi.mock("./asset-grid/AssetGrid", async () => {
  const react = await import("react");
  return {
    default: ({ assetsVersion }: { assetsVersion?: number }) =>
      react.createElement(
        "div",
        { "data-testid": "asset-grid", "data-assets-version": String(assetsVersion ?? 0) },
        "Asset grid",
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
import { createFolder, getMe, uploadAsset } from "../lib/api";
import { createFolder as createRootFolder } from "../lib/api/folders";
import {
  getImportRun,
  getImportRuns,
  type ImportRunDetail,
  type ImportRunList,
  type ImportRunSummary,
} from "../lib/api/settings";
import { storeToken } from "../lib/auth";
import { MODE_STORAGE_KEY } from "../lib/mode";
import { describeQueueCount } from "./review/useReviewCounts";

const getImportRunsMock = vi.mocked(getImportRuns);
const getImportRunMock = vi.mocked(getImportRun);
const createFolderMock = vi.mocked(createFolder);
const createRootFolderMock = vi.mocked(createRootFolder);
const uploadAssetMock = vi.mocked(uploadAsset);
const getMeMock = vi.mocked(getMe);

function makeSummary(overrides: Partial<ImportRunSummary> = {}): ImportRunSummary {
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
    ...overrides,
  };
}

function makeDetail(overrides: Partial<ImportRunDetail> = {}): ImportRunDetail {
  return { ...makeSummary(), errors: [], ...overrides };
}

function makeList(runs: ImportRunSummary[]): ImportRunList {
  return { runs, total: runs.length, limit: 1, offset: 0 };
}

const running = makeDetail({
  status: "running",
  finished_at: null,
  registered: 2,
  known: 1,
  ignored: 0,
  failed: 0,
  discovered: 4,
  skipped: 1,
});
const finished = makeDetail();

/** A promise whose settlement the test drives, for ordering two responses. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

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

function openReviewButton(): HTMLElement {
  return screen.getByRole("button", { name: "Review" });
}

function importsTab(): HTMLElement {
  return screen.getByRole("tab", { name: /^Imports running/ });
}

function badgeText(): string {
  // The tab's text also carries its label, so read only the count span.
  const tab = screen.getByTestId("review-count-imports");
  return tab.querySelector(".sidebar-count")?.textContent ?? "";
}

function headerCount(): HTMLElement {
  const element = document.querySelector(".review-queue-count");
  if (!element) throw new Error("No queue count in the header");
  return element as HTMLElement;
}

/** The Review entry exists only once `/me` answers, so this waits for the role. */
async function renderApp(): Promise<void> {
  render(<App />);
  await screen.findByRole("button", { name: "Review" });
}

/**
 * Opening a queue is two steps now: the header's Review entry, which lands on
 * the resolved queue (bundles for this file's editor), then the tab for the
 * queue under test. A viewer's entry already resolves to imports, so clicking
 * its own tab is a no-op re-selection rather than a second fetch.
 */
async function openImportsQueue(): Promise<void> {
  fireEvent.click(openReviewButton());
  fireEvent.click(await screen.findByRole("tab", { name: /^Imports running/ }));
  await screen.findByRole("heading", { name: "Imports running" });
}

/** Resolves after real timers have had a chance to fire anything they hold. */
function settle(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

beforeEach(() => {
  installMemoryStorage();
  storeToken("access-token");
  getMeMock.mockReset();
  getMeMock.mockResolvedValue({
    id: "u1",
    username: "editor",
    email: null,
    role: "editor",
    disabled: false,
    kind: "local",
  });
  getImportRunsMock.mockReset();
  getImportRunsMock.mockResolvedValue(makeList([makeSummary({ status: "running", finished_at: null })]));
  getImportRunMock.mockReset();
  getImportRunMock.mockResolvedValue(running);
  createFolderMock.mockReset();
  createFolderMock.mockResolvedValue({ id: "folder-nested", name: "nested", tags: [] });
  createRootFolderMock.mockReset();
  createRootFolderMock.mockResolvedValue({ id: "folder-workshop", name: "Workshop", tags: [] });
  uploadAssetMock.mockReset();
  uploadAssetMock.mockResolvedValue({ id: "asset-1" });
});

afterEach(() => {
  cleanup();
});

describe("App imports destination (AC-7)", () => {
  it("shows one imports tab in one tablist, whichever mode is showing", async () => {
    await renderApp();
    await openImportsQueue();

    // One entry point now, not one per rail: the tab row is rendered inside the
    // destination, so there is exactly one tablist and exactly one tab.
    expect(screen.getAllByRole("tablist")).toHaveLength(1);
    expect(screen.getAllByRole("tab", { name: /^Imports running/ })).toHaveLength(1);

    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));

    // The header is outside both panes, so the switch cannot duplicate the row
    // or move the selection off the queue that is open.
    expect(screen.getAllByRole("tablist")).toHaveLength(1);
    expect(screen.getAllByRole("tab", { name: /^Imports running/ })).toHaveLength(1);
    expect(importsTab().getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("heading", { name: "Imports running" })).toBeTruthy();
  });

  it("opens the queue with exactly one entry fetch and keeps it open across a mode switch", async () => {
    await renderApp();
    await openImportsQueue();

    expect(getImportRunsMock).toHaveBeenCalledTimes(1);
    expect(getImportRunsMock).toHaveBeenCalledWith(1, 0);
    expect(getImportRunMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));

    expect(window.localStorage.getItem(MODE_STORAGE_KEY)).toBe("projects");
    expect(screen.getByRole("heading", { name: "Imports running" })).toBeTruthy();
    // The mode switch must not re-enter the queue's hook.
    expect(getImportRunsMock).toHaveBeenCalledTimes(1);
  });

  it("does not fetch before the queue is opened", async () => {
    await renderApp();
    await settle(30);

    // App load reads no run history. Entering Review would first land on the
    // bundle queue - that is why this file mocks the bundle report at all - so
    // nothing here has armed the imports queue yet.
    expect(getImportRunsMock).not.toHaveBeenCalled();
    expect(getImportRunMock).not.toHaveBeenCalled();
  });
});

describe("App imports badge (AC-8)", () => {
  it("dashes an unfetched count, then says one while running and zero once the run finished", async () => {
    await renderApp();

    // The counts live inside Review, so before entry there is no imports tab to
    // carry one - and no badge anywhere else claiming a zero.
    expect(screen.queryAllByTestId("review-count-imports")).toHaveLength(0);

    // Entering Review lands on the bundle queue; the imports tab is rendered but
    // has never fetched, so its count is unknown: a dash, never a zero.
    fireEvent.click(openReviewButton());
    const tab = await screen.findByRole("tab", { name: /^Imports running/ });
    expect(badgeText()).toBe("\u2013");
    expect(tab.getAttribute("title")).toBe("Count unavailable");
    expect(tab.getAttribute("aria-label")).toBe("Imports running: Count unavailable");

    fireEvent.click(tab);
    await screen.findByRole("heading", { name: "Imports running" });
    await waitFor(() => expect(badgeText()).toBe("1"));

    // The run finishes; the operator's refresh is the next honest read.
    getImportRunsMock.mockResolvedValue(makeList([makeSummary()]));
    getImportRunMock.mockResolvedValue(finished);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(badgeText()).toBe("0"));
  });
});

describe("App imports coordinator (AC-9)", () => {
  it("words the imports tab and the queue header from the same count on one fetch", async () => {
    await renderApp();
    await openImportsQueue();

    await waitFor(() => expect(badgeText()).toBe("1"));
    expect(headerCount().textContent).toBe("1");
    // One wording function feeds both surfaces, so they cannot describe the
    // same number two ways.
    expect(headerCount().getAttribute("title")).toBe(describeQueueCount("imports", 1));
    expect(importsTab().getAttribute("title")).toBe(describeQueueCount("imports", 1));
    expect(getImportRunsMock).toHaveBeenCalledTimes(1);
  });

  it("fetches the entry pair exactly once under StrictMode", async () => {
    render(
      <React.StrictMode>
        <App />
      </React.StrictMode>,
    );
    await screen.findByRole("button", { name: "Review" });

    await openImportsQueue();

    expect(getImportRunsMock).toHaveBeenCalledTimes(1);
    expect(getImportRunMock).toHaveBeenCalledTimes(1);
  });

  it("never lets a superseded response paint over a newer one", async () => {
    const older = deferred<ImportRunDetail>();
    const newer = deferred<ImportRunDetail>();
    getImportRunMock.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);

    await renderApp();
    await openImportsQueue();
    // The entry detail is still on the wire; a manual refresh overtakes it.
    await waitFor(() => expect(getImportRunMock).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(getImportRunMock).toHaveBeenCalledTimes(2));

    await act(async () => {
      newer.resolve(finished);
    });
    await waitFor(() => expect(badgeText()).toBe("0"));

    // The entry response lands last and carries the stale running run.
    await act(async () => {
      older.resolve(running);
    });

    expect(badgeText()).toBe("0");
    expect(screen.getByText("Succeeded")).toBeTruthy();
  });
});

describe("App imports folder scan (AC-4)", () => {
  it("creates the top-level folder, uploads, fires both change signals and returns to files", async () => {
    // Start in projects mode: the return to files mode must be the upload's
    // doing, not the initial state.
    window.localStorage.setItem(MODE_STORAGE_KEY, "projects");
    await renderApp();
    await openImportsQueue();

    const input = document.querySelector('input[type="file"]') as HTMLInputElement | null;
    if (!input) throw new Error("No folder-scan file input");
    const file = new File(["stl-bytes"], "part.stl", { type: "model/stl" });
    Object.defineProperty(file, "webkitRelativePath", { value: "Workshop/part.stl" });

    fireEvent.change(input, { target: { files: [file] } });

    // The folder name becomes a top-level folder before the files land in it.
    await waitFor(() =>
      expect(createRootFolderMock).toHaveBeenCalledWith("Workshop", [], undefined),
    );
    await waitFor(() => expect(uploadAssetMock).toHaveBeenCalledTimes(1));
    expect(uploadAssetMock.mock.calls[0][0]).toBe(file);
    expect(uploadAssetMock.mock.calls[0][1]).toEqual({ folder_id: "folder-workshop" });

    // Both change signals: the assets generation and the folder generation.
    await waitFor(() =>
      expect(screen.getByTestId("files-sidebar").dataset.foldersVersion).toBe("1"),
    );
    expect((await screen.findByTestId("asset-grid")).dataset.assetsVersion).toBe("1");

    // ...and the upload hands the new folder back to App, which shows files mode.
    await waitFor(() => expect(window.localStorage.getItem(MODE_STORAGE_KEY)).toBe("files"));
    expect(screen.getByRole("complementary", { name: "Files" })).toBeTruthy();
    // The upload returned to files mode, so the projects pane is hidden.
    expect(screen.getByText("Project view").closest("[hidden]")).toBeTruthy();
  });
});

describe("App imports for a viewer", () => {
  it("reads the run state, gets no folder panel and never writes", async () => {
    getMeMock.mockResolvedValue({
      id: "u1",
      username: "viewer",
      email: null,
      role: "viewer",
      disabled: false,
      kind: "local",
    });

    await renderApp();
    await openImportsQueue();

    await waitFor(() => expect(badgeText()).toBe("1"));
    expect(screen.getByText("Import in progress")).toBeTruthy();
    expect(screen.queryByText("Local folder scan")).toBeNull();

    await settle(30);

    expect(createRootFolderMock).not.toHaveBeenCalled();
    expect(uploadAssetMock).not.toHaveBeenCalled();
  });
});
