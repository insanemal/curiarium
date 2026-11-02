/**
 * The Tab cycle shared by the modal surfaces that contain one.
 *
 * The member list is the set of controls a browser would actually stop on,
 * which is not the same as "every element the selector matches": a `<summary>`
 * is natively tabbable while the menu inside its closed `<details>` is present
 * and focusable in jsdom but unreachable in a browser, and a non-roving
 * `[role="treeitem"]` row is `tabindex="-1"`. Filtering only by selector would
 * make a trap look correct in jsdom while Tab walked straight out of an
 * `aria-modal` sheet in a browser.
 */

/**
 * Everything the folder drawer cycles through, in DOM order. The tree
 * contributes only its single roving `[tabindex="0"]` row (APG one-tab-stop);
 * the other rows are deliberately excluded because they are not natively
 * tabbable.
 */
export const DRAWER_TABBABLE_SELECTOR =
  'input:not([tabindex="-1"]), button:not([tabindex="-1"]), summary:not([tabindex="-1"]), a[href]:not([tabindex="-1"]), [role="treeitem"][tabindex="0"]';

/** The only fields of a keyboard event `wrapTab` reads. */
export type TrapTabEvent = {
  defaultPrevented: boolean;
  shiftKey: boolean;
  preventDefault: () => void;
};

/** A `<details>`' own disclosure control: its first direct-child `<summary>`. */
function ownerSummary(details: Element): Element | null {
  for (const child of Array.from(details.children)) {
    if (child.tagName === "SUMMARY") return child;
  }
  return null;
}

/**
 * Whether a closed `<details>` hides `element`.
 *
 * The whole ancestor chain is walked, not just the nearest closed details: a
 * summary nested inside a second closed details is hidden by the outer one
 * even though the inner details' own summary would not hide it, and content
 * rendered inside a summary stays visible while that details is closed.
 */
export function hiddenByClosedDetails(element: Element): boolean {
  let ancestor = element.parentElement;
  while (ancestor) {
    if (ancestor.tagName === "DETAILS" && (ancestor as HTMLDetailsElement).open === false) {
      const summary = ownerSummary(ancestor);
      const visible = summary !== null && (summary === element || summary.contains(element));
      if (!visible) return true;
    }
    ancestor = ancestor.parentElement;
  }
  return false;
}

/** The natively tabbable members of `root`, in document order. */
export function tabbableIn(root: Element, selector: string): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(selector)).filter(
    element => element.matches(":not([disabled])") && !hiddenByClosedDetails(element),
  );
}

/**
 * Manual wrap: focus must never leave the surface, even when it is nested
 * inside another one. `FolderPicker` owns this exact semantics; the drawer
 * reuses it with its own selector.
 */
export function wrapTab(root: Element, selector: string, event: TrapTabEvent): void {
  // A nested surface (the Sidebar's editor and its FolderPicker) owns Tab while
  // it is open: it preventDefaults, so the outer trap must not wrap as well.
  if (event.defaultPrevented) return;
  const items = tabbableIn(root, selector);
  if (items.length === 0) {
    event.preventDefault();
    return;
  }
  const first = items[0];
  const last = items[items.length - 1];
  const active = document.activeElement;
  const inside = root.contains(active);
  if (event.shiftKey) {
    if (!inside || active === first) {
      event.preventDefault();
      last.focus();
    }
    return;
  }
  if (!inside || active === last) {
    event.preventDefault();
    first.focus();
  }
}
