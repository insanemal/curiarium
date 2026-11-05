import {
  Asset,
  ListAssetsResult,
  listAssets,
  listProjectAssets,
} from "../../lib/api";
import type { FolderPathSegment } from "../../lib/folderTree";
import { colsForWidth } from "../../lib/virtualGrid";

/** Sort modes offered by the toolbar; the grouped sorts render section buckets. */
export type SortKey = "name" | "size" | "creator" | "type" | "folder";
export type SortDir = "asc" | "desc";

/** Keys the server orders the whole listing by; `type`/`folder` stay client-side. */
export const SERVER_SORT_KEYS: readonly SortKey[] = ["name", "size", "creator"];

export function serverSortKey(key: SortKey): "name" | "size" | "creator" | null {
  return SERVER_SORT_KEYS.includes(key) ? (key as "name" | "size" | "creator") : null;
}

/**
 * The missing-files pin is deliberately absent from this type: a per-call
 * override is a droppable pin, and this scope must not be droppable.
 */
export type RefreshOpts = {
  tags?: string[];
  search?: string;
  keepLoadedPages?: boolean;
  notInProject?: boolean;
  /** One call's override of the files-mode Unfiled pile. */
  unfiled?: boolean;
  creator?: string | null;
  sortKey?: SortKey;
  sortDir?: SortDir;
};
/**
 * What the last storage scan said about the flag set. `unknown` covers both "no
 * scan has completed in this session" and "the advisory status read failed",
 * and it must never be rendered as "everything is on disk": the three states
 * are three different sentences.
 */
export type MissingScanOutcome = "unknown" | "clean" | "failed";

/**
 * One bucket of a grouped sort (a folder or a file type). `path` is the
 * root→leaf folder trail for a folder bucket; the project's sectioned view
 * derives its depth from it. Absent for type buckets and the unassigned pile.
 */
export type GroupBucket = {
  id: string;
  title: string;
  items: Asset[];
  path?: FolderPathSegment[];
};
export type PreviewVariant = "card" | "modal";

export function extOf(name: string) {
  const m = /\.([^.]+)$/.exec(name);
  return (m?.[1] || "").toLowerCase();
}

export const MODEL_EXTS = new Set(["stl", "3mf", "step", "stp", "obj"]);
export const LIGHTBURN_EXTS = new Set(["lbrn", "lbrn2"]);
export const ENGRAVING_EXTS = new Set([
  "svg",
  "dxf",
  "ai",
  "eps",
  "pdf",
  "plt",
  "hpgl",
  "png",
  "jpg",
  "jpeg",
  "bmp",
  "gif",
  "tif",
  "tiff",
  "lbrn",
  "lbrn2",
  "ezd",
]);

export const ROWS_PER_BATCH = 5;
/**
 * First-load page size in project mode: `MEMBER_PAGE_SIZE` in `ProjectLanding`
 * asks for the same ceiling, and it is `MAX_ASSET_PAGE_LIMIT` (1000). Every part
 * of a project must arrive in one page, because the sectioned view counts the
 * loaded rows: a 60-part project split across pages would under-report every
 * section count on the first page.
 */
export const PROJECT_PAGE_SIZE = 1000;
/** Cards rendered per group before the "Show all" affordance is offered. */
export const GROUP_CARD_CAP = 24;
/** Upper bound on how long a re-scan triggered from a card is polled. */
export const RESCAN_TIMEOUT_MS = 10 * 60 * 1000;
export const RESCAN_POLL_INTERVAL_MS = 1000;

export function formatMissingSince(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

/**
 * First-load page size: a handful of card rows. Uses the same `colsForWidth`
 * as the virtualization so the page size and the rendered row width can never
 * disagree.
 */
export function rowsBatchSizeForWidth(width: number) {
  return colsForWidth(width) * ROWS_PER_BATCH;
}

/** Placeholder so the folder nav props stay non-optional inside the grid. */
export function noop(): void {}

/**
 * The one place that decides between the folder-scoped and project-scoped asset
 * listing. A project listing takes no folder scope because membership IS the
 * scope; the folder tree is fetched separately and used only for presentation
 * (the sectioned view), never to narrow the listing. So `folderId` and `unfiled`
 * are ignored on the project branch. `notInProject` is files-only: a project
 * listing is already scoped by membership. `unfiled` narrows the files listing
 * to the library root and is mutually exclusive with `folderId`.
 */
export function fetchAssetPage(opts: {
  projectId?: string | null;
  q?: string;
  tags?: string[];
  folderId?: string;
  unfiled?: boolean;
  missing?: boolean;
  notInProject?: boolean;
  creator?: string;
  sort?: "name" | "size" | "creator";
  order?: "asc" | "desc";
  limit?: number;
  offset?: number;
}): Promise<ListAssetsResult> {
  if (opts.projectId) {
    return listProjectAssets({
      projectId: opts.projectId,
      q: opts.q,
      tags: opts.tags,
      creator: opts.creator,
      sort: opts.sort,
      order: opts.order,
      limit: opts.limit,
      offset: opts.offset,
    });
  }
  return listAssets({
    q: opts.q,
    tags: opts.tags,
    // Never both: the server rejects an unfiled listing that names a folder.
    folder_id: opts.unfiled ? undefined : opts.folderId,
    unfiled: opts.unfiled,
    // A project has no missing queue, so `missing` is ignored on that branch.
    missing: opts.missing,
    not_in_project: opts.notInProject,
    creator: opts.creator,
    sort: opts.sort,
    order: opts.order,
    limit: opts.limit,
    offset: opts.offset,
  });
}
