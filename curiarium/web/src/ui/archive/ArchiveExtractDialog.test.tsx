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

/**
 * A promise the test settles by hand. Typed at the call site: an untyped
 * deferred infers `unknown` and fails TS2345 where the mock wants
 * `Promise<ArchiveListing>` / `Promise<ArchiveExtractResult>`.
 */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(settle => {
    resolve = settle;
  });
  return { promise, resolve };
}

/** The action's own busy-stage focus home, found structurally. */
function busyHome(control: HTMLElement): HTMLElement {
  const home = control.closest<HTMLElement>('[tabindex="-1"]');
  if (!home) throw new Error("the control has no tabindex=-1 container");
  return home;
}

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

  it("takes a forward Tab from the listing stage's unlisted home to the first member", async () => {
    const listing = deferred<Awaited<ReturnType<typeof listArchiveEntries>>>();
    // Never settles, so the stage stays `listing` with every control disabled.
    listArchiveEntriesMock.mockReturnValue(listing.promise);
    const { onClose } = renderDialog();

    // jsdom does not focus on a click; focus first so the press disables the
    // active element and the action's recovery effect re-homes onto its
    // container, which is inside the panel but not in the Tab ring.
    const trigger = screen.getByRole("button", { name: "Extract Moai.zip" });
    trigger.focus();
    fireEvent.click(trigger);

    // The accessible name is pinned by the button's `aria-label`, so the busy
    // stage is found by its visible text, not its role name.
    const listingControl = await screen.findByRole("button", { name: "Extract Moai.zip" });
    await waitFor(() => expect(listingControl.textContent).toBe("Listing..."));
    const home = busyHome(listingControl);
    await waitFor(() => expect(document.activeElement).toBe(home));

    const dialog = screen.getByRole("dialog");
    const close = within(dialog).getByRole("button", { name: "Close" });
    expect(home).not.toBe(dialog);

    fireEvent.keyDown(home, { key: "Tab" });

    expect(document.activeElement).toBe(close);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("takes a backward Tab from the listing stage's unlisted home to the member before it", async () => {
    const listing = deferred<Awaited<ReturnType<typeof listArchiveEntries>>>();
    listArchiveEntriesMock.mockReturnValue(listing.promise);
    const { onClose } = renderDialog();

    const trigger = screen.getByRole("button", { name: "Extract Moai.zip" });
    trigger.focus();
    fireEvent.click(trigger);

    const listingControl = await screen.findByRole("button", { name: "Extract Moai.zip" });
    await waitFor(() => expect(listingControl.textContent).toBe("Listing..."));
    const home = busyHome(listingControl);
    await waitFor(() => expect(document.activeElement).toBe(home));

    const dialog = screen.getByRole("dialog");
    const close = within(dialog).getByRole("button", { name: "Close" });

    fireEvent.keyDown(home, { key: "Tab", shiftKey: true });

    expect(document.activeElement).toBe(close);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("keeps both Tab directions inside the extracting stage's unlisted home", async () => {
    const extracting = deferred<Awaited<ReturnType<typeof extractArchive>>>();
    extractArchiveMock.mockReturnValue(extracting.promise);
    const { onClose } = renderDialog();

    const trigger = screen.getByRole("button", { name: "Extract Moai.zip" });
    trigger.focus();
    fireEvent.click(trigger);
    const confirm = await screen.findByRole("button", { name: "Extract 2 files" });
    confirm.focus();
    fireEvent.click(confirm);

    const extractingControl = await screen.findByRole("button", { name: "Extracting..." });
    expect((extractingControl as HTMLButtonElement).disabled).toBe(true);
    const home = busyHome(extractingControl);
    await waitFor(() => expect(document.activeElement).toBe(home));

    const dialog = screen.getByRole("dialog");
    const close = within(dialog).getByRole("button", { name: "Close" });

    fireEvent.keyDown(home, { key: "Tab" });
    expect(document.activeElement).toBe(close);

    home.focus();
    fireEvent.keyDown(home, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(close);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("takes a forward Tab from the batch progress paragraph to the member after it", () => {
    renderDialog({ targets: [TARGET, { path: "Buddha.zip", label: "Buddha.zip" }] });
    const dialog = screen.getByRole("dialog");
    const trigger = within(dialog).getByRole("button", { name: "Extract Moai.zip" });
    const close = within(dialog).getByRole("button", { name: "Close" });
    const progress = screen.getByRole("status");

    progress.focus();
    fireEvent.keyDown(progress, { key: "Tab" });

    expect(document.activeElement).toBe(trigger);
    expect(document.activeElement).not.toBe(close);
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
