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

type MenuPosition = { top: number; left: number };

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

type CardMenuPanelProps = {
  label: string;
  sections: CardMenuSection[];
  position: MenuPosition;
  triggerRef: RefObject<HTMLButtonElement | null>;
  onClose: () => void;
};

function CardMenuPanel({
  label,
  sections,
  position,
  triggerRef,
  onClose,
}: CardMenuPanelProps): ReactElement {
  const menuRef = useRef<HTMLDivElement | null>(null);
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

  // The position is measured once at open. A scroll or a resize moves the
  // trigger out from under a `position: fixed` menu, so the menu closes rather
  // than floating away from its card (a scroll listener in the capture phase
  // catches the grid's own scroll container too).
  useEffect(() => {
    const close = () => onCloseRef.current();
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

  const runTypeahead = (key: string) => {
    const state = typeaheadRef.current;
    const now = Date.now();
    if (now - state.lastAt > TYPEAHEAD_RESET_MS) state.buffer = "";
    state.buffer += key.toLowerCase();
    state.lastAt = now;
    if (state.timer) clearTimeout(state.timer);
    state.timer = setTimeout(() => {
      state.buffer = "";
    }, TYPEAHEAD_RESET_MS);

    const match = flatItems.findIndex(
      item => !item.disabled && item.label.toLowerCase().startsWith(state.buffer),
    );
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

  return createPortal(
    <div
      ref={menuRef}
      role="menu"
      aria-label={label}
      tabIndex={-1}
      style={{ top: position.top, left: position.left, width: MENU_WIDTH }}
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
  const [position, setPosition] = useState<MenuPosition | null>(null);

  const openMenu = () => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    setPosition({
      top: rect.bottom + MENU_GAP,
      left: Math.max(VIEWPORT_MARGIN, rect.right - MENU_WIDTH),
    });
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
      {open && position && (
        <CardMenuPanel
          label={label}
          sections={sections}
          position={position}
          triggerRef={triggerRef}
          onClose={closeMenu}
        />
      )}
    </>
  );
}
