// The bundle queue's pure presentation rules. Keeping them out of the view
// lets the render stay declarative and the wording stay unit-testable without
// mounting a component.

import type { BundleDetectCounts, BundleDetectItem } from "../../lib/api/bundles";

export type BundleFilter = "all" | "ready" | "blocked" | "medium" | "low";

export const BUNDLE_FILTERS: { id: BundleFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "ready", label: "Ready" },
  { id: "blocked", label: "Blocked" },
  // Ordered as the confidences read, high to low, so the chips are a ladder an
  // operator can walk down rather than a set in arrival order.
  { id: "medium", label: "Medium confidence" },
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
  // The `?? 0` is what makes an older payload safe: a report written before the
  // medium confidence omits the field, and this shows `0 medium` rather than
  // `undefined medium`.
  const medium = counts.medium ?? 0;
  return `${bundles} found - ${counts.high} high confidence, ${medium} medium, ${counts.low} low`;
}

export function matchesFilter(row: BundleDetectItem, filter: BundleFilter): boolean {
  if (filter === "ready") return row.status === "ready";
  if (filter === "blocked") return row.status !== "ready";
  if (filter === "medium") return row.confidence === "medium";
  if (filter === "low") return row.confidence === "low";
  return true;
}

export function isBlocked(row: BundleDetectItem): boolean {
  return row.status !== "ready";
}

/** A row this queue can actually create from. Already-created rows are not
 *  creatable: the server can only answer `skipped`/`already_created` for them,
 *  so sending them would be a request that cannot create anything. The id is
 *  the identity, so a nameless project is still settled. */
export function isCreatable(row: BundleDetectItem): boolean {
  return !isBlocked(row) && !row.existing_project_id;
}

/** A row the queue may extract in place: it is blocked as an archive the scan
 *  never saw, and it names the archive to extract. The path check is what makes
 *  the label insufficient - a row without an archive path cannot be extracted,
 *  so the blocked-reason cell, the Enter key and the bulk count all read this
 *  one predicate and stay honest together: one rule feeds all three, so they
 *  cannot drift apart. */
export function isArchiveOnly(row: BundleDetectItem): boolean {
  return row.blocked_reason === "archive_only" && Boolean(row.archive?.rel_path);
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
    (options.showDismissed || !row.dismissed) &&
    (options.showCreated || !row.existing_project_id)
  );
}

/** A row that still needs a decision: visible under both toggles at their
 *  defaults. Delegating to `matchesVisibility` is what makes the badge and the
 *  list agree by construction rather than by coincidence. */
export function needsDecision(row: BundleDetectItem): boolean {
  return matchesVisibility(row, { showDismissed: false, showCreated: false });
}

export function confidenceLabel(confidence: string): string {
  if (confidence === "high") return "High confidence";
  if (confidence === "medium") return "Medium confidence";
  // The fallback stays: an unknown value from a newer server still reads as the
  // lowest confidence we know, rather than printing nothing.
  return "Low confidence";
}

export function statusLine(row: BundleDetectItem): string {
  if (!isBlocked(row)) return "Ready";
  const reason = row.blocked_reason
    ? BLOCKED_REASON_LABELS[row.blocked_reason] ?? row.blocked_reason
    : "blocked";
  return `Blocked - ${reason}`;
}

/** The detail line for a row that already became a project, or null when it did
 *  not. The id is the identity and is never spoken: a name we have is named, a
 *  name we do not have says so honestly rather than printing a uuid or nothing.
 *
 *  The asset identity is spoken differently on purpose. It is a claim about the
 *  FILES, not about intent: the project holds the bundle's files, but the app
 *  did not necessarily make it from this bundle, so the line says "Already in"
 *  and names the evidence. The marker, source URL and name identities keep the
 *  flat "Already created as project" line - the first two are the app's own
 *  record, and a name match is already a guess about the same project. */
export function createdProjectLine(row: BundleDetectItem): string | null {
  if (!row.existing_project_id) return null;
  const name = (row.existing_project_name ?? "").trim();
  if (row.existing_project_match === "assets") {
    if (name) return `Already in ${name} (matched by shared files)`;
    return "Already in a project (matched by shared files) - its name is unavailable.";
  }
  if (name) return `Already created as project ${name}`;
  return "Already created as a project - its name is unavailable.";
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
  // One branch per reason rather than a nested ternary: each reason's suffix is
  // an independent fact, and a reader must be able to add a fourth without
  // untangling the others.
  const { reason, rel_path: relPath } = row.cover;
  if (!relPath) return "Cover: none";
  if (reason === "largest-image-unregistered") return `Cover ${relPath} (not registered)`;
  if (reason === "model-preview") return `Cover ${relPath} (model preview)`;
  return `Cover ${relPath}`;
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
