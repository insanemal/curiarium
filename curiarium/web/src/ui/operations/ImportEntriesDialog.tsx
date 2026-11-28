import React, { useEffect, useId, useRef, useState } from "react";

import { wrapTab } from "../../lib/focusTrap";
import { formatBytes } from "../../lib/format";

/** One member a source listed, as both import preflights answer it: the path,
 *  the bytes it holds and the server's `junk` verdict. */
export type PickableEntry = { path: string; size: number; junk: boolean };

/** One member the server will NOT register, with its own reason, kept verbatim
 *  because the client decided none of these. */
export type ExcludedEntry = { path: string; reason: string };

/**
 * What this dialog can Tab through.
 *
 * The member list contributes ONE member of its own - the listbox - because the
 * rows are a roving tab stop (`tabindex="0"` on the active row alone, `-1` on
 * every other), so a static selector cannot name the row a browser would stop
 * on. The listbox itself is `tabindex="-1"`: it is the ring's first member and
 * the focus home for a listing with no rows, while the active row is what a
 * Tab actually lands on.
 */
const DIALOG_TABBABLE_SELECTOR =
  'input:not([tabindex="-1"]), button:not([tabindex="-1"]), [role="listbox"]';

/** The reason a junk member is shown with when the caller's `excluded` list
 *  names none - a URL zip's listing carries verdicts, not sentences. */
const JUNK_REASON = "the server leaves this member out";

/** The "Choose files" button's fallback wording, for a door with no selection. */
const CHOOSE_ENTRIES = "Choose files";

/** The server's reason for a row, or null when the row is selectable. A row the
 *  caller's `excluded` list names is refused with THAT sentence; a junk member
 *  with no sentence of its own falls back to the neutral one, so a junk row is
 *  never tickable either way. */
function exclusionReason(entry: PickableEntry, excluded: Map<string, string>): string | null {
  const named = excluded.get(entry.path);
  if (named !== undefined) return named;
  return entry.junk ? JUNK_REASON : null;
}

type Props = {
  /** The dialog's visible heading, e.g. "Choose files". Named by
   *  `aria-labelledby`, so the accessible name is the visible one. */
  title: string;
  /** What is being picked, e.g. the archive's filename: the heading's second
   *  line, and the second id in `aria-labelledby`. */
  label: string;
  /** Every member the source listed, in the server's order. Rendered in exactly
   *  that order, because the operator compares the dialog against the plan. */
  entries: PickableEntry[];
  /** The members the server will NOT register, with its own reason: they are
   *  rendered disabled beside their reason rather than hidden, so the operator
   *  can see what the plan leaves out. A path here that is not in `entries` has
   *  no row - this is a join by path, not a second listing. */
  excluded?: ExcludedEntry[];
  /** The paths that start ticked (the caller's draft selection). Read ONCE on
   *  mount: the caller owns the real state and hears the outcome through
   *  `onConfirm`, so a later prop change does not fight the operator's ticks. */
  selected: string[];
  /** Reports the ticked paths in `entries` order. Called at most once per
   *  press of "Use these files"; the dialog does not close itself, so the caller
   *  decides whether the selection is applied. */
  onConfirm: (paths: string[]) => void;
  /** Closes the dialog. Refused while `busy`. */
  onClose: () => void;
  /** A request is in flight: every button is disabled and Escape does nothing,
   *  because abandoning the dialog must not abandon the request it started. */
  busy?: boolean;
};

/** Focuses the listing's first member row, or the listbox when the listing is
 *  empty. Both are programmatically focusable, so this never lands on `<body>`. */
function focusListHome(list: HTMLDivElement | null, firstRow: HTMLDivElement | null): void {
  (firstRow ?? list)?.focus();
}

/**
 * The archive ENTRY PICKER, shared by every door that has to name the members a
 * plan should take: the two extract workflows and a URL zip's `import-zip`.
 *
 * It mirrors `ZipImportModal`'s proven keyboard and aria contract - role=dialog,
 * `aria-modal`, `aria-labelledby` on the visible heading plus label, `tabbableIn`
 * / `wrapTab`, Escape with `preventDefault` + `stopPropagation` that never closes
 * while busy, and focus handed back to the trigger on unmount - with ONE change
 * of shape: the member list is a single tab stop (`role="listbox"`,
 * `role="option"` rows, `aria-multiselectable`), not one checkbox per member. A
 * 600-member archive must be a handful of Tabs, never 600: the arrow keys, Home
 * and End move the ACTIVE row, and focus follows it.
 *
 * The caller owns both the listing and the selection; this dialog only decides
 * how they are shown and read back.
 */
export default function ImportEntriesDialog(props: Props): React.ReactElement {
  const { title, label, entries, excluded, selected, onConfirm, onClose, busy = false } = props;

  // Four pickers can coexist (two extract doors plus two URL imports), so the
  // ids naming this dialog must be per-instance.
  const id = useId();
  const titleId = `${id}-title`;
  const labelId = `${id}-label`;
  const panelRef = useRef<HTMLDivElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const rowRefs = useRef<(HTMLDivElement | null)[]>([]);
  /** The control the dialog opened from, captured before this dialog's entry
   *  effect moves focus inside it. `<body>` means nothing in particular held
   *  focus, so there is nothing to give back. */
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  // A path -> the reason the caller gave it. Both of these are small and cheap
  // beside a render, so they are plain values rather than memoised ones.
  const reasons = new Map((excluded ?? []).map(entry => [entry.path, entry.reason] as const));
  const selectable = entries
    .filter(entry => exclusionReason(entry, reasons) === null)
    .map(entry => entry.path);

  const [activeIndex, setActiveIndex] = useState(0);
  const [ticked, setTicked] = useState<Set<string>>(() => {
    // The caller's draft, intersected with the rows a plan can register: a stale
    // path (a member the listing no longer has) must not travel back to the plan.
    const allowed = new Set(selectable);
    return new Set(selected.filter(path => allowed.has(path)));
  });

  useEffect(() => {
    // Captured HERE and not in the caller: the mount is the last moment the
    // trigger still holds focus, because the line after it moves the caret.
    const trigger = document.activeElement;
    restoreFocusRef.current =
      trigger instanceof HTMLElement && trigger !== document.body ? trigger : null;
    focusListHome(listRef.current, rowRefs.current[0] ?? null);
  }, []);

  useEffect(() => {
    return () => {
      const trigger = restoreFocusRef.current;
      if (trigger !== null && trigger.isConnected) trigger.focus();
    };
  }, []);

  // The listing is the caller's and does not change while the dialog is open,
  // but a shorter one handed in mid-flight must not leave the roving index past
  // its end (an active row nobody can see would be a lost tab stop).
  const currentIndex = entries.length === 0 ? -1 : Math.min(activeIndex, entries.length - 1);

  function toggle(path: string): void {
    if (busy) return;
    const entry = entries.find(candidate => candidate.path === path);
    if (entry === undefined || exclusionReason(entry, reasons) !== null) return;
    setTicked(previous => {
      const next = new Set(previous);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  function moveActive(next: number): void {
    if (entries.length === 0) return;
    const clamped = Math.min(Math.max(next, 0), entries.length - 1);
    setActiveIndex(clamped);
    rowRefs.current[clamped]?.focus();
  }

  function handleListKeyDown(event: React.KeyboardEvent<HTMLDivElement>): void {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        moveActive(currentIndex + 1);
        return;
      case "ArrowUp":
        event.preventDefault();
        moveActive(currentIndex - 1);
        return;
      case "Home":
        event.preventDefault();
        moveActive(0);
        return;
      case "End":
        event.preventDefault();
        moveActive(entries.length - 1);
        return;
      case " ":
      case "Spacebar":
        event.preventDefault();
        if (currentIndex >= 0) toggle(entries[currentIndex].path);
        return;
      default:
        return;
    }
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLDivElement>): void {
    if (event.key === "Escape") {
      // `stopPropagation` keeps an enclosing drawer's Escape handler out of it:
      // this dialog is the only surface that may answer it while it is open.
      event.preventDefault();
      event.stopPropagation();
      // An in-flight request must not be abandoned from under its own answer.
      if (busy) return;
      onClose();
      return;
    }
    if (event.key === "Tab") {
      const panel = panelRef.current;
      if (panel === null) {
        event.preventDefault();
        return;
      }
      // `wrapFromUnlistedFocus` is what makes the roving row reachable in both
      // directions: the focused row is deliberately NOT in the ring's selector,
      // so without it a browser would Tab out of an `aria-modal` dialog.
      wrapTab(panel, DIALOG_TABBABLE_SELECTOR, event, { wrapFromUnlistedFocus: true });
    }
  }

  const allSelected = selectable.length > 0 && selectable.every(path => ticked.has(path));

  function confirmSelection(): void {
    onConfirm(entries.filter(entry => ticked.has(entry.path)).map(entry => entry.path));
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      // Named by the visible heading plus the label rather than by an
      // `aria-label`, which would drop the title the operator can see.
      aria-labelledby={`${titleId} ${labelId}`}
      onKeyDown={handleKeyDown}
      // Focusable, so a click on the backdrop lands on the dialog surface - the
      // nearest focusable ancestor of the click - instead of <body>, and the
      // Escape that follows still reaches this handler.
      tabIndex={-1}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4"
    >
      <div
        ref={panelRef}
        className="bg-panel-strong rounded-lg shadow-2xl max-w-3xl w-full max-h-full overflow-hidden flex flex-col"
      >
        <div className="border-b border-panel px-4 py-3">
          <h2 id={titleId} className="text-lg font-semibold">
            {title}
          </h2>
          <p id={labelId} className="text-sm opacity-70">
            {label}
          </p>
        </div>
        <div className="p-4 space-y-3 overflow-auto">
          <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
            {/* The count is announced, not just drawn: a Space pressed on a row
                changes nothing else the operator can hear. */}
            <span aria-live="polite" data-import-entries-count="true">
              {`${ticked.size} of ${entries.length} selected`}
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                className="px-2 py-1 rounded-md border border-panel-strong text-sm disabled:opacity-60"
                onClick={() => setTicked(new Set(selectable))}
                disabled={busy || allSelected || selectable.length === 0}
              >
                Select all
              </button>
              <button
                type="button"
                className="px-2 py-1 rounded-md border border-panel-strong text-sm disabled:opacity-60"
                onClick={() => setTicked(new Set())}
                disabled={busy || ticked.size === 0}
              >
                Select none
              </button>
            </div>
          </div>

          <div
            ref={listRef}
            role="listbox"
            aria-multiselectable="true"
            aria-label="Archive entries"
            // Programmatic only (see DIALOG_TABBABLE_SELECTOR): the active row is
            // the list's single tab stop, this container is its focus home.
            tabIndex={-1}
            onKeyDown={handleListKeyDown}
            className="border border-panel-strong rounded-md max-h-[360px] overflow-auto"
          >
            {entries.map((entry, index) => {
              const reason = exclusionReason(entry, reasons);
              const checked = ticked.has(entry.path);
              return (
                <div
                  key={entry.path}
                  ref={node => {
                    rowRefs.current[index] = node;
                  }}
                  role="option"
                  aria-checked={checked}
                  aria-disabled={reason !== null ? "true" : undefined}
                  tabIndex={index === currentIndex ? 0 : -1}
                  onClick={() => toggle(entry.path)}
                  onFocus={() => setActiveIndex(index)}
                  className="flex items-center gap-3 px-3 py-2 border-b border-panel-strong last:border-b-0 text-sm"
                >
                  <span aria-hidden="true" className="w-4 text-center">
                    {checked ? "\u2713" : ""}
                  </span>
                  <span className="flex-1 truncate">{entry.path}</span>
                  {/* The server's reason, verbatim: the client decided none. */}
                  {reason !== null && <span className="text-xs opacity-70">{reason}</span>}
                  <span className="text-xs opacity-70">{formatBytes(entry.size)}</span>
                </div>
              );
            })}
            {entries.length === 0 && (
              <div className="px-3 py-4 text-sm opacity-60">No files found.</div>
            )}
          </div>

          <div className="flex flex-wrap gap-3">
            <button
              type="button"
              className="px-4 py-2 rounded-md bg-accent disabled:opacity-60"
              onClick={confirmSelection}
              disabled={busy || ticked.size === 0}
            >
              Use these files
            </button>
            <button
              type="button"
              className="px-4 py-2 rounded-md border border-panel-strong disabled:opacity-60"
              onClick={onClose}
              disabled={busy}
            >
              Cancel
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

type PickerProps = {
  /** The archive's label, for the dialog's heading. */
  label: string;
  entries: PickableEntry[];
  excluded: ExcludedEntry[];
  /** The door's draft selection; an empty list means "the whole archive". */
  selected: string[];
  onConfirm: (paths: string[]) => void;
  disabled?: boolean;
  /** The door's attribute namespace. */
  dataPrefix: string;
};

/**
 * The extract doors' "Choose files" control: a button, the picker over a plan's
 * own listing, and the count of what is chosen.
 *
 * The listing and the exclusions are the PLAN's (`review.entries` /
 * `review.excluded`), so the picker and the review cannot disagree about which
 * members are selectable: a member the review says is excluded is refused here
 * with the same sentence, because it is the same array.
 *
 * An empty listing renders the button disabled rather than opening a dialog with
 * nothing in it - a plan that listed no member has nothing to pick.
 */
export function ArchiveEntryPicker(props: PickerProps): React.ReactElement {
  const { label, entries, excluded, selected, onConfirm, disabled = false, dataPrefix } = props;
  const [open, setOpen] = useState(false);

  const empty = entries.length === 0;
  const flag = (name: string): Record<string, string> => ({ [`data-${dataPrefix}-${name}`]: "true" });

  // A confirmed answer closes the picker and is the door's from there: the
  // selection changes its params, which re-plans the review for the narrowed set.
  function handOverSelection(paths: string[]): void {
    setOpen(false);
    onConfirm(paths);
  }

  return (
    <>
      <button
        type="button"
        className="text-xs px-2.5 py-1.5 rounded-md border border-panel-strong disabled:opacity-60"
        {...flag("choose-entries")}
        {...(empty ? flag("choose-entries-empty") : {})}
        disabled={disabled || empty}
        onClick={() => setOpen(true)}
      >
        {selected.length > 0 ? `${selected.length} of ${entries.length} files chosen` : CHOOSE_ENTRIES}
      </button>
      {open && (
        <ImportEntriesDialog
          title="Choose files"
          label={label}
          entries={entries}
          excluded={excluded}
          selected={selected}
          onConfirm={handOverSelection}
          onClose={() => setOpen(false)}
          busy={disabled}
        />
      )}
    </>
  );
}
