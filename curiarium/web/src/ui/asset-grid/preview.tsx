import React, { useEffect, useId, useRef, useState } from "react";
import { type Asset, type PreparedPrint, fileUrl } from "../../lib/api";
import { formatBytes } from "../../lib/format";
import type { PreviewSettings, ResolvedTheme } from "../../lib/settings";
import { useFileGrants } from "../../lib/useFileGrants";
import type { ConfirmRequest } from "../ConfirmDialog";
import FileDownloadLink from "../FileDownloadLink";
import LightBurnPreview from "../LightBurnPreview";
import TagBadge from "../TagBadge";
import AssetDetailsEditor, { type DetailSection } from "./AssetDetailsEditor";
import SupportingFilesPanel from "./SupportingFilesPanel";
import {
  LIGHTBURN_EXTS,
  MODEL_EXTS,
  extOf,
  formatMissingSince,
  type PreviewVariant,
} from "./assetGridMeta";

// Three.js is the single largest dependency in the app and only 3D assets need
// it, so the viewer and the snapshot renderer are fetched on demand. Both live
// in `../ModelViewer`; the named export is adapted to `lazy`'s default-export
// contract.
const ModelViewer = React.lazy(() => import("../ModelViewer"));
const ModelSnapshot = React.lazy(() =>
  import("../ModelViewer").then((module) => ({ default: module.ModelSnapshot })),
);

/** Visible stand-in while a lazily loaded 3D chunk is on the wire. */
export function PreviewLoading({ label = "Loading 3D preview…" }: { label?: string }) {
  return <div className="flex h-full w-full items-center justify-center text-xs text-muted">{label}</div>;
}

function formatPreparedDuration(totalSeconds?: number | null) {
  if (!totalSeconds || totalSeconds < 1) return null;
  const roundedMinutes = Math.max(1, Math.round(totalSeconds / 60));
  const days = Math.floor(roundedMinutes / 1440);
  const hours = Math.floor((roundedMinutes % 1440) / 60);
  const minutes = roundedMinutes % 60;
  return [
    days ? `${days}d` : "",
    hours ? `${hours}h` : "",
    minutes ? `${minutes}m` : "",
  ].filter(Boolean).join(" ");
}

/** Mirrors the backend's literal marker for an undetected slicer. */
const UNKNOWN_SLICER = "unknown";

function nonEmptyString(value?: string | null) {
  if (typeof value !== "string") return null;
  return value.trim() || null;
}

function preparedTitle(prepared: PreparedPrint) {
  const printer = nonEmptyString(prepared.printer);
  return printer ? `Prepared for ${printer}` : "Prepared print";
}

function preparedSlicer(prepared: PreparedPrint) {
  const slicer = nonEmptyString(prepared.slicer);
  if (!slicer || slicer === UNKNOWN_SLICER) return "Unknown slicer";
  return slicer;
}

function preparedMaterial(prepared: PreparedPrint) {
  return nonEmptyString(prepared.material) ?? "Unknown material";
}

function preparedNozzle(prepared: PreparedPrint) {
  const nozzle = prepared.nozzle_mm;
  if (typeof nozzle !== "number" || !Number.isFinite(nozzle)) return "Unknown nozzle";
  return `${nozzle} mm`;
}

function preparedTime(prepared: PreparedPrint) {
  return formatPreparedDuration(prepared.estimated_seconds) ?? "Unknown time";
}

export function PreparedPrintSummary({
  prepared,
  onClear,
  clearing = false,
}: {
  prepared: PreparedPrint;
  onClear?: () => void;
  clearing?: boolean;
}) {
  // Fixed four-slot layout: missing facts render as placeholders so the line
  // never reflows when a payload is partially populated.
  const facts = [
    preparedSlicer(prepared),
    preparedMaterial(prepared),
    preparedNozzle(prepared),
    preparedTime(prepared),
  ];
  return (
    <div className="flex items-start justify-between gap-2 rounded-md border border-accent-soft bg-accent-soft px-2.5 py-2 text-xs text-foreground">
      <span>
        <span className="font-semibold">{preparedTitle(prepared)}</span>
        <span> · {facts.join(" · ")}</span>
      </span>
      {onClear && (
        <button type="button" className="shrink-0 underline-offset-2 hover:underline disabled:opacity-60" onClick={onClear} disabled={clearing}>
          {clearing ? "Clearing..." : "Clear"}
        </button>
      )}
    </div>
  );
}

export function renderPreviewContent(
  asset: Asset,
  variant: PreviewVariant,
  theme: ResolvedTheme,
  previewMode: PreviewSettings["mode"] = "automatic",
  onThumbnailSaved?: (asset: Asset) => void
) {
  const ext = extOf(asset.filename);
  const assetUrl = fileUrl(asset.url);
  const thumbUrl = asset.thumb_url ? fileUrl(asset.thumb_url) : null;
  const imgClass =
    variant === "card"
      ? "w-full h-full object-cover"
      : "w-full h-full object-contain bg-panel-strong";
  const is3d = MODEL_EXTS.has(ext);
  const isLightBurn = LIGHTBURN_EXTS.has(ext);

  if (variant === "card") {
    if (thumbUrl) {
      return <img src={thumbUrl} alt={asset.filename} loading="lazy" decoding="async" className={imgClass} />;
    }
    if (ext === "svg") {
      return <img src={assetUrl} alt={asset.filename} loading="lazy" decoding="async" className={imgClass} />;
    }
    if (is3d) {
      if (previewMode === "disabled") {
        return <div className="flex h-full w-full items-center justify-center text-xs text-muted">Preview generation disabled</div>;
      }
      return (
        <React.Suspense fallback={<PreviewLoading />}>
          <ModelSnapshot
            url={assetUrl}
            ext={ext}
            assetId={asset.id}
            theme={theme}
            mode={previewMode}
            onThumbnailSaved={onThumbnailSaved}
          />
        </React.Suspense>
      );
    }
    if (isLightBurn) {
      return (
        <LightBurnPreview
          url={assetUrl}
          assetId={asset.id}
          filename={asset.filename}
          imgClass={imgClass}
          size={asset.size}
          variant="card"
          mode={previewMode}
        />
      );
    }
    return (
      <div className="flex items-center justify-center w-full h-full text-sm opacity-60">
        No preview
      </div>
    );
  }

  if (is3d) {
    return (
      <React.Suspense fallback={<PreviewLoading />}>
        <ModelViewer key={`${variant}-${asset.id}`} url={assetUrl} ext={ext} assetId={asset.id} theme={theme} />
      </React.Suspense>
    );
  }
  if (thumbUrl || ext === "svg") {
    const src = thumbUrl || assetUrl;
    return <img src={src} alt={asset.filename} decoding="async" className={imgClass} />;
  }
  if (isLightBurn) {
    return (
      <LightBurnPreview
        url={assetUrl}
        assetId={asset.id}
        filename={asset.filename}
        imgClass={imgClass}
        size={asset.size}
        variant="modal"
      />
    );
  }
  return (
    <div className="flex items-center justify-center w-full h-full text-sm opacity-60">
      Preview unavailable
    </div>
  );
}

export function AssetPreviewModal({
  asset,
  theme,
  onClose,
  canContribute,
  canDelete,
  onSaveTags,
  onSaveDetails,
  onRename,
  onMoveFolder,
  folderOptions,
  showFolderControls,
  moving,
  onAssetChanged,
  onRequestConfirm,
  initialSection,
}: {
  asset: Asset;
  theme: ResolvedTheme;
  onClose: () => void;
  canContribute: boolean;
  canDelete: boolean;
  onSaveTags: (id: string, tags: string[]) => Promise<void>;
  onSaveDetails: (id: string, payload: { notes: string; creator: string }) => Promise<void>;
  onRename: (id: string, name: string) => Promise<void>;
  onMoveFolder: (id: string, folder_id: string | null) => Promise<void>;
  folderOptions: { id: string | null; name: string }[];
  showFolderControls: boolean;
  moving: boolean;
  onAssetChanged: (asset: Asset) => void;
  onRequestConfirm: (request: ConfirmRequest) => void;
  initialSection?: DetailSection | null;
}) {
  // Prime the preview's grants on open: a cold modal would otherwise fire an
  // unsigned <img>/<iframe>/model request and 401.
  const ready = useFileGrants([asset.url, asset.thumb_url]);
  const headingId = useId();
  const panelRef = useRef<HTMLDivElement | null>(null);
  // Captured before any child effect can move focus (child effects run first),
  // and restored on close unless the opener has since unmounted.
  const [previouslyFocused] = useState<HTMLElement | null>(() =>
    typeof document !== "undefined" && document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null,
  );

  useEffect(() => {
    // A menu item that names a field lets that field's own mount effect win;
    // focusing the panel here would otherwise steal it back.
    if (!initialSection || initialSection === "files") panelRef.current?.focus();
    return () => {
      if (previouslyFocused && previouslyFocused.isConnected) previouslyFocused.focus();
    };
    // The section is the dialog's opening intent and never changes in place.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Tab") {
      // `aria-modal="true"` promises modality, so Tab must not walk out into
      // the grid behind the overlay. Cycle within the panel instead.
      const focusable = Array.from(
        event.currentTarget.querySelectorAll<HTMLElement>(
          'button, input, textarea, select, a[href], [tabindex]:not([tabindex="-1"])',
        ),
      ).filter(element => !element.hasAttribute("disabled"));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      // The panel itself can hold focus (it is the initial focus target when no
      // field was named), so a backwards Tab from there must also stay inside.
      if (event.shiftKey && (document.activeElement === first || document.activeElement === event.currentTarget)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
      return;
    }
    // An inline editor that consumed Escape (a reverted rename) must not also
    // close the dialog under the user.
    if (event.key !== "Escape" || event.defaultPrevented) return;
    // A nested dialog (a supporting-file preview) owns the key while it is
    // open: closing its host out from under it would strand the user.
    if (panelRef.current?.querySelector('[role="dialog"]')) return;
    event.preventDefault();
    onClose();
  };

  const stop = (e: React.MouseEvent) => e.stopPropagation();
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4"
      onClick={onClose}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        tabIndex={-1}
        className="bg-panel-strong rounded-lg shadow-2xl max-w-5xl w-full max-h-full overflow-hidden flex flex-col"
        onClick={stop}
        onKeyDown={handleKeyDown}
      >
        <div className="flex items-center justify-between border-b border-panel px-4 py-3">
          <div>
            <h2 id={headingId} className="text-lg font-semibold">{asset.name}</h2>
            <p className="text-sm opacity-70">
              {asset.filename} · {formatBytes(asset.size)}
            </p>
            {(asset.creator || asset.collection) && (
              <p className="text-xs text-muted">
                {[
                  asset.creator ? `By ${asset.creator}` : null,
                  asset.collection ? `Collection (legacy): ${asset.collection}` : null,
                ].filter(Boolean).join(" · ")}
              </p>
            )}
            {asset.prepared_print && (
              <div className="mt-2"><PreparedPrintSummary prepared={asset.prepared_print} /></div>
            )}
            {asset.missing_since && (
              // The card keeps only the "File missing" headline now, so the date
              // lives here, where there is room for it (Mf-xmty).
              <p className="mt-2 text-xs text-red-700 dark:text-red-300">
                File missing from storage since {formatMissingSince(asset.missing_since)}.
              </p>
            )}
          </div>
          <button
            className="px-3 py-1 rounded-md border border-panel-strong text-sm"
            onClick={onClose}
          >
            Close
          </button>
        </div>
        <div className="p-4 space-y-4 overflow-auto">
          <div className="w-full h-[70vh] min-h-[400px]">
            <div className="w-full h-full rounded-lg bg-panel-soft flex items-center justify-center overflow-hidden">
              {ready ? renderPreviewContent(asset, "modal", theme) : <p className="text-sm opacity-70">Preparing preview...</p>}
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            {asset.tags.length ? (
              asset.tags.map(tag => <TagBadge key={tag} tag={tag} />)
            ) : (
              <span className="text-xs opacity-60">No tags</span>
            )}
          </div>
          {/* The notes prose block that used to sit here is gone: the editor
              below shows the same text and, for a contributor, edits it in
              place — printing it twice was the clutter this ticket removes. */}
          <AssetDetailsEditor
            asset={asset}
            canContribute={canContribute}
            canDelete={canDelete}
            onSaveTags={onSaveTags}
            onSaveDetails={onSaveDetails}
            onRename={onRename}
            onMoveFolder={onMoveFolder}
            folderOptions={folderOptions}
            showFolderControls={showFolderControls}
            moving={moving}
            initialSection={initialSection}
          />
          <SupportingFilesPanel
            asset={asset}
            canContribute={canContribute}
            canDelete={canDelete}
            onAssetChanged={onAssetChanged}
            onRequestConfirm={onRequestConfirm}
            defaultExpanded={initialSection === "files"}
          />
          <div className="flex gap-3">
            <FileDownloadLink
              className="px-3 py-2 rounded-md bg-accent text-sm"
              rel={asset.url}
              filename={asset.filename}
            >
              Download
            </FileDownloadLink>
            <button
              className="px-3 py-2 rounded-md border border-panel-strong text-sm"
              onClick={onClose}
            >
              Close
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
