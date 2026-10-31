/**
 * Copy text with the async clipboard API where the browser has it, and with a
 * hidden textarea + document.execCommand where it does not: LAN deployments are
 * served over plain http, where `navigator.clipboard` is undefined because the
 * context is not secure. Throws when neither path works, so the caller can toast
 * a failure instead of pretending it copied.
 */
export async function copyText(text: string): Promise<void> {
  const clipboard: Clipboard | undefined = navigator.clipboard;
  if (clipboard?.writeText) {
    try {
      await clipboard.writeText(text);
      return;
    } catch {
      // the API exists but refused the write (permissions, focus): try the legacy path
    }
  }
  if (copyWithTextarea(text)) return;
  throw new Error("Clipboard is not available");
}

/** The pre-async-copy path: `execCommand("copy")` on a selected, off-screen textarea. */
function copyWithTextarea(text: string): boolean {
  if (typeof document.execCommand !== "function") return false;

  const selection = document.getSelection();
  const previousRange = selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null;

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.readOnly = true;
  textarea.style.position = "fixed";
  textarea.style.top = "-1000px";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  try {
    textarea.select();
    return document.execCommand("copy");
  } finally {
    document.body.removeChild(textarea);
    if (selection && previousRange) {
      selection.removeAllRanges();
      selection.addRange(previousRange);
    }
  }
}
