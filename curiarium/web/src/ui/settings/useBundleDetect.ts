import React from "react";
import {
  UnauthorizedError,
  createBundles,
  getBundleReport,
  setBundleDismissed,
  type BundleCreateItem,
  type BundleReport,
} from "../../lib/api";
import type { Section } from "./types";

type Props = {
  section: Section;
  /** Write role (editor+): a viewer must never fire the detection request. */
  enabled: boolean;
  onUnauthorized?: () => void;
};

export type BundleDetectState = {
  report: BundleReport | null;
  loading: boolean;
  busy: boolean;
  error: string | null;
  showDismissed: boolean;
  setShowDismissed: (value: boolean) => void;
  refresh: () => Promise<void>;
  create: (items: BundleCreateItem[]) => Promise<void>;
  dismiss: (keys: string[], dismissed: boolean) => Promise<void>;
};

/**
 * Vendor bundle detection for the Imports section. Detection walks the whole
 * library server-side, so the hook is inert until the section is open and the
 * caller may write, fetches exactly once per activation and never polls. A
 * failure becomes an error string; nothing here throws out of the hook.
 */
export function useBundleDetect({ section, enabled, onUnauthorized }: Props): BundleDetectState {
  const active = enabled && section === "imports";
  const [report, setReport] = React.useState<BundleReport | null>(null);
  const [fetching, setFetching] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [showDismissed, setShowDismissed] = React.useState(false);
  const cancelled = React.useRef(false);

  const refresh = React.useCallback(async () => {
    if (!active) return;
    setFetching(true);
    try {
      // Dismissed rows stay in the payload so the card can reveal them without
      // a second round trip; the card does the filtering.
      const next = await getBundleReport({ includeDismissed: true });
      if (cancelled.current) return;
      setReport(next);
      setError(null);
    } catch (err) {
      if (cancelled.current) return;
      if (err instanceof UnauthorizedError) onUnauthorized?.();
      setError("Bundle detection is unavailable.");
    } finally {
      if (!cancelled.current) setFetching(false);
    }
  }, [active, onUnauthorized]);

  React.useEffect(() => {
    if (!active) return;
    cancelled.current = false;
    void refresh();
    return () => {
      cancelled.current = true;
    };
  }, [active, refresh]);

  const mutate = React.useCallback(
    async (call: () => Promise<unknown>, fallback: string) => {
      if (!active) return;
      setBusy(true);
      try {
        await call();
        setError(null);
        await refresh();
      } catch (err) {
        if (err instanceof UnauthorizedError) onUnauthorized?.();
        setError(err instanceof Error && err.message ? err.message : fallback);
      } finally {
        setBusy(false);
      }
    },
    [active, onUnauthorized, refresh],
  );

  const create = React.useCallback(
    async (items: BundleCreateItem[]) => {
      await mutate(() => createBundles(items), "Failed to create the selected bundles.");
    },
    [mutate],
  );

  const dismiss = React.useCallback(
    async (keys: string[], dismissed: boolean) => {
      await mutate(
        () => setBundleDismissed(keys, dismissed),
        dismissed ? "Failed to dismiss the selected bundles." : "Failed to restore the bundle.",
      );
    },
    [mutate],
  );

  return {
    report,
    // Before the first report arrives there is nothing to show yet, so report
    // the pending state rather than flashing the empty one.
    loading: fetching || (active && report === null && error === null),
    busy,
    error,
    showDismissed,
    setShowDismissed,
    refresh,
    create,
    dismiss,
  };
}
