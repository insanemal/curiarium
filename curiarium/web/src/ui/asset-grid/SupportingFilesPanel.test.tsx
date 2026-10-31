// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

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

import SupportingFilesPanel from "./SupportingFilesPanel";
import {
  listAssetFileLimits,
  listAssetFiles,
  uploadAssetFile,
  type Asset,
  type AssetFile,
  type AssetFileLimits,
} from "../../lib/api";

const listAssetFilesMock = vi.mocked(listAssetFiles);
const listAssetFileLimitsMock = vi.mocked(listAssetFileLimits);
const uploadAssetFileMock = vi.mocked(uploadAssetFile);

const NO_FILE_LIMITS: AssetFileLimits = {
  count: 0,
  total_bytes: 0,
  max_count: 0,
  max_file_bytes: 0,
  max_total_bytes: 0,
};

const FILE_LIMITS: AssetFileLimits = {
  count: 1,
  total_bytes: 2,
  max_count: 20,
  max_file_bytes: 10 * 1024 * 1024,
  max_total_bytes: 100 * 1024 * 1024,
};

const A_FILE: AssetFile = {
  id: "f1",
  filename: "a.pdf",
  mime: "application/pdf",
  size: 1,
  position: 0,
  url: "/files/f1",
};

const B_FILE: AssetFile = {
  id: "f2",
  filename: "b.pdf",
  mime: "application/pdf",
  size: 1,
  position: 1,
  url: "/files/f2",
};

let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

/** Drain the promises a handler awaits so a request count is settled. */
async function flushMicrotasks(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
  });
}

function makeAsset(overrides: Partial<Asset> = {}): Asset {
  return {
    id: "a1",
    name: "model.stl",
    filename: "model.stl",
    mime: "model/stl",
    size: 1024,
    tags: [],
    url: "/files/a1",
    supporting_file_count: 0,
    ...overrides,
  };
}

function uploadInput(container: HTMLElement): HTMLInputElement {
  const input = container.querySelector('input[type="file"]');
  if (!(input instanceof HTMLInputElement)) throw new Error("no file input rendered");
  return input;
}

function uploadFiles(input: HTMLInputElement, filenames: string[]): void {
  fireEvent.change(input, {
    target: {
      files: filenames.map(name => new File(["x"], name, { type: "application/pdf" })),
    },
  });
}

beforeEach(() => {
  consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  uploadAssetFileMock.mockReset();
  listAssetFilesMock.mockReset();
  listAssetFilesMock.mockResolvedValue([]);
  listAssetFileLimitsMock.mockReset();
  listAssetFileLimitsMock.mockResolvedValue(NO_FILE_LIMITS);
});

afterEach(() => {
  cleanup();
  consoleErrorSpy.mockRestore();
});

describe("SupportingFilesPanel upload refresh", () => {
  it("issues exactly one list and one limits request per successful upload batch (panel collapsed)", async () => {
    uploadAssetFileMock.mockResolvedValue(makeAsset({ supporting_file_count: 2 }));
    listAssetFilesMock.mockResolvedValue([A_FILE, B_FILE]);
    listAssetFileLimitsMock.mockResolvedValue({ ...FILE_LIMITS, count: 2 });

    const { container } = render(
      <SupportingFilesPanel
        asset={makeAsset()}
        canContribute
        canDelete={false}
        onAssetChanged={vi.fn()}
        onRequestConfirm={vi.fn()}
      />,
    );
    await flushMicrotasks();

    // Pinned invariant: a collapsed panel issues no requests.
    expect(listAssetFilesMock).not.toHaveBeenCalled();
    expect(listAssetFileLimitsMock).not.toHaveBeenCalled();

    // Two files in ONE change event are one upload batch, not two refreshes.
    uploadFiles(uploadInput(container), ["a.pdf", "b.pdf"]);
    await flushMicrotasks();
    await flushMicrotasks();

    expect(listAssetFilesMock).toHaveBeenCalledTimes(1);
    await flushMicrotasks();
    expect(listAssetFilesMock).toHaveBeenCalledTimes(1);
    expect(listAssetFileLimitsMock).toHaveBeenCalledTimes(1);
    await flushMicrotasks();
    expect(listAssetFileLimitsMock).toHaveBeenCalledTimes(1);

    // The batch opens the panel so the user sees what landed.
    expect(screen.getByText("a.pdf")).toBeTruthy();
    expect(screen.getByText("b.pdf")).toBeTruthy();
  });

  it("issues exactly one list and one limits request when the panel is already expanded", async () => {
    uploadAssetFileMock.mockResolvedValue(makeAsset({ supporting_file_count: 1 }));
    listAssetFilesMock.mockResolvedValue([A_FILE]);
    listAssetFileLimitsMock.mockResolvedValue(FILE_LIMITS);

    const { container } = render(
      <SupportingFilesPanel
        asset={makeAsset()}
        canContribute
        canDelete={false}
        onAssetChanged={vi.fn()}
        onRequestConfirm={vi.fn()}
        defaultExpanded
      />,
    );
    await flushMicrotasks();

    // The mount load of an already-expanded panel is expected; count from the upload.
    expect(listAssetFilesMock).toHaveBeenCalledTimes(1);
    expect(listAssetFileLimitsMock).toHaveBeenCalledTimes(1);
    listAssetFilesMock.mockClear();
    listAssetFileLimitsMock.mockClear();

    uploadFiles(uploadInput(container), ["a.pdf"]);
    await flushMicrotasks();
    await flushMicrotasks();

    expect(listAssetFilesMock).toHaveBeenCalledTimes(1);
    await flushMicrotasks();
    expect(listAssetFilesMock).toHaveBeenCalledTimes(1);
    expect(listAssetFileLimitsMock).toHaveBeenCalledTimes(1);
    await flushMicrotasks();
    expect(listAssetFileLimitsMock).toHaveBeenCalledTimes(1);

    // Uploading into an open panel must not close it.
    expect(screen.getByText("a.pdf")).toBeTruthy();
  });

  /**
   * REGRESSION GUARD, not a fails-before test. On the broken code the throw skips
   * the `try` refresh, so the partial-failure path already issued exactly one GET;
   * the failing-before evidence for "one refresh" comes from the two tests above.
   */
  it("refreshes once after a partial failure and shows the file that landed", async () => {
    uploadAssetFileMock
      .mockResolvedValueOnce(makeAsset({ supporting_file_count: 1 }))
      .mockRejectedValueOnce(new Error("nope"));
    listAssetFilesMock.mockResolvedValue([A_FILE]);
    listAssetFileLimitsMock.mockResolvedValue(FILE_LIMITS);

    const { container } = render(
      <SupportingFilesPanel
        asset={makeAsset()}
        canContribute
        canDelete={false}
        onAssetChanged={vi.fn()}
        onRequestConfirm={vi.fn()}
      />,
    );
    await flushMicrotasks();

    uploadFiles(uploadInput(container), ["a.pdf", "b.pdf"]);
    await flushMicrotasks();
    await flushMicrotasks();

    expect(listAssetFilesMock).toHaveBeenCalledTimes(1);
    await flushMicrotasks();
    expect(listAssetFilesMock).toHaveBeenCalledTimes(1);
    expect(listAssetFileLimitsMock).toHaveBeenCalledTimes(1);
    await flushMicrotasks();
    expect(listAssetFileLimitsMock).toHaveBeenCalledTimes(1);

    // The stored file is shown; the rejected one never existed server-side.
    expect(screen.getByText("a.pdf")).toBeTruthy();
    expect(screen.queryByText("b.pdf")).toBeNull();
    expect(consoleErrorSpy).toHaveBeenCalled();
  });
});
