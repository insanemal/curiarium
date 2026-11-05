import { useMemo, type RefObject } from "react";

import { type Asset, type Folder } from "../../lib/api";
import { GRID_GAP_PX } from "../../lib/virtualGrid";
import { type FilesScopeKind } from "../filesScope";
import type { VirtualRowsResult } from "../useVirtualRows";
import {
  GROUP_CARD_CAP,
  SERVER_SORT_KEYS,
  type GroupBucket,
  type MissingScanOutcome,
  type SortKey,
} from "./assetGridMeta";
import ProjectFolderSections from "./ProjectFolderSections";
import { projectSectionModel } from "./projectSections";

type AssetGridBodyProps = {
  /** Virtualization result from the root; its refs are attached verbatim. */
  virtual: VirtualRowsResult;
  renderedRows: Asset[][];
  renderCard: (item: Asset) => React.ReactNode;
  sortKey: SortKey;
  folderGroups: GroupBucket[];
  typeGroups: GroupBucket[];
  collapsedGroups: Set<string>;
  expandedGroups: Set<string>;
  toggleGroup: (id: string) => void;
  toggleGroupExpanded: (id: string) => void;
  isProject: boolean;
  /** The Files scope on screen, derived once by `filesScope`; the body only picks copy. */
  scopeKind: FilesScopeKind;
  /** Pinned missing-files mode; only affects the empty-state copy here. */
  missingOnly?: boolean;
  /** What the last scan said about the flag set, for the empty-state copy. */
  missingScan?: MissingScanOutcome;
  subfolders: Folder[];
  loading: boolean;
  /** Retryable first-load failure; null when the listing loaded or was never attempted. */
  error?: string | null;
  /** Re-issues the first page (the error panel's "Try again"). */
  onRetry: () => void;
  items: Asset[];
  queryRef: RefObject<string>;
  activeTags: string[];
  activeCreator: string | null;
  notInProject: boolean;
  hasMore: boolean;
  loadingMore: boolean;
  loadMore: () => void;
};

/**
 * The result area: the project's folder-sectioned view, grouped sections for
 * the grouped sorts, or the virtualized flat grid otherwise, plus the empty
 * hints and the "Load more" control. Owns no effects and no state; every value
 * comes from the root (the project filter's own state lives in its child, and
 * the section model is a `useMemo` derivation, not state).
 */
export default function AssetGridBody({
  virtual,
  renderedRows,
  renderCard,
  sortKey,
  folderGroups,
  typeGroups,
  collapsedGroups,
  expandedGroups,
  toggleGroup,
  toggleGroupExpanded,
  isProject,
  scopeKind,
  missingOnly,
  missingScan,
  subfolders,
  loading,
  error,
  onRetry,
  items,
  queryRef,
  activeTags,
  activeCreator,
  notInProject,
  hasMore,
  loadingMore,
  loadMore,
}: AssetGridBodyProps) {
  // Any active filter means an empty list is an empty RESULT, never an empty
  // set: a search, tag chips, a creator or "not in any project" can all empty
  // the rows while the queue itself is not empty.
  const narrowed =
    Boolean(queryRef.current) || activeTags.length > 0 || activeCreator !== null || notInProject;

  // The scope kind is derived once by `filesScope` and handed down, so these
  // empty states cannot disagree with the breadcrumb or the drop policy. Only
  // the kind is needed here: the three Files scopes stay mutually exclusive by
  // construction, and `filesActive` keeps project and missing mode clear of
  // every Files-scope empty state below.
  const filesActive = !isProject && !missingOnly;
  const folderScope = filesActive && scopeKind === "folder";
  const unfiledScope = filesActive && scopeKind === "unfiled";
  const allFilesScope = filesActive && scopeKind === "all";
  const settled = !loading && !items.length && !error;

  // A project's parts section by folder, two levels deep, from the folder tree
  // the listing hook fetched for this mode. The second argument is the loaded
  // order, so each section keeps the server sort the toolbar asked for. One
  // group means there is nothing to filter between (a folder-less project
  // included), so the flat grid keeps it.
  const sections = useMemo(() => projectSectionModel(folderGroups, items), [folderGroups, items]);
  // The sectioned view is the project's structure layered on a server-ordered
  // listing, so it applies exactly to the server-backed sorts. The grouped
  // sorts (`folder`, `type`) keep their own branches below: `File type` is
  // offered in project mode's toolbar and renders today's grouped branch, not
  // the folder sections.
  const sectionedProject =
    isProject && sections.groups.length >= 2 && SERVER_SORT_KEYS.includes(sortKey);

  return (
    <>
      {/* Pinned mode has no toolbar-sourced rows to fall back on, so the queue
          owns a first-load line: the toolbar's "Loading..." describes the
          toolbar, not the queue. */}
      {missingOnly && loading && !items.length && (
        <p className="text-sm opacity-70" data-missing-loading>Loading missing files…</p>
      )}

      {/* A failed fetch is not an empty result. Without this the grid looked
          identical to a genuinely empty one, so the missing empty-states below
          would claim "No missing files flagged" on top of a failed request. */}
      {error && !loading && !items.length && (
        <div className="text-sm" data-grid-error>
          <p className="font-medium">Could not load the files</p>
          <p className="opacity-70">{error}</p>
          <button
            type="button"
            className="mt-2 px-3 py-2 rounded-md border border-panel-strong text-sm"
            onClick={onRetry}
          >
            Try again
          </button>
        </div>
      )}

      {/* Flag-truth when the scan status is unknown: an unknown scan is not
          evidence that nothing is missing, so the "on disk" sentence renders
          only for `clean`. The repo has already shipped two labels that lied
          about an empty list; this is not the third. */}
      {missingOnly && !loading && !items.length && !error && narrowed && (
        <div className="text-sm opacity-70" data-missing-empty-filter>
          <p className="font-medium">No missing files match this filter</p>
          <p>Clear the search and the filters to see the whole queue.</p>
        </div>
      )}
      {missingOnly && !loading && !items.length && !error && !narrowed && (
        <div className="text-sm opacity-70" data-missing-empty={missingScan ?? "unknown"}>
          <p className="font-medium">No missing files flagged</p>
          {missingScan === "clean" ? (
            <p>Every file an asset points at is on disk.</p>
          ) : missingScan === "failed" ? (
            <p>The last scan did not finish, so some missing files may not be listed yet. Run a scan again before trusting an empty queue.</p>
          ) : (
            <p>Nothing is flagged as missing yet. A scan is what finds files that disappeared, and no completed scan is known for this session.</p>
          )}
        </div>
      )}

      {/* The Unfiled row exists to answer "what is still loose?": an empty pile
          is the good outcome, so say so instead of leaving the page blank. But
          an empty result is not an empty pile: any filter that matched nothing
          must not claim every asset is filed while the sidebar count disagrees. */}
      {unfiledScope && settled && !narrowed && (
        <div className="text-sm opacity-70" data-unfiled-empty>
          <p className="font-medium">Nothing unfiled</p>
          <p>Every asset is in a folder.</p>
        </div>
      )}
      {unfiledScope && settled && narrowed && (
        <div className="text-sm opacity-70" data-unfiled-empty-filter>
          <p className="font-medium">No unfiled files match this filter</p>
          <p>Clear the search and the filters to see everything at the library root.</p>
        </div>
      )}

      {/* "All files" spans every folder, so an empty list there says something
          about the whole library, and only when nothing narrows it. */}
      {allFilesScope && settled && !narrowed && (
        <div className="text-sm opacity-70" data-all-files-empty>
          <p className="font-medium">The library is empty</p>
          <p>Upload to Unfiled or a folder to get started.</p>
        </div>
      )}
      {allFilesScope && settled && narrowed && (
        <div className="text-sm opacity-70" data-all-files-empty-filter>
          <p className="font-medium">No files match this filter</p>
          <p>Clear the search and the filters to see the whole library.</p>
        </div>
      )}

      {/* A folder holding only subfolders must never look broken (#15). Applies
          to both the flat and the grouped sort modes. When a filter is what
          emptied the view, sending the user to the tiles above would be wrong
          advice, so the narrowed case gets its own line. */}
      {folderScope && settled && !narrowed && subfolders.length === 0 && (
        <div className="text-sm opacity-70" data-folder-empty>
          <p className="font-medium">Nothing in this folder yet</p>
          <p>This view lists the folder's own files. Files in its subfolders are not included.</p>
        </div>
      )}
      {folderScope && settled && !narrowed && subfolders.length > 0 && (
        <p className="text-sm opacity-70">
          No files directly in this folder — pick a subfolder above.
        </p>
      )}
      {folderScope && settled && narrowed && (
        <div className="text-sm opacity-70" data-folder-empty-filter>
          <p className="font-medium">No files in this folder match this filter</p>
          <p>Clear the search and the filters to see the folder's direct files.</p>
        </div>
      )}

      {/* The project is the last scope to route through the shared `narrowed`
          flag rather than its own hand-rolled predicate: that flag already
          folds the search, the tag chips, the creator and `notInProject`, so a
          creator or tag filter that empties the grid is an empty RESULT and no
          longer claims the project is empty. `notInProject` is defensive here
          because its checkbox is files-only and each grid owns its own
          `useAssetList` state, so it is always false in project mode.
          `settled` already carries `!error`, so this branch must never re-grow
          its own `&& !error` (the Mf-qzx5 failure mode) or a failed fetch would
          paint an empty project over the error panel. */}
      {isProject && settled && !narrowed && (
        <p className="text-sm opacity-70" data-project-empty>
          This project has no files yet.
        </p>
      )}
      {isProject && settled && narrowed && (
        <div className="text-sm opacity-70" data-project-empty-filter>
          <p className="font-medium">No files in this project match this filter</p>
          <p>Clear the search and the filters to see the whole project.</p>
        </div>
      )}

      {sectionedProject ? (
        <ProjectFolderSections
          groups={sections.groups}
          renderCard={renderCard}
          hasMore={hasMore}
        />
      ) : ["folder", "type"].includes(sortKey) ? (
        <div className="space-y-4">
          {(sortKey === "folder" ? folderGroups : typeGroups).map(group => {
            const collapsed = collapsedGroups.has(group.id);
            const expanded = expandedGroups.has(group.id);
            const visibleItems = expanded
              ? group.items
              : group.items.slice(0, GROUP_CARD_CAP);
            return (
              <div
                key={group.id}
                className="rounded-lg border border-panel bg-panel-soft"
              >
                <button
                  className="w-full flex items-center justify-between px-4 py-3 text-left"
                  onClick={() => toggleGroup(group.id)}
                >
                  <div>
                    <div className="font-medium">{group.title}</div>
                    <div className="text-xs opacity-70">{group.items.length} item{group.items.length === 1 ? "" : "s"}</div>
                  </div>
                  <span className="text-sm opacity-70">{collapsed ? "Show" : "Hide"}</span>
                </button>
                {!collapsed && (
                  <div className="px-4 pb-4 overflow-x-auto">
                    <div className="flex gap-4 min-h-[280px]">
                      {visibleItems.map(it => (
                        <div key={it.id} className="min-w-[260px] max-w-[320px]">
                          {renderCard(it)}
                        </div>
                      ))}
                      {group.items.length > GROUP_CARD_CAP && (
                        <div className="min-w-[140px] shrink-0 flex items-center justify-center">
                          <button
                            type="button"
                            className="px-3 py-2 rounded-md border border-panel-strong text-sm"
                            onClick={() => toggleGroupExpanded(group.id)}
                          >
                            {expanded ? "Show fewer" : `Show all ${group.items.length}`}
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <div
          ref={virtual.setGridWrapRef}
          data-virtual-grid
          style={{ display: "flex", flexDirection: "column", gap: GRID_GAP_PX }}
        >
          {virtual.startRow > 0 && (
            <div data-virtual-spacer="top" aria-hidden="true" style={{ height: virtual.topSpacer }} />
          )}
          {renderedRows.map((row, offset) => {
            const rowIndex = virtual.startRow + offset;
            return (
              <div
                key={rowIndex}
                data-row-index={rowIndex}
                ref={virtual.setRowRef(rowIndex)}
                className="grid gap-4"
                style={{ gridTemplateColumns: `repeat(${virtual.cols}, minmax(0, 1fr))` }}
              >
                {row.map(item => renderCard(item))}
              </div>
            );
          })}
          {virtual.endRow >= 0 && virtual.endRow < virtual.rowCount - 1 && (
            <div
              data-virtual-spacer="bottom"
              aria-hidden="true"
              style={{ height: virtual.bottomSpacer }}
            />
          )}
        </div>
      )}
      {hasMore && (
        <div className="flex justify-center pt-2">
          <button
            className="px-4 py-2 rounded-md border border-panel-strong text-sm disabled:opacity-60"
            onClick={loadMore}
            disabled={loadingMore}
          >
            {loadingMore ? "Loading..." : "Load more"}
          </button>
        </div>
      )}
    </>
  );
}
