// Import surface that outlived the write-door routes. `POST /import`,
// `POST /import/zip` and `POST /import/project` are gone: link and zip imports
// are the `import-link` and `import-zip` operations now, driven through the
// operations ledger (`./operations`). The read-only preflights
// (`POST /import/inspect`, `POST /import/zip/entries`) still answer, but no
// client calls them, so they have no function here either.

import { authHeaders } from "../auth";
import { apiBase } from "./baseUrl";
import { UnauthorizedError, readErrorMessage } from "./http";

// --- Archives already in the library ----------------------------------------
// A `.zip` sitting in the library is readable and extractable in place: the two
// calls below take a library-relative path instead of an upload or a link, so an
// archive that arrived by upload, import or a library scan needs no re-import to
// unzip. Turning the extraction into a project is no longer a call here: it is
// the `extract-and-create` operation, driven through the operations ledger.

// The archive payload also carries `junk`, but no client reads it there, so it
// stays off the type.
export type ArchiveEntry = { path: string; size: number };

export type ArchiveExtractResult = {
  path: string;
  destination: string;
  files: number;
  bytes: number;
  already_present: boolean;
  scan_started: boolean;
};

/** What the archive holds: `GET /import/archive/entries`. */
export async function listArchiveEntries(
  path: string,
): Promise<{ path: string; entries: ArchiveEntry[] }> {
  // `URLSearchParams` encodes the user's path; the query is never concatenated.
  const query = new URLSearchParams({ path }).toString();
  const res = await fetch(`${apiBase()}/import/archive/entries?${query}`, {
    headers: authHeaders(),
  });
  if (res.status === 401) {
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    const message = await readErrorMessage(res, "Archive listing failed");
    throw new Error(message);
  }
  return res.json();
}

/**
 * Extracts a library archive into a new folder beside itself: `entries: null`
 * takes the whole archive, an array takes exactly those entries. A destination
 * that already holds files answers 409 rather than overwriting them.
 */
export async function extractArchive(payload: {
  path: string;
  entries?: string[] | null;
}): Promise<ArchiveExtractResult> {
  const res = await fetch(`${apiBase()}/import/archive`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  if (res.status === 401) {
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    const message = await readErrorMessage(res, "Extraction failed");
    throw new Error(message);
  }
  return res.json();
}
