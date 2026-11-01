import { FileWarning, FolderUp, PackageSearch, Trash2, type LucideIcon } from "lucide-react";

import { roleAtLeast, type Role } from "../../lib/auth";
import type { ActiveView, ReviewQueueKey } from "../views";
import { describeQueueCount, formatReviewCount, type ReviewCounts } from "./useReviewCounts";

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

/**
 * One row per queue; later slices add their queue here rather than to the JSX.
 * `minRole` is per row because the queues' own routes differ: creating or
 * dismissing a bundle is CONTRIBUTE, while `GET /imports/runs` is READ - a
 * viewer already reads the run history, so the queue must not delete a proven
 * read surface. `GET /assets?missing=true` and `GET /library/scan` are READ too,
 * so a viewer reads the missing queue; only the rescan it offers is a WRITE. A
 * role below a row's threshold simply does not see that row.
 *
 * Trash follows the same split: `GET /trash` is require_read, so a viewer reads
 * the queue, while `POST /trash/purge` is require_admin - so the queue hides its
 * Empty trash control below admin rather than disabling it.
 */
const QUEUES: { key: ReviewQueueKey; label: string; minRole: Role; icon: LucideIcon }[] = [
  { key: "bundles", label: "Bundles to confirm", minRole: "contributor", icon: PackageSearch },
  { key: "imports", label: "Imports running", minRole: "viewer", icon: FolderUp },
  { key: "missing", label: "Missing files", minRole: "viewer", icon: FileWarning },
  { key: "trash", label: "Trash", minRole: "viewer", icon: Trash2 },
];

/**
 * The REVIEW group both sidebars render, from one place so the two cannot drift
 * apart. When no queue is visible for this role - an unknown role, or one below
 * every row's threshold - the group and its "Review" heading render nothing at
 * all.
 */
export default function ReviewNav({
  activeView = "library",
  activeQueue = null,
  counts,
  onOpenQueue,
  role = null,
}: Props) {
  const visible = QUEUES.filter(queue => roleAtLeast(role, queue.minRole));
  if (!visible.length) return null;

  return (
    <div className="sidebar-review">
      <div className="sidebar-section-heading">
        <span>Review</span>
      </div>
      {visible.map(queue => {
        const count = counts?.[queue.key] ?? null;
        const selected = activeView === "review" && activeQueue === queue.key;
        // An `aria-label` replaces the button's contents, so the count has to
        // appear here too or the badge is never announced.
        const countText = describeQueueCount(queue.key, count);
        const Icon = queue.icon;
        return (
          <button
            key={queue.key}
            type="button"
            className={`sidebar-all-items ${selected ? "is-selected" : ""}`}
            aria-label={`${queue.label}: ${countText}`}
            aria-current={selected ? "page" : undefined}
            onClick={() => onOpenQueue?.(queue.key)}
          >
            <span className="sidebar-all-icon"><Icon className="h-4 w-4" /></span>
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
