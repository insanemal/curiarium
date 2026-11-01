import type { ReactNode } from "react";

import { formatReviewCount } from "./useReviewCounts";

type Props = {
  title: string;
  explanation: string;
  /** Rows that need a decision. `null` = not known yet, rendered as a dash. */
  count: number | null;
  /** What the count means in words, e.g. "3 bundles awaiting a decision". */
  countTitle: string;
  /** `null` until the first successful fetch. */
  lastRefreshedAt?: number | null;
  /** Omitted by a queue that polls instead of offering a refresh. */
  onRefresh?: () => void;
  refreshing?: boolean;
  /** Notices that belong to the whole queue: truncation, stale dismissals. */
  notices?: ReactNode;
  /** Aggregate line under the explanation; `undefined` renders nothing. */
  summary?: string;
  children: ReactNode;
};

/** Local wall-clock time for the header; the operator cares when, not a date. */
export function formatClock(at: number): string {
  return new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/**
 * The header shell every review queue reuses. Filters, bulk actions and the
 * rows themselves differ per queue, so they arrive through `children`; only
 * what every queue states identically lives in here.
 */
export default function ReviewQueueView({
  title,
  explanation,
  count,
  countTitle,
  lastRefreshedAt = null,
  onRefresh,
  refreshing = false,
  notices,
  summary,
  children,
}: Props) {
  return (
    <section className="review-queue" aria-label={title}>
      <header className="review-queue-header">
        <h1 className="review-queue-title">{title}</h1>
        <span className="review-queue-count" title={countTitle}>
          {formatReviewCount(count)}
        </span>
        {onRefresh && (
          <button
            type="button"
            className="review-refresh"
            onClick={onRefresh}
            disabled={refreshing}
          >
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        )}
        <span className="review-refreshed">
          {lastRefreshedAt === null
            ? "Not refreshed yet"
            : `Last refreshed ${formatClock(lastRefreshedAt)}`}
        </span>
      </header>
      <p className="review-explanation">{explanation}</p>
      {summary !== undefined && <p className="review-queue-summary">{summary}</p>}
      {notices}
      {children}
    </section>
  );
}
