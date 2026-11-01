import FolderScanPanel from "./FolderScanPanel";
import ImportRunsPanel from "./ImportRunsPanel";
import ReviewQueueView from "./ReviewQueueView";
import { useFolderScan } from "./useFolderScan";
import type { ImportRunsState } from "./useImportRuns";
import { describeQueueCount, importsBadgeCount } from "./useReviewCounts";

type Props = {
  state: ImportRunsState;
  /** `editor` and above: the folder panel creates folders and uploads files. */
  canWrite: boolean;
  onAssetsChanged?: () => void;
  onFoldersChanged?: () => void;
  onUnauthorized?: () => void;
  onSelectFolder?: (id: string | null) => void;
};

/**
 * The Imports running queue. The run state is App-owned (the sidebar badge reads
 * it too); the folder scan state is not: it lives exactly as long as this view
 * is mounted, so leaving the queue discards a selection and the last status
 * line, as it did inside Settings.
 *
 * The folder panel is hidden, not disabled, below `editor`, which is one rank
 * stricter than the two `contribute` routes it calls - creating the top-level
 * folder (`POST /folders`) and storing the files (`POST /upload`). A contributor
 * still sees the run, its counters, errors and Refresh - moving the UI broadens
 * no write permission.
 */
export default function ImportsReviewView({
  state,
  canWrite,
  onAssetsChanged,
  onFoldersChanged,
  onUnauthorized,
  onSelectFolder,
}: Props) {
  const folderScan = useFolderScan({
    enabled: canWrite,
    onAssetsChanged,
    onFoldersChanged,
    onUnauthorized,
    onSelectFolder,
  });
  const count = importsBadgeCount(state);

  return (
    <ReviewQueueView
      title="Imports running"
      explanation="The latest mounted-import run. While a run is walking the mount its counters update live, and the queue refreshes itself until it finishes."
      count={count}
      countTitle={describeQueueCount("imports", count)}
      lastRefreshedAt={state.lastRefreshedAt}
      onRefresh={() => void state.refresh()}
      refreshing={state.refreshing}
      notices={
        state.error && state.run ? (
          <p className="review-notice">
            {state.error} Refresh failed; the run below is the last one we could read.
          </p>
        ) : undefined
      }
    >
      <ImportRunsPanel state={state} />
      {canWrite && <FolderScanPanel {...folderScan} />}
    </ReviewQueueView>
  );
}
