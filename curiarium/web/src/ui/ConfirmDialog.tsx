import type { KeyboardEvent, ReactElement, ReactNode } from "react";

/**
 * A deferred confirmation: the grid stores one of these and renders a single
 * `<ConfirmDialog>` for it, so cards never own dialog state of their own.
 * `body` may be a node when the caller needs more than one paragraph.
 */
export type ConfirmRequest = {
  title: string;
  body?: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  run: () => void | Promise<void>;
};

type Props = {
  title: string;
  body?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  busy?: boolean;
  /**
   * Refuses the confirm button without hiding it, for a destructive action that
   * is not allowed right now. Focus moves to Cancel in that case: leaving it on
   * a disabled button would drop the keyboard user out of the dialog.
   */
  confirmDisabled?: boolean;
  /** Shows what the busy state is doing; defaults to "Working...". */
  busyLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
};

export default function ConfirmDialog({
  title,
  body,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  danger = false,
  busy = false,
  confirmDisabled = false,
  busyLabel = "Working...",
  onConfirm,
  onCancel,
}: Props): ReactElement {
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.key !== "Escape") return;
    // An in-tree `onKeyDown` runs before the document bubble listener, so
    // `defaultPrevented` is how this aria-modal surface claims the key, busy or not.
    event.preventDefault();
    if (busy) return;
    onCancel();
  };

  const confirmClasses = danger
    ? "bg-red-600 text-white"
    : "bg-accent hover:bg-accent-strong";

  // A caller passes text for the common one-line body and a node when the body
  // is a run of paragraphs; wrapping a node in a `<p>` would nest paragraphs.
  const bodyIsText = typeof body === "string" || typeof body === "number";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-dialog-title"
      onKeyDown={handleKeyDown}
    >
      <div className="bg-panel-strong rounded-lg shadow-2xl w-full max-w-lg flex flex-col">
        <div className="border-b border-panel px-4 py-3">
          <h2 id="confirm-dialog-title" className="text-lg font-semibold">
            {title}
          </h2>
        </div>

        {body && (
          <div className="px-4 py-3 text-sm">
            {bodyIsText ? <p>{body}</p> : body}
          </div>
        )}

        <div className="flex items-center justify-end gap-2 border-t border-panel px-4 py-3">
          <button
            type="button"
            className="px-3 py-2 rounded-md border border-panel-strong text-sm disabled:opacity-60"
            onClick={onCancel}
            disabled={busy}
            autoFocus={confirmDisabled}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`px-3 py-2 rounded-md text-sm disabled:opacity-60 ${confirmClasses}`}
            onClick={onConfirm}
            disabled={busy || confirmDisabled}
            autoFocus={!confirmDisabled}
          >
            {busy ? busyLabel : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
