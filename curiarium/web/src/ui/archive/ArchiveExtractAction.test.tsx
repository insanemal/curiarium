// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  listArchiveEntries: vi.fn(),
  extractArchive: vi.fn(),
}));

import { extractArchive, listArchiveEntries, type ArchiveExtractResult } from "../../lib/api";
import { getToasts, resetToasts } from "../../lib/toast";
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
  resetToasts();
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
    // Nothing failed, so a success raises no toast.
    expect(getToasts()).toHaveLength(0);
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
    // Still mounted, so the inline line is the whole report: a regression that
    // painted AND toasted would tell the operator twice.
    expect(getToasts()).toHaveLength(0);
  });

  it("cancelling the confirmation leaves the archive untouched", async () => {
    render(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);

    fireEvent.click(screen.getByRole("button", { name: "Extract Moai.zip" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(screen.getByRole("button", { name: "Extract Moai.zip" })).toBeTruthy();
    expect(extractArchiveMock).not.toHaveBeenCalled();
  });
});

describe("failure reporting after the instance is gone", () => {
  /** A promise the test settles itself, so a rejection can land after a remount. */
  function deferred<T>(): {
    promise: Promise<T>;
    resolve: (value: T) => void;
    reject: (reason: unknown) => void;
  } {
    let resolve!: (value: T) => void;
    let reject!: (reason: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  }

  it("toasts a failed extraction whose row changed under it, naming the archive that failed", async () => {
    const message = "Refusing to overwrite: Moai.stl";
    const extracting = deferred<ArchiveExtractResult>();
    extractArchiveMock.mockReturnValue(extracting.promise);

    const { rerender } = render(
      <ArchiveExtractAction key="Alpha" canContribute path="Alpha.zip" label="Alpha.zip" />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Extract Alpha.zip" }));
    fireEvent.click(await screen.findByRole("button", { name: "Extract 2 files" }));
    await waitFor(() =>
      expect(extractArchiveMock).toHaveBeenCalledWith({ path: "Alpha.zip", entries: null }),
    );

    // The caller keys the action by the active row, so this is a remount: the
    // instance that issued the write is gone when its answer arrives.
    rerender(
      <ArchiveExtractAction key="Beta" canContribute path="Beta.zip" label="Beta.zip" />,
    );

    await act(async () => {
      extracting.reject(new Error(message));
    });

    const toasts = getToasts();
    expect(toasts).toHaveLength(1);
    expect(toasts[0].tone).toBe("error");
    // The message alone never named the archive, so the toast has to.
    expect(toasts[0].message).toBe(`Extract Alpha.zip failed: ${message}`);
    // Nothing was painted: the instance that could paint it was unmounted.
    expect(screen.queryByText(message)).toBeNull();
    expect(screen.queryByText(/Extract Alpha\.zip failed/)).toBeNull();
  });

  it("toasts a failed listing whose row changed under it, naming the archive that failed", async () => {
    const message = "Archive not found";
    const listing = deferred<Awaited<ReturnType<typeof listArchiveEntries>>>();
    listArchiveEntriesMock.mockReturnValue(listing.promise);

    const { rerender } = render(
      <ArchiveExtractAction key="Alpha" canContribute path="Alpha.zip" label="Alpha.zip" />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Extract Alpha.zip" }));
    await waitFor(() => expect(listArchiveEntriesMock).toHaveBeenCalledWith("Alpha.zip"));

    rerender(
      <ArchiveExtractAction key="Beta" canContribute path="Beta.zip" label="Beta.zip" />,
    );

    await act(async () => {
      listing.reject(new Error(message));
    });

    const toasts = getToasts();
    expect(toasts).toHaveLength(1);
    expect(toasts[0].tone).toBe("error");
    expect(toasts[0].message).toBe(`Could not list Alpha.zip: ${message}`);
    expect(screen.queryByText(message)).toBeNull();
    expect(screen.queryByText(/Could not list Alpha\.zip/)).toBeNull();
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

describe("ArchiveExtractAction confirm narration", () => {
  // The archive from the live report (Mf-0ck5): the operator pressed Extract
  // expecting a tower and got a row named after the archive, because the models
  // were packed inside a nested `.zip` that extraction leaves whole.
  const TOWER_ROOT = "01_Fates End - Wizard Tower";

  function towerListing(): typeof ENTRIES {
    return [
      { path: `${TOWER_ROOT}/README.txt`, size: 64 },
      { path: `${TOWER_ROOT}/01_Wizard/wizard_tower_base.stl`, size: 2048 },
      { path: `${TOWER_ROOT}/01_Wizard/wizard_tower_roof.stl`, size: 2048 },
      { path: `${TOWER_ROOT}/images/preview.png`, size: 1024 },
      { path: `${TOWER_ROOT}/nested/accessories.zip`, size: 8192 },
      { path: "__MACOSX/._wizard_tower_base.stl", size: 4 },
    ];
  }

  async function openConfirm(listing: { path: string; size: number }[]): Promise<void> {
    listArchiveEntriesMock.mockResolvedValue({ path: "Moai.zip", entries: listing });
    render(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);
    fireEvent.click(screen.getByRole("button", { name: "Extract Moai.zip" }));
    await screen.findByText(/extract into a new folder beside the archive\?$/);
  }

  it("keeps the count-and-size sentence as the panel's first line, byte for byte", async () => {
    await openConfirm(towerListing());

    const lines = screen.getAllByText(/^(\S|\s)+$/).filter(node => node.tagName === "P");
    expect(lines[0].textContent).toBe(
      "6 files, 13.1 KB — extract into a new folder beside the archive?",
    );
    // The panel's own count includes the `__MACOSX` member, because that sentence
    // counts the listing; the plan below counts what will be written, and says so.
    expect(screen.getByText(/It also holds 1 system file/).textContent).toContain("is not written");
  });

  it("says what the archive holds and which directory the entries live under", async () => {
    await openConfirm(towerListing());

    const narration = screen.getAllByText(/It holds|lands under|nested archive/).map(node => node.textContent);
    expect(narration).toContain("It holds 2 models, 1 image, 1 nested archive, 1 document.");
    expect(narration).toContain(`Everything lands under "${TOWER_ROOT}/" inside the new folder.`);
  });

  it("says so out loud when the models are only inside a nested archive", async () => {
    await openConfirm([
      { path: `${TOWER_ROOT}/README.txt`, size: 64 },
      { path: `${TOWER_ROOT}/wizard_v5.4_models.zip`, size: 90240 },
    ]);

    const warning = await screen.findByText(/No model is packed in this archive itself/);
    expect(warning.textContent).toContain("wizard_v5.4_models.zip");
    expect(warning.textContent).toContain("it does not unzip it");
    // The operator can still choose differently: the buttons are the same ones.
    expect(screen.getByRole("button", { name: "Extract 2 files" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeTruthy();
  });

  it("says the nested archives stay packed when the archive has models of its own", async () => {
    await openConfirm([
      { path: "tower.stl", size: 2048 },
      { path: "extras.zip", size: 512 },
    ]);

    const warning = await screen.findByText(/nested archive(s)? stays? packed/);
    expect(warning.textContent).toContain("One nested archive stays packed");
  });

  it("says an archive of system files writes nothing, instead of offering a leap", async () => {
    await openConfirm([{ path: ".DS_Store", size: 6148 }]);

    expect((await screen.findByText(/extracting it writes nothing/)).textContent).toBe(
      "This archive holds nothing but 1 system file - extracting it writes nothing.",
    );
  });

  it("says nothing about models before the listing has been read", () => {
    render(<ArchiveExtractAction canContribute path="Moai.zip" label="Moai.zip" />);

    expect(screen.queryByText(/It holds/)).toBeNull();
    expect(listArchiveEntriesMock).not.toHaveBeenCalled();
  });
});
