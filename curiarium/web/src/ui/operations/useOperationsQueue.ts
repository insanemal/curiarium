import React from "react";

// Read from the domain module, never the `../../lib/api` barrel: several suites
// replace that barrel with a fixed object, so a named import from it would come
// back `undefined` there (`useBundleDetect.ts` documents the same rule).
import { listOperations, type Operation } from "../../lib/api/operations";
import { UnauthorizedError } from "../../lib/api/http";
import { isActiveStatus, queueRows, type QueueRow } from "./operationView";

/**
 * The one shared operations queue: filters, pages and a poll that runs only
 * while something on the page is still moving.
 *
 * The discipline is `ui/review/useImportRuns`' verbatim, so the two queues
 * cannot drift:
 *
 * - Fetch-on-open. `enabled` false issues no request; enabling issues exactly
 *   one read of page 0 with the current filters. A filter change is a new page
 *   0 and REPLACES the rows; `loadMore` reads the next offset and APPENDS. The
 *   route drops its `total`, so `hasMore` is only "the last page came back
 *   full" - the one honest answer available.
 * - The newest response wins. Every read takes a sequence number and only the
 *   one still holding it may paint, so a slow first page cannot land on top of
 *   a filter change.
 * - `armed` gates the requests and is deliberately NOT cleared on disable: a
 *   poll already in flight may land after the operator leaves (ADR 0011).
 * - `pollTick` keeps the chain alive. The timer effect depends on the active
 *   flag and that counter, so a failed poll - which changes no row - re-arms
 *   instead of dying with the queue frozen mid-flight.
 *
 * 401 is the host's business: it calls `onUnauthorized` and writes no error
 * sentence. Any other failure becomes `Error.message` (the API layer already
 * carries the server's own `detail`) with a fallback sentence.
 */

export type OperationsFilters = {
  /** "" means every status. Otherwise one of the server's six. */
  state: string;
  /** "" means every workflow. Otherwise one of the nine names. */
  workflow: string;
  /** "" means everyone. A username narrows to that submitter. */
  submitted_by: string;
};

export type OperationsQueueState = {
  /** The rows, in the server's order (newest first). */
  rows: Operation[];
  /** The row view models, in the server's order. */
  view: QueueRow[];
  /** The first page of this activation is in flight. */
  loading: boolean;
  /** At least one successful read, so the badge can tell `–` from `0`. */
  loaded: boolean;
  error: string | null;
  filters: OperationsFilters;
  setFilters: (next: Partial<OperationsFilters>) => void;
  /** Page 0 of the current filters, with its own in-flight state. */
  refresh: () => Promise<void>;
  /** The next page, appended. A no-op once the last page came back short. */
  loadMore: () => Promise<void>;
  /** The last page came back full - the route drops its `total`, so this is the
   *  only honest end-of-list signal there is. */
  hasMore: boolean;
  /** Null until a read succeeded; active rows on the loaded pages. */
  activeCount: number | null;
};

type Props = {
  /** Whether the queue is open. Never gates a poll already in flight. */
  enabled: boolean;
  onUnauthorized?: () => void;
};

/** One page's worth of rows. `PAGE_SIZE` is the server's default too. */
export const PAGE_SIZE = 50;

const POLL_MS = 1500;
const EMPTY_FILTERS: OperationsFilters = { state: "", workflow: "", submitted_by: "" };
const ERROR_FALLBACK = "The operations queue is unavailable right now.";

/** Which read this is: the page 0 of an activation or a filter change, the next
 *  appended page, or a background re-read of the window already shown. */
type LoadMode = "first" | "more" | "poll";

function filterKey(filters: OperationsFilters): string {
  return `${filters.state}\u0000${filters.workflow}\u0000${filters.submitted_by}`;
}

function failureDetail(err: unknown): string {
  return err instanceof Error && err.message ? err.message : ERROR_FALLBACK;
}

export function useOperationsQueue({ enabled, onUnauthorized }: Props): OperationsQueueState {
  const [rows, setRows] = React.useState<Operation[]>([]);
  const [loading, setLoading] = React.useState(false);
  const [loaded, setLoaded] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [filters, setFiltersState] = React.useState<OperationsFilters>(EMPTY_FILTERS);
  const [hasMore, setHasMore] = React.useState(false);
  // Advanced after every settled non-auth read; see the hook's doc comment.
  const [pollTick, setPollTick] = React.useState(0);
  // Newest request wins: a slow read must never paint over a newer one.
  const seq = React.useRef(0);
  // One entry read per activation. See the hook's doc comment.
  const walked = React.useRef(false);
  // Gates every request; a poll survives leaving the queue.
  const armed = React.useRef(false);
  // The filters the last read was issued for, so a filter CHANGE (and not a
  // re-render with an equal literal) is what triggers the next page 0.
  const readKey = React.useRef<string | null>(null);

  // The request reads the CURRENT filters and rows through refs, so the callbacks
  // stay stable: a new filter object must not be a new `load` identity, and a
  // Load more must page from the length that is on screen now.
  const filtersRef = React.useRef(filters);
  filtersRef.current = filters;
  const rowsRef = React.useRef<Operation[]>(rows);
  rowsRef.current = rows;
  const hasMoreRef = React.useRef(hasMore);
  hasMoreRef.current = hasMore;

  const load = React.useCallback(
    async (mode: LoadMode): Promise<void> => {
      // Early-returning on `!enabled` here would also kill a poll the operator
      // navigated away from mid-flight, which is accepted behaviour.
      if (!armed.current) return;
      const id = ++seq.current;
      const shown = rowsRef.current;
      const offset = mode === "more" ? shown.length : 0;
      // A poll re-reads the window already on screen: page 0 alone would drop a
      // Load more from under the operator on the next tick.
      const limit = mode === "poll" ? Math.max(PAGE_SIZE, shown.length) : PAGE_SIZE;
      if (mode === "first") setLoading(true);
      try {
        const page = await listOperations({ ...filtersRef.current, limit, offset });
        if (id !== seq.current) return;
        setRows(previous => (mode === "more" ? [...previous, ...page] : page));
        setHasMore(page.length === limit);
        setLoaded(true);
        setError(null);
        setPollTick(tick => tick + 1);
      } catch (err) {
        if (id !== seq.current) return;
        if (err instanceof UnauthorizedError) {
          // A revoked session is not a queue failure: stop the chain and let the
          // shell sign the operator out.
          armed.current = false;
          onUnauthorized?.();
          return;
        }
        setError(failureDetail(err));
        // The next poll must survive a failure that changed no row.
        setPollTick(tick => tick + 1);
      } finally {
        if (id === seq.current && mode === "first") setLoading(false);
      }
    },
    [onUnauthorized],
  );

  /** The operator's Refresh: page 0 of the current filters, with its loading state. */
  const refresh = React.useCallback(() => load("first"), [load]);

  /** The next appended page, and a no-op when the last page was short. */
  const loadMore = React.useCallback((): Promise<void> => {
    if (!hasMoreRef.current) return Promise.resolve();
    return load("more");
  }, [load]);

  const setFilters = React.useCallback((next: Partial<OperationsFilters>) => {
    setFiltersState(previous => ({ ...previous, ...next }));
  }, []);

  const filtersKey = filterKey(filters);

  React.useEffect(() => {
    if (!enabled) {
      // Leaving the queue only resets the entry-read guard, so re-entry
      // refetches. `armed` deliberately stays true: the in-flight poll lands.
      walked.current = false;
      return;
    }
    // React.StrictMode invokes this effect twice for the same activation; a
    // second page 0 would be a real double request. Guarding here rather than
    // from the cleanup keeps the first read alive.
    if (walked.current && readKey.current === filtersKey) return;
    armed.current = true;
    walked.current = true;
    readKey.current = filtersKey;
    void load("first");
  }, [enabled, filtersKey, load]);

  // Some row on the page is still moving, so the page is worth re-reading. The
  // counter keeps the chain alive across a failed poll that changed no row.
  const activePresent = rows.some(row => isActiveStatus(row.status));
  React.useEffect(() => {
    if (!armed.current) return;
    if (!activePresent) return;
    const timer = window.setTimeout(() => void load("poll"), POLL_MS);
    return () => window.clearTimeout(timer);
  }, [activePresent, pollTick, load]);

  const view = React.useMemo(() => queueRows(rows), [rows]);

  return {
    rows,
    view,
    loading,
    loaded,
    error,
    filters,
    setFilters,
    refresh,
    loadMore,
    hasMore,
    // `null` until a read succeeded: the badge's `–` is not `0`.
    activeCount: loaded ? rows.filter(row => isActiveStatus(row.status)).length : null,
  };
}
