import { useMemo, useState } from "react";

import { Asset, Folder } from "../../lib/api";
import { childFolders, folderPath, type FolderPathSegment } from "../../lib/folderTree";
import { extOf, type GroupBucket, type SortDir, type SortKey } from "./assetGridMeta";

type AssetSortingOptions = {
  items: Asset[];
  folders: Folder[];
  folderId?: string | null;
  /** The toolbar's sort state is owned by the listing hook (it drives requests). */
  sortKey: SortKey;
  setSortKey: (key: SortKey) => void;
  sortDir: SortDir;
  setSortDir: (dir: SortDir) => void;
};

export type AssetSortingApi = {
  sortKey: SortKey;
  setSortKey: (key: SortKey) => void;
  sortDir: SortDir;
  setSortDir: (dir: SortDir) => void;
  sortedItems: Asset[];
  folderGroups: GroupBucket[];
  typeGroups: GroupBucket[];
  collapsedGroups: Set<string>;
  expandedGroups: Set<string>;
  toggleGroup: (id: string) => void;
  toggleGroupExpanded: (id: string) => void;
  folderById: Record<string, Folder>;
  folderNames: Record<string, string>;
  subfolders: Folder[];
};

/**
 * Sort order, the grouped buckets, the derived folder lookups and the group
 * collapse state. Pure derivation from the listing: no effects, no requests.
 */
export function useAssetSorting({
  items,
  folders,
  folderId,
  sortKey,
  setSortKey,
  sortDir,
  setSortDir,
}: AssetSortingOptions): AssetSortingApi {
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());

  const folderById = useMemo(() => {
    const map: Record<string, Folder> = {};
    folders.forEach(f => { map[f.id] = f; });
    return map;
  }, [folders]);

  const folderNames = useMemo(() => {
    const m: Record<string, string> = {};
    // Cost note (accepted, not hidden): this asks `folderPath` once per folder,
    // and `folderPath` rebuilds its own index on every call, so building names
    // for N folders is O(N^2) whenever the folder array changes - now including
    // every project mount. `folderTree.ts` is outside this ticket and the
    // `pathFor` cache below cannot lower the call count (that wrapper also calls
    // `folderPath`), so the cost stays. It is accepted because folder counts are
    // in the hundreds and this memo runs only when `folders` changes.
    for (const f of folders) {
      m[f.id] = folderPath(folders, f.id).map(segment => segment.name).join(" / ");
    }
    return m;
  }, [folders]);

  // One path builder for the whole hook: `folderPath` is cycle-guarded and
  // applies the "Untitled" fallback, so the names map and the bucket trails
  // can never disagree. Memoised per folder id for the items on screen.
  const pathFor = useMemo(() => {
    const cache = new Map<string | null, FolderPathSegment[]>();
    return (folderId: string | null | undefined): FolderPathSegment[] => {
      const key = folderId ?? null;
      const cached = cache.get(key);
      if (cached) return cached;
      const path = folderPath(folders, key);
      cache.set(key, path);
      return path;
    };
  }, [folders]);

  // Direct subfolders of the open folder, derived from the folder list the grid
  // already fetched — no extra requests.
  const subfolders = useMemo(() => childFolders(folders, folderId ?? null), [folderId, folders]);

  const sortedItems = useMemo(() => {
    // name/size/creator are server-backed: the full listing is already ordered,
    // so re-sorting the loaded page here would undo the global order.
    if (sortKey !== "type" && sortKey !== "folder") return items;

    const copy = [...items];
    const dir = sortDir === "asc" ? 1 : -1;
    const nameForFolder = (a: Asset) => (a.folder_id ? folderNames[a.folder_id] || "" : "");
    copy.sort((a, b) => {
      if (sortKey === "type") {
        return extOf(a.filename).localeCompare(extOf(b.filename)) * dir;
      }
      // folder
      return nameForFolder(a).localeCompare(nameForFolder(b)) * dir;
    });
    return copy;
  }, [items, sortKey, sortDir, folderNames]);

  // Derived unconditionally, not only for the `Folder` sort: project mode
  // sections its members by folder and cannot select that sort, while the
  // library's `Folder` sort reads the very same buckets. The bucket ids, the
  // `Unassigned` pile and the ordering are unchanged, and `path` carries the
  // root→leaf trail the project view trims its own root off.
  const folderGroups = useMemo<GroupBucket[]>(() => {
    const grouping: Record<string, GroupBucket> = {};
    for (const item of items) {
      const key = item.folder_id || "__ungrouped";
      if (!grouping[key]) {
        grouping[key] = {
          id: `folder:${key}`,
          title: item.folder_id ? folderNames[item.folder_id] || "Untitled" : "Unassigned",
          items: [],
          // The unassigned pile is not a folder, so it has no trail at all.
          path: item.folder_id ? pathFor(item.folder_id) : undefined,
        };
      }
      grouping[key].items.push(item);
    }
    const dir = sortDir === "asc" ? 1 : -1;
    return Object.values(grouping)
      .sort((a, b) => a.title.localeCompare(b.title) * dir)
      .map(group => ({
        ...group,
        items: group.items.sort((a, b) => a.filename.localeCompare(b.filename)),
      }));
  }, [items, folderNames, pathFor, sortDir]);

  const typeGroups = useMemo<GroupBucket[]>(() => {
    if (sortKey !== "type") return [];
    const grouping: Record<string, GroupBucket> = {};
    for (const item of items) {
      const ext = extOf(item.filename) || "other";
      if (!grouping[ext]) {
        grouping[ext] = {
          id: `type:${ext}`,
          title: ext === "other" ? "Other" : ext.toUpperCase(),
          items: [],
        };
      }
      grouping[ext].items.push(item);
    }
    const dir = sortDir === "asc" ? 1 : -1;
    return Object.values(grouping)
      .sort((a, b) => a.title.localeCompare(b.title) * dir)
      .map(group => ({
        ...group,
        items: group.items.sort((a, b) => a.filename.localeCompare(b.filename)),
      }));
  }, [items, sortKey, sortDir]);

  const toggleGroup = (id: string) => {
    setCollapsedGroups(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleGroupExpanded = (id: string) => {
    setExpandedGroups(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return {
    sortKey,
    setSortKey,
    sortDir,
    setSortDir,
    sortedItems,
    folderGroups,
    typeGroups,
    collapsedGroups,
    expandedGroups,
    toggleGroup,
    toggleGroupExpanded,
    folderById,
    folderNames,
    subfolders,
  };
}
