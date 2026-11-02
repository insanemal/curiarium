// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// App's own imports from the barrel, as a fixed object. `getTrash` and
// `purgeTrash` are deliberately ABSENT: `useTrash` imports them from the domain
// module below, so a barrel import would throw "No export is defined on the mock"
// instead of quietly reading a second mock instance.
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
  listProjects: vi.fn(async () => []),
  logout: vi.fn(async () => {}),
  refreshToken: vi.fn(),
}));

// The domain module the hook really uses. `GET /trash` and `POST /trash/purge`
// are the only network touchpoints of this queue.
vi.mock("../lib/api/trash", () => ({
  getTrash: vi.fn(),
  purgeTrash: vi.fn(),
}));

// A contributor or above enters Review on the bundle queue first, so
// `useBundleDetect` arms there. The report is empty on purpose: this file
// asserts trash reads only, and no queue assertion may shift because a
// neighbour fetched.
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

// The two hooks App arms on load read their own domain modules.
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

vi.mock("./asset-grid/AssetGrid", async () => {
  const react = await import("react");
  return { default: () => react.createElement("div", null, "Asset grid") };
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
import { getTrash, purgeTrash, type TrashList, type TrashPurgeResult } from "../lib/api/trash";
import { storeToken } from "../lib/auth";
import { describeQueueCount } from "./review/useReviewCounts";

const getTrashMock = vi.mocked(getTrash);
const purgeTrashMock = vi.mocked(purgeTrash);
const getMeMock = vi.mocked(getMe);

const TWO_ENTRIES: TrashList = {
  entries: [
    {
      path: ".trash/20261027-abc-old-folder",
      name: "old-folder",
      size: 2048,
      mtime: "2026-10-27T10:00:00Z",
    },
    {
      path: ".trash/20261028-def-bits",
      name: "bits",
      size: 1024,
      mtime: "2026-10-28T11:00:00Z",
    },
  ],
  total_size: 3072,
};

const EMPTY_TRASH: TrashList = { entries: [], total_size: 0 };

const PURGED: TrashPurgeResult = { purged: 2, bytes: 3072, failed: [] };

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

function trashTab(): HTMLElement {
  return screen.getByRole("tab", { name: /^Trash/ });
}

function badgeText(): string {
  // The tab's text also carries its label, so read only the count span.
  const tab = screen.getByTestId("review-count-trash");
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
 * Opening the trash is two steps now: the header's Review entry, which lands on
 * the resolved queue (bundles for a contributor or above, imports for a
 * viewer), then the Trash tab. Re-entry lands on whichever queue was last
 * selected, so the tab click is a no-op re-selection rather than a second read.
 */
async function openTrashQueue(): Promise<void> {
  fireEvent.click(openReviewButton());
  fireEvent.click(await screen.findByRole("tab", { name: /^Trash/ }));
  await screen.findByRole("heading", { name: "Trash" });
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
    username: "admin",
    email: null,
    role: "admin",
    disabled: false,
    kind: "local",
  });
  getTrashMock.mockReset();
  getTrashMock.mockResolvedValue(TWO_ENTRIES);
  purgeTrashMock.mockReset();
  purgeTrashMock.mockResolvedValue(PURGED);
});

afterEach(() => {
  cleanup();
});

describe("App trash destination (ADR 0011 slice 4)", () => {
  it("shows one Trash tab in one tablist, whichever mode is showing", async () => {
    await renderApp();
    await openTrashQueue();

    // One entry point now, not one per rail: the tab row is rendered inside the
    // destination, so there is exactly one tablist and exactly one Trash tab.
    expect(screen.getAllByRole("tablist")).toHaveLength(1);
    expect(screen.getAllByRole("tab", { name: /^Trash/ })).toHaveLength(1);

    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));

    // The header is outside both panes, so the switch cannot duplicate the row
    // or move the selection off the queue that is open.
    expect(screen.getAllByRole("tablist")).toHaveLength(1);
    expect(screen.getAllByRole("tab", { name: /^Trash/ })).toHaveLength(1);
    expect(trashTab().getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("heading", { name: "Trash" })).toBeTruthy();
  });

  it("shows an en dash before the trash is read and the same number in the header afterwards", async () => {
    await renderApp();

    // Counts belong to the queues inside Review, so before entry there is no
    // trash tab to carry one - and no badge elsewhere claiming an empty trash.
    expect(screen.queryAllByTestId("review-count-trash")).toHaveLength(0);
    expect(getTrashMock).not.toHaveBeenCalled();

    // Entering Review lands on the bundle queue; the trash tab is rendered but
    // unfetched, and `GET /trash` walks the whole storage tree, so its count is
    // unknown - a dash, never a zero that would claim an empty trash.
    fireEvent.click(openReviewButton());
    const tab = await screen.findByRole("tab", { name: /^Trash/ });
    expect(badgeText()).toBe("\u2013");
    expect(tab.getAttribute("title")).toBe("Count unavailable");
    expect(getTrashMock).not.toHaveBeenCalled();

    await openTrashQueue();
    await waitFor(() => expect(badgeText()).toBe("2"));

    expect(getTrashMock).toHaveBeenCalledTimes(1);
    expect(headerCount().textContent).toBe(badgeText());
    // One wording function feeds both surfaces, so they cannot describe the
    // same number two ways.
    expect(headerCount().getAttribute("title")).toBe(describeQueueCount("trash", 2));
    expect(trashTab().getAttribute("title")).toBe(describeQueueCount("trash", 2));
  });

  it("does not read the trash on app load", async () => {
    await renderApp();
    await settle(30);

    expect(getTrashMock).not.toHaveBeenCalled();
  });

  it("an admin's purge refetches exactly once and moves the badge and the header to zero", async () => {
    // The entry read finds two rows; the refetch after the purge finds none.
    getTrashMock.mockResolvedValueOnce(TWO_ENTRIES).mockResolvedValueOnce(EMPTY_TRASH);

    await renderApp();
    await openTrashQueue();
    await waitFor(() => expect(badgeText()).toBe("2"));

    fireEvent.click(screen.getByRole("button", { name: "Empty trash" }));
    // Opening the confirmation is not the purge.
    expect(purgeTrashMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Empty trash permanently" }));

    await waitFor(() => expect(purgeTrashMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(getTrashMock).toHaveBeenCalledTimes(2));

    expect(await screen.findByText(/2 entries removed \(3\.0 KB\)/)).toBeTruthy();
    await waitFor(() => expect(badgeText()).toBe("0"));
    expect(headerCount().textContent).toBe("0");
    expect(headerCount().getAttribute("title")).toBe(describeQueueCount("trash", 0));
  });

  // Read is READ; purge is ADMIN. All three non-admin roles must keep every
  // byte of the read surface and reach no code path that can issue a purge -
  // the button and the dialog are both behind `canPurge`, and `state.purge()`
  // has exactly one call site, inside that gate.
  it.each<"viewer" | "contributor" | "editor">(["viewer", "contributor", "editor"])(
    "gives a %s the queue, its rows, sizes and Refresh and no path to a purge",
    async role => {
      getMeMock.mockResolvedValue({
        id: "u1",
        username: role,
        email: null,
        role,
        disabled: false,
        kind: "local",
      });

      await renderApp();
      await openTrashQueue();
      await waitFor(() => expect(badgeText()).toBe("2"));

      expect(screen.getByText("old-folder")).toBeTruthy();
      expect(screen.getByText("bits")).toBeTruthy();
      expect(screen.getByText("2.0 KB")).toBeTruthy();
      expect(screen.getByText("1.0 KB")).toBeTruthy();
      expect(screen.getByText("2 entries · 3.0 KB")).toBeTruthy();
      expect(screen.getByRole("button", { name: "Refresh" })).toBeTruthy();

      // Hidden, not disabled: a non-admin has no path to the purge at all.
      expect(screen.queryByRole("button", { name: "Empty trash" })).toBeNull();
      const text = document.body.textContent || "";
      expect(text).not.toContain("Empty trash");
      expect(text).not.toContain("Empty the trash?");

      await settle(30);

      expect(purgeTrashMock).not.toHaveBeenCalled();
      expect(getTrashMock).toHaveBeenCalledTimes(1);
    },
  );

  it("keeps the queue open across a Files/Projects switch", async () => {
    await renderApp();
    await openTrashQueue();

    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));

    expect(screen.getByRole("heading", { name: "Trash" })).toBeTruthy();
    // The mode switch must not re-enter the queue's hook.
    expect(getTrashMock).toHaveBeenCalledTimes(1);
  });

  it("reads once per entry", async () => {
    await renderApp();
    await openTrashQueue();

    expect(getTrashMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Back to library" }));
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Trash" })).toBeNull(),
    );

    await openTrashQueue();

    await waitFor(() => expect(getTrashMock).toHaveBeenCalledTimes(2));
  });

  it("reads exactly once under React.StrictMode", async () => {
    render(
      <React.StrictMode>
        <App />
      </React.StrictMode>,
    );
    await screen.findByRole("button", { name: "Review" });

    await openTrashQueue();

    expect(getTrashMock).toHaveBeenCalledTimes(1);
  });
});
