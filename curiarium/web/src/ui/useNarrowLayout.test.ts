// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";

import indexCss from "../index.css?raw";
import { NARROW_MAX_WIDTH_PX, NARROW_MEDIA_QUERY, useNarrowLayout } from "./useNarrowLayout";

type Listener = () => void;

/** A `MediaQueryList` stand-in that records its listeners, like a browser's. */
class FakeMediaQueryList {
  matches: boolean;
  readonly media: string;
  private readonly listeners = new Set<Listener>();

  constructor(media: string, matches: boolean) {
    this.media = media;
    this.matches = matches;
  }

  addEventListener(_type: string, listener: Listener): void {
    this.listeners.add(listener);
  }

  removeEventListener(_type: string, listener: Listener): void {
    this.listeners.delete(listener);
  }

  /** Fires every registered `change` listener, as the browser would. */
  dispatchChange(): void {
    for (const listener of Array.from(this.listeners)) listener();
  }

  listenerCount(): number {
    return this.listeners.size;
  }
}

let created: FakeMediaQueryList[] = [];

/** Installs a `matchMedia` that hands out a fresh query object per call. */
function stubMatchMedia(matches: boolean): void {
  created = [];
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: (query: string) => {
      const queryList = new FakeMediaQueryList(query, matches);
      created.push(queryList);
      return queryList;
    },
  });
}

function removeMatchMedia(): void {
  delete (window as { matchMedia?: unknown }).matchMedia;
}

afterEach(() => {
  cleanup();
  removeMatchMedia();
});

describe("useNarrowLayout", () => {
  it("is wide when the runtime has no matchMedia at all", () => {
    // The premise: jsdom 26 ships no `matchMedia`, so this is the path every
    // existing suite takes.
    expect(typeof window.matchMedia).toBe("undefined");

    const { result } = renderHook(() => useNarrowLayout());

    expect(result.current).toBe(false);
  });

  it("is narrow when the breakpoint query matches", () => {
    stubMatchMedia(true);

    const { result } = renderHook(() => useNarrowLayout());

    expect(result.current).toBe(true);
    // The query is built from the constant, so the two cannot disagree.
    expect(created).toHaveLength(1);
    expect(created[0].media).toBe(NARROW_MEDIA_QUERY);
  });

  it("re-renders when the media query reports a change", () => {
    stubMatchMedia(false);
    const { result } = renderHook(() => useNarrowLayout());
    expect(result.current).toBe(false);

    act(() => {
      created[0].matches = true;
      created[0].dispatchChange();
    });

    expect(result.current).toBe(true);
  });

  it("removes its listener on unmount", () => {
    stubMatchMedia(true);
    const { unmount } = renderHook(() => useNarrowLayout());
    expect(created[0].listenerCount()).toBe(1);

    unmount();

    expect(created[0].listenerCount()).toBe(0);
  });

  it("creates one MediaQueryList per hook instance, never a module singleton", () => {
    stubMatchMedia(false);

    const first = renderHook(() => useNarrowLayout());
    const second = renderHook(() => useNarrowLayout());

    expect(created).toHaveLength(2);
    expect(created[0]).not.toBe(created[1]);
    // Both instances are subscribed to their own object, so one unmounting must
    // not silence the other.
    expect(created[0].listenerCount()).toBe(1);
    expect(created[1].listenerCount()).toBe(1);
    first.unmount();
    expect(created[0].listenerCount()).toBe(0);
    expect(created[1].listenerCount()).toBe(1);
    second.unmount();
  });

  it("the two declarations agree on the breakpoint", () => {
    // This is NOT "the query fires": jsdom has no layout, so no stylesheet is
    // ever evaluated. It proves only that the CSS media query which carries the
    // drawer and `NARROW_MAX_WIDTH_PX` name the same number - the one thing two
    // independent declarations can drift on.
    const scrimRule = indexCss.indexOf(".folder-drawer-scrim");
    expect(scrimRule, "the drawer scrim rule is in the stylesheet").toBeGreaterThan(-1);

    const queries = Array.from(indexCss.matchAll(/@media\s*\(max-width:\s*(\d+)px\)/g));
    const enclosing = queries.filter(match => (match.index ?? 0) < scrimRule).pop();
    expect(enclosing, "the scrim sits inside a max-width media query").toBeTruthy();

    const block = indexCss.slice(enclosing!.index, scrimRule);
    expect(block, "the same block makes the rail a fixed sheet").toContain(".library-rail");
    expect(block).toMatch(/position:\s*fixed/);

    expect(Number(enclosing![1]), "the two declarations agree").toBe(NARROW_MAX_WIDTH_PX);
  });
});
