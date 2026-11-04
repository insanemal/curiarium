// @vitest-environment jsdom
//
// A library `.zip` has two doors: the preview footer's extract action and the
// card kebab. These probes pin that the kebab is the SAME door — one
// `ArchiveExtractDialog`, one `{path, entries: null}` request, and no preview
// opened on the way — that a `.zip` with no `storage_path` (a mount/reference
// row, whose file is not the library's own copy) gets no item at all, and that
// a viewer's kebab never carries the door.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { Asset } from "../lib/api";
import type { ArchiveExtractResult } from "../lib/api/imports";

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
  extractArchive: vi.fn(),
  listArchiveEntries: vi.fn(),
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

// The real settle waits up to ten minutes of polls: a component test must never
// run it, and its outcome is not what these probes are about.
vi.mock("../lib/scanSettle", () => ({
  settleAfterExtract: vi.fn(async () => "settled"),
  waitForScanSettle: vi.fn(async () => "settled"),
  SETTLE_POLL_MS: 1500,
  SETTLE_TIMEOUT_MS: 600000,
}));

import { extractArchive, listArchiveEntries, listAssets } from "../lib/api";
import { settleAfterExtract, type SettleOutcome } from "../lib/scanSettle";
import AssetGrid from "./asset-grid/AssetGrid";

const listAssetsMock = vi.mocked(listAssets);
const listArchiveEntriesMock = vi.mocked(listArchiveEntries);
const extractArchiveMock = vi.mocked(extractArchive);
const settleAfterExtractMock = vi.mocked(settleAfterExtract);

/** A managed library copy: `storage_path` is what the extract API takes. */
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
  scan_started: true,
};

function serve(asset: Asset): void {
  listAssetsMock.mockResolvedValue({ items: [asset], hasMore: false });
}

/** The kebab is a portal, so the menu items are queried on `screen`. */
function openKebab(): void {
  fireEvent.click(screen.getByRole("button", { name: "More actions" }));
  screen.getByRole("menu");
}

/**
 * Walks kebab -> Extract archive… -> List -> Confirm, and stops once the one
 * `extractArchive` request has been made. The count is taken up front so a
 * second extraction in the same test (after a `cleanup`) still waits for its
 * own request instead of matching the first one.
 */
async function extractThroughDialog(): Promise<void> {
  const before = extractArchiveMock.mock.calls.length;

  openKebab();
  fireEvent.click(screen.getByRole("menuitem", { name: "Extract archive…" }));
  fireEvent.click(screen.getByRole("button", { name: "Extract Moai.zip" }));
  fireEvent.click(await screen.findByRole("button", { name: "Extract 2 files" }));

  await waitFor(() => expect(extractArchiveMock).toHaveBeenCalledTimes(before + 1));
  expect(extractArchiveMock).toHaveBeenLastCalledWith({ path: "Moai.zip", entries: null });
}

/** A promise the test settles by hand: the ordering must be the test's, not the clock's. */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(res => {
    resolve = res;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  // `mockReset` too, not just `clearAllMocks`: a deferred left unresolved by an
  // earlier test must not be handed to this one as its settle.
  settleAfterExtractMock.mockReset();
  settleAfterExtractMock.mockResolvedValue("settled");
  serve(MOAI_ZIP);
  listArchiveEntriesMock.mockResolvedValue({
    path: "Moai.zip",
    entries: [
      { path: "Moai.stl", size: 2048 },
      { path: "readme.txt", size: 512 },
    ],
  });
  extractArchiveMock.mockResolvedValue(EXTRACTED);
});

afterEach(cleanup);

describe("extracting an archive from the card kebab", () => {
  it("extracts a zip from the card kebab without opening the preview", async () => {
    // Proves the kebab opens the shared extract dialog rather than the
    // preview, then walks the dialog's own List -> Confirm -> Extract flow that
    // the preview footer already owns, and that the request is the whole
    // archive (`entries: null`), not a selection.
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
      expect(extractArchiveMock).toHaveBeenCalledWith({ path: "Moai.zip", entries: null }),
    );
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

  it("waits for the library scan before refetching the assets", async () => {
    // Pins `refreshAfterExtract` (useAssetActions.ts): `await settleAfterExtract(...)`
    // must come BEFORE `await refresh()`, or the refetch races the walk.
    const settle = deferred<SettleOutcome>();
    settleAfterExtractMock.mockReturnValueOnce(settle.promise);

    render(<AssetGrid canContribute canDelete theme="dark" />);
    await screen.findByText("Moai");
    // The mount's own page-1 walk has landed; freeze its count so every later
    // call is, by definition, the post-extract refetch.
    await act(async () => {
      await Promise.resolve();
    });
    const callsBeforeExtract = listAssetsMock.mock.calls.length;

    await extractThroughDialog();

    await waitFor(() => expect(settleAfterExtractMock).toHaveBeenCalledTimes(1));
    // The wait is asked about exactly the results the dialog reported.
    expect(settleAfterExtractMock.mock.calls[0][0]).toEqual([EXTRACTED]);
    // The scan is still running: nothing may have re-read the listing yet.
    // This is the race the ticket exists to close, so it fails loudly if
    // `settleAfterExtract` is dropped.
    expect(listAssetsMock).toHaveBeenCalledTimes(callsBeforeExtract);

    settle.resolve("settled");

    // ...and once the scan has settled, the refetch does happen.
    await waitFor(() =>
      expect(listAssetsMock.mock.calls.length).toBeGreaterThan(callsBeforeExtract),
    );
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
    extractArchiveMock.mockResolvedValue({ ...EXTRACTED, already_present: true });
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
