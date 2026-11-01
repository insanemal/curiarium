/** A mutually exclusive main-pane destination. `review` is the work-queue
 *  surface; `settings` is configuration and `audit` is admin-only. */
export type ActiveView = "library" | "settings" | "audit" | "review";

/** Which review queue is open in the main pane. Every key here has a queue
 *  view; nothing may be added without one. */
export type ReviewQueueKey = "bundles" | "imports" | "missing" | "trash";
