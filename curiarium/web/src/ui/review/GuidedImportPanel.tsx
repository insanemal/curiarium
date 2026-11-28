import React from "react";

// Imported from the domain module, not the `../../lib/api` barrel: several
// suites replace that barrel with a fixed object, so a named import from it
// would come back `undefined` there (`useBundleDetect.ts` documents the same
// rule).
import {
  createBundles,
  type BundleCoverCandidate,
  type BundleCreateItem,
  type BundleDetectItem,
  type BundleSuggestion,
} from "../../lib/api/bundles";
import { UnauthorizedError } from "../../lib/api/http";
import { loadSettings } from "../../lib/settings";
import { notifyInfo } from "../../lib/toast";
import OperationPanel from "../operations/OperationPanel";
import SplitAndCreatePanel from "./SplitAndCreatePanel";
import {
  METADATA_FIELDS,
  countsLine,
  coverLine,
  shapeLabel,
  suggestionFieldLabel,
  suggestionSourceLine,
} from "./bundleRows";

type Props = {
  /** The detected row the panel is reviewing: its facts, its offer and its fans. */
  row: BundleDetectItem;
  /** The write gate: a viewer renders nothing and issues no request. */
  canContribute: boolean;
  /** The pane's typed name, when the operator already typed one. */
  initialName?: string;
  /** The operator landed on a project: the host decides where that is. */
  onLanded?: (projectId: string) => void;
  /** The host's refetch signal. */
  onChanged?: () => void;
  /** Session expiry is the host's news. */
  onUnauthorized?: () => void;
};

const PRIMARY_BUTTON = "text-xs px-2.5 py-1.5 rounded-md bg-accent disabled:opacity-60";
const SECONDARY_BUTTON =
  "text-xs px-2.5 py-1.5 rounded-md border border-panel-strong disabled:opacity-60";
const SUGGESTION_BOX =
  "rounded-md border border-dashed border-panel-strong px-2 py-1 flex flex-col gap-1";

/** The thrown message, or a neutral fallback when the throw was not an Error.
 *  The API layer already turns the server's `detail` into `Error.message`, so
 *  this is the one place a failure sentence is chosen. */
function failureDetail(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

/**
 * The vendor cookie saved in Settings, read at CALL time.
 *
 * Exactly `ui/review/useBundleDetect.ts`'s `savedThingiverseCookie` rule and for
 * the same reason: `App` holds settings in React state, `saveSettings` is the
 * only origin, and this panel threads no settings prop through `App` - so the
 * one honest read is the store, at the moment the operation is offered. A value
 * captured at mount would miss a paste made since.
 */
function savedThingiverseCookie(): string | undefined {
  return loadSettings().thingiverse.cookie || undefined;
}

/**
 * The guided import door: everything the engine parsed for one row, in the
 * operator's hands before anything is written. Self-contained on purpose - it
 * owns its own write, so the host only supplies the row, the gate and its
 * callbacks.
 *
 * Nothing is pre-accepted: `accepted`/`dismissed` start empty, and a create
 * writes only what this panel's map names. A shape the server cannot enact is
 * handed to `SplitAndCreatePanel`, the one door that can move the bytes, and the
 * Create button stays shut for it: the server refuses `needs-arrange` for a shape
 * it cannot enact, so only that door can make the shape true.
 */
export default function GuidedImportPanel({
  row,
  canContribute,
  initialName,
  onLanded,
  onChanged,
  onUnauthorized,
}: Props): React.ReactElement | null {
  const [name, setName] = React.useState(initialName ?? row.name_suggestion);
  const [cover, setCover] = React.useState("engine");
  const [sources, setSources] = React.useState<string[]>(() => {
    const first = row.provenance?.candidates?.[0]?.url ?? row.source_url ?? "";
    return first ? [first] : [];
  });
  const [accepted, setAccepted] = React.useState<Record<string, string>>({});
  const [acceptedFields, setAcceptedFields] = React.useState<Set<string>>(() => new Set<string>());
  const [dismissed, setDismissed] = React.useState<Set<string>>(() => new Set<string>());
  const [editing, setEditing] = React.useState<string | null>(null);
  const [editingValue, setEditingValue] = React.useState("");
  const [shape, setShape] = React.useState(row.matches?.[0]?.shape ?? row.metadata_shape);
  // Whether the enrichment door has been revealed. Mounting the shared panel is
  // deliberate lazily: it issues an in-flight lookup READ on mount, and a report
  // of a hundred rows must not cost a hundred reads. The click is the only thing
  // that opens it.
  const [enrichOpen, setEnrichOpen] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [failure, setFailure] = React.useState<string | null>(null);
  const [already, setAlready] = React.useState<string | null>(null);

  // The host may hand this panel a fresh callback identity every render, so the
  // callbacks the write reads are kept in refs: a WRITE must not run again
  // because a parent re-rendered.
  const unauthorizedRef = React.useRef(onUnauthorized);
  unauthorizedRef.current = onUnauthorized;

  if (!canContribute) return null;

  const images = row.images ?? [];
  const seenAssets = new Set(images.map(image => image.asset_id));
  const seenPaths = new Set(images.map(image => image.rel_path));
  const extraCovers: BundleCoverCandidate[] = [];
  const inertCovers: BundleCoverCandidate[] = [];
  // A face is offered only when it is a registered asset. The engine's own list
  // also carries unregistered files, which a create cannot set: those render as
  // inert spans rather than as radios that would do nothing.
  for (const candidate of row.cover_candidates ?? []) {
    if (candidate.asset_id === null) {
      if (seenPaths.has(candidate.rel_path)) continue;
      inertCovers.push(candidate);
      seenPaths.add(candidate.rel_path);
      continue;
    }
    if (seenAssets.has(candidate.asset_id) || seenPaths.has(candidate.rel_path)) continue;
    extraCovers.push(candidate);
    seenAssets.add(candidate.asset_id);
    seenPaths.add(candidate.rel_path);
  }

  const suggestions = row.metadata_suggestions ?? [];
  const matches = row.matches ?? [];
  const chosenMatch = matches.find(match => match.shape === shape);
  // The server's own verdict is the whole reason to arrange first: it refuses
  // the create with `needs-arrange` for a shape it cannot enact, so the Create
  // button must not fire for one. The split door beside it is the only thing that
  // can make the shape true.
  const needsArranging = chosenMatch?.creatable === false;
  const applyDisabled = busy || chosenMatch?.creatable === false;

  /** A field the operator said yes to: it shows as accepted and is no longer
   *  dismissed, so the two markers never disagree. */
  function markAccepted(field: string): void {
    setAcceptedFields(prev => new Set(prev).add(field));
    setDismissed(prev => {
      if (!prev.has(field)) return prev;
      const next = new Set(prev);
      next.delete(field);
      return next;
    });
  }

  function unmarkAccepted(field: string): void {
    setAcceptedFields(prev => {
      if (!prev.has(field)) return prev;
      const next = new Set(prev);
      next.delete(field);
      return next;
    });
  }

  function accept(suggestion: BundleSuggestion): void {
    // The project's name has its own field, and the server refuses a `title`
    // metadata key, so accepting a title renames the create instead.
    if (suggestion.field === "title") setName(suggestion.value);
    else setAccepted(prev => ({ ...prev, [suggestion.field]: suggestion.value }));
    markAccepted(suggestion.field);
    if (editing === suggestion.field) setEditing(null);
  }

  /** The "Use this source" button is the accept: a typed URL is written only
   *  after the operator has said so, never on the strength of typing it. */
  function acceptSource(source: string): void {
    const value = source.trim();
    if (!value) return;
    setAccepted(prev => ({ ...prev, source_url: value }));
    markAccepted("source_url");
    if (editing === "source_url") setEditing(null);
  }

  /** Taking an accept back forgets the decision: the title keeps its typed name
   *  (it lives in `name`, not in the metadata map). */
  function undoAccept(field: string): void {
    unmarkAccepted(field);
    if (field === "title") return;
    setAccepted(prev => {
      if (prev[field] === undefined) return prev;
      const next = { ...prev };
      delete next[field];
      return next;
    });
  }

  function dismiss(field: string): void {
    setAccepted(prev => {
      if (prev[field] === undefined) return prev;
      const next = { ...prev };
      delete next[field];
      return next;
    });
    unmarkAccepted(field);
    setDismissed(prev => new Set(prev).add(field));
    if (editing === field) setEditing(null);
  }

  function undoDismiss(field: string): void {
    setDismissed(prev => {
      const next = new Set(prev);
      next.delete(field);
      return next;
    });
  }

  function startEdit(suggestion: BundleSuggestion): void {
    setEditing(suggestion.field);
    setEditingValue(accepted[suggestion.field] ?? suggestion.value);
  }

  function saveEdit(field: string): void {
    if (field === "title") setName(editingValue);
    else setAccepted(prev => ({ ...prev, [field]: editingValue }));
    markAccepted(field);
    setEditing(null);
  }

  function updateSource(index: number, value: string): void {
    setSources(prev => {
      const next = prev.length === 0 ? [""] : [...prev];
      while (next.length <= index) next.push("");
      next[index] = value;
      return next;
    });
  }

  function addSource(): void {
    setSources(prev => (prev.length === 0 ? ["", ""] : [...prev, ""]));
  }

  async function apply(): Promise<void> {
    setBusy(true);
    setFailure(null);
    setAlready(null);
    try {
      // Only what the operator accepted: an accepted edit is a value, a dismiss
      // is a null, and everything else is absent so it is never written.
      const metadata: Record<string, string | null> = {};
      for (const field of METADATA_FIELDS) {
        if (accepted[field] !== undefined) metadata[field] = accepted[field];
        else if (dismissed.has(field)) metadata[field] = null;
      }
      const item: BundleCreateItem = { key: row.key, name: name.trim(), shape, metadata };
      if (cover === "none") item.cover = { mode: "none" };
      else if (cover !== "engine") item.cover = { mode: "asset", asset_id: cover };
      const response = await createBundles([item]);
      const result = response.results[0];
      if (result?.status === "created") {
        notifyInfo(`Created: ${result.name ?? name}`);
        onLanded?.(result.project_id ?? "");
        onChanged?.();
      } else if (result?.status === "skipped" && result.reason === "already_created") {
        notifyInfo(`Already created as ${result.name ?? name}`);
        setAlready(`Already created as ${result.name ?? name}`);
      } else {
        setFailure(result?.reason ?? "The create did not land.");
      }
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized?.();
        return;
      }
      setFailure(failureDetail(err, "The create did not land."));
    } finally {
      setBusy(false);
    }
  }

  // The enrichment door's params, built at render so the cookie is read at CALL
  // time (see `savedThingiverseCookie`): an empty cookie is omitted rather than
  // sent blank, because a blank Cookie header is not "no cookie" to every
  // vendor.
  const cookie = savedThingiverseCookie();
  const enrichParams: Record<string, unknown> = {
    keys: [row.key],
    ...(cookie ? { thingiverse_cookie: cookie } : {}),
  };

  return (
    <div className="flex flex-col gap-3">
      <fieldset className="flex flex-col gap-1">
        <legend>Cover</legend>
        <label className="flex items-center gap-2">
          <input
            type="radio"
            name={`guided-cover-${row.key}`}
            value="engine"
            checked={cover === "engine"}
            onChange={() => setCover("engine")}
            data-guided-cover-default="true"
          />
          {`Engine's choice - ${row.cover.rel_path ?? "none"}`}
        </label>
        <label className="flex items-center gap-2">
          <input
            type="radio"
            name={`guided-cover-${row.key}`}
            value="none"
            checked={cover === "none"}
            onChange={() => setCover("none")}
            data-guided-cover-none="true"
          />
          No cover
        </label>
        {images.map(image => (
          <label key={image.asset_id} className="flex items-center gap-2">
            <input
              type="radio"
              name={`guided-cover-${row.key}`}
              value={image.asset_id}
              checked={cover === image.asset_id}
              onChange={() => setCover(image.asset_id)}
              data-guided-cover-asset={image.asset_id}
            />
            {image.rel_path}
          </label>
        ))}
        {extraCovers.map(candidate => {
          const assetId = candidate.asset_id ?? "";
          return (
            <label key={assetId} className="flex items-center gap-2">
              <input
                type="radio"
                name={`guided-cover-${row.key}`}
                value={assetId}
                checked={cover === assetId}
                onChange={() => setCover(assetId)}
                data-guided-cover-asset={assetId}
              />
              {candidate.rel_path}
            </label>
          );
        })}
        {inertCovers.map(candidate => (
          <span
            key={candidate.rel_path}
            className="text-muted"
            data-guided-cover-candidate="true"
          >
            {`${candidate.rel_path} (not registered - cannot be chosen)`}
          </span>
        ))}
      </fieldset>

      <div className="flex flex-col gap-2">
        <div className="flex flex-col gap-1">
          <span>Metadata source</span>
          <div className="flex items-center gap-2">
            <input
              aria-label="Metadata source"
              className="rounded-md border border-panel-strong px-2 py-1"
              data-guided-source-url="true"
              value={sources[0] ?? ""}
              onChange={event => updateSource(0, event.target.value)}
            />
            <button
              type="button"
              className={SECONDARY_BUTTON}
              data-guided-accept-source="true"
              onClick={() => acceptSource(sources[0] ?? "")}
            >
              Use this source
            </button>
          </div>
          <span className="text-muted text-xs">
            Used as the project's Source URL when accepted.
          </span>
        </div>
        {sources.slice(1).map((source, index) => (
          <div key={index} className="flex flex-col gap-1">
            <span>Another source</span>
            <div className="flex items-center gap-2">
              <input
                aria-label={`Another source ${index + 1}`}
                className="rounded-md border border-panel-strong px-2 py-1"
                data-guided-source-url-extra="true"
                value={source}
                onChange={event => updateSource(index + 1, event.target.value)}
              />
              <button
                type="button"
                className={SECONDARY_BUTTON}
                data-guided-accept-source="true"
                onClick={() => acceptSource(source)}
              >
                Use this source
              </button>
            </div>
          </div>
        ))}
        <div className="review-detail-actions">
          <button type="button" className={SECONDARY_BUTTON} data-guided-add-source="true" onClick={addSource}>
            Add another source
          </button>
          {/* This button REVEALS the enrichment door; it writes nothing itself.
              The old handler hid a blocking plan+submit+poll inside
              `lib/api/bundles`, so it had no review, no narration and no undo.
              The operation door has all three. */}
          <button
            type="button"
            className={SECONDARY_BUTTON}
            data-guided-enrich="true"
            onClick={() => setEnrichOpen(true)}
          >
            Read the source page
          </button>
        </div>
        {enrichOpen && (
          <OperationPanel
            workflow="enrich"
            target=""
            params={enrichParams}
            canContribute={canContribute}
            onChanged={onChanged}
            onUnauthorized={onUnauthorized}
            dataPrefix="guided-enrich"
          />
        )}
      </div>

      <div className="flex flex-col gap-2">
        <span>What the app parsed</span>
        <div className="review-detail-facts" data-guided-facts="true">
          <span className="text-muted">{`The app measured: ${countsLine(row)}`}</span>
          <span className="text-muted">{coverLine(row)}</span>
        </div>
        {suggestions.map(suggestion => {
          const label = suggestionFieldLabel(suggestion.field);
          if (dismissed.has(suggestion.field)) {
            return (
              <div
                key={suggestion.field}
                className="rounded-md border border-panel-strong px-2 py-1 flex items-center gap-2"
                data-suggestion-dismissed="true"
              >
                <span className="text-muted">{`${label} - Dismissed`}</span>
                <button
                  type="button"
                  className={SECONDARY_BUTTON}
                  data-suggestion-undo="true"
                  onClick={() => undoDismiss(suggestion.field)}
                >
                  {`Undo ${label}`}
                </button>
              </div>
            );
          }
          if (acceptedFields.has(suggestion.field) && editing !== suggestion.field) {
            return (
              <div
                key={suggestion.field}
                data-suggestion="true"
                data-suggestion-field={suggestion.field}
                data-suggestion-accepted="true"
                className={SUGGESTION_BOX}
              >
                <span className="flex flex-wrap items-center gap-2">
                  <span data-suggestion-tag="true" className="text-amber-400">
                    Suggested
                  </span>{" "}
                  {label}
                  <span
                    data-suggestion-accepted-tag="true"
                    className="text-emerald-400"
                  >
                    Accepted
                  </span>
                </span>
                <span data-suggestion-value="true">
                  {accepted[suggestion.field] ?? suggestion.value}
                </span>
                <span data-suggestion-source="true" className="text-muted font-mono">
                  {suggestionSourceLine(suggestion)}
                </span>
                <span className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    className={SECONDARY_BUTTON}
                    data-suggestion-undo-accept="true"
                    onClick={() => undoAccept(suggestion.field)}
                  >
                    Undo accept
                  </button>
                </span>
              </div>
            );
          }
          return (
            <div
              key={suggestion.field}
              data-suggestion="true"
              data-suggestion-field={suggestion.field}
              className={SUGGESTION_BOX}
            >
              <span>
                <span data-suggestion-tag="true" className="text-amber-400">
                  Suggested
                </span>{" "}
                {label}
              </span>
              <span data-suggestion-value="true">{suggestion.value}</span>
              <span data-suggestion-source="true" className="text-muted font-mono">
                {suggestionSourceLine(suggestion)}
              </span>
              {editing === suggestion.field ? (
                <span className="flex flex-wrap items-center gap-2">
                  <input
                    className="rounded-md border border-panel-strong px-2 py-1"
                    data-suggestion-edit-input="true"
                    value={editingValue}
                    onChange={event => setEditingValue(event.target.value)}
                  />
                  <button
                    type="button"
                    className={SECONDARY_BUTTON}
                    data-suggestion-save="true"
                    onClick={() => saveEdit(suggestion.field)}
                  >
                    Save
                  </button>
                </span>
              ) : (
                <span className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    className={SECONDARY_BUTTON}
                    data-suggestion-accept="true"
                    onClick={() => accept(suggestion)}
                  >
                    {`Accept ${label}`}
                  </button>
                  <button
                    type="button"
                    className={SECONDARY_BUTTON}
                    data-suggestion-edit="true"
                    onClick={() => startEdit(suggestion)}
                  >
                    {`Edit ${label}`}
                  </button>
                  <button
                    type="button"
                    className={SECONDARY_BUTTON}
                    data-suggestion-dismiss="true"
                    onClick={() => dismiss(suggestion.field)}
                  >
                    {`Dismiss ${label}`}
                  </button>
                </span>
              )}
            </div>
          );
        })}
      </div>

      <fieldset className="flex flex-col gap-1">
        <legend>Shape</legend>
        {matches.map(match => (
          <label key={match.shape} className="flex items-center gap-2">
            <input
              type="radio"
              name={`guided-shape-${row.key}`}
              value={match.shape}
              checked={shape === match.shape}
              onChange={() => setShape(match.shape)}
              data-guided-shape={match.shape}
            />
            {shapeLabel(match.shape)}
          </label>
        ))}
        {chosenMatch?.note ? <span className="text-muted">{chosenMatch.note}</span> : null}
        {needsArranging && (
          <span className="text-muted" data-guided-needs-arranging="true">
            {`Needs arranging: ${chosenMatch?.note ?? "the server cannot create this shape yet"}`}
          </span>
        )}
      </fieldset>

      <div className="review-detail-actions">
        <button
          type="button"
          className={PRIMARY_BUTTON}
          data-guided-apply="true"
          disabled={applyDisabled}
          onClick={() => void apply()}
        >
          Import project
        </button>
      </div>
      {failure !== null && (
        <span className="review-detail-reason text-red-500" role="alert" data-guided-failure="true">
          {failure}
        </span>
      )}
      {already !== null && <span data-guided-already="true">{already}</span>}
      {needsArranging && (
        <SplitAndCreatePanel
          bundleKey={row.key}
          shape={shape}
          canContribute={canContribute}
          onChanged={onChanged}
          onUnauthorized={onUnauthorized}
        />
      )}
    </div>
  );
}
