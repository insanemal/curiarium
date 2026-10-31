import { copyText } from "../../lib/clipboard";
import { notifyError, notifyInfo } from "../../lib/toast";

const MAX_VISIBLE_FAILURES = 10;

type Props = { failed: number; errors: string[] };

function headline(failed: number): string {
  return failed === 1 ? "1 file failed" : `${failed} files failed`;
}

/** How much of the failure list the panel is actually showing, and out of what. */
function visibleLine(failed: number, shown: number): string {
  if (shown >= failed) {
    return failed === 1 ? "Showing the only failure" : `Showing all ${failed} failures`;
  }
  return `Showing the first ${shown} of ${failed} failures`;
}

async function copyFailures(errors: string[]): Promise<void> {
  try {
    await copyText(errors.join("\n"));
    notifyInfo(`Copied ${errors.length} failure${errors.length === 1 ? "" : "s"} to the clipboard`);
  } catch {
    notifyError("Could not copy the failures to the clipboard");
  }
}

export default function LibraryScanFailures({ failed, errors }: Props) {
  if (failed <= 0) return null;

  const visible = errors.slice(0, MAX_VISIBLE_FAILURES);
  const hidden = errors.length - visible.length;
  const notRecorded = failed - errors.length;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm font-medium text-red-400">{headline(failed)}</p>
        {errors.length > 0 && (
          <button
            type="button"
            className="text-xs px-2 py-1 rounded-md border border-panel-strong"
            onClick={() => {
              void copyFailures(errors);
            }}
          >
            Copy
          </button>
        )}
      </div>
      {visible.length > 0 && (
        <div className="rounded-md border border-dashed border-panel-strong p-3 font-mono text-[11px] break-all">
          {visible.map((message, index) => (
            <div key={index}>{message}</div>
          ))}
          {hidden > 0 && <div className="opacity-70">+{hidden} more</div>}
        </div>
      )}
      <p className="text-xs opacity-70">{visibleLine(failed, visible.length)}</p>
      {notRecorded > 0 && (
        <p className="text-xs opacity-70">
          {notRecorded} more failure{notRecorded === 1 ? "" : "s"} were not recorded individually.
        </p>
      )}
    </div>
  );
}
