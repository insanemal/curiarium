import { listProjects } from "./api/projects";
import { type LibraryMode, decideMode, readStoredModeState } from "./mode";

/**
 * Boot budget for the project-count probe. It covers BOTH round trips
 * `listProjects()` makes: `GET /projects` and the grant prime it awaits after
 * the list lands. Past this the app starts in Files rather than keep the first
 * paint blank.
 */
export const MODE_DECISION_TIMEOUT_MS = 2000;

/**
 * The boot decision, memoised for the rest of the module's life. Non-rejecting:
 * `main.tsx` awaits it in front of the first render, where a rejection would
 * blank the page permanently.
 */
let primed: Promise<LibraryMode> | null = null;

/** Test-only: forget the memoised boot decision so the next call decides again. */
export function resetInitialMode(): void {
  primed = null;
}

/**
 * Chooses the mode the app boots into. A stored choice is answered from storage
 * alone (zero network calls); only an absent one probes the library.
 */
export function resolveInitialMode(): Promise<LibraryMode> {
  if (!primed) primed = decide();
  return primed;
}

async function decide(): Promise<LibraryMode> {
  try {
    const state = readStoredModeState();
    // Stored, invalid or unreadable: the answer is already known from storage,
    // so boot costs no request at all.
    if (state.kind !== "absent") return decideMode(state, null);
    return decideMode(state, await countProjects());
  } catch {
    // Nothing above is expected to throw; the catch only guarantees the
    // contract main.tsx relies on (a settled promise, never a rejection).
    return "files";
  }
}

/**
 * The library's project count, or `null` when it cannot be known in time.
 *
 * A failure is just "unknown count" -> Files. In particular a 401 must NOT
 * toast, log, or route through `handleUnauthorized()`: that clears the token
 * and kills the reload-refresh revival, and `auth_required: false` deployments
 * have no token yet still answer `GET /projects`. For the same reason the probe
 * does not gate on `readToken()`.
 */
function countProjects(): Promise<number | null> {
  return new Promise(resolve => {
    let settled = false;
    const finish = (count: number | null) => {
      if (settled) return;
      settled = true;
      // Clear on settle in both directions, so no dangling 2s timer outlives
      // the decision (or the test that made it).
      clearTimeout(timer);
      resolve(count);
    };
    const timer = setTimeout(() => finish(null), MODE_DECISION_TIMEOUT_MS);
    listProjects().then(
      projects => finish(projects.length),
      () => finish(null),
    );
  });
}
