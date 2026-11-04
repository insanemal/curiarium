import React, { useEffect, useId, useRef, useState, type ReactElement } from "react";
// The type comes from the domain module, not the `../../lib/api` barrel: App
// and AssetGrid tests replace that barrel with a fixed object, so a new named
// import from it would throw "No export is defined on the mock".
import type { ArchiveExtractResult } from "../../lib/api/imports";
import { tabbableIn, wrapTab } from "../../lib/focusTrap";
import ArchiveExtractAction from "./ArchiveExtractAction";

/**
 * What this dialog can Tab through.
 *
 * Deliberately not `DRAWER_TABBABLE_SELECTOR`: that one also carries `summary`,
 * `a[href]` and a roving tree row, none of which this dialog ever renders, so a
 * Tab would be allowed to wrap onto a member that does not exist.
 */
const DIALOG_TABBABLE_SELECTOR = 'input:not([tabindex="-1"]), button:not([tabindex="-1"])';

/** One archive to extract. `label` is what the user reads, `path` is what the API takes. */
export type ExtractTarget = { path: string; label: string };

type Props = {
  /** One target for a card kebab or a blocked row; several for the bulk bar. Never empty. */
  targets: ExtractTarget[];
  canContribute: boolean;
  /** Array-shaped so a batch is additive rather than a second code path. */
  onExtracted: (results: ArchiveExtractResult[]) => void;
  onClose: () => void;
};

function countLabel(count: number): string {
  return count === 1 ? "1 archive" : `${count} archives`;
}

/**
 * The confirmation surface for extracting one archive, or a queue of them,
 * beside themselves.
 *
 * It holds no extract logic of its own: every archive goes through
 * `ArchiveExtractAction`, which lists, confirms and extracts. For a batch this
 * component only walks the targets one at a time, because the action is a
 * self-contained per-archive control and running two at once would race on the
 * same destination check. It also means a failed archive keeps its own inline
 * error, and the batch moves on via `Skip this archive` instead of swallowing
 * it.
 */
export default function ArchiveExtractDialog({
  targets,
  canContribute,
  onExtracted,
  onClose,
}: Props): ReactElement {
  // A card kebab and the bulk bar can both open this dialog for the same
  // archive, so the ids naming it must be per-instance.
  const id = useId();
  const headingId = `${id}-heading`;
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const progressRef = useRef<HTMLParagraphElement | null>(null);
  const [index, setIndex] = useState(0);
  const [collected, setCollected] = useState<ArchiveExtractResult[]>([]);
  // The caller refetches on every report, so one dialog must report exactly
  // once - a close that races the last target must not double it.
  const reportedRef = useRef(false);

  const batch = targets.length > 1;
  const current = targets[index];

  function report(results: ArchiveExtractResult[]): void {
    if (reportedRef.current) return;
    reportedRef.current = true;
    onExtracted(results);
  }

  // Entry: focus a real control, not the container, so `wrapTab` can compare
  // the active element against the first and last tabbable member.
  useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    const first = tabbableIn(panel, DIALOG_TABBABLE_SELECTOR)[0] ?? overlayRef.current;
    first?.focus();
  }, []);

  // Recovery: advancing the batch remounts the keyed `ArchiveExtractAction`, so
  // the button that was just pressed leaves the DOM and focus drops to <body> -
  // which also kills the tab trap, because the key handler lives on the overlay
  // and <body> is its ancestor, not its descendant. Re-enter the dialog on the
  // new target without stealing a caret that is still inside it.
  useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    const active = document.activeElement;
    if (active instanceof HTMLElement && active !== document.body && panel.contains(active)) return;
    (progressRef.current ?? tabbableIn(panel, DIALOG_TABBABLE_SELECTOR)[0] ?? overlayRef.current)?.focus();
  }, [index]);

  function handleOne(result: ArchiveExtractResult): void {
    const all = [...collected, result];
    setCollected(all);
    if (index + 1 >= targets.length) {
      report(all);
      return;
    }
    setIndex(index + 1);
  }

  function skip(): void {
    if (index + 1 >= targets.length) {
      // Skipping the last one ends the batch with whatever the others gave;
      // an empty batch reports nothing at all.
      if (collected.length > 0) report(collected);
      return;
    }
    setIndex(index + 1);
  }

  function handleClose(): void {
    // A partial batch still reports its successes: closing early hands the
    // caller the archives that were extracted, because those files are already
    // on disk and its grid is already stale.
    if (collected.length > 0) report(collected);
    onClose();
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLDivElement>): void {
    if (event.key === "Escape") {
      // `stopPropagation` keeps the page-level Escape handlers (the drawer, the
      // landing view) out of it: closing this dialog must not also close the
      // surface that opened it.
      event.preventDefault();
      event.stopPropagation();
      handleClose();
      return;
    }
    if (event.key === "Tab") {
      const panel = panelRef.current;
      if (!panel) {
        event.preventDefault();
        return;
      }
      // The action's busy stages (`listing`, `extracting`) pin focus on their
      // own `tabindex="-1"` container: inside the panel, but not in the Tab
      // ring, so the anchor rules alone would let Tab walk out of this
      // `aria-modal` dialog. This dialog is the only caller opting in.
      wrapTab(panel, DIALOG_TABBABLE_SELECTOR, event, { wrapFromUnlistedFocus: true });
    }
  }

  // `countLabel` already says "2 archives", so the plural word must not be
  // repeated here: the heading is the dialog's accessible name.
  const heading = batch
    ? `Extract ${countLabel(targets.length)}`
    : `Extract ${current.label}`;

  let body: ReactElement;
  if (!canContribute) {
    // Below contributor the action renders nothing at all, so say why rather
    // than advertise a control the role cannot use.
    body = <p className="text-sm text-muted">Extracting archives needs contributor access.</p>;
  } else if (batch) {
    body = (
      <div className="flex flex-col gap-3">
        <p role="status" tabIndex={-1} ref={progressRef} className="text-sm text-muted">
          {`Archive ${index + 1} of ${targets.length} - ${current.label}`}
        </p>
        <p className="text-sm text-muted">
          Each archive is extracted into a new folder beside it; skipping one leaves it untouched.
        </p>
        <ArchiveExtractAction
          key={current.path}
          canContribute={canContribute}
          path={current.path}
          label={current.label}
          variant="detail"
          onExtracted={handleOne}
        />
        <div>
          <button
            type="button"
            className="px-3 py-2 rounded-md border border-panel-strong text-sm"
            onClick={skip}
          >
            Skip this archive
          </button>
        </div>
      </div>
    );
  } else {
    body = (
      <div className="flex flex-col gap-3">
        <p className="text-sm text-muted">
          Extract this archive into a new folder beside it. The library scan registers the files
          afterwards.
        </p>
        <ArchiveExtractAction
          canContribute={canContribute}
          path={current.path}
          label={current.label}
          variant="detail"
          // Routed through `report` so a second extract, or a close that races
          // one, cannot report twice.
          onExtracted={result => report([result])}
        />
      </div>
    );
  }

  return (
    <div
      ref={overlayRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={headingId}
      onKeyDown={handleKeyDown}
      // Focusable, so a click on the backdrop lands on the dialog surface - the
      // nearest focusable ancestor of the click - instead of <body>. Without
      // this the panel's key handler never sees the Escape that follows.
      tabIndex={-1}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4"
    >
      <div
        ref={panelRef}
        className="bg-panel-strong rounded-lg shadow-2xl max-w-3xl w-full max-h-full overflow-hidden flex flex-col p-4 gap-3"
      >
        <div className="flex items-center justify-between gap-3">
          <h2 id={headingId} className="text-lg font-semibold">
            {heading}
          </h2>
          <button
            type="button"
            className="px-3 py-1 rounded-md border border-panel-strong text-sm"
            onClick={handleClose}
          >
            Close
          </button>
        </div>
        {body}
      </div>
    </div>
  );
}
