# Changelog

Notable changes to Curiarium. The product version lives in `curiarium/api/version.py`
and its mirrors are checked by `make verify-version`, so this file is a record rather
than a source of truth.

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
