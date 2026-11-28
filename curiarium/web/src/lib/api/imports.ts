// What is left of the import surface after the write doors became operations.
// Every route that used to live here is gone: `POST /import`,
// `POST /import/zip`, `POST /import/project`, `GET /import/archive/entries` and
// `POST /import/archive`. Link and zip imports are the `import-link` and
// `import-zip` operations, extracting a library archive is the `extract`
// operation, and a project import is those same workflows' `attach-project`
// step - all driven through the operations ledger (`./operations`). The two
// read-only preflights (`POST /import/inspect`, `POST /import/zip/entries`)
// survive here because they are what a client needs to REVIEW a fetch before it
// is planned, and they write nothing: a URL's filename, mime and zip-ness, and a
// URL zip's entry listing. Both are `contribute` on the server (they resolve
// DNS), and both are served below.
//
// Two TYPES survive as well, because pure modules outside `lib/api` still speak
// them: `ui/archive/archivePlan.ts` and `ui/review/extractAndCreate.ts` classify
// a file listing, and `ui/review/useBundleDetect.ts`/`lib/scanSettle.ts` read an
// extraction's own report.

import { authHeaders } from "../auth";
import { apiBase } from "./baseUrl";
import { UnauthorizedError, readErrorMessage } from "./http";

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

/** One member of a URL zip, as `POST /import/zip/entries` lists it. `junk` is the
 *  server's verdict that the member is OS metadata, and the entry picker uses it
 *  to choose the modal's default ticks. */
export type ZipEntry = { path: string; size: number; junk: boolean };

/** A URL's preflight answer: the filename the server would give it, its mime and
 *  whether it is a zip. It reads `POST /import/inspect`, which resolves DNS and
 *  writes nothing. */
export type ImportInspectInfo = { filename: string; mime: string; is_zip: boolean };

/** The body every import preflight takes: the same shape the `import-link` and
 *  `import-zip` workflows accept, minus anything the plan computes. */
export type ImportRequestBody = {
  url: string;
  title?: string;
  notes?: string;
  tags?: string[];
  folder_id?: string;
  filename?: string;
  makerworld_cookie?: string;
  thingiverse_cookie?: string;
};

/** Reads what a URL import would fetch, before it is planned: the filename, mime
 *  and whether the payload is a zip. It reads `POST /import/inspect`, a
 *  read-only preflight that survives because a client has to review a fetch
 *  before it is planned; it writes nothing. */
export async function inspectImportLink(body: ImportRequestBody): Promise<ImportInspectInfo> {
  const res = await fetch(`${apiBase()}/import/inspect`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) {
    throw new Error(await readErrorMessage(res, "Failed to inspect the import link"));
  }
  return res.json();
}

/** Lists a URL zip's entries, the picker's source for choosing what to import.
 *  It reads `POST /import/zip/entries`, a read-only preflight that survives
 *  because a client has to review the members before it is planned; it writes
 *  nothing. */
export async function listImportZipEntries(
  body: ImportRequestBody,
): Promise<{ filename: string; entries: ZipEntry[] }> {
  const res = await fetch(`${apiBase()}/import/zip/entries`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) {
    throw new Error(await readErrorMessage(res, "Failed to list the import zip's entries"));
  }
  return res.json();
}
