import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
  type ReactElement,
} from "react";

import {
  type Asset,
  type AssetFile,
  type AssetFileLimits,
  deleteAssetFile,
  listAssetFileLimits,
  listAssetFiles,
  renameAssetFile,
  reorderAssetFiles,
  uploadAssetFile,
} from "../../lib/api";
import { formatBytes } from "../../lib/format";
import { notifyError } from "../../lib/toast";
import type { ConfirmRequest } from "../ConfirmDialog";
import FileDownloadLink from "../FileDownloadLink";
import AssetFilePreview from "./AssetFilePreview";

type Props = {
  asset: Asset;
  canContribute: boolean;
  canDelete: boolean;
  onAssetChanged: (asset: Asset) => void;
  onRequestConfirm: (request: ConfirmRequest) => void;
  /** Open on mount. Default false so opening the dialog issues no requests. */
  defaultExpanded?: boolean;
};

/**
 * The supporting-files console for one asset, moved off the grid card and into
 * the large-preview detail dialog. It owns the supporting-file list, its quota
 * and every per-file mutation; the card only ever links to it.
 */
export default function SupportingFilesPanel({
  asset,
  canContribute,
  canDelete,
  onAssetChanged,
  onRequestConfirm,
  defaultExpanded = false,
}: Props): ReactElement {
  const [filesExpanded, setFilesExpanded] = useState(defaultExpanded);
  const [supportingFiles, setSupportingFiles] = useState<AssetFile[]>([]);
  const [fileLimits, setFileLimits] = useState<AssetFileLimits | null>(null);
  const [filesLoading, setFilesLoading] = useState(false);
  const [filesUploading, setFilesUploading] = useState(false);
  const [renamingFileId, setRenamingFileId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [fileRenameError, setFileRenameError] = useState<string | null>(null);
  const [fileBusy, setFileBusy] = useState(false);
  const [previewFile, setPreviewFile] = useState<AssetFile | null>(null);
  const relatedInputRef = useRef<HTMLInputElement | null>(null);

  const loadSupportingFiles = async () => {
    setFilesLoading(true);
    try {
      const [files, limits] = await Promise.all([
        listAssetFiles(asset.id),
        // A broken limits endpoint must not hide a list that loaded fine.
        listAssetFileLimits(asset.id).catch(err => {
          console.error(err);
          return null;
        }),
      ]);
      setSupportingFiles(files);
      setFileLimits(limits);
    } catch (err) {
      console.error(err);
      notifyError(err instanceof Error ? err.message : "Failed to load supporting files");
    } finally {
      setFilesLoading(false);
    }
  };

  // Collapsed panels issue no requests: the dialog must open without touching
  // listAssetFiles/listAssetFileLimits unless the user asked for the files.
  useEffect(() => {
    if (filesExpanded) void loadSupportingFiles();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filesExpanded, asset.id]);

  useEffect(() => {
    setPreviewFile(null);
    setRenamingFileId(null);
    setFileRenameError(null);
  }, [asset.id]);

  const addRelatedFiles = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    if (!files.length) return;
    setFilesUploading(true);
    try {
      let updated = asset;
      for (const file of files) {
        updated = await uploadAssetFile(asset.id, file);
        onAssetChanged(updated);
      }
      setFilesExpanded(true);
      await loadSupportingFiles();
    } catch (err) {
      console.error(err);
      notifyError(err instanceof Error ? err.message : "Failed to add files");
    } finally {
      setFilesUploading(false);
      // Refresh even after a partial failure: files before the failing one are
      // already stored, and a limit error must leave the list showing reality.
      await loadSupportingFiles();
    }
  };

  const removeRelatedFile = (file: AssetFile) => {
    onRequestConfirm({
      title: `Remove ${file.filename} from this model?`,
      danger: true,
      confirmLabel: "Remove",
      run: async () => {
        try {
          const updated = await deleteAssetFile(asset.id, file.id);
          onAssetChanged(updated);
          await loadSupportingFiles();
        } catch (err) {
          console.error(err);
          notifyError(err instanceof Error ? err.message : "Failed to remove file");
        }
      },
    });
  };

  const startFileRename = (file: AssetFile) => {
    setRenamingFileId(file.id);
    setRenameValue(file.filename);
    setFileRenameError(null);
  };

  const cancelFileRename = () => {
    setRenamingFileId(null);
    setRenameValue("");
    setFileRenameError(null);
  };

  const commitFileRename = async (file: AssetFile) => {
    const filename = renameValue.trim();
    if (!filename) {
      setFileRenameError("Enter a file name.");
      return;
    }
    if (filename === file.filename) {
      cancelFileRename();
      return;
    }
    setFileBusy(true);
    setFileRenameError(null);
    try {
      const updated = await renameAssetFile(asset.id, file.id, filename);
      setSupportingFiles(current => current.map(entry => (entry.id === file.id ? updated : entry)));
      setRenamingFileId(null);
      await loadSupportingFiles();
    } catch (err) {
      console.error(err);
      const message = err instanceof Error && err.message ? err.message : "Rename failed";
      setFileRenameError(message);
      notifyError(message);
    } finally {
      setFileBusy(false);
    }
  };

  const handleFileRenameKey = (event: KeyboardEvent<HTMLInputElement>, file: AssetFile) => {
    if (event.key === "Enter") {
      event.preventDefault();
      void commitFileRename(file);
    } else if (event.key === "Escape") {
      event.preventDefault();
      cancelFileRename();
    }
  };

  const moveFile = async (file: AssetFile, direction: -1 | 1) => {
    const index = supportingFiles.findIndex(entry => entry.id === file.id);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= supportingFiles.length) return;
    const swapped = [...supportingFiles];
    [swapped[index], swapped[target]] = [swapped[target], swapped[index]];
    setFileBusy(true);
    try {
      setSupportingFiles(await reorderAssetFiles(asset.id, swapped.map(entry => entry.id)));
      await loadSupportingFiles();
    } catch (err) {
      console.error(err);
      notifyError(err instanceof Error ? err.message : "Failed to reorder supporting files");
    } finally {
      setFileBusy(false);
    }
  };

  const fileCount = fileLimits?.count ?? asset.supporting_file_count ?? 0;
  const maxFileCount = fileLimits?.max_count ?? 0;
  const atFileLimit = maxFileCount > 0 && fileCount >= maxFileCount;
  const fileCountLabel = maxFileCount > 0 ? `${fileCount} / ${maxFileCount}` : String(fileCount);
  const fileLimitsTitle = fileLimits
    ? [
        `Supporting files: ${fileCount} of ${maxFileCount > 0 ? maxFileCount : "unlimited"}`,
        `${formatBytes(fileLimits.total_bytes)} used${
          fileLimits.max_total_bytes > 0
            ? ` of ${formatBytes(fileLimits.max_total_bytes)}`
            : " (no total limit)"
        }`,
        fileLimits.max_file_bytes > 0
          ? `${formatBytes(fileLimits.max_file_bytes)} max per file`
          : "no per-file limit",
      ].join(" · ")
    : undefined;

  return (
    <div className="rounded-md border border-panel-strong bg-panel-strong/40 text-sm">
      <div className="flex items-center justify-between gap-2 p-2">
        <button
          type="button"
          className="flex min-w-0 items-center gap-2 text-left"
          onClick={() => setFilesExpanded(value => !value)}
          title={fileLimitsTitle}
        >
          <span className="font-medium">Supporting files</span>
          <span className="rounded-full bg-panel px-2 py-0.5 text-[11px] text-muted" title={fileLimitsTitle}>
            {fileCountLabel}
          </span>
        </button>
        {canContribute && (
          <>
            <input
              ref={relatedInputRef}
              type="file"
              multiple
              className="hidden"
              accept=".pdf,.txt,.md,.markdown,.csv,.log,.json,.html,.htm,.xml,.yaml,.yml,.png,.jpg,.jpeg,.gif,.webp,.bmp,.doc,.docx,.rtf,.zip,.gcode,.gco,.bgcode,.3mf"
              onChange={addRelatedFiles}
            />
            <button
              type="button"
              className="rounded-md border border-panel-strong px-2 py-1 text-xs disabled:opacity-60"
              disabled={filesUploading || atFileLimit}
              onClick={() => relatedInputRef.current?.click()}
              title={
                atFileLimit
                  ? `Supporting file limit reached (${maxFileCount})`
                  : "Add documentation or a prepared print"
              }
            >
              {filesUploading ? "Adding..." : "Add files"}
            </button>
          </>
        )}
      </div>
      {filesExpanded && (
        <div className="border-t border-panel px-2 py-2">
          {filesLoading ? (
            <div className="text-xs text-muted">Loading files...</div>
          ) : supportingFiles.length ? (
            <div className="space-y-1">
              {supportingFiles.map((file, index) => (
                <div key={file.id} className="rounded px-1 py-1 hover:bg-panel-soft">
                  {renamingFileId === file.id ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <input
                        value={renameValue}
                        onChange={event => {
                          setRenameValue(event.target.value);
                          setFileRenameError(null);
                        }}
                        onKeyDown={event => handleFileRenameKey(event, file)}
                        aria-label={`Rename ${file.filename}`}
                        className="min-w-0 flex-1 rounded-md border border-panel-strong bg-panel-soft px-2 py-1 text-xs"
                        autoFocus
                      />
                      <button
                        type="button"
                        className="rounded-md bg-accent px-2 py-1 text-[11px] disabled:opacity-60"
                        disabled={fileBusy}
                        onClick={() => commitFileRename(file)}
                      >
                        Save
                      </button>
                      <button
                        type="button"
                        className="rounded-md border border-panel-strong px-2 py-1 text-[11px] disabled:opacity-60"
                        disabled={fileBusy}
                        onClick={cancelFileRename}
                      >
                        Cancel
                      </button>
                      {fileRenameError && (
                        <p role="alert" className="w-full text-xs text-red-700 dark:text-red-300">
                          {fileRenameError}
                        </p>
                      )}
                    </div>
                  ) : (
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <button
                        type="button"
                        className="min-w-0 truncate text-left text-xs underline-offset-2 hover:underline"
                        title={file.filename}
                        aria-label={`Preview ${file.filename}`}
                        onClick={() => setPreviewFile(file)}
                      >
                        {file.filename}
                      </button>
                      <div className="flex shrink-0 items-center gap-2 text-[11px]">
                        <span className="text-muted">{formatBytes(file.size)}</span>
                        <FileDownloadLink
                          className="underline-offset-2 hover:underline"
                          rel={file.url}
                          filename={file.filename}
                        >
                          Download
                        </FileDownloadLink>
                        {/* Reordering and the display-only rename are
                            contribute surfaces: they rewrite list order and
                            a label, never the bytes. Remove deletes a file,
                            so it keeps the narrower `canDelete` gate. */}
                        {canContribute && (
                          <>
                            <button
                              type="button"
                              className="px-1 disabled:opacity-40"
                              aria-label={`Move ${file.filename} up`}
                              disabled={fileBusy || index === 0}
                              onClick={() => moveFile(file, -1)}
                            >
                              ↑
                            </button>
                            <button
                              type="button"
                              className="px-1 disabled:opacity-40"
                              aria-label={`Move ${file.filename} down`}
                              disabled={fileBusy || index === supportingFiles.length - 1}
                              onClick={() => moveFile(file, 1)}
                            >
                              ↓
                            </button>
                            <button
                              type="button"
                              disabled={fileBusy}
                              onClick={() => startFileRename(file)}
                            >
                              Rename
                            </button>
                            {canDelete && (
                              <button
                                type="button"
                                className="text-red-700 dark:text-red-300"
                                onClick={() => removeRelatedFile(file)}
                              >
                                Remove
                              </button>
                            )}
                          </>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <div className="text-xs text-muted">Add assembly instructions, notes, other documentation, or a prepared print.</div>
          )}
        </div>
      )}
      {previewFile && (
        <AssetFilePreview
          key={previewFile.id}
          assetId={asset.id}
          file={previewFile}
          onClose={() => setPreviewFile(null)}
        />
      )}
    </div>
  );
}
