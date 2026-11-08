// The create-time half of a derived cover: a bundle with no image is still
// renderable from its own model, so the project it becomes gets a face. The
// render is the same client-side snapshot job the asset grid already runs
// (Mf-5915), queued and persisted through the same endpoint - the only new
// decision here is that it can be asked for before the project exists, because
// the asset the cover is rendered from is registered already.

import { extOf } from "../../lib/assetKinds";
import type { BundleDetectItem } from "../../lib/api/bundles";
import { uploadGeneratedThumbnail } from "../../lib/api/upload";
import { primeGrants } from "../../lib/fileGrants";
import { previewQueue } from "../../lib/previewQueue";
import { generateModelSnapshot, type SnapshotJob } from "../ModelViewer";

/** Big enough for a project card, small enough that a 40 KB PNG is not a
 *  megabyte. The grid renders its own snapshots at the card's pixel size; a
 *  persisted cover outlives that layout, so it is rendered once at a fixed one. */
export const COVER_SNAPSHOT_SIZE = 512;

type PrimeModelCoverOptions = {
  /** Resolves an API-relative cover path into a browser-loadable, grant-signed
   *  URL. Required, not defaulted: this module must not learn the API base, and
   *  a silent identity default would fetch `/file/...` off the page origin and
   *  fail only in production. The render site's owner supplies `fileUrl`. */
  mediaUrl: (rel: string) => string;
  render?: SnapshotJob;
  save?: (id: string, blob: Blob) => Promise<unknown>;
};

/** The one row shape this module acts on: a model-preview cover whose asset is
 *  registered and whose thumbnail is not generated yet. */
function needsRender(row: BundleDetectItem): boolean {
  const cover = row.cover;
  return (
    cover.reason === "model-preview" &&
    Boolean(cover.asset_id) &&
    !cover.thumb_url &&
    Boolean(cover.url)
  );
}

/**
 * Renders and persists a cover for every row that only has a model preview,
 * returning how many were saved.
 *
 * Fire-and-forget by design: a cover must never block or fail a create, so each
 * row's failure is a warning and the next row still runs. Never throws.
 */
export async function primeModelCovers(
  rows: readonly BundleDetectItem[],
  options: PrimeModelCoverOptions,
): Promise<number> {
  const { mediaUrl, render = generateModelSnapshot, save = uploadGeneratedThumbnail } = options;
  let saved = 0;
  for (const row of rows) {
    if (!needsRender(row)) continue;
    // Read once and narrowed here, so the closure and the save below need no
    // non-null assertion: `needsRender` cannot narrow for the compiler. The two
    // paths are kept apart on purpose - `servedPath` is where the bytes are
    // fetched from, `storagePath` is what names the file's type.
    const { asset_id: assetId, url: servedPath, rel_path: storagePath } = row.cover;
    if (!assetId || !servedPath) continue;
    try {
      // The render fetches the model with a browser request, so the grant has to
      // be minted first, exactly as the grid's card preview does.
      await primeGrants([servedPath]);
      // Nobody cancels a create-time cover: an unmounted row has no owner left
      // to cancel it, and the queue's own job budget is the bound. The signal is
      // constructed anyway because `enqueue` requires one - deleting it would
      // break the queue's contract, not save a line.
      const controller = new AbortController();
      const image = await previewQueue.enqueue({
        signal: controller.signal,
        run: signal =>
          render(
            mediaUrl(servedPath),
            extOf(storagePath ?? ""),
            COVER_SNAPSHOT_SIZE,
            COVER_SNAPSHOT_SIZE,
            "light",
            signal,
          ),
      });
      // `null` means no result will ever arrive (budget spent, job dropped or
      // timed out): nothing to persist, and retrying would only burn the budget.
      if (image === null) continue;
      const blob = await fetch(image).then(response => response.blob());
      await save(assetId, blob);
      saved += 1;
    } catch (err) {
      console.warn(`Cover preview for ${row.key} could not be saved:`, err);
    }
  }
  return saved;
}
