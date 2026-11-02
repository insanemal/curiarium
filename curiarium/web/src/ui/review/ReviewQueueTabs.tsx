import { useRef, type KeyboardEvent } from "react";

import type { Role } from "../../lib/auth";
import type { ReviewQueueKey } from "../views";
import { visibleQueues } from "./queues";
import { describeQueueCount, formatReviewCount, type ReviewCounts } from "./useReviewCounts";

type Props = {
  /** Which queue is open. The row only exists while Review is open, so there is no `activeView`. */
  activeQueue?: ReviewQueueKey | null;
  /** `null` per queue means "not known yet" and renders as a dash, never a zero. */
  counts?: ReviewCounts;
  onSelect?: (key: ReviewQueueKey) => void;
  /** `null` until the session answers; an unknown role gets no review surface. */
  role?: Role | null;
};

/**
 * The Review destination's queue selector: one `role="tab"` per visible queue,
 * choosing which queue the main pane shows. Queues keep no data of their own
 * here - selecting one is the same state change `ReviewNav` used to make, so
 * fetch-on-entry, the per-row `minRole` thresholds and the `–`-until-known
 * count rule all still hold (`ADR 0011` decisions 4, 6 and 7).
 *
 * Activation is manual, per APG: arrows/Home/End move focus only, and
 * Enter/Space/click activate the focused tab. Automatic activation would fire a
 * queue's first fetch on every arrow key, which is a whole-library walk for the
 * bundle queue.
 */
export default function ReviewQueueTabs({
  activeQueue = null,
  counts,
  onSelect,
  role = null,
}: Props) {
  const tabs = useRef(new Map<ReviewQueueKey, HTMLButtonElement | null>());
  const visible = visibleQueues(role);
  if (!visible.length) return null;
  // The selected row owns the group's single tab stop. App hands over the key it
  // resolved against the role, so a selected row is normally present; if the row
  // is somehow not one of the visible ones, the group would have no tab stop at
  // all and could not be reached from the keyboard, so the first visible row
  // holds it instead.
  const tabStop = visible.some(queue => queue.key === activeQueue) ? activeQueue : visible[0].key;

  const indexOfTarget = (target: EventTarget | null): number => {
    if (!(target instanceof Element)) return -1;
    const key = target.closest<HTMLElement>("[role='tab']")?.dataset.queue;
    return visible.findIndex(queue => queue.key === key);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = indexOfTarget(event.target);
    if (index < 0) return;
    // Browsers turn Enter/Space into a click on the focused button; activating
    // here and letting that click through would select the queue twice.
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelect?.(visible[index].key);
      return;
    }
    const last = visible.length - 1;
    let next: number;
    switch (event.key) {
      case "ArrowRight":
      case "ArrowDown":
        next = index === last ? 0 : index + 1;
        break;
      case "ArrowLeft":
      case "ArrowUp":
        next = index === 0 ? last : index - 1;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = last;
        break;
      default:
        return;
    }
    // Focus movement only: moving across the row must not fetch a queue.
    event.preventDefault();
    tabs.current.get(visible[next].key)?.focus();
  };

  return (
    <div role="tablist" aria-label="Review" className="review-tabs" onKeyDown={handleKeyDown}>
      {visible.map(queue => {
        const selected = activeQueue === queue.key;
        const count = counts?.[queue.key] ?? null;
        // An `aria-label` replaces the button's contents, so the count has to
        // appear here too or the badge is never announced.
        const countText = describeQueueCount(queue.key, count);
        const Icon = queue.icon;
        return (
          <button
            key={queue.key}
            ref={element => {
              tabs.current.set(queue.key, element);
            }}
            type="button"
            role="tab"
            id={`review-tab-${queue.key}`}
            aria-selected={selected}
            aria-controls={`review-tabpanel-${queue.key}`}
            tabIndex={queue.key === tabStop ? 0 : -1}
            aria-label={`${queue.label}: ${countText}`}
            title={countText}
            data-testid={`review-count-${queue.key}`}
            data-queue={queue.key}
            className="review-tab"
            onClick={() => onSelect?.(queue.key)}
          >
            <Icon className="h-4 w-4" aria-hidden="true" />
            <span>{queue.label}</span>
            <span className="sidebar-count">{formatReviewCount(count)}</span>
          </button>
        );
      })}
    </div>
  );
}
