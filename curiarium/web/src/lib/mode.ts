/** Top-level browsing mode of the library. */
export type LibraryMode = "files" | "projects";

export const MODE_STORAGE_KEY = "curiarium_library_mode";

const MODES: readonly LibraryMode[] = ["files", "projects"];
const DEFAULT_MODE: LibraryMode = "files";

/** What this browser's storage says about the mode, before any decision. */
export type StoredModeState =
  | { kind: "mode"; mode: LibraryMode }
  | { kind: "invalid" }
  | { kind: "absent" }
  | { kind: "unreadable" };

/** Reads the persisted mode without deciding which mode to show. Never throws. */
export function readStoredModeState(): StoredModeState {
  if (typeof window === "undefined") return { kind: "absent" };
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(MODE_STORAGE_KEY);
  } catch {
    // Unreadable storage cannot be trusted, and cannot be overwritten either.
    return { kind: "unreadable" };
  }
  if (raw === null) return { kind: "absent" };
  // Only an exact match counts, so stale or hand-edited values cannot put the
  // UI into a mode it does not know how to render.
  return MODES.includes(raw as LibraryMode)
    ? { kind: "mode", mode: raw as LibraryMode }
    : { kind: "invalid" };
}

/**
 * The one place a mode is chosen from the stored state.
 *
 * `projectCount` is `null` when the count is unknown: `loadMode()` never has
 * one, and `lib/modeBootstrap.ts` leaves it null when the request failed or ran
 * out of budget. An unknown count NEVER guesses, because guessing Projects and
 * persisting it would overwrite the state the guess was based on.
 *
 * `invalid` and `unreadable` deliberately resolve to Files, never to the
 * absent-derived default. This is deliberately stricter than the theme's
 * absent/unrecognised collapse in `lib/settings.ts` (where an unrecognised
 * theme quietly becomes the default), because there a wrong guess only changes
 * a colour while here a derived mode must never be persisted over an unknown
 * stored value: doing so would turn one stale value into a permanent choice.
 */
export function decideMode(
  state: StoredModeState,
  projectCount: number | null,
): LibraryMode {
  switch (state.kind) {
    case "mode":
      // A stored choice always wins: no derivation, no network.
      return state.mode;
    case "invalid":
    case "unreadable":
      return DEFAULT_MODE;
    case "absent":
      return projectCount !== null && projectCount > 0 ? "projects" : DEFAULT_MODE;
  }
}

/** The persisted mode, or "files" when nothing valid is stored. */
export function loadMode(): LibraryMode {
  return decideMode(readStoredModeState(), null);
}

/** Persists the mode; storage failures are ignored (the mode still applies for this session). */
export function saveMode(mode: LibraryMode): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(MODE_STORAGE_KEY, mode);
  } catch {
    // ignore storage errors
  }
}
