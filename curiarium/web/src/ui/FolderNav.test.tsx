// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

import type { Folder } from "../lib/api";
import FolderBreadcrumbs, { FolderTiles } from "./FolderNav";

const folders: Folder[] = [
  { id: "parts", name: "Parts", tags: [], storage_path: "Parts" },
  { id: "clamps", name: "Clamps", tags: [], parent_id: "parts", storage_path: "Parts/Clamps" },
  { id: "brackets", name: "Brackets", tags: [], parent_id: "parts", storage_path: "Parts/Brackets" },
  {
    id: "corners",
    name: "Corners",
    tags: [],
    parent_id: "brackets",
    storage_path: "Parts/Brackets/Corners",
  },
  { id: "elsewhere", name: "Elsewhere", tags: [], storage_path: "Elsewhere" },
];

function breadcrumbsNav(): HTMLElement {
  const nav = document.querySelector<HTMLElement>("[data-folder-breadcrumbs]");
  if (!nav) throw new Error("breadcrumb nav not rendered");
  return nav;
}

function folderTiles(): HTMLElement {
  const section = document.querySelector<HTMLElement>("[data-folder-tiles]");
  if (!section) throw new Error("folder tiles not rendered");
  return section;
}

afterEach(cleanup);

describe("FolderBreadcrumbs", () => {
  it("shows the All-files scope as one non-interactive current crumb", () => {
    const { container } = render(
      <FolderBreadcrumbs folders={folders} folderId={null} onNavigate={vi.fn()} />,
    );

    const nav = breadcrumbsNav();
    expect(nav.getAttribute("aria-label")).toBe("Files scope");
    // No button at all: the only crumb is the scope you are already in, so a
    // button here would navigate nowhere.
    expect(within(nav).queryAllByRole("button")).toEqual([]);
    const current = container.querySelector<HTMLElement>("[data-scope-current]");
    expect(current?.textContent).toBe("All files");
    expect(current?.getAttribute("aria-current")).toBe("page");
  });

  it("says what each scope contains, in a note outside the nav landmark", () => {
    const { container } = render(
      <FolderBreadcrumbs folders={folders} folderId={null} onNavigate={vi.fn()} />,
    );

    const nav = breadcrumbsNav();
    const note = container.querySelector<HTMLElement>("[data-scope-note]");
    expect(note?.textContent).toBe("Every file in the library, in every folder.");
    // The nav is a link landmark: the prose must not live inside it.
    expect(nav.querySelector("[data-scope-note]")).toBeNull();
    expect(nav.nextElementSibling).toBe(note);
  });

  it("names Unfiled as the current scope and offers All files as the way back", () => {
    const onNavigate = vi.fn();
    render(
      <FolderBreadcrumbs
        folders={folders}
        folderId={null}
        unfiled
        onNavigate={onNavigate}
        onSelectUnfiled={vi.fn()}
      />,
    );

    const nav = within(breadcrumbsNav());
    expect(nav.getAllByRole("button").map(button => button.textContent)).toEqual([
      "All files",
      "Unfiled",
    ]);
    expect(nav.getByRole("button", { name: "Unfiled" }).getAttribute("aria-current")).toBe(
      "page",
    );
    expect(document.querySelector("[data-scope-note]")?.textContent).toBe(
      "Files at the library root: no folder owns them.",
    );

    fireEvent.click(nav.getByRole("button", { name: "All files" }));
    expect(onNavigate).toHaveBeenLastCalledWith(null);
  });

  it("leaves the Unfiled crumb inert when its caller wires no unfiled selection", () => {
    const onNavigate = vi.fn();
    render(
      <FolderBreadcrumbs folders={folders} folderId={null} unfiled onNavigate={onNavigate} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Unfiled" }));
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("calls onSelectUnfiled from the current Unfiled crumb when a caller wires it", () => {
    const onSelectUnfiled = vi.fn();
    render(
      <FolderBreadcrumbs
        folders={folders}
        folderId={null}
        unfiled
        onNavigate={vi.fn()}
        onSelectUnfiled={onSelectUnfiled}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Unfiled" }));
    expect(onSelectUnfiled).toHaveBeenCalledTimes(1);
  });

  it("hides the trail while a selected folder's path cannot be resolved", () => {
    const { container } = render(
      <FolderBreadcrumbs folders={folders} folderId="ghost" onNavigate={vi.fn()} />,
    );
    expect(container.querySelector("[data-folder-breadcrumbs]")).toBeNull();
    expect(container.querySelector("[data-scope-note]")).toBeNull();
  });

  it("lists every ancestor in root → current order with aria-current on the last", () => {
    render(<FolderBreadcrumbs folders={folders} folderId="corners" onNavigate={vi.fn()} />);

    const nav = within(breadcrumbsNav());
    expect(nav.getAllByRole("button").map(button => button.textContent)).toEqual([
      "All files",
      "Parts",
      "Brackets",
      "Corners",
    ]);
    expect(nav.getByRole("button", { name: "All files" }).getAttribute("aria-current")).toBeNull();
    expect(
      nav.getByRole("button", { name: "Brackets" }).getAttribute("aria-current"),
    ).toBeNull();
    expect(nav.getByRole("button", { name: "Corners" }).getAttribute("aria-current")).toBe(
      "page",
    );
    // The current folder crumb stays a button: the caller navigates from it.
    expect(document.querySelector("[data-scope-note]")?.textContent).toBe(
      "Direct contents of Corners. Subfolders are tiles, not included here.",
    );
  });

  it("titles each crumb with the full path up to it", () => {
    render(<FolderBreadcrumbs folders={folders} folderId="corners" onNavigate={vi.fn()} />);
    expect(
      document.querySelector<HTMLElement>('[data-folder-crumb="corners"]')?.title,
    ).toBe("Parts / Brackets / Corners");
  });

  it("navigates to a middle segment and to the root", () => {
    const onNavigate = vi.fn();
    render(<FolderBreadcrumbs folders={folders} folderId="corners" onNavigate={onNavigate} />);

    fireEvent.click(screen.getByRole("button", { name: "Brackets" }));
    expect(onNavigate).toHaveBeenLastCalledWith("brackets");

    fireEvent.click(screen.getByRole("button", { name: "All files" }));
    expect(onNavigate).toHaveBeenLastCalledWith(null);
  });
});

describe("FolderTiles", () => {
  it("renders nothing at the root or for a leaf folder", () => {
    const root = render(
      <FolderTiles folders={folders} folderId={null} onNavigate={vi.fn()} />,
    );
    expect(root.container.querySelector("[data-folder-tiles]")).toBeNull();
    root.unmount();

    const leaf = render(
      <FolderTiles folders={folders} folderId="clamps" onNavigate={vi.fn()} />,
    );
    expect(leaf.container.querySelector("[data-folder-tiles]")).toBeNull();
  });

  it("shows exactly the direct children of the open folder", () => {
    render(<FolderTiles folders={folders} folderId="parts" onNavigate={vi.fn()} />);

    const tiles = within(folderTiles());
    expect(tiles.getByRole("heading", { name: "Subfolders" })).toBeTruthy();
    expect(
      tiles.getAllByRole("button").map(button => button.getAttribute("data-folder-tile")),
    ).toEqual(["brackets", "clamps"]);
    expect(tiles.getByRole("button", { name: /Brackets/ })).toBeTruthy();
    // Only Brackets itself has a child in the fixture.
    expect(tiles.getByRole("button", { name: /1 subfolder/ })).toBeTruthy();
    expect(tiles.queryByRole("button", { name: /Corners/ })).toBeNull();
    expect(tiles.queryByRole("button", { name: /Elsewhere/ })).toBeNull();
  });

  it("navigates on click, Enter and Space", () => {
    const onNavigate = vi.fn();
    render(<FolderTiles folders={folders} folderId="parts" onNavigate={onNavigate} />);

    fireEvent.click(screen.getByRole("button", { name: /Brackets/ }));
    expect(onNavigate).toHaveBeenLastCalledWith("brackets");

    const clamps = screen.getByRole("button", { name: /Clamps/ });
    fireEvent.keyDown(clamps, { key: "Enter" });
    expect(onNavigate).toHaveBeenLastCalledWith("clamps");

    onNavigate.mockClear();
    fireEvent.keyDown(clamps, { key: " " });
    expect(onNavigate).toHaveBeenLastCalledWith("clamps");

    // Unrelated keys must not navigate.
    onNavigate.mockClear();
    fireEvent.keyDown(clamps, { key: "a" });
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("flags a subfolder that is not linked to a directory", () => {
    // A migrated legacy row keeps a NULL storage path - and the API may hand
    // the empty string - because its assets cannot be pinned to one directory.
    const mixed: Folder[] = [
      { id: "parts", name: "Parts", tags: [], storage_path: "Parts" },
      { id: "clamps", name: "Clamps", tags: [], parent_id: "parts", storage_path: "Parts/Clamps" },
      { id: "legacy", name: "Legacy", tags: [], parent_id: "parts", storage_path: null },
      { id: "blank", name: "Blank", tags: [], parent_id: "parts", storage_path: "" },
    ];

    render(<FolderTiles folders={mixed} folderId="parts" onNavigate={vi.fn()} />);

    const tiles = within(folderTiles());

    function expectNoDirectory(tile: HTMLElement) {
      expect(tile.getAttribute("data-folder-tile-no-directory")).toBe("true");
      expect(tile.getAttribute("title")).toBe(
        "No directory on disk - files are grouped by metadata only",
      );
      const contents = within(tile);
      expect(contents.getByText("No directory")).toBeTruthy();
      // The note is screen-reader text, not a tooltip-only hint.
      expect(contents.getByText(/files are grouped by metadata only/)).toBeTruthy();
    }

    expectNoDirectory(tiles.getByRole("button", { name: /Legacy/ }));
    expectNoDirectory(tiles.getByRole("button", { name: /Blank/ }));

    // A linked child carries neither the attribute nor the badge.
    const linked = tiles.getByRole("button", { name: /Clamps/ });
    expect(linked.hasAttribute("data-folder-tile-no-directory")).toBe(false);
    expect(linked.getAttribute("title")).toBeNull();
    expect(within(linked).queryByText("No directory")).toBeNull();
  });
});
