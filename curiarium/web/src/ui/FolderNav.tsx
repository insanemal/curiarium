import { Fragment, type ReactNode } from "react";
import { ChevronRight, Folder as FolderIcon } from "lucide-react";

import type { Folder } from "../lib/api";
import { childFolders, folderPath } from "../lib/folderTree";
import { filesScope } from "./filesScope";

type FolderNavProps = {
  folders: readonly Folder[];
  folderId: string | null;
  onNavigate: (id: string | null) => void;
};

type FolderBreadcrumbsProps = FolderNavProps & {
  /** The library-root scope is the active one, so the trail names it. */
  unfiled?: boolean;
  /**
   * AssetGrid has no unfiled-selecting callback yet: the current "Unfiled"
   * crumb only becomes a working control once a caller wires this up.
   */
  onSelectUnfiled?: () => void;
};

const crumbBaseClass = "px-2 py-1 rounded-md border border-panel-strong text-sm";
const crumbLinkClass = `${crumbBaseClass} hover:bg-panel-strong`;

// Legacy rows migrated from v3 keep a NULL storage path by design when their
// assets cannot be pinned to one directory, so the tile says so instead of
// implying every folder owns a directory.
const NO_DIRECTORY_NOTE = "No directory on disk - files are grouped by metadata only";

/**
 * The Files-scope trail: the current scope, plus its ancestors when a folder is
 * open. Navigation only: the caller owns the folder state, so every folder
 * crumb is a plain button (no links, no history entries). The label, the note
 * and the drop policy all come from `filesScope`, so the trail cannot describe a
 * different scope than the sidebar accepts drops for.
 */
export default function FolderBreadcrumbs({
  folders,
  folderId,
  onNavigate,
  unfiled = false,
  onSelectUnfiled,
}: FolderBreadcrumbsProps) {
  const scope = filesScope({ folderId, unfiled, folders });
  const segments = folderId ? folderPath(folders, folderId) : [];
  // Only the folder scope depends on the folder list: a lone "All files" crumb
  // while it loads would misdescribe the trail. The All-files and Unfiled
  // scopes are knowable without it, so they always render.
  if (folderId && !segments.length) return null;

  let trail: ReactNode;
  if (scope.kind === "all") {
    // A button that navigates to the scope you are already in is a dead
    // control, so the single crumb states the scope and does nothing.
    trail = (
      <span data-scope-current aria-current="page" className={crumbBaseClass}>
        {scope.label}
      </span>
    );
  } else if (scope.kind === "unfiled") {
    trail = (
      <>
        <button type="button" className={crumbLinkClass} onClick={() => onNavigate(null)}>
          All files
        </button>
        <ChevronRight className="h-4 w-4 opacity-60" aria-hidden="true" />
        {/* A button for the same reason a folder's current crumb is one, and
            inert until a caller wires `onSelectUnfiled`: App owns scope
            selection, so the grid has no unfiled destination to give yet. */}
        <button
          type="button"
          data-scope-current
          aria-current="page"
          className={crumbLinkClass}
          onClick={onSelectUnfiled}
        >
          {scope.label}
        </button>
      </>
    );
  } else {
    trail = (
      <>
        <button type="button" className={crumbLinkClass} onClick={() => onNavigate(null)}>
          All files
        </button>
        {segments.map((segment, index) => {
          const isCurrent = index === segments.length - 1;
          // Title is the full trail up to this crumb, which disambiguates
          // same-named folders living in different branches.
          const title = segments.slice(0, index + 1).map(part => part.name).join(" / ");
          return (
            <Fragment key={segment.id}>
              <ChevronRight className="h-4 w-4 opacity-60" aria-hidden="true" />
              <button
                type="button"
                data-folder-crumb={segment.id}
                title={title}
                aria-current={isCurrent ? "page" : undefined}
                className={crumbLinkClass}
                onClick={() => onNavigate(segment.id)}
              >
                {segment.name}
              </button>
            </Fragment>
          );
        })}
      </>
    );
  }

  return (
    <>
      <nav
        aria-label="Files scope"
        data-folder-breadcrumbs
        className="flex flex-wrap items-center gap-1 text-sm"
      >
        {trail}
      </nav>
      {/* The nav is a link landmark, so the scope note is its sibling. */}
      <p data-scope-note className="text-sm opacity-70">
        {scope.note}
      </p>
    </>
  );
}

/**
 * Direct subfolders of the open folder as cards. Without them a folder with
 * only subfolders (and no direct assets) looks empty.
 */
export function FolderTiles({ folders, folderId, onNavigate }: FolderNavProps) {
  if (!folderId) return null;

  const children = childFolders(folders, folderId);
  if (!children.length) return null;

  return (
    <section
      aria-label="Subfolders"
      data-folder-tiles
      className="rounded-lg border border-panel bg-panel-soft p-4"
    >
      <h2 className="mb-3 text-sm font-medium opacity-80">Subfolders</h2>
      <div
        className="grid gap-4"
        style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))" }}
      >
        {children.map(child => {
          const subfolderCount = childFolders(folders, child.id).length;
          const noDirectory = !child.storage_path;
          return (
            <button
              key={child.id}
              type="button"
              data-folder-tile={child.id}
              data-folder-tile-no-directory={noDirectory ? "true" : undefined}
              title={noDirectory ? NO_DIRECTORY_NOTE : undefined}
              onClick={() => onNavigate(child.id)}
              // jsdom does not synthesize a click from Enter/Space on a button;
              // handle the keys explicitly so keyboard navigation works there.
              onKeyDown={event => {
                if (event.key !== "Enter" && event.key !== " ") return;
                event.preventDefault();
                onNavigate(child.id);
              }}
              className="flex items-center gap-3 rounded-lg border border-panel-strong bg-panel px-4 py-3 text-left hover:bg-panel-strong"
            >
              <FolderIcon className="h-5 w-5 shrink-0 opacity-80" aria-hidden="true" />
              <span className="min-w-0 flex flex-col">
                <span className="truncate font-medium">{child.name || "Untitled"}</span>
                {noDirectory && (
                  <>
                    <span className="text-xs opacity-70">No directory</span>
                    {/* The tooltip alone is not an accessible note. */}
                    <span className="sr-only">{NO_DIRECTORY_NOTE}</span>
                  </>
                )}
                {subfolderCount > 0 && (
                  <span className="text-xs opacity-70">
                    {subfolderCount} subfolder{subfolderCount === 1 ? "" : "s"}
                  </span>
                )}
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
