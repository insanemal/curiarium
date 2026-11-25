// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// Same scaffolding as App.missing.test.tsx: this file mounts the real App, so
// the barrel has to carry every name its import graph reaches.
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

// Entering Review arms the resolved queue, so both neighbours are stubbed with
// empty answers: no assertion in this file may shift because one fetched.
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

// The shell's hook reads its pair from the DOMAIN module; the card's Re-scan
// reaches the barrel instead, so both are stubbed.
vi.mock("../lib/api/settings", () => ({
  getImportRuns: vi.fn(async () => ({ runs: [], total: 0, limit: 1, offset: 0 })),
  getImportRun: vi.fn(),
  getLibraryScanStatus: vi.fn(),
  startLibraryScan: vi.fn(),
}));

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

vi.mock("./Sidebar", async () => {
  const react = await import("react");
  return { default: () => react.createElement("aside", { "aria-label": "Files" }) };
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
import { getAssetCounts, getMe, listAssets, listCreators, listFolders, listTags, type Me } from "../lib/api";
import {
  getLibraryScanStatus as readScanStatus,
  startLibraryScan as startShellScan,
  type LibraryScanStatus,
} from "../lib/api/settings";
import { storeToken, type Role } from "../lib/auth";
import { MODE_STORAGE_KEY } from "../lib/mode";

const getAssetCountsMock = vi.mocked(getAssetCounts);
const getMeMock = vi.mocked(getMe);
const listAssetsMock = vi.mocked(listAssets);
const listTagsMock = vi.mocked(listTags);
const listCreatorsMock = vi.mocked(listCreators);
const listFoldersMock = vi.mocked(listFolders);
const getScanStatusMock = vi.mocked(readScanStatus);
const startScanMock = vi.mocked(startShellScan);

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
  window.localStorage.setItem(MODE_STORAGE_KEY, "files");
  storeToken("access-token");

  getMeMock.mockReset();
  getMeMock.mockResolvedValue(me("editor"));

  getAssetCountsMock.mockReset();
  getAssetCountsMock.mockResolvedValue({ total: 9, unfiled: 0, missing: 1 });

  listAssetsMock.mockReset();
  listAssetsMock.mockResolvedValue({ items: [], hasMore: false });
  listTagsMock.mockReset();
  listTagsMock.mockResolvedValue([]);
  listCreatorsMock.mockReset();
  listCreatorsMock.mockResolvedValue([]);
  listFoldersMock.mockReset();
  listFoldersMock.mockResolvedValue([]);

  getScanStatusMock.mockReset();
  getScanStatusMock.mockResolvedValue(makeStatus());
  startScanMock.mockReset();
  startScanMock.mockResolvedValue(makeStatus({ running: true, finished_at: null }));
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

describe("App storage scan from the cold library header (Mf-7mio)", () => {
  it("starts a scan from the header and polls it to the last-scan line", async () => {
    getMeMock.mockResolvedValue(me("editor"));
    startScanMock.mockResolvedValue(makeStatus({ running: true, finished_at: null }));
    getScanStatusMock
      // Entry read: the scan just started.
      .mockResolvedValueOnce(makeStatus({ running: true, finished_at: null }))
      // The poll after POLL_MS: the scan finished.
      .mockResolvedValue(makeStatus({ finished_at: 200, missing: 1 }));

    await renderApp();
    await settle(30);

    // Nothing is fetched on app load: the header action only renders.
    expect(getScanStatusMock).not.toHaveBeenCalled();

    const headerAction = screen.getByRole("button", { name: "Rescan storage folder" });
    expect(headerAction.closest('[data-scan-surface="header"]')).toBeTruthy();
    // The cold library shows exactly one scan surface - the header one.
    expect(screen.queryAllByText("Rescan storage folder")).toHaveLength(1);

    // Mutation note for AC-1: dropping `scanArmed` from `enabled` must red BOTH
    // halves below - the line never leaves "Scan in progress" AND no
    // "Last scan:" ever appears - because the poll loop never arms.
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      fireEvent.click(headerAction);
      await flush();

      expect(startScanMock).toHaveBeenCalledTimes(1);
      expect(screen.getByText("Scan in progress")).toBeTruthy();
      expect(screen.queryByText(/Last scan:/)).toBeNull();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1600);
      });
      await flush();

      expect(
        screen.getByText("Last scan: 3 new, 1 known, 1 missing, 0 failed"),
      ).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it("gives a viewer no header action but still reads the status on queue entry", async () => {
    getMeMock.mockResolvedValue(me("viewer"));

    await renderApp();
    await settle(30);

    // Absent from the DOM, not disabled: the scan is `POST /library/scan`.
    expect(screen.queryByRole("button", { name: /rescan storage folder/i })).toBeNull();
    expect(document.querySelector('[data-scan-surface="header"]')).toBeNull();
    expect(startScanMock).not.toHaveBeenCalled();
    expect(getScanStatusMock).not.toHaveBeenCalled();

    // A viewer's Review entry resolves to imports; the missing queue is a tab
    // away, and its entry read stays READ.
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    await waitFor(() => expect(screen.getByRole("tabpanel").id).toBe("review-tabpanel-imports"));
    fireEvent.click(await screen.findByRole("tab", { name: /^Missing files/ }));
    await screen.findByRole("heading", { name: "Missing files" });

    await waitFor(() => expect(getScanStatusMock).toHaveBeenCalledTimes(1));
    expect(startScanMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /rescan storage folder/i })).toBeNull();
  });
});
