// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within, type RenderResult } from "@testing-library/react";
import { createRef } from "react";

import type { Asset, Folder, Project } from "../lib/api";

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
  createProject,
  deleteAsset,
  listAssets,
  listCreators,
  listFolders,
  listProjectAssets,
  listProjects,
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
const createProjectMock = vi.mocked(createProject);
const listProjectsMock = vi.mocked(listProjects);
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

/** The measured Stagetop shape: a project folder, one wave, and Wave 1.5. */
const stagetopFolders: Folder[] = [
  { id: "f-top", name: "Stagetop", tags: [] },
  { id: "f-wave1", name: "StageTop Wave One - Personal Use", tags: [], parent_id: "f-top" },
  { id: "f-core", name: "Core", tags: [], parent_id: "f-wave1" },
  { id: "f-acc", name: "Accessories", tags: [], parent_id: "f-wave1" },
  { id: "f-legs", name: "Legs", tags: [], parent_id: "f-wave1" },
  { id: "f-play", name: "PlayTiles", tags: [], parent_id: "f-wave1" },
  { id: "f-wave15", name: "Wave 1.5", tags: [], parent_id: "f-top" },
];

/** One project member filed in `folderId`, with a stable, searchable id. */
function memberIn(folderId: string, index: number): Asset {
  const id = `${folderId}-${index}`;
  return {
    id,
    name: `${folderId} part ${index}`,
    filename: `${id}.stl`,
    mime: "model/stl",
    size: 1000,
    tags: [],
    url: `/api/assets/${id}/file`,
    thumb_url: `/api/assets/${id}/thumb`,
    supporting_file_count: 0,
    folder_id: folderId,
  };
}

/** 60 parts: Core 12, Accessories 9, Legs 6, PlayTiles 18, Wave 1.5 15. */
function stagetopMembers(): Asset[] {
  return [
    ...Array.from({ length: 12 }, (_, index) => memberIn("f-core", index + 1)),
    ...Array.from({ length: 9 }, (_, index) => memberIn("f-acc", index + 1)),
    ...Array.from({ length: 6 }, (_, index) => memberIn("f-legs", index + 1)),
    ...Array.from({ length: 18 }, (_, index) => memberIn("f-play", index + 1)),
    ...Array.from({ length: 15 }, (_, index) => memberIn("f-wave15", index + 1)),
  ];
}

/** Mounts the grid over the Stagetop shape: folders and 60 members loaded. */
function renderStagetopProject(props: ProjectGridProps = {}) {
  listFoldersMock.mockResolvedValue(stagetopFolders);
  listProjectAssetsMock.mockResolvedValue({ items: stagetopMembers(), hasMore: false });
  return renderProjectGrid(props);
}

/** The toolbar's sort select (the only combobox offering a `size` option). */
function sortSelect(): HTMLSelectElement {
  const select = screen
    .getAllByRole("combobox")
    .find(element =>
      Array.from((element as HTMLSelectElement).options).some(option => option.value === "size"),
    );
  if (!select) throw new Error("sort select not found");
  return select as HTMLSelectElement;
}

/** The project section with this exact aria-label, or null. */
function projectSection(label: string): HTMLElement | null {
  return (
    Array.from(document.querySelectorAll<HTMLElement>("[data-project-section]")).find(
      section => section.getAttribute("aria-label") === label,
    ) ?? null
  );
}

/** The asset ids of the cards rendered inside one section, in DOM order. */
function sectionIds(section: HTMLElement): string[] {
  return Array.from(section.querySelectorAll<HTMLElement>("[data-asset-id]")).map(
    element => element.dataset.assetId as string,
  );
}

/** The Stagetop shape with the Core members loaded in a deliberately reversed order. */
function stagetopMembersReversedCore(): Asset[] {
  return [
    ...Array.from({ length: 12 }, (_, index) => memberIn("f-core", 12 - index)),
    ...Array.from({ length: 9 }, (_, index) => memberIn("f-acc", index + 1)),
    ...Array.from({ length: 6 }, (_, index) => memberIn("f-legs", index + 1)),
    ...Array.from({ length: 18 }, (_, index) => memberIn("f-play", index + 1)),
    ...Array.from({ length: 15 }, (_, index) => memberIn("f-wave15", index + 1)),
  ];
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
  it("lists project assets through listProjectAssets and reads the folder tree for sectioning", async () => {
    projectGrid();

    await screen.findByText("Calibration cube");

    expect(listProjectAssetsMock).toHaveBeenCalledTimes(1);
    expect(listProjectAssetsMock.mock.calls[0][0]).toMatchObject({
      projectId: "p1",
      q: "",
      tags: [],
    });
    expect(listAssetsMock).not.toHaveBeenCalled();
    // Mf-aaqi reverses the old "never touches folders" invariant on purpose:
    // project mode reads the tree to section the members, and the tree never
    // scopes the listing (membership does).
    expect(listFoldersMock).toHaveBeenCalled();
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

  it("sections a project's parts by folder and filters by wave", async () => {
    renderStagetopProject();

    await waitFor(() =>
      expect(document.querySelectorAll("[data-project-section]")).toHaveLength(5),
    );
    // All 60 parts arrive in ONE page: the sections count the loaded rows, so
    // a screenful-sized first page would under-report every section count.
    expect(listProjectAssetsMock.mock.calls[0][0]).toMatchObject({ limit: 1000 });
    // And all 60 render, because every section fits under the 24-card cap.
    expect(document.querySelectorAll("[data-asset-id]")).toHaveLength(60);

    const sections = Array.from(document.querySelectorAll<HTMLElement>("[data-project-section]"));
    const summary = sections.map(section => ({
      label: section.getAttribute("aria-label"),
      count: section.querySelector("[data-section-count]")?.textContent,
    }));
    expect(summary).toEqual([
      { label: "StageTop Wave One - Personal Use . Accessories", count: "9" },
      { label: "StageTop Wave One - Personal Use . Core", count: "12" },
      { label: "StageTop Wave One - Personal Use . Legs", count: "6" },
      { label: "StageTop Wave One - Personal Use . PlayTiles", count: "18" },
      { label: "Wave 1.5", count: "15" },
    ]);

    const listingCalls = listProjectAssetsMock.mock.calls.length;
    fireEvent.click(screen.getByRole("radio", { name: "Wave 1.5 15" }));

    expect(document.querySelectorAll("[data-asset-id]")).toHaveLength(15);
    // Filtering is a predicate over the loaded members: no refetch, no request.
    expect(listProjectAssetsMock).toHaveBeenCalledTimes(listingCalls);
    expect(screen.getByText("Showing 15 of 60 parts in Wave 1.5")).toBeTruthy();
  });

  it("announces a partially loaded project as loaded and pages by the 1000-part page size", async () => {
    const loaded = stagetopMembers();
    const extra = memberIn("f-core", 13);
    listFoldersMock.mockResolvedValue(stagetopFolders);
    listProjectAssetsMock.mockResolvedValueOnce({ items: loaded, hasMore: true });
    listProjectAssetsMock.mockResolvedValueOnce({ items: [extra], hasMore: false });

    renderProjectGrid();

    await waitFor(() =>
      expect(document.querySelectorAll("[data-project-section]")).toHaveLength(5),
    );

    // The counts are the loaded page, and the live line says so rather than
    // presenting the loaded total as the whole project.
    const live = document.querySelector<HTMLElement>("[data-project-visible-count]");
    expect(live?.textContent).toBe("Showing 60 of 60 loaded parts - more to load");

    fireEvent.click(screen.getByRole("button", { name: "Load more" }));

    await waitFor(() => expect(listProjectAssetsMock).toHaveBeenCalledTimes(2));
    // Pins the loadMore page size: it must ask for the same 1000-part page as
    // the first load, not a screenful.
    expect(listProjectAssetsMock.mock.calls[1][0]).toMatchObject({
      projectId: "p1",
      limit: 1000,
      offset: 60,
    });

    // The appended part renders inside its own section, and the fully loaded
    // project drops the "loaded ... more to load" qualifier.
    await waitFor(() => {
      const core = projectSection("StageTop Wave One - Personal Use . Core");
      expect(core).toBeTruthy();
      expect(core?.querySelector('[data-asset-id="f-core-13"]')).toBeTruthy();
    });
    await waitFor(() =>
      expect(
        document.querySelector<HTMLElement>("[data-project-visible-count]")?.textContent,
      ).toBe("Showing 61 of 61 parts"),
    );
  });

  it("keeps the toolbar's sort order inside a section", async () => {
    const loaded = stagetopMembersReversedCore();
    listFoldersMock.mockResolvedValue(stagetopFolders);
    listProjectAssetsMock.mockResolvedValue({ items: loaded, hasMore: false });

    renderProjectGrid();

    const coreLabel = "StageTop Wave One - Personal Use . Core";
    await waitFor(() => expect(projectSection(coreLabel)).toBeTruthy());

    const loadedCoreIds = loaded
      .filter(item => item.folder_id === "f-core")
      .map(item => item.id);
    const initial = projectSection(coreLabel);
    if (!initial) throw new Error("Core section not rendered");
    // The DOM follows the loaded (server) order, not the alphabetical filename
    // order the bucket itself carries.
    expect(sectionIds(initial)).toEqual(loadedCoreIds);
    expect(sectionIds(initial)).not.toEqual(
      [...loadedCoreIds].sort((a, b) => a.localeCompare(b)),
    );

    const reversed = [...loaded].reverse();
    listProjectAssetsMock.mockResolvedValueOnce({ items: reversed, hasMore: false });
    fireEvent.change(sortSelect(), { target: { value: "size" } });

    await waitFor(() =>
      expect(listProjectAssetsMock).toHaveBeenLastCalledWith(
        expect.objectContaining({ sort: "size" }),
      ),
    );
    await waitFor(() => {
      const updated = projectSection(coreLabel);
      if (!updated) throw new Error("Core section disappeared");
      expect(sectionIds(updated)).toEqual(
        reversed.filter(item => item.folder_id === "f-core").map(item => item.id),
      );
    });
  });

  it("keeps the File type sort in a project on the grouped type branch", async () => {
    renderStagetopProject();

    await waitFor(() =>
      expect(document.querySelectorAll("[data-project-section]")).toHaveLength(5),
    );

    fireEvent.change(sortSelect(), { target: { value: "type" } });

    await waitFor(() => expect(document.querySelector("[data-project-section]")).toBeNull());
    // The sectioned view (and its live count line) is gone: the type guard held.
    expect(document.querySelector("[data-project-visible-count]")).toBeNull();
    expect(document.querySelector("[data-project-sections]")).toBeNull();
    // The grouped File type branch renders instead, under its own STL heading.
    expect(await screen.findByText("STL")).toBeTruthy();
  });

  it("card checkboxes still drive the bulk bar in the sectioned view", async () => {
    renderStagetopProject({ canContribute: true, canDelete: false });

    await waitFor(() =>
      expect(document.querySelectorAll("[data-project-section]")).toHaveLength(5),
    );

    fireEvent.click(checkboxFor("f-core-1"));

    const bar = within(screen.getByTestId("bulk-action-bar"));
    expect(bar.getByRole("button", { name: "Remove from project" })).toBeTruthy();
    expect(bar.getByText("1 selected")).toBeTruthy();
    expect(bar.queryByRole("button", { name: "Delete" })).toBeNull();
  });

  it("still renders every member of a folder-less project (unchanged flat grid)", async () => {
    // The deliberately-unresolved degenerate case: a folder-less project keeps
    // today's grid with no header and no filter row. The literal single-header
    // alternative is a later decision, not a guess here.
    listFoldersMock.mockResolvedValue([]);
    listProjectAssetsMock.mockResolvedValue({
      items: Array.from({ length: 3 }, (_, index) => makeSyntheticAsset(index + 1)),
      hasMore: false,
    });

    renderProjectGrid();

    expect(await screen.findByText("Asset 3")).toBeTruthy();
    expect(screen.getByText("Asset 1")).toBeTruthy();
    expect(document.querySelectorAll("[data-asset-id]")).toHaveLength(3);
    expect(document.querySelector("[data-project-section]")).toBeNull();
    expect(screen.queryByRole("radiogroup", { name: "Filter by folder" })).toBeNull();
  });

  it("falls back to the flat grid when the project's folder tree cannot be read", async () => {
    // `handleApiError` reports the failure through console.error; keep the
    // output clean while still letting the toast assertion see the report.
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      // A failed tree read must degrade presentation only: the members are
      // still listed (membership, not the tree, scopes the listing), but the
      // model sees no folder paths, so nothing may section.
      listFoldersMock.mockRejectedValueOnce(new Error("folders down"));
      listProjectAssetsMock.mockResolvedValue({ items: stagetopMembers(), hasMore: false });

      renderProjectGrid();

      await screen.findByText("f-core part 1");
      await waitFor(() =>
        expect(
          getToasts().some(toast => toast.message === "Failed to load folders. Please refresh."),
        ).toBe(true),
      );

      expect(document.querySelector("[data-project-section]")).toBeNull();
      expect(screen.queryByRole("radiogroup", { name: "Filter by folder" })).toBeNull();
      // The unchanged flat branch still renders the loaded members.
      expect(document.querySelector("[data-virtual-grid]")).not.toBeNull();
    } finally {
      consoleError.mockRestore();
    }
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

describe("AssetGrid project picker hand-off", () => {
  /**
   * The middle link of App → AssetGrid → ProjectPickerDialog. Both project-list
   * callbacks are mounted here: `onProjectsChanged` is the bump the grid has always
   * forwarded, `onProjectCreated` is the append threaded straight through to the
   * dialog without the grid holding an opinion about the row.
   */
  function renderHandoffGrid(handoff: {
    onProjectCreated: (project: Project) => void;
    onProjectsChanged: () => void;
  }) {
    return render(
      <AssetGrid
        canContribute
        canDelete
        theme="dark"
        projectId="p1"
        projectName="Hydra"
        onProjectsChanged={handoff.onProjectsChanged}
        onProjectCreated={handoff.onProjectCreated}
      />,
    );
  }

  /** What `createProject` resolves. No field here is something the client could fill in. */
  function createdServerRow(): Project {
    return {
      id: "p9",
      name: "Fresh",
      description: null,
      creator: null,
      source_url: null,
      licence: null,
      licence_url: null,
      origin: "own",
      cover_asset_id: null,
      cover_thumb_url: "/thumbs/fresh.jpg?v=9",
      asset_count: 7,
      metadata_json: {},
      total_size: 0,
      created_at: "2026-01-03T00:00:00Z",
      updated_at: "2026-01-03T00:00:00Z",
    };
  }

  /**
   * Select both loaded members and reach the picker through the card kebab — the
   * sequence `AssetGrid.projectPicker.test.tsx` drives.
   */
  async function openPickerFromCard(utils: RenderResult): Promise<HTMLElement> {
    const boxes = screen.getAllByRole("checkbox", { name: "Select" });
    expect(boxes).toHaveLength(2);
    boxes.forEach(box => fireEvent.click(box));
    return openPickerFromMenu(utils);
  }

  /** The kebab half of the sequence, for a second open over an existing selection. */
  async function openPickerFromMenu(utils: RenderResult): Promise<HTMLElement> {
    fireEvent.click(within(cardFor("a1")).getByRole("button", { name: "More actions" }));
    // One card menu is open, so this cannot be the bulk bar's button of the same name.
    fireEvent.click(screen.getByRole("menuitem", { name: "Add to project…" }));
    return utils.findByRole("dialog", { name: "Add to project" });
  }

  /** Types a name into the inline create field and submits the create-and-add form. */
  function createAndAdd(dialog: HTMLElement, name: string): void {
    fireEvent.change(within(dialog).getByRole("textbox", { name: "New project name" }), {
      target: { value: name },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create and add" }));
  }

  /** A `listProjects` that never answers, so no refetch can be the source of a row. */
  function leaveTheListUnresolved(): void {
    listProjectsMock.mockReturnValue(new Promise<Project[]>(() => {}));
  }

  it("threads the row the picker created straight to the caller, with no list to refetch from", async () => {
    const createdRow = createdServerRow();
    leaveTheListUnresolved();
    createProjectMock.mockResolvedValue(createdRow);
    addProjectAssetsMock.mockResolvedValue({ added: ["a1", "b2"], skipped: [], failed: [] });
    const onProjectCreated = vi.fn();
    const onProjectsChanged = vi.fn();
    const utils = renderHandoffGrid({ onProjectCreated, onProjectsChanged });

    await screen.findByText("Calibration cube");
    const dialog = await openPickerFromCard(utils);

    // The dialog asked for the list and is still waiting: its own list is pending,
    // so the row below cannot have arrived through a render of fetched projects.
    expect(listProjectsMock).toHaveBeenCalledTimes(1);
    expect(within(dialog).getByText("Loading projects…")).toBeTruthy();
    expect(within(dialog).queryByRole("radio", { name: "Hydra" })).toBeNull();

    createAndAdd(dialog, "Fresh");

    await waitFor(() => expect(onProjectCreated).toHaveBeenCalledTimes(1));
    const handed = onProjectCreated.mock.calls[0][0];
    // Identity: the grid handed over the object `createProject` resolved rather than
    // rebuilding one from the typed name on the way out.
    expect(handed).toBe(createdRow);
    expect(onProjectCreated).toHaveBeenCalledWith(createdRow);
    expect(handed.asset_count).toBe(7);
    expect(handed.cover_thumb_url).toBe("/thumbs/fresh.jpg?v=9");

    // The bump survived threading as well: App still gets its `assetsVersion` nudge
    // beside the append.
    expect(onProjectsChanged).toHaveBeenCalled();

    expect(createProjectMock).toHaveBeenCalledTimes(1);
    expect(createProjectMock).toHaveBeenCalledWith({ name: "Fresh" });
    expect(addProjectAssetsMock).toHaveBeenCalledTimes(1);
    expect(addProjectAssetsMock).toHaveBeenCalledWith("p9", ["a1", "b2"]);
  });

  it("reports the same row twice when the create path runs twice — the dedupe is App's", async () => {
    const createdRow = createdServerRow();
    leaveTheListUnresolved();
    // Both creates resolve to the very same row, which is what a retry or a second
    // "Add to project…" over the same selection hands back.
    createProjectMock.mockResolvedValue(createdRow);
    addProjectAssetsMock.mockResolvedValue({ added: ["a1", "b2"], skipped: [], failed: [] });
    const onProjectCreated = vi.fn();
    const onProjectsChanged = vi.fn();
    const utils = renderHandoffGrid({ onProjectCreated, onProjectsChanged });

    await screen.findByText("Calibration cube");

    // One grid, one threaded prop, two creates (the add closes the dialog each time).
    createAndAdd(await openPickerFromCard(utils), "Fresh");
    await waitFor(() => expect(onProjectCreated).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(utils.queryByRole("dialog")).toBeNull());

    createAndAdd(await openPickerFromMenu(utils), "Fresh");
    await waitFor(() => expect(onProjectCreated).toHaveBeenCalledTimes(2));

    // The grid forwards what it is given and adds no dedupe of its own. The id guard
    // lives in `App.handleProjectCreated`, and it is pinned there at App level.
    expect(onProjectCreated).toHaveBeenNthCalledWith(1, createdRow);
    expect(onProjectCreated).toHaveBeenNthCalledWith(2, createdRow);
    expect(onProjectCreated.mock.calls[0][0]).toBe(createdRow);
    expect(onProjectCreated.mock.calls[1][0]).toBe(createdRow);
    expect(onProjectsChanged).toHaveBeenCalled();
    expect(createProjectMock).toHaveBeenCalledTimes(2);
  });
});
