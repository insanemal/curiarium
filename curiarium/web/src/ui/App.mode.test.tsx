// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// The children are mocked: these tests cover App's mode state, persistence and
// the lazy mount/hide contract, not the panes' own rendering.
vi.mock("../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  FORBIDDEN_MESSAGE: "Your role does not allow this action.",
  apiHealth: vi.fn(async () => ({ ok: true, auth_required: true })),
  fileUrl: vi.fn((rel: string) => rel),
  getApiBase: vi.fn(() => "http://localhost:8000"),
  // `missing` is part of AssetCounts: an un-updated literal renders the nav
  // badge as the string "undefined".
  getAssetCounts: vi.fn(async () => ({ total: 0, unfiled: 0, missing: 0 })),
  getMe: vi.fn(async () => ({
    id: "u1",
    username: "admin",
    email: null,
    role: "admin",
    disabled: false,
    kind: "local",
  })),
  listProjects: vi.fn(async () => []),
  logout: vi.fn(async () => {}),
  refreshToken: vi.fn(),
}));

// The queue's own hook runs when the stand-in view opens it; an empty run list
// keeps this file's subject (mode state) off the network.
vi.mock("../lib/api/settings", () => ({
  getImportRuns: vi.fn(async () => ({ runs: [], total: 0, limit: 1, offset: 0 })),
  getImportRun: vi.fn(),
  // App arms `useLibraryScan`; its pair comes from this domain module.
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

// The real Sidebar renders App's counts beside its two rows, so the mock prints
// them too: that is the only observable of the counts refresh (AC-5).
vi.mock("./Sidebar", async () => {
  const react = await import("react");
  return {
    default: ({
      totalCount,
      unfiledCount,
      unfiled,
      onSelectUnfiled,
      onOpenQueue,
    }: {
      totalCount?: number | null;
      unfiledCount?: number | null;
      unfiled?: boolean;
      onSelectUnfiled?: () => void;
      onOpenQueue?: (key: "imports") => void;
    }) =>
      react.createElement(
        "aside",
        { "aria-label": "Files", "data-unfiled": String(Boolean(unfiled)) },
        react.createElement("p", null, "Files tree"),
        react.createElement("p", { "data-testid": "total-count" }, totalCount == null ? "none" : String(totalCount)),
        react.createElement("p", { "data-testid": "unfiled-count" }, unfiledCount == null ? "none" : String(unfiledCount)),
        react.createElement("button", { type: "button", onClick: onSelectUnfiled }, "Select unfiled"),
        react.createElement(
          "button",
          { type: "button", onClick: () => onOpenQueue?.("imports") },
          "Open imports queue",
        ),
      ),
  };
});

vi.mock("./ProjectSidebar", async () => {
  const react = await import("react");
  return {
    default: ({
      onOpenSettings,
      onOpenQueue,
    }: {
      onOpenSettings?: () => void;
      onOpenQueue?: (key: "imports") => void;
    }) =>
      react.createElement(
        "aside",
        { "aria-label": "Projects" },
        react.createElement("p", null, "Projects list"),
        react.createElement("button", { type: "button", onClick: onOpenSettings }, "Open settings"),
        react.createElement(
          "button",
          { type: "button", onClick: () => onOpenQueue?.("imports") },
          "Open imports queue",
        ),
      ),
  };
});

vi.mock("./ProjectView", async () => {
  const react = await import("react");
  return { default: () => react.createElement("div", null, "Project view") };
});

// A real card move reaches App only through `onAssetsFiled`, so the stand-in
// offers exactly that callback.
vi.mock("./asset-grid/AssetGrid", async () => {
  const react = await import("react");
  return {
    default: ({ unfiled, onAssetsFiled }: { unfiled?: boolean; onAssetsFiled?: () => void }) =>
      react.createElement(
        "div",
        { "data-testid": "asset-grid", "data-unfiled": String(Boolean(unfiled)) },
        react.createElement("button", { type: "button", onClick: onAssetsFiled }, "File one asset"),
      ),
  };
});
vi.mock("./settings/Settings", async () => {
  const react = await import("react");
  // Settings is configuration only now: it has no scan action and receives no
  // `onSelectFolder`.
  return { default: () => react.createElement("div", null, "Settings view") };
});

// The folder scan is a work surface, so the "scan now hands a folder back to
// App" premise lives in the imports queue; Settings keeps no such control.
vi.mock("./review/ImportsReviewView", async () => {
  const react = await import("react");
  return {
    default: ({ onSelectFolder }: { onSelectFolder?: (id: string | null) => void }) =>
      react.createElement(
        "div",
        null,
        react.createElement("p", null, "Imports review view"),
        react.createElement(
          "button",
          { type: "button", onClick: () => onSelectFolder?.("folder-1") },
          "Scan now",
        ),
      ),
  };
});
vi.mock("./AuditLog", () => ({ default: () => null }));
vi.mock("./UploadBar", () => ({ default: () => null }));
vi.mock("./UploadQueuePanel", () => ({ default: () => null }));
vi.mock("./Login", () => ({ default: () => null }));

import App, { SettingsLoading } from "./App";
import { getAssetCounts, listProjects } from "../lib/api";
import { storeToken } from "../lib/auth";
import { MODE_STORAGE_KEY } from "../lib/mode";

const listProjectsMock = vi.mocked(listProjects);
const getAssetCountsMock = vi.mocked(getAssetCounts);

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
  listProjectsMock.mockClear();
  getAssetCountsMock.mockReset();
  getAssetCountsMock.mockResolvedValue({ total: 0, unfiled: 0, missing: 0 });
  storeToken("access-token");
});

afterEach(() => {
  cleanup();
});

describe("App library mode switch", () => {
  it("seeds the active pane from the persisted mode", async () => {
    window.localStorage.setItem(MODE_STORAGE_KEY, "projects");

    render(<App />);

    await waitFor(() => expect(listProjectsMock).toHaveBeenCalled());
    expect(screen.getByRole("complementary", { name: "Projects" })).toBeTruthy();
    expect(screen.queryByRole("complementary", { name: "Files" })).toBeNull();
    // The files pane has never been visited, so it is not mounted at all.
    expect(screen.queryByText("Files tree")).toBeNull();
  });

  it("persists the chosen mode and never unmounts a visited pane", async () => {
    render(<App />);

    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));

    expect(window.localStorage.getItem(MODE_STORAGE_KEY)).toBe("projects");
    expect(screen.getByRole("complementary", { name: "Projects" })).toBeTruthy();
    expect(screen.queryByRole("complementary", { name: "Files" })).toBeNull();
    // Still mounted, just hidden: its selection/search state survives.
    expect(screen.getByText("Files tree")).toBeTruthy();
    await waitFor(() => expect(listProjectsMock).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("radio", { name: "Files" }));

    expect(window.localStorage.getItem(MODE_STORAGE_KEY)).toBe("files");
    expect(screen.getByRole("complementary", { name: "Files" })).toBeTruthy();
    expect(screen.queryByRole("complementary", { name: "Projects" })).toBeNull();
    expect(screen.getByText("Projects list")).toBeTruthy();
  });

  it("renders a visible fallback while the lazy Settings chunk loads", () => {
    // Settings sits behind `React.lazy`, so on a cold load the route shows the
    // Suspense fallback. Racing that fallback inside App is order-dependent:
    // React keeps a resolved lazy component cached for the whole module, so any
    // earlier test that opened Settings makes the next mount render it
    // synchronously. Pinning the fallback's own output is deterministic.
    render(<SettingsLoading />);

    expect(screen.getByText("Loading settings…")).toBeTruthy();
  });

  it("replaces the fallback with the Settings view once the chunk resolves", async () => {
    window.localStorage.setItem(MODE_STORAGE_KEY, "projects");

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Open settings" }));

    expect(await screen.findByText("Settings view")).toBeTruthy();
    expect(screen.queryByText("Loading settings…")).toBeNull();
  });

  it("keeps Settings reachable while projects mode is active", async () => {
    window.localStorage.setItem(MODE_STORAGE_KEY, "projects");

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "Open settings" }));

    expect(await screen.findByText("Settings view")).toBeTruthy();
  });

  it("returns to files mode when a folder scan in the imports queue selects a folder", async () => {
    window.localStorage.setItem(MODE_STORAGE_KEY, "projects");

    render(<App />);

    // In projects mode the queue is reached from that sidebar, and the scan
    // action hands a folder back to App while the projects pane is active.
    fireEvent.click(await screen.findByRole("button", { name: "Open imports queue" }));
    fireEvent.click(await screen.findByRole("button", { name: "Scan now" }));

    expect(window.localStorage.getItem(MODE_STORAGE_KEY)).toBe("files");
    expect(screen.getByRole("complementary", { name: "Files" })).toBeTruthy();
    expect(screen.queryByRole("complementary", { name: "Projects" })).toBeNull();
  });

  it("selects the unfiled pile without a folder, and passes it to both panes", async () => {
    render(<App />);

    expect((await screen.findByTestId("asset-grid")).dataset.unfiled).toBe("false");

    fireEvent.click(screen.getByRole("button", { name: "Select unfiled" }));

    expect(screen.getByTestId("asset-grid").dataset.unfiled).toBe("true");
    expect(screen.getByRole("complementary", { name: "Files" }).dataset.unfiled).toBe("true");
  });

  // AC-5: a move is reconciled locally and never bumps `assetsVersion`, so the
  // count behind the sidebar row must be refreshed through `onAssetsFiled`.
  it("refreshes the unfiled count after a card is filed, without an assets-version bump", async () => {
    getAssetCountsMock
      // `missing` rides along in every counts payload so the nav badge never
      // renders "undefined".
      .mockResolvedValueOnce({ total: 5, unfiled: 2, missing: 0 })
      .mockResolvedValueOnce({ total: 5, unfiled: 1, missing: 0 });

    render(<App />);

    await waitFor(() =>
      expect(screen.getByTestId("unfiled-count").textContent).toBe("2"),
    );
    expect(screen.getByTestId("total-count").textContent).toBe("5");

    fireEvent.click(screen.getByRole("button", { name: "File one asset" }));

    await waitFor(() =>
      expect(screen.getByTestId("unfiled-count").textContent).toBe("1"),
    );
    expect(getAssetCountsMock).toHaveBeenCalledTimes(2);
  });

});
