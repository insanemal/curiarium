// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { ImportRunDetail } from "../../lib/api/settings";
import ImportsReviewView from "./ImportsReviewView";
import type { ImportRunsState } from "./useImportRuns";
import { describeQueueCount } from "./useReviewCounts";

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

const running = makeRun({
  status: "running",
  finished_at: null,
  registered: 2,
  known: 1,
  ignored: 0,
  failed: 1,
  discovered: 4,
  skipped: 1,
});

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

function renderView(state: ImportRunsState, canWrite = false) {
  return render(<ImportsReviewView state={state} canWrite={canWrite} />);
}

function headerCount(): HTMLElement {
  const element = document.querySelector(".review-queue-count");
  if (!element) throw new Error("No queue count in the header");
  return element as HTMLElement;
}

afterEach(() => {
  cleanup();
});

describe("ImportsReviewView running state (AC-1)", () => {
  it("shows live counters and a count of one while a run walks the mount", () => {
    renderView(makeState({ run: running }));

    expect(screen.getByRole("heading", { name: "Imports running" })).toBeTruthy();
    expect(screen.getByText("Import in progress")).toBeTruthy();
    expect(screen.getByText("4 discovered, 2 registered, 1 skipped, 1 failed")).toBeTruthy();

    expect(headerCount().textContent).toBe("1");
    expect(headerCount().getAttribute("title")).toBe(describeQueueCount("imports", 1));
  });
});

describe("ImportsReviewView idle state (AC-3)", () => {
  it("shows the idle line and the last finished run in the same render", () => {
    renderView(makeState({ run: makeRun({ status: "failed", reason: "Permission denied" }) }));

    expect(screen.getByText("No import currently running")).toBeTruthy();
    expect(screen.getByText("Failed")).toBeTruthy();
    expect(screen.getByText(/Started .* · finished .* · 42s/)).toBeTruthy();
    expect(screen.getByText("Permission denied")).toBeTruthy();
    expect(headerCount().textContent).toBe("0");
  });
});

describe("ImportsReviewView folder-panel gating", () => {
  it("hides the folder scan below editor", () => {
    renderView(makeState({ run: running }), false);

    expect(screen.queryByText("Local folder scan")).toBeNull();
    // The run itself is a read surface: a viewer still gets it.
    expect(screen.getByText("Import in progress")).toBeTruthy();
  });

  it("shows the folder scan for an editor", () => {
    renderView(makeState({ run: running }), true);

    expect(screen.getByText("Local folder scan")).toBeTruthy();
  });
});

describe("ImportsReviewView refresh failure notice", () => {
  it("keeps the last run visible under the refresh-failure notice", () => {
    const { container } = renderView(
      makeState({ run: makeRun(), error: "Import run history is unavailable." }),
    );

    const notice = container.querySelector(".review-notice");
    if (!notice) throw new Error("No refresh-failure notice");
    expect(notice.textContent).toContain("Refresh failed");
    expect(notice.textContent).toContain("Import run history is unavailable.");
    // A failed refresh must never erase the run the operator was reading.
    expect(screen.getByText("Succeeded")).toBeTruthy();
  });

  it("shows no notice for a clean state", () => {
    const { container } = renderView(makeState({ run: makeRun() }));

    expect(container.querySelector(".review-notice")).toBeNull();
  });
});

describe("ImportsReviewView header wording (AC-9)", () => {
  it.each<[string, Partial<ImportRunsState>, number | null]>([
    ["unknown", { loaded: false }, null],
    ["idle", { run: makeRun() }, 0],
    ["running", { run: running }, 1],
  ])("words the %s count exactly as the nav does", (_label, overrides, expected) => {
    renderView(makeState(overrides));

    expect(headerCount().getAttribute("title")).toBe(
      describeQueueCount("imports", expected),
    );
  });
});

describe("ImportsReviewView refresh control", () => {
  it("calls the state's refresh once", () => {
    const state = makeState({ run: makeRun() });
    renderView(state);

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    expect(state.refresh).toHaveBeenCalledTimes(1);
  });

  it("reads Refreshing… and is disabled while the operator's refresh runs", () => {
    const state = makeState({ run: makeRun(), refreshing: true });
    renderView(state);

    const button = screen.getByRole("button", { name: "Refreshing…" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    fireEvent.click(button);
    expect(state.refresh).not.toHaveBeenCalled();
  });
});
