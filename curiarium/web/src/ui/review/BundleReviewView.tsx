import React from "react";
import { Check } from "lucide-react";

import {
  ENRICH_MAX_KEYS,
  type BundleCover,
  type BundleCreateItem,
  type BundleDetectItem,
  type BundleReport,
} from "../../lib/api/bundles";
// The type comes from the domain module, not the `../../lib/api` barrel: the
// tests replace that barrel with a fixed object, so a named import from it
// would throw "No export is defined on the mock".
import type { ArchiveExtractResult } from "../../lib/api/imports";
import { fileUrl } from "../../lib/api/mediaUrls";
import { extOf } from "../../lib/assetKinds";
import { copyText } from "../../lib/clipboard";
import type { ResolvedTheme } from "../../lib/settings";
import { notifyError, notifyInfo } from "../../lib/toast";
import { useFileGrants } from "../../lib/useFileGrants";
import ArchiveExtractAction from "../archive/ArchiveExtractAction";
import ArchiveExtractDialog, { type ExtractTarget } from "../archive/ArchiveExtractDialog";
import ErrorBoundary from "../ErrorBoundary";
import { ModelSnapshot } from "../ModelViewer";
import {
  BUNDLE_FILTERS,
  archiveLine,
  bulkEnrichTitle,
  canEnrich,
  canRecheck,
  confidenceLabel,
  countLabel,
  countsLine,
  coverLine,
  createdProjectLine,
  enrichSelectionLabel,
  enrichUnavailableLine,
  enrichmentLine,
  headline,
  isArchiveOnly,
  isCreatable,
  matchesFilter,
  matchesVisibility,
  rowBucket,
  rowStateChip,
  statusLine,
  typedName,
  type BundleFilter,
} from "./bundleRows";
import { primeModelCovers } from "./bundleCovers";
import {
  duplicateGroupCount,
  duplicateGroupLine,
  duplicateReclaimableBytes,
  duplicateReportText,
  duplicateSummaryLine,
  duplicateToggleLabel,
  showsDuplicateReport,
} from "./duplicateArchives";
import ReviewQueueView from "./ReviewQueueView";
import ScanFidelityNotice from "./ScanFidelityNotice";
import type { BundleDetectState } from "./useBundleDetect";
import { bundleBadgeCount, describeQueueCount } from "./useReviewCounts";

const PRIMARY_BUTTON = "text-xs px-2.5 py-1.5 rounded-md bg-accent disabled:opacity-60";
const SECONDARY_BUTTON =
  "text-xs px-2.5 py-1.5 rounded-md border border-panel-strong disabled:opacity-60";

/** The old card's colours, kept so a row's confidence reads the same way here:
 *  high is the vendor telling us, medium is an id we inferred from the folder
 *  name (worth a look), low is a shape guess. */
function confidenceClass(confidence: string): string {
  if (confidence === "high") return "text-emerald-400";
  if (confidence === "medium") return "text-amber-400";
  return "text-zinc-500";
}

/** Why a report with rows shows none of them. A project outranks a dismissal,
 *  exactly as `rowBucket` says, so a queue of one kind is never confused with a
 *  queue of the other. */
type HiddenReason = "created" | "dismissed" | "mixed";

function hiddenReason(rows: BundleDetectItem[]): HiddenReason {
  if (rows.every(row => rowBucket(row) === "created")) return "created";
  if (rows.every(row => rowBucket(row) === "dismissed")) return "dismissed";
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

/** What the queue says while the scan is still registering an extraction. */
function settlingLine(results: ArchiveExtractResult[]): string {
  const label = results.length === 1 ? results[0].path : `${results.length} archives`;
  return `Extracting ${label} - waiting for the library scan to register the files. This queue updates itself.`;
}

/** The archives the given rows name. One rule feeds the blocked-reason cell,
 *  the Enter key and the bulk bar, so all three doors extract the same set. */
function extractTargets(rows: BundleDetectItem[]): ExtractTarget[] {
  return rows.filter(isArchiveOnly).flatMap(row => {
    const path = row.archive?.rel_path;
    return path ? [{ path, label: row.title }] : [];
  });
}

/** The detail pane's one enrichment door: a row with a gap gets the first
 *  pass, and a row the page already answered can only be re-checked. Returns
 *  null when neither applies, so the button is not rendered at all. */
function enrichActionLabel(row: BundleDetectItem): string | null {
  if (canEnrich(row)) return "Enrich from source page";
  if (canRecheck(row)) return "Re-check source page";
  return null;
}

type Props = {
  state: BundleDetectState;
  /** Writes (create, dismiss, extract) are CONTRIBUTE actions; a viewer reads. */
  canContribute: boolean;
  /** Only reaches the derived cover's renderer, which reads its colours from the
   *  `--mv-*` variables; App does not thread it yet, so light is the default. */
  theme?: ResolvedTheme;
};

/**
 * The row's face: a served thumbnail when one exists, otherwise the bundle's own
 * model rendered into one, otherwise nothing at all.
 *
 * Module-level on purpose: a parent re-render must not remount this component,
 * because a remount would start the snapshot job over.
 */
function CoverThumb({
  cover,
  theme,
}: {
  cover: BundleCover;
  theme: ResolvedTheme;
}): React.ReactElement {
  // The `<img>`/snapshot re-reads the grant cache on every render, so the cache
  // has to stay warm while the row is mounted (`preview.tsx`'s idiom, where the
  // same hook gates the modal's own preview). The result gates the image: the
  // first request must never be unsigned.
  const ready = useFileGrants([cover.url, cover.thumb_url]);
  // A thumbnail saved after mount (the renderer's callback) replaces the served
  // one without a refetch of the report.
  const [thumb, setThumb] = React.useState<string | null>(null);
  // The empty shell, never `null`: the row is a six-track grid, and a null child
  // reserves no track at all, so a skipped cell would shift every cell after it
  // one track left. This holds the cell from the first paint too - `useFileGrants`
  // answers false on a cold cache while the grant is minted.
  if (!ready) return <span className="review-thumb" aria-hidden="true" />;
  const path = thumb ?? cover.thumb_url;
  if (path) {
    return (
      <span className="review-thumb" aria-hidden="true">
        <img src={fileUrl(path)} alt="" aria-hidden="true" className="review-thumb-img" />
      </span>
    );
  }
  if (cover.reason === "model-preview" && cover.asset_id && cover.url) {
    return (
      <span className="review-thumb" aria-hidden="true">
        <ErrorBoundary compact>
          <React.Suspense fallback={null}>
            <ModelSnapshot
              url={fileUrl(cover.url)}
              ext={extOf(cover.rel_path ?? "")}
              assetId={cover.asset_id}
              theme={theme}
              // A 2.5rem cell has no room for "Preview budget reached", and the
              // status is not what the operator is looking at: the face is.
              showStatus={false}
              onThumbnailSaved={saved => setThumb(saved.thumb_url ?? null)}
            />
          </React.Suspense>
        </ErrorBoundary>
      </span>
    );
  }
  // No cover is a real answer (`reason === "none"`), not a broken image: the
  // empty square keeps the cell, and with it the six-track grid, reserved.
  return <span className="review-thumb" aria-hidden="true" />;
}

/** Why the group list can be short even though it is never capped by `limit`. */
const TRUNCATED_DUPLICATES =
  "The walk stopped at the report limit, so this list is a lower bound: some duplicate groups may be missing.";

/**
 * The duplicate-archive notice: what the walk found on disk, and its report text
 * to carry out of the app.
 *
 * Module-level on purpose: a parent re-render must not remount this component,
 * or the operator's expanded list would collapse under them.
 */
function DuplicateArchivesPanel({ report }: { report: BundleReport }): React.ReactElement | null {
  const [open, setOpen] = React.useState(false);
  // No element at all, not an empty shell: this sits in `ReviewQueueView`'s
  // notices, a flex column which leaves no track for an absent child, so there
  // is nothing to hold - unlike `CoverThumb` in the six-track row grid.
  if (!showsDuplicateReport(report)) return null;

  const groups = report.duplicates ?? [];
  const count = duplicateGroupCount(report);
  const bytes = duplicateReclaimableBytes(report);
  const text = duplicateReportText(report);

  // The server's string, not one rebuilt from the groups: two formatters drift.
  // It arrives as an argument rather than being closed over so the only call
  // site is the button that proves the text exists.
  const copyReport = async (reportText: string) => {
    try {
      await copyText(reportText);
      notifyInfo("Copied the duplicate report to the clipboard");
    } catch {
      notifyError("Could not copy the duplicate report to the clipboard");
    }
  };

  return (
    <div className="review-panel" data-duplicate-report="">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium">{duplicateSummaryLine(count, bytes)}</span>
        {/* Only offered when the server sent text: a copy that copies nothing
            would only toast a lie. */}
        {text !== null && (
          <button type="button" className={SECONDARY_BUTTON} onClick={() => void copyReport(text)}>
            Copy report
          </button>
        )}
        {groups.length > 0 && (
          <button
            type="button"
            className="review-filter"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
          >
            {duplicateToggleLabel(groups.length, open, report.truncated)}
          </button>
        )}
      </div>
      {report.truncated && <p className="review-notice">{TRUNCATED_DUPLICATES}</p>}
      {open && groups.length > 0 && (
        <ul className="flex flex-col gap-1 text-[11px]">
          {groups.map(group => (
            <li key={group.sha256}>
              <span className="flex flex-col gap-0.5">
                <span>{duplicateGroupLine(group)}</span>
                {group.paths.map(path => (
                  <span key={path} className="font-mono break-all">
                    {path}
                  </span>
                ))}
              </span>
            </li>
          ))}
        </ul>
      )}
      {/* The notice's whole point: it is a reading aid. Nothing here touches
          the files, so it says so, and names the only next step there is. */}
      <p className="text-[11px] opacity-70">
        Nothing is deleted or moved here: copy the report and act outside the app.
      </p>
    </div>
  );
}

/**
 * The bundle queue: a dense list on the left, the active row's details on the
 * right. Presentational - every fetch and mutation lives in `useBundleDetect`,
 * so this view only filters rows, holds a selection, a per-row name draft and
 * the active row.
 */
export default function BundleReviewView({ state, canContribute, theme = "light" }: Props) {
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
  // One dialog serves all three doors (card, blocked row, bulk bar). `returnKey`
  // is the row to put focus back on when it closes, so a keyboard user who
  // pressed Enter is not dropped on <body>.
  const [extractRequest, setExtractRequest] = React.useState<{
    targets: ExtractTarget[];
    returnKey: string | null;
  } | null>(null);
  const [settling, setSettling] = React.useState<string | null>(null);
  const [settleTimedOut, setSettleTimedOut] = React.useState(false);
  const rowRefs = React.useRef(new Map<string, HTMLDivElement>());
  // Where the active row sat before a refetch reordered or removed it. A key
  // survives a reorder; the remembered index is only the fallback for a row
  // that vanished, so the operator lands on whatever took its place.
  const lastIndexRef = React.useRef(0);
  // Whether the list owned focus a moment ago. Removing the focused row hands
  // focus to <body> before any effect can observe it, so `document.activeElement`
  // alone cannot tell us to put focus back.
  const listHadFocusRef = React.useRef(false);
  // The detail pane is the other door whose focused control can be removed by
  // the same settle refetch, so it needs the same memory.
  const asideHadFocusRef = React.useRef(false);
  // The landmark the empty-queue step of the focus ladder hands the caret to:
  // an emptied queue renders neither the listbox nor a row.
  const queueRootRef = React.useRef<HTMLElement | null>(null);

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
  const extractableSelected = selected.filter(isArchiveOnly);
  const enrichableSelected = selected.filter(canEnrich);
  // Deliberately `low` only, never `medium`: the bulk bar sends `name_only: true`,
  // which writes no source URL, and a medium row's whole point is the URL derived
  // from the id in its folder name. A medium row is created from the detail panel,
  // where the enrichment is written. Widening this line silently discards that URL.
  const lowCreatable = visible.filter(row => row.confidence === "low" && isCreatable(row));
  const stale = report?.stale_dismissals ?? [];
  const activeRow = filtered.find(row => row.key === activeKey) ?? null;
  const activeArchive = activeRow === null ? null : archiveLine(activeRow);
  const activeCreatedLine = activeRow === null ? null : createdProjectLine(activeRow);
  const activeEnrichmentLine = activeRow === null ? null : enrichmentLine(activeRow);
  const activeEnrichLabel = activeRow === null ? null : enrichActionLabel(activeRow);
  const activeEnrichReason = activeRow === null ? null : enrichUnavailableLine(activeRow);

  // The active key, not an index: a refetch reorders rows and an index would
  // silently point at a different bundle after every mutation.
  React.useEffect(() => {
    const restoreFocus = listHadFocusRef.current || asideHadFocusRef.current;
    if (filtered.length === 0) {
      if (activeKey !== null) setActiveKey(null);
      // Neither the listbox nor the aside renders a row here, so the queue's
      // own region is the only honest home. Both flags are consumed: a later
      // filter chip that empties the list must not have its caret stolen by a
      // flag left standing.
      if (restoreFocus) {
        asideHadFocusRef.current = false;
        listHadFocusRef.current = false;
        queueRootRef.current?.focus();
      }
      return;
    }
    const index = filtered.findIndex(row => row.key === activeKey);
    if (index >= 0) {
      lastIndexRef.current = index;
      return;
    }
    const nextIndex = Math.min(lastIndexRef.current, filtered.length - 1);
    const nextKey = filtered[nextIndex].key;
    lastIndexRef.current = nextIndex;
    setActiveKey(nextKey);
    // The rows are already committed, so this focuses the replacement row
    // before the state update above re-renders it as the current one. The
    // aside flag is consumed here because removing the focused element fires
    // no `focusout`, so this is the only place that can clear a stale `true`;
    // focusing the row re-sets the list flag through its own focus event.
    if (restoreFocus) {
      asideHadFocusRef.current = false;
      rowRefs.current.get(nextKey)?.focus();
    }
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
    // A cover must never block or fail a create: `primeModelCovers` swallows its
    // own failures and is deliberately not awaited.
    void primeModelCovers(creatableSelected, { mediaUrl: fileUrl });
    void state.create(createItems(creatableSelected));
  };

  const createLowAsNameOnly = () => {
    void primeModelCovers(lowCreatable, { mediaUrl: fileUrl });
    void state.create(lowCreatable.map(row => ({ key: row.key, name_only: true })));
  };

  const dismissSelected = () => {
    void state.dismiss(selected.map(row => row.key), true);
  };

  // The server refuses more than `ENRICH_MAX_KEYS` keys per request, so the
  // bulk door sends the first ones in row order and its label says so
  // (`enrichSelectionLabel`).
  const enrichSelected = () => {
    const keys = enrichableSelected.slice(0, ENRICH_MAX_KEYS).map(row => row.key);
    if (keys.length > 0) void state.enrich(keys);
  };

  const openExtract = (targets: ExtractTarget[], returnKey: string | null) => {
    if (!canContribute || targets.length === 0) return;
    setSettleTimedOut(false);
    setExtractRequest({ targets, returnKey });
  };

  const closeExtract = () => {
    const key = extractRequest?.returnKey ?? null;
    setExtractRequest(null);
    if (key) rowRefs.current.get(key)?.focus();
  };

  const extractSelected = () => {
    openExtract(extractTargets(extractableSelected), activeKey);
  };

  // Shared by the dialog and the detail pane's own `ArchiveExtractAction`, so
  // the two doors cannot settle, or report, differently. `afterExtract` waits
  // for the library walk to register the extracted files and then refetches:
  // the hook owns the refetch, so this view never calls `refresh` for it.
  const handleExtracted = (results: ArchiveExtractResult[]) => {
    // `files > 0` is the direct test of "something was written": a junk-only
    // archive answers `files: 0` with `already_present: false`, so filtering on
    // `already_present` would report a no-write as an extraction.
    const written = results.filter(result => result.files > 0);
    if (written.length === 0) {
      // Nothing landed, so nothing will register: no notice, no wait.
      setSettling(null);
      setSettleTimedOut(false);
      return;
    }
    setSettleTimedOut(false);
    setSettling(settlingLine(results));
    void state
      .afterExtract(results)
      .then(outcome => {
        setSettleTimedOut(outcome === "timeout");
      })
      .catch(() => {
        // A failed settle is indistinguishable from a slow one to the
        // operator, and leaving it uncaught would be an unhandled rejection.
        setSettleTimedOut(true);
      })
      .finally(() => {
        setSettling(null);
      });
  };

  // The timeout line tells the operator to refresh once the scan finishes, so
  // acting on that advice has to clear it: a notice that outlives its own
  // instructions is a lie the next reader would believe.
  const refreshNow = () => {
    setSettleTimedOut(false);
    void state.refresh();
  };

  const createActive = () => {
    if (!activeRow) return;
    void primeModelCovers([activeRow], { mediaUrl: fileUrl });
    void state.create(createItems([activeRow]));
  };

  const dismissActive = () => {
    if (activeRow) void state.dismiss([activeRow.key], !activeRow.dismissed);
  };

  // A re-check is a forced refetch of a page that already answered; a first
  // pass reads the page only to fill the empties. The two branches pass a
  // different argument count on purpose, so each door's call is what the hook
  // and its tests see.
  const enrichActive = () => {
    if (!activeRow) return;
    if (canRecheck(activeRow)) {
      void state.enrich([activeRow.key], { refresh: true });
      return;
    }
    void state.enrich([activeRow.key]);
  };

  const moveActive = (index: number) => {
    const row = filtered[index];
    if (!row) return;
    lastIndexRef.current = index;
    setActiveKey(row.key);
    rowRefs.current.get(row.key)?.focus();
  };

  // Arrow keys, Home/End, Space and Enter: the mouse's gestures, without one. A
  // click on the row body moves the detail target; the glyph toggles bulk
  // selection. Space is the glyph's keyboard equivalent on the active row, and
  // Enter is the blocked-reason cell's.
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
      case "Enter": {
        // The blocked-reason cell is the mouse's door (it cannot be focusable
        // inside an option); Enter is its keyboard equivalent, gated on the
        // same `canContribute && isArchiveOnly` predicate so the two cannot
        // disagree about who may extract.
        event.preventDefault();
        if (!canContribute || activeKey === null) break;
        const row = filtered.find(item => item.key === activeKey);
        if (row && isArchiveOnly(row)) openExtract(extractTargets([row]), row.key);
        break;
      }
      default:
        break;
    }
  };

  const handleListBlur = (event: React.FocusEvent<HTMLDivElement>) => {
    const next = event.relatedTarget;
    if (next instanceof Node && event.currentTarget.contains(next)) return;
    listHadFocusRef.current = false;
  };

  // The detail pane owns the other door: its extract trigger (and that
  // control's own stage buttons) can be removed by the settle refetch, and
  // DOM removal fires no `focusout`, so the flag has to be consumed at the
  // ladder instead of cleared here.
  const handleAsideBlur = (event: React.FocusEvent<HTMLElement>) => {
    const next = event.relatedTarget;
    if (next instanceof Node && event.currentTarget.contains(next)) return;
    asideHadFocusRef.current = false;
  };

  const renderRow = (row: BundleDetectItem) => {
    const isSelected = selectedKeys.includes(row.key);
    const isActive = row.key === activeKey;
    const chip = rowStateChip(row);
    return (
      <div
        key={row.key}
        role="option"
        aria-selected={isSelected}
        // The row the detail panel describes, not only the row styled as current.
        aria-current={isActive ? "true" : undefined}
        // Only the archive-only option advertises Enter, and `aria-keyshortcuts`
        // is the one way to say so without adding a tab stop.
        aria-keyshortcuts={canContribute && isArchiveOnly(row) ? "Enter" : undefined}
        data-bundle-row={row.key}
        tabIndex={isActive ? 0 : -1}
        className={rowClassName(isSelected, isActive)}
        ref={element => {
          if (element) rowRefs.current.set(row.key, element);
          else rowRefs.current.delete(row.key);
        }}
        onClick={() => setActiveKey(row.key)}
      >
        <CoverThumb cover={row.cover} theme={theme} />
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
        {/* For a zip-only row the blocked reason IS the affordance: clicking
            it extracts the archive in place. Deliberately not a <button>, not
            `role="button"` and not `tabIndex` - nothing focusable may live
            inside an option, or the listbox would have a second tab stop per
            row (see the glyph above). Space is already bulk selection, so
            Enter on the option is the keyboard equivalent. Every other row
            keeps a plain, inert status cell. */}
        {canContribute && isArchiveOnly(row) ? (
          <span
            className="review-row-status underline decoration-dotted underline-offset-2 hover:text-accent cursor-pointer"
            title="Extract this archive in place (Enter)"
            onClick={event => {
              event.stopPropagation();
              openExtract(extractTargets([row]), row.key);
            }}
          >
            {statusLine(row)}
          </span>
        ) : (
          <span className="review-row-status">{statusLine(row)}</span>
        )}
        <span className="review-row-state" title={chip.title ?? undefined}>
          {chip.label}
        </span>
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
      {report && <DuplicateArchivesPanel report={report} />}
      {report && <ScanFidelityNotice report={report} />}
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
          <button type="button" className={SECONDARY_BUTTON} onClick={refreshNow}>
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
              disabled={busy || extractableSelected.length === 0}
              onClick={extractSelected}
            >
              {`Extract selected archives (${extractableSelected.length})`}
            </button>
            <button
              type="button"
              className={SECONDARY_BUTTON}
              data-bundle-enrich-selected="true"
              title={bulkEnrichTitle(enrichableSelected.length, selected.length)}
              disabled={busy || enrichableSelected.length === 0}
              onClick={enrichSelected}
            >
              {enrichSelectionLabel(enrichableSelected.length, selected.length)}
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
        {/* `review-notice` rather than a new class: the stylesheet owns the
            queue's notice chrome, and this ticket's scope does not reach
            `index.css`. */}
        {(settling || settleTimedOut) && (
          <p role="status" aria-live="polite" className="review-notice">
            {settling ??
              "The library scan is taking longer than expected. Refresh the queue when it finishes."}
          </p>
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
          <aside
            className="review-detail"
            aria-label="Bundle details"
            // React's onFocus/onBlur are focusin/focusout, so they bubble: a
            // focus anywhere in this subtree sets the flag, which is the point.
            onFocus={() => {
              asideHadFocusRef.current = true;
            }}
            onBlur={handleAsideBlur}
          >
            {activeRow === null ? (
              <p className="review-empty">Select a bundle to see its details.</p>
            ) : (
              <>
                <h2 className="review-detail-title">{activeRow.title}</h2>
                <div className="review-detail-facts">
                  <span>{statusLine(activeRow)}</span>
                  <span className="font-mono">{activeRow.rel_path}</span>
                  <span>{countsLine(activeRow)}</span>
                  <CoverThumb cover={activeRow.cover} theme={theme} />
                  <span>{coverLine(activeRow)}</span>
                  {activeArchive && <span>{activeArchive}</span>}
                  {activeRow.creator && <span>{`Creator ${activeRow.creator}`}</span>}
                  {activeRow.licence && <span>{`Licence ${activeRow.licence}`}</span>}
                  {activeRow.source_url && <span>{`Source ${activeRow.source_url}`}</span>}
                  <span>{`Detected by ${activeRow.detector} (${activeRow.metadata_shape})`}</span>
                  {/* What the vendor's page supplied, set apart from the
                      detector's own verdict above so a reader can tell the
                      two apart. */}
                  {activeEnrichmentLine && (
                    <span data-bundle-enrichment="true">{activeEnrichmentLine}</span>
                  )}
                  {activeRow.description_excerpt && (
                    <span className="review-detail-excerpt">{activeRow.description_excerpt}</span>
                  )}
                  {activeCreatedLine && <span>{activeCreatedLine}</span>}
                </div>
                {canContribute && (
                  <>
                    {/* An already-created row has nothing to create, so the name
                        field and the button go rather than sitting disabled. */}
                    {!activeRow.existing_project_id && (
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
                      {!activeRow.existing_project_id && (
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
                      {activeEnrichLabel !== null && (
                        <button
                          type="button"
                          className={SECONDARY_BUTTON}
                          data-bundle-enrich="true"
                          disabled={busy}
                          title="Read the creator and licence from the bundle's source page (only empty fields are filled)"
                          onClick={enrichActive}
                        >
                          {activeEnrichLabel}
                        </button>
                      )}
                      {activeEnrichReason !== null && (
                        // Deliberately NOT a live region and not `role=alert`:
                        // the queue already speaks through exactly two polite
                        // regions (the selection count and the notice, both
                        // outside this aside), and a third would re-announce
                        // this same sentence on every ArrowDown.
                        // It lives inside `canContribute` because it stands where
                        // the door would: a viewer sees neither the door nor the
                        // reason why there is none.
                        <span className="review-detail-reason" data-bundle-enrich-reason="true">
                          {activeEnrichReason}
                        </span>
                      )}
                    </div>
                  </>
                )}
                {/* The zip-only door. The queue passes the real capability
                    through, unlike the settings card, which sat behind an
                    editor-only gate; the component hides itself below
                    contributor and the route enforces `require_contribute`. */}
                {activeRow.blocked_reason === "archive_only" && activeRow.archive?.rel_path && (
                  <ArchiveExtractAction
                    // React reuses this instance across a row change, so a
                    // mid-stage `stage`/`entries` would survive while `path`
                    // and `label` move on - the operator would confirm one
                    // archive and extract another. Keying remounts back to
                    // idle, and the mount-time focus skip keeps the caret.
                    key={activeRow.archive.rel_path}
                    canContribute={canContribute}
                    path={activeRow.archive.rel_path}
                    label={activeRow.title}
                    variant="detail"
                    // The two doors - this AT-safe button and the dialog the
                    // blocked-reason cell / Enter / bulk bar open - share
                    // `handleExtracted`, so neither can refresh differently.
                    onExtracted={result => {
                      handleExtracted([result]);
                    }}
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
      // The empty-queue step of the focus ladder focuses this landmark: a row
      // that vanished with nothing to replace it leaves no other home.
      rootRef={queueRootRef}
      // The big count is what still needs a decision; this line carries the
      // whole walk's totals, the way the old Settings card's aggregate did.
      summary={report === null ? undefined : headline(report.counts)}
      lastRefreshedAt={state.lastRefreshedAt}
      onRefresh={refreshNow}
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
      {state.notice && (
        <p className="review-notice" role="status">
          {state.notice}
        </p>
      )}
      {body}
      {extractRequest && (
        <ArchiveExtractDialog
          targets={extractRequest.targets}
          canContribute={canContribute}
          // Close first: that restores focus to the row that opened the
          // dialog, then the settle starts from there.
          onExtracted={results => {
            closeExtract();
            handleExtracted(results);
          }}
          onClose={closeExtract}
        />
      )}
    </ReviewQueueView>
  );
}
