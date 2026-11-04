// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

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
    // Zero files here too: both nothing-written outcomes report `files: 0`, and
    // this branch has to be read first or it would borrow the junk-only wording.
    extractArchiveMock.mockResolvedValue({
      ...RESULT,
      files: 0,
      bytes: 0,
      already_present: true,
      scan_started: false,
    });
    render(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);

    await extractOnce();

    const status = await screen.findByRole("status");
    expect(status.textContent).toBe("Already extracted - nothing to do.");
    expect(status.textContent).not.toContain("Extracted");
  });

  it("says nothing was written when the archive held no extractable files", async () => {
    // Junk-only: the server writes nothing and starts no scan, but it is not
    // `already_present` either, so it needs its own line - and no scan claim.
    extractArchiveMock.mockResolvedValue({
      ...RESULT,
      files: 0,
      bytes: 0,
      already_present: false,
      scan_started: false,
    });
    render(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);

    await extractOnce();

    const status = await screen.findByRole("status");
    expect(status.textContent).toContain("Nothing was written");
    expect(status.textContent).not.toContain("A library scan");
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

describe("keyboard focus across stages", () => {
  /** A promise the test settles itself, to observe a stage that lasts one request. */
  function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
    let resolve!: (value: T) => void;
    const promise = new Promise<T>(res => {
      resolve = res;
    });
    return { promise, resolve };
  }

  function trigger(): HTMLElement {
    return screen.getByRole("button", { name: "Extract Moai.zip" });
  }

  /** The idle/done/error primary control: visible label "Extract archive". */
  function archiveButton(): HTMLElement {
    const button = trigger();
    expect(button.textContent).toBe("Extract archive");
    return button;
  }

  /** The idle/listing/done/error root, found through the trigger it holds. */
  function idleContainer(): HTMLElement {
    const parent = trigger().parentElement;
    if (!parent) throw new Error("the trigger has no container");
    return parent;
  }

  /** The confirm/extracting root, found through the prompt it holds. */
  function confirmContainer(): HTMLElement {
    const parent = screen.getByText(/extract into a new folder beside the archive\?$/)
      .parentElement;
    if (!parent) throw new Error("the prompt has no container");
    return parent;
  }

  it("keeps focus on each stage's own control from idle through done", async () => {
    const listing = deferred<Awaited<ReturnType<typeof listArchiveEntries>>>();
    listArchiveEntriesMock.mockReturnValue(listing.promise);
    const extracting = deferred<ArchiveExtractResult>();
    extractArchiveMock.mockReturnValue(extracting.promise);

    render(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);

    const idle = idleContainer();
    trigger().focus();
    fireEvent.click(trigger());

    // `listing` disables the only control: its container is the focus home.
    await waitFor(() => expect(document.activeElement).toBe(idle));
    expect(document.activeElement).not.toBe(trigger());
    expect(document.activeElement).not.toBe(document.body);
    expect((trigger() as HTMLButtonElement).disabled).toBe(true);

    await act(async () => {
      listing.resolve({ path: "Moai.zip", entries: ENTRIES });
    });

    const confirmButton = await screen.findByRole("button", { name: "Extract 2 files" });
    await waitFor(() => expect(document.activeElement).toBe(confirmButton));

    confirmButton.focus();
    fireEvent.click(confirmButton);

    // `extracting` disables every control: the panel itself is the home again.
    const extractingButton = screen.getByRole("button", {
      name: "Extracting...",
    }) as HTMLButtonElement;
    const cancelButton = screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement;
    expect(extractingButton.disabled).toBe(true);
    expect(cancelButton.disabled).toBe(true);
    const panel = confirmContainer();
    await waitFor(() => expect(document.activeElement).toBe(panel));
    expect(document.activeElement).not.toBe(extractingButton);
    expect(document.activeElement).not.toBe(document.body);

    await act(async () => {
      extracting.resolve(RESULT);
    });

    const status = await screen.findByRole("status");
    expect(status.textContent).toBe("Extracted 2 files. A library scan has started.");
    await waitFor(() =>
      expect(document.activeElement).toBe(archiveButton()),
    );
  });

  it("re-homes onto the trigger when the extraction finishes", async () => {
    // `extracting -> done` unmounts the confirmation panel under the caret, so
    // this pins that transition on its own rather than behind the listing one.
    const extracting = deferred<ArchiveExtractResult>();
    extractArchiveMock.mockReturnValue(extracting.promise);
    render(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);

    trigger().focus();
    fireEvent.click(trigger());
    const confirmButton = await screen.findByRole("button", { name: "Extract 2 files" });
    confirmButton.focus();
    fireEvent.click(confirmButton);

    await waitFor(() => expect(document.activeElement).toBe(confirmContainer()));
    expect(document.activeElement).not.toBe(document.body);

    await act(async () => {
      extracting.resolve(RESULT);
    });

    expect((await screen.findByRole("status")).textContent).toBe(
      "Extracted 2 files. A library scan has started.",
    );
    await waitFor(() => expect(document.activeElement).toBe(archiveButton()));
    expect(document.activeElement).not.toBe(document.body);
  });

  it("returns focus to the trigger when the confirmation is cancelled", async () => {
    render(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);

    trigger().focus();
    fireEvent.click(trigger());

    const cancelButton = await screen.findByRole("button", { name: "Cancel" });
    cancelButton.focus();
    fireEvent.click(cancelButton);

    await waitFor(() => expect(document.activeElement).toBe(trigger()));
  });

  it("re-homes after a listing error", async () => {
    // The container survives `listing -> error`, so a container-only guard
    // would leave focus on it; the new stage's control has to win.
    listArchiveEntriesMock.mockRejectedValue(new Error("boom"));
    render(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);

    trigger().focus();
    fireEvent.click(trigger());

    expect(await screen.findByText("boom")).toBeTruthy();
    await waitFor(() =>
      expect(document.activeElement).toBe(archiveButton()),
    );
  });

  it("re-homes after an extraction error", async () => {
    extractArchiveMock.mockRejectedValue(new Error("Refusing to overwrite"));
    render(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);

    trigger().focus();
    fireEvent.click(trigger());
    const confirmButton = await screen.findByRole("button", { name: "Extract 2 files" });
    confirmButton.focus();
    fireEvent.click(confirmButton);

    expect(await screen.findByText("Refusing to overwrite")).toBeTruthy();
    await waitFor(() =>
      expect(document.activeElement).toBe(archiveButton()),
    );
  });

  it("does not steal focus on mount", () => {
    // A bulk-dialog batch remount or an aside mounts this action with no focus
    // of its own; grabbing it would yank a caret the user never lost.
    const outside = document.createElement("button");
    outside.textContent = "Elsewhere";
    document.body.appendChild(outside);
    outside.focus();

    render(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);

    expect(document.activeElement).toBe(outside);
    outside.remove();
  });

  it("does not take focus on mount when nothing else holds it", () => {
    // `<body>` means no one owns the caret: this mount is an aside or a batch
    // remount and must still not grab it.
    expect(document.activeElement).toBe(document.body);

    render(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);

    expect(document.activeElement).toBe(document.body);
  });

  it("leaves a focus move made elsewhere during the round-trip alone", async () => {
    const listing = deferred<Awaited<ReturnType<typeof listArchiveEntries>>>();
    listArchiveEntriesMock.mockReturnValue(listing.promise);
    render(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);

    trigger().focus();
    fireEvent.click(trigger());

    const outside = document.createElement("button");
    outside.textContent = "Elsewhere";
    document.body.appendChild(outside);
    outside.focus();

    await act(async () => {
      listing.resolve({ path: "Moai.zip", entries: ENTRIES });
    });

    // Anywhere in the document counts, not just this panel.
    expect(document.activeElement).toBe(outside);
    outside.remove();
  });

  it("leaves focus alone when a re-render does not change the stage", async () => {
    const { rerender } = render(
      <ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />,
    );

    trigger().focus();
    fireEvent.click(trigger());
    const confirmButton = await screen.findByRole("button", { name: "Extract 2 files" });
    confirmButton.focus();
    fireEvent.click(confirmButton);
    await screen.findByRole("status");

    const outside = document.createElement("button");
    outside.textContent = "Elsewhere";
    document.body.appendChild(outside);
    outside.focus();

    rerender(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);

    expect(document.activeElement).toBe(outside);
    outside.remove();
  });

  it("adds no key handling of its own", async () => {
    render(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);

    trigger().focus();
    fireEvent.click(trigger());

    const confirmButton = await screen.findByRole("button", { name: "Extract 2 files" });
    confirmButton.focus();
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });

    // The enclosing dialog owns Escape; this action neither closes nor restarts.
    expect(screen.getByRole("button", { name: "Extract 2 files" })).toBeTruthy();
    expect(extractArchiveMock).not.toHaveBeenCalled();
    expect(listArchiveEntriesMock).toHaveBeenCalledTimes(1);
  });
});
