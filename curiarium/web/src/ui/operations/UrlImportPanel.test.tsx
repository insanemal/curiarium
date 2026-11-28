// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";

const importsApi = vi.hoisted(() => ({
  inspectImportLink: vi.fn(),
  listImportZipEntries: vi.fn(),
}));
vi.mock("../../lib/api/imports", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api/imports")>()),
  inspectImportLink: importsApi.inspectImportLink,
  listImportZipEntries: importsApi.listImportZipEntries,
}));

const operationsApi = vi.hoisted(() => ({
  planOperation: vi.fn(),
  submitOperation: vi.fn(),
  getOperation: vi.fn(),
  undoOperation: vi.fn(),
  listOperations: vi.fn(),
}));
vi.mock("../../lib/api/operations", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api/operations")>()),
  planOperation: operationsApi.planOperation,
  submitOperation: operationsApi.submitOperation,
  getOperation: operationsApi.getOperation,
  undoOperation: operationsApi.undoOperation,
  listOperations: operationsApi.listOperations,
}));

const projectsApi = vi.hoisted(() => ({ listProjects: vi.fn() }));
vi.mock("../../lib/api/projects", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api/projects")>()),
  listProjects: projectsApi.listProjects,
}));

import {
  inspectImportLink,
  listImportZipEntries,
  type ImportInspectInfo,
  type ZipEntry,
} from "../../lib/api/imports";
import { UnauthorizedError } from "../../lib/api/http";
import {
  listOperations,
  planOperation,
  submitOperation,
  undoOperation,
  getOperation,
  type Operation,
  type OperationPlan,
  type OperationReview as Review,
} from "../../lib/api/operations";
import { listProjects, type Project } from "../../lib/api/projects";
import { loadSettings, resetSettingsCache, saveSettings } from "../../lib/settings";
import UrlImportPanel from "./UrlImportPanel";

const inspectMock = vi.mocked(inspectImportLink);
const listEntriesMock = vi.mocked(listImportZipEntries);
const planMock = vi.mocked(planOperation);
const submitMock = vi.mocked(submitOperation);
const getOperationMock = vi.mocked(getOperation);
const undoMock = vi.mocked(undoOperation);
const listOperationsMock = vi.mocked(listOperations);
const listProjectsMock = vi.mocked(listProjects);

const URL = "https://makerworld.test/models/Hydra";

const NOT_A_ZIP: ImportInspectInfo = { filename: "Hydra.3mf", mime: "model/3mf", is_zip: false };
const A_ZIP: ImportInspectInfo = { filename: "Hydra.zip", mime: "application/zip", is_zip: true };

const ZIP_ENTRIES: ZipEntry[] = [
  { path: "Hydra.stl", size: 2048, junk: false },
  { path: "notes/readme.txt", size: 12, junk: false },
  { path: "__MACOSX/._Hydra.stl", size: 300, junk: true },
];

const COOKIES = {
  makerworld_cookie: "makerworld-cookie",
  thingiverse_cookie: "thingiverse-cookie",
};

function makeReview(workflow: string, overrides: Partial<Review> = {}): Review {
  return {
    workflow,
    target: URL,
    moves: 0,
    creates: 1,
    adopts: 0,
    deletes: 0,
    members: 0,
    groups: [],
    cover: null,
    excluded: [],
    existing_project_id: null,
    existing_project_name: null,
    move_list: [],
    projects: [],
    ...overrides,
  };
}

function makePlan(workflow: string, overrides: Partial<Review> = {}): OperationPlan {
  return {
    plan_id: "plan-1",
    workflow,
    target: URL,
    steps: [{ index: 0, op: "fetch", summary: "fetch the page" }],
    review: makeReview(workflow, overrides),
  };
}

function makeOperation(overrides: Partial<Operation> = {}): Operation {
  return {
    id: "op-7",
    workflow: "import-link",
    target: URL,
    status: "queued",
    submitted_by: "u1",
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    finished_at: null,
    error: null,
    undo_state: "none",
    superseded_by: null,
    steps: [],
    ...overrides,
  };
}

function project(overrides: Partial<Project> = {}): Project {
  return {
    id: "p1",
    name: "Hydra Project",
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

function renderPanel(
  overrides: Partial<ComponentProps<typeof UrlImportPanel>> = {},
): ReturnType<typeof render> {
  return render(
    <UrlImportPanel canContribute folderId="folder-1" onChanged={vi.fn()} {...overrides} />,
  );
}

function typeUrl(): void {
  fireEvent.change(screen.getByRole("textbox", { name: "Import link" }), {
    target: { value: URL },
  });
}

async function checkLink(): Promise<void> {
  fireEvent.click(screen.getByRole("button", { name: "Check the link" }));
  await waitFor(() => expect(inspectMock).toHaveBeenCalledTimes(1));
}

beforeEach(() => {
  vi.resetAllMocks();
  // The panel reads the store at call time, so the cookies are saved the way the
  // settings screen saves them and the cache is dropped afterwards.
  saveSettings({
    ...loadSettings(),
    imports: { rememberCookies: true },
    makerworld: { cookie: "makerworld-cookie" },
    thingiverse: { cookie: "thingiverse-cookie" },
  });
  resetSettingsCache();
  inspectMock.mockResolvedValue(NOT_A_ZIP);
  listEntriesMock.mockResolvedValue({ filename: "Hydra.zip", entries: ZIP_ENTRIES });
  planMock.mockResolvedValue(makePlan("import-link"));
  submitMock.mockResolvedValue(makeOperation());
  getOperationMock.mockResolvedValue(makeOperation({ status: "done" }));
  listOperationsMock.mockResolvedValue([]);
  undoMock.mockResolvedValue({
    operation_id: "op-7",
    undone: true,
    project_id: null,
    project_name: null,
    moves: 0,
    projects_removed: [],
  });
  listProjectsMock.mockResolvedValue([project()]);
});

afterEach(cleanup);

describe("UrlImportPanel", () => {
  it("checks the link with the URL and the cookies saved in settings", async () => {
    // Catches a door that drops the saved cookies, reads them once at mount, or
    // sends a blank one instead of omitting it.
    renderPanel();
    expect(inspectMock).not.toHaveBeenCalled();

    typeUrl();
    await checkLink();

    expect(inspectMock).toHaveBeenCalledWith({
      url: URL,
      folder_id: "folder-1",
      ...COOKIES,
    });
    expect(screen.getByText(NOT_A_ZIP.filename)).toBeTruthy();
    expect(screen.getByText(NOT_A_ZIP.mime)).toBeTruthy();
  });

  it("shows the import-link review, and no picker, for a link that is not a zip", async () => {
    // Catches a door that offers a member picker for a single file, and one that
    // shows the confirm before the review the server computed.
    const { container } = renderPanel();
    expect(planMock).not.toHaveBeenCalled();

    typeUrl();
    await checkLink();

    expect(screen.queryByRole("button", { name: "Choose files" })).toBeNull();
    await waitFor(() => expect(container.querySelector("[data-url-import-plan]")).toBeTruthy());
    expect(screen.getByRole("button", { name: "Import from the page" })).toBeTruthy();
    expect(submitMock).not.toHaveBeenCalled();

    const [request] = planMock.mock.calls[planMock.mock.calls.length - 1];
    expect(request.workflow).toBe("import-link");
    expect(request.target).toBe(URL);
    expect(request.params).toEqual({
      folder_id: "folder-1",
      filename: NOT_A_ZIP.filename,
      ...COOKIES,
    });
  });

  it("submits exactly the picked subset as the import-zip plan's entries", async () => {
    // Catches a selection lost between the picker and the plan, a panel offered
    // before any selection exists, and a plan keyed on something but the URL.
    inspectMock.mockResolvedValue(A_ZIP);
    planMock.mockResolvedValue(makePlan("import-zip", { members: 1 }));
    renderPanel();

    typeUrl();
    await checkLink();

    // A zip with no selection is not a plan: the server refuses `entries`=none.
    expect(planMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Choose files" }));
    await screen.findByRole("dialog");
    expect(listEntriesMock).toHaveBeenCalledWith({ url: URL, ...COOKIES });

    // The second member is unticked; the junk member is never tickable.
    fireEvent.click(screen.getAllByRole("option")[1]);
    fireEvent.click(screen.getByRole("button", { name: "Use these files" }));

    await waitFor(() => expect(planMock).toHaveBeenCalledTimes(1));
    expect(planMock).toHaveBeenCalledWith({
      workflow: "import-zip",
      target: URL,
      params: {
        entries: ["Hydra.stl"],
        folder_id: "folder-1",
        filename: A_ZIP.filename,
        ...COOKIES,
      },
    });
    expect(screen.getByText("1 of 3 files chosen")).toBeTruthy();

    fireEvent.click(await screen.findByRole("button", { name: "Import 1 file from the zip" }));

    await waitFor(() =>
      expect(submitMock).toHaveBeenCalledWith({
        planId: "plan-1",
        idempotencyKey: expect.any(String),
      }),
    );
  });

  it("rides a new project as project_name, drafted from the page's filename", async () => {
    // Catches a new-project target that sends nothing (a loose asset nobody asked
    // for) and a draft that overwrites the operator's own name.
    renderPanel();
    typeUrl();
    await checkLink();

    fireEvent.click(screen.getByRole("radio", { name: "New project from the page" }));

    const nameInput = (await screen.findByRole("textbox", {
      name: "Project name",
    })) as HTMLInputElement;
    await waitFor(() => expect(nameInput.value).toBe("Hydra"));

    await waitFor(() => expect(planMock).toHaveBeenCalled());
    const [request] = planMock.mock.calls[planMock.mock.calls.length - 1];
    expect(request.params).toMatchObject({ project_name: "Hydra" });
    expect(request.params).not.toHaveProperty("project_id");

    fireEvent.change(nameInput, { target: { value: "Hydra Remix" } });
    await waitFor(() => {
      const [latest] = planMock.mock.calls[planMock.mock.calls.length - 1];
      expect(latest.params).toMatchObject({ project_name: "Hydra Remix" });
    });
  });

  it("rides an existing project as project_id, never as its name", async () => {
    // Catches a plan addressed by a name, which is not an identity.
    renderPanel();
    typeUrl();
    await checkLink();

    fireEvent.click(screen.getByRole("radio", { name: "Existing project" }));

    await waitFor(() => {
      const [request] = planMock.mock.calls[planMock.mock.calls.length - 1];
      expect(request.params).toMatchObject({ project_id: "p1" });
      expect(request.params).not.toHaveProperty("project_name");
    });
  });

  it("hands a 401 from the preflight to the caller", async () => {
    // Catches a session expiry turned into an ordinary error sentence, or one
    // that still lets the door plan import work.
    inspectMock.mockRejectedValue(new UnauthorizedError());
    const onUnauthorized = vi.fn();
    renderPanel({ onUnauthorized });

    typeUrl();
    fireEvent.click(screen.getByRole("button", { name: "Check the link" }));

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    expect(planMock).not.toHaveBeenCalled();
  });

  it("reaches for no native dialog, on success or on refusal", async () => {
    // Catches a native browser confirmation or alert creeping back into the door:
    // the repo keeps one app-owned vocabulary for both.
    const confirmSpy = vi.spyOn(window, "confirm");
    const alertSpy = vi.spyOn(window, "alert");
    listEntriesMock.mockRejectedValue(new Error("the archive is unreadable"));
    inspectMock.mockResolvedValue(A_ZIP);
    renderPanel();

    typeUrl();
    await checkLink();
    fireEvent.click(screen.getByRole("button", { name: "Choose files" }));
    await waitFor(() =>
      expect(screen.getByText("the archive is unreadable")).toBeTruthy(),
    );

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(alertSpy).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
    alertSpy.mockRestore();
  });

  it("renders nothing and asks the server nothing for a viewer", () => {
    // Catches a gate that hides the controls but still resolves DNS on mount.
    const { container } = renderPanel({ canContribute: false });

    expect(container.innerHTML).toBe("");
    expect(inspectMock).not.toHaveBeenCalled();
    expect(planMock).not.toHaveBeenCalled();
  });
});
