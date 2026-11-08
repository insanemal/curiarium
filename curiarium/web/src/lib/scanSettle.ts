/**
 * The one place in the tree that knows how to wait for a library scan.
 *
 * A scan is asynchronous by design: `POST /library/scan` answers as soon as the
 * walk is queued, and the files a caller just wrote are only visible once the
 * walk registers them. Anything that must observe the result of a write (an
 * archive extract today, an import later) therefore polls the status endpoint
 * here instead of growing its own retry loop. Keeping the loop in one module
 * also keeps its two awkward rules in one place: a read that never happened
 * cannot claim the scan settled, and a revoked session must never be mistaken
 * for a slow scan.
 *
 * Domain modules only, never the `lib/api` barrel: `App`'s tests replace that
 * barrel with a fixed object, so a named import from it would throw "No export
 * is defined on the mock". `ui/review/useLibraryScan.ts` sets the precedent.
 * `ui/asset-grid/assetGridMeta.ts` (imported for `RESCAN_TIMEOUT_MS`) does reach
 * the barrel itself, so a test that both mocks `lib/api` and loads this module
 * at runtime must keep the names it pulls real.
 */
import { UnauthorizedError } from "./api/http";
import { getLibraryScanStatus, startLibraryScan } from "./api/settings";
import type { ArchiveExtractResult } from "./api/imports";
import { RESCAN_TIMEOUT_MS } from "../ui/asset-grid/assetGridMeta";

/**
 * Matches `POLL_MS` in `ui/review/useLibraryScan.ts`. Both are private, so the
 * comment is the only thing tying them together. The tree does hold a second
 * rate: `ui/asset-grid/assetGridMeta.ts`'s `RESCAN_POLL_INTERVAL_MS` polls at
 * 1000ms for the per-card Re-scan, which is its own waiter.
 */
export const SETTLE_POLL_MS = 1500;

/** `RESCAN_TIMEOUT_MS` itself - the other scan waiter's deadline, not a copy of it. */
export const SETTLE_TIMEOUT_MS = RESCAN_TIMEOUT_MS;

/**
 * `settled` - no scan was running any more, so the write is visible.
 * `timeout` - we stopped waiting without proof: the deadline passed, a status
 *             read failed, or re-kicking the scan failed.
 * `cancelled` - the caller withdrew, or its session was revoked.
 */
export type SettleOutcome = "settled" | "timeout" | "cancelled";

export type SettleOptions = {
  /** Polled between reads: a dialog the user closed must stop the wait. */
  cancelled?: () => boolean;
  /** A revoked session is the shell's business, not a scan outcome. */
  onUnauthorized?: () => void;
};

/** The exact text `startLibraryScan` throws for a 409 (`lib/api/settings.ts:106-113`). */
const ALREADY_RUNNING = "A scan is already running";

/**
 * Shared by both waits so neither can drift from the poll interval. The plain
 * global `setTimeout` is used (not `window.setTimeout`) so fake timers under
 * jsdom intercept it and the tests can step the poll deterministically.
 */
function nextPoll(): Promise<void> {
  return new Promise<void>(resolve => {
    setTimeout(resolve, SETTLE_POLL_MS);
  });
}

/**
 * Waits until no library scan is running, or until the wait is pointless.
 *
 * The deadline is fixed once at entry rather than refreshed per poll: a scan
 * that keeps restarting must not extend the wait forever. `Date.now()` is read
 * fresh inside the loop, because a fake clock only moves between polls.
 *
 * This never throws, and each failure is mapped to a different outcome on
 * purpose:
 * - `UnauthorizedError` becomes `cancelled` after `onUnauthorized`, never a
 *   plain `timeout`: a revoked session has to reach the shell and sign the
 *   operator out, and folding it into "the scan was slow" would hide it.
 * - Any other read rejection becomes `timeout`. The read that would have said
 *   "not running" never happened, so this wait has no evidence the scan
 *   finished and must not claim it did - the same reasoning
 *   `ui/review/useLibraryScan.ts` applies when it treats a swallowed status
 *   read as `unknown` rather than a clean disk.
 */
export async function waitForScanSettle(opts: SettleOptions = {}): Promise<SettleOutcome> {
  const deadline = Date.now() + SETTLE_TIMEOUT_MS;
  for (;;) {
    if (opts.cancelled?.()) return "cancelled";
    let running: boolean;
    try {
      const status = await getLibraryScanStatus();
      running = status.running;
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        opts.onUnauthorized?.();
        return "cancelled";
      }
      return "timeout";
    }
    if (!running) return "settled";
    if (Date.now() >= deadline) return "timeout";
    await nextPoll();
  }
}

/**
 * Waits out the scan an archive extract relies on, re-kicking it when the
 * extract could not.
 *
 * `start_scan()` returns False when a scan already holds the slot and does NOT
 * queue another pass (`curiarium/api/library_scan.py:83-84` and `:253-258`), so
 * the walk that was already running may have passed the new directory before
 * the extract wrote it. An extract that reports `scan_started: false` was in
 * exactly that position: the first window drains that pre-existing walk, and
 * only then is a fresh pass kicked, because a second start while the old walk
 * is still running would be refused. That gives two windows, never a third.
 *
 * Nothing written means nothing to wait for: a batch of all-`already_present`
 * results, or one whose entries were all junk (`files: 0`), returns without
 * touching the network at all.
 */
export async function settleAfterExtract(
  results: ArchiveExtractResult[],
  opts: SettleOptions = {},
): Promise<SettleOutcome> {
  const written = results.filter(result => result.files > 0);
  if (written.length === 0) return "settled";

  const first = await waitForScanSettle(opts);
  if (first !== "settled") return first;

  // The extract started its own scan, or one was already running and is now
  // finished with the new files: either way there is nothing left to kick.
  if (!written.some(result => result.scan_started === false)) return "settled";

  try {
    await startLibraryScan();
  } catch (err) {
    if (err instanceof UnauthorizedError) {
      opts.onUnauthorized?.();
      return "cancelled";
    }
    // A scan that grabbed the slot between our read and this call is not a
    // failure: it walks the new directory anyway, which is what we wanted.
    if (!(err instanceof Error) || err.message !== ALREADY_RUNNING) return "timeout";
  }

  return waitForScanSettle(opts);
}
