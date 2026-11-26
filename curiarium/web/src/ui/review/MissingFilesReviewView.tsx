import type { RefObject } from "react";

import type { EngravingSettings, PreviewSettings, ResolvedTheme, SlicerSettings } from "../../lib/settings";
import AssetGrid from "../asset-grid/AssetGrid";
import type { MissingScanOutcome } from "../asset-grid/assetGridMeta";
import LibraryScanPanel from "./LibraryScanPanel";
import ReviewQueueView from "./ReviewQueueView";
import ScanStorageAction from "./ScanStorageAction";
import { describeQueueCount } from "./useReviewCounts";
import { scanOutcome, type LibraryScanState } from "./useLibraryScan";

type Props = {
  state: LibraryScanState;
  /** `editor` and above: the rescan is `POST /library/scan` (write). */
  canWrite: boolean;
  canContribute: boolean;
  canDelete: boolean;
  /** App's `counts?.missing`: `null` until the whole-library counts land. */
  missingCount: number | null;
  assetsVersion: number;
  onRefresh: () => void;
  onAssetsFiled: () => void;
  /**
   * App's `scanFromShell`: the shell's sticky arm plus its one `run`. Both doors
   * in the queue (the panel and the empty state) hand this down, so the poll
   * stays alive after the operator leaves for the library.
   */
  onScan: () => void;
  /** App's `handleFoldersChanged`: a card's settled Re-scan creates folders, so
   *  the sidebar tree must refetch. */
  onFoldersChanged: () => void;
  onUnauthorized?: () => void;
  slicerSettings?: SlicerSettings;
  engravingSettings?: EngravingSettings;
  previewSettings?: PreviewSettings;
  theme: ResolvedTheme;
  scrollElementRef?: RefObject<HTMLElement | null>;
};

/**
 * What the header's "last refreshed" line may claim, per scan outcome. Only a
 * clean scan says the flag set is complete.
 */
const SCAN_LABELS: Record<MissingScanOutcome, string> = {
  unknown: "No completed scan is known for this session",
  clean: "Flagged set from the last completed scan; every check passed",
  failed: "Flagged set from the last scan, which could not check every file",
};

/**
 * The Missing files queue: the rescan surface plus the real `AssetGrid` pinned to
 * `missingOnly`.
 *
 * The queue mounts its own grid instead of cloning the library's, and passes no
 * `onNavigateFolder`: `AssetGrid` already hides the breadcrumbs, the folder
 * tiles, the uploader and drag/drop in `missingOnly` mode, so there is no escape
 * path back into the library from here.
 *
 * The pin itself lives in `useAssetList`'s closure and is absent from
 * `RefreshOpts`, so the toolbar's search, sort, tag, creator and
 * "not in any project" controls can only NARROW the queue - never drop the
 * missing filter. `Reset` clears tags and creator only, for the same reason.
 *
 * `AssetGrid` cannot report when its own list settled, so the header says what
 * the scan knows rather than when a fetch finished. A card-initiated Re-scan
 * reports the statuses it polls back through `state.adopt`, so the header and
 * the empty state follow the scan that produced the rows rather than the last
 * scan this hook read itself.
 *
 * The queue no longer starts a scan itself: both of its doors (the panel and
 * the empty-state action) hand the shell's `scanFromShell`, so the shell's
 * sticky arm is set before `run()` and the poll keeps running after the operator
 * leaves Review for the library.
 */
export default function MissingFilesReviewView({
  state,
  canWrite,
  canContribute,
  canDelete,
  missingCount,
  assetsVersion,
  onRefresh,
  onAssetsFiled,
  onScan,
  onFoldersChanged,
  onUnauthorized,
  slicerSettings,
  engravingSettings,
  previewSettings,
  theme,
  scrollElementRef,
}: Props) {
  const missingScan = scanOutcome(state.scan);

  return (
    <ReviewQueueView
      title="Missing files"
      explanation="Files an asset still points at that the last scan could not find. Nothing is deleted from here: re-scan once the file is back, or forget the entry to drop the row."
      count={missingCount}
      countTitle={describeQueueCount("missing", missingCount)}
      lastRefreshedLabel={SCAN_LABELS[missingScan]}
      onRefresh={onRefresh}
    >
      <LibraryScanPanel state={state} canWrite={canWrite} onScan={onScan} />
      <AssetGrid
        missingOnly
        missingScan={missingScan}
        canContribute={canContribute}
        canDelete={canDelete}
        assetsVersion={assetsVersion}
        missingEmptyAction={
          canWrite ? (
            <ScanStorageAction
              state={state}
              canWrite={canWrite}
              surface="empty"
              onScan={onScan}
            />
          ) : undefined
        }
        onAssetsFiled={onAssetsFiled}
        onFoldersChanged={onFoldersChanged}
        onScanStatus={state.adopt}
        onUnauthorized={onUnauthorized}
        slicerSettings={slicerSettings}
        engravingSettings={engravingSettings}
        previewSettings={previewSettings}
        theme={theme}
        scrollElementRef={scrollElementRef}
      />
    </ReviewQueueView>
  );
}
