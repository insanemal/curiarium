import { useEffect, useRef, useState, type ReactElement } from "react";
import {
  extractArchive,
  listArchiveEntries,
  type ArchiveEntry,
  type ArchiveExtractResult,
} from "../../lib/api";
import { tabbableIn } from "../../lib/focusTrap";
import { formatBytes } from "../../lib/format";

type Props = {
  /** Contribute capability: extraction writes new bytes into the library. Required. */
  canContribute: boolean;
  /** Library-relative path of the archive, e.g. "Moai.zip". */
  path: string;
  /** Human label for the accessible names, e.g. "Moai.zip". */
  label: string;
  /** "detail" for the asset preview footer, "row" for a compact settings-card row. */
  variant?: "detail" | "row";
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
  onExtracted,
}: Props): ReactElement | null {
  const [stage, setStage] = useState<Stage>("idle");
  const [entries, setEntries] = useState<ArchiveEntry[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const containerRef = useRef<HTMLDivElement | null>(null);
  const previousStageRef = useRef<Stage | null>(null);

  const button = variant === "row" ? ROW_BUTTON : DETAIL_BUTTON;
  const primaryButton = `${button} bg-accent hover:bg-accent-strong`;

  function fail(err: unknown): void {
    setStage("error");
    setError(err instanceof Error && err.message.trim() ? err.message : "Extraction failed");
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
      fail(err);
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
      fail(err);
    }
  }

  function cancel(): void {
    setEntries([]);
    setStage("idle");
  }

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
    return (
      // tabindex="-1": programmatically focusable, never in a trap's Tab cycle.
      <div className="flex flex-col gap-2" ref={containerRef} tabIndex={-1}>
        <p className="text-sm text-muted">
          {`${countLabel(entries.length)}, ${formatBytes(totalBytes(entries))} — extract into a new folder beside the archive?`}
        </p>
        <div className="flex gap-2">
          <button
            type="button"
            className={primaryButton}
            disabled={extracting}
            onClick={() => void confirmExtract()}
          >
            {extracting ? "Extracting..." : `Extract ${countLabel(entries.length)}`}
          </button>
          <button type="button" className={button} disabled={extracting} onClick={cancel}>
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
        disabled={stage === "listing"}
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
