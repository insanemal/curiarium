import React from "react";

// Imported from the domain modules, not the `../lib/api` barrel: `App`'s tests
// mock the barrel as a fixed object, so a new named import from it would break
// those mocks. `App.tsx` documents the same constraint for `../lib/api/auth`.
import {
  createBundles,
  getBundleReport,
  setBundleDismissed,
  type BundleCreateItem,
  type BundleReport,
} from "../../lib/api/bundles";
import { UnauthorizedError } from "../../lib/api/http";

type Props = {
  /** Whether the review queue is open (and the role may write). */
  enabled: boolean;
  onUnauthorized?: () => void;
};

export type BundleDetectState = {
  report: BundleReport | null;
  loading: boolean;
  busy: boolean;
  error: string | null;
  lastRefreshedAt: number | null;
  showDismissed: boolean;
  setShowDismissed: (value: boolean) => void;
  refresh: () => Promise<void>;
  create: (items: BundleCreateItem[]) => Promise<void>;
  dismiss: (keys: string[], dismissed: boolean) => Promise<void>;
};

/**
 * Vendor bundle detection for the review queue. The queue owns the hook now: it
 * is inert until the review view is open, fetches exactly once per activation
 * and never polls - the walk is expensive, so polling it would put a full
 * server-side library walk on a timer. Two rules keep that honest:
 *
 * - One walk per activation. React.StrictMode invokes the activation effect
 *   twice, so a `walked` guard replaces the effect's cleanup as the thing that
 *   remembers "this activation already fetched"; invalidating the request from
 *   the cleanup would throw away the only report the activation ever asks for.
 * - The newest request wins. Every request takes a sequence number and only the
 *   one still holding it may write state, so a slow pre-mutation walk can never
 *   paint over the refetch a mutation started, and leaving the queue drops
 *   whatever is still in flight.
 *
 * A failure becomes an error string; nothing here throws out of the hook.
 */
export function useBundleDetect({ enabled, onUnauthorized }: Props): BundleDetectState {
  const [report, setReport] = React.useState<BundleReport | null>(null);
  const [fetching, setFetching] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [lastRefreshedAt, setLastRefreshedAt] = React.useState<number | null>(null);
  const [showDismissed, setShowDismissed] = React.useState(false);
  // Newest request wins: a walk that started before a mutation's refetch must
  // never overwrite the newer report with its pre-mutation copy.
  const seq = React.useRef(0);
  // One whole-library walk per activation. See the hook's doc comment.
  const walked = React.useRef(false);

  const refresh = React.useCallback(async () => {
    if (!enabled) return;
    const id = ++seq.current;
    setFetching(true);
    try {
      // The badge must classify every row, and a withheld row would make the
      // count unknowable, so ask for the full walk. 1000 is the API's maximum
      // limit; dismissed rows stay in the payload so the queue can reveal them
      // without a second round trip and does the filtering itself.
      const next = await getBundleReport({ includeDismissed: true, limit: 1000 });
      if (id !== seq.current) return;
      setReport(next);
      setError(null);
      setLastRefreshedAt(Date.now());
    } catch (err) {
      if (id !== seq.current) return;
      if (err instanceof UnauthorizedError) onUnauthorized?.();
      setError("Bundle detection is unavailable.");
    } finally {
      if (id === seq.current) setFetching(false);
    }
  }, [enabled, onUnauthorized]);

  React.useEffect(() => {
    if (!enabled) {
      // Leaving the queue drops anything still in flight for this activation.
      walked.current = false;
      seq.current += 1;
      return;
    }
    // React.StrictMode invokes this effect twice for the same activation; a
    // second whole-library walk would be a real double fetch of the same
    // report. Guarding here rather than from the cleanup keeps the first walk
    // alive: a cleanup-driven sequence bump would discard its only result.
    if (walked.current) return;
    walked.current = true;
    void refresh();
  }, [enabled, refresh]);

  const mutate = React.useCallback(
    async (call: () => Promise<unknown>, fallback: string) => {
      if (!enabled) return;
      setBusy(true);
      try {
        await call();
        setError(null);
        await refresh();
      } catch (err) {
        if (err instanceof UnauthorizedError) onUnauthorized?.();
        setError(err instanceof Error && err.message ? err.message : fallback);
      } finally {
        setBusy(false);
      }
    },
    [enabled, onUnauthorized, refresh],
  );

  const create = React.useCallback(
    async (items: BundleCreateItem[]) => {
      await mutate(() => createBundles(items), "Failed to create the selected bundles.");
    },
    [mutate],
  );

  const dismiss = React.useCallback(
    async (keys: string[], dismissed: boolean) => {
      await mutate(
        () => setBundleDismissed(keys, dismissed),
        dismissed ? "Failed to dismiss the selected bundles." : "Failed to restore the bundle.",
      );
    },
    [mutate],
  );

  return {
    report,
    // Before the first report arrives there is nothing to show yet, so report
    // the pending state rather than flashing the empty one.
    loading: fetching || (enabled && report === null && error === null),
    busy,
    error,
    lastRefreshedAt,
    showDismissed,
    setShowDismissed,
    refresh,
    create,
    dismiss,
  };
}
