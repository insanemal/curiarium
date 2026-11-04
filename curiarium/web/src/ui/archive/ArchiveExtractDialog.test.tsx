// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("../../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  listArchiveEntries: vi.fn(),
  extractArchive: vi.fn(),
}));

import { extractArchive, listArchiveEntries, type ArchiveExtractResult } from "../../lib/api";
import ArchiveExtractDialog, { type ExtractTarget } from "./ArchiveExtractDialog";

const listArchiveEntriesMock = vi.mocked(listArchiveEntries);
const extractArchiveMock = vi.mocked(extractArchive);

const ARCHIVE_ENTRIES = [
  { path: "Moai.stl", size: 2048 },
  { path: "readme.txt", size: 512 },
];

const RESULT: ArchiveExtractResult = {
  path: "Moai.zip",
  destination: "Moai",
  files: 2,
  bytes: 2560,
  already_present: false,
  scan_started: true,
};

const TARGET: ExtractTarget = { path: "Moai.zip", label: "Moai.zip" };

/** List, confirm and extract through the shared action. */
async function extractOnce(label = "Moai.zip"): Promise<void> {
  fireEvent.click(screen.getByRole("button", { name: `Extract ${label}` }));
  await screen.findByText(/extract into a new folder beside the archive\?$/);
  fireEvent.click(screen.getByRole("button", { name: "Extract 2 files" }));
}

function renderDialog(overrides: {
  targets?: ExtractTarget[];
  canContribute?: boolean;
  onExtracted?: (results: ArchiveExtractResult[]) => void;
  onClose?: () => void;
} = {}) {
  const onExtracted = overrides.onExtracted ?? vi.fn();
  const onClose = overrides.onClose ?? vi.fn();
  render(
    <ArchiveExtractDialog
      targets={overrides.targets ?? [TARGET]}
      canContribute={overrides.canContribute ?? true}
      onExtracted={onExtracted}
      onClose={onClose}
    />,
  );
  return { onExtracted, onClose };
}

beforeEach(() => {
  listArchiveEntriesMock.mockReset();
  listArchiveEntriesMock.mockImplementation(async (path: string) => ({
    path,
    entries: ARCHIVE_ENTRIES,
  }));
  extractArchiveMock.mockReset();
  extractArchiveMock.mockResolvedValue(RESULT);
});

afterEach(cleanup);

describe("ArchiveExtractDialog", () => {
  it("extracts through the shared action and reports the result to its caller", async () => {
    const onExtracted = vi.fn();
    renderDialog({ onExtracted });

    await extractOnce();

    await waitFor(() => expect(onExtracted).toHaveBeenCalledTimes(1));
    expect(extractArchiveMock).toHaveBeenCalledWith({ path: "Moai.zip", entries: null });
  });

  it("closes on Escape", () => {
    const onClose = vi.fn();
    renderDialog({ onClose });

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps Tab inside the panel", () => {
    const { onClose } = renderDialog();
    const dialog = screen.getByRole("dialog");
    const close = within(dialog).getByRole("button", { name: "Close" });
    const extract = within(dialog).getByRole("button", { name: "Extract Moai.zip" });

    expect(document.activeElement).toBe(close);

    extract.focus();
    fireEvent.keyDown(extract, { key: "Tab" });

    expect(document.activeElement).toBe(close);
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("keeps the first control focused on mount, before any advance", () => {
    renderDialog();
    const dialog = screen.getByRole("dialog", { name: "Extract Moai.zip" });
    const close = within(dialog).getByRole("button", { name: "Close" });

    // The recovery effect must not replace the documented mount behaviour.
    expect(document.activeElement).toBe(close);
  });

  it("keeps focus inside the dialog when a batch advances to the next archive", async () => {
    const second: ArchiveExtractResult = {
      ...RESULT,
      path: "Buddha.zip",
      destination: "Buddha",
    };
    extractArchiveMock.mockResolvedValueOnce(RESULT).mockResolvedValueOnce(second);
    renderDialog({ targets: [TARGET, { path: "Buddha.zip", label: "Buddha.zip" }] });

    // jsdom does not move focus on a click, a browser does. Focus each control
    // by hand before pressing it, or the button whose removal this test is
    // about was never the active element and focus could never be lost.
    const open = screen.getByRole("button", { name: "Extract Moai.zip" });
    open.focus();
    fireEvent.click(open);
    const confirm = await screen.findByRole("button", { name: "Extract 2 files" });
    confirm.focus();
    fireEvent.click(confirm);

    expect(await screen.findByText("Archive 2 of 2 - Buddha.zip")).toBeTruthy();

    const dialog = screen.getByRole("dialog", { name: "Extract 2 archives" });
    // The pressed button remounted away, so focus has to be re-homed inside the
    // panel - from <body> neither the trap nor Escape would ever reach it.
    expect(document.activeElement).not.toBe(document.body);
    expect(dialog.contains(document.activeElement)).toBe(true);

    const tabbable = within(dialog)
      .getAllByRole("button")
      .filter(button => !(button as HTMLButtonElement).disabled);
    const last = tabbable[tabbable.length - 1];
    last.focus();
    fireEvent.keyDown(last, { key: "Tab" });

    expect(document.activeElement).toBe(tabbable[0]);
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it("returns the single result as an array", async () => {
    const onExtracted = vi.fn();
    renderDialog({ onExtracted });

    await extractOnce();

    await waitFor(() => expect(onExtracted).toHaveBeenCalledTimes(1));
    const reported = onExtracted.mock.calls[0][0];
    expect(reported).toHaveLength(1);
    expect(reported[0]).toEqual(RESULT);
  });

  it("is named by its heading", () => {
    renderDialog();

    expect(screen.getByRole("dialog", { name: "Extract Moai.zip" })).toBeTruthy();
  });

  it("walks a batch one archive at a time and reports them in order", async () => {
    const onExtracted = vi.fn();
    const second: ArchiveExtractResult = {
      ...RESULT,
      path: "Buddha.zip",
      destination: "Buddha",
    };
    extractArchiveMock.mockResolvedValueOnce(RESULT).mockResolvedValueOnce(second);
    renderDialog({
      targets: [TARGET, { path: "Buddha.zip", label: "Buddha.zip" }],
      onExtracted,
    });

    expect(screen.getByText("Archive 1 of 2 - Moai.zip")).toBeTruthy();
    await extractOnce("Moai.zip");

    expect(await screen.findByText("Archive 2 of 2 - Buddha.zip")).toBeTruthy();
    expect(onExtracted).not.toHaveBeenCalled();

    await extractOnce("Buddha.zip");

    await waitFor(() => expect(onExtracted).toHaveBeenCalledTimes(1));
    const reported = onExtracted.mock.calls[0][0];
    expect(reported).toHaveLength(2);
    expect(reported[0]).toEqual(RESULT);
    expect(reported[1]).toEqual(second);
  });
});
