import React from "react";

import type { BundleReport } from "../../lib/api/bundles";
import type { BundleDetectState } from "./useBundleDetect";

export type ReviewCounts = { bundles: number | null };

/**
 * How many bundles still need a decision: undismissed and not already a
 * project. Blocked rows count - they still need someone to decide - and an
 * unknown or untrustworthy report is `null`, which renders as a dash. A zero
 * here must always mean a genuinely empty queue.
 */
export function bundleBadgeCount(report: BundleReport | null): number | null {
  if (report === null) return null;
  // A truncated walk may have withheld a row, so a row count would be a
  // plausible-looking undercount. The Trash precedent is the same
  // `–`-until-known rule.
  if (report.truncated) return null;
  return report.bundles.filter(row => !row.dismissed && !row.existing_project).length;
}

/** `null` is "not known yet", never zero. */
export function formatReviewCount(count: number | null): string {
  return count === null ? "\u2013" : String(count);
}

/**
 * The one wording a count uses in a nav label, a tooltip and a queue header, so
 * the badge and the queue cannot describe the same number two ways. Capitalised
 * because every caller starts a sentence with it.
 */
export function countAwaitingText(count: number | null, noun: string): string {
  if (count === null) return "Count unavailable";
  return `${count} ${count === 1 ? noun : `${noun}s`} awaiting a decision`;
}

export function useReviewCounts(state: BundleDetectState): ReviewCounts {
  const bundles = React.useMemo(() => bundleBadgeCount(state.report), [state.report]);
  // A fresh object on every render would churn the nav even when the count is
  // unchanged, so memoise the shape too.
  return React.useMemo(() => ({ bundles }), [bundles]);
}
