import React, { useEffect, useRef } from "react";
import { entriesFromFileList } from "../lib/uploadTree";
import UrlImportPanel from "./operations/UrlImportPanel";
import { useUpload } from "./useUpload";

type Props = {
  /** Contribute capability: every control here uploads. A role below
   * contributor never sees this bar. */
  canContribute: boolean;
  onUploaded: () => void;
  folderId?: string | null;
  onUnauthorized?: () => void;
};

/**
 * The header's contribute bar: file upload and directory upload through
 * `useUpload`, and the URL import door beside them.
 *
 * The link-import family that used to live here was gone with `POST /import`,
 * `POST /import/zip` and `POST /import/project` (three routes); it is back as the
 * `import-link` and `import-zip` OPERATIONS, driven by `UrlImportPanel`
 * (Mf-x65b), which owns its own preflight, its own entry picker and its own
 * plan. This bar only places it in the row.
 */
export default function UploadBar({
  canContribute,
  onUploaded,
  folderId,
  onUnauthorized,
}: Props) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const uploader = useUpload({
    folderId,
    onUnauthorized,
    onUploaded: () => onUploaded(),
  });

  useEffect(() => {
    if (!folderInputRef.current) return;
    folderInputRef.current.setAttribute("webkitdirectory", "");
    folderInputRef.current.setAttribute("directory", "");
  }, []);

  const onPick = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const entries = entriesFromFileList(e.target.files || []);
    if (!entries.length) return;
    await uploader.upload(entries);
    if (inputRef.current) inputRef.current.value = "";
  };

  const onPickFolder = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const entries = entriesFromFileList(e.target.files || []);
    if (!entries.length) return;
    await uploader.upload(entries);
    if (folderInputRef.current) folderInputRef.current.value = "";
  };

  // Every control in this bar contributes (a file upload and a directory
  // upload), so a role below contributor gets no bar at all rather than a row of
  // disabled buttons advertising a capability it lacks.
  // The early return sits after every hook so the hook order never changes.
  if (!canContribute) return null;

  return (
    <>
    <div className="flex items-center gap-2 flex-wrap">
      <input
        ref={inputRef}
        type="file"
        onChange={onPick}
        multiple
        accept=".png,.jpg,.jpeg,.webp,.bmp,.gif,.svg,.stl,.step,.stp,.3mf,.lbrn,.lbrn2,.zip"
        className="hidden"
      />
      <input
        ref={folderInputRef}
        type="file"
        onChange={onPickFolder}
        multiple
        className="hidden"
      />
      <button
        className="px-3 py-2 rounded-md bg-accent hover:bg-accent-strong disabled:opacity-60"
        disabled={uploader.busy}
        onClick={() => inputRef.current?.click()}
      >
        {uploader.busy ? "Uploading..." : "Upload"}
      </button>
      <button
        className="px-3 py-2 rounded-md border border-panel-strong disabled:opacity-60"
        disabled={uploader.busy}
        onClick={() => folderInputRef.current?.click()}
      >
        {uploader.busy ? "Uploading..." : "Upload folder"}
      </button>
      <UrlImportPanel
        canContribute={canContribute}
        folderId={folderId}
        onChanged={onUploaded}
        onUnauthorized={onUnauthorized}
      />
    </div>
    {uploader.modal}
    </>
  );
}
