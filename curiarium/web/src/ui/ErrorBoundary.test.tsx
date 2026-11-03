// @vitest-environment jsdom
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

vi.mock("../lib/reload", () => ({
  reloadWithFreshChunks: vi.fn(),
  buildReloadUrl: vi.fn(),
}));

import ErrorBoundary from "./ErrorBoundary";
import { reloadWithFreshChunks } from "../lib/reload";

const reloadMock = vi.mocked(reloadWithFreshChunks);

/** A component whose render throws; the chunk test gives it the browser's message. */
function Throws({ error }: { error: Error }): never {
  throw error;
}

// A boundary is only exercised by a failing render, and React logs every caught
// error; keep that noise out of the test output.
beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe("ErrorBoundary", () => {
  it("renders its children with no wrapper element on success", () => {
    const { container } = render(
      <ErrorBoundary>
        <p>child content</p>
      </ErrorBoundary>,
    );

    expect(screen.getByText("child content")).toBeTruthy();
    expect(container.childElementCount).toBe(1);
    expect(container.firstElementChild?.tagName).toBe("P");
  });

  // The faithful seam for a stale deploy: in a browser a failed dynamic
  // `import()` rejects the React.lazy loader promise with exactly this
  // TypeError, and React rethrows it at the nearest boundary. The render-throw
  // test below is the same error arriving after the chunk loaded.
  it("catches a rejected lazy chunk import and offers the reload action", async () => {
    const chunkError = new TypeError(
      "Failed to fetch dynamically imported module: /assets/Settings-stale.js",
    );
    const StaleChunk = React.lazy(() => Promise.reject(chunkError));

    render(
      <ErrorBoundary>
        <React.Suspense fallback={<p>loading the chunk</p>}>
          <StaleChunk />
        </React.Suspense>
      </ErrorBoundary>,
    );

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText("This view failed to load")).toBeTruthy();
    expect(screen.getByText(/updated since this page was opened/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reload app" })).toBeTruthy();
    // React never retries the rejected loader, so offering a retry would be a
    // dead action.
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("explains the chunk-failure message and offers only the reload action", () => {
    render(
      <ErrorBoundary>
        <Throws error={new TypeError("Failed to fetch dynamically imported module")} />
      </ErrorBoundary>,
    );

    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByText("This view failed to load")).toBeTruthy();
    expect(screen.getByText(/updated since this page was opened/)).toBeTruthy();
    const reload = screen.getByRole("button", { name: "Reload app" }) as HTMLButtonElement;
    expect(reload.disabled).toBe(false);
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("uses the generic copy for an unrelated render failure", () => {
    render(
      <ErrorBoundary>
        <Throws error={new Error("boom")} />
      </ErrorBoundary>,
    );

    expect(screen.getByText("This view could not be shown")).toBeTruthy();
    expect(screen.getByText(/You can try again, or reload the app/)).toBeTruthy();
    expect(screen.queryByText(/updated since this page was opened/)).toBeNull();
    // A generic render failure may be transient, so it keeps the retry.
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reload app" })).toBeTruthy();
  });

  it("recovers when Try again re-renders a fixed child", () => {
    let shouldThrow = true;
    function MaybeThrows() {
      if (shouldThrow) throw new Error("boom");
      return <p>recovered</p>;
    }

    render(
      <ErrorBoundary>
        <MaybeThrows />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeTruthy();

    // The fault is gone by the time the user retries, so the retry succeeds.
    shouldThrow = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(screen.getByText("recovered")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("reloads through the cache-busting helper, without navigating itself", () => {
    const searchBefore = window.location.search;

    render(
      <ErrorBoundary>
        <Throws error={new TypeError("Failed to fetch dynamically imported module")} />
      </ErrorBoundary>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Reload app" }));

    expect(reloadMock).toHaveBeenCalledTimes(1);
    // The real helper would replace the document URL; nothing here does.
    expect(window.location.search).toBe(searchBefore);
  });
});
