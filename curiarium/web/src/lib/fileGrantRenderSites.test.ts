/**
 * Ticket-Mf-8a4e acceptance guard: every source file that resolves a
 * `fileUrl(...)` into browser-loaded markup must keep that path granted, either
 * through `useFileGrants` (silent: `<img src>`) or `useLiveFileGrants` (live:
 * `<a href>`). ADR 0005 decision 10: a grant lives 60 s, so a render site with
 * no keeper goes blank a minute after mount.
 *
 * Still a source scan rather than an ESLint rule, in the idiom of
 * `noNativeDialogs.test.ts`: it reports every offending `file:line` at once
 * instead of stopping at the first. Sources come from Vite's raw glob rather
 * than `node:fs` because the app has no `@types/node` and must not gain one.
 *
 * Honest limitation: this is file-level, not a linter. A `fileUrl()` call
 * added to a file that already imports a keeper passes, even if that
 * particular call renders into an element the keeper does not cover. The
 * behavioural half of the contract lives in `ui/projectFileGrants.test.tsx`.
 */

import { describe, expect, it } from "vitest";

// Keys are relative to this test file, e.g. `"../ui/ProjectLanding.tsx"`.
const RAW_SOURCES = import.meta.glob("../**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** This test lives in `src/lib`, and glob keys are relative to it. */
const SELF_DIR = "lib";
/** This test's own path, relative to `src`. */
const SELF = `${SELF_DIR}/fileGrantRenderSites.test.ts`;
const FILE_URL_CALL = /\bfileUrl\s*\(/;
const TEST_FILE = /\.test\.tsx?$/;
/**
 * An import of either keeper, as written in `src`: `import { useFileGrants }
 * from "../lib/useFileGrants"`. Two names, one module: the hook file defines
 * both variants.
 */
const KEEPER_IMPORT =
  /import\s*\{[^}]*\buse(?:Live)?FileGrants\b[^}]*\}\s*from\s*["'][^"']*useFileGrants["']/;

/**
 * `export function fileUrl(rel: string)` here is the definition, not a call
 * site, so it is skipped explicitly rather than allowlisted. Every other
 * render site is a caller.
 */
const FILE_URL_DEFINITION = "lib/api/mediaUrls.ts";

/**
 * The only files allowed to call `fileUrl` without importing a keeper, each
 * with the reason it may. Keyed by `src`-relative path.
 */
export const FILE_GRANT_ALLOWLIST: Record<string, string> = {
  "ui/asset-grid/useAssetActions.ts":
    "The slicer/engraver OS handoff: it `await primeGrants([...])` immediately before assigning `window.location.href` (ADR 0005 §7/§10), so it primes at click time and renders nothing.",
  "lib/api/downloads.ts":
    "`downloadAsset` is a real `fetch` that sends `authHeaders()`; it is not a render site and needs no keeper.",
};

/**
 * Glob keys are relative to this file (`"./toast.ts"`, `"../ui/AssetGrid.tsx"`);
 * resolve them to paths relative to `src` without pulling in `node:path`.
 */
function toSourcePath(globKey: string): string {
  const resolved: string[] = [];
  for (const segment of `${SELF_DIR}/${globKey}`.split("/")) {
    if (segment === "..") resolved.pop();
    else if (segment && segment !== ".") resolved.push(segment);
  }
  return resolved.join("/");
}

/** Every `.ts`/`.tsx` source under `src` except this test, keyed by `src`-relative path. */
const SOURCES: Record<string, string> = {};
for (const [globKey, text] of Object.entries(RAW_SOURCES)) {
  const file = toSourcePath(globKey);
  if (file !== SELF) SOURCES[file] = text;
}

const FILES = Object.keys(SOURCES).sort();

function read(file: string): string {
  return SOURCES[file];
}

/** A keeper import is the whole compliance test for a file (see the docstring). */
function importsKeeper(text: string): boolean {
  return KEEPER_IMPORT.test(text);
}

/**
 * Lines that document `fileUrl()` in prose are not call sites. This is a line
 * prefix check, not a parser: a trailing comment on a code line is still
 * scanned, and that is the conservative direction.
 */
function isCommentLine(line: string): boolean {
  const trimmed = line.trim();
  return trimmed.startsWith("//") || trimmed.startsWith("/*") || trimmed.startsWith("*");
}

/**
 * Every `fileUrl()` line in a file that renders it without a keeper. Returns
 * all of them, so one run reports the whole backlog.
 */
function renderSiteOffences(): string[] {
  const violations: string[] = [];
  for (const file of FILES) {
    if (TEST_FILE.test(file)) continue;
    if (file === FILE_URL_DEFINITION) continue;
    if (Object.prototype.hasOwnProperty.call(FILE_GRANT_ALLOWLIST, file)) continue;
    const text = read(file);
    if (!FILE_URL_CALL.test(text)) continue;
    if (importsKeeper(text)) continue;
    text.split("\n").forEach((line, index) => {
      if (isCommentLine(line)) return;
      if (FILE_URL_CALL.test(line)) violations.push(`${file}:${index + 1}: ${line.trim()}`);
    });
  }
  return violations;
}

describe("fileUrl render sites keep their grants warm", () => {
  it("every file that renders fileUrl() imports useFileGrants or useLiveFileGrants", () => {
    expect(
      renderSiteOffences(),
      "Mf-8a4e: a rendered fileUrl() with no grant keeper goes blank 60 s after mount (ADR 0005 decision 10). Import useFileGrants (silent <img>) or useLiveFileGrants (<a href>).",
    ).toEqual([]);
  });

  it("pins the allowlist to files that really call fileUrl, so a stale exemption cannot survive", () => {
    for (const file of Object.keys(FILE_GRANT_ALLOWLIST)) {
      expect(FILES).toContain(file);
      expect(FILE_URL_CALL.test(read(file))).toBe(true);
    }
  });

  it("the asset preview is already a hooked render site and is not exempt", () => {
    expect(FILES).toContain("ui/asset-grid/preview.tsx");
    expect(importsKeeper(read("ui/asset-grid/preview.tsx"))).toBe(true);
    expect(Object.keys(FILE_GRANT_ALLOWLIST)).not.toContain("ui/asset-grid/preview.tsx");
  });

  it("skips the fileUrl definition itself rather than treating it as a call site", () => {
    expect(FILES).toContain(FILE_URL_DEFINITION);
    expect(read(FILE_URL_DEFINITION)).toContain("export function fileUrl(rel: string)");
    expect(Object.keys(FILE_GRANT_ALLOWLIST)).not.toContain(FILE_URL_DEFINITION);
  });

  it("scans the real source tree (never passes on an empty file list)", () => {
    expect(FILES).toContain("ui/ProjectLanding.tsx");
    expect(FILES).toContain("lib/api/mediaUrls.ts");
    expect(FILES.length).toBeGreaterThan(30);
  });
});
