// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { Role } from "../lib/auth";
import WorkspaceUtilities from "./WorkspaceUtilities";

afterEach(() => {
  cleanup();
});

describe("WorkspaceUtilities role gating", () => {
  it.each<[Role, boolean]>([
    ["viewer", false],
    ["editor", false],
    ["admin", true],
  ])("renders Settings always and Audit log only for an admin (%s)", (role, canAudit) => {
    render(<WorkspaceUtilities role={role} />);

    expect(screen.getByRole("button", { name: "Settings" })).toBeTruthy();
    expect(!!screen.queryByRole("button", { name: "Audit log" })).toBe(canAudit);
  });

  it("leaves the audit entry out while the role is unknown", () => {
    render(<WorkspaceUtilities />);

    expect(screen.getByRole("button", { name: "Settings" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Audit log" })).toBeNull();
  });

  it("still routes both clicks for an admin", () => {
    const onOpenSettings = vi.fn();
    const onOpenAudit = vi.fn();
    render(<WorkspaceUtilities role="admin" onOpenSettings={onOpenSettings} onOpenAudit={onOpenAudit} />);

    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.click(screen.getByRole("button", { name: "Audit log" }));

    expect(onOpenSettings).toHaveBeenCalledTimes(1);
    expect(onOpenAudit).toHaveBeenCalledTimes(1);
  });
});

describe("WorkspaceUtilities selection", () => {
  it("marks Settings current and selected while settings is the open view", () => {
    render(<WorkspaceUtilities role="admin" activeView="settings" />);

    const settings = screen.getByRole("button", { name: "Settings" });
    expect(settings.getAttribute("aria-current")).toBe("true");
    expect(settings.className).toContain("is-selected");

    const audit = screen.getByRole("button", { name: "Audit log" });
    expect(audit.getAttribute("aria-current")).toBeNull();
    expect(audit.className).not.toContain("is-selected");
  });

  it("marks the audit log current and selected while audit is the open view", () => {
    render(<WorkspaceUtilities role="admin" activeView="audit" />);

    const audit = screen.getByRole("button", { name: "Audit log" });
    expect(audit.getAttribute("aria-current")).toBe("true");
    expect(audit.className).toContain("is-selected");

    const settings = screen.getByRole("button", { name: "Settings" });
    expect(settings.getAttribute("aria-current")).toBeNull();
    expect(settings.className).not.toContain("is-selected");
  });

  it("leaves both entries unmarked while the library is open", () => {
    render(<WorkspaceUtilities role="admin" activeView="library" />);

    expect(screen.getByRole("button", { name: "Settings" }).getAttribute("aria-current")).toBeNull();
    expect(screen.getByRole("button", { name: "Audit log" }).getAttribute("aria-current")).toBeNull();
  });
});

describe("WorkspaceUtilities uniqueness", () => {
  it("renders one Settings entry and, for an admin, one Audit log entry", () => {
    const { unmount } = render(<WorkspaceUtilities role="admin" />);

    expect(screen.getAllByRole("button", { name: "Settings" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Audit log" })).toHaveLength(1);
    unmount();

    render(<WorkspaceUtilities role="viewer" />);

    expect(screen.getAllByRole("button", { name: "Settings" })).toHaveLength(1);
    expect(screen.queryAllByRole("button", { name: "Audit log" })).toHaveLength(0);
  });

  it("names who is signed in and how", () => {
    render(<WorkspaceUtilities role="editor" username="ada" kind="break-glass" />);

    expect(screen.getByTestId("signed-in-username").textContent).toBe("ada");
    expect(screen.getByTestId("signed-in-role").textContent).toBe("editor");

    const identity = screen.getByTestId("signed-in-username").parentElement;
    expect(identity?.getAttribute("title")).toBe("Signed in via break-glass admin");
  });
});
