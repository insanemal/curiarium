import { describe, expect, it } from "vitest";

import { errorMessageFromBody } from "./http";

describe("errorMessageFromBody", () => {
  it("reads a dict detail's `reason` as the sentence to show", () => {
    // The superseded-undo refusal is a dict detail; showing the operator raw
    // JSON would hide the one sentence the server wrote.
    const body = JSON.stringify({
      detail: {
        reason: "a later operation (op-7) changed the same paths after this one",
        superseded_by: "op-7",
        undo_state: "superseded",
      },
    });
    expect(errorMessageFromBody(body, "Failed to undo the operation")).toBe(
      "a later operation (op-7) changed the same paths after this one",
    );
  });

  it("leaves a dict detail with no `reason` as the raw body", () => {
    const body = JSON.stringify({ detail: { superseded_by: "op-7", undo_state: "superseded" } });
    expect(errorMessageFromBody(body, "fallback")).toBe(body);
  });

  it("reads a string detail, trimmed", () => {
    expect(errorMessageFromBody(JSON.stringify({ detail: "  refused  " }), "fallback")).toBe(
      "refused",
    );
  });

  it("returns the fallback for an empty body", () => {
    expect(errorMessageFromBody("   ", "the fallback")).toBe("the fallback");
  });
});
