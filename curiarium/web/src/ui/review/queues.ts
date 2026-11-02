import { FileWarning, FolderUp, PackageSearch, Trash2, type LucideIcon } from "lucide-react";

import { roleAtLeast, type Role } from "../../lib/auth";
import type { ReviewQueueKey } from "../views";

/** One row of the Review group: which queue, what it is called, who may see it. */
export type ReviewQueue = {
  key: ReviewQueueKey;
  label: string;
  minRole: Role;
  icon: LucideIcon;
};

/**
 * One row per queue; later slices add their queue here rather than to the JSX.
 * `minRole` is per row because the queues' own routes differ: creating or
 * dismissing a bundle is CONTRIBUTE, while `GET /imports/runs` is READ - a
 * viewer already reads the run history, so the queue must not delete a proven
 * read surface. `GET /assets?missing=true` and `GET /library/scan` are READ too,
 * so a viewer reads the missing queue; only the rescan it offers is a WRITE. A
 * role below a row's threshold simply does not see that row.
 *
 * Trash follows the same split: `GET /trash` is require_read, so a viewer reads
 * the queue, while `POST /trash/purge` is require_admin - so the queue hides its
 * Empty trash control below admin rather than disabling it.
 */
export const QUEUES: readonly ReviewQueue[] = [
  { key: "bundles", label: "Bundles to confirm", minRole: "contributor", icon: PackageSearch },
  { key: "imports", label: "Imports running", minRole: "viewer", icon: FolderUp },
  { key: "missing", label: "Missing files", minRole: "viewer", icon: FileWarning },
  { key: "trash", label: "Trash", minRole: "viewer", icon: Trash2 },
];

/**
 * The rows this role may see, in table order. An unknown (`null`) role is below
 * every threshold, so it sees nothing - the same fail-closed rule as the
 * capabilities in `App.tsx`.
 */
export function visibleQueues(role: Role | null | undefined): ReviewQueue[] {
  return QUEUES.filter(queue => roleAtLeast(role ?? null, queue.minRole));
}

/**
 * Which queue a destination actually opens. The stored key can belong to a role
 * the session no longer holds (a demotion, or `bundles` stored while `role` is
 * still `null`), so the key is resolved against the visible rows before anything
 * renders or fetches: an invisible key falls back to the first visible row, and
 * a role with no visible row resolves to `null` - no Review destination at all.
 */
export function resolveQueue(
  role: Role | null | undefined,
  key: ReviewQueueKey | null | undefined,
): ReviewQueueKey | null {
  const visible = visibleQueues(role);
  if (!visible.length) return null;
  if (key != null && visible.some(queue => queue.key === key)) return key;
  return visible[0].key;
}
