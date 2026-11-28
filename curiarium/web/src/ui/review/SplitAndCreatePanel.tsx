import React from "react";

// Imported from the domain module, not the `../../lib/api` barrel: several
// suites replace that barrel with a fixed object, so a named import from it
// would come back `undefined` there (`useBundleDetect.ts` documents the same
// rule).
import type { OperationUndo } from "../../lib/api/operations";
import { notifyInfo } from "../../lib/toast";
import OperationPanel from "../operations/OperationPanel";

type Props = {
  /** The storage-relative key whose top folders the split hoists. */
  bundleKey: string;
  /** The shape the operator chose; "" means the server's own default. */
  shape: string;
  /** The write gate: a viewer renders nothing and issues no request. */
  canContribute: boolean;
  /** The host's completion signal: refetch whatever the host owns. */
  onChanged?: () => void;
  /** Session expiry is the host's news, not this panel's. */
  onUnauthorized?: () => void;
};

/** The news an undo answers with: what it folded back, in the host's toast. */
function announceUndo(answer: OperationUndo): void {
  const removed = answer.projects_removed ?? [];
  notifyInfo("Split undone", removed.length > 0 ? { details: removed } : undefined);
}

/**
 * The split door, whole: the plan the server computed, ONE action labelled with
 * its outcome, and a popup that narrates the operation the action queued.
 *
 * The review, the state machine and the dialog all live in
 * `ui/operations/OperationPanel`, so this file is the SPLIT's door - its target,
 * its params and the news its undo answers with - and not a second
 * implementation. The server computes every step: the panel asks for a plan and
 * can only send that plan's `plan_id` back, and the moves are information the
 * operator reads rather than a ladder they have to climb.
 *
 * A viewer renders nothing at all: no wrapper, no layout track, no request.
 */
export default function SplitAndCreatePanel(props: Props): React.ReactElement | null {
  const { bundleKey, shape, canContribute, onChanged, onUnauthorized } = props;

  return (
    <OperationPanel
      workflow="split-and-create"
      target={bundleKey}
      params={shape ? { shape } : {}}
      canContribute={canContribute}
      onChanged={onChanged}
      onUnauthorized={onUnauthorized}
      dataPrefix="split"
      onUndo={announceUndo}
    />
  );
}
