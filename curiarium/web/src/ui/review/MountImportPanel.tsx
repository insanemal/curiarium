import type { MountImportStatus } from "../../lib/api/settings";
import {
  countsLine,
  formatDuration,
  formatTime,
  isFailure,
  modeLabel,
  reason,
  rejectedNote,
  statusLabel,
} from "../../lib/mountImport";
import type { MountImportState } from "./useMountImport";

const MAX_VISIBLE_ERRORS = 5;

/** The one sentence the running state owes the operator: the ledger writes a
 *  walk's counters with its result, so there is genuinely nothing to count yet
 *  and a line of zeros would be a claim the server has not made. */
const COUNTERS_LATER =
  "The walk's counters are reported when it finishes: the operations ledger records a run's totals with its result, not while it runs.";

function timingLine(status: MountImportStatus): string {
  const started = formatTime(status.started_at);
  const finished = formatTime(status.finished_at);
  const duration = formatDuration(status.started_at, status.finished_at);
  const parts: string[] = [];
  if (started) parts.push(`Started ${started}`);
  if (finished) parts.push(`finished ${finished}`);
  if (duration) parts.push(duration);
  return parts.join(" · ");
}

function statusToneClass(status: MountImportStatus): string {
  if (isFailure(status)) return "text-red-400";
  if (status.outcome === "skipped") return "text-muted";
  return "opacity-80";
}

function reasonToneClass(status: MountImportStatus): string {
  if (isFailure(status)) return "text-red-400";
  return "text-muted";
}

function ErrorList({ errors }: { errors: string[] }) {
  const visible = errors.slice(0, MAX_VISIBLE_ERRORS);
  const hidden = errors.length - visible.length;
  if (!visible.length) return null;

  return (
    <div className="rounded-md border border-dashed border-panel-strong p-3 font-mono text-[11px] break-all">
      {visible.map((message, index) => (
        <div key={index}>{message}</div>
      ))}
      {hidden > 0 && <div className="opacity-70">+{hidden} more</div>}
    </div>
  );
}

/** A walk in flight: its mode and its clock, and the words that say why there is
 *  no counters line yet. Deliberately no number is printed here. */
function RunningWalk({ status }: { status: MountImportStatus }) {
  return (
    <div className="flex flex-col gap-1">
      <p className="text-sm font-medium">Import in progress</p>
      <p className="text-xs opacity-80">{modeLabel(status)}</p>
      <p className="text-xs opacity-80">{timingLine(status)}</p>
      <p className="text-xs opacity-80">{COUNTERS_LATER}</p>
    </div>
  );
}

/** A walk that ended, whatever the ending was. The counters, the oversize note,
 *  the failure sentence and the reason all come off the one ledger row. */
function FinishedWalk({ status }: { status: MountImportStatus }) {
  const rejected = rejectedNote(status);
  const why = reason(status);

  return (
    <div className="flex flex-col gap-1">
      <p className={`text-sm font-medium ${statusToneClass(status)}`}>{statusLabel(status)}</p>
      <p className="text-xs opacity-80">{modeLabel(status)}</p>
      <p className="text-xs opacity-80">{timingLine(status)}</p>
      <p className="text-xs opacity-80">{countsLine(status)}</p>
      {rejected && <p className="text-xs opacity-80">{rejected}</p>}
      {status.error && <p className="text-xs text-red-400">{status.error}</p>}
      {why && <p className={`text-xs ${reasonToneClass(status)}`}>{why}</p>}
      <ErrorList errors={status.errors} />
    </div>
  );
}

/**
 * The newest mounted import inside the Imports running queue. The queue's header
 * already carries the title, count and Refresh, so this panel only states the
 * walk itself - and, while nothing is walking, the literal idle line above the
 * last walk that did.
 *
 * The walk is an operation in the shared ledger now, so the copy says so: there
 * is no run history to page and no live overlay to fill in, only one row whose
 * step reports its totals when it finishes.
 */
export default function MountImportPanel({ state }: { state: MountImportState }) {
  const { status, loading, error } = state;
  const walking = status !== null && status.running;

  return (
    <div className="review-panel">
      <div>
        <div className="text-lg font-semibold">Latest walk</div>
        <p className="text-sm opacity-70">
          The newest mount import recorded in the operations ledger. Its counters are written
          with the walk&rsquo;s result, so they arrive when it finishes.
        </p>
      </div>
      {walking && <RunningWalk status={status} />}
      {status && !walking && (
        <div className="flex flex-col gap-1">
          <p className="text-sm font-medium">No walk currently running</p>
          <FinishedWalk status={status} />
        </div>
      )}
      {!status && loading && <p className="text-sm">Loading the mount import...</p>}
      {!status && !loading && error && (
        <div className="flex flex-col gap-1">
          <p className="text-sm text-red-400">{error}</p>
          <div>
            <button
              type="button"
              className="review-refresh"
              disabled={state.refreshing}
              onClick={() => void state.refresh()}
            >
              {state.refreshing ? "Trying…" : "Try again"}
            </button>
          </div>
        </div>
      )}
      {!status && !loading && !error && (
        <div className="flex flex-col gap-1">
          <p className="text-sm font-medium">No walk currently running</p>
          <p className="text-xs opacity-70">No mount import has run yet.</p>
          <p className="text-xs opacity-70">
            One is recorded at every boot, whenever a mount is configured and enabled.
          </p>
        </div>
      )}
    </div>
  );
}
