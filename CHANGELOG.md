# Changelog

Notable changes to Curiarium. The product version lives in `curiarium/api/version.py`
and its mirrors are checked by `make verify-version`, so this file is a record rather
than a source of truth.

## [5.2.1] — 2026-11-27

Corrective release: the detection engine is replaced, and the behaviour changes are
those corrections — not new capability.

Every number below is a measurement **on one reference library** (151 projects, ~2,800
assets, ~190 detected rows). No count is a promise about another installation: another
library has different junk, different duplicates and a different number of projects.
Behaviour is stated as behaviour.

### Changed

- **One detector.** `bundle_detect.py` is deleted (2,038 lines, 78 definitions) and
  `proposal_engine` now owns both the ranking and the facts — metadata, provenance,
  hierarchical groups, twin verification, cover selection and documents. The engine runs
  app-free and offline, and is scored outside the app by `curiarium/scripts/propose_import.py`
  against real trees and a committed label fixture. On the reference library it agrees with
  every confirmed label and returns identical answers for eight archetypes under renamed
  copies, which is what proves the rules are shape-based rather than name-based.
- **Import is a visible decision.** Every row lists the shapes that fit, recommended first,
  with counts, and `manual` is always available. High-confidence rows create in one click
  and land on the project; everything else opens a guided panel where the cover, the
  metadata source and every value parsed from a document (with its source) is yours to
  accept, edit or dismiss. A licence is never written without an explicit accept.
- **Arranging.** Preview → apply → undo, from a review row and from a project page, with
  moves shown as moves and a stale plan refused rather than re-planned silently.
- **Adopting.** A project whose folder grew can take on the new files: the preview names
  them by group, and adopting turns new subfolders into real groups. Undo removes exactly
  what it added.
- **PDFs are read.** `pypdf` mines document info and text; on the reference library this
  gained a fact for 14 of 160 rows (licence +8, source_url +11, description +11) and 34
  provenance candidates across 13 rows, for 0.67 s over the whole tree.
- **Uploads land in your folder**, not in a private per-file directory, and a filename that
  carries a path is honoured or refused — never flattened into underscores.
- **A checked-out repository is a project boundary.** The API image ships git; when git
  cannot read a checkout the row says so and is offered as one project, never split.

### Fixed

- git was missing from the API image, so repository detection was silently dead in
  production and a git project was split into its parts. CI could not see it because eight
  git tests skipped, and a skipped test is indistinguishable from a passing one. The image
  now ships git and a test that runs **in the image** reds if it disappears.
- `sanitize_filename` flattened `/` into `_`, so `Folder/file.stl` became `Folder_file.stl`
  and named a directory that no folder row described — the disk and the tree disagreed.
- A path already inside an offered row is no longer offered again; duplicates become
  members instead of invitations to fragment a product. Print output (a folder of `.gcode`)
  is not a project.
- `add_assets` / `remove_assets` / `_extend_project` bound whole member lists into one SQL
  `IN`; adopting chunks at 500, so a large adopt cannot fail on its own size.
- `.dockerignore` did not exclude `.venv`, so the build context depended on the builder's
  machine and could shadow the packages the image installs. Both image contexts are pinned
  by a test that plants files and asserts they are absent from the built image.
- A finished rescan refreshes the folder tree from every door, and only when it changed
  something.
- ADR 0010's stale mechanics are marked rather than rewritten (83 unmarked blocks → 0);
  superseded decisions stay on the record and Decision 18 is untouched.

### Upgrade notes

Schema 14, no migration. Auth-disabled deployments are unchanged. Nothing on disk is
rewritten by the upgrade: rescan to refresh the tree, then review and adopt as you please.
Arranging is opt-in and undoable.

## [5.2.0] — 2026-11-10

The first tagged release of the fork. Everything below landed between `v5.2.0-rc.1`
(2026-10-29) and this tag — 300 commits.

### Added

- **Bundle detection engine** — seven rules identify an importable product from what
  is on disk: a Thingiverse readme, sibling vendor documents, the model/image/text
  trio, an id in the directory name, a title-shaped name, a single-root archive, and
  compound multi-part bundles. Each report carries the rule set's version and stamp,
  and an append-only ledger names every path a rule declined and which rule refused it.
- **Extract and create** — an archive becomes a project from the entries it planned:
  the listing is read before a byte is written, members resolve by storage path, the
  project and its members are committed in one transaction, and a second run on the
  same archive answers "already created" instead of making a second project. Archives
  that are already extracted are creatable too — nothing is re-extracted or overwritten.
  "Extract and create" works on a selection: one project per archive, per-archive
  verdicts, one queue refresh.
- **Identity** — OIDC SSO (Cosmos Cloud and generic providers), TOTP MFA, session
  revocation on password change, admin password reset, and first-run admin setup
  (`AUTH_FIRST_RUN_SETUP`) for a deployment with no local account yet.
- **Access control** — every route classified in one permission matrix and every
  mutating route mapped to an audit action, with boot guards that refuse to start on
  an unclassified route or an unaudited mutation. Users and roles admin screen.
- **Storage safety** — snapshots through CephFS `.snap`, ZFS, a BTRFS sidecar or
  nothing at all; signed file grants; no-redirect IP-pinned HTTP for outbound fetches;
  uploads streamed straight to storage with no `/tmp` spool.
- **Review queue** — one queue per job, APG keyboard patterns (single tab stop listbox,
  roving tabindex, manual activation), drift notices instead of silent attachment, and
  a per-row reason whenever a row is blocked.
- **Operations** — multi-arch GHCR images, an API variant with the ZFS userspace
  (`-zfs`), Cosmos and TrueNAS deployment guides, a docs reachability guard, and a
  publish-time assertion that the image names the commit it was built from.

### Changed

- Folders are 1:1 with disk. Deletes go to trash, and restore is a copy-back into
  `recovered/` — never a revert in place.
- Zip entries keep their original filenames. Provenance beats tidiness; the archive's
  title names the project and nothing is prettified.
- **A row's status comes from disk**: it is creatable when its key resolves to a
  registered folder holding registered models, not because a particular detection
  branch produced it. Blocked reasons are explanations now, not gates.
- A project created from a bundle carries its **images** as members, so a cover is
  chosen from the archive's own previews instead of being added by hand.
- Creating a project anywhere — review queue, files view, bulk — updates the Projects
  list and lands you on the new project.
- One project dedupe across every creation door, so a project made from a bundle is
  visible to the archive door and vice versa.
- Purple is the default theme, with contrast enforced by a test rather than reviewed
  by eye.

### Fixed

- An extracted archive reported as *"zip only — extract the archive in place"* and
  impossible to import as a project, because the row described a directory as a zip.
- A verified twin's own payload directory losing its row, leaving nothing that owned
  the files.
- Rows titled after a nested archive's filename instead of the product it belongs to.
- Blank covers and blank cards on deployments whose base image ships no mime database.
- Duplicate projects when two creation doors examined the same files.
- A stale Projects list after creating a project from Review or the Files view.
- A selection bar whose only live action was metadata enrichment on rows that had no
  source page — and which then refused with a paragraph of causes the API never sent.
- The bulk bar offering "Extract" but not "Extract and create", so a multi-selection
  had to be walked through detection a second time.

### Upgrade notes

- Schema 14. Migrations run automatically at start; take a snapshot first anyway.
- Deployments with auth disabled keep working: `/health` reports `auth_required:false`,
  routes stay open, and `/login` answers 503.
- No manual data migration. Bundles detected by an older rule set are re-detected;
  refuse-and-explain is preferred to guessing.
