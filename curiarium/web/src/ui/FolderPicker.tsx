import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent,
  type ReactElement,
} from "react";
import { Check, ChevronDown, ChevronRight, Folder as FolderIcon, Search } from "lucide-react";

import type { Folder } from "../lib/api";
import {
  ROOT_GROUP,
  childrenIndex,
  flattenFolderTree,
  folderPath,
  indexFolders,
  searchFolders,
} from "../lib/folderTree";

export type FolderPickerProps = {
  /** Unique id stem for this instance's DOM ids. */
  id: string;
  /** Visible field label, also the dialog's and tree's accessible name. */
  label: string;
  folders: readonly Folder[];
  /** Selected parent folder id; null = the top level. */
  value: string | null;
  /** Fires ONLY on commit (Enter/Space/click), never while navigating. */
  onChange: (id: string | null) => void;
  /** Rows rendered disabled and not selectable, e.g. a folder and its subtree. */
  excludeIds?: ReadonlySet<string>;
  /** Which excluded id is "the folder being moved", so it can say WHY it is excluded. */
  selfId?: string | null;
  rootLabel?: string;
  disabled?: boolean;
};

/** Row id standing in for "no parent folder"; never a folder, never omitted. */
const ROOT_ROW = "__root__";
const UNTITLED = "Untitled";
const SEARCH_CAP = 50;
const TYPEAHEAD_RESET_MS = 500;
const INDENT_STEP_PX = 14;
const PATH_SEPARATOR = " / ";
/**
 * Everything the Tab trap cycles through, in DOM order. Row chevrons are
 * `tabIndex={-1}` controls for pointer users, so they must not enter the cycle.
 */
const TABBABLE_SELECTOR =
  'input:not([tabindex="-1"]), button:not([tabindex="-1"]), [data-picker-row][tabindex="0"]';

const SELF_REASON = "Can't move a folder into itself";
const DESCENDANT_REASON = "Inside the folder being moved";

/** One visible browse-mode row, already copied out of the tree helpers. */
type Row = { id: string; name: string; depth: number; hasChildren: boolean };

function displayName(folder: Folder): string {
  return folder.name || UNTITLED;
}

/** DOM-id-safe fragment of a folder id: uuids today, but not a promise. */
function rowKeyOf(folderId: string): string {
  return folderId.replace(/[^A-Za-z0-9_-]/g, "-");
}

/** A printable character that feeds typeahead rather than a shortcut. */
function isTypeaheadKey(event: KeyboardEvent<HTMLElement>): boolean {
  return (
    event.key.length === 1 &&
    event.key !== " " &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.altKey
  );
}

function reasonFor(folderId: string, selfId: string | null): string {
  return folderId === selfId ? SELF_REASON : DESCENDANT_REASON;
}

function statusFor(total: number): string {
  if (total === 0) return "No matching folders";
  if (total > SEARCH_CAP) return `Showing ${SEARCH_CAP} of ${total} — keep typing to narrow`;
  return `${total} match${total === 1 ? "" : "es"}`;
}

/** Which state modifiers a row or option carries; all are independent. */
type RowState = {
  /** This row holds the committed `value`, not merely the keyboard highlight. */
  current?: boolean;
  /** This row is the keyboard highlight (focused row / `aria-activedescendant`). */
  active?: boolean;
  excluded?: boolean;
  /** The Root row only. */
  root?: boolean;
};

function rowClassNames({ current, active, excluded, root }: RowState): string {
  const classNames = ["folder-picker-row"];
  if (current) classNames.push("is-current");
  if (active) classNames.push("is-active");
  if (excluded) classNames.push("is-excluded");
  if (root) classNames.push("is-root");
  return classNames.join(" ");
}

/**
 * Searchable parent-folder picker (Mf-08sb). Replaces the flat `<select>`s:
 * browse mode is an APG treeview over `folderTree`, search mode is a listbox
 * that keeps focus in the input, so a deep folder is two keystrokes away.
 *
 * The dialog is NOT portaled: it must stay inside the host dialog's DOM
 * subtree so the host's Escape/Tab handling and focus containment still see it.
 */
export default function FolderPicker({
  id,
  label,
  folders,
  value,
  onChange,
  excludeIds,
  selfId = null,
  rootLabel = "Root (top level)",
  disabled = false,
}: FolderPickerProps): ReactElement {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const byId = useMemo(() => indexFolders(folders), [folders]);
  const selected = value === null ? undefined : byId[value];
  const currentLabel = selected ? displayName(selected) : rootLabel;

  function openPicker(): void {
    if (disabled) return;
    setOpen(true);
  }

  function closePicker(): void {
    setOpen(false);
    triggerRef.current?.focus();
  }

  function commit(folderId: string | null): void {
    onChange(folderId);
    closePicker();
  }

  function triggerKeys(event: KeyboardEvent<HTMLButtonElement>): void {
    if (disabled) return;
    if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setOpen(true);
    }
  }

  return (
    <div className="folder-picker">
      <span className="folder-picker-label" id={`${id}-label`}>{label}:</span>
      <button
        type="button"
        className="folder-picker-trigger"
        id={`${id}-trigger`}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-labelledby={`${id}-label ${id}-value`}
        disabled={disabled}
        ref={triggerRef}
        onClick={openPicker}
        onKeyDown={triggerKeys}
      >
        <FolderIcon aria-hidden="true" className="folder-picker-icon" />
        <span id={`${id}-value`} className="folder-picker-value">{currentLabel}</span>
        <ChevronDown aria-hidden="true" className="folder-picker-chevron" />
      </button>
      {open && (
        <FolderPickerDialog
          id={id}
          label={label}
          folders={folders}
          value={value}
          rootLabel={rootLabel}
          excludeIds={excludeIds}
          selfId={selfId}
          onCommit={commit}
          onClose={closePicker}
        />
      )}
    </div>
  );
}

type FolderPickerDialogProps = {
  id: string;
  label: string;
  folders: readonly Folder[];
  value: string | null;
  rootLabel: string;
  excludeIds?: ReadonlySet<string>;
  selfId: string | null;
  onCommit: (id: string | null) => void;
  onClose: () => void;
};

/**
 * Mounted only while open, so every per-open state below starts fresh without
 * a reset effect: expansion, the active row, the query and the typeahead buffer
 * all die with the component.
 */
function FolderPickerDialog({
  id,
  label,
  folders,
  value,
  rootLabel,
  excludeIds,
  selfId,
  onCommit,
  onClose,
}: FolderPickerDialogProps): ReactElement {
  const byId = useMemo(() => indexFolders(folders), [folders]);
  const groups = useMemo(() => childrenIndex(folders), [folders]);
  /** The selected id, or null when it is unknown/stale. */
  const knownId = value !== null && byId[value] !== undefined ? value : null;
  /** The row that gets focus on open; a stale id falls back to the Root row. */
  const anchorId = knownId ?? ROOT_ROW;

  const [expanded, setExpanded] = useState<ReadonlySet<string>>(
    () => new Set(knownId === null ? [] : folderPath(folders, knownId).slice(0, -1).map(segment => segment.id)),
  );
  const [activeId, setActiveId] = useState<string>(anchorId);
  const [query, setQuery] = useState("");
  /** Which option index the user last moved to, and for which query. */
  const [searchChoice, setSearchChoice] = useState<{ query: string; index: number } | null>(null);

  const panelRef = useRef<HTMLDivElement>(null);
  const rowRefs = useRef(new Map<string, HTMLElement>());
  const typeahead = useRef<{ text: string; at: number }>({ text: "", at: 0 });

  const rows = useMemo<Row[]>(
    () =>
      flattenFolderTree(folders, { expanded }).map(row => ({
        id: row.id,
        name: displayName(byId[row.id]),
        depth: row.depth,
        hasChildren: row.hasChildren,
      })),
    [folders, expanded, byId],
  );

  /** child id → parent id (null = the Root row), for ArrowLeft. */
  const parentIds = useMemo(() => {
    const parents = new Map<string, string | null>();
    for (const [groupId, children] of Object.entries(groups)) {
      for (const child of children) {
        parents.set(child.id, groupId === ROOT_GROUP ? null : groupId);
      }
    }
    return parents;
  }, [groups]);

  /** Lowercased full path per id, so typeahead can match a path fragment. */
  const paths = useMemo(() => {
    const map = new Map<string, string>();
    for (const folder of folders) {
      const path = folderPath(folders, folder.id).map(segment => segment.name).join(PATH_SEPARATOR);
      map.set(folder.id, path.toLowerCase());
    }
    return map;
  }, [folders]);

  const searching = query.trim() !== "";
  const { matches, total } = useMemo(() => searchFolders(folders, query, SEARCH_CAP), [folders, query]);

  const selectableIndexes = useMemo(
    () =>
      matches.reduce<number[]>((indexes, match, index) => {
        if (!(excludeIds?.has(match.id) ?? false)) indexes.push(index);
        return indexes;
      }, []),
    [matches, excludeIds],
  );

  const defaultIndex = selectableIndexes.length > 0 ? selectableIndexes[0] : -1;
  const remembered =
    searchChoice !== null && searchChoice.query === query && selectableIndexes.includes(searchChoice.index)
      ? searchChoice.index
      : null;
  const activeIndex = remembered ?? defaultIndex;
  const activeMatch = activeIndex === -1 ? undefined : matches[activeIndex];
  const activeOptionId = activeMatch ? `${id}-option-${rowKeyOf(activeMatch.id)}` : undefined;

  // The anchor is decided once, when the dialog opens: a later `folders`
  // refresh must not move the caret back onto the anchor mid-search.
  const initialAnchor = useRef(anchorId);

  // The one focus effect: focus the anchor row (not the input) so arrows work
  // immediately, and bring it into view. `scrollIntoView` is absent in jsdom,
  // hence the optional call rather than a polyfill.
  useEffect(() => {
    const element = rowRefs.current.get(initialAnchor.current);
    element?.focus();
    element?.scrollIntoView?.({ block: "center" });
  }, []);

  // A refresh that drops the active folder unmounts its row and drops focus on
  // <body>, where no keydown reaches this panel. Recover the roving tabstop,
  // but never steal the caret from the search field.
  useEffect(() => {
    if (searching) return;
    const visibleIds = [ROOT_ROW, ...rows.map(row => row.id)];
    if (visibleIds.includes(activeId)) return;
    const fallback = visibleIds.includes(anchorId) ? anchorId : ROOT_ROW;
    setActiveId(fallback);
    if (document.activeElement === null || document.activeElement === document.body) {
      rowRefs.current.get(fallback)?.focus();
    }
  }, [rows, activeId, anchorId, searching]);

  function isExcluded(folderId: string): boolean {
    return excludeIds?.has(folderId) ?? false;
  }

  function select(folderId: string): void {
    if (isExcluded(folderId)) return;
    onCommit(folderId);
  }

  function registerRow(folderId: string, element: HTMLDivElement | null): void {
    if (element) rowRefs.current.set(folderId, element);
    else rowRefs.current.delete(folderId);
  }

  function focusRow(folderId: string): void {
    rowRefs.current.get(folderId)?.focus();
  }

  function setRowExpanded(folderId: string, expand: boolean): void {
    setExpanded(previous => {
      const next = new Set(previous);
      if (expand) next.add(folderId);
      else next.delete(folderId);
      return next;
    });
  }

  /** Is `candidateId` inside the subtree rooted at `folderId`? */
  function isInsideSubtree(candidateId: string, folderId: string): boolean {
    let current = parentIds.get(candidateId) ?? null;
    const guard = new Set<string>();
    while (current !== null && !guard.has(current)) {
      if (current === folderId) return true;
      guard.add(current);
      current = parentIds.get(current) ?? null;
    }
    return false;
  }

  /**
   * Collapse one branch. If the cursor sits inside the subtree that is about to
   * unmount it moves up to the collapsed row, otherwise focus would fall to
   * <body> and every later keydown would miss this panel.
   */
  function collapseBranch(folderId: string): void {
    if (activeId === folderId || isInsideSubtree(activeId, folderId)) {
      setActiveId(folderId);
      focusRow(folderId);
    }
    setRowExpanded(folderId, false);
  }

  /** Flip one branch's expansion without committing anything. */
  function toggleExpanded(folderId: string): void {
    if (expanded.has(folderId)) collapseBranch(folderId);
    else setRowExpanded(folderId, true);
  }

  function moveActive(offset: 1 | -1): void {
    const visibleIds = [ROOT_ROW, ...rows.map(row => row.id)];
    const index = visibleIds.indexOf(activeId);
    const from = index === -1 ? 0 : index;
    const target = visibleIds[Math.min(Math.max(from + offset, 0), visibleIds.length - 1)];
    setActiveId(target);
    focusRow(target);
  }

  function expandOrDescend(): void {
    if (activeId === ROOT_ROW) {
      const first = rows[0];
      if (first) {
        setActiveId(first.id);
        focusRow(first.id);
      }
      return;
    }
    const index = rows.findIndex(row => row.id === activeId);
    const row = rows[index];
    if (!row) return;
    if (row.hasChildren && !expanded.has(row.id)) {
      setRowExpanded(row.id, true);
      return;
    }
    const child = rows[index + 1];
    if (child && parentIds.get(child.id) === row.id) {
      setActiveId(child.id);
      focusRow(child.id);
    }
  }

  function collapseOrAscend(): void {
    if (activeId === ROOT_ROW) return;
    const row = rows.find(candidate => candidate.id === activeId);
    if (!row) return;
    if (row.hasChildren && expanded.has(row.id)) {
      collapseBranch(row.id);
      return;
    }
    const parentId = parentIds.get(row.id) ?? ROOT_ROW;
    setActiveId(parentId);
    focusRow(parentId);
  }

  function commitActive(): void {
    if (activeId === ROOT_ROW) {
      onCommit(null);
      return;
    }
    select(activeId);
  }

  /**
   * Typeahead over full paths, cycling rather than filtering. A repeated
   * character resets the buffer to that character, so "bb" walks the b-matches.
   */
  function runTypeahead(key: string): void {
    const now = Date.now();
    const previous = typeahead.current;
    const withinWindow = now - previous.at <= TYPEAHEAD_RESET_MS;
    const repeated = previous.text.endsWith(key);
    const text = `${withinWindow && !repeated ? previous.text : ""}${key}`;
    typeahead.current = { text, at: now };

    const needle = text.toLowerCase();
    const startIndex = rows.findIndex(row => row.id === activeId);
    for (let step = 1; step <= rows.length; step += 1) {
      const candidate = rows[(startIndex + step + rows.length) % rows.length];
      if (candidate && (paths.get(candidate.id) ?? "").includes(needle)) {
        setActiveId(candidate.id);
        focusRow(candidate.id);
        return;
      }
    }
  }

  function stepSearch(offset: 1 | -1): void {
    if (selectableIndexes.length === 0) return;
    const position = selectableIndexes.indexOf(activeIndex);
    let nextPosition: number;
    if (position === -1) {
      nextPosition = offset === 1 ? 0 : selectableIndexes.length - 1;
    } else {
      nextPosition = Math.min(Math.max(position + offset, 0), selectableIndexes.length - 1);
    }
    setSearchChoice({ query, index: selectableIndexes[nextPosition] });
  }

  function commitSearch(): void {
    if (!activeMatch) return;
    select(activeMatch.id);
  }

  function tabbables(): HTMLElement[] {
    const panel = panelRef.current;
    if (!panel) return [];
    return Array.from(panel.querySelectorAll<HTMLElement>(TABBABLE_SELECTOR)).filter(element =>
      element.matches(":not([disabled])"),
    );
  }

  /** Manual wrap: focus must never leave the panel, even though it is nested. */
  function trapTab(event: KeyboardEvent<HTMLDivElement>): void {
    const items = tabbables();
    if (items.length === 0) {
      event.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    const insidePanel = panelRef.current?.contains(active) ?? false;
    if (event.shiftKey) {
      if (!insidePanel || active === first) {
        event.preventDefault();
        last.focus();
      }
      return;
    }
    if (!insidePanel || active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function handleBrowseKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        moveActive(1);
        return;
      case "ArrowUp":
        event.preventDefault();
        moveActive(-1);
        return;
      case "ArrowRight":
        event.preventDefault();
        expandOrDescend();
        return;
      case "ArrowLeft":
        event.preventDefault();
        collapseOrAscend();
        return;
      case "Home":
        event.preventDefault();
        setActiveId(ROOT_ROW);
        focusRow(ROOT_ROW);
        return;
      case "End": {
        event.preventDefault();
        const last = rows[rows.length - 1];
        const target = last ? last.id : ROOT_ROW;
        setActiveId(target);
        focusRow(target);
        return;
      }
      case "Enter":
      case " ":
        event.preventDefault();
        commitActive();
        return;
      default:
        if (isTypeaheadKey(event)) {
          event.preventDefault();
          runTypeahead(event.key);
        }
    }
  }

  function handleSearchKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        stepSearch(1);
        return;
      case "ArrowUp":
        event.preventDefault();
        stepSearch(-1);
        return;
      case "Enter":
        event.preventDefault();
        commitSearch();
        return;
      default:
        // Home/End and every other key stay native to the input's caret.
        return;
    }
  }

  /**
   * Escape must not reach the host dialog that owns this picker, and it clears
   * the query before it closes anything.
   */
  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (searching && query !== "") setQuery("");
      else onClose();
      return;
    }
    if (event.key === "Tab") {
      trapTab(event);
      return;
    }
    // A focused control inside the panel owns its own activation keys: the
    // search field its text entry, and a button (Cancel, a branch chevron) its
    // native click. Intercepting those here would commit a row instead.
    if (event.target instanceof HTMLButtonElement) return;
    if (!searching && event.target instanceof HTMLInputElement) return;
    if (searching) handleSearchKeyDown(event);
    else handleBrowseKeyDown(event);
  }

  function handleScrimMouseDown(event: MouseEvent<HTMLDivElement>): void {
    if (event.target === event.currentTarget) onClose();
  }

  function renderTree(): ReactElement {
    return (
      <div role="tree" aria-label={label} className="folder-picker-tree">
        <div
          role="treeitem"
          data-picker-row={ROOT_ROW}
          aria-level={1}
          aria-selected={value === null}
          tabIndex={activeId === ROOT_ROW ? 0 : -1}
          className={rowClassNames({ current: value === null, active: activeId === ROOT_ROW, root: true })}
          onClick={() => onCommit(null)}
          ref={element => registerRow(ROOT_ROW, element)}
        >
          <span className="folder-picker-row-name">{rootLabel}</span>
          {value === null && <Check aria-hidden="true" />}
        </div>
        {rows.map(row => {
          const excluded = isExcluded(row.id);
          const rowKey = rowKeyOf(row.id);
          return (
            <div
              key={row.id}
              role="treeitem"
              data-picker-row={row.id}
              aria-level={row.depth + 2}
              aria-expanded={row.hasChildren ? expanded.has(row.id) : undefined}
              aria-selected={value === row.id}
              aria-disabled={excluded ? true : undefined}
              aria-describedby={excluded ? `${id}-reason-${rowKey}` : undefined}
              tabIndex={activeId === row.id ? 0 : -1}
              className={rowClassNames({ current: value === row.id, active: activeId === row.id, excluded })}
              onClick={() => select(row.id)}
              ref={element => registerRow(row.id, element)}
            >
              <span
                className="folder-picker-indent"
                style={{ width: `${row.depth * INDENT_STEP_PX}px` }}
                aria-hidden="true"
              />
              {row.hasChildren ? (
                <button
                  type="button"
                  className="folder-picker-branch"
                  tabIndex={-1}
                  aria-label={expanded.has(row.id) ? "Collapse" : "Expand"}
                  onClick={event => {
                    // Expanding is not a commit, and must not move the active row.
                    event.stopPropagation();
                    toggleExpanded(row.id);
                  }}
                >
                  <ChevronRight aria-hidden="true" />
                </button>
              ) : (
                <span className="folder-picker-branch" aria-hidden="true" />
              )}
              <FolderIcon aria-hidden="true" className="folder-picker-icon" />
              <span className="folder-picker-row-name">{row.name}</span>
              {value === row.id && <Check aria-hidden="true" />}
              {excluded && (
                <span className="folder-picker-reason" id={`${id}-reason-${rowKey}`}>
                  {reasonFor(row.id, selfId)}
                </span>
              )}
            </div>
          );
        })}
      </div>
    );
  }

  function renderSearchResults(): ReactElement {
    return (
      <ul role="listbox" id={`${id}-options`} aria-label={label} className="folder-picker-list">
        {matches.map((match, index) => {
          const excluded = isExcluded(match.id);
          const rowKey = rowKeyOf(match.id);
          const describedBy = [
            `${id}-option-path-${rowKey}`,
            ...(excluded ? [`${id}-reason-option-${rowKey}`] : []),
          ].join(" ");
          return (
            <li
              key={match.id}
              role="option"
              id={`${id}-option-${rowKey}`}
              data-picker-option={match.id}
              aria-labelledby={`${id}-option-label-${rowKey}`}
              aria-selected={match.id === value}
              aria-disabled={excluded ? true : undefined}
              aria-describedby={describedBy}
              data-active={index === activeIndex ? "true" : undefined}
              className={rowClassNames({ current: match.id === value, active: index === activeIndex, excluded })}
              onClick={() => select(match.id)}
            >
              <span className="folder-picker-row-name" id={`${id}-option-label-${rowKey}`}>
                {match.name}
              </span>
              <span className="folder-picker-path" id={`${id}-option-path-${rowKey}`}>{match.path}</span>
              {excluded && (
                <span className="folder-picker-reason" id={`${id}-reason-option-${rowKey}`}>
                  {reasonFor(match.id, selfId)}
                </span>
              )}
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <div className="folder-picker-scrim" onMouseDown={handleScrimMouseDown} data-folder-picker={id}>
      <div
        className="folder-picker-panel"
        role="dialog"
        aria-modal="true"
        aria-label={label}
        ref={panelRef}
        onKeyDown={handleKeyDown}
      >
        <div className="folder-picker-search">
          <Search aria-hidden="true" className="folder-picker-icon" />
          <input
            id={`${id}-search`}
            className="folder-picker-input"
            type="text"
            aria-label={`Search folders in ${label}`}
            role={searching ? "combobox" : "textbox"}
            placeholder="Search folders"
            value={query}
            onChange={event => setQuery(event.target.value)}
            {...(searching
              ? {
                  "aria-autocomplete": "list" as const,
                  "aria-expanded": true,
                  "aria-controls": `${id}-options`,
                  "aria-activedescendant": activeOptionId,
                }
              : {})}
          />
        </div>
        {searching ? renderSearchResults() : renderTree()}
        <div className="folder-picker-foot">
          <span className="folder-picker-status" role="status" aria-live="polite">
            {searching ? statusFor(total) : "Use the arrow keys to browse"}
          </span>
          <button type="button" className="folder-picker-cancel" onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  );
}
