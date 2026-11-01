// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

// The queue owns bundle detection now, so Settings must never reach it.
// Mocking the domain module keeps a regression observable as "called" rather
// than a module-resolution failure.
vi.mock("../lib/api/bundles", () => ({
  createBundles: vi.fn(),
  getBundleReport: vi.fn(),
  setBundleDismissed: vi.fn(),
}));

// The import-run history moved to REVIEW, and the hook imports it from this
// domain module. This mock is what makes "Settings makes no GET /imports/runs"
// observable: the barrel mock below is a fixed object with no `getImportRuns`,
// so a regression that reached for the barrel would surface as a swallowed
// "Import run history is unavailable." while this mock stayed uncalled.
vi.mock("../lib/api/settings", async importOriginal => {
  const actual = await importOriginal<typeof import("../lib/api/settings")>();
  return { ...actual, getImportRuns: vi.fn(), getImportRun: vi.fn() };
});

// Every network touchpoint of Settings (storage + the embedded TrashCard) is
// mocked, so the component renders without a backend.
vi.mock("../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  createFolder: vi.fn(),
  createUser: vi.fn(),
  getMountImportSettings: vi.fn(),
  getLibraryScanStatus: vi.fn(),
  getStorageCapabilities: vi.fn(async () => ({ volumes: [] })),
  getStorageSettings: vi.fn(),
  getTrash: vi.fn(async () => ({ entries: [], total_size: 0 })),
  listSessions: vi.fn(async () => []),
  listUsers: vi.fn(async () => []),
  listUserSessions: vi.fn(),
  purgeTrash: vi.fn(),
  resetUserPassword: vi.fn(),
  revokeSession: vi.fn(),
  revokeUserSession: vi.fn(),
  startLibraryScan: vi.fn(),
  updateMountImportSettings: vi.fn(),
  updateStorageSettings: vi.fn(),
  updateUser: vi.fn(),
}));

// The section is stubbed: its own flows are covered in MfaSection.test.tsx, so
// this file only proves Settings routes the card to it and passes the role.
vi.mock("./settings/MfaSection", async () => {
  const react = await import("react");
  return {
    default: ({ role }: { role?: string | null }) =>
      react.createElement("div", { "data-testid": "mfa-section" }, `role:${role ?? "none"}`),
  };
});

import Settings from "./settings/Settings";
import {
  getMountImportSettings,
  getStorageSettings,
  listSessions,
  updateMountImportSettings,
  updateStorageSettings,
} from "../lib/api";
import { createBundles, getBundleReport, setBundleDismissed } from "../lib/api/bundles";
import { getImportRun, getImportRuns } from "../lib/api/settings";
import type { AppSettings } from "../lib/settings";

const createBundlesMock = vi.mocked(createBundles);
const getBundleReportMock = vi.mocked(getBundleReport);
const setBundleDismissedMock = vi.mocked(setBundleDismissed);
const getImportRunsMock = vi.mocked(getImportRuns);
const getImportRunMock = vi.mocked(getImportRun);
const getMountImportSettingsMock = vi.mocked(getMountImportSettings);
const getStorageSettingsMock = vi.mocked(getStorageSettings);
const listSessionsMock = vi.mocked(listSessions);
const updateMountImportSettingsMock = vi.mocked(updateMountImportSettings);
const updateStorageSettingsMock = vi.mocked(updateStorageSettings);

const settings: AppSettings = {
  slicer: { enabled: false, selected: "orca" },
  engraving: { enabled: false, selected: "lightburn" },
  theme: { selected: "system" },
  makerworld: { cookie: "" },
  thingiverse: { cookie: "" },
  imports: { rememberCookies: false },
  network: { publicUrl: "" },
  previews: { mode: "automatic" },
};

const storedStorage = {
  template: "{folder}/{model}/{filename}",
  default_template: "{folder}/{model}/{filename}",
  allowed_tokens: ["folder", "model", "filename"],
  sample_path: "Props/Cable clip/Cable clip.3mf",
  moved: 0,
  skipped: 0,
};

/** Render Settings and navigate from the root menu into the storage section. */
async function renderStorageSection() {
  render(<Settings settings={settings} onChange={() => {}} role="admin" />);

  fireEvent.click(await screen.findByRole("button", { name: /Storage Structure/ }));

  const input = await screen.findByDisplayValue("{folder}/{model}/{filename}");
  return input as HTMLInputElement;
}

describe("Settings storage structure", () => {
  beforeEach(() => {
    getStorageSettingsMock.mockReset();
    getStorageSettingsMock.mockResolvedValue(storedStorage);
    updateStorageSettingsMock.mockReset();
    updateStorageSettingsMock.mockResolvedValue(storedStorage);
  });

  afterEach(() => {
    cleanup();
  });

  it("shows the editable path template", async () => {
    const input = await renderStorageSection();

    expect(input.tagName).toBe("INPUT");
    expect(input.disabled).toBe(false);
    expect(screen.getByText(/never moved/)).toBeTruthy();
  });

  it("offers no control to reorganize existing files", async () => {
    await renderStorageSection();

    expect(screen.queryByText(/reorganize existing|apply to existing/i)).toBeNull();
    expect(document.body.textContent || "").not.toMatch(/reorganize existing|apply to existing/i);
    expect(document.body.querySelector('input[type="checkbox"]')).toBeNull();
  });

  it("saves only the template and confirms existing files are untouched", async () => {
    const input = await renderStorageSection();

    fireEvent.change(input, { target: { value: "{folder}/{model}" } });
    fireEvent.click(screen.getByRole("button", { name: "Save storage structure" }));

    await waitFor(() => expect(updateStorageSettingsMock).toHaveBeenCalledTimes(1));
    const payload = updateStorageSettingsMock.mock.calls[0][0];
    expect(Object.keys(payload)).toEqual(["template"]);
    expect(payload).toEqual({ template: "{folder}/{model}" });

    await screen.findByText(/Storage structure saved\. New uploads and imports use this template; existing files are never moved\./);
  });
});

describe("Settings imports (AC-5)", () => {
  beforeEach(() => {
    createBundlesMock.mockReset();
    getBundleReportMock.mockReset();
    setBundleDismissedMock.mockReset();
    getImportRunsMock.mockReset();
    getImportRunMock.mockReset();
    // These are shared spies: without a reset here a request made by another
    // section's test would read as a Settings regression (or hide one).
    getStorageSettingsMock.mockReset();
    getMountImportSettingsMock.mockReset();
    getMountImportSettingsMock.mockResolvedValue({ enabled: false, copy_files: true, path: null });
    updateMountImportSettingsMock.mockReset();
    updateMountImportSettingsMock.mockResolvedValue({ enabled: true, copy_files: true, path: null });
  });

  afterEach(() => {
    cleanup();
  });

  /** Render Settings and open the Import sources section for the given role. */
  async function renderImports(role: "admin" | "editor" | "contributor" | "viewer") {
    render(<Settings settings={settings} onChange={() => {}} role={role} />);
    fireEvent.click(await screen.findByRole("button", { name: /Import sources/ }));
    // Positive control: the section has settled (the cookie copy rendered), so
    // the "never called" assertions below cannot pass on a not-yet-run effect.
    await screen.findByText(/Remember import cookies on this device/);
  }

  /** The run history and the folder scan are work surfaces, not configuration. */
  function expectNoWorkSurface(): void {
    expect(screen.queryByText("Latest run")).toBeNull();
    expect(screen.queryByText("No import currently running")).toBeNull();
    expect(screen.queryByText("Mount import runs")).toBeNull();
    expect(screen.queryByText("Local folder scan")).toBeNull();
    // The missing-export trap: a run-history import that only reached the fixed
    // barrel mock would land here as the panel's swallowed error string instead.
    expect(screen.queryByText("Import run history is unavailable.")).toBeNull();
  }

  it.each<"admin" | "editor" | "contributor" | "viewer">([
    "admin",
    "editor",
    "contributor",
    "viewer",
  ])("shows no run surface and makes no import-run request for a %s", async role => {
    await renderImports(role);

    expectNoWorkSurface();
    // The removal proof is the call, not the DOM: Settings used to fire this on
    // every mount, from any section.
    expect(getImportRunsMock).not.toHaveBeenCalled();
    expect(getImportRunMock).not.toHaveBeenCalled();
  });

  it("keeps the section labelled Import sources", async () => {
    await renderImports("viewer");

    expect(screen.getByText("Import sources")).toBeTruthy();
    expect(screen.queryByText("Imports & Folder Scan")).toBeNull();
  });

  it("scopes the cookie copy to rate limits and reports the remember opt-in", async () => {
    const onChange = vi.fn();
    render(<Settings settings={settings} onChange={onChange} role="admin" />);

    fireEvent.click(await screen.findByRole("button", { name: /Import sources/ }));

    // Both cards state that the cookie is never used for paid content.
    expect((await screen.findAllByText(/Never used for paid content/)).length).toBe(2);

    fireEvent.click(screen.getByLabelText(/Remember import cookies on this device/));

    const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1];
    const next = lastCall?.[0] as AppSettings;
    expect(next.imports.rememberCookies).toBe(true);
  });

  it("lets an editor toggle remember-cookies in Import sources", async () => {
    const onChange = vi.fn();
    render(<Settings settings={settings} onChange={onChange} role="editor" />);

    fireEvent.click(await screen.findByRole("button", { name: /Import sources/ }));
    fireEvent.click(await screen.findByLabelText(/Remember import cookies on this device/));

    const lastCall = onChange.mock.calls[onChange.mock.calls.length - 1];
    expect((lastCall?.[0] as AppSettings).imports.rememberCookies).toBe(true);
  });

  it("keeps the mount-import card editable for an admin", async () => {
    await renderImports("admin");

    // Mount settings are admin-only (the load is admin-gated), so the editable
    // card belongs to the admin case; a non-admin sees no card at all.
    const enable = (await screen.findByLabelText(
      "Enable mount import on startup",
    )) as HTMLInputElement;
    expect(enable.checked).toBe(false);

    fireEvent.click(enable);

    await waitFor(() =>
      expect(
        (screen.getByLabelText("Enable mount import on startup") as HTMLInputElement).checked,
      ).toBe(true),
    );

    // Both cookie cards have a Save button, so scope to the mount card.
    const mountCard = screen.getByText("Mount import (server)").closest(".rounded-lg");
    if (!mountCard) throw new Error("No mount-import card");
    fireEvent.click(within(mountCard as HTMLElement).getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(updateMountImportSettingsMock).toHaveBeenCalledWith({
        enabled: true,
        copy_files: true,
      }),
    );
  });

  it("omits the mount-import card and never fetches admin settings for a viewer", async () => {
    await renderImports("viewer");

    expect(screen.queryByText("Mount import (server)")).toBeNull();
    expect(getMountImportSettingsMock).not.toHaveBeenCalled();
    expect(getStorageSettingsMock).not.toHaveBeenCalled();
    expect(getBundleReportMock).not.toHaveBeenCalled();
    expect(getImportRunsMock).not.toHaveBeenCalled();
    expect(getImportRunMock).not.toHaveBeenCalled();
    // No review destination lives inside Settings, for any role.
    expect(screen.queryByRole("button", { name: /^Bundles to confirm/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Imports running/ })).toBeNull();
  });

  it("gives a contributor no folder scan and no bundle fetch", async () => {
    await renderImports("contributor");

    // The folder scan is a WRITE surface and lives in the queue now; the bundle
    // queue only needs CONTRIBUTE - a contributor acts there, not here.
    expectNoWorkSurface();
    expect(getBundleReportMock).not.toHaveBeenCalled();
    expect(getImportRunsMock).not.toHaveBeenCalled();
    expect(getImportRunMock).not.toHaveBeenCalled();
  });
});

describe("Settings role gating", () => {
  beforeEach(() => {
    getBundleReportMock.mockClear();
    getStorageSettingsMock.mockReset();
    getMountImportSettingsMock.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it("hides the admin-only storage section from a viewer", async () => {
    render(<Settings settings={settings} onChange={() => {}} role="viewer" />);

    await screen.findByRole("button", { name: /Import sources/ });
    expect(screen.queryByRole("button", { name: /Storage Structure/ })).toBeNull();
  });

  it("replaces the storage section with a notice when a non-admin opens it", async () => {
    const { rerender } = render(
      <Settings settings={settings} onChange={() => {}} role="admin" />,
    );
    fireEvent.click(await screen.findByRole("button", { name: /Storage Structure/ }));
    await screen.findByDisplayValue("{folder}/{model}/{filename}");

    rerender(<Settings settings={settings} onChange={() => {}} role="viewer" />);

    expect(await screen.findByText("Administrator access required.")).toBeTruthy();
    expect(screen.queryByDisplayValue("{folder}/{model}/{filename}")).toBeNull();
  });

  it("replaces the users section with a notice when the role is revalidated down", async () => {
    const { rerender } = render(
      <Settings settings={settings} onChange={() => {}} role="admin" />,
    );
    fireEvent.click(await screen.findByRole("button", { name: /Users/ }));
    await screen.findByText("Accounts that can sign in to this vault.");

    // No URL to navigate to and the menu entry is hidden for non-admins, so a
    // rerender is the honest test of "the role dropped while the section was open".
    rerender(<Settings settings={settings} onChange={() => {}} role="viewer" />);

    expect(await screen.findByText("Administrator access required.")).toBeTruthy();
    expect(screen.queryByText("Accounts that can sign in to this vault.")).toBeNull();
  });
});

describe("Settings sessions", () => {
  beforeEach(() => {
    listSessionsMock.mockReset();
    listSessionsMock.mockResolvedValue([]);
  });

  afterEach(() => {
    cleanup();
  });

  it("opens the sessions section for a viewer and loads the device list once", async () => {
    render(<Settings settings={settings} onChange={() => {}} role="viewer" />);

    fireEvent.click(await screen.findByRole("button", { name: /Sessions/ }));

    expect(await screen.findByText("Devices signed in to this account.")).toBeTruthy();
    expect(listSessionsMock).toHaveBeenCalledTimes(1);
  });
});

describe("Settings two-factor", () => {
  afterEach(() => {
    cleanup();
  });

  it("opens the two-factor section and hands it the role", async () => {
    render(<Settings settings={settings} onChange={() => {}} role="editor" />);

    // The card is not admin-gated: any account may enrol itself.
    fireEvent.click(await screen.findByRole("button", { name: /Two-factor authentication/ }));

    expect((await screen.findByTestId("mfa-section")).textContent).toBe("role:editor");
  });
});
