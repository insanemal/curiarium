// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

// The queue renders the REAL AssetGrid pinned to `missingOnly`, so this barrel
// mock has to carry every name that grid's import graph reaches (the same
// factory AssetGrid.test.tsx uses) plus App's own imports: health, counts,
// `/me`, projects and the session calls. A fixed object is what keeps the
// assertions below reading a mock instead of the network.
vi.mock("../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  FORBIDDEN_MESSAGE: "Your role does not allow this action.",
  addProjectAssets: vi.fn(),
  apiHealth: vi.fn(async () => ({ ok: true, auth_required: true })),
  assetFilePreviewUrl: vi.fn((rel: string) => `${rel}?inline=1`),
  batchDeleteAssets: vi.fn(),
  batchMoveAssets: vi.fn(),
  batchSetMeta: vi.fn(),
  batchSetTags: vi.fn(),
  createFolder: vi.fn(),
  createProject: vi.fn(),
  deleteAsset: vi.fn(),
  deleteAssetFile: vi.fn(),
  deletePreparedPrint: vi.fn(),
  downloadAsset: vi.fn(),
  downloadZip: vi.fn(),
  fileUrl: vi.fn((rel: string) => rel),
  getApiBase: vi.fn(() => "http://localhost:8000"),
  getAssetCounts: vi.fn(),
  getAssetDeletePreview: vi.fn(),
  getLibraryScanStatus: vi.fn(),
  getMe: vi.fn(),
  listAssetFileLimits: vi.fn(async () => ({
    count: 0,
    total_bytes: 0,
    max_count: 0,
    max_file_bytes: 0,
    max_total_bytes: 0,
  })),
  listAssetFiles: vi.fn(async () => []),
  listAssets: vi.fn(),
  listCreators: vi.fn(),
  listFolders: vi.fn(),
  listProjectAssets: vi.fn(),
  listProjects: vi.fn(),
  listTags: vi.fn(),
  logout: vi.fn(async () => {}),
  refreshToken: vi.fn(),
  renameAssetFile: vi.fn(),
  reorderAssetFiles: vi.fn(async (_assetId: string, fileIds: string[]) => fileIds),
  setTags: vi.fn(),
  startLibraryScan: vi.fn(),
  updateAssetFolder: vi.fn(),
  updateAssetMeta: vi.fn(),
  uploadAsset: vi.fn(),
  uploadAssetFile: vi.fn(),
}));

// An editor's Review entry resolves to the bundle queue, so `useBundleDetect`
// arms on entry. The report is empty on purpose: this file asserts the missing
// queue only, and no assertion may shift because a neighbour fetched.
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

// The queue's hook imports its pair from the DOMAIN module, while the card's
// Re-scan reaches the same routes through the barrel above. Both are stubbed:
// they are two mock instances for one pair of endpoints.
vi.mock("../lib/api/settings", () => ({
  getImportRuns: vi.fn(async () => ({ runs: [], total: 0, limit: 1, offset: 0 })),
  getImportRun: vi.fn(),
  getLibraryScanStatus: vi.fn(),
  startLibraryScan: vi.fn(),
}));

// `loadSettings`/`saveSettings`/`resolveTheme` are the only seams this file
// needs; the real grid also imports `slicerLabelFor`/`engraverLabelFor`, so the
// actual module is spread rather than replaced.
vi.mock("../lib/settings", async importOriginal => {
  const actual = await importOriginal<typeof import("../lib/settings")>();
  return {
    ...actual,
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
    resolveTheme: () => "light" as const,
  };
});

// The review entry point lives in the header, so each rail only has to mount the
// pane the mode assertions look for. Every prop App passes is accepted and
// ignored: nothing in this file asserts the rails' contents.
vi.mock("./Sidebar", async () => {
  const react = await import("react");
  return { default: () => react.createElement("aside", { "aria-label": "Files" }) };
});

vi.mock("./ProjectSidebar", async () => {
  const react = await import("react");
  return { default: () => react.createElement("aside", { "aria-label": "Projects" }) };
});

vi.mock("./ProjectView", async () => {
  const react = await import("react");
  return { default: () => react.createElement("div", null, "Project view") };
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
import {
  deleteAsset,
  getAssetCounts,
  getLibraryScanStatus,
  getMe,
  listAssets,
  listCreators,
  listFolders,
  listTags,
  startLibraryScan,
  type Asset,
  type Me,
} from "../lib/api";
import {
  getLibraryScanStatus as readQueueScanStatus,
  startLibraryScan as startQueueScan,
  type LibraryScanStatus,
} from "../lib/api/settings";
import { storeToken, type Role } from "../lib/auth";
import { MODE_STORAGE_KEY } from "../lib/mode";
import { describeQueueCount } from "./review/useReviewCounts";

const getAssetCountsMock = vi.mocked(getAssetCounts);
const getMeMock = vi.mocked(getMe);
const listAssetsMock = vi.mocked(listAssets);
const listTagsMock = vi.mocked(listTags);
const listCreatorsMock = vi.mocked(listCreators);
const listFoldersMock = vi.mocked(listFolders);
const deleteAssetMock = vi.mocked(deleteAsset);
// The queue's own status read (domain module)...
const getQueueStatusMock = vi.mocked(readQueueScanStatus);
const startQueueScanMock = vi.mocked(startQueueScan);
// ...and the card's Re-scan pair (barrel).
const getCardScanStatusMock = vi.mocked(getLibraryScanStatus);
const startCardScanMock = vi.mocked(startLibraryScan);

/** One flagged row: the file is gone but the library entry still points at it. */
const missingAsset: Asset = {
  id: "asset-missing",
  name: "Lost bracket",
  filename: "lost-bracket.stl",
  mime: "model/stl",
  size: 2048,
  tags: [],
  url: "/api/assets/asset-missing/file",
  thumb_url: null,
  supporting_file_count: 0,
  missing_since: "2026-10-27T11:31:34+00:00",
};

function makeStatus(overrides: Partial<LibraryScanStatus> = {}): LibraryScanStatus {
  return {
    running: false,
    started_at: 1_700_000_000,
    finished_at: 1_700_000_010,
    registered: 3,
    known: 1,
    ignored: 0,
    missing: 1,
    failed: 0,
    errors: [],
    ...overrides,
  };
}

function me(role: Role): Me {
  return { id: "u1", username: role, email: null, role, disabled: false, kind: "local" };
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

/** The Review entry exists only once `/me` answers, so this waits for the role. */
async function renderApp(): Promise<void> {
  render(<App />);
  await screen.findByRole("button", { name: "Review" });
}

function openReviewButton(): HTMLElement {
  return screen.getByRole("button", { name: "Review" });
}

function missingTab(): HTMLElement {
  return screen.getByRole("tab", { name: /^Missing files/ });
}

/**
 * Opening the missing queue is two steps now: the header's Review entry, which
 * lands on the resolved queue (bundles for an editor, imports for a viewer),
 * then the Missing files tab.
 */
async function openQueue(): Promise<void> {
  fireEvent.click(openReviewButton());
  fireEvent.click(await screen.findByRole("tab", { name: /^Missing files/ }));
  await screen.findByRole("heading", { name: "Missing files" });
}

/** The queue's entry status read lands after the heading, so wait for it. */
async function queueReady(): Promise<void> {
  await waitFor(() => expect(getQueueStatusMock).toHaveBeenCalledTimes(1));
}

function badge(): HTMLElement {
  // The tab carries the wording and the count; `title` lives on the tab itself.
  return screen.getByTestId("review-count-missing");
}

function badgeText(): string {
  // The tab's text also carries its label, so read only the count span.
  return badge().querySelector(".sidebar-count")?.textContent ?? "";
}

function headerCount(): HTMLElement {
  const element = document.querySelector(".review-queue-count");
  if (!element) throw new Error("No queue count in the header");
  return element as HTMLElement;
}

/** `listAssets` calls that were issued with the missing pin. */
function pinnedCalls() {
  return listAssetsMock.mock.calls.filter(([params]) => params?.missing === true);
}

/** The argument of the last pinned `listAssets` call. */
function lastPinnedParams() {
  const calls = pinnedCalls();
  return calls[calls.length - 1]?.[0];
}

function cardFor(assetId: string): HTMLElement {
  const card = document.querySelector<HTMLElement>(`[data-asset-id="${assetId}"]`);
  if (!card) throw new Error(`card ${assetId} is not rendered`);
  return card;
}

async function findCard(assetId: string): Promise<HTMLElement> {
  return waitFor(() => cardFor(assetId));
}

/**
 * Opens a card's kebab and returns the menu. The menu is portalled to
 * `document.body`, so it is never inside the card and must be queried at
 * screen scope.
 */
function openCardMenu(card: HTMLElement): HTMLElement {
  fireEvent.click(within(card).getByRole("button", { name: "More actions" }));
  return screen.getByRole("menu");
}

/** Resolves after real timers have had a chance to fire anything they hold. */
function settle(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/** Flushes pending promise resolutions (and React work) without touching timers. */
async function flush(): Promise<void> {
  await act(async () => {
    for (let tick = 0; tick < 12; tick += 1) await Promise.resolve();
  });
}

beforeEach(() => {
  installMemoryStorage();
  storeToken("access-token");

  getMeMock.mockReset();
  getMeMock.mockResolvedValue(me("editor"));

  // The whole-library counts: one read on app load, refetched only when a
  // change signal bumps `assetsVersion`/`folderVersion`/`countsVersion`.
  getAssetCountsMock.mockReset();
  getAssetCountsMock.mockResolvedValue({ total: 9, unfiled: 0, missing: 1 });

  // The library grid lists the unfiled library at app load (no pin); the queue
  // lists the pinned set. Both read the same mock, so the pin is what decides.
  listAssetsMock.mockReset();
  listAssetsMock.mockImplementation(async params => ({
    items: params?.missing === true ? [missingAsset] : [],
    hasMore: false,
  }));
  listTagsMock.mockReset();
  listTagsMock.mockResolvedValue([]);
  listCreatorsMock.mockReset();
  listCreatorsMock.mockResolvedValue([]);
  listFoldersMock.mockReset();
  listFoldersMock.mockResolvedValue([]);

  deleteAssetMock.mockReset();
  deleteAssetMock.mockResolvedValue({ ok: true, trash_path: ".trash/asset-missing", files: 0, bytes: 0 });

  getQueueStatusMock.mockReset();
  getQueueStatusMock.mockResolvedValue(makeStatus());
  startQueueScanMock.mockReset();
  startQueueScanMock.mockResolvedValue(makeStatus({ running: true, finished_at: null }));

  getCardScanStatusMock.mockReset();
  getCardScanStatusMock.mockResolvedValue(makeStatus());
  startCardScanMock.mockReset();
  startCardScanMock.mockResolvedValue(makeStatus({ running: true, finished_at: null }));
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

describe("App missing queue entry policy and pin (Mf-1tx3)", () => {
  it("does not read the scan status or pin the listing before the queue is opened", async () => {
    await renderApp();
    await settle(30);

    // The count lives on the queue's own tab, so nothing carries a badge before
    // Review is entered - and the entry is not paid for at app load either.
    expect(screen.queryAllByTestId("review-count-missing")).toHaveLength(0);
    expect(getQueueStatusMock).not.toHaveBeenCalled();
    expect(pinnedCalls()).toHaveLength(0);

    // Entering Review lands on the bundle queue; the missing tab's number comes
    // from the counts read app load already paid for, and an unfailed counts
    // read is a number, not a dash.
    fireEvent.click(openReviewButton());
    await waitFor(() => expect(badgeText()).toBe("1"));
    expect(getQueueStatusMock).not.toHaveBeenCalled();
    expect(pinnedCalls()).toHaveLength(0);
  });

  it("opens the queue with exactly one scan-status read and pins the listing", async () => {
    await renderApp();
    await openQueue();
    await queueReady();
    await settle(30);

    expect(getQueueStatusMock).toHaveBeenCalledTimes(1);

    // The library grid already listed WITHOUT the pin at app load, so only the
    // calls issued after the queue opened are expected to carry `missing: true`.
    const pinned = pinnedCalls();
    expect(pinned.length).toBeGreaterThan(0);
    const params = lastPinnedParams();
    expect(params?.missing).toBe(true);
    expect(typeof params?.limit).toBe("number");
    expect(typeof params?.offset).toBe("number");

    // The facets must describe the same pinned set as the listing.
    expect(listTagsMock).toHaveBeenLastCalledWith(expect.objectContaining({ missing: true }));
    expect(listCreatorsMock).toHaveBeenLastCalledWith(expect.objectContaining({ missing: true }));
  });

  it("reads the scan status exactly once under StrictMode", async () => {
    render(
      <React.StrictMode>
        <App />
      </React.StrictMode>,
    );
    await screen.findByRole("button", { name: "Review" });

    await openQueue();
    await queueReady();
    await settle(30);

    expect(getQueueStatusMock).toHaveBeenCalledTimes(1);
    // No `listAssets` count is asserted on purpose: the shared `useAssetList`
    // has no StrictMode guard yet. That is a recorded follow-up, not a claim
    // this ticket makes.
  });

  it("keeps the queue open across a Files/Projects mode switch without re-reading the status", async () => {
    await renderApp();
    await openQueue();
    await queueReady();

    // One tab row, rendered inside the destination, before the switch...
    expect(screen.getAllByRole("tablist")).toHaveLength(1);

    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));

    expect(window.localStorage.getItem(MODE_STORAGE_KEY)).toBe("projects");
    // ...and still exactly one after it: the header is outside both panes, so
    // the switch cannot duplicate the row or move the selection.
    expect(screen.getAllByRole("tablist")).toHaveLength(1);
    expect(missingTab().getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("heading", { name: "Missing files" })).toBeTruthy();
    await settle(30);
    // The switch must not re-enter the queue's hook either.
    expect(getQueueStatusMock).toHaveBeenCalledTimes(1);
  });
});

describe("App missing queue coherence (Mf-1tx3)", () => {
  it("refetches the grid and the counts once when a rescan finishes", async () => {
    startQueueScanMock.mockResolvedValue(makeStatus({ running: true, finished_at: null }));
    getQueueStatusMock
      // Entry read: a finished scan, so nothing is polling yet.
      .mockResolvedValueOnce(makeStatus({ finished_at: 100, missing: 1 }))
      // The one poll after Rescan: the scan finished.
      .mockResolvedValue(makeStatus({ finished_at: 200, missing: 1 }));

    await renderApp();
    await openQueue();
    await queueReady();
    await waitFor(() => expect(badgeText()).toBe("1"));
    await waitFor(() => expect(pinnedCalls().length).toBe(1));

    const pinnedBefore = pinnedCalls().length;
    expect(getAssetCountsMock).toHaveBeenCalledTimes(1);

    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      fireEvent.click(screen.getByRole("button", { name: "Rescan storage folder" }));
      await flush();
      expect(startQueueScanMock).toHaveBeenCalledTimes(1);
      // The POST landed and the hook poll is armed; advance past POLL_MS.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1600);
      });
      await flush();
    } finally {
      vi.useRealTimers();
    }

    // The scan finishing bumps BOTH change signals (assets and folders) in one
    // commit, so App's counts effect runs once: the app-load read plus exactly
    // one post-scan read, and the pinned grid is re-issued exactly once.
    await waitFor(() => expect(getAssetCountsMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(pinnedCalls().length).toBe(pinnedBefore + 1));
    expect(lastPinnedParams()?.missing).toBe(true);
    // Entry read plus the one poll the fake timers drove: the finished status
    // is what fired the two change signals.
    expect(getQueueStatusMock).toHaveBeenCalledTimes(2);

    await settle(30);
    expect(getAssetCountsMock).toHaveBeenCalledTimes(2);
    expect(pinnedCalls().length).toBe(pinnedBefore + 1);
  });

  it("drops the badge and the header by one after a Forget", async () => {
    getAssetCountsMock
      .mockResolvedValueOnce({ total: 9, unfiled: 0, missing: 1 })
      .mockResolvedValue({ total: 9, unfiled: 0, missing: 0 });

    await renderApp();
    await openQueue();
    await queueReady();
    await waitFor(() => expect(badgeText()).toBe("1"));

    const card = await findCard(missingAsset.id);
    expect(within(card).getByText("File missing")).toBeTruthy();

    // Forget entry is the missing card's delete: the same typed confirmation
    // the server demands (`confirm_name`).
    fireEvent.click(
      within(openCardMenu(card)).getByRole("menuitem", { name: "Forget entry" }),
    );
    const dialog = within(screen.getByRole("dialog", { name: /^Forget/ }));
    const confirm = dialog.getByRole("button", { name: "Forget entry" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);

    fireEvent.change(dialog.getByLabelText("Type the file name to confirm"), {
      target: { value: missingAsset.filename },
    });
    fireEvent.click(confirm);

    await waitFor(() => expect(deleteAssetMock).toHaveBeenCalledTimes(1));
    expect(deleteAssetMock).toHaveBeenCalledWith(missingAsset.id, missingAsset.filename);

    // One delete = one refetch of the counts: the app-load read plus this one.
    await waitFor(() => expect(getAssetCountsMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(badgeText()).toBe("0"));
    expect(headerCount().textContent).toBe("0");
    expect(getAssetCountsMock).toHaveBeenCalledTimes(2);
  });

  it("takes a reappeared file out of the queue and off the badge", async () => {
    // The API half of this proof is `test_a_cleared_flag_leaves_the_list` in
    // curiarium/api/tests/test_asset_missing_filter.py: a cleared flag drops the
    // row from `GET /assets?missing=true`. This half proves the queue follows it.
    let pinnedReads = 0;
    listAssetsMock.mockImplementation(async params => {
      if (params?.missing !== true) return { items: [], hasMore: false };
      pinnedReads += 1;
      return { items: pinnedReads === 1 ? [missingAsset] : [], hasMore: false };
    });
    getAssetCountsMock
      .mockResolvedValueOnce({ total: 9, unfiled: 0, missing: 1 })
      .mockResolvedValue({ total: 9, unfiled: 0, missing: 0 });
    startCardScanMock.mockResolvedValue(makeStatus({ running: true, finished_at: null }));
    getCardScanStatusMock.mockResolvedValue(makeStatus({ finished_at: 200, missing: 0 }));

    await renderApp();
    await openQueue();
    await queueReady();
    await waitFor(() => expect(badgeText()).toBe("1"));

    const card = await findCard(missingAsset.id);
    fireEvent.click(within(openCardMenu(card)).getByRole("menuitem", { name: "Re-scan" }));

    await waitFor(() => expect(startCardScanMock).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(document.querySelector(`[data-asset-id="${missingAsset.id}"]`)).toBeNull(),
    );
    await waitFor(() => expect(badgeText()).toBe("0"));
    expect(headerCount().textContent).toBe("0");
    // The queue never left its pin to notice the file was back.
    expect(lastPinnedParams()?.missing).toBe(true);
  });

  it("never calls a failed scan a clean disk", async () => {
    getQueueStatusMock.mockResolvedValue(
      makeStatus({
        running: false,
        finished_at: 100,
        registered: 0,
        known: 0,
        ignored: 0,
        missing: 0,
        failed: 2,
        errors: ["a.stl: boom", "b.stl: boom"],
      }),
    );
    getAssetCountsMock.mockResolvedValue({ total: 9, unfiled: 0, missing: 0 });
    listAssetsMock.mockImplementation(async () => ({ items: [], hasMore: false }));

    await renderApp();
    await openQueue();
    await queueReady();

    // The headline honesty assertion of this slice: an empty queue after a
    // failed scan is "we could not check", never "the disk is clean".
    expect(await screen.findByText("2 files failed")).toBeTruthy();
    expect(screen.getByText("a.stl: boom")).toBeTruthy();
    expect(screen.getByText(/could not check every file/)).toBeTruthy();

    const block = await waitFor(() => {
      const found = document.querySelector<HTMLElement>('[data-missing-empty="failed"]');
      if (!found) throw new Error("the failed empty-state is not rendered");
      return found;
    });
    expect(block.textContent).toMatch(/did not finish/);
    expect(block.textContent).toMatch(/may not be listed yet/);
    expect(document.body.textContent).not.toMatch(/is on disk/);
    expect(document.body.textContent).not.toMatch(/Every file an asset points at/);
  });

  it("does not call a failed card Re-scan a clean disk", async () => {
    // The queue's own entry read is clean; the card's Re-scan cannot check
    // every file, so the previous all-clear must not survive it.
    getQueueStatusMock.mockResolvedValue(makeStatus({ finished_at: 100, failed: 0, missing: 1 }));
    let pinnedReads = 0;
    listAssetsMock.mockImplementation(async params => {
      if (params?.missing !== true) return { items: [], hasMore: false };
      pinnedReads += 1;
      // The file came back, so the refresh after the scan returns no rows.
      return { items: pinnedReads === 1 ? [missingAsset] : [], hasMore: false };
    });
    getAssetCountsMock
      .mockResolvedValueOnce({ total: 9, unfiled: 0, missing: 1 })
      .mockResolvedValue({ total: 9, unfiled: 0, missing: 0 });
    startCardScanMock.mockResolvedValue(makeStatus({ running: true, finished_at: null }));
    getCardScanStatusMock.mockResolvedValue(
      makeStatus({
        finished_at: 200,
        missing: 0,
        failed: 2,
        errors: ["a.stl: boom", "b.stl: boom"],
      }),
    );

    await renderApp();
    await openQueue();
    await queueReady();
    await waitFor(() => expect(badgeText()).toBe("1"));

    const card = await findCard(missingAsset.id);
    fireEvent.click(within(openCardMenu(card)).getByRole("menuitem", { name: "Re-scan" }));

    await waitFor(() =>
      expect(document.querySelector(`[data-asset-id="${missingAsset.id}"]`)).toBeNull(),
    );

    const block = await waitFor(() => {
      const found = document.querySelector<HTMLElement>('[data-missing-empty="failed"]');
      if (!found) throw new Error("the failed empty-state is not rendered");
      return found;
    });
    expect(block.textContent).toMatch(/did not finish/);
    expect(document.body.textContent).not.toMatch(/is on disk/);
    expect(document.body.textContent).not.toMatch(/Every file an asset points at/);
    expect(document.body.textContent).not.toMatch(/every check passed/);
    expect(document.body.textContent).toMatch(/could not check every file/);
  });

  it("does not call a card scan clean while it has not finished", async () => {
    getQueueStatusMock.mockResolvedValue(makeStatus({ finished_at: 100, failed: 0, missing: 1 }));
    let pinnedReads = 0;
    listAssetsMock.mockImplementation(async params => {
      if (params?.missing !== true) return { items: [], hasMore: false };
      pinnedReads += 1;
      return { items: pinnedReads === 1 ? [missingAsset] : [], hasMore: false };
    });
    getAssetCountsMock
      .mockResolvedValueOnce({ total: 9, unfiled: 0, missing: 1 })
      .mockResolvedValue({ total: 9, unfiled: 0, missing: 0 });
    startCardScanMock.mockResolvedValue(makeStatus({ running: true, finished_at: null }));
    getCardScanStatusMock
      // The card's first read: still walking, so its loop parks in the sleep.
      .mockResolvedValueOnce(makeStatus({ running: true, finished_at: null }))
      // The next read: the scan stopped but never recorded a finish.
      .mockResolvedValue(makeStatus({ running: false, finished_at: null, failed: 0 }));

    await renderApp();
    await openQueue();
    await queueReady();
    await waitFor(() => expect(badgeText()).toBe("1"));

    const card = await findCard(missingAsset.id);

    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      fireEvent.click(within(openCardMenu(card)).getByRole("menuitem", { name: "Re-scan" }));
      await flush();

      // A scan that has not finished is `unknown`: the clean entry read is stale.
      expect(document.body.textContent).not.toMatch(/every check passed/);
      expect(document.body.textContent).toMatch(/No completed scan is known/);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      await flush();
    } finally {
      vi.useRealTimers();
    }

    // No `finished_at` is no completed scan at all, so the empty queue stays
    // unknown rather than becoming the clean entry read again.
    const block = await waitFor(() => {
      const found = document.querySelector<HTMLElement>('[data-missing-empty="unknown"]');
      if (!found) throw new Error("the unknown empty-state is not rendered");
      return found;
    });
    expect(block.textContent).toMatch(/Nothing is flagged as missing yet/);
    expect(document.body.textContent).not.toMatch(/every check passed/);
    expect(document.body.textContent).not.toMatch(/is on disk/);
  });

  it("still calls a clean card Re-scan clean", async () => {
    // The anti-over-correction guard: a card Re-scan that checked every file
    // must clear the queue's previous "could not check" verdict.
    getQueueStatusMock.mockResolvedValue(makeStatus({ finished_at: 100, failed: 2, missing: 1 }));
    listAssetsMock.mockImplementation(async params => ({
      items: params?.missing === true ? [missingAsset] : [],
      hasMore: false,
    }));
    getAssetCountsMock.mockResolvedValue({ total: 9, unfiled: 0, missing: 1 });
    startCardScanMock.mockResolvedValue(makeStatus({ running: true, finished_at: null }));
    getCardScanStatusMock.mockResolvedValue(makeStatus({ finished_at: 200, failed: 0, missing: 1 }));

    await renderApp();
    await openQueue();
    await queueReady();
    await waitFor(() => expect(badgeText()).toBe("1"));
    expect(await screen.findByText(/could not check every file/)).toBeTruthy();

    const card = await findCard(missingAsset.id);
    fireEvent.click(within(openCardMenu(card)).getByRole("menuitem", { name: "Re-scan" }));

    await waitFor(() => expect(document.body.textContent).toMatch(/every check passed/));
    // The file is still missing, so the row remains and no empty state appears.
    expect(document.querySelector(`[data-asset-id="${missingAsset.id}"]`)).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/Every file an asset points at is on disk/);
  });
});

describe("App missing queue counts and coordinator (Mf-1tx3)", () => {
  it("reads the missing count from one fetch into both the badge and the header", async () => {
    getAssetCountsMock.mockResolvedValue({ total: 40, unfiled: 3, missing: 12 });

    await renderApp();
    await openQueue();
    await queueReady();

    await waitFor(() => expect(badgeText()).toBe("12"));
    expect(headerCount().textContent).toBe("12");
    // One wording feeds both surfaces, so the same number cannot be described
    // two ways.
    expect(badge().getAttribute("title")).toBe(describeQueueCount("missing", 12));
    expect(headerCount().getAttribute("title")).toBe(describeQueueCount("missing", 12));
    // Opening the queue costs no extra counts read.
    expect(getAssetCountsMock).toHaveBeenCalledTimes(1);
  });

  it("dashes the count and says Count unavailable when the counts fetch failed", async () => {
    // App logs a non-auth counts failure on the console; that noise is not an
    // assertion.
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    getAssetCountsMock.mockRejectedValue(new Error("counts are down"));

    try {
      await renderApp();
      await openQueue();
      await queueReady();

      expect(badgeText()).toBe("\u2013");
      expect(badge().getAttribute("title")).toBe("Count unavailable");
      expect(headerCount().textContent).toBe("\u2013");
      expect(headerCount().getAttribute("title")).toBe("Count unavailable");
    } finally {
      consoleError.mockRestore();
    }
  });

  it("reads a known zero as an empty queue, not an unknown", async () => {
    getAssetCountsMock.mockResolvedValue({ total: 4, unfiled: 0, missing: 0 });
    listAssetsMock.mockImplementation(async () => ({ items: [], hasMore: false }));

    await renderApp();
    await openQueue();
    await queueReady();

    await waitFor(() => expect(badgeText()).toBe("0"));
    expect(badge().getAttribute("title")).toBe("0 missing files awaiting a decision");
    expect(headerCount().textContent).toBe("0");
  });

  it("renders an empty all-clear only when the scan is clean", async () => {
    getAssetCountsMock.mockResolvedValue({ total: 4, unfiled: 0, missing: 0 });
    getQueueStatusMock.mockResolvedValue(makeStatus({ finished_at: 100, missing: 0, failed: 0 }));
    listAssetsMock.mockImplementation(async () => ({ items: [], hasMore: false }));

    await renderApp();
    await openQueue();
    await queueReady();

    expect(await screen.findByText("Every file an asset points at is on disk.")).toBeTruthy();
    expect(document.querySelector('[data-missing-empty="clean"]')).toBeTruthy();
  });

  it("stays flag-truth when no completed scan is known", async () => {
    getAssetCountsMock.mockResolvedValue({ total: 4, unfiled: 0, missing: 0 });
    getQueueStatusMock.mockResolvedValue(makeStatus({ finished_at: null, missing: 0 }));
    listAssetsMock.mockImplementation(async () => ({ items: [], hasMore: false }));

    await renderApp();
    await openQueue();
    await queueReady();

    const block = await waitFor(() => {
      const found = document.querySelector<HTMLElement>('[data-missing-empty="unknown"]');
      if (!found) throw new Error("the unknown empty-state is not rendered");
      return found;
    });
    expect(block.textContent).toMatch(/Nothing is flagged as missing yet/);
    // No completed scan is not evidence that nothing is missing.
    expect(document.body.textContent).not.toMatch(/is on disk/);
  });
});

describe("App missing queue roles (Mf-1tx3)", () => {
  it("gives an editor the rescan, Re-scan and Forget", async () => {
    getMeMock.mockResolvedValue(me("editor"));

    await renderApp();
    await openQueue();
    await queueReady();

    const card = await findCard(missingAsset.id);
    expect(screen.getByRole("button", { name: "Rescan storage folder" })).toBeTruthy();

    const menu = openCardMenu(card);
    expect(within(menu).getByRole("menuitem", { name: "Re-scan" })).toBeTruthy();
    expect(within(menu).getByRole("menuitem", { name: "Forget entry" })).toBeTruthy();
  });

  it("gives a viewer the queue, the banner and no write control at all", async () => {
    getMeMock.mockResolvedValue(me("viewer"));

    await renderApp();

    // A viewer has no bundle tab, so their Review entry resolves to the first
    // queue they may see - imports. The missing queue is one tab away.
    fireEvent.click(openReviewButton());
    await waitFor(() => expect(screen.getByRole("tabpanel").id).toBe("review-tabpanel-imports"));
    fireEvent.click(await screen.findByRole("tab", { name: /^Missing files/ }));
    await screen.findByRole("heading", { name: "Missing files" });
    await queueReady();

    // The tab and the queue are READ surfaces: a viewer keeps both.
    expect(missingTab().getAttribute("aria-selected")).toBe("true");
    const card = await findCard(missingAsset.id);
    expect(within(card).getByText("File missing")).toBeTruthy();

    // Absent from the DOM, not disabled: the rescan is `POST /library/scan`.
    expect(screen.queryByRole("button", { name: "Rescan storage folder" })).toBeNull();

    const menu = openCardMenu(card);
    expect(within(menu).queryByRole("menuitem", { name: "Re-scan" })).toBeNull();
    expect(within(menu).queryByRole("menuitem", { name: "Forget entry" })).toBeNull();

    // `GET /library/scan` is READ, so a viewer still reads the status, once.
    expect(getQueueStatusMock).toHaveBeenCalledTimes(1);
  });
});
