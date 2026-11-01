// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { ImportRunDetail } from "../../lib/api/settings";
import ImportRunsPanel from "./ImportRunsPanel";
import type { ImportRunsState } from "./useImportRuns";

function makeRun(overrides: Partial<ImportRunDetail> = {}): ImportRunDetail {
  return {
    id: "run-1",
    status: "succeeded",
    mode: "copy",
    root: "/mnt/models",
    reason: null,
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
function makeState(overrides: Partial<ImportRunsState> = {}): ImportRunsState {
  return {
    run: null,
    loading: false,
    refreshing: false,
    loaded: true,
    error: null,
    lastRefreshedAt: null,
    refresh: vi.fn(),
    ...overrides,
  };
}

function renderPanel(state: ImportRunsState): void {
  render(<ImportRunsPanel state={state} />);
}

afterEach(() => {
  cleanup();
});

describe("ImportRunsPanel", () => {
  it("explains where the data comes from", () => {
    renderPanel(makeState({ run: makeRun() }));

    expect(screen.getByText("Latest run")).toBeTruthy();
    expect(screen.getByText(/reported by the API/i)).toBeTruthy();
    expect(screen.getByText(/update live/i)).toBeTruthy();
  });

  it("never repeats the removed claim that storage rescans stay in the log", () => {
    // The sentence was wrong (storage scans are not import runs) and was dropped
    // rather than moved; a copy-paste resurrection must fail here.
    renderPanel(makeState({ run: makeRun() }));

    expect(screen.queryByText(/storage rescans stay in the log/i)).toBeNull();
    expect(document.body.textContent || "").not.toMatch(/storage rescans stay in the log/i);
  });

  it("shows the empty state before any run has been recorded", () => {
    renderPanel(makeState());

    expect(screen.getByText("No import currently running")).toBeTruthy();
    expect(screen.getByText("No mount import has run yet.")).toBeTruthy();
    expect(screen.getByText(/recorded at startup/i)).toBeTruthy();
  });

  it("shows a loading line instead of the empty state while the first fetch runs", () => {
    renderPanel(makeState({ loading: true, loaded: false }));

    expect(screen.getByText("Loading import runs...")).toBeTruthy();
    expect(screen.queryByText("No mount import has run yet.")).toBeNull();
    expect(screen.queryByText("No import currently running")).toBeNull();
  });

  it("surfaces the fetch error instead of the empty state", () => {
    renderPanel(
      makeState({
        loaded: false,
        error: "Import run history is unavailable.",
      }),
    );

    expect(screen.getByText("Import run history is unavailable.")).toBeTruthy();
    expect(screen.queryByText("No mount import has run yet.")).toBeNull();
  });

  it("offers a retry that calls the state's refresh exactly once", () => {
    const refresh = vi.fn(async () => {});
    renderPanel(
      makeState({ loaded: false, error: "Import run history is unavailable.", refresh }),
    );

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("reads Trying… and is disabled while that retry is in flight", () => {
    const refresh = vi.fn(async () => {});
    renderPanel(
      makeState({
        loaded: false,
        error: "Import run history is unavailable.",
        refreshing: true,
        refresh,
      }),
    );

    const button = screen.getByRole("button", { name: "Trying…" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    fireEvent.click(button);
    expect(refresh).not.toHaveBeenCalled();
  });

  it("shows live counters while a run is walking", () => {
    renderPanel(
      makeState({
        run: makeRun({
          status: "running",
          finished_at: null,
          registered: 2,
          known: 1,
          ignored: 0,
          failed: 1,
          discovered: 4,
          skipped: 1,
        }),
      }),
    );

    expect(screen.getByText("Import in progress")).toBeTruthy();
    expect(screen.getByText("4 discovered, 2 registered, 1 skipped, 1 failed")).toBeTruthy();
    expect(screen.queryByText(/finished/i)).toBeNull();
    // A running run is not idle: the idle line belongs to a finished one.
    expect(screen.queryByText("No import currently running")).toBeNull();
  });

  it("summarises a succeeded run", () => {
    renderPanel(makeState({ run: makeRun() }));

    expect(screen.getByText("Succeeded")).toBeTruthy();
    expect(screen.getByText("Copied into storage")).toBeTruthy();
    expect(screen.getByText("7 discovered, 4 registered, 3 skipped, 0 failed")).toBeTruthy();
    expect(screen.getByText(/Started .* · finished .* · 42s/)).toBeTruthy();
  });

  it("mentions oversize files only when the server rejected some", () => {
    renderPanel(makeState({ run: makeRun({ ignored: 2, rejected: 2, skipped: 4 }) }));

    expect(
      screen.getByText("7 discovered, 4 registered, 4 skipped, 0 failed, 2 oversize"),
    ).toBeTruthy();
    expect(screen.getByText(/2 oversize files exceeded the size limit/i)).toBeTruthy();
  });

  it("styles a failed run with its reason and errors", () => {
    renderPanel(
      makeState({
        run: makeRun({
          status: "failed",
          reason: "Permission denied on /mnt/models",
          errors: ["a.stl: unreadable", "b.3mf: truncated"],
        }),
      }),
    );

    const status = screen.getByText("Failed");
    expect(status.className).toContain("text-red-400");
    expect(screen.getByText("Permission denied on /mnt/models")).toBeTruthy();
    expect(screen.getByText("a.stl: unreadable")).toBeTruthy();
    expect(screen.getByText("b.3mf: truncated")).toBeTruthy();
  });

  it("treats an interrupted run as a failure and truncates long error lists", () => {
    const errors = ["e1", "e2", "e3", "e4", "e5", "e6", "e7"];
    renderPanel(
      makeState({ run: makeRun({ status: "interrupted", reason: "Process exited", errors }) }),
    );

    const status = screen.getByText("Interrupted");
    expect(status.className).toContain("text-red-400");
    expect(screen.getByText("e5")).toBeTruthy();
    expect(screen.queryByText("e6")).toBeNull();
    expect(screen.getByText("+2 more")).toBeTruthy();
  });

  it("does not style a skipped run as an error and shows its reason", () => {
    renderPanel(
      makeState({ run: makeRun({ status: "skipped", reason: "Mount path is not configured" }) }),
    );

    const status = screen.getByText("Skipped");
    expect(status.className).toContain("text-muted");
    expect(status.className).not.toContain("text-red-400");
    expect(screen.getByText("Mount path is not configured")).toBeTruthy();
  });

  it("survives a status it does not know", () => {
    renderPanel(makeState({ run: makeRun({ status: "reticulating" }) }));

    expect(screen.getByText("Unknown (reticulating)")).toBeTruthy();
    expect(screen.getByText(/7 discovered/)).toBeTruthy();
  });
});

describe("ImportRunsPanel idle state (AC-3)", () => {
  it("states that nothing is running above the last finished run's full detail", () => {
    renderPanel(
      makeState({
        run: makeRun({
          status: "failed",
          reason: "Permission denied on /mnt/models",
          errors: ["a.stl: unreadable", "b.3mf: truncated"],
        }),
      }),
    );

    // Both facts in one render: the queue is idle *and* the last run is still
    // readable. Dropping the finished detail would hide the history the operator
    // came for; dropping the idle line would read as "this run is still going".
    expect(screen.getByText("No import currently running")).toBeTruthy();
    expect(screen.getByText("Failed")).toBeTruthy();
    expect(screen.getByText(/Started .* · finished .* · 42s/)).toBeTruthy();
    expect(screen.getByText("7 discovered, 4 registered, 3 skipped, 0 failed")).toBeTruthy();
    expect(screen.getByText("Permission denied on /mnt/models")).toBeTruthy();
    expect(screen.getByText("a.stl: unreadable")).toBeTruthy();
    expect(screen.getByText("b.3mf: truncated")).toBeTruthy();
  });

  it("claims no status at all when no run has ever been recorded", () => {
    renderPanel(makeState());

    expect(screen.getByText("No import currently running")).toBeTruthy();
    // An absent history must not borrow a status label from the finished-run UI.
    for (const label of ["Succeeded", "Failed", "Interrupted", "Skipped"]) {
      expect(screen.queryByText(label)).toBeNull();
    }
    expect(screen.queryByText(/discovered/)).toBeNull();
  });
});
