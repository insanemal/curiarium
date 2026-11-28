import { describe, expect, it } from "vitest";

import type { Role } from "../../lib/auth";
import { QUEUES, resolveQueue, visibleQueues } from "./queues";

describe("visibleQueues", () => {
  it.each<Role>(["contributor", "editor", "admin"])(
    "lists all five queues for a %s",
    role => {
      expect(visibleQueues(role).map(queue => queue.key)).toEqual([
        "bundles",
        "imports",
        "missing",
        "trash",
        "operations",
      ]);
    },
  );

  // `GET /operations?workflow=mount-import`, `GET /assets?missing=true+`,
  // `GET /trash` and `GET /operations` are all require_read, so a viewer keeps
  // those four rows; only bundles needs CONTRIBUTE, and only the queues' own
  // controls (rescan/purge/cancel/undo) need a write.
  it("lists the four read surfaces for a viewer and withholds the bundle row", () => {
    expect(visibleQueues("viewer").map(queue => queue.key)).toEqual([
      "imports",
      "missing",
      "trash",
      "operations",
    ]);
  });

  it.each<[Role | null | undefined]>([[null], [undefined]])(
    "lists nothing while the session's role is unknown (%s)",
    role => {
      expect(visibleQueues(role)).toEqual([]);
    },
  );

  it("keeps the table in bundles, imports, missing, trash, operations order", () => {
    // The order is load-bearing: `resolveQueue` falls back to the first visible
    // row, so the table decides which queue an absent or invisible key opens.
    expect(QUEUES.map(queue => queue.key)).toEqual([
      "bundles",
      "imports",
      "missing",
      "trash",
      "operations",
    ]);
  });

  it("sets the bundle threshold at contributor and the other four at viewer", () => {
    expect(QUEUES.map(queue => [queue.key, queue.minRole])).toEqual([
      ["bundles", "contributor"],
      ["imports", "viewer"],
      ["missing", "viewer"],
      ["trash", "viewer"],
      ["operations", "viewer"],
    ]);
  });

  // `GET /operations` is require_read, so the operations history is a viewer
  // surface: withholding the row from a viewer would delete a proven read.
  it("offers the operations row to a viewer", () => {
    expect(visibleQueues("viewer").map(queue => queue.key)).toContain("operations");
  });
});

describe("resolveQueue", () => {
  // The latent bug this guards: `activeQueue` defaults to "bundles" while the
  // role is still `null`, so a null role must resolve to no queue at all rather
  // than to a row the session cannot see.
  it.each([["bundles"], ["imports"], ["missing"], ["trash"], ["operations"], [null], [undefined]] as const)(
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
    expect(resolveQueue("viewer", "operations")).toBe("operations");
  });

  it("treats an absent key as the first visible row", () => {
    expect(resolveQueue("contributor", null)).toBe("bundles");
    expect(resolveQueue("viewer", undefined)).toBe("imports");
  });

  // The new key was appended LAST on purpose. Had it been inserted before
  // "bundles", the fallback would have moved: an absent stored key would open
  // operations for every role instead of the bundle queue.
  it("keeps bundles as the default for a contributor", () => {
    expect(resolveQueue("contributor", null)).toBe("bundles");
  });
});
