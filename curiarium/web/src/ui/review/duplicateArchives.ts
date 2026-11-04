// The duplicate-archive notice's pure rules: what a report actually knows about
// byte-identical archives, and how that knowledge is worded. Kept out of the
// view so the `–`-until-known rule and the wording stay unit-testable without
// mounting a component.

import type { BundleDuplicateGroup, BundleReport } from "../../lib/api/bundles";
import { formatBytes } from "../../lib/format";
import { countLabel } from "./bundleRows";
import { formatReviewCount } from "./useReviewCounts";

/**
 * Whether the report has anything to say about duplicates. A truncated walk is
 * not an empty one, so a cut-short report shows the panel even with no groups:
 * staying silent there would be exactly the silent under-reporting the ticket
 * forbids.
 */
export function showsDuplicateReport(report: BundleReport): boolean {
  return (report.duplicates?.length ?? 0) > 0 || report.truncated;
}

/**
 * How many duplicate groups the report found, or `null` when that is unknown.
 * The `truncated -> null` rule deliberately inherits `bundleBadgeCount`'s "a
 * plausible-looking undercount is worse than a dash". It is conservative here:
 * truncation only ever cut the bundle ROWS (`limit`), and duplicates are never
 * capped by `limit`, so the group list itself may well be complete - but the
 * report cannot prove that, and a `–` never lies.
 */
export function duplicateGroupCount(report: BundleReport): number | null {
  if (report.truncated) return null;
  if (report.duplicates === undefined) return null;
  return report.duplicates.length;
}

/**
 * The bytes a deduplication could reclaim, or `null` when unknown. An omitted
 * field is not a zero: `0 B` would claim the walk found nothing to reclaim.
 */
export function duplicateReclaimableBytes(report: BundleReport): number | null {
  if (report.truncated) return null;
  if (typeof report.duplicates_total_bytes !== "number") return null;
  return report.duplicates_total_bytes;
}

/**
 * The service's own report text, verbatim, or `null` when it sent none. Never
 * rebuilt from the groups: the server's string and a client rebuild would drift
 * the first time either side changes.
 */
export function duplicateReportText(report: BundleReport): string | null {
  const text = report.duplicates_text;
  if (typeof text !== "string") return null;
  return text.length > 0 ? text : null;
}

/**
 * The one line that summarises the duplicate report. Each half is unknown
 * independently, so each half gets its own `–`: collapsing "either unknown"
 * into one branch would print a total it does not have.
 */
export function duplicateSummaryLine(groups: number | null, bytes: number | null): string {
  if (groups !== null && bytes !== null) {
    return `${countLabel(groups, "duplicate group", "duplicate groups")} - ${formatBytes(bytes)} reclaimable`;
  }
  if (groups === null && bytes !== null) {
    return `Duplicate groups: ${formatReviewCount(null)} - ${formatBytes(bytes)} reclaimable`;
  }
  if (groups !== null) {
    return `${countLabel(groups, "duplicate group", "duplicate groups")} - reclaimable size ${formatReviewCount(null)}`;
  }
  return `Duplicate groups: ${formatReviewCount(null)} - reclaimable size ${formatReviewCount(null)}`;
}

/** One group: its size, how many copies of it exist, and what the extra copies cost. */
export function duplicateGroupLine(group: BundleDuplicateGroup): string {
  return `${formatBytes(group.size)} - ${countLabel(group.paths.length, "copy", "copies")} - ${formatBytes(group.reclaimable_bytes)} reclaimable`;
}

/**
 * The reveal toggle's label. A truncated report shows no count in `(N)` form:
 * an exact-looking number beside a `–` total would read as a complete list, so
 * that case says "listed" instead.
 */
export function duplicateToggleLabel(listed: number, open: boolean, truncated: boolean): string {
  if (open) return "Hide groups";
  if (truncated) return `Show the ${listed} listed groups`;
  return `Show groups (${listed})`;
}
