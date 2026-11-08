import { type Project, listProjects } from "./api/projects";
import { type LibraryMode, decideMode, readStoredModeState } from "./mode";

/**
 * Boot budget for the project-list probe. It covers BOTH round trips
 * `listProjects()` makes: `GET /projects` and the grant prime it awaits after
 * the list lands. Past this the app starts in Files rather than keep the first
 * paint blank.
 */
export const MODE_DECISION_TIMEOUT_MS = 2000;

/**
 * The boot decision: the chosen mode plus the project list it was decided from.
 *
 * `projects` is handed to App so it can seed its project state with the list the
 * probe already fetched: a fresh boot into Projects then costs one
 * `GET /projects`, not a probe plus App's own read. `null` means "no list was
 * fetched", and App fetches its own (a stored mode answers without a request, a
 * failed or timed-out probe has no list to hand over).
 */
export type BootDecision = { mode: LibraryMode; projects: Project[] | null };

/**
 * The boot decision, memoised for the rest of the module's life. Non-rejecting:
 * `main.tsx` awaits it in front of the first render, where a rejection would
 * blank the page permanently.
 */
let primed: Promise<BootDecision> | null = null;

/** Test-only: forget the memoised boot decision so the next call decides again. */
export function resetInitialMode(): void {
  primed = null;
}

/**
 * Chooses the mode the app boots into, with the project list it decided from. A
 * stored choice is answered from storage alone (zero network calls); only an
 * absent one probes the library.
 */
export function resolveInitialMode(): Promise<BootDecision> {
  if (!primed) primed = decide();
  return primed;
}

async function decide(): Promise<BootDecision> {
  try {
    const state = readStoredModeState();
    // Stored, invalid or unreadable: the answer is already known from storage,
    // so boot costs no request at all - and there is no list to hand down.
    if (state.kind !== "absent") return { mode: decideMode(state, null), projects: null };
    const projects = await probeProjects();
    // An empty library hands back `[]`, not `null`: it is a real answer (and
    // decides Files), so App must not fetch the list a second time.
    return { mode: decideMode(state, projects?.length ?? null), projects };
  } catch {
    // Nothing above is expected to throw; the catch only guarantees the
    // contract main.tsx relies on (a settled promise, never a rejection).
    return { mode: "files", projects: null };
  }
}

/**
 * The project list the mode is decided from, or `null` when it cannot be known
 * in time.
 *
 * A failure is just "unknown list" -> Files, and no list for App. In particular
 * a 401 must NOT toast, log, or route through `handleUnauthorized()`: that
 * clears the token and kills the reload-refresh revival, and
 * `auth_required: false` deployments have no token yet still answer
 * `GET /projects`. For the same reason the probe does not gate on `readToken()`.
 */
function probeProjects(): Promise<Project[] | null> {
  return new Promise(resolve => {
    let settled = false;
    const finish = (projects: Project[] | null) => {
      if (settled) return;
      settled = true;
      // Clear on settle in both directions, so no dangling 2s timer outlives
      // the decision (or the test that made it).
      clearTimeout(timer);
      resolve(projects);
    };
    const timer = setTimeout(() => finish(null), MODE_DECISION_TIMEOUT_MS);
    listProjects().then(
      projects => finish(projects),
      () => finish(null),
    );
  });
}
