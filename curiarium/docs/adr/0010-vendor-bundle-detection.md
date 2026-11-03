# ADR 0010: Vendor bundle detection, and creating one project per bundle

**Status:** Accepted

**Accepted:** 2026-11-01

**Implementation:** ticket `Mf-vwqf`.

**Extended:** ticket `Mf-h0p8` (R3 depth bound, container rule, create-time refusal).

**Extended:** ticket `Mf-0780` (markers merged per named pair, so detection no
longer depends on which of the two documents shipped which sentence).

**Extended:** ticket `Mf-1zmd` (an archive already in the library extracts in
place through `POST /import/archive`; the decision below is unchanged, the door
is no longer a URL fetch).

**Extended:** ticket `Mf-f3c2` (the report is confirmed from a REVIEW queue
rather than a Settings card, and the UI gate is the route's CONTRIBUTE threshold
instead of editor; every decision below is unchanged - see ADR 0011).

**Extended:** ticket `Mf-6b82` (an archive whose entries are all rooted at a
sibling directory pairs with that directory when the root is not a generic
container name; the stem rule is unchanged and keeps precedence).

## Context

A library filled by `rsync`, a mount import or a bulk unzip arrives the way the
vendor shipped it: one directory per thing, holding the model files, a preview
image or five, and the `README.txt`/`LICENSE.txt` (or `<name>_README` /
`<name>_LICENSE`) that say who made it and under what licence. Curiarium saw
those directories only as folders full of loose files. Nothing said "these
eleven files are one model by one person", so the provenance a Thingiverse page
states plainly - title, creator, licence, source URL, which image is the
preview - had to be retyped per asset, or was simply lost. The operator's
complaint was concrete: *"I have 300 bundles on disk and 300 empty projects to
make."*

Three facts about the existing system shaped the answer:

* **A project is a row set, not a directory.** `Project` and `ProjectAsset`
  describe a membership; the bytes belong to `Asset` rows, which belong to
  folders. So "create a project from this directory" is a database act over
  already-registered assets, not a file move.
* **`ready` means *registered*.** A project is only worth creating when its
  model assets are `Asset` rows a scan has already written - a cover needs an
  asset id, and membership needs asset ids. Extraction alone registers nothing.
* **Nothing in this product can hold a pending decision.** One process, one
  SQLite file (AGENTS.md, ADR 0006), no queue and no worker. A detection report
  that had to be *stored* before it could be confirmed would need a table, a
  migration, a staleness policy and a sweeper - for a screen the operator opens,
  reads and acts on within a minute.

The one thing the ticket could not decide from the filesystem alone is what a
directory *means*. Vendor archives are not a format: Thingiverse writes two
specific sentences, Printables writes different ones, and half the trees on a
real disk are somebody's own export. So the report has to be honest about how
sure it is, and the refusal to create has to be separate from that certainty -
otherwise one unparseable README makes a perfectly good bundle unusable.

## Decision

1. **Three rules, strongest first, in a pure module.**
   `curiarium/api/bundle_detect.py` holds every judgement and performs no I/O:
   no `Session`, no `engine`, no `open()`, no `Path.read_*`, no `os.scandir`.
   It is handed a `BundleSnapshot` (a listing of `BundleEntry(rel_path, size)`)
   and the handful of documents the caller actually opened, and returns
   `BundleFacts` or `None`. Each rule carries a detector name, a confidence and
   a metadata shape, and they are evaluated in this order:

   | # | Detector | Confidence | Shape | Fires when |
   |---|---|---|---|---|
   | R1 | `thingiverse-readme` | `high` | `root-docs` | the bundle root holds `README.txt` and/or `LICENSE.txt` - **at least one** of the two - and between them the pair carries both of Thingiverse's markers **in whichever document they were shipped**: `created by Thingiverse user <name>` with `licensed under <token>` on one side, `<title> by <creator> on Thingiverse: <url>` on the other |
   | R2 | `thingiverse-sibling-docs` | `high` | `sibling-docs` | the same pair shipped beside the files as `<bundle>_README` / `<bundle>_LICENSE` - as two files, or as two directories each holding one text file (the legacy flattening) - at least one of them present, and both markers present in whichever of the two documents holds them |
   | R3 | `generic-bundle` | `low` | `generic` | nothing parses, but the tree still looks like one thing: at least one model **at the bundle's own depth** - depth 0, depth 1 under `files/`, or depth 1 under exactly one model-bearing subdirectory - plus one image and one text entry |

   `high` therefore means *the vendor told us*, never *there is a `.txt` file*.
   A README that exists but lacks the markers is not R1: both high-confidence
   rules miss, and the bundle either falls through to R3 - low confidence, name
   only, every provenance field `None` - or is not a bundle at all. The title is
   the parsed licence title, else `display_name(bundle_name)` (a leading
   `^\d+[_-]+` ordinal dropped, underscores to spaces, whitespace collapsed, no
   dash rewriting); the creator is the created-by sentence's, the licence and
   its URL come from an explicit token table, and an unknown token is echoed
   back with **no** URL, because a guessed licence link is a legal statement we
   are not entitled to make. The description is the document that carried the
   created-by sentence, and it is `None` when nothing did. The cover is the
   largest image, ties broken by path.

   The model evidence for R3 is bounded to the bundle's own depth, in one of
   three shapes: a model at depth 0; a model at depth 1 under the named
   `R3_MODEL_DIRS` (`{"files"}`); or every depth-1 model under exactly one
   model-bearing subdirectory of any name - the "one folder in, models out"
   shape (`STL/`, a wrapped vendor zip's own directory). Two or more *unnamed*
   model-bearing subdirectories, with no model at depth 0 and none under
   `files/`, is a pile of things rather than a bundle - `files/a.stl` beside
   `other/b.stl` still counts, because the named allowance is applied first -
   and depth 2 or deeper is still refused. Cardinality, not a longer name list,
   is the bound: a conventional-name table only ever helps names someone thought
   of, while the count of unnamed model-bearing subdirectories separates one
   shipped thing from a pile. Measured on the owner's staging tree (131 directories + 208
   zips), the shipped rule offers 115 candidates (97 directories + 18 archives)
   and this rule offers 121 (+6 archives, 0 directories, 0 removals, 0 newly
   offered containers). The six are five Fates End towers (`01_Wizard`,
   `02_Fighter`, `04_Rogue`, `06_Ranger`, `10_Monk`) and `Articulated Flexi -
   Cinderwing3D - Pumpkinurtle`, each `<wrapper>/*.stl` plus 3-4 previews and
   one vendor `.txt`. Conventional-name widening alone (`stl/`, `models/`, ...)
   gains literally zero, so it was not adopted as a separate rule - the
   single-model-directory rule subsumes it. A depth-2 allowance is refused on
   `All Pokemon.zip`: 2 models out of 607 unrelated top-level folders and 2096
   images would be offered as ONE bundle. The three single-subdir directories in
   the corpus (`LittleTup`, `Taffi`, `GavinIsABot`) still fail R3, because each
   has zero images and zero text - the depth bound is not what excludes them.
   R1 and R2 are bounded by construction, because their marker documents live at
   depth 0; R3 counts roles, so an unbounded model count makes every ancestor of
   a bundle a candidate on its descendants' models. Images and text cannot
   supply the trio alone, and a real generic bundle keeps its previews in
   `images/`, so bounding them would only lose bundles. `counts`, `model_entries`
   and the cover stay folded totals, so every number on a row that *is* a bundle
   is unchanged. A model outside the allowed shapes fails closed - the directory
   is not offered rather than wrongly created - and `R3_MODEL_DIRS` is now only
   the named allowance: widening further means changing the cardinality rule,
   not adding a name.

2. **Classification is suffix-only, and never looks at a stem.**
   `classify_entry` maps an entry to `model` / `image` / `text` / `archive` /
   `other` from `PurePath(rel_path).suffix.lower()` alone. This is the Mf-tkdp
   lesson taken seriously: that ticket's identity bug derived display names from
   stems, so two files sharing a stem across extensions collapsed into one and
   **67 files were silently dropped** from a library. A role is a property of
   the path, not of the name before the dot: `a/part.stl` and `b/part.stl` are
   two entries with two roles because their paths differ. The model set is
   `config.IMPORT_ALLOWED_EXTS` minus `.zip` (an archive is a container, never a
   bundle member) and the image set is `library_scan.IMAGE_SUFFIXES` itself, not
   a second table - the scanner, the thumbnailer and this module must never
   disagree about what a thumbnail-able file is.

3. **No migration. The report is recomputed from disk on every read.**
   There is no candidate table, no cache and no stored detection state. A
   directory added by `rsync`, a folder renamed by hand, a zip dropped beside its
   twin: the very next `GET /bundles/detect` says so. `build_report` walks the
   storage root through `library_scan.iter_directories` (a documented public
   wrapper over the library's own `_walk_storage`, so a read-only caller outside
   that module reuses its pruning instead of importing an underscore name),
   builds each candidate's snapshot, reads at most four documents bounded by
   `BUNDLE_DOC_MAX_BYTES`, and resolves each candidate against the database. It
   writes nothing - no row, no file, no folder healing - which is what makes it
   safe on a `READ` route.

   Two pieces of state do survive, and neither needs a schema change:

   * **Dismissals** live in the existing `appconfig` key/value store, under the
     single key `bundle.dismissals` (`settings_service.BUNDLE_DISMISSALS_KEY`),
     as JSON `{key: {"at": iso, "by": username|null}}`. One key holds the whole
     map because a bundle key is a relative path: a row per bundle would be a
     working set nobody ever reads one row at a time.
   * **The project's own record** of what created it is a `_`-prefixed marker in
     the existing `Project.metadata_json`:
     `{"_bundle": {"key", "detector", "confidence", "rel_dir", "archive",
     "twin_verified"}}`. `Project.origin` keeps its closed
     `CHECK (origin IN ('own','imported'))` - a bundle-created project is
     `imported`, because it is - and no new column, no new enum value and no
     migration is involved. The underscore prefix is the existing convention for
     reserved keys, so the merge in `project_service.merge_project_marker` only
     ever writes `_`-prefixed keys and an operator's own metadata is untouched.

4. **The folder is the truth; a twin zip is provenance only.** When a directory
   and a `<name>.zip` sit side by side (`twin_key` casefolds the basename and
   strips exactly one trailing `.zip`), the directory is the bundle and the
   archive attaches no files. One bundle, one project, the folder's assets as
   members - never six members and never two projects for one thing. The archive
   is *reported* with `twin_verified`, which compares the casefolded **basenames**
   inside the zip with the casefolded basenames of the folder's recursive
   entries. Names only: no checksum, no byte comparison, no assertion. The
   archive attaches nothing, so a mismatch is information for the operator
   (`false`), not a failure to raise; and any read error is `false` too, because
   a `READ` route may not 500 over a corrupt zip.

   **Amended by `Mf-6b82`.** The stem rule is unchanged: casefolded basenames,
   unordered set equality, a mismatch reported as `false`. A non-stem archive now
   also pairs with a *same-parent* directory named by its own common entry root,
   and only when that root is not one of `files` / `images` / `models` / `stl` -
   in the owner's library 51 of the 57 measured entry-root matches were one of
   `files` / `images` / `models` / `stl`, most of them `files/`, so a rule without
   that guard pairs half the library to the wrong directory.
   Verification on this path is **containment**, not equality: every entry's
   bundle-relative path (casefolded) and *declared* size must be present in the
   directory's post-claim listing. A lived-in directory legitimately holds more
   than its archive, so equality would refuse the very pair this exists for; and
   the declared size is a zip header field, so this decides pairing and is never
   an integrity claim. `twin_verified` therefore means two things by path: on the
   stem path it is an operator remark (either answer is information), while on
   the entry-root path it is a *pairing condition* and is always true by
   construction - an archive that fails containment simply does not pair. The
   archive attaches nothing on either path. **Interim, until `Mf-t12a`:** a
   directory no detector offers (models and images, no text) is reported as one
   `blocked` / `archive_only` row keyed at the *directory*, so the pair is
   visible rather than the directory vanishing and the archive standing alone.
   That row reports `assets_total: 0` and carries the archive-only remedy copy,
   both wrong for a directory that may already hold registered assets, and it
   yields to the low-confidence folder candidate `Mf-t12a` will produce for the
   same key.

5. **Zip-only creation is deferred to `POST /import/zip` and then a library
   scan.** An archive with no extracted twin is still read - through
   `zip_service.read_zip_text_entry`, the one read path that never extracts,
   capped on the declared size *and* on the stream - so the report can show its
   title, creator, licence, source URL and cover. It is always `blocked` with
   `blocked_reason: "archive_only"` and creates nothing. That is the right door
   because `ready` requires a **registered** model asset: extracting a zip
   writes files, and only a scan turns files into `Asset` rows, so a create from
   an archive would otherwise have to extract *and* scan inside a write route
   that the operator thought was a project create. The existing import door
   already does both halves properly, with its own audit row (`asset.create` per
   extracted entry), its own entry listing and its own error reporting. The
   report's copy names the whole path - import the zip, let the scan run, re-run
   detection - and the bundle is then `ready` on its own.

   **Amended by `Mf-1zmd`.** `POST /import/zip` was the only door when this was
   written, and it starts from bytes the server just fetched, so an archive that
   arrived by `rsync` could never be extracted by the product. The door for that
   case is `POST /import/archive`: `GET /import/archive/entries` lists the
   archive in the library and the POST extracts it in place into `<parent>/<stem>`
   - exactly the pair `twin_key` already recognises - and the next scan registers
   it. The decision stands: creation is still from registered assets, never from
   an archive, and no detector changed; only the extraction no longer has to
   come from a URL.

6. **Confidence and creatability are orthogonal.** The report has a `status`
   (`ready` / `blocked`) with a `blocked_reason` (`unresolved_directory`,
   `not_scanned`, `archive_only`), and it is computed from the database, never
   from the rule that matched:

   * the folder is resolved by `bundle_service._folder_index`, which indexes
     every `Folder` row by its `storage_path` casefolded: a bundle is resolved
     only when a row's `storage_path` casefolds to the bundle key. The rows
     migrated from the flattened v3 schema carry `storage_path IS NULL`
     (Mf-um7x), so they can never match - **that single fact is the whole
     legacy refusal**;
   * `ready` also needs at least one *registered model* asset under the key, so
     a resolved folder a scan has not reached yet is `blocked`/`not_scanned`
     rather than a project with no members.

   So a legacy R2 bundle reports `detector: thingiverse-sibling-docs`,
   `confidence: high` **and** `status: blocked`. That combination is the point:
   the detection is good and the refusal is about the directory row. Heal the
   row (a rescan gives it its real `storage_path`) and the same bundle becomes
   creatable with no code change, because nothing keys on which rule fired. A
   create request recomputes the report and refuses a `blocked` key outright, so
   a stale client cannot create one either.

7. **Dismissals are keyed by the relative path, and a rename silently
   un-dismisses.** The key is the storage-root-relative directory (or the zip's
   relative path) - the same string the report shows as `key` and `rel_path` -
   because that is the identity a human sees and the one a create request
   carries. The cost is accepted and documented rather than hidden: rename the
   directory and the dismissal no longer matches, so the bundle reappears. At
   household scale that is a shrug, not an incident, and the report says it out
   loud: `stale_dismissals` lists every dismissal key that matches no live
   bundle, so a dismissal that has lost its target is visible instead of looking
   like a bundle that vanished.

8. **The dismissal map is one JSON value, read-modify-written, and that race is
   accepted.** `set_dismissals` reads the map, adds or removes the requested
   keys, writes it back, and audits each key that actually changed. Two
   concurrent dismiss requests can therefore lose an entry. Household scale, one
   operator, and every change is an `audit_events` row - so the accepted failure
   is recoverable by hand and visible in the log, which is a better trade than a
   table for a working set. `POST /bundles/dismiss` is a toggle
   (`{keys, dismissed}`), so a dismissal has a visible undo rather than a second
   route.

9. **Creation is audited, and that deliberately diverges from `POST /projects`
   being exempt.** `POST /projects` stays in `audit.EXEMPT_ROUTES` with its
   existing reason ("creates a project"): the object it creates can be destroyed
   later, and `project.delete` is what is recorded. `POST /bundles/create` is
   `bundle.create` (one row per created bundle, target = the bundle key, detail
   = project id, detector, confidence, asset counts, `name_only`) and
   `POST /bundles/dismiss` is `bundle.dismiss` (one row per key that actually
   changed). The divergence is deliberate, for two reasons a reviewer has to
   sign off on:

   * a bundle create is a **bulk, disk-derived act** - one confirmation can turn
     forty directories into forty projects, and the thing being recorded is not
     "a project row appeared" but "this directory on this disk became this
     project", which no other surface can say;
   * the event doubles as the operator's **creation history**: with no pending
     table and no migration, `GET /audit?action=bundle.create` is the only record
     of which bundles were confirmed, when, by whom, and which were created
     name-only. The `_bundle` marker on each project says what created *that*
     row; the audit row says what the operator did.

   Both are written with `audit.record`, not `audit.stage`: the effect is
   committed by shared services that own their sessions
   (`project_service.create_project`, `project_service.add_assets`), so there is
   no request session to join. That makes them fail-loud in the taxonomy's terms
   - the row is written in its own transaction immediately after the effect and
   a failure raises, after `log.exception`, rather than being swallowed.

10. **One project per bundle, and an exact casefolded name match is a skip, not a
    suffix.** `find_existing_project` asks, in this order: the `_bundle` marker
    key of any project, then `project_service.find_project_by_source` for the
    parsed Thingiverse URL, then an exact casefolded `Project.name` match. Any
    hit means `already_created`, the report says so before anyone presses
    create, and the create returns `skipped` with the existing project id and
    **never touches the row**. A human-edited project - renamed, re-described,
    with members added by hand - is therefore never overwritten by a re-run.

    This is deliberately *not* `_create_import_project`'s behaviour, which
    suffixes past a `NOCASE` collision (`Name`, `Name (2)`, ...) because an
    import that has already written its files must not fail on a name. A bundle
    create is the opposite situation: the owner asked for one project per
    bundle, nothing has been written when the name is chosen, and the second of
    two genuinely different bundles whose names de-underscore to the same string
    is better reported as a skip the operator can see than silently become
    `Moai statue (2)`. The typed name is still `_clean_name`d and still passes
    `idx_project_name_unique`; a 409 that slips past the pre-check (a typed name
    that *cleans* to an existing one) is caught and converted to a `skipped`
    with the reason "project name already exists" - never a 500, and never a
    half-created project.

11. **`library_scan.RESERVED_DIRS` already owns `bundles/`.** The scanner prunes
    the top-level names `thumbs`, `bundles` and `.incoming`, so a top-level
    `bundles/` directory is invisible to detection - which is correct, because
    that is the app-owned store for supporting files, not a place vendor bundles
    live. The word is now shared with the new `/bundles/*` routes, and that is
    the confusion this paragraph exists to head off: the routes are about
    *detected* vendor directories anywhere in the library, the reserved
    directory is an app-owned sibling of `thumbs/` and `.incoming/`. Detection
    inherits the pruning for free by walking through
    `library_scan.iter_directories`, along with hidden-name and symlinked-
    directory skipping.

12. **Three commits per created bundle, and the idempotency that makes the
    window safe.** One created bundle commits three times: the project row
    *with* its `_bundle` marker (one transaction, through
    `project_service.create_project(metadata=...)`, so a crash can never leave
    an unmapped project), then the member assets
    (`project_service.add_assets`), then the `bundle.create` audit row. Two
    shared services own their sessions, so this cannot be one transaction
    without refactoring them, and that is accepted rather than paid for. What
    makes it safe is the re-run: a crash between the windows leaves a project
    with a marker and no members, the next report says `already_created`, and
    the next create is a `skipped` carrying the existing id. Duplication is
    unreachable; a missing membership is visible and repairable by hand.

    A create request never trusts its own keys either: it recomputes the report
    first and looks every key up in the fresh rows, so a bundle that has turned
    `blocked` since the screen rendered creates nothing, and an unknown key is
    `failed` ("unknown bundle").

13. **The report is bounded, and says so.** The walk stops at
    `MAX_SCAN_ENTRIES` (200 000 entries) and each document read is capped at
    `BUNDLE_DOC_MAX_BYTES` (64 KiB) - on the declared size *and* on the stream
    for a zip member, because a crafted archive can lie about the former.
    `limit` (default 200, 1..1000) bounds the returned `bundles`; `truncated` is
    `true` when rows were withheld or the walk was cut short, and the counts
    still describe everything that was found, so a truncated page never reads as
    a smaller library. `path` scopes the walk, and every key stays
    storage-root-relative, so a scoped report and a whole-storage report name
    the same bundle and a create pass can find a row a scoped report showed.

14. **A directory that contains bundles is a container, not a bundle.**
    Claiming a child before its parent is not enough on its own: `import/` can
    hold a loose model plus an image and a text file at depth 0, so R3 accepts
    it on its *own* files even with the depth bound, and one confirmation would
    make a project named `import` that swallows the library - the exact failure
    the "never create a project you cannot attach assets to" constraint was
    written to prevent, inverted. So `iter_bundle_candidates` keeps the strict
    ancestors of every bundle it claims and skips a **low-confidence** candidate
    whose key is in that set, *without* claiming it, so the rejected container's
    own files still bubble up to a legitimate ancestor instead of being
    stripped. A low folder candidate that strictly contains a zip-only candidate
    is dropped as well once the archive pass has run. **High-confidence
    candidates are exempt**, and the exemption is a fact rather than a
    convenience: their documents sit at depth 0, so they provably attach only
    their own models - `test_a_parent_attaches_only_its_own_models` pins
    `assets_total == 13` while the project attaches 1 - and demoting one would
    silently lose a real bundle to a spurious subfolder. Low confidence is a
    guess, and a guess must never swallow a subtree, including a subtree made of
    other low candidates.

    The attach set is unchanged: a created project still attaches exactly the
    model files its own snapshot names. Independently of detection, `_create_one`
    refuses a low-confidence candidate whose registered assets under the key
    outnumber the assets its own snapshot names (`len(assets) > len(own_assets)`
    - that difference *is* another bundle's assets), returning `failed` with
    reason `contains_bundles` before `project_service.create_project` runs.
    Nothing is written and no half-created project can exist, because the guard
    precedes the project row. It is `failed`, never `blocked`: `blocked` keeps
    meaning database readiness (Mf-um7x), and a detection mistake must not
    masquerade as an unresolved `Folder`.

15. **A document says what it is; the filename is a tie-break.** Detection was
    originally keyed to file names: `README.txt` had to carry `created by
    Thingiverse user <name>` with `licensed under <token>`, and `LICENSE.txt`
    had to carry `<title> by <creator> on Thingiverse: <url>`. Real exports are
    under no such obligation: the two real directory shapes in the ticket differ
    only in which document carries which sentence, the contents swapped between
    them, and only **11 of 92** bundles parsed - the remaining 81 sitting at low
    confidence while carrying identical information in the other file. So the
    question worth asking was never "which file is this?" but "what does the
    text say?":

    * `_scan_doc(text)` returns the markers one document carries - the
      created-by creator and licence token from `_README_MARKERS`, and the
      title, title-line creator, URL and thing id from `_LICENSE_LINE` - and
      the filename is never consulted.
    * `_merge_pair(paths, docs)` folds one **named pair** in candidate order.
      The created-by creator wins outright and its document becomes the
      `description`; the title-line creator is a fallback **only** when the
      pair carries no created-by sentence anywhere, and that fallback has no
      created-by document, so `description` is `None`. Licence token, title and
      URL each take the first non-empty value seen, so path order is a
      tie-break and nothing is preferred because of the file it lived in;
      `thing_id` comes from the same match as the winning URL and may be
      `None`. The merge returns `None` unless creator **and** licence token
      **and** title **and** URL are all present, so a half-parsed pair is never
      promoted to high confidence.
    * Pair discovery is per named pair and needs only one member:
      `_root_pair_paths` takes whichever of `README.txt` / `LICENSE.txt`
      exists at depth 0 (**at least one**), and `_sibling_pair_paths` takes
      `<bundle>_README` / `<bundle>_LICENSE` in either the file form or the
      legacy one-text-file-directory form (**at least one**). One document
      carrying both markers is therefore R1 or R2 `high`, whichever name it
      shipped under.
    * Merging is scoped to a named pair only, so a generic `notes.txt` that
      happens to carry both markers stays `low` and keeps reporting
      `generic-bundle` / `generic` as its detector and shape. A stray prose
      file is not a document pair, and detection must not pretend it is.
    * `MAX_DOC_CANDIDATES` moved 3 -> 4. With two readable pairs the old bound
      cut the second sibling document (`README.txt`, `LICENSE.txt`,
      `X_README`, `X_LICENSE`), so the sibling merge could never complete;
      four is exactly the number of named marker documents.
    * Two shipped `_LICENSE_LINE` defects went with it. The `^` anchor plus
      `MULTILINE` let a marker sitting mid-line after another sentence leak
      that sentence into the title, and a non-greedy title split at the wrong
      `" by "` in a title that itself contains one. The line regex is now
      unanchored, not `MULTILINE`, with a **greedy** title, and the captured
      title is trimmed at its **last** `[.!?]\s+` sentence boundary - a bare dot
      is not a boundary, so `v1.2` survives. The cost is accepted rather than
      guessed around: a genuine title containing `". "` (`Dr. No Replica`)
      loses its leading part, which is indistinguishable from a sentence of
      prose sitting before the marker.

    This changes DETECTION, never READINESS. `status` and `blocked_reason`
    still come from the database alone - the resolved `Folder.storage_path` and
    the registered model assets - so detection cannot flip any bundle's
    `status`, and nothing about the Mf-um7x legacy refusal changes. The report's
    *row set* can change, which is the one consequence that is real: flipping a
    bundle from low to high changes tree *claiming*, not just values, because
    `iter_bundle_candidates` exempts high-confidence candidates from the Mf-h0p8
    container skip. The exemption stays sound for the reason it was argued
    there - a high bundle's marker documents sit at depth 0, so it attaches only
    its own models - and the container-rule tests are the regression signal for
    that.

## Consequences

* One directory per vendor bundle becomes one project, with creator, licence,
  licence URL, source URL, description excerpt and cover taken from the vendor's
  own documents, and its model files attached as members. The alternative -
  retyping 300 pages' worth of provenance - is what this replaces.
* The report is a dry run by construction: `GET /bundles/detect` writes nothing,
  so it can be opened, closed and reopened, and it is never stale because there
  is nothing to be stale.
* Two new `CONTRIBUTE` routes (`POST /bundles/create`, `POST /bundles/dismiss`)
  and one new `READ` route (`GET /bundles/detect`), so the matrix moves from 94
  routes to 97 (11 public, 34 read, 21 contribute, 13 write, 18 admin).
  `bundle.create` and `bundle.dismiss` join `audit.ACTIONS` (50 -> 52) and
  `audit.ROUTE_ACTIONS` (43 -> 45), and both boot guards -
  `permissions.assert_routes_classified` and `audit.assert_audit_coverage` -
  stay green.
* **A weak heuristic can be wrong in both directions.** R3 will call somebody's
  hand-made `stl+jpg+notes.txt` directory a bundle; a bundle with a README that
  lacks the markers is R3, not R1. The mitigation is not a better regex, it is
  the confidence badge, the name-only create, the per-row dismiss and the fact
  that a created project is an ordinary project an operator can delete, rename
  or fix.
* **A container directory is no longer offered, and that removes a capability.**
  A real "pile" directory that wraps several bundles can no longer become one
  project; restoring it would need an explicit override, which is a request or
  schema change and deliberately out of this ADR. A generic bundle whose models
  live two levels down, or under more than one model-bearing directory, is
  likewise no longer offered - it fails closed rather than wrongly created;
  a single model-bearing directory of any name is offered by the cardinality
  rule above.
* **A title containing a sentence boundary loses its leading part.** `Dr. No
  Replica` is reported as `No Replica`, because the trim that keeps a preceding
  sentence of prose out of the title is the same trim that eats the title. It is
  accepted and pinned as a known limitation
  (`test_a_title_containing_a_sentence_boundary_is_a_known_limitation`), not
  guessed around: the two shapes are byte-identical.
* **The live improvement is not a number this repository can assert.** The
  ticket's "81 of 92 become high" claim cannot be re-measured here - the live
  library is off limits, and a fixture is not that library. The evidence this
  ADR offers instead is shape parity: every marker-bearing layout that can be
  constructed - canonical root pair, swapped root pair, both markers in one
  document (newline-separated and mid-line), swapped sibling pair as files and
  as legacy directories - parses to the same facts whichever file holds which
  sentence. Anything stronger would be a claim about a disk we do not have.
* **The detection walk is a synchronous multi-thousand-stat walk on a `READ`
  route.** It is bounded by `MAX_SCAN_ENTRIES`, by the scan's own pruning, by
  the document caps and by `limit`, the client fetches once per card open and
  never polls, and `path` scopes it. It is still the most expensive `GET` in the
  product, and that is stated rather than hidden.
* **A dismissal is a path, so a rename un-dismisses** (accepted, surfaced as
  `stale_dismissals`) and **the dismissal map is a read-modify-write**
  (accepted at household scale, audited per changed key).
* **A zip-only bundle cannot be created from the report.** The operator is sent
  to `POST /import/zip` and a scan; that is a second gesture for a bundle that
  arrived as an archive, and it is the gesture that actually registers assets.
* Nothing here heals a folder row. A legacy bundle stays `blocked` until Mf-um7x
  (or a rescan) gives its `Folder` a `storage_path`, and the report is the
  surface that says which bundles are waiting on that.

## Alternatives considered

- **A `bundle_candidates` table, populated by a scan, confirmed later.**
  Rejected: it needs a migration, a staleness policy for rows whose directory
  has since moved, a sweeper for abandoned rows, and a second source of truth
  next to the disk. The report is a pure function of the tree and the rows, so
  the table would exist only to make the answer older.
- **A new `Project.origin` value (`'bundle'`) or a `Project.bundle_key`
  column.** Rejected: `origin`'s `CHECK (origin IN ('own','imported'))` is a
  closed vocabulary about *where the bytes came from*, and a bundle's bytes came
  from an import. Widening it would need a migration and would still not carry
  the detector, the confidence, the archive or `twin_verified`. The `_bundle`
  marker in the existing metadata object carries all of it, commits with the row,
  and is already the convention for reserved keys.
- **Extract the zip inside `POST /bundles/create`.** Rejected: a create route
  that also extracts and then scans is three routes in a trench coat, it would
  duplicate `POST /import/zip`'s entry listing, its per-entry error reporting and
  its `asset.create` rows, and a failure halfway through would leave files on
  disk with no rows. The existing door is better at every one of those.
- **Report a zip-only bundle as `ready` and create the project empty, to be
  filled after the import.** Rejected: an empty project is exactly the state the
  ticket exists to end, and `already_created` would then make the real create a
  skip - the bundle could never become a project with members.
- **Key the legacy refusal on the detector** (refuse R2, allow R1). Rejected:
  it hard-codes a guess about which rule implies which history. The refusal is
  about a `Folder` row with no `storage_path`, which is a fact; healing that row
  must be enough, and it is.
- **Suffix the project name on collision, like `_create_import_project`.**
  Rejected: nothing has been written yet when a bundle create picks its name, so
  there is nothing to lose by refusing; and two different bundles quietly
  becoming `Name` and `Name (2)` hides a decision the operator should make.
  A visible skip is the honest answer.
- **An `EXEMPT_ROUTES` entry for `POST /bundles/create`, for consistency with
  `POST /projects`.** Rejected: consistency with the exemption class is worth
  less than the record. A bulk, disk-derived create is precisely the act an
  operator later wants to reconstruct, and `project.delete` cannot say which
  directories were confirmed or which were created name-only.
- **A table for dismissals.** Rejected: a working set of tens of keys, never
  read one row at a time, and the one thing this product does not need is
  another migration for a UI toggle.
- **A byte-level `twin_verified` (checksums, hashes or decompressed bytes).**
  Rejected: the archive attaches nothing, so the check would exist to assert
  something nobody acts on, at the cost of hashing every byte of every twin zip
  on a `READ` route. **Re-scoped by `Mf-6b82`:** the entry-root fallback does
  compare the declared `ZipInfo.file_size` with the size on disk, which is not a
  byte comparison - it reads no bytes at all, and it decides pairing, not
  integrity. Comparing content would still be rejected for the reason above.

## Rollback

Revertible without a schema rollback: there is no migration. Reverting removes
the three routes, `bundle_detect`, `bundle_service`, the schemas, the two audit
actions, their `ROUTE_ACTIONS` entries and the settings key; the
`bundle.dismissals` appconfig row and the `_bundle` markers already written stay
behind as inert data (a marker is just metadata on a project, and the merge only
ever writes `_`-prefixed keys). Projects created through the report are ordinary
`Project` rows with `origin = 'imported'`, so they need no undoing - deleting
them is `DELETE /project/{project_id}` like any other, and their assets are
untouched by their membership being removed. Detection leaves nothing on disk to
clean up, because it never writes.

Operationally: an operator who preferred the old flow loses only the report
screen. The library, the scan, the import door and every `Asset` row are
unchanged.

## Verification

- `tests/test_bundle_detect.py` proves the pure layer from literals: the suffix
  table (including two files sharing a stem), the shared `IMAGE_SUFFIXES`
  constant, both parser shapes and their negative cases (a README with a licence
  marker but no creator invents no author), the licence table and its
  never-invent-a-URL rule, `display_name`'s ordinal stripping and dash
  preservation, `twin_key`, the cover pick and its tie-break, and the rule
  precedence - R1 before R2 before R3, and a marker-less README falling to
  `generic-bundle` / `low` with every enrichment field `None`. It also pins R3's
  depth bound: a `files/` trio is `generic-bundle` / `low`, the depth-0 trio is
  unchanged, a model under `stl/` is not a bundle and a model below depth 1 is
  not a bundle - and R1 still parses a bundle whose models live at depth 1 under
  `files/`.
- `tests/test_bundle_service.py` drives the whole contract through
  `TestClient(main.app)` against a temporary storage root and database: the Moai
  fixture (README, LICENSE, `images/*.jpg`, `files/*.stl`) reports one bundle,
  high confidence, `ready`, and becomes one project with creator, licence,
  source URL, cover and three members; the twin-zip fixture yields exactly one
  bundle, one project and three members (not six) with `twin_verified: true`, and
  a same-stem zip with different basenames yields `twin_verified: false` and
  still exactly one project; the marker-less bundle is created name-only with
  every provenance column `NULL`; a second run reports `already_created` and
  creates nothing; an edited project keeps its name and members; detection
  writes nothing (filesystem snapshot plus `Project`/`Asset`/`Folder`/`AssetFile`
  and audit row counts identical before and after); the legacy fixture with
  `_README`/`_LICENSE` directories and a `storage_path IS NULL` folder row is
  `high` **and** `blocked`/`unresolved_directory` and creates nothing, including
  through bulk name-only; the archive-only bundle is read from its entries and
  is `blocked`/`archive_only`; dismissing hides a bundle, undo brings it back
  and a stale key is reported; `limit` and `path` bound and scope the report; and
  the bounded zip reader reads a member without extracting, refuses a declared
  oversize member, rejects a traversal entry and answers `None` for a missing
  member or a bad archive. It also proves the container rule: on the replica of
  the live shape `import/` (with its own depth-0 trio and a residual model-only
  subdirectory) is not offered, neither is `import/ParentOf` nor the model-only
  directory, while the Moai bundle under `import/` stays `high` with
  `model_count == 3`; create-all over the offered rows makes no project named
  `import`, shares no asset between two projects and gives each project exactly
  its own model count; a low container of low-confidence children and a low
  container of a zip-only bundle are both rejected; and a direct `_create_one`
  call proves the `contains_bundles` refusal writes no `Project` row while an
  ordinary depth-0 trio still creates.
- The same two files now also pin the marker merge, because a detection keyed to
  file names silently loses a real bundle. In `tests/test_bundle_detect.py`: the
  canonical and the swapped root pair produce identical `creator`, `licence`,
  `licence_url`, `title`, `source_url`, `thing_id`, `detector` and
  `metadata_shape`; both markers in one document are `high`, both
  newline-separated and mid-line, with no preceding sentence leaking into the
  title; the swapped sibling pair is the same bundle in file form and in the
  legacy one-text-file-directory form; a named pair with no created-by sentence
  anywhere still parses `high` through the title line's creator **and reports no
  description**; an incomplete named pair stays `low` and invents nothing; a
  generic `notes.txt` carrying both markers stays `low` with
  `detector: generic-bundle`; a title that contains ` by ` keeps it; a version
  in the title survives a bare dot; and
  `test_all_four_named_documents_fit_the_candidate_cap` pins
  `MAX_DOC_CANDIDATES == 4` with the old cap of three as the starvation witness.
  In `tests/test_bundle_service.py`, end to end through
  `TestClient(main.app)`: a swapped bundle is `high` **and** `ready` and creates
  one project carrying the creator, licence, licence URL, source URL and the
  created-by document as its description; a swapped zip is `high` and
  `blocked`/`archive_only` and creates nothing; and a mixed layout completes
  only because the fourth document is read.
- The same two files also pin entry-root pairing (`Mf-6b82`). In
  `tests/test_bundle_detect.py`, from literals: `common_entry_root` returns the one
  top-level segment with its first-seen casing, and `None` for mixed roots, a
  top-level file, empty input and an all-`__MACOSX` archive, while ignoring a
  `__MACOSX/` prefix over real entries; `is_generic_container_root` accepts
  exactly `files`/`images`/`models`/`stl` casefolded and refuses
  `files_and_docs`, `my files`, `FileShare` and `17_ClockworkMonster`; and
  `archive_owned_facts` renders the 12-model, 2-render Clockwork listing as a
  `generic-bundle` / `low` generic row with the largest render as its cover and
  every provenance field `None`, while refusing a directory that holds text, that
  has no image, or whose only models sit at depth 2 (the `Mf-h0p8` bound still
  holds). In `tests/test_bundle_service.py`, against a temporary root: the real
  Clockwork pair - `import/17_ClockworkMonster/` (12 STL, 2 PNG, no text),
  `17_Fates End - ClockworkMonster Tower.zip` and its byte-identical ` (1)`
  duplicate - is exactly one row keyed at the directory, with
  `twin_verified: true` and the non-duplicate name as `archive.rel_path`, neither
  zip a row of its own, and `create` `blocked` with no project written; a generic
  archive rooted at `files/` beside a directory holding the same four payloads is
  refused - its own `blocked`/`archive_only` row, no row claiming it, and its
  directory still offered - while the Clockwork pair pairs in the same report,
  which is the test that discriminates; a constructed mini-library of ten pairs
  with identical contents reports `accepted=4, rejected=6`, so the name guard and
  not the containment check is what refuses the generic roots; a real bundle
  (models + images + `notes.txt`) beside an archive rooted at its directory but
  named differently is an ordinary folder bundle with `twin_verified: true`, and
  create attaches its two models and nothing from the archive; an archive that
  also carries `__MACOSX/` AppleDouble cruft still pairs on its real entries,
  because both layers skip it through the one `is_macosx_artifact` predicate; a
  pair-only directory holding a nested archive is still offered, so the verified
  pair is never a target of the strip; and a low container holding a paired
  directory still drops the container, so the `archive_only_keys` strip fires for
  a pair-only candidate too.
- `tests/test_bundles_api.py` covers the HTTP surface as a role sweep: a viewer
  can `GET /bundles/detect` (200) and both POSTs answer 403 with
  `permissions.DENY_DETAIL[CONTRIBUTE]`; the report body matches the pinned
  schema; create answers the per-item `created`/`skipped`/`blocked`/`failed`
  shape with a 200 on partial success, like `/assets/batch/*`; and an unknown key
  is `failed` rather than a 500.
- The permission sweep (`tests/test_permissions_matrix.py`) calls all three new
  routes at every role and in the auth-disabled mode and pins the matrix at 97
  with the 11/34/21/13/18 split; `tests/test_permissions_docs.py` pins the same
  size sentence and the three rows in `docs/PERMISSIONS.md`.
- The audit sweep (`tests/test_audit_log.py`) gains `POST /bundles/create` and
  `POST /bundles/dismiss` in its `CALLS` table and asserts their
  `bundle.create` / `bundle.dismiss` rows; `tests/test_snapshots.py` pins
  `len(audit.ACTIONS)` and `len(audit.ROUTE_ACTIONS)`, and the coverage sentence
  in `docs/AUDIT.md` moves with them.
- `tests/test_openapi_contract.py` pins the new schema components
  (`BundleReportOut`, `BundleDetectItemOut`, `BundleCreateRequest`,
  `BundleCreateResponse`, `BundleDismissRequest`, `BundleDismissResponse`, ...)
  and the three new paths.

## Superseding conditions

This ADR is superseded if any of the following becomes true:

- detection starts reading, writing or caching a stored candidate set instead of
  being recomputed from disk on each read;
- a migration appears for bundles - a candidate table, a `Project.bundle_key`
  column, a widened `origin` vocabulary or a dismissals table;
- `classify_entry` starts consulting a stem, or a second image-suffix table
  appears beside `library_scan.IMAGE_SUFFIXES`;
- the twin zip stops being provenance and starts attaching files, or
  `twin_verified` starts comparing content bytes - a checksum, a hash or
  decompressed data - instead of names and declared sizes;
- creatability starts keying on the detector or the confidence rather than on
  a resolved `Folder.storage_path` and a registered model asset;
- a zip-only bundle becomes creatable from the report without
  `POST /import/zip` and a scan;
- `POST /bundles/create` or `POST /bundles/dismiss` stops being audited, or
  `POST /projects` starts being audited (which would make the divergence
  unnecessary rather than deliberate);
- a name collision starts being suffixed instead of skipped;
- the dismissal key stops being the relative path, or `stale_dismissals` stops
  being reported;
- the report stops being bounded by `limit`, `truncated` or `MAX_SCAN_ENTRIES`.
- R3's model evidence stops being bounded to the bundle's own depth, or the
  bound widens past the three allowed shapes - depth 0, depth 1 under the named
  `R3_MODEL_DIRS`, or depth 1 under exactly one model-bearing subdirectory - or
  a candidate with two or more unnamed model-bearing subdirectories, and no
  depth-0 or named-directory model, starts qualifying on them;
- a directory that contains bundles starts being offered as a bundle again, or a
  high-confidence candidate starts being demoted for containing one;
- creation stops refusing a low candidate whose registered assets outnumber its
  own snapshot's, or that refusal starts reporting `blocked` instead of
  `failed`;
- detection starts preferring a document by its filename again, beyond the fixed
  tie-break order `_root_pair_paths` / `_sibling_pair_paths` establish;
- the pair merge stops requiring all four provenance fields before it reports
  high confidence, or a named-pair merge starts reaching outside its own pair.
