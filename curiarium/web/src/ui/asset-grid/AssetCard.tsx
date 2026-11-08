import React from "react";

import type { Asset } from "../../lib/api";
import { useFileGrants } from "../../lib/useFileGrants";
import TagBadge from "../TagBadge";
import CardMenu, { type CardMenuItem, type CardMenuSection } from "./CardMenu";
import { ENGRAVING_EXTS, MODEL_EXTS, extOf } from "./assetGridMeta";
import { cardFacts } from "./cardFacts";
import { PreparedPrintSummary, renderPreviewContent } from "./preview";
import type { AssetCardProps } from "./assetCardProps";

type Props = AssetCardProps & {
  /**
   * Project mode only: contributor-level membership removal. Never a destroy
   * capability, so it is absent in Files mode by construction.
   */
  onRemoveFromProject?: (asset: Asset) => void;
};

const AssetCard = React.memo(function AssetCard({
  item,
  canContribute,
  canDelete,
  onPreview,
  onOpenDetails,
  onDownloadSingle,
  onDownloadByTag,
  downloading,
  onDelete,
  onAddToProject,
  onRemoveFromProject,
  selected,
  onToggleSelected,
  slicerEnabled,
  slicerLabel,
  onOpenInSlicer,
  engravingEnabled,
  engraverLabel,
  onOpenInEngraving,
  theme,
  previewMode,
  onAssetChanged,
  onForgetMissing,
  onRescanMissing,
  onExtractArchive,
  rescanning,
  onClearPreparedPrint,
  preparedClearing,
}: Props) {
  // The card's own <img>/model preview re-reads the grant cache on every render,
  // so the cache has to stay warm while the card is mounted: a card that
  // re-renders after expiry (a selection toggle) would otherwise swap a working
  // `src` for an unsigned one, and a remounted row (the virtualized grid keys
  // rows by index, so scrolling out unmounts the cards) starts cold. The result
  // is discarded: a card must never hold back its layout.
  useFileGrants([item.url, item.thumb_url]);

  const ext = extOf(item.filename);
  const canOpenInSlicer = slicerEnabled && (Boolean(item.prepared_print) || MODEL_EXTS.has(ext));
  const canOpenInEngraving = engravingEnabled && ENGRAVING_EXTS.has(ext);

  const sections: CardMenuSection[] = [
    {
      items: [
        { label: "Open preview", onSelect: () => onPreview(item) },
        { label: "Download", onSelect: () => onDownloadSingle(item), disabled: downloading },
      ],
    },
  ];

  if (item.tags.length) {
    sections.push({
      label: "Download by tag",
      items: item.tags.map(tag => ({
        label: `Download tag: ${tag}`,
        onSelect: () => onDownloadByTag(tag),
      })),
    });
  }

  const toolItems: CardMenuItem[] = [];
  // Adding a file to a project is a contribute write, exactly as it was on the
  // old card and as the bulk bar still gates it.
  if (canContribute) {
    toolItems.push({ label: "Add to project…", onSelect: () => onAddToProject(item) });
    // The same predicate the preview footer uses (`ui/asset-grid/preview.tsx:404`):
    // `storage_path` is filled in by a scan or an upload but is absent on
    // mount/reference rows, whose file is not the library's own copy. A
    // reference-row `.zip` therefore gets no item rather than a wrong path.
    if (ext === "zip" && item.storage_path) {
      toolItems.push({ label: "Extract archive…", onSelect: () => onExtractArchive(item) });
    }
  }
  if (canOpenInSlicer) {
    toolItems.push({ label: `Open in ${slicerLabel}`, onSelect: () => onOpenInSlicer(item) });
  }
  if (canOpenInEngraving) {
    toolItems.push({ label: `Open in ${engraverLabel}`, onSelect: () => onOpenInEngraving(item) });
  }
  if (toolItems.length) {
    sections.push({ items: toolItems });
  }

  const editItems: CardMenuItem[] = [];
  if (canDelete) {
    editItems.push({ label: "Rename…", onSelect: () => onOpenDetails(item, "rename") });
  }
  if (canContribute) {
    editItems.push({ label: "Edit tags…", onSelect: () => onOpenDetails(item, "tags") });
    editItems.push({ label: "Edit details…", onSelect: () => onOpenDetails(item, "details") });
  }
  editItems.push({
    label: `Supporting files (${item.supporting_file_count})…`,
    onSelect: () => onOpenDetails(item, "files"),
  });
  sections.push({ items: editItems });

  if (onRemoveFromProject) {
    // Its own labelled group, never the danger group and never the slot Delete
    // occupies in Files mode: unlinking a membership must not read as a delete.
    sections.push({
      label: "Project",
      items: [{ label: "Remove from project", onSelect: () => onRemoveFromProject(item) }],
    });
  }

  const destructiveItems: CardMenuItem[] = [];
  if (item.prepared_print && canDelete && item.prepared_print.removable) {
    destructiveItems.push({
      label: "Clear prepared print",
      onSelect: () => onClearPreparedPrint(item),
      disabled: preparedClearing,
    });
  }
  if (item.missing_since && canDelete) {
    destructiveItems.push({
      label: "Re-scan",
      onSelect: () => onRescanMissing(item),
      disabled: rescanning,
    });
    destructiveItems.push({ label: "Forget entry", onSelect: () => onForgetMissing(item) });
  }
  if (canDelete) {
    destructiveItems.push({ label: "Delete", onSelect: () => onDelete(item), danger: true });
  }
  if (destructiveItems.length) {
    sections.push({ items: destructiveItems });
  }

  return (
    <div
      className="rounded-lg border border-panel overflow-hidden bg-panel-soft"
      data-asset-id={item.id}
    >
      <div
        className="h-40 relative cursor-pointer"
        onDoubleClick={() => onPreview(item)}
        title="Double-click to open large preview"
        onClick={event => event.stopPropagation()}
      >
        {renderPreviewContent(item, "card", theme, previewMode, onAssetChanged)}
      </div>
      {item.missing_since && (
        <div
          role="status"
          className="m-3 mb-0 rounded-md border border-red-300 bg-red-50 dark:bg-red-950/40 px-3 py-2 text-sm"
        >
          <span className="font-semibold text-red-700 dark:text-red-300">File missing</span>
        </div>
      )}
      <div className="p-3 flex flex-col gap-2">
        {item.prepared_print && <PreparedPrintSummary prepared={item.prepared_print} />}
        <div className="text-sm font-medium truncate" title={`${item.name} · ${item.filename}`}>
          {item.name}
        </div>
        <div className="text-[11px] truncate text-subtle" data-card-facts>
          {cardFacts(item).join(" · ")}
        </div>
        <div className="text-[11px] truncate text-subtle" title={item.filename}>
          {item.filename}
        </div>
        {(item.creator || item.created_by) && (
          <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
            {item.creator && <span>By {item.creator}</span>}
            {item.created_by && <span>Added by {item.created_by}</span>}
          </div>
        )}
        <div className="flex flex-wrap gap-1">
          {item.tags.length ? (
            item.tags.map(tag => <TagBadge key={tag} tag={tag} />)
          ) : (
            <span className="text-xs opacity-60">No tags</span>
          )}
        </div>
        <div className="flex items-center justify-between gap-2">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={selected}
              onChange={() => onToggleSelected(item.id)}
              className="w-4 h-4"
            />
            <span>Select</span>
          </label>
          <CardMenu label="More actions" sections={sections} />
        </div>
      </div>
    </div>
  );
});

export default AssetCard;
