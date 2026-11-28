// Vendor bundle detection: a dry-run report computed from disk, plus the
// create and dismiss actions that follow it. Field names are the server's
// snake_case verbatim - the report is served straight from the API.

import { authHeaders } from "../auth";
import type { BatchFailure } from "./assets";
import { apiBase } from "./baseUrl";
import { UnauthorizedError, assertOk, readErrorMessage } from "./http";

/** The cover a created project would get, and why that one was chosen.
 *  `reason` is `largest-image | largest-image-unregistered | model-preview | none`.
 *  `url`/`thumb_url` are the served paths, set exactly when `asset_id` is. */
export type BundleCover = {
  asset_id: string | null;
  rel_path: string | null;
  reason: string;
  url: string | null;
  thumb_url: string | null;
};

/** A twin zip: provenance for the folder that is the project's truth. */
export type BundleArchive = {
  rel_path: string;
  twin_verified: boolean;
};

/** One model-bearing group inside a compound folder (`compound-bundle` / low:
 *  one product in several parts), and whether it is somebody else's thing. Only
 *  a compound row fills this; every other row serves an empty list. */
export type BundleGroup = {
  name: string;
  model_count: number;
  foreign: boolean;
  /** Was this directory a row of its own before the compound rule absorbed it?
   *  A SERVICE fact, not a detection fact: the detector's own group is the three
   *  fields above, and this one exists because only the server can answer it - it
   *  is also what says whether the server will accept an exclusion for this
   *  group, so a checkbox must never exist where it is false. */
  separable: boolean;
};

/** The rule set one report was decided against: the revision a report was taken
 *  at, and the stamp that proves what its tables were. The stamp is a sha256 of
 *  every row-deciding table, so two reports are comparable by value or provably
 *  not comparable - which is what makes an "unknown" render a dash rather than a
 *  plausible-looking number. */
export type BundleRuleSet = {
  version: string;
  stamp: string;
};

/** One path the walk saw and refused to offer, with the rule that refused it.
 *  `kind` says what the path IS on disk (`archive` or `directory`): a consumer
 *  can tell a missing row from a missing folder without a second walk. Every
 *  absence is named - that is the whole point of the ledger. */
export type BundleRefusal = {
  rel_path: string;
  rule: string;
  kind: string;
};

/** Which identity claimed a row: the `_bundle` marker the app wrote, the
 *  project's source URL, an exact casefolded name, or the bundle's own files
 *  already sitting in one project. They are not equally certain: a marker is
 *  proof, a source URL is strong, a name is a guess, and shared files are
 *  strong evidence about the files but say nothing about intent. */
export type ExistingProjectMatch = "marker" | "source_url" | "name" | "assets";

/** What a row took from its vendor page, and when. `fields` names only what
 *  THIS row took: a request may enrich several bundles, and each record speaks
 *  for its own row rather than the request's. */
export type BundleEnrichment = {
  source_url: string;
  // NULLABLE on the wire: a record written by an older build, or a cache entry
  // whose stamp was lost, still holds what the page said. The row shows those
  // fields with an unknown date rather than hiding the record or inventing one.
  fetched_at: string | null;
  fields: string[];
};

/** The server refuses more than 25 keys per request, so the client's cap and its
 *  label must be the same number: one constant feeds both, and they cannot
 *  drift into a request the server would reject. */
export const ENRICH_MAX_KEYS = 25;

/** One of the folder shapes a row could be imported as, with the engine's rule
 *  token for it and the counts and groups THAT shape would produce. The server
 *  serves the proposal engine's ranking, recommended first, so the first entry
 *  IS the recommendation and the pane offers the rest as an override. */
export type BundleMatch = {
  shape: string;
  /** The proposal engine's token for this reading, ALWAYS `engine:<shape>` - the
   *  same token the create marker records, so one spelling owns the rule. */
  rule: string;
  /** The engine's confidence band for this reading (`high | medium | low`). */
  confidence: string;
  model_count: number;
  image_count: number;
  text_count: number;
  other_count: number;
  groups?: BundleGroup[];
  note?: string | null;
  /** The ENGINE's verdict on this reading: can the server enact it TODAY?
   *  `creatable: false` marks an honest reading it cannot create from yet (a
   *  directory's `split-*` / `add-to-project`, which need arranging first, or an
   *  archive's `archive-leave`, which has nothing to import), and such a match
   *  always carries a `note` saying why. ABSENT means enactable: a payload from
   *  before this field existed is one the server could always act on, so the
   *  reader must treat it as `true` rather than refuse every old report. The
   *  create endpoint answers `needs-arrange` for a false shape and writes
   *  nothing, which is why a client must never send one. */
  creatable?: boolean;
};

/** One value the engine mined from a document, and WHERE it read it. A
 *  suggestion, never a fact: nothing is written unless the operator accepts it.
 *  `where` is the document and page, e.g. "manual.pdf p2". */
export type BundleSuggestion = {
  field: string;
  value: string;
  where: string;
  confidence: string;
};

export type BundleProvenanceCandidate = {
  url: string;
  provider: string;
  subject_id: string | null;
  where: string;
  confidence: string;
  also_in?: { where: string; confidence: string }[];
};
export type BundleProvenanceUnresolved = { text: string; where: string; reason: string };
export type BundleProvenance = {
  candidates: BundleProvenanceCandidate[];
  unresolved: BundleProvenanceUnresolved[];
  confidence: string;
};

/** One face the engine's walk found, bundle-relative. `asset_id`/`url`/`thumb_url`
 *  are set exactly when the same file is a registered asset, so a chooser only
 *  ever offers a face the create can set. */
export type BundleCoverCandidate = {
  rel_path: string;
  reason: string;
  bytes: number;
  asset_id: string | null;
  url: string | null;
  thumb_url: string | null;
};

/** One registered image of the row: the chooser's selectable faces. */
export type BundleImage = {
  asset_id: string;
  rel_path: string;
  url: string;
  thumb_url: string | null;
};

/** One detected bundle. `status` and `confidence` are orthogonal. */
export type BundleDetectItem = {
  key: string;
  rel_path: string;
  title: string;
  name_suggestion: string;
  creator: string | null;
  licence: string | null;
  licence_url: string | null;
  source_url: string | null;
  /** The engine's recommendation for this row, mirrored from `matches[0]`:
   *  `detector` is `matches[0].rule` (always `engine:<shape>`), `confidence` is
   *  the engine's band and `metadata_shape` is `matches[0].shape`. They keep
   *  their wire names, and a payload served before the engine wrote these fields
   *  still carries the older detector vocabulary - which is why the client keeps
   *  labels for those shapes too. */
  detector: string;
  confidence: string;
  metadata_shape: string;
  description_excerpt: string | null;
  model_count: number;
  image_count: number;
  text_count: number;
  other_count: number;
  // The `archive` role's count (what the folder holds as a `.zip`, normally 1).
  // OPTIONAL on purpose, like the neighbouring fields: a payload from before
  // this field was served omits it, and absent means "the server did not say",
  // so the detail pane renders an en dash rather than a fabricated 0.
  // Deliberately not on `BundleMatch`: the report artifact's before/after shape
  // pair stays comparable, and only a detect row carries the archive role.
  archive_count?: number;
  // OPTIONAL on purpose, like the neighbouring fields: a payload from before the
  // breakdown was served omits it, and every existing fixture must keep
  // compiling. `undefined` means "the server did not say". Only a compound row
  // fills it; the review detail pane renders the breakdown from it, while the
  // grouped *project* presentation and the variant filter are still C3/Mf-aaqi.
  groups?: BundleGroup[];
  // OPTIONAL on purpose, exactly like the field above: fixtures and cached
  // payloads written before the chooser existed omit it, and a reader must
  // treat that as "the recommendation is the only shape on offer" rather than
  // as an error or an empty offer. The first entry is the recommendation.
  matches?: BundleMatch[];
  cover: BundleCover;
  assets_total: number;
  status: string;
  blocked_reason: string | null;
  archive: BundleArchive | null;
  // Both are required so the compiler forces every fixture to declare the pair:
  // a payload that names an id also names its project (or says it is nameless).
  existing_project_id: string | null;
  existing_project_name: string | null;
  // OPTIONAL on purpose: a payload from before this identity existed omits it,
  // and every existing fixture (including the ones another lane owns) must keep
  // compiling. `undefined` means "the server did not say", never "assets".
  existing_project_match?: ExistingProjectMatch | null;
  // OPTIONAL on purpose, exactly as `existing_project_match` is: a payload from
  // before the source-page action existed omits it, and every existing fixture
  // must keep compiling. `undefined` means "the server did not say"; `null` is
  // the server saying this row took nothing from the page.
  enrichment?: BundleEnrichment | null;
  // The four fields below are OPTIONAL for the same reason as their neighbours:
  // a payload from before they were served omits them, and `undefined` means
  // "the server did not say" - never "the document suggested nothing".
  /** What a document CLAIMED and where, for the guided panel. */
  metadata_suggestions?: BundleSuggestion[];
  /** The provider URLs the engine mined; null for a row whose facts carry none. */
  provenance?: BundleProvenance | null;
  /** Every face the walk found, registered or not. */
  cover_candidates?: BundleCoverCandidate[];
  /** The row's registered images: the covers a create can actually set. */
  images?: BundleImage[];
  /** Does the bundle folder behind this already-created row still hold parts
   *  the project does not? `bundle_drift_state` is the server's word
   *  (`known | unknown | not_applicable`) and `bundle_drift_count` is null
   *  unless it is `known`. Both are OPTIONAL on the wire type on purpose: the
   *  six factories in the tree (`App.review.test.tsx`,
   *  `bundleCovers.test.ts`, `bundleRows.test.ts`, `useReviewCounts.test.ts`,
   *  `useBundleDetect.test.tsx`, `BundleReviewView.test.tsx`) build this object
   *  literally and are outside this change - and an absent field means a payload
   *  from before this field existed, which is `not_applicable` (nothing new to
   *  say), never `0`. */
  bundle_drift_state?: string;
  bundle_drift_count?: number | null;
  dismissed: boolean;
};

export type BundleDetectCounts = {
  /** The rows this report HAS. It keeps its old meaning: the count is the rows
   *  the walk offered, not the number the `limit` let onto the wire. */
  total: number;
  /** How many rows this report has (= `total`) and how many of them the server's
   *  `limit` kept back. Required on purpose: the server always emits both now
   *  (defaulted to 0 in the schema), so the truncated notice can say what the
   *  operator is and is not looking at instead of guessing. */
  offered: number;
  withheld: number;
  high: number;
  /** Optional: a payload from before the medium confidence simply omits it, so readers use `?? 0`. */
  medium?: number;
  low: number;
  ready: number;
  blocked: number;
  dismissed: number;
  already_created: number;
};

/** Byte-identical archives: one bundle on disk, the rest reclaimable bytes. */
export type BundleDuplicateGroup = {
  sha256: string;
  size: number;
  paths: string[];
  reclaimable_bytes: number;
};

export type BundleReport = {
  root: string;
  truncated: boolean;
  counts: BundleDetectCounts;
  bundles: BundleDetectItem[];
  /** Dismissal keys that match no live bundle any more (a directory renamed). */
  stale_dismissals: string[];
  // All three are optional because an omitted field has to stay representable as
  // UNKNOWN: the duplicate notice renders `–`, never `0`. That is the opposite
  // of `BundleDetectCounts.medium?` above, where `?? 0` IS right - a zero here
  // would claim an empty duplicate report, which is a claim the client cannot
  // make from a payload that never carried the field.
  /** The groups, or omitted when this response had no duplicate report. */
  duplicates?: BundleDuplicateGroup[];
  /** The walk's own reclaimable total, or omitted when it is unknown. */
  duplicates_total_bytes?: number;
  /** Generated by the SERVICE and copied verbatim: two formatters would drift. */
  duplicates_text?: string;
  // Optional for the same reason the `duplicates*` trio above is: a payload from
  // before these fields were served omits them, and an omitted field must stay
  // representable as UNKNOWN so the notice renders `–` rather than a false `0`.
  /** The rule set this report was decided against, or omitted when unknown. */
  rule_set?: BundleRuleSet;
  /** Every path the walk refused to offer, or omitted when the server did not say. */
  refused?: BundleRefusal[];
};

export type BundleReportQuery = {
  /** Scopes the walk; "" is the whole storage root. */
  path?: string;
  limit?: number;
  includeDismissed?: boolean;
};

export type BundleCreateItem = {
  key: string;
  /** Absent means "use the server's name suggestion". */
  name?: string | null;
  name_only?: boolean;
  /** Group names to leave out of the project; the server keeps every group when
   *  the field is absent. OMITTED entirely when nothing is excluded, so every
   *  existing row's request body stays byte-identical. */
  exclude_groups?: string[];
  /** The operator's own choice: one of the row's `matches` shapes. OMITTED when
   *  the server's recommendation stands, so an untouched row's request body is
   *  the one it always was. */
  shape?: string | null;
  /** AUTO: one action, high confidence only. The server refuses every other band
   *  and every shape it cannot do safely, so this flag never decides a band. */
  auto?: boolean;
  /** The cover the operator chose; OMITTED means the engine's choice stands (a
   *  deliberate "no cover" is a different request, not a null id). */
  cover?: { mode: "asset" | "none"; asset_id?: string | null } | null;
  /** The metadata the operator ACCEPTED: field -> value (an edit) or null (a
   *  dismiss). Present means nothing outside the map is written, so a licence the
   *  operator did not accept is never written. */
  metadata?: Record<string, string | null> | null;
};

export type BundleCreateItemResult = {
  key: string;
  status: string;
  project_id: string | null;
  name: string | null;
  assets_added: number;
  skipped_assets: number;
  failed_assets: BatchFailure[];
  reason: string | null;
};

export type BundleCreateResponse = {
  created: number;
  skipped: number;
  blocked: number;
  failed: number;
  results: BundleCreateItemResult[];
};

export type BundleDismissResponse = {
  dismissed: string[];
  stale_dismissals: string[];
};

export type BundleEnrichItemResult = {
  key: string;
  /** `fetched` (the page answered now), `cached` (a fresh record answered, no
   *  request), `refused` (the site refused; `reason` names the env var to set),
   *  `unavailable` (the bundle names no vendor id) or `unknown` (not live). */
  status: string;
  source_url: string | null;
  creator: string | null;
  licence: string | null;
  licence_url: string | null;
  /** The fields the PAGE supplied, which may be fewer than the empty ones this
   *  row still has: the page only knows what it carries. */
  filled: string[];
  fetched_at: string | null;
  reason: string | null;
};

export type BundleEnrichResponse = {
  fetched: number;
  cached: number;
  refused: number;
  unavailable: number;
  unknown: number;
  results: BundleEnrichItemResult[];
};

/** The report is a dry run: detection writes nothing to disk and no rows. */
export async function getBundleReport(params: BundleReportQuery = {}): Promise<BundleReport> {
  const qs = new URLSearchParams();
  if (params.path) qs.set("path", params.path);
  if (typeof params.limit === "number") qs.set("limit", String(params.limit));
  if (typeof params.includeDismissed === "boolean") {
    qs.set("include_dismissed", params.includeDismissed ? "true" : "false");
  }
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const res = await fetch(`${apiBase()}/bundles/detect${suffix}`, { headers: authHeaders() });
  assertOk(res, "Failed to load the bundle report");
  return res.json();
}

/** Creates one project per item; partial success is the contract. */
export async function createBundles(items: BundleCreateItem[]): Promise<BundleCreateResponse> {
  const res = await fetch(`${apiBase()}/bundles/create`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ items }),
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) {
    throw new Error(await readErrorMessage(res, "Failed to create the selected bundles"));
  }
  return res.json();
}

/** A toggle rather than a one-way dismiss, so the UI can offer an undo. */
export async function setBundleDismissed(
  keys: string[],
  dismissed: boolean,
): Promise<BundleDismissResponse> {
  const res = await fetch(`${apiBase()}/bundles/dismiss`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ keys, dismissed }),
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) {
    throw new Error(await readErrorMessage(res, "Failed to dismiss the selected bundles"));
  }
  return res.json();
}

/** Fills the gaps from each bundle's vendor page. The report is still the
 *  truth: this refetches after the call, and the response only says what
 *  happened, so the caller can tell an answered page from a refused one. */
export async function enrichBundles(
  keys: string[],
  options: { refresh?: boolean; thingiverseCookie?: string } = {},
): Promise<BundleEnrichResponse> {
  // Trim here, once, at the single body builder: `ImportCookiesCard` commits the
  // paste untrimmed, and a copied Cookie header usually ends in a newline. An
  // untrimmed-but-empty value is truthy, and the server prefers a sent cookie
  // over `THINGIVERSE_COOKIE`, so shipping `"   "` would mask a server-side
  // cookie with one that can never authenticate.
  const thingiverseCookie = (options.thingiverseCookie ?? "").trim();
  const res = await fetch(`${apiBase()}/bundles/enrich`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({
      keys,
      refresh: options.refresh ?? false,
      ...(thingiverseCookie ? { thingiverse_cookie: thingiverseCookie } : {}),
    }),
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) {
    throw new Error(await readErrorMessage(res, "Failed to read the source page"));
  }
  return res.json();
}
