export type FilesScopeKind = "all" | "unfiled" | "folder";
export type FilesDropPolicy = "refuse" | "upload";

export type FilesScope = {
  kind: FilesScopeKind;
  label: string;
  note: string;
  dropPolicy: FilesDropPolicy;
};

export type FilesScopeInput = {
  folderId: string | null;
  unfiled: boolean;
  // Only the id and the name matter here; the full `Folder` rows are accepted
  // structurally so the sidebar and the grid can pass what they already hold.
  folders: readonly { id: string; name: string }[];
};

/**
 * The single answer to "which Files scope is on screen, what is it called, what
 * does it contain and may files be dropped on it". The breadcrumb, the sidebar
 * drop policy and the empty-state copy all read this so they cannot disagree.
 */
export function filesScope({ folderId, unfiled, folders }: FilesScopeInput): FilesScope {
  if (folderId !== null) {
    // An unknown id is the folder list mid-load, not an empty folder, so the
    // label falls back instead of rendering blank.
    const label = folders.find(folder => folder.id === folderId)?.name || "this folder";
    return {
      kind: "folder",
      label,
      note: `Direct contents of ${label}. Subfolders are tiles, not included here.`,
      dropPolicy: "upload",
    };
  }

  if (unfiled) {
    // Unfiled is the library root, which is a real upload destination.
    return {
      kind: "unfiled",
      label: "Unfiled",
      note: "Files at the library root: no folder owns them.",
      dropPolicy: "upload",
    };
  }

  // "All files" is a result scope: it spans every folder, so no single folder
  // could own an upload dropped there.
  return {
    kind: "all",
    label: "All files",
    note: "Every file in the library, in every folder.",
    dropPolicy: "refuse",
  };
}
