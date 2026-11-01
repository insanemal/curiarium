import React from "react";

// Imported from the domain modules, not the `../../lib/api` barrel: `App`'s
// tests replace that barrel with a fixed object, so a new named import from it
// would throw "No export is defined on the mock". `useImportRuns` and
// `useBundleDetect` set the same precedent.
import { UnauthorizedError } from "../../lib/api/http";
import { getTrash, purgeTrash, type TrashList, type TrashPurgeResult } from "../../lib/api/trash";

type Props = {
  /** Whether the trash queue is open. Nothing is fetched while it is not. */
  enabled: boolean;
  onUnauthorized?: () => void;
};

export type TrashState = {
  trash: TrashList | null;
  /** The first read of this activation is still pending. */
  loading: boolean;
  /** An operator-requested refresh is in flight. */
  refreshing: boolean;
  /** A failed READ. Kept apart from `purgeError` on purpose. */
  error: string | null;
  /** A failed PURGE request. Kept apart from `error` on purpose. */
  purgeError: string | null;
  purgeResult: TrashPurgeResult | null;
  purging: boolean;
  lastRefreshedAt: number | null;
  refresh: () => Promise<void>;
  /** Resolves true when the purge request succeeded; the caller closes its dialog on true. */
  purge: () => Promise<boolean>;
};

/** `assertOk`'s own fallback for a read; the card's wording, kept verbatim. */
const READ_FALLBACK = "Unable to load the trash.";
/** The card's purge fallback, kept verbatim. */
const PURGE_FALLBACK = "Failed to empty the trash.";

function errorText(err: unknown, fallback: string): string {
  if (err instanceof Error && err.message.trim()) return err.message;
  return fallback;
}

/**
 * The trash queue's one state owner: the listing, the purge and both failures.
 *
 * Two failure channels, not one. `error` is a failed READ and `purgeError` is a
 * failed PURGE, because a single channel is exactly what let the old card
 * render a failed read as if the purge had failed, and - the worse half - let a
 * failed purge leave an empty list that then read as "The trash is empty". The
 * rows, the result box and the notice each consult the channel that owns them.
 *
 * Nothing polls, and nothing is armed on app load. `GET /trash` sizes every
 * entry with a recursive `os.walk`, so a timer here would re-walk the storage
 * tree forever in order to repaint a number an operator reads once; the queue
 * is armed by `enabled` (the trash queue being open), reads once per
 * activation, and refetches only when the operator asks or after a purge.
 *
 * The failures worth documenting:
 * - `assertOk` maps every 403 to `FORBIDDEN_MESSAGE`, so `POST /trash/purge`'s
 *   MFA gate (`enforce_admin_mfa`) surfaces as "Your role does not allow this
 *   action." exactly as it did in the card. This queue deliberately does NOT
 *   attempt step-up: the dialog states the reason and leaves the operator to
 *   re-authenticate.
 * - A failed refresh keeps `trash` and sets `error`, so the operator keeps the
 *   rows and the header's "Last refreshed" age instead of losing a readable
 *   list to a transient failure.
 * - A failed purge leaves `trash` alone for the same reason: the rows are still
 *   what the last successful read found.
 *
 * No `loaded` flag: a successful read always yields a `TrashList`, so
 * `trash === null` already means "no successful read yet".
 */
export function useTrash({ enabled, onUnauthorized }: Props): TrashState {
  const [trash, setTrash] = React.useState<TrashList | null>(null);
  const [refreshing, setRefreshing] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [purgeError, setPurgeError] = React.useState<string | null>(null);
  const [purgeResult, setPurgeResult] = React.useState<TrashPurgeResult | null>(null);
  const [purging, setPurging] = React.useState(false);
  const [lastRefreshedAt, setLastRefreshedAt] = React.useState<number | null>(null);
  // Newest request wins: a slow pre-purge read must never paint its stale copy
  // over the purge's refetch. A read still in flight when the queue closes is
  // allowed to land - it is the read the shell would have issued anyway, and
  // newest-wins discards it as soon as a newer one starts on re-entry.
  const seq = React.useRef(0);
  // One entry read per activation. React.StrictMode invokes the activation
  // effect twice in development; a second read would be a real double walk.
  const walked = React.useRef(false);

  /**
   * `background` only means "not the operator's Refresh": the entry read passes
   * true so the header's button does not flicker busy before the first paint.
   */
  const load = React.useCallback(
    async (background: boolean) => {
      // The queue owns this hook, so a closed queue reads nothing. There is no
      // background chain to keep alive here, so gating the whole call is safe.
      if (!enabled) return;
      const id = ++seq.current;
      if (!background) setRefreshing(true);
      try {
        const list = await getTrash();
        if (id !== seq.current) return;
        setTrash(list);
        setError(null);
        setLastRefreshedAt(Date.now());
      } catch (err) {
        if (id !== seq.current) return;
        if (err instanceof UnauthorizedError) {
          // A revoked session is not a read failure: let the shell sign out.
          onUnauthorized?.();
          return;
        }
        // The rows stay; the notice says the list is the last one we could read.
        setError(errorText(err, READ_FALLBACK));
      } finally {
        if (id === seq.current && !background) setRefreshing(false);
      }
    },
    [enabled, onUnauthorized],
  );

  /** The operator's Refresh (and Try again): shows its own in-flight state. */
  const refresh = React.useCallback(() => load(false), [load]);

  const purge = React.useCallback(async (): Promise<boolean> => {
    setPurging(true);
    setPurgeError(null);
    setPurgeResult(null);
    // Invalidate every read in flight before the purge: a read started before
    // it would list entries the purge is about to remove.
    seq.current += 1;
    try {
      const result = await purgeTrash();
      setPurgeResult(result);
      // Exactly one refetch, through the same newest-wins read path.
      await load(false);
      return true;
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized?.();
        return false;
      }
      // `trash` is deliberately untouched: the rows are still what the last
      // successful read found, and an empty list must not read as "empty trash".
      setPurgeError(errorText(err, PURGE_FALLBACK));
      return false;
    } finally {
      setPurging(false);
    }
  }, [load, onUnauthorized]);

  React.useEffect(() => {
    if (!enabled) {
      // Leaving the queue resets only the entry guard, so re-entry refetches.
      walked.current = false;
      return;
    }
    if (walked.current) return;
    walked.current = true;
    void load(true);
  }, [enabled, load]);

  return {
    trash,
    // Before a successful read there is nothing to show, so report the pending
    // state rather than flashing the empty sentence. A failed first read clears
    // it for the retryable error instead. A 401 leaves it true - no error text
    // is set on purpose and the shell is signing the operator out.
    loading: enabled && trash === null && error === null,
    refreshing,
    error,
    purgeError,
    purgeResult,
    purging,
    lastRefreshedAt,
    refresh,
    purge,
  };
}
