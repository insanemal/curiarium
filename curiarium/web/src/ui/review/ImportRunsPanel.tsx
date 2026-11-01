import type { ImportRunSummary } from "../../lib/api/settings";
import {
  formatImportRunDuration,
  formatImportRunTime,
  importRunCountsLine,
  importRunModeLabel,
  importRunReason,
  importRunRejectedNote,
  importRunStatusLabel,
  isImportRunFailure,
} from "../../lib/importRuns";
import type { ImportRunsState } from "./useImportRuns";

const MAX_VISIBLE_ERRORS = 5;

function timingLine(run: ImportRunSummary): string {
  const started = formatImportRunTime(run.started_at);
  const finished = formatImportRunTime(run.finished_at);
  const duration = formatImportRunDuration(run.started_at, run.finished_at);
  const parts: string[] = [];
  if (started) parts.push(`Started ${started}`);
  if (finished) parts.push(`finished ${finished}`);
  if (duration) parts.push(duration);
  return parts.join(" · ");
}

function statusToneClass(run: ImportRunSummary): string {
  if (isImportRunFailure(run)) return "text-red-400";
  if (run.status === "skipped") return "text-muted";
  return "opacity-80";
}

function reasonToneClass(run: ImportRunSummary): string {
  if (isImportRunFailure(run)) return "text-red-400";
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

function RunningRun({ run }: { run: ImportRunSummary }) {
  const rejectedNote = importRunRejectedNote(run);

  return (
    <div className="flex flex-col gap-1">
      <p className="text-sm font-medium">Import in progress</p>
      <p className="text-xs opacity-80">{importRunModeLabel(run)}</p>
      <p className="text-xs opacity-80">{timingLine(run)}</p>
      <p className="text-xs opacity-80">{importRunCountsLine(run)}</p>
      {rejectedNote && <p className="text-xs opacity-80">{rejectedNote}</p>}
    </div>
  );
}

function FinishedRun({ run }: { run: ImportRunSummary & { errors: string[] } }) {
  const rejectedNote = importRunRejectedNote(run);
  const reason = importRunReason(run);

  return (
    <div className="flex flex-col gap-1">
      <p className={`text-sm font-medium ${statusToneClass(run)}`}>{importRunStatusLabel(run)}</p>
      <p className="text-xs opacity-80">{importRunModeLabel(run)}</p>
      <p className="text-xs opacity-80">{timingLine(run)}</p>
      <p className="text-xs opacity-80">{importRunCountsLine(run)}</p>
      {rejectedNote && <p className="text-xs opacity-80">{rejectedNote}</p>}
      {reason && <p className={`text-xs ${reasonToneClass(run)}`}>{reason}</p>}
      <ErrorList errors={run.errors} />
    </div>
  );
}

/**
 * The latest mounted-import run inside the Imports running queue. The queue's
 * header already carries the title, count and Refresh, so this panel only
 * states the run itself - and, while nothing is walking, the literal idle line
 * above the last run that did.
 */
export default function ImportRunsPanel({ state }: { state: ImportRunsState }) {
  const { run, loading, error } = state;

  return (
    <div className="review-panel">
      <div>
        <div className="text-lg font-semibold">Latest run</div>
        <p className="text-sm opacity-70">
          The latest run reported by the API. While a run is walking the mount its counters
          update live.
        </p>
      </div>
      {run && run.status === "running" && <RunningRun run={run} />}
      {run && run.status !== "running" && (
        <div className="flex flex-col gap-1">
          <p className="text-sm font-medium">No import currently running</p>
          <FinishedRun run={run} />
        </div>
      )}
      {!run && loading && <p className="text-sm">Loading import runs...</p>}
      {!run && !loading && error && (
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
      {!run && !loading && !error && (
        <div className="flex flex-col gap-1">
          <p className="text-sm font-medium">No import currently running</p>
          <p className="text-xs opacity-70">No mount import has run yet.</p>
          <p className="text-xs opacity-70">
            A run is recorded at startup whenever a mount is configured and enabled.
          </p>
        </div>
      )}
    </div>
  );
}
