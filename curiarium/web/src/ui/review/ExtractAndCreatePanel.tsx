import React from "react";

// Imported from the domain module, not the `../../lib/api` barrel: several
// suites replace that barrel with a fixed object, so a named import from it
// would come back `undefined` there (`useBundleDetect.ts` documents the same
// rule).
import type { OperationUndo } from "../../lib/api/operations";
import { notifyInfo } from "../../lib/toast";
import { ArchiveEntryPicker } from "../operations/ImportEntriesDialog";
import OperationPanel from "../operations/OperationPanel";
import { extractUndoLine } from "../operations/operationView";

type Props = {
  /** The archive's library-relative path; the operation is keyed on it. */
  archivePath: string;
  /** The write gate: a viewer renders nothing and issues no request. */
  canContribute: boolean;
  /** The operator's typed name, already trimmed; "" lets the server name the
   *  project after the archive. */
  name?: string;
  /** The host's completion signal: refetch whatever the host owns. */
  onChanged?: () => void;
  /** Session expiry is the host's news, not this panel's. */
  onUnauthorized?: () => void;
};

/** The news an undo answers with, in the host's toast. The extracted files
 *  deliberately stay after an undo (the server documents the residue), so the
 *  sentence says so instead of implying the tree was erased. */
function announceUndo(answer: OperationUndo): void {
  notifyInfo("Extract undone", { details: [extractUndoLine(answer)] });
}

/**
 * The extract-and-create door: the plan the server computed, ONE action labelled
 * with its outcome, the archive's entry picker, and a popup that narrates the
 * operation it queued.
 *
 * The review, the state machine and the dialog live in
 * `ui/operations/OperationPanel`, so this file is the door's target, its params,
 * its picker and its undo news - not a third implementation. The server computes
 * every step: this panel asks for a plan and can only send that plan's `plan_id`
 * back. A zero-step plan (the server found a project already) is rendered as a
 * finished sentence and gets NO action at all, because there is nothing to
 * approve.
 *
 * The ENTRY PICKER is the plan's own listing: a confirmed selection changes
 * `params.entries`, which makes the panel re-PLAN (a READ) and re-render the
 * review for the narrowed set, so the confirm then submits the plan the operator
 * actually saw. The default is NO selection - the whole archive - so a plain
 * extract stays one click and sends no `entries` key at all.
 *
 * A viewer renders nothing at all: no wrapper, no layout track, no request.
 */
export default function ExtractAndCreatePanel(props: Props): React.ReactElement | null {
  const { archivePath, canContribute, name, onChanged, onUnauthorized } = props;
  const trimmedName = (name ?? "").trim();
  // An empty list means "the whole archive": the plan is then asked with no
  // `entries` key, so the default extract is byte-identical to the pre-picker
  // one.
  const [entries, setEntries] = React.useState<string[] | null>(null);

  return (
    <OperationPanel
      workflow="extract-and-create"
      target={archivePath}
      params={{
        ...(trimmedName ? { name: trimmedName } : {}),
        ...(entries ? { entries } : {}),
      }}
      canContribute={canContribute}
      onChanged={onChanged}
      onUnauthorized={onUnauthorized}
      dataPrefix="extract"
      onUndo={announceUndo}
      render={run =>
        run.plan ? (
          <ArchiveEntryPicker
            label={archivePath}
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
