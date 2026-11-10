import type { LibraryMode } from "../lib/mode";

/** A mutually exclusive main-pane destination. `review` is the work-queue
 *  surface; `settings` is configuration and `audit` is admin-only. */
export type ActiveView = "library" | "settings" | "audit" | "review";

/** Which review queue is open in the main pane. Every key here has a queue
 *  view; nothing may be added without one. */
export type ReviewQueueKey = "bundles" | "imports" | "missing" | "trash";

/**
 * What the header's one "back" control is called, per mode it returns to.
 *
 * The control only ever does one thing - `setActiveView("library")` - and
 * `library` here is an `ActiveView`, never a `LibraryMode`: the modes are
 * `files` and `projects` (`lib/mode.ts`), and "library" is also the app's word
 * for the whole collection and the name of the mode radiogroup ("Library mode").
 * So the old single label named a destination that does not exist on the axis
 * the button actually leaves, and told the operator nothing about which pane
 * they would land in. The label now names the mode the click restores, which is
 * the mode already selected - the control never changes it.
 *
 * Keyed off `LibraryMode` so a third mode cannot arrive unnamed: `Record` makes
 * it a type error. `App.review.test.tsx` adds a runtime belt for a build that
 * widens `LibraryMode` with a cast - it asserts the key set against the two
 * mode names restated in the test, because `lib/mode.ts` keeps its own list
 * private; the type is what a genuinely new mode has to pass through.
 */
export const BACK_TO_MODE_LABEL: Record<LibraryMode, string> = {
  files: "Back to files",
  projects: "Back to projects",
};
