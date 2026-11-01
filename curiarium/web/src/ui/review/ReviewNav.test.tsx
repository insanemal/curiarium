// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { Role } from "../../lib/auth";
import type { ReviewQueueKey } from "../views";
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
  ])("renders the review queue only from contributor up (%s)", (role, visible) => {
    render(<ReviewNav role={role} counts={{ bundles: 3 }} />);

    expect(!!screen.queryByRole("button", { name: /^Bundles to confirm/ })).toBe(visible);
  });

  it("renders nothing while the session's role is unknown", () => {
    const unknown = render(<ReviewNav role={null} counts={{ bundles: 3 }} />);
    expect(unknown.container.innerHTML).toBe("");
    cleanup();

    const absent = render(<ReviewNav role={undefined} counts={{ bundles: 3 }} />);
    expect(absent.container.innerHTML).toBe("");
  });
});

describe("ReviewNav counts", () => {
  it("shows a dash, not a zero, while the count is unknown", () => {
    render(<ReviewNav role="editor" counts={{ bundles: null }} />);

    const count = screen.getByTestId("review-count-bundles");
    expect(count.textContent).toBe("\u2013");
    expect(count.getAttribute("title")).toBe("Count unavailable");
  });

  it("shows a genuine zero for an empty section", () => {
    render(<ReviewNav role="editor" counts={{ bundles: 0 }} />);

    const count = screen.getByTestId("review-count-bundles");
    expect(count.textContent).toBe("0");
    expect(count.getAttribute("title")).toBe("0 bundles awaiting a decision");
  });

  it("shows the count once it is known, with a different title", () => {
    render(<ReviewNav role="editor" counts={{ bundles: 128 }} />);

    const count = screen.getByTestId("review-count-bundles");
    expect(count.textContent).toBe("128");
    expect(count.getAttribute("title")).toBe("128 bundles awaiting a decision");
  });

  it("uses the singular noun for exactly one row", () => {
    render(<ReviewNav role="editor" counts={{ bundles: 1 }} />);

    expect(screen.getByTestId("review-count-bundles").getAttribute("title")).toBe(
      "1 bundle awaiting a decision",
    );
  });

  it("dashes the count when the counts have not been passed at all", () => {
    render(<ReviewNav role="editor" />);

    expect(screen.getByTestId("review-count-bundles").textContent).toBe("\u2013");
  });

  it("announces the count in the button's name, not only in the badge", () => {
    const { unmount } = render(<ReviewNav role="editor" counts={{ bundles: 7 }} />);

    const known = screen.getByRole("button", {
      name: /^Bundles to confirm: 7 bundles awaiting a decision$/,
    });
    expect(known.getAttribute("aria-label")).toContain("7");
    expect(known.getAttribute("aria-label")).toContain("awaiting a decision");
    unmount();

    render(<ReviewNav role="editor" counts={{ bundles: null }} />);

    expect(
      screen.getByRole("button", { name: /^Bundles to confirm: Count unavailable$/ }),
    ).toBeTruthy();
  });
});

describe("ReviewNav selection", () => {
  it("marks the row as the current page only for the open queue", () => {
    render(<ReviewNav role="editor" activeView="review" activeQueue="bundles" counts={{ bundles: 2 }} />);

    expect(
      screen.getByRole("button", { name: /^Bundles to confirm/ }).getAttribute("aria-current"),
    ).toBe("page");
  });

  it("leaves the row unmarked while the library mode is showing", () => {
    render(<ReviewNav role="editor" activeView="library" activeQueue="bundles" counts={{ bundles: 2 }} />);

    expect(
      screen.getByRole("button", { name: /^Bundles to confirm/ }).getAttribute("aria-current"),
    ).toBeNull();
  });

  it("leaves the row unmarked while another queue is open", () => {
    // `imports` arrives with a later slice; the row must not claim the page
    // even when the open key is one this component does not render yet.
    const otherQueue = "imports" as unknown as ReviewQueueKey;
    render(<ReviewNav role="editor" activeView="review" activeQueue={otherQueue} counts={{ bundles: 2 }} />);

    expect(
      screen.getByRole("button", { name: /^Bundles to confirm/ }).getAttribute("aria-current"),
    ).toBeNull();
  });

  it("leaves the row unmarked when no queue is open", () => {
    render(<ReviewNav role="editor" activeView="review" activeQueue={null} counts={{ bundles: 2 }} />);

    expect(
      screen.getByRole("button", { name: /^Bundles to confirm/ }).getAttribute("aria-current"),
    ).toBeNull();
  });
});

describe("ReviewNav navigation", () => {
  it("opens the bundle queue once on click", () => {
    const onOpenQueue = vi.fn();
    render(
      <ReviewNav role="editor" counts={{ bundles: 5 }} onOpenQueue={onOpenQueue} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /^Bundles to confirm/ }));

    expect(onOpenQueue).toHaveBeenCalledTimes(1);
    expect(onOpenQueue).toHaveBeenCalledWith("bundles");
  });

  it("heads the group with the word Review", () => {
    const { container } = render(<ReviewNav role="editor" counts={{ bundles: 5 }} />);

    const heading = container.querySelector(".sidebar-section-heading");
    expect(heading?.textContent).toBe("Review");
    expect(container.querySelector(".sidebar-review")).toBeTruthy();
  });
});
