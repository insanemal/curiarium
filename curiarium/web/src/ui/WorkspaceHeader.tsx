import type { Ref } from "react";

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
};

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
 */
export default function WorkspaceHeader({
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
}: Props) {
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
