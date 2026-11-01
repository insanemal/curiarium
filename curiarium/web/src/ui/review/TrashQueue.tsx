import { type ReactElement, useState } from "react";

import type { TrashPurgeResult } from "../../lib/api/trash";
import { formatBytes } from "../../lib/format";
import ConfirmDialog from "../ConfirmDialog";
import ReviewQueueView from "./ReviewQueueView";
import { describeQueueCount, trashBadgeCount } from "./useReviewCounts";
import type { TrashState } from "./useTrash";

type Props = {
  state: TrashState;
  /** `admin` only: `POST /trash/purge` empties a folder nobody can restore. */
  canPurge: boolean;
};

/** The card's secondary button, kept so the retry reads the other queues' way. */
const SECONDARY_BUTTON =
  "text-xs px-2.5 py-1.5 rounded-md border border-panel-strong disabled:opacity-60";

/**
 * The Trash queue: the card that used to sit at the bottom of Settings' Storage
 * Structure section, promoted to a full-width queue (ADR 0011, slice 4).
 *
 * Every fetch and mutation lives in `useTrash`; this view is presentational and
 * its only local state is whether the purge dialog is open.
 *
 * There is deliberately no restore control - not a button, not a link, nothing
 * matching /restore/i. Restoring is manual by design: the entry's bytes are
 * still on disk, so the operator moves it back into the storage folder and
 * rescans. The explanation sentence is the whole affordance.
 *
 * The dialog is `ConfirmDialog` rather than the card's hand-rolled overlay,
 * which had no Escape handler and no initial focus; that move is what makes
 * the confirmation keyboard-complete (Escape, `aria-modal`,
 * `aria-labelledby`, autofocus on Confirm - or on Cancel when the purge is
 * refused).
 */
export default function TrashQueueView({ state, canPurge }: Props): ReactElement {
  const [confirming, setConfirming] = useState(false);

  const entries = state.trash?.entries ?? [];
  const totalSize = state.trash?.total_size ?? 0;
  // A snapshot refusal is READ state, never recomputed from the role: the
  // server decides whether this storage volume can take a snapshot, and a
  // deployment with `policy: strict` and no snapshot capability refuses the
  // purge outright. Re-deriving it here would be a second, drifting answer.
  const purgeNotice = state.trash?.snapshot ?? null;
  const purgeRefused = purgeNotice?.action === "refuse";
  // A purge that removed nothing is a failure even though it answered 200.
  const purgeFailed =
    state.purgeError !== null ||
    (state.purgeResult !== null && state.purgeResult.failed.length > 0);
  const entryCount = entries.length;
  const entryNoun = entryCount === 1 ? "entry" : "entries";

  return (
    <ReviewQueueView
      title="Trash"
      explanation="Deleted folders wait here before they are purged. Deleting a folder never frees its bytes — only emptying the trash does. Restoring is file-level: move an entry back into the storage folder, then rescan the library."
      count={trashBadgeCount(state.trash)}
      countTitle={describeQueueCount("trash", trashBadgeCount(state.trash))}
      lastRefreshedAt={state.lastRefreshedAt}
      onRefresh={() => void state.refresh()}
      refreshing={state.refreshing}
      notices={
        state.error && state.trash !== null ? (
          <p className="review-notice">
            {state.error} Refresh failed; the list below is the last one we could read.
          </p>
        ) : undefined
      }
    >
      {state.loading && (
        <p className="review-loading" aria-busy="true">Loading trash…</p>
      )}

      {!state.loading && state.error !== null && state.trash === null && (
        <div className="review-error" role="alert">
          <p>{state.error}</p>
          <button
            type="button"
            className={SECONDARY_BUTTON}
            onClick={() => void state.refresh()}
          >
            Try again
          </button>
        </div>
      )}

      {entries.length > 0 && (
        <ul className="flex flex-col divide-y divide-panel rounded-md border border-panel-strong">
          {entries.map(entry => (
            <li
              key={entry.path}
              className="flex items-center justify-between gap-3 px-3 py-2 text-sm"
            >
              <div className="min-w-0">
                <div className="truncate font-medium" title={entry.path}>{entry.name}</div>
                <div className="text-xs opacity-60">
                  {new Date(entry.mtime).toLocaleString()}
                </div>
              </div>
              <span className="shrink-0 text-xs opacity-80">{formatBytes(entry.size)}</span>
            </li>
          ))}
        </ul>
      )}

      {!state.loading && state.trash !== null && (
        <div className="review-toolbar">
          <span className="text-sm opacity-80">
            {entryCount} {entryNoun} · {formatBytes(totalSize)}
          </span>
          {/* Hidden, never disabled, below admin: a viewer, a contributor or an
              editor must have no path to a purge (ADR 0011 decisions 7/15). */}
          {canPurge && (
            <button
              type="button"
              className="text-sm px-3 py-2 rounded-md bg-red-600 text-white disabled:opacity-60"
              onClick={() => setConfirming(true)}
              disabled={entries.length === 0}
            >
              Empty trash
            </button>
          )}
        </div>
      )}

      {state.purgeResult !== null && <PurgeResult result={state.purgeResult} />}

      {!confirming && state.purgeError !== null && (
        <p className="review-notice">{state.purgeError}</p>
      )}

      {/* The lying-label rule: only a read that succeeded, on an empty list,
          with no failed read and no failed purge behind it may claim this. */}
      {!state.loading &&
        state.trash !== null &&
        entries.length === 0 &&
        state.error === null &&
        !purgeFailed && (
          <p className="review-empty">
            The trash is empty. Folders you delete will appear here until the trash is emptied.
          </p>
        )}

      {confirming && canPurge && (
        <ConfirmDialog
          title="Empty the trash?"
          body={
            <>
              <p>
                This permanently deletes {entryCount} {entryNoun} ({formatBytes(totalSize)}) from
                the server. It cannot be undone and nothing can be restored afterwards.
              </p>
              <p className="opacity-70">
                Only the contents of the trash folder are removed; your library is not touched.
              </p>
              {purgeNotice?.action === "snapshot" && (
                <p className="opacity-70">
                  A filesystem snapshot of the library is taken before the trash is emptied,
                  named like <code>{purgeNotice.pattern}</code>. The exact name is recorded in
                  the audit log.
                  {purgeNotice.policy === "strict"
                    ? " If the snapshot cannot be taken, the purge is refused and nothing is deleted."
                    : " If the snapshot cannot be taken, the purge goes ahead without one and the miss is recorded in the audit log."}
                </p>
              )}
              {purgeNotice?.action === "proceed_without" && (
                <p className="opacity-70">
                  No snapshot can be taken on this storage volume{purgeNotice.detail ? `, because ${purgeNotice.detail}` : ""}.
                  {" Emptying the trash still goes ahead and the missed snapshot is recorded in the audit log."}
                </p>
              )}
              {purgeNotice?.action === "refuse" && (
                <p className="text-red-400">
                  This storage volume cannot take snapshots, and this deployment refuses to
                  empty the trash without one. Nothing will be deleted.
                </p>
              )}
              {state.purgeError !== null && <p className="text-red-400">{state.purgeError}</p>}
            </>
          }
          confirmLabel="Empty trash permanently"
          cancelLabel="Cancel"
          danger
          busy={state.purging}
          busyLabel="Emptying…"
          confirmDisabled={purgeRefused}
          onConfirm={() => {
            void state.purge().then(ok => {
              if (ok) setConfirming(false);
            });
          }}
          onCancel={() => setConfirming(false)}
        />
      )}
    </ReviewQueueView>
  );
}

/** What the last purge removed, and what it could not. */
function PurgeResult({ result }: { result: TrashPurgeResult }): ReactElement {
  const removed = `${result.purged} ${result.purged === 1 ? "entry" : "entries"} removed (${formatBytes(result.bytes)})`;
  const failed = result.failed.length > 0;
  // A total failure is HTTP 200 with `purged: 0`; "0 entries removed" would read
  // as a success, so that case states the failure alone.
  const showsRemovedLine = !failed || result.purged > 0;

  return (
    <div className="review-panel">
      {showsRemovedLine && <div>{removed}</div>}
      {failed && result.purged === 0 && <div>Nothing was removed.</div>}
      {failed && result.purged > 0 && <div>Some entries could not be removed:</div>}
      {failed && (
        <div className="mt-1 text-xs text-red-400">
          {result.failed.map((message, index) => (
            <div key={index} className="break-all">{message}</div>
          ))}
        </div>
      )}
    </div>
  );
}
