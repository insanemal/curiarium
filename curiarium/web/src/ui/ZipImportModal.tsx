import React, { useEffect, useId, useMemo, useRef, useState } from "react";
import type { ZipEntry } from "../lib/zipUtils";
import { formatBytes } from "../lib/format";
import { tabbableIn, wrapTab } from "../lib/focusTrap";

/**
 * What this prompt can Tab through.
 *
 * Deliberately not `DRAWER_TABBABLE_SELECTOR`: that one also carries `summary`
 * and `a[href]`, which this prompt never renders, so a Tab would be allowed to
 * wrap onto a member that does not exist.
 */
const PROMPT_TABBABLE_SELECTOR = 'input:not([tabindex="-1"]), button:not([tabindex="-1"])';

export type ZipPromptConfig = {
  label: string;
  onImportAsZip: () => Promise<void>;
  loadEntries: () => Promise<ZipEntry[]>;
  onImportSelected: (entries: string[]) => Promise<void>;
};

type ZipPromptState = {
  config: ZipPromptConfig;
};

type PromptStage = "choice" | "select";

function errorMessage(err: unknown, fallback: string) {
  if (err instanceof Error && err.message.trim()) return err.message;
  return fallback;
}

export function useZipImportPrompt() {
  const [state, setState] = useState<ZipPromptState | null>(null);
  const [stage, setStage] = useState<PromptStage>("choice");
  const [entries, setEntries] = useState<ZipEntry[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const resolveRef = useRef<(() => void) | null>(null);
  /**
   * The control the prompt opened from, so closing returns focus to it. A
   * drag-drop has no trigger control, so this is usually null.
   */
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  const reset = () => {
    setStage("choice");
    setEntries([]);
    setSelected(new Set());
    setBusy(false);
    setError(null);
  };

  const close = () => {
    const restore = restoreFocusRef.current;
    restoreFocusRef.current = null;
    setState(null);
    reset();
    if (resolveRef.current) {
      resolveRef.current();
      resolveRef.current = null;
    }
    if (restore && restore.isConnected) restore.focus();
  };

  const prompt = (config: ZipPromptConfig) => {
    if (state) {
      return Promise.resolve();
    }
    // Captured before the prompt mounts: afterwards its entry effect has
    // already moved focus inside it, and the trigger would be lost. `<body>`
    // means nothing in particular held focus, so there is nothing to restore.
    const trigger = document.activeElement;
    restoreFocusRef.current =
      trigger instanceof HTMLElement && trigger !== document.body ? trigger : null;
    reset();
    setState({ config });
    return new Promise<void>(resolve => {
      resolveRef.current = resolve;
    });
  };

  const loadEntries = async () => {
    if (!state) return;
    setBusy(true);
    setError(null);
    try {
      const list = await state.config.loadEntries();
      setEntries(list);
      setSelected(new Set(list.map(entry => entry.path)));
      setStage("select");
    } catch (err) {
      setError(errorMessage(err, "Unable to read zip contents"));
    } finally {
      setBusy(false);
    }
  };

  const importAsZip = async () => {
    if (!state) return;
    setBusy(true);
    setError(null);
    try {
      await state.config.onImportAsZip();
      close();
    } catch (err) {
      setError(errorMessage(err, "Import failed"));
      setBusy(false);
    }
  };

  const importSelected = async () => {
    if (!state) return;
    const selectedEntries = Array.from(selected);
    if (!selectedEntries.length) {
      setError("Select at least one file to import.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await state.config.onImportSelected(selectedEntries);
      close();
    } catch (err) {
      setError(errorMessage(err, "Import failed"));
      setBusy(false);
    }
  };

  const toggleEntry = (path: string) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const selectAll = () => {
    setSelected(new Set(entries.map(entry => entry.path)));
  };

  const clearAll = () => {
    setSelected(new Set());
  };

  const selectedCount = selected.size;
  const allSelected = entries.length > 0 && selectedCount === entries.length;

  const modal = state ? (
    <ZipImportModal
      label={state.config.label}
      stage={stage}
      entries={entries}
      selected={selected}
      selectedCount={selectedCount}
      allSelected={allSelected}
      busy={busy}
      error={error}
      onClose={close}
      onImportAsZip={importAsZip}
      onLoadEntries={loadEntries}
      onImportSelected={importSelected}
      onToggleEntry={toggleEntry}
      onSelectAll={selectAll}
      onClearAll={clearAll}
    />
  ) : null;

  return { prompt, modal, isOpen: Boolean(state) };
}

function ZipImportModal({
  label,
  stage,
  entries,
  selected,
  selectedCount,
  allSelected,
  busy,
  error,
  onClose,
  onImportAsZip,
  onLoadEntries,
  onImportSelected,
  onToggleEntry,
  onSelectAll,
  onClearAll,
}: {
  label: string;
  stage: PromptStage;
  entries: ZipEntry[];
  selected: Set<string>;
  selectedCount: number;
  allSelected: boolean;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onImportAsZip: () => void;
  onLoadEntries: () => void;
  onImportSelected: () => void;
  onToggleEntry: (path: string) => void;
  onSelectAll: () => void;
  onClearAll: () => void;
}) {
  const entryCount = entries.length;
  const title = useMemo(() => (stage === "choice" ? "Import zip" : "Choose files"), [stage]);
  // Four prompts can coexist (the UploadBar, grid and Sidebar uploaders plus
  // the project attacher), so the ids naming this dialog must be per-instance.
  const id = useId();
  const titleId = `${id}-title`;
  const labelId = `${id}-label`;
  const panelRef = useRef<HTMLDivElement | null>(null);

  // Entry: focus a real control, not the container. `wrapTab` compares the
  // active element against `first`/`last`, so a container-focused dialog lets
  // Shift+Tab leak out.
  useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    tabbableIn(panel, PROMPT_TABBABLE_SELECTOR)[0]?.focus();
  }, []);

  // Recovery: choice -> select swaps the two choice buttons for the select
  // list, so the focused node unmounts and focus drops to <body>. Re-enter the
  // panel (FolderPicker's roving-tabstop recovery idiom) without stealing a
  // caret that is still inside it.
  useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    const active = document.activeElement;
    if (active && active !== document.body && panel.contains(active)) return;
    tabbableIn(panel, PROMPT_TABBABLE_SELECTOR)[0]?.focus();
  }, [stage]);

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      // The drawer's guard bails silently on a descendant dialog, so this
      // prompt is the only surface that can answer Escape while it is open.
      // `stopPropagation` keeps the project/landing Escape handlers out of it.
      event.preventDefault();
      event.stopPropagation();
      // An in-flight upload must not be cancelled out from under its request.
      if (busy) return;
      onClose();
      return;
    }
    if (event.key === "Tab") {
      const panel = panelRef.current;
      if (!panel) {
        event.preventDefault();
        return;
      }
      wrapTab(panel, PROMPT_TABBABLE_SELECTOR, event);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4">
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        // Named by the visible heading plus the label rather than by an
        // `aria-label`, which would drop the title the user can see.
        aria-labelledby={`${titleId} ${labelId}`}
        onKeyDown={handleKeyDown}
        className="bg-panel-strong rounded-lg shadow-2xl max-w-3xl w-full max-h-full overflow-hidden flex flex-col"
      >
        <div className="flex items-center justify-between border-b border-panel px-4 py-3">
          <div>
            <h2 id={titleId} className="text-lg font-semibold">
              {title}
            </h2>
            <p id={labelId} className="text-sm opacity-70">
              {label}
            </p>
          </div>
          <button
            className="px-3 py-1 rounded-md border border-panel-strong text-sm disabled:opacity-60"
            onClick={onClose}
            disabled={busy}
          >
            Close
          </button>
        </div>
        <div className="p-4 space-y-4 overflow-auto">
          {stage === "choice" && (
            <div className="space-y-4">
              <p className="text-sm opacity-80">
                How would you like to handle this zip file?
              </p>
              <div className="flex flex-wrap gap-3">
                <button
                  className="px-4 py-2 rounded-md bg-accent disabled:opacity-60"
                  onClick={onImportAsZip}
                  disabled={busy}
                >
                  Import as zip
                </button>
                <button
                  className="px-4 py-2 rounded-md border border-panel-strong disabled:opacity-60"
                  onClick={onLoadEntries}
                  disabled={busy}
                >
                  Import and unzip
                </button>
              </div>
            </div>
          )}

          {stage === "select" && (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                <div className="opacity-70">
                  {selectedCount} of {entryCount} selected
                </div>
                <div className="flex gap-2">
                  <button
                    className="px-2 py-1 rounded-md border border-panel-strong text-sm disabled:opacity-60"
                    onClick={onSelectAll}
                    disabled={busy || allSelected}
                  >
                    Select all
                  </button>
                  <button
                    className="px-2 py-1 rounded-md border border-panel-strong text-sm disabled:opacity-60"
                    onClick={onClearAll}
                    disabled={busy || selectedCount === 0}
                  >
                    Clear
                  </button>
                </div>
              </div>

              <div className="border border-panel-strong rounded-md max-h-[360px] overflow-auto">
                {entries.map(entry => (
                  <label
                    key={entry.path}
                    className="flex items-center gap-3 px-3 py-2 border-b border-panel-strong last:border-b-0 text-sm"
                  >
                    <input
                      type="checkbox"
                      checked={selected.has(entry.path)}
                      onChange={() => onToggleEntry(entry.path)}
                      disabled={busy}
                      className="w-4 h-4"
                    />
                    <span className="flex-1 truncate">{entry.path}</span>
                    <span className="text-xs opacity-70">{formatBytes(entry.size)}</span>
                  </label>
                ))}
                {entryCount === 0 && (
                  <div className="px-3 py-4 text-sm opacity-60">No files found in this zip.</div>
                )}
              </div>

              <div className="flex flex-wrap gap-3">
                <button
                  className="px-4 py-2 rounded-md bg-accent disabled:opacity-60"
                  onClick={onImportSelected}
                  disabled={busy || selectedCount === 0}
                >
                  Import selected
                </button>
                <button
                  className="px-4 py-2 rounded-md border border-panel-strong disabled:opacity-60"
                  onClick={onClose}
                  disabled={busy}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {error && (
            <div className="text-sm text-red-600 border border-red-300 rounded-md px-3 py-2">
              {error}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
