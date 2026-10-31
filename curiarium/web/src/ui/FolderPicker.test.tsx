// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

import type { Folder } from "../lib/api";
import { descendantIds } from "../lib/folderTree";
import FolderPicker, { type FolderPickerProps } from "./FolderPicker";

// Keys are relative to this test file, e.g. `"./Sidebar.tsx"`.
const RAW_SOURCES = import.meta.glob("./**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** This test lives in `src/ui`, and glob keys are relative to it. */
const SELF_DIR = "ui";
/** This test's own path, relative to `src`. */
const SELF = `${SELF_DIR}/FolderPicker.test.tsx`;

/** Resolve a glob key to a `src`-relative path without pulling in `node:path`. */
function toSourcePath(globKey: string): string {
  const resolved: string[] = [];
  for (const segment of `${SELF_DIR}/${globKey}`.split("/")) {
    if (segment === "..") resolved.pop();
    else if (segment && segment !== ".") resolved.push(segment);
  }
  return resolved.join("/");
}

/** Every `.ts`/`.tsx` source under `src/ui` except this test, by `src`-relative path. */
const SOURCES: Record<string, string> = {};
for (const [globKey, text] of Object.entries(RAW_SOURCES)) {
  const file = toSourcePath(globKey);
  if (file !== SELF) SOURCES[file] = text;
}

function folder(id: string, name: string, parentId: string | null = null): Folder {
  return { id, name, tags: [], parent_id: parentId };
}

/** Top-level order is AAA, Cut, Prints; Prints has Resin, FDM < Resin, FDM > PETG. */
const BASIC: Folder[] = [
  folder("aaa", "AAA"),
  folder("prints", "Prints"),
  folder("resin", "Resin", "prints"),
  folder("fdm", "FDM", "prints"),
  folder("petg", "PETG", "fdm"),
  folder("cut", "Cut"),
];

/** Two sibling paths containing "b", so a repeated key must cycle, not accumulate. */
const REPEAT: Folder[] = [folder("b1", "Blue"), folder("b2", "Brick"), folder("n1", "Pearl")];

const CHAIN: Folder[] = [
  folder("l1", "Level 1"),
  folder("l2", "Level 2", "l1"),
  folder("l3", "Level 3", "l2"),
  folder("l4", "Level 4", "l3"),
  folder("l5", "Level 5", "l4"),
];

/** Prints is the last top-level row; the deep leaf is under Workshop, not Prints. */
const TYPEAHEAD: Folder[] = [
  folder("t1", "Workshop"),
  folder("t2", "Resin", "t1"),
  folder("t3", "Tough", "t2"),
  folder("t4", "Black Matte", "t3"),
  folder("t5", "Prints"),
];

const SEARCHABLE: Folder[] = [...BASIC, folder("cast", "Cast Resin")];

const EXCLUDED: Folder[] = [
  folder("prints", "Prints"),
  folder("fdm", "FDM", "prints"),
  folder("petg", "PETG", "fdm"),
  folder("cut", "Cut"),
];

const BULK: Folder[] = Array.from({ length: 60 }, (_, index) =>
  folder(`bulk-${index}`, `Resin Batch ${String(index + 1).padStart(2, "0")}`),
);

const RANKING: Folder[] = [
  folder("z1", "Black PETG"),
  folder("p2", "PETG Zeta"),
  folder("p1", "PETG Alpha"),
  folder("x1", "Matte PETG"),
  ...BULK,
];

const FILLER: Folder[] = Array.from({ length: 400 }, (_, index) =>
  folder(`filler-${index}`, `Part ${String(index + 1).padStart(3, "0")}`),
);

/**
 * 404 folders, six levels deep. The chain root ("Zinc") sorts AFTER "Black PLA",
 * so tree position and search position agree on nothing: only the leaf's own
 * name fragment finds it.
 */
const DEEP: Folder[] = [
  folder("pla", "Black PLA"),
  folder("chain-1", "Zinc"),
  folder("chain-2", "Tough", "chain-1"),
  folder("chain-3", "Matte", "chain-2"),
  folder("chain-4", "Deep", "chain-3"),
  folder("chain-5", "Deeper", "chain-4"),
  folder("chain-6", "Black Matte PETG", "chain-5"),
  ...FILLER,
];

type ScrollPrototype = { scrollIntoView?: (options?: ScrollIntoViewOptions) => void };

let scrollSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  scrollSpy = vi.fn();
  // jsdom has no scrollIntoView; the component's `?.` is the whole shiv.
  (Element.prototype as ScrollPrototype).scrollIntoView = scrollSpy;
});

afterEach(() => {
  delete (Element.prototype as ScrollPrototype).scrollIntoView;
  cleanup();
});

function setup(props: Partial<FolderPickerProps> = {}) {
  const onChange = vi.fn();
  const view = render(
    <FolderPicker id="picker" label="Location" folders={BASIC} value={null} onChange={onChange} {...props} />,
  );
  return { onChange, ...view };
}

function trigger(): HTMLButtonElement {
  return screen.getByRole("button", { name: /^Location:/ }) as HTMLButtonElement;
}

function searchInput(): HTMLInputElement {
  return screen.getByLabelText("Search folders in Location") as HTMLInputElement;
}

function row(folderId: string): HTMLElement {
  const element = document.querySelector<HTMLElement>(`[data-picker-row="${folderId}"]`);
  if (!element) throw new Error(`row ${folderId} is not rendered`);
  return element;
}

function optionElements(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>("[data-picker-option]"));
}

function optionIds(): (string | null)[] {
  return optionElements().map(element => element.getAttribute("data-picker-option"));
}

function pressKey(key: string, init: { shiftKey?: boolean; on?: HTMLElement } = {}): void {
  const target = init.on ?? document.activeElement;
  if (!(target instanceof HTMLElement)) throw new Error(`nothing is focused to receive ${key}`);
  fireEvent.keyDown(target, { key, shiftKey: init.shiftKey ?? false });
}

function openWithPointer(): void {
  fireEvent.click(trigger());
}

describe("FolderPicker", () => {
  it("renders a plain dialog-trigger button, never a button pretending to be a combobox", () => {
    setup();

    const button = screen.getByRole("button", { name: "Location: Root (top level)" });
    expect(button.tagName).toBe("BUTTON");
    expect(button.getAttribute("aria-haspopup")).toBe("dialog");
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(button.getAttribute("role")).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows the current folder's leaf name, honours rootLabel, and disabled blocks opening", () => {
    setup({ value: "petg" });
    expect(screen.getByRole("button", { name: "Location: PETG" })).toBeTruthy();
    cleanup();

    setup({ rootLabel: "Top level" });
    expect(screen.getByRole("button", { name: "Location: Top level" })).toBeTruthy();
    cleanup();

    const { onChange } = setup({ disabled: true });
    const button = screen.getByRole("button", { name: "Location: Root (top level)" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    fireEvent.keyDown(button, { key: "ArrowDown" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("opens a dialog that contains the tree named after the field label", () => {
    setup();
    openWithPointer();

    const dialog = screen.getByRole("dialog", { name: "Location" });
    expect(within(dialog).getByRole("tree", { name: "Location" })).toBeTruthy();
    expect(trigger().getAttribute("aria-expanded")).toBe("true");
  });

  it("expands and scrolls to the current parent on open", () => {
    setup({ folders: CHAIN, value: "l5" });
    openWithPointer();

    for (const ancestor of ["l1", "l2", "l3", "l4"]) {
      expect(row(ancestor).getAttribute("aria-expanded")).toBe("true");
    }
    expect(row("l5").tabIndex).toBe(0);
    expect(document.activeElement).toBe(row("l5"));
    expect(scrollSpy).toHaveBeenCalledTimes(1);
    expect(scrollSpy).toHaveBeenCalledWith({ block: "center" });
  });

  it("falls back to the Root row when the value is not in folders", () => {
    setup({ value: "ghost" });
    expect(screen.getByRole("button", { name: "Location: Root (top level)" })).toBeTruthy();

    fireEvent.keyDown(trigger(), { key: "ArrowDown" });
    expect(screen.getByRole("tree", { name: "Location" })).toBeTruthy();
    expect(document.activeElement).toBe(row("__root__"));
  });

  it("operates entirely by keyboard", () => {
    const { onChange } = setup();
    fireEvent.keyDown(trigger(), { key: "ArrowDown" });
    expect(document.activeElement).toBe(row("__root__"));

    pressKey("ArrowUp");
    expect(document.activeElement).toBe(row("__root__"));

    pressKey("ArrowDown");
    expect(document.activeElement).toBe(row("aaa"));
    pressKey("ArrowDown");
    expect(document.activeElement).toBe(row("cut"));
    pressKey("ArrowDown");
    expect(document.activeElement).toBe(row("prints"));

    pressKey("ArrowRight");
    expect(row("prints").getAttribute("aria-expanded")).toBe("true");
    expect(document.activeElement).toBe(row("prints"));

    pressKey("ArrowRight");
    expect(document.activeElement).toBe(row("fdm"));

    pressKey("ArrowLeft");
    expect(document.activeElement).toBe(row("prints"));

    pressKey("ArrowLeft");
    expect(row("prints").getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(row("prints"));

    pressKey("Home");
    expect(document.activeElement).toBe(row("__root__"));

    pressKey("End");
    expect(document.activeElement).toBe(row("prints"));

    pressKey(" ");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("prints");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger());

    fireEvent.keyDown(trigger(), { key: "Enter" });
    expect(screen.getByRole("dialog", { name: "Location" })).toBeTruthy();
    pressKey("Escape");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("typeahead walks to a deep row by path fragment without filtering the tree", () => {
    setup({ folders: TYPEAHEAD, value: "t4" });
    openWithPointer();
    expect(document.activeElement).toBe(row("t4"));

    pressKey("Home");
    expect(document.activeElement).toBe(row("__root__"));

    pressKey("b");
    pressKey("l");
    pressKey("a");

    expect(document.activeElement).toBe(row("t4"));
    expect(document.querySelectorAll("[data-picker-row]").length).toBe(6);
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.getByRole("tree", { name: "Location" })).toBeTruthy();
  });

  it("keeps the Root row first at level 1 and commits null from it", () => {
    const { onChange } = setup();
    openWithPointer();

    const tree = screen.getByRole("tree", { name: "Location" });
    const first = tree.firstElementChild as HTMLElement;
    expect(first.getAttribute("data-picker-row")).toBe("__root__");
    expect(first.getAttribute("aria-level")).toBe("1");
    expect(first.getAttribute("aria-selected")).toBe("true");
    // A folder named "AAA" sorts first but still sits after the Root row.
    expect(tree.children[1]?.getAttribute("data-picker-row")).toBe("aaa");
    expect(row("aaa").getAttribute("aria-level")).toBe("2");

    pressKey("Home");
    expect(document.activeElement).toBe(first);
    pressKey("Enter");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(null);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("expands a branch from the chevron without committing", () => {
    const { onChange } = setup();
    openWithPointer();
    expect(document.activeElement).toBe(row("__root__"));
    expect(row("__root__").className).toBe("folder-picker-row is-current is-active is-root");
    expect(row("prints").className).toBe("folder-picker-row");

    const expander = within(row("prints")).getByRole("button", { name: "Expand" });
    expect(expander.tabIndex).toBe(-1);
    fireEvent.click(expander);

    expect(onChange).not.toHaveBeenCalled();
    expect(row("prints").getAttribute("aria-expanded")).toBe("true");
    expect(row("resin")).toBeTruthy();
    // The chevron toggles expansion only: no commit, no move of the active row.
    expect(document.activeElement).toBe(row("__root__"));
    expect(row("__root__").tabIndex).toBe(0);
    expect(within(row("prints")).getByRole("button", { name: "Collapse" })).toBe(expander);

    fireEvent.click(expander);
    expect(row("prints").getAttribute("aria-expanded")).toBe("false");
    expect(document.querySelector('[data-picker-row="resin"]')).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("moves the cursor out of a subtree it collapses", () => {
    const { onChange } = setup();
    openWithPointer();

    pressKey("End");
    pressKey("ArrowRight");
    pressKey("ArrowRight");
    expect(document.activeElement).toBe(row("fdm"));

    // Collapsing from an ancestor chevron unmounts the focused row, so the
    // cursor has to move up or focus falls to <body> and every later keydown
    // misses the panel entirely.
    fireEvent.click(within(row("prints")).getByRole("button", { name: "Collapse" }));

    expect(document.querySelector('[data-picker-row="fdm"]')).toBeNull();
    expect(document.activeElement).toBe(row("prints"));
    expect(row("prints").tabIndex).toBe(0);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("recovers the roving tabstop when a refresh drops the active folder", () => {
    const view = render(
      <FolderPicker id="picker" label="Location" folders={BASIC} value="aaa" onChange={vi.fn()} />,
    );
    fireEvent.click(trigger());
    expect(document.activeElement).toBe(row("aaa"));

    view.rerender(
      <FolderPicker
        id="picker"
        label="Location"
        folders={BASIC.filter(item => item.id !== "aaa")}
        value="aaa"
        onChange={vi.fn()}
      />,
    );

    expect(document.querySelector('[data-picker-row="aaa"]')).toBeNull();
    expect(row("__root__").tabIndex).toBe(0);
    expect(document.activeElement).toBe(row("__root__"));
  });

  it("traps Tab inside the dialog panel", () => {
    setup();
    openWithPointer();

    const cancel = screen.getByRole("button", { name: "Cancel" });
    cancel.focus();
    expect(document.activeElement).toBe(cancel);

    fireEvent.keyDown(cancel, { key: "Tab" });
    expect(document.activeElement).toBe(searchInput());

    fireEvent.keyDown(searchInput(), { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(cancel);

    const panel = screen.getByRole("dialog", { name: "Location" });
    expect(panel.contains(document.activeElement)).toBe(true);
  });

  it("keeps row chevrons out of the Tab cycle, even with several expandable rows", () => {
    setup({ folders: CHAIN, value: "l5" });
    openWithPointer();

    const panel = screen.getByRole("dialog", { name: "Location" });
    // Four expandable rows are on screen, each carrying a tabIndex={-1} chevron.
    expect(panel.querySelectorAll('[data-picker-row][aria-expanded] button').length).toBe(4);

    // What a browser would tab through: input, the active row, then Cancel.
    const cycle = Array.from(panel.querySelectorAll<HTMLElement>("input, button, [data-picker-row]")).filter(
      element => element.tabIndex >= 0,
    );
    expect(cycle.length).toBe(3);
    expect(cycle[0]).toBe(searchInput());
    expect(cycle[1]).toBe(row("l5"));
    expect(cycle[2]).toBe(screen.getByRole("button", { name: "Cancel" }));

    const input = searchInput();
    const cancel = screen.getByRole("button", { name: "Cancel" });
    input.focus();
    fireEvent.keyDown(input, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(cancel);

    fireEvent.keyDown(cancel, { key: "Tab" });
    expect(document.activeElement).toBe(input);
  });

  it("switches to a listbox while search keeps the focus in the input", () => {
    const { onChange } = setup({ folders: SEARCHABLE });
    openWithPointer();

    const input = searchInput();
    input.focus();
    fireEvent.change(input, { target: { value: "res" } });

    expect(screen.queryByRole("tree")).toBeNull();
    const listbox = screen.getByRole("listbox", { name: "Location" });
    expect(input.getAttribute("role")).toBe("combobox");
    expect(input.getAttribute("aria-autocomplete")).toBe("list");
    expect(input.getAttribute("aria-expanded")).toBe("true");
    expect(input.getAttribute("aria-controls")).toBe("picker-options");
    expect(document.activeElement).toBe(input);

    expect(optionIds()).toEqual(["resin", "cast"]);
    const first = optionElements()[0];
    // Nothing is committed yet (`value` is null), so only the highlight moves.
    expect(first.getAttribute("aria-selected")).toBe("false");
    expect(first.getAttribute("data-active")).toBe("true");
    expect(input.getAttribute("aria-activedescendant")).toBe("picker-option-resin");
    // The leaf is the accessible name; the trail is a description.
    expect(within(listbox).getByRole("option", { name: "Resin" })).toBe(first);
    expect(first.getAttribute("aria-describedby")).toBe("picker-option-path-resin");
    expect(document.getElementById("picker-option-path-resin")?.textContent).toBe("Prints / Resin");
    expect(screen.getByRole("status").textContent).toBe("2 matches");

    pressKey("ArrowDown", { on: input });
    expect(input.getAttribute("aria-activedescendant")).toBe("picker-option-cast");
    expect(first.getAttribute("data-active")).toBeNull();
    expect(optionElements()[1].getAttribute("data-active")).toBe("true");

    pressKey("Enter", { on: input });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("cast");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("lets the empty search field take its own keystrokes instead of the tree typeahead", () => {
    const { onChange } = setup();
    openWithPointer();

    const input = searchInput();
    input.focus();

    // fireEvent returns false when the handler called preventDefault, i.e. when
    // the keystroke was swallowed before the field could insert it.
    expect(fireEvent.keyDown(input, { key: "r" })).toBe(true);
    expect(fireEvent.keyDown(input, { key: "e" })).toBe(true);
    expect(fireEvent.keyDown(input, { key: " " })).toBe(true);
    expect(document.activeElement).toBe(input);

    fireEvent.change(input, { target: { value: "res" } });
    expect(screen.getByRole("listbox", { name: "Location" })).toBeTruthy();
    expect(document.activeElement).toBe(input);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("does not commit a row when Enter is pressed in the search field", () => {
    const { onChange } = setup();
    openWithPointer();

    const input = searchInput();
    input.focus();
    fireEvent.keyDown(input, { key: "Enter" });

    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Location" })).toBeTruthy();
    expect(screen.getByRole("tree", { name: "Location" })).toBeTruthy();
  });

  it("lets the Cancel button keep its own Enter and Space", () => {
    const { onChange } = setup();
    openWithPointer();

    const cancel = screen.getByRole("button", { name: "Cancel" });
    cancel.focus();

    // Tab lands on Cancel, so Enter there must cancel - not commit the row the
    // tree happens to be highlighting.
    expect(fireEvent.keyDown(cancel, { key: "Enter" })).toBe(true);
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Location" })).toBeTruthy();

    expect(fireEvent.keyDown(cancel, { key: " " })).toBe(true);
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Location" })).toBeTruthy();
  });

  it("does not re-focus the anchor row when folders refresh while browsing", () => {
    const view = render(
      <FolderPicker id="picker" label="Location" folders={BASIC} value="aaa" onChange={vi.fn()} />,
    );
    fireEvent.click(trigger());
    expect(document.activeElement).toBe(row("aaa"));
    expect(scrollSpy).toHaveBeenCalledTimes(1);

    // The user arrows off the anchor row, then `aaa` disappears, so the anchor
    // recomputes to the Root row. The tree stays mounted throughout, which is
    // what makes this a real guard: re-focusing the anchor would yank the caret.
    pressKey("ArrowDown");
    expect(document.activeElement).toBe(row("cut"));

    view.rerender(
      <FolderPicker
        id="picker"
        label="Location"
        folders={BASIC.filter(item => item.id !== "aaa")}
        value="aaa"
        onChange={vi.fn()}
      />,
    );

    // The anchor is decided once, on open: the caret stays where the user put it.
    expect(document.activeElement).toBe(row("cut"));
    expect(row("cut").tabIndex).toBe(0);
    expect(scrollSpy).toHaveBeenCalledTimes(1);
  });

  it("clears the query before Escape closes the dialog", () => {
    setup({ folders: SEARCHABLE });
    openWithPointer();

    const input = searchInput();
    input.focus();
    fireEvent.change(input, { target: { value: "res" } });
    expect(screen.getByRole("listbox", { name: "Location" })).toBeTruthy();

    // First Escape drops back to browsing, second Escape closes.
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.getByRole("dialog", { name: "Location" })).toBeTruthy();
    expect(screen.getByRole("tree", { name: "Location" })).toBeTruthy();
    expect(input.value).toBe("");

    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Location" })).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("marks the committed value as selected, separately from the highlight", () => {
    setup({ folders: SEARCHABLE, value: "resin" });
    openWithPointer();

    const input = searchInput();
    input.focus();
    fireEvent.change(input, { target: { value: "resin" } });
    expect(optionIds()).toEqual(["resin", "cast"]);

    const [resin, cast] = optionElements();
    expect(resin.getAttribute("aria-selected")).toBe("true");
    expect(resin.getAttribute("data-active")).toBe("true");
    expect(cast.getAttribute("aria-selected")).toBe("false");

    pressKey("ArrowDown", { on: input });
    expect(input.getAttribute("aria-activedescendant")).toBe("picker-option-cast");
    // The highlight moved off the committed value; the selection stayed put.
    expect(resin.getAttribute("aria-selected")).toBe("true");
    expect(resin.getAttribute("data-active")).toBeNull();
    expect(cast.getAttribute("aria-selected")).toBe("false");
    expect(cast.getAttribute("data-active")).toBe("true");
  });

  it("ranks prefix matches first, ties by path, and caps the list", () => {
    setup({ folders: RANKING });
    openWithPointer();

    const input = searchInput();
    input.focus();
    fireEvent.change(input, { target: { value: "petg" } });
    expect(optionIds()).toEqual(["p1", "p2", "z1", "x1"]);
    expect(screen.getByRole("status").textContent).toBe("4 matches");

    fireEvent.change(input, { target: { value: "resin" } });
    expect(optionElements().length).toBe(50);
    expect(screen.getByRole("status").textContent).toBe("Showing 50 of 60 — keep typing to narrow");
  });

  it("reaches a six-deep folder with a name fragment and two keystrokes", () => {
    const { onChange } = setup({ folders: DEEP });
    openWithPointer();

    const input = searchInput();
    input.focus();
    fireEvent.change(input, { target: { value: "blac" } });

    expect(optionIds()).toEqual(["pla", "chain-6"]);
    expect(optionElements().length).toBe(2);
    expect(optionElements().length).toBeLessThanOrEqual(50);

    pressKey("ArrowDown", { on: input });
    pressKey("Enter", { on: input });

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("chain-6");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("cycles typeahead matches when the same character is pressed twice", () => {
    const { onChange } = setup({ folders: REPEAT });
    openWithPointer();
    expect(document.activeElement).toBe(row("__root__"));

    pressKey("b");
    expect(document.activeElement).toBe(row("b1"));

    // A second "b" restarts the buffer, so it walks to the next b-match instead
    // of accumulating "bb" (which matches nothing and would leave it on b1).
    pressKey("b");
    expect(document.activeElement).toBe(row("b2"));

    pressKey("b");
    expect(document.activeElement).toBe(row("b1"));

    expect(onChange).not.toHaveBeenCalled();
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("keeps the browse expansion set when a search is cleared", () => {
    setup();
    openWithPointer();

    pressKey("End");
    expect(document.activeElement).toBe(row("prints"));
    pressKey("ArrowRight");
    expect(row("prints").getAttribute("aria-expanded")).toBe("true");
    expect(row("fdm")).toBeTruthy();

    const input = searchInput();
    input.focus();
    fireEvent.change(input, { target: { value: "res" } });
    expect(screen.queryByRole("tree")).toBeNull();

    fireEvent.change(input, { target: { value: "" } });
    expect(screen.getByRole("tree", { name: "Location" })).toBeTruthy();
    expect(row("prints").getAttribute("aria-expanded")).toBe("true");
    expect(row("fdm")).toBeTruthy();
  });

  it("announces and refuses excluded rows while keeping them reachable", () => {
    const excludeIds = new Set(["prints", ...descendantIds(EXCLUDED, "prints")]);
    const { onChange } = setup({ folders: EXCLUDED, excludeIds, selfId: "prints" });
    openWithPointer();

    expect(row("prints").getAttribute("aria-disabled")).toBe("true");
    expect(row("prints").getAttribute("aria-describedby")).toBe("picker-reason-prints");
    expect(document.getElementById("picker-reason-prints")?.textContent).toBe(
      "Can't move a folder into itself",
    );

    pressKey("ArrowDown");
    expect(document.activeElement).toBe(row("cut"));
    pressKey("ArrowDown");
    expect(document.activeElement).toBe(row("prints"));

    fireEvent.click(row("prints"));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Location" })).toBeTruthy();

    pressKey("ArrowRight");
    expect(row("fdm").getAttribute("aria-disabled")).toBe("true");
    expect(row("fdm").getAttribute("aria-describedby")).toBe("picker-reason-fdm");
    expect(document.getElementById("picker-reason-fdm")?.textContent).toBe(
      "Inside the folder being moved",
    );

    pressKey("ArrowRight");
    expect(document.activeElement).toBe(row("fdm"));
    pressKey("ArrowRight");
    pressKey("ArrowRight");
    expect(document.activeElement).toBe(row("petg"));
    expect(row("petg").getAttribute("aria-disabled")).toBe("true");

    pressKey("Enter");
    expect(onChange).not.toHaveBeenCalled();
    pressKey(" ");
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(row("petg"));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Location" })).toBeTruthy();

    const input = searchInput();
    input.focus();
    fireEvent.change(input, { target: { value: "p" } });
    expect(optionIds()).toEqual(["prints", "petg"]);
    expect(optionElements()[0].getAttribute("aria-disabled")).toBe("true");
    expect(input.hasAttribute("aria-activedescendant")).toBe(false);

    pressKey("ArrowDown", { on: input });
    expect(input.hasAttribute("aria-activedescendant")).toBe(false);
    pressKey("Enter", { on: input });
    expect(onChange).not.toHaveBeenCalled();
  });

  it("calls onChange only on commit, never while navigating", () => {
    const { onChange } = setup();
    openWithPointer();

    pressKey("ArrowDown");
    pressKey("ArrowDown");
    pressKey("ArrowLeft");
    pressKey("ArrowRight");
    pressKey("ArrowRight");
    pressKey("Home");
    pressKey("End");
    pressKey("p");
    pressKey("r");
    expect(onChange).not.toHaveBeenCalled();

    const input = searchInput();
    input.focus();
    fireEvent.change(input, { target: { value: "res" } });
    pressKey("ArrowDown", { on: input });
    fireEvent.change(input, { target: { value: "resin" } });
    pressKey("ArrowUp", { on: input });
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Location" })).toBeTruthy();
  });

  it("routes every flat folder <select> through FolderPicker", () => {
    const files = Object.keys(SOURCES).sort();

    // Fails loudly rather than silently passing on an empty glob.
    expect(files.length).toBeGreaterThan(30);
    expect(files).toContain("ui/Sidebar.tsx");
    expect(files).toContain("ui/BulkActionBar.tsx");
    expect(files).toContain("ui/asset-grid/AssetDetailsEditor.tsx");

    const callSites = ["ui/Sidebar.tsx", "ui/BulkActionBar.tsx"];
    const flatSelects = callSites.filter(file => /<select/.test(SOURCES[file]));
    expect(flatSelects).toEqual([]);
    const missingImport = callSites.filter(file => !/import[^;]*FolderPicker/.test(SOURCES[file]));
    expect(missingImport).toEqual([]);

    // Documented exemption: the asset editor keeps its select until Mf-el5v
    // (outside this ticket's touch-set, which is why only the select is pinned).
    const deferred = SOURCES["ui/asset-grid/AssetDetailsEditor.tsx"];
    expect(deferred).toContain("<select");
  });
});
