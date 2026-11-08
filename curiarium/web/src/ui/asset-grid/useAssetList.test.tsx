// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";

// The hook reaches its listing through `assetGridMeta.fetchAssetPage`, which
// imports `listAssets`/`listProjectAssets` from the barrel, so the barrel is
// the mock that intercepts every listing request.
vi.mock("../../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  listAssets: vi.fn(),
  listProjectAssets: vi.fn(),
  listTags: vi.fn(),
  listCreators: vi.fn(),
  listFolders: vi.fn(),
}));

import type { Asset, ListAssetsResult } from "../../lib/api";
import { listAssets, listCreators, listFolders, listTags } from "../../lib/api";
import { useAssetList } from "./useAssetList";

const listAssetsMock = vi.mocked(listAssets);
const listCreatorsMock = vi.mocked(listCreators);
const listFoldersMock = vi.mocked(listFolders);
const listTagsMock = vi.mocked(listTags);

function asset(id: string): Asset {
  return {
    id,
    name: `Asset ${id}`,
    filename: `${id}.png`,
    mime: "image/png",
    size: 1000,
    tags: [],
    url: `/api/assets/${id}/file`,
    thumb_url: `/api/assets/${id}/thumb`,
    supporting_file_count: 0,
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

function page(ids: string[], hasMore: boolean): ListAssetsResult {
  return { items: ids.map(asset), hasMore };
}

function mountHook() {
  return renderHook(() => useAssetList({ foldersVersion: 0, assetsVersion: 0 }));
}

beforeEach(() => {
  listAssetsMock.mockReset();
  listTagsMock.mockReset();
  listTagsMock.mockResolvedValue(["tag-a"]);
  listCreatorsMock.mockReset();
  listCreatorsMock.mockResolvedValue(["ada"]);
  listFoldersMock.mockReset();
  listFoldersMock.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("useAssetList", () => {
  it("the newest request wins when a Load more starts after a refresh", async () => {
    // Without newest-wins the older refresh lands last and rewinds the list.
    const first = deferred<ListAssetsResult>();
    // A plain refresh sets hasMore=false and hides Load more, so the reachable
    // race is the in-place refresh the assetsVersion effect issues.
    const inPlace = deferred<ListAssetsResult>();
    const more = deferred<ListAssetsResult>();
    listAssetsMock
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(inPlace.promise)
      .mockReturnValueOnce(more.promise);

    const { result } = mountHook();
    await act(async () => {
      first.resolve(page(["a1", "a2"], true));
    });
    await waitFor(() => expect(result.current.items.map(a => a.id)).toEqual(["a1", "a2"]));

    let refreshDone = Promise.resolve();
    act(() => {
      refreshDone = result.current.refresh({ keepLoadedPages: true });
    });
    let moreDone = Promise.resolve();
    act(() => {
      moreDone = result.current.loadMore();
    });

    // The Load more page settles first...
    await act(async () => {
      more.resolve(page(["m3", "m4"], false));
    });
    // ...and the older refresh settles last.
    await act(async () => {
      inPlace.resolve(page(["a1", "a2"], true));
      await Promise.all([refreshDone, moreDone]);
    });

    expect(result.current.items.map(a => a.id)).toEqual(["a1", "a2", "m3", "m4"]);
    expect(result.current.hasMore).toBe(false);
    expect(result.current.loading).toBe(false);
  });

  it("the newest request wins when a refresh starts after a Load more", async () => {
    // Without newest-wins the stale page is appended onto the refresh's list.
    const first = deferred<ListAssetsResult>();
    const stale = deferred<ListAssetsResult>();
    const fresh = deferred<ListAssetsResult>();
    listAssetsMock
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(stale.promise)
      .mockReturnValueOnce(fresh.promise);

    const { result } = mountHook();
    await act(async () => {
      first.resolve(page(["a1", "a2"], true));
    });
    await waitFor(() => expect(result.current.items.map(a => a.id)).toEqual(["a1", "a2"]));

    let moreDone = Promise.resolve();
    act(() => {
      moreDone = result.current.loadMore();
    });
    let refreshDone = Promise.resolve();
    act(() => {
      refreshDone = result.current.refresh();
    });

    // The newer refresh lands first...
    await act(async () => {
      fresh.resolve(page(["b1", "b2"], true));
    });
    // ...and the stale Load more page settles last.
    await act(async () => {
      stale.resolve(page(["c3", "c4"], true));
      await Promise.all([refreshDone, moreDone]);
    });

    expect(result.current.items.map(a => a.id)).toEqual(["b1", "b2"]);
    expect(result.current.offset).toBe(2);
    expect(result.current.hasMore).toBe(true);
    expect(result.current.loading).toBe(false);
    expect(result.current.loadingMore).toBe(false);
  });

  it("fires one list, one tags and one creators request per entry under StrictMode", async () => {
    // Without the walked guard StrictMode's second effect invoke double-fetches.
    listAssetsMock.mockResolvedValue(page(["s1"], false));

    const { result } = renderHook(() => useAssetList({ foldersVersion: 0, assetsVersion: 0 }), {
      wrapper: React.StrictMode,
    });
    await waitFor(() => expect(result.current.items.map(a => a.id)).toEqual(["s1"]));

    expect(listAssetsMock).toHaveBeenCalledTimes(1);
    expect(listTagsMock).toHaveBeenCalledTimes(1);
    expect(listCreatorsMock).toHaveBeenCalledTimes(1);
  });

  it("two Load more clicks in one tick append the page once", async () => {
    // The second click reads the pre-update `loadingMore` state, so without the
    // ref latch it fetches the same offset and appends that page twice.
    const first = deferred<ListAssetsResult>();
    const more = deferred<ListAssetsResult>();
    listAssetsMock.mockReturnValueOnce(first.promise).mockReturnValueOnce(more.promise);

    const { result } = mountHook();
    await act(async () => {
      first.resolve(page(["a1", "a2"], true));
    });
    await waitFor(() => expect(result.current.items.map(a => a.id)).toEqual(["a1", "a2"]));

    act(() => {
      void result.current.loadMore();
      void result.current.loadMore();
    });
    expect(listAssetsMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      more.resolve(page(["m3", "m4"], false));
    });

    expect(listAssetsMock).toHaveBeenCalledTimes(2);
    expect(result.current.items.map(a => a.id)).toEqual(["a1", "a2", "m3", "m4"]);
  });
});
