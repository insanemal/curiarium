import { describe, expect, it } from "vitest";

import { filesScope } from "./filesScope";

describe("filesScope", () => {
  it("resolves the All-files scope and refuses a drop there", () => {
    expect(filesScope({ folderId: null, unfiled: false, folders: [] })).toEqual({
      kind: "all",
      label: "All files",
      note: "Every file in the library, in every folder.",
      dropPolicy: "refuse",
    });
  });

  it("resolves the Unfiled scope as the root upload destination", () => {
    expect(filesScope({ folderId: null, unfiled: true, folders: [] })).toEqual({
      kind: "unfiled",
      label: "Unfiled",
      note: "Files at the library root: no folder owns them.",
      dropPolicy: "upload",
    });
  });

  it("names the open folder in the label and the note", () => {
    expect(
      filesScope({
        folderId: "p",
        unfiled: false,
        folders: [{ id: "p", name: "Parts" }],
      }),
    ).toEqual({
      kind: "folder",
      label: "Parts",
      note: "Direct contents of Parts. Subfolders are tiles, not included here.",
      dropPolicy: "upload",
    });
  });

  it("falls back to a placeholder label when the folder id cannot be resolved", () => {
    expect(
      filesScope({
        folderId: "ghost",
        unfiled: false,
        folders: [{ id: "p", name: "Parts" }],
      }),
    ).toEqual({
      kind: "folder",
      label: "this folder",
      note: "Direct contents of this folder. Subfolders are tiles, not included here.",
      dropPolicy: "upload",
    });
  });

  it("refuses drops in exactly one of the three scopes", () => {
    const policies = [
      filesScope({ folderId: null, unfiled: false, folders: [] }).dropPolicy,
      filesScope({ folderId: null, unfiled: true, folders: [] }).dropPolicy,
      filesScope({
        folderId: "p",
        unfiled: false,
        folders: [{ id: "p", name: "Parts" }],
      }).dropPolicy,
    ];

    expect(policies).toEqual(["refuse", "upload", "upload"]);
  });
});
