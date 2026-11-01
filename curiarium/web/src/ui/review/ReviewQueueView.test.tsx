// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import ReviewQueueView, { formatClock } from "./ReviewQueueView";
import { describeQueueCount } from "./useReviewCounts";

/** A fixed instant so the clock's format, not the wall time, is under test. */
const FIXED_AT = Date.UTC(2026, 0, 2, 14, 5);

/** `HH:MM` plus whatever 12-hour marker the host locale appends. */
const CLOCK = /^\d{1,2}:\d{2}(?:\s?[ap]m)?$/i;

afterEach(() => {
  cleanup();
});

describe("ReviewQueueView header", () => {
  it("renders the title, the explanation and a dash for an unknown count", () => {
    render(
      <ReviewQueueView title="Bundles to confirm" explanation="Bundles the detector found." count={null} countTitle={describeQueueCount("bundles", null)}>
        <p>rows</p>
      </ReviewQueueView>,
    );

    expect(screen.getByRole("heading", { name: "Bundles to confirm" })).toBeTruthy();
    expect(screen.getByText("Bundles the detector found.")).toBeTruthy();

    const count = screen.getByText("\u2013");
    expect(count.getAttribute("title")).toBe("Count unavailable");
  });

  it("renders a known count with its noun in the title", () => {
    render(
      <ReviewQueueView title="Bundles to confirm" explanation="Bundles." count={12} countTitle={describeQueueCount("bundles", 12)}>
        <p>rows</p>
      </ReviewQueueView>,
    );

    const count = screen.getByText("12");
    expect(count.getAttribute("title")).toBe("12 bundles awaiting a decision");
  });

  it("formats the clock as HH:MM", () => {
    expect(formatClock(FIXED_AT)).toMatch(CLOCK);
  });

  it("says it has not refreshed before the first fetch", () => {
    render(
      <ReviewQueueView title="Bundles to confirm" explanation="Bundles." count={0} countTitle={describeQueueCount("bundles", 0)}>
        <p>rows</p>
      </ReviewQueueView>,
    );

    expect(screen.getByText("Not refreshed yet")).toBeTruthy();
  });

  it("stamps the last refresh once one succeeded", () => {
    render(
      <ReviewQueueView
        title="Bundles to confirm"
        explanation="Bundles."
        count={0}
        countTitle={describeQueueCount("bundles", 0)}
        lastRefreshedAt={FIXED_AT}
      >
        <p>rows</p>
      </ReviewQueueView>,
    );

    expect(screen.getByText(/^Last refreshed \d{1,2}:\d{2}(?:\s?[ap]m)?$/i)).toBeTruthy();
  });

  // The missing queue cannot observe when its own list settled: its rows belong
  // to AssetGrid. It states what the last scan knows instead, verbatim.
  it("renders the label verbatim instead of a timestamp", () => {
    render(
      <ReviewQueueView
        title="Missing files"
        explanation="Files an asset points at that are gone."
        count={0}
        countTitle={describeQueueCount("missing", 0)}
        lastRefreshedAt={FIXED_AT}
        lastRefreshedLabel="Flagged set from the last completed scan"
      >
        <p>rows</p>
      </ReviewQueueView>,
    );

    expect(screen.getByText("Flagged set from the last completed scan")).toBeTruthy();
    expect(screen.queryByText(/Last refreshed/)).toBeNull();
  });

  it("renders no refreshed line at all for null", () => {
    const { container } = render(
      <ReviewQueueView
        title="Missing files"
        explanation="Files an asset points at that are gone."
        count={0}
        countTitle={describeQueueCount("missing", 0)}
        lastRefreshedAt={FIXED_AT}
        lastRefreshedLabel={null}
      >
        <p>rows</p>
      </ReviewQueueView>,
    );

    expect(container.querySelector(".review-refreshed")).toBeNull();
    expect(screen.queryByText(/Last refreshed/)).toBeNull();
  });

  it("still shows Not refreshed yet without either prop", () => {
    const { unmount } = render(
      <ReviewQueueView title="Bundles to confirm" explanation="Bundles." count={0} countTitle={describeQueueCount("bundles", 0)}>
        <p>rows</p>
      </ReviewQueueView>,
    );

    expect(screen.getByText("Not refreshed yet")).toBeTruthy();
    unmount();

    // An explicit `null` timestamp is the same "nothing fetched yet" state.
    render(
      <ReviewQueueView
        title="Bundles to confirm"
        explanation="Bundles."
        count={0}
        countTitle={describeQueueCount("bundles", 0)}
        lastRefreshedAt={null}
      >
        <p>rows</p>
      </ReviewQueueView>,
    );

    expect(screen.getByText("Not refreshed yet")).toBeTruthy();
  });
});

describe("ReviewQueueView refresh control", () => {
  it("renders no refresh button for a queue that does not offer one", () => {
    render(
      <ReviewQueueView title="Bundles to confirm" explanation="Bundles." count={1} countTitle={describeQueueCount("bundles", 1)}>
        <p>rows</p>
      </ReviewQueueView>,
    );

    expect(screen.queryByRole("button", { name: "Refresh" })).toBeNull();
  });

  it("calls onRefresh once on click", () => {
    const onRefresh = vi.fn();
    render(
      <ReviewQueueView title="Bundles to confirm" explanation="Bundles." count={1} countTitle={describeQueueCount("bundles", 1)} onRefresh={onRefresh}>
        <p>rows</p>
      </ReviewQueueView>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("reads Refreshing… and is disabled while a refresh runs", () => {
    const onRefresh = vi.fn();
    render(
      <ReviewQueueView
        title="Bundles to confirm"
        explanation="Bundles."
        count={1}
        countTitle={describeQueueCount("bundles", 1)}
        onRefresh={onRefresh}
        refreshing
      >
        <p>rows</p>
      </ReviewQueueView>,
    );

    const button = screen.getByRole("button", { name: "Refreshing…" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    fireEvent.click(button);
    expect(onRefresh).not.toHaveBeenCalled();
  });
});

describe("ReviewQueueView body", () => {
  it("renders queue-wide notices and the queue's own rows", () => {
    const { container } = render(
      <ReviewQueueView
        title="Bundles to confirm"
        explanation="Bundles."
        count={2}
        countTitle={describeQueueCount("bundles", 2)}
        notices={<p className="review-notice">Detection stopped early.</p>}
      >
        <ul>
          <li>bundle row</li>
        </ul>
      </ReviewQueueView>,
    );

    const queue = container.querySelector(".review-queue");
    if (!queue) throw new Error("No review queue region");
    expect(queue.contains(screen.getByText("Detection stopped early."))).toBe(true);
    expect(screen.getByText("bundle row")).toBeTruthy();
  });
});
