import { ScrollText, Settings as SettingsIcon } from "lucide-react";

import { roleAtLeast, type Role } from "../lib/auth";
import type { ActiveView } from "./views";

type Props = {
  activeView?: ActiveView;
  onOpenSettings?: () => void;
  onOpenAudit?: () => void;
  /** `null` until `/me` answers; admin-only entries stay hidden until then. */
  role?: Role | null;
  /** `/me`'s username, so the app can say who you are signed in as. */
  username?: string | null;
  /** "password" | "oidc" | "break-glass", shown as a tooltip on the role chip. */
  kind?: string | null;
};

/**
 * Settings, Audit and the signed-in identity, rendered once by
 * `WorkspaceHeader` rather than once per sidebar: the header sits outside both
 * mode panes, so each of these has exactly one entry point in Files mode and in
 * Projects mode, and the two modes cannot drift apart. Admin-only gating lives
 * here only.
 */
export default function WorkspaceUtilities({
  activeView = "library",
  onOpenSettings,
  onOpenAudit,
  role = null,
  username = null,
  kind = null,
}: Props) {
  const howSignedIn =
    kind === "oidc" ? "single sign-on" : kind === "break-glass" ? "break-glass admin" : kind === "password" ? "password" : null;
  return (
    <div className="workspace-utilities">
      {(username || role) && (
        <div
          className="flex items-center justify-between gap-2 px-1 pb-1 text-[11px] leading-tight opacity-80"
          title={howSignedIn ? `Signed in via ${howSignedIn}` : undefined}
        >
          <span className="truncate" data-testid="signed-in-username">
            {username ?? "auth disabled"}
          </span>
          {role && (
            <span
              className="shrink-0 rounded border border-panel-strong px-1 py-px uppercase tracking-wide"
              data-testid="signed-in-role"
            >
              {role}
            </span>
          )}
        </div>
      )}
      <button
        type="button"
        onClick={onOpenSettings}
        className={`workspace-dest ${activeView === "settings" ? "is-selected" : ""}`}
        aria-label="Settings"
        aria-current={activeView === "settings" ? "true" : undefined}
      >
        <SettingsIcon className="h-4 w-4" />
        <span>Settings</span>
      </button>
      {roleAtLeast(role, "admin") && (
        <button
          type="button"
          onClick={onOpenAudit}
          className={`workspace-dest ${activeView === "audit" ? "is-selected" : ""}`}
          aria-label="Audit log"
          aria-current={activeView === "audit" ? "true" : undefined}
        >
          <ScrollText className="h-4 w-4" />
          <span>Audit log</span>
        </button>
      )}
    </div>
  );
}
