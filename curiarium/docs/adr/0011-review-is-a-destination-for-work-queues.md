# ADR 0011: Work queues live in REVIEW, not in Settings

**Status:** Accepted

**Accepted:** 2026-11-01

**Implementation:** ticket `Mf-f3c2` (slice 1 of the information-architecture
change `Mf-q7vs`: the bundle queue) and ticket `Mf-xe4l` (slice 2: imports
running). Slices 3-4 (missing files, trash) adopt this shell without
re-deciding it.

**Extended:** ADR 0010, whose report is the first tenant of this surface.

## Context

Settings accumulated work that is done, not configured. By the time of this
decision its Imports section carried the vendor-bundle report (`GET
/bundles/detect`: a whole-library walk, a filterable list, and the create and
dismiss actions that follow it); its Storage section carried the storage rescan
and the trash queue; its Imports section also carried import runs and the local
folder scan. The owner's complaint was not about any one of them: *"we need to
move that bundle scan results somewhere else. The whole UI flow sucks. Not just
for this, but for everything."*

Three facts about the existing shell shaped the answer.

* **Settings has no room for work.** It is a root menu of configuration sections,
  each one a stack of cards in the main pane. A queue wants the whole pane: a
  dense list, a selected item's details beside it, and bulk actions that apply to
  a selection. The bundle card rendered one full-width bordered card per bundle -
  `Mf-f3c2` measured the old card at 12.4 DOM nodes and 2.5 tab stops per row -
  so a 40-bundle library was 40 cards and 100 tab stops of scrolling before the
  first decision could be made.
* **The main pane had nowhere to put work.** `App.tsx` switched
  `library | settings | audit` with local state. Library is a browsing
  destination and Audit is admin-only, so work that is neither had to live inside
  Settings by default rather than by choice.
* **A badge needs a coordinator, not a poll.** Every queue the design adds wants
  a count in the sidebar. One fetch per badge, on its own timer, would put a
  whole-storage walk on a schedule.

The approved design (`.plans/Mf-q7vs-ia.md`, designer pass 2026-11-01) fixed the
navigation model: keep the Files/Projects mode switch for browsing, and add a
shared REVIEW group beneath the active mode's contents for work. This ADR records
the parts of that design a future reader would otherwise have to re-derive: the
destination model, the count rule, the role threshold and the fetch policy.

## Decision

1. **`review` is a fourth destination, not a mode.** `App.tsx`'s local view
   state becomes `ActiveView = "library" | "settings" | "audit" | "review"` plus a
   `ReviewQueueKey` naming which queue is open (`ui/views.ts`). Destinations stay
   mutually exclusive and remain local state: there is no router and no URL or
   history model. Making a queue addressable is its own follow-up, and doing it
   inside this slice would have meant inventing a routing model for four surfaces
   at once.

2. **REVIEW is one group, rendered by both sidebars.** `ui/review/ReviewNav.tsx`
   is rendered by `Sidebar.tsx` and `ProjectSidebar.tsx` with identical props,
   immediately above the shared footer, so the group is present in Files and
   Projects mode and the two sidebars cannot drift apart. Switching mode does
   **not** close an open queue (the group is shared, so the operator's place
   survives); library navigation - selecting a folder or the unfiled pile,
   opening a project, Settings' "Scan now" - still returns to the library,
   because those are browsing actions.

3. **A queue is a header, a queue-specific toolbar, and list + detail.** The
   shared part is `ui/review/ReviewQueueView.tsx`: title, count, one-line
   explanation, last-refreshed time and Refresh, plus a notices slot. Filters,
   selection, bulk actions and the rows belong to each queue. A selected item is
   shown in a **detail panel** beside the list, never in a modal: the item's
   metadata, its editable project name and its row actions all live there, so
   reading one item never covers the queue.

4. **One count coordinator.** `ui/review/useReviewCounts.ts` derives every review
   badge from state the queue already holds - for bundles, the report from
   `useBundleDetect` - and returns a `ReviewCounts` shape (`{ bundles: number |
   null; imports: number | null }`) that each slice extends with its own source.
   It is a closed type on purpose: a new queue has to be added in one place.
   There is no second fetch for a badge and no per-badge timer. The rule that
   matters is that a count is `null` - rendered as an en dash - when it has not
   been fetched yet, when the first fetch failed, and when the report is
   truncated. A failed *refresh* keeps the last known number: the report behind
   it is still the last honest one, and the header's "Last refreshed" time is
   what says how old it is, so dashing it would throw away information the
   operator still has. A zero must always mean a genuinely empty queue: this
   repo has twice shipped a label that lied about an empty list (the `ROOT`
   badge and the Unfiled empty state that claimed "Every asset is in a folder"
   under a filter), and a badge is the same class of label.

   The imports count has to decide an unknown of its own. `run === null` cannot
   distinguish "the first fetch failed" from "no run has ever happened", so
   `useImportRuns` reports a `loaded` flag - at least one **successful** fetch -
   and the badge is `null` until then, `1` while the newest run is `running`,
   and `0` once a fetch has succeeded and nothing is running. That is the one
   place this change defines a new meaning for "unknown"; everywhere else the
   existing `–`-until-known rule applies unchanged.

5. **The bundle badge counts decisions, not detections.** Undismissed rows that
   are not already a project, **including blocked rows** - a blocked bundle still
   needs someone to decide to fix it, extract it from its zip or dismiss it. It is
   computed by filtering the delivered rows, never by arithmetic on
   `report.counts`: `counts.total` includes dismissed rows while
   `counts.already_created` counts over all of them, so `total - dismissed -
   already_created` double-subtracts a row that is both. A truncated report
   (`walk_truncated` or a withheld row) yields `null`, because a row count over a
   partial walk is a plausible-looking undercount.

6. **A report is fetched on entry, and only on entry.** `useBundleDetect` is
   armed by `canContribute && activeView === "review" && activeQueue ===
   "bundles"`: one walk per visit, one refetch after each create or dismiss, and
   the queue's Refresh for anything else. It never polls and never fires on app
   load. The walk reads the storage tree and up to three small documents per
   candidate, so painting a badge at every load would tax every contributor for a
   number most sessions never read; `–` until the queue is opened is the honest
   and cheaper state, and the header's "Last refreshed" label makes staleness
   visible instead of silent. Two mechanisms keep "one walk" true: an activation
   guard, so React.StrictMode's dev double-invoke cannot issue a second walk of
   the same report, and a request sequence in which the newest request wins, so a
   slow walk started before a mutation can never paint its pre-mutation copy over
   the mutation's refetch, and a request still in flight when the queue closes is
   discarded. The report is requested with `includeDismissed` and `limit: 1000`
   (the API's maximum) in a single call, because the Show-dismissed toggle and the
   badge must come from the same walk.

7. **A row's role threshold is its route's threshold, per row.** The bundle card
   sat inside Settings behind an editor-only gate, which was stricter than the
   API: `GET /bundles/detect` is `require_read`, and `POST /bundles/create` and
   `/bundles/dismiss` are `require_contribute`. A queue that exists in order to
   act takes the same threshold, so a contributor can use the surface the API
   always granted them, and a viewer gets no bundle row and no detection request
   at all - the settings card's deliberate "detection must never be fired for a
   viewer" invariant is preserved.

   The threshold is per row (`minRole` on `QUEUES` in `ui/review/ReviewNav.tsx`),
   not one gate on the group, because the queues' routes differ. `GET
   /imports/runs` is `require_read`, and a viewer already reads the run history
   from Settings today; a group-wide contributor gate would have deleted a
   proven read surface in order to tidy a menu. So "Imports running" is
   `minRole: "viewer"` and "Bundles to confirm" stays `"contributor"`, and a
   role below every row (or an unknown `null` role) renders no group and no
   "Review" heading at all. Moving the UI broadens no write permission: every
   control is additionally gated on the caller's capability, the folder-scan
   panel is hidden, not disabled, below `editor`, and the server remains the
   enforcement point.

8. **Bundles are not assets.** The queue keeps its own report, filters and domain
   actions and does not route through `AssetGrid`. Later slices that show assets
   (missing files) reuse `AssetGrid` with its virtualisation, selection and
   keyboard behaviour intact; nothing in this slice touches it.

9. **Settings loses the surface in the same commit that adds it.** The bundle card
   and the fetch/mutation wiring of its Settings-mounted hook are deleted with the
   new queue in one change, so there is no window in which two survivors poll the
   same report or a dead card lingers behind a menu. The imports slice follows the
   same rule: the run card, the folder-scan card, `useImportRuns` and
   `useFolderScan` leave `ui/settings/` in the commit that adds the queue, and
   Settings no longer arms either hook (so opening Settings issues no
   `GET /imports/runs` at all). The API functions stay: they are not Settings
   code. What remains under the relabelled **Import sources** section is
   configuration only - the mount-import settings, the remember-cookies
   preference and the two import-cookie workarounds.

10. **The imports queue polls, and only while a run is walking.** `useImportRuns`
    fetches once per activation of the imports queue - never on app load, and
    never for a queue that is not open - and from then on a 1.5 s timer refetches
    only while the newest run's `status` is `running`; when the run flips to a
    finished status the timer arms nothing. The timer effect depends on both the
    run object and a `pollTick` counter that advances after every settled
    non-authorised refresh, success or failure, because a failed poll changes no
    run identity and would otherwise end the chain with the badge stuck at 1.
    Two refs carry two jobs: a `walked` guard gives one entry fetch per
    activation (React.StrictMode's dev double-invoke cannot double-fetch it) and
    is reset on leaving the queue so a re-entry refetches; an `armed` ref, set
    when the hook first activates, gates only **background** refresh, so
    `refresh` early-returns on `!armed` but never on `!enabled`. A request
    sequence makes the newest response win. There is no backoff to tune: the
    mount import is a local read, and a `401` stops the chain and signs the
    operator out instead of retrying.

11. **Imports states the idle fact and keeps the last run.** When nothing is
    running the queue renders the literal line "No import currently running"
    **above** the last finished run - status label, timing, counters, rejected
    note, reason and per-file errors unchanged - because losing the last run is
    the regression this slice exists to avoid. A library whose run history is
    empty renders the same idle line plus "No mount import has run yet." and
    claims no status label; a failed first fetch renders the error with a
    "Try again" that refreshes; a failed *refresh* over a still-rendered run
    keeps the run and adds one notice, because a transient failure must not erase
    history the operator can still read. The queue's count and the sidebar badge
    both come from `describeQueueCount`, so one number cannot be worded two ways.

## Consequences

- Settings is configuration only. Its imports section is relabelled **Import
  sources** and holds what configuration actually is: mount-import settings, the
  remember-cookies preference and the two import-cookie workarounds. Run history
  and the local folder scan are work and live in REVIEW → Imports running.
- A viewer now has a REVIEW group with exactly one row. `GET /imports/runs` is
  `require_read`, so the run history it could already read from Settings moved
  with the work instead of disappearing; a contributor sees the same run surface,
  counters, errors and Refresh, and loses only the folder panel, which creates
  folders (`write`) and uploads (`contribute`).
- Two limitations are accepted and recorded rather than smuggled. While a run is
  walking and the operator navigates away from the queue, the poll continues
  until that run stops - a read the shell would have to re-issue anyway, and the
  alternative leaves the sidebar badge on the value it had when the queue
  closed. And a persistent non-authorised poll failure retries every 1.5 s with
  no backoff for as long as a run is running, because a slow local API and a
  broken one look the same at that size; a `401` is the exception and stops the
  chain.
- Every later queue follows the same shape, so a new queue costs a view and a nav
  row, not a new navigation model.
- Blocked rows are now selectable for dismissal. The old card disabled their
  checkboxes, so a blocked bundle could only be dismissed after a fix - while the
  badge counted it as needing a decision. Rows that can never be created from
  (blocked, or already a project) are excluded from the create payload, since the
  server can only answer `skipped` for them.
- The old card's aggregate line ("N bundles found - X high confidence, Y low")
  survives as the line under the explanation, so the totals the card reported are
  still stated; the header's large count is the number that still needs a
  decision, which is the same number as the sidebar badge. The one-line tally is
  no longer the headline, because it described the walk rather than the work.
- The list has **one** tab stop rather than one per row. Rows carry no focusable
  child: the whole listbox uses a roving tabindex keyed by bundle key (never by
  index, so a refetch cannot move the operator to a different bundle), arrows and
  Home/End move the detail panel, and Space toggles bulk selection. When the
  active row vanishes under a refetch (a dismissal, or a refresh that found the
  directory gone), the detail panel moves to the row that took its place, and if
  the list still held focus that row is focused too - a mouse click on a write
  button moves focus to that button first, so the operator's focus is never
  stolen.
- A queue's filter, selection and typed names live in the queue view, so leaving
  REVIEW discards them. That is the same contract the Settings card had, and it
  keeps the App state free of per-queue drafts.
- The row budget is 8 DOM nodes per bundle against the old card's 12.4, and the
  1000-row ceiling is a rendered-list ceiling, not a virtualised one. If a real
  library crosses it the queue needs `useVirtualRows`, which this slice does not
  take on.

## Alternatives considered

- **Keep the card in Settings and improve it.** Cheapest, and it is what shipped
  twice already. Rejected because the complaint is structural: the next three
  queues (imports running, missing files, trash) would each need the same
  full-width treatment, and Settings' root-menu/section/Back pattern exists so a
  preference can be found, not so work can be done.
- **Repurpose the Files/Projects mode switch as a work switcher.** Rejected by the
  design: the mode switch selects which browsing surface is mounted and each pane
  keeps its own state, so using it for queues would throw away the library's
  place.
- **A whole-queue modal.** Rejected: the design reserves modals for short blocking
  tasks (a destructive confirmation, a project picker, an asset preview) and a
  list of a hundred decisions is not short.
- **A router or URL model now.** Deferred deliberately. Two coexisting
  destination models in one commit is how a half-migrated shell happens.
- **A count fetch on app load to populate the badge eagerly.** Rejected: a full
  storage walk on every load, for a number that is stale the moment the disk
  changes, and a stale positive is worse than a dash.
- **Counting from `report.counts` arithmetic.** Rejected: the counts describe
  overlapping sets (see decision 5), so the arithmetic is only correct when one of
  the two sets is empty.
- **Virtualising the list in this slice.** Deferred: bundles are the one queue
  whose rows are not assets and whose ceiling is bounded by the report limit.
  Missing files, which are unbounded, reuse `AssetGrid` and its virtualiser.

## Rollback

Revert the commit. There is no migration, no schema change and no stored state to
unwind: dismissals remain the single `bundle.dismissals` appconfig value, the API
is unchanged, and the bundle report is still recomputed from disk on every read.
The only operator-visible loss on a revert is the queue itself.

## Verification

- `ui/App.review.test.tsx` proves the destination and the fetch policy through the
  real `ReviewNav` and the real `BundleReviewView`: no `GET /bundles/detect` before
  the queue is opened, exactly one on entry with `{includeDismissed: true, limit:
  1000}`, one refetch after a create and one after a dismiss with the badge and the
  view both updating from that single new report, no timer firing while the
  operator sits in the queue, one refetch on re-entry, exactly one walk under
  `React.StrictMode`, no REVIEW group and no walk at all for a viewer, the queue
  surviving a Files/Projects switch, and Back to library returning to the library.
  `ui/review/useBundleDetect.test.tsx` pins the same rules at the hook: one walk
  per activation, a superseded response never overwriting a newer one, no polling,
  and one refetch per mutation.
- `ui/review/BundleReviewView.test.tsx` covers the six states the design names -
  loading, retryable error, genuinely empty, empty-after-filter, stale dismissals,
  truncated - plus the all-dismissed variant that must never read as "detected
  nothing", every capability the old card had (filters, Show dismissed, typed
  name, create selected, create low as name-only, dismiss and undo, archive
  extraction, already-created state), read-only behaviour below contributor, one
  tab stop in the listbox, arrows/Home/End moving the detail panel, Space toggling
  selection without moving the panel, the archive-extraction action refreshing the
  queue when it completes, and the detail panel following a programmatic refetch
  that removed the active row.
- `ui/review/useReviewCounts.test.ts` pins the badge definition and its nulls: a
  truncated report whose rows look complete is `null`, a dismissed row and an
  already-created row are each excluded once, a row that is both is not subtracted
  twice, and blocked rows count.
- `ui/review/ReviewNav.test.tsx` pins the role gate (contributor and above only),
  the dash for an unknown count, and `aria-current` on exactly the open queue;
  `Sidebar.test.tsx` and `ProjectSidebar.test.tsx` each prove the group renders in
  that mode.
- `ui/Settings.test.tsx` is the removal proof: for an editor, a contributor and a
  viewer, opening Imports and Import sources renders no bundle surface and no run
  surface, and makes no request to `GET /bundles/detect` or `GET /imports/runs` -
  the call itself, not merely the DOM, because a Settings-mounted hook would
  still fetch behind a card that rendered nothing.
- `ui/review/useImportRuns.test.tsx` pins the fetch policy the way
  `useBundleDetect.test.tsx` pins the walk: no poll at all while never enabled
  (zero calls after ten simulated minutes), one list plus one detail call on
  entry, exactly one entry fetch under `React.StrictMode`, three 1.5 s ticks
  while a run is running (three list and three detail calls), no further call
  once the run flips to a finished status, a failed poll that re-arms rather than
  ending the chain, and a superseded response that never paints over a newer one.
- `ui/review/ImportRunsPanel.test.tsx` covers the states: running counters,
  succeeded/failed/interrupted/skipped/unknown statuses and their tones, the
  timing and counts lines, the oversize note, the five-line error cap with
  "+N more", the idle line above a finished run, the empty history that claims no
  status, and the retryable error with no run.
  `ui/review/FolderScanPanel.test.tsx` (first test the panel has had) pins every
  control, label and status string, and `ui/App.imports.test.tsx` proves an
  upload from the queue creates the folder, fires both change signals and
  navigates.
- `ui/App.imports.test.tsx` also proves the coordinator: the sidebar badge text
  equals the queue header count on one fetch, the badge is a dash before entry,
  1 while a run is running and 0 once it is finished, and the Imports row renders
  for a viewer as well as an editor while no bundle row appears for a viewer.
- `src/lib/noNativeDialogs.test.ts` and `src/lib/palette.test.ts` stay green: the
  queue uses `ConfirmDialog`-free interactions, and every new rule comes from the
  existing `--mv-*` tokens.

At the time of landing: web 1356 tests in 117 files became 1451 in 122; the API
suite (2326 passed, 1 skipped), the route count (100) and the schema (14) are
unchanged, because this is a web-only change with no migration.

## Superseding conditions

This ADR is superseded if any of the following becomes true:

- the shell gains a router or a URL/history model, so a review queue is
  addressable rather than local view state;
- a work queue returns to Settings, or Settings grows a second full-width queue
  surface for anything other than configuration;
- the badge starts polling, or starts being computed from a source other than the
  state the open queue shows;
- an unknown count starts rendering as `0`, or a truncated report starts
  producing a number;
- the REVIEW group is rendered differently by the two sidebars, or one mode stops
  showing it;
- a bundle row becomes creatable while blocked or already a project, or blocked
  rows stop counting toward the badge;
- the UI gate for a bundle write moves away from the route's CONTRIBUTE
  threshold, in either direction;
- bundles become assets and the queue is rewritten onto `AssetGrid`, or the
  queue's list needs virtualisation past its report limit;
- the imports queue polls while no run is running, stops polling while one is, or
  fetches the run history for a queue that is not open;
- an unknown imports count starts rendering as `0`, or a finished run stops being
  shown under the idle line;
- the folder-scan panel becomes visible below `editor`, or the run history moves
  back into Settings.
