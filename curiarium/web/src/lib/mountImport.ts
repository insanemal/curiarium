import type { MountImportStatus } from "./api/settings";

/** The outcomes that carry a reason: a walk that skipped, one that failed, and
 *  one the operator cancelled all say why. A finished walk does not. */
function carriesReason(status: MountImportStatus): boolean {
  if (status.status === "failed" || status.status === "cancelled") return true;
  return status.outcome === "skipped";
}

/**
 * The walk as one word, read off the operation's status and the step's
 * outcome together: `done` alone cannot say whether the walk registered what it
 * found or skipped the whole mount, because both are a `done` row.
 *
 * `status === null` is not a state the wire can answer - the ledger simply holds
 * no `mount-import` row - so the label says that rather than pretending to know
 * an outcome. A status this client does not know reads verbatim: the client
 * invents no word for a state it does not recognise.
 */
export function statusLabel(status: MountImportStatus): string {
  switch (status.status) {
    case null:
      return "No walk recorded";
    case "queued":
      return "Queued";
    case "running":
      return "Running";
    case "cancelling":
      return "Cancelling";
    case "done":
      if (status.outcome === "skipped") return "Skipped";
      return "Succeeded";
    case "failed":
      return "Failed";
    case "cancelled":
      return "Cancelled";
    default:
      return `Unknown (${status.status})`;
  }
}

/** Failed is the one state the UI styles as a problem. A cancelled walk is an
 *  operator decision, not a fault, and a walk that skipped the mount said why. */
export function isFailure(status: MountImportStatus): boolean {
  return status.status === "failed";
}

export function modeLabel(status: MountImportStatus): string {
  if (status.mode === "copy") return "Copied into storage";
  if (status.mode === "reference") return "Referenced in place";
  return "Mode not recorded";
}

function formatSeconds(totalSeconds: number): string {
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  if (minutes < 60) {
    const seconds = totalSeconds % 60;
    return seconds === 0 ? `${minutes}m` : `${minutes}m ${seconds}s`;
  }
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes === 0 ? `${hours}h` : `${hours}h ${restMinutes}m`;
}

/** Human duration for a finished walk; "" while it is still running and "" when
 *  the server recorded no start, because a duration needs both ends. */
export function formatDuration(startedAt: number | null, finishedAt: number | null): string {
  if (startedAt === null || finishedAt === null) return "";
  return formatSeconds(Math.max(0, Math.round(finishedAt - startedAt)));
}

/** Local time for an epoch-seconds timestamp; "" when there is no timestamp. */
export function formatTime(value: number | null): string {
  if (value === null) return "";
  const date = new Date(value * 1000);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString();
}

function fileNoun(count: number): string {
  return count === 1 ? "file" : "files";
}

export function countsLine(status: MountImportStatus): string {
  const parts = [
    `${status.discovered} discovered`,
    `${status.registered} registered`,
    `${status.skipped} skipped`,
    `${status.failed} failed`,
  ];
  if (status.rejected > 0) {
    parts.push(`${status.rejected} oversize`);
  }
  return parts.join(", ");
}

/** Explains the oversize subset, which is counted inside `skipped`. */
export function rejectedNote(status: MountImportStatus): string {
  if (status.rejected <= 0) return "";
  const noun = fileNoun(status.rejected);
  return `${status.rejected} oversize ${noun} exceeded the size limit and ${
    status.rejected === 1 ? "was" : "were"
  } skipped.`;
}

/**
 * Why a walk did nothing or went wrong. Only skipped, failed and cancelled
 * walks carry one; a missing reason still renders a placeholder for a walk the
 * server recorded as skipped, which is the one case where the reason IS the
 * report.
 */
export function reason(status: MountImportStatus): string {
  if (!carriesReason(status)) return "";
  const text = (status.reason ?? "").trim();
  if (text) return text;
  if (status.outcome === "skipped") return "No reason was recorded.";
  return "";
}
