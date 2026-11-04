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
import type { ArchiveExtractResult } from "../../lib/api/imports";
import { settleAfterExtract, type SettleOutcome } from "../../lib/scanSettle";

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
  showCreated: boolean;
  setShowCreated: (value: boolean) => void;
  refresh: () => Promise<void>;
  create: (items: BundleCreateItem[]) => Promise<void>;
  dismiss: (keys: string[], dismissed: boolean) => Promise<void>;
  afterExtract: (results: ArchiveExtractResult[]) => Promise<SettleOutcome>;
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
 *
 * The two reveal toggles (`showDismissed`, `showCreated`) are session state: they
 * default off, survive leaving and re-entering the queue because the hook stays
 * mounted for the App's lifetime, and reset on reload. Nothing is written to
 * localStorage on purpose - a persisted Show created would put the dead rows
 * back in the queue for whoever ever flipped it, which is the bug this queue
 * exists to avoid. Created rows are permanently done, so hiding them is the
 * default the ticket settles on.
 *
 * `afterExtract` exists because the view used to refresh *immediately* after the
 * extraction POST (`BundleReviewView.tsx:530`), which cannot work: on the server
 * `archive_service` ends with `scan_started = bool(seams.start_scan())` and
 * `library_scan.start_scan()` only starts a daemon thread and returns at once, so
 * a row turns `ready` only once the walk finds the twin folder with registered
 * models (`bundle_service.py:1079-1095`). Nothing in the tree ever proved that
 * fixed; waiting for the walk to settle is a new mechanism, not a repair of one.
 * It is a plain async callback, never an effect - an effect would re-run under
 * StrictMode and would need a dependency on the results array.
 */
export function useBundleDetect({ enabled, onUnauthorized }: Props): BundleDetectState {
  const [report, setReport] = React.useState<BundleReport | null>(null);
  const [fetching, setFetching] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [lastRefreshedAt, setLastRefreshedAt] = React.useState<number | null>(null);
  const [showDismissed, setShowDismissed] = React.useState(false);
  const [showCreated, setShowCreated] = React.useState(false);
  // Mirrored every render (not only when `enabled` changes) so the settle loop
  // below reads the queue's live open/closed state rather than the value it
  // closed over when the extraction began.
  const enabledRef = React.useRef(enabled);
  enabledRef.current = enabled;
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

  const afterExtract = React.useCallback(
    async (results: ArchiveExtractResult[]): Promise<SettleOutcome> => {
      if (!enabledRef.current) return "cancelled";
      const outcome = await settleAfterExtract(results, {
        cancelled: () => !enabledRef.current,
        onUnauthorized,
      });
      // Leaving the queue cancels: nothing is mounted to paint the new report.
      if (!enabledRef.current) return "cancelled";
      // Through `refresh` rather than `getBundleReport` directly so its `seq`
      // newest-wins guard covers an operator clicking Refresh mid-settle.
      await refresh();
      return outcome;
    },
    [onUnauthorized, refresh],
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
    showCreated,
    setShowCreated,
    refresh,
    create,
    dismiss,
    afterExtract,
  };
}
