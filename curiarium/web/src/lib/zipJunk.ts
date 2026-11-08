/**
 * Hand-maintained mirror of `curiarium/api/entry_policy.py`.
 *
 * The Python predicate is the authority: it decides what a server-side zip door
 * may import, and those doors refuse junk whatever the browser sends. This
 * mirror has a second job the server cannot do, because a locally picked zip is
 * never listed server-side and is uploaded as ordinary files through the plain
 * upload route - it never meets `zip_service.extract_zip_entries_to_assets`.
 * So this file decides BOTH the prompt's default ticks and the local door's
 * refusal (`buildUploadEntriesFromZip` in `./zipUtils`).
 *
 * The mirror is bound, not only maintained by hand:
 * `curiarium/api/tests/test_junk_vocabulary_mirror.py` compares the three
 * tables and both test-vector sets, so vocabulary drift fails a Python test.
 *
 * Drift therefore matters more here than a mis-ticked default: a path this file
 * fails to recognise is pre-ticked and then, on a locally picked zip, uploaded
 * as a real file. Widen both tables together, in
 * `curiarium/api/entry_policy.py` first.
 */

const JUNK_SEGMENTS = new Set(["__macosx", "$recycle.bin"]);
const JUNK_BASENAMES = new Set([".ds_store", "thumbs.db", "desktop.ini"]);
const JUNK_PREFIXES = ["._", ".spotlight-"];

/**
 * Matching is per path segment, so a junk directory makes everything beneath
 * it junk too (a `._foo/` folder and its contents). Both separators are
 * accepted because zip listings arrive with either.
 */
export function isJunkZipPath(path: string): boolean {
  const segments = (path || "")
    .toLowerCase()
    .split(/[\\/]/)
    .filter(Boolean);
  return segments.some(segment => {
    if (JUNK_SEGMENTS.has(segment) || JUNK_BASENAMES.has(segment)) return true;
    return JUNK_PREFIXES.some(prefix => segment.startsWith(prefix));
  });
}
