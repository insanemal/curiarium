// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// App's own imports from the barrel, as a fixed object: the review queue
// reaches the API through `useBundleDetect`'s domain module instead.
vi.mock("../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  FORBIDDEN_MESSAGE: "Your role does not allow this action.",
  apiHealth: vi.fn(async () => ({ ok: true, auth_required: true })),
  fileUrl: vi.fn((rel: string) => rel),
  getApiBase: vi.fn(() => "http://localhost:8000"),
  getAssetCounts: vi.fn(async () => ({ total: 0, unfiled: 0 })),
  getMe: vi.fn(async () => ({
    id: "u1",
    username: "contributor",
    email: null,
    role: "contributor",
    disabled: false,
    kind: "local",
  })),
  listProjects: vi.fn(async () => []),
  logout: vi.fn(async () => {}),
  refreshToken: vi.fn(),
}));

vi.mock("../lib/api/bundles", () => ({
  getBundleReport: vi.fn(),
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

type NavProps = {
  role?: Role | null;
  activeView?: ActiveView;
  activeQueue?: ReviewQueueKey | null;
  reviewCounts?: ReviewCounts;
  onOpenQueue?: (key: ReviewQueueKey) => void;
};

/**
 * The real `ReviewNav` is what proves App passes the counts and the opener
 * through both sidebars; the rest of each sidebar is replaced, not the group.
 */
vi.mock("./Sidebar", async () => {
  const react = await import("react");
  const { default: ReviewNav } = await import("./review/ReviewNav");
  return {
    default: ({ role, activeView, activeQueue, reviewCounts, onOpenQueue }: NavProps) =>
      react.createElement(
        "aside",
        { "aria-label": "Files" },
        react.createElement(ReviewNav, {
          role,
          activeView,
          activeQueue: activeQueue ?? null,
          counts: reviewCounts,
          onOpenQueue,
        }),
      ),
  };
});

vi.mock("./ProjectSidebar", async () => {
  const react = await import("react");
  const { default: ReviewNav } = await import("./review/ReviewNav");
  return {
    default: ({ role, activeView, activeQueue, reviewCounts, onOpenQueue }: NavProps) =>
      react.createElement(
        "aside",
        { "aria-label": "Projects" },
        react.createElement(ReviewNav, {
          role,
          activeView,
          activeQueue: activeQueue ?? null,
          counts: reviewCounts,
          onOpenQueue,
        }),
      ),
  };
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
import {
  createBundles,
  getBundleReport,
  setBundleDismissed,
  type BundleDetectItem,
  type BundleReport,
} from "../lib/api/bundles";
import { storeToken, type Role } from "../lib/auth";
import { MODE_STORAGE_KEY } from "../lib/mode";
import type { ReviewCounts } from "./review/useReviewCounts";
import type { ActiveView, ReviewQueueKey } from "./views";

const getBundleReportMock = vi.mocked(getBundleReport);
const createBundlesMock = vi.mocked(createBundles);
const setBundleDismissedMock = vi.mocked(setBundleDismissed);

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

function makeBundle(overrides: Partial<BundleDetectItem> = {}): BundleDetectItem {
  return {
    key: "Moai",
    rel_path: "Moai",
    title: "Moai statue",
    name_suggestion: "Moai statue",
    creator: "sculpteo",
    licence: "CC BY",
    licence_url: null,
    source_url: null,
    detector: "thingiverse-readme",
    confidence: "high",
    metadata_shape: "root-docs",
    description_excerpt: null,
    model_count: 1,
    image_count: 1,
    text_count: 0,
    other_count: 0,
    cover: { asset_id: null, rel_path: null, reason: "largest-image-unregistered" },
    assets_total: 2,
    status: "ready",
    blocked_reason: null,
    archive: null,
    existing_project: null,
    dismissed: false,
    ...overrides,
  };
}

function makeReport(bundles: BundleDetectItem[]): BundleReport {
  const ready = bundles.filter(row => row.status === "ready").length;
  return {
    root: "",
    truncated: false,
    counts: {
      total: bundles.length,
      high: bundles.filter(row => row.confidence === "high").length,
      low: bundles.filter(row => row.confidence === "low").length,
      ready,
      blocked: bundles.length - ready,
      dismissed: bundles.filter(row => row.dismissed).length,
      already_created: bundles.filter(row => row.existing_project).length,
    },
    bundles,
    stale_dismissals: [],
  };
}

/** One bundle awaiting a decision, one dismissed: the badge must say 1. */
const FIRST_REPORT = makeReport([
  makeBundle(),
  makeBundle({ key: "Old", title: "Old bundle", dismissed: true }),
]);

/** The same walk after Moai became a project: nothing awaits a decision. */
const SECOND_REPORT = makeReport([
  makeBundle({ existing_project: "Moai statue" }),
  makeBundle({ key: "Old", title: "Old bundle", dismissed: true }),
]);

/** The same walk after Moai was dismissed: nothing awaits a decision either. */
const DISMISSED_REPORT = makeReport([
  makeBundle({ dismissed: true }),
  makeBundle({ key: "Old", title: "Old bundle", dismissed: true }),
]);

/** The nav exists only once `/me` answers, so this waits for the role. */
async function renderApp(): Promise<void> {
  render(<App />);
  await waitFor(() => expect(screen.getAllByRole("button", { name: /^Bundles to confirm/ }).length).toBeGreaterThan(0));
}

function openQueueButton(): HTMLElement {
  return screen.getAllByRole("button", { name: /^Bundles to confirm/ })[0];
}

function badgeText(): string {
  return screen.getAllByTestId("review-count-bundles")[0].textContent ?? "";
}

/** Resolves after real timers have had a chance to fire anything they hold. */
function settle(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

beforeEach(() => {
  installMemoryStorage();
  storeToken("access-token");
  getBundleReportMock.mockReset();
  getBundleReportMock.mockResolvedValue(FIRST_REPORT);
  createBundlesMock.mockReset();
  createBundlesMock.mockResolvedValue({
    created: 1,
    skipped: 0,
    blocked: 0,
    failed: 0,
    results: [],
  });
  setBundleDismissedMock.mockReset();
  setBundleDismissedMock.mockResolvedValue({ dismissed: ["Moai"], stale_dismissals: [] });
});

afterEach(() => {
  cleanup();
});

describe("App review destination", () => {
  it("does not walk the library before the queue is opened", async () => {
    await renderApp();
    await settle(30);

    expect(getBundleReportMock).not.toHaveBeenCalled();
    expect(badgeText()).toBe("\u2013");
  });

  it("opens the queue with exactly one full-report fetch and paints the badge from it", async () => {
    await renderApp();

    fireEvent.click(openQueueButton());

    expect(await screen.findByRole("heading", { name: "Bundles to confirm" })).toBeTruthy();
    await screen.findByRole("listbox", { name: "Bundles to confirm" });
    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
    expect(getBundleReportMock).toHaveBeenCalledWith({ includeDismissed: true, limit: 1000 });
    // Undismissed and not already a project: the dismissed row does not count.
    await waitFor(() => expect(badgeText()).toBe("1"));
  });

  it("does not poll while the operator sits in the queue", async () => {
    await renderApp();
    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });
    expect(getBundleReportMock).toHaveBeenCalledTimes(1);

    await settle(60);

    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
  });

  it("refetches once after a create and updates the badge from the new report", async () => {
    getBundleReportMock
      .mockResolvedValueOnce(FIRST_REPORT)
      .mockResolvedValueOnce(SECOND_REPORT);
    await renderApp();
    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });
    await waitFor(() => expect(badgeText()).toBe("1"));

    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    await waitFor(() => expect(createBundlesMock).toHaveBeenCalledTimes(1));
    expect(createBundlesMock.mock.calls[0][0]).toEqual([{ key: "Moai", name: "Moai statue" }]);
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(badgeText()).toBe("0"));
    expect(await screen.findByText("Already created as project Moai statue")).toBeTruthy();
  });

  it("refetches once after a dismiss and updates the badge from the new report", async () => {
    getBundleReportMock
      .mockResolvedValueOnce(FIRST_REPORT)
      .mockResolvedValueOnce(DISMISSED_REPORT);
    await renderApp();
    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });
    await waitFor(() => expect(badgeText()).toBe("1"));

    // Space is the row glyph's keyboard equivalent: it selects for the bulk bar.
    fireEvent.keyDown(screen.getAllByRole("option")[0], { key: " " });
    fireEvent.click(screen.getByRole("button", { name: /^Dismiss selected/ }));

    await waitFor(() => expect(setBundleDismissedMock).toHaveBeenCalledTimes(1));
    expect(setBundleDismissedMock).toHaveBeenCalledWith(["Moai"], true);
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(badgeText()).toBe("0"));
  });

  it("gives a viewer no queue row and never walks the library", async () => {
    vi.mocked(getMe).mockResolvedValueOnce({
      id: "u1",
      username: "viewer",
      email: null,
      role: "viewer",
      disabled: false,
      kind: "local",
    });
    render(<App />);

    // No positive signal exists for an absent row, so let /me and any deferred
    // work settle before asserting the row and the walk never happened.
    await settle(30);

    expect(screen.queryByRole("button", { name: /^Bundles to confirm/ })).toBeNull();
    expect(getBundleReportMock).not.toHaveBeenCalled();
  });

  it("walks the library exactly once under StrictMode", async () => {
    render(
      <React.StrictMode>
        <App />
      </React.StrictMode>,
    );
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: /^Bundles to confirm/ }).length).toBeGreaterThan(0),
    );

    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });

    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
  });

  it("refetches once on re-entry, not on every render", async () => {
    await renderApp();
    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });
    expect(getBundleReportMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Back to library" }));
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Bundles to confirm" })).toBeNull(),
    );

    fireEvent.click(openQueueButton());
    await screen.findByRole("heading", { name: "Bundles to confirm" });
    await waitFor(() => expect(getBundleReportMock).toHaveBeenCalledTimes(2));
  });

  it("keeps the queue open across a Files/Projects switch", async () => {
    await renderApp();
    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });

    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));

    expect(window.localStorage.getItem(MODE_STORAGE_KEY)).toBe("projects");
    expect(screen.getByRole("heading", { name: "Bundles to confirm" })).toBeTruthy();
    expect(getBundleReportMock).toHaveBeenCalledTimes(1);
  });

  it("returns to the library from the queue", async () => {
    await renderApp();
    fireEvent.click(openQueueButton());
    await screen.findByRole("listbox", { name: "Bundles to confirm" });

    fireEvent.click(screen.getByRole("button", { name: "Back to library" }));

    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Bundles to confirm" })).toBeNull(),
    );
    expect(screen.getByText("Asset grid")).toBeTruthy();
  });
});
