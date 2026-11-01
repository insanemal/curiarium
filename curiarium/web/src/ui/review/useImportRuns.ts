import React from "react";

// Imported from the domain modules, not the `../../lib/api` barrel: `App`'s
// tests replace that barrel with a fixed object, so a new named import from it
// would throw "No export is defined on the mock". `useBundleDetect` sets the
// same precedent.
import { UnauthorizedError } from "../../lib/api/http";
import { getImportRun, getImportRuns, type ImportRunDetail } from "../../lib/api/settings";

type Props = {
  /** Whether the imports queue is open. Never gates a poll already in flight. */
  enabled: boolean;
  onUnauthorized?: () => void;
};

export type ImportRunsState = {
  run: ImportRunDetail | null;
  /** The first fetch of this activation is still pending. */
  loading: boolean;
  /** A refresh the operator asked for is in flight. Background polls do not
   *  set it: a run that polls every 1.5s must not flicker the Refresh button
   *  disabled for the whole time it walks the mount. */
  refreshing: boolean;
  /**
   * At least one successful fetch. `run === null` alone cannot tell "never
   * fetched / the first fetch failed" from "no run has ever happened", and the
   * sidebar badge needs that difference to decide between `–` and `0`.
   */
  loaded: boolean;
  error: string | null;
  lastRefreshedAt: number | null;
  refresh: () => Promise<void>;
};

const POLL_MS = 1500;

/**
 * The latest mounted-import run, with per-file errors. The list endpoint has no
 * detail payload, so a refresh asks for the newest page and then fetches that
 * run by id.
 *
 * The queue owns the hook, so it does nothing until the imports queue is open,
 * fetches once per activation and then polls only while the newest run is
 * actually walking the mount. Three rules keep that honest:
 *
 * - Two refs with two jobs. `walked` guards the per-activation entry fetch
 *   (React.StrictMode invokes the activation effect twice, and a re-entry must
 *   refetch); `armed` gates only background refresh. Disabling does not bump
 *   the request sequence, because a poll already in flight must be allowed to
 *   land after the operator navigates away - a deliberate, recorded trade.
 * - The newest request wins. Every refresh takes a sequence number and only the
 *   one still holding it may write state.
 * - `pollTick` keeps the poll chain alive. The timer effect depends on both the
 *   run's identity and that counter, so a failed poll - which changes no run -
 *   still re-arms the chain instead of dying with a badge stuck at 1.
 *
 * A failure becomes an error string; nothing here throws out of the hook.
 */
export function useImportRuns({ enabled, onUnauthorized }: Props): ImportRunsState {
  const [run, setRun] = React.useState<ImportRunDetail | null>(null);
  const [refreshing, setRefreshing] = React.useState(false);
  const [loaded, setLoaded] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [lastRefreshedAt, setLastRefreshedAt] = React.useState<number | null>(null);
  // Advanced after every settled non-auth refresh; see the doc comment.
  const [pollTick, setPollTick] = React.useState(0);
  // Newest request wins: a slow poll must never paint over a newer one.
  const seq = React.useRef(0);
  // One entry fetch per activation. See the hook's doc comment.
  const walked = React.useRef(false);
  // Gates background refresh only, so a poll survives leaving the queue.
  const armed = React.useRef(false);

  const load = React.useCallback(async (background: boolean) => {
    // Early-returning on `!enabled` here would also kill a poll the operator
    // navigated away from mid-run, which is accepted behaviour (ADR 0011).
    if (!armed.current) return;
    const id = ++seq.current;
    if (!background) setRefreshing(true);
    try {
      const list = await getImportRuns(1, 0);
      if (id !== seq.current) return;
      const latest = list.runs[0];
      if (!latest) {
        setRun(null);
        setLoaded(true);
        setError(null);
        setLastRefreshedAt(Date.now());
        setPollTick(tick => tick + 1);
        return;
      }
      const detail = await getImportRun(latest.id);
      if (id !== seq.current) return;
      setRun(detail);
      setLoaded(true);
      setError(null);
      setLastRefreshedAt(Date.now());
      setPollTick(tick => tick + 1);
    } catch (err) {
      if (id !== seq.current) return;
      if (err instanceof UnauthorizedError) {
        // A revoked session is not a poll failure: stop the chain and let the
        // shell sign the operator out.
        armed.current = false;
        onUnauthorized?.();
        return;
      }
      setError("Import run history is unavailable.");
      // The next poll must survive a failure that changed no run state.
      setPollTick(tick => tick + 1);
    } finally {
      if (id === seq.current && !background) setRefreshing(false);
    }
  }, [onUnauthorized]);

  /** The operator's Refresh (and Try again): shows its own in-flight state. */
  const refresh = React.useCallback(() => load(false), [load]);

  React.useEffect(() => {
    if (!enabled) {
      // Leaving the queue only resets the entry-fetch guard, so re-entry
      // refetches. `armed` deliberately stays true: the in-flight poll lands.
      walked.current = false;
      return;
    }
    // React.StrictMode invokes this effect twice for the same activation; a
    // second entry fetch would be a real double request. Guarding here rather
    // than from the cleanup keeps the first fetch alive.
    if (walked.current) return;
    armed.current = true;
    walked.current = true;
    void load(true);
  }, [enabled, load]);

  React.useEffect(() => {
    if (!armed.current) return;
    // Nothing to poll unless the newest run is still walking the mount.
    if (run?.status !== "running") return;
    const timer = window.setTimeout(() => void load(true), POLL_MS);
    return () => window.clearTimeout(timer);
  }, [run, pollTick, load]);

  return {
    run,
    // Before the first successful fetch there is nothing to show yet, so report
    // the pending state rather than flashing "no run has ever happened". A
    // failed first fetch clears it for the retryable error instead.
    loading: enabled && !loaded && error === null,
    refreshing,
    loaded,
    error,
    lastRefreshedAt,
    refresh,
  };
}
