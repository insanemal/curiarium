// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import CardMenu, { type CardMenuSection } from "./CardMenu";

/**
 * Mirrors of the module-private constants in `CardMenu.tsx`. They are not
 * exported, which is deliberate: the assertions below are exact pixel values, so
 * a change to any of them fails here by number instead of drifting silently.
 */
const MENU_WIDTH = 224;
const MENU_GAP = 4;
const VIEWPORT_MARGIN = 8;

/**
 * jsdom's `innerWidth`/`innerHeight` are own accessors. Read them once so each
 * test can put the real ones back; a missing descriptor throws here instead of
 * leaving a stubbed viewport behind for the next test in the file.
 */
function viewportDescriptor(key: "innerWidth" | "innerHeight"): PropertyDescriptor {
  const descriptor = Object.getOwnPropertyDescriptor(window, key);
  if (!descriptor) throw new Error(`jsdom installs no own ${key} descriptor`);
  return descriptor;
}

const realViewport = {
  innerWidth: viewportDescriptor("innerWidth"),
  innerHeight: viewportDescriptor("innerHeight"),
};

function setViewport(width: number, height: number): void {
  Object.defineProperty(window, "innerWidth", { value: width, configurable: true });
  Object.defineProperty(window, "innerHeight", { value: height, configurable: true });
}

function restoreViewport(): void {
  Object.defineProperty(window, "innerWidth", realViewport.innerWidth);
  Object.defineProperty(window, "innerHeight", realViewport.innerHeight);
}

/**
 * jsdom has no layout, so a `DOMRect`-shaped value has to be supplied by hand.
 * Only the edges the placement reads are populated; `left` mirrors `right` so the
 * value stays internally consistent at the zero width nothing reads.
 */
function rectOf(top: number, height: number, right = 0): DOMRect {
  return {
    top,
    bottom: top + height,
    height,
    left: right,
    right,
    width: 0,
    x: 0,
    y: top,
    toJSON: () => ({}),
  } as DOMRect;
}

type TriggerGeometry = { top: number; height: number; right?: number };

/**
 * Give jsdom the two rects a browser would compute: the trigger's, read when the
 * menu opens, and the menu's own height, read once in the layout effect. There is
 * no menu element to spy on before it mounts, so the prototype is stubbed and
 * each call is routed by the attributes that identify its node.
 */
function stubGeometry(trigger: TriggerGeometry, menuHeight: number): { menuMeasurements: () => number } {
  let menuMeasurements = 0;
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element): DOMRect {
    if (this.getAttribute("role") === "menu") {
      menuMeasurements += 1;
      return rectOf(0, menuHeight);
    }
    if (this.getAttribute("aria-haspopup") === "menu") {
      return rectOf(trigger.top, trigger.height, trigger.right);
    }
    return rectOf(0, 0);
  });
  return { menuMeasurements: () => menuMeasurements };
}

/**
 * The two invariants every placement owes the owner's report: the menu starts at
 * or below the top margin, and its rendered bottom is inside the bottom margin.
 * `menuHeight` is the height the stub reported; a capped menu renders shorter.
 */
function expectMenuWithinViewport(menuHeight: number, viewportHeight: number): void {
  const panel = menu();
  const top = Number.parseFloat(panel.style.top);
  const maxHeight = panel.style.maxHeight ? Number.parseFloat(panel.style.maxHeight) : undefined;
  const height = Math.min(menuHeight, maxHeight ?? menuHeight);
  expect(top).toBeGreaterThanOrEqual(VIEWPORT_MARGIN);
  expect(top + height).toBeLessThanOrEqual(viewportHeight - VIEWPORT_MARGIN);
}

/**
 * The other half of the owner's report: the menu has to sit wholly on one side of
 * its trigger rather than straddling it. This is the placement arithmetic only;
 * whether the real CSS box measures what the stub was told is a browser fact.
 */
function expectMenuClearOfTrigger(anchor: { top: number; bottom: number }, menuHeight: number): void {
  const panel = menu();
  const top = Number.parseFloat(panel.style.top);
  const maxHeight = panel.style.maxHeight ? Number.parseFloat(panel.style.maxHeight) : undefined;
  const height = Math.min(menuHeight, maxHeight ?? menuHeight);
  const below = top >= anchor.bottom + MENU_GAP;
  const above = top + height <= anchor.top - MENU_GAP;
  expect(below || above).toBe(true);
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  restoreViewport();
});

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

  it("cycles between items that share an initial on a repeated key, wrapping at the end", () => {
    // A realistic kebab menu: three items start with "D".
    const sections: CardMenuSection[] = [
      {
        items: [
          { label: "Download", onSelect: () => {} },
          { label: "Download tag: pla", onSelect: () => {} },
          { label: "Download tag: petg", onSelect: () => {} },
        ],
      },
    ];
    render(<CardMenu label="More actions" sections={sections} />);
    openMenu();

    fireEvent.keyDown(menu(), { key: "d" });
    expect(activeItemLabel()).toBe("Download");

    fireEvent.keyDown(menu(), { key: "d" });
    expect(activeItemLabel()).toBe("Download tag: pla");

    fireEvent.keyDown(menu(), { key: "d" });
    expect(activeItemLabel()).toBe("Download tag: petg");

    // A repeated key walks the matches and wraps back to the first.
    fireEvent.keyDown(menu(), { key: "d" });
    expect(activeItemLabel()).toBe("Download");
  });

  it("skips a disabled same-initial item when cycling", () => {
    const sections: CardMenuSection[] = [
      {
        items: [
          { label: "Download", onSelect: () => {} },
          { label: "Download by tag", onSelect: () => {}, disabled: true },
          { label: "Download by material", onSelect: () => {} },
        ],
      },
    ];
    render(<CardMenu label="More actions" sections={sections} />);
    openMenu();

    fireEvent.keyDown(menu(), { key: "d" });
    expect(activeItemLabel()).toBe("Download");

    fireEvent.keyDown(menu(), { key: "d" });
    expect(activeItemLabel()).toBe("Download by material");
  });

  it("restarts the buffer at a repeated letter after a two-letter prefix", () => {
    // FolderPicker's shipping rule: a repeated character restarts the buffer at
    // that character instead of building the unmatchable "doo".
    const sections: CardMenuSection[] = [
      {
        items: [
          { label: "Open preview", onSelect: () => {} },
          { label: "Download", onSelect: () => {} },
          { label: "Open in new tab", onSelect: () => {} },
          { label: "Open folder", onSelect: () => {} },
        ],
      },
    ];
    render(<CardMenu label="More actions" sections={sections} />);
    openMenu();

    fireEvent.keyDown(menu(), { key: "d" });
    expect(activeItemLabel()).toBe("Download");

    // "do" still matches "Download", so the buffer keeps narrowing in place.
    fireEvent.keyDown(menu(), { key: "o" });
    expect(activeItemLabel()).toBe("Download");

    // The second "o" restarts at "o" and advances past the active item.
    fireEvent.keyDown(menu(), { key: "o" });
    expect(activeItemLabel()).toBe("Open in new tab");
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

/**
 * Placement arithmetic.
 *
 * jsdom computes no layout, so every number here is stubbed: these tests prove
 * that the placement arithmetic reaches the DOM as the intended `top`, `left` and
 * `max-height`, and nothing about real layout. They cannot show that the menu
 * does not overlap its trigger, that a capped menu grows no double scrollbar,
 * that a focus ring is visible inside a scrolled menu, or that a last-row menu
 * stays open after focusing its own first item instead of being closed by the
 * capture-phase scroll listener; those need a real browser.
 */
describe("CardMenu placement", () => {
  const VIEWPORT = { width: 1024, height: 768 };

  /** A card in the last visible row: its trigger's bottom sits 8px above the fold. */
  const LAST_ROW = { top: 744, height: 16, right: 100 };

  beforeEach(() => {
    setViewport(VIEWPORT.width, VIEWPORT.height);
  });

  it("keeps the below-the-trigger position, uncapped, when the menu fits", () => {
    stubGeometry({ top: 100, height: 16, right: 100 }, 200);
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    const panel = menu();
    expect(panel.style.top).toBe(`${100 + 16 + MENU_GAP}px`);
    expect(panel.style.left).toBe(`${VIEWPORT_MARGIN}px`);
    // The uncapped render has to stay what it was before the menu was measured.
    expect(panel.style.maxHeight).toBe("");
    expect(panel.style.overflowY).toBe("");
    expectMenuClearOfTrigger({ top: 100, bottom: 116 }, 200);
  });

  it("flips above the trigger when the menu would run off the bottom", () => {
    stubGeometry(LAST_ROW, 200);
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    // Today's answer would be 764 with a 200px menu, i.e. 196px past the fold.
    const naiveTop = LAST_ROW.top + LAST_ROW.height + MENU_GAP;
    const flippedTop = LAST_ROW.top - MENU_GAP - 200;
    const panel = menu();
    expect(panel.style.top).toBe(`${flippedTop}px`);
    expect(panel.style.top).not.toBe(`${naiveTop}px`);
    expect(panel.style.maxHeight).toBe("");
    expectMenuWithinViewport(200, VIEWPORT.height);
    expectMenuClearOfTrigger({ top: LAST_ROW.top, bottom: LAST_ROW.top + LAST_ROW.height }, 200);
    // Measuring on mount must not cost the menu its opening focus.
    expect(activeItemLabel()).toBe("Open preview");
  });

  it("pulls a right-edge menu back inside the right margin", () => {
    stubGeometry({ top: 100, height: 16, right: 1020 }, 200);
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    const panel = menu();
    expect(panel.style.left).toBe(`${VIEWPORT.width - VIEWPORT_MARGIN - MENU_WIDTH}px`);
    // Right-aligning alone would leave 220px of menu past a 1024px viewport.
    expect(panel.style.left).not.toBe(`${1020 - MENU_WIDTH}px`);
  });

  it("leaves a left-edge menu at the left margin", () => {
    stubGeometry({ top: 100, height: 16, right: 100 }, 200);
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    expect(menu().style.left).toBe(`${VIEWPORT_MARGIN}px`);
  });

  it("caps to the larger gap below and scrolls internally when neither side fits", () => {
    stubGeometry({ top: 300, height: 16, right: 100 }, 800);
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    // gapBelow = 768 - 8 - 320 = 440, gapAbove = 300 - 4 - 8 = 288.
    const panel = menu();
    expect(panel.style.top).toBe(`${300 + 16 + MENU_GAP}px`);
    expect(panel.style.maxHeight).toBe(`${VIEWPORT.height - VIEWPORT_MARGIN - (300 + 16 + MENU_GAP)}px`);
    expect(panel.style.maxHeight).toBe("440px");
    // Not the whole viewport inset: that cap would have to cover the trigger.
    expect(panel.style.maxHeight).not.toBe(`${VIEWPORT.height - 2 * VIEWPORT_MARGIN}px`);
    expect(panel.style.overflowY).toBe("auto");
    expectMenuWithinViewport(800, VIEWPORT.height);
    expectMenuClearOfTrigger({ top: 300, bottom: 316 }, 800);
  });

  it("caps to the larger gap above when that side has more room", () => {
    stubGeometry({ top: 600, height: 16, right: 100 }, 800);
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    // gapAbove = 600 - 4 - 8 = 588 beats gapBelow = 768 - 8 - 620 = 140, so the
    // capped menu sits wholly above its trigger, starting at the top margin.
    const panel = menu();
    expect(panel.style.maxHeight).toBe(`${600 - MENU_GAP - VIEWPORT_MARGIN}px`);
    expect(panel.style.maxHeight).toBe("588px");
    expect(panel.style.top).toBe(`${VIEWPORT_MARGIN}px`);
    expect(panel.style.overflowY).toBe("auto");
    expectMenuWithinViewport(800, VIEWPORT.height);
    expectMenuClearOfTrigger({ top: 600, bottom: 616 }, 800);
  });

  it("prefers below when the two gaps are equal", () => {
    // top 376 puts gapBelow (768 - 8 - 396 = 364) exactly equal to gapAbove (376 - 4 - 8 = 364).
    stubGeometry({ top: 376, height: 16, right: 100 }, 800);
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    const panel = menu();
    expect(panel.style.maxHeight).toBe("364px");
    expect(panel.style.top).toBe(`${376 + 16 + MENU_GAP}px`);
    expectMenuWithinViewport(800, VIEWPORT.height);
    expectMenuClearOfTrigger({ top: 376, bottom: 392 }, 800);
  });

  it("flips above rather than capping when above fits", () => {
    stubGeometry({ top: 684, height: 16, right: 100 }, 300);
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    const panel = menu();
    expect(panel.style.top).toBe(`${684 - MENU_GAP - 300}px`);
    expect(panel.style.top).not.toBe(`${684 + 16 + MENU_GAP}px`);
    expect(panel.style.maxHeight).toBe("");
    expectMenuWithinViewport(300, VIEWPORT.height);
    expectMenuClearOfTrigger({ top: 684, bottom: 700 }, 300);
  });

  it("falls back to the full inset when the trigger leaves a gap on neither side", () => {
    // A trigger taller than the viewport: neither one-sided gap is positive.
    stubGeometry({ top: 0, height: 1000, right: 100 }, 500);
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    const panel = menu();
    expect(panel.style.top).toBe(`${VIEWPORT_MARGIN}px`);
    expect(panel.style.maxHeight).toBe(`${VIEWPORT.height - 2 * VIEWPORT_MARGIN}px`);
    expect(panel.style.overflowY).toBe("auto");
    expectMenuWithinViewport(500, VIEWPORT.height);
  });

  it("measures the menu exactly once and never re-measures", () => {
    const geometry = stubGeometry(LAST_ROW, 200);
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();
    expect(geometry.menuMeasurements()).toBe(1);

    // A resize closes the menu instead of re-measuring it under a moved trigger.
    fireEvent(window, new Event("resize"));
    expect(screen.queryByRole("menu")).toBeNull();
    expect(geometry.menuMeasurements()).toBe(1);
  });
});

/**
 * The lifetime guarantees around the placement. Until now these were asserted in
 * comments only: a scroll or a resize closes the menu, and the focus handed back
 * to the trigger must not scroll the viewport to the card the user just left.
 */
describe("CardMenu scroll and resize lifetime", () => {
  it("closes when the grid's own scroller scrolls, returning focus without scrolling", () => {
    render(<CardMenu label="More actions" sections={makeSections()} />);
    const button = trigger();
    const focus = vi.spyOn(button, "focus");
    openMenu();

    // The grid's scroller is an ancestor-side element the window listener only
    // sees in the capture phase: a scroll event does not bubble at all.
    const scroller = document.createElement("div");
    document.body.appendChild(scroller);
    fireEvent(scroller, new Event("scroll"));
    scroller.remove();

    expect(screen.queryByRole("menu")).toBeNull();
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
  });

  it("closes on a window resize and returns focus to the trigger", () => {
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    fireEvent(window, new Event("resize"));

    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("keeps a capped menu open when the menu itself scrolls", () => {
    // The cap is what lets the menu scroll at all, and its own scroll moves
    // nothing it is anchored to: only a scroll that moves the trigger closes it.
    // Without the exemption the menu would close on the first wheel tick, or as
    // soon as arrow keys focused an item below the cap and the browser scrolled
    // it into view.
    setViewport(1024, 768);
    stubGeometry({ top: 300, height: 16, right: 100 }, 800);
    render(<CardMenu label="More actions" sections={makeSections()} />);
    openMenu();

    const panel = menu();
    expect(panel.style.overflowY).toBe("auto");
    fireEvent(panel, new Event("scroll"));

    expect(screen.queryByRole("menu")).toBe(panel);
  });
});
