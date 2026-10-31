import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
  type ReactElement,
} from "react";

import { type Asset } from "../../lib/api";
import {
  normaliseCreator,
  normaliseName,
  normaliseNotes,
} from "../../lib/metadataValidation";
import TagInput from "../TagInput";

export type DetailSection = "rename" | "tags" | "details" | "files";

type Props = {
  asset: Asset;
  canContribute: boolean;
  canDelete: boolean;
  onSaveTags: (id: string, tags: string[]) => Promise<void>;
  onSaveDetails: (id: string, payload: { notes: string; creator: string }) => Promise<void>;
  onRename: (id: string, name: string) => Promise<void>;
  onMoveFolder: (id: string, folder_id: string | null) => Promise<void>;
  folderOptions: { id: string | null; name: string }[];
  /** Project mode hides every folder control (projects have no folder axis). */
  showFolderControls: boolean;
  moving: boolean;
  /** Focus the matching control on mount. */
  initialSection?: DetailSection | null;
};

/**
 * The detail dialog's editor block. It owns the field-local edit state that
 * used to live on every grid card; the card now only links here.
 */
export default function AssetDetailsEditor({
  asset,
  canContribute,
  canDelete,
  onSaveTags,
  onSaveDetails,
  onRename,
  onMoveFolder,
  folderOptions,
  showFolderControls,
  moving,
  initialSection,
}: Props): ReactElement {
  const [nameValue, setNameValue] = useState(asset.name);
  const [nameDirty, setNameDirty] = useState(false);
  const [nameError, setNameError] = useState<string | null>(null);
  const [nameSaving, setNameSaving] = useState(false);
  const nameInputRef = useRef<HTMLInputElement | null>(null);

  const [tagList, setTagList] = useState<string[]>(asset.tags);
  const [tagsDirty, setTagsDirty] = useState(false);
  const tagsSectionRef = useRef<HTMLDivElement | null>(null);

  const [creatorValue, setCreatorValue] = useState(asset.creator || "");
  const [notesValue, setNotesValue] = useState(asset.notes || "");
  const [creatorError, setCreatorError] = useState<string | null>(null);
  const [notesError, setNotesError] = useState<string | null>(null);
  const [detailsDirty, setDetailsDirty] = useState(false);
  const creatorInputRef = useRef<HTMLInputElement | null>(null);

  const previousAssetId = useRef(asset.id);
  useEffect(() => {
    // A different asset is a different edit session: drop every draft.
    if (previousAssetId.current !== asset.id) {
      previousAssetId.current = asset.id;
      setNameValue(asset.name);
      setNameDirty(false);
      setNameError(null);
      setTagList(asset.tags);
      setTagsDirty(false);
      setCreatorValue(asset.creator || "");
      setNotesValue(asset.notes || "");
      setDetailsDirty(false);
      setCreatorError(null);
      setNotesError(null);
      return;
    }
    // A server-side update to an untouched field should be visible; an update
    // to a field the user is editing must not clobber their draft.
    if (!nameDirty) setNameValue(asset.name);
    if (!tagsDirty) setTagList(asset.tags);
    if (!detailsDirty) {
      setCreatorValue(asset.creator || "");
      setNotesValue(asset.notes || "");
    }
  }, [
    asset.id,
    asset.name,
    asset.tags,
    asset.creator,
    asset.notes,
    nameDirty,
    tagsDirty,
    detailsDirty,
  ]);

  // The menu item that opened the dialog names the field to land on.
  useEffect(() => {
    if (initialSection === "rename") {
      nameInputRef.current?.focus();
    } else if (initialSection === "details") {
      creatorInputRef.current?.focus();
    } else if (initialSection === "tags") {
      // TagInput owns its textbox, so reach it through the section wrapper.
      tagsSectionRef.current?.querySelector("input")?.focus();
    }
  }, [initialSection, asset.id]);

  const commitName = async () => {
    const normalised = normaliseName(nameValue);
    if (normalised.error) {
      setNameError(normalised.error);
      return;
    }
    const next = normalised.value ?? "";
    setNameError(null);
    if (next === asset.name) {
      setNameDirty(false);
      return;
    }
    setNameSaving(true);
    try {
      await onRename(asset.id, next);
      setNameValue(next);
      setNameDirty(false);
    } catch {
      // `onRename` already reported the failure once; only revert the field.
      setNameValue(asset.name);
      setNameDirty(false);
    } finally {
      setNameSaving(false);
    }
  };

  const handleNameKey = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      void commitName();
    } else if (event.key === "Escape") {
      event.preventDefault();
      setNameValue(asset.name);
      setNameError(null);
      setNameDirty(false);
    }
  };

  const saveTags = async () => {
    await onSaveTags(asset.id, tagList);
    setTagsDirty(false);
  };

  const cancelTags = () => {
    setTagList(asset.tags);
    setTagsDirty(false);
  };

  const saveDetails = async () => {
    const creator = normaliseCreator(creatorValue);
    const notes = normaliseNotes(notesValue);
    setCreatorError(creator.error);
    setNotesError(notes.error);
    if (creator.error || notes.error) return;
    await onSaveDetails(asset.id, { notes: notes.value ?? "", creator: creator.value ?? "" });
    setDetailsDirty(false);
  };

  const cancelDetails = () => {
    setCreatorValue(asset.creator || "");
    setNotesValue(asset.notes || "");
    setCreatorError(null);
    setNotesError(null);
    setDetailsDirty(false);
  };

  const handleFolderChange = (event: ChangeEvent<HTMLSelectElement>) => {
    const next = event.target.value || null;
    const current = asset.folder_id || null;
    if (next === current) return;
    void onMoveFolder(asset.id, next);
  };

  const noteText = (asset.notes || "").trim();

  return (
    <div className="flex flex-col gap-4">
      {canDelete && (
        <div className="flex flex-col gap-1">
          <label className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wide text-muted">Name</span>
            <input
              ref={nameInputRef}
              aria-label="Name"
              value={nameValue}
              onChange={event => {
                setNameValue(event.target.value);
                setNameDirty(true);
                setNameError(null);
              }}
              onKeyDown={handleNameKey}
              className="w-full rounded-md border border-panel-strong bg-panel-soft px-2 py-1.5 text-sm"
            />
          </label>
          {nameError && (
            <p role="alert" className="text-xs text-red-700 dark:text-red-300">
              {nameError}
            </p>
          )}
          <button
            type="button"
            className="self-start rounded-md bg-accent px-3 py-1 text-sm disabled:opacity-60"
            disabled={nameSaving}
            onClick={() => void commitName()}
          >
            Save name
          </button>
        </div>
      )}

      {canContribute && (
        <div ref={tagsSectionRef} className="flex flex-col gap-2">
          <TagInput value={tagList} onChange={tags => { setTagList(tags); setTagsDirty(true); }} placeholder="Type and press comma/Enter" />
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="rounded-md bg-accent px-3 py-1 text-sm"
              onClick={() => void saveTags()}
            >
              Save tags
            </button>
            <button
              type="button"
              className="rounded-md border border-panel-strong px-3 py-1 text-sm"
              onClick={cancelTags}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {canContribute ? (
        <div className="flex flex-col gap-2">
          <label className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wide text-muted">Creator</span>
            <input
              ref={creatorInputRef}
              value={creatorValue}
              onChange={event => {
                setCreatorValue(event.target.value);
                setDetailsDirty(true);
                setCreatorError(null);
              }}
              className="w-full rounded-md border border-panel-strong bg-panel-soft px-2 py-1.5"
              placeholder="Designer or source"
            />
            {creatorError && (
              <p role="alert" className="text-xs text-red-700 dark:text-red-300">
                {creatorError}
              </p>
            )}
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] uppercase tracking-wide text-muted">Notes</span>
            <textarea
              value={notesValue}
              onChange={event => {
                setNotesValue(event.target.value);
                setDetailsDirty(true);
                setNotesError(null);
              }}
              className="w-full min-h-[80px] rounded-md border border-panel-strong bg-panel-soft p-2"
              placeholder="Add some details about this asset"
            />
            {notesError && (
              <p role="alert" className="text-xs text-red-700 dark:text-red-300">
                {notesError}
              </p>
            )}
          </label>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="rounded-md bg-accent px-3 py-1 text-sm"
              onClick={() => void saveDetails()}
            >
              Save details
            </button>
            <button
              type="button"
              className="rounded-md border border-panel-strong px-3 py-1 text-sm"
              onClick={cancelDetails}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <div className="text-xs">
            <span className="block text-[10px] uppercase text-muted">Creator</span>
            {asset.creator || "Not set"}
          </div>
          {asset.creator && <div className="text-xs text-muted">By {asset.creator}</div>}
          {asset.created_by && <div className="text-xs text-muted">Added by {asset.created_by}</div>}
          {asset.collection && (
            <div className="text-xs text-muted">Collection (legacy): {asset.collection}</div>
          )}
          <div className={`text-sm whitespace-pre-wrap ${noteText ? "text-foreground" : "opacity-60"}`}>
            {noteText || "Add notes"}
          </div>
        </div>
      )}

      {showFolderControls && canDelete && (
        <label className="flex flex-col gap-1">
          <span className="text-xs font-semibold uppercase text-muted">
            {moving ? "Updating..." : "Folder"}
          </span>
          <select
            value={asset.folder_id || ""}
            onChange={handleFolderChange}
            disabled={moving}
            className="rounded-md border border-panel-strong bg-panel-strong px-2 py-1 text-sm"
          >
            {folderOptions.map(option => (
              <option key={option.id || "none"} value={option.id || ""}>
                {option.name}
              </option>
            ))}
          </select>
        </label>
      )}
    </div>
  );
}
