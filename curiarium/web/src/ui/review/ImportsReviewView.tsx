import FolderScanPanel from "./FolderScanPanel";
import MountImportPanel from "./MountImportPanel";
import ReviewQueueView from "./ReviewQueueView";
import { useFolderScan } from "./useFolderScan";
import type { MountImportState } from "./useMountImport";
import { describeQueueCount, importsBadgeCount } from "./useReviewCounts";

type Props = {
  state: MountImportState;
  /** `editor` and above: the folder panel creates folders and uploads files. */
  canWrite: boolean;
  onAssetsChanged?: () => void;
  onFoldersChanged?: () => void;
  onUnauthorized?: () => void;
  onSelectFolder?: (id: string | null) => void;
};

/**
 * The Imports running queue. The walk state is App-owned (the sidebar badge reads
 * it too); the folder scan state is not: it lives exactly as long as this view
 * is mounted, so leaving the queue discards a selection and the last status
 * line, as it did inside Settings.
 *
 * The folder panel is hidden, not disabled, below `editor`, which is one rank
 * stricter than the two `contribute` routes it calls - creating the top-level
 * folder (`POST /folders`) and storing the files (`POST /upload`). A contributor
 * still sees the walk, its counters, errors and Refresh - moving the UI broadens
 * no write permission. The walk itself is one `GET /operations?workflow=mount-import`
 * read, which is `read` like the rest of the ledger, so every role sees it.
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
      explanation="The newest mounted-import operation in the operations ledger. The queue refreshes itself until the walk finishes, and the totals land when the walk does."
      count={count}
      countTitle={describeQueueCount("imports", count)}
      lastRefreshedAt={state.lastRefreshedAt}
      onRefresh={() => void state.refresh()}
      refreshing={state.refreshing}
      notices={
        state.error && state.status ? (
          <p className="review-notice">
            {state.error} Refresh failed; the record below is the last one we could read.
          </p>
        ) : undefined
      }
    >
      <MountImportPanel state={state} />
      {canWrite && <FolderScanPanel {...folderScan} />}
    </ReviewQueueView>
  );
}
