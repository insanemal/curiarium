import React from "react";

import type { BundleDetectCounts, BundleDetectItem } from "../../lib/api";
import type { BundleDetectState } from "./useBundleDetect";

type Filter = "all" | "ready" | "blocked" | "low";

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "ready", label: "Ready" },
  { id: "blocked", label: "Blocked" },
  { id: "low", label: "Low confidence" },
];

/** The server's blocked reasons, said the way an operator would say them. */
const BLOCKED_REASON_LABELS: Record<string, string> = {
  unresolved_directory: "no matching folder on disk",
  not_scanned: "no model file registered by a scan",
  archive_only: "zip only - import it, then run a library scan",
};

function headline(counts: BundleDetectCounts): string {
  const bundles = counts.total === 1 ? "1 bundle" : `${counts.total} bundles`;
  return `${bundles} found - ${counts.high} high confidence, ${counts.low} low`;
}

function matchesFilter(row: BundleDetectItem, filter: Filter): boolean {
  if (filter === "ready") return row.status === "ready";
  if (filter === "blocked") return row.status !== "ready";
  if (filter === "low") return row.confidence === "low";
  return true;
}

function isBlocked(row: BundleDetectItem): boolean {
  return row.status !== "ready";
}

function confidenceLabel(confidence: string): string {
  return confidence === "high" ? "High confidence" : "Low confidence";
}

function confidenceClass(confidence: string): string {
  return confidence === "high" ? "text-emerald-400" : "text-amber-400";
}

function statusLine(row: BundleDetectItem): string {
  if (!isBlocked(row)) return "Ready";
  const reason = row.blocked_reason
    ? BLOCKED_REASON_LABELS[row.blocked_reason] ?? row.blocked_reason
    : "blocked";
  return `Blocked - ${reason}`;
}

function countLabel(value: number, one: string, many: string): string {
  return `${value} ${value === 1 ? one : many}`;
}

function countsLine(row: BundleDetectItem): string {
  const parts = [
    countLabel(row.model_count, "model", "models"),
    countLabel(row.image_count, "image", "images"),
    countLabel(row.text_count, "text file", "text files"),
    countLabel(row.other_count, "other file", "other files"),
  ];
  return `${parts.join(", ")} - ${countLabel(row.assets_total, "registered asset", "registered assets")}`;
}

function coverLine(row: BundleDetectItem): string {
  if (!row.cover.rel_path) return "Cover: none";
  if (row.cover.reason === "largest-image-unregistered") {
    return `Cover ${row.cover.rel_path} (not registered)`;
  }
  return `Cover ${row.cover.rel_path}`;
}

function archiveLine(row: BundleDetectItem): string | null {
  if (!row.archive) return null;
  return `Archive ${row.archive.rel_path} - twin ${row.archive.twin_verified ? "verified" : "not verified"}`;
}

/** The name the operator would create with: their edit, else the suggestion. */
function typedName(names: Record<string, string>, row: BundleDetectItem): string {
  const name = names[row.key] ?? row.name_suggestion;
  return name.trim();
}

type RowProps = {
  row: BundleDetectItem;
  selected: boolean;
  name: string;
  busy: boolean;
  onToggle: () => void;
  onNameChange: (value: string) => void;
  onUndoDismiss: () => void;
};

function BundleRow({
  row,
  selected,
  name,
  busy,
  onToggle,
  onNameChange,
  onUndoDismiss,
}: RowProps) {
  const archive = archiveLine(row);

  return (
    <div className="flex flex-col gap-1 rounded-md border border-dashed border-panel-strong p-3">
      <div className="flex items-start justify-between gap-2">
        <label className="flex items-center gap-2 text-sm font-medium">
          <input
            type="checkbox"
            className="w-4 h-4"
            aria-label={`Select ${row.title}`}
            checked={selected}
            disabled={isBlocked(row)}
            onChange={onToggle}
          />
          <span>{row.title}</span>
        </label>
        <span className={`text-xs ${confidenceClass(row.confidence)}`}>
          {confidenceLabel(row.confidence)}
        </span>
      </div>
      <div className="text-xs opacity-80">{statusLine(row)}</div>
      <div className="font-mono text-[11px] break-all">{row.rel_path}</div>
      <div className="text-xs opacity-80">{countsLine(row)}</div>
      <div className="text-xs opacity-70">{coverLine(row)}</div>
      {archive && <div className="text-xs opacity-70">{archive}</div>}
      {row.existing_project && (
        <div className="text-xs text-emerald-400">
          {`Already created as project ${row.existing_project}`}
        </div>
      )}
      <input
        type="text"
        aria-label={`Name for ${row.title}`}
        className="text-sm px-2 py-1 rounded-md border border-panel-strong bg-panel"
        value={name}
        onChange={e => onNameChange(e.target.value)}
      />
      {row.dismissed && (
        <button
          type="button"
          className="text-xs px-2 py-1 rounded-md border border-panel-strong self-start disabled:opacity-60"
          disabled={busy}
          onClick={onUndoDismiss}
        >
          Undo dismiss
        </button>
      )}
    </div>
  );
}

type Props = {
  state: BundleDetectState;
};

/**
 * The dry-run bundle report. Presentational: every fetch and mutation lives in
 * `useBundleDetect`, so the card only filters rows and collects a selection,
 * a typed name and the dismiss toggle.
 */
export default function BundleDetectCard({ state }: Props) {
  const [filter, setFilter] = React.useState<Filter>("all");
  const [selectedKeys, setSelectedKeys] = React.useState<string[]>([]);
  const [names, setNames] = React.useState<Record<string, string>>({});

  const { report, loading, busy, error, showDismissed, setShowDismissed } = state;
  const rows = report?.bundles ?? [];
  // Dismissals are filtered here, not server-side: one fetch feeds the toggle.
  const visible = rows.filter(row => showDismissed || !row.dismissed);
  const filtered = visible.filter(row => matchesFilter(row, filter));
  const selected = visible.filter(row => selectedKeys.includes(row.key));
  const creatableSelected = selected.filter(row => !isBlocked(row));
  const lowConfidence = visible.filter(row => row.confidence === "low" && !isBlocked(row));
  const ready = !loading && !error;
  const stale = report?.stale_dismissals ?? [];

  const toggleSelection = (key: string) => {
    setSelectedKeys(prev => (prev.includes(key) ? prev.filter(k => k !== key) : [...prev, key]));
  };

  const createSelected = async () => {
    const items = creatableSelected.map(row => {
      const name = typedName(names, row);
      return name ? { key: row.key, name } : { key: row.key };
    });
    await state.create(items);
  };

  const createNameOnly = async () => {
    await state.create(lowConfidence.map(row => ({ key: row.key, name_only: true })));
  };

  const dismissSelected = async () => {
    await state.dismiss(
      selected.map(row => row.key),
      true,
    );
  };

  const undoDismiss = async (key: string) => {
    await state.dismiss([key], false);
  };

  return (
    <div className="rounded-lg border border-panel bg-panel-soft p-4 flex flex-col gap-3">
      <div>
        <div className="text-lg font-semibold">Vendor bundles</div>
        <p className="text-sm opacity-70">
          Folders that look like one download each: a Thingiverse README and licence, sibling
          docs, or a model/image/text mix. Detection writes nothing - creating a project is a
          separate, explicit step.
        </p>
      </div>
      {report && <p className="text-sm font-medium">{headline(report.counts)}</p>}
      {report?.truncated && (
        <p className="text-xs opacity-70">
          The walk stopped at the report limit; some bundles may be missing.
        </p>
      )}
      {stale.length > 0 && (
        <p className="text-xs opacity-70">
          {`${countLabel(stale.length, "dismissal", "dismissals")} no longer match a bundle on disk.`}
        </p>
      )}
      {loading && <p className="text-sm opacity-70">Detecting vendor bundles…</p>}
      {!loading && error && <p className="text-sm text-red-400">{error}</p>}
      {ready && rows.length === 0 && (
        <p className="text-sm opacity-70">No vendor bundles were detected.</p>
      )}
      {ready && rows.length > 0 && (
        <>
          <div className="flex items-center gap-2 flex-wrap">
            {FILTERS.map(tab => (
              <button
                key={tab.id}
                type="button"
                aria-pressed={filter === tab.id}
                className={
                  filter === tab.id
                    ? "text-xs px-2 py-1 rounded-md bg-accent"
                    : "text-xs px-2 py-1 rounded-md border border-panel-strong"
                }
                onClick={() => setFilter(tab.id)}
              >
                {tab.label}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <button
              type="button"
              className="text-sm px-3 py-2 rounded-md bg-accent hover:bg-accent-strong disabled:opacity-60"
              disabled={busy || creatableSelected.length === 0}
              onClick={() => void createSelected()}
            >
              Create selected
            </button>
            <button
              type="button"
              className="text-sm px-3 py-2 rounded-md border border-panel-strong disabled:opacity-60"
              disabled={busy || lowConfidence.length === 0}
              onClick={() => void createNameOnly()}
            >
              Create all as name-only
            </button>
            <button
              type="button"
              className="text-xs px-2 py-1 rounded-md border border-panel-strong disabled:opacity-60"
              disabled={busy || selected.length === 0}
              onClick={() => void dismissSelected()}
            >
              Dismiss selected
            </button>
            <label className="text-xs flex items-center gap-2">
              <input
                type="checkbox"
                checked={showDismissed}
                onChange={e => setShowDismissed(e.target.checked)}
                className="w-4 h-4"
              />
              <span>Show dismissed</span>
            </label>
          </div>
          {filtered.length === 0 && (
            <p className="text-sm opacity-70">No bundles match this filter.</p>
          )}
          {filtered.map(row => (
            <BundleRow
              key={row.key}
              row={row}
              selected={selectedKeys.includes(row.key)}
              name={names[row.key] ?? row.name_suggestion}
              busy={busy}
              onToggle={() => toggleSelection(row.key)}
              onNameChange={value => setNames(prev => ({ ...prev, [row.key]: value }))}
              onUndoDismiss={() => void undoDismiss(row.key)}
            />
          ))}
        </>
      )}
    </div>
  );
}
