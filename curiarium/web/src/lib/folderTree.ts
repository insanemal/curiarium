import type { Folder } from "./api";

export type FolderPathSegment = { id: string; name: string };

/** Flat row of the sidebar tree. */
export type FolderTreeRow = { id: string; depth: number; hasChildren: boolean };

/** One hit of `searchFolders`: the folder, its own name, and its root→leaf trail. */
export type FolderSearchMatch = { id: string; name: string; path: string };

/** `childrenIndex` key holding the folders that sit at the top level. */
export const ROOT_GROUP = "__root";

const UNTITLED = "Untitled";
const PATH_SEPARATOR = " / ";

function nameOf(folder: Folder): string {
  return folder.name || UNTITLED;
}

/** Fallback name first, then id so equal names keep a stable order. */
function compareFolders(a: Folder, b: Folder): number {
  const byName = nameOf(a).localeCompare(nameOf(b));
  if (byName !== 0) return byName;
  return a.id.localeCompare(b.id);
}

/** `folders` keyed by id; later duplicates win, matching the inlined maps in the UI. */
export function indexFolders(folders: readonly Folder[]): Record<string, Folder> {
  const map: Record<string, Folder> = {};
  for (const folder of folders) {
    map[folder.id] = folder;
  }
  return map;
}

function parentOf(folder: Folder): string | null {
  return folder.parent_id ?? null;
}

/**
 * The `parent_id` that is usable as-is: null when it is missing, unknown
 * (orphan) or points at the folder itself. Cycle detection is layered on top.
 */
function directParentOf(folder: Folder, byId: Record<string, Folder>): string | null {
  const parentId = folder.parent_id;
  if (!parentId) return null;
  if (parentId === folder.id) return null;
  if (!byId[parentId]) return null;
  return parentId;
}

/** Walks up until the chain ends; `true` means it genuinely terminates at a root. */
function terminatesAtRoot(
  folderId: string,
  byId: Record<string, Folder>,
  cache: Map<string, boolean>,
): boolean {
  const path: string[] = [];
  const seen = new Set<string>();
  let current: string | null = folderId;
  let outcome: boolean | null = null;

  while (current !== null) {
    const cached = cache.get(current);
    if (cached !== undefined) {
      outcome = cached;
      break;
    }
    if (seen.has(current)) {
      outcome = false;
      break;
    }
    seen.add(current);
    path.push(current);
    current = directParentOf(byId[current], byId);
  }

  const terminates = outcome ?? true;
  for (const id of path) cache.set(id, terminates);
  return terminates;
}

/**
 * One normalisation pass every export derives from: orphans, self-parents and
 * folders trapped in a cycle are promoted to roots so nothing is unreachable
 * and no folder can be its own ancestor.
 */
function effectiveParents(byId: Record<string, Folder>): Record<string, string | null> {
  const cache = new Map<string, boolean>();
  const parents: Record<string, string | null> = {};
  for (const id of Object.keys(byId)) {
    const folder = byId[id];
    parents[id] = terminatesAtRoot(id, byId, cache) ? directParentOf(folder, byId) : null;
  }
  return parents;
}

/**
 * Root→current trail of `folderId`. Returns `[]` for a null/unknown id, stops
 * at a missing parent, and is cycle-guarded so corrupt data cannot hang.
 */
export function folderPath(
  folders: readonly Folder[],
  folderId: string | null | undefined,
): FolderPathSegment[] {
  if (!folderId) return [];
  const byId = indexFolders(folders);
  let current = byId[folderId];
  if (!current) return [];

  const segments: FolderPathSegment[] = [{ id: current.id, name: nameOf(current) }];
  const guard = new Set<string>([current.id]);
  while (parentOf(current)) {
    const parent = byId[parentOf(current) as string];
    if (!parent || guard.has(parent.id)) break;
    segments.unshift({ id: parent.id, name: nameOf(parent) });
    guard.add(parent.id);
    current = parent;
  }
  return segments;
}

/**
 * Every folder grouped by its effective parent, roots under `ROOT_GROUP`, each
 * sibling list ordered by fallback name then id. Each id appears exactly once.
 */
export function childrenIndex(folders: readonly Folder[]): Record<string, Folder[]> {
  const byId = indexFolders(folders);
  const parents = effectiveParents(byId);
  const groups: Record<string, Folder[]> = {};

  for (const folder of Object.values(byId)) {
    const key = parents[folder.id] ?? ROOT_GROUP;
    if (!groups[key]) groups[key] = [];
    groups[key].push(folder);
  }
  for (const list of Object.values(groups)) {
    list.sort(compareFolders);
  }
  return groups;
}

/** Direct children of `parentId` (null = effective roots), ordered by name then id. */
export function childFolders(folders: readonly Folder[], parentId: string | null): Folder[] {
  return childrenIndex(folders)[parentId ?? ROOT_GROUP] ?? [];
}

/**
 * Every id reachable downward from `folderId` with effective parents, never
 * including `folderId` itself. Empty for an unknown id or a leaf.
 */
export function descendantIds(folders: readonly Folder[], folderId: string): Set<string> {
  const groups = childrenIndex(folders);
  const ids = new Set<string>();
  const stack: Folder[] = [...(groups[folderId] ?? [])];

  while (stack.length > 0) {
    const folder = stack.pop();
    if (!folder) continue;
    if (folder.id === folderId || ids.has(folder.id)) continue;
    ids.add(folder.id);
    stack.push(...(groups[folder.id] ?? []));
  }
  return ids;
}

/**
 * Depth-first rows in the sidebar's render order: roots first, siblings by
 * name, children only under an expanded (or always-expanded) parent. Subtrees
 * rejected by `isVisible` are skipped whole.
 */
export function flattenFolderTree(
  folders: readonly Folder[],
  options: {
    expanded: ReadonlySet<string>;
    alwaysExpanded?: boolean;
    isVisible?: (id: string) => boolean;
  },
): FolderTreeRow[] {
  const groups = childrenIndex(folders);
  const rows: FolderTreeRow[] = [];
  const visited = new Set<string>();

  const visit = (parentId: string, depth: number): void => {
    for (const folder of groups[parentId] ?? []) {
      if (visited.has(folder.id)) continue;
      visited.add(folder.id);
      if (options.isVisible && !options.isVisible(folder.id)) continue;

      const children = groups[folder.id] ?? [];
      rows.push({ id: folder.id, depth, hasChildren: children.length > 0 });
      if (children.length && (options.alwaysExpanded || options.expanded.has(folder.id))) {
        visit(folder.id, depth + 1);
      }
    }
  };

  visit(ROOT_GROUP, 0);
  return rows;
}

/**
 * Folders whose own name contains `query` (case-insensitive), best matches
 * first: name prefixes, then name substrings, ties by full path. `total` counts
 * every hit before the `cap` is applied.
 */
export function searchFolders(
  folders: readonly Folder[],
  query: string,
  cap = 50,
): { matches: FolderSearchMatch[]; total: number } {
  const needle = query.trim().toLowerCase();
  if (!needle) return { matches: [], total: 0 };

  const byId = indexFolders(folders);
  const hits: { match: FolderSearchMatch; rank: number }[] = [];

  for (const folder of Object.values(byId)) {
    const name = nameOf(folder);
    const haystack = name.toLowerCase();
    const isPrefix = haystack.startsWith(needle);
    if (!isPrefix && !haystack.includes(needle)) continue;
    const path = folderPath(folders, folder.id)
      .map(segment => segment.name)
      .join(PATH_SEPARATOR);
    hits.push({ match: { id: folder.id, name, path }, rank: isPrefix ? 0 : 1 });
  }

  hits.sort((a, b) => {
    if (a.rank !== b.rank) return a.rank - b.rank;
    const byPath = a.match.path.localeCompare(b.match.path);
    if (byPath !== 0) return byPath;
    return a.match.id.localeCompare(b.match.id);
  });

  return { matches: hits.slice(0, cap).map(hit => hit.match), total: hits.length };
}
