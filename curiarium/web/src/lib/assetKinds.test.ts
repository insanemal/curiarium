import { describe, expect, it } from "vitest";

// The backend's own TEXT-role table, read verbatim: the cross-language pin below
// fails if the web set and `bundle_detect.BUNDLE_TEXT_EXTS` drift apart. Vite's
// `?raw` query is the repo's idiom for reading a non-TS source (the palette and
// no-native-dialogs tests use it too), so this file needs no `node:fs`.
import bundleDetectSource from "../../../api/bundle_detect.py?raw";
// The scan's image table, read verbatim for the same reason: it is the suffix
// set `classify_entry` asks for the IMAGE role.
import libraryScanSource from "../../../api/library_scan.py?raw";
// The import allow-list, from which `bundle_detect.MODEL_SUFFIXES` is DERIVED
// (`frozenset(IMPORT_ALLOWED_EXTS - {".zip"})`). Read verbatim so the model
// role is parsed from its real source instead of restated as a literal here.
import configSource from "../../../api/config.py?raw";

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
  it("mirrors bundle_detect.BUNDLE_TEXT_EXTS", () => {
    // Parsed, not imported: the Python table is the other side of a language
    // boundary, and reading it here fails the web suite the moment the two
    // tables drift rather than waiting for a Python mirror test.
    const literal = /BUNDLE_TEXT_EXTS\s*=\s*frozenset\(\{([^}]*)\}\)/.exec(bundleDetectSource);
    expect(literal).not.toBeNull();
    const pythonExts = (literal?.[1].match(/["']\.[^"']*["']/g) ?? []).map(quoted =>
      quoted.slice(1, -1),
    );
    const webExts = [...DOCUMENT_EXTS].map(ext => "." + ext);
    expect(
      new Set(webExts),
      "the web DOCUMENT_EXTS and bundle_detect.BUNDLE_TEXT_EXTS drifted",
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
  it("mirrors library_scan.IMAGE_SUFFIXES", () => {
    const literal = /IMAGE_SUFFIXES\s*=\s*\{([^}]*)\}/.exec(libraryScanSource);
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

  it("mirrors config.IMPORT_ALLOWED_EXTS minus .zip", () => {
    // The DERIVATION first, so a change to how the model table is built fails here
    // and tells a reader to re-derive, rather than comparing this web set against
    // a table that no longer means the same thing.
    expect(bundleDetectSource).toMatch(
      /MODEL_SUFFIXES\s*=\s*frozenset\(\s*IMPORT_ALLOWED_EXTS\s*-\s*\{[\s\S]*?\.zip[\s\S]*?\}\s*\)/,
    );
    const literal = /^IMPORT_ALLOWED_EXTS\s*=\s*\{([^}]*)\}/m.exec(configSource);
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
