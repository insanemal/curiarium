import { describe, expect, it } from "vitest";

import type { Asset, Folder } from "../../lib/api";
import { folderPath } from "../../lib/folderTree";
import type { GroupBucket } from "./assetGridMeta";
import { TOP_LEVEL_TITLE, ALL_PARTS_TITLE, projectSectionModel } from "./projectSections";

/** The measured Stagetop shape: a project folder, a wave, and one more wave. */
const stagetopFolders: Folder[] = [
  { id: "f-top", name: "Stagetop", tags: [] },
  { id: "f-wave1", name: "StageTop Wave One - Personal Use", tags: [], parent_id: "f-top" },
  { id: "f-core", name: "Core", tags: [], parent_id: "f-wave1" },
  { id: "f-acc", name: "Accessories", tags: [], parent_id: "f-wave1" },
  { id: "f-legs", name: "Legs", tags: [], parent_id: "f-wave1" },
  { id: "f-play", name: "PlayTiles", tags: [], parent_id: "f-wave1" },
  { id: "f-wave15", name: "Wave 1.5", tags: [], parent_id: "f-top" },
];

let assetCounter = 0;

function makeAsset(folderId: string | null): Asset {
  assetCounter += 1;
  const id = `asset-${assetCounter}`;
  return {
    id,
    name: `Part ${assetCounter}`,
    filename: `part-${assetCounter}.stl`,
    mime: "model/stl",
    size: 1000,
    tags: [],
    url: `/api/assets/${id}/file`,
    thumb_url: `/api/assets/${id}/thumb`,
    supporting_file_count: 0,
    folder_id: folderId,
  };
}

/** A bucket the way `useAssetSorting` builds it: real `folderPath` output. */
function bucketFor(folders: Folder[], folderId: string | null, count: number): GroupBucket {
  const path = folderPath(folders, folderId);
  const title = path.length ? path.map(segment => segment.name).join(" / ") : "Unassigned";
  return {
    id: `folder:${folderId ?? "__ungrouped"}`,
    title,
    items: Array.from({ length: count }, () => makeAsset(folderId)),
    path: folderId ? path : undefined,
  };
}

function sectionTitles(model: ReturnType<typeof projectSectionModel>) {
  return model.groups.map(group => ({
    title: group.title,
    count: group.count,
    sections: group.sections.map(section => ({ title: section.title, count: section.items.length })),
  }));
}

describe("projectSectionModel", () => {
  it("trims the project root and splits Wave One into Core/Accessories/Legs/PlayTiles with counts", () => {
    const model = projectSectionModel([
      bucketFor(stagetopFolders, "f-core", 12),
      bucketFor(stagetopFolders, "f-acc", 9),
      bucketFor(stagetopFolders, "f-legs", 6),
      bucketFor(stagetopFolders, "f-play", 18),
      bucketFor(stagetopFolders, "f-wave15", 15),
    ]);

    expect(model.groups).toHaveLength(2);
    expect(sectionTitles(model)).toEqual([
      {
        title: "StageTop Wave One - Personal Use",
        count: 45,
        sections: [
          { title: "Accessories", count: 9 },
          { title: "Core", count: 12 },
          { title: "Legs", count: 6 },
          { title: "PlayTiles", count: 18 },
        ],
      },
      {
        title: "Wave 1.5",
        count: 15,
        // One remaining level below the trimmed root: the group IS the section.
        sections: [{ title: "", count: 15 }],
      },
    ]);
    expect(model.total).toBe(60);
    const sectionSum = model.groups
      .flatMap(group => group.sections)
      .reduce((sum, section) => sum + section.items.length, 0);
    expect(sectionSum).toBe(60);
  });

  it("a project with no folder assignments yields one group over every part", () => {
    const model = projectSectionModel([bucketFor(stagetopFolders, null, 7)]);

    expect(model.groups).toHaveLength(1);
    expect(model.groups[0].title).toBe(ALL_PARTS_TITLE);
    expect(model.groups[0].count).toBe(7);
    expect(model.groups[0].sections).toEqual([
      { key: "group:__root:__group", title: "", items: expect.any(Array) },
    ]);
    expect(model.total).toBe(7);
  });

  it("keeps a one-level-deep project's group as its own section", () => {
    const folders: Folder[] = [
      { id: "f-top", name: "Stagetop", tags: [] },
      { id: "f-a", name: "Wave One", tags: [], parent_id: "f-top" },
      { id: "f-b", name: "Wave Two", tags: [], parent_id: "f-top" },
    ];
    const model = projectSectionModel([
      bucketFor(folders, "f-a", 4),
      bucketFor(folders, "f-b", 5),
    ]);

    expect(sectionTitles(model)).toEqual([
      { title: "Wave One", count: 4, sections: [{ title: "", count: 4 }] },
      { title: "Wave Two", count: 5, sections: [{ title: "", count: 5 }] },
    ]);
  });

  it("puts the parts directly at the trimmed root under At the top level", () => {
    // Stagetop / StageTop Wave One - Personal Use / {Core, Accessories} trims to
    // the Wave One folder, so a part filed nowhere sits at the trimmed root.
    const model = projectSectionModel([
      bucketFor(stagetopFolders, "f-core", 3),
      bucketFor(stagetopFolders, "f-acc", 2),
      bucketFor(stagetopFolders, null, 4),
    ]);

    expect(sectionTitles(model)).toEqual([
      { title: "Accessories", count: 2, sections: [{ title: "", count: 2 }] },
      { title: TOP_LEVEL_TITLE, count: 4, sections: [{ title: "", count: 4 }] },
      { title: "Core", count: 3, sections: [{ title: "", count: 3 }] },
    ]);
    expect(model.total).toBe(9);
  });

  it("keeps two folders that share a name as two groups", () => {
    const folders: Folder[] = [
      { id: "f-top", name: "Stagetop", tags: [] },
      { id: "f-left", name: "Wave", tags: [], parent_id: "f-top" },
      { id: "f-right", name: "Wave", tags: [], parent_id: "f-top" },
    ];
    const model = projectSectionModel([
      bucketFor(folders, "f-left", 2),
      bucketFor(folders, "f-right", 3),
    ]);

    expect(model.groups).toHaveLength(2);
    expect(model.groups.map(group => group.id)).toEqual(["group:f-left", "group:f-right"]);
    expect(model.groups.map(group => group.title)).toEqual(["Wave", "Wave"]);
    expect(model.groups.map(group => group.count)).toEqual([2, 3]);
  });

  it("puts a bucket filed directly in the project root under At the top level", () => {
    // Stagetop / Wave One is the common prefix once Core and Accessories are in
    // the mix, so parts filed directly in Wave One sit at the trimmed root.
    const model = projectSectionModel([
      bucketFor(stagetopFolders, "f-wave1", 2),
      bucketFor(stagetopFolders, "f-core", 3),
      bucketFor(stagetopFolders, "f-acc", 4),
    ]);

    expect(sectionTitles(model)).toEqual([
      { title: "Accessories", count: 4, sections: [{ title: "", count: 4 }] },
      { title: TOP_LEVEL_TITLE, count: 2, sections: [{ title: "", count: 2 }] },
      { title: "Core", count: 3, sections: [{ title: "", count: 3 }] },
    ]);
  });

  it("keeps two sibling folders with the same name as two sections", () => {
    const folders: Folder[] = [
      { id: "f-top", name: "Stagetop", tags: [] },
      { id: "f-wave", name: "Wave", tags: [], parent_id: "f-top" },
      { id: "f-core-a", name: "Core", tags: [], parent_id: "f-wave" },
      { id: "f-core-b", name: "Core", tags: [], parent_id: "f-wave" },
      { id: "f-wave15", name: "Wave 1.5", tags: [], parent_id: "f-top" },
    ];
    const model = projectSectionModel([
      bucketFor(folders, "f-core-a", 2),
      bucketFor(folders, "f-core-b", 5),
      bucketFor(folders, "f-wave15", 1),
    ]);

    const wave = model.groups.find(group => group.title === "Wave");
    if (!wave) throw new Error("Wave group not found");
    expect(wave.sections).toHaveLength(2);
    expect(wave.sections.map(section => section.title)).toEqual(["Core", "Core"]);
    expect(wave.sections.map(section => section.items.length)).toEqual([2, 5]);
    // Different folder ids behind the identical heading is the point.
    expect(wave.sections[0].key).not.toBe(wave.sections[1].key);
  });

  it("keeps the loaded order inside a section when one is given", () => {
    // Wave 1.5 anchors the common prefix at Stagetop, so Wave One is the group
    // and Core is a section below it.
    const buckets = [
      bucketFor(stagetopFolders, "f-core", 3),
      bucketFor(stagetopFolders, "f-acc", 2),
      bucketFor(stagetopFolders, "f-wave15", 1),
    ];
    // The bucket's own order is filename order; this loaded order reverses it.
    const byFilename = buckets[0].items;
    const loaded = [...byFilename].reverse();
    const rest = buckets.slice(1).flatMap(bucket => bucket.items);

    const model = projectSectionModel(buckets, [...loaded, ...rest]);
    const core = model.groups
      .flatMap(group => group.sections)
      .find(section => section.title === "Core");
    if (!core) throw new Error("Core section not found");

    expect(core.items.map(item => item.id)).toEqual(loaded.map(item => item.id));
    // Without an order the section keeps the bucket's filename order.
    const unordered = projectSectionModel(buckets);
    const unorderedCore = unordered.groups
      .flatMap(group => group.sections)
      .find(section => section.title === "Core");
    expect(unorderedCore?.items.map(item => item.id)).toEqual(byFilename.map(item => item.id));
  });

  it("returns no groups for empty input", () => {
    expect(projectSectionModel([])).toEqual({ groups: [], total: 0 });
    expect(
      projectSectionModel([{ id: "folder:f-core", title: "Core", items: [], path: [] }]),
    ).toEqual({ groups: [], total: 0 });
  });
});
