import { describe, expect, it } from "vitest";

import {
  DRIFT_KNOWN,
  DRIFT_NOT_APPLICABLE,
  DRIFT_UNKNOWN,
  driftNoticeLine,
  driftWantsArrange,
  projectArrangeOffer,
} from "./projectBundleDrift";

const DASH = "\u2013";
const TAIL = "parts in this folder are not in the project";

describe("driftNoticeLine", () => {
  it("says nothing for a project with no marker, at any count", () => {
    // `not_applicable` is "not applicable", not "unknown": a hand-made project
    // has no folder identity, so there is no question to answer.
    for (const count of [null, 0, 1, 3]) {
      expect(driftNoticeLine(DRIFT_NOT_APPLICABLE, count)).toBeNull();
    }
  });

  it("says nothing for a known zero, so a fresh project shows no zero", () => {
    expect(driftNoticeLine(DRIFT_KNOWN, 0)).toBeNull();
  });

  it("says nothing for a known state whose count never arrived", () => {
    expect(driftNoticeLine(DRIFT_KNOWN, null)).toBeNull();
  });

  it("uses the singular for one new part", () => {
    expect(driftNoticeLine(DRIFT_KNOWN, 1)).toBe("1 part in this folder is not in the project");
  });

  it("uses the plural for several new parts", () => {
    expect(driftNoticeLine(DRIFT_KNOWN, 3)).toBe(`3 ${TAIL}`);
  });

  it("renders an en dash for unknown, never a zero and never a hyphen", () => {
    const line = driftNoticeLine(DRIFT_UNKNOWN, null);
    expect(line).toBe(`${DASH} ${TAIL}`);
    // The character itself: U+2013, not "-" and not "0".
    expect(line).toContain("\u2013");
    expect(line).not.toContain("- parts");
    expect(line).not.toContain("0");
  });

  it("treats an unrecognised state as unknown", () => {
    expect(driftNoticeLine("grew", 4)).toBe(`${DASH} ${TAIL}`);
  });

  it("answers every one of the three states, and only known can name a number", () => {
    // Exhaustive over the contract: each state has a defined answer, the two
    // silent states are silent for different reasons, and a number is spoken
    // only by the state that has one.
    const answers = [DRIFT_NOT_APPLICABLE, DRIFT_KNOWN, DRIFT_UNKNOWN].map(state => ({
      state,
      line: driftNoticeLine(state, state === DRIFT_KNOWN ? 3 : null),
    }));
    for (const { line } of answers) expect(line === null || typeof line === "string").toBe(true);
    expect(answers[0].line).toBeNull();
    expect(answers[1].line).toBe(`3 ${TAIL}`);
    expect(answers[2].line).toBe(`${DASH} ${TAIL}`);
    expect(driftNoticeLine(DRIFT_KNOWN, 0)).toBeNull();
    expect(driftNoticeLine(DRIFT_NOT_APPLICABLE, 0)).toBeNull();
  });
});

describe("projectArrangeOffer", () => {
  it("offers the detection marker's key and shape", () => {
    // A detection marker (no `source`) records the row's storage-relative path as
    // `key`, which is exactly what `GET /bundles/arrange` measures a plan against.
    expect(projectArrangeOffer({ key: "Hydra", shape: "split-top-folders" })).toEqual({
      bundleKey: "Hydra",
      shape: "split-top-folders",
    });
  });

  it("prefers rel_dir over the key for a zip-project marker", () => {
    // A zip-project's `key` is an identity, so its path is `rel_dir`; a client
    // that offered the key would ask the server about `zip-project:<url>`.
    expect(
      projectArrangeOffer({
        key: "zip-project:https://example.test/thing/1",
        rel_dir: "import/Hydra",
        shape: "generic",
      }),
    ).toEqual({ bundleKey: "import/Hydra", shape: "generic" });
  });

  it("offers nothing for a zip-project identity with no rel_dir", () => {
    // The identity is not a path; `GET /bundles/arrange` would answer `unknown
    // bundle`, so there is no door to offer.
    expect(
      projectArrangeOffer({ key: "zip-project:https://example.test/thing/1", shape: "generic" }),
    ).toBeNull();
  });

  it("offers nothing without a non-empty shape", () => {
    // The shape chooses which moves the plan would make, so a marker with no
    // shape has no plan to ask for - and an empty string is not a shape.
    expect(projectArrangeOffer({ key: "Hydra" })).toBeNull();
    expect(projectArrangeOffer({ key: "Hydra", shape: "" })).toBeNull();
    expect(projectArrangeOffer({ key: "Hydra", shape: 42 })).toBeNull();
  });

  it("offers nothing for a non-object marker", () => {
    // Catches a read of `.shape` off null/undefined/string: the marker is
    // arbitrary metadata, not a typed payload.
    for (const marker of [null, undefined, "Hydra", 7]) {
      expect(projectArrangeOffer(marker)).toBeNull();
    }
  });

  it("offers nothing when neither rel_dir nor key names a path", () => {
    expect(projectArrangeOffer({ shape: "generic" })).toBeNull();
    expect(projectArrangeOffer({ key: "", shape: "generic" })).toBeNull();
    expect(projectArrangeOffer({ rel_dir: "", key: "Hydra", shape: "generic" })).toEqual({
      bundleKey: "Hydra",
      shape: "generic",
    });
  });
});

describe("driftWantsArrange", () => {
  it("offers the door only for a known count above zero", () => {
    // Catches a door offered on a fresh project (known/0) or on an unmeasured
    // one: the notice itself is silent there, so a door beneath it would
    // contradict the sentence above it.
    expect(driftWantsArrange(DRIFT_KNOWN, 2)).toBe(true);
    expect(driftWantsArrange(DRIFT_KNOWN, 0)).toBe(false);
    expect(driftWantsArrange(DRIFT_KNOWN, null)).toBe(false);
    expect(driftWantsArrange(DRIFT_UNKNOWN, null)).toBe(false);
    expect(driftWantsArrange(DRIFT_NOT_APPLICABLE, 3)).toBe(false);
  });
});
