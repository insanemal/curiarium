// @vitest-environment jsdom
import React, { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import ImportEntriesDialog, {
  type ExcludedEntry,
  type PickableEntry,
} from "./ImportEntriesDialog";

const ENTRIES: PickableEntry[] = [
  { path: "Hydra.stl", size: 2048, junk: false },
  { path: "notes/readme.txt", size: 12, junk: false },
  { path: "__MACOSX/._Hydra.stl", size: 300, junk: true },
];

const EXCLUDED: ExcludedEntry[] = [
  { path: "__MACOSX/._Hydra.stl", reason: "OS metadata: a macOS resource fork" },
];

type HarnessProps = {
  entries?: PickableEntry[];
  excluded?: ExcludedEntry[];
  selected?: string[];
  busy?: boolean;
  onConfirm?: (paths: string[]) => void;
  onClose?: () => void;
};

/** The dialog is mounted by a trigger, because focus has to come back to one. */
function Harness({
  entries = ENTRIES,
  excluded = EXCLUDED,
  selected = ["Hydra.stl", "notes/readme.txt"],
  busy = false,
  onConfirm,
  onClose,
}: HarnessProps) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        Open picker
      </button>
      {open && (
        <ImportEntriesDialog
          title="Choose files"
          label="Hydra.zip"
          entries={entries}
          excluded={excluded}
          selected={selected}
          busy={busy}
          onConfirm={paths => onConfirm?.(paths)}
          onClose={() => {
            onClose?.();
            setOpen(false);
          }}
        />
      )}
    </div>
  );
}

/** Opens the dialog the way a keyboard user would: the trigger holds focus. */
function openPicker(props: HarnessProps = {}) {
  render(<Harness {...props} />);
  const trigger = screen.getByRole("button", { name: "Open picker" });
  trigger.focus();
  fireEvent.click(trigger);
  return trigger;
}

function rows(): HTMLElement[] {
  return screen.getAllByRole("option");
}

/** A listing big enough that a per-row tab stop would be thousands of Tabs. */
function manyEntries(count: number): PickableEntry[] {
  return Array.from({ length: count }, (_, index) => ({
    path: `part-${index}.stl`,
    size: index,
    junk: false,
  }));
}

afterEach(cleanup);

describe("ImportEntriesDialog", () => {
  it("moves focus into the member list on mount", () => {
    // Catches a dialog that opens with focus on <body>: Escape and the arrow keys
    // would then never reach it.
    openPicker();

    expect(document.activeElement).toBe(rows()[0]);
    expect(rows()[0].getAttribute("tabindex")).toBe("0");
  });

  it("gives a 600-member listing exactly one tab stop", () => {
    // Catches one Tab per member: the roving tabindex is the whole point of the
    // listbox shape, and a per-row checkbox would be 600 stops.
    openPicker({ entries: manyEntries(600), excluded: [], selected: [] });

    const list = rows();
    expect(list).toHaveLength(600);
    expect(list.filter(row => row.getAttribute("tabindex") === "0")).toHaveLength(1);
    expect(list.filter(row => row.getAttribute("tabindex") === "-1")).toHaveLength(599);
  });

  it("moves the active row with the arrow keys and jumps with Home/End", () => {
    // Catches a listbox whose `tabindex="0"` never follows the row focus is on:
    // the tab stop would be stranded on a member the operator has left.
    openPicker();

    fireEvent.keyDown(rows()[0], { key: "ArrowDown" });
    expect(document.activeElement).toBe(rows()[1]);
    expect(rows()[1].getAttribute("tabindex")).toBe("0");
    expect(rows()[0].getAttribute("tabindex")).toBe("-1");

    fireEvent.keyDown(rows()[1], { key: "End" });
    expect(document.activeElement).toBe(rows()[2]);

    fireEvent.keyDown(rows()[2], { key: "Home" });
    expect(document.activeElement).toBe(rows()[0]);

    // The ends hold: one ArrowUp at the first row stays there.
    fireEvent.keyDown(rows()[0], { key: "ArrowUp" });
    expect(document.activeElement).toBe(rows()[0]);
  });

  it("traps Tab, wrapping the last control to the first and back", () => {
    // Catches a dialog a real browser can Tab out of through an aria-modal sheet,
    // and a roving row (deliberately outside the ring's selector) that hands Tab
    // back to the page instead of the list's container.
    openPicker({ selected: [] });

    const listbox = screen.getByRole("listbox");
    const first = screen.getByRole("button", { name: "Select all" });
    const last = screen.getByRole("button", { name: "Cancel" });

    last.focus();
    fireEvent.keyDown(last, { key: "Tab" });
    expect(document.activeElement).toBe(first);

    fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);

    // Shift+Tab from the focused row stays inside the dialog: it lands on the
    // ring member a browser would have reached (the listbox itself).
    rows()[0].focus();
    fireEvent.keyDown(rows()[0], { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(listbox);
  });

  it("toggles the active row with Space and announces the count", () => {
    // Catches a keyboard path that can move but not tick, and a count nobody
    // hears because it is not a live region.
    openPicker();

    expect(rows()[0].getAttribute("aria-checked")).toBe("true");
    fireEvent.keyDown(rows()[0], { key: " " });
    expect(rows()[0].getAttribute("aria-checked")).toBe("false");
    expect(screen.getByText("1 of 3 selected")).toBeTruthy();

    fireEvent.keyDown(rows()[0], { key: " " });
    expect(rows()[0].getAttribute("aria-checked")).toBe("true");
    expect(screen.getByText("2 of 3 selected")).toBeTruthy();
  });

  it("shows an excluded member disabled with the server's reason, and never ticks it", () => {
    // Catches a picker that hides what the plan leaves out, paraphrases the
    // server's reason, drops the byte size, or lets "Select all" sweep a refused
    // member into the plan.
    openPicker({ selected: [] });

    const junk = rows()[2];
    expect(junk.getAttribute("aria-disabled")).toBe("true");
    expect(junk.textContent).toContain("OS metadata: a macOS resource fork");
    expect(junk.textContent).toContain("300 B");

    fireEvent.click(junk);
    expect(junk.getAttribute("aria-checked")).toBe("false");

    fireEvent.click(screen.getByRole("button", { name: "Select all" }));
    expect(junk.getAttribute("aria-checked")).toBe("false");
    expect(rows()[0].getAttribute("aria-checked")).toBe("true");
    expect(rows()[1].getAttribute("aria-checked")).toBe("true");
  });

  it("falls back to the entry's own junk verdict when the source names no reason", () => {
    // Catches a junk member becoming tickable because a URL zip's listing carries
    // verdicts rather than sentences.
    openPicker({ excluded: [], selected: [] });

    expect(rows()[2].getAttribute("aria-disabled")).toBe("true");
    expect(rows()[2].textContent).toContain("the server leaves this member out");
    fireEvent.click(screen.getByRole("button", { name: "Select all" }));
    expect(rows()[2].getAttribute("aria-checked")).toBe("false");
  });

  it("closes on Escape and hands focus back to the trigger", () => {
    // Catches a dialog that strands the operator's caret on <body>.
    const onClose = vi.fn();
    const trigger = openPicker({ onClose });

    fireEvent.keyDown(screen.getByRole("listbox"), { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("does nothing on Escape while a request is in flight", () => {
    // Catches an abandoned request: close while busy would drop an upload's
    // answer on the floor.
    const onClose = vi.fn();
    openPicker({ busy: true, onClose });

    fireEvent.keyDown(rows()[0], { key: "Escape" });

    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("reports exactly the ticked paths through onConfirm", () => {
    // Catches a confirm that sends the whole listing, or one that keeps a row
    // the operator unticked.
    const onConfirm = vi.fn();
    openPicker({ onConfirm });

    fireEvent.click(rows()[1]);
    fireEvent.click(screen.getByRole("button", { name: "Use these files" }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onConfirm).toHaveBeenCalledWith(["Hydra.stl"]);
  });
});
