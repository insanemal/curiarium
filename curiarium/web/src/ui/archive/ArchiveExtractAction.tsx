import { useEffect, useRef, useState, type ReactElement } from "react";
import {
  extractArchive,
  listArchiveEntries,
  type ArchiveEntry,
  type ArchiveExtractResult,
} from "../../lib/api";
import { tabbableIn } from "../../lib/focusTrap";
import { formatBytes } from "../../lib/format";
import { notifyError } from "../../lib/toast";
import { archivePlanLines, planArchive } from "./archivePlan";

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

/** `confirm` and `extracting` share the confirmation panel. */
type Stage = "idle" | "listing" | "confirm" | "extracting" | "done" | "error";

const DETAIL_BUTTON = "px-3 py-2 rounded-md border border-panel-strong text-sm disabled:opacity-60";
// A compact bordered row button, for a dense list row or a settings card.
const ROW_BUTTON =
  "text-xs px-2 py-1 rounded-md border border-panel-strong self-start disabled:opacity-60";

/**
 * What this action can Tab through: only buttons, so `DRAWER_TABBABLE_SELECTOR`
 * (which also carries `summary` and `a[href]`) would let a Tab wrap onto a
 * member this control never renders.
 */
const ACTION_TABBABLE_SELECTOR = 'button:not([tabindex="-1"])';

/**
 * The stage's one focus home: its first enabled control, or the container
 * itself while every control in the stage is disabled (`listing`,
 * `extracting`) - the same fallback `ZipImportModal` uses for its overlay.
 * A disabled control is not a home: it refuses focus, and a browser has
 * already blurred it.
 */
function focusActionHome(container: HTMLDivElement): void {
  (tabbableIn(container, ACTION_TABBABLE_SELECTOR)[0] ?? container).focus();
}

function countLabel(count: number): string {
  return count === 1 ? "1 file" : `${count} files`;
}

function doneLine(result: ArchiveExtractResult): string {
  // Two different nothings. This one is a destination that was already there;
  // the one below is an archive whose entries all turned out to be junk.
  if (result.already_present) return "Already extracted - nothing to do.";
  // Nothing was written, so no scan can register it: claiming one would lie.
  if (result.files === 0) return "Nothing was written - the archive held no extractable files.";
  const scan = result.scan_started
    ? "A library scan has started."
    : "A library scan is already running.";
  return `Extracted ${countLabel(result.files)}. ${scan}`;
}

// Out of the aside a failure has no heading of its own, so the toast must name
// the archive itself: `err.message` names it only for the 404/twin-name cases,
// never for 409/413/invalid-zip/network.
function extractFailureLine(path: string, message: string): string {
  return `Extract ${path} failed: ${message}`;
}

function listingFailureLine(path: string, message: string): string {
  return `Could not list ${path}: ${message}`;
}

function totalBytes(entries: ArchiveEntry[]): number {
  return entries.reduce((sum, entry) => sum + entry.size, 0);
}

/**
 * Extracts a `.zip` that is already in the library, beside itself. Self-driving:
 * it lists the archive's entries on click, asks for confirmation with the count
 * and size, then extracts the whole archive and reports what the server did.
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
  const [stage, setStage] = useState<Stage>("idle");
  const [entries, setEntries] = useState<ArchiveEntry[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const containerRef = useRef<HTMLDivElement | null>(null);
  const previousStageRef = useRef<Stage | null>(null);
  const aliveRef = useRef(true);
  // A failure scheduled by a request that no longer has a home.
  const owedRef = useRef("");

  const button = variant === "row" ? ROW_BUTTON : DETAIL_BUTTON;
  const primaryButton = `${button} bg-accent hover:bg-accent-strong`;

  function fail(err: unknown, line: (message: string) => string): void {
    const message = err instanceof Error && err.message.trim() ? err.message : "Extraction failed";
    const report = line(message);
    if (!aliveRef.current) {
      notifyError(report);
      return;
    }
    owedRef.current = report;
    setStage("error");
    setError(message);
  }

  async function startListing(): Promise<void> {
    setStage("listing");
    setError("");
    setMessage("");
    try {
      const result = await listArchiveEntries(path);
      setEntries(result.entries);
      setStage("confirm");
    } catch (err) {
      fail(err, message => listingFailureLine(path, message));
    }
  }

  async function confirmExtract(): Promise<void> {
    setStage("extracting");
    try {
      // Whole archive: this control never picks entries.
      const result = await extractArchive({ path, entries: null });
      setStage("done");
      setMessage(doneLine(result));
      onExtracted?.(result);
    } catch (err) {
      fail(err, message => extractFailureLine(path, message));
    }
  }

  function cancel(): void {
    setEntries([]);
    setStage("idle");
  }

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      // A failure only this instance ever knew about, handed to the toast
      // because the catch had nowhere live to paint it.
      if (owedRef.current) {
        notifyError(owedRef.current);
        owedRef.current = "";
      }
    };
  }, []);

  // No dependency array on purpose: whatever this instance got as far as
  // committing is on screen, so nothing is still owed. Do NOT key this on
  // [stage]/[error] - a second failure whose setStage/setError is a state no-op
  // would not re-run a keyed effect, leaving owedRef set while the identical
  // line was already painted, and the unmount would then toast a message the
  // operator already read.
  useEffect(() => {
    owedRef.current = "";
  });

  // Recovery: every stage advance swaps this control's own controls, so the
  // button that was just pressed leaves the DOM - or is still there with
  // `disabled` on it, which a browser blurs and jsdom does not - and focus
  // drops to <body>. <body> is outside the enclosing dialog's Tab trap and out
  // of reach of its Escape handler, so re-enter on the new stage's first real
  // control. A caret the user moved themselves, anywhere in the document, is
  // left alone; the initial mount is skipped because this action also mounts in
  // surfaces that own no focus of their own (a bulk-dialog batch remount, an
  // aside), where grabbing it would yank a caret the user never lost.
  useEffect(() => {
    const previous = previousStageRef.current;
    previousStageRef.current = stage;
    if (previous === null || previous === stage) return;
    const container = containerRef.current;
    if (!container) return; // Below contributor nothing at all is rendered.
    const active = document.activeElement;
    const holdsLiveCaret =
      active instanceof HTMLElement &&
      active !== document.body &&
      // Our own container is only ever this effect's busy home, never a caret
      // a user chose: leaving focus there would lose listing -> error.
      active !== container &&
      active.isConnected &&
      !active.hasAttribute("disabled");
    if (holdsLiveCaret) return;
    focusActionHome(container);
  }, [stage]);

  // Every control here writes into the library, so a role below contributor gets
  // no control at all rather than a disabled button advertising a capability it
  // lacks. The early return sits after every hook so the hook order never changes.
  if (!canContribute) return null;

  if (stage === "confirm" || stage === "extracting") {
    const extracting = stage === "extracting";
    // The listing was already read to write this sentence, so the operator can be
    // told what the archive actually holds before committing: how many of the
    // entries are models, and whether the models are only inside a nested archive
    // (Mf-0ck5 - the surprise that started the ticket was an "Extract" that
    // released a `.zip` and a row named after it, not the tower).
    const plan = archivePlanLines(planArchive(entries));
    return (
      // tabindex="-1": programmatically focusable, never in a trap's Tab cycle.
      <div className="flex flex-col gap-2" ref={containerRef} tabIndex={-1}>
        <p className="text-sm text-muted">
          {`${countLabel(entries.length)}, ${formatBytes(totalBytes(entries))} — extract into a new folder beside the archive?`}
        </p>
        {plan.map(line => (
          <p key={line} className="text-sm text-muted" data-archive-plan="true">
            {line}
          </p>
        ))}
        <div className="flex gap-2">
          <button
            type="button"
            className={primaryButton}
            disabled={disabled || extracting}
            onClick={() => void confirmExtract()}
          >
            {extracting ? "Extracting..." : `Extract ${countLabel(entries.length)}`}
          </button>
          <button
            type="button"
            className={button}
            disabled={disabled || extracting}
            onClick={cancel}
          >
            Cancel
          </button>
        </div>
      </div>
    );
  }

  return (
    // tabindex="-1": programmatically focusable, never in a trap's Tab cycle.
    <div className="flex flex-col gap-1" ref={containerRef} tabIndex={-1}>
      <button
        type="button"
        aria-label={`Extract ${label}`}
        className={primaryButton}
        disabled={disabled || stage === "listing"}
        onClick={() => void startListing()}
      >
        {stage === "listing" ? "Listing..." : "Extract archive"}
      </button>
      {stage === "done" && (
        <p role="status" aria-live="polite" className="text-sm text-muted">
          {message}
        </p>
      )}
      {stage === "error" && <p className="text-sm text-red-400">{error}</p>}
    </div>
  );
}
