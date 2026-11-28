import { type LibraryScanStatus } from "../../lib/api/settings";
import type { LibraryScanState } from "./useLibraryScan";

/** Where a mount lives, so a test can tell the four surfaces apart. Test-only
 *  metadata: no styling and no behaviour reads it. */
type ScanSurface = "header" | "panel" | "empty" | "detail";

type Props = {
  state: LibraryScanState;
  /** `contributor` and above: the `library-scan` workflow is CONTRIBUTE. The
   *  status line and the error line are reads and stay visible to a viewer - the
   *  error line is also how a refusal's sentence, a 429 cooldown included,
   *  reaches the operator. */
  canContribute: boolean;
  surface: ScanSurface;
  /** The queue's entry point: every mount passes the shell's `scanFromShell`,
   *  which arms the shell's poll before starting; this component never calls
   *  `state.run`. The arm is sticky, so the poll outlives this mount: a rescan
   *  started here is still followed from any view, the library included. */
  onScan: () => void;
};

/** What the last scan reported, in one line; the action never invents a number. */
function scanLine(scan: LibraryScanStatus): string {
  if (scan.running) return "Scan in progress";
  return `Last scan: ${scan.registered} new, ${scan.known} known, ${scan.missing} missing, ${scan.failed} failed`;
}

/**
 * One storage-scan action, one wording, four surfaces: the library header, the
 * missing queue's panel, that queue's empty state and an unresolved detection
 * row. The `data-scan-surface` attribute is test-only metadata, not styling.
 */
export default function ScanStorageAction({ state, canContribute, surface, onScan }: Props) {
  const { scan, busy, error } = state;

  // Nothing to say: no control for this role and no read to report.
  if (!canContribute && !scan && !error) return null;

  return (
    <div className="flex items-center gap-3 flex-wrap" data-scan-surface={surface}>
      {canContribute && (
        <button
          className="text-sm px-3 py-2 rounded-md border border-panel-strong disabled:opacity-60"
          disabled={busy || Boolean(scan?.running)}
          onClick={onScan}
        >
          {scan?.running ? "Scanning..." : "Rescan storage folder"}
        </button>
      )}
      {scan && <span className="text-xs opacity-80">{scanLine(scan)}</span>}
      {error && <span className="text-xs text-red-400">{error}</span>}
    </div>
  );
}
