// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createRef } from "react";

import type { Asset } from "../lib/api";

vi.mock("../lib/api", () => ({
  UnauthorizedError: class UnauthorizedError extends Error {},
  addProjectAssets: vi.fn(),
  batchDeleteAssets: vi.fn(),
  batchMoveAssets: vi.fn(),
  batchSetMeta: vi.fn(),
  batchSetTags: vi.fn(),
  createProject: vi.fn(),
  deleteAsset: vi.fn(),
  deleteAssetFile: vi.fn(),
  deletePreparedPrint: vi.fn(),
  downloadZip: vi.fn(),
  fileUrl: vi.fn((rel: string) => rel),
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
  listProjectAssets: vi.fn(),
  listFolders: vi.fn(async () => []),
  listProjects: vi.fn(async () => []),
  listCreators: vi.fn(async () => []),
  listTags: vi.fn(async () => []),
  removeProjectAssets: vi.fn(),
  setTags: vi.fn(),
  startLibraryScan: vi.fn(),
  updateAssetFolder: vi.fn(),
  updateAssetMeta: vi.fn(),
  uploadAsset: vi.fn(),
  uploadAssetFile: vi.fn(),
}));

vi.mock("./ModelViewer", () => ({
  default: () => null,
  ModelSnapshot: () => null,
}));

vi.mock("./LightBurnPreview", () => ({
  default: () => null,
}));

import AssetGrid from "./asset-grid/AssetGrid";
import {
  addProjectAssets,
  batchDeleteAssets,
  batchSetTags,
  deleteAsset,
  listAssets,
  listCreators,
  listFolders,
  listProjectAssets,
  listTags,
  removeProjectAssets,
  setTags,
  updateAssetMeta,
  uploadAsset,
} from "../lib/api";
import { getToasts, resetToasts } from "../lib/toast";
import { ESTIMATED_ROW_HEIGHT, GRID_GAP_PX, colsForWidth } from "../lib/virtualGrid";

const listAssetsMock = vi.mocked(listAssets);
const listCreatorsMock = vi.mocked(listCreators);
const listProjectAssetsMock = vi.mocked(listProjectAssets);
const listFoldersMock = vi.mocked(listFolders);
const listTagsMock = vi.mocked(listTags);
const removeProjectAssetsMock = vi.mocked(removeProjectAssets);
const deleteAssetMock = vi.mocked(deleteAsset);
const batchDeleteAssetsMock = vi.mocked(batchDeleteAssets);
const addProjectAssetsMock = vi.mocked(addProjectAssets);
const updateAssetMetaMock = vi.mocked(updateAssetMeta);
const setTagsMock = vi.mocked(setTags);
const batchSetTagsMock = vi.mocked(batchSetTags);
const uploadAssetMock = vi.mocked(uploadAsset);

const assets: Asset[] = [
  {
    id: "a1",
    name: "Calibration cube",
    filename: "calibration-cube.stl",
    mime: "model/stl",
    size: 1200,
    tags: ["pla"],
    url: "/api/assets/a1/file",
    thumb_url: "/api/assets/a1/thumb",
    supporting_file_count: 0,
  },
  {
    id: "b2",
    name: "Spool holder",
    filename: "spool-holder.stl",
    mime: "model/stl",
    size: 3400,
    tags: [],
    url: "/api/assets/b2/file",
    thumb_url: "/api/assets/b2/thumb",
    supporting_file_count: 0,
  },
];

function projectGrid(projectId: string | null = "p1") {
  return render(<AssetGrid canContribute canDelete theme="dark" projectId={projectId} projectName="Hydra" />);
}

type ProjectGridProps = {
  canContribute?: boolean;
  canDelete?: boolean;
  onProjectsChanged?: () => void;
  scrollElementRef?: React.RefObject<HTMLElement | null>;
};

/** Props-driven project render, so each test can choose its role. */
function renderProjectGrid(props: ProjectGridProps = {}) {
  return render(
    <AssetGrid
      canContribute={props.canContribute ?? true}
      canDelete={props.canDelete ?? true}
      theme="dark"
      projectId="p1"
      projectName="Hydra"
      onProjectsChanged={props.onProjectsChanged}
      scrollElementRef={props.scrollElementRef}
    />,
  );
}

function cardFor(assetId: string): HTMLElement {
  const card = document.querySelector<HTMLElement>(`[data-asset-id="${assetId}"]`);
  if (!card) throw new Error(`card ${assetId} is not rendered`);
  return card;
}

/** The card's own selection box, scoped so two cards can never be confused. */
function checkboxFor(assetId: string): HTMLInputElement {
  return within(cardFor(assetId)).getByRole("checkbox", { name: "Select" }) as HTMLInputElement;
}

function openCardMenu(assetId: string): HTMLElement {
  fireEvent.click(within(cardFor(assetId)).getByRole("button", { name: "More actions" }));
  return screen.getByRole("menu");
}

/** Every write API a role without the capability must never reach. */
function expectNoWriteCalls(): void {
  expect(removeProjectAssetsMock).not.toHaveBeenCalled();
  expect(deleteAssetMock).not.toHaveBeenCalled();
  expect(batchDeleteAssetsMock).not.toHaveBeenCalled();
  expect(addProjectAssetsMock).not.toHaveBeenCalled();
  expect(updateAssetMetaMock).not.toHaveBeenCalled();
  expect(setTagsMock).not.toHaveBeenCalled();
  expect(batchSetTagsMock).not.toHaveBeenCalled();
  expect(uploadAssetMock).not.toHaveBeenCalled();
}

function makeSyntheticAsset(index: number): Asset {
  const id = `asset-${index}`;
  return {
    id,
    name: `Asset ${index}`,
    filename: `asset-${index}.png`,
    mime: "image/png",
    size: 1000,
    tags: [],
    url: `/api/assets/${id}/file`,
    thumb_url: `/api/assets/${id}/thumb`,
    supporting_file_count: 0,
  };
}

/** A DOMRect-shaped object for the virtualiser's rect reads. */
function rectOf(top: number, height: number): DOMRect {
  return {
    top,
    bottom: top + height,
    left: 0,
    right: 0,
    width: 0,
    height,
    x: 0,
    y: top,
    toJSON: () => ({}),
  } as DOMRect;
}

beforeEach(() => {
  listAssetsMock.mockReset();
  listCreatorsMock.mockReset();
  listProjectAssetsMock.mockReset();
  listFoldersMock.mockReset();
  listTagsMock.mockReset();
  removeProjectAssetsMock.mockReset();
  resetToasts();

  listAssetsMock.mockResolvedValue({ items: [], hasMore: false });
  listCreatorsMock.mockResolvedValue([]);
  listProjectAssetsMock.mockResolvedValue({ items: assets, hasMore: false });
  listFoldersMock.mockResolvedValue([]);
  listTagsMock.mockResolvedValue([]);
  removeProjectAssetsMock.mockResolvedValue({ removed: [], skipped: [], failed: [] });
});

afterEach(cleanup);

describe("AssetGrid in project mode", () => {
  it("lists project assets through listProjectAssets and never touches folders", async () => {
    projectGrid();

    await screen.findByText("Calibration cube");

    expect(listProjectAssetsMock).toHaveBeenCalledTimes(1);
    expect(listProjectAssetsMock.mock.calls[0][0]).toMatchObject({
      projectId: "p1",
      q: "",
      tags: [],
    });
    expect(listAssetsMock).not.toHaveBeenCalled();
    expect(listFoldersMock).not.toHaveBeenCalled();
    // Tag chips follow the open project now (Mf-o4vi), so they carry its id.
    expect(listTagsMock).toHaveBeenCalledWith({
      q: "",
      project_id: "p1",
      folder_id: undefined,
      // A project has no folder axis, so the Unfiled pile is files-only too.
      unfiled: undefined,
      not_in_project: undefined,
      // The facet carries the pinned `missing` scope like the listing does; a
      // project grid is never in missing mode, so it is false here.
      missing: false,
    });
  });

  it("hides every folder affordance", async () => {
    const { container } = projectGrid();
    await screen.findByText("Calibration cube");

    expect(container.querySelector("[data-folder-breadcrumbs]")).toBeNull();
    expect(container.querySelector("[data-folder-tiles]")).toBeNull();
    expect(screen.queryByRole("button", { name: /^Folder:/ })).toBeNull();

    const sortSelect = screen
      .getAllByRole("combobox")
      .find(element =>
        Array.from((element as HTMLSelectElement).options).some(option => option.value === "size"),
      ) as HTMLSelectElement;
    expect(Array.from(sortSelect.options).map(option => option.value)).not.toContain("folder");
  });

  it("ignores file drags instead of offering an upload", async () => {
    const { container } = projectGrid();
    await screen.findByText("Calibration cube");

    const root = container.firstElementChild as Element;
    fireEvent.dragEnter(root, {
      dataTransfer: { types: ["Files"], items: [], files: [], dropEffect: "copy" },
    });

    expect(screen.queryByText(/Drop files or folders to upload/)).toBeNull();
  });

  it("hides the files-mode unassigned filter entirely", async () => {
    projectGrid();
    await screen.findByText("Calibration cube");

    // Membership already scopes a project listing, so the checkbox is files-only.
    expect(screen.queryByRole("checkbox", { name: "Not in any project" })).toBeNull();
    for (const [params] of listProjectAssetsMock.mock.calls) {
      expect(params).not.toHaveProperty("not_in_project");
    }
    expect(listAssetsMock).not.toHaveBeenCalled();
  });

  it("passes the submitted query to listProjectAssets", async () => {
    projectGrid();
    await screen.findByText("Calibration cube");

    fireEvent.change(screen.getByPlaceholderText("Search title, filename, notes..."), {
      target: { value: "cube" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() =>
      expect(listProjectAssetsMock).toHaveBeenLastCalledWith(
        expect.objectContaining({ projectId: "p1", q: "cube" }),
      ),
    );
  });

  it("renders the empty-project message instead of files-mode empty copy", async () => {
    listProjectAssetsMock.mockResolvedValue({ items: [], hasMore: false });

    projectGrid();

    expect(await screen.findByText("This project has no files yet.")).toBeTruthy();
  });

  it("does not claim an empty project while a search filter is active", async () => {
    listProjectAssetsMock.mockResolvedValue({ items: [], hasMore: false });

    projectGrid();

    // A submitted query that matched nothing is an empty result, not an empty
    // project, so the empty-project copy must stay off.
    expect(await screen.findByText("This project has no files yet.")).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("Search title, filename, notes..."), {
      target: { value: "nothing-matches" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() => expect(screen.queryByText("This project has no files yet.")).toBeNull());
  });

  it("does not claim an empty project when a creator filter is active", async () => {
    listProjectAssetsMock.mockResolvedValue({ items: [], hasMore: false });
    listCreatorsMock.mockResolvedValue(["Ada"]);

    projectGrid();

    // The creator select is mounted in project mode too, so a creator that
    // matches nothing in this project is an empty result, not an empty project.
    const creatorSelect = await screen.findByRole("combobox", { name: "Creator" });
    expect(await screen.findByText("This project has no files yet.")).toBeTruthy();
    fireEvent.change(creatorSelect, { target: { value: "Ada" } });

    const filterBlock = await waitFor(() => {
      const found = document.querySelector<HTMLElement>("[data-project-empty-filter]");
      if (!found) throw new Error("project filtered-empty state not rendered");
      return found;
    });
    expect(
      within(filterBlock).getByText("No files in this project match this filter"),
    ).toBeTruthy();
    expect(document.querySelector("[data-project-empty]")).toBeNull();
    expect(screen.queryByText("This project has no files yet.")).toBeNull();
  });

  it("gives a tag-filtered empty project the filtered-empty line", async () => {
    listProjectAssetsMock.mockResolvedValue({ items: [], hasMore: false });
    listTagsMock.mockResolvedValue(["pla"]);

    projectGrid();
    await screen.findByText("This project has no files yet.");

    // A tag chip that matches nothing is an empty result, not an empty project.
    fireEvent.click(await screen.findByRole("button", { name: "pla" }));

    const filterBlock = await waitFor(() => {
      const found = document.querySelector<HTMLElement>("[data-project-empty-filter]");
      if (!found) throw new Error("project filtered-empty state not rendered");
      return found;
    });
    expect(
      within(filterBlock).getByText("No files in this project match this filter"),
    ).toBeTruthy();
    expect(
      within(filterBlock).getByText("Clear the search and the filters to see the whole project."),
    ).toBeTruthy();
    expect(document.querySelector("[data-project-empty]")).toBeNull();
    expect(screen.queryByText("This project has no files yet.")).toBeNull();
  });

  it("shows the failed fetch, not an empty project, when the listing fails", async () => {
    listProjectAssetsMock.mockRejectedValueOnce(new Error("boom"));

    projectGrid();

    const panel = await waitFor(() => {
      const found = document.querySelector<HTMLElement>("[data-grid-error]");
      if (!found) throw new Error("error panel not rendered");
      return found;
    });
    expect(within(panel).getByText("Could not load the files")).toBeTruthy();
    expect(within(panel).getByText("Failed to load assets.")).toBeTruthy();
    // A failed request is not an empty project: the empty copy must stay off.
    expect(document.querySelector("[data-project-empty]")).toBeNull();
    expect(screen.queryByText("This project has no files yet.")).toBeNull();
  });

  it("keeps using listAssets when no project is set", async () => {
    listAssetsMock.mockResolvedValue({ items: assets, hasMore: false });

    render(<AssetGrid canContribute canDelete theme="dark" />);

    await screen.findByText("Calibration cube");

    expect(listAssetsMock).toHaveBeenCalledTimes(1);
    expect(listProjectAssetsMock).not.toHaveBeenCalled();
    expect(listFoldersMock).toHaveBeenCalled();
  });
});

describe("AssetGrid project membership", () => {
  it("offers a contributor Remove from project and never the asset delete", async () => {
    renderProjectGrid({ canContribute: true, canDelete: true });
    await screen.findByText("Calibration cube");

    const menu = openCardMenu("a1");
    const group = within(menu).getByRole("group", { name: "Project" });
    expect(within(group).getByRole("menuitem", { name: "Remove from project" })).toBeTruthy();
    // Project membership is not ownership: no child may offer the destroy route.
    expect(within(menu).queryByRole("menuitem", { name: "Delete" })).toBeNull();
  });

  it("removes a member through removeProjectAssets only and keeps the asset in the library", async () => {
    const onProjectsChanged = vi.fn();
    removeProjectAssetsMock.mockResolvedValue({ removed: ["a1"], skipped: [], failed: [] });

    const view = renderProjectGrid({ onProjectsChanged });
    await screen.findByText("Calibration cube");

    fireEvent.click(
      within(openCardMenu("a1")).getByRole("menuitem", { name: "Remove from project" }),
    );
    const dialog = screen.getByRole("dialog", {
      name: 'Remove "Calibration cube" from this project?',
    });
    expect(dialog.textContent).toMatch(/stays in your library/i);
    expect(dialog.textContent).toMatch(/membership/i);

    fireEvent.click(within(dialog).getByRole("button", { name: "Remove from project" }));

    await waitFor(() => expect(removeProjectAssetsMock).toHaveBeenCalledWith("p1", ["a1"]));
    expect(deleteAssetMock).not.toHaveBeenCalled();
    expect(batchDeleteAssetsMock).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByText("Calibration cube")).toBeNull());
    expect(onProjectsChanged).toHaveBeenCalledTimes(1);

    view.unmount();
    // Client-side companion to the server byte-truth test
    // `test_remove_project_assets_removes_the_memberships_only`: the library
    // listing still has the file after the membership was unlinked.
    listAssetsMock.mockResolvedValue({ items: assets, hasMore: false });
    render(<AssetGrid canContribute canDelete theme="dark" />);
    expect(await screen.findByText("Calibration cube")).toBeTruthy();
  });

  it("bulk removal sends one call with both ids and no delete", async () => {
    const onProjectsChanged = vi.fn();
    removeProjectAssetsMock.mockResolvedValue({
      removed: ["a1", "b2"],
      skipped: [],
      failed: [],
    });

    renderProjectGrid({ onProjectsChanged });
    await screen.findByText("Calibration cube");

    for (const box of screen.getAllByRole("checkbox", { name: "Select" })) {
      fireEvent.click(box);
    }
    const bar = within(screen.getByTestId("bulk-action-bar"));
    fireEvent.click(bar.getByRole("button", { name: "Remove from project" }));

    const dialog = screen.getByRole("dialog", {
      name: "Remove 2 files from this project?",
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove from project" }));

    await waitFor(() => expect(removeProjectAssetsMock).toHaveBeenCalledTimes(1));
    const [projectId, ids] = removeProjectAssetsMock.mock.calls[0];
    expect(projectId).toBe("p1");
    expect([...ids].sort()).toEqual(["a1", "b2"]);
    expect(deleteAssetMock).not.toHaveBeenCalled();
    expect(batchDeleteAssetsMock).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByText("Calibration cube")).toBeNull());
    expect(screen.queryByText("Spool holder")).toBeNull();
    expect(onProjectsChanged).toHaveBeenCalledTimes(1);
  });

  it("shows neither Remove nor Delete to a viewer", async () => {
    renderProjectGrid({ canContribute: false, canDelete: false });
    await screen.findByText("Calibration cube");

    const menu = openCardMenu("a1");
    expect(within(menu).queryByRole("menuitem", { name: "Remove from project" })).toBeNull();
    fireEvent.keyDown(menu, { key: "Escape" });
    expect(screen.queryByRole("button", { name: "Remove from project" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();

    // Ported sweep: nothing a viewer can click may reach a write route.
    const controls = screen.getAllByRole("button");
    expect(controls.length).toBeGreaterThan(0);
    for (const control of controls) fireEvent.click(control);
    await waitFor(() => expectNoWriteCalls());
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows no delete affordance to an editor or admin", async () => {
    // App maps both editor and admin to `canDelete`, so this covers both.
    renderProjectGrid({ canContribute: true, canDelete: true });
    await screen.findByText("Calibration cube");

    const menu = openCardMenu("a1");
    expect(within(menu).queryByRole("menuitem", { name: /delete/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /delete/i })).toBeNull();
    fireEvent.keyDown(menu, { key: "Escape" });

    fireEvent.click(screen.getAllByRole("checkbox", { name: "Select" })[0]);
    const bar = within(screen.getByTestId("bulk-action-bar"));
    expect(bar.getByRole("button", { name: "Remove from project" })).toBeTruthy();
    expect(bar.queryByRole("button", { name: "Delete" })).toBeNull();
  });

  it("keeps the flat grid virtualised for a large project", async () => {
    const total = 200;
    listProjectAssetsMock.mockResolvedValue({
      items: Array.from({ length: total }, (_, index) => makeSyntheticAsset(index + 1)),
      hasMore: false,
    });
    const scrollElementRef = createRef<HTMLDivElement>();

    render(
      <div ref={scrollElementRef} data-testid="project-scroller">
        <AssetGrid
          canContribute
          canDelete
          theme="dark"
          projectId="p1"
          projectName="Hydra"
          scrollElementRef={scrollElementRef}
        />
      </div>,
    );
    await screen.findByText("Asset 1");

    const scroller = scrollElementRef.current;
    if (!scroller) throw new Error("scroll harness did not mount");
    Object.defineProperty(scroller, "clientHeight", { value: 768, configurable: true });
    vi.spyOn(scroller, "getBoundingClientRect").mockReturnValue(rectOf(0, 768));

    const renderedAtTop = document.querySelectorAll("[data-asset-id]").length;
    expect(renderedAtTop).toBeLessThan(total);
    expect(renderedAtTop).toBeLessThan(60);
    const bottomSpacer = document.querySelector<HTMLElement>('[data-virtual-spacer="bottom"]');
    expect(Number.parseFloat(bottomSpacer?.style.height ?? "0")).toBeGreaterThan(0);

    // Scroll into the middle: the top spacer becomes real too, and the DOM
    // stays bounded rather than rendering all 200 cards.
    const gridWrap = document.querySelector<HTMLElement>("[data-virtual-grid]");
    if (!gridWrap) throw new Error("virtual grid wrapper not rendered");
    vi.spyOn(gridWrap, "getBoundingClientRect").mockImplementation(() =>
      rectOf(-scroller.scrollTop, 0),
    );
    scroller.scrollTop = 12500;
    fireEvent.scroll(scroller);

    await waitFor(() => expect(screen.queryByText("Asset 1")).toBeNull());
    const topSpacer = document.querySelector<HTMLElement>('[data-virtual-spacer="top"]');
    expect(Number.parseFloat(topSpacer?.style.height ?? "0")).toBeGreaterThan(0);
    expect(document.querySelectorAll("[data-asset-id]").length).toBeLessThan(total);
  });

  it("renders the final member rows at the bottom of a 200-member project (Mf-d8vc)", async () => {
    const total = 200;
    listProjectAssetsMock.mockResolvedValue({
      items: Array.from({ length: total }, (_, index) => makeSyntheticAsset(index + 1)),
      hasMore: false,
    });
    const scrollElementRef = createRef<HTMLDivElement>();

    render(
      <div ref={scrollElementRef} data-testid="project-scroller">
        <AssetGrid
          canContribute
          canDelete
          theme="dark"
          projectId="p1"
          projectName="Hydra"
          scrollElementRef={scrollElementRef}
        />
      </div>,
    );
    await screen.findByText("Asset 1");

    const scroller = scrollElementRef.current;
    if (!scroller) throw new Error("scroll harness did not mount");
    Object.defineProperty(scroller, "clientHeight", { value: 768, configurable: true });
    vi.spyOn(scroller, "getBoundingClientRect").mockReturnValue(rectOf(0, 768));
    const gridWrap = document.querySelector<HTMLElement>("[data-virtual-grid]");
    if (!gridWrap) throw new Error("virtual grid wrapper not rendered");
    vi.spyOn(gridWrap, "getBoundingClientRect").mockImplementation(() =>
      rectOf(-scroller.scrollTop, 0),
    );

    const rowCount = Math.ceil(total / colsForWidth(window.innerWidth));
    const lastRow = rowCount - 1;
    // The scroll height the shipped geometry implies: one row height per row
    // plus one gap between neighbours.
    scroller.scrollTop = rowCount * ESTIMATED_ROW_HEIGHT + lastRow * GRID_GAP_PX;
    fireEvent.scroll(scroller);

    // The listener is on `document` capture and the offset math follows the
    // live scroller, so the very bottom of the project is reachable.
    await waitFor(() =>
      expect(document.querySelector('[data-asset-id="asset-200"]')).toBeTruthy(),
    );
    const rows = Array.from(document.querySelectorAll("[data-row-index]")).map(element =>
      Number(element.getAttribute("data-row-index")),
    );
    expect(rows[0]).toBeGreaterThan(0);
    expect(lastRow - rows[0]).toBeLessThan(6);
    expect(rows[rows.length - 1]).toBe(lastRow);
  });

  it("reverses a failed unlink, keeping the row and reporting it", async () => {
    removeProjectAssetsMock.mockResolvedValue({
      removed: [],
      skipped: [],
      failed: [{ id: "a1", code: "error", status: 500, error: "nope" }],
    });

    renderProjectGrid();
    await screen.findByText("Calibration cube");

    fireEvent.click(
      within(openCardMenu("a1")).getByRole("menuitem", { name: "Remove from project" }),
    );
    const dialog = screen.getByRole("dialog", {
      name: 'Remove "Calibration cube" from this project?',
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove from project" }));

    await waitFor(() => expect(removeProjectAssetsMock).toHaveBeenCalledTimes(1));
    // The failed membership is still there, so the row must come back.
    expect(screen.getByText("Calibration cube")).toBeTruthy();
    expect(getToasts().some(toast => /nope/.test(toast.message))).toBe(true);
  });

  it("keeps the survivors of a partially failed bulk unlink selected for a retry", async () => {
    // A per-asset partial outcome is legal: the server unlinked one membership
    // and rejected the other, so one row really left and one came back.
    removeProjectAssetsMock.mockResolvedValue({
      removed: ["a1"],
      skipped: [],
      failed: [{ id: "b2", code: "error", status: 500, error: "lock timeout" }],
    });

    renderProjectGrid();
    await screen.findByText("Calibration cube");

    fireEvent.click(checkboxFor("a1"));
    fireEvent.click(checkboxFor("b2"));

    fireEvent.click(
      within(screen.getByTestId("bulk-action-bar")).getByRole("button", {
        name: "Remove from project",
      }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "Remove 2 files from this project?",
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove from project" }));

    await waitFor(() => expect(removeProjectAssetsMock).toHaveBeenCalledTimes(1));
    const [projectId, ids] = removeProjectAssetsMock.mock.calls[0];
    expect(projectId).toBe("p1");
    expect([...ids].sort()).toEqual(["a1", "b2"]);

    await waitFor(() => expect(screen.queryByText("Calibration cube")).toBeNull());
    expect(screen.getByText("Spool holder")).toBeTruthy();

    // The retry selection is the point of this test. A survivor restored by a
    // per-asset failure stays ticked, because the bar's failure report is only
    // useful if the user can retry exactly the rows that did not move.
    expect(checkboxFor("b2").checked).toBe(true);
    const retryBar = within(screen.getByTestId("bulk-action-bar"));
    expect(retryBar.getByText("1 selected")).toBeTruthy();

    // And the bar acts on exactly those survivors: the retry is one single
    // membership, so it takes the single-asset wording and call shape.
    fireEvent.click(retryBar.getByRole("button", { name: "Remove from project" }));
    const retryDialog = screen.getByRole("dialog", {
      name: 'Remove "Spool holder" from this project?',
    });
    fireEvent.click(within(retryDialog).getByRole("button", { name: "Remove from project" }));

    await waitFor(() => expect(removeProjectAssetsMock).toHaveBeenCalledTimes(2));
    const [retryProjectId, retryIds] = removeProjectAssetsMock.mock.calls[1];
    expect(retryProjectId).toBe("p1");
    expect([...retryIds]).toEqual(["b2"]);

    // Membership removal is never deletion, whatever the outcome.
    expect(deleteAssetMock).not.toHaveBeenCalled();
    expect(batchDeleteAssetsMock).not.toHaveBeenCalled();
  });

  it("reaches Remove from project from the card menu with the keyboard", async () => {
    renderProjectGrid({ canContribute: true, canDelete: true });
    await screen.findByText("Calibration cube");

    // Keyboard path: the trigger opens the menu, which focuses its first enabled
    // item; arrows walk it. jsdom does not synthesise a browser's Enter->click
    // activation on a focused native button, so the choice is driven by clicking
    // the item the keyboard actually focused.
    const trigger = within(cardFor("a1")).getByRole("button", { name: "More actions" });
    trigger.focus();
    expect(document.activeElement).toBe(trigger);
    fireEvent.click(trigger);

    const menu = screen.getByRole("menu");
    expect((document.activeElement as HTMLElement).getAttribute("role")).toBe("menuitem");

    // Removing a membership is not the first entry, so the walk has to cross
    // the download and edit sections to reach it. The menu's own item count is
    // the bound: a menu that lost the entry fails the assertion below instead
    // of spinning the loop forever.
    const itemCount = within(menu).getAllByRole("menuitem").length;
    for (
      let step = 0;
      step < itemCount && document.activeElement?.textContent !== "Remove from project";
      step += 1
    ) {
      fireEvent.keyDown(menu, { key: "ArrowDown" });
    }
    const focused = document.activeElement as HTMLElement;
    expect(focused.textContent).toBe("Remove from project");

    // It sits in the Project section, not in the per-file actions above it.
    const group = within(menu).getByRole("group", { name: "Project" });
    expect(group.contains(focused)).toBe(true);

    fireEvent.click(focused);
    expect(
      screen.getByRole("dialog", { name: 'Remove "Calibration cube" from this project?' }),
    ).toBeTruthy();
    expect(deleteAssetMock).not.toHaveBeenCalled();
  });

  it("selects all loaded members and clears them again", async () => {
    renderProjectGrid();
    await screen.findByText("Calibration cube");

    // The bar only renders with a non-empty selection, so surface it with one
    // card first; Select all then fills in the rest of the loaded window.
    fireEvent.click(checkboxFor("a1"));
    const bar = within(screen.getByTestId("bulk-action-bar"));
    fireEvent.click(bar.getByRole("button", { name: "Select all" }));

    await waitFor(() => expect(bar.getByText("2 selected")).toBeTruthy());
    expect(checkboxFor("a1").checked).toBe(true);
    expect(checkboxFor("b2").checked).toBe(true);

    fireEvent.click(bar.getByRole("button", { name: "Clear" }));

    await waitFor(() => expect(screen.queryByTestId("bulk-action-bar")).toBeNull());
    expect(checkboxFor("a1").checked).toBe(false);
    expect(checkboxFor("b2").checked).toBe(false);

    // Selection is pure UI state: no membership or delete route may be touched.
    expectNoWriteCalls();
  });
});
