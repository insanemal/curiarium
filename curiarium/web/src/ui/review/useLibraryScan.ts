import React from "react";

// Imported from the domain modules, not the `../../lib/api` barrel: `App`'s
// tests replace that barrel with a fixed object, so a new named import from it
// would throw "No export is defined on the mock". `useImportRuns` sets the same
// precedent.
import { UnauthorizedError } from "../../lib/api/http";
import { getLibraryScanStatus, startLibraryScan, type LibraryScanStatus } from "../../lib/api/settings";
import type { MissingScanOutcome } from "../asset-grid/assetGridMeta";

type Props = {
  /** Whether the missing queue is open. Nothing is fetched on app load. */
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
 * The queue owns the hook, so it reads once per activation and then polls only
 * while a scan is running. That entry read is the point of this hook: the old
 * settings-owned version never fetched an initial status, so the missing-flag
 * set was `unknown` until somebody pressed Rescan.
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
  // The baseline for the announcement: seeded on the first successful read,
  // kept across activations so a finished scan announces exactly once.
  const seeded = React.useRef(false);
  const announced = React.useRef<number | null>(null);

  const readStatus = React.useCallback(async () => {
    try {
      const next = await getLibraryScanStatus();
      setScan(next);
      setLoaded(true);
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
    if (!enabled || !scan?.running) return;
    // Re-arms on every successful read because `scan` gets a new identity, and
    // on `pollTick` when a failed read changed nothing.
    const timer = window.setTimeout(() => void readStatus(), POLL_MS);
    return () => window.clearTimeout(timer);
  }, [enabled, scan, pollTick, readStatus]);

  // Announce once per completed scan, on the running -> finished transition. A
  // scan can also finish between polls, so `finished_at` is the signal, not
  // `running`.
  const finishedAt = loaded && scan && !scan.running ? scan.finished_at : null;
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
    announced.current = finishedAt;
    onFoldersChanged?.();
    onAssetsChanged?.();
  }, [loaded, finishedAt, onFoldersChanged, onAssetsChanged]);

  const run = React.useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      setScan(await startLibraryScan());
      setLoaded(true);
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized?.();
        return;
      }
      // A 409 arrives as `Error("A scan is already running")`, which IS the
      // message the panel shows for it.
      setError(err instanceof Error ? err.message : "Failed to start scan");
    } finally {
      setBusy(false);
    }
  }, [onUnauthorized]);

  return { scan, busy, error, loaded, run };
}
