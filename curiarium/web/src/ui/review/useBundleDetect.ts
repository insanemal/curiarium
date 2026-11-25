import React from "react";

// Imported from the domain modules, not the `../lib/api` barrel: `App`'s tests
// mock the barrel as a fixed object, so a new named import from it would break
// those mocks. `App.tsx` documents the same constraint for `../lib/api/auth`.
import {
  arrangeBundles,
  createBundles,
  enrichBundles,
  getArrangePlan,
  getBundleReport,
  setBundleDismissed,
  undoArrange as undoArrangeRequest,
  type BundleArrangeApplied,
  type BundleArrangePlan,
  type BundleArrangeUndo,
  type BundleCreateItem,
  type BundleCreateResponse,
  type BundleEnrichItemResult,
  type BundleReport,
} from "../../lib/api/bundles";
import { UnauthorizedError } from "../../lib/api/http";
import type { ArchiveExtractResult } from "../../lib/api/imports";
import { settleAfterExtract, type SettleOutcome } from "../../lib/scanSettle";
import { loadSettings } from "../../lib/settings";

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
  /** The server's verdict for one bulk create, or null when nothing answered.
   *
   *  It returns rather than being a void action because the caller is the only
   *  thing that knows what the click meant: `POST /bundles/create` answers one
   *  result per key, `project_id` is non-null on `skipped` as well as on
   *  `created`, and only the doors can decide which of them to report to the
   *  owner of the project list. A create that failed or armed nothing answers
   *  null - the error line already speaks it.
   */
  create: (items: BundleCreateItem[]) => Promise<BundleCreateResponse | null>;
  dismiss: (keys: string[], dismissed: boolean) => Promise<void>;
  enrich: (keys: string[], options?: { refresh?: boolean }) => Promise<void>;
  /** A line about the last enrichment that is news rather than a failure. */
  notice: string | null;
  afterExtract: (results: ArchiveExtractResult[]) => Promise<SettleOutcome>;
  /** The settle alone: waits for the library walk to register the extracted files
   *  and refetches nothing. The bulk door runs one per archive and re-walks once
   *  at the end, so per-archive use of `afterExtract` would walk the report N+1
   *  times. */
  waitAfterExtract: (results: ArchiveExtractResult[]) => Promise<SettleOutcome>;
};

/** The arrange doors: the preview, the apply and the undo.
 *
 *  They live on the queue's state for the same reason every other write does -
 *  the view never fetches, so the three calls need a home here. They are
 *  declared BESIDE `BundleDetectState` rather than inside it so a fixture that
 *  only reads a report (the badge's own suite) keeps compiling without them;
 *  `useBundleDetect` returns this intersection, and the review view asks for it.
 *
 *  Only `arrange` and `undoArrange` refetch: the preview is a READ that writes
 *  nothing, and the report a refetch would bring is the one already on screen.
 *  Each call answers the server's verdict, or null when nothing answered - the
 *  error line already speaks the failure, exactly as `create` does. */
export type BundleArrangeActions = {
  arrangePlan: (key: string, shape: string) => Promise<BundleArrangePlan | null>;
  arrange: (key: string, shape: string, planId: string) => Promise<BundleArrangeApplied | null>;
  undoArrange: (arrangeId: string) => Promise<BundleArrangeUndo | null>;
};

/** The queue's state as the review view consumes it: the detection report and
 *  every door that writes on it. */
export type BundleReviewState = BundleDetectState & BundleArrangeActions;

/** What an enrichment response means to the operator.
 *
 *  A refusal is the server's operator sentence, kept verbatim and deduped: two
 *  rows refused for the same reason must not print it twice. Every other
 *  outcome is a notice, because a click that did something must not look like
 *  a broken button. `fetched` is the silent success: the report itself changes,
 *  which is the visible answer.
 */
function enrichmentMessage(results: BundleEnrichItemResult[]): {
  error: string | null;
  notice: string | null;
} {
  const refused = results.filter(result => result.status === "refused");
  if (refused.length > 0) {
    const reasons = Array.from(
      new Set(
        refused
          .map(result => result.reason)
          .filter((reason): reason is string => Boolean(reason)),
      ),
    );
    return {
      error: reasons.length > 0 ? reasons.join(" - ") : "The source page refused to be read.",
      notice: null,
    };
  }
  if (results.some(result => result.status === "fetched")) return { error: null, notice: null };
  if (results.some(result => result.status === "cached")) {
    return { error: null, notice: "Already checked from the source page" };
  }
  if (results.some(result => result.status === "unavailable")) {
    return { error: null, notice: "This bundle names no vendor page to read" };
  }
  if (results.some(result => result.status === "unknown")) {
    return { error: null, notice: "That bundle is no longer in the report" };
  }
  return { error: null, notice: null };
}

/** The cookie the Settings card saved, read at the moment of the call.
 *
 * Call time, not render time: `App` holds settings as React state while
 * `saveSettings` and the storage cache are the origin, so a value captured at
 * mount would miss a paste made since - `lib/api/baseUrl.ts`'s
 * `apiBaseFromSettings` reads `loadSettings()` the same way for the same reason.
 * Reading it here also avoids threading a prop through `App.tsx`, which this
 * ticket deliberately does not touch.
 */
function savedThingiverseCookie(): string | undefined {
  return loadSettings().thingiverse.cookie || undefined;
}

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
 * `waitAfterExtract` exists because the view used to refresh *immediately* after
 * the extraction POST (`BundleReviewView.tsx:530`), which cannot work: on the
 * server `archive_service` ends with `scan_started = bool(seams.start_scan())`
 * and `library_scan.start_scan()` only starts a daemon thread and returns at
 * once, so a row turns `ready` only once the walk finds the twin folder with
 * registered models (`bundle_service.py:1079-1095`). Nothing in the tree ever
 * proved that fixed; waiting for the walk to settle is a new mechanism, not a
 * repair of one. It is a plain async callback, never an effect - an effect would
 * re-run under StrictMode and would need a dependency on the results array.
 *
 * It is the single waiter in the tree. `afterExtract` is that same wait followed
 * by one refetch, which is the right shape for the doors that write one archive
 * and then want the queue repainted. The bulk door writes N archives and re-walks
 * the whole library once at the end, so it takes the wait alone: N of
 * `afterExtract` would be N whole-library walks for a single click.
 */
export function useBundleDetect({ enabled, onUnauthorized }: Props): BundleReviewState {
  const [report, setReport] = React.useState<BundleReport | null>(null);
  const [fetching, setFetching] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);
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
      // A create or a dismissal is a new decision, so the last enrichment's
      // notice no longer describes what the operator just did. `refresh` never
      // clears it: a refetch is not a new answer.
      setNotice(null);
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

  // Deliberately not routed through `mutate`, for the same reason `enrich` is
  // not: `mutate` discards the response, and here the response is the only
  // record of which keys became projects. Everything else about `mutate`'s
  // contract is kept, in the same order - `busy`, the notice reset (a create is
  // a new decision, so the last enrichment's sentence no longer describes what
  // the operator did), `error` cleared only after the request answered, the
  // refetch inside the try, and `busy` released in `finally`.
  const create = React.useCallback(
    async (items: BundleCreateItem[]) => {
      if (!enabled) return null;
      setBusy(true);
      setNotice(null);
      try {
        const response = await createBundles(items);
        setError(null);
        await refresh();
        return response;
      } catch (err) {
        if (err instanceof UnauthorizedError) onUnauthorized?.();
        setError(
          err instanceof Error && err.message
            ? err.message
            : "Failed to create the selected bundles.",
        );
        return null;
      } finally {
        setBusy(false);
      }
    },
    [enabled, onUnauthorized, refresh],
  );

  // Modelled on `enrich`, not on `mutate`: the one-step extract-and-create door
  // needs the server's verdict, and `mutate` discards the response. It still
  // clears `notice`, because a create is a new decision and the last
  // enrichment's sentence no longer describes what the operator just did.
  const dismiss = React.useCallback(
    async (keys: string[], dismissed: boolean) => {
      await mutate(
        () => setBundleDismissed(keys, dismissed),
        dismissed ? "Failed to dismiss the selected bundles." : "Failed to restore the bundle.",
      );
    },
    [mutate],
  );

  // Deliberately not routed through `mutate`: that helper discards the
  // response, and the response is the whole answer here - a refusal carries the
  // operator's reason, and `cached`/`unavailable`/`unknown` each need their own
  // words. The ordering matters as much: `refresh` writes `error` itself, so
  // the message is computed and set AFTER the refetch it waited for, or the
  // refetch would clear it. `notice` survives `refresh` for the same reason the
  // toggle state does: only a new answer replaces it.
  const enrich = React.useCallback(
    async (keys: string[], options: { refresh?: boolean } = {}) => {
      if (!enabled) return;
      setBusy(true);
      try {
        const response = await enrichBundles(keys, {
          refresh: options.refresh,
          thingiverseCookie: savedThingiverseCookie(),
        });
        await refresh();
        const message = enrichmentMessage(response.results);
        setError(message.error);
        setNotice(message.notice);
      } catch (err) {
        if (err instanceof UnauthorizedError) onUnauthorized?.();
        setError(
          err instanceof Error && err.message ? err.message : "Failed to read the source page.",
        );
        setNotice(null);
      } finally {
        setBusy(false);
      }
    },
    [enabled, onUnauthorized, refresh],
  );

  // Modelled on `create`, not on `mutate`: the response IS the answer - the
  // moves, the refusals and the plan id the apply has to echo - and `mutate`
  // discards it. A preview writes nothing, so it refetches nothing either: the
  // report it would fetch is the one already on screen.
  const arrangePlan = React.useCallback(
    async (key: string, shape: string) => {
      if (!enabled) return null;
      setBusy(true);
      setNotice(null);
      try {
        const plan = await getArrangePlan(key, shape);
        setError(null);
        return plan;
      } catch (err) {
        if (err instanceof UnauthorizedError) onUnauthorized?.();
        setError(
          err instanceof Error && err.message
            ? err.message
            : "Failed to preview the arrangement.",
        );
        return null;
      } finally {
        setBusy(false);
      }
    },
    [enabled, onUnauthorized],
  );

  // The apply is a write, so it keeps `create`'s whole order: `busy`, the notice
  // reset (an arrangement is a new decision, so the last enrichment's sentence
  // no longer describes what the operator just did), `error` cleared only after
  // the request answered, the refetch inside the try, `busy` released in
  // `finally`. The response is returned because the pane must name the moves
  // that landed, offer the undo by id, and say what the server could not see.
  const arrange = React.useCallback(
    async (key: string, shape: string, planId: string) => {
      if (!enabled) return null;
      setBusy(true);
      setNotice(null);
      try {
        const applied = await arrangeBundles({ key, shape, planId });
        setError(null);
        await refresh();
        return applied;
      } catch (err) {
        if (err instanceof UnauthorizedError) onUnauthorized?.();
        setError(
          err instanceof Error && err.message ? err.message : "Failed to arrange the bundle.",
        );
        return null;
      } finally {
        setBusy(false);
      }
    },
    [enabled, onUnauthorized, refresh],
  );

  // The undo is the same write shape, and it returns too: `project_removed` and
  // the second self-report are the only record of what the fold-back left.
  const undoArrange = React.useCallback(
    async (arrangeId: string) => {
      if (!enabled) return null;
      setBusy(true);
      setNotice(null);
      try {
        const undone = await undoArrangeRequest(arrangeId);
        setError(null);
        await refresh();
        return undone;
      } catch (err) {
        if (err instanceof UnauthorizedError) onUnauthorized?.();
        setError(
          err instanceof Error && err.message ? err.message : "Failed to undo the arrangement.",
        );
        return null;
      } finally {
        setBusy(false);
      }
    },
    [enabled, onUnauthorized, refresh],
  );

  // The settle alone. See the hook's doc comment: this is the one waiter, and
  // `afterExtract` below is it plus a refetch.
  const waitAfterExtract = React.useCallback(
    async (results: ArchiveExtractResult[]): Promise<SettleOutcome> => {
      if (!enabledRef.current) return "cancelled";
      return settleAfterExtract(results, {
        cancelled: () => !enabledRef.current,
        onUnauthorized,
      });
    },
    [onUnauthorized],
  );

  const afterExtract = React.useCallback(
    async (results: ArchiveExtractResult[]): Promise<SettleOutcome> => {
      const outcome = await waitAfterExtract(results);
      // Leaving the queue cancels: nothing is mounted to paint the new report.
      if (!enabledRef.current) return "cancelled";
      // Through `refresh` rather than `getBundleReport` directly so its `seq`
      // newest-wins guard covers an operator clicking Refresh mid-settle.
      await refresh();
      return outcome;
    },
    [waitAfterExtract, refresh],
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
    enrich,
    notice,
    arrangePlan,
    arrange,
    undoArrange,
    afterExtract,
    waitAfterExtract,
  };
}
