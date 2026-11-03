import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type MutableRefObject,
  type ReactElement,
  type Ref,
} from "react";
import { MoreHorizontal } from "lucide-react";

import type { Role } from "../lib/auth";
import type { LibraryMode } from "../lib/mode";
import ModeSwitch from "./ModeSwitch";
import WorkspaceUtilities from "./WorkspaceUtilities";
import { resolveQueue } from "./review/queues";
import type { ActiveView } from "./views";

type Props = {
  mode: LibraryMode;
  onModeChange: (mode: LibraryMode) => void;
  activeView?: ActiveView;
  /** Opens whichever queue the role may see; App resolves the key. */
  onOpenReview?: () => void;
  /** Focus lands here when leaving Review; see App's review-leave effect. */
  reviewButtonRef?: Ref<HTMLButtonElement>;
  onOpenSettings?: () => void;
  onOpenAudit?: () => void;
  /** `null` until `/me` answers; an unknown role has no Review destination. */
  role?: Role | null;
  username?: string | null;
  kind?: string | null;
  /** Below the breakpoint the header carries the folder drawer's toggle. */
  narrow?: boolean;
  drawerOpen?: boolean;
  onToggleDrawer?: () => void;
  /** The drawer rail's id, for the toggle's `aria-controls`. */
  drawerId?: string;
  drawerToggleRef?: Ref<HTMLButtonElement>;
  /** The `...` disclosure's trigger; App's review-leave fallback focuses it. */
  menuTriggerRef?: Ref<HTMLButtonElement>;
};

/** Attach one node to both the header's own ref and the caller's. */
function assignRef<T>(ref: Ref<T> | undefined, value: T | null): void {
  if (!ref) return;
  if (typeof ref === "function") {
    ref(value);
    return;
  }
  (ref as MutableRefObject<T | null>).current = value;
}

/**
 * The wide header. It is deliberately unchanged from the layout that shipped
 * before the drawer: at wide there is no disclosure and no toggle, so the DOM,
 * the classes and their order are exactly what `WorkspaceHeader.test.tsx` pins.
 */
function WideHeader({
  mode,
  onModeChange,
  activeView = "library",
  onOpenReview,
  reviewButtonRef,
  onOpenSettings,
  onOpenAudit,
  role = null,
  username = null,
  kind = null,
}: Props): ReactElement {
  const reviewReachable = resolveQueue(role, null) !== null;
  const reviewSelected = activeView === "review";
  return (
    <header className="workspace-header">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <ModeSwitch mode={mode} onChange={onModeChange} />
        {reviewReachable && (
          <button
            ref={reviewButtonRef}
            type="button"
            className={`workspace-dest ${reviewSelected ? "is-selected" : ""}`}
            aria-current={reviewSelected ? "true" : undefined}
            onClick={onOpenReview}
          >
            <span>Review</span>
          </button>
        )}
      </div>
      <WorkspaceUtilities
        activeView={activeView}
        onOpenSettings={onOpenSettings}
        onOpenAudit={onOpenAudit}
        role={role}
        username={username}
        kind={kind}
      />
    </header>
  );
}

/**
 * The narrow header: the mode switch plus the folder drawer's toggle on the
 * left, and a plain disclosure on the right that carries Review and the
 * utilities. It is a disclosure, not a menu: a `role="menu"` would owe the
 * arrow-key/roving-focus model, and these are ordinary destinations.
 *
 * The panel is mounted only while it is open, so a closed disclosure has no
 * focusable destinations behind it and no trap to escape from.
 */
function NarrowHeader({
  mode,
  onModeChange,
  activeView = "library",
  onOpenReview,
  reviewButtonRef,
  onOpenSettings,
  onOpenAudit,
  role = null,
  username = null,
  kind = null,
  drawerOpen = false,
  onToggleDrawer,
  drawerId,
  drawerToggleRef,
  menuTriggerRef,
}: Props): ReactElement {
  const [menuOpen, setMenuOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  /**
   * Set whenever the header itself closes the panel, so the effect below knows
   * the close is ours to finish rather than an ordinary re-render.
   */
  const restoreFocusRef = useRef(false);
  const panelId = useId();
  const reviewReachable = resolveQueue(role, null) !== null;
  const reviewSelected = activeView === "review";

  const setTrigger = useCallback(
    (node: HTMLButtonElement | null) => {
      triggerRef.current = node;
      assignRef(menuTriggerRef, node);
    },
    [menuTriggerRef],
  );

  // The trigger toggles on its own click and a click inside the panel is the
  // panel's business, so only a pointerdown elsewhere closes the disclosure.
  // Focus stays wherever the user put it: an outside close is not ours to undo.
  useEffect(() => {
    if (!menuOpen) return;
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (target && (panelRef.current?.contains(target) || triggerRef.current?.contains(target))) return;
      setMenuOpen(false);
    };
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [menuOpen]);

  useEffect(() => {
    if (!restoreFocusRef.current) return;
    restoreFocusRef.current = false;
    // Activating a destination unmounts its own button, so the browser drops
    // focus to <body>. A control that is still connected kept focus on purpose
    // (a click on a destination that stays mounted), so it is left alone.
    const active = document.activeElement as HTMLElement | null;
    if (active && active !== document.body && active.isConnected) return;
    triggerRef.current?.focus();
  }, [menuOpen]);

  function closeAndActivate(activate: () => void): void {
    restoreFocusRef.current = true;
    setMenuOpen(false);
    activate();
  }

  function handlePanelKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key !== "Escape") return;
    event.preventDefault();
    restoreFocusRef.current = true;
    setMenuOpen(false);
  }

  return (
    <header className="workspace-header">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <ModeSwitch mode={mode} onChange={onModeChange} />
        {mode === "files" && activeView === "library" && (
          <button
            ref={drawerToggleRef}
            type="button"
            className="workspace-drawer-toggle"
            aria-expanded={drawerOpen}
            aria-controls={drawerId}
            onClick={onToggleDrawer}
          >
            <span>Folders</span>
          </button>
        )}
      </div>
      <div className="workspace-menu">
        <button
          ref={setTrigger}
          type="button"
          className="workspace-menu-trigger"
          aria-label="More destinations"
          aria-expanded={menuOpen}
          aria-controls={panelId}
          onClick={() => setMenuOpen(current => !current)}
        >
          <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
          <span aria-hidden="true">•••</span>
        </button>
        {menuOpen && (
          <div
            ref={panelRef}
            id={panelId}
            className="workspace-menu-panel"
            onKeyDown={handlePanelKeyDown}
          >
            {reviewReachable && (
              <button
                ref={reviewButtonRef}
                type="button"
                className={`workspace-dest ${reviewSelected ? "is-selected" : ""}`}
                aria-current={reviewSelected ? "true" : undefined}
                onClick={() => closeAndActivate(() => onOpenReview?.())}
              >
                <span>Review</span>
              </button>
            )}
            <WorkspaceUtilities
              activeView={activeView}
              onOpenSettings={onOpenSettings ? () => closeAndActivate(onOpenSettings) : undefined}
              onOpenAudit={onOpenAudit ? () => closeAndActivate(onOpenAudit) : undefined}
              role={role}
              username={username}
              kind={kind}
            />
          </div>
        )}
      </div>
    </header>
  );
}

/**
 * The one header both modes share: the Files/Projects switch, the Review
 * destination and the Settings/Audit/identity utilities. It is rendered by
 * `App` outside the two mode panes, so each entry point exists exactly once
 * whatever the mode is, and switching mode cannot duplicate or strand one.
 *
 * Review carries no count badge on purpose: the counts belong to the queues
 * inside the destination, and summing the four would report an unknown as a
 * zero (`ADR 0011` decision 4). The entry is hidden below every queue's
 * threshold - a role with no visible queue has no Review destination.
 *
 * Narrow and wide are two arrangements of the same header, never two headers:
 * the wide one is untouched so the existing suite keeps pinning it, and the
 * narrow one carries the drawer's toggle and the `...` disclosure.
 */
export default function WorkspaceHeader(props: Props): ReactElement {
  return props.narrow ? <NarrowHeader {...props} /> : <WideHeader {...props} />;
}
