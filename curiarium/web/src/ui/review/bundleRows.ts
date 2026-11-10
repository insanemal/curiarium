// The bundle queue's pure presentation rules. Keeping them out of the view
// lets the render stay declarative and the wording stay unit-testable without
// mounting a component.

import {
  ENRICH_MAX_KEYS,
  type BundleDetectCounts,
  type BundleDetectItem,
} from "../../lib/api/bundles";
import { DRIFT_KNOWN, DRIFT_NOT_APPLICABLE } from "../projectBundleDrift";
// Declaration-only import cycles, proved green by these suites: this module
// imports the count formatter from `useReviewCounts`, which imports
// `needsDecision` back from here, and `projectBundleDrift` imports the same
// formatter. Every module involved only declares functions and constants, none
// reads another's value at module-evaluation time, and the repo's eslint config
// has no `import/no-cycle` - so the en dash stays the ONE formatter instead of a
// copy per module.
import { formatReviewCount } from "./useReviewCounts";

export type BundleFilter = "all" | "ready" | "blocked" | "medium" | "low";

export const BUNDLE_FILTERS: { id: BundleFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "ready", label: "Ready" },
  { id: "blocked", label: "Blocked" },
  // Ordered as the confidences read, high to low, so the chips are a ladder an
  // operator can walk down rather than a set in arrival order.
  { id: "medium", label: "Medium confidence" },
  { id: "low", label: "Low confidence" },
];

/** The server's blocked reasons, said the way an operator would say them. */
export const BLOCKED_REASON_LABELS: Record<string, string> = {
  unresolved_directory: "no matching folder on disk",
  not_scanned: "no model file registered by a scan",
  archive_only: "zip only - extract the archive in place, then the scan registers it",
};

export function headline(counts: BundleDetectCounts): string {
  const bundles = counts.total === 1 ? "1 bundle" : `${counts.total} bundles`;
  // The `?? 0` is what makes an older payload safe: a report written before the
  // medium confidence omits the field, and this shows `0 medium` rather than
  // `undefined medium`.
  const medium = counts.medium ?? 0;
  return `${bundles} found - ${counts.high} high confidence, ${medium} medium, ${counts.low} low`;
}

export function matchesFilter(row: BundleDetectItem, filter: BundleFilter): boolean {
  if (filter === "ready") return row.status === "ready";
  if (filter === "blocked") return row.status !== "ready";
  if (filter === "medium") return row.confidence === "medium";
  if (filter === "low") return row.confidence === "low";
  return true;
}

export function isBlocked(row: BundleDetectItem): boolean {
  return row.status !== "ready";
}

/** A row this queue can actually create from. Already-created rows are not
 *  creatable: the server can only answer `skipped`/`already_created` for them,
 *  so sending them would be a request that cannot create anything. The id is
 *  the identity, so a nameless project is still settled. */
export function isCreatable(row: BundleDetectItem): boolean {
  return !isBlocked(row) && !row.existing_project_id;
}

/** Whether THIS REPORT says the row's key holds registered files. It is a claim about one
 *  dry-run report, never a promise about what is on disk when a button is clicked: the report
 *  is a walk somebody asked for, and a later scan can register more files under the same key.
 *  SO NO WRITE DECISION MAY READ THIS. A create, an extract or an attach keeps asking the
 *  server, which re-reads disk inside its own transaction; `isCreatable` stays the server's
 *  answer for exactly that reason, and this module never re-implements folder resolution or
 *  the one-owner map. What this predicate may decide is only what the queue's words may claim
 *  and which doors it may offer. An absent count is not a payload: a payload that omits the
 *  field answers false, which is the behaviour this ticket starts from. */
export function payloadOnDisk(row: BundleDetectItem): boolean {
  return row.assets_total > 0;
}

/** Whether the row names a vendor page an enrichment could read. The server
 *  only issues an enrichable key when the row names a vendor id, so a LOW
 *  hash-named row carries no source at all. */
export function hasThingSource(row: BundleDetectItem): boolean {
  return Boolean(row.source_url);
}

/** Whether the row still has a field the source page could supply. Either gap
 *  is enough: the action fills only what is empty, so a known creator does not
 *  make a missing licence unreachable. */
export function needsEnrichment(row: BundleDetectItem): boolean {
  return !row.creator || !row.licence;
}

/** A row the bulk bar may send: a source to read, a gap to fill, no page record
 *  yet, and not already a project. A row the page already answered is not
 *  re-fetched by a second bulk pass, and an already-created row's values are
 *  never used again, so both are excluded here rather than at the call site. */
export function canEnrich(row: BundleDetectItem): boolean {
  return (
    hasThingSource(row) && needsEnrichment(row) && !row.enrichment && !row.existing_project_id
  );
}

/** Why a row offers no enrichment door. It is a fact about the row - it names no
 *  vendor page to read - and it is said exactly where `canEnrich` refuses for
 *  that reason: the same four gates, with the one that failed pulled out. Every
 *  other absent door stays silent, because another line already speaks it (a
 *  filled Creator or Licence for `needsEnrichment`, `enrichmentLine` for an
 *  answered page, `createdProjectLine` for a settled project), so this sentence
 *  never decays into "something is missing". */
export const NO_SOURCE_PAGE_REASON =
  "No source page: this bundle was matched from its files, not from a vendor page.";

/** The sentence `noPayloadDoor` refuses the enrichment doors with - the refusal text of the
 *  very predicate that gated them, on Mf-ny89 part C's pattern for the blocked-reason label.
 *  It claims no remedy the queue does not offer, carries no digit, and says nothing about the
 *  row's metadata: what it states is the two doors the row does not have.
 *
 *  One sentence, two mouths. It is read by the detail pane (`data-bundle-enrich-reason`, one
 *  row) and by the bulk bar (`bulkEnrichNoneLine`, N rows), so it is worded to be true in both:
 *  it speaks of the files rather than of "this row", and it states the two absent doors as a
 *  fact about them, which is a claim a selection can also make of itself. */
export const NO_PAYLOAD_DOOR_REASON =
  "No door for these files: nothing here creates a project or extracts an archive, so the source page is left unread rather than offered as the only action.";

/** Why a row offers no enrichment door. Both causes are decided by the very predicates that
 *  gated the doors: the absent source page by `hasThingSource` (with the other three
 *  `canEnrich` gates asked alongside it), and a payload no door can reach by `noPayloadDoor`,
 *  which both offer predicates read. A sentence is therefore never a guess about a row it has
 *  not looked at. */
export function enrichUnavailableLine(row: BundleDetectItem): string | null {
  if (!hasThingSource(row)) {
    // The four-gate question, unchanged: this sentence is said only when the missing page is
    // THE reason. Every other absent door stays silent, because another line already speaks it
    // (`needsEnrichment` for a filled Creator or Licence, `enrichmentLine` for an answered
    // page, `createdProjectLine` for a settled project), so it never decays into "something is
    // missing".
    if (!needsEnrichment(row) || row.enrichment || row.existing_project_id) return null;
    return NO_SOURCE_PAGE_REASON;
  }
  // The row DOES name a page, so the only thing left that can refuse a door is the payload
  // predicate. Same shape as the branch above: the sentence is decided by the predicate that
  // gated the button, here `noPayloadDoor`, which both offer predicates read.
  if ((canEnrich(row) || canRecheck(row)) && noPayloadDoor(row)) return NO_PAYLOAD_DOOR_REASON;
  return null;
}

/** The bulk bar's single sentence when nothing in the selection is sendable, or null when the bar
 *  has no honest sentence to speak. Two rules, both inherited from the paragraph this ticket
 *  deleted rather than invented:
 *
 *  - ONE cause or silence. Each line is decided per row by `enrichUnavailableLine`, so a bar that
 *    recites one row's sentence about a selection the same predicate answered differently is the
 *    deleted paragraph back in miniature - `bulkEnrichTitle` was just reduced to a count for
 *    exactly that reason. If the non-null lines disagree, nothing is said.
 *  - no door claimed absent that the bar is holding open. The payload sentence asserts there is
 *    no create and no extraction, so it may not be spoken while a selected row has either; the
 *    page sentence asserts no vendor page, so it may not be spoken while a selected row names
 *    one. A bar that is offering an action has no business explaining why it is not.
 *
 *  The row's own door absent (its line null) is not a cause and never speaks. */
export function bulkEnrichNoneLine(rows: BundleDetectItem[]): string | null {
  let spoken: string | null = null;
  for (const row of rows) {
    const line = enrichUnavailableLine(row);
    if (line === null) continue;
    if (spoken !== null && line !== spoken) return null;
    spoken = line;
  }
  if (spoken === null) return null;
  for (const row of rows) {
    if (spoken === NO_PAYLOAD_DOOR_REASON && (isCreatable(row) || isArchiveOnly(row))) return null;
    if (spoken === NO_SOURCE_PAGE_REASON && hasThingSource(row)) return null;
  }
  return spoken;
}

/** The detail pane's re-check door: the page answered before and the row is
 *  still live, so asking again (with `refresh: true`) is a real action. A row
 *  that took nothing yet belongs to `canEnrich` instead, not both. */
export function canRecheck(row: BundleDetectItem): boolean {
  return hasThingSource(row) && !row.existing_project_id && Boolean(row.enrichment);
}

/** How each supplied field reads inside the provenance sentence. An unknown
 *  field from a newer server falls back to its own name rather than vanishing:
 *  the sentence still has to say what the page supplied. */
const ENRICHMENT_FIELD_LABELS: Record<string, string> = {
  creator: "creator",
  licence: "licence",
  licence_url: "licence URL",
};

/** The provenance sentence for the detail pane, or null for a row the page
 *  never answered. `fields` names only what THIS row took, so the sentence says
 *  exactly that and nothing about the other rows in the same request. A record
 *  without a stamp still speaks its fields, with the date said to be unknown
 *  rather than dropped or invented. */
export function enrichmentLine(row: BundleDetectItem): string | null {
  const enrichment = row.enrichment;
  if (!enrichment) return null;
  const stamp = enrichment.fetched_at ? enrichment.fetched_at.slice(0, 10) : null;
  const when = stamp ? `fetched ${stamp}` : "fetched date unknown";
  const fields = enrichment.fields
    .map(field => ENRICHMENT_FIELD_LABELS[field] ?? field)
    .join(", ");
  if (!fields) return `From the source page (${when})`;
  return `From the source page: ${fields} (${when})`;
}

/** The bulk bar's words. Two different shortfalls share one form - `(N of M)` -
 *  because they answer the same question: how much of what I selected will this
 *  send?
 *
 *  The order below is load-bearing. The cap branch comes first: at (30, 30) the
 *  cap alone decides the sentence, and a shortfall branch ahead of it would say
 *  `(30)`, a claim the request cannot keep and a byte-pinned expectation in two
 *  suites. The only cell where the two shortfalls collide is (25, 30) - 25 rows
 *  are sendable and 25 is also the cap - and it reads `(25 of 30)`, true under
 *  either cause. Every other collision is `(0 of M)`, where nothing is sent and
 *  the cause is always the same one. */
export function enrichSelectionLabel(enrichable: number, selected: number): string {
  if (enrichable > ENRICH_MAX_KEYS) return `Enrich selected (${ENRICH_MAX_KEYS} of ${selected})`;
  if (enrichable < selected) return `Enrich selected (${enrichable} of ${selected})`;
  return `Enrich selected (${enrichable})`;
}

/** The bulk button's hover sentence, or undefined when the label already says everything. It
 *  speaks the COUNT only. Which rows are missing and why is a fact about each row, and each
 *  row already says it - `enrichUnavailableLine` is decided by the same predicates that gated
 *  its door. A bulk sentence cannot know that, so the paragraph that used to sit here did the
 *  opposite: one fixed multi-clause sentence listing every gate a row might fail, emitted
 *  without reading a single selected row, so an operator whose one row failed one clause was
 *  handed the whole list and none of it described their row. The count branch now stops at the
 *  count, and the zero branch defers to `bulkEnrichNoneLine` - which says nothing at all rather
 *  than recite one row's verdict about the rest.
 *
 *  The order below is load-bearing: the cap branch still outranks the shortfall, exactly as
 *  `enrichSelectionLabel`'s branches do, and `sent === selected` still comes first, so
 *  `(0, 0)` and `(25, 25)` stay silent. */
export function bulkEnrichTitle(enrichable: number, selected: number): string | undefined {
  const sent = Math.min(enrichable, ENRICH_MAX_KEYS);
  if (sent === selected) return undefined;
  if (enrichable > ENRICH_MAX_KEYS) {
    // The cap outranks the shortfall, exactly as the label's branches do.
    return `The server takes at most ${ENRICH_MAX_KEYS} bundles per request, so it sends ${ENRICH_MAX_KEYS} of the ${enrichable} that can be enriched, leaving ${enrichable - ENRICH_MAX_KEYS} for a later pass.`;
  }
  // Nothing is sendable, so there is no count to explain. The bar renders the selected rows'
  // own sentence instead (`enrichUnavailableLine`), which is decided per row by the predicates
  // that refused each door - not a guess about all four gates at once.
  if (enrichable === 0) return undefined;
  return `${enrichable} of the ${selected} selected can be enriched`;
}

/** The one blocked reason that names an extraction instead of a create. */
const ARCHIVE_ONLY = "archive_only";

/** The one blocked reason that is a claim about what a scan registered. */
const NOT_SCANNED = "not_scanned";

/** Whether a row's path is an archive on disk. Case-insensitive on purpose: the
 *  server's own walk lowercases the suffix before comparing it
 *  (`bundle_service._suffix`), so `Foo.ZIP` is a zip row, and the extract door
 *  accepts it case-insensitively too. Deliberately not `isZipFile` from
 *  `lib/zipUtils`, which applies the same suffix to a picked File's NAME on the
 *  upload path: a queue predicate should not take its vocabulary from the upload
 *  path, and this one is judged against the server's rule rather than a local
 *  picker's. The two residuals a suffix test cannot see - a DIRECTORY named
 *  `Foo.zip`, and a file literally named `.zip` - are refused server-side. */
function isArchivePath(path: string): boolean {
  return path.toLowerCase().endsWith(".zip");
}

/** The archive this row can extract in place, or null when it offers no such
 *  door. Two real wire shapes reach it, and only one of them has a twin:
 *
 *  - a row that IS an archive. The server builds a zip-only candidate with
 *    `archive_path=None`, so such a row ships `archive: null` and its own
 *    `rel_path` ends `.zip`;
 *  - a `pair_only` row: a directory the container rule refuses, which ships its
 *    twin zip in `archive` - the field's one meaning is "the twin of a DIRECTORY
 *    bundle", and that row really is one.
 *
 *  So the twin is asked first and the row's own path is the fallback. The
 *  blocked reason is asked before either, because the twin is attached whatever
 *  the status: a ready row and an `unresolved_directory` row both name one, and
 *  extracting it is no remedy for either. One rule feeds every affordance, so
 *  the cell, the Enter key, the bulk count, the bulk dialog, the detail door and
 *  the one-step door cannot disagree about which rows have a door. */
export function extractPathFor(row: BundleDetectItem): string | null {
  if (row.blocked_reason !== ARCHIVE_ONLY) return null;
  const twin = row.archive?.rel_path;
  if (twin) return twin;
  return isArchivePath(row.rel_path) ? row.rel_path : null;
}

/** Whether the row offers the in-place extraction door. It reads `extractPathFor`
 *  rather than repeating the shape, so a row with a door always has a target and
 *  a row with a target always has a door. */
export function isArchiveOnly(row: BundleDetectItem): boolean {
  return extractPathFor(row) !== null;
}

export const EXTRACT_AND_CREATE_LABEL = "Extract and create project";

/** The new door's hover sentence: what it does that plain Extract does not.
 *  Plain Extract runs the same extraction and the same settle wait
 *  (`BundleReviewView.handleExtracted` -> `state.afterExtract`); the only step
 *  it omits is the project, so the hint must not claim otherwise. Plain Extract
 *  is named by what it does rather than by a quoted label: its visible text is
 *  "Extract archive" while its accessible name is `Extract <title>`, so a quote
 *  would be a name neither the operator nor a screen reader hears. */
export const EXTRACT_AND_CREATE_HINT =
  "Extracts the archive in place, waits for the library scan to register the files, then creates one project from the result. The plain Extract button does the extraction and the wait, and stops before the project.";

/** The cell's title for a row both doors can serve. It quotes the new button's
 *  own accessible name, which is a label the detail panel really carries. */
export const EXTRACT_AND_CREATE_ROW_TITLE =
  "Extract this archive in place (Enter). The detail panel's 'Extract and create project' does the whole job: extract, wait for the scan, then create the project.";

/** A row the one-step door may run on: the zip-only row, and not one that
 *  already became a project. The server's `already_created` is the real belt;
 *  this is the pre-flight that keeps the button off a finished row. */
export function canExtractAndCreate(row: BundleDetectItem): boolean {
  return isArchiveOnly(row) && !row.existing_project_id;
}

/** The blocked-reason cell's hover sentence. It is a text edit only: the cell
 *  keeps its click and Enter behaviour. A row the one-step door can serve gets
 *  the pointer to it; every other row keeps the sentence it had, byte for byte. */
export function archiveExtractTitle(row: BundleDetectItem): string {
  if (canExtractAndCreate(row)) return EXTRACT_AND_CREATE_ROW_TITLE;
  return "Extract this archive in place (Enter)";
}

/** The row this ticket exists for: the report says its key holds registered files, and yet the
 *  queue has nothing it can DO with them. Three facts, every one read off the report through
 *  the predicate that already decides the door:
 *
 *  - `payloadOnDisk` - the report counts registered assets under the key;
 *  - `!isCreatable` - the server's own answer is that this row creates no project (blocked,
 *    or its payload already sits in one);
 *  - `extractPathFor(row) === null` - and no archive is offered either, in place or one-step.
 *
 *  This is a CLIENT PRODUCT REFUSAL, not a server rule: the server would happily enrich such
 *  a row and cache whatever the page said. The queue refuses to offer that action ALONE,
 *  because reading a vendor page for a payload nothing here can act on reads to the operator
 *  as the app refusing to do anything. Dismiss is always live and is deliberately NOT company
 *  here: it hides the row, it does not act on the payload. */
export function noPayloadDoor(row: BundleDetectItem): boolean {
  return payloadOnDisk(row) && !isCreatable(row) && extractPathFor(row) === null;
}

/** The ONE enrichment offer predicate. The bulk bar's count, the bulk bar's disabled state and
 *  the detail pane's door all read THIS, so a row the bulk bar counts is a row the detail pane
 *  would offer, and neither can drift from the other. `canEnrich` stays the server-shaped
 *  question (source page, a gap, no page record, not a project) and stays unchanged; the
 *  refusal is layered here, once.
 *
 *  The layer leans on `canEnrich` and `canRecheck` each excluding `existing_project_id`: if
 *  either ever stopped excluding it, a settled row could reach here with its payload claim
 *  standing, and the detail pane's hidden Create button would become the hole in the theorem. */
export function canOfferEnrich(row: BundleDetectItem): boolean {
  return canEnrich(row) && !noPayloadDoor(row);
}

/** The detail pane's re-check door, offered under the same rule. A row whose payload no door
 *  can reach is not re-fetched for metadata either, so the two doors cannot disagree about
 *  which rows are work. */
export function canOfferRecheck(row: BundleDetectItem): boolean {
  return canRecheck(row) && !noPayloadDoor(row);
}

/** The two reveal toggles that widen what the queue shows. */
export type VisibilityOptions = {
  showDismissed: boolean;
  showCreated: boolean;
};

/** Whether a row survives the queue's reveal toggles. The list and the badge
 *  both call this one rule, so they cannot describe the queue two ways. */
export function matchesVisibility(row: BundleDetectItem, options: VisibilityOptions): boolean {
  return (
    (options.showDismissed || !row.dismissed) &&
    (options.showCreated || !row.existing_project_id)
  );
}

/** A row that still needs a decision: visible under both toggles at their
 *  defaults. Delegating to `matchesVisibility` is what makes the badge and the
 *  list agree by construction rather than by coincidence. */
export function needsDecision(row: BundleDetectItem): boolean {
  return matchesVisibility(row, { showDismissed: false, showCreated: false });
}

export function confidenceLabel(confidence: string): string {
  if (confidence === "high") return "High confidence";
  if (confidence === "medium") return "Medium confidence";
  // The fallback stays: an unknown value from a newer server still reads as the
  // lowest confidence we know, rather than printing nothing.
  return "Low confidence";
}

/** The `archive_only` sentence for a row that is NOT an archive on disk: the
 *  live `import/01_Fates End - Wizard Tower` shape, whose `rel_path` is a
 *  directory. "zip only" is false about that row, and one string has to serve
 *  both directory shapes `extractPathFor` knows - a directory with a twin, where
 *  the Extract door extracts the TWIN and never this folder, and a directory with
 *  no twin, where there is no door at all. So this sentence claims no remedy (the
 *  door's own title is the only place a remedy is promised), names no count (an
 *  unknown count is `countsLine`'s en dash to say, never a number invented here)
 *  and calls the row nothing it is not: it says only which thing on disk the row
 *  is, in the same "X only" voice as the zip sentence it sits beside. */
export const ARCHIVE_ONLY_FOLDER_LABEL = "folder only - this row is a folder, not an archive";

/** The `not_scanned` sentence for a row the report says is NOT empty - the shape Mf-quv4 left
 *  behind, where the row lands in this arm because it owns no registered MODEL while a scan has
 *  registered files under its key (a folder whose every model belongs to a deeper row, or one
 *  that holds only images and text). `BLOCKED_REASON_LABELS.not_scanned`'s "no model file
 *  registered by a scan" is true of it but reads as "there is nothing here", next to a counts
 *  line that says otherwise. So this says which of the two it is: still no model of its own,
 *  and files registered under it. Same "no X" voice as the string it replaces, no digit, and no
 *  remedy - the arm offers none. */
export const NOT_SCANNED_REGISTERED_LABEL =
  "no model file of its own - a scan has registered files under it";

/** The blocked reason's words for THIS row. `archive_only` is read through
 *  `isArchivePath` - the very predicate `extractPathFor` reads - so the door and
 *  the sentence cannot disagree about whether the row is a zip: a row the badge
 *  calls a zip is the only row the sentence calls a zip. Every other reason keeps
 *  the plain lookup, an unknown one still reads as its own name, and a row with
 *  no reason at all says `blocked` rather than nothing. `not_scanned` is read
 *  through `payloadOnDisk` - the very predicate the door offers read - so the
 *  sentence cannot call a row the report calls non-empty an unscanned one: a row
 *  with registered files gets the sentence that says so, and a row with none
 *  keeps the plain lookup byte for byte. */
export function blockedReasonLabel(row: BundleDetectItem): string {
  if (row.blocked_reason === ARCHIVE_ONLY) {
    return isArchivePath(row.rel_path)
      ? BLOCKED_REASON_LABELS.archive_only
      : ARCHIVE_ONLY_FOLDER_LABEL;
  }
  if (row.blocked_reason === NOT_SCANNED) {
    // The assets_total === 0 case keeps the plain lookup byte for byte; only a row the report
    // calls non-empty gets the new sentence.
    return payloadOnDisk(row)
      ? NOT_SCANNED_REGISTERED_LABEL
      : BLOCKED_REASON_LABELS.not_scanned;
  }
  if (!row.blocked_reason) return "blocked";
  return BLOCKED_REASON_LABELS[row.blocked_reason] ?? row.blocked_reason;
}

export function statusLine(row: BundleDetectItem): string {
  if (!isBlocked(row)) return "Ready";
  return `Blocked - ${blockedReasonLabel(row)}`;
}

/** The bucket a row falls in for the queue's reveal toggles. This is the
 *  reporter's verdict on whether the row is settled, not the row's words: an
 *  existing project outranks a dismissal, because creating is the stronger
 *  settlement of the two. */
export type RowBucket = "created" | "dismissed" | null;

export function rowBucket(row: BundleDetectItem): RowBucket {
  if (row.existing_project_id) return "created";
  if (row.dismissed) return "dismissed";
  return null;
}

/** The detail line for a row that already became a project, or null when it did
 *  not. The id is the identity and is never spoken: a name we have is named, a
 *  name we do not have says so honestly rather than printing a uuid or nothing.
 *
 *  The asset identity is spoken differently on purpose. It is a claim about the
 *  FILES, not about intent: the project holds the bundle's files, but the app
 *  did not necessarily make it from this bundle, so the line says "Already in"
 *  and names the evidence. The marker, source URL and name identities keep the
 *  flat "Already created as project" line - the first two are the app's own
 *  record, and a name match is already a guess about the same project. */
export function createdProjectLine(row: BundleDetectItem): string | null {
  if (!row.existing_project_id) return null;
  const name = (row.existing_project_name ?? "").trim();
  if (row.existing_project_match === "assets") {
    if (name) return `Already in ${name} (matched by shared files)`;
    return "Already in a project (matched by shared files) - its name is unavailable.";
  }
  if (name) return `Already created as project ${name}`;
  return "Already created as a project - its name is unavailable.";
}

/** The already-created row's second sentence: does this folder still hold parts
 *  the project does not? null for every row the question does not apply to -
 *  no existing project, a payload from before the field existed, or a project
 *  whose folder has not grown - so a quiet queue stays quiet. */
export function newPartsLine(row: BundleDetectItem): string | null {
  if (!row.existing_project_id) return null;
  // An absent field predates the drift contract, which is `not_applicable`
  // (nothing new to say) rather than a zero.
  const state = row.bundle_drift_state ?? DRIFT_NOT_APPLICABLE;
  if (state === DRIFT_NOT_APPLICABLE) return null;
  if (state === DRIFT_KNOWN) {
    const count = row.bundle_drift_count;
    // A known state's count is its own evidence; a missing or zero count means
    // this folder has not grown, and the queue says nothing.
    if (count === null || count === undefined || count <= 0) return null;
    return `${countLabel(count, "new part", "new parts")} since this was created`;
  }
  // `unknown`, and any state this client does not know: the dash comes from the
  // one formatter, never a zero.
  return `${formatReviewCount(null)} new parts since this was created`;
}

/** The row chip's words, kept apart from `rowBucket` on purpose. The bucket is
 *  the reporter's verdict on whether the row is settled; the chip is the
 *  narrower claim "who made this project?". A project the queue only found by
 *  shared files is settled - hidden with the created rows - yet the app may not
 *  have made it, so its chip must not say "Created". */
export const ROW_STATE_CREATED = "Created";
export const ROW_STATE_IN_PROJECT = "In project";
export const ROW_STATE_DISMISSED = "Dismissed";

/** A chip's words and, when the words abbreviate a longer sentence, that
 *  sentence as the hover `title`. The non-interactive spans in the row use the
 *  same idiom (`review-row-path`, `review-row-confidence`). */
export type RowStateChip = { label: string; title: string | null };

export function rowStateChip(row: BundleDetectItem): RowStateChip {
  if (row.existing_project_id) {
    // Only the asset identity is a claim about the files rather than the app's
    // own record, so only it needs the weaker words. Its title is the same
    // sentence the detail line speaks, so the chip and the panel cannot
    // disagree.
    if (row.existing_project_match === "assets") {
      return { label: ROW_STATE_IN_PROJECT, title: createdProjectLine(row) };
    }
    return { label: ROW_STATE_CREATED, title: null };
  }
  if (row.dismissed) return { label: ROW_STATE_DISMISSED, title: null };
  return { label: "", title: null };
}

export function countLabel(value: number, one: string, many: string): string {
  return `${value} ${value === 1 ? one : many}`;
}

export function countsLine(row: BundleDetectItem): string {
  const parts = [
    countLabel(row.model_count, "model", "models"),
    countLabel(row.image_count, "image", "images"),
    countLabel(row.text_count, "text file", "text files"),
    countLabel(row.other_count, "other file", "other files"),
  ];
  return `${parts.join(", ")} - ${countLabel(row.assets_total, "registered asset", "registered assets")}`;
}

export function coverLine(row: BundleDetectItem): string {
  // One branch per reason rather than a nested ternary: each reason's suffix is
  // an independent fact, and a reader must be able to add a fourth without
  // untangling the others.
  const { reason, rel_path: relPath } = row.cover;
  if (!relPath) return "Cover: none";
  if (reason === "largest-image-unregistered") return `Cover ${relPath} (not registered)`;
  if (reason === "model-preview") return `Cover ${relPath} (model preview)`;
  return `Cover ${relPath}`;
}

export function archiveLine(row: BundleDetectItem): string | null {
  if (!row.archive) return null;
  return `Archive ${row.archive.rel_path} - twin ${row.archive.twin_verified ? "verified" : "not verified"}`;
}

/** The name the operator would create with: their edit, else the suggestion. */
export function typedName(names: Record<string, string>, row: BundleDetectItem): string {
  const name = names[row.key] ?? row.name_suggestion;
  return name.trim();
}
