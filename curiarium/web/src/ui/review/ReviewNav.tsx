import { PackageSearch } from "lucide-react";

import { roleAtLeast, type Role } from "../../lib/auth";
import type { ActiveView, ReviewQueueKey } from "../views";
import { countAwaitingText, formatReviewCount, type ReviewCounts } from "./useReviewCounts";

type Props = {
  activeView?: ActiveView;
  /** Which queue is open, so exactly one row can be `aria-current="page"`. */
  activeQueue?: ReviewQueueKey | null;
  /** `null` per queue means "not known yet" and renders as a dash, never a zero. */
  counts?: ReviewCounts;
  onOpenQueue?: (key: ReviewQueueKey) => void;
  /** `null` until the session answers; an unknown role gets no review surface. */
  role?: Role | null;
};

/** One row per queue; later slices add their queue here rather than to the JSX. */
const QUEUES: { key: ReviewQueueKey; label: string }[] = [
  { key: "bundles", label: "Bundles to confirm" },
];

/**
 * The REVIEW group both sidebars render, from one place so the two cannot drift
 * apart. Creating or dismissing a bundle is a CONTRIBUTE write - the API's own
 * threshold - so a viewer gets no row at all, and detection is never fired on
 * their behalf.
 */
export default function ReviewNav({
  activeView = "library",
  activeQueue = null,
  counts,
  onOpenQueue,
  role = null,
}: Props) {
  if (!roleAtLeast(role, "contributor")) return null;

  return (
    <div className="sidebar-review">
      <div className="sidebar-section-heading">
        <span>Review</span>
      </div>
      {QUEUES.map(queue => {
        const count = counts?.[queue.key] ?? null;
        const selected = activeView === "review" && activeQueue === queue.key;
        // An `aria-label` replaces the button's contents, so the count has to
        // appear here too or the badge is never announced.
        const countText = countAwaitingText(count, "bundle");
        return (
          <button
            key={queue.key}
            type="button"
            className={`sidebar-all-items ${selected ? "is-selected" : ""}`}
            aria-label={`${queue.label}: ${countText}`}
            aria-current={selected ? "page" : undefined}
            onClick={() => onOpenQueue?.(queue.key)}
          >
            <span className="sidebar-all-icon"><PackageSearch className="h-4 w-4" /></span>
            <span className="flex-1 text-left">{queue.label}</span>
            <span className="sidebar-count" data-testid={`review-count-${queue.key}`} title={countText}>
              {formatReviewCount(count)}
            </span>
          </button>
        );
      })}
    </div>
  );
}
