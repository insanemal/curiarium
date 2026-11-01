// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { LibraryScanStatus } from "../../lib/api/settings";
import LibraryScanPanel from "./LibraryScanPanel";
import type { LibraryScanState } from "./useLibraryScan";

function makeStatus(overrides: Partial<LibraryScanStatus> = {}): LibraryScanStatus {
  return {
    running: false,
    started_at: 1_700_000_000,
    finished_at: 1_700_000_010,
    registered: 3,
    known: 1,
    ignored: 0,
    missing: 0,
    failed: 0,
    errors: [],
    ...overrides,
  };
}

/** The whole hook state, so a test states only the field it is about. */
function makeState(overrides: Partial<LibraryScanState> = {}): LibraryScanState {
  return {
    scan: makeStatus(),
    busy: false,
    error: null,
    loaded: true,
    run: vi.fn(async () => {}),
    adopt: vi.fn(),
    ...overrides,
  };
}

function renderPanel(state: LibraryScanState = makeState(), canWrite = true) {
  return render(<LibraryScanPanel state={state} canWrite={canWrite} />);
}

afterEach(() => {
  cleanup();
});

describe("LibraryScanPanel", () => {
  it("renders the storage explainer and no invented numbers before a scan is known", () => {
    renderPanel(makeState({ scan: null, loaded: false }));

    expect(screen.getByText("Files added from outside Curiarium")).toBeTruthy();
    expect(screen.getByText(/Nothing is moved or deleted/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Rescan storage folder" })).toBeTruthy();
    // No status read has succeeded, so the panel must not print a status line at
    // all - a "Last scan: 0 new, 0 known..." would be a fabricated result.
    expect(screen.queryByText(/Last scan:/)).toBeNull();
    expect(screen.queryByText(/files failed/)).toBeNull();
  });

  it("renders the last-scan line with every counter", () => {
    renderPanel(makeState({ scan: makeStatus({ registered: 3, known: 1, missing: 0, failed: 2 }) }));

    expect(screen.getByText("Last scan: 3 new, 1 known, 0 missing, 2 failed")).toBeTruthy();
  });

  it("reads Scanning... and is disabled while a scan runs", () => {
    renderPanel(makeState({ scan: makeStatus({ running: true, finished_at: null }) }));

    const button = screen.getByRole("button", { name: "Scanning..." }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByText("Scan in progress")).toBeTruthy();
    expect(screen.queryByText(/Last scan:/)).toBeNull();
  });

  it("runs the scan once on click", () => {
    const state = makeState();
    renderPanel(state);

    fireEvent.click(screen.getByRole("button", { name: "Rescan storage folder" }));

    expect(state.run).toHaveBeenCalledTimes(1);
  });

  it("shows the already-running message", () => {
    renderPanel(makeState({ error: "A scan is already running" }));

    expect(screen.getByText("A scan is already running")).toBeTruthy();
  });

  it("renders the failure report when files failed", () => {
    renderPanel(
      makeState({
        scan: makeStatus({
          failed: 2,
          errors: ["a.stl: IntegrityError: duplicate name", "b.stl: IntegrityError: duplicate name"],
        }),
      }),
    );

    expect(screen.getByText("2 files failed")).toBeTruthy();
    expect(screen.getByText("a.stl: IntegrityError: duplicate name")).toBeTruthy();
    expect(screen.getByText("b.stl: IntegrityError: duplicate name")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Copy" })).toBeTruthy();
  });

  // The rescan is `POST /library/scan` (WRITE); the status line and the failure
  // report are `GET /library/scan` (READ). Moving this surface into the queue
  // must broaden no write permission, and must hide nothing a viewer may read.
  it("hides the rescan button below editor but keeps the read surfaces", () => {
    const { container } = renderPanel(
      makeState({ scan: makeStatus({ failed: 2, errors: ["a.stl: IntegrityError: duplicate name"] }) }),
      false,
    );

    expect(screen.queryByRole("button", { name: /Rescan storage folder/ })).toBeNull();
    // Not merely disabled: the element is absent from the DOM entirely.
    expect(container.textContent ?? "").not.toContain("Rescan storage folder");
    expect(screen.getByText(/Last scan: 3 new, 1 known, 0 missing, 2 failed/)).toBeTruthy();
    expect(screen.getByText("2 files failed")).toBeTruthy();
    // The only button left is the failure report's own Copy.
    expect(screen.getByRole("button", { name: "Copy" })).toBeTruthy();
    expect(container.querySelectorAll("button")).toHaveLength(1);
  });
});
