// Link and zip imports. `importToProject` belongs here because its route is
// `/import/project`, alongside the rest of the import surface.

import { authHeaders } from "../auth";
import type { Asset } from "./assets";
import { apiBase } from "./baseUrl";
import { UnauthorizedError, readErrorMessage } from "./http";
import type { Project } from "./projects";

export type ImportInspectInfo = {
  filename: string;
  mime: string;
  is_zip: boolean;
};

export type ZipEntryInfo = {
  path: string;
  size: number;
  junk: boolean;
};

export type ImportZipResult = {
  assets: Asset[];
  failed: string[];
};

export async function importFromLink(payload: {
  url: string;
  title?: string;
  notes?: string;
  tags?: string[];
  folder_id?: string;
  filename?: string;
  makerworld_cookie?: string;
  thingiverse_cookie?: string;
}) {
  const res = await fetch(`${apiBase()}/import`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  if (res.status === 401) {
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    let message = "Import failed";
    try {
      const data = await res.json();
      if (typeof data?.detail === "string") message = data.detail;
    } catch {
      // ignore parse errors
    }
    throw new Error(message);
  }
  return res.json();
}

export async function inspectImportLink(payload: {
  url: string;
  title?: string;
  notes?: string;
  tags?: string[];
  folder_id?: string;
  filename?: string;
  makerworld_cookie?: string;
  thingiverse_cookie?: string;
}): Promise<ImportInspectInfo> {
  const res = await fetch(`${apiBase()}/import/inspect`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  if (res.status === 401) {
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    const message = await readErrorMessage(res, "Inspect failed");
    throw new Error(message);
  }
  return res.json();
}

export async function listImportZipEntries(payload: {
  url: string;
  title?: string;
  notes?: string;
  tags?: string[];
  folder_id?: string;
  filename?: string;
  makerworld_cookie?: string;
  thingiverse_cookie?: string;
}): Promise<{ filename: string; entries: ZipEntryInfo[] }> {
  const res = await fetch(`${apiBase()}/import/zip/entries`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  if (res.status === 401) {
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    const message = await readErrorMessage(res, "Zip listing failed");
    throw new Error(message);
  }
  return res.json();
}

export async function importZipFromLink(payload: {
  url: string;
  entries: string[];
  title?: string;
  notes?: string;
  tags?: string[];
  folder_id?: string;
  filename?: string;
  makerworld_cookie?: string;
  thingiverse_cookie?: string;
}): Promise<ImportZipResult> {
  const res = await fetch(`${apiBase()}/import/zip`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  if (res.status === 401) {
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    const message = await readErrorMessage(res, "Zip import failed");
    throw new Error(message);
  }
  return res.json();
}

/**
 * Outcome of `POST /import/project`.
 *
 * `created` is false when the same model page had already been imported, so
 * the existing project was reused. `gated` means the model's files are behind
 * an account or a purchase: the project exists with the page's details, the
 * server's `message` explains it, and nothing was downloaded. `failed` lists
 * zip entries that did not import and may be retried.
 */
export type ImportProjectResult = {
  project: Project;
  created: boolean;
  assets: Asset[];
  added: string[];
  skipped: string[];
  failed: string[];
  gated: boolean;
  message: string;
};

/**
 * Imports a model page into a project: `project_id` adds the files to an
 * existing project, `project_name` names a new one, and omitting both lets the
 * server name the project from the page. Omitting `entries` keeps the whole
 * file/zip as one asset; an array extracts exactly those zip entries.
 */
export async function importToProject(payload: {
  url: string;
  project_id?: string;
  project_name?: string;
  entries?: string[];
  title?: string;
  notes?: string;
  tags?: string[];
  folder_id?: string;
  filename?: string;
  makerworld_cookie?: string;
  thingiverse_cookie?: string;
}): Promise<ImportProjectResult> {
  const res = await fetch(`${apiBase()}/import/project`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  if (res.status === 401) {
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    let message = "Import failed";
    try {
      const data = await res.json();
      if (typeof data?.detail === "string") message = data.detail;
    } catch {
      // ignore parse errors
    }
    throw new Error(message);
  }
  return res.json();
}

// --- Archives already in the library ----------------------------------------
// A `.zip` sitting in the library is extractable in place: the two calls below
// take a library-relative path instead of an upload or a link, so an archive
// that arrived by upload, import or a library scan needs no re-import to unzip.

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

/**
 * The extract-and-create door's verdict (Mf-0ck5).
 *
 * `partial` and `nothing_resolved` created no project and carry no `project`;
 * `planned`/`resolved` are the two set sizes the server compared and `failed`
 * names every listed entry that never became a registered asset, so "how many
 * entries failed" is a fact the server measured rather than a number the client
 * inferred. `twin` is the library-relative directory to point the operator at.
 */
export type ArchiveProjectResult = {
  status: "created" | "already_created" | "partial" | "nothing_resolved";
  archive: string;
  twin: string;
  planned: number;
  resolved: number;
  failed: string[];
  project: Project | null;
};

/**
 * Declares an archive's entries to be one project: `POST /import/archive/project`.
 *
 * The server matches the entries it listed against the assets the extraction
 * registered and creates the project from those ids in one transaction, so
 * neither the queue nor detection is asked whether the folder is a bundle. Every
 * verdict answers 200; `status` is the verdict.
 */
export async function createProjectFromExtract(payload: {
  path: string;
  name?: string | null;
  entries?: string[] | null;
}): Promise<ArchiveProjectResult> {
  const res = await fetch(`${apiBase()}/import/archive/project`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(payload),
  });
  if (res.status === 401) {
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    const message = await readErrorMessage(res, "Project creation failed");
    throw new Error(message);
  }
  return res.json();
}
