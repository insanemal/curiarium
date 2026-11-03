// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// The children are mocked: these tests cover App's mode state, persistence and
// the lazy mount/hide contract, not the panes' own rendering.
//
// ONE spy serves both the barrel and the domain module below: `modeBootstrap`
// imports `listProjects` from `../lib/api/projects` while App imports it from
// the `../lib/api` barrel, and the acceptance counts both halves of the boot
// (the probe AND App's own effect). Two spies would let one of those calls hide
// from the assertion.
const { listProjectsSpy } = vi.hoisted(() => ({ listProjectsSpy: vi.fn() }));

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
  listProjects: listProjectsSpy,
  logout: vi.fn(async () => {}),
  refreshToken: vi.fn(),
}));

vi.mock("../lib/api/projects", () => ({ listProjects: listProjectsSpy }));

// The queue's own hook runs when the queue opens it; an empty run list keeps
// this file's subject (mode state) off the network.
vi.mock("../lib/api/settings", () => ({
  getImportRuns: vi.fn(async () => ({ runs: [], total: 0, limit: 1, offset: 0 })),
  getImportRun: vi.fn(),
  // App arms `useLibraryScan`; its pair comes from this domain module.
  getLibraryScanStatus: vi.fn(),
  startLibraryScan: vi.fn(),
}));

// Entering Review opens the stored queue first (a bundle decision for an
// admin), so the walk has to answer without touching the network.
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

// The real Sidebar renders App's counts beside its two rows, so the mock prints
// them too: that is the only observable of the counts refresh (AC-5). The
// review entry lives in the header, so nothing here needs a queue opener.
vi.mock("./Sidebar", async () => {
  const react = await import("react");
  return {
    default: ({
      totalCount,
      unfiledCount,
      unfiled,
      onSelectUnfiled,
    }: {
      totalCount?: number | null;
      unfiledCount?: number | null;
      unfiled?: boolean;
      onSelectUnfiled?: () => void;
    }) =>
      react.createElement(
        "aside",
        { "aria-label": "Files", "data-unfiled": String(Boolean(unfiled)) },
        react.createElement("p", null, "Files tree"),
        react.createElement("p", { "data-testid": "total-count" }, totalCount == null ? "none" : String(totalCount)),
        react.createElement("p", { "data-testid": "unfiled-count" }, unfiledCount == null ? "none" : String(unfiledCount)),
        react.createElement("button", { type: "button", onClick: onSelectUnfiled }, "Select unfiled"),
      ),
  };
});

vi.mock("./ProjectView", async () => {
  const react = await import("react");
  // A stateful stand-in: the lazy-mount contract is "a visited pane stays
  // mounted (only hidden)", so the pane must own state that a switch preserves.
  // It also prints the project count it was handed and offers the assets-change
  // callback App wires to the `assetsVersion` bump (AC-5).
  function ProjectViewStandIn({
    projects,
    onAssetsChanged,
  }: {
    projects?: Project[] | null;
    onAssetsChanged?: () => void;
  }) {
    const [query, setQuery] = react.useState("");
    return react.createElement(
      "div",
      { "data-testid": "project-view" },
      react.createElement("p", null, "Project view"),
      react.createElement(
        "p",
        { "data-testid": "project-count" },
        projects == null ? "none" : String(projects.length),
      ),
      react.createElement("button", { type: "button", onClick: onAssetsChanged }, "Bump assets"),
      react.createElement("input", {
        "aria-label": "Project search stand-in",
        value: query,
        onChange: (event: { target: { value: string } }) => setQuery(event.target.value),
      }),
    );
  }
  return { default: ProjectViewStandIn };
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
import { resetInitialMode, resolveInitialMode } from "../lib/modeBootstrap";
import type { Project } from "../lib/api/projects";

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
  // The boot decision is memoised module-wide, so one test's probe would
  // otherwise answer the next test's `resolveInitialMode()`.
  resetInitialMode();
  listProjectsSpy.mockReset();
  listProjectsSpy.mockResolvedValue([]);
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
    // Projects mode hides the Files rail outright, so it is not a landmark.
    expect(screen.queryByRole("complementary", { name: "Files" })).toBeNull();
    expect(screen.getByTestId("project-view")).toBeTruthy();
    // The files pane has never been visited, so it is not mounted at all.
    expect(screen.queryByText("Files tree")).toBeNull();
  });

  it("persists the chosen mode and never unmounts a visited pane", async () => {
    render(<App />);

    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));

    expect(window.localStorage.getItem(MODE_STORAGE_KEY)).toBe("projects");
    expect(screen.queryByRole("complementary", { name: "Files" })).toBeNull();
    // Still mounted, just hidden: its selection/search state survives.
    expect(screen.getByText("Files tree").closest("[hidden]")).toBeTruthy();
    await waitFor(() => expect(listProjectsMock).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Project search stand-in"), {
      target: { value: "hydra" },
    });

    fireEvent.click(screen.getByRole("radio", { name: "Files" }));

    expect(window.localStorage.getItem(MODE_STORAGE_KEY)).toBe("files");
    expect(screen.getByRole("complementary", { name: "Files" })).toBeTruthy();
    expect(screen.getByTestId("project-view").closest("[hidden]")).toBeTruthy();
    // The pane stayed mounted, so its own state came back with it.
    expect((screen.getByLabelText("Project search stand-in") as HTMLInputElement).value).toBe(
      "hydra",
    );

    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));

    expect((screen.getByLabelText("Project search stand-in") as HTMLInputElement).value).toBe(
      "hydra",
    );
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

    fireEvent.click(await screen.findByRole("button", { name: "Settings" }));

    expect(await screen.findByText("Settings view")).toBeTruthy();
    expect(screen.queryByText("Loading settings…")).toBeNull();
  });

  it("keeps Settings reachable while projects mode is active", async () => {
    window.localStorage.setItem(MODE_STORAGE_KEY, "projects");

    render(<App />);

    // the header sits outside both panes, so there is exactly one entry point
    // and projects mode cannot strand it.
    expect(await screen.findAllByRole("button", { name: "Settings" })).toHaveLength(1);
    // the premise: we are in projects mode, whose pane does not need a rail.
    expect(screen.getByTestId("project-view")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));

    expect(await screen.findByText("Settings view")).toBeTruthy();
  });

  it("returns to files mode when a folder scan in the imports queue selects a folder", async () => {
    window.localStorage.setItem(MODE_STORAGE_KEY, "projects");

    render(<App />);

    // The queue is reached from the shared header, so it opens in projects mode
    // too; the scan action then hands a folder back to App.
    fireEvent.click(await screen.findByRole("button", { name: "Review" }));
    fireEvent.click(await screen.findByRole("tab", { name: /^Imports running/ }));
    expect(await screen.findByText("Imports review view")).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "Scan now" }));

    expect(window.localStorage.getItem(MODE_STORAGE_KEY)).toBe("files");
    expect(screen.getByRole("complementary", { name: "Files" })).toBeTruthy();
    // files mode is showing, so the projects pane is mounted but hidden.
    expect(screen.getByTestId("project-view").closest("[hidden]")).toBeTruthy();
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

  // The boot decision arrives as a prop and must be honoured by the FIRST
  // commit, and never re-decided when the project list lands.
  it("renders the boot mode on the first commit and keeps it when the list resolves empty", async () => {
    render(<App initialMode="projects" />);

    // Synchronous on purpose: nothing between render() and these assertions.
    expect(
      screen.getByRole("radio", { name: "Projects" }).getAttribute("aria-checked"),
    ).toBe("true");
    expect(screen.queryByRole("complementary", { name: "Files" })).toBeNull();

    // The default mock resolves `[]`. An empty library must not flip the mode.
    await waitFor(() => expect(listProjectsMock).toHaveBeenCalled());
    expect(
      screen.getByRole("radio", { name: "Projects" }).getAttribute("aria-checked"),
    ).toBe("true");
    expect(screen.queryByRole("complementary", { name: "Files" })).toBeNull();
  });

  // Anti-flip guard: App must never consult the project list to decide the
  // mode. Only lib/modeBootstrap decides, once, before the first paint. An
  // implementation that decided from `listProjects()` in an effect would call
  // the mock and flip this boot-files render over to projects.
  it("never consults the project list to decide the mode", async () => {
    listProjectsMock.mockResolvedValue([
      { id: "p1", name: "Hydra" },
      { id: "p2", name: "Basilisk" },
    ] as unknown as Project[]);

    render(<App initialMode="files" />);

    // Synchronous: the boot mode is already what is committed.
    expect(screen.getByRole("radio", { name: "Files" }).getAttribute("aria-checked")).toBe(
      "true",
    );
    expect(screen.getByRole("complementary", { name: "Files" })).toBeTruthy();
    expect(listProjectsMock).not.toHaveBeenCalled();

    await waitFor(() =>
      expect(
        screen.getByRole("radio", { name: "Files" }).getAttribute("aria-checked"),
      ).toBe("true"),
    );
    expect(listProjectsMock).not.toHaveBeenCalled();
  });

  // The fresh-boot path: modeBootstrap already paid for `GET /projects`, and
  // App seeds its state from that list. Under StrictMode a take-once read in a
  // useState initialiser would ALSO show 2 items - but only because two fetches
  // happened - so this test is what pins purity: the count below includes the
  // boot probe, and it must stay at one.
  it("seeds the grid from the boot list, so a fresh boot costs one GET /projects", async () => {
    listProjectsSpy.mockResolvedValue([{ id: "p1" }, { id: "p2" }] as unknown as Project[]);

    const boot = await resolveInitialMode();
    expect(boot.mode).toBe("projects");
    expect(listProjectsSpy).toHaveBeenCalledTimes(1);

    render(
      <React.StrictMode>
        <App initialMode={boot.mode} initialProjects={boot.projects} />
      </React.StrictMode>,
    );

    await waitFor(() => expect(screen.getByTestId("project-count").textContent).toBe("2"));
    // One call total: the boot probe. App did not read the list a second time.
    expect(listProjectsSpy).toHaveBeenCalledTimes(1);
  });

  // AC-5: the seed guard must not freeze the list. A mutation bumps
  // `assetsVersion`, which makes the seeded list stale, so the effect refetches.
  it("refetches the project list after an assets bump, despite the seed", async () => {
    listProjectsSpy.mockResolvedValue([{ id: "p1" }] as unknown as Project[]);

    const boot = await resolveInitialMode();
    render(<App initialMode={boot.mode} initialProjects={boot.projects} />);

    await waitFor(() => expect(screen.getByTestId("project-count").textContent).toBe("1"));
    expect(listProjectsSpy).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Bump assets" }));

    await waitFor(() => expect(listProjectsSpy).toHaveBeenCalledTimes(2));
  });
});
