// The bundle queue's pure presentation rules. Keeping them out of the view
// lets the render stay declarative and the wording stay unit-testable without
// mounting a component.

import type { BundleDetectCounts, BundleDetectItem } from "../../lib/api/bundles";

export type BundleFilter = "all" | "ready" | "blocked" | "low";

export const BUNDLE_FILTERS: { id: BundleFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "ready", label: "Ready" },
  { id: "blocked", label: "Blocked" },
  { id: "low", label: "Low confidence" },
];

/** The server's blocked reasons, said the way an operator would say them. */
export const BLOCKED_REASON_LABELS: Record<string, string> = {
  unresolved_directory: "no matching folder on disk",
  not_scanned: "no model file registered by a scan",
  archive_only: "zip only - extract the archive in place, then the scan registers it",
};

export function headline(counts: BundleDetectCounts): string {
  const bundles = counts.total === 1 ? "1 bundle" : `${counts.total} bundles`;
  return `${bundles} found - ${counts.high} high confidence, ${counts.low} low`;
}

export function matchesFilter(row: BundleDetectItem, filter: BundleFilter): boolean {
  if (filter === "ready") return row.status === "ready";
  if (filter === "blocked") return row.status !== "ready";
  if (filter === "low") return row.confidence === "low";
  return true;
}

export function isBlocked(row: BundleDetectItem): boolean {
  return row.status !== "ready";
}

/** A row this queue can actually create from. Already-created rows are not
 *  creatable: the server can only answer `skipped`/`already_created` for them,
 *  so sending them would be a request that cannot create anything. */
export function isCreatable(row: BundleDetectItem): boolean {
  return !isBlocked(row) && !row.existing_project;
}

/** The two reveal toggles that widen what the queue shows. */
export type VisibilityOptions = {
  showDismissed: boolean;
  showCreated: boolean;
};

/** Whether a row survives the queue's reveal toggles. The list and the badge
 *  both call this one rule, so they cannot describe the queue two ways. */
export function matchesVisibility(row: BundleDetectItem, options: VisibilityOptions): boolean {
  return (
    (options.showDismissed || !row.dismissed) && (options.showCreated || !row.existing_project)
  );
}

/** A row that still needs a decision: visible under both toggles at their
 *  defaults. Delegating to `matchesVisibility` is what makes the badge and the
 *  list agree by construction rather than by coincidence. */
export function needsDecision(row: BundleDetectItem): boolean {
  return matchesVisibility(row, { showDismissed: false, showCreated: false });
}

export function confidenceLabel(confidence: string): string {
  return confidence === "high" ? "High confidence" : "Low confidence";
}

export function statusLine(row: BundleDetectItem): string {
  if (!isBlocked(row)) return "Ready";
  const reason = row.blocked_reason
    ? BLOCKED_REASON_LABELS[row.blocked_reason] ?? row.blocked_reason
    : "blocked";
  return `Blocked - ${reason}`;
}

export function countLabel(value: number, one: string, many: string): string {
  return `${value} ${value === 1 ? one : many}`;
}

export function countsLine(row: BundleDetectItem): string {
  const parts = [
    countLabel(row.model_count, "model", "models"),
    countLabel(row.image_count, "image", "images"),
    countLabel(row.text_count, "text file", "text files"),
    countLabel(row.other_count, "other file", "other files"),
  ];
  return `${parts.join(", ")} - ${countLabel(row.assets_total, "registered asset", "registered assets")}`;
}

export function coverLine(row: BundleDetectItem): string {
  if (!row.cover.rel_path) return "Cover: none";
  if (row.cover.reason === "largest-image-unregistered") {
    return `Cover ${row.cover.rel_path} (not registered)`;
  }
  return `Cover ${row.cover.rel_path}`;
}

export function archiveLine(row: BundleDetectItem): string | null {
  if (!row.archive) return null;
  return `Archive ${row.archive.rel_path} - twin ${row.archive.twin_verified ? "verified" : "not verified"}`;
}

/** The name the operator would create with: their edit, else the suggestion. */
export function typedName(names: Record<string, string>, row: BundleDetectItem): string {
  const name = names[row.key] ?? row.name_suggestion;
  return name.trim();
}
