// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { Asset } from "../../lib/api";
import { GROUP_CARD_CAP } from "./assetGridMeta";
import ProjectFolderSections from "./ProjectFolderSections";
import type { ProjectGroup } from "./projectSections";

type SectionSpec = { title: string; count: number };

let assetCounter = 0;

function makeAsset(name: string): Asset {
  assetCounter += 1;
  const id = `asset-${assetCounter}`;
  return {
    id,
    name,
    filename: `${name}.stl`,
    mime: "model/stl",
    size: 1000,
    tags: [],
    url: `/api/assets/${id}/file`,
    thumb_url: `/api/assets/${id}/thumb`,
    supporting_file_count: 0,
  };
}

/** A group shaped the way `projectSectionModel` produces one. */
function makeGroup(id: string, title: string, sections: SectionSpec[]): ProjectGroup {
  const built = sections.map(section => ({
    key: `${id}:${section.title || "__group"}`,
    title: section.title,
    items: Array.from({ length: section.count }, () => makeAsset(`${title}-part`)),
  }));
  return {
    id,
    title,
    count: built.reduce((sum, section) => sum + section.items.length, 0),
    sections: built,
  };
}

/** The measured Stagetop shape: two waves, five sections, 60 parts. */
function stagetopGroups(): ProjectGroup[] {
  return [
    makeGroup("group:f-wave1", "StageTop Wave One - Personal Use", [
      { title: "Accessories", count: 9 },
      { title: "Core", count: 12 },
      { title: "Legs", count: 6 },
      { title: "PlayTiles", count: 18 },
    ]),
    makeGroup("group:f-wave15", "Wave 1.5", [{ title: "", count: 15 }]),
  ];
}

function renderSections(groups: ProjectGroup[]) {
  const renderCard = vi.fn((item: Asset) => <div data-asset-id={item.id}>{item.name}</div>);
  const view = render(<ProjectFolderSections groups={groups} renderCard={renderCard} />);
  return { renderCard, view };
}

function renderedIds(): string[] {
  return Array.from(document.querySelectorAll<HTMLElement>("[data-asset-id]")).map(
    element => element.dataset.assetId as string,
  );
}

function radio(name: string): HTMLElement {
  return screen.getByRole("radio", { name });
}

function checkedRadios(): HTMLElement[] {
  return screen
    .getAllByRole("radio")
    .filter(element => element.getAttribute("aria-checked") === "true");
}

afterEach(cleanup);

describe("ProjectFolderSections", () => {
  it("renders each folder as a section carrying its part count", () => {
    renderSections(stagetopGroups());

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
      // One level: the group is the section, so its own title is the label.
      { label: "Wave 1.5", count: "15" },
    ]);
  });

  it("filters to one wave without refetching", () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    try {
      const groups = stagetopGroups();
      const { renderCard } = renderSections(groups);
      expect(renderedIds()).toHaveLength(60);
      expect(screen.getByText("Showing 60 of 60 parts")).toBeTruthy();

      const wave15 = groups[1];
      const waveIds = wave15.sections.flatMap(section => section.items.map(item => item.id));
      renderCard.mockClear();

      fireEvent.click(radio("Wave 1.5 15"));

      expect(renderedIds().sort()).toEqual([...waveIds].sort());
      expect(renderedIds()).toHaveLength(15);
      // The filter is a predicate over loaded items: only the new visible ones
      // are passed to the card renderer, and nothing was requested.
      const renderedByCard = new Set(renderCard.mock.calls.map(([item]) => (item as Asset).id));
      expect([...renderedByCard].sort()).toEqual([...waveIds].sort());
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(screen.getByText("Showing 15 of 60 parts in Wave 1.5")).toBeTruthy();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("lays each section out as a wrapping grid and announces the live count politely", () => {
    renderSections(stagetopGroups());

    const grids = Array.from(document.querySelectorAll<HTMLElement>("[data-section-grid]"));
    expect(grids).toHaveLength(5);
    for (const grid of grids) {
      // A wrapping grid, not the grouped branch's single horizontal strip.
      expect(grid.style.gridTemplateColumns).toContain("auto-fill");
      expect(grid.className).not.toContain("overflow-x-auto");
    }

    expect(
      document.querySelector("[data-project-visible-count]")?.getAttribute("aria-live"),
    ).toBe("polite");
  });

  it("a 45-part section discloses the cap and shows all 45 on demand", () => {
    renderSections([makeGroup("group:f-big", "Wave Big", [{ title: "", count: 45 }])]);

    expect(renderedIds()).toHaveLength(GROUP_CARD_CAP);
    const header = document.querySelector<HTMLElement>("[data-section-count]");
    expect(header?.textContent).toBe("45");

    fireEvent.click(screen.getByRole("button", { name: "Show all 45" }));
    expect(renderedIds()).toHaveLength(45);
    expect(screen.getByRole("button", { name: "Show fewer" })).toBeTruthy();
    expect(document.querySelector<HTMLElement>("[data-section-count]")?.textContent).toBe("45");

    fireEvent.click(screen.getByRole("button", { name: "Show fewer" }));
    expect(renderedIds()).toHaveLength(GROUP_CARD_CAP);
    expect(screen.getByRole("button", { name: "Show all 45" })).toBeTruthy();
  });

  it("section counts sum to the project total", () => {
    renderSections(stagetopGroups());

    expect(renderedIds()).toHaveLength(60);
    expect(screen.getByText("Showing 60 of 60 parts")).toBeTruthy();

    cleanup();
    // Two capped sections: the header still counts all of each, and the live
    // line still reports the project, never the rendered cards.
    renderSections([
      makeGroup("group:f-a", "Wave A", [{ title: "", count: 30 }]),
      makeGroup("group:f-b", "Wave B", [{ title: "", count: 30 }]),
    ]);

    expect(renderedIds()).toHaveLength(GROUP_CARD_CAP * 2);
    const counts = Array.from(document.querySelectorAll("[data-section-count]")).map(
      element => element.textContent,
    );
    expect(counts).toEqual(["30", "30"]);
    expect(screen.getByText("Showing 60 of 60 parts")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Show all 30" })).toHaveLength(2);
  });

  it("the filter row is one tab stop", () => {
    renderSections(stagetopGroups());

    const tabStops = screen
      .getAllByRole("radio")
      .filter(element => element.getAttribute("tabindex") === "0");
    expect(tabStops).toHaveLength(1);
    expect(tabStops[0].getAttribute("aria-checked")).toBe("true");
    expect(tabStops[0]).toBe(radio("All 60"));

    const group = screen.getByRole("radiogroup", { name: "Filter by folder" });

    fireEvent.keyDown(group, { key: "ArrowRight" });
    const first = radio("StageTop Wave One - Personal Use 45");
    expect(first.getAttribute("aria-checked")).toBe("true");
    expect(document.activeElement).toBe(first);
    expect(first.getAttribute("tabindex")).toBe("0");

    fireEvent.keyDown(group, { key: "ArrowRight" });
    const second = radio("Wave 1.5 15");
    expect(second.getAttribute("aria-checked")).toBe("true");
    expect(document.activeElement).toBe(second);
    expect(checkedRadios()).toEqual([second]);

    fireEvent.keyDown(group, { key: "End" });
    expect(document.activeElement).toBe(second);

    fireEvent.keyDown(group, { key: "Home" });
    const all = radio("All 60");
    expect(document.activeElement).toBe(all);
    expect(all.getAttribute("aria-checked")).toBe("true");
    expect(checkedRadios()).toEqual([all]);

    // Backwards keys and wrap-around are pinned too, so ArrowLeft/ArrowUp can
    // never be deleted without a red test.
    fireEvent.keyDown(group, { key: "ArrowLeft" });
    const last = radio("Wave 1.5 15");
    expect(document.activeElement).toBe(last);
    expect(last.getAttribute("aria-checked")).toBe("true");

    fireEvent.keyDown(group, { key: "ArrowUp" });
    expect(document.activeElement).toBe(radio("StageTop Wave One - Personal Use 45"));

    fireEvent.keyDown(group, { key: "ArrowUp" });
    expect(document.activeElement).toBe(radio("All 60"));

    // From the first chip one more backwards step wraps to the last.
    fireEvent.keyDown(group, { key: "ArrowUp" });
    expect(document.activeElement).toBe(last);
    expect(checkedRadios()).toEqual([last]);
  });

  it("falls back to All when the selected folder no longer exists", () => {
    const { view } = renderSections(stagetopGroups());
    fireEvent.click(radio("Wave 1.5 15"));
    expect(renderedIds()).toHaveLength(15);

    // A project switch: the stored id is gone, so the render resolves to All
    // instead of stranding a filter over parts it never described.
    view.rerender(
      <ProjectFolderSections
        groups={[makeGroup("group:f-other", "Wave Other", [{ title: "", count: 3 }])]}
        renderCard={item => <div data-asset-id={item.id}>{item.name}</div>}
      />,
    );

    expect(radio("All 3").getAttribute("aria-checked")).toBe("true");
    expect(renderedIds()).toHaveLength(3);
    expect(screen.getByText("Showing 3 of 3 parts")).toBeTruthy();
  });
});
