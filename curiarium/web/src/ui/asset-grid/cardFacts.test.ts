import { describe, expect, it } from "vitest";

import type { Asset } from "../../lib/api";
import { cardFacts } from "./cardFacts";

/**
 * Minimal asset fixture: only the fields the fact line is allowed to read have
 * meaningful values. Everything else is deliberately populated so the guard
 * test below can prove the function ignores it.
 */
function makeAsset(overrides: Partial<Asset> = {}): Asset {
  return {
    id: "a1",
    name: "Bracket",
    filename: "bracket.stl",
    mime: "model/stl",
    size: 1200,
    tags: [],
    url: "/file/a1/bracket.stl",
    thumb_url: "/thumb/a1.jpg",
    supporting_file_count: 0,
    ...overrides,
  };
}

describe("cardFacts", () => {
  it("shows only the formatted size when the asset has no supporting files", () => {
    expect(cardFacts(makeAsset({ size: 1200, supporting_file_count: 0 }))).toEqual(["1.2 KB"]);
  });

  it("appends a singular supporting-file count for exactly one file", () => {
    expect(cardFacts(makeAsset({ size: 4096, supporting_file_count: 1 }))).toEqual([
      "4.0 KB",
      "1 supporting file",
    ]);
  });

  it("pluralises the supporting-file count for more than one file", () => {
    expect(cardFacts(makeAsset({ size: 4096, supporting_file_count: 3 }))).toEqual([
      "4.0 KB",
      "3 supporting files",
    ]);
  });

  it("keeps the size first so the fact line reads size then attachments", () => {
    const [first, second] = cardFacts(makeAsset({ size: 4096, supporting_file_count: 2 }));
    expect(first).toBe("4.0 KB");
    expect(second).toBe("2 supporting files");
  });

  it("returns a fresh array so a caller mutating it cannot poison the next call", () => {
    const asset = makeAsset({ size: 1200 });

    const first = cardFacts(asset);
    first.push("caller mutation");

    expect(cardFacts(asset)).toEqual(["1.2 KB"]);
  });

  // `Asset` has no `created_at`/`added_at` column and no bbox/volume/dimensions
  // field (geometry exists only transiently inside Three.js), so an added-date
  // or dimensions segment would have to be invented. Inventing one is exactly
  // what this ticket refuses; this guard fails the moment someone tries.
  it("never invents an added-date, dimensions or volume segment", () => {
    const facts = cardFacts(
      makeAsset({
        size: 4096,
        supporting_file_count: 1,
        created_by: "ada",
        missing_since: "2026-10-27T11:31:34+00:00",
        prepared_print: {
          printer: "X1C",
          material: "PLA",
          nozzle_mm: 0.4,
          estimated_seconds: 7200,
          slicer: "OrcaSlicer",
          removable: true,
        },
      }),
    );

    for (const fact of facts) {
      expect(fact).not.toMatch(/added|created|dimension|mm³|bbox/i);
    }
  });

  // The function may read only `size` and `supporting_file_count`. Changing
  // every other field must not move the output, which is what keeps the fact
  // line from quietly becoming a second metadata surface.
  it("reads only size and supporting_file_count", () => {
    const original = cardFacts(makeAsset({ size: 1200, supporting_file_count: 0 }));
    const reskinned = cardFacts(
      makeAsset({
        id: "other",
        name: "A completely different name",
        filename: "other-model.3mf",
        mime: "model/3mf",
        size: 1200,
        supporting_file_count: 0,
        tags: ["pla", "strong", "gift"],
        creator: "Grace Hopper",
        created_by: "Ada",
        missing_since: "2026-10-27T11:31:34+00:00",
        prepared_print: {
          printer: "Prusa MK3S+",
          material: "PETG",
          nozzle_mm: 0.6,
          estimated_seconds: 900,
          slicer: "PrusaSlicer",
          removable: false,
        },
      }),
    );

    expect(reskinned).toEqual(original);
  });
});
