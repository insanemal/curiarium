import {
  type Dispatch,
  type MutableRefObject,
  type SetStateAction,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  Asset,
  Folder,
  UnauthorizedError,
  listCreators,
  listFolders,
  listTags,
} from "../../lib/api";
import { refreshWindow } from "../../lib/assetPaging";
import { notifyError } from "../../lib/toast";
import { useStableCallback } from "../../lib/useStableCallback";
import {
  ROWS_PER_BATCH,
  fetchAssetPage,
  rowsBatchSizeForWidth,
  serverSortKey,
  type RefreshOpts,
  type SortDir,
  type SortKey,
} from "./assetGridMeta";

type AssetListOptions = {
  folderId?: string | null;
  /** Files mode: show only the library root's assets (mutually exclusive with a folder). */
  unfiled?: boolean;
  /**
   * Pinned "missing files" scope. A prop, never a `RefreshOpts` override: it
   * must be impossible for a toolbar control to drop it, and every request this
   * hook builds (`refresh`, `loadMore`) reads it from here. `resetFilters`
   * clears tags+creator only, so it cannot escape.
   */
  missingOnly?: boolean;
  projectId?: string | null;
  foldersVersion: number;
  assetsVersion: number;
  onUnauthorized?: () => void;
};

export type AssetListApi = {
  items: Asset[];
  setItems: Dispatch<SetStateAction<Asset[]>>;
  folders: Folder[];
  allTags: string[];
  setAllTags: Dispatch<SetStateAction<string[]>>;
  creators: string[];
  activeCreator: string | null;
  /** Setting a creator refetches from the first page, like every other filter. */
  setActiveCreator: (creator: string | null) => void;
  sortKey: SortKey;
  setSortKey: (key: SortKey) => void;
  sortDir: SortDir;
  setSortDir: (dir: SortDir) => void;
  activeTags: string[];
  setActiveTags: Dispatch<SetStateAction<string[]>>;
  loading: boolean;
  loadingMore: boolean;
  /**
   * A retryable first-load failure. The toast alone left the grid
   * indistinguishable from a genuinely empty listing, so the empty-state copy
   * must be able to tell them apart. Null once a retry lands.
   */
  error: string | null;
  offset: number;
  setOffset: Dispatch<SetStateAction<number>>;
  hasMore: boolean;
  /** Files mode only: the grid is narrowed to assets that belong to no project. */
  notInProject: boolean;
  /** Files mode only: the pile the loaded page describes (root vs everything). */
  unfiled: boolean;
  /** Latest typed query; a ref so typing never re-renders the grid. */
  queryRef: MutableRefObject<string>;
  refresh: (opts?: RefreshOpts) => Promise<void>;
  loadMore: () => Promise<void>;
  toggleTag: (tag: string) => void;
  toggleNotInProject: (next: boolean) => void;
  /** Clears the tag and creator filters in one refetch. */
  resetFilters: () => void;
  handleApiError: (err: unknown, message?: string) => boolean;
};

/**
 * Listing state for the grid: the loaded page of assets, the folder tree, the
 * tag chips and every request that fills or extends them.
 */
export function useAssetList({
  folderId,
  unfiled = false,
  missingOnly = false,
  projectId,
  foldersVersion,
  assetsVersion,
  onUnauthorized,
}: AssetListOptions): AssetListApi {
  const isProject = projectId != null;
  const [items, setItems] = useState<Asset[]>([]);
  const [folders, setFolders] = useState<Folder[]>([]);
  // Latest typed query. Held in a ref (not state) so typing does not re-render
  // the grid; only submitting a search refetches.
  const queryRef = useRef("");
  const [activeTags, setActiveTags] = useState<string[]>([]);
  const [allTags, setAllTags] = useState<string[]>([]);
  const [creators, setCreators] = useState<string[]>([]);
  const [activeCreator, setActiveCreatorState] = useState<string | null>(null);
  const [sortKey, setSortKeyState] = useState<SortKey>("name");
  const [sortDir, setSortDirState] = useState<SortDir>("asc");
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // A listing failure the user can retry in place. Held in state (not just
  // toasted) so an empty grid never lies about being an empty listing.
  const [error, setError] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [notInProject, setNotInProject] = useState(false);
  // The pile the loaded page describes. The prop is the source of truth (App
  // owns the selector); this mirrors it so `refresh`'s request and the empty
  // state can never disagree about which pile is on screen.
  const [unfiledState, setUnfiledState] = useState(unfiled);
  const [pageSize] = useState<number>(() => {
    if (typeof window === "undefined") return ROWS_PER_BATCH;
    return rowsBatchSizeForWidth(window.innerWidth);
  });
  // Newest request wins: `refresh` and `loadMore` both rebuild their request
  // from this render's closure and then write `items`/`offset`/`hasMore`, so a
  // response that started earlier must never land after one that started later.
  // One counter for both, incremented by each: the last request to start owns
  // the list and the loser is dropped whichever order the two settle in. Not
  // cancellation - `fetchAssetPage` takes no AbortSignal.
  const seq = useRef(0);
  // Which request raised `loading`. Two requests can be in flight with
  // different flags (`refresh` raises `loading`, `loadMore` raises
  // `loadingMore`), so the queue hooks' `if (id === seq.current) setX(false)`
  // would strand `loading` forever when a Load more supersedes a refresh. The
  // raiser lowers its own flag; a superseded request never lowers a newer one's.
  const loadingOwner = useRef(0);
  // `loadingMore` is state, so two Load more clicks in one tick both read the
  // pre-update `false` and both fetch the same offset, appending that page
  // twice. The latch is the guard; the state stays for rendering.
  const inFlightMore = useRef(false);

  const handleApiError = useStableCallback((err: unknown, message?: string) => {
    if (err instanceof UnauthorizedError) {
      onUnauthorized?.();
      return true;
    }
    console.error(err);
    if (message) notifyError(message);
    return false;
  });

  const refresh = useStableCallback(async (opts: RefreshOpts = {}) => {
    const id = ++seq.current;
    loadingOwner.current = id;
    setLoading(true);
    setError(null);
    if (!opts.keepLoadedPages) {
      setHasMore(false);
      setOffset(0);
    }
    const search = opts.search ?? queryRef.current;
    const tags = opts.tags ?? activeTags;
    const targetFolderId = folderId || undefined;
    // One value for both requests, so the chips can never describe a different
    // pile than the grid. The prop wins (App owns the All files/Unfiled
    // selector); `RefreshOpts` may override it for a single call.
    const effectiveUnfiled = isProject ? false : opts.unfiled ?? unfiled;
    setUnfiledState(effectiveUnfiled);
    const effectiveNotInProject = opts.notInProject ?? notInProject;
    const creator = opts.creator !== undefined ? opts.creator : activeCreator;
    const effectiveSortKey = opts.sortKey ?? sortKey;
    const effectiveSortDir = opts.sortDir ?? sortDir;
    const paging = refreshWindow(offset, pageSize, opts.keepLoadedPages);
    try {
      const [assetsResult, tagsResult, creatorsResult] = await Promise.allSettled([
        fetchAssetPage({
          projectId,
          q: search,
          tags,
          folderId: targetFolderId,
          unfiled: effectiveUnfiled,
          missing: missingOnly,
          notInProject: effectiveNotInProject,
          creator: creator ?? undefined,
          sort: serverSortKey(effectiveSortKey) ?? undefined,
          order: effectiveSortDir,
          limit: paging.limit,
          offset: paging.offset,
        }),
        // The facets must describe the same pinned set as the listing, or the
        // chips and the creator dropdown lie about the rest of the library.
        listTags({
          q: search,
          folder_id: isProject || effectiveUnfiled ? undefined : targetFolderId,
          unfiled: isProject ? undefined : effectiveUnfiled,
          project_id: projectId ?? undefined,
          not_in_project: isProject ? undefined : effectiveNotInProject,
          missing: missingOnly,
        }),
        listCreators({
          q: search,
          tags,
          folder_id: isProject || effectiveUnfiled ? undefined : targetFolderId,
          unfiled: isProject ? undefined : effectiveUnfiled,
          project_id: projectId ?? undefined,
          not_in_project: isProject ? undefined : effectiveNotInProject,
          missing: missingOnly,
        }),
      ]);
      // A newer request owns the list now; this result must not repaint it.
      if (id !== seq.current) return;

      if (assetsResult.status === "fulfilled") {
        setItems(assetsResult.value.items);
        setOffset(assetsResult.value.items.length);
        setHasMore(assetsResult.value.hasMore);
        setError(null);
      } else {
        handleApiError(
          assetsResult.reason,
          "Failed to load assets. Please sign in again or refresh.",
        );
        // An auth failure signs the operator out, so an inline retry would be
        // nonsense; every other failure is retryable in place.
        if (!(assetsResult.reason instanceof UnauthorizedError)) {
          setError("Failed to load assets.");
        }
      }

      if (tagsResult.status === "fulfilled") {
        setAllTags(tagsResult.value);
      } else {
        setAllTags([]);
        handleApiError(tagsResult.reason);
      }

      if (creatorsResult.status === "fulfilled") {
        setCreators(creatorsResult.value);
      } else {
        setCreators([]);
        handleApiError(creatorsResult.reason);
      }
    } finally {
      // The raiser lowers its own flag: a superseded refresh must not clear the
      // spinner of the request that replaced it.
      if (loadingOwner.current === id) {
        loadingOwner.current = 0;
        setLoading(false);
      }
    }
  });

  const loadMore = useStableCallback(async () => {
    if (inFlightMore.current || !hasMore) return;
    inFlightMore.current = true;
    const id = ++seq.current;
    setLoadingMore(true);
    try {
      const data = await fetchAssetPage({
        projectId,
        q: queryRef.current,
        tags: activeTags,
        folderId: folderId || undefined,
        unfiled: isProject ? false : unfiled,
        missing: missingOnly,
        notInProject,
        creator: activeCreator ?? undefined,
        sort: serverSortKey(sortKey) ?? undefined,
        order: sortDir,
        limit: pageSize,
        offset,
      });
      // Superseded by a newer request: appending here would splice a stale page
      // onto a list it no longer describes.
      if (id !== seq.current) return;
      setItems(prev => [...prev, ...data.items]);
      setOffset(offset + data.items.length);
      setHasMore(data.hasMore);
    } catch (err) {
      // A stale failure belongs to a list nobody is showing any more.
      if (id !== seq.current) return;
      handleApiError(err, "Failed to load more assets.");
    } finally {
      inFlightMore.current = false;
      setLoadingMore(false);
    }
  });

  // One entry refresh per pile. Folder, Unfiled, missing-only and project are
  // four separate piles, so the key carries all four - a boolean guard would
  // stop refetching on a filter change. The refetch starts at the first page,
  // like every other pile change.
  const pileKey = `${projectId ?? ""}|${folderId ?? ""}|${unfiled ? 1 : 0}|${missingOnly ? 1 : 0}`;
  // React.StrictMode invokes this effect twice for the same mount; a second
  // (list + tags + creators) request would be a real double fetch of the same
  // first page. Guarding here rather than from the cleanup keeps the first
  // request alive: a cleanup-driven reset would discard its only result.
  const walked = useRef<string | null>(null);
  useEffect(() => {
    if (walked.current === pileKey) return;
    walked.current = pileKey;
    void refresh();
  }, [pileKey, refresh]);
  // `assetsVersion` bumps when another view (upload, folder scan) changed
  // assets. Refetch in place instead of remounting: the ref is seeded with the
  // current version so mounting performs no extra fetch, and comparing values
  // makes the effect idempotent under StrictMode's double mount.
  // The refetch is keyed on the version value; `refresh` is referentially
  // stable (useStableCallback), so listing it as a dependency cannot loop, and
  // the ref guard keeps the effect idempotent under StrictMode.
  // Invariant: no call site bumps `assetsVersion` and `folderId` in the same
  // commit; if one ever does, that is two refreshes.
  const seenAssetsVersion = useRef(assetsVersion);
  useEffect(() => {
    if (seenAssetsVersion.current === assetsVersion) return;
    seenAssetsVersion.current = assetsVersion;
    refresh({ keepLoadedPages: true });
  }, [assetsVersion, refresh]);
  useEffect(() => {
    // Project mode never shows (or needs) the folder tree.
    if (isProject) return;
    (async () => {
      try {
        setFolders(await listFolders());
      } catch (err) {
        handleApiError(err, "Failed to load folders. Please refresh.");
      }
    })();
  }, [foldersVersion, isProject]);

  const toggleTag = (t: string) => {
    const next = activeTags.includes(t) ? activeTags.filter(x => x !== t) : [...activeTags, t];
    setActiveTags(next);
    refresh({ tags: next });
  };

  const toggleNotInProject = useStableCallback((next: boolean) => {
    setNotInProject(next);
    // No `keepLoadedPages`: like every other filter change, the paging window
    // resets to the first page.
    refresh({ notInProject: next });
  });

  const setActiveCreator = useStableCallback((creator: string | null) => {
    setActiveCreatorState(creator);
    refresh({ creator });
  });

  const setSortKey = useStableCallback((key: SortKey) => {
    setSortKeyState(key);
    refresh({ sortKey: key });
  });

  const setSortDir = useStableCallback((dir: SortDir) => {
    setSortDirState(dir);
    refresh({ sortDir: dir });
  });

  const resetFilters = useStableCallback(() => {
    setActiveTags([]);
    setActiveCreatorState(null);
    refresh({ tags: [], creator: null });
  });

  return {
    items,
    setItems,
    folders,
    allTags,
    setAllTags,
    creators,
    activeCreator,
    setActiveCreator,
    sortKey,
    setSortKey,
    sortDir,
    setSortDir,
    activeTags,
    setActiveTags,
    loading,
    loadingMore,
    error,
    offset,
    setOffset,
    hasMore,
    notInProject,
    unfiled: unfiledState,
    queryRef,
    refresh,
    loadMore,
    toggleTag,
    toggleNotInProject,
    resetFilters,
    handleApiError,
  };
}
