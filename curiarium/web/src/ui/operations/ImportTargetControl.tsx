import { useState, type ReactElement } from "react";

import { UnauthorizedError } from "../../lib/api/http";
import { listProjects, type Project } from "../../lib/api/projects";

/** Where a URL import lands: nowhere (a loose asset), a project created from the
 *  page under the operator's name, or an existing project. */
export type ImportTarget =
  | { kind: "none" }
  | { kind: "new"; projectName: string }
  | { kind: "existing"; projectId: string; projectName: string };

/**
 * The project fields a URL import plan takes for a target.
 *
 * `{kind:"none"}` -> `{}`: the import is a loose asset (the library root, or the
 * folder the door already named). `{kind:"new", projectName}` -> `{ project_name }`:
 * the server creates the project from that name. `{kind:"existing", projectId}` ->
 * `{ project_id }`: addressed by id, never by name, because a name is not an
 * identity - two projects may share one, and the planner proves the id exists.
 *
 * A blank new-project name sends NOTHING: the planner refuses a blank
 * `params.project_name` with a 400, so "no name to create a project from" is the
 * loose asset rather than a refusal of the whole import.
 */
export function targetParams(target: ImportTarget): {
  project_id?: string;
  project_name?: string;
} {
  switch (target.kind) {
    case "none":
      return {};
    case "new": {
      const name = target.projectName.trim();
      return name === "" ? {} : { project_name: name };
    }
    case "existing":
      return { project_id: target.projectId };
  }
}

const FOCUS_VISIBLE =
  "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2";
const INPUT_BASE = `rounded-md border border-panel-strong bg-panel-soft px-2 py-1.5 text-sm ${FOCUS_VISIBLE}`;

const NO_PROJECTS_NOTE = "No projects yet, so the import creates a new project from the page.";
const LOAD_FAILED_NOTE =
  "Could not load the project list, so the import creates a new project from the page.";

type Props = {
  /** The chosen target. Fully controlled: the caller owns it because the plan's
   *  params are built from it. */
  value: ImportTarget;
  /** Every choice, including the operator's typing in the new-project name. */
  onChange: (target: ImportTarget) => void;
  /** Refuses every control (another write is in flight in the same door). */
  disabled?: boolean;
  /** Session expiry is the caller's news, not this control's: a 401 while the
   *  project list loads is reported here and then handled like any other
   *  failure, so the target still falls back to a new project. */
  onUnauthorized?: () => void;
};

/** The project list is fetched once, on the first "Existing project" click, and
 *  never again from this mount: a second fetch would only re-ask a question the
 *  operator is already looking at the answer to. */
type ProjectList =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; projects: Project[] }
  | { status: "failed" };

const OPTIONS: { kind: ImportTarget["kind"]; label: string }[] = [
  { kind: "none", label: "No project" },
  { kind: "new", label: "New project from the page" },
  { kind: "existing", label: "Existing project" },
];

function existingTarget(project: Project): ImportTarget {
  return { kind: "existing", projectId: project.id, projectName: project.name };
}

/**
 * Where a URL import lands: no project, a project named by the operator, or an
 * existing one.
 *
 * A NATIVE radiogroup, so the whole group is one tab stop (the APG answer) with
 * no roving-tabindex code of its own; the chosen branch's input is the next stop.
 * The project list is read lazily and its two empty answers are stated in words
 * rather than swallowed - the target falls back to a new project, and the note
 * stays visible so the operator can see why "Existing project" did not stick.
 */
export default function ImportTargetControl({
  value,
  onChange,
  disabled = false,
  onUnauthorized,
}: Props): ReactElement {
  const [list, setList] = useState<ProjectList>({ status: "idle" });
  // A note outlives the fallback to "new": the whole point is that the operator
  // reads why the list could not be used even though the choice moved back.
  const [note, setNote] = useState<string | null>(null);
  // Keeps "Existing project" checked while its list loads (the target itself is
  // only set once there is a project to name).
  const [loadingExisting, setLoadingExisting] = useState(false);

  function fallBackToNew(message: string): void {
    setNote(message);
    setLoadingExisting(false);
    onChange({ kind: "new", projectName: "" });
  }

  async function loadProjects(): Promise<void> {
    setList({ status: "loading" });
    try {
      const projects = await listProjects();
      setList({ status: "ready", projects });
      setLoadingExisting(false);
      if (projects.length === 0) {
        fallBackToNew(NO_PROJECTS_NOTE);
        return;
      }
      setNote(null);
      onChange(existingTarget(projects[0]));
    } catch (err) {
      if (err instanceof UnauthorizedError) onUnauthorized?.();
      else console.error(err);
      setList({ status: "failed" });
      fallBackToNew(LOAD_FAILED_NOTE);
    }
  }

  function chooseExisting(): void {
    switch (list.status) {
      case "ready":
        if (list.projects.length === 0) fallBackToNew(NO_PROJECTS_NOTE);
        else {
          setNote(null);
          onChange(existingTarget(list.projects[0]));
        }
        return;
      case "failed":
        fallBackToNew(LOAD_FAILED_NOTE);
        return;
      case "loading":
        setLoadingExisting(true);
        return;
      case "idle":
        setLoadingExisting(true);
        void loadProjects();
        return;
    }
  }

  function choose(kind: ImportTarget["kind"]): void {
    switch (kind) {
      case "none":
        setNote(null);
        onChange({ kind: "none" });
        return;
      case "new":
        setNote(null);
        onChange({ kind: "new", projectName: "" });
        return;
      case "existing":
        setNote(null);
        chooseExisting();
        return;
    }
  }

  function isChecked(kind: ImportTarget["kind"]): boolean {
    if (kind === "existing") return value.kind === "existing" || loadingExisting;
    return value.kind === kind;
  }

  const listedProjects = list.status === "ready" ? list.projects : [];
  // A chosen project the list no longer holds stays selectable, so a selection
  // is never silently swapped for a neighbour just because the list moved on.
  const currentProjectMissing =
    value.kind === "existing" &&
    !listedProjects.some(project => project.id === value.projectId);

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
      <div
        role="radiogroup"
        aria-label="Import into"
        className="flex flex-wrap items-center gap-x-3 gap-y-1"
      >
        {OPTIONS.map(option => (
          <label key={option.kind} className="flex cursor-pointer items-center gap-1.5">
            <input
              type="radio"
              name="import-target"
              className={`h-4 w-4 ${FOCUS_VISIBLE}`}
              checked={isChecked(option.kind)}
              disabled={disabled}
              onChange={() => choose(option.kind)}
            />
            {option.label}
          </label>
        ))}
      </div>

      {value.kind === "new" && (
        <input
          type="text"
          aria-label="Project name"
          placeholder="Project name"
          className={INPUT_BASE}
          value={value.projectName}
          disabled={disabled}
          onChange={event => onChange({ kind: "new", projectName: event.target.value })}
        />
      )}

      {value.kind === "existing" && (
        <select
          aria-label="Project"
          className={INPUT_BASE}
          value={value.projectId}
          disabled={disabled}
          onChange={event => {
            const picked = listedProjects.find(project => project.id === event.target.value);
            if (picked !== undefined) onChange(existingTarget(picked));
          }}
        >
          {currentProjectMissing && <option value={value.projectId}>{value.projectName}</option>}
          {listedProjects.map(project => (
            <option key={project.id} value={project.id}>
              {project.name}
            </option>
          ))}
        </select>
      )}

      {note !== null && <p className="text-muted">{note}</p>}
    </div>
  );
}
