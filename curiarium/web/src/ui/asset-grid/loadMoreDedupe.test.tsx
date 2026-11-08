// @vitest-environment jsdom
//
// Mf-r5ox: ten unawaited "Load more" activations must not fetch or append the
// same page twice.
//
// Ten clicks can reach the hook in two shapes. Real input - a user mashing the
// button, or a script clicking the live control - re-renders the button as
// `disabled` after the first click, so the browser delivers at most one and the
// rest land on an inert element. Programmatic dispatch (`node.dispatchEvent`)
// in one task bypasses that: neither jsdom nor a browser consults `disabled`
// for a dispatched event, and no render can happen between the ten synchronous
// dispatches, so the hook sees ten activations while `loadingMore` is still
// false in its closure.
//
// Measured against a canned 47-asset paginated server, page size 20:
//   * pre-fix hook (421ed23^): 11 listAssets calls (offset 0, then offset 20 ten
//     times), 220 cards, 40 distinct ids, 180 duplicate rows - 47 assets served.
//   * HEAD hook (Mf-m45p latch): 2 calls (0, 20), 40 cards, 40 distinct, 0
//     duplicates; paging to the end reaches 3 calls (0, 20, 40), 47 distinct,
//     and no Load more button. Real-input semantics: 1 effective click out of
//     10, the same 2 calls and 40 cards.
//
// The canned server below mirrors `curiarium/api/routers/asset_helpers.py:
// asset_page`: at most `limit` rows plus `hasMore`/`nextOffset`, where the
// server learns `has_more` from one extra probed row.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createRef } from "react";

import type { Asset } from "../../lib/api";

vi.mock("../../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  AssetNotFoundError: class AssetNotFoundError extends Error {},
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

vi.mock("../ModelViewer", () => ({ default: () => null, ModelSnapshot: () => null }));
vi.mock("../LightBurnPreview", () => ({ default: () => null }));

import { listAssets } from "../../lib/api";
import AssetGrid from "./AssetGrid";
import { rowsBatchSizeForWidth } from "./assetGridMeta";

const TOTAL = 47;
/** 1200px is 4 columns, and a first page is 5 rows. */
const VIEW_WIDTH = 1200;
const PAGE = rowsBatchSizeForWidth(VIEW_WIDTH);

/** What the canned `/assets/counts` endpoint reports for the pile below. */
const SERVER_COUNTS = { total: TOTAL, unfiled: 0, missing: 0 };

const ALL: Asset[] = Array.from({ length: TOTAL }, (_, i) => {
  const n = String(i + 1).padStart(2, "0");
  return {
    id: `asset-${n}`,
    name: `Part ${n}`,
    filename: `part-${n}.stl`,
    mime: "model/stl",
    size: 1000 + i,
    tags: [],
    url: `/api/assets/asset-${n}/file`,
    thumb_url: null,
    folder_id: null,
    supporting_file_count: 0,
  };
});

const listAssetsMock = vi.mocked(listAssets);

type Call = { limit: number; offset: number };
let calls: Call[] = [];

/** Canned server: the real endpoint's limit/offset + has-more contract. */
function installServer() {
  calls = [];
  listAssetsMock.mockImplementation(
    async (params: NonNullable<Parameters<typeof listAssets>[0]> = {}) => {
      const offset = params.offset ?? 0;
      const limit = params.limit ?? TOTAL;
      calls.push({ limit, offset });
      // A macrotask, like a real fetch: one tick of asynchrony per request.
      await new Promise(resolve => setTimeout(resolve, 0));
      const items = ALL.slice(offset, offset + limit);
      return { items, hasMore: offset + items.length < TOTAL, nextOffset: offset + items.length };
    },
  );
}

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

/**
 * A tall emulated viewport: every loaded row renders, so the DOM is the model
 * of what the list holds - which is what makes a duplicate row observable.
 */
function renderGrid() {
  const scrollerRef = createRef<HTMLDivElement>();
  const utils = render(
    <div ref={scrollerRef} data-testid="scroller">
      <AssetGrid canContribute canDelete theme="dark" scrollElementRef={scrollerRef} />
    </div>,
  );
  const scroller = scrollerRef.current;
  if (!scroller) throw new Error("scroller did not mount");
  Object.defineProperty(scroller, "clientHeight", { value: 30000, configurable: true });
  vi.spyOn(scroller, "getBoundingClientRect").mockReturnValue(rectOf(0, 30000));
  return utils;
}

function cardIds(): string[] {
  return Array.from(document.querySelectorAll<HTMLElement>("[data-asset-id]")).map(
    el => el.getAttribute("data-asset-id") ?? "",
  );
}

function loadMoreButton(): HTMLButtonElement | null {
  return screen.queryByRole("button", { name: /Load more|Loading/ }) as HTMLButtonElement | null;
}

function mustLoadMore(): HTMLButtonElement {
  const button = loadMoreButton();
  if (!button) throw new Error("Load more is not rendered");
  return button;
}

/** Every request the grid made, as `offset/limit`, in send order. */
function requestedWindows(): string[] {
  return calls.map(call => `${call.offset}/${call.limit}`);
}

async function settle(ms = 60) {
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, ms));
  });
}

async function firstPageLanded() {
  await waitFor(() => expect(cardIds()).toHaveLength(PAGE), { timeout: 4000 });
}

beforeEach(() => {
  Object.defineProperty(window, "innerWidth", { value: VIEW_WIDTH, configurable: true });
  Object.defineProperty(window, "innerHeight", { value: 1000, configurable: true });
  installServer();
  // The measured page size; the canned numbers below assume it.
  expect(PAGE).toBe(20);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Load more", () => {
  it("never fetches or appends the same page twice when ten clicks land in one task", async () => {
    // The failing shape: a page-context loop dispatching on the live node, so
    // `disabled` is never consulted and no render separates the ten events.
    renderGrid();
    await firstPageLanded();
    const node = mustLoadMore();

    act(() => {
      for (let i = 0; i < 10; i += 1) {
        node.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      }
    });
    await settle();

    expect(listAssetsMock).toHaveBeenCalledTimes(2);
    expect(requestedWindows()).toEqual(["0/20", "20/20"]);
    const ids = cardIds();
    expect(ids).toHaveLength(40);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("disables Load more synchronously, so a real tenth click never reaches the hook", async () => {
    renderGrid();
    await firstPageLanded();
    fireEvent.click(mustLoadMore());

    // The guard a real user meets: setLoadingMore(true) is flushed by the click.
    const button = mustLoadMore();
    expect(button.disabled).toBe(true);
    expect(button.textContent).toBe("Loading...");

    // A dispatched click still reaches React on a disabled button - jsdom has
    // no activation behaviour to inhibit it, unlike a browser, which would not
    // deliver a click on a disabled control at all. The latch must hold anyway.
    fireEvent.click(button);
    await settle();

    expect(listAssetsMock).toHaveBeenCalledTimes(2);
    const ids = cardIds();
    expect(ids).toHaveLength(40);
    expect(new Set(ids).size).toBe(40);
  });

  it("reaches the server's asset count exactly once paging ends", async () => {
    renderGrid();
    await firstPageLanded();

    // One awaited click at a time is what the disabled control allows.
    for (let i = 0; i < 10; i += 1) {
      const button = loadMoreButton();
      if (!button) break;
      fireEvent.click(button);
      await settle();
    }

    expect(requestedWindows()).toEqual(["0/20", "20/20", "40/20"]);
    const ids = cardIds();
    expect(ids).toHaveLength(TOTAL);
    expect(new Set(ids).size).toBe(TOTAL);
    // `SERVER_COUNTS` is what `/assets/counts` would report for this pile: the
    // paged list must end on that number, with nothing shown twice.
    expect(new Set(ids).size).toBe(SERVER_COUNTS.total);
    expect(loadMoreButton()).toBeNull();
  });
});
