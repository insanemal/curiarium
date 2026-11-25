/**
 * Asset-kind classification for the project landing page (Mf-6cxs).
 *
 * Two questions live here and must stay separate. The AFFORDANCE predicates
 * (`isModelAsset`, `isImageAsset`, `is3mfAsset`) answer "what can this browser
 * open or paint?"; they drive the cover fallback, the gallery and the
 * open-in-slicer gate. `classifyMemberRole` answers "what role did the backend
 * give this member?" and mirrors the Python classifier table for table.
 *
 * `AssetGrid` keeps its own extension sets for the same affordance reason; the
 * affordance sets here intentionally agree with it on the model extensions.
 */
import type { Asset } from "./api";

/** The fields needed to classify an asset; anything shaped like an Asset works. */
export type AssetKindSource = Pick<Asset, "filename" | "mime">;

/**
 * Lowercase extension without the dot. Returns "" for dotless names
 * ("README"), trailing dots ("backup.") and dotfiles (".gitignore").
 */
export function extOf(name: string): string {
  const base = name.split("/").pop() ?? name;
  const dot = base.lastIndexOf(".");
  if (dot <= 0 || dot === base.length - 1) return "";
  return base.slice(dot + 1).toLowerCase();
}

/** Extensions browsers render as `<img>` without plugins. */
const IMAGE_EXTS = new Set([
  "png",
  "jpg",
  "jpeg",
  "gif",
  "webp",
  "avif",
  "bmp",
  "svg",
  "tif",
  "tiff",
]);

/** Slicer-openable model extensions; mirrors the set in `AssetGrid`. */
const MODEL_EXTS = new Set(["stl", "3mf", "step", "stp", "obj"]);

/** Extensions the bundle classifier calls TEXT: the PDFs, READMEs and manuals a
 *  project holds as documents. MIRRORS `bundle_detect.BUNDLE_TEXT_EXTS` - pinned
 *  by a test that reads that Python table, so the two cannot drift. */
export const DOCUMENT_EXTS: ReadonlySet<string> = new Set([
  "txt",
  "md",
  "pdf",
  "html",
  "htm",
  "rtf",
]);

/**
 * The backend's member ROLES. These tables are NOT `MODEL_EXTS` / `IMAGE_EXTS`
 * above, and deliberately so: those two gate slicer and gallery AFFORDANCES
 * (what this browser can open or paint), while the tables below mirror the ROLE
 * the backend stamped on a member. The two questions have different answers -
 * `.obj` opens in a slicer yet the backend calls it `other`, `.lbrn` is a
 * `model` no slicer here opens, and `.gif`/`.svg` paint in the gallery yet are
 * `other` files. Do not "unify" them with the affordance sets: a shared table
 * would have to lie to one caller or the other.
 */

/** `bundle_detect.MODEL_SUFFIXES`, i.e. `config.IMPORT_ALLOWED_EXTS` minus
 *  `.zip` (a `.zip` is a container, never a model member). Pinned by value in
 *  the test: that Python name is derived, not written as a literal. */
export const MEMBER_MODEL_EXTS: ReadonlySet<string> = new Set([
  "stl",
  "3mf",
  "step",
  "stp",
  "lbrn",
  "lbrn2",
]);

/** `library_scan.IMAGE_SUFFIXES`, suffix-only and narrower than what a browser
 *  paints: no `.gif`, no `.svg`, no `.tif`/`.tiff`. Pinned by a test that reads
 *  that Python table. */
export const MEMBER_IMAGE_EXTS: ReadonlySet<string> = new Set([
  "png",
  "jpg",
  "jpeg",
  "webp",
  "bmp",
]);

/** The role `bundle_detect.classify_entry` gives one member. */
export type MemberRole = "model" | "image" | "text" | "other" | "archive";

/**
 * The backend's role for a member, decided by extension and nothing else.
 *
 * MIRRORS `bundle_detect.classify_entry`, in its order: archive (`.zip`), model,
 * image, text, other. Suffix-only on purpose - never the mime type - because
 * that is how the backend decides, and a page that disagreed with the rows the
 * same project was created from would be the one lie this classification exists
 * to prevent.
 */
export function classifyMemberRole(asset: AssetKindSource): MemberRole {
  const ext = extOf(asset.filename);
  if (ext === "zip") return "archive";
  if (MEMBER_MODEL_EXTS.has(ext)) return "model";
  if (MEMBER_IMAGE_EXTS.has(ext)) return "image";
  if (DOCUMENT_EXTS.has(ext)) return "text";
  return "other";
}

/** True for assets that can appear in the gallery strip (mime first, then ext). */
export function isImageAsset(asset: AssetKindSource): boolean {
  const mime = (asset.mime || "").toLowerCase();
  if (mime.startsWith("image/")) return true;
  return IMAGE_EXTS.has(extOf(asset.filename));
}

/** True for 3MF containers, which can carry a usable cover thumbnail. */
export function is3mfAsset(asset: AssetKindSource): boolean {
  if (extOf(asset.filename) === "3mf") return true;
  const mime = (asset.mime || "").toLowerCase();
  // Covers both `model/3mf` and the official vendor mime
  // `application/vnd.ms-package.3dmanufacturing-3dmodel+xml`.
  return mime.includes("3mf") || mime.includes("3dmanufacturing");
}

/** True for slicer-openable files by extension; callers OR in `prepared_print`. */
export function isModelAsset(asset: AssetKindSource): boolean {
  return MODEL_EXTS.has(extOf(asset.filename));
}

/** True for the TEXT role, by extension alone - never the mime type. `.pdf` is
 *  served as `application/pdf`, and the backend decides the role by suffix, so
 *  the browser must follow the same rule rather than guessing from mime. */
export function isDocumentAsset(asset: AssetKindSource): boolean {
  return DOCUMENT_EXTS.has(extOf(asset.filename));
}

/** Short uppercase type chip for compact file rows ("STL", "PDF", "FILE"). */
export function typeLabelFor(asset: AssetKindSource): string {
  const ext = extOf(asset.filename);
  return ext ? ext.toUpperCase() : "FILE";
}
