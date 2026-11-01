import { type LibraryScanStatus } from "../../lib/api/settings";
import LibraryScanFailures from "./LibraryScanFailures";
import type { LibraryScanState } from "./useLibraryScan";

type Props = {
  state: LibraryScanState;
  /**
   * `editor` and above: `POST /library/scan` is WRITE. The status line and the
   * failure report are reads (`GET /library/scan` is READ) and stay visible to
   * a viewer, so moving the surface broadens no write permission.
   */
  canWrite: boolean;
};

/** What the last scan reported, in one line; the panel never invents a number. */
function scanLine(scan: LibraryScanStatus): string {
  if (scan.running) return "Scan in progress";
  return `Last scan: ${scan.registered} new, ${scan.known} known, ${scan.missing} missing, ${scan.failed} failed`;
}

/**
 * The storage rescan, moved out of Settings into the Missing files queue. It is
 * work on the library, not configuration, and it is the only way the operator
 * can refresh the flag set the queue lists.
 */
export default function LibraryScanPanel({ state, canWrite }: Props) {
  const { scan, busy, error, run } = state;

  return (
    <div className="rounded-lg border border-panel bg-panel-soft p-4 flex flex-col gap-3" data-library-scan>
      <div>
        <div className="text-lg font-semibold">Files added from outside Curiarium</div>
        <p className="text-sm opacity-70">
          Scan the storage folder and register files you copied in with rsync, cp or another tool.
          Nothing is moved or deleted: files are registered where they already sit, and folders are created from the directory tree.
          Files that have disappeared are counted and flagged in the grid, but never removed from the library.
        </p>
      </div>
      <div className="flex items-center gap-3 flex-wrap">
        {canWrite && (
          <button
            className="text-sm px-3 py-2 rounded-md border border-panel-strong disabled:opacity-60"
            disabled={busy || Boolean(scan?.running)}
            onClick={() => void run()}
          >
            {scan?.running ? "Scanning..." : "Rescan storage folder"}
          </button>
        )}
        {scan && <span className="text-xs opacity-80">{scanLine(scan)}</span>}
        {error && <span className="text-xs text-red-400">{error}</span>}
      </div>
      {scan && <LibraryScanFailures failed={scan.failed} errors={scan.errors} />}
    </div>
  );
}
