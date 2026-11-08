// @vitest-environment jsdom
// Mf-flt1: `handleProjectImported` opens the project before App's first project
// list has resolved, so ProjectView mounts with `projects === null` and no Back
// button to focus. The list arriving does not change the open/close effect's
// id, so the fix keys the open side on the Back button's presence instead. This
// test drives a genuinely deferred list (no seeded project list) and reads the
// ordering fact - the open view is on screen while the list is still on the
// wire - because a resolved-list variant cannot tell the two implementations
// apart.
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// The real `UploadBar` and `ProjectView` run in this file: the upload is the
// trigger under test and the view is where focus lands. Every other child is
// stubbed the way `App.imports.test.tsx` stubs it.
vi.mock("../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  FORBIDDEN_MESSAGE: "Your role does not allow this action.",
  addProjectAssets: vi.fn(),
  apiHealth: vi.fn(async () => ({ ok: true, auth_required: true })),
  assetFilePreviewUrl: vi.fn((rel: string) => rel),
  createFolder: vi.fn(),
  createProject: vi.fn(),
  deleteProject: vi.fn(),
  downloadZip: vi.fn(),
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
  importFromLink: vi.fn(),
  importToProject: vi.fn(),
  importZipFromLink: vi.fn(),
  inspectImportLink: vi.fn(),
  listAssets: vi.fn(async () => ({ items: [], hasMore: false })),
  listImportZipEntries: vi.fn(),
  listProjectAssets: vi.fn(async () => ({ items: [], hasMore: false })),
  listProjects: vi.fn(),
  logout: vi.fn(async () => {}),
  refreshToken: vi.fn(),
  stableFileUrl: vi.fn((rel: string) => rel),
  updateProject: vi.fn(),
  uploadAsset: vi.fn(),
}));

// An editor's Review entry resolves to the bundle queue, so `useBundleDetect`
// arms on entry. Nothing here enters Review; the empty report keeps that door
// shut without touching the network.
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

vi.mock("../lib/api/folders", () => ({
  createFolder: vi.fn(),
}));

vi.mock("../lib/settings", async importOriginal => ({
  ...(await importOriginal<typeof import("../lib/settings")>()),
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

vi.mock("./Sidebar", async () => {
  const react = await import("react");
  return {
    default: () =>
      react.createElement("aside", { "aria-label": "Files" }, "Files tree"),
  };
});

vi.mock("./asset-grid/AssetGrid", async () => {
  const react = await import("react");
  return { default: () => react.createElement("div", null, "Asset grid") };
});
vi.mock("./settings/Settings", async () => {
  const react = await import("react");
  return { default: () => react.createElement("div", null, "Settings view") };
});
vi.mock("./AuditLog", () => ({ default: () => null }));
vi.mock("./UploadQueuePanel", () => ({ default: () => null }));
vi.mock("./Login", () => ({ default: () => null }));

import App from "./App";
import {
  getMe,
  importToProject,
  inspectImportLink,
  listProjects,
  type Asset,
  type ImportProjectResult,
  type Project,
} from "../lib/api";
import { storeToken } from "../lib/auth";

const getMeMock = vi.mocked(getMe);
const importToProjectMock = vi.mocked(importToProject);
const inspectImportLinkMock = vi.mocked(inspectImportLink);
const listProjectsMock = vi.mocked(listProjects);

const LINK_URL = "https://www.printables.com/model/1-hydra";
const LINK_INPUT = "Paste model link (MakerWorld, Printables, Thingiverse...)";

function asset(name: string): Asset {
  return {
    id: `asset-${name}`,
    name,
    filename: name,
    mime: "model/stl",
    size: 1,
    tags: [],
    url: `/assets/${name}`,
    supporting_file_count: 0,
  };
}

function project(id: string, name: string): Project {
  return {
    id,
    name,
    description: null,
    creator: "Ada",
    source_url: LINK_URL,
    licence: "CC-BY",
    licence_url: null,
    origin: "imported",
    cover_asset_id: null,
    cover_thumb_url: null,
    asset_count: 1,
    metadata_json: {},
    total_size: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
  };
}

function projectResult(overrides: Partial<ImportProjectResult> = {}): ImportProjectResult {
  return {
    project: project("p9", "Hydra"),
    created: true,
    assets: [asset("model.stl")],
    added: ["model.stl"],
    skipped: [],
    failed: [],
    gated: false,
    message: "Imported Hydra.",
    ...overrides,
  };
}

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
  listProjectsMock.mockReset();
  inspectImportLinkMock.mockReset();
  importToProjectMock.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("App project focus after an import (Mf-flt1)", () => {
  it("focuses Back when the imported project opens before the first list resolves", async () => {
    const gate = deferred<Project[]>();
    // Every call gets the same pending list, so a stray refetch cannot resolve
    // to `undefined` and turn a focus assertion into a crash.
    listProjectsMock.mockReturnValue(gate.promise);
    inspectImportLinkMock.mockResolvedValue({
      filename: "model.stl",
      mime: "model/stl",
      is_zip: false,
    });
    importToProjectMock.mockResolvedValue(projectResult());

    render(<App />);
    // The shell is painted once the role resolves; the empty storage leaves the
    // mode on files, which is where the upload bar lives.
    await screen.findByRole("button", { name: "Review" });

    fireEvent.change(screen.getByPlaceholderText(LINK_INPUT), { target: { value: LINK_URL } });
    fireEvent.click(screen.getByRole("radio", { name: "New project from the page" }));
    fireEvent.click(screen.getByRole("button", { name: "Import link" }));

    await waitFor(() => expect(listProjectsMock).toHaveBeenCalledTimes(1));
    // The ordering fact: the open id is already set while the list is genuinely
    // still on the wire, so ProjectView is mounted without a Back button.
    expect(screen.getByText("Loading projects…")).toBeTruthy();

    await act(async () => {
      gate.resolve([project("p9", "Hydra")]);
    });

    const back = screen.getByRole("button", { name: "Back to projects" });
    expect(document.activeElement).toBe(back);
    expect(document.activeElement).not.toBe(document.body);
  });
});
