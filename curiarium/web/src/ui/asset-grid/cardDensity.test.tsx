// @vitest-environment jsdom
//
// M1 density deliverable: the card used to render a file-management console on
// every card (download <select>, folder <select>, hidden input[type=file],
// Add files, inline rename/notes/tag editors, supporting-file rows and a Model
// details panel). It is now preview + name + fact line + filename + attribution
// + tags + the `Select` checkbox + ONE kebab menu. These probes pin that
// reduction with the numbers measured at base 2731ec9 and after the change.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { Asset } from "../../lib/api";

vi.mock("../../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  addProjectAssets: vi.fn(),
  batchDeleteAssets: vi.fn(),
  batchMoveAssets: vi.fn(),
  batchSetMeta: vi.fn(),
  batchSetTags: vi.fn(),
  createProject: vi.fn(),
  createFolder: vi.fn(),
  deleteAsset: vi.fn(),
  deleteAssetFile: vi.fn(),
  deletePreparedPrint: vi.fn(),
  downloadZip: vi.fn(),
  fileUrl: vi.fn((rel: string) => rel),
  getAssetDeletePreview: vi.fn(),
  getLibraryScanStatus: vi.fn(),
  assetFilePreviewUrl: vi.fn((rel: string) => `${rel}?inline=1`),
  listAssetFileLimits: vi.fn(async () => ({
    count: 0,
    total_bytes: 0,
    max_count: 0,
    max_file_bytes: 0,
    max_total_bytes: 0,
  })),
  listAssetFiles: vi.fn(async () => []),
  renameAssetFile: vi.fn(),
  reorderAssetFiles: vi.fn(async (_assetId: string, fileIds: string[]) => fileIds),
  listAssets: vi.fn(),
  listFolders: vi.fn(async () => []),
  listProjects: vi.fn(async () => []),
  listCreators: vi.fn(async () => []),
  listTags: vi.fn(async () => []),
  setTags: vi.fn(),
  startLibraryScan: vi.fn(),
  updateAssetFolder: vi.fn(),
  updateAssetMeta: vi.fn(),
  uploadAsset: vi.fn(),
  uploadAssetFile: vi.fn(),
}));

vi.mock("../ModelViewer", () => ({
  default: () => null,
  ModelSnapshot: () => null,
}));

vi.mock("../LightBurnPreview", () => ({
  default: () => null,
}));

import { listAssets } from "../../lib/api";
import AssetCard from "./AssetCard";
import type { AssetCardProps } from "./assetCardProps";
import AssetGrid from "./AssetGrid";

const listAssetsMock = vi.mocked(listAssets);

function makeAsset(id: string, overrides: Partial<Asset> = {}): Asset {
  return {
    id,
    name: `Model ${id}`,
    filename: `${id}.png`,
    mime: "image/png",
    size: 12_345,
    tags: [],
    url: `/file/${id}.png`,
    thumb_url: `/thumb/${id}.jpg`,
    supporting_file_count: 0,
    ...overrides,
  };
}

/** Full editor privileges: the widest card, which is the one being measured. */
function cardProps(item: Asset): AssetCardProps {
  return {
    item,
    canContribute: true,
    canDelete: true,
    onPreview: vi.fn(),
    onOpenDetails: vi.fn(),
    onDownloadSingle: vi.fn(),
    onDownloadByTag: vi.fn(),
    downloading: false,
    onDelete: vi.fn(),
    onAddToProject: vi.fn(),
    selected: false,
    onToggleSelected: vi.fn(),
    slicerEnabled: true,
    slicerLabel: "Orca",
    onOpenInSlicer: vi.fn(),
    engravingEnabled: true,
    engraverLabel: "LightBurn",
    onOpenInEngraving: vi.fn(),
    theme: "dark",
    previewMode: "automatic",
    onAssetChanged: vi.fn(),
    onForgetMissing: vi.fn(),
    onRescanMissing: vi.fn(),
    rescanning: false,
    onClearPreparedPrint: vi.fn(),
    preparedClearing: false,
  };
}

/** The exact measurement both probes use, so before/after stay comparable. */
function measure(card: HTMLElement) {
  return {
    nodes: card.querySelectorAll("*").length,
    interactive: card.querySelectorAll("button, input, select, textarea, a").length,
    selects: card.querySelectorAll("select").length,
    fileInputs: card.querySelectorAll('input[type="file"]').length,
    textareas: card.querySelectorAll("textarea").length,
  };
}

const BARE = makeAsset("bare");

// Base 2731ec9: 39 element nodes / 11 interactive controls (2 select, 1 file
// input). After: 14 / 2, no select and no file input.
const TAGGED = makeAsset("tagged", {
  tags: ["pla", "strong", "gift"],
  creator: "Grace Hopper",
  created_by: "Ada",
  notes: "A long note about this model that would previously have rendered a Model details panel.",
  supporting_file_count: 2,
});
// Base: 47 / 11. After: 19 / 2.

const PREPARED = makeAsset("prepared", {
  filename: "prepared.3mf",
  mime: "model/3mf",
  prepared_print: {
    printer: "X1C",
    material: "PLA",
    nozzle_mm: 0.4,
    estimated_seconds: 7200,
    slicer: "OrcaSlicer",
    removable: true,
  },
  supporting_file_count: 1,
});
// Base: 44 / 12. After: 18 / 2.

const MISSING = makeAsset("missing", { missing_since: "2026-10-27T11:31:34+00:00" });
// Base: 45 / 13. After: 16 / 2.

const cardCases = [
  // name / fixture / budget / base nodes+interactive -> after nodes+interactive
  { name: "bare", asset: BARE, budget: 16 }, // 39/11 -> 14/2
  { name: "tagged", asset: TAGGED, budget: 22 }, // 47/11 -> 19/2
  { name: "prepared-print", asset: PREPARED, budget: 20 }, // 44/12 -> 18/2
  { name: "missing", asset: MISSING, budget: 18 }, // 45/13 -> 16/2
];

describe("AssetCard per-variant density", () => {
  beforeEach(() => vi.clearAllMocks());
  afterEach(cleanup);

  it.each(cardCases)(
    "a $name card's element-node and interactive-control counts are at or below the recorded budget",
    ({ asset, budget }) => {
      const view = render(<AssetCard {...cardProps(asset)} />);

      const card = view.container.querySelector<HTMLElement>(`[data-asset-id="${asset.id}"]`);
      expect(card).not.toBeNull();
      const counts = measure(card as HTMLElement);

      // Budget carries headroom over the measured after-value; the base->after
      // pair for this variant is recorded next to its fixture above.
      expect(counts.nodes).toBeLessThanOrEqual(budget);
      expect(counts.interactive).toBeLessThanOrEqual(2);

      // The two per-card controls the console needed are gone outright.
      expect(counts.selects).toBe(0);
      expect(counts.fileInputs).toBe(0);
      expect(counts.textareas).toBe(0);
    },
  );
});

describe("grouped (type-sort) grid density", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listAssetsMock.mockResolvedValue({
      items: Array.from({ length: 30 }, (_, index) => makeAsset(`asset-${index + 1}`)),
      hasMore: false,
    });
  });
  afterEach(cleanup);

  it("renders the 24-card grouped page within the recorded document-wide budgets", async () => {
    render(<AssetGrid canContribute canDelete theme="dark" />);
    await screen.findByText("Model asset-1");

    // Grouped sorts are the non-virtualised mode: 24 cards, then expanded on
    // demand. Find the sort control by its `folder` sentinel option rather than
    // by label, because the toolbar also carries a creator select.
    const sortSelect = screen
      .getAllByRole("combobox")
      .find(element =>
        Array.from((element as HTMLSelectElement).options).some(option => option.value === "folder"),
      ) as HTMLSelectElement | undefined;
    if (!sortSelect) throw new Error("sort control not rendered");
    fireEvent.change(sortSelect, { target: { value: "type" } });

    await waitFor(() => expect(document.querySelector("[data-virtual-grid]")).toBeNull());

    const cards = Array.from(document.querySelectorAll<HTMLElement>("[data-asset-id]"));
    const documentNodes = document.querySelectorAll("*").length;
    const documentInteractive = document.querySelectorAll("button, input, select, textarea, a").length;
    const perCardTotals = (selector: string) =>
      cards.reduce((total, card) => total + card.querySelectorAll(selector).length, 0);

    // Base 2731ec9: 992 document nodes / 247 interactive / 144 card buttons /
    // 48 card selects / 24 card file inputs. After: 448 / 55 / 24 / 0 / 0.
    expect(cards).toHaveLength(24);
    expect(documentNodes).toBeLessThanOrEqual(520);
    expect(documentInteractive).toBeLessThanOrEqual(64);
    expect(perCardTotals("select")).toBe(0);
    expect(perCardTotals('input[type="file"]')).toBe(0);
    expect(perCardTotals("textarea")).toBe(0);
  });
});
