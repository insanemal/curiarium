import React from "react";
import { Check } from "lucide-react";

import {
  ENRICH_MAX_KEYS,
  type BundleCover,
  type BundleCreateItem,
  type BundleCreateResponse,
  type BundleDetectItem,
  type BundleReport,
} from "../../lib/api/bundles";
// Domain module, not the `../../lib/api` barrel: the tests replace that barrel
// with a fixed object (that is what `ArchiveExtractAction` reaches), so a named
// runtime import from it would throw "No export is defined on the mock".
import {
  createProjectFromExtract,
  extractArchive,
  listArchiveEntries,
  type ArchiveExtractResult,
} from "../../lib/api/imports";
import { fileUrl } from "../../lib/api/mediaUrls";
import { extOf } from "../../lib/assetKinds";
import { copyText } from "../../lib/clipboard";
import type { ResolvedTheme } from "../../lib/settings";
import { notifyError, notifyInfo } from "../../lib/toast";
import { useFileGrants } from "../../lib/useFileGrants";
import ArchiveExtractAction from "../archive/ArchiveExtractAction";
import ArchiveExtractDialog, { type ExtractTarget } from "../archive/ArchiveExtractDialog";
import { archivePlanLines } from "../archive/archivePlan";
import ErrorBoundary from "../ErrorBoundary";
import { ModelSnapshot } from "../ModelViewer";
import {
  BUNDLE_FILTERS,
  EXTRACT_AND_CREATE_HINT,
  EXTRACT_AND_CREATE_LABEL,
  archiveExtractTitle,
  archiveLine,
  bulkEnrichNoneLine,
  bulkEnrichTitle,
  canExtractAndCreate,
  canOfferEnrich,
  canOfferRecheck,
  confidenceLabel,
  countLabel,
  countsLine,
  coverLine,
  createdProjectLine,
  enrichSelectionLabel,
  enrichUnavailableLine,
  enrichmentLine,
  extractPathFor,
  headline,
  isArchiveOnly,
  isCreatable,
  matchesFilter,
  matchesVisibility,
  newPartsLine,
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
import {
  bulkPhaseLine,
  bulkPlanLines,
  bulkStopLine,
  bulkSummaryLine,
  bulkVerdictLines,
  outcomeLine,
  phaseLine,
  planExtractAndCreateMany,
  runExtractAndCreate,
  runExtractAndCreateMany,
  type BulkArchiveTarget,
  type BulkPlan,
  type BulkRunResult,
} from "./extractAndCreate";
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

/** The blocked-reason cell has two faces. It is a zip-only row's only door, so
 *  its live face carries the door's own underline and pointer; its shut face is
 *  the same sentence styled as any plain row's, because a cell that still looks
 *  like a door while refusing is the defect the run's write gate exists
 *  against. */
function blockedReasonClass(live: boolean): string {
  if (!live) return "review-row-status";
  return "review-row-status underline decoration-dotted underline-offset-2 hover:text-accent cursor-pointer";
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

/** The archives the given rows can extract. `extractPathFor` is the one rule, so
 *  the dialog is never handed a row that has no door: a ready row's twin zip is
 *  provenance, not a target, and the helper answers null for it. */
function extractTargets(rows: BundleDetectItem[]): ExtractTarget[] {
  return rows.flatMap(row => {
    const path = extractPathFor(row);
    return path ? [{ path, label: row.title }] : [];
  });
}

/** The many-archive door's snapshot of the rows it was clicked on: one target per
 *  extractable row, in queue order, carrying the label a verdict has to name and
 *  the operator's typed name (null when they typed nothing). Taken once, at the
 *  click, so the confirm step describes the click that opened it rather than a
 *  selection that moved underneath. */
function bulkTargets(
  rows: BundleDetectItem[],
  names: Record<string, string>,
): BulkArchiveTarget[] {
  return rows.flatMap(row => {
    const path = extractPathFor(row);
    return path ? [{ path, label: row.title, name: typedName(names, row) || null }] : [];
  });
}

/** The many-archive door's three faces, and nothing else is ever in flight. */
type BulkExtractState =
  | { phase: "planning"; rows: BundleDetectItem[]; targets: BulkArchiveTarget[]; line: string }
  | {
      phase: "confirm";
      rows: BundleDetectItem[];
      targets: BulkArchiveTarget[];
      plan: BulkPlan;
      lines: string[];
    }
  | { phase: "running"; rows: BundleDetectItem[]; targets: BulkArchiveTarget[]; line: string };

/** The detail pane's one enrichment door: a row with a gap gets the first
 *  pass, and a row the page already answered can only be re-checked. Returns
 *  null when neither applies, so the button is not rendered at all. The bulk
 *  bar's count and its disabled state read the same pair of offer predicates. */
function enrichActionLabel(row: BundleDetectItem): string | null {
  if (canOfferEnrich(row)) return "Enrich from source page";
  if (canOfferRecheck(row)) return "Re-check source page";
  return null;
}

type Props = {
  state: BundleDetectState;
  /** Writes (create, dismiss, extract) are CONTRIBUTE actions; a viewer reads. */
  canContribute: boolean;
  /** At least one project was created by this click, and the operator stays put.
   *
   *  App owns the project list, so a queue that creates a project without saying
   *  so leaves the sidebar and the project grid showing a list from before the
   *  click - the owner's "I have to refresh the whole thing". This is the same
   *  callback App already passes to `AssetGrid` and `ImportsReviewView`; the
   *  queue adds no mechanism of its own.
   */
  onProjectsChanged?: () => void;
  /** Land on the one project this click created.
   *
   *  Offered only when one click made exactly one project; the bump above is what
   *  a multi-row create gets, because landing on an arbitrary one of five is
   *  worse than not landing at all.
   */
  onProjectImported?: (projectId: string) => void;
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
export default function BundleReviewView({
  state,
  canContribute,
  onProjectsChanged,
  onProjectImported,
  theme = "light",
}: Props) {
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
  // Groups the operator took out of a compound row's project, per row key. A row
  // with nothing excluded has no entry at all: an empty list here would put
  // `exclude_groups: []` on the wire, which is not the same body as omitting it.
  const [excludedGroups, setExcludedGroups] = React.useState<Record<string, string[]>>({});
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
  // The one-step door's narration. It lives in the VIEW, not in the hook: a
  // successful create hides the row, and the hook clears `notice` on every
  // mutation. `key` is the row it belongs to; `running` is the run's own
  // in-flight flag, and it reaches every mutating door through `actionsLocked`
  // below.
  const [extractAndCreate, setExtractAndCreate] = React.useState<{
    key: string;
    line: string | null;
    running: boolean;
  } | null>(null);
  // What the run is about to release, said while it runs rather than only after:
  // the one-step door has no confirm step of its own, so this is where the
  // operator learns the models were inside a nested archive (Mf-0ck5). Same
  // classifier, and the same three lines, as `ArchiveExtractAction`'s panel.
  const [extractPlan, setExtractPlan] = React.useState<{ key: string; lines: string[] } | null>(null);
  // The many-archive form of the same door. Planning writes nothing, so the
  // click reads every listing first and renders the aggregate payload inline in
  // the bulk bar before anything is extracted; confirming then runs one archive
  // at a time. `rows` and `targets` are the click's own snapshot, which is what
  // the stale check below is derived from.
  const [bulkExtract, setBulkExtract] = React.useState<BulkExtractState | null>(null);
  // The bulk door's verdicts, at queue level: every successful create hides its
  // own row, so a per-row notice would vanish with the row it describes - the same
  // reason the one-step door's line lives in the notices slot.
  const [bulkNotice, setBulkNotice] = React.useState<{
    summary: string | null;
    stop: string | null;
    verdicts: string[];
  } | null>(null);
  // Where the caret goes when the confirm is cancelled: back where the click was.
  const bulkExtractButtonRef = React.useRef<HTMLButtonElement | null>(null);
  // Set by the cancel, consumed by the effect below once the lock is gone.
  const bulkReturnFocus = React.useRef(false);
  const extractAndCreateRunning = extractAndCreate?.running === true;
  // A confirm step is a door held open on a snapshot, so it holds the queue's
  // writes exactly as an in-flight run does.
  const bulkLocking = bulkExtract !== null;
  // One gate on every MUTATING door in the queue: the five bulk buttons, the bulk
  // door's own button and its confirm, the detail pane's create/dismiss/enrich,
  // the detail pane's own extract action, the blocked-reason cell and Enter on a
  // zip-only row. `busy` alone reports
  // only the final `creating` phase, while a one-step run owns the queue's
  // writes across all four of its phases, from the listing to the create - so
  // its own `running` flag locks its siblings for the whole of it. The run is
  // not cancellable, and a dismissal written mid-run would either have to be
  // undone or would still race the in-flight create; leaving the queue is the
  // cancellation (`afterExtract` answers `cancelled` on unmount, and
  // `runExtractAndCreate` then creates nothing). The controls it leaves alone
  // are the ones that cannot race the run: the filters, the reveal toggles,
  // Refresh, `Select all shown` / `Clear selection` and the typed-name and
  // group-include inputs, because the run snapshots what it needs when it
  // starts. It does shut two things that write nothing on their own - the
  // extract action's listing trigger and its confirm panel's `Cancel` - and
  // that is the action's own contract: every control of an instance whose
  // write would race the run refuses. `bulkLocking` is the same rule for the
  // many-archive door: planning, confirming and running all hold the queue, so no
  // other write can land between the summary the operator read and the writes it
  // described.
  const actionsLocked = busy || extractAndCreateRunning || bulkLocking;
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
  // Whether the open confirm still describes the selection it was opened on. It
  // is derived every render, against the SNAPSHOT's path list, so a selection
  // that moved underneath cannot be written under an older summary: the confirm
  // either describes exactly what will be written, or it refuses.
  const bulkLivePaths = extractableSelected.flatMap(row => {
    const path = extractPathFor(row);
    return path ? [path] : [];
  });
  const bulkConfirmStale =
    bulkExtract?.phase === "confirm" &&
    (bulkLivePaths.length !== bulkExtract.targets.length ||
      bulkLivePaths.some((path, index) => path !== bulkExtract.targets[index].path));
  // The bulk count, the bulk disabled state and the detail pane's door are one
  // predicate: `canOfferEnrich` is the single answer to "would this row be sent",
  // so a row the bar counts is a row the detail pane offers, and neither drifts.
  const enrichableSelected = selected.filter(canOfferEnrich);
  // Nothing sendable: the bar says why in the selected rows' own words. Each row's sentence is
  // decided by the predicates that refused its doors (`enrichUnavailableLine`), and
  // `bulkEnrichNoneLine` is what turns those per-row lines into ONE bar sentence under two
  // rules, both inherited from the paragraph this ticket deleted: it speaks ONE cause or it
  // stays silent (a bar reciting one row's verdict about a selection the same predicate answered
  // differently is that deleted paragraph in miniature), and it claims no door absent that the
  // bar is itself holding open (no payload sentence while a selected row can create or extract,
  // no page sentence while a selected row names a page). A row whose own line is null is not a
  // cause and never speaks, so a wholly-silent selection renders no sentence at all.
  const enrichNoneLine =
    selected.length > 0 && enrichableSelected.length === 0
      ? bulkEnrichNoneLine(selected)
      : null;
  // Deliberately `low` only, never `medium`: the bulk bar sends `name_only: true`,
  // which writes no source URL, and a medium row's whole point is the URL derived
  // from the id in its folder name. A medium row is created from the detail panel,
  // where the enrichment is written. Widening this line silently discards that URL.
  const lowCreatable = visible.filter(row => row.confidence === "low" && isCreatable(row));
  const stale = report?.stale_dismissals ?? [];
  const activeRow = filtered.find(row => row.key === activeKey) ?? null;
  const activeGroups = activeRow?.groups ?? [];
  const activeExcluded = activeRow === null ? [] : excludedGroups[activeRow.key] ?? [];
  const activeArchive = activeRow === null ? null : archiveLine(activeRow);
  const activeExtractPath = activeRow === null ? null : extractPathFor(activeRow);
  const activeCreatedLine = activeRow === null ? null : createdProjectLine(activeRow);
  const activeNewPartsLine = activeRow === null ? null : newPartsLine(activeRow);
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

  // `include` is the checkbox's own state, so the update is a function of what
  // the operator just asked for rather than of the list they saw a render ago.
  const toggleGroupExclusion = (key: string, name: string, include: boolean) => {
    setExcludedGroups(prev => {
      const others = (prev[key] ?? []).filter(item => item !== name);
      const next = include ? others : [...others, name];
      const draft = { ...prev };
      // Dropping the key, not storing `[]`: the create body must stay
      // byte-identical to the one that never mentions the field.
      if (next.length === 0) delete draft[key];
      else draft[key] = next;
      return draft;
    });
  };

  const createItems = (
    items: BundleDetectItem[],
    options: { excludeGroups?: boolean } = {},
  ): BundleCreateItem[] =>
    items.map(row => {
      const name = typedName(names, row);
      const item: BundleCreateItem = name ? { key: row.key, name } : { key: row.key };
      const excluded = excludedGroups[row.key] ?? [];
      // The bulk doors mean "include every group", so they pass no option and
      // send no field; only the detail pane can carry an exclusion.
      if (options.excludeGroups && excluded.length > 0) item.exclude_groups = excluded;
      return item;
    });

  /** Reports a bulk create to the owner of the project list, from the server's
   *  verdict and nothing else.
   *
   *  Only `status === "created"` counts. `project_id` is non-null on the
   *  `already_created` skip too (`bundle_service.py:2990-2996`), so reporting
   *  anything with an id would append rows - or land the operator on a project -
   *  this click never made; the name-collision skip at `:2987` answers
   *  `project_id: null` and is a second reason not to read the id as the verdict.
   *  `blocked` and `failed` wrote nothing either. A response with no created item
   *  therefore reports nothing at all: no wasted refetch of a list that cannot
   *  have changed.
   *
   *  Landing is narrower still: one item asked for, one project made. Five rows
   *  that all created is a real change to the list (so it is bumped) but no
   *  single project to land on. And when the landing call goes out it is the only
   *  one: `handleProjectImported` bumps `assetsVersion` itself, so one click
   *  stays one request.
   */
  const reportBulkCreate = (
    requested: BundleCreateItem[],
    response: BundleCreateResponse | null,
  ) => {
    if (!response) return;
    const made = response.results.filter(result => result.status === "created");
    if (made.length === 0) return;
    const landed = made.length === 1 && requested.length === 1 ? made[0].project_id : null;
    if (landed) onProjectImported?.(landed);
    else onProjectsChanged?.();
  };

  /** One door, one call, one report: the three create doors differ only in which
   *  rows they turn into items. */
  const createAndReport = async (items: BundleCreateItem[]): Promise<void> => {
    reportBulkCreate(items, await state.create(items));
  };

  const createSelected = () => {
    // A cover must never block or fail a create: `primeModelCovers` swallows its
    // own failures and is deliberately not awaited.
    void primeModelCovers(creatableSelected, { mediaUrl: fileUrl });
    void createAndReport(createItems(creatableSelected));
  };

  const createLowAsNameOnly = () => {
    void primeModelCovers(lowCreatable, { mediaUrl: fileUrl });
    void createAndReport(lowCreatable.map(row => ({ key: row.key, name_only: true })));
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
  // instructions is a lie the next reader would believe. The one-step door's
  // verdict is the other notice this Refresh answers, so its line clears too - a
  // "Refresh this queue" that leaves the stale verdict standing is the same
  // complaint in a second place. Only the line goes: a run still in flight keeps
  // `running`, so refreshing cannot re-enable the button under it.
  const refreshNow = () => {
    setSettling(null);
    setSettleTimedOut(false);
    setExtractAndCreate(current => (current ? { ...current, line: null } : null));
    setExtractPlan(null);
    void state.refresh();
  };

  const createActive = () => {
    if (!activeRow) return;
    void primeModelCovers([activeRow], { mediaUrl: fileUrl });
    void createAndReport(createItems([activeRow], { excludeGroups: true }));
  };

  // One action, four named phases, and the server as the readiness authority:
  // the orchestration itself is `runExtractAndCreate` (React-free, unit-tested),
  // so this only supplies the real deps and paints what each phase said.
  const startExtractAndCreate = (row: BundleDetectItem) => {
    const path = extractPathFor(row);
    if (!canContribute || !path) return;
    const key = row.key;
    // Each update is a function of the current state so a run that was
    // superseded (or cleared) is never resurrected by a late phase.
    const update = (line: string | null, running: boolean) => {
      setExtractAndCreate(current =>
        current && current.key === key ? { key, line, running } : current,
      );
    };
    setExtractAndCreate({ key, line: phaseLine("listing"), running: true });
    setExtractPlan({ key, lines: [] });
    void runExtractAndCreate({
      archivePath: path,
      item: { name: typedName(names, row) || null },
      deps: {
        listEntries: listArchiveEntries,
        extract: extractArchive,
        settle: results => state.afterExtract(results),
        // The extract-and-create door, called straight: it answers one verdict
        // for one archive, so there is no per-row key to match and no bundle
        // report to consult. Its refusal sentences are built from the counts it
        // measured, which is why nothing here infers a reason.
        create: payload => createProjectFromExtract(payload),
        // The cover is cosmetic and must never block or fail the action, exactly
        // as the other create doors treat it.
        beforeCreate: () => {
          void primeModelCovers([row], { mediaUrl: fileUrl });
        },
        onPhase: phase => update(phaseLine(phase), true),
        onPlan: plan => {
          const lines = archivePlanLines(plan);
          setExtractPlan(current => (current && current.key === key ? { key, lines } : current));
        },
      },
    }).then(outcome => {
      update(outcomeLine(outcome), false);
      // The list owner hears about the one verdict that wrote a project. This
      // door asks for one archive, so a `created` verdict is one project and the
      // operator lands on it - the row is the server's own re-read `ProjectOut`,
      // which is the only reason App can be handed an id at all; nothing here
      // invents a Project client-side. A `created` that named no row is still a
      // new project, so it gets the bump instead of the landing. `already_created`
      // wrote nothing this run and so reports nothing.
      let landedOn = null as string | null;
      if (outcome.kind === "created") {
        const createdId = outcome.project?.id;
        // Only a host that CAN land counts as landed: with no callback the queue
        // stays on screen, so the re-walk below is still the answer.
        if (createdId && onProjectImported) {
          onProjectImported(createdId);
          landedOn = createdId;
        } else {
          onProjectsChanged?.();
        }
      }
      // The door writes no bundle report of its own, so the queue is re-walked
      // once a project actually exists - including the already-created verdict,
      // whose row is created too and should read that way. A LANDING is the one
      // case that skips it, and that is not an omission: leaving Review
      // deactivates the hook, which bumps its request sequence
      // (`useBundleDetect.ts`'s `seq`), so a walk started now fails its own
      // newest-wins guard and its answer is thrown away - a whole-library server
      // walk for nothing. Re-entering the queue re-arms the activation effect and
      // walks properly then (pinned by App.review's "refetches once on re-entry").
      if (!landedOn && (outcome.kind === "created" || outcome.kind === "already-created")) {
        void state.refresh();
      }
    });
  };

  // The many-archive door, step one: read every listing and say what the whole
  // click would write. Nothing is extracted or created here, so an abandoned
  // confirm costs one listing per archive and no bytes. The rows and targets are
  // snapshotted now; the plan pass only ever adds the listings to them.
  const startBulkExtractAndCreate = (): void => {
    if (!canContribute || actionsLocked) return;
    const rows = extractableSelected.slice();
    const targets = bulkTargets(rows, names);
    if (targets.length === 0) return;
    setBulkNotice(null);
    setBulkExtract({ phase: "planning", rows, targets, line: "Reading the archives' file lists…" });
    void planExtractAndCreateMany({
      targets,
      listEntries: listArchiveEntries,
      // A function of the current state, so a plan that answered after the
      // confirm was cancelled never resurrects it.
      onProgress: line =>
        setBulkExtract(current =>
          current && current.phase === "planning" ? { ...current, line } : current,
        ),
    }).then(plan => {
      setBulkExtract(current =>
        current && current.phase === "planning"
          ? { phase: "confirm", rows, targets, plan, lines: bulkPlanLines(plan) }
          : current,
      );
    });
  };

  const cancelBulkExtractAndCreate = React.useCallback((): void => {
    bulkReturnFocus.current = true;
    setBulkExtract(null);
  }, []);

  // The bulk button is disabled while the confirm is open (`bulkLocking` holds
  // every mutating door), and a disabled control takes no focus - so the caret is
  // returned after the commit that clears the lock rather than inside the
  // handler, where the focus call would be swallowed.
  React.useEffect(() => {
    if (bulkExtract !== null || !bulkReturnFocus.current) return;
    bulkReturnFocus.current = false;
    bulkExtractButtonRef.current?.focus();
  }, [bulkExtract]);

  // Escape is the cancel for both open phases of the bulk door: the planning
  // pass and the confirm. Neither has written anything - planning is N listing
  // round-trips and the confirm is a snapshot - so leaving is free, which is
  // exactly why it must be one keystroke. `running` is deliberately not bound:
  // that run is already writing projects and is not cancellable.
  React.useEffect(() => {
    const phase = bulkExtract?.phase;
    if (phase !== "planning" && phase !== "confirm") return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      cancelBulkExtractAndCreate();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [bulkExtract?.phase, cancelBulkExtractAndCreate]);

  // Step two: the confirmed click. One archive at a time, in queue order, then
  // ONE re-walk of the report - the bulk door settles through
  // `state.waitAfterExtract`, which is the settle alone, because `afterExtract`
  // also walks the whole library and N of those would be N+1 walks for one click.
  const confirmBulkExtractAndCreate = (): void => {
    if (bulkExtract?.phase !== "confirm" || bulkConfirmStale) return;
    const { rows, targets, plan } = bulkExtract;
    setBulkExtract({
      phase: "running",
      rows,
      targets,
      line: bulkPhaseLine(1, targets.length, "listing"),
    });
    void runExtractAndCreateMany({
      plan,
      deps: {
        extract: extractArchive,
        create: payload => createProjectFromExtract(payload),
        settle: results => state.waitAfterExtract(results),
      },
      // A cover is cosmetic and must never block or fail a write, exactly as the
      // other create doors treat it.
      beforeCreate: archive => {
        const row = rows.find(item => extractPathFor(item) === archive.path);
        if (row) void primeModelCovers([row], { mediaUrl: fileUrl });
      },
      onProgress: line =>
        setBulkExtract(current =>
          current && current.phase === "running" ? { ...current, line } : current,
        ),
      // Each verdict is said as it lands, so a long queue narrates itself. The
      // final list below replaces these with the same lines in full.
      onVerdict: verdict => {
        const line = bulkVerdictLines([verdict])[0];
        if (line) {
          setBulkNotice(current => ({
            summary: null,
            stop: null,
            verdicts: [...(current?.verdicts ?? []), line],
          }));
        }
      },
    }).then((result: BulkRunResult) => {
      setBulkExtract(null);
      setBulkNotice({
        summary: bulkSummaryLine(result),
        stop: bulkStopLine(result),
        verdicts: bulkVerdictLines(result.verdicts),
      });
      // A landing is single-archive parity: land, no bump, no walk. Anything else
      // reports the change, and re-walks the queue at most once.
      if (result.landing && onProjectImported) {
        onProjectImported(result.landing.id);
        return;
      }
      if (result.created > 0) onProjectsChanged?.();
      if (!result.stopped && (result.created > 0 || result.alreadyCreated > 0)) {
        void state.refresh();
      }
    });
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
    if (canOfferRecheck(activeRow)) {
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
        // same `canContribute && isArchiveOnly` predicate plus the run's write
        // lock, so the two cannot disagree about who may extract when.
        event.preventDefault();
        if (!canContribute || actionsLocked || activeKey === null) break;
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
    // The cell only renders for a contributor; whether its door is live also
    // needs the archive-only shape and a queue whose writes are not held by the
    // one-step run.
    const archiveDoorLive = !actionsLocked && isArchiveOnly(row);
    return (
      <div
        key={row.key}
        role="option"
        aria-selected={isSelected}
        // The row the detail panel describes, not only the row styled as current.
        aria-current={isActive ? "true" : undefined}
        // Only the archive-only option advertises Enter, and `aria-keyshortcuts`
        // is the one way to say so without adding a tab stop. While the run
        // holds the queue's writes the door is shut, so the shortcut is not
        // advertised either.
        aria-keyshortcuts={canContribute && archiveDoorLive ? "Enter" : undefined}
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
            Enter on the option is the keyboard equivalent. Every other row,
            and this cell while a one-step run holds the queue's writes, keeps
            a plain, inert status cell. */}
        {canContribute && isArchiveOnly(row) ? (
          <span
            className={blockedReasonClass(archiveDoorLive)}
            title={archiveDoorLive ? archiveExtractTitle(row) : undefined}
            onClick={event => {
              // Shut while the run owns the queue's writes. Returning before
              // `stopPropagation` lets the row's own click move the detail
              // target, which is a read and never a write.
              if (!archiveDoorLive) return;
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
      {/* A one-way write must leave a sentence behind even when its own row is
          hidden by the create, so the verdict lives here rather than in the aside. */}
      {extractPlan && extractPlan.lines.length > 0 && (
        /* The listing was read back before anything was written, which is what
           lets the one-step door - it has no confirm panel - say what the archive
           holds at all. It says it mid-run: these lines appear after the
           extraction has been dispatched, and the only exit left is leaving the
           queue, which does not undo that write. What protects the operator here
           is the door's own refusals, not the choice this sentence reads as. */
        <div className="flex flex-col gap-1" data-extract-and-create-plan={extractPlan.key}>
          {extractPlan.lines.map(line => (
            <p key={line} className="review-notice">
              {line}
            </p>
          ))}
        </div>
      )}
      {extractAndCreate?.line && (
        <p
          className="review-notice"
          role="status"
          aria-live="polite"
          data-extract-and-create-notice={extractAndCreate.key}
        >
          {extractAndCreate.line}
        </p>
      )}
      {/* The many-archive door's verdicts. ONE live container: the summary, then
          the named verdicts in queue order, each naming its own archive. Queue
          level for the same reason the one-step line is: a successful create
          hides its own row, so a per-row sentence would vanish with the row. */}
      {bulkNotice !== null && (
        <div
          role="status"
          aria-live="polite"
          className="review-notice flex flex-col gap-1"
          data-bulk-extract-and-create-notice="true"
        >
          {bulkNotice.summary !== null && <p>{bulkNotice.summary}</p>}
          {bulkNotice.stop !== null && <p>{bulkNotice.stop}</p>}
          {bulkNotice.verdicts.map((line, index) => (
            <p key={`${index}-${line}`}>{line}</p>
          ))}
        </div>
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
              disabled={actionsLocked || creatableSelected.length === 0}
              onClick={createSelected}
            >
              {`Create selected (${creatableSelected.length})`}
            </button>
            <button
              type="button"
              className={SECONDARY_BUTTON}
              disabled={actionsLocked || lowCreatable.length === 0}
              onClick={createLowAsNameOnly}
            >
              {`Create low as name-only (${lowCreatable.length})`}
            </button>
            <button
              type="button"
              className={SECONDARY_BUTTON}
              disabled={actionsLocked || extractableSelected.length === 0}
              onClick={extractSelected}
            >
              {`Extract selected archives (${extractableSelected.length})`}
            </button>
            {/* The many-archive form of the same door: it reads the same
                `extractableSelected` array and the same `actionsLocked`
                expression, so one predicate decides both buttons' labels,
                counts and disabled states. */}
            <button
              type="button"
              ref={bulkExtractButtonRef}
              className={SECONDARY_BUTTON}
              data-bulk-extract-and-create="true"
              disabled={actionsLocked || extractableSelected.length === 0}
              onClick={startBulkExtractAndCreate}
            >
              {`Extract and create (${extractableSelected.length})`}
            </button>
            <button
              type="button"
              className={SECONDARY_BUTTON}
              data-bundle-enrich-selected="true"
              title={bulkEnrichTitle(enrichableSelected.length, selected.length)}
              disabled={actionsLocked || enrichableSelected.length === 0}
              onClick={enrichSelected}
            >
              {enrichSelectionLabel(enrichableSelected.length, selected.length)}
            </button>
            <button
              type="button"
              className={SECONDARY_BUTTON}
              disabled={actionsLocked || selected.length === 0}
              onClick={dismissSelected}
            >
              {`Dismiss selected (${selected.length})`}
            </button>
            <span className="review-selection" aria-live="polite">
              {selected.length === 0
                ? "No bundles selected"
                : `${countLabel(selected.length, "bundle", "bundles")} selected`}
            </span>
            {selected.length > 0 && enrichableSelected.length === 0 && enrichNoneLine !== null && (
              // A disabled control's title is not reliably surfaced as a
              // tooltip, and the control is not focusable, so the button's
              // `title` alone is unreachable and the same sentence is rendered
              // as text. Last in the row, and `basis-full` gives it a line of
              // its own with nothing after it. Deliberately NOT a live
              // region and not `role=alert`: the bar already speaks the count
              // through `.review-selection`, and a polite region here would
              // re-announce this same sentence on every selection change.
              <span className="review-detail-reason basis-full" data-bundle-enrich-none-reason="true">
                {enrichNoneLine}
              </span>
            )}
            {/* The many-archive door, inline in the bar it was clicked from:
                the aggregate payload while planning, then the confirm. `basis-full`
                gives it a line of its own whatever the buttons beside it do. */}
            {bulkExtract !== null && (
              <div
                className="basis-full flex flex-col gap-1"
                data-bulk-extract-and-create-bar="true"
              >
                {bulkExtract.phase === "planning" && (
                  <span
                    className="review-detail-reason"
                    data-bulk-extract-and-create-progress="true"
                  >
                    {bulkExtract.line}
                  </span>
                )}
                {bulkExtract.phase === "running" && (
                  <span
                    className="review-detail-reason"
                    data-bulk-extract-and-create-progress="true"
                    role="status"
                    aria-live="polite"
                  >
                    {bulkExtract.line}
                  </span>
                )}
                {bulkExtract.phase === "confirm" && (
                  <div
                    className="basis-full flex flex-col gap-1"
                    data-bulk-extract-and-create-confirm="true"
                  >
                    {bulkExtract.lines.map((line, index) => (
                      <p key={`${index}-${line}`} className="review-detail-reason">
                        {line}
                      </p>
                    ))}
                    {/* The summary was built from the snapshot the click made,
                        so a selection that moved since is refused rather than
                        written under these words. */}
                    {bulkConfirmStale && (
                      <p
                        className="review-detail-reason"
                        data-bulk-extract-and-create-stale="true"
                      >
                        The selection changed since this summary was read - cancel and choose
                        again.
                      </p>
                    )}
                    <div className="flex gap-2">
                      <button
                        type="button"
                        className={PRIMARY_BUTTON}
                        disabled={bulkConfirmStale}
                        onClick={confirmBulkExtractAndCreate}
                      >
                        {`Extract and create ${bulkExtract.targets.length} ${bulkExtract.targets.length === 1 ? "project" : "projects"}`}
                      </button>
                      <button
                        type="button"
                        className={SECONDARY_BUTTON}
                        onClick={cancelBulkExtractAndCreate}
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}
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
                  {activeNewPartsLine && (
                    <span data-bundle-new-parts="true">{activeNewPartsLine}</span>
                  )}
                </div>
                {/* The compound row's breakdown. Only a row with groups renders
                    it, so every other pane is exactly as it was; the checkboxes
                    are the operator's only say in what the project contains, and
                    they exist solely for a group the server will let us exclude. */}
                {activeGroups.length > 0 && (
                  <div
                    className="review-detail-facts"
                    role="group"
                    aria-label="Groups in this bundle"
                  >
                    {activeGroups.map(group => (
                      <div key={group.name} className="flex flex-wrap items-center gap-2">
                        <span>{`${group.name} - ${countLabel(group.model_count, "part", "parts")}`}</span>
                        {group.foreign && <span className="review-detail-reason">unrelated</span>}
                        {/* Both gates, because the server refuses either refusal:
                            `not_a_foreign_group` for the row's own part and
                            `not_separable_group` for one that was never a row of
                            its own. A checkbox the server would reject is a trap. */}
                        {group.foreign && group.separable && canContribute && (
                          <label className="review-toggle">
                            <input
                              type="checkbox"
                              aria-label={`Include ${group.name}`}
                              // Checked means "in the project": absence from the
                              // excluded list is the default, so a fresh row sends
                              // no `exclude_groups` at all.
                              checked={!activeExcluded.includes(group.name)}
                              onChange={event =>
                                toggleGroupExclusion(
                                  activeRow.key,
                                  group.name,
                                  event.target.checked,
                                )
                              }
                            />
                            <span>Include</span>
                          </label>
                        )}
                      </div>
                    ))}
                  </div>
                )}
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
                      {/* The one-step door. A zip-only row is blocked, so the plain Create button
                          beside it is disabled - this is the action that makes the row creatable
                          without a second click after a wait. */}
                      {canExtractAndCreate(activeRow) && (
                        <button
                          type="button"
                          className={PRIMARY_BUTTON}
                          data-extract-and-create="true"
                          disabled={actionsLocked}
                          title={EXTRACT_AND_CREATE_HINT}
                          onClick={() => startExtractAndCreate(activeRow)}
                        >
                          {EXTRACT_AND_CREATE_LABEL}
                        </button>
                      )}
                      {!activeRow.existing_project_id && (
                        <button
                          type="button"
                          className={PRIMARY_BUTTON}
                          disabled={actionsLocked || !isCreatable(activeRow)}
                          onClick={createActive}
                        >
                          Create project
                        </button>
                      )}
                      <button
                        type="button"
                        className={SECONDARY_BUTTON}
                        disabled={actionsLocked}
                        onClick={dismissActive}
                      >
                        {activeRow.dismissed ? "Undo dismiss" : "Dismiss"}
                      </button>
                      {activeEnrichLabel !== null && (
                        <button
                          type="button"
                          className={SECONDARY_BUTTON}
                          data-bundle-enrich="true"
                          disabled={actionsLocked}
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
                {activeExtractPath !== null && (
                  <ArchiveExtractAction
                    // React reuses this instance across a row change, so a
                    // mid-stage `stage`/`entries` would survive while `path`
                    // and `label` move on - the operator would confirm one
                    // archive and extract another. Keying remounts back to
                    // idle, and the mount-time focus skip keeps the caret.
                    key={activeExtractPath}
                    canContribute={canContribute}
                    path={activeExtractPath}
                    label={activeRow.title}
                    variant="detail"
                    // A second extract landing while the run's settle is
                    // pending would run a second settle and a second refetch,
                    // so this action refuses at every stage while the run owns
                    // the queue's writes.
                    disabled={actionsLocked}
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
