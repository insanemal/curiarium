import React from "react";

import type { BundleReport } from "../../lib/api/bundles";
import {
  refusedCount,
  refusedLine,
  ruleSetLine,
  ruleSetStamp,
  showsScanFidelity,
} from "./scanFidelity";

/**
 * The scan-fidelity notice: how many paths the walk refused to offer, and the
 * rule set that decided it.
 *
 * Module-level default export on purpose: a parent re-render must not remount
 * this component, and there is no state inside it to lose.
 */
export default function ScanFidelityNotice({ report }: { report: BundleReport }): React.ReactElement | null {
  // No element at all, not an empty shell: this sits in `ReviewQueueView`'s
  // notices, a flex column which leaves no track for an absent child, so there
  // is nothing to hold (the same reasoning as the duplicate panel's).
  //
  // Deliberately no truncation paragraph of its own: `truncated` makes
  // `showsDuplicateReport` true by construction, so the duplicate panel is always
  // present above this one whenever a walk was cut short, and it already says so.
  // A second copy would be one claim in two places, free to drift.
  if (!showsScanFidelity(report)) return null;

  const count = refusedCount(report);
  const stamp = ruleSetStamp(report);
  const line = ruleSetLine(report.rule_set);

  // No control of any kind: the notice is a reading aid, and nothing it says
  // acts on a path. The full 64-character stamp must stay reachable by keyboard,
  // touch and screen reader, which is why it rides in a `title` and an
  // `sr-only` span instead of being truncated out of the visible line.
  return (
    <div className="review-panel" data-scan-fidelity="">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium">{refusedLine(count)}</span>
        <span className="text-[11px] opacity-70" title={stamp ?? undefined}>
          {line}
          {stamp !== null && <span className="sr-only">{` Full stamp ${stamp}`}</span>}
        </span>
      </div>
    </div>
  );
}
