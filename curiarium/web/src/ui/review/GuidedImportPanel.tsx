import React from "react";

// Imported from the domain module, not the `../../lib/api` barrel: several
// suites replace that barrel with a fixed object, so a named import from it
// would come back `undefined` there (`useBundleDetect.ts` documents the same
// rule).
import {
  createBundles,
  enrichBundles,
  getArrangePlan,
  type BundleArrangePlan,
  type BundleCoverCandidate,
  type BundleCreateItem,
  type BundleDetectItem,
  type BundleSuggestion,
} from "../../lib/api/bundles";
import { UnauthorizedError } from "../../lib/api/http";
import { notifyInfo } from "../../lib/toast";
import ArrangePlanPanel from "./ArrangePlanPanel";
import {
  METADATA_FIELDS,
  arrangeActionLabel,
  arrangeMoveLine,
  arrangeRefusalLine,
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
 * The guided import door: everything the engine parsed for one row, in the
 * operator's hands before anything is written. Self-contained on purpose - it
 * owns its own plan read and its own write, so the host only supplies the row,
 * the gate and its callbacks.
 *
 * Nothing is pre-accepted: `accepted`/`dismissed` start empty, and a create
 * writes only what this panel's map names. A shape the server cannot enact is
 * handed to `ArrangePlanPanel`, the one door that can move the bytes.
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
  const [plan, setPlan] = React.useState<BundleArrangePlan | null>(null);
  const [planFailure, setPlanFailure] = React.useState<string | null>(null);
  const [enrichLine, setEnrichLine] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [failure, setFailure] = React.useState<string | null>(null);
  const [already, setAlready] = React.useState<string | null>(null);

  // The host may hand this panel a fresh callback identity every render, so the
  // plan effect reads it through a ref: the plan is keyed by the row and the
  // shape alone, and a READ must not run again because a parent re-rendered.
  const unauthorizedRef = React.useRef(onUnauthorized);
  unauthorizedRef.current = onUnauthorized;

  // A plan is a READ: fetch it for the shape on offer, and again whenever the
  // operator picks another. A viewer issues no request at all.
  React.useEffect(() => {
    if (!canContribute) return;
    let live = true;
    setPlan(null);
    setPlanFailure(null);
    getArrangePlan(row.key, shape)
      .then(next => {
        if (live) setPlan(next);
      })
      .catch((err: unknown) => {
        if (!live) return;
        // A 401 is the host's cue to sign the operator out, so the panel writes
        // no sentence of its own.
        if (err instanceof UnauthorizedError) {
          unauthorizedRef.current?.();
          return;
        }
        setPlanFailure(failureDetail(err, "Failed to preview the arrangement."));
      });
    return () => {
      live = false;
    };
  }, [row.key, shape, canContribute]);

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
  const moves = plan?.moves ?? [];
  const creates = plan?.creates ?? [];
  const refusals = plan?.refusals ?? [];
  // The server's verdict and the plan's own moves both say the files must be
  // arranged first: one when the shape is refused outright, the other when it
  // is enactable but not from where the files sit today.
  const needsArranging = chosenMatch?.creatable === false || moves.length > 0;
  const applyDisabled =
    busy ||
    plan === null ||
    planFailure !== null ||
    refusals.length > 0 ||
    moves.length > 0 ||
    chosenMatch?.creatable === false;

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

  async function enrich(): Promise<void> {
    setBusy(true);
    setFailure(null);
    try {
      const response = await enrichBundles([row.key]);
      const result = response.results[0];
      if (result === undefined) {
        setEnrichLine("The source page answered nothing for this bundle.");
      } else if (result.reason !== null) {
        // The server's own sentence: it decided the refusal, so it says why.
        setEnrichLine(result.reason);
      } else if (result.filled.length === 0) {
        setEnrichLine(`Filled nothing from ${result.source_url ?? "the source page"}`);
      } else {
        setEnrichLine(
          `Filled ${result.filled.join(", ")} from ${result.source_url ?? "the source page"}`,
        );
      }
      onChanged?.();
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized?.();
        return;
      }
      setFailure(failureDetail(err, "Failed to read the source page."));
    } finally {
      setBusy(false);
    }
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
          <button
            type="button"
            className={SECONDARY_BUTTON}
            data-guided-enrich="true"
            disabled={busy}
            onClick={() => void enrich()}
          >
            Read the source page
          </button>
        </div>
        {enrichLine !== null && <span data-guided-enrich-line="true">{enrichLine}</span>}
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

      <div className="review-detail-facts" role="group" aria-label="What will move">
        <span>What will move</span>
        {plan !== null && (
          <>
            <ul className="flex flex-col gap-1" data-guided-moves="true">
              {moves.map((move, index) => (
                <li
                  key={`${index}-${move.from}-${move.to}`}
                  data-guided-move="true"
                  data-guided-action={move.action}
                >
                  <span className="font-mono">{arrangeMoveLine(move)}</span>{" "}
                  <span className="text-muted">{arrangeActionLabel(move.action)}</span>
                </li>
              ))}
            </ul>
            {creates.map(create => (
              <span key={create.path} className="font-mono" data-guided-create="true">
                {create.path}
              </span>
            ))}
            {refusals.map(refusal => (
              <span
                key={`${refusal.status}-${refusal.reason}`}
                className="review-detail-reason text-red-500"
                data-guided-refusal="true"
              >
                {arrangeRefusalLine(refusal)}
              </span>
            ))}
            {moves.length === 0 && creates.length === 0 && (
              <span data-guided-no-moves="true">
                No file moves: the project is created from the files already in place.
              </span>
            )}
          </>
        )}
        {planFailure !== null && (
          <span
            className="review-detail-reason text-red-500"
            data-guided-plan-failure="true"
          >
            {planFailure}
          </span>
        )}
      </div>

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
        <ArrangePlanPanel
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
