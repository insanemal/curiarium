import { describe, expect, it } from "vitest";

import type { Role } from "../../lib/auth";
import { QUEUES, resolveQueue, visibleQueues } from "./queues";

describe("visibleQueues", () => {
  it.each<Role>(["contributor", "editor", "admin"])(
    "lists all four queues for a %s",
    role => {
      expect(visibleQueues(role).map(queue => queue.key)).toEqual([
        "bundles",
        "imports",
        "missing",
        "trash",
      ]);
    },
  );

  // `GET /imports/runs`, `GET /assets?missing=true+` and `GET /trash` are all
  // require_read, so a viewer keeps those three rows; only bundles needs
  // CONTRIBUTE, and only the queue's rescan/purge controls need a write.
  it("lists the three read surfaces for a viewer and withholds the bundle row", () => {
    expect(visibleQueues("viewer").map(queue => queue.key)).toEqual([
      "imports",
      "missing",
      "trash",
    ]);
  });

  it.each<[Role | null | undefined]>([[null], [undefined]])(
    "lists nothing while the session's role is unknown (%s)",
    role => {
      expect(visibleQueues(role)).toEqual([]);
    },
  );

  it("keeps the table in bundles, imports, missing, trash order", () => {
    // The order is load-bearing: `resolveQueue` falls back to the first visible
    // row, so the table decides which queue an absent or invisible key opens.
    expect(QUEUES.map(queue => queue.key)).toEqual([
      "bundles",
      "imports",
      "missing",
      "trash",
    ]);
  });

  it("sets the bundle threshold at contributor and the other three at viewer", () => {
    expect(QUEUES.map(queue => [queue.key, queue.minRole])).toEqual([
      ["bundles", "contributor"],
      ["imports", "viewer"],
      ["missing", "viewer"],
      ["trash", "viewer"],
    ]);
  });
});

describe("resolveQueue", () => {
  // The latent bug this guards: `activeQueue` defaults to "bundles" while the
  // role is still `null`, so a null role must resolve to no queue at all rather
  // than to a row the session cannot see.
  it.each([["bundles"], ["imports"], ["missing"], ["trash"], [null], [undefined]] as const)(
    "resolves a null role to no queue, whatever key was stored (%s)",
    key => {
      expect(resolveQueue(null, key)).toBeNull();
    },
  );

  it("falls back to the first visible row when the stored key is not visible", () => {
    // A viewer can hold "bundles" after a demotion; the fallback is the first
    // row a viewer actually reads.
    expect(resolveQueue("viewer", "bundles")).toBe("imports");
  });

  it("keeps a key the role may see", () => {
    expect(resolveQueue("viewer", "missing")).toBe("missing");
    expect(resolveQueue("admin", "trash")).toBe("trash");
    expect(resolveQueue("contributor", "bundles")).toBe("bundles");
  });

  it("treats an absent key as the first visible row", () => {
    expect(resolveQueue("contributor", null)).toBe("bundles");
    expect(resolveQueue("viewer", undefined)).toBe("imports");
  });
});
