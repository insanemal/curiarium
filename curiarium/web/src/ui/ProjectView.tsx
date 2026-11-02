import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type ReactElement,
  type RefObject,
} from "react";
import { Layers3, Search, X } from "lucide-react";

import { createProject, fileUrl, UnauthorizedError, type Project } from "../lib/api";
import { sourceChipLabel } from "../lib/provenance";
import type {
  EngravingSettings,
  PreviewSettings,
  ResolvedTheme,
  SlicerSettings,
} from "../lib/settings";
import { notifyError } from "../lib/toast";
import { useFileGrants } from "../lib/useFileGrants";
import LicenceChip from "./LicenceChip";
import ProjectLanding from "./ProjectLanding";

type Props = {
  /**
   * Contribute and delete capabilities of the current role, required, not
   * defaulted: the compiler is what keeps the gating airtight.
   */
  canContribute: boolean;
  canDelete: boolean;
  /** `null` while the project list is loading. */
  projects: Project[] | null;
  selectedProjectId: string | null;
  onOpenProject: (id: string) => void;
  onCloseProject: () => void;
  /** App appends the created row to its list; see App.handleProjectCreated. */
  onProjectCreated: (project: Project) => void;
  onProjectUpdated: (project: Project) => void;
  onProjectDeleted: (id: string) => void;
  onAssetsChanged: () => void;
  assetsVersion: number;
  onUnauthorized?: () => void;
  slicerSettings?: SlicerSettings;
  engravingSettings?: EngravingSettings;
  previewSettings?: PreviewSettings;
  theme: ResolvedTheme;
  /** Scroll container the project's member grid lives in (the app's `<main>`). */
  scrollElementRef?: RefObject<HTMLElement | null>;
};

function coverFor(project: Project, className: string) {
  if (project.cover_thumb_url) {
    return <img src={fileUrl(project.cover_thumb_url)} alt="" className={className} />;
  }
  return (
    <span className={`${className} flex items-center justify-center bg-panel-strong opacity-60`}>
      <Layers3 className="h-7 w-7" aria-hidden="true" />
    </span>
  );
}

/** The card's own pill: the source host as plain text (no link inside a button). */
const SOURCE_CHIP =
  "inline-flex min-w-0 items-center rounded-full border border-panel-strong bg-panel-soft px-2.5 py-0.5 text-xs font-medium text-muted";

/** One shared control shape, so the search, create and card actions agree. */
const CONTROL = "rounded-md border border-panel-strong px-3 py-2 text-sm";

type ProjectCardGridProps = {
  projects: Project[];
  onOpenProject: (id: string) => void;
};

/**
 * The project card grid, split out so its grant keeper is mounted exactly while
 * the grid is on screen: one silent `useFileGrants` over every cover thumb,
 * because each card paints an `<img src>` that must not repaint on a re-mint
 * (ADR 0005 decision 10). Opening a project unmounts the grid, so a landing
 * never mints another project's cover.
 */
function ProjectCardGrid({ projects, onOpenProject }: ProjectCardGridProps): ReactElement {
  useFileGrants(projects.map(project => project.cover_thumb_url));
  return (
    <div
      className="grid gap-4"
      style={{ gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))" }}
    >
      {projects.map(project => (
        <button
          key={project.id}
          type="button"
          data-project-card={project.id}
          aria-label={`Open project ${project.name}`}
          onClick={() => onOpenProject(project.id)}
          className="flex flex-col overflow-hidden rounded-lg border border-panel bg-panel-soft text-left transition-colors hover:bg-panel-strong"
        >
          {coverFor(project, "h-32 w-full object-cover")}
          <span className="flex min-w-0 flex-col gap-1 p-3">
            <span className="truncate font-medium">{project.name}</span>
            {project.description && (
              <span className="line-clamp-1 text-sm text-muted">{project.description}</span>
            )}
            <span className="text-xs opacity-70">
              {project.asset_count} file{project.asset_count === 1 ? "" : "s"}
            </span>
            {project.creator && (
              <span className="text-xs text-muted">By {project.creator}</span>
            )}
            {/* Provenance row: text only, because the whole card is one
                button and an anchor inside it would be invalid HTML. */}
            <span className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs">
              <LicenceChip licence={project.licence} licenceUrl={project.licence_url} />
              {project.source_url && (
                <span data-source-chip="true" title={project.source_url} className={SOURCE_CHIP}>
                  <span className="truncate">{sourceChipLabel(project.source_url)}</span>
                </span>
              )}
            </span>
          </span>
        </button>
      ))}
    </div>
  );
}

/**
 * Projects pane: the project index (search plus a contribute-gated inline
 * create) and the project landing page once a project is opened. Focus follows
 * the drill-down: opening focuses Back, closing returns focus to the card that
 * was opened, and a create/delete/missing-id outcome lands on a card or on the
 * notice, never on `<body>`.
 */
export default function ProjectView({
  canContribute,
  canDelete,
  projects,
  selectedProjectId,
  onOpenProject,
  onCloseProject,
  onProjectCreated,
  onProjectUpdated,
  onProjectDeleted,
  onAssetsChanged,
  assetsVersion,
  onUnauthorized,
  slicerSettings,
  engravingSettings,
  previewSettings,
  theme,
  scrollElementRef,
}: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const backButtonRef = useRef<HTMLButtonElement | null>(null);
  const nameInputRef = useRef<HTMLInputElement | null>(null);
  const noticeRef = useRef<HTMLParagraphElement | null>(null);
  const previousSelectedId = useRef<string | null>(null);
  const pendingFocusId = useRef<string | null>(null);
  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  /** The card for `id`, found the one way every focus path looks it up. */
  const cardFor = useCallback((id: string): HTMLElement | null => {
    const cards = containerRef.current?.querySelectorAll<HTMLElement>("[data-project-card]");
    return Array.from(cards ?? []).find(element => element.dataset.projectCard === id) ?? null;
  }, []);

  // Open → Back; close → the card that was open. A created or deleted card is
  // handled by the effects below, which own their own landing target.
  useEffect(() => {
    const previous = previousSelectedId.current;
    previousSelectedId.current = selectedProjectId;
    if (selectedProjectId) {
      backButtonRef.current?.focus();
      return;
    }
    if (!previous) return;
    cardFor(previous)?.focus();
  }, [selectedProjectId, cardFor]);

  // A project deleted elsewhere cannot stay open: the detail view would be a
  // dead end. The list owns the truth, so close back to it.
  useEffect(() => {
    if (!selectedProjectId || projects === null) return;
    if (projects.some(project => project.id === selectedProjectId)) return;
    // Not a dead pane: land on the index with a notice a screen reader announces.
    setNotice("That project no longer exists.");
    onCloseProject();
  }, [projects, selectedProjectId, onCloseProject]);

  // A created card arrives one render after the list grows, so the id waits in
  // a ref; the open-project effect above never reads or clears it.
  useEffect(() => {
    const id = pendingFocusId.current;
    if (!id || selectedProjectId) return;
    const card = cardFor(id);
    if (!card) return;
    pendingFocusId.current = null;
    card.focus();
  }, [projects, selectedProjectId, cardFor]);

  // Delete and a vanished id both unmount everything that held focus, so the
  // notice is the only reachable target; `tabIndex={-1}` is what makes it
  // focusable, and focusing it puts the screen reader on the announcement.
  useEffect(() => {
    if (!notice) return;
    noticeRef.current?.focus();
  }, [notice]);

  // The inline create form opens in place; focus follows it in.
  useEffect(() => {
    if (creating) nameInputRef.current?.focus();
  }, [creating]);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!selectedProjectId || event.key !== "Escape") return;
    // A child that already handled Escape (e.g. a closing modal) keeps it.
    if (event.defaultPrevented) return;
    // An open dialog owns Escape: closing the project under it would strand
    // the dialog's state on the project list.
    if ((event.target as Element).closest?.('[role="dialog"]')) return;
    event.preventDefault();
    // A deliberate close is not an outcome: no notice survives it.
    setNotice(null);
    onCloseProject();
  };

  const cancelCreate = () => {
    setCreating(false);
    setNewName("");
  };

  const handleCreate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = newName.trim();
    if (!name || submitting) return;
    setSubmitting(true);
    try {
      const project = await createProject({ name });
      onProjectCreated(project);
      pendingFocusId.current = project.id; // focus the new card one render later
      setNewName("");
      setCreating(false);
      setSearch(""); // a filter must not hide the card we focus
      setNotice(null);
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized?.();
        return;
      }
      notifyError(err instanceof Error ? err.message : "Could not create the project.");
      // Keep the typed name and the form open: nothing is lost, retry is one click.
    } finally {
      setSubmitting(false);
    }
  };

  // App closes the project in the same commit that drops the row, so the
  // landing - and whatever held focus inside it - is gone: the notice is the
  // only landing target left.
  const handleDelete = (id: string) => {
    const name = projects?.find(project => project.id === id)?.name;
    setNotice(name ? `Deleted project “${name}”.` : "Deleted the project.");
    onProjectDeleted(id);
  };

  // Opening a project deliberately clears an earlier outcome's notice.
  const handleOpenCard = (id: string) => {
    setNotice(null);
    onOpenProject(id);
  };

  const handleBack = () => {
    setNotice(null);
    onCloseProject();
  };

  if (projects === null) {
    return <p className="text-sm opacity-70">Loading projects…</p>;
  }

  const selectedProject = selectedProjectId
    ? projects.find(project => project.id === selectedProjectId) ?? null
    : null;

  if (selectedProject) {
    return (
      <div ref={containerRef} className="flex flex-col gap-4" onKeyDown={handleKeyDown}>
        <nav aria-label="Breadcrumb" className="flex items-center gap-2 text-sm text-muted">
          <span>Projects</span>
          <span aria-hidden="true">/</span>
          <span aria-current="page" className="truncate font-medium">
            {selectedProject.name}
          </span>
        </nav>
        <div>
          <button
            ref={backButtonRef}
            type="button"
            onClick={handleBack}
            aria-label="Back to projects"
            className="px-3 py-2 rounded-md border border-panel-strong text-sm"
          >
            ← All projects
          </button>
        </div>
        <ProjectLanding
          canContribute={canContribute}
          canDelete={canDelete}
          project={selectedProject}
          assetsVersion={assetsVersion}
          onProjectUpdated={onProjectUpdated}
          onProjectDeleted={handleDelete}
          onAssetsChanged={onAssetsChanged}
          onUnauthorized={onUnauthorized}
          slicerSettings={slicerSettings}
          engravingSettings={engravingSettings}
          previewSettings={previewSettings}
          theme={theme}
          scrollElementRef={scrollElementRef}
        />
      </div>
    );
  }

  const query = search.trim().toLowerCase();
  const filtered = query
    ? projects.filter(project =>
        [project.name, project.description, project.creator].some(value =>
          value?.toLowerCase().includes(query),
        ),
      )
    : projects;
  const countLabel = query
    ? `${filtered.length} of ${projects.length} projects`
    : `${projects.length} project${projects.length === 1 ? "" : "s"}`;

  return (
    <div ref={containerRef} className="flex flex-col gap-4" onKeyDown={handleKeyDown}>
      {notice && (
        <p
          ref={noticeRef}
          role="status"
          tabIndex={-1}
          className="rounded-md border border-panel-strong bg-panel-soft px-3 py-2 text-sm"
        >
          {notice}
        </p>
      )}
      <div className="flex items-baseline justify-between gap-3">
        <h1 className="text-xl font-semibold">Projects</h1>
        <span className="text-sm opacity-70">{countLabel}</span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label className="sidebar-search min-w-0 flex-1">
          <Search className="h-4 w-4 shrink-0" aria-hidden="true" />
          <input
            type="search"
            value={search}
            onChange={event => setSearch(event.target.value)}
            placeholder="Search projects"
            aria-label="Search projects"
          />
          {search && (
            <button
              type="button"
              onClick={() => setSearch("")}
              aria-label="Clear project search"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </label>
        {canContribute &&
          (creating ? (
            <form
              aria-label="New project"
              onSubmit={handleCreate}
              onKeyDown={event => {
                if (event.key !== "Escape") return;
                // Cancel in place, without a request: the form owns Escape.
                event.preventDefault();
                cancelCreate();
              }}
              className="flex flex-wrap items-center gap-2"
            >
              <input
                ref={nameInputRef}
                aria-label="Project name"
                value={newName}
                onChange={event => setNewName(event.target.value)}
                className={`${CONTROL} min-w-0`}
              />
              <button
                type="submit"
                disabled={submitting || !newName.trim()}
                className={`${CONTROL} disabled:opacity-50`}
              >
                Create project
              </button>
              <button type="button" onClick={cancelCreate} className={CONTROL}>
                Cancel
              </button>
            </form>
          ) : (
            <button type="button" onClick={() => setCreating(true)} className={CONTROL}>
              New project
            </button>
          ))}
      </div>
      {projects.length === 0 ? (
        <div className="rounded-lg border border-dashed border-panel-strong bg-panel-soft px-4 py-8 text-center text-sm text-muted">
          <p className="font-medium">No projects yet.</p>
          <p>Projects are collections of files from any folder.</p>
        </div>
      ) : filtered.length === 0 ? (
        <div className="rounded-lg border border-dashed border-panel-strong bg-panel-soft px-4 py-8 text-center text-sm text-muted">
          <p className="font-medium">No projects match “{search.trim()}”.</p>
        </div>
      ) : (
        <ProjectCardGrid projects={filtered} onOpenProject={handleOpenCard} />
      )}
    </div>
  );
}
