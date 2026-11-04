import type { Asset } from "../../lib/api";
import type { PreviewSettings, ResolvedTheme } from "../../lib/settings";
import type { DetailSection } from "./AssetDetailsEditor";

export type AssetCardProps = {
  item: Asset;
  /**
   * Contribute capability: upload, import, tag and organise. Required, no
   * default, so a missed call site is a type error rather than a silent false.
   */
  canContribute: boolean;
  /**
   * Delete capability: destroy, rename and move. Separate from
   * `canContribute` because the API splits CONTRIBUTE from WRITE.
   */
  canDelete: boolean;
  onPreview: (asset: Asset | null) => void;
  onOpenDetails: (asset: Asset, section: DetailSection) => void;
  onDownloadSingle: (asset: Asset) => void;
  onDownloadByTag: (tag: string) => void;
  downloading: boolean;
  onDelete: (asset: Asset) => void;
  onAddToProject: (asset: Asset) => void;
  selected: boolean;
  onToggleSelected: (id: string) => void;
  slicerEnabled: boolean;
  slicerLabel: string;
  onOpenInSlicer: (asset: Asset) => void;
  engravingEnabled: boolean;
  engraverLabel: string;
  onOpenInEngraving: (asset: Asset) => void;
  theme: ResolvedTheme;
  previewMode: PreviewSettings["mode"];
  onAssetChanged: (asset: Asset) => void;
  onForgetMissing: (asset: Asset) => void;
  onRescanMissing: (asset: Asset) => void;
  /**
   * Opens the shared archive-extract dialog for a library `.zip`. Required, no
   * default, so a missed call site is a type error rather than a card menu that
   * silently loses the door.
   */
  onExtractArchive: (asset: Asset) => void;
  rescanning: boolean;
  onClearPreparedPrint: (asset: Asset) => void;
  preparedClearing: boolean;
};
