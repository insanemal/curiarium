// @vitest-environment jsdom
//
// A library `.zip` has two doors: the preview footer's extract action and the
// card kebab. These probes pin that the kebab is the SAME door — one
// `ArchiveExtractDialog`, one `extract` operation submitted through the shared
// action, and no preview opened on the way — that a `.zip` with no
// `storage_path` (a mount/reference row, whose file is not the library's own
// copy) gets no item at all, and that a viewer's kebab never carries the door.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { Asset } from "../lib/api";
import type { ArchiveExtractResult } from "../lib/api/imports";
// The action drives the operations ledger through `useOperationRun`, which
// imports the domain module directly: the barrel mock below cannot stand in for
// it, so the three calls are replaced where the panel reads them.
const operationsApi = vi.hoisted(() => ({
  planOperation: vi.fn(),
  submitOperation: vi.fn(),
  getOperation: vi.fn(),
  undoOperation: vi.fn(),
}));
vi.mock("../lib/api/operations", async importOriginal => ({
  ...(await importOriginal<typeof import("../lib/api/operations")>()),
  planOperation: operationsApi.planOperation,
  submitOperation: operationsApi.submitOperation,
  getOperation: operationsApi.getOperation,
  undoOperation: operationsApi.undoOperation,
}));

vi.mock("../lib/api", () => ({
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

// The real settle can poll for ten minutes of a clock a component test does not
// own. Nothing in this door may call it any more - the extract's `register` step
// writes the rows in-process - so the mock is here to make that observable.
vi.mock("../lib/scanSettle", () => ({
  settleAfterExtract: vi.fn(async () => "settled"),
  waitForScanSettle: vi.fn(async () => "settled"),
  SETTLE_POLL_MS: 1500,
  SETTLE_TIMEOUT_MS: 600000,
}));

import { listAssets } from "../lib/api";
import {
  getOperation,
  planOperation,
  submitOperation,
  type Operation,
  type OperationPlan,
  type OperationReview,
  type OperationStep,
} from "../lib/api/operations";
import { settleAfterExtract } from "../lib/scanSettle";
import AssetGrid from "./asset-grid/AssetGrid";

const listAssetsMock = vi.mocked(listAssets);
const planOperationMock = vi.mocked(planOperation);
const submitOperationMock = vi.mocked(submitOperation);
const getOperationMock = vi.mocked(getOperation);
const settleAfterExtractMock = vi.mocked(settleAfterExtract);

/** A managed library copy: `storage_path` is what the extract operation targets. */
const MOAI_ZIP: Asset = {
  id: "z1",
  name: "Moai",
  filename: "Moai.zip",
  mime: "application/zip",
  size: 4096,
  tags: [],
  url: "/file/z1/Moai.zip",
  thumb_url: null,
  storage_path: "Moai.zip",
  supporting_file_count: 0,
};

/** The same archive as a mount/reference row: no library copy to extract. */
const MOAI_ZIP_REFERENCE: Asset = { ...MOAI_ZIP, storage_path: null, is_reference: true };

const EXTRACTED: ArchiveExtractResult = {
  path: "Moai.zip",
  destination: "Moai",
  files: 2,
  bytes: 2560,
  already_present: false,
  scan_started: false,
};

function extractReview(): OperationReview {
  return {
    workflow: "extract",
    target: "Moai.zip",
    moves: 0,
    creates: 0,
    adopts: 0,
    deletes: 0,
    members: 2,
    groups: [{ name: "", members: 2, models: 1 }],
    cover: null,
    excluded: [{ path: "__MACOSX/._Moai.stl", reason: "system metadata" }],
    existing_project_id: null,
    existing_project_name: null,
    move_list: [],
    projects: [],
  };
}

const PLAN: OperationPlan = {
  plan_id: "plan-Moai.zip",
  workflow: "extract",
  target: "Moai.zip",
  steps: [
    { index: 0, op: "extract", summary: "extract 2 file(s) from 'Moai.zip'" },
    { index: 1, op: "register", summary: "register the 2 file(s) the extraction wrote" },
  ],
  review: extractReview(),
};

function step(overrides: Partial<OperationStep> = {}): OperationStep {
  return {
    index: 0,
    op: "extract",
    status: "pending",
    result: null,
    error: null,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

function queuedRow(): Operation {
  return {
    id: "op-1",
    workflow: "extract",
    target: "Moai.zip",
    status: "queued",
    submitted_by: "u1",
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    finished_at: null,
    error: null,
    undo_state: "unavailable",
    superseded_by: null,
    steps: [],
  };
}

function finishedRow(result: ArchiveExtractResult): Operation {
  return {
    ...queuedRow(),
    status: "done",
    steps: [
      step({ index: 0, op: "extract", status: "done", result: { ...result } }),
      step({ index: 1, op: "register", status: "done" }),
    ],
  };
}

function serve(asset: Asset): void {
  listAssetsMock.mockResolvedValue({ items: [asset], hasMore: false });
}

/** The kebab is a portal, so the menu items are queried on `screen`. */
function openKebab(): void {
  fireEvent.click(screen.getByRole("button", { name: "More actions" }));
  screen.getByRole("menu");
}

/**
 * Walks kebab -> Extract archive… -> plan -> confirm, and stops once the one
 * submit has been made. The count is taken up front so a second extraction in
 * the same test (after a `cleanup`) still waits for its own request instead of
 * matching the first one.
 */
async function extractThroughDialog(): Promise<void> {
  const before = submitOperationMock.mock.calls.length;

  openKebab();
  fireEvent.click(screen.getByRole("menuitem", { name: "Extract archive…" }));
  fireEvent.click(screen.getByRole("button", { name: "Extract Moai.zip" }));
  fireEvent.click(await screen.findByRole("button", { name: "Extract 2 files" }));

  // The plan's id is the whole request: the dialog assembles nothing itself.
  await waitFor(() => expect(submitOperationMock).toHaveBeenCalledTimes(before + 1));
  expect(submitOperationMock).toHaveBeenLastCalledWith({
    planId: "plan-Moai.zip",
    idempotencyKey: expect.any(String),
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  // `mockReset` too, not just `clearAllMocks`: a deferred left unresolved by an
  // earlier test must not be handed to this one as its settle.
  settleAfterExtractMock.mockReset();
  settleAfterExtractMock.mockResolvedValue("settled");
  serve(MOAI_ZIP);
  planOperationMock.mockReset();
  planOperationMock.mockResolvedValue(PLAN);
  submitOperationMock.mockReset();
  submitOperationMock.mockResolvedValue(queuedRow());
  getOperationMock.mockReset();
  getOperationMock.mockResolvedValue(finishedRow(EXTRACTED));
});

afterEach(cleanup);

describe("extracting an archive from the card kebab", () => {
  it("extracts a zip from the card kebab without opening the preview", async () => {
    // Proves the kebab opens the shared extract dialog rather than the preview,
    // then walks the dialog's own plan -> confirm -> submit flow that the preview
    // footer already owns, and that the operation targets the whole archive.
    render(<AssetGrid canContribute canDelete theme="dark" />);
    await screen.findByText("Moai");

    openKebab();
    fireEvent.click(screen.getByRole("menuitem", { name: "Extract archive…" }));

    expect(screen.getByRole("dialog", { name: "Extract Moai.zip" })).toBeTruthy();
    // The card-menu door must not double as the preview's: the preview dialog is
    // named after the asset itself, so its absence is visible right here.
    expect(screen.queryByRole("dialog", { name: "Moai" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Extract Moai.zip" }));
    fireEvent.click(await screen.findByRole("button", { name: "Extract 2 files" }));

    await waitFor(() =>
      expect(planOperationMock).toHaveBeenCalledWith({
        workflow: "extract",
        target: "Moai.zip",
        params: expect.any(Object),
      }),
    );
    await waitFor(() => expect(submitOperationMock).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("dialog", { name: "Moai" })).toBeNull();
  });

  it("offers no extract item for a zip with no storage_path", async () => {
    // Proves the item follows the same `storage_path` predicate as the preview
    // footer: a mount/reference `.zip` has no library copy, so the menu offers
    // no door rather than a wrong path. `Add to project…` is asserted present so
    // this cannot pass on a menu that never opened.
    serve(MOAI_ZIP_REFERENCE);
    render(<AssetGrid canContribute canDelete theme="dark" />);
    await screen.findByText("Moai");

    openKebab();
    expect(screen.queryByRole("menuitem", { name: "Extract archive…" })).toBeNull();
    expect(screen.getByRole("menuitem", { name: "Add to project…" })).toBeTruthy();
  });

  it("refetches the assets without waiting for a library scan", async () => {
    // Pins `refreshAfterExtract` (useAssetActions.ts): the extract's register step
    // writes the asset rows in-process, so there is no walk to settle - a wait
    // here would poll a scan that was never started, and the refetch would be
    // held hostage by it.
    render(<AssetGrid canContribute canDelete theme="dark" />);
    await screen.findByText("Moai");
    const callsBeforeExtract = listAssetsMock.mock.calls.length;

    await extractThroughDialog();

    await waitFor(() =>
      expect(listAssetsMock.mock.calls.length).toBeGreaterThan(callsBeforeExtract),
    );
    expect(settleAfterExtractMock).not.toHaveBeenCalled();
  });

  it("files the new assets with the app once the extract wrote something", async () => {
    // Pins the `if (results.some(r => !r.already_present)) onAssetsFiled?.()` line
    // in `refreshAfterExtract`: a write must refetch App's counts, a no-op must not.
    const onAssetsFiled = vi.fn();
    render(
      <AssetGrid canContribute canDelete theme="dark" onAssetsFiled={onAssetsFiled} />,
    );
    await screen.findByText("Moai");

    await extractThroughDialog();

    await waitFor(() => expect(onAssetsFiled).toHaveBeenCalledTimes(1));

    // Second half: every result was already on disk, so the extract wrote
    // nothing and there is no count for App to refresh.
    cleanup();
    getOperationMock.mockResolvedValue(finishedRow({ ...EXTRACTED, already_present: true }));
    const filedNothing = vi.fn();
    render(
      <AssetGrid canContribute canDelete theme="dark" onAssetsFiled={filedNothing} />,
    );
    await screen.findByText("Moai");
    const callsBeforeExtract = listAssetsMock.mock.calls.length;

    await extractThroughDialog();

    // The refetch still happens (proving the flow ran to completion); only the
    // filing signal is withheld.
    await waitFor(() =>
      expect(listAssetsMock.mock.calls.length).toBeGreaterThan(callsBeforeExtract),
    );
    expect(filedNothing).not.toHaveBeenCalled();
  });

  it("hides the extract menu item from a viewer", async () => {
    // Extracting writes new bytes into the library, so it is a contribute
    // capability: a viewer's kebab must not carry the item. `Open preview` is
    // the always-present first entry, so it proves the menu really opened.
    render(<AssetGrid canContribute={false} canDelete={false} theme="dark" />);
    await screen.findByText("Moai");

    openKebab();
    expect(screen.queryByRole("menuitem", { name: "Extract archive…" })).toBeNull();
    expect(screen.getByRole("menuitem", { name: "Open preview" })).toBeTruthy();
  });
});
