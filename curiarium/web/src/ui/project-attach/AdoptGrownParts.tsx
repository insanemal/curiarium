import React from "react";

// Imported from the domain module, not the `../../lib/api` barrel: several
// suites replace that barrel with a fixed object, so a named import from it
// would come back `undefined` there (`useBundleDetect.ts` documents the same
// rule).
import type { Operation, OperationUndo } from "../../lib/api/operations";
import { notifyInfo } from "../../lib/toast";
import OperationPanel from "../operations/OperationPanel";

type Props = {
  /** The project whose folder grew; the server resolves the delta from its marker. */
  projectId: string;
  /** The host's completion signal: refetch the project, its members and the drift. */
  onChanged?: () => void;
  /** Session expiry is the host's news, not this control's. */
  onUnauthorized?: () => void;
  /**
   * Whether this control currently has a run to show. The drift notice that
   * hosts it is keyed on the drift COUNT, which the run itself drives to zero -
   * so without this the host would unmount the control the instant the adopt
   * finished, taking the result line and the undo button with it. `true` while a
   * submitted operation is on screen, `false` again once it has been undone.
   */
  onActiveChange?: (active: boolean) => void;
};

/** The news an undo answers with, in the host's toast. The member count is the
 *  server's own answer, so a leg that removed nothing says nothing extra. */
function announceUndo(answer: OperationUndo): void {
  notifyInfo(
    "Adopt undone",
    answer.members_removed
      ? { details: [`${answer.members_removed} files removed`] }
      : undefined,
  );
}

/**
 * The project page's grown-folder action: the drift notice's ONE action,
 * labelled with the outcome ("Add 171 files in 9 groups") and never with the
 * subsystem.
 *
 * The review, the state machine and the dialog live in
 * `ui/operations/OperationPanel`, so this file is the ADOPT's door - its target,
 * its words and its host contract - and not a second implementation. It submits
 * the server's plan, narrates the runner in the shared progress dialog and
 * offers the undo the server says is still meaningful.
 *
 * It does NOT replace the manual attach picker: that stays for choosing
 * individual files, while this action adopts the whole delta the marker names.
 */
export default function AdoptGrownParts(props: Props): React.ReactElement {
  const { projectId, onChanged, onUnauthorized, onActiveChange } = props;

  // The host hears only a TRANSITION: the panel reports the row on every change
  // (and on mount with `null`), so re-reporting the same answer would fight a
  // host that stores it as state.
  const activeRef = React.useRef<boolean | null>(null);

  // An undone operation has nothing left to offer, so it reports inactive and
  // the host may close the notice (the undo's own `onChanged` refetches the
  // drift, which brings the action back).
  function reportActive(operation: Operation | null): void {
    const active = operation !== null && operation.undo_state !== "undone";
    if (activeRef.current === active) return;
    activeRef.current = active;
    onActiveChange?.(active);
  }

  return (
    <OperationPanel
      workflow="adopt"
      target={projectId}
      canContribute
      onChanged={onChanged}
      onUnauthorized={onUnauthorized}
      dataPrefix="adopt"
      onUndo={announceUndo}
      onOperation={reportActive}
    />
  );
}
