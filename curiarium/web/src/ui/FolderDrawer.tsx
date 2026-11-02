import { useEffect, useRef, type ReactElement, type ReactNode } from "react";

import { DRAWER_TABBABLE_SELECTOR, wrapTab } from "../lib/focusTrap";
import type { LibraryMode } from "../lib/mode";

type Props = {
  /** The rail's DOM id, so the header toggle can point `aria-controls` at it. */
  id: string;
  mode: LibraryMode;
  narrow: boolean;
  open: boolean;
  onClose: () => void;
  /** The folder tree. Its only child, so the rail node is never remounted. */
  children: ReactNode;
};

/**
 * The one rail node, at every width.
 *
 * Below the breakpoint the same element becomes a full-height sheet: it is the
 * dialog, it traps Tab and it answers Escape. Above it the element is the plain
 * column it always was, so nothing about the wide layout changes. Rendering one
 * stable `div.library-rail` (instead of a wide rail plus a narrow sheet) is what
 * keeps the tree's search, expansion and roving selection from being remounted
 * or duplicated when the viewport crosses the breakpoint.
 *
 * The key trap lives on the document, not on the rail node: a keydown only
 * bubbles to a div while focus is already inside it, and two reachable paths
 * leave focus on `<body>` while the sheet is `aria-modal` - the Sidebar
 * editor's name input unmounting after its own Escape, and a pointer click on
 * the non-focusable scrim. `<body>` is an ancestor of the React root, so a
 * div-level handler never sees those keys at all. The listener exists only
 * while `narrow && open`, so wide and closed states stay exactly as they were.
 */
export default function FolderDrawer({
  id,
  mode,
  narrow,
  open,
  onClose,
  children,
}: Props): ReactElement {
  const rootRef = useRef<HTMLDivElement | null>(null);
  /** Previous `open`, so focus moves only on the closed -> open transition. */
  const wasOpenRef = useRef(false);

  useEffect(() => {
    const wasOpen = wasOpenRef.current;
    wasOpenRef.current = open;
    // A width change must never re-focus: only an actual closed -> open does,
    // so a narrow -> wide -> narrow cycle cannot steal focus from the page.
    if (!open || wasOpen) return;
    const root = rootRef.current;
    if (!root) return;
    const roving = root.querySelector<HTMLElement>('[role="treeitem"][tabindex="0"]');
    (roving ?? root).focus();
  }, [open]);

  useEffect(() => {
    // One owner, and only while the sheet is actually modal: at wide the rail
    // is an ordinary column and must not swallow Escape or Tab.
    if (!narrow || !open) return;

    // Escape is handled in the CAPTURE phase, Tab in the BUBBLE phase. The
    // split is forced, not stylistic:
    //
    // React 18 flushes discrete events (keydown) synchronously at the end of
    // its root dispatch, and the root container is a descendant of `document`.
    // A bubble-phase listener on `document` therefore runs AFTER React has
    // already applied the state update, so the Sidebar editor's unmount is in
    // the DOM and the nested-dialog guard below would see no dialog - the same
    // Escape would then close the editor and the sheet together. `document` is
    // an ancestor of the root container, so a capture-phase listener provably
    // runs BEFORE any React handler can mutate the DOM. That is a DOM-spec
    // ordering guarantee, not a React timing assumption: the guard always
    // inspects pre-dispatch DOM, in jsdom and in every browser.
    function handleEscape(event: globalThis.KeyboardEvent): void {
      if (event.key !== "Escape") return;
      if (event.defaultPrevented) return;
      // Sidebar's own create/edit editor is a dialog too, and its handlers do
      // not preventDefault: without this guard one Escape would close the
      // editor and the sheet together. The nested FolderPicker keeps its own
      // Escape for the same reason - its panel carries `role="dialog"` and is a
      // descendant of the rail, so this guard bails before its stopPropagation
      // could ever matter.
      if (rootRef.current?.querySelector('[role="dialog"]')) return;
      event.preventDefault();
      onClose();
    }

    // Tab stays on the bubble phase on purpose: it must run after the nested
    // FolderPicker's own React handler has handled and preventDefault()ed the
    // key, which `wrapTab` observes via `defaultPrevented`. Capturing Tab would
    // steal it from the nested surface.
    function handleTab(event: globalThis.KeyboardEvent): void {
      if (event.key !== "Tab") return;
      const root = rootRef.current;
      if (!root) return;
      wrapTab(root, DRAWER_TABBABLE_SELECTOR, event);
    }

    document.addEventListener("keydown", handleEscape, true);
    document.addEventListener("keydown", handleTab);
    return () => {
      document.removeEventListener("keydown", handleEscape, true);
      document.removeEventListener("keydown", handleTab);
    };
  }, [narrow, open, onClose]);

  let drawerState: "open" | "closed-narrow" | "hidden-mode";
  if (mode !== "files") drawerState = "hidden-mode";
  else if (narrow && !open) drawerState = "closed-narrow";
  else drawerState = "open";

  const isDialog = narrow && open;

  return (
    <div
      id={id}
      ref={rootRef}
      className="library-rail"
      // One attribute, two reasons: Projects mode hides the rail at every width,
      // while the narrow layout hides it until the sheet is opened.
      // `data-drawer-state` keeps the two distinguishable in the DOM.
      hidden={mode !== "files" || (narrow && !open)}
      data-drawer-state={drawerState}
      // The focus fallback: an empty tree has no roving row to land on.
      tabIndex={-1}
      role={isDialog ? "dialog" : undefined}
      aria-modal={isDialog ? "true" : undefined}
      aria-label={isDialog ? "Library folders" : undefined}
    >
      {children}
    </div>
  );
}
