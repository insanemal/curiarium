// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { strToU8, zipSync } from "fflate";

import { isJunkZipPath } from "./zipJunk";
import { buildUploadEntriesFromZip, readZipEntries } from "./zipUtils";

// Mirrors the table in `curiarium/api/entry_policy.py`: the two must agree, because
// the browser's copy decides both what the prompt pre-ticks and what a locally
// picked zip may upload. `curiarium/api/tests/test_junk_vocabulary_mirror.py`
// compares this vector set with the Python one, so drift fails a Python test.
const JUNK_PATHS = [
  "__MACOSX/._part.stl",
  "__MACOSX/a/._nested.stl",
  ".DS_Store",
  "a/.DS_Store",
  "a/b/Thumbs.db",
  "desktop.ini",
  "._part.stl",
  ".Spotlight-V100/x",
  "$RECYCLE.BIN/S-1-5/x.stl",
  "A/B/.DS_STORE",
  "x\\__MACOSX\\y.stl",
];

const PAYLOAD_PATHS = [
  "part.stl",
  "a/b/nested.stl",
  "notes.txt",
  "model.ds_store.stl",
  "_part.stl",
  "a/b",
  "my__MACOSX.stl",
  "thumbs.dbx",
  "",
];

describe("isJunkZipPath", () => {
  it.each(JUNK_PATHS)("treats %s as OS metadata", path => {
    expect(isJunkZipPath(path), `expected ${JSON.stringify(path)} to be junk`).toBe(true);
  });

  it.each(PAYLOAD_PATHS)("treats %s as importable", path => {
    expect(isJunkZipPath(path), `expected ${JSON.stringify(path)} to be payload`).toBe(false);
  });
});

/**
 * The local door. A zip the user picks is never listed server-side: its entries
 * are read in the browser and uploaded as ordinary files, so nothing downstream
 * meets the server's zip guard. These two are what stops OS metadata riding in
 * through that door - `readZipEntries` flags it, `buildUploadEntriesFromZip`
 * refuses it.
 */
describe("the locally picked zip", () => {
  const archive = () =>
    zipSync({
      "part.stl": strToU8("solid cube"),
      ".DS_Store": strToU8("junk"),
      "__MACOSX/._part.stl": strToU8("junk"),
    });

  /** jsdom's File has no `arrayBuffer()`, so the reader gets a File-shaped stub. */
  function zipFile(): File {
    const bytes = archive();
    const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    return { arrayBuffer: async () => buffer } as unknown as File;
  }

  it("flags the archive's OS metadata when it reads the entries", async () => {
    const { entries } = await readZipEntries(zipFile());

    const flagged = entries.filter(entry => entry.junk).map(entry => entry.path);
    expect(flagged.sort()).toEqual([".DS_Store", "__MACOSX/._part.stl"]);
    expect(entries.find(entry => entry.path === "part.stl")?.junk).toBe(false);
  });

  it("uploads only the payload when a revealed junk row is ticked", async () => {
    const data: Record<string, Uint8Array> = {
      "part.stl": strToU8("solid cube"),
      ".DS_Store": strToU8("junk"),
      "__MACOSX/._part.stl": strToU8("junk"),
    };

    const built = buildUploadEntriesFromZip(
      data,
      ["part.stl", ".DS_Store", "__MACOSX/._part.stl"],
      "picked"
    );

    expect(built.map(entry => entry.relativePath)).toEqual(["picked/part.stl"]);
    expect(built.map(entry => entry.file.name)).toEqual(["part.stl"]);
  });
});
