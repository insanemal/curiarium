import React from "react";

import type { AssetCounts } from "../../lib/api/assets";
import type { BundleReport } from "../../lib/api/bundles";
import type { TrashList } from "../../lib/api/trash";
import type { ReviewQueueKey } from "../views";
import { needsDecision } from "./bundleRows";
import type { BundleDetectState } from "./useBundleDetect";
import type { ImportRunsState } from "./useImportRuns";

/** Closed on purpose: every queue the REVIEW group renders has a key here. */
export type ReviewCounts = {
  bundles: number | null;
  imports: number | null;
  missing: number | null;
  trash: number | null;
};

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
  // The list's own predicate, on the list's own array: at the default toggles
  // the badge and the rows cannot drift.
  return report.bundles.filter(needsDecision).length;
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

/**
 * How many files the last scan flagged as missing. This costs no request: App
 * already fetches `/assets/counts` for the sidebar, so the badge and the queue
 * header read the same one number. `null` means that counts fetch has not
 * succeeded - it renders as a dash, never `0`.
 */
export function missingBadgeCount(assetCounts: AssetCounts | null): number | null {
  if (assetCounts === null) return null;
  return assetCounts.missing;
}

/**
 * How many entries sit in the trash: `null` until a read has succeeded, then the
 * entry count. `GET /trash` sizes every entry with a recursive walk, so the
 * count is only known once the queue has been opened - it is never painted at
 * app load and never polled.
 */
export function trashBadgeCount(trash: TrashList | null): number | null {
  // `null` is "no read has succeeded yet". Until App passes a listing in, a call
  // can still arrive with nothing at all, which must read as unknown too - never
  // as a zero that claims an empty trash.
  if (trash == null) return null;
  return trash.entries.length;
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
  if (key === "missing") return countAwaitingText(count, "missing file");
  // A trashed entry does not await a decision, so the "awaiting a decision"
  // wording would lie, and "The trash is empty" would lie again when a purge
  // just failed and left rows behind. State the count instead.
  if (key === "trash") {
    if (count === null) return "Count unavailable";
    if (count === 0) return "No entries in the trash";
    if (count === 1) return "1 entry in the trash";
    return `${count} entries in the trash`;
  }
  if (count === null) return "Count unavailable";
  if (count === 0) return "No import currently running";
  if (count === 1) return "1 import running";
  return `${count} imports running`;
}

/**
 * The nav's four counts, from four independent sources. `trash` is required
 * because the trash queue closes the set: a caller that has not opened it yet
 * passes `null`, which renders as a dash rather than a fake zero.
 */
export function useReviewCounts(
  bundle: BundleDetectState,
  imports: ImportRunsState,
  assetCounts: AssetCounts | null,
  trash: TrashList | null,
): ReviewCounts {
  const bundles = React.useMemo(() => bundleBadgeCount(bundle.report), [bundle.report]);
  const importsCount = importsBadgeCount(imports);
  const missing = React.useMemo(() => missingBadgeCount(assetCounts), [assetCounts]);
  // The list object is fresh per fetch, so the entry count is memoised on it and
  // the outer memo still sees only a primitive.
  const trashCount = React.useMemo(() => trashBadgeCount(trash), [trash]);
  // A fresh object on every render would churn the nav even when no count
  // changed, so memoise the shape on the primitives.
  return React.useMemo(
    () => ({ bundles, imports: importsCount, missing, trash: trashCount }),
    [bundles, importsCount, missing, trashCount],
  );
}
