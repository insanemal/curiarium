// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";

import type { BatchFailure, Project } from "../lib/api";

vi.mock("../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  addProjectAssets: vi.fn(),
  createProject: vi.fn(),
  listProjects: vi.fn(),
}));

import ProjectPickerDialog from "./ProjectPickerDialog";
import { UnauthorizedError, addProjectAssets, createProject, listProjects } from "../lib/api";
import { getToasts, resetToasts } from "../lib/toast";

const addProjectAssetsMock = vi.mocked(addProjectAssets);
const createProjectMock = vi.mocked(createProject);
const listProjectsMock = vi.mocked(listProjects);

function makeProject(overrides: Partial<Project> = {}): Project {
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
    asset_count: 3,
    metadata_json: {},
    total_size: 4096,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    ...overrides,
  };
}

const hydra = makeProject();
const orion = makeProject({ id: "p2", name: "Orion", asset_count: 1 });

const ids = ["a1", "a2", "a3"];

function failure(id: string, error: string): BatchFailure {
  return { id, code: "error", status: 400, error };
}

type HarnessProps = {
  assetIds: string[];
  onClose: () => void;
  onChanged: () => void;
  /** The list owner's append: the server's row for a project this dialog created. */
  onProjectCreated?: (project: Project) => void;
  onUnauthorized: () => void;
};

/** Mounts the picker the way the grid does: `onClose` really unmounts it. */
function Harness({ assetIds, onClose, onChanged, onProjectCreated, onUnauthorized }: HarnessProps) {
  const [open, setOpen] = useState(true);
  if (!open) return null;
  return (
    <ProjectPickerDialog
      assetIds={assetIds}
      onClose={() => {
        setOpen(false);
        onClose();
      }}
      onChanged={onChanged}
      onProjectCreated={onProjectCreated}
      onUnauthorized={onUnauthorized}
    />
  );
}

/**
 * A mount that hands the dialog a prop it does not declare.
 *
 * `ProjectPickerDialog` must never receive the landing callback: the operator here
 * is mid-edit choosing which project to add the selection to, so a callback that
 * switched mode and opened the new project would abandon that selection. Passing
 * `onProjectImported` through a cast keeps the mount compiling while the dialog
 * ignores it — and makes the moment someone wires it up observable, because the spy
 * below starts firing.
 */
function LandingSpyHarness({
  onProjectImported,
  ...harness
}: Omit<HarnessProps, "onClose"> & { onProjectImported?: (project: Project) => void }) {
  const [open, setOpen] = useState(true);
  if (!open) return null;
  const dialogProps = {
    ...harness,
    onClose: () => setOpen(false),
    onProjectImported,
  } as unknown as Parameters<typeof ProjectPickerDialog>[0];
  return <ProjectPickerDialog {...dialogProps} />;
}

function renderPicker(overrides: Partial<Omit<HarnessProps, "assetIds">> & { assetIds?: string[] } = {}) {
  const onClose = overrides.onClose ?? vi.fn();
  const onChanged = overrides.onChanged ?? vi.fn();
  const onProjectCreated = overrides.onProjectCreated;
  const onUnauthorized = overrides.onUnauthorized ?? vi.fn();
  const utils = render(
    <Harness
      assetIds={overrides.assetIds ?? ids}
      onClose={onClose}
      onChanged={onChanged}
      onProjectCreated={onProjectCreated}
      onUnauthorized={onUnauthorized}
    />,
  );
  return { ...utils, onClose, onChanged, onProjectCreated, onUnauthorized };
}

/** Types a name into the inline create field and submits the create-and-add form. */
function createAndAdd(dialog: HTMLElement, name: string): void {
  fireEvent.change(within(dialog).getByRole("textbox", { name: "New project name" }), {
    target: { value: name },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Create and add" }));
}

/** A promise the test settles by hand, for the "request in flight" cases. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.clearAllMocks();
  resetToasts();
  listProjectsMock.mockResolvedValue([hydra, orion]);
  addProjectAssetsMock.mockResolvedValue({ added: ids, skipped: [], failed: [] });
});

afterEach(cleanup);

describe("ProjectPickerDialog", () => {
  it("lists the projects and keeps the add button disabled until one is chosen", async () => {
    const { onClose } = renderPicker();

    const dialog = await screen.findByRole("dialog", { name: "Add to project" });
    await within(dialog).findByRole("radio", { name: "Hydra" });

    expect(within(dialog).getByText("3 files")).toBeTruthy();
    expect(within(dialog).getByText("1 file")).toBeTruthy();

    const disabledButton = within(dialog).getByRole("button", { name: "Add 3 files" }) as HTMLButtonElement;
    expect(disabledButton.disabled).toBe(true);
    fireEvent.click(disabledButton);
    expect(addProjectAssetsMock).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("radio", { name: "Orion" }));
    const enabledButton = within(dialog).getByRole("button", {
      name: 'Add 3 files to "Orion"',
    }) as HTMLButtonElement;
    expect(enabledButton.disabled).toBe(false);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("sends the whole 100-id selection in exactly one request", async () => {
    const many = Array.from({ length: 100 }, (_, index) => `asset-${index + 1}`);
    addProjectAssetsMock.mockResolvedValue({ added: many, skipped: [], failed: [] });
    const onClose = vi.fn();
    renderPicker({ assetIds: many, onClose });

    const dialog = await screen.findByRole("dialog", { name: "Add to project" });
    fireEvent.click(await within(dialog).findByRole("radio", { name: "Hydra" }));
    fireEvent.click(within(dialog).getByRole("button", { name: 'Add 100 files to "Hydra"' }));

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(addProjectAssetsMock).toHaveBeenCalledTimes(1);
    expect(addProjectAssetsMock).toHaveBeenCalledWith("p1", many);
  });

  it("creates a project inline and adds the selection to it", async () => {
    const created = makeProject({ id: "p9", name: "Fresh", asset_count: 0 });
    createProjectMock.mockResolvedValue(created);
    const onChanged = vi.fn();
    const onClose = vi.fn();
    renderPicker({ onChanged, onClose });

    const dialog = await screen.findByRole("dialog", { name: "Add to project" });
    await within(dialog).findByRole("radio", { name: "Hydra" });

    fireEvent.change(within(dialog).getByRole("textbox", { name: "New project name" }), {
      target: { value: "  Fresh  " },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create and add" }));

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(createProjectMock).toHaveBeenCalledTimes(1);
    expect(createProjectMock).toHaveBeenCalledWith({ name: "Fresh" });
    expect(addProjectAssetsMock).toHaveBeenCalledTimes(1);
    expect(addProjectAssetsMock).toHaveBeenCalledWith("p9", ids);
    // Creating the project is itself a change App has to pick up.
    expect(onChanged).toHaveBeenCalled();
    expect(getToasts().some(toast => toast.message === 'Add to "Fresh": 3 added.')).toBe(true);
    expect(screen.queryByRole("dialog", { name: "Add to project" })).toBeNull();
  });

  it("summarises added, skipped and failed members in one toast", async () => {
    addProjectAssetsMock.mockResolvedValue({
      added: ["a1", "a2", "a3"],
      skipped: ["b1", "b2"],
      failed: [failure("c1", "not found")],
    });
    renderPicker();

    const dialog = await screen.findByRole("dialog", { name: "Add to project" });
    fireEvent.click(await within(dialog).findByRole("radio", { name: "Hydra" }));
    fireEvent.click(within(dialog).getByRole("button", { name: 'Add 3 files to "Hydra"' }));

    await waitFor(() =>
      expect(
        getToasts().some(
          toast =>
            toast.message === 'Add to "Hydra": 3 added, 2 already in project, 1 failed.',
        ),
      ).toBe(true),
    );
    const toast = getToasts().find(entry => entry.message.startsWith('Add to "Hydra"'));
    expect(toast?.tone).toBe("info");
    expect(toast?.details).toEqual(["c1: not found"]);
  });

  it("uses an error tone when nothing was added and something failed", async () => {
    addProjectAssetsMock.mockResolvedValue({
      added: [],
      skipped: [],
      failed: [failure("a1", "asset not found")],
    });
    renderPicker();

    const dialog = await screen.findByRole("dialog", { name: "Add to project" });
    fireEvent.click(await within(dialog).findByRole("radio", { name: "Hydra" }));
    fireEvent.click(within(dialog).getByRole("button", { name: 'Add 3 files to "Hydra"' }));

    await waitFor(() =>
      expect(getToasts().some(toast => toast.tone === "error")).toBe(true),
    );
    expect(
      getToasts().some(toast => toast.message === 'Add to "Hydra": 0 added, 1 failed.'),
    ).toBe(true);
  });

  it("closes on Escape when idle", async () => {
    const onClose = vi.fn();
    renderPicker({ onClose });

    const dialog = await screen.findByRole("dialog", { name: "Add to project" });
    await within(dialog).findByRole("radio", { name: "Hydra" });
    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("ignores Escape while the add request is in flight", async () => {
    const pending = deferred<{
      added: string[];
      skipped: string[];
      failed: BatchFailure[];
    }>();
    addProjectAssetsMock.mockReturnValue(pending.promise);
    const onClose = vi.fn();
    renderPicker({ onClose });

    const dialog = await screen.findByRole("dialog", { name: "Add to project" });
    fireEvent.click(await within(dialog).findByRole("radio", { name: "Hydra" }));
    fireEvent.click(within(dialog).getByRole("button", { name: 'Add 3 files to "Hydra"' }));

    await within(dialog).findByRole("button", { name: "Adding…" });
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();

    await act(async () => {
      pending.resolve({ added: ids, skipped: [], failed: [] });
    });
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it("reports a 401 from listProjects as unauthorized", async () => {
    listProjectsMock.mockRejectedValue(new UnauthorizedError());
    const onUnauthorized = vi.fn();
    renderPicker({ onUnauthorized });

    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("dialog", { name: "Add to project" })).toBeTruthy();
  });

  it("keeps the created project and the dialog open when the add fails", async () => {
    const created = makeProject({ id: "p9", name: "Fresh", asset_count: 0 });
    createProjectMock.mockResolvedValue(created);
    addProjectAssetsMock.mockRejectedValue(
      new Error("Editor or administrator access required"),
    );
    const onChanged = vi.fn();
    const onClose = vi.fn();
    renderPicker({ onChanged, onClose });

    const dialog = await screen.findByRole("dialog", { name: "Add to project" });
    await within(dialog).findByRole("radio", { name: "Hydra" });

    fireEvent.change(within(dialog).getByRole("textbox", { name: "New project name" }), {
      target: { value: "Fresh" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create and add" }));

    await waitFor(() => expect(addProjectAssetsMock).toHaveBeenCalledTimes(1));
    expect(onClose).not.toHaveBeenCalled();
    expect(onChanged).toHaveBeenCalledTimes(1);

    const fresh = (await within(dialog).findByRole("radio", { name: "Fresh" })) as HTMLInputElement;
    expect(fresh.checked).toBe(true);
    expect(
      getToasts().some(
        toast =>
          toast.tone === "error" &&
          toast.message === "Editor or administrator access required",
      ),
    ).toBe(true);
  });

  it("hands the list owner the server's own row for the project it created", async () => {
    // Values no client could invent: the server's own member count and its
    // cache-busted cover path. A hand-off built from the typed name would carry
    // neither, so these two fields are what "the server's row" actually means.
    const createdRow = makeProject({
      id: "p9",
      name: "Fresh",
      asset_count: 7,
      cover_thumb_url: "/thumbs/x.jpg?v=3",
    });
    createProjectMock.mockResolvedValue(createdRow);
    const onProjectCreated = vi.fn();
    const onClose = vi.fn();
    renderPicker({ onProjectCreated, onClose });

    const dialog = await screen.findByRole("dialog", { name: "Add to project" });
    await within(dialog).findByRole("radio", { name: "Hydra" });
    createAndAdd(dialog, "Fresh");

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(onProjectCreated).toHaveBeenCalledTimes(1);
    expect(onProjectCreated).toHaveBeenCalledWith(createdRow);

    const handed = onProjectCreated.mock.calls[0][0];
    // Identity, not structural equality: the hand-off is the very object
    // `createProject` resolved, so nothing rebuilt it on the way out.
    expect(handed).toBe(createdRow);
    expect(handed.asset_count).toBe(7);
    expect(handed.cover_thumb_url).toBe("/thumbs/x.jpg?v=3");
  });

  it("appends AND bumps — the append never replaces the existing notification", async () => {
    const createdRow = makeProject({ id: "p9", name: "Fresh", asset_count: 0 });
    createProjectMock.mockResolvedValue(createdRow);
    // One ordered tape for both channels, so a swap or a drop shows up as an
    // unexpected entry rather than a passing `toHaveBeenCalled`.
    const reported: string[] = [];
    const onChanged = vi.fn(() => reported.push("onChanged"));
    const onProjectCreated = vi.fn(() => reported.push("onProjectCreated"));
    const onClose = vi.fn();
    renderPicker({ onChanged, onProjectCreated, onClose });

    const dialog = await screen.findByRole("dialog", { name: "Add to project" });
    await within(dialog).findByRole("radio", { name: "Hydra" });
    createAndAdd(dialog, "Fresh");

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    // The bump is the pre-existing half of this contract (it reaches App as
    // `onProjectsChanged`, which bumps `assetsVersion`); the append sits beside it.
    expect(onChanged).toHaveBeenCalled();
    expect(onProjectCreated).toHaveBeenCalledTimes(1);
    // Bump first, then the append, then the membership bump from `addToProject`
    // itself — the create's report did not consume the add's.
    expect(reported).toEqual(["onChanged", "onProjectCreated", "onChanged"]);
  });

  it("keeps the created row selectable in the dialog's own list", async () => {
    const createdRow = makeProject({ id: "p9", name: "Fresh", asset_count: 7 });
    createProjectMock.mockResolvedValue(createdRow);
    // A failing add keeps the dialog mounted, which is the state the private append
    // exists for. App's own append is not a substitute: the radio can only be
    // checked against a row this dialog is still rendering.
    addProjectAssetsMock.mockRejectedValue(
      new Error("Editor or administrator access required"),
    );
    const onClose = vi.fn();
    renderPicker({ onClose });

    const dialog = await screen.findByRole("dialog", { name: "Add to project" });
    await within(dialog).findByRole("radio", { name: "Hydra" });
    createAndAdd(dialog, "Fresh");

    await waitFor(() => expect(addProjectAssetsMock).toHaveBeenCalledTimes(1));
    expect(onClose).not.toHaveBeenCalled();

    const fresh = (await within(dialog).findByRole("radio", { name: "Fresh" })) as HTMLInputElement;
    expect(fresh.checked).toBe(true);
    const hydra = within(dialog).getByRole("radio", { name: "Hydra" }) as HTMLInputElement;
    expect(hydra.checked).toBe(false);
    // The server's count, so the row this list shows is the row the server sent.
    expect(within(dialog).getByText("7 files")).toBeTruthy();
    // And the add button still names it: the operator can add the selection to the
    // project they just made without leaving the dialog.
    expect(within(dialog).getByRole("button", { name: 'Add 3 files to "Fresh"' })).toBeTruthy();
  });

  it("offers the list owner an append and nothing that lands on the project", async () => {
    // The pin: a bump and an append are safe mid-edit, a landing is not. Flipping
    // mode and opening the project would abandon the selection the operator is
    // choosing, so `onProjectImported` must never be reached from this dialog.
    const createdRow = makeProject({ id: "p9", name: "Fresh", asset_count: 0 });
    createProjectMock.mockResolvedValue(createdRow);
    const landingSpy = vi.fn();
    const onChanged = vi.fn();
    const onProjectCreated = vi.fn();

    const handoff = {
      assetIds: ids,
      onChanged,
      onProjectCreated,
      onUnauthorized: vi.fn(),
      onProjectImported: landingSpy,
    };

    // Create-and-add path.
    const first = render(<LandingSpyHarness {...handoff} />);
    const createDialog = await first.findByRole("dialog", { name: "Add to project" });
    createAndAdd(createDialog, "Fresh");
    await waitFor(() => expect(onProjectCreated).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(addProjectAssetsMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(first.queryByRole("dialog")).toBeNull());

    // Plain add path, on a fresh mount: the create closed the first dialog.
    const second = render(<LandingSpyHarness {...handoff} />);
    const addDialog = await second.findByRole("dialog", { name: "Add to project" });
    fireEvent.click(await within(addDialog).findByRole("radio", { name: "Hydra" }));
    fireEvent.click(within(addDialog).getByRole("button", { name: 'Add 3 files to "Hydra"' }));
    await waitFor(() => expect(addProjectAssetsMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(second.queryByRole("dialog")).toBeNull());

    expect(landingSpy).not.toHaveBeenCalled();
    // Read the pair as "bump yes, landing no": the list owner was told, and nobody
    // was told to navigate.
    expect(onChanged).toHaveBeenCalled();
    expect(onProjectCreated).toHaveBeenCalledTimes(1);
  });

  it("hands the list owner the created row even when the add that follows fails", async () => {
    const createdRow = makeProject({ id: "p9", name: "Fresh", asset_count: 0 });
    createProjectMock.mockResolvedValue(createdRow);
    addProjectAssetsMock.mockRejectedValue(
      new Error("Editor or administrator access required"),
    );
    const onChanged = vi.fn();
    const onProjectCreated = vi.fn();
    const onClose = vi.fn();
    renderPicker({ onChanged, onProjectCreated, onClose });

    const dialog = await screen.findByRole("dialog", { name: "Add to project" });
    await within(dialog).findByRole("radio", { name: "Hydra" });
    createAndAdd(dialog, "Fresh");

    await waitFor(() => expect(addProjectAssetsMock).toHaveBeenCalledTimes(1));
    // The project really exists — a failed add does not un-create it — so the list
    // owner has to be handed the row even though nothing was ever added to it.
    expect(onProjectCreated).toHaveBeenCalledTimes(1);
    expect(onProjectCreated).toHaveBeenCalledWith(createdRow);
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();
  });
});
