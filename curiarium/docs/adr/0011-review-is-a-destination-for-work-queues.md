# ADR 0011: Work queues live in REVIEW, not in Settings

**Status:** Accepted

**Accepted:** 2026-11-01

**Implementation:** ticket `Mf-f3c2` (slice 1 of the information-architecture
change `Mf-q7vs`: the bundle queue), ticket `Mf-xe4l` (slice 2: imports running),
ticket `Mf-1tx3` (slice 3: missing files), ticket `Mf-rz5k` (slice 4: the
trash queue) and ticket `Mf-vinb` (the Review group moves into one shared
header).

**Amended:** 2026-11-02 - ticket `Mf-vinb` supersedes decision 2: the REVIEW
group is no longer rendered by both sidebars, it is a tablist inside the Review
destination reached from a single shared header.

**Amended:** 2026-11-03 - ticket `Mf-en7k` corrects this record's file
inventory. `Mf-vinb` deleted `ui/review/ReviewNav.tsx` and its test and
`Mf-x1p4` deleted `ui/ProjectSidebar.tsx`, so every passage naming them is now
stated as history, and each REVIEW role gate is pointed at the test that pins it
today: `ui/review/ReviewQueueTabs.test.tsx` for the rows, one by one, and
`ui/WorkspaceHeader.test.tsx` for the Review entry. No decision changes.

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

2. **REVIEW is one group.** As accepted, both sidebars rendered a copy of it:
   `ui/review/ReviewNav.tsx` was rendered by `Sidebar.tsx` and
   `ProjectSidebar.tsx` with identical props, immediately above the shared
   footer, so the group was present in Files and Projects mode and the two
   sidebars could not drift apart. Switching mode does **not** close an open
   queue (the group is shared, so the operator's place survives); library
   navigation - selecting a folder or the unfiled pile, opening a project,
   Imports running's "Scan now" - still returns to the library, because those
   are browsing actions.

   **Superseded by `Mf-vinb` (2026-11-02).** The paragraph above is the decision
   as accepted, kept as its history; the two sidebar copies it describes are
   gone. `App.tsx` now renders a single `ui/WorkspaceHeader.tsx` **outside both
   mode panes**, and that header's **Review** entry is the group's only entry
   point. Queue selection is a `role="tablist"` inside the destination
   (`ui/review/ReviewQueueTabs.tsx`, with its rows in the pure table
   `ui/review/queues.ts`), one `role="tab"` per queue the role may see, in one
   `role="tabpanel"`; the two sidebars render no part of the group and take no
   review props at all, so they can no longer drift apart - neither of them
   carries it. What the group *does* is unchanged, deliberately and
   verbatim: the report is fetched on entry and only on entry (decision 6), each
   row keeps its own route's `minRole` threshold (decision 7), and an unknown
   count is still an en dash and never a zero (decisions 4 and 5). Switching
   mode still does not close an open queue - the destination is shared and the
   header sits above both panes - and library navigation still returns to the
   library. One new guard keeps threshold and fetch bound to the same value: the
   queue a destination opens is resolved against the role on every render
   (`resolveQueue`), and both the view guards and the four fetch predicates read
   that resolved key, so a stored `bundles` key can neither render the bundle
   queue nor arm a detection walk for a role that has no bundle row.

   **Amended by `Mf-x1p4` (2026-11-03).** The paragraph above still stands as
   the `Mf-vinb` record, but its last sentence described a transition that has
   since happened: `ProjectSidebar.tsx` is gone - `Mf-x1p4` deleted it, along
   with its test, once Projects mode became the main pane's own experience (a
   cover grid with search and a contribute-gated inline create, then the
   landing). The header is therefore the only carrier of the Review destination
   in **both** modes, and there is no second rail left to drift from it.

3. **A queue is a header, a queue-specific toolbar, and list + detail.** The
   shared part is `ui/review/ReviewQueueView.tsx`: title, count, one-line
   explanation, last-refreshed time and Refresh, plus a notices slot. Filters,
   selection, bulk actions and the rows belong to each queue. A selected item is
   shown in a **detail panel** beside the list, never in a modal: the item's
   metadata, its editable project name and its row actions all live there, so
   reading one item never covers the queue.

4. **One count coordinator.** `ui/review/useReviewCounts.ts` derives every review
   badge from state the queue already holds - for bundles, the report from
   `useBundleDetect` - and returns a `ReviewCounts` shape that is now closed over
   all four queues: `{ bundles, imports, missing, trash }`, each `number | null`
   with one source per key - the bundle report, `useImportRuns`, the
   `/assets/counts` read App already performs, and `useTrash`. It is a closed
   type on purpose: a new queue has to be added in one place.
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

   The `trash` key is the one badge whose unknown has a price attached. `GET
   /trash` sizes every entry with a recursive `os.walk`, so the key stays `null`
   - the same en dash - until the queue's first successful read, and only then
   becomes the entry count. That is the `–`-until-known rule unchanged; what is
   new is that resolving the unknown costs a walk, which is why the read waits
   for the queue to be opened instead of running at app load.

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
   API: `GET /bundles/detect` is `require_read`, and `POST /bundles/create`,
   `/bundles/dismiss` and `/bundles/enrich` are `require_contribute`. A queue that
   exists in order to act takes the same threshold, so a contributor can use the
   surface the API always granted them, and a viewer gets no bundle row and no
   detection request at all - the settings card's deliberate "detection must never
   be fired for a viewer" invariant is preserved.

   The threshold is per row (`minRole` on `QUEUES` in `ui/review/queues.ts`,
   the table `ReviewNav.tsx` used to own), not one gate on the group, because
   the queues' routes differ. `GET
   /imports/runs` is `require_read`, and a viewer already reads the run history
   from Settings today; a group-wide contributor gate would have deleted a
   proven read surface in order to tidy a menu. So "Imports running" is
   `minRole: "viewer"` and "Bundles to confirm" stays `"contributor"`, and a
   role below every row (or an unknown `null` role) renders no group and no
   Review entry at all. Moving the UI broadens no write permission: every
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

12. **A queue filters on the server, never on a fetched page.** The missing
    queue asks `GET /assets` for `missing=true`, which the API applies as
    `Asset.missing_since IS NOT NULL` - a nullable TEXT timestamp, so the test is
    `is_not(None)` and never a truthiness check - and its badge reads a third
    unfiltered number, `missing`, from `GET /assets/counts`. `GET /tags` and
    `GET /creators` take the same flag, because an unscoped facet beside a scoped
    listing is a lying control: the chip would count a different set than the
    grid it sits beside. The filter composes with `folder_id`, `unfiled`, `q`,
    `tags`, `creator`, `added_by` and `not_in_project` - a missing file can sit
    in a folder, in a project and carry tags - so unlike `unfiled` with
    `folder_id` it is not a 400 pair. Filtering a fetched page on the client is
    forbidden: the count would then describe a set the grid is not showing, and
    the paging window would only ever search the pages already loaded, which at
    2,300+ assets and growing is not the library.

13. **A pinned scope is a prop, never a per-call override.** `missingOnly` is an
    option on `useAssetList` and lives in that hook's closure; it is deliberately
    absent from `RefreshOpts`, the type every toolbar control refreshes through,
    so no control can drop it. `unfiled` is the counter-example that taught this:
    it is a per-call override, and a per-call override is a droppable pin - one
    refresh that leaves it out silently widens the scope. The pin buys the queue
    the whole of `AssetGrid`: the same grid - the flat branch's virtualised
    list, and the grouped `File type`/`Folder` sorts' 24-card sections with
    "Show all N", exactly as in the library - plus selection and bulk bar,
    keyboard operation, previews and card actions, all of them already proven
    in the library, under a scope that cannot be escaped. Project mode also
    derives its own sectioned view from the folder tree (Mf-aaqi): depth-aware,
    with the project root taken as the longest common prefix of the members'
    folder paths trimmed off and one remaining level shown as the group itself,
    under a top-level filter row that is a pure predicate over the loaded items
    (no refetch). This is new presentation, not data the grid already had: the
    tree read had been short-circuited in project mode, so the sectioned view
    makes project mode issue one `GET /folders` per tree-read activation - mount,
    a folder-tree version bump, or entering project mode - rather than promising
    one per mount: that read sits outside the StrictMode one-read claim recorded
    below, so it is not covered by the listing's `walked` guard. It is
    presentation only - it never scopes the listing, which is scoped by
    membership alone - but it is a new failure path, because a failed tree read
    now degrades project mode to the flat grid and toasts. No new entity and no
    listing API change. What the toolbar
    keeps is what only narrows - search, sort, tag chips, the creator facet and
    "not in any project" - and `Reset` clears tags and creator only. The folder
    chrome (breadcrumbs, folder tiles, the uploader, drag/drop) is closed in
    this mode, because a breadcrumb would navigate out of the queue and a
    missing file is not a place to drop a new one.

14. **An empty queue states what is known, not what is hoped.** The empty state
    has three forms, not two, chosen by `scanOutcome` from the advisory status:
    `clean` only when a scan completed and reported no failures, `failed` when it
    reported some, and `unknown` when no scan has completed this session, one is
    still running, or the advisory status read failed and was swallowed. The
    status is not only the one the hook read itself: a per-card Re-scan starts
    its own scan, so it reports every status it polls back through
    `useLibraryScan.adopt`, and the panel and this outcome follow the scan that
    actually produced the rows rather than the previous one. "Every file an
    asset points at is on disk." renders for `clean` alone. `unknown`
    stays flag-truth - "No missing files flagged", "nothing is flagged as
    missing yet" - because an unknown scan is not evidence that the disk is
    whole. `failed` says the last scan did not finish and the list may be
    incomplete. An empty list under a search, tag, creator or "not in any
    project" filter is a fourth case again: an empty *result*, not an empty
    queue. This repo has already shipped two labels that lied about an empty
    list (the `ROOT` badge, and the Unfiled empty state that claimed "Every asset
    is in a folder" under a filter), and this is the rule that stops a third.

15. **The rescan moved, and that broadens who can run it.** The storage rescan
    left the admin-only Settings section (`Storage`) for this queue, which is
    gated at `POST /library/scan`'s own WRITE threshold - `editor`. Decision 7
    above says a row's threshold is its route's threshold, so this is that
    decision applied rather than a new one, but the effect deserves stating
    instead of being discovered: a surface that was de facto admin-only is now
    available to every editor. Nothing's permission changed on the server;
    `GET /library/scan` was always READ and `POST /library/scan` was always
    WRITE. The button is hidden, not disabled, below `editor`, while the status
    line and the failure report stay visible to any reader - a viewer reads the
    queue and the last scan's outcome and simply cannot start a walk.

16. **The trash queue is readable; emptying it is not.** `GET /trash` is READ and
    `POST /trash/purge` is ADMIN, and `authorize()` runs `enforce_admin_mfa`
    after the rank check, so the purge is MFA-gated as well. The nav row's
    threshold is its route's threshold (decision 7), so it is
    `minRole: "viewer"` - `GET /trash` is READ - while the purge gets a third
    capability of its own,
    `canPurgeTrash = roleAtLeast(role, "admin")`, derived in `ui/App.tsx` from
    the same `/me` role. `canDelete` is deliberately not reused: it is
    `editor`/WRITE, one rank below the purge route, so borrowing it would have
    offered an admin action to editors. The Empty trash button and its dialog are
    hidden, not disabled, below `admin` (decisions 7 and 15), and a viewer, a
    contributor or an editor keeps the queue, its rows, the sizes, the total, the
    header count and Refresh.

    Two failure channels, not one. `useTrash` keeps `error` (a failed READ) apart
    from `purgeError` (a failed PURGE); the card funnelled both into one channel,
    which would let a failed read render as a failed purge and a failed purge
    leave an empty list that then read as "The trash is empty". So the empty
    sentence is gated on `!error`, on `trash !== null` and on the last purge not
    having failed - a `purgeError`, or a `purgeResult` with a non-empty
    `failed[]`. A total purge failure arrives as HTTP 200 with `purged: 0` and a
    non-empty `failed[]`, so the result box says "Nothing was removed." rather
    than "0 entries removed" and never reads as success; a partial purge says
    "Some entries could not be removed:" above one red line per failure, and the
    rows that survived stay in the list. A failed purge also keeps the dialog
    open and states the reason inside it; cancelling turns that same reason into
    a notice, so a transient failure is neither lost nor repeated as a success.
    `assertOk` maps every 403 to "Your role does not allow this action.", so the
    purge's MFA gate surfaces as that message exactly as it did in the card, and
    the queue does not attempt step-up.

    The confirmation moved to `ConfirmDialog`, and that is a fix rather than a
    port. The card's hand-rolled overlay declared `role="dialog"`,
    `aria-modal` and `aria-labelledby` but had no Escape handler and no initial
    focus, so a keyboard user's focus stayed behind the overlay and the dialog
    could not be dismissed from the keyboard at all. `ConfirmDialog` has both,
    and the purge added three things to it: `body?: ReactNode`, rendered without
    the `<p>` wrapper when it is a node so the reason can be a run of
    paragraphs; `busyLabel`, so the wait names the purge instead of "Working...";
    and `confirmDisabled`, which moves autofocus to Cancel when the snapshot
    refuses the purge, so focus stays inside the dialog instead of landing on a
    disabled button.

17. **A zip-only row gets one action, and it is a client-side sequence with the
    server as the judge.** The row's one action lists the archive, extracts it,
    waits for the library scan to register the files, and then creates one
    project - four named phases, each said to the operator while it runs. It is
    client-side on purpose. Extraction plus an asynchronous scan is inherently
    two steps: `archive_service` ends with `scan_started =
    bool(seams.start_scan())` and `library_scan.start_scan()` only starts a
    daemon thread and returns at once, so the extracted files are invisible to
    the bundle report until the walk registers them. A single server call that
    claimed atomicity while scanning asynchronously would be a lie, and the
    operator waiting on it would have no way to tell a slow scan from a hung
    request. The honesty lives in a React-free, dependency-injected state machine,
    `ui/review/extractAndCreate.ts`, so every branch is unit-testable with fakes;
    the view only supplies the real deps and paints the sentences.

    The predicate and the words live in `ui/review/bundleRows.ts`, not inline in
    the component: `canExtractAndCreate` is the one rule that decides who gets
    the door, and `EXTRACT_AND_CREATE_LABEL`, `EXTRACT_AND_CREATE_HINT` and
    `archiveExtractTitle` are its sentences. `useBundleDetect` gained
    `createReporting`, which refetches and then returns the server's answer,
    while the existing `create` is untouched, so every bulk door is unchanged.
    The hint says plain Extract's shortfall exactly: plain Extract runs the same
    extraction and the same settle wait, and only stops before the project.
    The action mounts in the detail panel beside the untouched
    `ArchiveExtractAction` (decision 3: a selected item's row actions live in the
    detail panel), and nothing focusable enters the listbox option, so Space stays
    bulk selection and Enter stays plain Extract's door. The narration lives in
    the queue's notices slot rather than in the aside, because a successful
    create hides its own row by default (`showCreated` off) and a one-way write
    must leave a sentence behind.

    Four server facts shape what the client will and will not claim.
    `POST /import/archive` reports no per-entry failures: `ArchiveExtractOut`
    carries only `path`, `destination`, `files`, `bytes`, `already_present` and
    `scan_started` (`curiarium/api/schemas/imports.py:76-92`). So "a partially
    failed extraction" can only be answered honestly as "0 of M entries were
    written" - M from the pre-extract listing - plus the server's own message. A
    per-entry failure report would be a contract change to that schema and to
    `entry_policy`, and is a separate ticket; no count is ever invented that the
    response does not carry. The extract is all-or-nothing, so that zero is the
    truth and nothing was left half-written: the junk-only no-op returns
    `files=0, already_present=False` before any write
    (`archive_service.py:125-138`), the every-destination-exists no-op returns
    `files=0, already_present=True` (`:141-151`), `_refuse_occupied` refuses a
    partial collision at 409 *before* staging (`:153`, `:369-385`), and a
    mid-publish failure unlinks this request's own publications newest-first and
    re-raises (`:199-208`). The settle does not distinguish a failed scan from a
    clean one: `SettleOutcome` is only `settled | timeout | cancelled`
    (`curiarium/web/src/lib/scanSettle.ts:42`). So after a clean settle the create
    response is the only readiness authority - a still-blocked row answers
    `CREATE_BLOCKED` with the row's own `blocked_reason`
    (`curiarium/api/bundle_service.py:2328-2329`, with `NOT_SCANNED =
    "not_scanned"` at `:203`), worded through the existing
    `BLOCKED_REASON_LABELS` table and never inferred client-side from the
    refetched report, which would also race a stale one. One reason is the
    exception: `archive_only` is what this run just acted on, so its own sentence
    replaces the table's instruction to re-extract and names the pairing the scan
    still has not made instead. The `still-blocked` outcome carries `wrote` -
    `extracted.files > 0` - because the `already_present` route creates without
    writing a byte, and the sentence states which of the two happened. Exactly
    one item is sent and the server echoes its key (`bundle_service.py:2325`), so
    a response whose `results` do not name this row is a failure and never
    another row's verdict. And idempotence is the
    server's: `find_existing_project` answers `CREATE_SKIPPED`/`already_created`
    with the existing project's id and name (`bundle_service.py:2402-2416`), so a
    second invocation reports already-created and never makes a second project. A
    settle `timeout` or `cancelled` creates nothing at all, and a create that
    answers `failed` after a successful extraction keeps the files, leaves the
    row ready and the plain `Create project` affordance as the retry - the run
    rolls nothing back, and the deps type carries no rollback member at all (a
    compile-time fact, not a runtime lookup).

    The compound case is the server's as well: ONE item is sent for the row, so
    the server's own compound branch makes one project with its groups; the
    client adds no grouping logic and never sends `exclude_groups` from this
    door.

## Consequences

- Settings is configuration only. Its imports section is relabelled **Import
  sources** and holds what configuration actually is: mount-import settings, the
  remember-cookies preference and the two import-cookie workarounds. Run history
  and the local folder scan are work and live in REVIEW → Imports running.
- A viewer's REVIEW group has three rows - Imports running, Missing files and
  Trash - because each of those queues is gated at its own READ route. `GET
  /imports/runs` is `require_read`, so the run history it could already read
  from Settings moved with the work instead of disappearing; a contributor sees
  the same run surface, counters, errors and Refresh, and loses only the folder
  panel, which is hidden below `editor` although both of its calls - create the
  top-level folder (`POST /folders`) and upload the selection (`POST /upload`) -
  are `contribute`: a client gate one rank stricter than its routes, kept as it
  is by this ADR rather than lowered to their threshold.
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
  stolen. Recovery has two doors, and they share this one ladder: a caret that
  was in the detail pane when the settle refetch replaced its row lands on the
  row that replaced it. When no row survives - an in-place extract emptied the
  queue - the ladder's second step focuses the queue's own `region` landmark,
  `tabIndex={-1}`, reached through `ReviewQueueView`'s `rootRef` prop, because
  that branch renders neither the listbox nor a row. Each step consumes the flag
  it used: removing the focused element fires no `focusout`, so a flag left
  standing would pull the caret out of a filter chip the next time a filter
  empties the list. There is no `document.activeElement === <body>` heuristic -
  a report that replaces the active row with a row that is still there takes no
  focus at all, and a caret left on `<body>` in that case is by design.
- A queue's filter, selection and typed names live in the queue view, so leaving
  REVIEW discards them. That is the same contract the Settings card had, and it
  keeps the App state free of per-queue drafts.
- The row budget is 8 DOM nodes per bundle against the old card's 12.4, and the
  1000-row ceiling is a rendered-list ceiling, not a virtualised one. If a real
  library crosses it the queue needs `useVirtualRows`, which this slice does not
  take on.
- Settings is configuration only down to its Storage section, in the same sense
  the imports slice established: the path template, the capabilities card and the
  snapshot card. The trash card has left as well - `ui/TrashCard.tsx` became
  `ui/review/TrashQueue.tsx`, rewritten on the slice-1 shell and owned by
  `ui/review/useTrash.ts`, and `StorageSection` no longer renders it - and its
  queue lives in REVIEW → Trash. The rescan, its explainer, its "Last scan:
  N new, N known, N missing, N failed" line and its failure report are work and
  live in REVIEW → Missing files.
- The missing count adds no request and no timer. It is one number from the
  `/assets/counts` read App already performs for the sidebar, so the badge and
  the queue header cannot disagree, and an unknown count is an en dash, never
  `0`. A Forget, a per-card Re-scan and a completed storage scan each refetch the
  counts - and so does a **partial** bulk delete, whose successful removals used
  to leave the sidebar on the old number. That fix reaches back: a plain single
  delete carried the same staleness before this slice.
- Trash is the third of those rows, and it is a read surface the old placement
  hid: `GET /trash` is READ, so the admin-only Settings section was hiding a
  queue every role may open. REVIEW → Trash renders for a viewer, a
  contributor and an editor - rows, sizes, total, header count and Refresh - and
  only the Empty trash control is absent.
- The trash badge costs no request at app load and never polls. `GET /trash`
  sizes every entry with a recursive `os.walk`, so the count is an en dash until
  the queue is first opened, then the entry count, refreshed after a purge. It
  is `ReviewCounts`' required `trash` key, so the nav badge and the queue header
  read the same state and cannot disagree.
- A failed read and a failed purge are no longer the same event. `useTrash`
  keeps `error` and `purgeError` apart, so a failed read cannot render as a
  failed purge and a failed purge cannot leave an empty list reading as "The
  trash is empty"; and because a total failure is HTTP 200 with `purged: 0`, the
  result box says "Nothing was removed." instead of "0 entries removed".
- The purge confirmation is keyboard-complete. The card's overlay had no Escape
  handler and no initial focus; `ConfirmDialog` has both, and its
  `confirmDisabled` moves autofocus to Cancel when the snapshot refuses the
  purge, so focus never falls behind the overlay.
- The delete dialogs name the new destination: `ui/AssetDeleteDialog.tsx` and
  `ui/FolderDeleteDialog.tsx` say REVIEW → Trash instead of Settings → Storage.
  No restore control was added and none should appear - restoring is file-level
  and manual by design, which is what the queue's explanation says: "Restoring
  is file-level: move an entry back into the storage folder, then rescan the
  library." - and a grep for "Settings → Storage" under `curiarium/web/src` now
  finds only the two test files that assert it is gone.
- The shared fetch policy was an inherited limitation and is now closed. This
  slice recorded a `useAssetList` with no newest-wins request sequence and no
  React.StrictMode guard, so under StrictMode the missing queue issued two list
  fetches. Ticket `Mf-m45p` fixed that hook instead of leaving it to a follow-up.
  `refresh` and `loadMore` now share one newest-wins sequence: a response that
  started earlier is dropped whichever order the two settle in, so the last
  request to start owns the list. That is invalidation, not cancellation - the
  listing call takes no AbortSignal. The entry effect is guarded by a walked ref
  keyed on the pile (folder, Unfiled, missing-only, project), so one entry issues
  one list request, one tags request and one creators request, while a pile
  change still refetches because the key changes with it. Two Load more clicks in
  one tick are collapsed by a ref latch, since both would otherwise read the
  pre-update `loadingMore` state and append the same page twice. The StrictMode
  claim this ADR makes is therefore uniform rather than narrower than it sounds:
  the four queue hooks keep their own `walked` refs - `useBundleDetect`,
  `useImportRuns`, `useLibraryScan` and `useTrash`, each holding its entry read to
  one per activation - and the shared `useAssetList` now does the same. Two
  caveats sit outside that claim: it does not cover the hook's folder-tree read,
  and offset paging is not cursor paging, so a `limit`/`offset` listing can still
  skip or repeat a row when the underlying set changes under it.
- One limitation stays inherited and unchanged: only the flat branch of the grid
  is virtualised. The grouped (folder/type) branch remains a 24-card cap with a
  "Show all N" control, preserved as it was at HEAD, because retrofitting group
  virtualisation is a different ticket. The project's sectioned view (Mf-aaqi)
  is a SECOND unvirtualised branch and keeps the same 24-card cap per section
  with its "Show all N" escape, so the cap bounds EACH SECTION, not the branch:
  200 sections of 5 parts is ~1000 live cards. What is disclosed where is also
  precise: the section header carries the section's own count, the group chip
  carries the group's total, and the escape control names the capped section's
  full count only when that section is capped. A project above the 1000-member
  page shows loaded counts, disclosed as "loaded ... more to load" rather than
  presenting the loaded page as the whole project. Grouped-section
  virtualisation remains that separate ticket, and it now covers both branches.
- The toolbar sort still applies inside a section (Mf-aaqi): a section's item
  order is the loaded, server-ordered listing, so `Size`/`Creator`/`Asc`/`Desc`
  keep changing what is shown. Only the section LAYOUT is the project's folder
  structure, and it deliberately does not follow `sortDir`.
- A residual path-build cost is accepted and disclosed: building `folderNames`
  calls `folderPath` once per folder, and `folderPath` rebuilds its own index per
  call, so it is O(folders^2) whenever the folder array changes - now including
  every project mount. The single-index fix (build that index once and reuse it
  across the calls) does live inside this ADR's reach but is deliberately out of
  scope for this ticket: it belongs in `folderTree.ts`, a shared module another
  change is touching, and the cached `pathFor` wrapper cannot lower the call
  count because that wrapper also calls `folderPath`. The cost is disclosed here
  so it stays visible rather than silently inherited; folder counts are in the
  hundreds and the memo runs only when the array changes.
- One invariant was deliberately reversed, and the reversal is recorded here
  rather than the assertion quietly deleted: project mode now reads the folder
  tree. It previously never did - `useAssetList.ts` short-circuited the tree
  fetch in project mode and `AssetGrid.project.test.tsx` pinned
  "never touches folders". The tree is presentation only for a project: the
  listing is still scoped by membership alone, so reading it cannot change which
  parts are listed, only how they are sectioned.
- An untouched, pre-existing heuristic remains a known sharp edge. A successful
  walk over a partially mounted storage root can flag present files as missing,
  because presence falls through to `(root/managed_rel).exists()`, which is
  false under an unreadable parent. It was not proven sound here and is filed
  separately; what this slice changes is visibility, since the moved failure
  report is what makes such a scan visible instead of silent.

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
unwind: dismissals remain the single `bundle.dismissals` appconfig value, and the
bundle report is still recomputed from disk on every read. Slice 3's API change is
purely additive - one extra query parameter (`missing`) on `GET /assets` and its
two facets, and one extra count field on `GET /assets/counts` - so a revert loses
only the queue and that filter, and no existing caller's request changes shape.
The only operator-visible loss on a revert is the queue itself, and, for slice 3,
the ability to ask the API for the flagged rows in one request. Slice 4's trash
queue is web-only, so a revert restores the Settings card and loses the queue;
no API, permission, audit, migration or schema change is involved.

## Verification

- `ui/App.review.test.tsx` proves the destination and the fetch policy through the
  real `App` - the header's Review entry and the destination's own tab row - and
  the real `BundleReviewView`: no `GET /bundles/detect` before
  the queue is opened, exactly one on entry with `{includeDismissed: true, limit:
  1000}`, one refetch after a create and one after a dismiss with the badge and the
  view both updating from that single new report, no timer firing while the
  operator sits in the queue, one refetch on re-entry, exactly one walk under
  `React.StrictMode`, no bundle row and no bundle walk at all for a viewer (the
  imports, missing and trash rows are read surfaces a viewer does have), the
  queue surviving a Files/Projects switch, and Back to library returning to the
  library.
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
- `ui/review/ReviewQueueTabs.test.tsx` pins each row's own role gate, row by row:
  bundles from `contributor` up (a viewer gets no bundle row), imports, missing
  and trash from `viewer` up, no tablist at all while the role is unknown, the
  dash for an unknown count, and exactly one `aria-selected` tab. It is the pin
  that replaces the one that went with the deleted component: `Mf-vinb` removed
  `ui/review/ReviewNav.tsx` and its test, and the open-queue marker is
  `aria-selected` on the tabs, not `aria-current`, which is the header Review
  entry's own attribute (`WorkspaceHeader.test.tsx`, "marks the Review entry
  current only while Review is the open view"). The group's single entry is
  proven there too (the Review entry exists exactly once for every role that has
  a queue, and never for a role that has none) and end to end by
  `ui/App.review.test.tsx` ("gives a viewer no bundle tab and never walks the
  library"), not by a per-mode sidebar: `Mf-x1p4` removed
  `ProjectSidebar.tsx` and its test, and `Sidebar.tsx` carries no part of the
  group.
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
- `ui/App.imports.test.tsx` also proves the coordinator: the imports tab's badge
  (the `.sidebar-count` span inside `review-count-imports`) is worded from the
  same count as the queue header on one fetch, the badge is a dash before entry,
  1 while a run is running and 0 once it is finished, and the Imports row renders
  for a viewer as well as an editor. The viewer's missing bundle row is not this
  file's claim: `ui/App.review.test.tsx` and `ui/review/ReviewQueueTabs.test.tsx`
  pin it, the latter row by row.
- `src/lib/noNativeDialogs.test.ts` and `src/lib/palette.test.ts` stay green: the
  queues raise no native dialog - the queue-native confirmations are
  `ConfirmDialog`, and asset deletion keeps its own typed `AssetDeleteDialog` -
  and every new rule comes from the existing `--mv-*` tokens.
- `ui/review/extractAndCreate.test.ts` pins the four-phase order with recording
  fakes and every honesty branch by itself: an extract failure reports 0 of M and
  no create, a junk-only archive neither settles nor creates, a settle `timeout`
  creates nothing at all, a settle `cancelled` is silent, a
  `skipped`/`already_created` answer reports already-created and exactly one
  project across two identical runs, a failed create leaves the files - the deps
  type carries no rollback member (a compile-time fact) and the sentence says so
  - a `blocked`/`not_scanned` answer speaks the row's own sentence and says the
  extraction stands, a `blocked`/`archive_only` answer names the missing pairing
  instead of repeating the table's re-extract instruction, the same answer on the
  `already_present` route says the run wrote nothing, a `results` list naming
  another key and an empty `results` list each fail with the no-result
  sentence, a throwing `beforeCreate` cannot stop the create, and a
  compound-shaped row sends one item.
  `ui/review/bundleRows.test.ts` pins the predicate and both hover sentences,
  `ui/review/useBundleDetect.test.tsx` pins `createReporting` (the server's answer
  returned unchanged and the report refetched, the old `create` still returning
  nothing), and `ui/review/BundleReviewView.test.tsx` pins the single button and
  its absence for a ready row, an already-created row and a viewer, the narration
  surviving the hidden row - the same component instance rerendered with the
  created row hidden, its narration still on screen while the row is gone from
  the listbox - the narration cleared by the queue's Refresh that its own
  `settle-abandoned` sentence asks for, and plain Extract and the bulk label
  unchanged beside it.

At the time of landing: web 1356 tests in 117 files became 1451 in 122; the API
suite (2326 passed, 1 skipped), the route count (100) and the schema (14) are
unchanged, because this is a web-only change with no migration.

Slice 3 (`Mf-1tx3`) adds its own proofs. In the API suite,
`test_asset_missing_filter.py` pins the filter and the count: a flagged row is
listed and an unflagged one is not, a cleared flag leaves the list, `missing`
composes with every other filter, the count is re-derived from SQL rather than
trusted, and an offset walk pages with no duplicates or gaps.
`test_asset_unfiled_filter.py` grew the junk-parameter assertion that keeps the
counts from describing a filtered picture, and `test_library_scan.py` proves that
a scan over an absent root changes no flag - a failed scan cannot empty the
queue. On the web side, `ui/App.missing.test.tsx` pins the entry policy and the
pin (no status read and no pinned listing before the queue opens, exactly one
`GET /library/scan` on entry including under `React.StrictMode`), the four
coherence cases - a rescan refetching the grid and the counts once, a Forget
dropping the badge and the header together, a reappeared file leaving the queue,
a failed scan never rendering the all-clear - the badge/header equality from one
fetch, the en dash for an unknown count, and the viewer/editor gates. After
integration review it also pins the card-initiated scan: a per-card Re-scan
reports every status it polls through `onScanStatus` into
`useLibraryScan.adopt`, so a Re-scan that could not check every file, or that
has no `finished_at` yet, cannot leave the all-clear on screen, while one that
completed cleanly still reads clean.
`ui/review/MissingFilesReviewView.test.tsx` covers the queue states (loading, a
retryable error with Try again, empty, empty after a filter, scan-failed-empty,
scan-unknown), `LibraryScanPanel.test.tsx` and `useLibraryScan.test.ts` cover the
moved panel and its hook, and the hook's tests now cover `adopt` too: the
outcome follows the adopted status, it announces nothing a second time, and it
arms no second poll. `ui/AssetGrid.test.tsx` proves every toolbar control keeps
the pin, that the flat branch stays virtualised and that the grouped branch
stays a cap, and `ui/Settings.test.tsx` is the removal proof by call: no rescan
surface and no `GET /library/scan` for any role.
`src/lib/palette.test.ts` (WCAG AA) and `src/lib/noNativeDialogs.test.ts` stay
unmodified and green, and every new rule uses the existing `--mv-*` tokens.

The slice landed with the API suite at 2332 passed and 1 skipped (baseline 2326
and 1) and the web suite at 1621 tests in 129 files (baseline 1533 in 126). The
route count (100) and the schema (14) are still unchanged: an extra query
parameter and an extra count field add no route, no mutation, no audit action and
no migration.

Slice 4 (`Mf-rz5k`) adds its own proofs and no API proof at all - the route count
(100), the permission matrix, the audit coverage and the schema (14) are
unchanged, because the trash queue reuses `GET /trash` and `POST /trash/purge` as
they were. `ui/review/TrashQueue.test.tsx` (16) covers the queue's states and the
purge result box: a failed first read that renders no empty sentence, a failed
refresh that keeps the last readable list under one notice, "Nothing was
removed." for a total failure, "Some entries could not be removed:" for a partial
one with the rows that survived left in place, a purge failure shown inside the
still-open dialog and then as a notice, the three snapshot refusals, the dialog's
initial focus and Escape, the absent restore control, and no purge control or
dialog at all below `admin`. `ui/review/useTrash.test.ts` (11) pins the fetch
policy and the two channels at the hook: no read while the queue is never opened,
one per activation including under `React.StrictMode`, a refetch on re-entry, no
timer however long the queue stays open, the newest response winning, a 401
signing out instead of reporting an error, one refetch after a successful purge,
and a failed purge that sets only `purgeError` and keeps the rows.
`ui/App.trash.test.tsx` (10) proves the destination end to end: one Trash tab
in one tablist whichever mode is showing, the en dash before entry and the
header number after it, no read at app load, one read per entry including under
`React.StrictMode`, the queue surviving a Files/Projects switch, and - for an
admin alone - a purge that refetches exactly once and moves the badge and the
header to zero, against a viewer who gets the queue, its rows, its sizes and
Refresh but no path to a purge. `ui/ConfirmDialog.test.tsx`
grew three cases for `body`, `confirmDisabled` and `busyLabel` - Escape still
closes, and autofocus lands on Cancel when confirming is refused - while
`ui/review/ReviewQueueTabs.test.tsx` pins the Trash row for all four roles,
`ui/review/useReviewCounts.test.ts` pins `trashBadgeCount` and the wordings that
read it, `ui/settings/StorageSection.test.tsx` proves no trash surface is left
there, and `ui/Settings.test.tsx` is the removal proof by call: no request to
`lib/api/trash` for a viewer, a contributor, an editor or an admin.

## Superseding conditions

This ADR is superseded if any of the following becomes true:

- the shell gains a router or a URL/history model, so a review queue is
  addressable rather than local view state;
- a work queue returns to Settings, or Settings grows a second full-width queue
  surface for anything other than configuration;
- a badge fetches or polls on its own account - its own request or timer -
  instead of being read from the state its queue already holds, or starts being
  computed from a source other than the state the open queue shows (the imports
  count riding the running-run poll is not this, and the trash badge reads on
  entry and never polls);
- an unknown count starts rendering as `0`, or a truncated report starts
  producing a number;
- the REVIEW group gains a second carrier beside the header - a sidebar or a mode
  pane rendering its own copy of it - or one mode stops reaching it from the
  header;
- a bundle row becomes creatable while blocked or already a project, or blocked
  rows stop counting toward the badge;
- the UI gate for a bundle write moves away from the route's CONTRIBUTE
  threshold, in either direction;
- bundles become assets and the queue is rewritten onto `AssetGrid`, or the
  queue's list needs virtualisation past its report limit;
- the imports queue polls while no run is running, stops polling while one is, or
  issues an entry fetch for a queue that is not open (a poll that outlives
  navigation mid-run is the accepted limitation recorded under Consequences,
  not this);
- a finished run stops being shown under the idle line;
- the folder-scan panel becomes visible below `editor` - its gate is one rank
  stricter than its own `contribute` routes, and is kept as it is;
- the missing filter becomes client-side filtering of a fetched page, or
  `/assets/counts` goes back to two numbers;
- the missing pin becomes overridable per call, or moves out of the listing
  hook's closure;
- an unknown or failed scan status renders the all-clear copy;
- the grouped branch is claimed as virtualised without being virtualised;
- the UI gate for the rescan moves off `POST /library/scan`'s WRITE threshold in
  either direction;
- the Trash row's gate rises above `viewer`, so it stops being a viewer surface,
  or any purge control becomes visible or available below `admin` - in
  particular if `canPurgeTrash` is replaced by `canDelete`;
- the trash queue reads at app load instead of on entry;
- the empty sentence renders after a failed read or after a failed purge;
- a purge result box reports a total failure (`purged: 0` with a non-empty
  `failed[]`) as success, or a partial failure hides the rows that survived;
- the delete-confirmation copy points at Settings again instead of
  REVIEW → Trash;
- a restore control appears in the trash queue - restoring is manual by design,
  and the queue must not appear to offer it.
