/**
 * Recovery reload for a stale document (Mf-v1nt).
 *
 * `index.html` is served with `no-cache` and the content-hashed `/assets/*`
 * files with `immutable`. A document fetched before a deploy still names the
 * old chunk hashes, so its next dynamic `import()` rejects once the old files
 * are gone. Re-fetching the document is what names the current chunk graph, and
 * the `reload=<now>` parameter makes that document URL a fresh cache key.
 *
 * It guarantees a fresh document request. It does NOT invalidate old hashed
 * chunks that are still in a cache (harmless: nothing references them any
 * more), and it cannot help if the server itself still returns the old
 * `index.html`.
 */

/**
 * The current URL with a `reload=<now>` cache-buster, keeping any existing
 * query parameters and the fragment.
 */
export function buildReloadUrl(
  pathname: string,
  search: string,
  hash: string,
  now: number,
): string {
  const params = new URLSearchParams(search);
  params.set("reload", String(now));
  return `${pathname}?${params.toString()}${hash}`;
}

/** Re-fetches the document so dynamic imports resolve against current chunks. */
export function reloadWithFreshChunks(): void {
  if (typeof window === "undefined") return;
  const { pathname, search, hash } = window.location;
  // `replace()` not `assign()`: a recovery reload must not grow history.
  window.location.replace(buildReloadUrl(pathname, search, hash, Date.now()));
}
