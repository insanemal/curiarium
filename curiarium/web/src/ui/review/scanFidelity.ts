// The scan-fidelity notice's pure rules: what a report actually knows about the
// rule set that decided it and the paths it refused, and how that knowledge is
// worded. Kept out of the view so the `–`-until-known rule and the wording stay
// unit-testable without mounting a component.

import type { BundleReport, BundleRuleSet } from "../../lib/api/bundles";
import { countLabel } from "./bundleRows";
import { formatReviewCount } from "./useReviewCounts";

/**
 * Whether the report has anything to say about the scan itself. DELIBERATELY
 * not gated on `truncated`: truncation only ever cut the bundle ROWS, and
 * `refused` is never capped by `limit`, so a cut-short walk says nothing about
 * the ledger. A notice that appeared *only* because a walk was cut short would
 * be inventing a theme it has no data for; what makes it real is the server
 * having said something. (`showsDuplicateReport` does include `truncated`, and
 * that difference is the point: a truncated duplicate list is provably partial,
 * while a truncated ledger is not - so one must speak and the other must not.)
 */
export function showsScanFidelity(report: BundleReport): boolean {
  return Array.isArray(report.refused) || report.rule_set != null;
}

/**
 * How many paths the walk refused, or `null` when that is unknown. The
 * `truncated -> null` rule inherits `bundleBadgeCount`'s "a plausible-looking
 * undercount is worse than a dash": `MAX_SCAN_ENTRIES` can shorten the walk, so
 * a count the report cannot prove is a dash. Every entry counts regardless of
 * `kind` - filtering to `archive` would silently relabel the line from "N paths
 * refused" to "N archives refused".
 */
export function refusedCount(report: BundleReport): number | null {
  if (report.truncated) return null;
  if (!Array.isArray(report.refused)) return null;
  return report.refused.length;
}

/**
 * The refusal line. `0` and unknown must never render as each other: a real
 * zero is the server's own answer ("nothing was refused"), while a `–` says the
 * report cannot prove anything at all.
 */
export function refusedLine(count: number | null): string {
  if (count === null) return `Paths refused: ${formatReviewCount(null)}`;
  return `${countLabel(count, "path", "paths")} refused`;
}

/** Enough of the stamp to spot a changed rule set at a glance; the full 64
 *  characters ride in the title and the `sr-only` span, never truncated away. */
export const SHORT_STAMP = 8;

/**
 * The full rule-set stamp, or `null` when the report did not carry one. NOT
 * gated on `truncated`: the stamp describes the process's imported rule tables,
 * not the walk, so a cut-short walk does not make it unknown.
 */
export function ruleSetStamp(report: BundleReport): string | null {
  const stamp = report.rule_set?.stamp;
  if (typeof stamp !== "string" || stamp.length === 0) return null;
  return stamp;
}

/**
 * The rule-set line: the version an operator would quote, plus a short prefix of
 * the stamp so a changed rule set is visible without reading 64 characters.
 */
export function ruleSetLine(ruleSet: BundleRuleSet | null | undefined): string {
  if (ruleSet === null || ruleSet === undefined) return `Rule set ${formatReviewCount(null)}`;
  // A stamp that is present but unusable is not a reason to throw or to print
  // `()`: the version is still what an operator would quote, so it stays and the
  // unprovable half is simply not claimed.
  const stamp = ruleSet.stamp;
  if (typeof stamp !== "string" || stamp.length === 0) return `Rule set ${ruleSet.version}`;
  return `Rule set ${ruleSet.version} (${stamp.slice(0, SHORT_STAMP)})`;
}
