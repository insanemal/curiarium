import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type ReactElement,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";

export type CardMenuItem = {
  label: string;
  onSelect: () => void;
  disabled?: boolean;
  danger?: boolean;
};

export type CardMenuSection = {
  label?: string;
  items: CardMenuItem[];
};

export type CardMenuProps = {
  label: string;
  sections: CardMenuSection[];
};

const MENU_WIDTH = 224;
const MENU_GAP = 4;
const VIEWPORT_MARGIN = 8;
const TYPEAHEAD_RESET_MS = 500;

/** The trigger edges the placement is computed against. */
type MenuAnchor = { top: number; bottom: number; right: number };

type MenuPlacement = { top: number; left: number; maxHeight?: number };

type Viewport = { width: number; height: number };

/** First enabled item, or the first item when every item is disabled. */
function firstEnabledIndex(items: CardMenuItem[]): number {
  const index = items.findIndex(item => !item.disabled);
  return index >= 0 ? index : 0;
}

function enabledIndices(items: CardMenuItem[]): number[] {
  return items.reduce<number[]>((indices, item, index) => {
    if (!item.disabled) indices.push(index);
    return indices;
  }, []);
}

/** Next enabled item, wrapping around; a no-op when nothing is enabled. */
function stepIndex(items: CardMenuItem[], active: number, direction: 1 | -1): number {
  const enabled = enabledIndices(items);
  if (!enabled.length) return active;
  if (direction > 0) {
    return enabled.find(index => index > active) ?? enabled[0];
  }
  const previous = [...enabled].reverse().find(index => index < active);
  return previous ?? enabled[enabled.length - 1];
}

/** First enabled label with `prefix`, or -1 when nothing matches. */
function firstMatch(items: CardMenuItem[], prefix: string): number {
  return items.findIndex(item => !item.disabled && item.label.toLowerCase().startsWith(prefix));
}

/** First enabled prefix match after `from`, wrapping; stops after one full lap. */
function nextMatchAfter(items: CardMenuItem[], prefix: string, from: number): number {
  for (let step = 1; step <= items.length; step += 1) {
    const index = (from + step) % items.length;
    const item = items[index];
    if (item && !item.disabled && item.label.toLowerCase().startsWith(prefix)) return index;
  }
  return -1;
}

/** Clamp a vertical offset so the menu, or its cap, stays inside the margins. */
function clampTop(top: number, menuHeight: number, maxHeight: number | undefined, viewport: Viewport): number {
  const visibleHeight = Math.min(menuHeight, maxHeight ?? menuHeight);
  const limit = Math.max(VIEWPORT_MARGIN, viewport.height - VIEWPORT_MARGIN - visibleHeight);
  return Math.min(Math.max(VIEWPORT_MARGIN, top), limit);
}

/**
 * Where to put a `position: fixed` menu for a trigger with edges `anchor`.
 *
 * Horizontal: right-align to the trigger, then clamp into the viewport, so a
 * card at the right edge cannot push the menu off-screen. The width is always
 * `MENU_WIDTH`; measuring it would buy nothing but a reflow.
 *
 * Vertical: below the trigger when the menu fits there, else above, and when
 * neither side fits whole, the larger ONE-SIDED gap with the menu capped to it.
 * The cap is deliberately never the whole viewport outside the degenerate guard
 * below: the menu has to sit wholly on one side of its trigger. A menu allowed
 * to cover the trigger would fall inside the outside-mousedown exemption (the
 * `handlePointerDown` effect lets a mousedown inside the menu or on the trigger
 * through), so clicking the trigger would no longer close the menu.
 */
function placeMenu(anchor: MenuAnchor, menuHeight: number, viewport: Viewport): MenuPlacement {
  const left = Math.min(
    Math.max(VIEWPORT_MARGIN, anchor.right - MENU_WIDTH),
    Math.max(VIEWPORT_MARGIN, viewport.width - VIEWPORT_MARGIN - MENU_WIDTH),
  );

  const belowTop = anchor.bottom + MENU_GAP;
  const aboveTop = anchor.top - MENU_GAP - menuHeight;
  const bottomLimit = viewport.height - VIEWPORT_MARGIN;

  if (belowTop + menuHeight <= bottomLimit) {
    return { top: clampTop(belowTop, menuHeight, undefined, viewport), left };
  }
  if (aboveTop >= VIEWPORT_MARGIN) {
    return { top: clampTop(aboveTop, menuHeight, undefined, viewport), left };
  }

  const gapBelow = bottomLimit - belowTop;
  const gapAbove = anchor.top - MENU_GAP - VIEWPORT_MARGIN;

  // Degenerate: the trigger itself sits outside the margins, so neither side
  // offers a gap to cap to. Use the whole viewport and let the clamp place it.
  if (Math.max(gapBelow, gapAbove) <= 0) {
    const maxHeight = viewport.height - 2 * VIEWPORT_MARGIN;
    return { top: clampTop(VIEWPORT_MARGIN, menuHeight, maxHeight, viewport), left, maxHeight };
  }

  // Below wins ties: the menu opens the way the trigger points.
  const placeBelow = gapBelow >= gapAbove;
  const maxHeight = placeBelow ? gapBelow : gapAbove;
  const top = placeBelow ? belowTop : anchor.top - MENU_GAP - maxHeight;
  return { top: clampTop(top, menuHeight, maxHeight, viewport), left, maxHeight };
}

/** The viewport the placement is computed against. */
function viewportOf(): Viewport {
  return { width: window.innerWidth, height: window.innerHeight };
}

/** Whether two placements put the menu in the same place. */
function samePlacement(a: MenuPlacement, b: MenuPlacement): boolean {
  return a.top === b.top && a.left === b.left && a.maxHeight === b.maxHeight;
}

type CardMenuPanelProps = {
  label: string;
  sections: CardMenuSection[];
  anchor: MenuAnchor;
  triggerRef: RefObject<HTMLButtonElement | null>;
  onClose: () => void;
};

function CardMenuPanel({
  label,
  sections,
  anchor,
  triggerRef,
  onClose,
}: CardMenuPanelProps): ReactElement {
  const menuRef = useRef<HTMLDivElement | null>(null);
  // The placement for a menu that has not been measured yet (height 0). The
  // layout effect below corrects it before the browser paints, so it is never
  // visible; it exists so a menu that fits costs no extra commit, and so the
  // positioning arithmetic has one home.
  const [placement, setPlacement] = useState<MenuPlacement>(() => placeMenu(anchor, 0, viewportOf()));
  const flatItems = sections.flatMap(section => section.items);
  const [activeIndex, setActiveIndex] = useState(() => firstEnabledIndex(flatItems));
  const typeaheadRef = useRef({ buffer: "", lastAt: 0, timer: null as ReturnType<typeof setTimeout> | null });
  const onCloseRef = useRef(onClose);
  // Latest-ref sync belongs in a layout effect (the house `useStableCallback`
  // does the same): writing it during render is unsound under StrictMode.
  useLayoutEffect(() => {
    onCloseRef.current = onClose;
  });

  const focusItem = (index: number) => {
    const button = menuRef.current?.querySelector<HTMLButtonElement>(`[data-menu-item-index="${index}"]`);
    button?.focus();
  };

  // Opening focuses the active item; arrow keys move focus directly afterwards.
  useEffect(() => {
    const button = menuRef.current?.querySelector<HTMLButtonElement>(
      `[data-menu-item-index="${firstEnabledIndex(flatItems)}"]`,
    );
    button?.focus();
    // The list never changes while the menu is open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The menu is measured exactly once, on mount, and the seed is corrected here:
  // a layout effect's setState is flushed before paint, so the seed is never
  // visible. Deliberately NOT re-measured on scroll or resize, because both close
  // the menu (the virtualiser would otherwise recycle the card under a live menu)
  // and the sections are static for the life of one open.
  useLayoutEffect(() => {
    const menu = menuRef.current;
    if (!menu) return;
    const measured = placeMenu(anchor, menu.getBoundingClientRect().height, viewportOf());
    // Same place: no state update, so opening a menu that fits below the trigger
    // stays one commit (jsdom reports a zero-height menu, and `AssetGrid.test.tsx`
    // counts commits). A menu that does not fit commits once more, before paint.
    // The closure's `placement` is the seed because this effect runs once, at mount.
    if (!samePlacement(placement, measured)) setPlacement(measured);
    // The anchor is fixed for the life of one open, like the item list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Both a click inside the menu and the trigger itself are handled locally.
  useEffect(() => {
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (target && (menuRef.current?.contains(target) || triggerRef.current?.contains(target))) return;
      onCloseRef.current();
    };
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [triggerRef]);

  // The typeahead buffer's timer outlives a row that scrolls away, so drop it
  // with the menu rather than letting it fire into an unmounted component.
  useEffect(() => () => {
    const timer = typeaheadRef.current.timer;
    if (timer) clearTimeout(timer);
  }, []);

  // The position is measured once, on mount, and deliberately never
  // re-measured: a scroll or a resize moves the trigger out from under a
  // `position: fixed` menu, so the menu closes instead of floating away from its
  // card (the virtualiser would otherwise recycle the card under a live menu).
  // The capture-phase listener catches the grid's own scroll container too.
  // The menu's own internal scroll is exempt, because it moves nothing the menu
  // is anchored to.
  useEffect(() => {
    const close = (event: Event) => {
      // A scroll INSIDE the capped menu is the menu's own: it moves nothing the
      // menu is anchored to, so it must not close the menu (the same exemption the
      // mousedown handler above makes for clicks inside it). A scroll that moves
      // the trigger, and any resize, still closes.
      const target = event.target;
      if (target instanceof Node && menuRef.current?.contains(target)) return;
      onCloseRef.current();
    };
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, []);

  const moveActive = (index: number) => {
    setActiveIndex(index);
    focusItem(index);
  };

  const selectItem = (item: CardMenuItem) => {
    if (item.disabled) return;
    // Close first so a menu item that opens the dialog does not fight the
    // dialog for focus; the post-close focus return is guarded.
    onCloseRef.current();
    item.onSelect();
  };

  /**
   * Typeahead over item labels, cycling rather than filtering.
   *
   * A DIFFERENT character appends to the buffer and restarts the search from the
   * top; a REPEATED character restarts the buffer at that character and advances
   * from `activeIndex + 1`, wrapping, so "dd" walks the d-matches instead of
   * building the unmatchable "dd". A lone match therefore re-selects itself (an
   * idempotent no-op, not a dead end), and no match leaves `activeIndex` alone.
   * This mirrors `runTypeahead` in `ui/FolderPicker.tsx`, the shipping reference
   * for the rule; keep the two in step.
   */
  const runTypeahead = (key: string) => {
    const state = typeaheadRef.current;
    const now = Date.now();
    const lower = key.toLowerCase();
    const withinWindow = now - state.lastAt <= TYPEAHEAD_RESET_MS;
    const repeated = withinWindow && state.buffer.endsWith(lower);
    state.buffer = `${withinWindow && !repeated ? state.buffer : ""}${lower}`;
    state.lastAt = now;
    if (state.timer) clearTimeout(state.timer);
    state.timer = setTimeout(() => {
      state.buffer = "";
    }, TYPEAHEAD_RESET_MS);

    const match = repeated
      ? nextMatchAfter(flatItems, state.buffer, activeIndex)
      : firstMatch(flatItems, state.buffer);
    if (match >= 0) moveActive(match);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        moveActive(stepIndex(flatItems, activeIndex, 1));
        break;
      case "ArrowUp":
        event.preventDefault();
        moveActive(stepIndex(flatItems, activeIndex, -1));
        break;
      case "Home": {
        event.preventDefault();
        const enabled = enabledIndices(flatItems);
        if (enabled.length) moveActive(enabled[0]);
        break;
      }
      case "End": {
        event.preventDefault();
        const enabled = enabledIndices(flatItems);
        if (enabled.length) moveActive(enabled[enabled.length - 1]);
        break;
      }
      case "Escape":
      case "Tab":
        event.preventDefault();
        onCloseRef.current();
        break;
      default:
        if (event.key.length === 1 && event.key !== " " && !event.ctrlKey && !event.metaKey && !event.altKey) {
          event.preventDefault();
          runTypeahead(event.key);
        }
    }
  };

  const renderItem = (item: CardMenuItem, index: number): ReactElement => (
    <button
      key={index}
      type="button"
      role="menuitem"
      data-menu-item-index={index}
      tabIndex={index === activeIndex ? 0 : -1}
      disabled={item.disabled}
      className={`w-full px-3 py-1.5 text-left disabled:opacity-50 ${
        item.danger ? "text-red-700 dark:text-red-300" : ""
      }`}
      onClick={() => selectItem(item)}
    >
      {item.label}
    </button>
  );

  // Sections without a label contribute their items directly to the flat list;
  // a labelled section groups its items so assistive tech can announce it.
  let cursor = 0;
  const sectionStarts = sections.map(section => {
    const start = cursor;
    cursor += section.items.length;
    return start;
  });

  // The cap is applied only in the capped case, so the uncapped render stays
  // exactly what it was before the menu could be measured.
  const capped = placement.maxHeight !== undefined;

  return createPortal(
    <div
      ref={menuRef}
      role="menu"
      aria-label={label}
      tabIndex={-1}
      style={{
        top: placement.top,
        left: placement.left,
        width: MENU_WIDTH,
        ...(capped ? { maxHeight: placement.maxHeight, overflowY: "auto" as const } : {}),
      }}
      className="fixed rounded-md border border-panel-strong bg-panel shadow-lg py-1 text-sm z-50"
      onKeyDown={handleKeyDown}
    >
      {sections.map((section, sectionIndex) => {
        const start = sectionStarts[sectionIndex];
        const buttons = section.items.map((item, itemIndex) => renderItem(item, start + itemIndex));
        if (!section.label) {
          return <Fragment key={sectionIndex}>{buttons}</Fragment>;
        }
        return (
          <div key={sectionIndex} role="group" aria-label={section.label}>
            {buttons}
          </div>
        );
      })}
    </div>,
    document.body,
  );
}

/**
 * Kebab menu for one asset card.
 *
 * The card root is `overflow-hidden` (and grouped rows scroll horizontally), so
 * the menu is portalled to `document.body`: an in-flow or absolutely positioned
 * menu would be clipped in both grid modes and would change the measured row
 * height the virtualiser depends on. Only the menu is portalled; the trigger
 * stays in the card's normal flow.
 */
export default function CardMenu({ label, sections }: CardMenuProps): ReactElement {
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<MenuAnchor | null>(null);

  const openMenu = () => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    setAnchor({ top: rect.top, bottom: rect.bottom, right: rect.right });
    setOpen(true);
  };

  const closeMenu = () => {
    setOpen(false);
    // Guarded: virtualised rows are keyed by row index, so the trigger's row can
    // unmount while the menu is open and there is nothing left to focus.
    // `preventScroll` matters on the scroll-close path: without it, returning
    // focus would snap the viewport back to the card the user just scrolled past.
    triggerRef.current?.focus({ preventScroll: true });
  };

  const toggle = (event: ReactMouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    if (open) closeMenu();
    else openMenu();
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        className="rounded-md border border-panel-strong px-2 py-1 text-sm leading-none disabled:opacity-60"
        onClick={toggle}
      >
        <span aria-hidden="true">⋮</span>
      </button>
      {open && anchor && (
        <CardMenuPanel
          label={label}
          sections={sections}
          anchor={anchor}
          triggerRef={triggerRef}
          onClose={closeMenu}
        />
      )}
    </>
  );
}
