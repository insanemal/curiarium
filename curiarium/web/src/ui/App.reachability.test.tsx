// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// App's own imports from the barrel, as a fixed object. Every queue hook
// reaches the API through a domain module (mocked separately below), so a new
// barrel export would break this file rather than silently read a second mock.
vi.mock("../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  FORBIDDEN_MESSAGE: "Your role does not allow this action.",
  apiHealth: vi.fn(async () => ({ ok: true, auth_required: true })),
  fileUrl: vi.fn((rel: string) => rel),
  getApiBase: vi.fn(() => "http://localhost:8000"),
  getAssetCounts: vi.fn(async () => ({ total: 0, unfiled: 0, missing: 0 })),
  getMe: vi.fn(),
  listProjects: vi.fn(async () => []),
  logout: vi.fn(async () => {}),
  refreshToken: vi.fn(),
}));

// The four queue hooks read their own endpoints from these domain modules; the
// shapes are the real ones so a hook may render its count without throwing.
vi.mock("../lib/api/bundles", () => ({
  getBundleReport: vi.fn(),
  createBundles: vi.fn(),
  setBundleDismissed: vi.fn(),
}));

vi.mock("../lib/api/settings", () => ({
  getImportRuns: vi.fn(),
  getImportRun: vi.fn(),
  getLibraryScanStatus: vi.fn(),
  startLibraryScan: vi.fn(),
}));

vi.mock("../lib/api/trash", () => ({
  getTrash: vi.fn(),
  purgeTrash: vi.fn(),
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

// Both rails are deliberately empty: nothing in this file may be carried by a
// sidebar, so the header has to be the only carrier of every destination.
vi.mock("./Sidebar", () => ({ default: () => null }));
vi.mock("./ProjectSidebar", () => ({ default: () => null }));

// One distinct marker per destination, so a cell can name what rendered without
// driving the real surface.
vi.mock("./ProjectView", async () => {
  const react = await import("react");
  return {
    default: () => react.createElement("div", { "data-testid": "project-view" }, "Project view"),
  };
});
vi.mock("./asset-grid/AssetGrid", async () => {
  const react = await import("react");
  return { default: () => react.createElement("div", { "data-testid": "asset-grid" }, "Asset grid") };
});
vi.mock("./settings/Settings", async () => {
  const react = await import("react");
  return {
    default: () => react.createElement("div", { "data-testid": "settings-view" }, "Settings view"),
  };
});
vi.mock("./AuditLog", async () => {
  const react = await import("react");
  return { default: () => react.createElement("div", { "data-testid": "audit-view" }, "Audit view") };
});
vi.mock("./UploadBar", () => ({ default: () => null }));
vi.mock("./UploadQueuePanel", () => ({ default: () => null }));
vi.mock("./Login", () => ({ default: () => null }));

// The four queue views are markers only: a cell proves which queue the App
// mounted, and the hooks that fetch stay App-owned and real.
vi.mock("./review/BundleReviewView", async () => {
  const react = await import("react");
  return {
    default: () =>
      react.createElement("div", { "data-testid": "bundle-review-view" }, "Bundle queue"),
  };
});
vi.mock("./review/ImportsReviewView", async () => {
  const react = await import("react");
  return {
    default: () =>
      react.createElement("div", { "data-testid": "imports-review-view" }, "Imports queue"),
  };
});
vi.mock("./review/MissingFilesReviewView", async () => {
  const react = await import("react");
  return {
    default: () =>
      react.createElement("div", { "data-testid": "missing-review-view" }, "Missing queue"),
  };
});
vi.mock("./review/TrashQueue", async () => {
  const react = await import("react");
  return {
    default: () => react.createElement("div", { "data-testid": "trash-review-view" }, "Trash queue"),
  };
});

import App from "./App";
import { getMe, type Me } from "../lib/api";
import { getBundleReport, type BundleReport } from "../lib/api/bundles";
import {
  getImportRuns,
  getLibraryScanStatus,
  type ImportRunList,
  type LibraryScanStatus,
} from "../lib/api/settings";
import { getTrash, type TrashList } from "../lib/api/trash";
import { storeToken, type Role } from "../lib/auth";
import { MODE_STORAGE_KEY, type LibraryMode } from "../lib/mode";
import type { ReviewQueueKey } from "./views";

const getMeMock = vi.mocked(getMe);
const getBundleReportMock = vi.mocked(getBundleReport);
const getImportRunsMock = vi.mocked(getImportRuns);
const getLibraryScanStatusMock = vi.mocked(getLibraryScanStatus);
const getTrashMock = vi.mocked(getTrash);

// The table the acceptance asks for: 8 destinations, 2 modes, the 4 real roles.
const MODES: readonly LibraryMode[] = ["files", "projects"];
const ROLES: readonly Role[] = ["viewer", "contributor", "editor", "admin"];

type Destination =
  | { name: string; kind: "mode"; mode: LibraryMode }
  | { name: string; kind: "queue"; queue: ReviewQueueKey }
  | { name: string; kind: "settings" }
  | { name: string; kind: "audit" };

const DESTINATIONS: readonly Destination[] = [
  { name: "Files", kind: "mode", mode: "files" },
  { name: "Projects", kind: "mode", mode: "projects" },
  { name: "Review: bundles", kind: "queue", queue: "bundles" },
  { name: "Review: imports", kind: "queue", queue: "imports" },
  { name: "Review: missing", kind: "queue", queue: "missing" },
  { name: "Review: trash", kind: "queue", queue: "trash" },
  { name: "Settings", kind: "settings" },
  { name: "Audit", kind: "audit" },
];

const QUEUE_ORDER: readonly ReviewQueueKey[] = ["bundles", "imports", "missing", "trash"];

const QUEUE_LABELS: Record<ReviewQueueKey, string> = {
  bundles: "Bundles to confirm",
  imports: "Imports running",
  missing: "Missing files",
  trash: "Trash",
};

const QUEUE_MARKERS: Record<ReviewQueueKey, string> = {
  bundles: "bundle-review-view",
  imports: "imports-review-view",
  missing: "missing-review-view",
  trash: "trash-review-view",
};

// Written out literally rather than derived from `visibleQueues`, so the test
// states the threshold table instead of echoing the implementation of it.
const VISIBLE_QUEUES: Record<Role, readonly ReviewQueueKey[]> = {
  viewer: ["imports", "missing", "trash"],
  contributor: ["bundles", "imports", "missing", "trash"],
  editor: ["bundles", "imports", "missing", "trash"],
  admin: ["bundles", "imports", "missing", "trash"],
};

// The stored queue key defaults to `bundles`, and App resolves it against the
// role: a viewer lands on `imports`, everyone above it on `bundles`.
const RESOLVED_QUEUE: Record<Role, ReviewQueueKey> = {
  viewer: "imports",
  contributor: "bundles",
  editor: "bundles",
  admin: "bundles",
};

const EMPTY_REPORT: BundleReport = {
  root: "",
  truncated: false,
  counts: { total: 0, high: 0, low: 0, ready: 0, blocked: 0, dismissed: 0, already_created: 0 },
  bundles: [],
  stale_dismissals: [],
};

const EMPTY_RUNS: ImportRunList = { runs: [], total: 0, limit: 1, offset: 0 };

const FINISHED_SCAN: LibraryScanStatus = {
  running: false,
  started_at: 1_700_000_000,
  finished_at: 1_700_000_010,
  registered: 0,
  known: 0,
  ignored: 0,
  missing: 0,
  failed: 0,
  errors: [],
};

const EMPTY_TRASH: TrashList = { entries: [], total_size: 0 };

function me(role: Role): Me {
  return { id: "u1", username: role, email: null, role, disabled: false, kind: "local" };
}

// A `/me` that answers with a role the web app does not know: the session is
// valid but holds no rank, which is the honest way to reach `role = null` with
// a positive landing signal. A 403 leaves no observable change to await.
const UNKNOWN_ROLE_ME: Me = { ...me("viewer"), username: "guest", role: "guest" };

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
  Object.defineProperty(window, "localStorage", { configurable: true, value: storage });
}

/** Seeds the persisted mode, then renders and waits for `/me` to land. */
async function renderApp(mode: LibraryMode, role: Role): Promise<HTMLElement> {
  window.localStorage.setItem(MODE_STORAGE_KEY, mode);
  getMeMock.mockResolvedValue(me(role));
  const { container } = render(<App />);
  // The header's Review destination is both the positive signal that `/me`
  // landed and the control this matrix is about: waiting on it fails loudly
  // when no such control is rendered at all, which is the regression this file
  // exists to catch. The role chip then confirms which role answered.
  await screen.findByRole("button", { name: "Review" });
  await waitFor(() => expect(screen.getByTestId("signed-in-role").textContent).toBe(role));
  return container;
}

/** Seeds the persisted mode, then renders with a session that holds no role. */
async function renderAppWithUnknownRole(mode: LibraryMode): Promise<HTMLElement> {
  window.localStorage.setItem(MODE_STORAGE_KEY, mode);
  getMeMock.mockResolvedValue(UNKNOWN_ROLE_ME);
  const { container } = render(<App />);
  // `signed-in-username` still renders because `/me` answered; the role chip is
  // what is absent, and that is asserted in the cell.
  await waitFor(() => expect(screen.getByTestId("signed-in-username").textContent).toBe("guest"));
  return container;
}

/** Resolves after real timers have had a chance to fire anything they hold. */
function settle(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function expectSeededMode(mode: LibraryMode, context: string): void {
  const checked = screen
    .getAllByRole("radio")
    .filter(radio => radio.getAttribute("aria-checked") === "true");
  expect(checked, `${context}: exactly one mode radio is checked`).toHaveLength(1);
  expect(checked[0].textContent, `${context}: the seeded mode is the visible one`).toBe(
    mode === "files" ? "Files" : "Projects",
  );
}

/** The queue views the App actually mounted, in table order. */
function renderedQueueMarkers(): ReviewQueueKey[] {
  return QUEUE_ORDER.filter(queue => screen.queryByTestId(QUEUE_MARKERS[queue]) !== null);
}

function expectQueueTabs(role: Role, context: string): void {
  const tabs = screen.getAllByRole("tab");
  expect(
    tabs.map(tab => tab.dataset.queue),
    `${context}: the tab set is exactly the role's threshold set`,
  ).toEqual(VISIBLE_QUEUES[role]);
  for (const queue of QUEUE_ORDER) {
    const expected = VISIBLE_QUEUES[role].includes(queue) ? 1 : 0;
    expect(
      screen.queryAllByRole("tab", { name: new RegExp(`^${QUEUE_LABELS[queue]}`) }),
      `${context}: queue ${queue} is present iff the role meets its threshold`,
    ).toHaveLength(expected);
  }
}

function expectSelectedQueue(queue: ReviewQueueKey, context: string): void {
  const selected = screen
    .getAllByRole("tab")
    .filter(tab => tab.getAttribute("aria-selected") === "true");
  expect(selected, `${context}: exactly one tab is selected`).toHaveLength(1);
  expect(selected[0].dataset.queue, `${context}: the selected tab is ${queue}`).toBe(queue);
  expect(screen.getAllByRole("tabpanel"), `${context}: exactly one tabpanel`).toHaveLength(1);
  const panel = screen.getByRole("tabpanel");
  expect(panel.id, `${context}: the panel names the selected queue`).toBe(
    `review-tabpanel-${queue}`,
  );
  expect(panel.getAttribute("aria-labelledby"), `${context}: the panel is labelled by its tab`).toBe(
    `review-tab-${queue}`,
  );
}

function expectQueueMarker(queue: ReviewQueueKey, context: string): void {
  expect(renderedQueueMarkers(), `${context}: exactly the ${queue} queue view rendered`).toEqual([
    queue,
  ]);
}

/** Clicks the header's Review entry, which exists iff the role has a queue. */
async function openReview(context: string): Promise<void> {
  const entries = screen.getAllByRole("button", { name: "Review" });
  expect(entries, `${context}: exactly one Review entry`).toHaveLength(1);
  fireEvent.click(entries[0]);
  await screen.findByRole("tablist", { name: "Review" });
  expect(
    screen.getAllByRole("tablist"),
    `${context}: exactly one tablist while Review is open`,
  ).toHaveLength(1);
  expect(
    screen.getByRole("button", { name: "Review" }).getAttribute("aria-current"),
    `${context}: the Review entry is marked current while the destination is open`,
  ).toBe("true");
}

beforeEach(() => {
  installMemoryStorage();
  storeToken("access-token");
  getMeMock.mockReset();
  getBundleReportMock.mockReset();
  getBundleReportMock.mockResolvedValue(EMPTY_REPORT);
  getImportRunsMock.mockReset();
  getImportRunsMock.mockResolvedValue(EMPTY_RUNS);
  getLibraryScanStatusMock.mockReset();
  getLibraryScanStatusMock.mockResolvedValue(FINISHED_SCAN);
  getTrashMock.mockReset();
  getTrashMock.mockResolvedValue(EMPTY_TRASH);
});

afterEach(() => {
  cleanup();
});

async function assertCell(
  role: Role,
  mode: LibraryMode,
  destination: Destination,
): Promise<void> {
  const context = `${destination.name} in ${mode} mode as ${role}`;
  await renderApp(mode, role);

  // ac-1: the header exists once in both modes, whatever the role.
  expect(
    screen.getAllByRole("radiogroup", { name: "Library mode" }),
    `${context}: one mode switch`,
  ).toHaveLength(1);
  expect(screen.getAllByRole("radio", { name: "Files" }), `${context}: one Files radio`).toHaveLength(
    1,
  );
  expect(
    screen.getAllByRole("radio", { name: "Projects" }),
    `${context}: one Projects radio`,
  ).toHaveLength(1);
  expectSeededMode(mode, context);
  expect(screen.getAllByRole("button", { name: "Settings" }), `${context}: one Settings`).toHaveLength(
    1,
  );
  const auditEntries = screen.queryAllByRole("button", { name: "Audit log" });
  expect(auditEntries, `${context}: Audit log is admin-only`).toHaveLength(
    role === "admin" ? 1 : 0,
  );

  switch (destination.kind) {
    case "mode": {
      const label = destination.mode === "files" ? "Files" : "Projects";
      expect(
        screen.getAllByRole("radio", { name: label }),
        `${context}: one ${label} radio`,
      ).toHaveLength(1);
      fireEvent.click(screen.getByRole("radio", { name: label }));
      const pane = await screen.findByTestId(
        destination.mode === "files" ? "asset-grid" : "project-view",
      );
      expect(
        pane.closest("[hidden]"),
        `${context}: activating ${label} makes its own pane the visible one`,
      ).toBeNull();
      expect(
        screen.queryAllByRole("tablist"),
        `${context}: no tablist while browsing`,
      ).toHaveLength(0);
      return;
    }
    case "queue": {
      const queue = destination.queue;
      const visible = VISIBLE_QUEUES[role].includes(queue);
      await openReview(context);
      expectQueueTabs(role, context);
      // The latent-bug guard: entering Review lands on the resolved queue, not
      // on the stored `bundles` a viewer can never see.
      expectSelectedQueue(RESOLVED_QUEUE[role], context);
      const queueTabs = screen.queryAllByRole("tab", {
        name: new RegExp(`^${QUEUE_LABELS[queue]}`),
      });
      if (!visible) {
        // asserted honestly: the AC's "queue set is exactly the threshold set".
        expect(queueTabs, `${context}: a queue below the threshold has no tab`).toHaveLength(0);
        expectQueueMarker(RESOLVED_QUEUE[role], context);
        expect(
          screen.queryByTestId(QUEUE_MARKERS[queue]),
          `${context}: the invisible queue never renders`,
        ).toBeNull();
        return;
      }
      expect(queueTabs, `${context}: the queue's own tab, once`).toHaveLength(1);
      fireEvent.click(queueTabs[0]);
      expectSelectedQueue(queue, context);
      const marker = await screen.findByTestId(QUEUE_MARKERS[queue]);
      expect(marker, `${context}: activating the tab renders the queue's own view`).toBeTruthy();
      expectQueueMarker(queue, context);
      return;
    }
    case "settings": {
      const entries = screen.getAllByRole("button", { name: "Settings" });
      expect(entries, `${context}: one Settings entry`).toHaveLength(1);
      fireEvent.click(entries[0]);
      expect(await screen.findByTestId("settings-view"), `${context}: Settings rendered`).toBeTruthy();
      expect(screen.queryAllByRole("tablist"), `${context}: no tablist in Settings`).toHaveLength(0);
      return;
    }
    case "audit": {
      if (role !== "admin") {
        expect(auditEntries, `${context}: a non-admin has no Audit entry`).toHaveLength(0);
        expect(screen.queryByTestId("audit-view"), `${context}: and no audit page`).toBeNull();
        expect(screen.queryAllByRole("tablist"), `${context}: no tablist while browsing`).toHaveLength(0);
        return;
      }
      expect(auditEntries, `${context}: one Audit entry for an admin`).toHaveLength(1);
      fireEvent.click(auditEntries[0]);
      expect(await screen.findByTestId("audit-view"), `${context}: the audit page rendered`).toBeTruthy();
      expect(screen.queryAllByRole("tablist"), `${context}: no tablist in Audit`).toHaveLength(0);
      return;
    }
  }
}

async function assertUnknownRoleCell(mode: LibraryMode, destination: Destination): Promise<void> {
  const context = `${destination.name} in ${mode} mode with no role`;
  await renderAppWithUnknownRole(mode);

  expect(
    screen.getAllByRole("radiogroup", { name: "Library mode" }),
    `${context}: one mode switch even without a role`,
  ).toHaveLength(1);
  expectSeededMode(mode, context);
  expect(
    screen.queryAllByTestId("signed-in-role"),
    `${context}: no role chip for an unknown role`,
  ).toHaveLength(0);
  expect(screen.getAllByRole("button", { name: "Settings" }), `${context}: Settings stays`).toHaveLength(
    1,
  );
  expect(
    screen.queryAllByRole("button", { name: "Audit log" }),
    `${context}: no Audit entry without a role`,
  ).toHaveLength(0);
  expect(
    screen.queryAllByRole("button", { name: "Review" }),
    `${context}: a role with no visible queue has no Review entry`,
  ).toHaveLength(0);
  expect(screen.queryAllByRole("tablist"), `${context}: no tablist without a role`).toHaveLength(0);
  expect(screen.queryAllByRole("tab"), `${context}: no queue tab without a role`).toHaveLength(0);
  expect(renderedQueueMarkers(), `${context}: no queue view without a role`).toEqual([]);

  switch (destination.kind) {
    case "mode": {
      const label = destination.mode === "files" ? "Files" : "Projects";
      fireEvent.click(screen.getByRole("radio", { name: label }));
      const pane = await screen.findByTestId(
        destination.mode === "files" ? "asset-grid" : "project-view",
      );
      expect(
        pane.closest("[hidden]"),
        `${context}: browsing still works, and ${label} is the visible pane`,
      ).toBeNull();
      return;
    }
    case "queue": {
      expect(
        screen.queryAllByRole("tabpanel"),
        `${context}: no queue panel is reachable`,
      ).toHaveLength(0);
      return;
    }
    case "settings": {
      fireEvent.click(screen.getByRole("button", { name: "Settings" }));
      expect(await screen.findByTestId("settings-view"), `${context}: Settings rendered`).toBeTruthy();
      return;
    }
    case "audit": {
      expect(screen.queryByTestId("audit-view"), `${context}: no audit page`).toBeNull();
      return;
    }
  }

  // The header is outside both panes, so the second mode must repeat the same
  // absences; the row stays 8 tests while still covering both modes.
  const otherMode: LibraryMode = mode === "files" ? "projects" : "files";
  fireEvent.click(
    screen.getByRole("radio", { name: otherMode === "files" ? "Files" : "Projects" }),
  );
  expect(
    screen.queryAllByRole("button", { name: "Review" }),
    `${context}: the other mode adds no Review entry`,
  ).toHaveLength(0);
  expect(
    screen.queryAllByRole("button", { name: "Audit log" }),
    `${context}: the other mode adds no Audit entry`,
  ).toHaveLength(0);
  expect(
    screen.getAllByRole("button", { name: "Settings" }),
    `${context}: Settings stays exactly one in the other mode`,
  ).toHaveLength(1);
  expect(
    screen.queryAllByRole("tablist"),
    `${context}: the other mode adds no tablist`,
  ).toHaveLength(0);
  expect(renderedQueueMarkers(), `${context}: the other mode adds no queue view`).toEqual([]);
}

describe("App destination reachability matrix (AC-2)", () => {
  describe.each(ROLES)("role %s", role => {
    describe.each(MODES)("mode %s", mode => {
      it.each(DESTINATIONS)(
        "$name: one uniquely named entry, and activating it renders the destination",
        async destination => {
          await assertCell(role, mode, destination);
        },
      );
    });
  });
});

describe("App destination reachability with no role", () => {
  it.each(DESTINATIONS)(
    "$name: no Review entry, no Audit entry, and no queue surface anywhere",
    async destination => {
      await assertUnknownRoleCell("files", destination);
    },
  );
});

describe("App review destination lifecycle (AC-3, AC-6)", () => {
  it("keeps the same queue selected and fetches nothing when the mode changes with Review open", async () => {
    const context = "mode switch with Review open";
    await renderApp("files", "editor");
    await openReview(context);
    expectSelectedQueue("bundles", context);
    const panelId = screen.getByRole("tabpanel").id;
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));

    expect(
      screen.getAllByRole("tablist"),
      `${context}: the switch neither duplicates nor drops the tab row`,
    ).toHaveLength(1);
    expectSelectedQueue("bundles", context);
    expect(screen.getByRole("tabpanel").id, `${context}: the same panel stays open`).toBe(panelId);
    await settle(30);
    expect(getBundleReportMock, `${context}: the switch issues no bundle request`).toHaveBeenCalledTimes(
      1,
    );
    expect(getImportRunsMock, `${context}: the switch issues no imports request`).toHaveBeenCalledTimes(
      0,
    );
    expect(
      getLibraryScanStatusMock,
      `${context}: the switch issues no scan request`,
    ).toHaveBeenCalledTimes(0);
    expect(getTrashMock, `${context}: the switch issues no trash request`).toHaveBeenCalledTimes(0);
  });

  it("arms exactly the resolved queue on entry, nothing on keys or a mode switch, and refetches on re-entry", async () => {
    const context = "fetch on entry";
    await renderApp("files", "editor");
    await settle(30);

    // app load arms none of the four queues.
    expect(getBundleReportMock, `${context}: no walk on app load`).toHaveBeenCalledTimes(0);
    expect(getImportRunsMock, `${context}: no run read on app load`).toHaveBeenCalledTimes(0);
    expect(getLibraryScanStatusMock, `${context}: no scan read on app load`).toHaveBeenCalledTimes(0);
    expect(getTrashMock, `${context}: no trash read on app load`).toHaveBeenCalledTimes(0);

    // entering Review arms only the resolved queue (bundles for an editor).
    await openReview(context);
    expectSelectedQueue("bundles", context);
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(1));
    expect(getImportRunsMock, `${context}: imports is not armed on entry`).toHaveBeenCalledTimes(0);
    expect(getLibraryScanStatusMock, `${context}: missing is not armed on entry`).toHaveBeenCalledTimes(
      0,
    );
    expect(getTrashMock, `${context}: trash is not armed on entry`).toHaveBeenCalledTimes(0);

    // manual activation: arrows and Home/End move focus only.
    const bundlesTab = screen.getByRole("tab", { name: /^Bundles to confirm/ });
    fireEvent.keyDown(bundlesTab, { key: "ArrowRight" });
    expect(document.activeElement, `${context}: ArrowRight moves focus`).toBe(
      screen.getByRole("tab", { name: /^Imports running/ }),
    );
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "End" });
    expect(document.activeElement, `${context}: End moves focus to the last tab`).toBe(
      screen.getByRole("tab", { name: /^Trash/ }),
    );
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Home" });
    expect(document.activeElement, `${context}: Home moves focus to the first tab`).toBe(bundlesTab);
    await settle(30);
    expect(getBundleReportMock, `${context}: focus movement never fetches bundles`).toHaveBeenCalledTimes(
      1,
    );
    expect(
      getImportRunsMock,
      `${context}: focus movement never fetches imports`,
    ).toHaveBeenCalledTimes(0);
    expect(
      getLibraryScanStatusMock,
      `${context}: focus movement never fetches the scan status`,
    ).toHaveBeenCalledTimes(0);
    expect(getTrashMock, `${context}: focus movement never fetches trash`).toHaveBeenCalledTimes(0);

    // Enter is the activation, and it fetches exactly the focused queue.
    fireEvent.keyDown(bundlesTab, { key: "ArrowRight" });
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Enter" });
    await waitFor(() => expect(getImportRunsMock).toHaveBeenCalledTimes(1));
    expectSelectedQueue("imports", context);
    expect(getBundleReportMock, `${context}: activating imports leaves bundles alone`).toHaveBeenCalledTimes(
      1,
    );
    expect(getLibraryScanStatusMock, `${context}: activating imports reads no scan`).toHaveBeenCalledTimes(
      0,
    );
    expect(getTrashMock, `${context}: activating imports reads no trash`).toHaveBeenCalledTimes(0);

    // a mode switch with the queue open issues nothing.
    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));
    await settle(30);
    expect(getImportRunsMock, `${context}: a mode switch reads no imports`).toHaveBeenCalledTimes(1);
    expect(getBundleReportMock, `${context}: a mode switch walks no bundles`).toHaveBeenCalledTimes(1);

    // leaving and re-entering the destination refetches once.
    fireEvent.click(screen.getByRole("button", { name: "Back to library" }));
    expect(screen.queryAllByRole("tablist"), `${context}: leaving Review drops the tab row`).toHaveLength(
      0,
    );
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    await screen.findByRole("tablist", { name: "Review" });
    await waitFor(() => expect(getImportRunsMock).toHaveBeenCalledTimes(2));
    expect(getBundleReportMock, `${context}: the bundle queue stays closed on re-entry`).toHaveBeenCalledTimes(
      1,
    );
  });

  it("is the cheap smoke guard standing in for a browser check: the root is h-screen flex-col and every ancestor of <main> is bounded", async () => {
    const container = await renderApp("files", "editor");
    const root = container.firstElementChild as HTMLElement;
    expect(root.tagName, "the app root is the first element").toBe("DIV");
    expect(
      root.classList.contains("h-screen"),
      "the root keeps h-screen so a pane cannot grow past the viewport",
    ).toBe(true);
    expect(root.classList.contains("flex-col"), "the header sits above the working row").toBe(true);
    expect(
      root.classList.contains("min-h-screen"),
      "min-h-screen would let the root grow with content and unbind the virtualiser",
    ).toBe(false);

    const header = root.firstElementChild as HTMLElement;
    expect(
      header.classList.contains("workspace-header"),
      "the header is a direct child of the root",
    ).toBe(true);

    const row = header.nextElementSibling as HTMLElement;
    expect(row.classList.contains("flex"), "the working row is a flex box").toBe(true);
    expect(row.classList.contains("min-h-0"), "the working row may shrink").toBe(true);
    expect(row.classList.contains("flex-1"), "the working row fills the viewport under the header").toBe(
      true,
    );

    const rail = row.querySelector(".library-rail");
    expect(rail, ".library-rail is inside the bounded working row").toBeTruthy();

    const main = container.querySelector<HTMLElement>("main");
    if (!main) throw new Error("App rendered no <main>");
    expect(main.parentElement, "<main> is a direct child of the bounded working row").toBe(row);
    expect(main.classList.contains("min-h-0"), "<main> may shrink").toBe(true);
    expect(main.classList.contains("flex-1"), "<main> takes the row's remaining width").toBe(true);
    expect(main.classList.contains("overflow-auto"), "<main> stays the virtualiser's scroller").toBe(
      true,
    );
  });
});
