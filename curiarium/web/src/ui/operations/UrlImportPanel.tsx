import React, { useEffect, useRef, useState } from "react";

import { UnauthorizedError } from "../../lib/api/http";
import {
  inspectImportLink,
  listImportZipEntries,
  type ImportInspectInfo,
  type ZipEntry,
} from "../../lib/api/imports";
import { loadSettings } from "../../lib/settings";
import ImportEntriesDialog from "./ImportEntriesDialog";
import ImportTargetControl, { targetParams, type ImportTarget } from "./ImportTargetControl";
import OperationPanel from "./OperationPanel";

type Props = {
  /** Contribute capability: every control here fetches and writes into the
   *  library. A viewer renders nothing and issues no request, not even the
   *  preflight that resolves DNS. */
  canContribute: boolean;
  /** The folder the import lands in, if the operator is inside one; absent means
   *  the library root. Sent to both the preflight and the plan. */
  folderId?: string | null;
  /** The host's completion signal: a run finished or was undone, so whatever the
   *  host owns (the grid, a project) should refetch. */
  onChanged?: () => void;
  /** Session expiry is the host's news, not this panel's. */
  onUnauthorized?: () => void;
};

const FOCUS_VISIBLE =
  "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2";
const INPUT_BASE = `rounded-md border border-panel-strong bg-panel-soft px-2 py-1.5 text-sm ${FOCUS_VISIBLE}`;
const SECONDARY_BUTTON =
  "px-3 py-2 rounded-md border border-panel-strong text-sm disabled:opacity-60";

/**
 * The vendor cookies saved in Settings, read at CALL time.
 *
 * Exactly `ui/review/useBundleDetect.ts`'s `savedThingiverseCookie` rule and for
 * the same reason: `App` holds settings in React state, `saveSettings` is the
 * only origin, and this door threads no settings prop through `App` - so the one
 * honest read is the store, at the moment a request is built. An empty cookie is
 * omitted rather than sent blank, because a blank Cookie header is not "no
 * cookie" to every vendor.
 */
function savedCookies(): { makerworld_cookie?: string; thingiverse_cookie?: string } {
  const settings = loadSettings();
  const cookies: { makerworld_cookie?: string; thingiverse_cookie?: string } = {};
  const makerworld = settings.makerworld.cookie.trim();
  if (makerworld !== "") cookies.makerworld_cookie = makerworld;
  const thingiverse = settings.thingiverse.cookie.trim();
  if (thingiverse !== "") cookies.thingiverse_cookie = thingiverse;
  return cookies;
}

/** The inspected filename without its last extension: the project-name draft a
 *  page or archive offers before the operator edits it. */
function fileStem(filename: string): string {
  const cut = filename.lastIndexOf(".");
  return cut > 0 ? filename.slice(0, cut) : filename;
}

/** The server's own sentence when it sent one, and the door's fallback when it
 *  did not: the client paraphrases nothing it did not decide. */
function failureMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message.trim() !== "" ? err.message : fallback;
}

/**
 * The URL import door, restored (Mf-x65b): a link is inspected, given a place to
 * land, and then planned and run as the `import-link` or `import-zip` OPERATION.
 *
 * Every stage is real and each refusal is its own `role="alert"`: the preflight
 * (`inspectImportLink`) states what the link would fetch, the target control
 * states where it lands, the entry picker states which members of a zip travel,
 * and `OperationPanel` states the plan, the one action and the run. For a zip the
 * panel is not offered at all until a selection exists - the server refuses an
 * `import-zip` plan with no `entries` (400), so that step belongs to the picker
 * and never to a plan nobody could approve.
 */
export default function UrlImportPanel(props: Props): React.ReactElement | null {
  const { canContribute, folderId, onChanged, onUnauthorized } = props;

  const [url, setUrl] = useState("");
  const [checking, setChecking] = useState(false);
  const [checkFailure, setCheckFailure] = useState<string | null>(null);
  const [inspected, setInspected] = useState<ImportInspectInfo | null>(null);
  const [target, setTarget] = useState<ImportTarget>({ kind: "none" });
  const [listing, setListing] = useState(false);
  const [listFailure, setListFailure] = useState<string | null>(null);
  const [entries, setEntries] = useState<ZipEntry[] | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  // The confirmed selection, or null when the picker has not answered yet: a zip
  // is not a plan until the operator has said which members it takes.
  const [chosen, setChosen] = useState<string[] | null>(null);
  // The filename whose stem has already been drafted into the project name, so
  // the draft is written once per inspected page and never over an edit.
  const prefilledFor = useRef<string | null>(null);

  // A new page invalidates everything the previous one decided: its members, its
  // selection and its draft name are facts about a different URL, and offering a
  // plan for them would be a review of something the operator is not looking at.
  function forgetInspection(): void {
    setInspected(null);
    setEntries(null);
    setPickerOpen(false);
    setChosen(null);
    prefilledFor.current = null;
  }

  function changeUrl(next: string): void {
    setUrl(next);
    setCheckFailure(null);
    if (inspected !== null) forgetInspection();
  }

  async function checkLink(): Promise<void> {
    const trimmed = url.trim();
    if (trimmed === "") return;
    setChecking(true);
    setCheckFailure(null);
    try {
      const info = await inspectImportLink({
        url: trimmed,
        ...savedCookies(),
        ...(folderId ? { folder_id: folderId } : {}),
      });
      setInspected(info);
      setEntries(null);
      setChosen(null);
      setPickerOpen(false);
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized?.();
        return;
      }
      forgetInspection();
      setCheckFailure(failureMessage(err, "Failed to inspect the import link"));
    } finally {
      setChecking(false);
    }
  }

  async function chooseEntries(): Promise<void> {
    const trimmed = url.trim();
    if (trimmed === "") return;
    setListing(true);
    setListFailure(null);
    try {
      const answer = await listImportZipEntries({ url: trimmed, ...savedCookies() });
      setEntries(answer.entries);
      setPickerOpen(true);
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized?.();
        return;
      }
      setListFailure(failureMessage(err, "Failed to list the import zip's entries"));
    } finally {
      setListing(false);
    }
  }

  // The draft name, written once per inspected filename and only into an empty
  // "new project" name: the effect runs on every render, so the guard is what
  // keeps the operator's own edit from being overwritten.
  useEffect(() => {
    const filename = inspected?.filename ?? null;
    if (filename === null) return;
    if (target.kind !== "new" || target.projectName.trim() !== "") return;
    if (prefilledFor.current === filename) return;
    prefilledFor.current = filename;
    setTarget({ kind: "new", projectName: fileStem(filename) });
  }, [inspected, target]);

  // Every hook runs before the viewer gate, so the early return changes no hook
  // order; the gate is here because the preflight resolves DNS and a viewer must
  // not be able to provoke even a read.
  if (!canContribute) return null;

  const isZip = inspected?.is_zip ?? false;
  const trimmedUrl = url.trim();

  function importParams(): Record<string, unknown> {
    const params: Record<string, unknown> = { ...targetParams(target), ...savedCookies() };
    if (folderId) params.folder_id = folderId;
    if (inspected !== null) params.filename = inspected.filename;
    if (isZip && chosen !== null) params.entries = chosen;
    return params;
  }

  const draftSelection =
    chosen ?? (entries ?? []).filter(entry => !entry.junk).map(entry => entry.path);

  return (
    <div className="flex flex-col gap-2" data-url-import="true">
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="url"
          aria-label="Import link"
          placeholder="Paste model link (MakerWorld, Printables, Thingiverse...)"
          className={`${INPUT_BASE} w-80`}
          value={url}
          onChange={event => changeUrl(event.target.value)}
          onKeyDown={event => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            void checkLink();
          }}
        />
        <button
          type="button"
          className={SECONDARY_BUTTON}
          disabled={checking || trimmedUrl === ""}
          onClick={() => void checkLink()}
        >
          {checking ? "Checking..." : "Check the link"}
        </button>
      </div>

      {checkFailure !== null && (
        <span
          role="alert"
          className="review-detail-reason text-red-500"
          data-url-import-check-failure="true"
        >
          {checkFailure}
        </span>
      )}

      {inspected !== null && (
        <div
          className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm"
          data-url-import-info="true"
        >
          <span>{inspected.filename}</span>
          <span className="opacity-70">{inspected.mime}</span>
          <span className="opacity-70">{inspected.is_zip ? "zip archive" : "single file"}</span>
        </div>
      )}

      {inspected !== null && (
        <ImportTargetControl
          value={target}
          onChange={setTarget}
          onUnauthorized={onUnauthorized}
        />
      )}

      {isZip && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <button
            type="button"
            className={SECONDARY_BUTTON}
            disabled={listing}
            onClick={() => void chooseEntries()}
          >
            {listing ? "Reading the files..." : "Choose files"}
          </button>
          {chosen !== null && entries !== null && (
            <span className="opacity-70" data-url-import-selection="true">
              {`${chosen.length} of ${entries.length} files chosen`}
            </span>
          )}
        </div>
      )}

      {listFailure !== null && (
        <span
          role="alert"
          className="review-detail-reason text-red-500"
          data-url-import-list-failure="true"
        >
          {listFailure}
        </span>
      )}

      {pickerOpen && entries !== null && (
        <ImportEntriesDialog
          title="Choose files"
          label={inspected?.filename ?? trimmedUrl}
          entries={entries}
          // A URL zip's listing carries junk verdicts and no reasons, so the
          // dialog's `excluded` stays empty and the verdict itself disables a row.
          excluded={[]}
          selected={draftSelection}
          onConfirm={paths => {
            setChosen(paths);
            setPickerOpen(false);
          }}
          onClose={() => setPickerOpen(false)}
        />
      )}

      {inspected !== null && (!isZip || chosen !== null) && (
        <OperationPanel
          workflow={isZip ? "import-zip" : "import-link"}
          target={trimmedUrl}
          params={importParams()}
          canContribute={canContribute}
          dataPrefix="url-import"
          onChanged={onChanged}
          onUnauthorized={onUnauthorized}
        />
      )}
    </div>
  );
}
