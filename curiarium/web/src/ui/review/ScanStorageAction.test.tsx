// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { LibraryScanStatus } from "../../lib/api/settings";
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
  it("renders the byte-identical CTA and carries its surface", () => {
    const onScan = vi.fn();
    const { container } = render(
      <ScanStorageAction
        state={makeState()}
        canWrite
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
        canWrite
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
        canWrite
        surface="empty"
        onScan={vi.fn()}
      />,
    );

    expect(screen.getByText("Last scan: 3 new, 1 known, 1 missing, 0 failed")).toBeTruthy();
  });

  it("shows the error line", () => {
    render(
      <ScanStorageAction
        state={makeState({ error: "A scan is already running" })}
        canWrite
        surface="detail"
        onScan={vi.fn()}
      />,
    );

    expect(screen.getByText("A scan is already running")).toBeTruthy();
    expect(
      document.querySelector('[data-scan-surface="detail"]')?.textContent,
    ).toContain("A scan is already running");
  });

  it("gives a viewer the read line but no button", () => {
    const { container } = render(
      <ScanStorageAction
        state={makeState()}
        canWrite={false}
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
        canWrite={false}
        surface="header"
        onScan={vi.fn()}
      />,
    );

    expect(container.firstChild).toBeNull();
    expect(container.querySelector("[data-scan-surface]")).toBeNull();
  });
});
