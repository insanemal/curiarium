// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import StorageSection from "./StorageSection";
import type { StorageSettingsState } from "./useStorageSettings";

// The cards are separate subjects with their own suites; this file proves the
// section composes them and that the storage card itself is configuration only.
vi.mock("./useStorageCapabilities", () => ({
  useStorageCapabilities: () => ({ capabilities: null, loading: false, error: null, refresh: vi.fn(async () => {}) }),
}));
vi.mock("./StorageCapabilitiesCard", async () => {
  const react = await import("react");
  return { default: () => react.createElement("div", { "data-testid": "capabilities-card" }) };
});
vi.mock("./SnapshotCard", async () => {
  const react = await import("react");
  return { default: () => react.createElement("div", { "data-testid": "snapshot-card" }) };
});
vi.mock("../TrashCard", async () => {
  const react = await import("react");
  return { default: () => react.createElement("div", { "data-testid": "trash-card" }) };
});

function makeStorage(overrides: Partial<StorageSettingsState> = {}): StorageSettingsState {
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
    ...overrides,
  };
}

function renderSection(storage: StorageSettingsState = makeStorage()) {
  return render(<StorageSection storage={storage} onBack={() => {}} />);
}

afterEach(() => {
  cleanup();
});

describe("StorageSection", () => {
  it("renders the path template and the example path", () => {
    renderSection();

    const input = screen.getByDisplayValue("{folder}/{model}/{filename}") as HTMLInputElement;
    expect(input.tagName).toBe("INPUT");
    expect(screen.getByRole("button", { name: "Save storage structure" })).toBeTruthy();
    // The example is the draft template with the preview tokens filled in, never
    // the server's sample: the operator must see what the typed template does.
    expect(screen.getByText("Props/Workshop/Cable clip/Cable clip.3mf")).toBeTruthy();
  });

  it("renders the capabilities, snapshot and trash cards", () => {
    renderSection();

    expect(screen.getByTestId("capabilities-card")).toBeTruthy();
    expect(screen.getByTestId("snapshot-card")).toBeTruthy();
    expect(screen.getByTestId("trash-card")).toBeTruthy();
  });

  // The storage scan is work on the library, not configuration: it lives in
  // REVIEW -> Missing files now (ADR 0011 decision 15). Nothing of it may
  // survive here, not even as a disabled control.
  it("renders no rescan surface at all", () => {
    const { container } = renderSection();
    const text = container.textContent ?? "";

    expect(screen.queryByRole("button", { name: /Rescan storage folder/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Scanning\.\.\./ })).toBeNull();
    expect(text).not.toContain("Rescan storage folder");
    expect(text).not.toContain("Scanning...");
    expect(text).not.toContain("Last scan:");
    expect(text).not.toContain("files failed");
    expect(text).not.toContain("Files added from outside Curiarium");
  });

  it("offers no control to reorganize existing files", () => {
    const { container } = renderSection();

    expect(screen.queryByText(/reorganize existing|apply to existing/i)).toBeNull();
    expect(container.textContent ?? "").not.toMatch(/reorganize existing|apply to existing/i);
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
  });
});
