import TrashCard from "../TrashCard";
import SnapshotCard from "./SnapshotCard";
import StorageCapabilitiesCard from "./StorageCapabilitiesCard";
import { useStorageCapabilities } from "./useStorageCapabilities";
import type { StorageSettingsState } from "./useStorageSettings";

type Props = {
  storage: StorageSettingsState;
  onUnauthorized?: () => void;
  onBack: () => void;
};

export default function StorageSection({ storage, onUnauthorized, onBack }: Props) {
  const {
    template: storageTemplate,
    initial: storageInitial,
    tokens: storageTokens,
    sample: storageSample,
    status: storageStatus,
    loading: storageLoading,
    saving: storageSaving,
    setTemplate: setStorageTemplate,
    setStatus: setStorageStatus,
    save: saveStorageSettings,
  } = storage;

  const storageCapabilities = useStorageCapabilities();

  const isDirty = storageTemplate.trim() !== storageInitial;
  const previewValues: Record<string, string> = {
    folder: "Props/Workshop",
    collection: "Tabletop",
    tags: "Print in place + Useful",
    creator: "Example creator",
    model: "Cable clip",
    name: "Cable clip",
    filename: "Cable clip.3mf",
    id: "a1b2c3d4",
  };
  const draftSample = Object.entries(previewValues).reduce(
    (value, [token, replacement]) => value.split(`{${token}}`).join(replacement),
    storageTemplate.trim()
  );

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold">Storage Structure</h2>
          <p className="text-sm opacity-70">Choose how managed model files are organized on disk.</p>
        </div>
        <button className="text-sm px-3 py-2 rounded-md border border-panel-strong" onClick={onBack}>Back</button>
      </div>

      <div className="rounded-lg border border-panel bg-panel-soft p-4 flex flex-col gap-4">
        <div>
          <div className="text-lg font-semibold">Path template</div>
          <p className="text-sm opacity-70">
            The template applies only to new uploads and imports; existing files are never moved.
          </p>
          <p className="text-sm opacity-70">
            The model name is unique inside its Curiarium folder. Renaming a model also renames its file while preserving the extension.
          </p>
          <p className="text-sm opacity-70">
            The legacy <span className="font-mono">{"{collection}"}</span> token still works in saved templates but is no longer offered; use Projects to group files.
          </p>
        </div>
        <label className="flex flex-col gap-1">
          <span className="text-xs uppercase tracking-wide text-muted">Template</span>
          <input
            value={storageTemplate}
            onChange={e => { setStorageTemplate(e.target.value); setStorageStatus(null); }}
            className="px-3 py-2 rounded-md border border-panel-strong bg-panel-strong font-mono text-sm"
            placeholder="{folder}/{model}/{filename}"
            disabled={storageLoading || storageSaving}
          />
        </label>
        <div className="flex flex-wrap gap-1.5">
          {storageTokens.map(token => (
            <button
              key={token}
              type="button"
              onClick={() => setStorageTemplate(value => {
                const tokenText = `{${token}}`;
                if (token === "filename" && value.includes(tokenText)) return value;
                const filenameSuffix = "/{filename}";
                if (value.endsWith(filenameSuffix)) {
                  return `${value.slice(0, -filenameSuffix.length)}/${tokenText}${filenameSuffix}`;
                }
                return `${value}${value.endsWith("/") || !value ? "" : "/"}${tokenText}`;
              })}
              className="rounded-md border border-panel-strong px-2 py-1 font-mono text-[11px]"
            >
              {`{${token}}`}
            </button>
          ))}
        </div>
        <div className="rounded-md border border-dashed border-panel-strong p-3">
          <div className="text-[10px] uppercase tracking-wide text-muted">Example path</div>
          <div className="mt-1 break-all font-mono text-xs">{draftSample || storageSample || "Enter a template to preview its path"}</div>
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          <button
            className="text-sm px-3 py-2 rounded-md bg-accent disabled:opacity-60"
            disabled={storageLoading || storageSaving || !isDirty}
            onClick={saveStorageSettings}
          >
            {storageSaving ? "Saving..." : "Save storage structure"}
          </button>
          {storageStatus && <span className="text-xs opacity-80">{storageStatus}</span>}
        </div>
      </div>

      {/* The storage rescan and its failure report are work on the library,
          not configuration: they live in REVIEW → Missing files (ADR 0011
          decision 15). */}

      <StorageCapabilitiesCard state={storageCapabilities} />

      <SnapshotCard
        capabilities={storageCapabilities}
        canManage={storage.canManage}
        onUnauthorized={onUnauthorized}
      />

      <TrashCard onUnauthorized={onUnauthorized} />
    </div>
  );
}
