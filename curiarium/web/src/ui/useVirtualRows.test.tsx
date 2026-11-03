// @vitest-environment jsdom
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { createRef, useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ESTIMATED_ROW_HEIGHT, GRID_GAP_PX, chunkRows } from "../lib/virtualGrid";
import { useVirtualRows } from "./useVirtualRows";

// The hook is exercised through a harness rather than through `AssetGrid`: the
// subject is the scroll-container contract, and the real grid would drag the
// whole API surface (and its mocks) into a geometry test.

type Item = { id: string };

/** Scroll height of a 47-asset library at three columns: 16 rows. */
const BOTTOM_OF_47 = 16 * ESTIMATED_ROW_HEIGHT + 15 * GRID_GAP_PX;

/** A ref the test can point at whichever node is the scroller right now. */
type ScrollRef = { current: HTMLElement | null };

function makeItems(count: number): Item[] {
  return Array.from({ length: count }, (_, index) => ({ id: `asset-${index}` }));
}

function rectOf(top: number, height: number): DOMRect {
  return {
    top,
    bottom: top + height,
    height,
    left: 0,
    right: 0,
    width: 0,
    x: 0,
    y: top,
    toJSON: () => ({}),
  } as DOMRect;
}

function armScroller(element: HTMLElement): void {
  Object.defineProperty(element, "clientHeight", { value: 768, configurable: true });
  vi.spyOn(element, "getBoundingClientRect").mockReturnValue(rectOf(0, 768));
}

/** Make the wrapper's rect track the scroller's scroll position. */
function stubGridRect(scroller: HTMLElement): void {
  const gridWrap = document.querySelector<HTMLElement>("[data-virtual-grid]");
  if (!gridWrap) throw new Error("virtual grid wrapper not rendered");
  vi.spyOn(gridWrap, "getBoundingClientRect").mockImplementation(() =>
    rectOf(-scroller.scrollTop, 0),
  );
}

function rowIndexes(): number[] {
  return Array.from(document.querySelectorAll("[data-row-index]")).map(element =>
    Number(element.getAttribute("data-row-index")),
  );
}

/** Flushes the hook's rAF-throttled recompute and the React work it schedules. */
async function flushFrame(): Promise<void> {
  await act(async () => {
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
    await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
  });
}

async function scrollTo(scroller: HTMLElement, top: number): Promise<void> {
  scroller.scrollTop = top;
  fireEvent.scroll(scroller);
  await flushFrame();
}

type GridProps = {
  scrollElementRef: React.RefObject<HTMLElement | null>;
  items: readonly Item[];
  onCardClick?: (id: string) => void;
};

/** Renders the hook's window the way the grid does, without the grid's API. */
function Grid({ scrollElementRef, items, onCardClick }: GridProps) {
  const virtual = useVirtualRows({ scrollElementRef, items, cols: 3 });
  const rows = chunkRows(items, virtual.cols);
  return (
    <div ref={virtual.setGridWrapRef} data-virtual-grid>
      {rows.slice(virtual.startRow, virtual.endRow + 1).map((row, offset) => {
        const rowIndex = virtual.startRow + offset;
        return (
          <div key={rowIndex} data-row-index={rowIndex}>
            {row.map(item => (
              <div key={item.id} data-card-id={item.id} onClick={() => onCardClick?.(item.id)}>
                {item.id}
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}

/**
 * The ref is populated only after the first commit: parent effects run after
 * child effects, so the hook's effect bound its listener while the ref was
 * still null.
 */
function LateAttachHarness({
  items,
  scrollElementRef,
}: {
  items: readonly Item[];
  scrollElementRef: ScrollRef;
}) {
  const holder = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    scrollElementRef.current = holder.current;
  }, [scrollElementRef]);
  return (
    <div ref={holder} data-testid="scroller">
      <Grid scrollElementRef={scrollElementRef} items={items} />
    </div>
  );
}

describe("useVirtualRows scroll container (Mf-d8vc)", () => {
  afterEach(() => {
    cleanup();
    // The replacement test's scrollers live outside React's tree on purpose.
    document.body
      .querySelectorAll<HTMLElement>('[data-testid^="scroller-"]')
      .forEach(element => element.remove());
  });

  it("follows a scroller that is attached after the first commit", async () => {
    const items = makeItems(47);
    const scrollElementRef: ScrollRef = { current: null };
    render(<LateAttachHarness items={items} scrollElementRef={scrollElementRef} />);

    const scroller = document.querySelector<HTMLElement>('[data-testid="scroller"]');
    if (!scroller) throw new Error("scroll harness did not mount");
    expect(scrollElementRef.current).toBe(scroller);
    armScroller(scroller);
    stubGridRect(scroller);

    // Past the first overscan window, so a frozen grid is unmistakable.
    await scrollTo(scroller, 4000);

    const rows = rowIndexes();
    expect(rows[0]).toBeGreaterThan(0);
    expect(rows[rows.length - 1]).toBe(15);
  });

  it("follows a scroller that is replaced after the first commit", async () => {
    const items = makeItems(47);
    const activeRef: ScrollRef = { current: null };
    // The scroller is plain DOM outside React's tree: React must not own the
    // node whose replacement this test performs, or its own teardown would try
    // to remove a child that is no longer there.
    const scrollerA = document.createElement("div");
    scrollerA.dataset.testid = "scroller-a";
    document.body.appendChild(scrollerA);
    const gridHost = document.createElement("div");
    gridHost.dataset.testid = "grid-host";
    scrollerA.appendChild(gridHost);

    render(createPortal(<Grid scrollElementRef={activeRef} items={items} />, gridHost));
    activeRef.current = scrollerA;
    armScroller(scrollerA);
    stubGridRect(scrollerA);

    // Replace the node with plain DOM APIs, keeping the grid instance alive.
    const scrollerB = document.createElement("div");
    scrollerB.dataset.testid = "scroller-b";
    scrollerB.appendChild(gridHost);
    scrollerA.replaceWith(scrollerB);
    activeRef.current = scrollerB;
    armScroller(scrollerB);
    stubGridRect(scrollerB);

    // A detached node dispatches only to its own listeners: no capture path
    // reaches `document`, so the window must not move.
    await scrollTo(scrollerA, 4000);
    expect(rowIndexes()[0]).toBe(0);

    await scrollTo(scrollerB, 4000);
    const rows = rowIndexes();
    expect(rows[0]).toBeGreaterThan(0);
    expect(rows[rows.length - 1]).toBe(15);
  });

  it("reaches the last asset of a 47-asset library (16 rows of 3)", async () => {
    const items = makeItems(47);
    const scrollElementRef = createRef<HTMLDivElement>();
    const onCardClick = vi.fn();
    render(
      <div ref={scrollElementRef} data-testid="scroller">
        <Grid scrollElementRef={scrollElementRef} items={items} onCardClick={onCardClick} />
      </div>,
    );

    const scroller = document.querySelector<HTMLElement>('[data-testid="scroller"]');
    if (!scroller) throw new Error("scroll harness did not mount");
    armScroller(scroller);
    stubGridRect(scroller);

    // 16 rows: 16 * ESTIMATED_ROW_HEIGHT + 15 * GRID_GAP_PX.
    await scrollTo(scroller, BOTTOM_OF_47);

    const rows = rowIndexes();
    expect(rows[0]).toBeGreaterThanOrEqual(12);
    expect(rows[rows.length - 1]).toBe(15);
    const lastCard = document.querySelector<HTMLElement>('[data-card-id="asset-46"]');
    expect(lastCard).toBeTruthy();
    fireEvent.click(lastCard!);
    expect(onCardClick).toHaveBeenCalledWith("asset-46");
  });

  it("ignores a scroll inside the grid wrapper", async () => {
    const items = makeItems(47);
    const scrollElementRef = createRef<HTMLDivElement>();
    render(
      <div ref={scrollElementRef} data-testid="scroller">
        <Grid scrollElementRef={scrollElementRef} items={items} />
      </div>,
    );

    const scroller = document.querySelector<HTMLElement>('[data-testid="scroller"]');
    if (!scroller) throw new Error("scroll harness did not mount");
    armScroller(scroller);
    stubGridRect(scroller);

    // The scroller moved, but the only event is a card's own overflow: a
    // scroll inside the wrapper cannot move the grid, so nothing recomputes.
    scroller.scrollTop = BOTTOM_OF_47;
    const card = document.querySelector<HTMLElement>('[data-card-id="asset-0"]');
    if (!card) throw new Error("card asset-0 is not rendered");
    fireEvent.scroll(card);
    await flushFrame();
    expect(rowIndexes()[0]).toBe(0);

    fireEvent.scroll(scroller);
    await flushFrame();
    expect(rowIndexes()[0]).toBeGreaterThan(0);
  });
});
