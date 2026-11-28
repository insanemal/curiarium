import React from "react";

import type { OperationPlan, OperationReview as Review } from "../../lib/api/operations";
import {
  excludedLine,
  extractCoverLine,
  extractExistingLine,
  extractGroupLine,
  operationActionLabel,
  operationMoveLine,
  planSteps,
  projectLine,
  reviewFacts,
  workflowLabel,
} from "./operationView";

/**
 * The ONE review surface every workflow shares: what the plan would do, stated
 * once in one shape.
 *
 * Every sentence comes from `operationView`, so a door cannot word a move, a
 * count or a group differently from this surface. The two headings and the
 * empty-exclusion line are the only words this file owns. `dataPrefix` is the
 * door's attribute namespace ("split" renders `data-split-*`), and
 * `actionLabel` is the one action: `null` means the plan has nothing to approve
 * (a zero-step plan, or a plan with nothing to do), so no button is rendered at
 * all rather than a dead one.
 *
 * `children` is the door's own controls (the archive entry picker), rendered
 * after the facts and before the action, inside the same group.
 */

type Props = {
  plan: OperationPlan;
  /** The one action's outcome label; `null` means there is NO action (a zero-step
   *  plan, or a plan with nothing to do) and no button is rendered. */
  actionLabel: string | null;
  busy: boolean;
  onConfirm: () => void;
  /** The door's attribute namespace: "split" yields data-split-* etc. */
  dataPrefix: string;
  /** Extra controls the door owns (the archive entry picker). Rendered after the
   *  facts and before the action, inside the group. */
  children?: React.ReactNode;
};

/** The census is printed in the order the scan walks the roles, so two workflows
 *  never disagree about where a count sits. A role outside this list (a newer
 *  server's) has no line here - the client writes no words for it. */
const ROLE_ORDER = ["model", "image", "text", "archive", "other"];

/**
 * The machine word for each count `reviewFacts` prints, in the same order and
 * under the same conditions. `reviewFacts` owns WHICH lines exist; this owns
 * what each one is CALLED, so the two condition lists are deliberately adjacent:
 * a fact added to one without the other would mislabel every line after it.
 */
function factKinds(review: Review): string[] {
  const kinds: string[] = [];
  if (review.members > 0) kinds.push("files");
  if (review.groups.length > 0) kinds.push("groups");
  if (review.moves > 0) kinds.push("moves");
  if (review.projects.length > 0) kinds.push("projects");
  if (review.adopts > 0) kinds.push("adopts");
  if (review.deletes > 0) kinds.push("deletions");
  if ((review.existing_project_name ?? "").trim()) kinds.push("existing");
  return kinds;
}

/** One role's line: the count and the noun a human would use. The text role is
 *  a DOCUMENT to a reader, and every other role takes a plain plural. */
function roleLine(role: string, count: number): string {
  const noun = role === "text" ? "document" : role;
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

export default function OperationReview(props: Props): React.ReactElement {
  const { plan, actionLabel, busy, onConfirm, dataPrefix, children } = props;
  const review = plan.review;

  // The door's attribute namespace, built once: every tag below is mechanical.
  const flag = (name: string): Record<string, string> => ({ [`data-${dataPrefix}-${name}`]: "true" });
  const flagged = (name: string, value: string): Record<string, string> => ({
    [`data-${dataPrefix}-${name}`]: value,
  });

  const kinds = factKinds(review);
  const facts = reviewFacts(review).map((text, index) => ({
    kind: kinds[index] ?? "count",
    text,
  }));
  const byRole = review.by_role ?? {};
  const roles = ROLE_ORDER.filter(role => (byRole[role] ?? 0) > 0);
  const coverLine = extractCoverLine(review.cover);
  const existingName = (review.existing_project_name ?? "").trim();

  return (
    <div
      className="review-detail-facts"
      role="group"
      aria-label={`${workflowLabel(plan.workflow)} review`}
      {...flag("plan")}
    >
      <h3 className="text-sm font-semibold">What will happen</h3>
      <ul className="flex flex-col gap-1">
        {planSteps(plan).map((summary, index) => (
          <li key={index} {...flag("step-summary")}>
            {summary}
          </li>
        ))}
      </ul>

      {/* A library scan measures no counts at plan time, so an empty fact list
          is honest and the container disappears with it. */}
      {facts.length > 0 && (
        <div className="flex flex-wrap gap-2" {...flag("facts")}>
          {facts.map(fact => (
            <span key={fact.kind} {...flagged("fact", fact.kind)} {...flag("fact-text")}>
              {fact.text}
            </span>
          ))}
        </div>
      )}

      {review.move_list.length > 0 && (
        <ul className="flex flex-col gap-1">
          {review.move_list.map((move, index) => (
            <li
              key={`${index}-${move.source}-${move.target}`}
              {...flag("move")}
              {...flagged("action", move.action)}
            >
              <span className="font-mono">{operationMoveLine(move)}</span>{" "}
              <span className="text-muted">{operationActionLabel(move.action)}</span>
            </li>
          ))}
        </ul>
      )}

      {review.projects.length > 0 && (
        <ul className="flex flex-col gap-1">
          {review.projects.map(project => (
            <li key={project.root} {...flag("project")} {...flagged("root", project.root)}>
              {projectLine(project)}
            </li>
          ))}
        </ul>
      )}

      {review.groups.length > 0 && (
        <ul className="flex flex-col gap-1">
          {review.groups.map(group => (
            <li key={group.name} {...flag("group")} {...flagged("group-name", group.name)}>
              {extractGroupLine(group)}
            </li>
          ))}
        </ul>
      )}

      {/* The census, zeros skipped: they exist so the server's sum is checkable,
          not so every page prints five. A review from a server that predates the
          census renders none. */}
      {roles.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {roles.map(role => {
            const count = byRole[role] ?? 0;
            return (
              <span
                key={role}
                {...flagged("role", role)}
                {...flagged("count", String(count))}
              >
                {roleLine(role, count)}
              </span>
            );
          })}
        </div>
      )}

      {coverLine !== null && (
        <span {...flag("cover")}>
          {coverLine}
        </span>
      )}
      {existingName !== "" && (
        <span {...flag("existing")}>
          {extractExistingLine(review)}
        </span>
      )}

      {children}

      <h3 className="text-sm font-semibold">What is excluded</h3>
      {/* An empty list is stated, never omitted: a missing list and an empty one
          must not read the same. */}
      {review.excluded.length === 0 ? (
        <span {...flag("nothing-excluded")}>
          Nothing is excluded
        </span>
      ) : (
        review.excluded.map(entry => (
          <span key={entry.path} className="review-detail-reason" {...flag("excluded")}>
            {excludedLine(entry)}
          </span>
        ))
      )}

      {actionLabel !== null && (
        <div className="review-detail-actions">
          <button
            type="button"
            className="text-xs px-2.5 py-1.5 rounded-md bg-accent disabled:opacity-60"
            {...flag("action-button")}
            disabled={busy}
            onClick={onConfirm}
          >
            {actionLabel}
          </button>
        </div>
      )}
    </div>
  );
}
