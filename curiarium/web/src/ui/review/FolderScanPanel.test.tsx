// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import FolderScanPanel from "./FolderScanPanel";
import { SCAN_EXTS } from "./folderScan";
import type { FolderScanState } from "./useFolderScan";

/** The full state shape; every test overrides only the fields it is about. */
function makeState(overrides: Partial<FolderScanState> = {}): FolderScanState {
  return {
    scanBusy: false,
    scanStripRoot: false,
    setScanStripRoot: vi.fn(),
    folderInputRef: { current: null },
    prepareFromFileInput: vi.fn(),
    chooseFolder: vi.fn(async () => {}),
    rescan: vi.fn(async () => {}),
    resetScan: vi.fn(),
    totalSelected: 0,
    supportedSelected: 0,
    newSelected: 0,
    scanSkipped: 0,
    scanRoot: "",
    scanStatus: null,
    ...overrides,
  };
}

function renderPanel(state: FolderScanState): HTMLElement {
  const { container } = render(<FolderScanPanel {...state} />);
  return container;
}

afterEach(() => {
  cleanup();
});

describe("FolderScanPanel copy", () => {
  it("names the panel and states what a scan does", () => {
    renderPanel(makeState());

    expect(screen.getByText("Local folder scan")).toBeTruthy();
    expect(screen.getByText(/The folder name becomes a top-level folder/i)).toBeTruthy();
  });

  it("lists the supported extensions from the scanner's own allow-list", () => {
    renderPanel(makeState());

    // Read from SCAN_EXTS so a new supported extension cannot silently leave the
    // panel claiming a narrower list than the code accepts.
    expect(
      screen.getByText(
        `Supported extensions: ${SCAN_EXTS.join(", ")}. Selecting a folder uploads immediately.`,
      ),
    ).toBeTruthy();
  });
});

describe("FolderScanPanel controls", () => {
  it("toggles the top-level-folder strip through setScanStripRoot", () => {
    const state = makeState();
    renderPanel(state);

    const toggle = screen.getByLabelText("Skip the top-level folder name") as HTMLInputElement;
    expect(toggle.checked).toBe(false);

    fireEvent.click(toggle);

    expect(state.setScanStripRoot).toHaveBeenCalledTimes(1);
    expect(state.setScanStripRoot).toHaveBeenCalledWith(true);
  });

  it("reflects a checked strip-root toggle", () => {
    renderPanel(makeState({ scanStripRoot: true }));

    expect(
      (screen.getByLabelText("Skip the top-level folder name") as HTMLInputElement).checked,
    ).toBe(true);
  });

  it("opens the folder picker from Choose folder", () => {
    const state = makeState();
    renderPanel(state);

    fireEvent.click(screen.getByRole("button", { name: "Choose folder" }));

    expect(state.chooseFolder).toHaveBeenCalledTimes(1);
  });

  it("wires the hidden directory input to prepareFromFileInput", () => {
    const state = makeState();
    const container = renderPanel(state);

    const input = container.querySelector('input[type="file"]') as HTMLInputElement | null;
    if (!input) throw new Error("No hidden file input");
    // A directory picker needs `multiple`; the directory attributes are added by
    // the hook's effect, which this presentational test does not run.
    expect(input.multiple).toBe(true);
    expect(input.className).toContain("hidden");

    fireEvent.change(input);

    expect(state.prepareFromFileInput).toHaveBeenCalledTimes(1);
  });
});

describe("FolderScanPanel scan button", () => {
  it("reads Scan now before a folder is chosen, and is disabled with nothing supported", () => {
    renderPanel(makeState());

    const button = screen.getByRole("button", { name: "Scan now" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  it("reads Rescan now once files are selected and runs the rescan", () => {
    const state = makeState({ totalSelected: 3, supportedSelected: 2 });
    renderPanel(state);

    const button = screen.getByRole("button", { name: "Rescan now" }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);

    fireEvent.click(button);

    expect(state.rescan).toHaveBeenCalledTimes(1);
  });

  it("reads Uploading... and is disabled while a scan uploads", () => {
    const state = makeState({ scanBusy: true, totalSelected: 3, supportedSelected: 2 });
    renderPanel(state);

    const button = screen.getByRole("button", { name: "Uploading..." }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    fireEvent.click(button);
    expect(state.rescan).not.toHaveBeenCalled();
  });
});

describe("FolderScanPanel Clear", () => {
  it("is disabled with nothing selected", () => {
    renderPanel(makeState());

    expect((screen.getByRole("button", { name: "Clear" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
  });

  it("clears a selection", () => {
    const state = makeState({ totalSelected: 2, supportedSelected: 2 });
    renderPanel(state);

    fireEvent.click(screen.getByRole("button", { name: "Clear" }));

    expect(state.resetScan).toHaveBeenCalledTimes(1);
  });
});

describe("FolderScanPanel selection and status lines", () => {
  it("says no folder was selected yet", () => {
    renderPanel(makeState());

    expect(screen.getByText("No folder selected yet.")).toBeTruthy();
  });

  it("counts the selection, what is new and what was skipped, with the folder name", () => {
    renderPanel(
      makeState({
        totalSelected: 5,
        supportedSelected: 4,
        newSelected: 3,
        scanSkipped: 1,
        scanRoot: "Workshop",
      }),
    );

    expect(
      screen.getByText('Selected 5 file(s) from "Workshop". Ready: 4. New: 3. Skipped: 1.'),
    ).toBeTruthy();
  });

  it("omits the folder name when the selection has no common root", () => {
    renderPanel(makeState({ totalSelected: 2, supportedSelected: 2, newSelected: 2 }));

    expect(screen.getByText("Selected 2 file(s). Ready: 2. New: 2. Skipped: 0.")).toBeTruthy();
  });

  it("shows the uploading status line", () => {
    const container = renderPanel(makeState({ scanBusy: true, scanStatus: "Uploading..." }));

    // The same words sit on the busy button, so pin the status line itself.
    expect(container.querySelector(".text-xs.font-medium")?.textContent).toBe("Uploading...");
  });

  it("shows a partial failure with both counts", () => {
    renderPanel(makeState({ scanStatus: "Uploaded 2 file(s). Failed: 1." }));

    expect(screen.getByText("Uploaded 2 file(s). Failed: 1.")).toBeTruthy();
  });

  it("shows a transport failure", () => {
    renderPanel(makeState({ scanStatus: "Upload failed. Please try again." }));

    expect(screen.getByText("Upload failed. Please try again.")).toBeTruthy();
  });
});
