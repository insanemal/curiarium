import { describe, expect, it } from "vitest";

import type { ArchiveEntry } from "../../lib/api/imports";
import { archivePlanLines, commonRoot, planArchive } from "./archivePlan";

/** The archive from the live report: `01_Fates End - Wizard Tower.zip`, whose
 *  models sit in two subdirectories of one root, with images, a document and one
 *  nested archive. Real names, because a fixture made of `a.stl`/`b.png` proves
 *  nothing about a path with spaces, a leading number and a dot inside the name. */
function towerListing(): ArchiveEntry[] {
  const model = (name: string): ArchiveEntry => ({ path: `${ROOT}/01_Wizard/${name}`, size: 2048 });
  return [
    { path: `${ROOT}/README.txt`, size: 64 },
    { path: `${ROOT}/LICENSE.txt`, size: 64 },
    ...[
      "wizard_tower_base.stl",
      "wizard_tower_shaft_a.stl",
      "wizard_tower_shaft_b.stl",
      "wizard_tower_battlement.stl",
      "wizard_tower_roof.stl",
      "wizard_spire.stl",
      "wizard_staff.stl",
      "wizard_figure.stl",
    ].map(model),
    { path: `${ROOT}/02_Grounding/grounding_base.stl`, size: 4096 },
    { path: `${ROOT}/02_Grounding/grounding_rocks.stl`, size: 4096 },
    { path: `${ROOT}/images/preview.png`, size: 1024 },
    { path: `${ROOT}/images/box_art.png`, size: 1024 },
    { path: `${ROOT}/docs/building_guide.txt`, size: 32 },
    { path: `${ROOT}/nested/accessories.zip`, size: 8192 },
    // What a Mac or a Windows tool leaves behind, which the server will not write.
    { path: "__MACOSX/._wizard_tower_base.stl", size: 4 },
    { path: ".DS_Store", size: 6148 },
    { path: `${ROOT}/Thumbs.db`, size: 5120 },
    { path: `${ROOT}/01_Wizard/`, size: 0 },
  ];
}

const ROOT = "01_Fates End - Wizard Tower";

/** The same archive with its models still packed away: a readme and the nested
 *  `.zip` that holds them, which is the shape the ticket was filed about. The
 *  extra members ride on top, because the whole point is what a stray member
 *  does to the sentence. */
function packedTowerListing(extra: ArchiveEntry[] = []): ArchiveEntry[] {
  return [
    { path: `${ROOT}/README.txt`, size: 64 },
    { path: `${ROOT}/nested/wizard_v5.4_models.zip`, size: 90240 },
    ...extra,
  ];
}

/** A printed model in the root, plus what the scan can never register. */
function towerWithHiddenListing(): ArchiveEntry[] {
  return [
    { path: `${ROOT}/01_Wizard/wizard_tower_base.stl`, size: 2048 },
    { path: `${ROOT}/.hidden/handy.stl`, size: 2048 },
    { path: `${ROOT}/.hidden/build_notes.txt`, size: 64 },
  ];
}

describe("planArchive", () => {
  it("counts the kinds the operator is about to release, and ignores what the server drops", () => {
    const plan = planArchive(towerListing());

    expect(plan.models).toBe(10);
    expect(plan.images).toBe(2);
    expect(plan.nestedArchives).toBe(1);
    expect(plan.documents).toBe(3); // README, LICENSE, building_guide
    expect(plan.junk).toBe(3); // __MACOSX member, .DS_Store, Thumbs.db
    expect(plan.files, "the junk members and the directory entry are not files").toBe(16);
  });

  it("sums only what will be written", () => {
    const plan = planArchive(towerListing());

    // Eight 01_Wizard models at 2 KB, two grounding parts at 4 KB, two images at
    // 1 KB, three documents, the nested archive at 8 KB - the three junk members
    // and the directory entry are out.
    expect(plan.bytes).toBe(8 * 2048 + 2 * 4096 + 2 * 1024 + 64 + 64 + 32 + 8192);
  });

  it("names the one directory the entries are rooted at", () => {
    expect(planArchive(towerListing()).root).toBe(ROOT);
  });

  it("has no root when a file sits loose at the top level", () => {
    const plan = planArchive([
      { path: "README.txt", size: 1 },
      { path: "models/tower.stl", size: 1 },
    ]);

    expect(plan.root).toBeNull();
  });

  it("has no root when the first segments differ", () => {
    const plan = planArchive([
      { path: "01_Wizard/tower.stl", size: 1 },
      { path: "02_Grounding/base.stl", size: 1 },
    ]);

    expect(plan.root).toBeNull();
  });

  it("compares the root case-insensitively and keeps the casing it saw first", () => {
    expect(commonRoot(["Tower/a.stl", "tower/b.stl"])).toBe("Tower");
  });

  it("says when the models are only inside a nested archive", () => {
    const plan = planArchive([
      { path: "Wizard v5.4/README.txt", size: 64 },
      { path: "Wizard v5.4/wizard_v5.4_models.zip", size: 90240 },
    ]);

    expect(plan.models).toBe(0);
    expect(plan.modelsOnlyInsideNestedArchive).toBe(true);
  });

  it("does not claim a nested-archive warning when the archive holds models of its own", () => {
    const plan = planArchive([
      { path: "tower.stl", size: 2048 },
      { path: "extras.zip", size: 512 },
    ]);

    expect(plan.modelsOnlyInsideNestedArchive).toBe(false);
    expect(plan.nestedArchives).toBe(1);
  });

  it("classifies the extensions a print library actually carries", () => {
    const plan = planArchive([
      { path: "a.stl", size: 1 },
      { path: "b.3mf", size: 1 },
      { path: "c.obj", size: 1 },
      { path: "d.step", size: 1 },
      { path: "e.png", size: 1 },
      { path: "f.jpeg", size: 1 },
      { path: "g.pdf", size: 1 },
      { path: "h.txt", size: 1 },
    ]);

    expect(plan.models).toBe(4);
    expect(plan.images).toBe(2);
    expect(plan.documents).toBe(2);
  });

  it("counts nothing at all for an archive with no members", () => {
    const plan = planArchive([]);

    expect(plan.files).toBe(0);
    expect(plan.bytes).toBe(0);
    expect(plan.root).toBeNull();
    expect(plan.modelsOnlyInsideNestedArchive).toBe(false);
  });

  it("does not count what the scan can never register as a kind the project holds", () => {
    const plan = planArchive(towerWithHiddenListing());

    expect(plan.models).toBe(1); // only the printed one
    expect(plan.documents).toBe(0); // build_notes.txt sits under .hidden/
    expect(plan.unregistered).toBe(2);
  });

  it("counts the unregistered members in files and bytes, because extraction writes them", () => {
    const plan = planArchive(towerWithHiddenListing());

    expect(plan.files).toBe(3);
    expect(plan.bytes).toBe(2048 + 2048 + 64);
    expect(plan.root, "a dot directory is written too, so it is still where files land").toBe(ROOT);
  });

  it("counts a nested archive under a dot directory as unregistered, not nested", () => {
    const plan = planArchive([
      { path: `${ROOT}/README.txt`, size: 64 },
      { path: `.hidden/inner.zip`, size: 8192 },
    ]);

    expect(plan.nestedArchives).toBe(0);
    expect(plan.nestedArchivePaths).toEqual([]);
    expect(plan.unregistered).toBe(1);
    expect(plan.modelsOnlyInsideNestedArchive, "nothing left to unpack is left").toBe(false);
  });

  it("mirrors the server's skipped basenames, asked of the name it renders", () => {
    // `Thumbs.db ` is not junk to `zipJunk` - it normalises case and separators,
    // not whitespace - and it is not dot-prefixed. But `sanitize_filename` strips
    // the space before `library_scan.is_skipped_name` sees it, so the scan refuses
    // the file and no project built from this archive can hold it.
    const plan = planArchive([
      { path: `${ROOT}/01_Wizard/wizard_tower_base.stl`, size: 2048 },
      { path: `${ROOT}/01_Wizard/Thumbs.db `, size: 5120 },
    ]);

    expect(plan.models).toBe(1);
    expect(plan.documents, "nor as a document").toBe(0);
    expect(plan.unregistered).toBe(1);
    expect(plan.files).toBe(2);
  });

  it("has nothing unregistered in an ordinary listing", () => {
    expect(planArchive(towerListing()).unregistered).toBe(0);
  });

  it("tolerates a member with no path and no size", () => {
    const plan = planArchive([{ path: "", size: 0 }]);

    expect(plan.files).toBe(1);
    expect(plan.documents).toBe(1);
  });
});

describe("archivePlanLines", () => {
  it("names the kinds and the root, and never invents a count it did not take", () => {
    const lines = archivePlanLines(planArchive(towerListing()));

    expect(lines[0]).toBe("It holds 10 models, 2 images, 1 nested archive, 3 documents.");
    expect(lines[1]).toBe(`Everything lands under "${ROOT}/" inside the new folder.`);
    expect(lines[2]).toBe(
      "It also holds 3 system files - .DS_Store, Thumbs.db and the like - which are not written.",
    );
    expect(lines[3]).toContain("nested archive");
    expect(lines.length).toBe(4);
  });

  it("speaks the singular for one of anything", () => {
    const lines = archivePlanLines(planArchive([{ path: "tower.stl", size: 1024 }]));

    expect(lines[0]).toBe("It holds 1 model.");
    expect(lines[1]).toBe("The entries sit loose at the top of the new folder.");
  });

  it("says explicitly when the models are only inside a nested archive", () => {
    const lines = archivePlanLines(
      planArchive([
        { path: "Wizard v5.4/README.txt", size: 64 },
        { path: "Wizard v5.4/wizard_v5.4_models.zip", size: 90240 },
      ]),
    );

    const warning = lines[lines.length - 1];
    expect(warning).toContain("No model is packed in this archive itself");
    expect(warning).toContain("wizard_v5.4_models.zip");
    expect(warning).toContain("it does not unzip it");
  });

  it("names two nested archives and counts the rest", () => {
    const lines = archivePlanLines(
      planArchive([
        { path: "a.zip", size: 1 },
        { path: "b.zip", size: 1 },
        { path: "c.zip", size: 1 },
      ]),
    );

    const warning = lines[lines.length - 1];
    expect(warning).toContain('"a.zip" and "b.zip"');
    expect(warning).toContain("and 1 more");
  });

  it("warns about the nested archive even when a dotfile model rides along", () => {
    const plan = planArchive(packedTowerListing([{ path: `${ROOT}/.hidden/handy.stl`, size: 2048 }]));

    expect(plan.models).toBe(0);
    expect(plan.unregistered).toBe(1);
    expect(plan.modelsOnlyInsideNestedArchive, "the stray model is not one the library can hold").toBe(true);

    const lines = archivePlanLines(plan);
    const warning = lines[lines.length - 1];
    expect(warning).toContain("No model is packed in this archive itself");
    expect(warning).toContain("wizard_v5.4_models.zip");
  });

  it("says which of what it writes the library will never register", () => {
    const lines = archivePlanLines(planArchive(towerWithHiddenListing()));

    expect(lines).toContain(
      "It also writes 2 hidden files - the library will not register them, so they cannot be part of the project.",
    );
  });

  it("speaks the singular for one member the library will not register", () => {
    const lines = archivePlanLines(
      planArchive([
        { path: `${ROOT}/01_Wizard/wizard_tower_base.stl`, size: 2048 },
        { path: `${ROOT}/.hidden/handy.stl`, size: 2048 },
      ]),
    );

    expect(lines).toContain(
      "It also writes 1 hidden file - the library will not register it, so it cannot be part of the project.",
    );
  });

  it("says nothing about registering when the listing holds no such member", () => {
    const lines = archivePlanLines(planArchive(towerListing()));

    expect(lines.some(line => line.includes("will not register"))).toBe(false);
  });

  it("stays inside five lines when the listing holds something of everything", () => {
    const lines = archivePlanLines(
      planArchive([
        ...towerListing(),
        { path: `${ROOT}/.hidden/handy.stl`, size: 2048 },
        { path: `${ROOT}/.hidden/build_notes.txt`, size: 64 },
      ]),
    );

    expect(lines.length).toBe(5);
  });

  it("leaves the sizes to the panel's own first line", () => {
    const lines = archivePlanLines(planArchive([{ path: "tower.stl", size: 2048 }]));

    expect(lines[0]).not.toMatch(/KB|MB/);
  });

  it("says the nested archives stay packed when there are models as well", () => {
    const lines = archivePlanLines(planArchive([
      { path: "tower.stl", size: 2048 },
      { path: "extras.zip", size: 512 },
    ]));

    expect(lines[lines.length - 1]).toContain("One nested archive stays packed");
  });

  it("says an archive of nothing writes nothing", () => {
    expect(archivePlanLines(planArchive([]))).toEqual([
      "This archive holds no files, so extracting it writes nothing.",
    ]);
  });

  it("says so through the system files it left out", () => {
    expect(archivePlanLines(planArchive([{ path: ".DS_Store", size: 4 }]))).toEqual([
      "This archive holds nothing but 1 system file - extracting it writes nothing.",
    ]);
    expect(archivePlanLines(planArchive([{ path: ".DS_Store", size: 4 }, { path: "Thumbs.db", size: 4 }]))).toEqual([
      "This archive holds nothing but 2 system files - extracting it writes nothing.",
    ]);
  });
});
