/**
 * What an archive holds, said before a byte is written (Mf-0ck5).
 *
 * The server lists an archive's entries to show the confirm step, so the exact
 * paths are known before the extraction starts. This module turns that listing
 * into the sentence the operator needs: how many models, images and nested
 * archives they are about to release, and which directory the entries are rooted
 * at. The surprise it exists to kill is the one that opened the ticket - an
 * archive whose models sit *inside a nested archive*, so "Extract" hands the
 * library one more `.zip` and a row named after it, while the operator expected
 * the tower.
 *
 * It classifies, it does not decide: nothing here is a gate. Two exclusions run
 * through the counts, because two server rules sit between an archive and a
 * project: `lib/zipJunk`'s junk members, which are never written at all, and the
 * members the extraction writes but the storage scan can never register - a
 * dotfile, or anything under a dot directory - which are counted apart as
 * `unregistered` so the sentence never promises a project cannot keep.
 *
 * The dependency list is deliberately short and barrel-free. `lib/assetKinds`'
 * predicates take a `{ filename, mime }` pair and fall back to the extension when
 * the mime is empty, which is exactly what a zip entry is; `lib/zipJunk` mirrors
 * the server's own OS-metadata rule (bound by `test_junk_vocabulary_mirror.py`),
 * so a member this module calls junk is a member the server will not write; and
 * `lib/zipUtils` already owns "is this a zip". None of them reach `lib/api`, which
 * matters because the extract control's tests replace that barrel with three
 * names, and importing the review module here would pull the whole of it in.
 */
import { isImageAsset, isModelAsset } from "../../lib/assetKinds";
import { isJunkZipPath } from "../../lib/zipJunk";
import { isZipFile } from "../../lib/zipUtils";
import type { ArchiveEntry } from "../../lib/api/imports";

/** A zip member is directory-like when its name ends in a separator. */
function isDirectoryEntry(path: string): boolean {
  return (path || "").endsWith("/");
}

/**
 * Basenames the storage scan refuses outright: `library_scan.IGNORED_FILENAMES`,
 * asked of a member's last segment the way `_plannable` in
 * `curiarium/api/archive_project_service.py` asks it, through
 * `library_scan.is_skipped_name`.
 *
 * `lib/zipJunk` owns the junk half of the server's rules and already covers these
 * three names in any casing, so the reachable member here is the spelling junk
 * lets through and the scan does not - a `Thumbs.db ` with its trailing space,
 * which `sanitize_filename` renders back to `Thumbs.db` before the comparison.
 * It is also where the next row of the server's table has to land: widen both
 * tables together, `curiarium/api/library_scan.py` first.
 */
const SCAN_SKIPPED_BASENAMES = new Set(["thumbs.db", "desktop.ini", ".ds_store"]);

/**
 * One member segment as the scan compares it: `file_utils.sanitize_filename`
 * drops NULs and surrounding whitespace before a name is ever judged, so
 * `".hidden "` is a dot directory and `"Thumbs.db "` is a skipped name.
 */
function scanSegment(segment: string): string {
  return segment.replace(/\0/g, "").trim();
}

/**
 * Whether the scan could never register this member, whatever extraction writes
 * beside it - the rule `_plannable` in
 * `curiarium/api/archive_project_service.py` applies before it plans a project.
 *
 * Two halves, both the server's: every `/`-separated segment has to be visible
 * (`library_scan._walk_storage` prunes a dot directory outright), and the basename
 * has to survive `library_scan.is_skipped_name`. `/` is the split the server uses,
 * and the one a listing arrives with. The junk half of the story belongs to
 * `lib/zipJunk`, whose verdict the caller has already taken before asking here.
 */
function cannotBeRegistered(path: string): boolean {
  const segments = (path || "").split("/").map(scanSegment);
  if (segments.some(segment => segment.startsWith("."))) return true;
  return SCAN_SKIPPED_BASENAMES.has((segments[segments.length - 1] ?? "").toLowerCase());
}

/** One counted group: what it is, and how many of them there are. */
export type ArchivePlanPart = {
  one: string;
  many: string;
  count: number;
};

export type ArchivePlan = {
  /**
   * Non-junk, non-directory members - what the extraction writes.
   *
   * This counts the `unregistered` members too, and so does `bytes`: these two
   * describe what the extraction *spends*, not what the project holds. That is an
   * asymmetry on purpose, not an oversight to correct - the operator is told which
   * files land on disk, and only the four kind counts below promise what becomes
   * an asset.
   */
  files: number;
  /** Their combined size, which is what the extraction will spend (see `files`). */
  bytes: number;
  models: number;
  images: number;
  documents: number;
  nestedArchives: number;
  /** The nested archives' paths, sorted, because the warning has to name them. */
  nestedArchivePaths: string[];
  /** OS metadata the server drops; counted so the sentence can say so out loud. */
  junk: number;
  /**
   * Members the extraction writes but the library can never register, so no
   * project built from this archive can contain them: a dotfile, or anything under
   * a dot directory (see `cannotBeRegistered`). They are written - hence counted
   * in `files` and `bytes` - and excluded from `models`, `images`, `documents` and
   * `nestedArchives`, which are the kinds the library will actually hold.
   */
  unregistered: number;
  /**
   * The one directory every written entry sits under, or `null` when there is no
   * single answer - a loose file at the top level, or two different first
   * segments. Mirrors the server's own common root, which is what the created
   * project's groups are read off.
   */
  root: string | null;
  /**
   * The case the operator must be told about before committing: this archive
   * holds no model of its own, but it does hold an archive, so the models - if it
   * has any - are still packed inside it.
   */
  modelsOnlyInsideNestedArchive: boolean;
};

/**
 * Read one archive's listing into the plan the confirm step speaks.
 *
 * Pure and total: any listing produces a plan, and an empty one says so rather
 * than throwing, because the caller is mid-dialog.
 */
export function planArchive(entries: readonly ArchiveEntry[]): ArchivePlan {
  let bytes = 0;
  let models = 0;
  let images = 0;
  let documents = 0;
  let junk = 0;
  let unregistered = 0;
  const nestedArchivePaths: string[] = [];
  const written: string[] = [];

  for (const entry of entries) {
    const path = entry.path || "";
    if (isDirectoryEntry(path)) continue;
    if (isJunkZipPath(path)) {
      junk += 1;
      continue;
    }
    bytes += entry.size || 0;
    written.push(path);
    // Written, but never registered: counted apart rather than as a kind, so a
    // stray `.hidden/handy.stl` cannot promise a model the project will not have -
    // nor sit on the one warning this module exists to deliver.
    if (cannotBeRegistered(path)) {
      unregistered += 1;
      continue;
    }
    // A nested archive is an archive first: it is one file the extraction
    // releases whole, and counting its `.zip` as a document too would double it.
    if (isZipFile(path)) {
      nestedArchivePaths.push(path);
    } else if (isModelAsset({ filename: path, mime: "" })) {
      models += 1;
    } else if (isImageAsset({ filename: path, mime: "" })) {
      images += 1;
    } else {
      documents += 1;
    }
  }

  return {
    files: written.length,
    bytes,
    models,
    images,
    documents,
    nestedArchives: nestedArchivePaths.length,
    nestedArchivePaths: [...nestedArchivePaths].sort((a, b) => a.localeCompare(b)),
    junk,
    unregistered,
    root: commonRoot(written),
    modelsOnlyInsideNestedArchive: models === 0 && nestedArchivePaths.length > 0,
  };
}

/** The one first segment every entry shares, or `null`. Case-insensitive, first casing wins. */
export function commonRoot(paths: readonly string[]): string | null {
  let root: string | null = null;
  for (const path of paths) {
    const head = (path || "").split("/")[0];
    if (!head || head === path) return null; // a loose file at the top level
    if (root === null) root = head;
    else if (head.toLowerCase() !== root.toLowerCase()) return null;
  }
  return root;
}

/** `1 model` / `3 models`; nothing at all when the count is zero. */
function part(count: number, one: string, many: string): string | null {
  if (count === 0) return null;
  return `${count} ${count === 1 ? one : many}`;
}

/**
 * The lines the confirm step renders beside its count-and-size sentence.
 *
 * Two at the floor, five at the most, and never a claim the listing does not
 * support: it names the kinds it counted, the directory the entries are rooted
 * at, what it left out, what it writes that the library will not keep, and - when
 * the only archive in sight is a nested one - it says so in the same breath,
 * because that is the moment the operator can still choose differently.
 */
export function archivePlanLines(plan: ArchivePlan): string[] {
  const lines: string[] = [];

  const parts = [
    part(plan.models, "model", "models"),
    part(plan.images, "image", "images"),
    part(plan.nestedArchives, "nested archive", "nested archives"),
    part(plan.documents, "document", "documents"),
  ].filter((line): line is string => line !== null);

  if (plan.files === 0) {
    lines.push(
      plan.junk > 0
        ? `This archive holds nothing but ${plan.junk} ${plan.junk === 1 ? "system file" : "system files"} - extracting it writes nothing.`
        : "This archive holds no files, so extracting it writes nothing.",
    );
    return lines;
  }

  // Kinds, not a size: the panel's own first line already says how many files and
  // how many bytes the *listing* holds, and those two numbers count the system
  // metadata this plan leaves out. A second, smaller size beside them would read
  // as a disagreement rather than as the difference between the two - which the
  // sentence below spells out instead.
  lines.push(`It holds ${parts.join(", ")}.`);
  lines.push(
    plan.root === null
      ? "The entries sit loose at the top of the new folder."
      : `Everything lands under "${plan.root}/" inside the new folder.`,
  );
  if (plan.junk > 0) {
    lines.push(
      `It also holds ${plan.junk} ${plan.junk === 1 ? "system file" : "system files"} - .DS_Store, Thumbs.db and the like - which ${plan.junk === 1 ? "is" : "are"} not written.`,
    );
  }
  if (plan.unregistered > 0) {
    const many = plan.unregistered !== 1;
    lines.push(
      `It also writes ${part(plan.unregistered, "hidden file", "hidden files")} - the library will not register ${many ? "them" : "it"}, so ${many ? "they" : "it"} cannot be part of the project.`,
    );
  }
  if (plan.modelsOnlyInsideNestedArchive) {
    const named = plan.nestedArchivePaths
      .slice(0, 2)
      .map(path => `"${path.split("/").pop()}"`)
      .join(" and ");
    const more = plan.nestedArchivePaths.length > 2 ? ` and ${plan.nestedArchivePaths.length - 2} more` : "";
    lines.push(
      `No model is packed in this archive itself: the only archives inside are ${named}${more}. ` +
        "Extract releases that file whole - it does not unzip it, so no model appears in the library until that one is extracted too.",
    );
  } else if (plan.nestedArchives > 0) {
    lines.push(
      `${plan.nestedArchives === 1 ? "One nested archive stays packed" : `${plan.nestedArchives} nested archives stay packed`}: extraction releases ${plan.nestedArchives === 1 ? "it" : "them"} whole, so whatever is inside is not registered as files.`,
    );
  }
  return lines;
}
