// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import type { LibraryScanStatus } from "../../lib/api";
import StorageSection from "./StorageSection";
import type { LibraryScanState } from "./useLibraryScan";
import type { StorageSettingsState } from "./useStorageSettings";

// The cards around the storage panel are not what this test is about: only the
// wiring of the failures panel into the storage card is.
vi.mock("./useStorageCapabilities", () => ({
  useStorageCapabilities: () => ({ capabilities: null, loading: false, error: null, refresh: vi.fn(async () => {}) }),
}));
vi.mock("./SnapshotCard", () => ({ default: () => null }));
vi.mock("../TrashCard", () => ({ default: () => null }));

function makeStorage(): StorageSettingsState {
  return {
    template: "{folder}/{model}/{filename}",
    initial: "{folder}/{model}/{filename}",
    tokens: ["folder", "model", "filename"],
    sample: "Props/Clip/Clip.stl",
    status: null,
    loading: false,
    saving: false,
    canManage: false,
    setTemplate: vi.fn(),
    setStatus: vi.fn(),
    save: vi.fn(async () => {}),
  };
}

function makeScanStatus(overrides: Partial<LibraryScanStatus> = {}): LibraryScanStatus {
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

function makeLibraryScanState(scan: LibraryScanStatus): LibraryScanState {
  return { scan, busy: false, error: null, run: vi.fn(async () => {}) };
}

function renderSection(scan: LibraryScanStatus): void {
  render(
    <StorageSection
      storage={makeStorage()}
      libraryScanState={makeLibraryScanState(scan)}
      onBack={() => {}}
    />,
  );
}

afterEach(() => {
  cleanup();
});

describe("StorageSection", () => {
  it("renders the failures panel in the storage card when files failed", () => {
    renderSection(makeScanStatus({ failed: 2, errors: ["a.stl: IntegrityError: duplicate name"] }));

    expect(screen.getByText("2 files failed")).toBeTruthy();
    expect(screen.getByText("a.stl: IntegrityError: duplicate name")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Copy" })).toBeTruthy();
  });

  it("shows no failures panel on a clean scan", () => {
    renderSection(makeScanStatus());

    expect(screen.queryByText(/files failed/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
  });
});
