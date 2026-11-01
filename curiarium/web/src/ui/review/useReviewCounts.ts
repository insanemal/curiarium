import React from "react";

import type { BundleReport } from "../../lib/api/bundles";
import type { ReviewQueueKey } from "../views";
import type { BundleDetectState } from "./useBundleDetect";
import type { ImportRunsState } from "./useImportRuns";

/** Closed on purpose: every queue the REVIEW group renders has a key here. */
export type ReviewCounts = { bundles: number | null; imports: number | null };

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

/**
 * How many imports are running: `1` or `0` once a fetch has succeeded, `null`
 * before that. This is the one place the ticket decides what an unknown means,
 * and it cannot be read off the run alone: `run === null` covers both "the first
 * fetch failed" and "no run has ever happened", so `loaded` decides.
 */
export function importsBadgeCount(state: ImportRunsState): number | null {
  if (!state.loaded) return null;
  return state.run?.status === "running" ? 1 : 0;
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

/**
 * What one count means in words, for both the nav row and the queue header.
 * Bundles count decisions; imports count a boolean (running or not), so their
 * zero states the fact instead of counting anything.
 */
export function describeQueueCount(key: ReviewQueueKey, count: number | null): string {
  if (key === "bundles") return countAwaitingText(count, "bundle");
  if (count === null) return "Count unavailable";
  if (count === 0) return "No import currently running";
  if (count === 1) return "1 import running";
  return `${count} imports running`;
}

export function useReviewCounts(
  bundle: BundleDetectState,
  imports: ImportRunsState,
): ReviewCounts {
  const bundles = React.useMemo(() => bundleBadgeCount(bundle.report), [bundle.report]);
  const importsCount = importsBadgeCount(imports);
  // A fresh object on every render would churn the nav even when neither count
  // changed, so memoise the shape on the two primitives.
  return React.useMemo(() => ({ bundles, imports: importsCount }), [bundles, importsCount]);
}
