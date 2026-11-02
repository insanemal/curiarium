// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";

import {
  DRAWER_TABBABLE_SELECTOR,
  hiddenByClosedDetails,
  tabbableIn,
  wrapTab,
  type TrapTabEvent,
} from "./focusTrap";

afterEach(() => {
  document.body.innerHTML = "";
});

/** A stand-in for the React keyboard event `wrapTab` consumes. */
function tabEvent(options: { shiftKey?: boolean; defaultPrevented?: boolean } = {}): TrapTabEvent & {
  prevented: boolean;
} {
  const state = {
    defaultPrevented: options.defaultPrevented ?? false,
    shiftKey: options.shiftKey ?? false,
    prevented: false,
    preventDefault(): void {
      state.prevented = true;
      state.defaultPrevented = true;
    },
  };
  return state;
}

function mount(html: string): HTMLElement {
  const root = document.createElement("div");
  root.innerHTML = html;
  document.body.append(root);
  return root;
}

function idsOf(elements: HTMLElement[]): string[] {
  return elements.map(element => element.id);
}

describe("hiddenByClosedDetails", () => {
  it("hides a menu button inside a closed details but not its summary", () => {
    const root = mount(
      '<details><summary id="summary">Actions</summary><div><button id="menu">Rename</button></div></details>',
    );

    expect(hiddenByClosedDetails(root.querySelector("#summary")!)).toBe(false);
    expect(hiddenByClosedDetails(root.querySelector("#menu")!)).toBe(true);
  });

  it("hides a summary that a *different* closed details owns", () => {
    // The inner details is closed and its own summary is fine inside it, but the
    // outer closed details hides the whole inner subtree. Checking only the
    // nearest closed ancestor would call this visible.
    const root = mount(
      '<details id="outer"><summary id="outer-summary">Outer</summary>' +
        '<details id="inner"><summary id="inner-summary">Inner</summary></details></details>',
    );

    expect(hiddenByClosedDetails(root.querySelector("#outer-summary")!)).toBe(false);
    expect(hiddenByClosedDetails(root.querySelector("#inner-summary")!)).toBe(true);
  });

  it("keeps a focusable control rendered inside a summary visible", () => {
    const root = mount(
      '<details><summary id="summary"><button id="in-summary">Open</button></summary></details>',
    );

    expect(hiddenByClosedDetails(root.querySelector("#in-summary")!)).toBe(false);
  });

  it("hides nothing once the details is open", () => {
    const root = mount(
      '<details open><summary id="summary">Actions</summary><div><button id="menu">Rename</button></div></details>',
    );

    expect(hiddenByClosedDetails(root.querySelector("#menu")!)).toBe(false);
  });
});

describe("tabbableIn", () => {
  it("keeps the summary and drops the closed details' menu", () => {
    const root = mount(
      '<details><summary id="summary">Actions</summary><div><button id="menu">Rename</button></div></details>',
    );

    expect(idsOf(tabbableIn(root, DRAWER_TABBABLE_SELECTOR))).toEqual(["summary"]);
  });

  it("lets the menu join once the details is open", () => {
    const root = mount(
      '<details><summary id="summary">Actions</summary><div><button id="menu">Rename</button></div></details>',
    );
    (root.querySelector("details") as HTMLDetailsElement).open = true;

    expect(idsOf(tabbableIn(root, DRAWER_TABBABLE_SELECTOR))).toEqual(["summary", "menu"]);
  });

  it("excludes tabindex=-1 controls and disabled controls", () => {
    const root = mount(
      '<div><button id="plain">Plain</button>' +
        '<button id="roving" tabindex="-1">Chevron</button>' +
        '<button id="off" disabled>Off</button>' +
        '<input id="field"><input id="skipped" tabindex="-1"></div>',
    );

    expect(idsOf(tabbableIn(root, DRAWER_TABBABLE_SELECTOR))).toEqual(["plain", "field"]);
  });

  it("keeps only the roving treeitem, never the non-roving rows", () => {
    const root = mount(
      '<div role="tree">' +
        '<div role="treeitem" id="row-a" tabindex="0"></div>' +
        '<div role="treeitem" id="row-b" tabindex="-1"></div>' +
        "</div>",
    );

    expect(idsOf(tabbableIn(root, DRAWER_TABBABLE_SELECTOR))).toEqual(["row-a"]);
  });
});

describe("wrapTab", () => {
  it("wraps forward from the last member to the first", () => {
    const root = mount('<button id="first">1</button><button id="second">2</button>');
    const first = root.querySelector<HTMLElement>("#first")!;
    const second = root.querySelector<HTMLElement>("#second")!;
    second.focus();
    const event = tabEvent();

    wrapTab(root, "button", event);

    expect(event.prevented).toBe(true);
    expect(document.activeElement).toBe(first);
  });

  it("wraps backward from the first member to the last", () => {
    const root = mount('<button id="first">1</button><button id="second">2</button>');
    const first = root.querySelector<HTMLElement>("#first")!;
    const second = root.querySelector<HTMLElement>("#second")!;
    first.focus();
    const event = tabEvent({ shiftKey: true });

    wrapTab(root, "button", event);

    expect(event.prevented).toBe(true);
    expect(document.activeElement).toBe(second);
  });

  it("leaves a Tab between two members to the browser", () => {
    const root = mount('<button id="first">1</button><button id="second">2</button>');
    const first = root.querySelector<HTMLElement>("#first")!;
    first.focus();
    const event = tabEvent();

    wrapTab(root, "button", event);

    expect(event.prevented).toBe(false);
    expect(document.activeElement).toBe(first);
  });

  it("keeps focus inside when it starts outside the surface", () => {
    const root = mount('<button id="first">1</button><button id="second">2</button>');
    const outside = mount('<button id="outside">outside</button>').querySelector<HTMLElement>(
      "#outside",
    )!;
    outside.focus();
    const event = tabEvent();

    wrapTab(root, "button", event);

    expect(event.prevented).toBe(true);
    expect(document.activeElement).toBe(root.querySelector("#first"));
  });

  it("blocks Tab when the surface has nothing to focus", () => {
    const root = mount("<p>nothing focusable</p>");
    const event = tabEvent();

    wrapTab(root, "button", event);

    expect(event.prevented).toBe(true);
  });

  it("does nothing when a nested surface already claimed Tab", () => {
    const root = mount('<button id="first">1</button><button id="second">2</button>');
    const second = root.querySelector<HTMLElement>("#second")!;
    second.focus();
    const event = tabEvent({ defaultPrevented: true });

    wrapTab(root, "button", event);

    expect(event.prevented).toBe(false);
    expect(document.activeElement).toBe(second);
  });
});
