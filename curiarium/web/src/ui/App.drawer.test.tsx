// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

// The real App is the subject: the real Sidebar, the real WorkspaceHeader and
// the real AssetGrid all render here, which is what makes the focus, trap and
// virtualisation assertions mean anything. Only the network, the lazy views and
// the WebGL viewers are replaced.
vi.mock("../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  FORBIDDEN_MESSAGE: "Your role does not allow this action.",
  apiHealth: vi.fn(async () => ({ ok: true, auth_required: true })),
  fileUrl: vi.fn((rel: string) => rel),
  getApiBase: vi.fn(() => "http://localhost:8000"),
  getAssetCounts: vi.fn(async () => ({ total: 60, unfiled: 10, missing: 0 })),
  getMe: vi.fn(),
  listProjects: vi.fn(async () => []),
  logout: vi.fn(async () => {}),
  refreshToken: vi.fn(),
  // The folder tree (Sidebar).
  listFolders: vi.fn(async () => []),
  createFolder: vi.fn(),
  updateFolder: vi.fn(),
  downloadFolderZip: vi.fn(),
  deleteFolder: vi.fn(),
  getFolderDeletePreview: vi.fn(),
  // The asset grid and its dialogs.
  addProjectAssets: vi.fn(),
  batchDeleteAssets: vi.fn(),
  batchMoveAssets: vi.fn(),
  batchSetMeta: vi.fn(),
  batchSetTags: vi.fn(),
  createProject: vi.fn(),
  deleteAsset: vi.fn(),
  deleteAssetFile: vi.fn(),
  deletePreparedPrint: vi.fn(),
  downloadZip: vi.fn(),
  getAssetDeletePreview: vi.fn(),
  getLibraryScanStatus: vi.fn(),
  assetFilePreviewUrl: vi.fn((rel: string) => `${rel}?inline=1`),
  listAssetFileLimits: vi.fn(async () => ({
    count: 0,
    total_bytes: 0,
    max_count: 0,
    max_file_bytes: 0,
    max_total_bytes: 0,
  })),
  listAssetFiles: vi.fn(async () => []),
  renameAssetFile: vi.fn(),
  reorderAssetFiles: vi.fn(async (_assetId: string, fileIds: string[]) => fileIds),
  listAssets: vi.fn(),
  listCreators: vi.fn(async () => []),
  listTags: vi.fn(async () => []),
  setTags: vi.fn(),
  startLibraryScan: vi.fn(),
  updateAssetFolder: vi.fn(),
  updateAssetMeta: vi.fn(),
  uploadAsset: vi.fn(),
  uploadAssetFile: vi.fn(),
  uploadGeneratedThumbnail: vi.fn(),
}));

vi.mock("../lib/api/settings", () => ({
  getImportRuns: vi.fn(async () => ({ runs: [], total: 0, limit: 1, offset: 0 })),
  getImportRun: vi.fn(),
  getLibraryScanStatus: vi.fn(),
  startLibraryScan: vi.fn(),
}));

vi.mock("../lib/api/bundles", () => ({
  getBundleReport: vi.fn(async () => ({
    root: "",
    truncated: false,
    counts: { total: 0, high: 0, low: 0, ready: 0, blocked: 0, dismissed: 0, already_created: 0 },
    bundles: [],
    stale_dismissals: [],
  })),
  createBundles: vi.fn(),
  setBundleDismissed: vi.fn(),
}));

vi.mock("./ModelViewer", () => ({
  default: () => null,
  ModelSnapshot: () => null,
}));

vi.mock("./LightBurnPreview", () => ({
  default: () => null,
}));

// The lazy destinations are stand-ins: this file is about the shell, and each
// destination is proven by its own suite.
vi.mock("./settings/Settings", () => ({
  default: () => <div>Settings view</div>,
}));
vi.mock("./AuditLog", () => ({ default: () => <div>Audit view</div> }));
vi.mock("./ProjectView", () => ({ default: () => <div>Project view</div> }));
vi.mock("./review/ReviewQueueTabs", () => ({ default: () => <div>Review view</div> }));
vi.mock("./review/BundleReviewView", () => ({ default: () => null }));
vi.mock("./review/ImportsReviewView", () => ({ default: () => null }));
vi.mock("./review/MissingFilesReviewView", () => ({ default: () => null }));
vi.mock("./review/TrashQueue", () => ({ default: () => null }));
vi.mock("./UploadBar", () => ({ default: () => null }));
vi.mock("./UploadQueuePanel", () => ({ default: () => null }));
vi.mock("./Login", () => ({ default: () => null }));

import App from "./App";
import {
  getAssetCounts,
  getAssetDeletePreview,
  getMe,
  listAssets,
  listCreators,
  listFolders,
  listProjects,
  listTags,
  type Asset,
  type Folder,
} from "../lib/api";
import { storeToken, type Role } from "../lib/auth";
import { DRAWER_TABBABLE_SELECTOR, tabbableIn } from "../lib/focusTrap";
import { MODE_STORAGE_KEY } from "../lib/mode";
import {
  colsForWidth,
  computeVirtualWindow,
  rowOffsets,
  rowsTotal,
} from "../lib/virtualGrid";

const listAssetsMock = vi.mocked(listAssets);
const listFoldersMock = vi.mocked(listFolders);
const listCreatorsMock = vi.mocked(listCreators);
const listTagsMock = vi.mocked(listTags);
const listProjectsMock = vi.mocked(listProjects);
const getMeMock = vi.mocked(getMe);
const getAssetCountsMock = vi.mocked(getAssetCounts);
const getAssetDeletePreviewMock = vi.mocked(getAssetDeletePreview);

const TOTAL_ASSETS = 60;
const VIEWPORT_HEIGHT = 768;

/** Alpha owns a child, Beta is a sibling: the tree has two root rows. */
const FOLDERS: Folder[] = [
  { id: "folder-a", name: "Alpha", tags: [] },
  { id: "folder-a-1", name: "Alpha child", tags: [], parent_id: "folder-a" },
  { id: "folder-b", name: "Beta", tags: [] },
];

/** Vendor-free, WebGL-free fixture: a png with a thumbnail. */
function makeAsset(index: number): Asset {
  const id = `asset-${index}`;
  return {
    id,
    name: `Asset ${index}`,
    filename: `asset-${String(index).padStart(5, "0")}.png`,
    mime: "image/png",
    size: 10_000 - index,
    tags: [],
    url: `/api/assets/${id}/file`,
    thumb_url: `/api/assets/${id}/thumb`,
    supporting_file_count: 0,
  };
}

function makeAssets(count: number): Asset[] {
  return Array.from({ length: count }, (_, index) => makeAsset(index + 1));
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

type Listener = () => void;

/**
 * A per-test `matchMedia` stub. Each call hands out a fresh query object, so one
 * test (or one hook instance) cannot poison another's listener or `matches`.
 */
function stubNarrow(matches: boolean): void {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: (query: string) => {
      const listeners = new Set<Listener>();
      return {
        matches,
        media: query,
        onchange: null,
        addEventListener: (_type: string, listener: Listener) => {
          listeners.add(listener);
        },
        removeEventListener: (_type: string, listener: Listener) => {
          listeners.delete(listener);
        },
        addListener: (listener: Listener) => {
          listeners.add(listener);
        },
        removeListener: (listener: Listener) => {
          listeners.delete(listener);
        },
        dispatchEvent: () => true,
      };
    },
  });
}

function removeMatchMedia(): void {
  delete (window as { matchMedia?: unknown }).matchMedia;
}

/** The control a test uses to change the viewport. */
type Viewport = {
  setMatches: (next: boolean) => void;
};

/**
 * A `matchMedia` stub whose `matches` a test can flip and whose `change`
 * listeners it can fire, like a real resize. Every query object shares the same
 * value and listener set, because the hook keeps whichever object it was handed
 * at first render while later subscriptions must still be notified.
 */
function stubMutableNarrow(matches: boolean): Viewport {
  const listeners = new Set<Listener>();
  let current = matches;
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: (query: string) => ({
      get matches(): boolean {
        return current;
      },
      media: query,
      onchange: null,
      addEventListener: (_type: string, listener: Listener) => {
        listeners.add(listener);
      },
      removeEventListener: (_type: string, listener: Listener) => {
        listeners.delete(listener);
      },
      addListener: (listener: Listener) => {
        listeners.add(listener);
      },
      removeListener: (listener: Listener) => {
        listeners.delete(listener);
      },
      dispatchEvent: () => true,
    }),
  });
  return {
    setMatches(next: boolean): void {
      current = next;
      for (const listener of Array.from(listeners)) listener();
    },
  };
}

beforeEach(() => {
  installMemoryStorage();
  storeToken("access-token");
  window.history.replaceState(null, "", "/");
  removeMatchMedia();
  listAssetsMock.mockReset();
  listAssetsMock.mockResolvedValue({ items: makeAssets(TOTAL_ASSETS), hasMore: false });
  listFoldersMock.mockReset();
  listFoldersMock.mockResolvedValue(FOLDERS);
  listCreatorsMock.mockReset();
  listCreatorsMock.mockResolvedValue([]);
  listTagsMock.mockReset();
  listTagsMock.mockResolvedValue([]);
  listProjectsMock.mockReset();
  listProjectsMock.mockResolvedValue([]);
  getMeMock.mockReset();
  getMeMock.mockResolvedValue({
    id: "u1",
    username: "admin",
    email: null,
    role: "admin",
    disabled: false,
    kind: "local",
  });
  getAssetCountsMock.mockReset();
  getAssetCountsMock.mockResolvedValue({ total: TOTAL_ASSETS, unfiled: 10, missing: 0 });
  getAssetDeletePreviewMock.mockReset();
  getAssetDeletePreviewMock.mockResolvedValue({ assets: 0, files: 0, bytes: 0, snapshot: null });
});

afterEach(() => {
  cleanup();
  removeMatchMedia();
});

/** jsdom has no layout: supply the DOMRect-shaped value the offset math reads. */
function rectOf(top: number, height: number): DOMRect {
  return {
    top,
    bottom: top + height,
    height,
    left: 0,
    right: 0,
    width: 0,
    x: 0,
    y: top,
    toJSON: () => ({}),
  } as DOMRect;
}

function mainElement(): HTMLElement {
  const main = document.querySelector<HTMLElement>("main");
  if (!main) throw new Error("App rendered no <main>");
  return main;
}

function rail(): HTMLElement {
  const found = document.querySelector<HTMLElement>(".library-rail");
  if (!found) throw new Error("App rendered no rail");
  return found;
}

function drawerToggle(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Folders" }) as HTMLButtonElement;
}

function treeRow(id: string): HTMLElement {
  const row = document.querySelector<HTMLElement>(`[data-tree-row="${id}"]`);
  if (!row) throw new Error(`no tree row ${id}`);
  return row;
}

function summaryOf(row: HTMLElement): HTMLElement {
  const summary = row.querySelector<HTMLElement>("summary");
  if (!summary) throw new Error("row has no summary");
  return summary;
}

/** Query arguments of the last `listAssets` call. */
function lastListAssetsParams() {
  const calls = listAssetsMock.mock.calls;
  const params = calls.length ? calls[calls.length - 1][0] : undefined;
  if (!params) throw new Error("listAssets was never called");
  return params;
}

/** Render App in Files mode with the tree and the grid painted. */
async function renderFilesApp(
  options: {
    narrow?: boolean;
    role?: Role;
    /** Replace the default `matchMedia` stub, e.g. with the mutable one. */
    stubViewport?: (matches: boolean) => void;
  } = {},
): Promise<HTMLElement> {
  const { narrow = true, role = "admin", stubViewport = stubNarrow } = options;
  stubViewport(narrow);
  getMeMock.mockResolvedValue({
    id: "u1",
    username: "someone",
    email: null,
    role,
    disabled: false,
    kind: "local",
  });
  render(<App />);
  await screen.findByText("Asset 1");
  await waitFor(() => expect(document.querySelector('[role="treeitem"]')).toBeTruthy());
  return mainElement();
}

/** Opens the sheet and returns the roving tree row focus landed on. */
function openDrawer(): HTMLElement {
  fireEvent.click(drawerToggle());
  const roving = document.querySelector<HTMLElement>('[role="treeitem"][tabindex="0"]');
  if (!roving) throw new Error("the open drawer focused no tree row");
  return roving;
}

function gridWrap(): HTMLElement {
  const wrap = document.querySelector<HTMLElement>("[data-virtual-grid]");
  if (!wrap) throw new Error("virtual grid wrapper not rendered");
  return wrap;
}

/**
 * Gives jsdom the two numbers a browser would provide for `<main>`: a 768px
 * viewport and a scroll position, then makes the grid wrapper's rect track it.
 */
function armVirtualiser(main: HTMLElement): HTMLElement {
  Object.defineProperty(main, "clientHeight", { value: VIEWPORT_HEIGHT, configurable: true });
  vi.spyOn(main, "getBoundingClientRect").mockReturnValue(rectOf(0, VIEWPORT_HEIGHT));
  const wrap = gridWrap();
  vi.spyOn(wrap, "getBoundingClientRect").mockImplementation(() => rectOf(-main.scrollTop, 0));
  return wrap;
}

function scrollGrid(scroller: HTMLElement, top: number): void {
  scroller.scrollTop = top;
  fireEvent.scroll(scroller);
}

function mountedRowIndexes(): number[] {
  return Array.from(document.querySelectorAll("[data-row-index]")).map(element =>
    Number(element.getAttribute("data-row-index")),
  );
}

function cardCheckboxes(): HTMLInputElement[] {
  return screen.getAllByRole("checkbox", { name: "Select" }) as HTMLInputElement[];
}

/**
 * The row window the shipped geometry implies for `offset`, derived from
 * `computeVirtualWindow` rather than from a literal row range.
 */
function expectedRows(offset: number, rowCount: number, viewportHeight = VIEWPORT_HEIGHT): number[] {
  const heights = new Array<number>(rowCount).fill(0);
  const tops = rowOffsets(heights);
  const total = rowsTotal(heights);
  const range = computeVirtualWindow({ offset, viewportHeight, tops, total });
  return Array.from({ length: range.endRow - range.startRow + 1 }, (_, index) => range.startRow + index);
}

function rowCountFor(assetCount: number): number {
  return Math.ceil(assetCount / colsForWidth(window.innerWidth));
}

/** `TAG.class class ...` for the element and every ancestor, up to the root. */
function ancestorChain(element: Element): string[] {
  const chain: string[] = [];
  let node: Element | null = element;
  while (node) {
    chain.push(`${node.tagName}.${node.className}`);
    node = node.parentElement;
  }
  return chain;
}

/**
 * What a browser's native Tab order does next, which jsdom does not implement:
 * the next node in document order that is natively focusable and actually
 * rendered. Written from the DOM, never from the drawer's selector, so the walk
 * below is an independent expectation rather than an echo.
 */
function nativeNext(root: HTMLElement, from: Element | null, backward: boolean): HTMLElement | null {
  const candidates = Array.from(
    root.querySelectorAll<HTMLElement>('button, input, summary, a[href], [tabindex="0"]'),
  ).filter(element => element.tabIndex >= 0 && !element.matches(":disabled") && isNativelyVisible(element));
  if (candidates.length === 0) return null;
  const index = from ? candidates.indexOf(from as HTMLElement) : -1;
  if (index < 0) return backward ? candidates[candidates.length - 1] : candidates[0];
  const next = index + (backward ? -1 : 1);
  if (next < 0) return candidates[candidates.length - 1];
  if (next >= candidates.length) return candidates[0];
  return candidates[next];
}

/** A control inside a closed `<details>` (other than its summary) is not rendered. */
function isNativelyVisible(element: HTMLElement): boolean {
  const closed = element.closest("details:not([open])");
  if (!closed) return true;
  const summary = Array.from(closed.children).find(child => child.tagName === "SUMMARY");
  return Boolean(summary && summary.contains(element));
}

describe("App folder drawer", () => {
  it("opens from a labelled control and lands focus inside", async () => {
    await renderFilesApp();
    const toggle = drawerToggle();
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    const railId = toggle.getAttribute("aria-controls");
    expect(railId).toBeTruthy();
    expect(document.getElementById(railId!)).toBe(rail());
    expect(rail().hasAttribute("hidden")).toBe(true);
    expect(rail().getAttribute("data-drawer-state")).toBe("closed-narrow");

    // jsdom performs no implicit activation for Enter or Space on a native
    // button, so this click is the event a browser dispatches for either key.
    fireEvent.click(toggle);

    const dialog = await screen.findByRole("dialog", { name: "Library folders" });
    expect(dialog).toBe(rail());
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.getAttribute("data-drawer-state")).toBe("open");
    expect(dialog.hasAttribute("hidden")).toBe(false);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    const roving = document.querySelector<HTMLElement>('[role="treeitem"][tabindex="0"]');
    expect(roving).toBeTruthy();
    expect(document.activeElement).toBe(roving);
    expect(dialog.contains(document.activeElement!)).toBe(true);
  });

  it("closes on Escape and returns focus to the opener", async () => {
    await renderFilesApp();
    const toggle = drawerToggle();
    const roving = openDrawer();
    expect(document.activeElement).toBe(roving);

    fireEvent.keyDown(roving, { key: "Escape" });

    expect(rail().hasAttribute("hidden")).toBe(true);
    expect(rail().getAttribute("data-drawer-state")).toBe("closed-narrow");
    expect(rail().getAttribute("role")).toBeNull();
    expect(rail().getAttribute("aria-modal")).toBeNull();
    expect(screen.queryByRole("dialog", { name: "Library folders" })).toBeNull();
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    // Identity, not a re-query by name: focus went back to the exact control
    // that opened the sheet.
    expect(document.activeElement).toBe(toggle);
  });

  it("selecting anything inside the sheet closes it and proves the selection happened", async () => {
    await renderFilesApp();
    const toggle = drawerToggle();

    // A folder row.
    openDrawer();
    fireEvent.click(within(treeRow("folder-a")).getByRole("button", { name: "Alpha" }));
    expect(rail().hasAttribute("hidden")).toBe(true);
    expect(document.activeElement).toBe(toggle);
    await waitFor(() => expect(lastListAssetsParams().folder_id).toBe("folder-a"));

    // All files.
    openDrawer();
    fireEvent.click(within(rail()).getByRole("button", { name: "All files" }));
    expect(rail().hasAttribute("hidden")).toBe(true);
    expect(document.activeElement).toBe(toggle);
    await waitFor(() => expect(lastListAssetsParams().folder_id).toBeUndefined());
    expect(lastListAssetsParams().unfiled).toBe(false);

    // Unfiled.
    openDrawer();
    fireEvent.click(within(rail()).getByRole("button", { name: "Unfiled" }));
    expect(rail().hasAttribute("hidden")).toBe(true);
    expect(document.activeElement).toBe(toggle);
    await waitFor(() => expect(lastListAssetsParams().unfiled).toBe(true));
    expect(lastListAssetsParams().folder_id).toBeUndefined();
  });

  it("a selection from the grid does not steal focus", async () => {
    await renderFilesApp();
    // Into Alpha through the sheet, then one level deeper through the grid's own
    // subfolder tile, so a parent crumb exists and stays mounted.
    openDrawer();
    fireEvent.click(within(treeRow("folder-a")).getByRole("button", { name: "Alpha" }));
    const childTile = await waitFor(() => {
      const tile = document.querySelector<HTMLElement>('[data-folder-tile="folder-a-1"]');
      if (!tile) throw new Error("subfolder tile not rendered");
      return tile;
    });
    fireEvent.click(childTile);
    await waitFor(() => expect(lastListAssetsParams().folder_id).toBe("folder-a-1"));

    const crumb = await waitFor(() => {
      const found = document.querySelector<HTMLElement>('[data-folder-crumb="folder-a"]');
      if (!found) throw new Error("parent crumb not rendered");
      return found;
    });
    crumb.focus();
    fireEvent.click(crumb);

    // The grid's crumb goes through the shared handler, which must never move
    // focus: only the drawer's own wrappers close-and-return.
    expect(document.activeElement).toBe(crumb);
    expect(document.activeElement).not.toBe(drawerToggle());
    expect(document.activeElement).not.toBe(document.body);
    await waitFor(() => expect(lastListAssetsParams().folder_id).toBe("folder-a"));
  });

  it("the tree keyboard contract survives inside the sheet", async () => {
    await renderFilesApp();
    const rowA = openDrawer();
    expect(document.activeElement).toBe(rowA);
    // Root folders are default-expanded, so Alpha's child row already exists.
    const rowChild = treeRow("folder-a-1");

    fireEvent.keyDown(rowA, { key: "ArrowDown" });
    expect(document.activeElement).toBe(rowChild);
    expect(document.querySelectorAll('[role="treeitem"][tabindex="0"]')).toHaveLength(1);
    expect(rowChild.getAttribute("tabindex")).toBe("0");

    fireEvent.keyDown(rowChild, { key: "ArrowUp" });
    expect(document.activeElement).toBe(treeRow("folder-a"));

    // ArrowLeft collapses the open branch; ArrowRight expands it again and the
    // second ArrowRight enters the child row.
    fireEvent.keyDown(treeRow("folder-a"), { key: "ArrowLeft" });
    await waitFor(() => expect(document.querySelector('[data-tree-row="folder-a-1"]')).toBeNull());
    fireEvent.keyDown(treeRow("folder-a"), { key: "ArrowRight" });
    await waitFor(() => expect(document.querySelector('[data-tree-row="folder-a-1"]')).toBeTruthy());
    fireEvent.keyDown(treeRow("folder-a"), { key: "ArrowRight" });
    expect(document.activeElement).toBe(treeRow("folder-a-1"));
    expect(document.querySelectorAll('[role="treeitem"][tabindex="0"]')).toHaveLength(1);

    fireEvent.keyDown(treeRow("folder-a-1"), { key: "Enter" });

    await waitFor(() => expect(lastListAssetsParams().folder_id).toBe("folder-a-1"));
    expect(rail().hasAttribute("hidden")).toBe(true);
    expect(drawerToggle().getAttribute("aria-expanded")).toBe("false");
  });

  it("the Tab cycle equals the natively tabbable set", async () => {
    await renderFilesApp();
    const root = rail();
    openDrawer();

    const newFolder = document.querySelector<HTMLElement>(".sidebar-new-button")!;
    const searchInput = document.querySelector<HTMLElement>('input[aria-label="Search folders"]')!;
    const allFiles = screen.getByRole("button", { name: "All files" });
    const unfiled = screen.getByRole("button", { name: "Unfiled" });
    const rowA = treeRow("folder-a");
    const summaryA = summaryOf(rowA);
    const summaryChild = summaryOf(treeRow("folder-a-1"));
    const summaryB = summaryOf(treeRow("folder-b"));
    // Root folders are default-expanded, so the child row is in the DOM too:
    // its summary is tabbable, its row is not (it is not the roving stop).
    const walk = [newFolder, searchInput, allFiles, unfiled, rowA, summaryA, summaryChild, summaryB];

    expect(treeRow("folder-a-1").getAttribute("tabindex")).toBe("-1");
    expect(treeRow("folder-b").getAttribute("tabindex")).toBe("-1");

    for (let index = 0; index < walk.length - 1; index += 1) {
      const member = walk[index];
      member.focus();
      expect(document.activeElement).toBe(member);
      const notPrevented = fireEvent.keyDown(member, { key: "Tab" });
      // Every step but the last is the browser's to take.
      expect(notPrevented, `Tab from member ${index} must be left to the browser`).toBe(true);
      const next = nativeNext(root, member, false);
      expect(next, `no native successor for member ${index}`).toBeTruthy();
      next!.focus();
      expect(document.activeElement).toBe(walk[index + 1]);
      // The trap must never land inside a closed row menu.
      expect(document.activeElement!.closest("details:not([open]) .sidebar-folder-menu")).toBeNull();
    }

    // One more Tab from the last member wraps back to the first.
    summaryB.focus();
    expect(fireEvent.keyDown(summaryB, { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(newFolder);

    // And Shift+Tab from the first lands on the last.
    newFolder.focus();
    expect(fireEvent.keyDown(newFolder, { key: "Tab", shiftKey: true })).toBe(false);
    expect(document.activeElement).toBe(summaryB);
  });

  it("pins the roving row as the tree's single tab stop in the cycle", async () => {
    await renderFilesApp();
    const root = rail();

    // Select Beta, which closes the sheet and moves the tab stop to its row.
    openDrawer();
    fireEvent.click(within(treeRow("folder-b")).getByRole("button", { name: "Beta" }));
    await waitFor(() => expect(lastListAssetsParams().folder_id).toBe("folder-b"));
    expect(treeRow("folder-b").getAttribute("tabindex")).toBe("0");
    expect(treeRow("folder-a").getAttribute("tabindex")).toBe("-1");

    // Reopening lands on the roving row, and the walk now runs through it while
    // row A's summary stays in the cycle and row A's row does not.
    const roving = openDrawer();
    expect(roving).toBe(treeRow("folder-b"));

    const summaryA = summaryOf(treeRow("folder-a"));
    const summaryChild = summaryOf(treeRow("folder-a-1"));
    const unfiled = within(rail()).getByRole("button", { name: "Unfiled" });
    unfiled.focus();
    // Row A's own row is out of the cycle now; its summary and its child's
    // summary are still in it, and the tab stop is row B.
    expect(nativeNext(root, unfiled, false)).toBe(summaryA);
    expect(nativeNext(root, summaryA, false)).toBe(summaryChild);
    expect(nativeNext(root, summaryChild, false)).toBe(treeRow("folder-b"));
    expect(nativeNext(root, treeRow("folder-b"), false)).toBe(summaryOf(treeRow("folder-b")));
  });

  it("keeps a closed row menu out of the cycle and lets an open one join it", async () => {
    await renderFilesApp();
    const root = rail();
    openDrawer();

    const rowA = treeRow("folder-a");
    const summaryA = summaryOf(rowA);
    const closedMenuButton = rowA.querySelector<HTMLElement>(".sidebar-folder-menu button")!;
    expect(closedMenuButton).toBeTruthy();

    // Negative control: with the menu closed the native successor of the
    // summary is the child row's summary, never the hidden menu button.
    expect(nativeNext(root, summaryA, false)).toBe(summaryOf(treeRow("folder-a-1")));

    // Open row A's menu: its buttons are now rendered, so they join the walk.
    (rowA.querySelector("details") as HTMLDetailsElement).open = true;
    summaryA.focus();
    expect(fireEvent.keyDown(summaryA, { key: "Tab" })).toBe(true);
    const next = nativeNext(root, summaryA, false);
    expect(next).toBe(closedMenuButton);
    next!.focus();
    expect(document.activeElement!.closest("details:not([open]) .sidebar-folder-menu")).toBeNull();
  });

  it("starts the cycle at the search field for a viewer", async () => {
    await renderFilesApp({ role: "viewer" });
    const root = rail();
    openDrawer();

    // A viewer cannot create folders, so the first member is the search field.
    expect(document.querySelector(".sidebar-new-button")).toBeNull();
    const searchInput = document.querySelector<HTMLElement>('input[aria-label="Search folders"]')!;
    expect(nativeNext(root, null, false)).toBe(searchInput);

    searchInput.focus();
    expect(fireEvent.keyDown(searchInput, { key: "Tab", shiftKey: true })).toBe(false);
    expect(document.activeElement).toBe(summaryOf(treeRow("folder-b")));
  });

  // Honest scope note: jsdom's `fireEvent` dispatches inside `act`, which defers
  // React's flush until after every native listener on `document` has run. So
  // this test pins the SEMANTICS (one Escape closes one surface) but NOT the
  // browser flush ordering that the drawer's Escape listener must survive - in a
  // real browser React 18 flushes a discrete keydown synchronously at the end of
  // its root dispatch, before any bubble-phase document listener sees it. The
  // drawer's capture-phase registration is what makes the guard's pre-dispatch
  // DOM view structural rather than a timing accident. A raw out-of-`act`
  // dispatch would reproduce that ordering - the discrete flush is not
  // act-specific - but the not-wrapped-in-act warnings it rains down are why no
  // such test is kept; the ordering is enforced by the listener's phase instead.
  it("one Escape, one surface", async () => {
    await renderFilesApp();
    openDrawer();

    // The Sidebar's own create-folder editor is a dialog inside the sheet.
    fireEvent.click(document.querySelector<HTMLElement>(".sidebar-new-button")!);
    const editor = await screen.findByRole("dialog", { name: "Create folder" });
    const nameInput = within(editor).getByPlaceholderText("Folder name");
    expect(document.activeElement).toBe(nameInput);

    fireEvent.keyDown(nameInput, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Create folder" })).toBeNull());
    // One Escape closed the editor only: the sheet is still the open dialog.
    expect(screen.getByRole("dialog", { name: "Library folders" })).toBe(rail());
    expect(rail().hasAttribute("hidden")).toBe(false);
    expect(drawerToggle().getAttribute("aria-expanded")).toBe("true");
    expect(document.querySelector("dialog")).toBeNull();

    // Now the editor's nested picker: it owns Tab and Escape while it is open.
    fireEvent.click(document.querySelector<HTMLElement>(".sidebar-new-button")!);
    const reopened = await screen.findByRole("dialog", { name: "Create folder" });
    fireEvent.click(reopened.querySelector<HTMLElement>(".folder-picker-trigger")!);
    const picker = await screen.findByRole("dialog", { name: "Location" });
    const pickerInput = within(picker).getByRole("textbox", { name: "Search folders in Location" });
    const cancel = within(picker).getByRole("button", { name: "Cancel" });

    cancel.focus();
    fireEvent.keyDown(cancel, { key: "Tab" });
    expect(document.activeElement).toBe(pickerInput);
    expect(picker.contains(document.activeElement!)).toBe(true);

    fireEvent.keyDown(pickerInput, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Location" })).toBeNull());
    // Only the picker closed: the editor and the sheet are still there.
    expect(screen.getByRole("dialog", { name: "Create folder" })).toBe(reopened);
    expect(screen.getByRole("dialog", { name: "Library folders" })).toBe(rail());

    // Close the reopened editor the same way, which is also the state in which
    // the focused input is removed from the DOM.
    const reopenedInput = within(reopened).getByPlaceholderText("Folder name");
    reopenedInput.focus();
    fireEvent.keyDown(reopenedInput, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Create folder" })).toBeNull());
    expect(screen.getByRole("dialog", { name: "Library folders" })).toBe(rail());

    // A browser and jsdom both drop focus to <body> when the focused input
    // unmounts with its editor. That is exactly why the trap cannot live on the
    // rail div: <body> is an ancestor of the React root, so a keydown aimed at
    // it never reaches a handler hanging off the rail.
    expect(document.activeElement).toBe(document.body);
    // A pointer click on the non-focusable scrim leaves focus on <body> too, so
    // the recapture below is the same mechanism for both paths.
    expect(document.querySelector(".folder-drawer-scrim")).not.toBeNull();

    const members = tabbableIn(rail(), DRAWER_TABBABLE_SELECTOR);
    expect(members.length).toBeGreaterThan(1);
    const first = members[0];
    const last = members[members.length - 1];
    // Pin where "last" sits: the sheet's final member is the last folder row's
    // summary, not the tree's roving row.
    expect(last.tagName).toBe("SUMMARY");
    expect(treeRow("folder-b").contains(last)).toBe(true);

    // Tab from <body> is claimed by the trap and lands on the first member.
    expect(fireEvent.keyDown(document.body, { key: "Tab" })).toBe(false);
    expect(document.activeElement).toBe(first);
    expect(rail().contains(document.activeElement)).toBe(true);

    // Shift+Tab from <body> recaptures backwards, onto the last member.
    (document.activeElement as HTMLElement).blur();
    expect(document.activeElement).toBe(document.body);
    expect(fireEvent.keyDown(document.body, { key: "Tab", shiftKey: true })).toBe(false);
    expect(document.activeElement).toBe(last);

    // Escape on <body> belongs to the sheet: it closes, and focus returns to
    // the exact control that opened it.
    (document.activeElement as HTMLElement).blur();
    expect(document.activeElement).toBe(document.body);
    expect(fireEvent.keyDown(document.body, { key: "Escape" })).toBe(false);
    expect(rail().hasAttribute("hidden")).toBe(true);
    expect(drawerToggle().getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(drawerToggle());
  });

  it("a narrow -> wide -> narrow resize keeps the sheet and steals no focus", async () => {
    let viewport: Viewport = { setMatches: () => {} };
    await renderFilesApp({
      stubViewport: matches => {
        viewport = stubMutableNarrow(matches);
      },
    });
    const row = openDrawer();
    expect(document.activeElement).toBe(row);

    // Move the caret somewhere that is not the roving row, the way an operator
    // typing in the search field would.
    const searchInput = document.querySelector<HTMLInputElement>('input[aria-label="Search folders"]')!;
    searchInput.focus();
    expect(document.activeElement).toBe(searchInput);

    act(() => viewport.setMatches(false));
    act(() => viewport.setMatches(true));

    // The open sheet survived the round trip, and its state attributes never
    // flipped to a closed one.
    expect(drawerToggle().getAttribute("aria-expanded")).toBe("true");
    expect(rail().getAttribute("data-drawer-state")).toBe("open");
    expect(rail().hasAttribute("hidden")).toBe(false);
    // A width change re-focused nothing: the search field still owns the caret,
    // not the roving tree row that opening the sheet had focused.
    expect(document.activeElement).toBe(searchInput);
    expect(document.activeElement).not.toBe(row);
  });

  it("the drawer is not the grid's scroll container", async () => {
    const main = await renderFilesApp();
    const wrap = armVirtualiser(main);
    await waitFor(() => expect(mountedRowIndexes().length).toBeGreaterThan(0));

    const mainBefore = document.querySelector("main");
    const ancestorsBefore = ancestorChain(wrap);

    fireEvent.click(drawerToggle());
    expect(rail().getAttribute("role")).toBe("dialog");

    // (a) <main> is the same node while the sheet is open, and the scrim is
    // APPENDED after it: an unkeyed children array reconciled by index would
    // remount <main> if the scrim took a slot in front of it.
    expect(document.querySelector("main")).toBe(mainBefore);
    const row = main.parentElement!;
    const scrim = document.querySelector<HTMLElement>(".folder-drawer-scrim")!;
    expect(scrim).toBeTruthy();
    expect(row.lastElementChild).toBe(scrim);
    expect(Array.from(row.children).indexOf(scrim)).toBeGreaterThan(
      Array.from(row.children).indexOf(main),
    );
    // (b) the grid lives in <main>, never inside the drawer.
    expect(rail().contains(wrap)).toBe(false);
    expect(main.contains(wrap)).toBe(true);
    // (c) the virtual grid wrapper is the same node, so loaded pages and the
    // measured-height cache survived opening the sheet.
    expect(gridWrap()).toBe(wrap);

    // (d) with the sheet OPEN the mounted row window still tracks scrollTop.
    const rowCount = rowCountFor(TOTAL_ASSETS);
    const offset = 1400;
    scrollGrid(main, offset);
    await waitFor(() => expect(mountedRowIndexes()).toEqual(expectedRows(offset, rowCount)));
    expect(mountedRowIndexes().length).toBeLessThan(rowCount);

    // (e) opening the sheet changed no ancestor of the grid wrapper.
    expect(ancestorChain(gridWrap())).toEqual(ancestorsBefore);

    // (f) closing the sheet keeps <main>, the grid wrapper and the scroll.
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(rail().hasAttribute("hidden")).toBe(true);
    expect(document.querySelector("main")).toBe(mainBefore);
    expect(gridWrap()).toBe(wrap);
    expect(main.scrollTop).toBe(offset);
  });

  it("bulk selection is untouched", async () => {
    const main = await renderFilesApp();
    const wrap = armVirtualiser(main);
    await waitFor(() => expect(cardCheckboxes().length).toBeGreaterThanOrEqual(2));

    const before = cardCheckboxes();
    const [first, second] = before;
    fireEvent.click(first);
    fireEvent.click(second);
    expect(screen.getByText("2 selected")).toBeTruthy();
    const selectedIds = [first, second].map(
      checkbox => checkbox.closest("[data-asset-id]")!.getAttribute("data-asset-id"),
    );

    openDrawer();
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });

    expect(rail().hasAttribute("hidden")).toBe(true);
    expect(screen.getByText("2 selected")).toBeTruthy();
    const after = cardCheckboxes();
    // Same nodes in the same order: nothing in the grid was remounted.
    expect(after).toHaveLength(before.length);
    expect(after[0]).toBe(first);
    expect(after[1]).toBe(second);
    expect(after[0].checked).toBe(true);
    expect(after[1].checked).toBe(true);
    expect(after.filter(checkbox => checkbox.checked)).toHaveLength(2);
    expect(
      after
        .slice(0, 2)
        .map(checkbox => checkbox.closest("[data-asset-id]")!.getAttribute("data-asset-id")),
    ).toEqual(selectedIds);
    // Same wrapper: nothing remounted, so loaded pages survived too.
    expect(gridWrap()).toBe(wrap);
  });

  it("wide is unchanged", async () => {
    const main = await renderFilesApp({ narrow: false });

    expect(screen.queryByRole("button", { name: "Folders" })).toBeNull();
    expect(screen.queryByRole("button", { name: "More destinations" })).toBeNull();
    const railEl = rail();
    expect(railEl.hasAttribute("hidden")).toBe(false);
    expect(railEl.getAttribute("role")).toBeNull();
    expect(railEl.getAttribute("aria-modal")).toBeNull();
    // The wide rail is a landmark, not a dialog.
    expect(screen.getByRole("complementary", { name: "Files" })).toBeTruthy();
    expect(document.querySelector(".folder-drawer-scrim")).toBeNull();

    const wrap = armVirtualiser(main);
    await waitFor(() => expect(mountedRowIndexes().length).toBeGreaterThan(0));
    const rowCount = rowCountFor(TOTAL_ASSETS);
    for (const offset of [700, 2500]) {
      scrollGrid(main, offset);
      await waitFor(() => expect(mountedRowIndexes()).toEqual(expectedRows(offset, rowCount)));
      expect(mountedRowIndexes().length).toBeLessThan(rowCount);
    }
    expect(gridWrap()).toBe(wrap);
  });

  it("header destinations at narrow", async () => {
    await renderFilesApp();
    const trigger = screen.getByRole("button", { name: "More destinations" });
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    // The panel is mounted only while open, so its destinations do not exist.
    expect(screen.queryByRole("button", { name: "Settings" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Review" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Audit log" })).toBeNull();

    fireEvent.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getAllByRole("button", { name: "Settings" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Review" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Audit log" })).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(await screen.findByText("Settings view")).toBeTruthy();
    expect(document.activeElement).toBe(trigger);
    expect(document.activeElement).not.toBe(document.body);

    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("button", { name: "Audit log" }));
    expect(await screen.findByText("Audit view")).toBeTruthy();
    expect(document.activeElement).toBe(trigger);

    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    expect(await screen.findByText("Review view")).toBeTruthy();
    expect(document.activeElement).toBe(trigger);

    // Leaving Review unmounts the control that was clicked. A browser focuses
    // the button it is about to activate (jsdom does not), so focus it first to
    // reproduce the state the fallback exists for: the Review entry lives in the
    // closed disclosure, so the fallback must land on the disclosure's trigger,
    // never on <body>.
    const back = await screen.findByRole("button", { name: "Back to library" });
    back.focus();
    fireEvent.click(back);
    expect(await screen.findByText("Asset 1")).toBeTruthy();
    expect(document.activeElement).toBe(trigger);
    expect(document.activeElement).not.toBe(document.body);

    // The mode switch is still in the header and still switches mode.
    expect(screen.getByRole("radiogroup", { name: "Library mode" })).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));
    expect(window.localStorage.getItem(MODE_STORAGE_KEY)).toBe("projects");
  });

  it("Projects needs no drawer", async () => {
    stubNarrow(true);
    window.localStorage.setItem(MODE_STORAGE_KEY, "projects");
    render(<App />);

    expect(await screen.findByText("Project view")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Folders" })).toBeNull();
    const railEl = rail();
    expect(railEl.hasAttribute("hidden")).toBe(true);
    // The other reason for `hidden`, distinguishable in the DOM.
    expect(railEl.getAttribute("data-drawer-state")).toBe("hidden-mode");
    expect(railEl.getAttribute("role")).toBeNull();
    expect(railEl.getAttribute("aria-modal")).toBeNull();
    expect(document.querySelector(".folder-drawer-scrim")).toBeNull();
  });
});
