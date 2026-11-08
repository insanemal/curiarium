import { describe, expect, it } from "vitest";
import { buildReloadUrl } from "./reload";

describe("buildReloadUrl", () => {
  it("adds reload=<now> to an empty search", () => {
    expect(buildReloadUrl("/", "", "", 7)).toBe("/?reload=7");
  });

  it("keeps an existing query", () => {
    expect(buildReloadUrl("/", "?tab=review", "", 7)).toBe("/?tab=review&reload=7");
  });

  it("keeps the fragment last", () => {
    expect(buildReloadUrl("/app", "", "#sso_code=x", 7)).toBe("/app?reload=7#sso_code=x");
  });

  it("is deterministic for the same now and differs for another", () => {
    expect(buildReloadUrl("/", "", "", 7)).toBe(buildReloadUrl("/", "", "", 7));
    expect(buildReloadUrl("/", "", "", 7)).not.toBe(buildReloadUrl("/", "", "", 8));
  });

  it("leaves a trailing slash on the pathname alone", () => {
    expect(buildReloadUrl("/library/", "", "", 7)).toBe("/library/?reload=7");
  });
});
