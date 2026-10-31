// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import CardMenu, { type CardMenuSection } from "./CardMenu";

afterEach(cleanup);

/** A realistic card menu: two sections, one labelled, one with a disabled item. */
function makeSections(onSelect = vi.fn()): CardMenuSection[] {
  return [
    {
      items: [
        { label: "Open preview", onSelect },
        { label: "Download", onSelect, disabled: true },
      ],
    },
    {
      label: "Download by tag",
      items: [{ label: "Download tag: pla", onSelect }],
    },
    {
      items: [{ label: "Rename…", onSelect }],
    },
  ];
}

function trigger(): HTMLElement {
  return screen.getByRole("button", { name: "More actions" });
}

function menu(): HTMLElement {
  return screen.getByRole("menu");
}

function openMenu(): void {
  fireEvent.click(trigger());
}

function activeItemLabel(): string | null {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement)) return null;
  return active.getAttribute("role") === "menuitem" ? active.textContent : null;
}

describe("CardMenu trigger", () => {
  it("announces a popup menu and flips aria-expanded when it opens", () => {
    render(<CardMenu label="More actions" sections={makeSections()} />);

    const button = trigger();
    expect(button.getAttribute("aria-haspopup")).toBe("menu");
    expect(button.getAttribute("aria-expanded")).toBe("false");

    openMenu();
    expect(button.getAttribute("aria-expanded")).toBe("true");
  });
});

describe("CardMenu portal and grouping", () => {
  it("portals the menu to document.body instead of nesting it in the card", () => {
    const { container } = render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    expect(container.querySelector('[role="menu"]')).toBeNull();
    expect(document.body.querySelector('[role="menu"]')).not.toBeNull();
  });

  it("gives a labelled section an accessible group name and leaves an unlabelled one ungrouped", () => {
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    // Exactly one section carries a label, so exactly one group exists; the
    // unlabelled sections contribute their buttons to the flat list instead.
    const groups = screen.getAllByRole("group");
    expect(groups).toHaveLength(1);
    expect(groups[0].getAttribute("aria-label")).toBe("Download by tag");
  });

  it("renders menu items in section order", () => {
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    expect(screen.getAllByRole("menuitem").map(item => item.textContent)).toEqual([
      "Open preview",
      "Download",
      "Download tag: pla",
      "Rename…",
    ]);
  });

  it("keeps exactly one item in the roving tab order", () => {
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    const tabIndexes = screen
      .getAllByRole("menuitem")
      .map(item => item.getAttribute("tabindex"));
    expect(tabIndexes.filter(value => value === "0")).toHaveLength(1);
  });
});

describe("CardMenu keyboard navigation", () => {
  it("moves with ArrowDown/ArrowUp, wraps, and skips the disabled item", () => {
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    // "Download" is disabled, so the first enabled neighbour is "Download tag: pla".
    fireEvent.keyDown(menu(), { key: "ArrowDown" });
    expect(activeItemLabel()).toBe("Download tag: pla");

    fireEvent.keyDown(menu(), { key: "ArrowDown" });
    expect(activeItemLabel()).toBe("Rename…");

    fireEvent.keyDown(menu(), { key: "ArrowDown" });
    expect(activeItemLabel()).toBe("Open preview");

    fireEvent.keyDown(menu(), { key: "ArrowUp" });
    expect(activeItemLabel()).toBe("Rename…");
  });

  it("jumps to the first and last enabled items with Home and End", () => {
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    fireEvent.keyDown(menu(), { key: "End" });
    expect(activeItemLabel()).toBe("Rename…");

    fireEvent.keyDown(menu(), { key: "Home" });
    expect(activeItemLabel()).toBe("Open preview");
  });
});

describe("CardMenu typeahead", () => {
  const typeaheadSections: CardMenuSection[] = [
    {
      items: [
        { label: "Alpha", onSelect: () => {} },
        { label: "Beta", onSelect: () => {} },
        { label: "Bravo", onSelect: () => {} },
      ],
    },
  ];

  it("selects the first prefix match and a second letter narrows it", () => {
    render(<CardMenu label="More actions" sections={typeaheadSections} />);
    openMenu();

    fireEvent.keyDown(menu(), { key: "b" });
    expect(activeItemLabel()).toBe("Beta");

    // Still inside the 500 ms buffer, so the second letter extends the prefix.
    fireEvent.keyDown(menu(), { key: "r" });
    expect(activeItemLabel()).toBe("Bravo");
  });
});

describe("CardMenu closing", () => {
  it("closes on Escape and returns focus to the trigger", () => {
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();
    fireEvent.keyDown(menu(), { key: "Escape" });

    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("closes on Tab and returns focus to the trigger", () => {
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();
    fireEvent.keyDown(menu(), { key: "Tab" });

    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("closes on an outside mousedown but not on one inside the menu", () => {
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    fireEvent.mouseDown(menu());
    expect(screen.queryByRole("menu")).not.toBeNull();

    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("menu")).toBeNull();
  });
});

describe("CardMenu selection", () => {
  it("runs the item's onSelect exactly once and closes", () => {
    const onSelect = vi.fn();
    render(<CardMenu label="More actions" sections={makeSections(onSelect)} />);
    openMenu();

    fireEvent.click(screen.getByRole("menuitem", { name: "Open preview" }));

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("does not run onSelect for a disabled item", () => {
    const onSelect = vi.fn();
    render(<CardMenu label="More actions" sections={makeSections(onSelect)} />);
    openMenu();

    fireEvent.click(screen.getByRole("menuitem", { name: "Download" }));

    expect(onSelect).not.toHaveBeenCalled();
  });

  it("returns focus without throwing when the trigger's row unmounted", () => {
    // Virtualised rows are keyed by index: scrolling can unmount the whole card,
    // trigger and its portal together, while a queued Escape is still in flight.
    function Row({ show }: { show: boolean }) {
      return show ? <CardMenu label="More actions" sections={makeSections()} /> : null;
    }

    const { rerender } = render(<Row show />);
    openMenu();
    const orphanedMenu = menu();

    rerender(<Row show={false} />);
    expect(screen.queryByRole("menu")).toBeNull();

    expect(() => fireEvent.keyDown(orphanedMenu, { key: "Escape" })).not.toThrow();
    expect(screen.queryByRole("menu")).toBeNull();
  });
});
