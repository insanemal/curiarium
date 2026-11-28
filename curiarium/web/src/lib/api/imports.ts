// What is left of the import surface after the write doors became operations.
// Every route that used to live here is gone: `POST /import`,
// `POST /import/zip`, `POST /import/project`, `GET /import/archive/entries` and
// `POST /import/archive`. Link and zip imports are the `import-link` and
// `import-zip` operations, extracting a library archive is the `extract`
// operation, and a project import is those same workflows' `attach-project`
// step - all driven through the operations ledger (`./operations`). The
// read-only preflights (`POST /import/inspect`, `POST /import/zip/entries`) still
// answer, but no client calls them, so they have no function here either.
//
// Two TYPES survive, because pure modules outside `lib/api` still speak them:
// `ui/archive/archivePlan.ts` and `ui/review/extractAndCreate.ts` classify a
// file listing, and `ui/review/useBundleDetect.ts`/`lib/scanSettle.ts` read an
// extraction's own report.

/**
 * One member of an archive: a path inside it and the bytes it holds.
 *
 * Nothing fills one from the wire any more - the listing route that served this
 * shape is deleted, and an extract plan's review carries the census instead
 * (`members`, `groups`, `excluded`). The shape stays exported because the two
 * pure modules that decide what a listing MEANS (`ui/archive/archivePlan.ts`,
 * `ui/review/extractAndCreate.ts`) still take it, and their own callers are what
 * say where a listing came from.
 */
export type ArchiveEntry = { path: string; size: number };

/**
 * What one extraction wrote, exactly as the `extract` workflow's `extract` step
 * reports it: `path` and `destination` are library-relative, `files`/`bytes` are
 * what landed on disk, `already_present` says the destination was already there
 * (so nothing was written), and `scan_started` is the extractor's own flag - the
 * `extract` step runs with the walk off, because the `register` step after it
 * registers those files in-process.
 */
export type ArchiveExtractResult = {
  path: string;
  destination: string;
  files: number;
  bytes: number;
  already_present: boolean;
  scan_started: boolean;
};
