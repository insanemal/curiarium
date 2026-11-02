import React from "react";
import { Check } from "lucide-react";

import type { BundleCreateItem, BundleDetectItem } from "../../lib/api/bundles";
import ArchiveExtractAction from "../archive/ArchiveExtractAction";
import {
  BUNDLE_FILTERS,
  archiveLine,
  confidenceLabel,
  countLabel,
  countsLine,
  coverLine,
  headline,
  isCreatable,
  matchesFilter,
  matchesVisibility,
  statusLine,
  typedName,
  type BundleFilter,
} from "./bundleRows";
import ReviewQueueView from "./ReviewQueueView";
import type { BundleDetectState } from "./useBundleDetect";
import { bundleBadgeCount, describeQueueCount } from "./useReviewCounts";

const PRIMARY_BUTTON = "text-xs px-2.5 py-1.5 rounded-md bg-accent disabled:opacity-60";
const SECONDARY_BUTTON =
  "text-xs px-2.5 py-1.5 rounded-md border border-panel-strong disabled:opacity-60";

/** The old card's colours, kept so a row's confidence reads the same way here. */
function confidenceClass(confidence: string): string {
  return confidence === "high" ? "text-emerald-400" : "text-amber-400";
}

/** The row's rightmost column: an existing project outranks a dismissal. */
function rowState(row: BundleDetectItem): string {
  if (row.existing_project) return "Created";
  if (row.dismissed) return "Dismissed";
  return "";
}

/** Why a report with rows shows none of them. A project outranks a dismissal,
 *  exactly as `rowState` says, so a queue of one kind is never confused with a
 *  queue of the other. */
type HiddenReason = "created" | "dismissed" | "mixed";

function hiddenReason(rows: BundleDetectItem[]): HiddenReason {
  if (rows.every(row => rowState(row) === "Created")) return "created";
  if (rows.every(row => rowState(row) === "Dismissed")) return "dismissed";
  return "mixed";
}

/** What an empty queue says when the rows exist but are hidden. The mixed state
 *  is not either named one: folding it into one of them would make that branch
 *  lie, so it names both. */
const HIDDEN_COPY: Record<HiddenReason, string> = {
  created: "Every detected bundle has a project.",
  dismissed: "Every detected bundle is dismissed.",
  mixed: "Every detected bundle is dismissed or already has a project.",
};

/** The instruction that names the way out; the mixed state names none, so it
 *  shows both toggles instead. */
const HIDDEN_HINT: Partial<Record<HiddenReason, string>> = {
  created: "Turn on Show created to see them.",
  dismissed: "Turn on Show dismissed to see them.",
};

/** One reveal toggle. Its label is its accessible name, so the operator and a
 *  test read the same words. */
function visibilityToggle(
  label: string,
  checked: boolean,
  setChecked: (value: boolean) => void,
): React.ReactElement {
  return (
    <label className="review-toggle">
      <input
        type="checkbox"
        checked={checked}
        onChange={event => setChecked(event.target.checked)}
      />
      <span>{label}</span>
    </label>
  );
}

function rowClassName(selected: boolean, active: boolean): string {
  const classes = ["review-row"];
  if (selected) classes.push("is-selected");
  if (active) classes.push("is-current");
  return classes.join(" ");
}

type Props = {
  state: BundleDetectState;
  /** Writes (create, dismiss, extract) are CONTRIBUTE actions; a viewer reads. */
  canContribute: boolean;
};

/**
 * The bundle queue: a dense list on the left, the active row's details on the
 * right. Presentational - every fetch and mutation lives in `useBundleDetect`,
 * so this view only filters rows, holds a selection, a per-row name draft and
 * the active row.
 */
export default function BundleReviewView({ state, canContribute }: Props) {
  const {
    report,
    loading,
    busy,
    error,
    showDismissed,
    setShowDismissed,
    showCreated,
    setShowCreated,
  } = state;
  const [filter, setFilter] = React.useState<BundleFilter>("all");
  const [selectedKeys, setSelectedKeys] = React.useState<string[]>([]);
  const [names, setNames] = React.useState<Record<string, string>>({});
  const [activeKey, setActiveKey] = React.useState<string | null>(null);
  const rowRefs = React.useRef(new Map<string, HTMLDivElement>());
  // Where the active row sat before a refetch reordered or removed it. A key
  // survives a reorder; the remembered index is only the fallback for a row
  // that vanished, so the operator lands on whatever took its place.
  const lastIndexRef = React.useRef(0);
  // Whether the list owned focus a moment ago. Removing the focused row hands
  // focus to <body> before any effect can observe it, so `document.activeElement`
  // alone cannot tell us to put focus back.
  const listHadFocusRef = React.useRef(false);

  const rows = React.useMemo(() => report?.bundles ?? [], [report]);
  // One fetch feeds both toggles, and the badge calls the same predicate on the
  // same array: this is what keeps the count and the list from drifting.
  const visible = React.useMemo(
    () => rows.filter(row => matchesVisibility(row, { showDismissed, showCreated })),
    [rows, showDismissed, showCreated],
  );
  const filtered = React.useMemo(
    () => visible.filter(row => matchesFilter(row, filter)),
    [visible, filter],
  );
  const selected = visible.filter(row => selectedKeys.includes(row.key));
  const creatableSelected = selected.filter(isCreatable);
  const lowCreatable = visible.filter(row => row.confidence === "low" && isCreatable(row));
  const stale = report?.stale_dismissals ?? [];
  const activeRow = filtered.find(row => row.key === activeKey) ?? null;
  const activeArchive = activeRow === null ? null : archiveLine(activeRow);

  // The active key, not an index: a refetch reorders rows and an index would
  // silently point at a different bundle after every mutation.
  React.useEffect(() => {
    if (filtered.length === 0) {
      if (activeKey !== null) setActiveKey(null);
      return;
    }
    const index = filtered.findIndex(row => row.key === activeKey);
    if (index >= 0) {
      lastIndexRef.current = index;
      return;
    }
    const nextIndex = Math.min(lastIndexRef.current, filtered.length - 1);
    const nextKey = filtered[nextIndex].key;
    const restoreFocus = listHadFocusRef.current;
    lastIndexRef.current = nextIndex;
    setActiveKey(nextKey);
    // The rows are already committed, so this focuses the replacement row
    // before the state update above re-renders it as the current one.
    if (restoreFocus) rowRefs.current.get(nextKey)?.focus();
  }, [filtered, activeKey]);

  const toggleSelection = (key: string) => {
    setSelectedKeys(prev =>
      prev.includes(key) ? prev.filter(item => item !== key) : [...prev, key],
    );
  };

  const createItems = (items: BundleDetectItem[]): BundleCreateItem[] =>
    items.map(row => {
      const name = typedName(names, row);
      return name ? { key: row.key, name } : { key: row.key };
    });

  const createSelected = () => {
    void state.create(createItems(creatableSelected));
  };

  const createLowAsNameOnly = () => {
    void state.create(lowCreatable.map(row => ({ key: row.key, name_only: true })));
  };

  const dismissSelected = () => {
    void state.dismiss(selected.map(row => row.key), true);
  };

  const createActive = () => {
    if (activeRow) void state.create(createItems([activeRow]));
  };

  const dismissActive = () => {
    if (activeRow) void state.dismiss([activeRow.key], !activeRow.dismissed);
  };

  const moveActive = (index: number) => {
    const row = filtered[index];
    if (!row) return;
    lastIndexRef.current = index;
    setActiveKey(row.key);
    rowRefs.current.get(row.key)?.focus();
  };

  // Arrow keys, Home/End and Space: the mouse's two gestures, without one. A
  // click on the row body moves the detail target; the glyph toggles bulk
  // selection. Space is the glyph's keyboard equivalent on the active row.
  const handleListKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (filtered.length === 0) return;
    const last = filtered.length - 1;
    const current = Math.max(0, filtered.findIndex(row => row.key === activeKey));
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        moveActive(Math.min(current + 1, last));
        break;
      case "ArrowUp":
        event.preventDefault();
        moveActive(Math.max(current - 1, 0));
        break;
      case "Home":
        event.preventDefault();
        moveActive(0);
        break;
      case "End":
        event.preventDefault();
        moveActive(last);
        break;
      case " ":
        event.preventDefault();
        if (canContribute && activeKey !== null) toggleSelection(activeKey);
        break;
      default:
        break;
    }
  };

  const handleListBlur = (event: React.FocusEvent<HTMLDivElement>) => {
    const next = event.relatedTarget;
    if (next instanceof Node && event.currentTarget.contains(next)) return;
    listHadFocusRef.current = false;
  };

  const renderRow = (row: BundleDetectItem) => {
    const isSelected = selectedKeys.includes(row.key);
    const isActive = row.key === activeKey;
    return (
      <div
        key={row.key}
        role="option"
        aria-selected={isSelected}
        // The row the detail panel describes, not only the row styled as current.
        aria-current={isActive ? "true" : undefined}
        data-bundle-row={row.key}
        tabIndex={isActive ? 0 : -1}
        className={rowClassName(isSelected, isActive)}
        ref={element => {
          if (element) rowRefs.current.set(row.key, element);
          else rowRefs.current.delete(row.key);
        }}
        onClick={() => setActiveKey(row.key)}
      >
        <span className="review-row-name">
          {/* A plain span, not a checkbox: nothing focusable may live inside an
              option, or the listbox would have a second tab stop per row. The
              row body selects the detail target, this glyph selects for bulk
              actions, and Space is this glyph's keyboard equivalent. */}
          <span
            className={isSelected ? "review-check is-selected" : "review-check"}
            aria-hidden="true"
            onClick={
              canContribute
                ? event => {
                    event.stopPropagation();
                    toggleSelection(row.key);
                  }
                : undefined
            }
          >
            {isSelected && <Check className="h-3 w-3" />}
          </span>
          <span className="truncate">{row.title}</span>
        </span>
        <span className="review-row-path" title={row.rel_path}>{row.rel_path}</span>
        <span
          className={`review-row-confidence ${confidenceClass(row.confidence)}`}
          title={confidenceLabel(row.confidence)}
        >
          {row.confidence}
        </span>
        <span className="review-row-status">{statusLine(row)}</span>
        <span className="review-row-state">{rowState(row)}</span>
      </div>
    );
  };

  // Both toggles are the way out of an empty state as well as toolbar controls,
  // so each is defined once and rendered wherever it is needed.
  const showDismissedToggle = visibilityToggle("Show dismissed", showDismissed, setShowDismissed);
  const showCreatedToggle = visibilityToggle("Show created", showCreated, setShowCreated);

  const notices = (
    <>
      {report?.truncated && (
        <p className="review-notice">
          The walk stopped at the report limit; some bundles may be missing.
        </p>
      )}
      {stale.length > 0 && (
        <p className="review-notice">
          {`${countLabel(stale.length, "dismissal", "dismissals")} no longer match a bundle on disk.`}
        </p>
      )}
    </>
  );

  let body: React.ReactNode;
  if (!report) {
    if (loading) {
      body = (
        <p className="review-loading" aria-busy="true">Detecting vendor bundles…</p>
      );
    } else if (error) {
      body = (
        <div className="review-error" role="alert">
          <p>{error}</p>
          <button type="button" className={SECONDARY_BUTTON} onClick={() => void state.refresh()}>
            Try again
          </button>
        </div>
      );
    } else {
      body = null;
    }
  } else if (rows.length === 0) {
    body = <p className="review-empty">No vendor bundles were detected.</p>;
  } else if (visible.length === 0) {
    // Rows exist but are hidden, and which ones changes what the queue should
    // say. Each branch keeps the toggle that is its way out on screen, so the
    // instruction always has its control.
    const reason = hiddenReason(rows);
    body = (
      <>
        <div className="review-toolbar">
          {reason !== "dismissed" && showCreatedToggle}
          {reason !== "created" && showDismissedToggle}
        </div>
        <p className="review-empty">{HIDDEN_COPY[reason]}</p>
        {HIDDEN_HINT[reason] && <p className="review-empty">{HIDDEN_HINT[reason]}</p>}
      </>
    );
  } else {
    body = (
      <>
        <div className="review-toolbar">
          {BUNDLE_FILTERS.map(tab => (
            <button
              key={tab.id}
              type="button"
              aria-pressed={filter === tab.id}
              className={filter === tab.id ? "review-filter is-active" : "review-filter"}
              onClick={() => setFilter(tab.id)}
            >
              {tab.label}
            </button>
          ))}
          {showDismissedToggle}
          {showCreatedToggle}
          {canContribute && (
            <>
              <button
                type="button"
                className="review-filter"
                onClick={() => setSelectedKeys(filtered.map(row => row.key))}
              >
                Select all shown
              </button>
              <button
                type="button"
                className="review-filter"
                disabled={selectedKeys.length === 0}
                onClick={() => setSelectedKeys([])}
              >
                Clear selection
              </button>
            </>
          )}
          <span className="review-summary">{`Showing ${filtered.length} of ${visible.length}`}</span>
        </div>
        {/* Selection and the bulk bars exist only to feed a write, so a
            read-only view gets neither rather than disabled buttons that
            advertise a capability the operator does not have. */}
        {canContribute && (
          <div className="review-bulk" role="group" aria-label="Bundle actions">
            <button
              type="button"
              className={PRIMARY_BUTTON}
              disabled={busy || creatableSelected.length === 0}
              onClick={createSelected}
            >
              {`Create selected (${creatableSelected.length})`}
            </button>
            <button
              type="button"
              className={SECONDARY_BUTTON}
              disabled={busy || lowCreatable.length === 0}
              onClick={createLowAsNameOnly}
            >
              {`Create low as name-only (${lowCreatable.length})`}
            </button>
            <button
              type="button"
              className={SECONDARY_BUTTON}
              disabled={busy || selected.length === 0}
              onClick={dismissSelected}
            >
              {`Dismiss selected (${selected.length})`}
            </button>
            <span className="review-selection" aria-live="polite">
              {selected.length === 0
                ? "No bundles selected"
                : `${countLabel(selected.length, "bundle", "bundles")} selected`}
            </span>
          </div>
        )}
        {/* Filtered empty is a third state: the rows exist, the filter hid
            them. Say so, and give the way back instead of a dead list. */}
        {filtered.length === 0 && (
          <div className="review-toolbar">
            <p className="review-empty">No bundles match this filter.</p>
            <button type="button" className="review-filter" onClick={() => setFilter("all")}>
              Clear filters
            </button>
          </div>
        )}
        <div className="review-layout">
          <div
            className="review-list"
            role="listbox"
            // A viewer can select rows to read them but has no bulk write to
            // feed, so the list must not advertise a multi-select it cannot do.
            aria-multiselectable={canContribute ? true : undefined}
            aria-label="Bundles to confirm"
            onKeyDown={handleListKeyDown}
            onFocus={() => {
              listHadFocusRef.current = true;
            }}
            onBlur={handleListBlur}
          >
            {filtered.map(renderRow)}
          </div>
          <aside className="review-detail" aria-label="Bundle details">
            {activeRow === null ? (
              <p className="review-empty">Select a bundle to see its details.</p>
            ) : (
              <>
                <h2 className="review-detail-title">{activeRow.title}</h2>
                <div className="review-detail-facts">
                  <span>{statusLine(activeRow)}</span>
                  <span className="font-mono">{activeRow.rel_path}</span>
                  <span>{countsLine(activeRow)}</span>
                  <span>{coverLine(activeRow)}</span>
                  {activeArchive && <span>{activeArchive}</span>}
                  {activeRow.creator && <span>{`Creator ${activeRow.creator}`}</span>}
                  {activeRow.licence && <span>{`Licence ${activeRow.licence}`}</span>}
                  {activeRow.source_url && <span>{`Source ${activeRow.source_url}`}</span>}
                  <span>{`Detected by ${activeRow.detector} (${activeRow.metadata_shape})`}</span>
                  {activeRow.description_excerpt && (
                    <span className="review-detail-excerpt">{activeRow.description_excerpt}</span>
                  )}
                  {activeRow.existing_project && (
                    <span>{`Already created as project ${activeRow.existing_project}`}</span>
                  )}
                </div>
                {canContribute && (
                  <>
                    {/* An already-created row has nothing to create, so the name
                        field and the button go rather than sitting disabled. */}
                    {!activeRow.existing_project && (
                      <label className="review-detail-field">
                        <span>Project name</span>
                        <input
                          type="text"
                          aria-label={`Name for ${activeRow.title}`}
                          value={names[activeRow.key] ?? activeRow.name_suggestion}
                          onChange={event =>
                            setNames(prev => ({ ...prev, [activeRow.key]: event.target.value }))
                          }
                        />
                      </label>
                    )}
                    <div className="review-detail-actions">
                      {!activeRow.existing_project && (
                        <button
                          type="button"
                          className={PRIMARY_BUTTON}
                          disabled={busy || !isCreatable(activeRow)}
                          onClick={createActive}
                        >
                          Create project
                        </button>
                      )}
                      <button
                        type="button"
                        className={SECONDARY_BUTTON}
                        disabled={busy}
                        onClick={dismissActive}
                      >
                        {activeRow.dismissed ? "Undo dismiss" : "Dismiss"}
                      </button>
                    </div>
                  </>
                )}
                {/* The zip-only door. The queue passes the real capability
                    through, unlike the settings card, which sat behind an
                    editor-only gate; the component hides itself below
                    contributor and the route enforces `require_contribute`. */}
                {activeRow.blocked_reason === "archive_only" && activeRow.archive?.rel_path && (
                  <ArchiveExtractAction
                    canContribute={canContribute}
                    path={activeRow.archive.rel_path}
                    label={activeRow.title}
                    variant="detail"
                    onExtracted={() => void state.refresh()}
                  />
                )}
              </>
            )}
          </aside>
        </div>
      </>
    );
  }

  return (
    <ReviewQueueView
      title="Bundles to confirm"
      explanation="Folders that look like one download each: a Thingiverse README and licence, sibling docs, or a model/image/text mix. Detection writes nothing - creating a project is a separate, explicit step."
      count={bundleBadgeCount(report)}
      countTitle={describeQueueCount("bundles", bundleBadgeCount(report))}
      // The big count is what still needs a decision; this line carries the
      // whole walk's totals, the way the old Settings card's aggregate did.
      summary={report === null ? undefined : headline(report.counts)}
      lastRefreshedAt={state.lastRefreshedAt}
      onRefresh={() => void state.refresh()}
      refreshing={loading || busy}
      notices={notices}
    >
      {report && error && (
        // A failed create/dismiss keeps the server's message. There is no
        // "Try again": the retry is repeating the action, not refetching.
        <div className="review-error" role="alert">
          <p>{error}</p>
        </div>
      )}
      {body}
    </ReviewQueueView>
  );
}
