import { useCallback, useRef, useSyncExternalStore } from "react";

/**
 * The one narrow breakpoint, shared by the hook and the stylesheet. `index.css`
 * repeats the number in its `@media (max-width: 1180px)` block because CSS
 * cannot read a TS constant and a media query cannot be gated on JS state; the
 * two declarations are kept equal by a test, never by construction.
 */
export const NARROW_MAX_WIDTH_PX = 1180;

/**
 * The query itself, built from the constant so the two can never be spelled
 * differently. It is the same threshold the stylesheet already used for
 * `.review-layout`, so the app has exactly one breakpoint.
 */
export const NARROW_MEDIA_QUERY = `(max-width: ${NARROW_MAX_WIDTH_PX}px)`;

/** The slice of `MediaQueryList` this hook needs, plus its pre-2019 aliases. */
type MediaQueryListLike = {
  matches: boolean;
  addEventListener?: (type: "change", listener: () => void) => void;
  removeEventListener?: (type: "change", listener: () => void) => void;
  addListener?: (listener: () => void) => void;
  removeListener?: (listener: () => void) => void;
};

function createMediaQueryList(): MediaQueryListLike | null {
  if (typeof window === "undefined") return null;
  // jsdom 26 ships no `matchMedia` at all, so this is the path every existing
  // suite takes: no matchMedia means "wide", never a throw.
  if (typeof window.matchMedia !== "function") return null;
  return window.matchMedia(NARROW_MEDIA_QUERY) as unknown as MediaQueryListLike;
}

/**
 * Whether the viewport is at or below {@link NARROW_MAX_WIDTH_PX}.
 *
 * JS narrowness cannot be replaced by CSS alone: `role="dialog"`,
 * `aria-modal` and whether the off-screen rail stays focusable cannot be gated
 * by a media query, so `App` decides once here and passes the boolean down.
 *
 * The `MediaQueryList` is created per hook instance (a ref), never as a module
 * singleton: two mounted apps, or two test cases in one file, must not share a
 * listener or a stale `matches`. Legacy browsers expose `addListener` instead
 * of `addEventListener`; both are supported.
 */
export function useNarrowLayout(): boolean {
  const queryRef = useRef<MediaQueryListLike | null | undefined>(undefined);
  if (queryRef.current === undefined) {
    queryRef.current = createMediaQueryList();
  }
  const subscribe = useCallback((onStoreChange: () => void) => {
    const query = queryRef.current ?? null;
    if (!query) return () => {};
    if (typeof query.addEventListener === "function") {
      query.addEventListener("change", onStoreChange);
      return () => query.removeEventListener?.("change", onStoreChange);
    }
    if (typeof query.addListener === "function") {
      query.addListener(onStoreChange);
      return () => query.removeListener?.(onStoreChange);
    }
    return () => {};
  }, []);
  const getSnapshot = useCallback(() => queryRef.current?.matches ?? false, []);
  // The server snapshot is always wide: there is no viewport to measure.
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}
