// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { MountImportStatus } from "../../lib/api/settings";
import MountImportPanel from "./MountImportPanel";
import type { MountImportState } from "./useMountImport";

function makeStatus(overrides: Partial<MountImportStatus> = {}): MountImportStatus {
  return {
    status: "done",
    running: false,
    outcome: "registered",
    mode: "copy",
    root: "/mnt/models",
    reason: null,
    error: null,
    started_at: 1_700_000_000,
    finished_at: 1_700_000_042,
    registered: 4,
    known: 2,
    ignored: 1,
    failed: 0,
    rejected: 0,
    discovered: 7,
    skipped: 3,
    errors: [],
    ...overrides,
  };
}

/** The full state shape, so a test states only what it is about. */
function makeState(overrides: Partial<MountImportState> = {}): MountImportState {
  return {
    status: null,
    loading: false,
    refreshing: false,
    loaded: true,
    error: null,
    lastRefreshedAt: null,
    refresh: vi.fn(),
    ...overrides,
  };
}

function renderPanel(state: MountImportState): void {
  render(<MountImportPanel state={state} />);
}

/** The sentence the ticket pins, verbatim. */
const COUNTERS_LATER =
  "The walk's counters are reported when it finishes: the operations ledger records a run's " +
  "totals with its result, not while it runs.";

afterEach(() => {
  cleanup();
});

describe("MountImportPanel", () => {
  it("explains where the data comes from", () => {
    renderPanel(makeState({ status: makeStatus() }));

    expect(screen.getByText("Latest walk")).toBeTruthy();
    expect(screen.getByText(/newest mount import recorded in the operations ledger/i)).toBeTruthy();
    expect(screen.getByText(/counters are written with the walk/i)).toBeTruthy();
  });

  it("never claims live counters, which the ledger cannot answer", () => {
    renderPanel(makeState({ status: makeStatus() }));

    expect(screen.queryByText(/update live/i)).toBeNull();
    expect(document.body.textContent || "").not.toMatch(/update live/i);
  });

  it("shows the empty state before any walk has been recorded", () => {
    renderPanel(makeState());

    expect(screen.getByText("No walk currently running")).toBeTruthy();
    expect(screen.getByText("No mount import has run yet.")).toBeTruthy();
    expect(screen.getByText(/recorded at every boot/i)).toBeTruthy();
  });

  it("shows a loading line instead of the empty state while the first fetch runs", () => {
    renderPanel(makeState({ loading: true, loaded: false }));

    expect(screen.getByText("Loading the mount import...")).toBeTruthy();
    expect(screen.queryByText("No mount import has run yet.")).toBeNull();
    expect(screen.queryByText("No walk currently running")).toBeNull();
  });

  it("surfaces the fetch error instead of the empty state", () => {
    renderPanel(
      makeState({
        loaded: false,
        error: "The mount import status is unavailable.",
      }),
    );

    expect(screen.getByText("The mount import status is unavailable.")).toBeTruthy();
    expect(screen.queryByText("No mount import has run yet.")).toBeNull();
  });

  it("offers a retry that calls the state's refresh exactly once", () => {
    const refresh = vi.fn(async () => {});
    renderPanel(makeState({ loaded: false, error: "The mount import status is unavailable.", refresh }));

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("reads Trying… and is disabled while that retry is in flight", () => {
    const refresh = vi.fn(async () => {});
    renderPanel(
      makeState({
        loaded: false,
        error: "The mount import status is unavailable.",
        refreshing: true,
        refresh,
      }),
    );

    const button = screen.getByRole("button", { name: "Trying…" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    fireEvent.click(button);
    expect(refresh).not.toHaveBeenCalled();
  });
});

describe("MountImportPanel running state", () => {
  it("says the counters are reported when the walk finishes, and prints none of them", () => {
    renderPanel(
      makeState({
        status: makeStatus({ status: "running", running: true, outcome: null, finished_at: null }),
      }),
    );

    expect(screen.getByText("Import in progress")).toBeTruthy();
    expect(screen.getByText("Copied into storage")).toBeTruthy();
    expect(screen.getByText(COUNTERS_LATER)).toBeTruthy();

    // THE PIN: while a walk runs, the ledger has no totals, so the panel must
    // not print a counters line of zeros. Both the whole line and any single
    // counter word have to be absent.
    expect(screen.queryByText(/discovered/)).toBeNull();
    expect(document.body.textContent || "").not.toMatch(/discovered/);
    expect(document.body.textContent || "").not.toMatch(/0 discovered/);
    expect(document.body.textContent || "").not.toMatch(/registered/);

    // A running walk is not idle: the idle line belongs to a finished one.
    expect(screen.queryByText("No walk currently running")).toBeNull();
  });

  it("shows no finish time while the walk is in flight", () => {
    renderPanel(
      makeState({
        status: makeStatus({ status: "running", running: true, outcome: null, finished_at: null }),
      }),
    );

    expect(screen.queryByText(/finished/i)).toBeNull();
  });
});

describe("MountImportPanel finished states", () => {
  it("summarises a succeeded walk with its counters line", () => {
    renderPanel(makeState({ status: makeStatus() }));

    expect(screen.getByText("Succeeded")).toBeTruthy();
    expect(screen.getByText("Copied into storage")).toBeTruthy();
    expect(screen.getByText("7 discovered, 4 registered, 3 skipped, 0 failed")).toBeTruthy();
    expect(screen.getByText(/Started .* · finished .* · 42s/)).toBeTruthy();
  });

  it("mentions oversize files only when the server rejected some", () => {
    renderPanel(makeState({ status: makeStatus({ ignored: 2, rejected: 2, skipped: 4 }) }));

    expect(screen.getByText("7 discovered, 4 registered, 4 skipped, 0 failed, 2 oversize")).toBeTruthy();
    expect(screen.getByText(/2 oversize files exceeded the size limit/i)).toBeTruthy();
  });

  it("styles a failed walk with its sentence, its reason and its errors", () => {
    renderPanel(
      makeState({
        status: makeStatus({
          status: "failed",
          outcome: null,
          error: "The mount is unreadable",
          reason: "Permission denied on /mnt/models",
          errors: ["a.stl: unreadable", "b.3mf: truncated"],
        }),
      }),
    );

    const status = screen.getByText("Failed");
    expect(status.className).toContain("text-red-400");
    expect(screen.getByText("The mount is unreadable")).toBeTruthy();
    expect(screen.getByText("Permission denied on /mnt/models")).toBeTruthy();
    expect(screen.getByText("a.stl: unreadable")).toBeTruthy();
    expect(screen.getByText("b.3mf: truncated")).toBeTruthy();
    // A failed walk keeps whatever the row counted, which is nothing here.
    expect(screen.getByText("7 discovered, 4 registered, 3 skipped, 0 failed")).toBeTruthy();
  });

  it("treats a cancelled walk as a decision, not a failure, and truncates long error lists", () => {
    const errors = ["e1", "e2", "e3", "e4", "e5", "e6", "e7"];
    renderPanel(
      makeState({
        status: makeStatus({ status: "cancelled", outcome: null, reason: "Cancelled by hand", errors }),
      }),
    );

    const status = screen.getByText("Cancelled");
    expect(status.className).not.toContain("text-red-400");
    expect(screen.getByText("e5")).toBeTruthy();
    expect(screen.queryByText("e6")).toBeNull();
    expect(screen.getByText("+2 more")).toBeTruthy();
  });

  it("does not style a skipped walk as an error and shows its reason", () => {
    renderPanel(
      makeState({
        status: makeStatus({
          status: "done",
          outcome: "skipped",
          reason: "Mount path is not configured",
        }),
      }),
    );

    const status = screen.getByText("Skipped");
    expect(status.className).toContain("text-muted");
    expect(status.className).not.toContain("text-red-400");
    expect(screen.getByText("Mount path is not configured")).toBeTruthy();
  });

  it("survives a status it does not know", () => {
    renderPanel(makeState({ status: makeStatus({ status: "reticulating", outcome: null }) }));

    expect(screen.getByText("Unknown (reticulating)")).toBeTruthy();
    expect(screen.getByText(/7 discovered/)).toBeTruthy();
  });
});

describe("MountImportPanel idle state (AC-3)", () => {
  it("states that nothing is running above the last finished walk's full detail", () => {
    renderPanel(
      makeState({
        status: makeStatus({
          status: "failed",
          outcome: null,
          error: "The mount is unreadable",
          reason: "Permission denied on /mnt/models",
          errors: ["a.stl: unreadable", "b.3mf: truncated"],
        }),
      }),
    );

    // Both facts in one render: the queue is idle *and* the last walk is still
    // readable. Dropping the finished detail would hide the record the operator
    // came for; dropping the idle line would read as "this walk is still going".
    expect(screen.getByText("No walk currently running")).toBeTruthy();
    expect(screen.getByText("Failed")).toBeTruthy();
    expect(screen.getByText(/Started .* · finished .* · 42s/)).toBeTruthy();
    expect(screen.getByText("7 discovered, 4 registered, 3 skipped, 0 failed")).toBeTruthy();
    expect(screen.getByText("Permission denied on /mnt/models")).toBeTruthy();
    expect(screen.getByText("a.stl: unreadable")).toBeTruthy();
    expect(screen.getByText("b.3mf: truncated")).toBeTruthy();
  });

  it("claims no status at all when no walk has ever been recorded", () => {
    renderPanel(makeState());

    expect(screen.getByText("No walk currently running")).toBeTruthy();
    // An absent record must not borrow a status label from the finished-walk UI.
    for (const label of ["Succeeded", "Failed", "Cancelled", "Skipped", "No walk recorded"]) {
      expect(screen.queryByText(label)).toBeNull();
    }
    expect(screen.queryByText(/discovered/)).toBeNull();
  });
});
