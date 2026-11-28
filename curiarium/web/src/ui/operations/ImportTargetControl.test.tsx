// @vitest-environment jsdom
import React, { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const projectsApi = vi.hoisted(() => ({ listProjects: vi.fn() }));
vi.mock("../../lib/api/projects", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api/projects")>()),
  listProjects: projectsApi.listProjects,
}));

import { UnauthorizedError } from "../../lib/api/http";
import { listProjects, type Project } from "../../lib/api/projects";
import ImportTargetControl, { targetParams, type ImportTarget } from "./ImportTargetControl";

const listProjectsMock = vi.mocked(listProjects);

// The control's own sentences, pinned verbatim: the operator reads them INSTEAD
// of the target they asked for, so a reworded one is a behaviour change.
const NO_PROJECTS_NOTE = "No projects yet, so the import creates a new project from the page.";
const LOAD_FAILED_NOTE =
  "Could not load the project list, so the import creates a new project from the page.";

function project(overrides: Partial<Project> = {}): Project {
  return {
    id: "p1",
    name: "Hydra",
    description: null,
    creator: null,
    source_url: null,
    licence: null,
    licence_url: null,
    origin: "own",
    cover_asset_id: null,
    cover_thumb_url: null,
    asset_count: 0,
    metadata_json: {},
    total_size: 0,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

/** The control is fully controlled, so the harness owns the target and records
 *  every value it hands back. */
function Harness({
  initial,
  onChange,
  disabled,
  onUnauthorized,
}: {
  initial: ImportTarget;
  onChange?: (target: ImportTarget) => void;
  disabled?: boolean;
  onUnauthorized?: () => void;
}) {
  const [value, setValue] = useState<ImportTarget>(initial);
  return (
    <ImportTargetControl
      value={value}
      onChange={next => {
        setValue(next);
        onChange?.(next);
      }}
      disabled={disabled}
      onUnauthorized={onUnauthorized}
    />
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  listProjectsMock.mockResolvedValue([project()]);
});

afterEach(cleanup);

describe("targetParams", () => {
  it("sends each target's own fields and never the other target's", () => {
    // Catches a plan addressed by name ("Hydra") instead of by id, and a new
    // project that also carries an id it does not have.
    expect(targetParams({ kind: "none" })).toEqual({});
    expect(targetParams({ kind: "new", projectName: "Hydra" })).toEqual({
      project_name: "Hydra",
    });
    expect(
      targetParams({ kind: "existing", projectId: "p1", projectName: "Hydra" }),
    ).toEqual({ project_id: "p1" });

    expect(targetParams({ kind: "new", projectName: "Hydra" })).not.toHaveProperty(
      "project_id",
    );
    expect(
      targetParams({ kind: "existing", projectId: "p1", projectName: "Hydra" }),
    ).not.toHaveProperty("project_name");

    // A blank name is not a name the planner accepts (it refuses a blank
    // project_name with a 400), so it sends nothing rather than a refusal.
    expect(targetParams({ kind: "new", projectName: "   " })).toEqual({});
  });
});

describe("ImportTargetControl", () => {
  it("reads the project list on the first Existing click, and once only", () => {
    // Catches a mount-time fetch (a read nobody asked for) and a second fetch on
    // every re-selection.
    render(<Harness initial={{ kind: "none" }} />);
    expect(listProjectsMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("radio", { name: "Existing project" }));
    expect(listProjectsMock).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("radio", { name: "No project" }));
    fireEvent.click(screen.getByRole("radio", { name: "Existing project" }));

    expect(listProjectsMock).toHaveBeenCalledTimes(1);
  });

  it("picks the first listed project by id and shows it in the select", async () => {
    // Catches a value that carries only the name, or a select with no row that
    // matches the chosen id.
    const onChange = vi.fn();
    render(<Harness initial={{ kind: "none" }} onChange={onChange} />);

    fireEvent.click(screen.getByRole("radio", { name: "Existing project" }));

    await waitFor(() =>
      expect(onChange).toHaveBeenLastCalledWith({
        kind: "existing",
        projectId: "p1",
        projectName: "Hydra",
      }),
    );
    const select = screen.getByRole("combobox", { name: "Project" }) as HTMLSelectElement;
    expect(select.value).toBe("p1");
  });

  it("falls back to a new project when the library has no projects yet", async () => {
    // Catches a radio left checked over an empty select, and a note that vanishes
    // with the fallback so the operator never learns why.
    listProjectsMock.mockResolvedValue([]);
    const onChange = vi.fn();
    render(<Harness initial={{ kind: "none" }} onChange={onChange} />);

    fireEvent.click(screen.getByRole("radio", { name: "Existing project" }));

    await waitFor(() =>
      expect(screen.getByText(NO_PROJECTS_NOTE)).toBeTruthy(),
    );
    expect(onChange).toHaveBeenLastCalledWith({ kind: "new", projectName: "" });
    expect(
      (screen.getByRole("radio", { name: "New project from the page" }) as HTMLInputElement)
        .checked,
    ).toBe(true);
  });

  it("falls back to a new project when the list cannot be read", async () => {
    // Catches a silent failure: the choice would look made while the import went
    // somewhere else, with nothing saying the list was refused.
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    listProjectsMock.mockRejectedValue(new Error("network down"));
    const onChange = vi.fn();
    render(<Harness initial={{ kind: "none" }} onChange={onChange} />);

    fireEvent.click(screen.getByRole("radio", { name: "Existing project" }));

    await waitFor(() => expect(screen.getByText(LOAD_FAILED_NOTE)).toBeTruthy());
    expect(onChange).toHaveBeenLastCalledWith({ kind: "new", projectName: "" });
    consoleError.mockRestore();
  });

  it("hands a 401 while loading the list to the caller", async () => {
    // Catches a session expiry swallowed as an ordinary load failure.
    const onUnauthorized = vi.fn();
    listProjectsMock.mockRejectedValue(new UnauthorizedError());
    render(<Harness initial={{ kind: "none" }} onUnauthorized={onUnauthorized} />);

    fireEvent.click(screen.getByRole("radio", { name: "Existing project" }));

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
  });

  it("keeps the new project's typed name in the target", () => {
    // Catches a name input that only lives in the DOM: the review names the
    // project from the target, so the typed name has to be in the value.
    const onChange = vi.fn();
    render(<Harness initial={{ kind: "new", projectName: "" }} onChange={onChange} />);

    fireEvent.change(screen.getByRole("textbox", { name: "Project name" }), {
      target: { value: "Hydra" },
    });

    expect(onChange).toHaveBeenLastCalledWith({ kind: "new", projectName: "Hydra" });
  });
});
