import { describe, expect, it } from "vitest";

// The backend's Python tables, read verbatim so the cross-language pins below fail
// the moment the two sides drift. Read through a raw GLOB, not three `?raw`
// imports: the web image's build context is `curiarium/web`, so `../../../api/*.py`
// does not exist in-container, and a STATIC import of it fails to transform - which
// takes this whole file down instead of skipping three cross-tree pins (AGENTS.md
// guardrail 3; this exact defect shipped a red full gate on 2026-11-25). An eager
// glob that matches nothing yields an empty map, so the pins run on the host and
// skip in the image. Still no `node:fs`: the app has no `@types/node` and must not
// gain one.
const API_SOURCES = import.meta.glob("../../../api/*.py", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** One API source by basename, or `null` when the repo tree is not visible. */
function apiSource(name: string): string | null {
  return API_SOURCES[`../../../api/${name}`] ?? null;
}

/**
 * A cross-tree pin's source, for use INSIDE a test the matching `skipIf` guards.
 *
 * Throwing rather than returning `null` keeps the tests type-safe without a `!`:
 * a pin whose source vanished after its guard passed is a real failure, not an
 * absence.
 */
function apiSourceOrThrow(name: string): string {
  const text = apiSource(name);
  if (text === null) {
    throw new Error(`${name} is absent although the cross-tree pin was not skipped`);
  }
  return text;
}

// The scan's own tables, read verbatim: the cross-language pins below fail if the
// web sets and the backend's drift apart. Since Mf-1agz the TEXT and MODEL role
// tables live in `library_scan.py` (with `classify_entry`, the one owner of a
// member's role); `bundle_detect.py` is deleted.
const libraryScanSource = apiSource("library_scan.py");
// The import allow-list, from which `library_scan.MODEL_SUFFIXES` is DERIVED
// (`frozenset(IMPORT_ALLOWED_EXTS - {".zip"})`). Read verbatim so the model
// role is parsed from its real source instead of restated as a literal here.
const configSource = apiSource("config.py");

import {
  DOCUMENT_EXTS,
  MEMBER_IMAGE_EXTS,
  MEMBER_MODEL_EXTS,
  classifyMemberRole,
  extOf,
  is3mfAsset,
  isDocumentAsset,
  isImageAsset,
  isModelAsset,
  typeLabelFor,
  type MemberRole,
} from "./assetKinds";
import { formatDate } from "./format";

describe("extOf", () => {
  it("extracts the lowercase extension", () => {
    expect(extOf("photo.JPG")).toBe("jpg");
    expect(extOf("archive.tar.gz")).toBe("gz");
    expect(extOf("model.StL")).toBe("stl");
  });

  it("returns an empty string for dotless, trailing-dot and dotfile names", () => {
    expect(extOf("README")).toBe("");
    expect(extOf("backup.")).toBe("");
    expect(extOf(".gitignore")).toBe("");
  });
});

describe("isImageAsset", () => {
  it("detects browser-renderable image extensions, case-insensitively", () => {
    for (const filename of [
      "a.png",
      "b.jpg",
      "c.jpeg",
      "d.webp",
      "e.gif",
      "f.avif",
      "g.bmp",
      "H.PNG",
    ]) {
      expect(isImageAsset({ filename, mime: "" })).toBe(true);
    }
  });

  it("falls back to the mime type for dotless filenames", () => {
    expect(isImageAsset({ filename: "README", mime: "image/png" })).toBe(true);
    expect(isImageAsset({ filename: "README", mime: "" })).toBe(false);
  });

  it("rejects models and documents", () => {
    expect(isImageAsset({ filename: "benchy.stl", mime: "model/stl" })).toBe(false);
    expect(isImageAsset({ filename: "doc.pdf", mime: "application/pdf" })).toBe(false);
  });
});

describe("is3mfAsset", () => {
  it("detects 3MF by extension, case-insensitively", () => {
    expect(is3mfAsset({ filename: "benchy.3mf", mime: "" })).toBe(true);
    expect(is3mfAsset({ filename: "BENCHY.3MF", mime: "" })).toBe(true);
    expect(is3mfAsset({ filename: "benchy.stl", mime: "" })).toBe(false);
  });

  it("detects 3MF by mime when the filename is dotless", () => {
    expect(
      is3mfAsset({
        filename: "benchy",
        mime: "application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
      }),
    ).toBe(true);
  });
});

describe("isModelAsset", () => {
  it("matches the slicer-openable extensions, case-insensitively", () => {
    for (const filename of ["a.stl", "b.3mf", "c.step", "d.stp", "e.obj", "F.STL"]) {
      expect(isModelAsset({ filename, mime: "" })).toBe(true);
    }
  });

  it("rejects images, documents and dotless names", () => {
    expect(isModelAsset({ filename: "a.png", mime: "image/png" })).toBe(false);
    expect(isModelAsset({ filename: "doc.pdf", mime: "application/pdf" })).toBe(false);
    expect(isModelAsset({ filename: "README", mime: "" })).toBe(false);
  });
});

describe("isDocumentAsset", () => {
  it("is true for the TEXT-role extensions, case-insensitively", () => {
    for (const filename of [
      "manual.pdf",
      "NOTES.TXT",
      "readme.md",
      "page.html",
      "page.htm",
      "doc.rtf",
    ]) {
      expect(isDocumentAsset({ filename, mime: "" })).toBe(true);
    }
    // `.pdf` is served as `application/pdf`; the extension alone decides.
    expect(isDocumentAsset({ filename: "manual.pdf", mime: "application/pdf" })).toBe(true);
  });

  it("is false for models, images, archives, backups and dotless names", () => {
    for (const filename of ["benchy.stl", "photo.jpg", "part.stlbak", "archive.zip", "README"]) {
      expect(isDocumentAsset({ filename, mime: "" })).toBe(false);
    }
  });
});

describe("DOCUMENT_EXTS", () => {
  it.skipIf(libraryScanSource === null)("mirrors library_scan.BUNDLE_TEXT_EXTS", () => {
    // Parsed, not imported: the Python table is the other side of a language
    // boundary, and reading it here fails the web suite the moment the two
    // tables drift rather than waiting for a Python mirror test.
    const literal = /BUNDLE_TEXT_EXTS\s*=\s*frozenset\(\{([^}]*)\}\)/.exec(
      apiSourceOrThrow("library_scan.py"),
    );
    expect(literal).not.toBeNull();
    const pythonExts = (literal?.[1].match(/["']\.[^"']*["']/g) ?? []).map(quoted =>
      quoted.slice(1, -1),
    );
    const webExts = [...DOCUMENT_EXTS].map(ext => "." + ext);
    expect(
      new Set(webExts),
      "the web DOCUMENT_EXTS and library_scan.BUNDLE_TEXT_EXTS drifted",
    ).toEqual(new Set(pythonExts));
  });
});

describe("classifyMemberRole", () => {
  function roleOf(filename: string, mime = ""): MemberRole {
    return classifyMemberRole({ filename, mime });
  }

  it("gives the backend's role where the affordance predicates disagree", () => {
    // `.obj` opens in a slicer, but `classify_entry` calls it `other`.
    expect(roleOf("scan.obj")).toBe("other");
    // LightBurn files are `model` to the backend, though no slicer here opens them.
    expect(roleOf("sign.lbrn")).toBe("model");
    expect(roleOf("sign.lbrn2")).toBe("model");
    // A browser paints all three; the backend's image table holds none of them.
    expect(roleOf("loop.gif")).toBe("other");
    expect(roleOf("logo.svg")).toBe("other");
    expect(roleOf("scan.tif")).toBe("other");
    // Not a table entry anywhere.
    expect(roleOf("part.stlbak")).toBe("other");
    expect(roleOf("scene.3dm")).toBe("other");
    expect(roleOf("README")).toBe("other");
  });

  it("gives the roles both sides agree on", () => {
    expect(roleOf("archive.zip")).toBe("archive");
    expect(roleOf("manual.pdf")).toBe("text");
    expect(roleOf("photo.png")).toBe("image");
    expect(roleOf("body.stl")).toBe("model");
    expect(roleOf("sign.3mf")).toBe("model");
  });

  it("decides by extension, case-insensitively, and never by mime", () => {
    expect(roleOf("MODEL.STL")).toBe("model");
    expect(roleOf("SCAN.OBJ")).toBe("other");
    expect(roleOf("BACKUP.ZIP")).toBe("archive");
    // A truthful `image/png` mime cannot promote a `.gif`, and a lying one
    // cannot promote a suffix the table does not hold.
    expect(roleOf("loop.gif", "image/png")).toBe("other");
    expect(roleOf("README", "image/png")).toBe("other");
  });
});

describe("member role tables", () => {
  it.skipIf(libraryScanSource === null)("mirrors library_scan.IMAGE_SUFFIXES", () => {
    const literal = /IMAGE_SUFFIXES\s*=\s*\{([^}]*)\}/.exec(apiSourceOrThrow("library_scan.py"));
    expect(literal).not.toBeNull();
    const pythonExts = (literal?.[1].match(/["']\.[^"']*["']/g) ?? []).map(quoted =>
      quoted.slice(1, -1),
    );
    const webExts = [...MEMBER_IMAGE_EXTS].map(ext => "." + ext);
    expect(
      new Set(webExts),
      "the web MEMBER_IMAGE_EXTS and library_scan.IMAGE_SUFFIXES drifted",
    ).toEqual(new Set(pythonExts));
  });

  it.skipIf(libraryScanSource === null || configSource === null)(
    "mirrors config.IMPORT_ALLOWED_EXTS minus .zip",
    () => {
    // The DERIVATION first, so a change to how the model table is built fails here
    // and tells a reader to re-derive, rather than comparing this web set against
    // a table that no longer means the same thing.
    expect(apiSourceOrThrow("library_scan.py")).toMatch(
      /MODEL_SUFFIXES\s*=\s*frozenset\(\s*IMPORT_ALLOWED_EXTS\s*-\s*\{[\s\S]*?\.zip[\s\S]*?\}\s*\)/,
    );
    const literal = /^IMPORT_ALLOWED_EXTS\s*=\s*\{([^}]*)\}/m.exec(
      apiSourceOrThrow("config.py"),
    );
    expect(literal, "config.IMPORT_ALLOWED_EXTS is no longer a literal set").not.toBeNull();
    const allowed = (literal?.[1].match(/["']\.[^"']*["']/g) ?? []).map(quoted =>
      quoted.slice(1, -1),
    );
    expect(allowed, "config.IMPORT_ALLOWED_EXTS parsed as empty").not.toHaveLength(0);
    const derived = new Set(allowed.filter(ext => ext !== ".zip"));
    expect(
      new Set([...MEMBER_MODEL_EXTS].map(ext => "." + ext)),
      "the web MEMBER_MODEL_EXTS and config.IMPORT_ALLOWED_EXTS - .zip drifted",
    ).toEqual(derived);
  });
});

describe("typeLabelFor", () => {
  it("uppercases the extension and falls back to FILE", () => {
    expect(typeLabelFor({ filename: "body.stl", mime: "" })).toBe("STL");
    expect(typeLabelFor({ filename: "photo.webp", mime: "" })).toBe("WEBP");
    expect(typeLabelFor({ filename: "README", mime: "" })).toBe("FILE");
  });
});

describe("formatDate", () => {
  it("formats an ISO timestamp as a short human date", () => {
    const out = formatDate("2026-01-02T10:20:30Z");
    expect(out).toBeTruthy();
    expect(out).toContain("2026");
    expect(out).not.toContain("Invalid");
  });

  it("returns an empty string for nonsense input", () => {
    expect(formatDate("not-a-date")).toBe("");
    expect(formatDate("")).toBe("");
  });
});
