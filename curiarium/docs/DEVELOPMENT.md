# Curiarium Development Verification

## Prerequisites

- Docker with Docker Compose.
- GNU Make.
- `curl` for the container health check.

Host Python, Node, and Go installations are optional because the source-owned
verification targets use versioned container toolchains. Release image digests
are recorded later at the release-candidate gate.

Python direct dependencies are declared in `requirements.txt` and
`requirements-dev.txt`. The successful Python 3.12/Linux environments are frozen
in `requirements.lock` and `requirements-dev.lock`; Docker and CI install the lock
files.

## Complete verification

From the repository root:

```bash
make verify
```

This performs:

0. Ruff (API) and ESLint (web) gates; `make verify-lint` runs them alone.
1. API migration tests under Python 3.12.
2. Web TypeScript checking, Vitest tests, and the production Vite build under
   Node 24 LTS (`node:24-alpine`, npm 11, image digest
   `node@sha256:8a32c8cf649a49915a66e81e7a609223c708d0adabf288ee3f4077d765542f85`).
3. Bridge tests and compilation under Go 1.21.13.
4. Validation of the local, canonical production, historical alias, and smoke
   Compose configurations.
5. A clean container smoke test with temporary named volumes, HTTP health checks,
   non-root runtime assertions, schema-version verification, and log canaries:
   `/api/health?token=<canary>&sig=<canary>` is requested through the live web
   proxy, then the `api` service is stopped and the same query shape is sent
   again so the proxy's own error path answers. The run fails if either canary,
   `?token=` or `?sig=` appears in any container's log, so a signed file grant
   cannot survive in a request line the gate accepts.

The smoke test picks a free host-port lane, so a single run still lands on the
historical `18000` (API) and `15173` (web) whenever those are free. Lanes are
10 ports apart:

| Lane | API port | Web port |
| ---- | -------- | -------- |
| 0    | 18000    | 15173    |
| 1    | 18010    | 15183    |
| 2    | 18020    | 15193    |

`MV_LANE=1 make verify-smoke` pins a lane and fails fast if that lane is
already claimed by another run or either of its ports is busy. With `MV_LANE`
unset the script scans lanes 0-2 in order and takes the first that is both
unclaimed and has two free ports. A lane is claimed atomically with a lock
directory under `/tmp/curiarium-smoke-lanes/` (POSIX `mkdir` is atomic), not
merely probed: `compose up --build` needs minutes before it binds the ports, so
a probe alone would let two simultaneous runs pick the same lane. A run killed
hard leaves a stale lock, and the next run checks the recorded pid and takes
over automatically. The documented port overrides still win and disable both
scanning and locking:

```bash
CURIARIUM_SMOKE_API_PORT=28000 \
CURIARIUM_SMOKE_WEB_PORT=25173 \
make verify-smoke
```

The smoke project is removed with its named volumes and its images on success,
failure, or interruption. It does not use `curiarium/data` and does not open
`api/curiarium.db`.

## Parallel-safe verification

Two verification runs may share one host and one Docker daemon. Every mutable
thing a run creates is namespaced by `MV_RUN_ID`:

- the API test database and storage root, under
  `/tmp/curiarium-tests/<run id>/`;
- the four image tags (the repository names are unchanged, only the `:` tag is
  per run);
- the smoke Compose project name, and therefore its container names and named
  volumes;
- the smoke host ports, selected per lane as described above.

The default run id is the pid of the `make` process plus the worktree directory
name, sanitised to `[a-z0-9_-]`. Set `MV_RUN_ID` yourself to join a named lane,
for example `MV_RUN_ID=Mf-ly4o make verify`; values are lowercased and
sanitised, so that id becomes `mf-ly4o`. `make verify-fast` runs pytest on the
host and inherits the id, so a lane's database, storage and images share one
name. The `verify-api` container derives its own id from its pid instead: a
container has a private `/tmp`, so it has nothing to share with another run. The
smoke project is named `curiarium-gate0-smoke-<run id>`, so two runs never
share container names or named volumes.

A plain `make verify` is otherwise unchanged: same targets, same commands, and
it still lands on `18000`/`15173` whenever those are free. Each run removes the
images it built (the make targets remove theirs after use, and the smoke project
is torn down with `--rmi local`), so per-run tags do not accumulate.

A run killed hard (SIGKILL before the interpreter exits) can leave
`/tmp/curiarium-tests/<run id>/` behind. That directory is safe to delete and
belongs to no other run. It can also leave lane locks under
`/tmp/curiarium-smoke-lanes/`; those are reclaimed from their recorded pid
automatically, but `rm -rf /tmp/curiarium-smoke-lanes/*` clears them if a
stale lock ever blocks a run.

A run never deletes a database it did not create, so pointing `DB_URL` and
`FILE_STORAGE` at fixed paths keeps that data for investigation after the run
finishes.

## Fast gate for individual tickets

```bash
make verify-fast
```

Runs ruff + pytest (API), eslint + tsc + vitest (web, no production build) and
`go test ./...` (bridge) on the host toolchains in about a minute. It needs a
repo-local `.venv` (created with `uv venv --python 3.12 .venv`, then
`uv pip install -r requirements.lock -r requirements-dev.lock ruff` from
`curiarium/api/`) and `npm ci` once in `curiarium/web/`. The web toolchain
(vite 8, vitest 5) needs Node ≥ 22.12 on the host, so a host on an older Node
has to fall back to `make verify`, which runs the same steps in the containers. Use it per ticket,
then `make verify` exactly once per batch, on integrated main before the CI
dispatch. There are no per-ticket, per-merge or per-fix-forward full gates: a
ticket touching migrations, Dockerfiles, nginx, compose manifests or locks is
covered by that single end-of-batch gate, provided the batch ends green.

## Focused verification

```bash
make verify-api
make verify-web
make verify-bridge
make verify-compose
make verify-smoke
```

CI invokes these same targets. A local workaround that CI does not execute is not
release evidence.

### Snapshots against a real filesystem

`tests/test_snapshots_integration.py` is the only evidence that a snapshot is a
real copy-on-write view, so it is manual: it is skipped unless
`SNAPSHOT_INTEGRATION=1`, and it `pytest.skip`s a root that is not an explicit
scratch directory (it refuses nothing and fails nothing: a bad root is a skip
with the reason). `SNAPSHOT_INTEGRATION_ROOT` must be an existing directory whose
name starts with `curiarium-snapshot-scratch-`, at least three path components
deep, and neither the library root nor an ancestor nor a descendant of it.

```bash
cd curiarium/api
scratch=$(mktemp -d -t curiarium-snapshot-XXXXXX)
mkdir -p "$scratch"
SNAPSHOT_INTEGRATION=1 SNAPSHOT_INTEGRATION_ROOT="$scratch" \
  python -m pytest -q tests/test_snapshots_integration.py
```

CI never sets either variable, and neither one belongs in `.env.example` or a
compose manifest. Only the manifest half is guarded: a forwarded key that nothing
reads fails `tests/test_deploy_manifest_env.py`. An extra `.env.example` key
fails nothing, so that half is documentation discipline, not a gate.

### The virtual grid's scroll container (no browser harness)

jsdom does not scroll, so the scroll-container contract is the one part of the
grid that unit tests can only model: `useVirtualRows` listens for `scroll` on
`document` in the capture phase (element scroll events do not bubble) and reads
`<main>` fresh on every compute, so a late-attached or replaced scroller still
drives the window. The browser is the truth for Mf-d8vc and CI has none, so
that pass is manual after touching the grid.

Open the app at 1000x800, Files mode, a library with 47 assets (three columns,
so 16 rows), then paste this into the devtools console:

```js
const main = document.querySelector("main");
const rows = () => [...document.querySelectorAll("[data-row-index]")].map(el => +el.dataset.rowIndex);
({ scrollHeight: main.scrollHeight, clientHeight: main.clientHeight, scrollTop: main.scrollTop });
({ first: rows()[0], last: rows().at(-1), cards: document.querySelectorAll("[data-asset-id]").length });
main.scrollTop = main.scrollHeight;
await new Promise(r => requestAnimationFrame(r));
({ scrollTop: main.scrollTop, first: rows()[0], last: rows().at(-1) });
document.querySelectorAll("[data-asset-id]").at(-1)?.click();
```

Expected before scrolling: `clientHeight` about 745, `scrollTop` 0, the first
`data-row-index` 0 and the last in single digits - a window, never all 16 rows.
`scrollHeight` is 16 rows tall (16 x the measured row height plus 15 gaps; the
estimate is 344 per row until the cards are measured, so the number lands
anywhere from ~2.2k to ~5.7k and is not itself the assertion). After the scroll:
`scrollTop` equals `scrollHeight - clientHeight`, the first rendered index is
well above 0 (the last rows, near 15), the last rendered index is 15 and the
47th asset's card is in the DOM. The click opens that asset's card/menu without
an error. If the last report still says `first: 0` and there is no 47th card,
the listener is dead again.

## The proposal engine (one implementation behind the CLI and the API)

`curiarium/api/proposal_engine.py` is the only copy of the rules that answer "what
could this folder become": `measure()` walks a folder read-only, `propose()` ranks
the interpretations the facts support, `arrange_proposal()` returns the lines
describing what arranging one would cost (text only - it moves nothing), and
`validate()` scores the rules against the operator's labelled folders and returns
rows instead of printing them.
`curiarium/scripts/propose_import.py` is now argv plus rendering: it bootstraps
`sys.path` to `curiarium/api` and calls everything as `proposal_engine.X`.

That module is stdlib-only, and the constraint is load-bearing rather than tidy.
The operator's tight loop is a bare host python pointed at a live CephFS mount; a
`fastapi` or `sqlalchemy` import anywhere in the engine would make that loop need
the whole app environment to start, so
`curiarium/api/tests/test_proposal_engine.py` audits the engine's imports instead
of trusting a comment. The same module pins the split itself: it asserts the shim
and the test import the SAME module object (`sys.modules["proposal_engine"]`,
resolved to `curiarium/api/proposal_engine.py`) and that the shim defines no
`propose`, `measure` or `MODEL_EXTS` of its own. Copy the rules back into the
script and the suite reds on module identity, not on wording drift.

The two host-only proofs are the operator's, run against the real library:

```bash
python3 curiarium/scripts/propose_import.py --root /mnt/ceph/3D_models --only Vault --only import/Stagetop
python3 curiarium/scripts/propose_import.py --root /mnt/ceph/3D_models \
    --projects /tmp/project-keys.json \
    --labels curiarium/scripts/fixtures/proposal-labels.json --validate
```

`--projects` is a local export of the live project keys and is NOT committed: it
names the owner's whole library. The labelled fixtures ARE the regression gate -
they are the projects the owner confirmed by creating them, so a rule change that
would split a confirmed product or merge a confirmed collection fails the run.
[`curiarium/scripts/fixtures/README.md`](../scripts/fixtures/README.md) describes
the fixture and its `expect` vocabulary. `curiarium/api/tests/test_proposal_engine.py`
replays six of those labels over synthetic trees, so a rule change cannot silently
start fragmenting a library on CI. The endorphin SHAPE is replayed in the image
too: `curiarium/api/tests/test_git_image.py` builds a synthetic checkout with the
matrix's own `new_repo`/`git` helpers and asserts `git-project` at `high` with a
note-free creatable row, because a git shape that only the host CLI can score is
a shape CI cannot defend. The real library's labels stay host-CLI-only: that
replay needs the mounted library, so it is unchanged.

The engine's `MODEL_EXTS` is a DELIBERATE superset of the API's five-suffix table:
a proposal may name a payload no importer accepts, which is exactly what these
proposals are for. So `check_model_table.py` must not be pointed at it - that
script pins equality for the four copies that really must agree. The containment
(`library_scan.MODEL_SUFFIXES <= proposal_engine.MODEL_EXTS`) lives in the test
instead.

Since `Mf-1agz` the engine is also the ONLY thing that ranks the review queue:
`bundle_detect` is deleted, `bundle_service` asks `proposal_engine.propose()` about
every row (folder or `.zip`) and serves its option list as `matches`, and the
invariant is pinned by `curiarium/api/tests/test_bundle_report_engine.py` - for
every synthetic row the report's recommendation must equal
`propose(...)[0].shape`, with `engine:<shape>` as every match's rule. Three things
about that seam are easy to break and are named here rather than found later: the
report passes `git_timeout=5.0` (it DOES probe git - the label corpus needs it -
but never unbounded), it passes `project_keys` in STORAGE-relative spelling,
because `Facts.rel` is storage-relative and an `add-to-project` reading is
impossible without them. The third is the fallback contract: when a `.git` is
present and the probe cannot run, `git_state` returns a dict carrying `git_error`
rather than `None`, `propose()` withholds both split shapes and recommends
`one-project` at `medium` with a note naming the cause, and `None` now means
exactly "not a repo" - the image ships `git`, so that path is the exception
rather than the normal case (`Mf-ky16`).

## The arrange surface (approving a shape, and undoing it)

Detection says what a folder IS; the arrange is the one operation that makes the
disk match a shape the operator chose. Three routes, classified in
`permissions.ROUTE_PERMISSIONS`:

| Route | Level | What it does |
| --- | --- | --- |
| `GET /bundles/arrange?key=&shape=` | READ | re-walks the folder, asks `proposal_engine` for the move list that shape implies, and returns the moves, the directories they need, the prose rendered from them, the row's own `creatable` verdict and every refusal it found. Each move is `{from, to, kind, action}`: `kind` is `file`/`dir` (what the path is on disk), `action` is one of `hoist`/`move`/`file-archive`/`rename` (why the move exists). A dry run: it writes nothing |
| `POST /bundles/arrange` | WRITE | applies one previewed plan, echoing its `plan_id` back so a tree that changed since the preview is a 409 rather than an approval of something never shown |
| `POST /bundles/arrange/{arrange_id}/undo` | WRITE | folds the landed moves back through the same mover and restores the rows from the ledger |

The **ledger** is the durability story. It rides inside the project's existing
`_bundle` marker as `_bundle["arrange"]`, beside `key`/`detector`/`groups` - the
whole `_bundle` dict is passed to `project_service.merge_project_marker`, because
that function replaces a `_`-prefixed key wholesale and a nested-only write would
drop the detection fields. It carries `id`/`plan`/`shape`/`moves`/`creates`/
`projects`/`folders`/`assets`/`done`/`applied`/`undone`: `done` is how far a partial
apply got, `applied` says the move list landed, `undone` says it was folded back,
and `folders`/`assets` are the row paths an undo restores. Commit 1 writes the
project(s) and the ledger before a single byte moves, so an interrupted arrange is
a project with a replayable record, never a project with nothing behind it.

**One mover.** Every move - a file and a directory alike - goes through
`move_service.move_bytes`, the one arbiter of the bytes: `os.link` is primary
because it is atomic and refuses an occupied destination, the locked `os.rename`
fallback covers filesystems that cannot hardlink, and it is that rename which
moves a DIRECTORY (`os.link` refuses one with `EPERM`). There is no copy path.
`asset_service.move_managed_file` is now the publishing edge - the streamed upload
and the file `asset_file_service` saves - alongside
`asset_service._move_without_clobber`, the bare delegate `archive_service` stages
an extraction through and `snapshot_service` lands a download with. Neither
writes a ledger entry: publishing bytes into their rendered path is not a product
move, and a row per upload would bury the moves an operator made.
The arrange's loop asks `move_service.move` once per move with
`arrange:<arrange_id>:<index>` as the intent, so a resume at index N is a new
intent and a re-run of an index that already landed is the primitive's no-op;
undo asks `move_bytes` directly, because the arrange's own ledger in the project
marker is its record. That is what makes the cross-device case a refusal: `EXDEV`
is translated into a 409 (`"cross-device move refused: nothing moved, and
this operation has no copy path"`, or `"... after N of M moves landed; re-post the
same plan to finish"` for a partial one), and the pre-flight reports the same rule
as a `cross_device` refusal before any byte moves. The refusal is
`move_service.CrossDevice`, an `OSError` carrying `errno.EXDEV`. A `.zip` row is
refused with a pointer at the extract door for the same reason: unpacking would be
a copy.

The service's rules are in `curiarium/api/arrange_service.py`; the HTTP proofs are
`tests/test_arrange.py` (preview, apply, the four evidence layouts) and
`tests/test_arrange_undo.py` (the fold backwards, the row restore, the replay).

## The primitives (move, adopt, create-project)

Three coarse owners sit under the doors, and each is defined as much by what it
refuses as by what it does.

**`move_service`** owns relocation: the bytes, the rows that describe them, and the
`move_ledger` row undo replays. It never copies (there is no copy path, which is
why `EXDEV` is a refusal rather than a slow fallback that silently doubles a
library), it does not decide WHETHER a move should happen - intent, key and audit
event are the caller's - it owns no project membership, and it writes no audit row
of its own: the door stages its event through `events=` into the same transaction
as the ledger row and the row rewrite.

**`adopt_service`** owns the membership delta: the missing-only rule, so a second
adopt over the same set is a no-op that says `added=()`, and the group refresh as
a MEASUREMENT returned to the caller rather than a second write to the marker. It
never moves bytes and never touches the filesystem, it never creates or deletes a
`Project` row (deleting one would delete the carrier of the arrange's ledger,
which is what makes a second undo a 409), and it does not decide which files are
members - the ids are written verbatim, in the caller's order.

**`bundle_service.create_project`** owns the create a library path describes. It
resolves the path's members through `bundle_service.members_of`, drops the members
an excluded group owns, merges the groups into the marker through
`bundle_service.marker_groups` with the `bundle_service.attached_by_group` counts,
asks the ONE dedupe (`project_create_service.find_existing_project`, four
identities strongest first) and writes through
`project_create_service.create_project_from_members`, so the project row and its
memberships land in one transaction. It writes no audit row and cannot:
`audit.ACTIONS` has no `project.create`. The door audits - `bundle.create` for the
detection door, the operation's own lifecycle rows for the operations runner.

The verb lives beside the member rule rather than in `project_create_service`, and
that is a firewall, not taste: the archive-extract door imports
`project_create_service`, and `tests/test_archive_project.py` refuses to let ANY
bundle module appear in that door's static import closure - the walker sees a
function-local `import bundle_service` as an edge like any other. The split is
therefore `bundle_service` for the path-keyed verb and `project_create_service` for
the dedupe and the one write both doors share.

All three hold the same four properties, and each is a mechanism rather than a
promise. **Idempotent**: one intent is one `move_ledger` row, so a re-run over the
same pairs reports what the first run found (`already_applied`, with the recorded
`landed`) instead of moving anything twice, and an adopt adds only what the
project is missing. **Undoable**: `undo_ledger` replays a row backwards - bytes,
then rows, then `undone = 1` - and `adopt_service.undo_entry` removes exactly the
memberships the entry recorded as added and puts the group snapshot back, leaving
a membership an operator added afterwards alone; a create's undo is the
operation's own. **Refusing rather than guessing**: `Clobbered`, `CrossDevice`,
`UnknownSource`, `IntentMismatch`, `AlreadyUndone` and `Superseded` are the
vocabulary, and every door maps a refusal back to the exact status and message it
answered before, which is why `MoveRefused` subclasses `HTTPException` carrying
today's status and detail. **Audited at the door**: the primitive writes no audit
row and stages the caller's, in the caller's transaction, before the bytes move.

**The ledger** is `move_ledger`, schema 16: `id`, `intent` (UNIQUE), `key`,
`pairs_json`, `landed`, `applied`, `undone`, `superseded_by`, `created_at`. The
pairs are storage-relative, so an undo still names the right file after the
storage root was configured differently in between. `applied` means "this intent
is complete"; for the arrange's `apply_rows=False` shape it says the byte half is
done and the row half is the caller's commit 2. A row that is NOT complete is a
stopped run, and it RESUMES from `landed` rather than being treated as done: the
pairs before `landed` are skipped instead of re-clobbered, so a refusal that
stopped a move (a target that appeared, a cross-device mount) can be retried under
the intent already recorded. `prune()` keeps the newest 200 rows and everything
younger than 30 days - a union, not an intersection, so a quiet install never
loses a row it could still undo - and `supersede(prefix, by=...)` marks every live
row under an intent prefix as superseded, which is how a caller that has already
folded those moves back by its own reckoning (the arrange is the case it was
written for) stops a later `undo_ledger` relocating those bytes a second, blind
time. `audit_events` stays the permanent record of what moved; the ledger is a
bounded tail.

**The honest boundary.** The bundle door (`bundle_service._create_one`) and the
arrange still hand their OWN member sets to `create_project_from_members` rather
than composing through `create_project`. The bundle door's set is the CLAIMED set -
`_own_assets` narrowed by the report-wide `_claim_owners` map, a fact about the
scan - and its cover
comes from the artifact's evidence; the arrange's set is the POST-MOVE projected
paths, which registering by key would get wrong for the very plan that is moving
them. Converging them is a later slice, so nothing here should be read as saying
the three compose today.

**The asymmetry, resolved.** A contributor may create a project through
`POST /projects` (CONTRIBUTE) AND queue one through `POST /operations`, because
Mf-215n's amendment set the rubric the owner uses: an operation is contribute
unless the action can REMOVE data. The per-workflow level lives in one table,
`operations_service.WORKFLOW_LEVELS`, and the handler enforces it (a route may
declare only one level). A row for a workflow that does not exist yet is a
declaration for the record, not a claim that it runs - `WORKFLOWS` is still
`("create-project",)`.

## The import flow (auto for high confidence, guided for the rest)

Detection says what a row is and the arrange says what a shape costs. This is the
door that writes: one project per row, with the cover and the metadata the
operator accepted and nothing else. `Mf-yz9e` built it without a new route.

**The band is the engine's.** `proposal_engine._assign_confidence` sets each
`Option.confidence`: `high` when documentary evidence sits at the candidate's OWN
root (a document in a child documents the child) or when the reading is `git-project`
/ `add-to-project`; `medium` for structural evidence with a >=15-point margin over
the runner-up; `low` for a contested or guessed reading. `matches[0].confidence` is
served as the row's `confidence` and that is the only band the client ever sees -
it never derives one. The engine also holds the invariant that an alternative never
outranks the recommendation, so a HIGH row's recommendation is always a
one-project reading rather than a hopeful alternative.

**One predicate on the client.** `bundleRows.actableMode(row)` returns
`"auto" | "guided" | null`, and the same function feeds the row's confidence-cell
title (`actableReason`), the detail pane's buttons and the panel, so a badge and a
button cannot disagree. A settled row (`existing_project_id`) and an archive-only
row are `null`; `isCreatable` is the server's own gate, asked rather than
re-implemented; `"auto"` needs a HIGH row that is `ready` with a shape in
`AUTO_SHAPES`; anything else is `"guided"`.

**AUTO is a flag on the existing create route**, not a new one: `POST /bundles/create`
with `{"items": [{"key": ..., "auto": true}]}`. `bundle_service.AUTO_SHAPES` is
`{"one-project", "git-project"}` - small and explicit, the two shapes the create
door can enact in place. `add-to-project` is deliberately excluded: it is not in
`creatable_shapes`, its whole operation is the arrange door, and auto must never
half-do one. `_auto_refusal` runs BEFORE any write and refuses, in this order: a
non-HIGH band (`auto requires high confidence`), a shape outside `AUTO_SHAPES`, an
operator-supplied shape that differs from the recommendation, a row that is not
`ready`, and a recommended shape whose `proposal_engine.arrange_moves(facts, shape)`
is non-empty (`auto needs arranging first: N move(s)`). Every refusal is a refusal,
never a half-auto: the fallback is the guided panel. AUTO writes only the metadata
facts the engine itself calls `high` (a parsed `medium`/`low` field stays a
suggestion), takes the engine's cover, and reuses create's `already_created` for
idempotency. There is no new route, no new `permissions.ROUTE_PERMISSIONS` row and
no new audit action: the existing `bundle.create` event gained `auto`, `shape`,
`cover` and `accepted_fields`.

**GUIDED is the same route with a map**: `POST /bundles/create` with
`cover: {"mode": "asset", "asset_id": ...}` or `{"mode": "none"}` and
`metadata: {field: value | null}`. `bundle_service._metadata_write` reads that map
BEFORE it would fall back to `_enrichment`: a field absent from the map, or mapped
to `null`, is never written. That is what makes "a licence the operator did not
accept is never written" a server rule rather than a client courtesy. `metadata`
accepts only `creator`/`licence`/`licence_url`/`source_url`/`description`; a
project's title is its `name` field, so a `title` key is refused as an unknown
metadata field instead of being silently dropped. Omitting both `cover` and
`metadata` is the legacy create, byte-identical to before this ticket.

**The panel** is `curiarium/web/src/ui/review/GuidedImportPanel.tsx`, self-contained
like `ArrangePlanPanel`: it imports the domain module `lib/api/bundles` and never
the barrel, owns its own plan read and its own write, and renders nothing for a
viewer. It shows the cover chooser (the engine's choice, no cover, or the row's
registered images - an engine candidate with no registered asset is shown inert as
"not registered - cannot be chosen"), the metadata source field with "Use this
source" and on-demand enrichment, the parsed suggestions each accepted, edited or
dismissed with the `where` that produced them and nothing pre-accepted, the shape
chooser over `matches` (a `creatable: false` shape, or a plan with moves, says
"Needs arranging" and mounts `ArrangePlanPanel` instead of writing), and the move
list from `GET /bundles/arrange` before anything is written.

**Four additive row fields** carry that panel: `metadata_suggestions`, `provenance`,
`cover_candidates` and `images`. All are defaulted, and all are declared on
`BundleDetectItemOut` - a field the response model does not declare is dropped on
the way out, which is the `rule_set`/`refused` trap this repo has already fallen
into once. They are pinned by the set-equality test in
`curiarium/api/tests/test_bundles_api.py` and by the regenerated OpenAPI component
fixture.

**Archive rows are not a special case, they are a consequence.** `propose_archive`
never assigns a band, so every archive option keeps the `Option` default `low`.
AUTO therefore never runs on a `.zip`; its door stays the Extract route.
`/bundles/create` refusing a zip auto is the honest reading of that, and nobody
should "fix" it by inventing a band in the client.

The proofs live in `curiarium/api/tests/test_bundles_api.py` (auto success, refusal
and idempotency; guided accepted-only metadata; a dismissed licence; an operator
cover; split -> arrange -> undo) and in
`curiarium/web/src/ui/review/GuidedImportPanel.test.tsx` plus `bundleRows.test.ts`
for the predicate.

## Build warnings in the web bundle

`npm run build` in `curiarium/web` is expected to emit exactly the warnings below,
nothing else. Anything else is new and needs a disposition before it ships.

| Warning | Disposition |
| --- | --- |
| `[baseline-browser-mapping] The data in this module is over two months old.` | Informational. Comes from a dev dependency used by the Browserslist toolchain; no application code is shipped from it. Update with `npm i baseline-browser-mapping@latest -D` when it gets noisy. |
| `Browserslist: browsers data (caniuse-lite) is 12 months old.` | Dev-time staleness notice from Browserslist; not shipped. The fix is `npx update-browserslist-db@latest`. |
| `[MODULE_LEVEL_DIRECTIVE] The semantics of the module level directive "use client" in "node_modules/lucide-react/dist/esm/context.mjs" may not be preserved when bundling.` and the same warning for `Icon.mjs`. | Accepted. A `"use client"` directive is meaningless in an ESM browser build; the only consumer is the Vite client bundle, which has no server/client boundary. Rolldown reports this with a code frame; Rollup printed the same condition as `Module level directives cause errors when bundled`. |
| `Module "path" has been externalized for browser compatibility, imported by ".../node_modules/occt-import-js/dist/occt-import-js.js"` (and the same for `crypto`). | Filtered by `build.rolldownOptions.onwarn` in `curiarium/web/vite.config.js`. `occt-import-js` ships an emscripten bundle that contains Node-only code paths for `path`/`crypto`; only its WASM entry point is used, and it runs inside a worker, so the externalized browser stubs are never touched at runtime. The filter matches only these two messages from that one importer and forwards every other warning to Vite's default handler. Under Rolldown the emitting plugin is identified by `warning.plugin` (`rolldown:vite-resolve`) and the `[plugin ...]` prefix is no longer part of `warning.message`, which the filter tolerates. |
| The Rolldown chunk-size warning for `assets/index-*.js`. | Known and accepted, and **not** caused by this toolchain: it fired under Vite 5 too. Measured after the Vite 8 upgrade (Mf-qj7j): entry `index-*.js` 628.03 kB, with `jsx-runtime-*.js` (26.28 kB) and `rolldown-runtime-*.js` (0.72 kB) split out alongside it; under Vite 5 the same entry was 670.57 kB. `build.chunkSizeWarningLimit` stays at 600 kB, so the warning keeps firing until the entry is actually split. To bring it back under the limit, split the entry rather than raising the limit. |

Three.js and the settings page are not in the entry chunk. `preview.tsx`
reaches the viewer and the snapshot renderer through `React.lazy(() => import(...))`,
and `App.tsx` does the same for `./settings/Settings`, so they build into their own
chunks (`ModelViewer-*.js`, `Settings-*.js`) and are fetched only when a 3D asset or
the Settings view is first rendered. Those chunks are deliberately **not**
`modulepreload`-ed: a preloaded split chunk still costs the same network transfer on
first load, so preloading it would only buy parse time while making every visitor pay
for three.js again. `dist/index.html` therefore carries no
`<link rel="modulepreload">` at all — if one appears, something started eagerly
importing the split modules again.

A rejected split chunk no longer blanks the page. `src/ui/ErrorBoundary.tsx` is
mounted inside `<main>` in `App.tsx` around the routed views (keyed by the active
view, so switching views recovers) and reused compactly around the two 3D
previews in `preview.tsx`, so a failed `ModelViewer-*.js` replaces one preview
cell or modal rather than the whole library view. A rejected lazy `import()`
renders a recoverable screen with "Reload app" instead of an empty shell; a
rejected lazy loader stays rejected for the page's lifetime, so the chunk path
offers no retry, while an ordinary render failure also offers "Try again".
"Reload app" goes through `src/lib/reload.ts`, whose `?reload=<now>`
parameter makes the document URL a fresh cache key: nginx serves `index.html` with
`no-cache` and the hashed `/assets/*` files with `immutable`, so the fresh
document names the current chunk graph. That cache-buster guarantees a fresh
document request; it cannot invalidate old hashed chunks (harmless — nothing
references them any more), and no client-side action helps if the server itself
still serves a stale `index.html`.

## Schema migration rules

- Add the next immutable `Migration` entry in `api/migrations.py`.
- Never rename or reuse an applied version.
- Add fresh, prior-version, repeated-run, failure, and restore-oriented tests as
  applicable.
- Do not test against or commit a user's runtime database.
- A new migration must record its version AND its name - the boot guard refuses a
  drift between the two - and every object it creates must be `IF NOT EXISTS`, so
  a re-apply is harmless. Its test must not pin the newest version to a literal;
  `MIGRATIONS[-1].version` is the form the neighbouring tests use.
- A release that changes schema must document its supported source versions and
  matched database/storage restore procedure.
