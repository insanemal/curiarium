// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  listArchiveEntries: vi.fn(),
  extractArchive: vi.fn(),
}));

import { extractArchive, listArchiveEntries, type ArchiveExtractResult } from "../../lib/api";
import ArchiveExtractAction from "./ArchiveExtractAction";

const listArchiveEntriesMock = vi.mocked(listArchiveEntries);
const extractArchiveMock = vi.mocked(extractArchive);

const ENTRIES = [
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

/** List, confirm and extract: the whole happy path through the control. */
async function extractOnce(): Promise<void> {
  fireEvent.click(screen.getByRole("button", { name: "Extract Moai.zip" }));
  await screen.findByText(/extract into a new folder beside the archive\?$/);
  fireEvent.click(screen.getByRole("button", { name: "Extract 2 files" }));
}

beforeEach(() => {
  listArchiveEntriesMock.mockReset();
  listArchiveEntriesMock.mockResolvedValue({ path: "Moai.zip", entries: ENTRIES });
  extractArchiveMock.mockReset();
  extractArchiveMock.mockResolvedValue(RESULT);
});

afterEach(cleanup);

describe("ArchiveExtractAction", () => {
  it("renders nothing at all without the contribute capability", () => {
    const { container } = render(
      <ArchiveExtractAction canContribute={false} path="Moai.zip" label="Moai.zip" />,
    );

    expect(container.innerHTML).toBe("");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("lists the archive, confirms the count and extracts the whole archive", async () => {
    const onExtracted = vi.fn();
    render(
      <ArchiveExtractAction
        canContribute
        path="Moai.zip"
        label="Moai.zip"
        onExtracted={onExtracted}
      />,
    );

    const button = screen.getByRole("button", { name: "Extract Moai.zip" });
    fireEvent.click(button);

    await waitFor(() => expect(listArchiveEntriesMock).toHaveBeenCalledWith("Moai.zip"));
    expect(await screen.findByText("2 files, 2.5 KB — extract into a new folder beside the archive?")).toBeTruthy();
    // Listing alone writes nothing.
    expect(extractArchiveMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Extract 2 files" }));

    await waitFor(() =>
      expect(extractArchiveMock).toHaveBeenCalledWith({ path: "Moai.zip", entries: null }),
    );
    const status = await screen.findByRole("status");
    expect(status.textContent).toBe("Extracted 2 files. A library scan has started.");
    expect(status.getAttribute("aria-live")).toBe("polite");
  });

  it("says nothing was written when the destination already exists", async () => {
    extractArchiveMock.mockResolvedValue({ ...RESULT, already_present: true, scan_started: false });
    render(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);

    await extractOnce();

    const status = await screen.findByRole("status");
    expect(status.textContent).toBe("Already extracted - nothing to do.");
    expect(status.textContent).not.toContain("Extracted");
  });

  it("calls onExtracted exactly once on success", async () => {
    const onExtracted = vi.fn();
    render(
      <ArchiveExtractAction
        canContribute
        path="Moai.zip"
        label="Moai.zip"
        onExtracted={onExtracted}
      />,
    );

    await extractOnce();

    await waitFor(() => expect(onExtracted).toHaveBeenCalledTimes(1));
    expect(onExtracted).toHaveBeenCalledWith(RESULT);
  });

  it("surfaces a 409's blocking paths as the error text", async () => {
    const message = "Refusing to overwrite: Moai.stl, readme.txt";
    extractArchiveMock.mockRejectedValue(new Error(message));
    const onExtracted = vi.fn();
    render(
      <ArchiveExtractAction
        canContribute
        path="Moai.zip"
        label="Moai.zip"
        onExtracted={onExtracted}
      />,
    );

    await extractOnce();

    expect(await screen.findByText(message)).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull();
    expect(onExtracted).not.toHaveBeenCalled();
  });

  it("cancelling the confirmation leaves the archive untouched", async () => {
    render(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);

    fireEvent.click(screen.getByRole("button", { name: "Extract Moai.zip" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(screen.getByRole("button", { name: "Extract Moai.zip" })).toBeTruthy();
    expect(extractArchiveMock).not.toHaveBeenCalled();
  });
});
