import React, { useState, type ReactElement } from "react";
// Domain modules, never the `../../lib/api` barrel: several suites replace that
// barrel with a fixed object, so a named import from it would come back
// `undefined` there (`ui/review/useBundleDetect.ts` documents the same rule).
import type { ArchiveExtractResult } from "../../lib/api/imports";
import type { Operation, OperationUndo } from "../../lib/api/operations";
import { notifyInfo } from "../../lib/toast";
import { ArchiveEntryPicker } from "../operations/ImportEntriesDialog";
import OperationPanel from "../operations/OperationPanel";
import { extractResult, extractUndoLine } from "../operations/operationView";

type Props = {
  /** Contribute capability: extraction writes new bytes into the library. Required. */
  canContribute: boolean;
  /** Library-relative path of the archive, e.g. "Moai.zip". */
  path: string;
  /** Human label for the accessible names, e.g. "Moai.zip". */
  label: string;
  /** "detail" for the asset preview footer, "row" for a compact settings-card row. */
  variant?: "detail" | "row";
  /**
   * Refuse every control this instance renders, at every stage: the caller has
   * another write in flight on the same archive and this one would race it.
   */
  disabled?: boolean;
  /** Fires after a successful extract so the caller can refresh what it shows. */
  onExtracted?: (result: ArchiveExtractResult) => void;
};

const DETAIL_BUTTON = "px-3 py-2 rounded-md border border-panel-strong text-sm disabled:opacity-60";
// A compact bordered row button, for a dense list row or a settings card.
const ROW_BUTTON =
  "text-xs px-2 py-1 rounded-md border border-panel-strong self-start disabled:opacity-60";

/** The news an undo answers with. The extracted files deliberately stay (the
 *  server documents the residue), so the news is the undo's own answer rather
 *  than a claim of erasure. */
function announceUndo(answer: OperationUndo): void {
  notifyInfo("Extract undone", { details: [extractUndoLine(answer)] });
}

/**
 * Extracts a `.zip` that is already in the library, beside itself: the `extract`
 * operation, whole.
 *
 * The trigger, the review, the entry picker, the state machine and the progress
 * dialog all live in `ui/operations/OperationPanel` and its siblings, so this
 * file is this door's TRIGGER, its picker and its report: the plan the server
 * computes IS the review (how many members, which groups they fall into, and
 * every member it would leave out with its own reason), and the write is that
 * plan's `plan_id` handed back. There is no local stage machine to keep in step
 * with the runner.
 *
 * `variant` still names the two visual shapes the app mounts this control in,
 * but they now differ ONLY in the trigger button's classes: the review and the
 * dialog are the shared ones in both places.
 *
 * The ENTRY PICKER is the plan's own listing (`review.entries` / `review.excluded`),
 * so the picker and the review cannot disagree about which members are
 * selectable. A confirmed selection changes the plan's params, which re-plans
 * the review for the narrowed set; with no selection - the default - the whole
 * archive travels and the request carries no `entries` key at all.
 *
 * The server enforces the capability (`require_contribute`); hiding the control
 * below contributor is UX, not security.
 */
export default function ArchiveExtractAction({
  canContribute,
  path,
  label,
  variant = "detail",
  disabled = false,
  onExtracted,
}: Props): ReactElement | null {
  // The operator's draft selection, or null for "the whole archive". The panel
  // re-plans when this changes identity, and a null draft sends no `entries` key.
  const [entries, setEntries] = useState<string[] | null>(null);

  // The host's completion signal, once per operation, mapped exactly as the
  // deleted route answered it. `null` means the extract step reported nothing,
  // which is not a report and must not be passed on as one.
  function report(operation: Operation): void {
    const result = extractResult(operation);
    if (result !== null) onExtracted?.(result);
  }

  const button = variant === "row" ? ROW_BUTTON : DETAIL_BUTTON;

  return (
    <OperationPanel
      workflow="extract"
      target={path}
      params={entries ? { entries } : {}}
      canContribute={canContribute}
      variant="trigger"
      triggerLabel={`Extract ${label}`}
      triggerClassName={`${button} bg-accent`}
      disabled={disabled}
      dataPrefix="extract"
      onResult={report}
      onUndo={announceUndo}
      render={run =>
        run.plan ? (
          <ArchiveEntryPicker
            label={label}
            entries={run.plan.review.entries ?? []}
            excluded={run.plan.review.excluded}
            selected={entries ?? []}
            onConfirm={setEntries}
            disabled={run.busy}
            dataPrefix="extract"
          />
        ) : null
      }
    />
  );
}
