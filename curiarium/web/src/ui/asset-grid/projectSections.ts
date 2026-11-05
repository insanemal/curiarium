import type { Asset } from "../../lib/api";
import type { FolderPathSegment } from "../../lib/folderTree";
import type { GroupBucket } from "./assetGridMeta";

/** Group title for parts that sit directly at the project's (trimmed) root. */
export const TOP_LEVEL_TITLE = "At the top level";

/** Group title when no member carries a folder path at all (a folder-less project). */
export const ALL_PARTS_TITLE = "All parts";

/** One rendered heading inside a group: the level below the group's own folder. */
export type ProjectSection = { key: string; title: string; items: Asset[] };

/** One top-level folder of the project, as the filter row sees it. */
export type ProjectGroup = {
  id: string;
  title: string;
  count: number;
  sections: ProjectSection[];
};

export type ProjectSectionModel = { groups: ProjectGroup[]; total: number };

/**
 * The project's two-level folder shape, derived from the folder buckets the
 * listing already produced. No React, no requests: this is a pure function of
 * what is loaded.
 *
 * The project root is the longest common prefix of every bucket's folder path,
 * so it works whether or not a Folder row exists for the project folder itself.
 * That leaves four shapes:
 * - two levels below the root → a group with one section per child folder;
 * - one level below the root → the group IS the section (its header is the
 *   group title, the section title is empty);
 * - zero levels (parts directly at the trimmed root) → one group titled
 *   "At the top level";
 * - no bucket carries a path at all → a single group titled "All parts" over
 *   every part, which the grid renders as the unchanged flat grid rather than
 *   as a lone header.
 *
 * The section layout follows the project's structure, not the sort control:
 * unlike the `Folder` sort it never reads `sortDir`. Item order INSIDE a
 * section is the loaded listing's order: pass `order` (the loaded items) and
 * each section keeps that order, so the toolbar's server-backed sort still
 * applies. Omit it and a section keeps the bucket's own order (by filename).
 */
export function projectSectionModel(
  buckets: GroupBucket[],
  order?: readonly Asset[],
): ProjectSectionModel {
  const populated = buckets.filter(bucket => bucket.items.length > 0);
  if (populated.length === 0) return { groups: [], total: 0 };

  const prefixLength = commonPrefixLength(populated);
  // A project whose members are all unfiled has no folder structure at all, so
  // its lone group is the whole project rather than "parts at the root".
  const pathless = populated.every(bucket => !bucket.path || bucket.path.length === 0);
  const groupsById = new Map<string, ProjectGroup>();
  let total = 0;

  for (const bucket of populated) {
    const relative = relativeSegments(bucket, prefixLength);
    const groupFolder = relative[0];
    const groupId = `group:${groupFolder?.id ?? "__root"}`;
    const groupTitle = groupFolder?.name ?? (pathless ? ALL_PARTS_TITLE : TOP_LEVEL_TITLE);
    const below = relative.slice(1);
    const sectionTitle = below.map(segment => segment.name).join(" / ");
    // Keyed by folder id, not title: two sibling folders both named "Core"
    // must stay two sections instead of merging under one heading.
    const sectionKey = `${groupId}:${below.map(segment => segment.id).join("/") || "__group"}`;

    let group = groupsById.get(groupId);
    if (!group) {
      group = { id: groupId, title: groupTitle, count: 0, sections: [] };
      groupsById.set(groupId, group);
    }
    let section = group.sections.find(candidate => candidate.key === sectionKey);
    if (!section) {
      section = { key: sectionKey, title: sectionTitle, items: [] };
      group.sections.push(section);
    }

    // A plain loop, not `push(...items)`: the spread's argument count is bounded
    // by the engine, and `PROJECT_PAGE_SIZE` may one day be raised.
    for (const item of bucket.items) section.items.push(item);
    group.count += bucket.items.length;
    total += bucket.items.length;
  }

  const groups = [...groupsById.values()].sort(compareGroups);
  for (const group of groups) group.sections.sort(compareSections);
  if (order) sortSectionsByLoadedOrder(groups, order);
  return { groups, total };
}

/**
 * Re-orders each section's loaded items to the listing's own order. Section
 * ORDER stays title-based; only the item sequence inside a section follows the
 * server sort the toolbar asked for.
 */
function sortSectionsByLoadedOrder(groups: ProjectGroup[], order: readonly Asset[]): void {
  const position = new Map<string, number>();
  order.forEach((item, index) => position.set(item.id, index));
  for (const group of groups) {
    for (const section of group.sections) {
      section.items.sort(
        (a, b) =>
          (position.get(a.id) ?? Number.MAX_SAFE_INTEGER) -
          (position.get(b.id) ?? Number.MAX_SAFE_INTEGER),
      );
    }
  }
}

/** Trail below the project root. A bucket without a path sits at that root. */
function relativeSegments(bucket: GroupBucket, prefixLength: number): FolderPathSegment[] {
  const path = bucket.path ?? [];
  return path.slice(prefixLength);
}

/**
 * Length of the longest root→leaf prefix shared, segment by segment on id, by
 * every bucket that has a path. Buckets without one never extend it, and id
 * (not name) is the comparison so two folders sharing a name stay distinct.
 */
function commonPrefixLength(buckets: GroupBucket[]): number {
  const paths = buckets
    .map(bucket => bucket.path ?? [])
    .filter(path => path.length > 0);
  if (paths.length === 0) return 0;

  let shortest = paths[0].length;
  for (const path of paths) {
    if (path.length < shortest) shortest = path.length;
  }
  for (let index = 0; index < shortest; index += 1) {
    const id = paths[0][index].id;
    if (paths.some(path => path[index].id !== id)) return index;
  }
  return shortest;
}

function compareGroups(a: ProjectGroup, b: ProjectGroup): number {
  const byTitle = a.title.localeCompare(b.title);
  return byTitle !== 0 ? byTitle : a.id.localeCompare(b.id);
}

function compareSections(a: ProjectSection, b: ProjectSection): number {
  const byTitle = a.title.localeCompare(b.title);
  return byTitle !== 0 ? byTitle : a.key.localeCompare(b.key);
}
