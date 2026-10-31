import { describe, expect, it } from "vitest";

import type { Folder } from "./api";
import {
  ROOT_GROUP,
  childFolders,
  childrenIndex,
  descendantIds,
  flattenFolderTree,
  folderPath,
  indexFolders,
  searchFolders,
} from "./folderTree";

function folder(id: string, name: string, parent_id: string | null = null): Folder {
  return { id, name, tags: [], parent_id };
}

/** A row whose name the API handed back as null. */
function namelessFolder(id: string, parent_id: string | null = null): Folder {
  return { id, name: null as unknown as string, tags: [], parent_id };
}

/** Root → child → grandchild, plus an unrelated root and a middle sibling. */
const tree: Folder[] = [
  folder("parts", "Parts"),
  folder("brackets", "Brackets", "parts"),
  folder("corners", "Corners", "brackets"),
  folder("clamps", "Clamps", "parts"),
  folder("elsewhere", "Elsewhere"),
];

describe("indexFolders", () => {
  it("keys every folder by id", () => {
    const byId = indexFolders(tree);
    expect(Object.keys(byId).sort()).toEqual([
      "brackets",
      "clamps",
      "corners",
      "elsewhere",
      "parts",
    ]);
    expect(byId["corners"]).toBe(tree[2]);
  });
});

describe("folderPath", () => {
  it("returns root → leaf segments for a three-deep folder", () => {
    expect(folderPath(tree, "corners")).toEqual([
      { id: "parts", name: "Parts" },
      { id: "brackets", name: "Brackets" },
      { id: "corners", name: "Corners" },
    ]);
  });

  it("returns a single segment for a root folder", () => {
    expect(folderPath(tree, "parts")).toEqual([{ id: "parts", name: "Parts" }]);
  });

  it("returns [] for null and unknown ids", () => {
    expect(folderPath(tree, null)).toEqual([]);
    expect(folderPath(tree, undefined)).toEqual([]);
    expect(folderPath(tree, "nope")).toEqual([]);
  });

  it("falls back to \"Untitled\" for a blank name", () => {
    expect(folderPath([folder("blank", "")], "blank")).toEqual([
      { id: "blank", name: "Untitled" },
    ]);
  });

  it("is cycle-guarded: a parent loop terminates", () => {
    const looped: Folder[] = [
      { id: "a", name: "A", tags: [], parent_id: "b" },
      { id: "b", name: "B", tags: [], parent_id: "a" },
    ];
    expect(folderPath(looped, "a")).toEqual([
      { id: "b", name: "B" },
      { id: "a", name: "A" },
    ]);
  });

  it("stops at a missing parent instead of dropping the known tail", () => {
    const orphaned: Folder[] = [folder("child", "Child", "gone")];
    expect(folderPath(orphaned, "child")).toEqual([{ id: "child", name: "Child" }]);
  });
});

describe("childFolders", () => {
  it("returns direct children only, sorted by name", () => {
    expect(childFolders(tree, "parts").map(f => f.name)).toEqual(["Brackets", "Clamps"]);
  });

  it("never returns grandchildren or siblings", () => {
    const ids = childFolders(tree, "parts").map(f => f.id);
    expect(ids).not.toContain("corners");
    expect(ids).not.toContain("elsewhere");
  });

  it("treats a missing parent_id as a root", () => {
    expect(childFolders(tree, null).map(f => f.name)).toEqual(["Elsewhere", "Parts"]);
    expect(childFolders([{ id: "r", name: "Root", tags: [] }], null).map(f => f.id)).toEqual(["r"]);
  });

  it("returns [] for a leaf", () => {
    expect(childFolders(tree, "corners")).toEqual([]);
  });
});

describe("flattenFolderTree", () => {
  it("renders only the roots when nothing is expanded", () => {
    expect(flattenFolderTree(tree, { expanded: new Set() })).toEqual([
      { id: "elsewhere", depth: 0, hasChildren: false },
      { id: "parts", depth: 0, hasChildren: true },
    ]);
  });

  it("walks into expanded branches depth-first with increasing depth", () => {
    const rows = flattenFolderTree(tree, { expanded: new Set(["parts", "brackets"]) });
    expect(rows).toEqual([
      { id: "elsewhere", depth: 0, hasChildren: false },
      { id: "parts", depth: 0, hasChildren: true },
      { id: "brackets", depth: 1, hasChildren: true },
      { id: "corners", depth: 2, hasChildren: false },
      { id: "clamps", depth: 1, hasChildren: false },
    ]);
  });

  it("stops at a collapsed parent", () => {
    const rows = flattenFolderTree(tree, { expanded: new Set(["parts"]) });
    expect(rows.map(r => r.id)).toEqual(["elsewhere", "parts", "brackets", "clamps"]);
  });

  it("alwaysExpanded ignores the expanded set", () => {
    const rows = flattenFolderTree(tree, { expanded: new Set(), alwaysExpanded: true });
    expect(rows).toEqual([
      { id: "elsewhere", depth: 0, hasChildren: false },
      { id: "parts", depth: 0, hasChildren: true },
      { id: "brackets", depth: 1, hasChildren: true },
      { id: "corners", depth: 2, hasChildren: false },
      { id: "clamps", depth: 1, hasChildren: false },
    ]);
  });

  it("skips whole subtrees rejected by isVisible", () => {
    const rows = flattenFolderTree(tree, {
      expanded: new Set(["parts", "brackets"]),
      isVisible: id => id !== "brackets",
    });
    expect(rows.map(r => r.id)).toEqual(["elsewhere", "parts", "clamps"]);
  });

  it("is cycle-guarded", () => {
    const looped: Folder[] = [
      { id: "a", name: "A", tags: [], parent_id: "b" },
      { id: "b", name: "B", tags: [], parent_id: "a" },
      folder("root", "Root"),
      { id: "a2", name: "A2", tags: [], parent_id: "root" },
      { id: "b2", name: "B2", tags: [], parent_id: "a2" },
    ];
    const rows = flattenFolderTree(looped, { expanded: new Set(["root", "a2", "b2", "a", "b"]), alwaysExpanded: true });
    // Both cycle members are promoted to roots rather than dropped, so each is
    // rendered exactly once (roots sort by name: A, B, Root) and the walk ends.
    expect(rows).toEqual([
      { id: "a", depth: 0, hasChildren: false },
      { id: "b", depth: 0, hasChildren: false },
      { id: "root", depth: 0, hasChildren: true },
      { id: "a2", depth: 1, hasChildren: true },
      { id: "b2", depth: 2, hasChildren: false },
    ]);
  });
});

describe("corrupt parent data", () => {
  it("promotes orphans to roots and keeps their subtree reachable", () => {
    const orphaned: Folder[] = [
      folder("child", "Child", "gone"),
      folder("grandchild", "Grandchild", "child"),
      folder("root", "Root"),
    ];

    expect(childFolders(orphaned, null).map(f => f.id)).toEqual(["child", "root"]);
    expect(flattenFolderTree(orphaned, { expanded: new Set(["child"]) })).toEqual([
      { id: "child", depth: 0, hasChildren: true },
      { id: "grandchild", depth: 1, hasChildren: false },
      { id: "root", depth: 0, hasChildren: false },
    ]);
  });

  it("promotes both members of a two-folder cycle to roots, each exactly once", () => {
    const looped: Folder[] = [
      { id: "a", name: "A", tags: [], parent_id: "b" },
      { id: "b", name: "B", tags: [], parent_id: "a" },
    ];

    expect(childFolders(looped, null).map(f => f.id)).toEqual(["a", "b"]);
    expect(flattenFolderTree(looped, { expanded: new Set(), alwaysExpanded: true })).toEqual([
      { id: "a", depth: 0, hasChildren: false },
      { id: "b", depth: 0, hasChildren: false },
    ]);
  });

  it("treats a self-parented folder as a root and never as its own child", () => {
    const selfParented: Folder[] = [
      { id: "self", name: "Self", tags: [], parent_id: "self" },
    ];

    expect(childFolders(selfParented, null).map(f => f.id)).toEqual(["self"]);
    expect(childFolders(selfParented, "self")).toEqual([]);
    expect(flattenFolderTree(selfParented, { expanded: new Set(["self"]) })).toEqual([
      { id: "self", depth: 0, hasChildren: false },
    ]);
    expect(descendantIds(selfParented, "self")).toEqual(new Set());
  });

  it("sorts null names without throwing and falls back to \"Untitled\"", () => {
    const unnamed: Folder[] = [namelessFolder("z"), namelessFolder("a")];

    expect(childFolders(unnamed, null).map(f => f.id)).toEqual(["a", "z"]);
    expect(flattenFolderTree(unnamed, { expanded: new Set() }).map(r => r.id)).toEqual(["a", "z"]);
    expect(folderPath(unnamed, "a")).toEqual([{ id: "a", name: "Untitled" }]);
    expect(searchFolders(unnamed, "untitled").matches).toEqual([
      { id: "a", name: "Untitled", path: "Untitled" },
      { id: "z", name: "Untitled", path: "Untitled" },
    ]);
  });

  it("keeps duplicate ids at one row for both childFolders and flattenFolderTree", () => {
    const duplicated: Folder[] = [
      folder("dup", "Old"),
      folder("dup", "New"),
      folder("kid", "Kid", "dup"),
    ];

    expect(childFolders(duplicated, null).map(f => f.id)).toEqual(["dup"]);
    expect(childFolders(duplicated, null).map(f => f.name)).toEqual(["New"]);
    expect(childFolders(duplicated, "dup").map(f => f.id)).toEqual(["kid"]);
    expect(flattenFolderTree(duplicated, { expanded: new Set(["dup"]) })).toEqual([
      { id: "dup", depth: 0, hasChildren: true },
      { id: "kid", depth: 1, hasChildren: false },
    ]);
  });
});

describe("childrenIndex", () => {
  it("groups every folder exactly once, roots under ROOT_GROUP, siblings by name then id", () => {
    const messy: Folder[] = [
      folder("b", "Same", "parts"),
      folder("a", "Same", "parts"),
      folder("parts", "Parts"),
      folder("orphan", "Orphan", "gone"),
      folder("dupe", "Older"),
      folder("dupe", "Newer"),
      { id: "x", name: "X", tags: [], parent_id: "y" },
      { id: "y", name: "Y", tags: [], parent_id: "x" },
    ];

    const groups = childrenIndex(messy);
    expect(Object.keys(groups).sort()).toEqual([ROOT_GROUP, "parts"]);
    expect(groups["parts"].map(f => f.id)).toEqual(["a", "b"]);
    expect(groups[ROOT_GROUP].map(f => f.id)).toEqual(["dupe", "orphan", "parts", "x", "y"]);
    expect(groups[ROOT_GROUP].find(f => f.id === "dupe")?.name).toBe("Newer");
    expect(
      Object.values(groups)
        .flat()
        .map(f => f.id)
        .sort(),
    ).toEqual(["a", "b", "dupe", "orphan", "parts", "x", "y"]);
  });
});

describe("descendantIds", () => {
  it("returns the whole subtree and excludes the folder itself", () => {
    expect([...descendantIds(tree, "parts")].sort()).toEqual(["brackets", "clamps", "corners"]);
    expect(descendantIds(tree, "parts")).not.toContain("parts");
  });

  it("returns an empty set for a leaf and for an unknown id", () => {
    expect(descendantIds(tree, "corners")).toEqual(new Set());
    expect(descendantIds(tree, "nope")).toEqual(new Set());
  });

  it("uses effective parents and terminates on corrupt cyclic input", () => {
    const corrupt: Folder[] = [
      { id: "a", name: "A", tags: [], parent_id: "b" },
      { id: "b", name: "B", tags: [], parent_id: "a" },
      folder("root", "Root"),
      folder("kid", "Kid", "root"),
      folder("grandkid", "Grandkid", "kid"),
      folder("orphan", "Orphan", "gone"),
      folder("orphanKid", "Orphan Kid", "orphan"),
    ];

    expect([...descendantIds(corrupt, "root")].sort()).toEqual(["grandkid", "kid"]);
    expect([...descendantIds(corrupt, "orphan")]).toEqual(["orphanKid"]);
    // Nothing hangs off a promoted cycle member any more: both are roots.
    expect(descendantIds(corrupt, "a")).toEqual(new Set());
    expect(childFolders(corrupt, null).map(f => f.id)).toEqual(["a", "b", "orphan", "root"]);
  });
});

describe("searchFolders", () => {
  it("matches leaf names case-insensitively after trimming", () => {
    expect(searchFolders(tree, "  BRACK  ").matches.map(m => m.id)).toEqual(["brackets"]);
  });

  it("matches the leaf name only, never the path", () => {
    const nested: Folder[] = [
      folder("brackets", "Brackets"),
      folder("parts", "Parts", "brackets"),
    ];

    const result = searchFolders(nested, "brackets");
    expect(result.matches.map(m => m.id)).toEqual(["brackets"]);
    expect(result.total).toBe(1);
    expect(searchFolders(nested, "Parts /").matches).toEqual([]);
  });

  it("reports the root → leaf path joined with \" / \"", () => {
    expect(searchFolders(tree, "corners").matches).toEqual([
      { id: "corners", name: "Corners", path: "Parts / Brackets / Corners" },
    ]);
  });

  it("ranks name prefixes before name substrings", () => {
    const mixed: Folder[] = [folder("a", "A Clamp"), folder("z", "Clamp Zeta")];

    expect(searchFolders(mixed, "clamp").matches.map(m => m.id)).toEqual(["z", "a"]);
  });

  it("orders equal-ranked hits by full path", () => {
    const nested: Folder[] = [
      folder("parts", "Parts"),
      folder("x", "Clamp", "parts"),
      folder("y", "Clamp"),
    ];

    const result = searchFolders(nested, "lamp");
    expect(result.matches.map(m => m.path)).toEqual(["Clamp", "Parts / Clamp"]);
    expect(result.matches.map(m => m.id)).toEqual(["y", "x"]);
  });

  it("caps the matches but reports the uncapped total", () => {
    const many: Folder[] = Array.from({ length: 60 }, (_, index) =>
      folder(`f${index}`, `Widget ${index}`),
    );

    const capped = searchFolders(many, "widget");
    expect(capped.matches).toHaveLength(50);
    expect(capped.total).toBe(60);

    const small = searchFolders(many, "widget", 5);
    expect(small.matches).toHaveLength(5);
    expect(small.total).toBe(60);
  });

  it("dedupes duplicate ids, later duplicates winning", () => {
    const duplicated: Folder[] = [folder("dup", "Old Widget"), folder("dup", "New Widget")];

    const result = searchFolders(duplicated, "widget");
    expect(result.matches).toEqual([{ id: "dup", name: "New Widget", path: "New Widget" }]);
    expect(result.total).toBe(1);
  });

  it("returns nothing for a blank query", () => {
    expect(searchFolders(tree, "")).toEqual({ matches: [], total: 0 });
    expect(searchFolders(tree, "   ")).toEqual({ matches: [], total: 0 });
  });
});
