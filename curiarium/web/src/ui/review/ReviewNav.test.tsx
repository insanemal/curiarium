// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { Role } from "../../lib/auth";
import ReviewNav from "./ReviewNav";

afterEach(() => {
  cleanup();
});

describe("ReviewNav role gating", () => {
  it.each<[Role, boolean]>([
    ["contributor", true],
    ["editor", true],
    ["admin", true],
    ["viewer", false],
  ])("renders the bundle queue only from contributor up (%s)", (role, visible) => {
    render(<ReviewNav role={role} counts={{ bundles: 3, imports: null }} />);

    expect(!!screen.queryByRole("button", { name: /^Bundles to confirm/ })).toBe(visible);
  });

  // `GET /imports/runs` is require_read, so the imports row is a viewer surface:
  // gating it at contributor would delete a read surface a viewer already has.
  it.each<[Role, boolean]>([
    ["viewer", true],
    ["contributor", true],
    ["editor", true],
    ["admin", true],
  ])("renders the imports queue from viewer up (%s)", (role, visible) => {
    render(<ReviewNav role={role} counts={{ bundles: 3, imports: 1 }} />);

    expect(!!screen.queryByRole("button", { name: /^Imports running/ })).toBe(visible);
  });

  it("gives a viewer the read-only imports row and not the bundle row", () => {
    render(<ReviewNav role="viewer" counts={{ bundles: 3, imports: 1 }} />);

    expect(screen.getByRole("button", { name: /^Imports running/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Bundles to confirm/ })).toBeNull();
  });

  it("gives a contributor both rows", () => {
    render(<ReviewNav role="contributor" counts={{ bundles: 3, imports: 1 }} />);

    expect(screen.getByRole("button", { name: /^Bundles to confirm/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^Imports running/ })).toBeTruthy();
  });

  it("renders nothing while the session's role is unknown", () => {
    const unknown = render(<ReviewNav role={null} counts={{ bundles: 3, imports: null }} />);
    expect(unknown.container.innerHTML).toBe("");
    cleanup();

    const absent = render(<ReviewNav role={undefined} counts={{ bundles: 3, imports: null }} />);
    expect(absent.container.innerHTML).toBe("");
  });
});

describe("ReviewNav counts", () => {
  it("shows a dash, not a zero, while the count is unknown", () => {
    render(<ReviewNav role="editor" counts={{ bundles: null, imports: null }} />);

    const count = screen.getByTestId("review-count-bundles");
    expect(count.textContent).toBe("\u2013");
    expect(count.getAttribute("title")).toBe("Count unavailable");
  });

  it("shows a genuine zero for an empty section", () => {
    render(<ReviewNav role="editor" counts={{ bundles: 0, imports: null }} />);

    const count = screen.getByTestId("review-count-bundles");
    expect(count.textContent).toBe("0");
    expect(count.getAttribute("title")).toBe("0 bundles awaiting a decision");
  });

  it("shows the count once it is known, with a different title", () => {
    render(<ReviewNav role="editor" counts={{ bundles: 128, imports: null }} />);

    const count = screen.getByTestId("review-count-bundles");
    expect(count.textContent).toBe("128");
    expect(count.getAttribute("title")).toBe("128 bundles awaiting a decision");
  });

  it("uses the singular noun for exactly one row", () => {
    render(<ReviewNav role="editor" counts={{ bundles: 1, imports: null }} />);

    expect(screen.getByTestId("review-count-bundles").getAttribute("title")).toBe(
      "1 bundle awaiting a decision",
    );
  });

  it("dashes the count when the counts have not been passed at all", () => {
    render(<ReviewNav role="editor" />);

    expect(screen.getByTestId("review-count-bundles").textContent).toBe("\u2013");
    expect(screen.getByTestId("review-count-imports").textContent).toBe("\u2013");
  });

  it("announces the count in the button's name, not only in the badge", () => {
    const { unmount } = render(<ReviewNav role="editor" counts={{ bundles: 7, imports: null }} />);

    const known = screen.getByRole("button", {
      name: /^Bundles to confirm: 7 bundles awaiting a decision$/,
    });
    expect(known.getAttribute("aria-label")).toContain("7");
    expect(known.getAttribute("aria-label")).toContain("awaiting a decision");
    unmount();

    render(<ReviewNav role="editor" counts={{ bundles: null, imports: null }} />);

    expect(
      screen.getByRole("button", { name: /^Bundles to confirm: Count unavailable$/ }),
    ).toBeTruthy();
  });
});

describe("ReviewNav imports count (AC-8)", () => {
  it("shows an en dash for an unknown imports count, never a zero", () => {
    render(<ReviewNav role="viewer" counts={{ bundles: null, imports: null }} />);

    const count = screen.getByTestId("review-count-imports");
    expect(count.textContent).toBe("\u2013");
    expect(count.getAttribute("title")).toBe("Count unavailable");
  });

  it("says one import is running", () => {
    render(<ReviewNav role="viewer" counts={{ bundles: null, imports: 1 }} />);

    const count = screen.getByTestId("review-count-imports");
    expect(count.textContent).toBe("1");
    expect(count.getAttribute("title")).toBe("1 import running");
  });

  it("treats a zero as a known idle queue", () => {
    render(<ReviewNav role="viewer" counts={{ bundles: null, imports: 0 }} />);

    const count = screen.getByTestId("review-count-imports");
    expect(count.textContent).toBe("0");
    expect(count.getAttribute("title")).toBe("No import currently running");
  });

  it("announces the imports count in the button's name", () => {
    render(<ReviewNav role="viewer" counts={{ bundles: null, imports: 1 }} />);

    expect(
      screen.getByRole("button", { name: "Imports running: 1 import running" }),
    ).toBeTruthy();
  });
});

describe("ReviewNav selection", () => {
  it("marks the row as the current page only for the open queue", () => {
    render(<ReviewNav role="editor" activeView="review" activeQueue="bundles" counts={{ bundles: 2, imports: null }} />);

    expect(
      screen.getByRole("button", { name: /^Bundles to confirm/ }).getAttribute("aria-current"),
    ).toBe("page");
  });

  it("leaves the row unmarked while the library mode is showing", () => {
    render(<ReviewNav role="editor" activeView="library" activeQueue="bundles" counts={{ bundles: 2, imports: null }} />);

    expect(
      screen.getByRole("button", { name: /^Bundles to confirm/ }).getAttribute("aria-current"),
    ).toBeNull();
  });

  it("puts the page marker on the imports row when that queue is open", () => {
    render(
      <ReviewNav
        role="editor"
        activeView="review"
        activeQueue="imports"
        counts={{ bundles: 2, imports: 1 }}
      />,
    );

    expect(
      screen.getByRole("button", { name: /^Imports running/ }).getAttribute("aria-current"),
    ).toBe("page");
    expect(
      screen.getByRole("button", { name: /^Bundles to confirm/ }).getAttribute("aria-current"),
    ).toBeNull();
  });

  it("leaves the row unmarked when no queue is open", () => {
    render(<ReviewNav role="editor" activeView="review" activeQueue={null} counts={{ bundles: 2, imports: null }} />);

    expect(
      screen.getByRole("button", { name: /^Bundles to confirm/ }).getAttribute("aria-current"),
    ).toBeNull();
  });
});

describe("ReviewNav navigation", () => {
  it("opens the bundle queue once on click", () => {
    const onOpenQueue = vi.fn();
    render(
      <ReviewNav role="editor" counts={{ bundles: 5, imports: 0 }} onOpenQueue={onOpenQueue} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /^Bundles to confirm/ }));

    expect(onOpenQueue).toHaveBeenCalledTimes(1);
    expect(onOpenQueue).toHaveBeenCalledWith("bundles");
  });

  it("opens the imports queue once on click", () => {
    const onOpenQueue = vi.fn();
    render(
      <ReviewNav role="viewer" counts={{ bundles: null, imports: 1 }} onOpenQueue={onOpenQueue} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /^Imports running/ }));

    expect(onOpenQueue).toHaveBeenCalledTimes(1);
    expect(onOpenQueue).toHaveBeenCalledWith("imports");
  });

  it("heads the group with the word Review", () => {
    const { container } = render(<ReviewNav role="editor" counts={{ bundles: 5, imports: 0 }} />);

    const heading = container.querySelector(".sidebar-section-heading");
    expect(heading?.textContent).toBe("Review");
    expect(container.querySelector(".sidebar-review")).toBeTruthy();
  });
});
