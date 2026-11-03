// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, createEvent, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React, { useState } from "react";

import FolderDrawer from "./FolderDrawer";
import { useZipImportPrompt, type ZipPromptConfig } from "./ZipImportModal";
import type { ZipEntry } from "../lib/zipUtils";

/**
 * The zip prompt is rendered un-portalled inside the folder rail, and the
 * drawer guards Escape with a `[role="dialog"]` query over its own descendants.
 * These tests therefore mount the real drawer around the real prompt: a stub
 * would not reproduce the capture-phase bail-out that used to close the sheet
 * while the prompt was still pending.
 */

const DEFAULT_CONFIG: ZipPromptConfig = {
  label: "pack.zip",
  onImportAsZip: async () => {},
  loadEntries: async () => [],
  onImportSelected: async () => {},
};

/**
 * The rail's own controls, so the drawer has somewhere to put focus that is
 * outside the prompt - the condition that made Escape miss the prompt's panel.
 */
function RailContents({ config }: { config: ZipPromptConfig }) {
  const zipPrompt = useZipImportPrompt();
  return (
    <>
      <div role="tree" aria-label="Folders">
        <div role="treeitem" tabIndex={0}>
          Art
        </div>
      </div>
      {/*
        Prompted from a click, never at mount: the drawer's open effect focuses
        the roving tree row after the child's effects, so a mount-time prompt
        would have its focus entry stolen and the assertions would prove nothing.
      */}
      <button onClick={() => void zipPrompt.prompt(config)}>drop zip</button>
      {zipPrompt.modal}
    </>
  );
}

function Harness({
  onDrawerClose,
  config,
}: {
  onDrawerClose: () => void;
  config?: Partial<ZipPromptConfig>;
}) {
  // The sheet really opens and closes, so "the sheet stayed open" is a claim
  // about the drawer rather than about a frozen prop.
  const [open, setOpen] = useState(true);
  const promptConfig: ZipPromptConfig = { ...DEFAULT_CONFIG, ...config };

  return (
    <FolderDrawer
      id="rail"
      mode="files"
      narrow
      open={open}
      onClose={() => {
        onDrawerClose();
        setOpen(false);
      }}
    >
      <RailContents config={promptConfig} />
    </FolderDrawer>
  );
}

function renderHarness(config?: Partial<ZipPromptConfig>): {
  onDrawerClose: ReturnType<typeof vi.fn>;
  rail: HTMLElement;
} {
  const onDrawerClose = vi.fn();
  render(<Harness onDrawerClose={onDrawerClose} config={config} />);
  return { onDrawerClose, rail: screen.getByRole("dialog", { name: "Library folders" }) };
}

function openPrompt(): HTMLElement {
  fireEvent.click(screen.getByRole("button", { name: "drop zip" }));
  return screen.getByRole("dialog", { name: "Import zip pack.zip" });
}

function promptDialog(): HTMLElement | null {
  return screen.queryByRole("dialog", { name: "Import zip pack.zip" });
}

/** The 8-entry list the server now sends, with `junk` already decided. */
const NASTY_ENTRIES: ZipEntry[] = [
  { path: "part.stl", size: 1, junk: false },
  { path: "a/b/nested.stl", size: 1, junk: false },
  { path: "a/b/deep/inner.stl", size: 1, junk: false },
  { path: "__MACOSX/._part.stl", size: 1, junk: true },
  { path: "__MACOSX/a/._nested.stl", size: 1, junk: true },
  { path: ".DS_Store", size: 1, junk: true },
  { path: "a/.DS_Store", size: 1, junk: true },
  { path: "a/b/Thumbs.db", size: 1, junk: true },
];

const PAYLOAD_PATHS = ["part.stl", "a/b/nested.stl", "a/b/deep/inner.stl"];
const JUNK_PATHS = [
  "__MACOSX/._part.stl",
  "__MACOSX/a/._nested.stl",
  ".DS_Store",
  "a/.DS_Store",
  "a/b/Thumbs.db",
];

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Rows are labels, so the checkbox's accessible name starts with its path. */
function checkbox(prompt: HTMLElement, path: string): HTMLInputElement {
  return within(prompt).getByRole("checkbox", {
    name: new RegExp(`^${escapeRegExp(path)}`),
  }) as HTMLInputElement;
}

function queryCheckbox(prompt: HTMLElement, path: string): HTMLInputElement | null {
  return within(prompt).queryByRole("checkbox", {
    name: new RegExp(`^${escapeRegExp(path)}`),
  }) as HTMLInputElement | null;
}

async function openSelectStage(
  entries: ZipEntry[],
  config?: Partial<ZipPromptConfig>
): Promise<HTMLElement> {
  renderHarness({ loadEntries: async () => entries, ...config });
  const prompt = openPrompt();
  fireEvent.click(within(prompt).getByRole("button", { name: "Import and unzip" }));
  await within(prompt).findByRole("button", { name: "Import selected" });
  return prompt;
}

afterEach(() => {
  cleanup();
});

describe("ZipImportModal inside the folder rail", () => {
  it("opens as an aria-modal dialog nested in the rail, where the drawer's guard can see it", () => {
    const { rail } = renderHarness();

    const prompt = openPrompt();

    expect(prompt.getAttribute("aria-modal")).toBe("true");
    expect(prompt.getAttribute("role")).toBe("dialog");
    expect(rail.contains(prompt)).toBe(true);
    expect(prompt).not.toBe(rail);
  });

  it("closes only the prompt on Escape, then lets the drawer answer the next Escape", async () => {
    // Reproduces the ticket: the drawer's guard is a descendant-dialog query on
    // a capture-phase document listener, so a prompt without `role="dialog"`
    // could not stop the sheet from closing under it.
    const { onDrawerClose, rail } = renderHarness();
    const prompt = openPrompt();

    fireEvent.keyDown(within(prompt).getByRole("button", { name: "Import as zip" }), {
      key: "Escape",
    });

    await waitFor(() => expect(promptDialog()).toBeNull());
    expect(rail.getAttribute("data-drawer-state")).toBe("open");
    expect(rail.hidden).toBe(false);
    expect(onDrawerClose).not.toHaveBeenCalled();

    // With the prompt gone the guard has nothing to bail on: the sheet's own
    // Escape works again.
    fireEvent.keyDown(screen.getByRole("button", { name: "drop zip" }), { key: "Escape" });

    expect(rail.getAttribute("data-drawer-state")).toBe("closed-narrow");
    expect(onDrawerClose).toHaveBeenCalledTimes(1);
  });

  it("closes the sheet on Escape when no prompt is open", () => {
    const { onDrawerClose, rail } = renderHarness();

    fireEvent.keyDown(screen.getByRole("treeitem"), { key: "Escape" });

    expect(rail.getAttribute("data-drawer-state")).toBe("closed-narrow");
    expect(onDrawerClose).toHaveBeenCalledTimes(1);
  });

  it("keeps Tab and Shift+Tab inside the prompt instead of reaching the rail", () => {
    renderHarness();
    const treeRow = screen.getByRole("treeitem");
    const prompt = openPrompt();
    const close = within(prompt).getByRole("button", { name: "Close" });
    const unzip = within(prompt).getByRole("button", { name: "Import and unzip" });

    unzip.focus();
    fireEvent.keyDown(unzip, { key: "Tab" });
    expect(document.activeElement).toBe(close);
    expect(prompt.contains(document.activeElement)).toBe(true);

    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(unzip);
    expect(prompt.contains(document.activeElement)).toBe(true);

    expect(document.activeElement).not.toBe(treeRow);
  });

  it("keeps an in-flight import and the sheet alive when Escape is pressed", () => {
    const { onDrawerClose, rail } = renderHarness({
      onImportAsZip: () => new Promise<void>(() => {}),
    });
    const prompt = openPrompt();

    fireEvent.click(within(prompt).getByRole("button", { name: "Import as zip" }));
    // Every control is disabled while busy, so the panel itself carries the key.
    const dispatched = fireEvent.keyDown(prompt, { key: "Escape" });

    expect(dispatched).toBe(false);
    expect(promptDialog()).toBe(prompt);
    expect(rail.getAttribute("data-drawer-state")).toBe("open");
    expect(onDrawerClose).not.toHaveBeenCalled();
  });

  it("moves focus into the prompt and recovers it across the stage change", async () => {
    renderHarness();
    const treeRow = screen.getByRole("treeitem");
    expect(document.activeElement).toBe(treeRow);

    const prompt = openPrompt();
    const close = within(prompt).getByRole("button", { name: "Close" });
    expect(document.activeElement).toBe(close);

    // `choice -> select` unmounts the button that holds focus, which drops focus
    // to <body>; the prompt has to re-enter itself or Escape goes dead.
    const unzip = within(prompt).getByRole("button", { name: "Import and unzip" });
    unzip.focus();
    fireEvent.click(unzip);

    await screen.findByText("0 of 0 selected");
    expect(document.activeElement).toBe(close);
    expect(prompt.contains(document.activeElement)).toBe(true);
  });

  it("returns focus to the control the prompt was opened from", async () => {
    renderHarness();
    const trigger = screen.getByRole("button", { name: "drop zip" });
    trigger.focus();

    fireEvent.click(trigger);
    const prompt = screen.getByRole("dialog", { name: "Import zip pack.zip" });
    fireEvent.keyDown(within(prompt).getByRole("button", { name: "Close" }), { key: "Escape" });

    await waitFor(() => expect(promptDialog()).toBeNull());
    expect(document.activeElement).toBe(trigger);
  });

  it("keeps Escape alive when the backdrop is what holds focus", async () => {
    const { onDrawerClose, rail } = renderHarness();
    const dialog = openPrompt();

    // A backdrop click lands focus on the dialog surface, never <body>, which
    // is the whole point: the prompt keeps Escape instead of going dead.
    dialog.focus();
    expect(document.activeElement).toBe(dialog);

    fireEvent.keyDown(dialog, { key: "Escape" });

    await waitFor(() => expect(promptDialog()).toBeNull());
    expect(rail.getAttribute("data-drawer-state")).toBe("open");
    expect(onDrawerClose).not.toHaveBeenCalled();
  });

  it("re-homes focus to the dialog surface while an import is in flight", async () => {
    const { onDrawerClose, rail } = renderHarness({
      onImportAsZip: () => new Promise<void>(() => {}),
    });
    const dialog = openPrompt();

    fireEvent.click(within(dialog).getByRole("button", { name: "Import as zip" }));

    // jsdom does not blur a node when its `disabled` attribute appears, so this
    // pins the busy recovery effect rather than a browser blur.
    await waitFor(() => expect(document.activeElement).toBe(dialog));
    expect(promptDialog()).toBe(dialog);
    expect(rail.getAttribute("data-drawer-state")).toBe("open");

    const event = createEvent.keyDown(dialog, { key: "Escape" });
    fireEvent(dialog, event);

    expect(event.defaultPrevented).toBe(true);
    expect(promptDialog()).toBe(dialog);
    expect(onDrawerClose).not.toHaveBeenCalled();
  });

  it("pre-ticks only the payload rows and hides OS metadata by default", async () => {
    const prompt = await openSelectStage(NASTY_ENTRIES);

    expect(within(prompt).getByText("3 of 8 selected")).toBeTruthy();
    for (const path of PAYLOAD_PATHS) {
      expect(checkbox(prompt, path).checked).toBe(true);
    }
    for (const path of JUNK_PATHS) {
      expect(queryCheckbox(prompt, path)).toBeNull();
    }
    expect(within(prompt).getByRole("button", { name: "Show OS metadata (5)" })).toBeTruthy();
  });

  it("leaves OS metadata unticked when Select all is used", async () => {
    const onImportSelected = vi.fn(async () => {});
    const prompt = await openSelectStage(NASTY_ENTRIES, { onImportSelected });

    // Clear first, so the shortcut itself is what re-selects the payload.
    fireEvent.click(within(prompt).getByRole("button", { name: "Clear" }));
    fireEvent.click(within(prompt).getByRole("button", { name: "Select all" }));

    for (const path of PAYLOAD_PATHS) {
      expect(checkbox(prompt, path).checked).toBe(true);
    }
    for (const path of JUNK_PATHS) {
      expect(queryCheckbox(prompt, path)).toBeNull();
    }

    fireEvent.click(within(prompt).getByRole("button", { name: "Import selected" }));
    await waitFor(() => expect(onImportSelected).toHaveBeenCalledTimes(1));
    expect(onImportSelected).toHaveBeenCalledWith(PAYLOAD_PATHS);
  });

  it("reveals OS metadata on request and lets an explicit tick reach the import", async () => {
    const onImportSelected = vi.fn(async () => {});
    const prompt = await openSelectStage(NASTY_ENTRIES, { onImportSelected });

    fireEvent.click(within(prompt).getByRole("button", { name: "Show OS metadata (5)" }));

    for (const path of JUNK_PATHS) {
      expect(checkbox(prompt, path).checked).toBe(false);
    }
    const junk = checkbox(prompt, "__MACOSX/._part.stl");
    fireEvent.click(junk);
    expect(junk.checked).toBe(true);

    fireEvent.click(within(prompt).getByRole("button", { name: "Import selected" }));
    await waitFor(() => expect(onImportSelected).toHaveBeenCalledTimes(1));
    expect(onImportSelected).toHaveBeenCalledWith([...PAYLOAD_PATHS, "__MACOSX/._part.stl"]);
  });

  it("explains a junk-only zip and never imports it", async () => {
    const onImportSelected = vi.fn(async () => {});
    const prompt = await openSelectStage(
      [
        { path: ".DS_Store", size: 1, junk: true },
        { path: "__MACOSX/._a.stl", size: 1, junk: true },
      ],
      { onImportSelected }
    );

    expect(within(prompt).getByText(/This zip holds only OS metadata/)).toBeTruthy();
    expect(within(prompt).getByText("0 of 2 selected")).toBeTruthy();

    const importButton = within(prompt).getByRole("button", {
      name: "Import selected",
    }) as HTMLButtonElement;
    expect(importButton.disabled).toBe(true);
    fireEvent.click(importButton);
    expect(onImportSelected).not.toHaveBeenCalled();
  });

  it("says a zip import never adds OS metadata, even when it is ticked", async () => {
    const prompt = await openSelectStage(NASTY_ENTRIES);

    expect(within(prompt).getByText(/ticking one cannot put it/)).toBeTruthy();
  });

  it("does not offer the OS metadata toggle for a plain zip", async () => {
    const prompt = await openSelectStage([
      { path: "part.stl", size: 1, junk: false },
      { path: "notes.txt", size: 1, junk: false },
    ]);

    expect(within(prompt).getByText("2 of 2 selected")).toBeTruthy();
    expect(within(prompt).queryByRole("button", { name: /OS metadata/ })).toBeNull();
  });
});
