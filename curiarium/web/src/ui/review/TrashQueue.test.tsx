// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import type { TrashEntry, TrashList } from "../../lib/api/trash";
import TrashQueueView from "./TrashQueue";
import type { TrashState } from "./useTrash";

/**
 * The queue is presentational: every fetch and mutation lives in `useTrash`, so
 * these tests drive the state prop directly and never touch the network. The
 * card's six behaviours survive here as queue behaviours, plus the states the
 * queue adds (stale list, purge failure, role gates).
 */

const OLD_FOLDER: TrashEntry = {
  path: ".trash/20261027-abc-old-folder",
  name: "old-folder",
  size: 2048,
  mtime: "2026-10-27T10:00:00Z",
};

const BITS: TrashEntry = {
  path: ".trash/20261027-def-bits",
  name: "bits",
  size: 1024,
  mtime: "2026-10-27T11:00:00Z",
};

const SNAPSHOT_PATTERN = "<YYYYmmddTHHMMSSZ>-prepurge-<6 hex>";

function makeList(
  entries: TrashEntry[] = [OLD_FOLDER, BITS],
  overrides: Partial<TrashList> = {},
): TrashList {
  return {
    entries,
    total_size: entries.reduce((total, entry) => total + entry.size, 0),
    ...overrides,
  };
}

/** The full state shape, so each test states only what it is about. */
function makeState(overrides: Partial<TrashState> = {}): TrashState {
  return {
    trash: makeList(),
    loading: false,
    refreshing: false,
    error: null,
    purgeError: null,
    purgeResult: null,
    purging: false,
    lastRefreshedAt: 1_700_000_000_000,
    refresh: vi.fn(async () => {}),
    purge: vi.fn(async () => true),
    ...overrides,
  };
}

function renderQueue(state: TrashState, canPurge = true) {
  return render(<TrashQueueView state={state} canPurge={canPurge} />);
}

function emptyTrashButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Empty trash" }) as HTMLButtonElement;
}

function openDialog(state: TrashState, canPurge = true) {
  const utils = renderQueue(state, canPurge);
  fireEvent.click(emptyTrashButton());
  return { ...utils, dialog: screen.getByRole("dialog") };
}

afterEach(cleanup);

describe("TrashQueueView entries", () => {
  it("AC-1 lists both entry names with their sizes and the entry/size total", () => {
    renderQueue(makeState());

    expect(screen.getByText("old-folder")).toBeTruthy();
    expect(screen.getByText("bits")).toBeTruthy();
    expect(screen.getByText("2.0 KB")).toBeTruthy();
    expect(screen.getByText("1.0 KB")).toBeTruthy();
    expect(screen.getByText(/2 entries · 3\.0 KB/)).toBeTruthy();
  });

  it("AC-2 states the empty trash and disables the purge instead of removing it", () => {
    const { container } = renderQueue(makeState({ trash: makeList([], { total_size: 0 }) }));

    expect(screen.getByText(/The trash is empty/i)).toBeTruthy();
    expect(emptyTrashButton().disabled).toBe(true);
    // No result box: nothing has been purged in this state.
    expect(container.querySelector(".review-panel")).toBeNull();
  });
});

describe("TrashQueueView read states", () => {
  it("AC-3 claims only that the first read is pending", () => {
    const { container } = renderQueue(makeState({ trash: null, loading: true }));
    const text = container.textContent ?? "";

    expect(screen.getByText("Loading trash…")).toBeTruthy();
    expect(text).not.toContain("The trash is empty");
    // No count claim: not a total, not a zero, not an entry/size line.
    expect(text).not.toMatch(/\d+ entr/);
    expect(text).not.toContain("·");
    expect(screen.queryByRole("button", { name: "Empty trash" })).toBeNull();
  });

  it("AC-4 renders a failed first read with Try again and no empty sentence", async () => {
    const state = makeState({
      trash: null,
      loading: false,
      error: "Failed to load trash",
    });
    const { container } = renderQueue(state);

    expect(screen.getByText("Failed to load trash")).toBeTruthy();
    expect(container.querySelector(".review-error")?.getAttribute("role")).toBe("alert");
    expect(container.textContent ?? "").not.toContain("The trash is empty");

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    await waitFor(() => expect(state.refresh).toHaveBeenCalledTimes(1));
  });

  it("AC-5 keeps the last readable list under exactly one refresh-failure notice", () => {
    const { container } = renderQueue(makeState({ error: "Failed to load trash" }));

    // The rows and the header's age survive: a transient failure must not erase
    // a list the operator can still read.
    expect(screen.getByText("old-folder")).toBeTruthy();
    expect(screen.getByText("bits")).toBeTruthy();
    expect(screen.getByText(/2 entries · 3\.0 KB/)).toBeTruthy();
    expect(screen.getByTitle("2 entries in the trash")).toBeTruthy();

    const notices = screen.getAllByText(/Refresh failed/);
    expect(notices).toHaveLength(1);
    expect(container.querySelector(".review-error")).toBeNull();
  });
});

describe("TrashQueueView purge", () => {
  it("AC-6 purges only after the dialog is accepted, then closes it", async () => {
    const state = makeState();
    const { dialog } = openDialog(state);

    expect(dialog).toBeTruthy();
    expect(state.purge).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Empty trash permanently" }));

    await waitFor(() => expect(state.purge).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("AC-7 reports the partial purge instead of a bare success", () => {
    const { container } = renderQueue(
      makeState({
        trash: makeList([BITS]),
        purgeResult: { purged: 1, bytes: 1024, failed: ["/srv/library/.trash/x: permission denied"] },
      }),
    );

    expect(screen.getByText("1 entry removed (1.0 KB)")).toBeTruthy();
    expect(screen.getByText("Some entries could not be removed:")).toBeTruthy();
    expect(screen.getByText("/srv/library/.trash/x: permission denied")).toBeTruthy();
    expect(screen.getByText("bits")).toBeTruthy();
    expect(container.textContent ?? "").not.toContain("The trash is empty");
  });

  it("AC-7 says Nothing was removed when the purge removed nothing", () => {
    const { container } = renderQueue(
      makeState({
        trash: makeList([OLD_FOLDER]),
        purgeResult: { purged: 0, bytes: 0, failed: ["/srv/library/.trash/x: permission denied"] },
      }),
    );

    expect(screen.getByText("Nothing was removed.")).toBeTruthy();
    expect(screen.getByText("/srv/library/.trash/x: permission denied")).toBeTruthy();
    // "0 entries removed" would read as a success.
    expect(container.textContent ?? "").not.toContain("0 entries removed");
    // "Some entries" would claim a partial purge that removed nothing.
    expect(container.textContent ?? "").not.toContain("Some entries could not be removed");
    expect(container.textContent ?? "").not.toContain("The trash is empty");
  });

  it("AC-8 shows a purge failure inside the open dialog, then as one notice", () => {
    const failing = "Multi-factor authentication required";
    const opening = makeState({ trash: makeList([OLD_FOLDER]), purgeError: null });
    const { rerender } = openDialog(opening);

    // The view is presentational, so the failure arrives as state: re-render
    // with an empty list and the purge error while the dialog stays open.
    rerender(
      <TrashQueueView
        state={makeState({ trash: makeList([], { total_size: 0 }), purgeError: failing })}
        canPurge
      />,
    );

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(failing)).toBeTruthy();
    expect(document.body.textContent ?? "").not.toContain("The trash is empty");

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    const notice = screen.getByText(failing);
    expect(notice.className).toContain("review-notice");
    // A failed purge is not an empty trash.
    expect(document.body.textContent ?? "").not.toContain("The trash is empty");
  });

  it("AC-8 leaves the dialog open when the purge resolves false", async () => {
    const failing = "Multi-factor authentication required";
    const state = makeState({ purge: vi.fn(async () => false) });
    const { rerender } = openDialog(state);

    fireEvent.click(screen.getByRole("button", { name: "Empty trash permanently" }));
    await waitFor(() => expect(state.purge).toHaveBeenCalledTimes(1));

    // The reason is read in place: a failure keeps the dialog up.
    expect(screen.getByRole("dialog")).toBeTruthy();

    rerender(<TrashQueueView state={{ ...state, purgeError: failing }} canPurge />);
    expect(within(screen.getByRole("dialog")).getByText(failing)).toBeTruthy();
  });
});

describe("TrashQueueView snapshot notices", () => {
  it("AC-9 names the snapshot pattern and the strict refusal clause", async () => {
    const state = makeState({
      trash: makeList([OLD_FOLDER], {
        snapshot: {
          reason: "prepurge",
          policy: "strict",
          capability: "available",
          action: "snapshot",
          pattern: SNAPSHOT_PATTERN,
        },
      }),
    });
    openDialog(state);

    expect(
      screen.getByText(/A filesystem snapshot of the library is taken before the trash is emptied/),
    ).toBeTruthy();
    expect(screen.getByText(SNAPSHOT_PATTERN).tagName).toBe("CODE");
    expect(screen.getByText(/the purge is refused and nothing is deleted/)).toBeTruthy();

    const confirmButton = screen.getByRole("button", {
      name: "Empty trash permanently",
    }) as HTMLButtonElement;
    expect(confirmButton.disabled).toBe(false);

    fireEvent.click(confirmButton);
    await waitFor(() => expect(state.purge).toHaveBeenCalledTimes(1));
  });

  it("AC-10 warns that the purge goes ahead without a snapshot", () => {
    openDialog(
      makeState({
        trash: makeList([OLD_FOLDER], {
          snapshot: {
            reason: "prepurge",
            policy: "warn",
            capability: "unavailable",
            action: "proceed_without",
            pattern: SNAPSHOT_PATTERN,
            detail: "the volume is a network mount",
          },
        }),
      }),
    );

    expect(screen.getByText(/because the volume is a network mount/)).toBeTruthy();
    expect(screen.getByText(/still goes ahead/)).toBeTruthy();
    expect(screen.getByText(/recorded in the audit log/)).toBeTruthy();
  });

  it("AC-11 refuses the purge entirely when no snapshot can be taken", () => {
    const state = makeState({
      trash: makeList([OLD_FOLDER], {
        snapshot: {
          reason: "prepurge",
          policy: "strict",
          capability: "unavailable",
          action: "refuse",
          pattern: SNAPSHOT_PATTERN,
        },
      }),
    });
    openDialog(state);

    const refusal = screen.getByText(
      /this deployment refuses to empty the trash without one\. Nothing will be deleted/,
    );
    expect(refusal.className).toContain("text-red-400");

    const confirmButton = screen.getByRole("button", {
      name: "Empty trash permanently",
    }) as HTMLButtonElement;
    expect(confirmButton.disabled).toBe(true);
    expect(state.purge).not.toHaveBeenCalled();
    // A refused purge moves focus off the dead confirm button.
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel" }));
  });
});

describe("TrashQueueView dialog", () => {
  it("AC-15 is a modal dialog with initial focus and Escape to cancel", () => {
    const state = makeState();
    const { dialog } = openDialog(state);

    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Empty trash permanently" }),
    );

    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(state.purge).not.toHaveBeenCalled();
  });
});

describe("TrashQueueView has no restore affordance", () => {
  it("AC-17 offers no restore control, only the manual-restore sentence", () => {
    const { container } = renderQueue(makeState());

    expect(screen.queryByRole("button", { name: /restore/i })).toBeNull();
    expect(screen.queryByRole("link", { name: /restore/i })).toBeNull();
    const restoreLabels = Array.from(container.querySelectorAll("button, a, code")).filter(
      node => /restore/i.test(node.textContent ?? ""),
    );
    expect(restoreLabels).toEqual([]);

    const explanation = container.querySelector(".review-explanation");
    expect(explanation?.textContent).toContain(
      "move an entry back into the storage folder, then rescan the library",
    );
  });
});

describe("TrashQueueView role gate", () => {
  it("renders no purge control at all below admin, and opens no dialog", () => {
    const state = makeState();
    const { container } = renderQueue(state, false);

    expect(screen.queryByRole("button", { name: "Empty trash" })).toBeNull();
    expect(container.textContent ?? "").not.toContain("Empty trash");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(state.purge).not.toHaveBeenCalled();

    // Reading the queue still works: rows, the entry/size line and Refresh.
    expect(screen.getByText("old-folder")).toBeTruthy();
    expect(screen.getByText("bits")).toBeTruthy();
    expect(screen.getByText(/2 entries · 3\.0 KB/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeTruthy();
  });
});
