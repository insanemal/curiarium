// @vitest-environment jsdom
import { type ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

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

import type { Asset, PreparedPrint } from "../../lib/api";
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
    folderOptions: [],
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
