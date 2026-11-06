// The bundle queue's pure presentation rules. Keeping them out of the view
// lets the render stay declarative and the wording stay unit-testable without
// mounting a component.

import {
  ENRICH_MAX_KEYS,
  type BundleDetectCounts,
  type BundleDetectItem,
} from "../../lib/api/bundles";

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

export function enrichUnavailableLine(row: BundleDetectItem): string | null {
  if (hasThingSource(row)) return null;
  if (!needsEnrichment(row)) return null;
  if (row.enrichment) return null;
  if (row.existing_project_id) return null;
  return NO_SOURCE_PAGE_REASON;
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

/** What the bulk bar says when none of the selected rows can be enriched. It is
 *  the disabled button's `title`, but a disabled control's title is not reliably
 *  surfaced as a tooltip, and the control is not focusable, so the same sentence
 *  is also rendered as visible text - and the count form `(0 of 3)` has to be
 *  honest on its own. */
export const ENRICH_NONE_REASON =
  "None of the selected bundles can be enriched: each either names no source page, already has a creator and a licence, was already read from its page, or is already a project. A bundle that was already read from its page, and is not already a project, can be re-checked one at a time with 'Re-check source page'.";

/** The bulk button's hover sentence, or undefined when the label already says
 *  everything. It names all four `canEnrich` gates rather than the missing
 *  source alone, so it can never accuse an already-enriched row of having no
 *  page - the tooltip must not contradict the count it hangs on. */
export function bulkEnrichTitle(enrichable: number, selected: number): string | undefined {
  const sent = Math.min(enrichable, ENRICH_MAX_KEYS);
  if (sent === selected) return undefined;
  if (enrichable > ENRICH_MAX_KEYS) {
    // The cap outranks the shortfall, exactly as the label's branches do.
    return `The server takes at most ${ENRICH_MAX_KEYS} bundles per request, so it sends ${ENRICH_MAX_KEYS} of the ${enrichable} that can be enriched, leaving ${enrichable - ENRICH_MAX_KEYS} for a later pass.`;
  }
  if (enrichable === 0) return ENRICH_NONE_REASON;
  return `${enrichable} of the ${selected} selected can be enriched; the others either name no source page, already have a creator and a licence, were already read from their page, or are already a project.`;
}

/** A row the queue may extract in place: it is blocked as an archive the scan
 *  never saw, and it names the archive to extract. The path check is what makes
 *  the label insufficient - a row without an archive path cannot be extracted,
 *  so the blocked-reason cell, the Enter key and the bulk count all read this
 *  one predicate and stay honest together: one rule feeds all three, so they
 *  cannot drift apart. */
export function isArchiveOnly(row: BundleDetectItem): boolean {
  return row.blocked_reason === "archive_only" && Boolean(row.archive?.rel_path);
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

export function statusLine(row: BundleDetectItem): string {
  if (!isBlocked(row)) return "Ready";
  const reason = row.blocked_reason
    ? BLOCKED_REASON_LABELS[row.blocked_reason] ?? row.blocked_reason
    : "blocked";
  return `Blocked - ${reason}`;
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
