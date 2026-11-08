import React from "react";
import UploadBar from "./UploadBar";
import AssetGrid from "./asset-grid/AssetGrid";
import Sidebar from "./Sidebar";
import ProjectView from "./ProjectView";
import WorkspaceHeader from "./WorkspaceHeader";
import FolderDrawer from "./FolderDrawer";
import Login from "./Login";
import AuditLog from "./AuditLog";
import NotPermitted from "./NotPermitted";
import ErrorBoundary from "./ErrorBoundary";
import UploadQueuePanel from "./UploadQueuePanel";
import {
  UnauthorizedError,
  apiHealth,
  getApiBase,
  getAssetCounts,
  listProjects,
  logout,
  refreshToken,
  type AssetCounts,
  type HealthInfo,
  type Project,
} from "../lib/api";
import { notifyError } from "../lib/toast";
import ConfirmDialog from "./ConfirmDialog";
import {
  clearRefreshToken,
  clearToken,
  readRefreshToken,
  readToken,
  roleAtLeast,
  storeRefreshToken,
  storeToken,
} from "../lib/auth";
import { logoForTheme } from "../lib/brand";
import { type AppSettings, loadSettings, resolveTheme, saveSettings } from "../lib/settings";
import { type LibraryMode, loadMode, saveMode } from "../lib/mode";
import { useSessionRole } from "./useSessionRole";
// Imported from the domain module, not the `../lib/api` barrel: `App.role.test.tsx`
// and `App.mode.test.tsx` replace that barrel with a fixed object, so a new named
// import from it would throw "No export is defined on the mock" in files outside
// this ticket's touch-set. `ui/deferredFileGrants.test.tsx` sets the same precedent
// by importing `../lib/api/grants` directly.
import { OidcExchangeError, exchangeOidcCode } from "../lib/api/auth";
import type { ActiveView, ReviewQueueKey } from "./views";
import BundleReviewView from "./review/BundleReviewView";
import ImportsReviewView from "./review/ImportsReviewView";
import MissingFilesReviewView from "./review/MissingFilesReviewView";
import TrashQueueView from "./review/TrashQueue";
import { useBundleDetect } from "./review/useBundleDetect";
import { useImportRuns } from "./review/useImportRuns";
import { useLibraryScan } from "./review/useLibraryScan";
import { useReviewCounts } from "./review/useReviewCounts";
import { resolveQueue } from "./review/queues";
import ReviewQueueTabs from "./review/ReviewQueueTabs";
import { useTrash } from "./review/useTrash";
import { useNarrowLayout } from "./useNarrowLayout";

const DEFAULT_REFRESH_SECONDS = 900; // 15 minutes: the access token lifetime

/** The rail's DOM id: the narrow header's toggle points `aria-controls` at it. */
const DRAWER_ID = "library-folders";

// Settings is not needed to render the library, so it is fetched on demand and
// lands in its own chunk.
const Settings = React.lazy(() => import("./settings/Settings"));

/** Visible stand-in while the lazily loaded Settings chunk is on the wire. */
export function SettingsLoading() {
  return (
    <div className="flex h-full w-full items-center justify-center text-sm text-muted p-6">
      Loading settings…
    </div>
  );
}

/**
 * The API returns the browser to the web app with the handoff code in the URL
 * fragment (`#sso_code=...`, or `#sso_error=...` on a rejection), never the
 * query string: a fragment is not sent to the server nor written to the API's
 * access log. Read synchronously so the very first render already knows a
 * handoff is in flight.
 */
export function readSsoFragment(): { code: string | null; error: string | null } {
  if (typeof window === "undefined") return { code: null, error: null };
  const hash = window.location.hash.replace(/^#/, "");
  if (!hash) return { code: null, error: null };
  let code: string | null = null;
  let error: string | null = null;
  for (const part of hash.split("&")) {
    const separator = part.indexOf("=");
    if (separator < 0) continue;
    const key = part.slice(0, separator);
    if (key !== "sso_code" && key !== "sso_error") continue;
    let value = part.slice(separator + 1);
    try {
      value = decodeURIComponent(value);
    } catch {
      // A malformed escape is not a usable value: keep it raw and let the
      // exchange fail rather than throwing on a hostile URL.
    }
    if (!value) continue;
    if (key === "sso_code") code = value;
    else error = value;
  }
  return { code, error };
}

function hasSsoFragment(): boolean {
  const { code, error } = readSsoFragment();
  return Boolean(code || error);
}

/** Drops the fragment while keeping path and query, without a navigation. */
function clearSsoFragment(): void {
  if (typeof window === "undefined") return;
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
}

type AppProps = {
  /**
   * The boot mode, decided once before the first paint by
   * `lib/modeBootstrap.ts`. Omitted only by tests that render App directly, in
   * which case the stored mode is read on mount.
   */
  initialMode?: LibraryMode;
  /**
   * The boot probe's project list, already fetched by `lib/modeBootstrap.ts`,
   * so seeding the grid from it costs no second request. It must be read as a
   * prop, never consumed (no take-once read in a `useState` initialiser): that
   * initialiser is impure under StrictMode. `null`/omitted means "no list", and
   * the project-list effect fetches one.
   */
  initialProjects?: Project[] | null;
};

export default function App({ initialMode, initialProjects }: AppProps = {}) {
  const [token, setToken] = React.useState<string | null>(() => readToken());
  // Generation counter for "assets changed". The grid consumes it to refetch in
  // place; it no longer remounts the grid, so view state (search, sort,
  // selection, loaded pages, scroll) survives uploads and mutations.
  const [assetsVersion, setAssetsVersion] = React.useState(0);
  const [folderId, setFolderId] = React.useState<string | null>(null);
  // Files mode's third pile: the library root only (`folder_id IS NULL`).
  // Mutually exclusive with `folderId`, and never set alongside it.
  const [unfiled, setUnfiled] = React.useState(false);
  const [folderVersion, setFolderVersion] = React.useState(0);
  // Counts generation. A move is reconciled locally and never bumps
  // `assetsVersion`, so the grid reports it through `handleAssetsFiled`.
  const [countsVersion, setCountsVersion] = React.useState(0);
  const [counts, setCounts] = React.useState<AssetCounts | null>(null);
  // Top-level browsing mode, persisted per browser. The two panes are mounted
  // lazily (on first visit) and then kept mounted, so each mode keeps its own
  // selection/search/sidebar state while the other is hidden.
  // App no longer derives the mode from the project list: the boot decision is
  // made before first paint by `lib/modeBootstrap.ts` and passed in, so nothing
  // here may flip it after mount. The stored read is only the direct-render
  // fallback (tests); it is never consulted when `initialMode` is given.
  const [mode, setMode] = React.useState<LibraryMode>(() => initialMode ?? loadMode());
  const [visitedModes, setVisitedModes] = React.useState(() => ({
    files: mode === "files",
    projects: mode === "projects",
  }));
  const [openProjectId, setOpenProjectId] = React.useState<string | null>(null);
  const [projects, setProjects] = React.useState<Project[] | null>(() => initialProjects ?? null);
  // The `assetsVersion` the seeded list is valid for; `null` when there is no
  // seed. A boot list is current as of version 0, which is what keeps the list
  // effect below from re-fetching it - while a later mutation bump still
  // refetches.
  //
  // Captured once, like the seed itself, and never updated: it describes the
  // list `projects` started with, so reading it from a later prop (or stamping
  // it after a fetch) would make the guard describe a list App no longer holds.
  // Never moving off the seed's version is deliberate - past it the effect
  // behaves exactly as it did before the seed existed, which is how a re-login
  // still refreshes the list.
  const [projectsAsOf] = React.useState<number | null>(() =>
    initialProjects != null ? 0 : null,
  );
  const [health, setHealth] = React.useState<HealthInfo | null>(null);
  const [tokenTtl, setTokenTtl] = React.useState<number | null>(null);
  const [sessionExpired, setSessionExpired] = React.useState(false);
  // SSO handoff. `ssoPending` starts true when the URL carries a handoff
  // fragment, so the gate below shows a placeholder instead of the login form
  // while the code is exchanged: the form must never flash.
  const [ssoError, setSsoError] = React.useState<string | null>(null);
  const [ssoPending, setSsoPending] = React.useState<boolean>(() => hasSsoFragment());
  // The handoff code is single-use server-side, so the exchange must run once
  // even though React.StrictMode invokes effects twice.
  const ssoHandled = React.useRef<boolean>(false);
  const [activeView, setActiveView] = React.useState<ActiveView>("library");
  // Narrow-ness is decided once, here, and passed to both consumers (the
  // header's toggle and the rail): a CSS-only drawer cannot gate `role="dialog"`,
  // `aria-modal` or whether an off-screen rail stays focusable.
  const narrow = useNarrowLayout();
  const [drawerOpen, setDrawerOpen] = React.useState(false);
  // A review destination is `(review, queue)`; the mode-shaped views ignore the
  // queue. A URL/history model for either is a separate follow-up.
  const [activeQueue, setActiveQueue] = React.useState<ReviewQueueKey>("bundles");
  const [settings, setSettings] = React.useState<AppSettings>(() => loadSettings());
  const [confirmingLogout, setConfirmingLogout] = React.useState(false);
  // Scroll container for the asset grid's virtualization (see useVirtualRows).
  const mainScrollRef = React.useRef<HTMLElement | null>(null);
  // The folder drawer (narrow only): the rail's open state mirrored into a ref
  // for the click handlers, the toggle that opens it, and the ... disclosure's
  // trigger that takes focus back when a header destination unmounts.
  const drawerOpenRef = React.useRef(false);
  const drawerToggleRef = React.useRef<HTMLButtonElement | null>(null);
  const menuTriggerRef = React.useRef<HTMLButtonElement | null>(null);
  // The header's Review entry: the landing target when leaving Review unmounts
  // the control that was clicked.
  const reviewButtonRef = React.useRef<HTMLButtonElement | null>(null);
  // Access token value the post-reload refresh was already fired for. The
  // refresh token is single-use server-side, so the one-shot must not fire
  // twice for the same access token (React.StrictMode runs effects twice).
  const reloadRefreshFor = React.useRef<string | null>(null);
  const resolvedTheme = React.useMemo(
    () => resolveTheme(settings.theme.selected),
    [settings.theme.selected]
  );
  React.useEffect(() => {
    if (typeof document === "undefined") return;
    const root = document.documentElement;
    const isDark =
      resolvedTheme === "dark" ||
      resolvedTheme === "neon" ||
      resolvedTheme === "purple" ||
      resolvedTheme === "blue";
    root.classList.toggle("dark", isDark);
    root.classList.toggle("theme-neon", resolvedTheme === "neon");
    root.classList.toggle("theme-purple", resolvedTheme === "purple");
    root.classList.toggle("theme-blue", resolvedTheme === "blue");
  }, [resolvedTheme]);
  // Leaving the Review destination unmounts the control that was clicked
  // ("Back to library"), so the browser drops focus to <body> and a keyboard
  // user restarts at the top of the document. Land on the header's Review entry
  // instead - but only when nothing still-connected owns focus, so a click on a
  // header control that stays mounted (Settings, Audit, the mode switch) is
  // never stolen.
  const previousView = React.useRef<ActiveView>(activeView);
  React.useEffect(() => {
    const previous = previousView.current;
    previousView.current = activeView;
    if (previous !== "review" || activeView === "review") return;
    const active = document.activeElement as HTMLElement | null;
    if (active && active !== document.body && active.isConnected) return;
    // At narrow the Review entry lives inside the closed ... disclosure, so
    // there is no Review button to land on: the disclosure's trigger takes it.
    (reviewButtonRef.current ?? menuTriggerRef.current)?.focus();
  }, [activeView]);
  // The drawer-only wrappers read the ref rather than the state, so the value
  // the sheet was rendered with decides whether focus returns to the toggle.
  React.useEffect(() => {
    drawerOpenRef.current = drawerOpen;
  }, [drawerOpen]);
  React.useEffect(() => { (async ()=> setHealth(await apiHealth()))(); }, [settings.network.publicUrl]);
  React.useEffect(() => {
    saveSettings(settings);
  }, [settings]);
  const apiUp = health?.ok ?? null;
  const authRequired = health?.auth_required ?? true;

  // Stable identity so the SSO effect can depend on it without re-running on
  // every render; it only touches setters and token storage.
  const handleLogin = React.useCallback((tok: string, ttl: number, refresh: string) => {
    storeToken(tok);
    // Never leave a refresh token from a previous sign-in paired with the new
    // access token: store the new one, or drop whatever was stored before.
    if (refresh) {
      storeRefreshToken(refresh);
    } else {
      clearRefreshToken();
    }
    setToken(tok);
    setTokenTtl(ttl);
    setSessionExpired(false);
  }, []);

  const handleUnauthorized = React.useCallback(() => {
    if (sessionExpired) return;
    clearToken();
    setToken(null);
    setTokenTtl(null);
    setSessionExpired(true);
    notifyError("Your session has expired. Please sign in again.");
  }, [sessionExpired]);

  const identity = useSessionRole({ token, authRequired, onUnauthorized: handleUnauthorized });
  const role = identity.role;

  // Two capabilities, not one: a contributor may upload, import, tag and
  // organise, while destroy/rename/move stays editor-only. Mirrors the
  // API's CONTRIBUTE / WRITE split. `role` is null until `/me` answers and
  // stays null when the API forbids `/me`, so an unknown role has neither: fail
  // closed. The API is still the gate; this only stops a viewer or contributor
  // being shown controls whose requests the server refuses.
  const canContribute = roleAtLeast(role, "contributor");
  const canDelete = roleAtLeast(role, "editor");
  // Read is its own capability because the routes differ: `GET /imports/runs`
  // is require_read, so the Imports running queue is a viewer surface, while a
  // bundle decision is a contribute. Fail closed: `role` null has neither.
  const canRead = roleAtLeast(role, "viewer");
  // `permissions.py` declares `("GET", "/trash"): READ` (line ~278) and
  // `("POST", "/trash/purge"): ADMIN` (line ~373), so reading the queue and
  // emptying it are two capabilities. `canDelete` is deliberately NOT reused:
  // editor/WRITE is one rank below the purge route, and a viewer, contributor or
  // editor must keep the queue without gaining any path to the purge.
  const canPurgeTrash = roleAtLeast(role, "admin");

  // Each queue owns its own data. `enabled` includes `reviewOpen` and the
  // resolved queue key so a fetch happens per entry, not per app load: the app
  // does not pay a whole-library walk or an import-run read just to paint a
  // badge, and entering a queue is still exactly one fetch. Every guard and
  // every fetch reads the same derived key, never the stored one: a viewer's
  // stored `bundles` must neither render the bundle queue nor arm its walk.
  const resolvedQueue = resolveQueue(role, activeQueue);
  const reviewOpen = activeView === "review";
  const bundleReview = useBundleDetect({
    enabled: canContribute && reviewOpen && resolvedQueue === "bundles",
    onUnauthorized: handleUnauthorized,
  });
  // The imports queue is armed by READ, not contribute: `GET /imports/runs` is
  // require_read, and gating it on a write capability would leave a viewer's
  // queue on "Loading import runs..." forever.
  const importsReview = useImportRuns({
    enabled: canRead && reviewOpen && resolvedQueue === "imports",
    onUnauthorized: handleUnauthorized,
  });
  // `GET /trash` sizes every entry with a recursive `os.walk`, so the queue reads
  // once per entry and never on app load: the badge is a dash until the queue is
  // first opened, and nothing polls. `enabled` includes the active queue key so a
  // closed queue issues no read at all.
  const trashReview = useTrash({
    enabled: canRead && reviewOpen && resolvedQueue === "trash",
    onUnauthorized: handleUnauthorized,
  });
  // The missing badge costs no extra request: it reads the `/assets/counts`
  // number App already fetches for the sidebar.
  const reviewCounts = useReviewCounts(bundleReview, importsReview, counts, trashReview.trash);

  const handleOpenReview = React.useCallback((key: ReviewQueueKey) => {
    setActiveQueue(key);
    setActiveView("review");
  }, []);

  // The header's Review entry opens the destination, not a queue: the row that
  // is already resolved stays the one open, and a role with no visible queue
  // has no entry to click.
  const handleOpenReviewDestination = React.useCallback(() => {
    if (resolvedQueue) setActiveView("review");
  }, [resolvedQueue]);

  const handleFoldersChanged = React.useCallback(() => {
    setFolderVersion(v => v + 1);
  }, []);

  const handleAssetsChanged = React.useCallback(() => {
    setAssetsVersion(n => n + 1);
  }, []);

  const handleAssetsFiled = React.useCallback(() => {
    setCountsVersion(v => v + 1);
  }, []);

  // Declared after its callbacks: it registers them for the finish signal, and
  // a hook argument list cannot read a not-yet-initialised `const`. `GET
  // /library/scan` is READ, so a viewer may read the queue and the status; only
  // the rescan button is WRITE and the panel hides it below editor. Entry
  // fetches once, never on app load, and polls only while a scan is walking.
  const missingReview = useLibraryScan({
    enabled: canRead && reviewOpen && resolvedQueue === "missing",
    onUnauthorized: handleUnauthorized,
    onAssetsChanged: handleAssetsChanged,
    onFoldersChanged: handleFoldersChanged,
  });

  // Whole-library counts for the sidebar's rows and the review badges: total,
  // root and flagged-missing. Deliberately unfiltered,
  // and refetched when assets or folders change or a move was reported.
  React.useEffect(() => {
    if (authRequired && !token) return;
    let cancelled = false;
    (async () => {
      try {
        const next = await getAssetCounts();
        if (!cancelled) setCounts(next);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof UnauthorizedError) {
          handleUnauthorized();
          return;
        }
        // A counts failure must not interrupt browsing: the rows simply show
        // no numbers until the next refetch.
        console.error(err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authRequired, token, assetsVersion, folderVersion, countsVersion, handleUnauthorized]);

  // Selecting anything (a folder or All files) is a files-mode action: Settings'
  // "Scan now" and folder navigation call this while the projects pane may be
  // active, so the selection has to bring the files pane back into view.
  const showFilesView = React.useCallback(() => {
    saveMode("files");
    setMode("files");
    setVisitedModes(prev => (prev.files ? prev : { ...prev, files: true }));
    setActiveView("library");
  }, []);

  const handleSelectFolder = React.useCallback((id: string | null) => {
    setFolderId(id);
    // Any folder (or All files) leaves the Unfiled pile.
    setUnfiled(false);
    showFilesView();
  }, [showFilesView]);

  const handleSelectUnfiled = React.useCallback(() => {
    // The two views are exclusive, and an unfiled listing must never also carry
    // a folder id (the server 400s on that pair).
    setFolderId(null);
    setUnfiled(true);
    showFilesView();
  }, [showFilesView]);

  /** Closing the sheet always hands focus back to the control that opened it. */
  const closeDrawer = React.useCallback(() => {
    setDrawerOpen(false);
    drawerToggleRef.current?.focus();
  }, []);

  const handleToggleDrawer = React.useCallback(() => {
    setDrawerOpen(current => !current);
  }, []);

  // Drawer-only wrappers: closing the sheet belongs to the sheet, so it cannot
  // live in `handleSelectFolder` - that handler also serves the grid's
  // breadcrumbs and both review views, where closing would steal focus from the
  // crumb that was just clicked. The ref is read BEFORE the selection, so only
  // a selection made while the sheet was actually open closes it.
  const selectFromDrawer = React.useCallback(
    (id: string | null) => {
      const wasOpen = drawerOpenRef.current;
      handleSelectFolder(id);
      if (wasOpen) closeDrawer();
    },
    [handleSelectFolder, closeDrawer],
  );

  const unfiledFromDrawer = React.useCallback(() => {
    const wasOpen = drawerOpenRef.current;
    handleSelectUnfiled();
    if (wasOpen) closeDrawer();
  }, [handleSelectUnfiled, closeDrawer]);

  // A mode or view change closes the sheet WITHOUT touching focus: the control
  // the operator activated keeps it. A width change deliberately does not reset
  // `drawerOpen`, so a narrow -> wide -> narrow cycle cannot re-steal focus.
  React.useEffect(() => {
    setDrawerOpen(false);
  }, [mode, activeView]);

  const handleModeChange = React.useCallback((next: LibraryMode) => {
    saveMode(next);
    setMode(next);
    setVisitedModes(prev => (prev[next] ? prev : { ...prev, [next]: true }));
    // Switching mode must not throw the operator out of the queue: the REVIEW
    // group is shared by both modes, so a review destination stays open.
    setActiveView(view => (view === "review" ? view : "library"));
  }, []);

  const handleOpenProject = React.useCallback((id: string) => {
    setOpenProjectId(id);
    setActiveView("library");
  }, []);

  // A website import that created or joined a project lands the user on it.
  // The project list only loads once projects mode was visited, so the bump
  // of `assetsVersion` (the list effect's other dependency) refetches it even
  // when the import happened from a first, files-mode session.
  const handleProjectImported = React.useCallback(
    (projectId: string) => {
      handleModeChange("projects");
      setOpenProjectId(projectId);
      setAssetsVersion(n => n + 1);
    },
    [handleModeChange],
  );

  const handleCloseProject = React.useCallback(() => {
    setOpenProjectId(null);
  }, []);

  // A project edit (title, cover, ...) returns the fresh row, so the card grid
  // and the sidebar pick it up without a refetch. The cover thumbnail URL is
  // already cache-busted server-side.
  const handleProjectUpdated = React.useCallback((project: Project) => {
    setProjects(list => (list ? list.map(item => (item.id === project.id ? project : item)) : list));
  }, []);

  // The card grid appends the row the server just returned instead of
  // refetching, matching ProjectPickerDialog's precedent: an appended row keeps
  // the created card focusable immediately. It can disagree with the server's
  // name ordering until the next refetch (`assetsVersion`), which is accepted.
  const handleProjectCreated = React.useCallback((project: Project) => {
    setProjects(list => (list ? [...list, project] : [project]));
  }, []);

  // One handler for all three effects of a delete, so React batches them into a
  // single render: the open project is already closed on the render where its
  // row disappears, and the version bump refetches counts.
  const handleProjectDeleted = React.useCallback((id: string) => {
    setProjects(list => (list ? list.filter(project => project.id !== id) : list));
    setOpenProjectId(null);
    setAssetsVersion(n => n + 1);
  }, []);

  // The project list is App-owned: the sidebar and the project grid both render
  // from it, and a mutation elsewhere (`assetsVersion`) can change its counts.
  React.useEffect(() => {
    if (!visitedModes.projects) return;
    // The boot list is current as of this version -> no second request. A
    // mutation bump changes `assetsVersion` past it, so the list refetches from
    // then on - as does anything else that re-runs this effect (a re-login, via
    // `handleUnauthorized`), because the seed's stamp never moves.
    if (projectsAsOf === assetsVersion) return;
    let cancelled = false;
    (async () => {
      try {
        const list = await listProjects();
        if (!cancelled) setProjects(list);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof UnauthorizedError) {
          handleUnauthorized();
          return;
        }
        console.error(err);
        notifyError("Failed to load projects.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [visitedModes.projects, assetsVersion, projectsAsOf, handleUnauthorized]);

  const handleLogout = () => {
    const refresh = readRefreshToken();
    if (refresh) void logout(refresh);
    clearToken();
    setToken(null);
    setTokenTtl(null);
  };

  const resetSavedProxyUrl = () => {
    setSettings(prev => ({
      ...prev,
      network: {
        ...prev.network,
        publicUrl: "",
      },
    }));
  };

  /**
   * Swap the stored refresh token for a fresh token pair and persist both. The
   * refresh token is single-use server-side, so the rotated one has to replace
   * the old value before the next refresh can happen.
   */
  const refreshSession = React.useCallback(async () => {
    const refresh = readRefreshToken();
    if (!refresh) return false;
    try {
      const res = await refreshToken(refresh);
      storeToken(res.token);
      if (res.refresh_token) storeRefreshToken(res.refresh_token);
      setToken(res.token);
      setTokenTtl(res.expires_in);
      return true;
    } catch {
      return false;
    }
  }, []);

  React.useEffect(() => {
    if (!token) return;
    if (tokenTtl === null && reloadRefreshFor.current !== token && readRefreshToken()) {
      // A reload restores the access token but not its age, so it may already
      // have expired. Trade the refresh token for a live one right away instead
      // of waiting for a timer computed from the fallback TTL. The ref makes
      // this one-shot per access token, so a double-invoked effect cannot spend
      // the same single-use refresh token twice.
      reloadRefreshFor.current = token;
      void (async () => {
        if (!(await refreshSession())) handleUnauthorized();
      })();
      return;
    }
    const ttl = tokenTtl ?? DEFAULT_REFRESH_SECONDS;
    const refreshMs = Math.max(
      5 * 60 * 1000,
      Math.min(ttl * 0.8 * 1000, ttl * 1000 - 5 * 60 * 1000)
    );
    const timer = window.setTimeout(async () => {
      if (!(await refreshSession())) handleUnauthorized();
    }, refreshMs);
    return () => window.clearTimeout(timer);
  }, [token, tokenTtl, refreshSession, handleUnauthorized]);

  React.useEffect(() => {
    if (ssoHandled.current) return;
    const { code, error } = readSsoFragment();
    if (!code && !error) return;
    ssoHandled.current = true;
    // Strip the fragment *before* the first await. The handoff code must not
    // survive a reload, a back-navigation or a screenshot, and a re-invoked
    // effect that finds nothing can no longer spend it a second time.
    clearSsoFragment();
    if (error) {
      setSsoError(error);
      setSsoPending(false);
      return;
    }
    if (!code) {
      setSsoPending(false);
      return;
    }
    const handoff = code;
    void (async () => {
      try {
        const res = await exchangeOidcCode(handoff);
        handleLogin(res.token, res.expires_in, res.refresh_token ?? "");
      } catch (err) {
        // The API's message is for the console only: the screen gets a frozen
        // copy value, never server or IdP free text. A 409 is the persistent
        // "no administrator has signed in locally yet" condition, where "please
        // try again" would be actively wrong advice, so it gets its own line.
        console.error(err);
        setSsoError(err instanceof OidcExchangeError && err.status === 409 ? "no_admin_bootstrap" : "exchange_failed");
      } finally {
        setSsoPending(false);
      }
    })();
  }, [handleLogin]);

  if (authRequired && !token) {
    // A handoff in flight renders a placeholder rather than <Login>, so the
    // local password form never flashes before the exchange lands.
    if (ssoPending) {
      return (
        <div className="min-h-screen flex items-center justify-center">
          <p className="text-sm text-muted">Signing you in...</p>
        </div>
      );
    }
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Login onSuccess={handleLogin} apiUp={apiUp} theme={resolvedTheme} ssoError={ssoError} />
      </div>
    );
  }

  return (
    <div className="h-screen flex flex-col">
      <WorkspaceHeader
        mode={mode}
        onModeChange={handleModeChange}
        activeView={activeView}
        onOpenReview={handleOpenReviewDestination}
        reviewButtonRef={reviewButtonRef}
        onOpenSettings={() => setActiveView("settings")}
        onOpenAudit={() => setActiveView("audit")}
        role={role}
        username={identity.username}
        kind={identity.kind}
        narrow={narrow}
        drawerOpen={drawerOpen}
        onToggleDrawer={handleToggleDrawer}
        drawerId={DRAWER_ID}
        drawerToggleRef={drawerToggleRef}
        menuTriggerRef={menuTriggerRef}
      />
      <div className="flex min-h-0 flex-1">
        {/* One stable rail node at every width; below the breakpoint it is the
            folder sheet (role=dialog, focus trap, Escape). The `visitedModes.files`
            guard keeps its lazy mount: the tree is created on the first visit and
            then only hidden, so its search and selection survive a mode switch. */}
        <FolderDrawer
          id={DRAWER_ID}
          mode={mode}
          narrow={narrow}
          open={drawerOpen}
          onClose={closeDrawer}
        >
          {visitedModes.files && (
            <Sidebar
              selectedId={folderId}
              onSelect={selectFromDrawer}
              unfiled={unfiled}
              totalCount={counts?.total ?? null}
              unfiledCount={counts?.unfiled ?? null}
              onSelectUnfiled={unfiledFromDrawer}
              onFoldersChanged={handleFoldersChanged}
              foldersVersion={folderVersion}
              onAssetsChanged={handleAssetsChanged}
              onUnauthorized={handleUnauthorized}
              role={role}
            />
          )}
        </FolderDrawer>
        {/* The asset grid's virtualizer compensates row-height changes itself (see useVirtualRows), so native scroll anchoring must not apply a second adjustment. */}
        <main ref={mainScrollRef} className="min-h-0 flex-1 p-4 overflow-auto" style={{ overflowAnchor: "none" }}>
          {apiUp === false && (
            <div className="mb-3 p-2 rounded-md bg-red-100 text-red-900 dark:bg-red-900/30 dark:text-red-100 flex flex-wrap items-center gap-2">
              <span>API unreachable at {getApiBase()}. Ensure the API container is running and reachable.</span>
              {!!settings.network.publicUrl && (
                <button
                  className="px-2 py-1 rounded-md border border-red-400/70 text-xs font-medium"
                  onClick={resetSavedProxyUrl}
                  type="button"
                >
                  Reset saved proxy URL
                </button>
              )}
            </div>
          )}
          <header className="flex items-center justify-between mb-4 gap-4 flex-wrap">
            {activeView === "library" && (
              <img
                src={logoForTheme(resolvedTheme)}
                alt="Curiarium"
                className="h-40 w-auto max-w-[520px]"
              />
            )}
            <div className="flex flex-wrap items-center gap-3">
              {/* Uploads always land in a files folder, so projects mode hides the bar. */}
              {activeView === "library" && mode === "files" && (
                <UploadBar
                  canContribute={canContribute}
                  folderId={folderId}
                  makerworldCookie={settings.makerworld.cookie}
                  thingiverseCookie={settings.thingiverse.cookie}
                  onUploaded={handleAssetsChanged}
                  onUnauthorized={handleUnauthorized}
                  onProjectImported={handleProjectImported}
                />
              )}
              {activeView !== "library" && (
                <button
                  className="px-3 py-2 rounded-md border border-panel-strong text-sm"
                  onClick={() => setActiveView("library")}
                >
                  Back to library
                </button>
              )}
              <button
                onClick={() => setConfirmingLogout(true)}
                className="px-3 py-2 rounded-md border border-panel-strong text-sm"
              >
                Log out
              </button>
            </div>
          </header>
          {/* A rejected lazy chunk throws during render and React unwinds to
              the nearest boundary. Keying by `activeView` means navigating to
              another view (or back) recovers without a reload, and the shell
              (rail, header, log out) sits outside the boundary so the screen is
              never blank. */}
          <ErrorBoundary key={activeView}>
            {/* Same lazy-mount rule as the rail: each mode's grid is created on
                first visit and then stays mounted (only hidden), so search, sort,
                selection and loaded pages survive a mode switch. */}
            {activeView === "library" && visitedModes.files && (
              <div hidden={mode !== "files"}>
                <AssetGrid
                  canContribute={canContribute}
                  canDelete={canDelete}
                  folderId={folderId}
                  unfiled={unfiled}
                  foldersVersion={folderVersion}
                  assetsVersion={assetsVersion}
                  countsVersion={countsVersion}
                  onAssetsFiled={handleAssetsFiled}
                  onUnauthorized={handleUnauthorized}
                  slicerSettings={settings.slicer}
                  engravingSettings={settings.engraving}
                  previewSettings={settings.previews}
                  theme={resolvedTheme}
                  scrollElementRef={mainScrollRef}
                  onNavigateFolder={handleSelectFolder}
                  onProjectsChanged={handleAssetsChanged}
                />
              </div>
            )}
            {activeView === "library" && visitedModes.projects && (
              <div hidden={mode !== "projects"}>
                <ProjectView
                  canContribute={canContribute}
                  canDelete={canDelete}
                  projects={projects}
                  selectedProjectId={openProjectId}
                  onOpenProject={handleOpenProject}
                  onCloseProject={handleCloseProject}
                  onProjectCreated={handleProjectCreated}
                  onProjectUpdated={handleProjectUpdated}
                  onProjectDeleted={handleProjectDeleted}
                  onAssetsChanged={handleAssetsChanged}
                  assetsVersion={assetsVersion}
                  onUnauthorized={handleUnauthorized}
                  slicerSettings={settings.slicer}
                  engravingSettings={settings.engraving}
                  previewSettings={settings.previews}
                  theme={resolvedTheme}
                  scrollElementRef={mainScrollRef}
                />
              </div>
            )}
            {activeView === "settings" && (
              <React.Suspense fallback={<SettingsLoading />}>
                <Settings
                  settings={settings}
                  onChange={setSettings}
                  onUnauthorized={handleUnauthorized}
                  role={role}
                  username={identity.username}
                  kind={identity.kind}
                />
              </React.Suspense>
            )}
            {activeView === "review" && (
              <>
                <ReviewQueueTabs
                  activeQueue={resolvedQueue}
                  counts={reviewCounts}
                  onSelect={handleOpenReview}
                  role={role}
                />
                {/* One tabpanel for whichever queue the role may actually open: a
                    stored key the role cannot see is resolved away, so the panel
                    and the selected tab always name the same queue - the row reads
                    the resolved key too, because a tablist with no selected tab
                    would leave the panel labelled by an unselected one. */}
                {resolvedQueue !== null && (
                  <div
                    role="tabpanel"
                    id={`review-tabpanel-${resolvedQueue}`}
                    aria-labelledby={`review-tab-${resolvedQueue}`}
                  >
                    {resolvedQueue === "bundles" && (
                      <BundleReviewView state={bundleReview} canContribute={canContribute} />
                    )}
                    {resolvedQueue === "imports" && (
                      <ImportsReviewView
                        state={importsReview}
                        canWrite={canDelete}
                        onAssetsChanged={handleAssetsChanged}
                        onFoldersChanged={handleFoldersChanged}
                        onUnauthorized={handleUnauthorized}
                        onSelectFolder={handleSelectFolder}
                      />
                    )}
                    {resolvedQueue === "missing" && (
                      <MissingFilesReviewView
                        state={missingReview}
                        canWrite={canDelete}
                        canContribute={canContribute}
                        canDelete={canDelete}
                        missingCount={counts ? counts.missing : null}
                        assetsVersion={assetsVersion}
                        onRefresh={handleAssetsChanged}
                        onAssetsFiled={handleAssetsFiled}
                        onUnauthorized={handleUnauthorized}
                        slicerSettings={settings.slicer}
                        engravingSettings={settings.engraving}
                        previewSettings={settings.previews}
                        theme={resolvedTheme}
                        scrollElementRef={mainScrollRef}
                      />
                    )}
                    {resolvedQueue === "trash" && (
                      <TrashQueueView state={trashReview} canPurge={canPurgeTrash} />
                    )}
                  </div>
                )}
              </>
            )}
            {activeView === "audit" && !roleAtLeast(role, "admin") && <NotPermitted />}
            {activeView === "audit" && roleAtLeast(role, "admin") && (
              <AuditLog onUnauthorized={handleUnauthorized} />
            )}
          </ErrorBoundary>
        </main>
        {/* App's conditional children are only ever APPENDED after <main> in
            this array. React reconciles an unkeyed children array by index, so a
            child inserted before <main> (a scrim between the rail and it) takes
            a slot in front of it and remounts <main>. `useVirtualRows` now
            observes element scrolls through a `document` capture listener and
            reads the ref fresh, so it survives a late-attached or replaced
            scroller; a remount is still a real loss, because it throws away the
            grid's search, selection and loaded pages. */}
        {narrow && drawerOpen && <div className="folder-drawer-scrim" aria-hidden="true" />}
      </div>
      {confirmingLogout && (
        <ConfirmDialog
          title="Log out?"
          body="Are you sure you want to log out?"
          confirmLabel="Log out"
          onConfirm={() => {
            handleLogout();
            setConfirmingLogout(false);
          }}
          onCancel={() => setConfirmingLogout(false)}
        />
      )}
      {/* Fixed-position sibling of the layout: it never affects App's layout.
          Gated too: its only action is a retry of an upload, which is a
          contribute. A demotion below contributor while an upload is still
          running would otherwise leave a visible button whose click is refused
          with a raw 403. */}
      {canContribute && (
        <UploadQueuePanel onUploaded={handleAssetsChanged} onUnauthorized={handleUnauthorized} />
      )}
    </div>
  );
}
