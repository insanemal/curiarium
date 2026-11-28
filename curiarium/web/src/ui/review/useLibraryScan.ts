import React from "react";

// Imported from the domain modules, not the `../../lib/api` barrel: `App`'s
// tests replace that barrel with a fixed object, so a new named import from it
// would throw "No export is defined on the mock". `useImportRuns` sets the same
// precedent.
import { UnauthorizedError } from "../../lib/api/http";
import { getLibraryScanStatus, startLibraryScan, type LibraryScanStatus } from "../../lib/api/settings";
import { RESCAN_TIMEOUT_MS, type MissingScanOutcome } from "../asset-grid/assetGridMeta";

type Props = {
  /**
   * The missing queue is open, OR the shell has already armed a scan from a
   * surface outside the queue (the library header, the queue's own panel or
   * empty state through `scanFromShell`, a detection row). Nothing is fetched
   * on app load and nothing polls while idle.
   */
  enabled: boolean;
  onUnauthorized?: () => void;
  /**
   * A scan registers files and creates folders from the directory tree, so both
   * the grid and the sidebar tree must reload when it finishes. Without these two
   * signals the tree and grid stayed stale after a rescan (found in live testing,
   * 2026-10-31).
   */
  onAssetsChanged?: () => void;
  onFoldersChanged?: () => void;
};

export type LibraryScanState = {
  scan: LibraryScanStatus | null;
  busy: boolean;
  error: string | null;
  /** At least one successful status read this session; the entry read is advisory
   *  and a failure is swallowed, so this is the only way to say "we never knew". */
  loaded: boolean;
  run: () => Promise<void>;
  /**
   * Accepts a status for a scan somebody else started and polled - the per-card
   * Re-scan - so the panel and `scanOutcome` describe the scan that produced the
   * rows on screen instead of the previous one. It does not announce: the card
   * path refetches the grid and the counts itself.
   */
  adopt: (status: LibraryScanStatus) => void;
};

const POLL_MS = 1500;

/**
 * A pure read of what the last scan said, so the empty state cannot guess:
 * `as of the last scan the flag pass completed with no failures` (clean),
 * `it ran but could not check everything` (failed), and `we do not know`
 * (unknown - no completed scan this session, one still running, or the
 * advisory status read failed). Only `clean` may claim a clean disk.
 */
export function scanOutcome(scan: LibraryScanStatus | null): MissingScanOutcome {
  if (!scan || scan.running || scan.finished_at == null) return "unknown";
  return scan.failed > 0 ? "failed" : "clean";
}

/**
 * The storage scan as the Missing files queue needs it: what the last scan said,
 * whether one is walking right now, and the rescan action.
 *
 * The SHELL owns the single instance now: one poller, one announcement, one
 * `adopt` target shared by four contribute-gated mounts (the library header, the
 * missing queue's panel, its empty state and a detection row). The entry read is
 * still once per activation and the poll loop is still `enabled`-gated: that
 * entry read is the point of this hook, because the old settings-owned version
 * never fetched an initial status, so the missing-flag set was `unknown` until
 * somebody pressed Rescan.
 *
 * The armed flag is now set by the missing queue's panel and empty-state action
 * too, through App's `scanFromShell`, not only by the library header. That is
 * what keeps the poll alive when the operator leaves for the library while the
 * scan walks: a rescan started from the queue used to stop being observed the
 * moment the queue unmounted, and the tree stayed stale after it finished
 * (owner, 2026-11-25).
 *
 * Named consequence: the shell's arm flag is sticky, so once a scan has been
 * started from any of those surfaces the hook stays enabled and a later
 * re-entry into the missing queue does not re-read the status. It keeps the
 * session's last known status, which is fresher-or-equal anyway. Before that
 * first shell use, the queue's re-entry re-read is unchanged.
 *
 * The off-view poll is bounded on the STALL, not on the duration: a healthy scan
 * is followed for as long as it walks (a 15-minute rsync of a big storage folder
 * must keep its finish signal, and the loop still ends on `!scan.running`), while
 * a status read that has stopped succeeding stops the loop after
 * `RESCAN_TIMEOUT_MS`. The arbitration is that an advisory read failure while
 * off-view must be bounded - no unbounded background poll against a dead API -
 * without capping a scan that is simply slow.
 *
 * Accepted boundary: a RELOAD mid-scan still shows a stale tree on purpose.
 * Nothing reads the status on app load - that is the hook's whole point - so a
 * reload cannot know a scan was walking, and `seeded` re-baselines on the first
 * read afterwards. Do not paper over it with a load-time status read.
 *
 * The announcement itself is gated, not unconditional: `registered > 0` can
 * suppress a healing-only run, because the scan's asset-evidence backfill can
 * relink a migrated folder's `storage_path` with nothing registered. Names are
 * unaffected, so the visible tree is right - and nobody may "fix" this with an
 * unconditional announce, which would only re-announce noise.
 *
 * A scan the per-card Re-scan starts is invisible to this hook, so the grid
 * reports its statuses through `adopt`: the panel and the outcome follow that
 * scan, but the hook neither polls it a second time nor announces a completion
 * the card path already handled.
 */
export function useLibraryScan({
  enabled,
  onUnauthorized,
  onAssetsChanged,
  onFoldersChanged,
}: Props): LibraryScanState {
  const [scan, setScan] = React.useState<LibraryScanStatus | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [loaded, setLoaded] = React.useState(false);
  // Keeps the poll chain alive when a read fails and so changes no scan.
  const [pollTick, setPollTick] = React.useState(0);
  // One entry read per activation. See the hook's doc comment.
  const walked = React.useRef(false);
  // A scan the card's Re-Scan owns: this hook arms no timer for it and does not
  // announce its completion twice, but the panel and `scanOutcome` follow it.
  const adopted = React.useRef(false);
  // The baseline for the announcement: seeded on the first successful read,
  // kept across activations so a finished scan announces exactly once.
  const seeded = React.useRef(false);
  const announced = React.useRef<number | null>(null);
  // When the last status read SUCCEEDED, so an advisory-failure loop against a
  // dead API cannot outlive `RESCAN_TIMEOUT_MS` while the operator is on another
  // view. A failure and a 401 leave it alone: the bound is on the stall.
  const lastReadAt = React.useRef<number | null>(null);

  const readStatus = React.useCallback(async () => {
    try {
      const next = await getLibraryScanStatus();
      setScan(next);
      setLoaded(true);
      lastReadAt.current = Date.now();
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        // A revoked session is not an advisory failure: stop and let the shell
        // sign the operator out.
        onUnauthorized?.();
        return;
      }
      // The status read is advisory, so this failure is swallowed on purpose.
      // That is exactly why `scanOutcome` must answer "unknown" rather than
      // assume a clean disk: a swallowed read is a read that never happened.
      setPollTick(tick => tick + 1);
    }
  }, [onUnauthorized]);

  React.useEffect(() => {
    if (!enabled) {
      // Leaving the queue only resets the entry guard, so a re-entry refetches.
      walked.current = false;
      // A re-entry hands ownership back to this hook; a later adopt sets it again.
      adopted.current = false;
      return;
    }
    // React.StrictMode invokes this effect twice for the same activation; a
    // second entry read would be a real double request. Guarding here rather
    // than from the cleanup keeps the first read alive.
    if (walked.current) return;
    walked.current = true;
    void readStatus();
  }, [enabled, readStatus]);

  React.useEffect(() => {
    if (!enabled || adopted.current || !scan?.running) return;
    // The bound is on the STALL, not on the scan's duration: while reads keep
    // succeeding the loop follows the scan for as long as it walks, and it ends
    // on `!scan.running` anyway. Only a status read that has stopped succeeding
    // stops the loop - the advisory read must not poll a dead API for a whole
    // session while the operator is on another view.
    const last = lastReadAt.current;
    if (last != null && Date.now() - last > RESCAN_TIMEOUT_MS) return;
    // Re-arms on every successful read because `scan` gets a new identity, and
    // on `pollTick` when a failed read changed nothing.
    const timer = window.setTimeout(() => void readStatus(), POLL_MS);
    return () => window.clearTimeout(timer);
  }, [enabled, scan, pollTick, readStatus]);

  // Announce once per completed scan, on the running -> finished transition. A
  // scan can also finish between polls, so `finished_at` is the signal, not
  // `running`.
  const finishedAt = loaded && scan && !scan.running ? scan.finished_at : null;
  const registeredCount = scan?.registered ?? 0;
  const flaggedMissingCount = scan?.missing ?? 0;
  React.useEffect(() => {
    if (!loaded) return;
    if (!seeded.current) {
      // The first successful read establishes the baseline: a scan that had
      // already finished before this activation must not announce itself again.
      seeded.current = true;
      announced.current = finishedAt;
      return;
    }
    if (finishedAt == null || announced.current === finishedAt) return;
    // Advance the baseline BEFORE any callback: a run that announces neither
    // signal below must still never announce itself on a later re-read.
    announced.current = finishedAt;
    // The gate, spelled out. The server creates a folder row only for a
    // directory that had a file to register, so `registered > 0` is its own
    // evidence that the tree may have changed; a healing-only run registers
    // nothing and leaves names untouched, so it must not refresh the tree. The
    // asset signal also fires for a run that only newly flagged vanished files
    // (`missing > 0`) - swallowing that would leave the grid stale. Both zero
    // is a run with nothing to announce at all.
    if (registeredCount > 0) onFoldersChanged?.();
    if (registeredCount > 0 || flaggedMissingCount > 0) onAssetsChanged?.();
  }, [
    loaded,
    finishedAt,
    registeredCount,
    flaggedMissingCount,
    onFoldersChanged,
    onAssetsChanged,
  ]);

  const run = React.useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      // This scan IS this hook's to poll and to announce.
      adopted.current = false;
      setScan(await startLibraryScan());
      setLoaded(true);
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized?.();
        return;
      }
      // A scan that could not take the slot is NOT an error here: the server
      // accepts the operation and ends it `failed` with
      // `SCAN_ALREADY_RUNNING`, so `startLibraryScan` resolves with a status the
      // panel reads as running. Only a refused plan or submit throws, and its
      // message is what the panel shows.
      setError(err instanceof Error ? err.message : "Failed to start scan");
    } finally {
      setBusy(false);
    }
  }, [onUnauthorized]);

  const adopt = React.useCallback((next: LibraryScanStatus) => {
    adopted.current = true;
    // The card path already announced this completion by refetching the grid
    // and the counts, so move the baseline BEFORE the state lands: the
    // announcement effect must not fire both signals a second time.
    if (!next.running && next.finished_at != null) {
      announced.current = next.finished_at;
    }
    setScan(next);
    setLoaded(true);
  }, []);

  return { scan, busy, error, loaded, run, adopt };
}
