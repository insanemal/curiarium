// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { Role } from "../../lib/auth";
import ReviewQueueTabs from "./ReviewQueueTabs";
import { describeQueueCount } from "./useReviewCounts";

afterEach(() => {
  cleanup();
});

/**
 * The tab carries the `review-count-*` test id, so the number itself is the
 * `.sidebar-count` span inside it; the count's wording stays on the tab's title.
 */
function badgeOf(tab: HTMLElement): HTMLElement {
  const badge = tab.querySelector<HTMLElement>(".sidebar-count");
  if (!badge) throw new Error("tab has no count badge");
  return badge;
}

describe("ReviewQueueTabs role gating", () => {
  it.each<[Role, boolean]>([
    ["contributor", true],
    ["editor", true],
    ["admin", true],
    ["viewer", false],
  ])("renders the bundle queue only from contributor up (%s)", (role, visible) => {
    render(<ReviewQueueTabs role={role} counts={{ bundles: 3, imports: null, missing: null, trash: null }} />);

    expect(!!screen.queryByRole("tab", { name: /^Bundles to confirm/ })).toBe(visible);
  });

  // `GET /imports/runs` is require_read, so the imports row is a viewer surface:
  // gating it at contributor would delete a read surface a viewer already has.
  it.each<[Role, boolean]>([
    ["viewer", true],
    ["contributor", true],
    ["editor", true],
    ["admin", true],
  ])("renders the imports queue from viewer up (%s)", (role, visible) => {
    render(<ReviewQueueTabs role={role} counts={{ bundles: 3, imports: 1 , missing: null, trash: null }} />);

    expect(!!screen.queryByRole("tab", { name: /^Imports running/ })).toBe(visible);
  });

  it("gives a viewer the read-only imports row and not the bundle row", () => {
    render(<ReviewQueueTabs role="viewer" counts={{ bundles: 3, imports: 1 , missing: null, trash: null }} />);

    expect(screen.getByRole("tab", { name: /^Imports running/ })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: /^Bundles to confirm/ })).toBeNull();
  });

  it("gives a contributor both rows", () => {
    render(<ReviewQueueTabs role="contributor" counts={{ bundles: 3, imports: 1 , missing: null, trash: null }} />);

    expect(screen.getByRole("tab", { name: /^Bundles to confirm/ })).toBeTruthy();
    expect(screen.getByRole("tab", { name: /^Imports running/ })).toBeTruthy();
  });

  it("renders nothing while the session's role is unknown", () => {
    const unknown = render(<ReviewQueueTabs role={null} counts={{ bundles: 3, imports: null, missing: null, trash: null }} />);
    expect(unknown.container.innerHTML).toBe("");
    cleanup();

    const absent = render(<ReviewQueueTabs role={undefined} counts={{ bundles: 3, imports: null, missing: null, trash: null }} />);
    expect(absent.container.innerHTML).toBe("");
  });
});

describe("ReviewQueueTabs counts", () => {
  it("shows a dash, not a zero, while the count is unknown", () => {
    render(<ReviewQueueTabs role="editor" counts={{ bundles: null, imports: null, missing: null, trash: null }} />);

    const count = screen.getByTestId("review-count-bundles");
    expect(badgeOf(count).textContent).toBe("\u2013");
    expect(count.getAttribute("title")).toBe("Count unavailable");
  });

  it("shows a genuine zero for an empty section", () => {
    render(<ReviewQueueTabs role="editor" counts={{ bundles: 0, imports: null, missing: null, trash: null }} />);

    const count = screen.getByTestId("review-count-bundles");
    expect(badgeOf(count).textContent).toBe("0");
    expect(count.getAttribute("title")).toBe("0 bundles awaiting a decision");
  });

  it("shows the count once it is known, with a different title", () => {
    render(<ReviewQueueTabs role="editor" counts={{ bundles: 128, imports: null, missing: null, trash: null }} />);

    const count = screen.getByTestId("review-count-bundles");
    expect(badgeOf(count).textContent).toBe("128");
    expect(count.getAttribute("title")).toBe("128 bundles awaiting a decision");
  });

  it("uses the singular noun for exactly one row", () => {
    render(<ReviewQueueTabs role="editor" counts={{ bundles: 1, imports: null, missing: null, trash: null }} />);

    expect(screen.getByTestId("review-count-bundles").getAttribute("title")).toBe(
      "1 bundle awaiting a decision",
    );
  });

  it("dashes the count when the counts have not been passed at all", () => {
    render(<ReviewQueueTabs role="editor" />);

    expect(badgeOf(screen.getByTestId("review-count-bundles")).textContent).toBe("\u2013");
    expect(badgeOf(screen.getByTestId("review-count-imports")).textContent).toBe("\u2013");
  });

  it("announces the count in the button's name, not only in the badge", () => {
    const { unmount } = render(<ReviewQueueTabs role="editor" counts={{ bundles: 7, imports: null, missing: null, trash: null }} />);

    const known = screen.getByRole("tab", {
      name: /^Bundles to confirm: 7 bundles awaiting a decision$/,
    });
    expect(known.getAttribute("aria-label")).toContain("7");
    expect(known.getAttribute("aria-label")).toContain("awaiting a decision");
    unmount();

    render(<ReviewQueueTabs role="editor" counts={{ bundles: null, imports: null, missing: null, trash: null }} />);

    expect(
      screen.getByRole("tab", { name: /^Bundles to confirm: Count unavailable$/ }),
    ).toBeTruthy();
  });
});

describe("ReviewQueueTabs imports count (AC-8)", () => {
  it("shows an en dash for an unknown imports count, never a zero", () => {
    render(<ReviewQueueTabs role="viewer" counts={{ bundles: null, imports: null, missing: null, trash: null }} />);

    const count = screen.getByTestId("review-count-imports");
    expect(badgeOf(count).textContent).toBe("\u2013");
    expect(count.getAttribute("title")).toBe("Count unavailable");
  });

  it("says one import is running", () => {
    render(<ReviewQueueTabs role="viewer" counts={{ bundles: null, imports: 1 , missing: null, trash: null }} />);

    const count = screen.getByTestId("review-count-imports");
    expect(badgeOf(count).textContent).toBe("1");
    expect(count.getAttribute("title")).toBe("1 import running");
  });

  it("treats a zero as a known idle queue", () => {
    render(<ReviewQueueTabs role="viewer" counts={{ bundles: null, imports: 0 , missing: null, trash: null }} />);

    const count = screen.getByTestId("review-count-imports");
    expect(badgeOf(count).textContent).toBe("0");
    expect(count.getAttribute("title")).toBe("No import currently running");
  });

  it("announces the imports count in the button's name", () => {
    render(<ReviewQueueTabs role="viewer" counts={{ bundles: null, imports: 1 , missing: null, trash: null }} />);

    expect(
      screen.getByRole("tab", { name: "Imports running: 1 import running" }),
    ).toBeTruthy();
  });
});

describe("ReviewQueueTabs selection", () => {
  it("marks the row as the current page only for the open queue", () => {
    render(<ReviewQueueTabs role="editor" activeQueue="bundles" counts={{ bundles: 2, imports: null, missing: null, trash: null }} />);

    expect(
      screen.getByRole("tab", { name: /^Bundles to confirm/ }).getAttribute("aria-selected"),
    ).toBe("true");
  });

  // The row only renders inside Review, so there is no `activeView` to check:
  // with no queue resolved yet nothing is selected, and the group stays usable.
  it("leaves every row unselected when no queue is open", () => {
    render(<ReviewQueueTabs role="viewer" activeQueue={null} counts={{ bundles: null, imports: 1, missing: null, trash: null }} />);

    for (const tab of screen.getAllByRole("tab")) {
      expect(tab.getAttribute("aria-selected")).toBe("false");
    }
  });

  it("puts the page marker on the imports row when that queue is open", () => {
    render(
      <ReviewQueueTabs
        role="editor"
        activeQueue="imports"
        counts={{ bundles: 2, imports: 1 , missing: null, trash: null }}
      />,
    );

    expect(
      screen.getByRole("tab", { name: /^Imports running/ }).getAttribute("aria-selected"),
    ).toBe("true");
    expect(
      screen.getByRole("tab", { name: /^Bundles to confirm/ }).getAttribute("aria-selected"),
    ).toBe("false");
  });

  it("leaves the row unmarked when no queue is open", () => {
    render(<ReviewQueueTabs role="editor" activeQueue={null} counts={{ bundles: 2, imports: null, missing: null, trash: null }} />);

    expect(
      screen.getByRole("tab", { name: /^Bundles to confirm/ }).getAttribute("aria-selected"),
    ).toBe("false");
  });
});

describe("ReviewQueueTabs navigation", () => {
  it("opens the bundle queue once on click", () => {
    const onSelect = vi.fn();
    render(
      <ReviewQueueTabs role="editor" counts={{ bundles: 5, imports: 0 , missing: null, trash: null }} onSelect={onSelect} />,
    );

    fireEvent.click(screen.getByRole("tab", { name: /^Bundles to confirm/ }));

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("bundles");
  });

  it("opens the imports queue once on click", () => {
    const onSelect = vi.fn();
    render(
      <ReviewQueueTabs role="viewer" counts={{ bundles: null, imports: 1 , missing: null, trash: null }} onSelect={onSelect} />,
    );

    fireEvent.click(screen.getByRole("tab", { name: /^Imports running/ }));

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("imports");
  });

  it("heads the group with the word Review", () => {
    const { container } = render(<ReviewQueueTabs role="editor" counts={{ bundles: 5, imports: 0 , missing: null, trash: null }} />);

    const group = screen.getByRole("tablist", { name: "Review" });
    expect(group.classList.contains("review-tabs")).toBe(true);
    expect(container.querySelector(".review-tabs")).toBe(group);
  });
});

// `GET /assets?missing=true` and `GET /library/scan` are both require_read, so
// the missing queue is a viewer surface: only the rescan it offers is a WRITE.
describe("ReviewQueueTabs missing queue (Mf-1tx3)", () => {
  it.each<Role>(["viewer", "contributor", "editor", "admin"])(
    "shows the missing row from viewer up (%s)",
    role => {
      render(<ReviewQueueTabs role={role} counts={{ bundles: null, imports: null, missing: 2, trash: null }} />);

      expect(screen.getByRole("tab", { name: /^Missing files/ })).toBeTruthy();
    },
  );

  it("gives a viewer the missing row", () => {
    render(<ReviewQueueTabs role="viewer" counts={{ bundles: 3, imports: null, missing: 2, trash: null }} />);

    expect(screen.getByRole("tab", { name: /^Missing files/ })).toBeTruthy();
    // The row gate is per queue: the bundle queue still needs CONTRIBUTE.
    expect(screen.queryByRole("tab", { name: /^Bundles to confirm/ })).toBeNull();
  });

  it("dashes an unknown missing count, never a zero", () => {
    render(<ReviewQueueTabs role="viewer" counts={{ bundles: null, imports: null, missing: null, trash: null }} />);

    const count = screen.getByTestId("review-count-missing");
    expect(badgeOf(count).textContent).toBe("\u2013");
    expect(count.getAttribute("title")).toBe("Count unavailable");
  });

  it("says how many missing files await a decision", () => {
    const { unmount } = render(
      <ReviewQueueTabs role="viewer" counts={{ bundles: null, imports: null, missing: 4, trash: null }} />,
    );

    const count = screen.getByTestId("review-count-missing");
    expect(badgeOf(count).textContent).toBe("4");
    expect(count.getAttribute("title")).toBe("4 missing files awaiting a decision");
    unmount();

    render(<ReviewQueueTabs role="viewer" counts={{ bundles: null, imports: null, missing: 1, trash: null }} />);

    expect(screen.getByTestId("review-count-missing").getAttribute("title")).toBe(
      "1 missing file awaiting a decision",
    );
  });

  it("marks the missing row current only when it is open", () => {
    render(
      <ReviewQueueTabs
        role="editor"
        activeQueue="missing"
        counts={{ bundles: 2, imports: 1, missing: 4, trash: null }}
      />,
    );

    expect(
      screen.getByRole("tab", { name: /^Missing files/ }).getAttribute("aria-selected"),
    ).toBe("true");
    expect(
      screen.getByRole("tab", { name: /^Bundles to confirm/ }).getAttribute("aria-selected"),
    ).toBe("false");
    expect(
      screen.getByRole("tab", { name: /^Imports running/ }).getAttribute("aria-selected"),
    ).toBe("false");
  });

  it("opens the missing queue once on click", () => {
    const onSelect = vi.fn();
    render(
      <ReviewQueueTabs
        role="viewer"
        counts={{ bundles: null, imports: null, missing: 4, trash: null }}
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByRole("tab", { name: /^Missing files/ }));

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("missing");
  });
});

// `GET /trash` is require_read, so the trash row is a viewer surface; only the
// Empty trash control inside the queue is admin, and the queue hides it rather
// than disabling it.
describe("ReviewQueueTabs trash queue (slice 4)", () => {
  it.each<Role>(["admin", "editor", "contributor", "viewer"])(
    "shows the trash row from viewer up (%s)",
    role => {
      render(<ReviewQueueTabs role={role} counts={{ bundles: null, imports: null, missing: null, trash: 2 }} />);

      expect(screen.getByRole("tab", { name: /^Trash/ })).toBeTruthy();
    },
  );

  it("dashes an unknown trash count, never a zero", () => {
    render(<ReviewQueueTabs role="viewer" counts={{ bundles: null, imports: null, missing: null, trash: null }} />);

    const count = screen.getByTestId("review-count-trash");
    expect(badgeOf(count).textContent).toBe("\u2013");
    expect(count.getAttribute("title")).toBe("Count unavailable");
  });

  it("shows the entry count with the trash wording in its title", () => {
    const { unmount } = render(
      <ReviewQueueTabs role="editor" counts={{ bundles: null, imports: null, missing: null, trash: 12 }} />,
    );

    const count = screen.getByTestId("review-count-trash");
    expect(badgeOf(count).textContent).toBe("12");
    expect(count.getAttribute("title")).toBe(describeQueueCount("trash", 12));
    expect(count.getAttribute("title")).toBe("12 entries in the trash");
    unmount();

    render(<ReviewQueueTabs role="editor" counts={{ bundles: null, imports: null, missing: null, trash: 1 }} />);

    expect(screen.getByTestId("review-count-trash").getAttribute("title")).toBe(
      "1 entry in the trash",
    );
  });

  it("announces the trash count in the button's name", () => {
    render(<ReviewQueueTabs role="viewer" counts={{ bundles: null, imports: null, missing: null, trash: 3 }} />);

    expect(
      screen.getByRole("tab", { name: "Trash: 3 entries in the trash" }),
    ).toBeTruthy();
  });

  it("marks the trash row current only when it is the open queue", () => {
    const { unmount } = render(
      <ReviewQueueTabs
        role="viewer"
        activeQueue="trash"
        counts={{ bundles: null, imports: null, missing: null, trash: 2 }}
      />,
    );

    expect(
      screen.getByRole("tab", { name: /^Trash/ }).getAttribute("aria-selected"),
    ).toBe("true");
    expect(
      screen.getByRole("tab", { name: /^Missing files/ }).getAttribute("aria-selected"),
    ).toBe("false");
    expect(
      screen.getByRole("tab", { name: /^Imports running/ }).getAttribute("aria-selected"),
    ).toBe("false");
    unmount();

    render(
      <ReviewQueueTabs
        role="viewer"
        activeQueue="imports"
        counts={{ bundles: null, imports: null, missing: null, trash: 2 }}
      />,
    );

    expect(
      screen.getByRole("tab", { name: /^Trash/ }).getAttribute("aria-selected"),
    ).toBe("false");
  });

  it("opens the trash queue once on click", () => {
    const onSelect = vi.fn();
    render(
      <ReviewQueueTabs
        role="viewer"
        counts={{ bundles: null, imports: null, missing: null, trash: 2 }}
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByRole("tab", { name: /^Trash/ }));

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("trash");
  });
});

// APG tabs contract: arrows and Home/End move focus only, activation is manual,
// and the group holds exactly one tab stop. Automatic activation would fetch a
// queue on every arrow key - a whole-library walk for the bundle queue.
describe("ReviewQueueTabs keyboard (AC-6)", () => {
  it("moves focus with the arrow keys, wrapping at both ends, and activates nothing", () => {
    const onSelect = vi.fn();
    render(
      <ReviewQueueTabs
        role="viewer"
        activeQueue="imports"
        counts={{ bundles: null, imports: 1, missing: null, trash: null }}
        onSelect={onSelect}
      />,
    );

    const imports = screen.getByRole("tab", { name: /^Imports running/ });
    const missing = screen.getByRole("tab", { name: /^Missing files/ });
    const trash = screen.getByRole("tab", { name: /^Trash/ });

    imports.focus();
    fireEvent.keyDown(imports, { key: "ArrowRight" });
    expect(document.activeElement).toBe(missing);

    fireEvent.keyDown(missing, { key: "ArrowDown" });
    expect(document.activeElement).toBe(trash);

    // Past the last row wraps to the first.
    fireEvent.keyDown(trash, { key: "ArrowRight" });
    expect(document.activeElement).toBe(imports);

    // And before the first row wraps to the last.
    fireEvent.keyDown(imports, { key: "ArrowLeft" });
    expect(document.activeElement).toBe(trash);

    fireEvent.keyDown(trash, { key: "ArrowUp" });
    expect(document.activeElement).toBe(missing);

    expect(onSelect).not.toHaveBeenCalled();
  });

  it("moves focus to the ends with Home and End and activates nothing", () => {
    const onSelect = vi.fn();
    render(
      <ReviewQueueTabs
        role="viewer"
        activeQueue="missing"
        counts={{ bundles: null, imports: 1, missing: null, trash: null }}
        onSelect={onSelect}
      />,
    );

    const imports = screen.getByRole("tab", { name: /^Imports running/ });
    const missing = screen.getByRole("tab", { name: /^Missing files/ });
    const trash = screen.getByRole("tab", { name: /^Trash/ });

    missing.focus();
    fireEvent.keyDown(missing, { key: "End" });
    expect(document.activeElement).toBe(trash);

    fireEvent.keyDown(trash, { key: "Home" });
    expect(document.activeElement).toBe(imports);

    expect(onSelect).not.toHaveBeenCalled();
  });

  it("activates the focused tab once on Enter", () => {
    const onSelect = vi.fn();
    render(
      <ReviewQueueTabs
        role="viewer"
        activeQueue="imports"
        counts={{ bundles: null, imports: 1, missing: null, trash: null }}
        onSelect={onSelect}
      />,
    );

    const missing = screen.getByRole("tab", { name: /^Missing files/ });
    missing.focus();
    fireEvent.keyDown(missing, { key: "Enter" });

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("missing");
  });

  it("activates the focused tab once on Space", () => {
    const onSelect = vi.fn();
    render(
      <ReviewQueueTabs
        role="viewer"
        activeQueue="imports"
        counts={{ bundles: null, imports: 1, missing: null, trash: null }}
        onSelect={onSelect}
      />,
    );

    const trash = screen.getByRole("tab", { name: /^Trash/ });
    trash.focus();
    fireEvent.keyDown(trash, { key: " " });

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("trash");
  });

  it("activates a tab that is not the open one on click", () => {
    const onSelect = vi.fn();
    render(
      <ReviewQueueTabs
        role="viewer"
        activeQueue="missing"
        counts={{ bundles: null, imports: 1, missing: null, trash: null }}
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByRole("tab", { name: /^Imports running/ }));

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith("imports");
  });

  it("gives the group exactly one tab stop, on the selected tab", () => {
    render(
      <ReviewQueueTabs
        role="viewer"
        activeQueue="missing"
        counts={{ bundles: null, imports: 1, missing: null, trash: null }}
      />,
    );

    // Table order is imports, missing, trash for a viewer.
    expect(screen.getAllByRole("tab").map(tab => (tab as HTMLButtonElement).tabIndex)).toEqual([
      -1, 0, -1,
    ]);
  });

  it("hands the tab stop to the first visible row when the selected key is not visible", () => {
    // A viewer can hold "bundles" after a demotion; App resolves the key, but a
    // row that lost its selection must still leave the group keyboard-reachable.
    render(
      <ReviewQueueTabs
        role="viewer"
        activeQueue="bundles"
        counts={{ bundles: null, imports: 1, missing: null, trash: null }}
      />,
    );

    expect(screen.getAllByRole("tab").map(tab => (tab as HTMLButtonElement).tabIndex)).toEqual([
      0, -1, -1,
    ]);
  });

  it("selects exactly one tab", () => {
    render(
      <ReviewQueueTabs
        role="viewer"
        activeQueue="trash"
        counts={{ bundles: null, imports: 1, missing: null, trash: 2 }}
      />,
    );

    const selected = screen
      .getAllByRole("tab")
      .filter(tab => tab.getAttribute("aria-selected") === "true");

    expect(selected).toHaveLength(1);
    expect(selected[0].getAttribute("data-queue")).toBe("trash");
  });

  it.each<[Role | null | undefined]>([[null], [undefined]])(
    "renders no tablist at all while the role is unknown (%s)",
    role => {
      render(<ReviewQueueTabs role={role} counts={{ bundles: null, imports: null, missing: null, trash: null }} />);

      expect(screen.queryByRole("tablist")).toBeNull();
    },
  );

  it("pairs every tab with the panel its aria-controls names", () => {
    render(
      <>
        <ReviewQueueTabs
          role="viewer"
          activeQueue="missing"
          counts={{ bundles: null, imports: 1, missing: 2, trash: null }}
        />
        <div
          role="tabpanel"
          id="review-tabpanel-missing"
          aria-labelledby="review-tab-missing"
        />
      </>,
    );

    const missing = screen.getByRole("tab", { name: /^Missing files/ });
    expect(missing.id).toBe("review-tab-missing");
    expect(missing.getAttribute("aria-controls")).toBe("review-tabpanel-missing");

    // The selected tab's aria-controls has to resolve to a real element, or the
    // panel is announced without a tab to own it.
    const panelId = missing.getAttribute("aria-controls");
    expect(document.getElementById(panelId as string)?.getAttribute("role")).toBe("tabpanel");

    expect(screen.getByTestId("review-count-imports").getAttribute("aria-controls")).toBe(
      "review-tabpanel-imports",
    );
    expect(screen.getByTestId("review-count-trash").id).toBe("review-tab-trash");
  });

  it("announces every visible tab's count, dash and zero included", () => {
    render(
      <ReviewQueueTabs
        role="viewer"
        counts={{ bundles: null, imports: 1, missing: null, trash: 0 }}
      />,
    );

    expect(screen.getByRole("tab", { name: "Imports running: 1 import running" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Missing files: Count unavailable" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Trash: No entries in the trash" })).toBeTruthy();
  });
});
