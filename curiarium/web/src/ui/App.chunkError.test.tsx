// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

// Reproduces App.mode.test.tsx's harness, with one difference below: a stale
// Settings chunk. Vitest wraps a throwing `vi.mock` factory in its own Error and
// keeps the browser TypeError only as `cause`, so this file mounts a stand-in
// chunk component that throws that same TypeError. The genuine rejected-lazy
// path is covered by ErrorBoundary.test.tsx.
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
    username: "admin",
    email: null,
    role: "admin",
    disabled: false,
    kind: "local",
  })),
  listProjects: listProjectsSpy,
  logout: vi.fn(async () => {}),
  refreshToken: vi.fn(),
}));

vi.mock("../lib/api/projects", () => ({ listProjects: listProjectsSpy }));

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

// The difference from App.mode.test.tsx: this lazy chunk is gone after a
// deploy, so the stand-in component throws the raw TypeError a rejected import
// would surface during render. The error React sees is the same one the real
// rejected-lazy path produces; ErrorBoundary.test.tsx covers that seam.
vi.mock("./settings/Settings", () => {
  function RejectingChunk() {
    throw new TypeError("Failed to fetch dynamically imported module");
  }
  return { default: RejectingChunk };
});

vi.mock("../lib/reload", () => ({
  reloadWithFreshChunks: vi.fn(),
  buildReloadUrl: vi.fn(),
}));

// Marker children: the subject here is the boundary around the routed views.
vi.mock("./Sidebar", () => ({
  default: () => <aside aria-label="Files">Files tree</aside>,
}));
vi.mock("./ProjectView", () => ({
  default: () => <div data-testid="project-view">Project view</div>,
}));
vi.mock("./asset-grid/AssetGrid", () => ({
  default: () => <div data-testid="asset-grid">Asset grid</div>,
}));
vi.mock("./review/ImportsReviewView", () => ({
  default: () => <div>Imports review view</div>,
}));
vi.mock("./AuditLog", () => ({ default: () => null }));
vi.mock("./UploadBar", () => ({ default: () => null }));
vi.mock("./UploadQueuePanel", () => ({ default: () => null }));
vi.mock("./Login", () => ({ default: () => null }));

import App from "./App";
import { storeToken } from "../lib/auth";
import { resetInitialMode } from "../lib/modeBootstrap";
import { reloadWithFreshChunks } from "../lib/reload";

const reloadMock = vi.mocked(reloadWithFreshChunks);

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

beforeEach(() => {
  installMemoryStorage();
  resetInitialMode();
  listProjectsSpy.mockReset();
  listProjectsSpy.mockResolvedValue([]);
  storeToken("access-token");
  // React logs every caught render error; keep that noise out of the report.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe("App chunk load failure", () => {
  it("shows a recoverable view for a rejected lazy chunk and keeps the shell", async () => {
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText("This view failed to load")).toBeTruthy();
    expect(screen.getByText(/updated since this page was opened/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reload app" })).toBeTruthy();

    // The boundary wraps the routed views, not the shell: <main> and the header
    // actions (including Log out) survive a dead view.
    expect(document.querySelector("main")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Log out" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Reload app" }));
    expect(reloadMock).toHaveBeenCalledTimes(1);
  });

  it("recovers the shell by switching to another view", async () => {
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
    expect(await screen.findByRole("alert")).toBeTruthy();

    // Files mode - the default boot, so the header control is "Back to files".
    fireEvent.click(screen.getByRole("button", { name: "Back to files" }));

    expect(screen.queryByRole("alert")).toBeNull();
    expect(await screen.findByTestId("asset-grid")).toBeTruthy();
  });
});
