// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { Role } from "../lib/auth";
import WorkspaceHeader from "./WorkspaceHeader";

afterEach(() => {
  cleanup();
});

describe("WorkspaceHeader mode switch", () => {
  it("renders exactly one Library mode radiogroup and still switches mode", () => {
    const onModeChange = vi.fn();
    render(<WorkspaceHeader mode="files" onModeChange={onModeChange} />);

    // One header, outside both mode panes: the switch must not be duplicated.
    expect(screen.getAllByRole("radiogroup", { name: "Library mode" })).toHaveLength(1);

    fireEvent.click(screen.getByRole("radio", { name: "Projects" }));

    expect(onModeChange).toHaveBeenCalledTimes(1);
    expect(onModeChange).toHaveBeenCalledWith("projects");
  });
});

describe("WorkspaceHeader review destination", () => {
  it("names the Review entry exactly Review", () => {
    render(<WorkspaceHeader mode="files" onModeChange={vi.fn()} role="viewer" />);

    // No count badge in the name: the counts live inside the destination.
    expect(screen.getByRole("button", { name: "Review" })).toBeTruthy();
  });

  it.each<Role>(["viewer", "contributor", "editor", "admin"])(
    "offers the Review entry to a %s",
    role => {
      render(<WorkspaceHeader mode="files" onModeChange={vi.fn()} role={role} />);

      expect(screen.getAllByRole("button", { name: "Review" })).toHaveLength(1);
    },
  );

  it("hides the Review entry while the role is unknown", () => {
    const { unmount } = render(<WorkspaceHeader mode="files" onModeChange={vi.fn()} role={null} />);

    // No visible queue means no destination to open.
    expect(screen.queryByRole("button", { name: "Review" })).toBeNull();
    unmount();

    render(<WorkspaceHeader mode="files" onModeChange={vi.fn()} role={undefined} />);

    expect(screen.queryByRole("button", { name: "Review" })).toBeNull();
  });

  it("opens Review on click", () => {
    const onOpenReview = vi.fn();
    render(
      <WorkspaceHeader mode="files" onModeChange={vi.fn()} role="viewer" onOpenReview={onOpenReview} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Review" }));

    expect(onOpenReview).toHaveBeenCalledTimes(1);
  });

  it("marks the Review entry current only while Review is the open view", () => {
    const { unmount } = render(
      <WorkspaceHeader mode="files" onModeChange={vi.fn()} role="viewer" activeView="review" />,
    );

    const review = screen.getByRole("button", { name: "Review" });
    expect(review.getAttribute("aria-current")).toBe("true");
    expect(review.className).toContain("is-selected");
    unmount();

    render(<WorkspaceHeader mode="files" onModeChange={vi.fn()} role="viewer" activeView="library" />);

    const idle = screen.getByRole("button", { name: "Review" });
    expect(idle.getAttribute("aria-current")).toBeNull();
    expect(idle.className).not.toContain("is-selected");
  });

  it("paints no queue count badge in the header", () => {
    render(<WorkspaceHeader mode="files" onModeChange={vi.fn()} role="admin" activeView="review" />);

    // Counts belong to the tabs inside Review; a header badge would have to sum
    // an unknown into a zero.
    expect(screen.queryAllByTestId(/^review-count-/)).toHaveLength(0);
  });
});

describe("WorkspaceHeader utilities", () => {
  it.each<[Role | null, number]>([
    [null, 0],
    ["viewer", 0],
    ["contributor", 0],
    ["editor", 0],
    ["admin", 1],
  ])("renders Settings once and Audit log once only for an admin (%s)", (role, auditCount) => {
    render(<WorkspaceHeader mode="projects" onModeChange={vi.fn()} role={role} />);

    expect(screen.getAllByRole("button", { name: "Settings" })).toHaveLength(1);
    expect(screen.queryAllByRole("button", { name: "Audit log" })).toHaveLength(auditCount);
  });

  it("routes Settings and Audit from the header", () => {
    const onOpenSettings = vi.fn();
    const onOpenAudit = vi.fn();
    render(
      <WorkspaceHeader
        mode="projects"
        onModeChange={vi.fn()}
        role="admin"
        onOpenSettings={onOpenSettings}
        onOpenAudit={onOpenAudit}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    fireEvent.click(screen.getByRole("button", { name: "Audit log" }));

    expect(onOpenSettings).toHaveBeenCalledTimes(1);
    expect(onOpenAudit).toHaveBeenCalledTimes(1);
  });
});

describe("WorkspaceHeader narrow arrangement", () => {
  it("reflects the drawer state on the Folders toggle", () => {
    const onToggleDrawer = vi.fn();
    const { rerender } = render(
      <WorkspaceHeader
        narrow
        mode="files"
        onModeChange={vi.fn()}
        drawerOpen={false}
        onToggleDrawer={onToggleDrawer}
        drawerId="library-folders"
        role="viewer"
      />,
    );

    const toggle = screen.getByRole("button", { name: "Folders" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.getAttribute("aria-controls")).toBe("library-folders");

    fireEvent.click(toggle);
    expect(onToggleDrawer).toHaveBeenCalledTimes(1);

    rerender(
      <WorkspaceHeader
        narrow
        mode="files"
        onModeChange={vi.fn()}
        drawerOpen
        onToggleDrawer={onToggleDrawer}
        drawerId="library-folders"
        role="viewer"
      />,
    );

    // The attribute is the drawer's own state reflected back, not a constant.
    expect(screen.getByRole("button", { name: "Folders" }).getAttribute("aria-expanded")).toBe(
      "true",
    );
  });

  it("offers the drawer toggle only where a folder rail exists", () => {
    const { unmount } = render(
      <WorkspaceHeader narrow mode="files" onModeChange={vi.fn()} activeView="settings" />,
    );
    expect(screen.queryByRole("button", { name: "Folders" })).toBeNull();
    unmount();

    render(<WorkspaceHeader narrow mode="projects" onModeChange={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Folders" })).toBeNull();
  });

  it("keeps the wide arrangement free of the narrow controls", () => {
    render(<WorkspaceHeader mode="files" onModeChange={vi.fn()} role="admin" />);

    expect(screen.queryByRole("button", { name: "Folders" })).toBeNull();
    expect(screen.queryByRole("button", { name: "More destinations" })).toBeNull();
    // Wide keeps its destinations inline, exactly as before the drawer existed.
    expect(screen.getByRole("button", { name: "Review" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Settings" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Audit log" })).toBeTruthy();
  });
});
