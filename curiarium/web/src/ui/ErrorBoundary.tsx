import React from "react";
import { reloadWithFreshChunks } from "../lib/reload";

export type ErrorBoundaryProps = {
  children: React.ReactNode;
  /** Renders the small inline fallback instead of the full-view card. */
  compact?: boolean;
};

type ErrorBoundaryState = { error: Error | null };

/** The browser's message when a `React.lazy` import rejects after a deploy. */
const CHUNK_LOAD_FAILURE =
  /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module/i;

const CHUNK_COPY = {
  title: "This view failed to load",
  body: "The app was updated since this page was opened, so part of it could not be downloaded. Reload to get the current version.",
};

const GENERIC_COPY = {
  title: "This view could not be shown",
  body: "Something went wrong while rendering this view. You can try again, or reload the app.",
};

const ACTION_BUTTON = "px-2.5 py-1.5 rounded-md border border-panel-strong text-xs";

/**
 * Catches a rendering error in its children and offers a way out.
 *
 * React needs a class for this. On success it returns `children` untouched, not
 * a wrapper element: App's structural tests pin `<main>`/root node identity and
 * its unkeyed children order, and a wrapper would remount the grid.
 */
export default class ErrorBoundary extends React.Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return { error: error instanceof Error ? error : new Error(String(error)) };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    // No toast: the fallback below already tells the user what happened.
    console.error("ErrorBoundary caught a rendering error", error, info.componentStack);
  }

  private handleRetry = (): void => {
    this.setState({ error: null });
  };

  /** The fallback's actions; shared by the full and compact variants. */
  private renderActions(chunkFailed: boolean): React.ReactNode {
    // A rejected lazy chunk loader stays rejected for the page's lifetime, so
    // retrying it cannot work — only a fresh document (Reload) fetches the
    // current chunk graph. A generic render failure may be transient, so it
    // keeps both actions.
    return (
      <>
        {chunkFailed ? null : (
          <button type="button" className={ACTION_BUTTON} onClick={this.handleRetry}>
            Try again
          </button>
        )}
        <button type="button" className={ACTION_BUTTON} onClick={reloadWithFreshChunks}>
          Reload app
        </button>
      </>
    );
  }

  render(): React.ReactNode {
    const { error } = this.state;
    if (error === null) return this.props.children;

    const chunkFailed = CHUNK_LOAD_FAILURE.test(error.message);
    const copy = chunkFailed ? CHUNK_COPY : GENERIC_COPY;
    if (this.props.compact) {
      return (
        <div
          role="alert"
          className="flex h-full w-full flex-col items-center justify-center gap-2 p-3 text-center text-xs"
        >
          <div className="font-semibold">{copy.title}</div>
          <p className="text-muted">{copy.body}</p>
          <div className="flex flex-wrap items-center justify-center gap-2">
            {this.renderActions(chunkFailed)}
          </div>
        </div>
      );
    }

    return (
      <div
        role="alert"
        className="rounded-lg border border-panel bg-panel-soft p-4 flex flex-col gap-3"
      >
        <div className="text-lg font-semibold">{copy.title}</div>
        <p className="text-sm text-muted">{copy.body}</p>
        <div className="flex flex-wrap items-center gap-2">{this.renderActions(chunkFailed)}</div>
      </div>
    );
  }
}
