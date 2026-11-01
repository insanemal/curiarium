/** A mutually exclusive main-pane destination. `review` is the work-queue
 *  surface; `settings` is configuration and `audit` is admin-only. */
export type ActiveView = "library" | "settings" | "audit" | "review";

/** Which review queue is open in the main pane. Slice 4 adds "trash"; nothing
 *  else may be added without a queue view. */
export type ReviewQueueKey = "bundles" | "imports" | "missing";
