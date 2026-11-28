// The project view's bundle-drift notice, kept out of the view so the wording
// stays unit-testable without mounting - the same house style as
// `review/bundleRows.ts`. The state is never decided here: `state` is the wire's
// word, and `count` is the server's number (null unless the state is known).

import { formatReviewCount } from "./review/useReviewCounts";

/** The bundle folder still holds parts the project does not have. */
export const DRIFT_KNOWN = "known";
/** The server cannot tell: the marker's identity is not a path to walk. */
export const DRIFT_UNKNOWN = "unknown";
/** The project has no `_bundle` marker, so the question does not apply. */
export const DRIFT_NOT_APPLICABLE = "not_applicable";

export const ADD_DRIFT_PARTS_LABEL = "Add them";

const DRIFT_TAIL = "parts in this folder are not in the project";

/**
 * The notice sentence, or null when there is nothing to say.
 *
 * `not_applicable` is silence because a hand-made (or malformed-metadata)
 * project was never given a folder identity, and `known` with a zero count is
 * silence because a fresh project has nothing new - a `0` here would be a
 * notice about nothing. A `known` state whose count did not arrive is silence
 * too: the count is the state's own evidence, so a missing one says nothing
 * rather than borrowing the dash. Anything the client cannot resolve
 * (`unknown`, or an unrecognised state from a newer server) reads as the en
 * dash from `formatReviewCount`, never as a false zero.
 */
export function driftNoticeLine(state: string, count: number | null): string | null {
  if (state === DRIFT_NOT_APPLICABLE) return null;
  if (state === DRIFT_KNOWN) {
    if (count === null || count <= 0) return null;
    if (count === 1) return "1 part in this folder is not in the project";
    return `${count} ${DRIFT_TAIL}`;
  }
  // `unknown` and every state this client does not know. The dash comes from
  // the one formatter, so this sentence cannot invent a digit.
  return `${formatReviewCount(null)} ${DRIFT_TAIL}`;
}
