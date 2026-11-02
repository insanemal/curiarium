import React, { useEffect, useMemo, useState } from "react";

import { type Asset, type BatchFailure, type BatchMetaChange, type LibraryScanStatus, UnauthorizedError, deleteAsset, removeProjectAssets } from "../../lib/api";
import { AssetNotFoundError } from "../../lib/api/assets";
import { dropIds, failureLines } from "../../lib/bulkUi";
import { chunkRows, colsForWidth } from "../../lib/virtualGrid";
import { previewQueue } from "../../lib/previewQueue";
import { EngravingSettings, PreviewSettings, ResolvedTheme, SlicerSettings, engraverLabelFor, slicerLabelFor } from "../../lib/settings";
import { notifyError } from "../../lib/toast";
import { useStableCallback } from "../../lib/useStableCallback";
import AssetDeleteDialog, { type AssetDeleteFailure } from "../AssetDeleteDialog";
import BulkActionBar from "../BulkActionBar";
import BulkMetaDialog from "../BulkMetaDialog";
import ConfirmDialog from "../ConfirmDialog";
import FolderBreadcrumbs, { FolderTiles } from "../FolderNav";
import ProjectPickerDialog from "../ProjectPickerDialog";
import { filesScope } from "../filesScope";
import { useUpload } from "../useUpload";
import { useVirtualRows } from "../useVirtualRows";
import AssetGridBody from "./AssetGridBody";
import AssetToolbar from "./AssetToolbar";
import AssetCard from "./AssetCard";
import type { DetailSection } from "./AssetDetailsEditor";
import { AssetPreviewModal } from "./preview";
import { noop, type MissingScanOutcome } from "./assetGridMeta";
import { useAssetActions } from "./useAssetActions";
import { useAssetDrop, type DropPolicy } from "./useAssetDrop";
import { useAssetList } from "./useAssetList";
import { useAssetSorting } from "./useAssetSorting";
import { useBulkActions } from "./useBulkActions";

type Props = {
  /** Contribute capability: upload, import, tag, organise. Required, no default. */
  canContribute: boolean;
  /** Delete capability: destroy, rename and move. Required, no default. */
  canDelete: boolean;
  folderId?: string | null;
  /** Files mode: show only the library root's assets (the Unfiled row). */
  unfiled?: boolean;
  /**
   * Pinned missing-files mode: only assets a scan flagged as missing. The scope
   * lives in `useAssetList`'s closure and is absent from `RefreshOpts`, so no
   * toolbar control can drop it - the controls may only narrow further.
   */
  missingOnly?: boolean;
  /**
   * What the last storage scan said ("unknown" | "clean" | "failed"). Only
   * `clean` may claim that every file on disk is accounted for; the body's
   * empty-state copy is chosen from it.
   */
  missingScan?: MissingScanOutcome;
  /** When set, the grid shows this project's assets and hides all folder chrome. */
  projectId?: string | null;
  /** Display-only context for project mode (the caller renders the heading). */
  projectName?: string | null;
  foldersVersion?: number;
  assetsVersion?: number;
  /**
   * App-owned counts generation. Accepted for parity with `assetsVersion`, but
   * deliberately triggers no refetch here: a counts bump only means a move was
   * reconciled locally, and refetching the listing for it would be a new fetch.
   */
  countsVersion?: number;
  onUnauthorized?: () => void;
  slicerSettings?: SlicerSettings;
  engravingSettings?: EngravingSettings;
  previewSettings?: PreviewSettings;
  theme: ResolvedTheme;
  /** Scroll container the grid lives in (the app's `<main>`). */
  scrollElementRef?: React.RefObject<HTMLElement | null>;
  /** Navigate to a folder (or the root) from the breadcrumbs/tiles. */
  onNavigateFolder?: (id: string | null) => void;
  /**
   * Fires after the project picker created a project or changed its membership,
   * so App can refresh the counts it owns (it bumps `assetsVersion`).
   */
  onProjectsChanged?: () => void;
  /**
   * A single-card or bulk move landed. Moves are reconciled locally and never
   * bump `assetsVersion`, so App needs this signal to refresh `/assets/counts`.
   */
  onAssetsFiled?: () => void;
  /**
   * Fires for each status the per-card Re-scan reads, so the missing queue's
   * panel and empty state describe the scan that produced its rows.
   */
  onScanStatus?: (status: LibraryScanStatus) => void;
};

function errorMessage(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message.trim()) return err.message;
  return fallback;
}

export default function AssetGrid({
  canContribute,
  canDelete,
  folderId,
  unfiled = false,
  missingOnly = false,
  missingScan,
  projectId,
  projectName,
  foldersVersion = 0,
  assetsVersion = 0,
  // App passes its counts generation down; the grid itself refetches nothing on
  // it (see the Props comment). The underscore keeps lint honest about that.
  countsVersion: _countsVersion,
  onUnauthorized,
  slicerSettings,
  engravingSettings,
  previewSettings,
  theme,
  scrollElementRef,
  onNavigateFolder,
  onProjectsChanged,
  onAssetsFiled,
  onScanStatus,
}: Props) {
  const isProject = projectId != null;
  // Project membership is not asset ownership: removing a member drops a join
  // row, never the file. `DELETE /project/{id}/assets` is a CONTRIBUTE route
  // while `DELETE /asset/{id}` is WRITE, so in project mode no child may hold
  // the destroy capability at any role. Nothing below may hand raw `canDelete`
  // to a project-mode child.
  const canDeleteAssets = canDelete && !isProject;
  const [previewItem, setPreviewItem] = useState<Asset | null>(null);
  const [previewSection, setPreviewSection] = useState<DetailSection | null>(null);
  // The assets the user asked to delete, or null when the dialog is closed. The
  // parent stays the only owner of the request so the grid's optimistic update
  // and selection retention keep a single path.
  const [deleteTargets, setDeleteTargets] = useState<Asset[] | null>(null);
  // Unlink state is deliberately separate from the delete dialog's: removing a
  // membership is a contribute write, not a destroy, so it reports through the
  // bar like a bulk tag edit instead of the stepped delete dialog.
  const [unlinkBusy, setUnlinkBusy] = useState<{ label: string; count: number } | null>(null);
  const [unlinkFailures, setUnlinkFailures] = useState<BatchFailure[] | null>(null);
  const [unlinkRequestError, setUnlinkRequestError] = useState<string | null>(null);
  const [metaDialogOpen, setMetaDialogOpen] = useState(false);
  const [metaRequestError, setMetaRequestError] = useState<string | null>(null);
  const slicerEnabled = Boolean(slicerSettings?.enabled);
  const slicerLabel = slicerLabelFor(slicerSettings?.selected);
  const engravingEnabled = Boolean(engravingSettings?.enabled);
  const engraverLabel = engraverLabelFor(engravingSettings?.selected);

  // "Add to project…" is offered in BOTH files and project mode: adding a
  // project-A member to project-B is a legitimate thing to do.
  const [projectPickerIds, setProjectPickerIds] = useState<string[] | null>(null);
  const openProjectPicker = useStableCallback((ids: string[]) => setProjectPickerIds(ids));

  const {
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
    loading,
    loadingMore,
    error,
    offset,
    setOffset,
    hasMore,
    notInProject,
    unfiled: isUnfiled,
    queryRef,
    refresh,
    loadMore,
    toggleTag,
    toggleNotInProject,
    resetFilters,
    handleApiError,
  } = useAssetList({
    folderId,
    unfiled,
    missingOnly,
    projectId,
    foldersVersion,
    assetsVersion,
    onUnauthorized,
  });

  // Opening another folder is a new browsing session, so the per-session
  // preview budget (Mf-04od) is refunded; reset() resolves queued jobs with null.
  useEffect(() => {
    previewQueue.reset();
  }, [folderId]);

  const {
    selectedIds,
    toggleSelected,
    clearSelection,
    selectAllLoaded,
    bulkBusy,
    bulkFailures,
    bulkRequestError,
    bulkFailureLabels,
    dismissBulkFailures,
    bulkDelete,
    bulkMoveToFolder,
    bulkAddTags,
    bulkRemoveTags,
    bulkSetTags,
    bulkSetMeta,
    bulkAddToProject,
    downloadSelected,
    downloadByTag,
  } = useBulkActions({
    folderId,
    activeTags,
    activeCreator,
    items,
    setItems,
    offset,
    setOffset,
    setAllTags,
    handleApiError,
    onUnauthorized,
    onAddToProject: openProjectPicker,
    onAssetsFiled,
  });

  const {
    downloadingId,
    movingId,
    rescanning,
    replaceAsset,
    onSaveTags,
    onSaveDetails,
    onRename,
    onMoveFolder,
    clearPreparedPrint,
    preparedClearingId,
    downloadSingle,
    openInSlicer,
    openInEngraving,
    removeAsset,
    addToProject,
    forgetMissingAsset,
    rescanMissing,
    pendingConfirm,
    confirmBusy,
    setPendingConfirm,
    requestConfirm,
    runPendingConfirm,
  } = useAssetActions({
    items,
    setItems,
    setPreviewItem,
    refresh,
    handleApiError,
    onUnauthorized,
    slicerSettings,
    engravingSettings,
    selectedIds,
    bulkSetTags,
    offset,
    setOffset,
    setAllTags,
    folderId,
    activeTags,
    activeCreator,
    onRequestDelete: setDeleteTargets,
    onAddToProject: openProjectPicker,
    onAssetsFiled,
    onScanStatus,
  });

  // Membership removal is NOT deletion: this path calls `removeProjectAssets`
  // only. `deleteAsset` / `batchDeleteAssets` are destroy routes (WRITE) and
  // must never be called from the unlink path, whatever the role.
  const canRemoveFromProject = isProject && canContribute;
  const unlinkFailureLabels = useMemo(
    () => failureLines(unlinkFailures ?? [], items),
    [unlinkFailures, items],
  );

  const unlinkAssets = useStableCallback(async (targets: Asset[]) => {
    // Only reachable through `removeFromProject`, which is wired for project
    // mode alone; the guard keeps that a fact rather than a cast.
    if (projectId == null) return;
    const ids = targets.map(target => target.id);
    const before = items;
    const startOffset = offset;
    // Same discipline as `bulkDelete`: snapshot the window, drop optimistically,
    // and rewind the paging cursor by the rows that actually left it.
    const dropped = before.filter(item => ids.includes(item.id)).length;
    setUnlinkFailures(null);
    setUnlinkRequestError(null);
    setUnlinkBusy({ label: "Removing", count: ids.length });
    setItems(dropIds(before, ids));
    setOffset(Math.max(0, startOffset - dropped));
    try {
      const result = await removeProjectAssets(projectId, ids);
      const failedIds = new Set(result.failed.map(failure => failure.id));
      // A failed unlink means the membership is still there: put those rows
      // back from the snapshot (the cursor is recomputed absolutely, never
      // patched).
      const remaining = before.filter(
        item => !ids.includes(item.id) || failedIds.has(item.id),
      );
      setItems(remaining);
      setOffset(Math.max(0, startOffset - (before.length - remaining.length)));
      if (result.failed.length) {
        if (targets.length > 1) {
          setUnlinkFailures(result.failed);
        } else {
          // A card-menu unlink has no bar line to land in, so report it as a
          // toast while the row stays put.
          notifyError(result.failed[0].error || "Failed to remove the file from this project.");
        }
      }
      if (result.removed.length > 0) {
        // The files stay in the library; only memberships moved, so App has to
        // refetch the project counts/list rather than the asset listing. This
        // also holds for a partial success: the successful rows really left.
        onProjectsChanged?.();
      }
    } catch (err) {
      setItems(before);
      setOffset(startOffset);
      if (err instanceof UnauthorizedError) {
        onUnauthorized?.();
        return;
      }
      // A whole-request failure has no per-asset ids to report honestly, so the
      // single path toasts and the bulk path uses the bar's request-error slot.
      const fallback =
        targets.length === 1
          ? "Failed to remove the file from this project."
          : "Failed to remove the selection from this project.";
      const message = errorMessage(err, fallback);
      if (targets.length === 1) notifyError(message);
      else setUnlinkRequestError(message);
    } finally {
      setUnlinkBusy(null);
    }
  });

  // The confirm-gated entry point both the card menu and the bar share.
  const requestUnlink = useStableCallback((targets: Asset[]) => {
    const one = targets.length === 1 ? targets[0] : null;
    requestConfirm({
      title: one
        ? `Remove "${one.name}" from this project?`
        : `Remove ${targets.length} files from this project?`,
      body: `The file${targets.length === 1 ? " stays" : "s stay"} in your library. Only ${
        targets.length === 1 ? "its membership of" : "their membership of"
      } "${projectName ?? "this project"}" is removed, and you can add ${
        targets.length === 1 ? "it" : "them"
      } back at any time.`,
      confirmLabel: "Remove from project",
      // Neutral, never danger: this is not a destructive action on the bytes.
      danger: false,
      run: () => unlinkAssets(targets),
    });
  });

  const removeFromProject = useStableCallback((asset: Asset) => requestUnlink([asset]));
  const removeSelectionFromProject = useStableCallback(() => {
    const targets = items.filter(item => selectedIds.has(item.id));
    if (targets.length) requestUnlink(targets);
  });

  const dismissUnlinkFailures = useStableCallback(() => {
    dismissBulkFailures();
    setUnlinkFailures(null);
    setUnlinkRequestError(null);
  });

  // One request per path: a single asset needs its file name confirmed (the
  // server 400s otherwise), a selection is one batch call whose per-asset
  // failures come back for the dialog and stay listed in the bar.
  const runDelete = useStableCallback(async (targets: Asset[]) => {
    if (targets.length > 1) {
      const failures = await bulkDelete(targets.map(target => target.id));
      // A partial bulk success still removed rows, so the badge must drop by the
      // number that actually went: refetch the counts even with failures back.
      onAssetsFiled?.();
      const failed: AssetDeleteFailure[] = failures.map(failure => ({
        id: failure.id,
        message: failure.error,
      }));
      return { failed };
    }
    const [asset] = targets;
    // One row, reconciled like a batch of one: the optimistic drop rewinds the
    // cursor by exactly the row that left the loaded window, and a failure puts
    // both back from the snapshot without a refetch.
    const before = items;
    const startOffset = offset;
    const dropped = before.some(item => item.id === asset.id) ? 1 : 0;
    setItems(dropIds(before, [asset.id]));
    setOffset(Math.max(0, startOffset - dropped));
    try {
      await deleteAsset(asset.id, asset.filename);
      // The row is really gone, so App's counts must refetch too (this also
      // fixes a pre-existing sidebar-count staleness after a single delete).
      onAssetsFiled?.();
      return { failed: [] };
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized?.();
        // No rollback: the session is gone and the delete may still have applied.
        return { failed: [{ id: asset.id, message: "You were signed out. The delete may still have been applied." }] };
      }
      if (err instanceof AssetNotFoundError) {
        // Already gone server-side: the row is correctly absent, keep the rewind.
        // The counts must refetch for the same reason as the success path.
        onAssetsFiled?.();
        return { failed: [] };
      }
      // Rollback path: nothing was deleted, so the counts are still accurate.
      setItems(before);
      setOffset(startOffset);
      return { failed: [{ id: asset.id, message: errorMessage(err, "Delete failed.") }] };
    }
  });

  // The bar's Delete opens the same dialog for exactly the current selection.
  const openBulkDelete = useStableCallback(() => {
    const targets = items.filter(item => selectedIds.has(item.id));
    if (targets.length) setDeleteTargets(targets);
  });

  const closeDeleteDialog = useStableCallback(() => setDeleteTargets(null));

  // The grid's only in-place retry of a failed listing; stable so the body's
  // button keeps its identity across renders.
  const retryAssets = useStableCallback(() => {
    void refresh();
  });

  // Stable identities so `AssetCard`'s memo keeps skipping untouched cards.
  const openPreview = useStableCallback((asset: Asset | null) => {
    setPreviewSection(null);
    setPreviewItem(asset);
  });
  const openDetails = useStableCallback((asset: Asset, section: DetailSection) => {
    setPreviewSection(section);
    setPreviewItem(asset);
  });
  const closePreview = useStableCallback(() => {
    setPreviewItem(null);
    setPreviewSection(null);
  });

  const openMetaDialog = useStableCallback(() => {
    if (!selectedIds.size) return;
    setMetaRequestError(null);
    setMetaDialogOpen(true);
  });

  const closeMetaDialog = useStableCallback(() => {
    setMetaDialogOpen(false);
    setMetaRequestError(null);
  });

  const submitMeta = useStableCallback(async (change: BatchMetaChange) => {
    const error = await bulkSetMeta(change);
    if (error === null) {
      setMetaDialogOpen(false);
      setMetaRequestError(null);
      return;
    }
    setMetaRequestError(error);
  });

  const {
    sortedItems,
    folderGroups,
    typeGroups,
    collapsedGroups,
    expandedGroups,
    toggleGroup,
    toggleGroupExpanded,
    subfolders,
  } = useAssetSorting({
    items,
    folders,
    folderId,
    sortKey,
    setSortKey,
    sortDir,
    setSortDir,
  });

  // Flat-grid virtualization: chunk `sortedItems` into rows and render only
  // those covering the viewport. `colsForWidth(window.innerWidth)` is the seed;
  // the hook refines it from the container's measured width when it can.
  const virtual = useVirtualRows({
    scrollElementRef,
    items: sortedItems,
    cols: colsForWidth(typeof window === "undefined" ? 0 : window.innerWidth),
  });
  const virtualRows = useMemo(
    () => chunkRows(sortedItems, virtual.cols),
    [sortedItems, virtual.cols],
  );
  const renderedRows = virtualRows.slice(virtual.startRow, virtual.endRow + 1);

  // The one zip-aware uploader for the whole grid: its prompt modal is rendered
  // once below, and drag/drop uploads route through the same instance.
  const uploader = useUpload({
    folderId,
    onUnauthorized,
    onUploaded: () => {
      void refresh({ keepLoadedPages: true });
    },
  });

  // The scope owns the destination name: the old sorting-hook value said
  // "All Items" for the root and could not tell Unfiled from All files.
  const scope = filesScope({ folderId: folderId ?? null, unfiled: isUnfiled, folders });
  // Missing mode hides the folder chrome: a breadcrumb would navigate away and
  // unmount the queue, and a missing file is not a place to drop a new one, so
  // the drop hook stays inert there and in project mode.
  let dropPolicy: DropPolicy = { kind: "inert" };
  if (!isProject && !missingOnly) {
    if (scope.dropPolicy === "upload") {
      dropPolicy = { kind: "upload", name: scope.label };
    } else {
      // All files spans every folder, so no single folder could own an upload
      // dropped there: the overlay names the scopes that can instead.
      dropPolicy = {
        kind: "refuse",
        message: "All files is a result scope. Drop on Unfiled or a folder to upload.",
      };
    }
  }

  const {
    showDropOverlay,
    dropMessage,
    handleDragEnter,
    handleDragOver,
    handleDragLeave,
    handleDrop,
  } = useAssetDrop({ policy: dropPolicy, canContribute, uploader });

  // Single source of truth for card props so the virtualized flat grid and the
  // grouped rows render exactly the same cards. All callbacks are stable, so
  // `AssetCard`'s memo still skips untouched cards.
  const renderCard = (item: Asset) => (
    <AssetCard
      key={item.id}
      item={item}
      canContribute={canContribute}
      // Never the raw capability in project mode: unlink is not destroy.
      canDelete={canDeleteAssets}
      onPreview={openPreview}
      onOpenDetails={openDetails}
      onDownloadSingle={downloadSingle}
      onDownloadByTag={downloadByTag}
      downloading={downloadingId === item.id}
      onDelete={removeAsset}
      onAddToProject={addToProject}
      onRemoveFromProject={canRemoveFromProject ? removeFromProject : undefined}
      selected={selectedIds.has(item.id)}
      onToggleSelected={toggleSelected}
      slicerEnabled={slicerEnabled}
      slicerLabel={slicerLabel}
      onOpenInSlicer={openInSlicer}
      engravingEnabled={engravingEnabled}
      engraverLabel={engraverLabel}
      onOpenInEngraving={openInEngraving}
      theme={theme}
      previewMode={previewSettings?.mode || "automatic"}
      onAssetChanged={replaceAsset}
      onForgetMissing={forgetMissingAsset}
      onRescanMissing={rescanMissing}
      rescanning={rescanning}
      onClearPreparedPrint={clearPreparedPrint}
      preparedClearing={preparedClearingId === item.id}
    />
  );

  return (
    <div
      className="relative"
      data-project-id={projectId ?? undefined}
      data-project-name={projectName ?? undefined}
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {showDropOverlay && (
        <div className="absolute inset-0 z-40 flex items-center justify-center rounded-lg border-2 border-dashed border-accent bg-panel-overlay pointer-events-none">
          <div className="px-4 py-3 rounded-md border border-panel-strong bg-panel-strong shadow-sm text-sm">
            {dropMessage}
          </div>
        </div>
      )}
      <div className="flex flex-col gap-4">
      {/* Deliberately non-sticky: `BulkActionBar` is the single sticky element
          in this column (top-0 z-30), so the two cannot fight over the slot. */}
      {!isProject && !missingOnly && (
        <FolderBreadcrumbs
          folders={folders}
          folderId={folderId ?? null}
          onNavigate={onNavigateFolder ?? noop}
          unfiled={isUnfiled}
        />
      )}
      <BulkActionBar
        canContribute={canContribute}
        canDelete={canDeleteAssets}
        count={selectedIds.size}
        folders={folders}
        showFolderControls={!isProject}
        busy={bulkBusy ?? unlinkBusy}
        failures={bulkFailures ?? unlinkFailures}
        failureLabels={[...bulkFailureLabels, ...unlinkFailureLabels]}
        requestError={bulkRequestError ?? unlinkRequestError}
        onMove={bulkMoveToFolder}
        onAddTags={bulkAddTags}
        onRemoveTags={bulkRemoveTags}
        onEditMeta={openMetaDialog}
        onAddToProject={bulkAddToProject}
        onRemoveFromProject={canRemoveFromProject ? removeSelectionFromProject : undefined}
        onDownload={downloadSelected}
        onDelete={openBulkDelete}
        onClear={clearSelection}
        onSelectAll={selectAllLoaded}
        onDismissFailures={dismissUnlinkFailures}
      />
      {!isProject && !missingOnly && canContribute && uploader.modal}
      <AssetToolbar
        isProject={isProject}
        loading={loading}
        sortKey={sortKey}
        setSortKey={setSortKey}
        sortDir={sortDir}
        setSortDir={setSortDir}
        allTags={allTags}
        activeTags={activeTags}
        toggleTag={toggleTag}
        creators={creators}
        activeCreator={activeCreator}
        setActiveCreator={setActiveCreator}
        notInProject={notInProject}
        onToggleNotInProject={toggleNotInProject}
        onReset={resetFilters}
        onQueryChange={query => { queryRef.current = query; }}
        onSearch={query => { refresh({ search: query }); }}
      />
      {!isProject && !missingOnly && (
        <FolderTiles
          folders={folders}
          folderId={folderId ?? null}
          onNavigate={onNavigateFolder ?? noop}
        />
      )}
      <AssetGridBody
        virtual={virtual}
        renderedRows={renderedRows}
        renderCard={renderCard}
        sortKey={sortKey}
        folderGroups={folderGroups}
        typeGroups={typeGroups}
        scopeKind={scope.kind}
        missingOnly={missingOnly}
        missingScan={missingScan}
        collapsedGroups={collapsedGroups}
        expandedGroups={expandedGroups}
        toggleGroup={toggleGroup}
        toggleGroupExpanded={toggleGroupExpanded}
        isProject={isProject}
        subfolders={subfolders}
        loading={loading}
        error={error}
        onRetry={retryAssets}
        items={items}
        queryRef={queryRef}
        activeTags={activeTags}
        activeCreator={activeCreator}
        notInProject={notInProject}
        hasMore={hasMore}
        loadingMore={loadingMore}
        loadMore={loadMore}
      />
      {previewItem && (
        <AssetPreviewModal
          asset={previewItem}
          theme={theme}
          onClose={closePreview}
          canContribute={canContribute}
          canDelete={canDeleteAssets}
          onSaveTags={onSaveTags}
          onSaveDetails={onSaveDetails}
          onRename={onRename}
          onMoveFolder={onMoveFolder}
          folders={folders}
          showFolderControls={!isProject}
          moving={movingId === previewItem.id}
          onAssetChanged={replaceAsset}
          onRequestConfirm={requestConfirm}
          initialSection={previewSection}
        />
      )}
      </div>
      {pendingConfirm && (
        <ConfirmDialog
          title={pendingConfirm.title}
          body={pendingConfirm.body}
          confirmLabel={pendingConfirm.confirmLabel}
          danger={pendingConfirm.danger}
          busy={confirmBusy}
          onCancel={() => setPendingConfirm(null)}
          onConfirm={() => { void runPendingConfirm(); }}
        />
      )}
      {deleteTargets && (
        <AssetDeleteDialog
          assets={deleteTargets}
          onDelete={runDelete}
          onCancel={closeDeleteDialog}
          onClosed={closeDeleteDialog}
        />
      )}
      {metaDialogOpen && (
        <BulkMetaDialog
          open={metaDialogOpen}
          count={selectedIds.size}
          busy={Boolean(bulkBusy)}
          requestError={metaRequestError}
          onCancel={closeMetaDialog}
          onSubmit={change => { void submitMeta(change); }}
        />
      )}
      {projectPickerIds && (
        <ProjectPickerDialog
          assetIds={projectPickerIds}
          onClose={() => setProjectPickerIds(null)}
          onChanged={onProjectsChanged}
          onUnauthorized={onUnauthorized}
        />
      )}
    </div>
  );
}
