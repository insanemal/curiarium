import { useState, type ReactElement } from "react";
import {
  extractArchive,
  listArchiveEntries,
  type ArchiveEntry,
  type ArchiveExtractResult,
} from "../../lib/api";
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

  // Every control here writes into the library, so a role below contributor gets
  // no control at all rather than a disabled button advertising a capability it
  // lacks. The early return sits after every hook so the hook order never changes.
  if (!canContribute) return null;

  if (stage === "confirm" || stage === "extracting") {
    const extracting = stage === "extracting";
    return (
      <div className="flex flex-col gap-2">
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
    <div className="flex flex-col gap-1">
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
