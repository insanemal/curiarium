// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  deleteFolder: vi.fn(),
  getFolderDeletePreview: vi.fn(),
}));

import FolderDeleteDialog from "./FolderDeleteDialog";
import { deleteFolder, getFolderDeletePreview, type FolderDeletePreview, type FolderDeleteResult } from "../lib/api";

const getFolderDeletePreviewMock = vi.mocked(getFolderDeletePreview);
const deleteFolderMock = vi.mocked(deleteFolder);

const folder = { id: "f1", name: "Prints" };

const BASE_PREVIEW: FolderDeletePreview = {
  folders: 2,
  assets: 3,
  files: 5,
  bytes: 4096,
  in_trash: false,
  // BASE_PREVIEW describes a folder with no directory on disk, so no bytes move.
  would_move_bytes: false,
};

type Overrides = {
  onCancel?: () => void;
  onDeleted?: (result: FolderDeleteResult) => void;
};

function renderDialog(overrides: Overrides = {}) {
  const onCancel = vi.fn(overrides.onCancel ?? (() => {}));
  const onDeleted = vi.fn(overrides.onDeleted ?? (() => {}));
  render(
    <FolderDeleteDialog
      folder={folder}
      onCancel={onCancel}
      onDeleted={onDeleted}
    />,
  );
  return { onCancel, onDeleted };
}

function dialog() {
  return within(screen.getByRole("dialog", { name: /Delete folder/ }));
}

function confirmInput(): HTMLInputElement {
  return dialog().getByLabelText("Type the folder name to confirm") as HTMLInputElement;
}

function deleteButton(): HTMLButtonElement {
  return dialog().getByRole("button", { name: "Delete folder" }) as HTMLButtonElement;
}

describe("FolderDeleteDialog", () => {
  beforeEach(() => {
    getFolderDeletePreviewMock.mockReset();
    getFolderDeletePreviewMock.mockResolvedValue(BASE_PREVIEW);
    deleteFolderMock.mockReset();
    deleteFolderMock.mockResolvedValue({
      ok: true,
      trash_path: ".trash/20261027-abc-Prints",
      folders: 2,
      assets: 3,
      bytes: 4096,
    });
  });

  afterEach(cleanup);

  it("says nothing moves to the trash when the folder has no directory", async () => {
    getFolderDeletePreviewMock.mockResolvedValue({ ...BASE_PREVIEW, storage_path: null });
    renderDialog();

    expect(
      await dialog().findByText(
        "This folder is not linked to a directory on disk. Deleting it removes only its records - nothing is moved to .trash.",
      ),
    ).toBeTruthy();
    // The .trash promises are gone, not just accompanied by the new sentence.
    expect(dialog().queryByText(/first moved to the server/)).toBeNull();
    expect(dialog().queryByText(/move the folder back from/)).toBeNull();
  });

  it("shows the linked directory", async () => {
    getFolderDeletePreviewMock.mockResolvedValue({
      ...BASE_PREVIEW,
      storage_path: "Parent/Child",
    });
    renderDialog();

    const path = await dialog().findByText("Parent/Child");
    expect(path.tagName).toBe("CODE");
    expect(dialog().getByText(/first moved to the server/)).toBeTruthy();
    // The trash queue is surfaced under REVIEW → Trash now, never under the old
    // storage location the copy used to name.
    expect(dialog().getByText(/REVIEW → Trash/)).toBeTruthy();
    expect(dialog().queryByText(/Settings → Storage/)).toBeNull();
    expect(dialog().queryByText(/not linked to a directory on disk/)).toBeNull();
  });

  it("says a name-derived directory moves to the trash when an unlinked folder still has bytes", async () => {
    getFolderDeletePreviewMock.mockResolvedValue({
      ...BASE_PREVIEW,
      storage_path: null,
      would_move_bytes: true,
    });
    renderDialog();

    // The unlinked flag stays, but the "nothing moves" claim must be gone.
    expect(await dialog().findByText(/not linked to a directory on disk/)).toBeTruthy();
    expect(dialog().queryByText(/nothing is moved to .trash/)).toBeNull();
    expect(dialog().queryByText(/removes only its records/)).toBeNull();

    expect(dialog().getByText(/a directory for it does exist in storage/)).toBeTruthy();
    expect(dialog().getByText(/moves that directory into the server/)).toBeTruthy();
    // Same location line as the linked-directory branch, never the old one.
    expect(dialog().getByText(/REVIEW → Trash/)).toBeTruthy();
    expect(dialog().queryByText(/Settings → Storage/)).toBeNull();
    // The bytes really do land in .trash, so the restore caveat applies.
    expect(dialog().getByText(/move the folder back from/)).toBeTruthy();
  });

  it("deletes only once the folder name is typed and reports the result", async () => {
    const { onDeleted } = renderDialog();

    // The counts come from the preview, not from a guess.
    expect(await dialog().findByText("2 folders")).toBeTruthy();
    expect(dialog().getByText("3 tracked assets")).toBeTruthy();
    expect(deleteButton().disabled).toBe(true);

    fireEvent.change(confirmInput(), { target: { value: "Prints" } });
    expect(deleteButton().disabled).toBe(false);

    fireEvent.click(deleteButton());
    await waitFor(() => expect(deleteFolderMock).toHaveBeenCalledWith("f1", "Prints"));
    await waitFor(() => expect(onDeleted).toHaveBeenCalledTimes(1));
  });

  it("names the snapshot pattern when one is taken before the folder moves", async () => {
    getFolderDeletePreviewMock.mockResolvedValue({
      ...BASE_PREVIEW,
      snapshot: {
        reason: "predelete",
        policy: "strict",
        capability: "available",
        action: "snapshot",
        pattern: "<YYYYmmddTHHMMSSZ>-predelete-<6 hex>",
      },
    });

    renderDialog();

    expect(
      await dialog().findByText(/A filesystem snapshot of this folder is taken before it moves/),
    ).toBeTruthy();
    expect(dialog().getByText("<YYYYmmddTHHMMSSZ>-predelete-<6 hex>")).toBeTruthy();
    expect(dialog().getByText(/The exact name is recorded in the audit log/)).toBeTruthy();
    // The strict clause says a failed snapshot stops the delete.
    expect(dialog().getByText(/the delete is refused and nothing is removed/)).toBeTruthy();

    fireEvent.change(confirmInput(), { target: { value: "Prints" } });
    expect(deleteButton().disabled).toBe(false);
  });

  it("warns that the delete goes ahead without a snapshot when none can be taken", async () => {
    getFolderDeletePreviewMock.mockResolvedValue({
      ...BASE_PREVIEW,
      snapshot: {
        reason: "predelete",
        policy: "warn",
        capability: "unavailable",
        action: "proceed_without",
        pattern: "<YYYYmmddTHHMMSSZ>-predelete-<6 hex>",
        detail: "the volume is a network mount",
      },
    });

    renderDialog();

    expect(
      await dialog().findByText(/No snapshot can be taken on this storage volume, because the volume is a network mount/),
    ).toBeTruthy();
    expect(dialog().getByText(/The delete still goes ahead/)).toBeTruthy();
    expect(dialog().getByText(/the missed snapshot is recorded in the audit log/)).toBeTruthy();

    fireEvent.change(confirmInput(), { target: { value: "Prints" } });
    expect(deleteButton().disabled).toBe(false);
  });

  it("refuses the delete when the deployment demands a snapshot it cannot take", async () => {
    getFolderDeletePreviewMock.mockResolvedValue({
      ...BASE_PREVIEW,
      snapshot: {
        reason: "predelete",
        policy: "strict",
        capability: "unavailable",
        action: "refuse",
        pattern: "<YYYYmmddTHHMMSSZ>-predelete-<6 hex>",
      },
    });
    const { onDeleted } = renderDialog();

    expect(
      await dialog().findByText(/this deployment refuses a folder delete without one. Nothing will be deleted/),
    ).toBeTruthy();

    fireEvent.change(confirmInput(), { target: { value: "Prints" } });
    expect(deleteButton().disabled).toBe(true);
    expect(deleteFolderMock).not.toHaveBeenCalled();
    expect(onDeleted).not.toHaveBeenCalled();
  });
});
