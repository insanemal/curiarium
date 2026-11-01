// @vitest-environment jsdom
import { type ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const modelSnapshotProps = vi.hoisted(() => [] as Array<Record<string, unknown>>);

vi.mock("../ModelViewer", () => ({
  default: () => null,
  ModelSnapshot: (props: Record<string, unknown>) => {
    modelSnapshotProps.push(props);
    return null;
  },
}));

const lightBurnProps = vi.hoisted(() => [] as Array<Record<string, unknown>>);

vi.mock("../LightBurnPreview", () => ({
  default: (props: Record<string, unknown>) => {
    lightBurnProps.push(props);
    return null;
  },
}));

// The preview footer mounts `ArchiveExtractAction`, which imports the api barrel.
// Only the two archive calls are replaced; the rest of the barrel (`fileUrl`,
// the grant transport) has to stay real for the modal to render at all.
vi.mock("../../lib/api", async importOriginal => ({
  ...(await importOriginal<typeof import("../../lib/api")>()),
  listArchiveEntries: vi.fn(),
  extractArchive: vi.fn(),
}));

import type { Asset, Folder, PreparedPrint } from "../../lib/api";
import { AssetPreviewModal, PreparedPrintSummary, renderPreviewContent } from "./preview";

afterEach(cleanup);

beforeEach(() => {
  lightBurnProps.length = 0;
  modelSnapshotProps.length = 0;
});

/** The summary line sits on the outer div; the Clear button is a sibling. */
function summaryLine(prepared: PreparedPrint) {
  const { container } = render(<PreparedPrintSummary prepared={prepared} />);
  return (container.firstElementChild as HTMLElement).textContent;
}

describe("PreparedPrintSummary", () => {
  it("renders the prepared title and all four facts in a fixed order", () => {
    expect(
      summaryLine({
        printer: "Prusa MK3S+",
        slicer: "PrusaSlicer",
        material: "PLA",
        nozzle_mm: 0.4,
        layer_height_mm: 0.2,
        estimated_seconds: 11520,
        format: "bgcode",
        removable: true,
      }),
    ).toBe("Prepared for Prusa MK3S+ · PrusaSlicer · PLA · 0.4 mm · 3h 12m");

    // The title is its own span so callers can match it without the facts.
    expect(screen.getByText("Prepared for Prusa MK3S+")).toBeTruthy();
  });

  it("renders the all-unknown line for an unprepared payload", () => {
    expect(
      summaryLine({
        printer: null,
        slicer: "unknown",
        material: null,
        nozzle_mm: null,
        estimated_seconds: null,
      }),
    ).toBe("Prepared print · Unknown slicer · Unknown material · Unknown nozzle · Unknown time");

    expect(screen.getByText("Prepared print")).toBeTruthy();
  });

  it.each([undefined, null, "unknown", "  "])(
    "treats slicer %p as Unknown slicer",
    (slicer) => {
      expect(summaryLine({ slicer })).toBe(
        "Prepared print · Unknown slicer · Unknown material · Unknown nozzle · Unknown time",
      );
      cleanup();
    },
  );

  it("mixes known and unknown slots without dropping any", () => {
    expect(
      summaryLine({
        printer: "",
        slicer: "OrcaSlicer",
        material: "PETG",
        nozzle_mm: 0.6,
        estimated_seconds: 900,
      }),
    ).toBe("Prepared print · OrcaSlicer · PETG · 0.6 mm · 15m");
  });

  it("renders the Clear button only when handled and disables it while clearing", () => {
    const onClear = vi.fn();
    const prepared = { printer: "Voron" };
    const { rerender } = render(
      <PreparedPrintSummary prepared={prepared} onClear={onClear} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(onClear).toHaveBeenCalledTimes(1);

    rerender(<PreparedPrintSummary prepared={prepared} onClear={onClear} clearing />);
    const clearingButton = screen.getByRole("button", { name: "Clearing..." });
    expect((clearingButton as HTMLButtonElement).disabled).toBe(true);

    cleanup();
    render(<PreparedPrintSummary prepared={prepared} />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("renderPreviewContent LightBurn dispatcher", () => {
  const asset = {
    filename: "x.lbrn2",
    url: "/file",
    size: 1234,
    tags: [],
  } as unknown as Asset;

  it("passes size, card variant and the preview mode to the card preview", () => {
    render(renderPreviewContent(asset, "card", "dark", "disabled"));

    expect(lightBurnProps).toHaveLength(1);
    expect(lightBurnProps[0]).toMatchObject({
      size: 1234,
      variant: "card",
      mode: "disabled",
    });
  });

  it("omits the mode so the modal preview stays automatic", () => {
    render(renderPreviewContent(asset, "modal", "dark"));

    expect(lightBurnProps).toHaveLength(1);
    expect(lightBurnProps[0]).toMatchObject({ size: 1234, variant: "modal" });
    expect(lightBurnProps[0].mode).toBeUndefined();
  });
});

describe("renderPreviewContent 3D snapshot wiring", () => {
  const asset = {
    id: "a1",
    filename: "x.stl",
    url: "/api/assets/a1/file",
    thumb_url: null,
    tags: [],
  } as unknown as Asset;

  it("forwards the caller's thumbnail callback to the card snapshot", async () => {
    const onThumbnailSaved = vi.fn();

    render(renderPreviewContent(asset, "card", "light", "automatic", onThumbnailSaved));

    // React.lazy resolves the mocked snapshot asynchronously.
    await waitFor(() => expect(modelSnapshotProps).toHaveLength(1));
    expect(modelSnapshotProps[0].onThumbnailSaved).toBe(onThumbnailSaved);
  });

  it("never renders a snapshot in the modal variant", async () => {
    render(renderPreviewContent(asset, "modal", "light", "automatic", vi.fn()));

    // The modal renders the interactive viewer, so it is not on the persist path.
    await act(async () => {});
    expect(modelSnapshotProps).toHaveLength(0);
  });
});

const MODAL_ASSET: Asset = {
  id: "a9",
  name: "Wall bracket",
  filename: "bracket.stl",
  mime: "model/stl",
  size: 4096,
  tags: [],
  url: "/api/assets/a9/file",
  thumb_url: "/api/assets/a9/thumb",
  supporting_file_count: 0,
};

/** Every editor callback is a required prop on the upgraded dialog, no defaults. */
function modalProps(
  overrides: Partial<ComponentProps<typeof AssetPreviewModal>> = {},
): ComponentProps<typeof AssetPreviewModal> {
  return {
    asset: MODAL_ASSET,
    theme: "dark",
    onClose: vi.fn(),
    canContribute: false,
    canDelete: false,
    onSaveTags: vi.fn(async () => {}),
    onSaveDetails: vi.fn(async () => {}),
    onRename: vi.fn(async () => {}),
    onMoveFolder: vi.fn(async () => {}),
    folders: [],
    showFolderControls: true,
    moving: false,
    onAssetChanged: vi.fn(),
    onRequestConfirm: vi.fn(),
    ...overrides,
  };
}

describe("AssetPreviewModal dialog upgrade", () => {
  it("is a modal dialog labelled by the asset name", () => {
    render(<AssetPreviewModal {...modalProps()} />);

    const dialog = screen.getByRole("dialog", { name: MODAL_ASSET.name });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(screen.getByRole("heading", { name: MODAL_ASSET.name })).toBeTruthy();
  });

  it("closes on Escape and calls onClose once", () => {
    const onClose = vi.fn();
    render(<AssetPreviewModal {...modalProps({ onClose })} />);

    const dialog = screen.getByRole("dialog", { name: MODAL_ASSET.name });
    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("moves focus in on open and restores it to the opener on unmount", () => {
    const opener = document.createElement("button");
    opener.textContent = "Open details";
    document.body.appendChild(opener);
    opener.focus();

    const { unmount } = render(<AssetPreviewModal {...modalProps()} />);
    const dialog = screen.getByRole("dialog", { name: MODAL_ASSET.name });
    expect(document.activeElement).toBe(dialog);

    unmount();
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it("keeps both Close buttons wired to onClose", () => {
    const onClose = vi.fn();
    render(<AssetPreviewModal {...modalProps({ onClose })} />);

    const closeButtons = screen.getAllByRole("button", { name: "Close" });
    expect(closeButtons).toHaveLength(2);
    fireEvent.click(closeButtons[0]);
    fireEvent.click(closeButtons[1]);

    expect(onClose).toHaveBeenCalledTimes(2);
  });
});

describe("AssetPreviewModal archive extraction", () => {
  const ZIP_ASSET: Asset = { ...MODAL_ASSET, filename: "Moai.zip", mime: "application/zip" };

  it("offers the extract action for a zip the library holds", () => {
    render(
      <AssetPreviewModal
        {...modalProps({ canContribute: true, asset: { ...ZIP_ASSET, storage_path: "Moai.zip" } })}
      />,
    );

    expect(screen.getByRole("button", { name: "Extract Moai.zip" })).toBeTruthy();
  });

  it("offers nothing for a zip row with no library path", () => {
    // A mount/reference row has no `storage_path`: extracting would need a path
    // this asset does not own.
    render(<AssetPreviewModal {...modalProps({ canContribute: true, asset: ZIP_ASSET })} />);

    expect(screen.queryByRole("button", { name: "Extract Moai.zip" })).toBeNull();
  });

  it("offers nothing for a non-zip asset", () => {
    render(
      <AssetPreviewModal
        {...modalProps({ canContribute: true, asset: { ...MODAL_ASSET, storage_path: "bracket.stl" } })}
      />,
    );

    expect(screen.queryByRole("button", { name: /^Extract / })).toBeNull();
  });

  it("offers nothing to a reader", () => {
    render(
      <AssetPreviewModal
        {...modalProps({ canContribute: false, asset: { ...ZIP_ASSET, storage_path: "Moai.zip" } })}
      />,
    );

    expect(screen.queryByRole("button", { name: "Extract Moai.zip" })).toBeNull();
  });
});

// `Parts` has two children, so a child row needs its branch expanded first.
const FOLDERS: Folder[] = [
  { id: "parts", name: "Parts", tags: [], parent_id: null },
  { id: "brackets", name: "Brackets", tags: [], parent_id: "parts" },
  { id: "clamps", name: "Clamps", tags: [], parent_id: "parts" },
];

/** An asset in a known folder; `canDelete` is what unlocks the folder axis. */
function assetIn(folderId: string | null): Asset {
  return { ...MODAL_ASSET, folder_id: folderId };
}

function folderTrigger(): HTMLButtonElement {
  return screen.getByRole("button", { name: /^Folder:/ }) as HTMLButtonElement;
}

function pickerDialog(): HTMLElement | null {
  return screen.queryByRole("dialog", { name: "Folder" });
}

function pickerRow(folderId: string): HTMLElement {
  const element = document.querySelector<HTMLElement>(`[data-picker-row="${folderId}"]`);
  if (!element) throw new Error(`picker row ${folderId} is not rendered`);
  return element;
}

describe("AssetPreviewModal folder picker", () => {
  it("shows the asset's folder on the picker trigger", () => {
    render(<AssetPreviewModal {...modalProps({ canDelete: true, folders: FOLDERS, asset: assetIn("clamps") })} />);

    expect(screen.getByRole("button", { name: "Folder: Clamps" })).toBeTruthy();
  });

  it("opens the picker inside its own panel, not a portal", () => {
    render(<AssetPreviewModal {...modalProps({ canDelete: true, folders: FOLDERS })} />);

    fireEvent.click(folderTrigger());

    const picker = pickerDialog();
    expect(picker).toBeTruthy();
    // Nested, not appended to <body>: the host dialog's focus and key handling
    // only reach it because it stays inside the host's DOM subtree.
    const preview = screen.getByRole("dialog", { name: MODAL_ASSET.name });
    expect(preview.contains(picker)).toBe(true);
  });

  it("closes only the picker on Escape", () => {
    const onClose = vi.fn();
    const onMoveFolder = vi.fn(async () => {});
    render(
      <AssetPreviewModal
        {...modalProps({ canDelete: true, folders: FOLDERS, onClose, onMoveFolder })}
      />,
    );

    fireEvent.click(folderTrigger());
    // The picker focuses its anchor row on open; Escape must be dispatched
    // there, because the host swallows any Escape targeted at its own panel
    // while a nested dialog exists.
    const anchor = document.activeElement as HTMLElement;
    expect(anchor).toBe(pickerRow("__root__"));

    fireEvent.keyDown(anchor, { key: "Escape" });

    expect(pickerDialog()).toBeNull();
    expect(screen.getByRole("dialog", { name: MODAL_ASSET.name })).toBeTruthy();
    expect(document.activeElement).toBe(folderTrigger());
    expect(onClose).not.toHaveBeenCalled();
    expect(onMoveFolder).not.toHaveBeenCalled();

    // The host still owns Escape once the nested dialog is gone.
    fireEvent.keyDown(screen.getByRole("dialog", { name: MODAL_ASSET.name }), { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("traps Tab inside the picker while the preview trap is live", () => {
    render(<AssetPreviewModal {...modalProps({ canDelete: true, folders: FOLDERS })} />);

    fireEvent.click(folderTrigger());
    const picker = pickerDialog();
    const search = screen.getByRole("textbox", { name: "Search folders in Folder" });
    const cancel = picker?.querySelector<HTMLElement>(".folder-picker-cancel");
    if (!cancel) throw new Error("the picker has no Cancel button");

    // Forwards from the picker's DOM-last focusable wraps to its own first,
    // not to the host panel's Close buttons.
    cancel.focus();
    fireEvent.keyDown(cancel, { key: "Tab" });
    expect(document.activeElement).toBe(search);
    expect(picker?.contains(document.activeElement)).toBe(true);

    // Backwards from the picker's first focusable wraps to its own last.
    fireEvent.keyDown(search, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(cancel);
    expect(picker?.contains(document.activeElement)).toBe(true);
  });

  it("ignores Tab once a nested dialog has consumed it", () => {
    render(<AssetPreviewModal {...modalProps({ canDelete: true, folders: FOLDERS })} />);

    const preview = screen.getByRole("dialog", { name: MODAL_ASSET.name });
    const closeButtons = screen.getAllByRole("button", { name: "Close" });
    const footerClose = closeButtons[closeButtons.length - 1];

    // Pins the `defaultPrevented` guard directly: focus outcomes through the
    // real picker cannot observe it (its Cancel is not the host's DOM-last
    // focusable), so a capture listener stands in for an inner dialog that has
    // already taken the Tab move.
    const swallow = (event: Event) => event.preventDefault();
    preview.addEventListener("keydown", swallow, true);

    footerClose.focus();
    fireEvent.keyDown(footerClose, { key: "Tab" });

    // The host trap stays inert instead of rotating its own list.
    expect(document.activeElement).toBe(footerClose);

    preview.removeEventListener("keydown", swallow, true);
  });

  it("commits a row through the picker", () => {
    const onMoveFolder = vi.fn(async () => {});
    render(
      <AssetPreviewModal
        {...modalProps({ canDelete: true, folders: FOLDERS, onMoveFolder, asset: assetIn(null) })}
      />,
    );

    expect(screen.getByRole("button", { name: "Folder: Unassigned (top level)" })).toBeTruthy();
    fireEvent.click(folderTrigger());

    // `Brackets` is a child of `Parts`, so its branch must be expanded first;
    // expanding is not a commit.
    fireEvent.click(within(pickerRow("parts")).getByRole("button", { name: "Expand" }));
    expect(onMoveFolder).not.toHaveBeenCalled();

    fireEvent.click(pickerRow("brackets"));

    expect(onMoveFolder).toHaveBeenCalledWith("a9", "brackets");
    expect(pickerDialog()).toBeNull();
  });

  it("commits the root row to unassign", () => {
    const onMoveFolder = vi.fn(async () => {});
    render(
      <AssetPreviewModal
        {...modalProps({ canDelete: true, folders: FOLDERS, onMoveFolder, asset: assetIn("clamps") })}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Folder: Clamps" }));
    expect(screen.getByRole("treeitem", { name: "Unassigned (top level)" })).toBeTruthy();

    fireEvent.click(pickerRow("__root__"));

    expect(onMoveFolder).toHaveBeenCalledWith("a9", null);
    expect(pickerDialog()).toBeNull();
  });

  it("renders no trigger without folder controls", () => {
    const { unmount } = render(
      <AssetPreviewModal {...modalProps({ canDelete: true, folders: FOLDERS, showFolderControls: false })} />,
    );
    expect(screen.queryByRole("button", { name: /^Folder:/ })).toBeNull();
    unmount();

    // Moving between folders is delete-gated, so a contributor sees nothing.
    render(<AssetPreviewModal {...modalProps({ canContribute: true, folders: FOLDERS })} />);
    expect(screen.queryByRole("button", { name: /^Folder:/ })).toBeNull();
  });

  it("disables the trigger and announces the move while moving", () => {
    render(<AssetPreviewModal {...modalProps({ canDelete: true, folders: FOLDERS, moving: true })} />);

    expect(folderTrigger().disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toBe("Updating...");
  });
});
