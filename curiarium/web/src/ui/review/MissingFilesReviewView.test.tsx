// @vitest-environment jsdom
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

// The queue renders the REAL AssetGrid, so this barrel mock has to carry every
// name that grid's import graph reaches (AssetGrid, useAssetActions,
// useAssetList, useBulkActions, SupportingFilesPanel, preview...). It is the
// same factory AssetGrid.test.tsx uses, kept in step with it.
vi.mock("../../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  addProjectAssets: vi.fn(),
  batchDeleteAssets: vi.fn(),
  batchMoveAssets: vi.fn(),
  batchSetMeta: vi.fn(),
  batchSetTags: vi.fn(),
  createProject: vi.fn(),
  createFolder: vi.fn(),
  deleteAsset: vi.fn(),
  deleteAssetFile: vi.fn(),
  deletePreparedPrint: vi.fn(),
  downloadAsset: vi.fn(),
  downloadZip: vi.fn(),
  fileUrl: vi.fn((rel: string) => rel),
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
  listFolders: vi.fn(async () => []),
  listProjects: vi.fn(async () => []),
  listCreators: vi.fn(async () => []),
  listTags: vi.fn(async () => []),
  setTags: vi.fn(),
  startLibraryScan: vi.fn(),
  updateAssetFolder: vi.fn(),
  updateAssetMeta: vi.fn(),
  uploadAsset: vi.fn(),
  uploadAssetFile: vi.fn(),
}));

vi.mock("../ModelViewer", () => ({
  default: () => null,
  ModelSnapshot: () => null,
}));

vi.mock("../LightBurnPreview", () => ({
  default: () => null,
}));

import { listAssets, listCreators, listTags } from "../../lib/api";
import type { LibraryScanStatus } from "../../lib/api/settings";
import MissingFilesReviewView from "./MissingFilesReviewView";
import ReviewNav from "./ReviewNav";
import { describeQueueCount } from "./useReviewCounts";
import type { LibraryScanState } from "./useLibraryScan";

const listAssetsMock = vi.mocked(listAssets);
const listTagsMock = vi.mocked(listTags);
const listCreatorsMock = vi.mocked(listCreators);

function makeStatus(overrides: Partial<LibraryScanStatus> = {}): LibraryScanStatus {
  return {
    running: false,
    started_at: 1_700_000_000,
    finished_at: 1_700_000_010,
    registered: 3,
    known: 1,
    ignored: 0,
    missing: 0,
    failed: 0,
    errors: [],
    ...overrides,
  };
}

function makeState(overrides: Partial<LibraryScanState> = {}): LibraryScanState {
  return {
    scan: makeStatus(),
    busy: false,
    error: null,
    loaded: true,
    run: vi.fn(async () => {}),
    ...overrides,
  };
}

type ViewProps = ComponentProps<typeof MissingFilesReviewView>;

function viewProps(overrides: Partial<ViewProps> = {}): ViewProps {
  return {
    state: makeState(),
    canWrite: true,
    canContribute: true,
    canDelete: true,
    missingCount: 0,
    assetsVersion: 0,
    onRefresh: vi.fn(),
    onAssetsFiled: vi.fn(),
    onUnauthorized: vi.fn(),
    theme: "dark",
    ...overrides,
  };
}

function renderView(overrides: Partial<ViewProps> = {}) {
  return render(<MissingFilesReviewView {...viewProps(overrides)} />);
}

/** `listAssets` arguments of one call, with paging made explicit. */
function listCall(index: number) {
  const params = listAssetsMock.mock.calls[index]?.[0];
  if (!params || params.limit === undefined || params.offset === undefined) {
    throw new Error(`listAssets call ${index} was missing an explicit paging window`);
  }
  return { ...params, limit: params.limit, offset: params.offset };
}

/** The empty-state block and which outcome it claims, or null if there is none. */
function emptyStateMarker(container: HTMLElement): string | null {
  return container.querySelector("[data-missing-empty]")?.getAttribute("data-missing-empty") ?? null;
}

beforeEach(() => {
  listAssetsMock.mockReset();
  listAssetsMock.mockResolvedValue({ items: [], hasMore: false });
  listTagsMock.mockReset();
  listTagsMock.mockResolvedValue([]);
  listCreatorsMock.mockReset();
  listCreatorsMock.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
});

describe("MissingFilesReviewView queue", () => {
  it("pins the grid to the missing set", async () => {
    renderView();
    await waitFor(() => expect(listAssetsMock).toHaveBeenCalledTimes(1));

    // The pin is not an option the toolbar can drop: it travels in every
    // listing request the queue makes.
    const params = listCall(0);
    expect(params.missing).toBe(true);
    expect(typeof params.limit).toBe("number");
    expect(typeof params.offset).toBe("number");

    // The facets must describe the same pinned set as the listing, or the chips
    // and the creator dropdown would describe the rest of the library.
    await waitFor(() => expect(listTagsMock).toHaveBeenCalled());
    expect(listTagsMock).toHaveBeenLastCalledWith(expect.objectContaining({ missing: true }));
    expect(listCreatorsMock).toHaveBeenLastCalledWith(expect.objectContaining({ missing: true }));
  });

  it("heads the queue with the count App already fetched", async () => {
    const { container } = renderView({ missingCount: 12 });

    const count = container.querySelector(".review-queue-count");
    expect(count?.textContent).toBe("12");
    expect(count?.getAttribute("title")).toBe(describeQueueCount("missing", 12));
    expect(count?.getAttribute("title")).toBe("12 missing files awaiting a decision");

    // One wording in the sidebar row and in the queue header, or the same
    // number would be described two ways in two places.
    render(<ReviewNav role="viewer" counts={{ bundles: null, imports: null, missing: 12, trash: null }} />);
    expect(screen.getByTestId("review-count-missing").getAttribute("title")).toBe(
      count?.getAttribute("title"),
    );
  });

  it("dashes the count while it is unknown", () => {
    const { container } = renderView({ missingCount: null });

    const count = container.querySelector(".review-queue-count");
    expect(count?.textContent).toBe("\u2013");
    expect(count?.getAttribute("title")).toBe("Count unavailable");
  });

  it("renders the scan panel and its status", async () => {
    renderView();

    expect(screen.getByText("Files added from outside Curiarium")).toBeTruthy();
    expect(
      await screen.findByText("Last scan: 3 new, 1 known, 0 missing, 0 failed"),
    ).toBeTruthy();
  });
});

describe("MissingFilesReviewView loading and failure", () => {
  it("shows the loading line before the first page arrives", async () => {
    listAssetsMock.mockReturnValue(new Promise(() => {}));
    const { container } = renderView();

    expect(await screen.findByText("Loading missing files…")).toBeTruthy();
    expect(container.querySelector("[data-missing-loading]")).toBeTruthy();
    // A pending fetch is not an empty queue.
    expect(container.querySelector("[data-missing-empty]")).toBeNull();
  });

  it("shows a retryable error with Try again, not an empty queue", async () => {
    // The grid logs the swallowed fetch error; that noise is not an assertion.
    vi.spyOn(console, "error").mockImplementation(() => {});
    listAssetsMock.mockRejectedValueOnce(new Error("boom"));
    const { container } = renderView();

    expect(await screen.findByText("Could not load the files")).toBeTruthy();
    expect(container.querySelector("[data-grid-error]")).toBeTruthy();
    // A failed fetch must never be dressed up as "nothing is missing".
    expect(container.querySelector("[data-missing-empty]")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    await waitFor(() => expect(listAssetsMock).toHaveBeenCalledTimes(2));
    expect(listCall(1).missing).toBe(true);
  });
});

describe("MissingFilesReviewView empty state", () => {
  it("says the disk is clean only for a clean scan", async () => {
    const { container } = renderView({ state: makeState({ scan: makeStatus({ failed: 0 }) }) });

    expect(await screen.findByText("Every file an asset points at is on disk.")).toBeTruthy();
    expect(emptyStateMarker(container)).toBe("clean");
  });

  it("stays flag-truth when the scan status is unknown", async () => {
    const { container } = renderView({ state: makeState({ scan: null }) });

    expect(await screen.findByText("No missing files flagged")).toBeTruthy();
    expect(emptyStateMarker(container)).toBe("unknown");
    // An unknown scan is not evidence that nothing is missing.
    expect(screen.queryByText(/is on disk/)).toBeNull();
  });

  it("does not claim a clean disk after a failed scan", async () => {
    const { container } = renderView({ state: makeState({ scan: makeStatus({ failed: 1 }) }) });

    expect(await screen.findByText(/did not finish/)).toBeTruthy();
    expect(emptyStateMarker(container)).toBe("failed");
    expect(screen.queryByText(/is on disk/)).toBeNull();
  });

  it("calls an empty filtered result an empty result", async () => {
    const { container } = renderView();
    await screen.findByText("Every file an asset points at is on disk.");

    const searchInput = screen.getByPlaceholderText(
      "Search title, filename, notes...",
    ) as HTMLInputElement;
    fireEvent.change(searchInput, { target: { value: "cube" } });
    fireEvent.keyDown(searchInput, { key: "Enter" });

    await waitFor(() => expect(listAssetsMock).toHaveBeenCalledTimes(2));
    expect(listCall(1).q).toBe("cube");
    expect(container.querySelector("[data-missing-empty-filter]")).toBeTruthy();
    expect(screen.queryByText("Every file an asset points at is on disk.")).toBeNull();
  });
});

describe("MissingFilesReviewView write permission", () => {
  // The rescan is `POST /library/scan` (WRITE); the queue itself is a READ
  // surface, so a viewer reads it and only loses the button.
  it("offers the rescan only to a writer", async () => {
    const { rerender } = render(<MissingFilesReviewView {...viewProps({ canWrite: false })} />);

    expect(screen.queryByRole("button", { name: "Rescan storage folder" })).toBeNull();
    expect(screen.getByText("Files added from outside Curiarium")).toBeTruthy();

    rerender(<MissingFilesReviewView {...viewProps({ canWrite: true })} />);

    expect(screen.getByRole("button", { name: "Rescan storage folder" })).toBeTruthy();
  });
});
