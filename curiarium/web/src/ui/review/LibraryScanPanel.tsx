import LibraryScanFailures from "./LibraryScanFailures";
import ScanStorageAction from "./ScanStorageAction";
import type { LibraryScanState } from "./useLibraryScan";

type Props = {
  state: LibraryScanState;
  /**
   * `contributor` and above: the `library-scan` workflow is CONTRIBUTE. The
   * status line and the failure report are reads (`GET /operations?workflow=library-scan`
   * is READ) and stay visible to a viewer, so moving the surface broadens no
   * write permission.
   */
  canContribute: boolean;
  /**
   * Starts the SHELL's scan (App's `scanFromShell`: arm, then run), not this
   * mount's own copy: the shell owns the one poller, so the finish is observed
   * from any view - including after the operator leaves the queue.
   */
  onScan: () => void;
};

/**
 * The storage rescan, moved out of Settings into the Missing files queue. It is
 * work on the library, not configuration, and it is the only way the operator
 * can refresh the flag set the queue lists. The action row itself is shared with
 * the header, the empty state and a detection row, so the wording lives in
 * `ScanStorageAction`.
 *
 * The panel starts the SHELL's scan rather than the hook's own `run`, so a
 * finish is observed from any view: the sticky arm keeps the shell's poll alive
 * while the operator is on the library.
 */
export default function LibraryScanPanel({ state, canContribute, onScan }: Props) {
  const { scan } = state;

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
      <ScanStorageAction
        state={state}
        canContribute={canContribute}
        surface="panel"
        onScan={onScan}
      />
      {scan && <LibraryScanFailures failed={scan.failed} errors={scan.errors} />}
    </div>
  );
}
