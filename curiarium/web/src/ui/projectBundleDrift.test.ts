import { describe, expect, it } from "vitest";

import {
  DRIFT_KNOWN,
  DRIFT_NOT_APPLICABLE,
  DRIFT_UNKNOWN,
  driftNoticeLine,
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
