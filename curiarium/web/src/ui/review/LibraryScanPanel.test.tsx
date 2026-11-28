// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import {
  SCAN_ALREADY_RUNNING,
  scanStatusFromOperation,
  type LibraryScanStatus,
} from "../../lib/api/settings";
import type { Operation } from "../../lib/api/operations";
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

/** The operation row a scan the server refused leaves behind: the slot was held
 *  by another walker, so the row ends `failed` with the server's own sentence. */
function refusedScanRow(): Operation {
  return {
    id: "op-scan",
    workflow: "library-scan",
    target: "",
    status: "failed",
    submitted_by: "u1",
    created_at: "2026-01-01T00:00:00Z",
    started_at: "2026-01-01T00:00:00Z",
    finished_at: "2026-01-01T00:00:00Z",
    error: SCAN_ALREADY_RUNNING,
    undo_state: "none",
    superseded_by: null,
    steps: [
      {
        index: 0,
        op: "scan",
        status: "failed",
        result: null,
        error: SCAN_ALREADY_RUNNING,
        started_at: null,
        finished_at: null,
      },
    ],
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

function renderPanel(
  state: LibraryScanState = makeState(),
  canContribute = true,
  onScan: () => void = vi.fn(),
) {
  return render(<LibraryScanPanel state={state} canContribute={canContribute} onScan={onScan} />);
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

  it("starts the shell's scan once on click, never the hook's own run", () => {
    const state = makeState();
    const onScan = vi.fn();
    renderPanel(state, true, onScan);

    fireEvent.click(screen.getByRole("button", { name: "Rescan storage folder" }));

    expect(onScan).toHaveBeenCalledTimes(1);
    expect(state.run).not.toHaveBeenCalled();
  });

  it("shows the error line the hook reports", () => {
    renderPanel(makeState({ error: "Failed to plan the operation" }));

    expect(screen.getByText("Failed to plan the operation")).toBeTruthy();
  });

  it("shows a row-carried refusal as a scan already holding the slot", () => {
    // A refused scan is no longer a thrown 409 but the operation row's own
    // `failed` answer, so the panel reads it as a scan in progress and carries
    // the server's sentence in the status it renders.
    const scan = scanStatusFromOperation(refusedScanRow());
    renderPanel(makeState({ scan }));

    const button = screen.getByRole("button", { name: "Scanning..." }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByText("Scan in progress")).toBeTruthy();
    expect(screen.queryByText(/Last scan:/)).toBeNull();
    expect(scan.errors).toEqual([SCAN_ALREADY_RUNNING]);
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

  // The rescan is `POST /operations` (CONTRIBUTE, the `library-scan` workflow);
  // the status line and the failure report are the newest `library-scan` row
  // (READ). Moving this surface into the queue must broaden no write permission,
  // and must hide nothing a viewer may read.
  it("hides the rescan button below contributor but keeps the read surfaces", () => {
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

  // The contributor is the role the ticket exists for: the rescan moved down to
  // it, so the panel must offer the control to a capability that cannot delete.
  it("offers the rescan to a contributor", () => {
    const onScan = vi.fn();
    renderPanel(makeState(), true, onScan);

    fireEvent.click(screen.getByRole("button", { name: "Rescan storage folder" }));

    expect(onScan).toHaveBeenCalledTimes(1);
  });
});
