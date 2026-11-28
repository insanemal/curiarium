// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import {
  SCAN_ALREADY_RUNNING,
  scanStatusFromOperation,
  type LibraryScanStatus,
} from "../../lib/api/settings";
import type { Operation } from "../../lib/api/operations";
import ScanStorageAction from "./ScanStorageAction";
import type { LibraryScanState } from "./useLibraryScan";

function makeStatus(overrides: Partial<LibraryScanStatus> = {}): LibraryScanStatus {
  return {
    running: false,
    started_at: 1_700_000_000,
    finished_at: 1_700_000_010,
    registered: 3,
    known: 1,
    ignored: 0,
    missing: 1,
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

afterEach(() => {
  cleanup();
});

describe("ScanStorageAction", () => {
  it("gives a contributor the button, because the scan is CONTRIBUTE", () => {
    const onScan = vi.fn();
    render(
      <ScanStorageAction
        state={makeState()}
        canContribute
        surface="empty"
        onScan={onScan}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Rescan storage folder" }));
    expect(onScan).toHaveBeenCalledTimes(1);
  });

  it("renders the byte-identical CTA and carries its surface", () => {
    const onScan = vi.fn();
    const { container } = render(
      <ScanStorageAction
        state={makeState()}
        canContribute
        surface="header"
        onScan={onScan}
      />,
    );

    const button = screen.getByRole("button", { name: "Rescan storage folder" });
    expect(button.textContent).toBe("Rescan storage folder");
    expect(container.querySelector('[data-scan-surface="header"]')).toBeTruthy();

    fireEvent.click(button);
    expect(onScan).toHaveBeenCalledTimes(1);
  });

  it("reads Scanning... and disables the button while a scan runs", () => {
    render(
      <ScanStorageAction
        state={makeState({ scan: makeStatus({ running: true, finished_at: null }) })}
        canContribute
        surface="panel"
        onScan={vi.fn()}
      />,
    );

    const button = screen.getByRole("button", { name: "Scanning..." }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByText("Scan in progress")).toBeTruthy();
    expect(screen.queryByText(/Last scan:/)).toBeNull();
  });

  it("prints the last-scan line byte-identically", () => {
    render(
      <ScanStorageAction
        state={makeState({
          scan: makeStatus({ registered: 3, known: 1, missing: 1, failed: 0 }),
        })}
        canContribute
        surface="empty"
        onScan={vi.fn()}
      />,
    );

    expect(screen.getByText("Last scan: 3 new, 1 known, 1 missing, 0 failed")).toBeTruthy();
  });

  it("shows the error line the hook reports", () => {
    render(
      <ScanStorageAction
        state={makeState({ error: "Failed to plan the operation" })}
        canContribute
        surface="detail"
        onScan={vi.fn()}
      />,
    );

    expect(screen.getByText("Failed to plan the operation")).toBeTruthy();
    expect(
      document.querySelector('[data-scan-surface="detail"]')?.textContent,
    ).toContain("Failed to plan the operation");
  });

  it("shows a row-carried refusal as a scan already holding the slot", () => {
    // The refusal is the operation row's own `failed` answer now, not a thrown
    // 409: what the operator must see is that a scan IS running, and the
    // server's sentence must still reach this surface through the row.
    const scan = scanStatusFromOperation(refusedScanRow());
    render(
      <ScanStorageAction
        state={makeState({ scan })}
        canContribute
        surface="detail"
        onScan={vi.fn()}
      />,
    );

    const button = screen.getByRole("button", { name: "Scanning..." }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByText("Scan in progress")).toBeTruthy();
    expect(screen.queryByText(/Last scan:/)).toBeNull();
    expect(scan.errors).toEqual([SCAN_ALREADY_RUNNING]);
  });

  it("gives a viewer the read line but no button", () => {
    const { container } = render(
      <ScanStorageAction
        state={makeState()}
        canContribute={false}
        surface="panel"
        onScan={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: /Rescan storage folder/ })).toBeNull();
    expect(container.querySelectorAll("button")).toHaveLength(0);
    expect(screen.getByText("Last scan: 3 new, 1 known, 1 missing, 0 failed")).toBeTruthy();
  });

  it("renders nothing when it has nothing to show", () => {
    const { container } = render(
      <ScanStorageAction
        state={makeState({ scan: null, loaded: false })}
        canContribute={false}
        surface="header"
        onScan={vi.fn()}
      />,
    );

    expect(container.firstChild).toBeNull();
    expect(container.querySelector("[data-scan-surface]")).toBeNull();
  });
});
