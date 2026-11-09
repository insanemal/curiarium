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

**Extended:** ticket `Mf-z4vv` (byte-identical archives are one bundle by content
hash, reported with reclaimable bytes and a copyable list; the copy is never a
second row and nothing is ever deleted).

**Extended:** ticket `Mf-74x5` (a `.zip` nested inside a detected bundle is a
supporting member, never a candidate, never a pairing and never evidence; its
entries are opaque and nothing is ever extracted).

**Extended:** ticket `Mf-it2a` (a verified pairing on either route - entry root
or stem - makes an offered-nowhere directory a `generic-bundle` / `low` offer
whatever else it holds, and both routes now decide containment through one
predicate; the stem rule's precedence and the entry-root rule's generic-root
refusal are unchanged).

**Extended:** ticket `Mf-ey4q` (a Thingiverse id in a bundle's name is provenance
at `medium` confidence - a `source_url` and nothing else - and every container
guard now asks whether a confidence is inferred rather than whether it is `low`).

**Extended:** ticket `Mf-hvck` (a fifth rule, R5 `title-name`, offers a folder of
models whose NAME reads like a title at `low`, as a `bundle_service` post-pass
rather than a `detect_bundle` branch; every provenance field stays `None`).

**Extended:** ticket `Mf-cf3c` (a fourth "already exists" identity - a bundle
whose own model files are already contained in one hand-made project is reported
as `already_created` and named on the wire by which identity matched).

**Extended:** ticket `Mf-9ygi` (every bundle report carries a derived rule-set stamp and every row its stamp; a dev-only JSON serialiser + `--diff` makes two runs comparable or explicitly not; every candidate not offered carries the rule that refused it - the refused ledger is reported, the offered set is unchanged).

**Extended:** ticket `Mf-tcrw` (a sixth rule, R6 `archive-bundle`, offers an ARCHIVE whose models and images sit under a single root entry directory with no text file at `low`, consulted only on the archive carrier after R1/R2/R4/R3 refused, so no row those rules produced can be lowered and the refusal vocabulary is unchanged).

**Extended:** 2026-11-05 - ticket `Mf-nivh` (the detector still attributes
nothing; an explicit operator action, `POST /bundles/enrich`, may fill ONLY an
empty creator/licence from the vendor's own page, with `fetched_at`, the URL and
the field list recorded on the row and in the project's `_bundle` marker, and a
page that states no licence leaves it null; enrichment never runs during
detection).

**Extended:** 2026-11-05 - ticket `Mf-au4k` (the report's own rule-set stamp and its
refused ledger are declared on the response model and served on `GET /bundles/detect`;
the per-row stamp stays off the row model on purpose, one rule set per report. The
decision is unchanged - `tests/test_bundles_api.py` now actually asserts the body
against the model, which is what makes the pre-existing "the report body matches the
pinned schema" claim in Verification true rather than aspirational).

**Extended:** 2026-11-05 - ticket `Mf-8xrc` (a seventh rule, R7 `compound-bundle`,
offers ONE row for a folder holding one product in several model-bearing parts,
carrying a group breakdown with any foreign group named, as a post-pass after
every other pass; it mints no refusal vocabulary, no row any other rule offered
is rewritten, and the low/name-derived rule absorbs only what its own shape
generalises - the full reasoning and the measurement are Decision 23).

**Extended:** 2026-11-05 - ticket `Mf-ao6r` (the create path of a compound row -
an excluded group is an ATTACH decision, the group is re-offered from the created
project's own marker, and the detection rule and its stamp are untouched).

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

1. **Seven rules, strongest first; four of them are `detect_bundle`'s order.**
   `curiarium/api/bundle_detect.py` holds every judgement and performs no I/O:
   no `Session`, no `engine`, no `open()`, no `Path.read_*`, no `os.scandir`.
   It is handed a `BundleSnapshot` (a listing of `BundleEntry(rel_path, size)`)
   and the handful of documents the caller actually opened, and returns
   `BundleFacts` or `None`. Each rule carries a detector name, a confidence and
   a metadata shape. `detect_bundle` evaluates four of the seven - **R1, R2, R4,
   R3**, in that order - and the first one that fires decides the row:

   | # | Detector | Confidence | Shape | Fires when |
   |---|---|---|---|---|
   | R1 | `thingiverse-readme` | `high` | `root-docs` | the bundle root holds `README.txt` and/or `LICENSE.txt` - **at least one** of the two - and between them the pair carries both of Thingiverse's markers **in whichever document they were shipped**: `created by Thingiverse user <name>` with `licensed under <token>` on one side, `<title> by <creator> on Thingiverse: <url>` on the other |
   | R2 | `thingiverse-sibling-docs` | `high` | `sibling-docs` | the same pair shipped beside the files as `<bundle>_README` / `<bundle>_LICENSE` - as two files, or as two directories each holding one text file (the legacy flattening) - at least one of them present, and both markers present in whichever of the two documents holds them |
   | R4 | `thingiverse-id-name` | `medium` | `id-name` | nothing parses, but the bundle's name starts with a Thingiverse id (`NNNNNNN_Title` - 4+ digits then `_`) and the tree holds at least one model **at the bundle's own depth** (the same bound R3 uses); the id is provenance inferred from the name, buying `source_url` / `thing_id` and nothing else |
   | R3 | `generic-bundle` | `low` | `generic` | nothing parses, but the tree still looks like one thing: at least one model **at the bundle's own depth** - depth 0, depth 1 under `files/`, or depth 1 under exactly one model-bearing subdirectory - plus one image and one text entry |
   | R6 | `archive-bundle` | `low` | `archive-root` | an ARCHIVE whose payload - at least one model and at least one image - sits under a SINGLE root entry directory and ships no text file |
   | R7 | `compound-bundle` | `low` | `compound` | a FOLDER holds one product in several model-bearing parts - no model at its own strict depth and at least two model-bearing groups sharing a family token (Leg A), or it holds models of its own, every group below it is foreign, its own models are the strict majority, and at least one group carries a vendor id (Leg B) - and it is carried as one row with a group breakdown; `detect_bundle` never reaches it |

   **R5 `title-name` / `low` is the fifth rule, and `detect_bundle` never
   reaches it.** Its judgement lives in the same pure module - `is_title_name`
   and `title_name_facts` - so it is testable from literals like the other six,
   but `bundle_service` calls it as a post-pass over the folder walk, after the
   archive promotion and before the `contained_keys` strip, never as a branch of
   `detect_bundle` (Decision 21). A branch there would inherit the deepest-first
   walk and hand a parent's row to its child. It fires on a name and one
   own-depth model and, uniquely, `TITLE_NAME_BUNDLES_ENABLED` - the one switch,
   read in one place (Decision 21) - which puts it below every document rule and
   outside the order above: `low`, `title-name`, every provenance field `None`.

   **R6 `archive-bundle` / `low` is the sixth rule, and it is archive-only.** A
   directory never reaches it: `bundle_service` calls `archive_bundle_decision`
   on the archive carrier alone. That function is a wrapper around
   `detect_bundle_decision`, so R6 is consulted **only after R1, R2, R4 and R3
   all refused** and can never re-report a HIGH (or medium, or R3 trio) row
   lower. A directory whose payload is the same shape stays refused exactly as
   it always was.

   **R7 `compound-bundle` / `low` is the seventh rule, and it runs after every
   other pass.** `compound_bundle_facts` lives in the same pure module, so it is
   testable from literals like the other six, but `bundle_service` calls it as a
   post-pass at the very END of `_build_candidates` - after the folder pass,
   pairing, the zip-only pass, the archive promotion, the R5 name pass and the
   `contained_keys` strip - and never as a branch of `detect_bundle`. The reason
   is R5's reason exactly: the folder walk is deepest-first, so a branch there
   would hand a compound parent's row to its first child and lose the row that
   child owns. It fires on a folder whose models sit only in subdirectories and
   share a family token, or on a folder whose own models outnumber one foreign
   group, and it mints **no** refusal vocabulary - `REFUSAL_RULES` is unchanged
   and there is no `compound_bundle_decision`, because a directory it declines
   already carries an earlier pass's reason. One switch,
   `COMPOUND_BUNDLES_ENABLED`, read only inside `compound_bundle_facts`; `low`,
   `compound-bundle`, `compound`, every provenance field `None`. Decision 23 has
   the clauses, the markers, the two legs and the measurement.

   The module lists seven rules and `detect_bundle` runs four, so the order R1,
   R2, R4, R3 is that function's and never the rule set's. R5, R6 and R7 are the
   three rules that fire outside it, and none of them can rewrite a row another
   produced.

   `high` therefore means *the vendor told us*, never *there is a `.txt` file*.
   A README that exists but lacks the markers is not R1: both high-confidence
   rules miss, and the bundle either falls through to R4, or to R3 - low
   confidence, name only, every provenance field `None` - or is not a bundle at
   all. The title is the parsed licence title, else `display_name(bundle_name)`
   (a leading `^\d+[_-]+` ordinal dropped, underscores to spaces, whitespace
   collapsed, no dash rewriting); the creator is the created-by sentence's, the
   licence and its URL come from an explicit token table, and an unknown token
   is echoed back with **no** URL, because a guessed licence link is a legal
   statement we are not entitled to make. The description is the document that
   carried the created-by sentence, and it is `None` when nothing did. The cover
   is the largest image, ties broken by path.

   R4 sits **before** R3 in that order, so an id-named directory that would also
   satisfy R3's trio reads `medium` and not `low`: an id inferred from the
   operator-visible name is strictly more than a shape guess, because it says
   which thing, on which site, this came from. `medium` therefore means *we
   inferred the provenance from the name the operator can see*, never *we read it
   in a document the vendor wrote* - and it is a confidence of its own rather
   than a shade of `low`, which is why the report counts it separately as
   `counts.medium`.

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
   not adding a name. R4 shares that bound *and* that computation - `detect_bundle`
   calls `_own_depth_model_count` once and both R4 and R3 gate on the result - so
   the one cardinality rule governs a name-only offer exactly as it governs a
   shape-only one.

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

   **Extended by Mf-ao6r.** The marker-derived re-offer is not a second piece of
   stored state and not an exception to "nothing is stored": this decision
   already names the `_bundle` marker as one of the two pieces of state that
   legitimately survive, beside dismissals. C2 reads a NEW fact out of that same
   marker - which groups a create recorded as excluded - and nothing else. No new
   table, no new `appconfig` key, no migration, and a project deleted takes its
   exclusion with it, because the only reader, `_compound_splits`, reads the
   marker that project's own row owns. Both directions of that reading are
   pinned: a marker written before C2 (six keys, no `groups`) re-offers nothing at
   all and leaves its absorbed group exactly where it was
   (`test_a_pre_c2_marker_re_offers_nothing`), and a marker that recorded an
   exclusion re-offers precisely the group it names and nothing else
   (`test_a_re_offered_group_creates_its_own_project_and_only_when_re_offered`).

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
   an integrity claim.

   **Amended by `Mf-it2a`.** A verified pair on *either* route now makes the
   directory an offer **whatever else it holds**. The archive is the identity and
   images and text are decoration: the old "models and images, no text"
   qualification is gone, so a directory of models alone, one whose extraction
   left a README behind, and one of models plus images are all the same
   `generic-bundle` / `low` candidate keyed at the *directory*, with that archive
   reported as its `archive` and nothing attached from it. It is creatable
   exactly like any other low row, because creatability is still a resolved
   `Folder` plus registered model assets and never a detector name. **A stem twin
   whose directory no rule offers is verified the same way** - through the same
   `archive_payload_contained` predicate - and joins the same promotion pass with
   no entry root; a stem twin whose directory some rule already detects keeps the
   names-only answer, because there the archive is that directory's provenance
   and never a pairing; and a stem twin that *fails* containment is consumed
   rather than retried under the entry-root name (Decision 19). Without a pairing
   the same shape stays silent by this rule (Decision 18), save for R5's separate
   name signal (Decision 21).

   `twin_verified` therefore no longer means "always true by construction" on a
   verified route: an entry-root pairing and a promoted or pair-only stem pairing
   are **re-verified at report time** by that same containment predicate, so a
   `false` there means the bytes on disk changed since the scan. Only the plain
   twin of a directory some rule detects keeps the names-only basename remark,
   where either answer is information for the operator.

5. **Duplicate archives are one bundle, and hashing never deletes.** Identity
   for an archive is its content hash: a sha256 over the file, read in bounded
   chunks. Two archives with the same hash are one bundle, so the copy is
   suppressed from both archive passes - it is never a second bundle row and can
   never pair a second directory - and reported in `duplicates` with its
   `reclaimable_bytes` and a copyable, tab-separated `duplicates_text` naming the
   same paths. Hashing is size-first: a byte-identical copy is necessarily
   equal-size, so only archives sharing a size with another archive are opened,
   and a uniquely-sized archive is never read; the worst case, a library where
   every archive is the same size, reads each archive exactly once. Nothing here
   hashes a zip's member entries: identity is over the archive files the walk
   finds, so a nested archive is still identified as a copy, but it is then
   removed from the reported group because a member of a live bundle is never
   offered for deletion (Decision 17). The report never deletes, renames or
   moves a file - it only renders text (`keep` and `duplicate` lines) an operator
   can act on themselves.

6. **Zip-only creation is deferred to `POST /import/zip` and then a library
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

7. **Confidence and creatability are orthogonal.** The report has a `status`
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

8. **Dismissals are keyed by the relative path, and a rename silently
   un-dismisses.** The key is the storage-root-relative directory (or the zip's
   relative path) - the same string the report shows as `key` and `rel_path` -
   because that is the identity a human sees and the one a create request
   carries. The cost is accepted and documented rather than hidden: rename the
   directory and the dismissal no longer matches, so the bundle reappears. At
   household scale that is a shrug, not an incident, and the report says it out
   loud: `stale_dismissals` lists every dismissal key that matches no live
   bundle, so a dismissal that has lost its target is visible instead of looking
   like a bundle that vanished.

9. **The dismissal map is one JSON value, read-modify-written, and that race is
   accepted.** `set_dismissals` reads the map, adds or removes the requested
   keys, writes it back, and audits each key that actually changed. Two
   concurrent dismiss requests can therefore lose an entry. Household scale, one
   operator, and every change is an `audit_events` row - so the accepted failure
   is recoverable by hand and visible in the log, which is a better trade than a
   table for a working set. `POST /bundles/dismiss` is a toggle
   (`{keys, dismissed}`), so a dismissal has a visible undo rather than a second
   route.

10. **Creation is audited, and that deliberately diverges from `POST /projects`
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

11. **One project per bundle, and an exact casefolded name match is a skip, not a
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

    **A fourth identity: a hand-made project that already holds the bundle's own
    files.** All three identities above ask what a project says about itself -
    its marker, its source URL, its name - and a project the owner made by hand
    over `17_ClockworkMonster/`'s files says none of the three. It has no
    `_bundle` marker, because the app did not create it; no source URL, because
    its origin is `own`; and its name, "Fates End - ClockworkMonster Tower", is
    not equal, casefolded or otherwise, to the folder-derived suggestion
    `ClockworkMonster`. Every older identity misses, the row is offered as new
    work and counts toward the pending badge, and a create produces a SECOND
    project over the same twelve files - the duplication one project per bundle
    exists to prevent. Asset containment is the only identity that can see such
    a project, because it is the only one that asks about the files rather than
    about the record.

    The rule is containment, and both halves of it are exact: the containment set
    must be non-empty, and every asset in it must be a member of the SAME
    project. Containment is not intersection - an asset with no membership
    contributes nothing to any project's count, so a project holding only some of
    the bundle's files can never reach the size of the set, and a large project
    that happens to hold one file cannot claim a bundle it does not represent. An
    empty containment set can never claim a bundle either, because the empty set
    is contained in every project and the first project in row order would then
    own every unscanned bundle in the library.

    **What the containment set is, stated because it is a reading and not a
    quotation.** The set is the models a create would actually attach - the
    `models` local `_report_row` builds and the `model_assets` `_create_one`
    attaches - and not every registered asset of the bundle. The ticket asks for
    "every registered asset", and this is a deliberate reading of that sentence:
    the app only ever attaches models to a project, so literal full-set
    containment over the images and the text as well would report the owner's own
    hand-made project as NOT owning the bundle it was made from, because a
    bundle's previews and licence files are registered as assets by the scan and
    never become project members. The acceptance criterion would then be false in
    production, in exactly the case that motivated it. The alternative reading is
    recorded in `## Alternatives considered`.

    **Which identity is reported, and why it is on the wire.** The lookup answers
    `ExistingProject(project, match)` with `match` one of `marker`,
    `source_url`, `name` or `assets`, served as `existing_project_match` beside
    the id and the name. The four are not equally certain: a marker is proof that
    the app made this project from this bundle, a source URL is strong evidence
    of the same origin, a name match is already a guess, and asset containment is
    strong evidence about the files that says nothing about intent - a human may
    have made that project for reasons of their own. The row copy therefore
    splits: an `assets` match reads "Already in <project> (matched by shared
    files)", and its nameless variant "Already in a project (matched by shared
    files) - its name is unavailable.", while the other three keep the flat
    "Already created as project X". The queue's `review-row-state` chip makes the
    same split in two words: an `assets` match chips "In project" and carries
    that sentence as its hover title, while the other three chip `Created`.
    `existing_project_match` is optional in the
    client's `BundleDetectItem` type on purpose: a payload from before this
    identity existed omits it, and `undefined` must mean "the server did not
    say", never `assets`.

    **Precedence, and why the marker stays cheapest.** The marker returns from
    inside the single `Project` loop, before the source-URL lookup, the name match
    or any membership read: it is proof, so it is also the first answer, and a
    marker match never pays for the index at all. The name match returns before
    the asset identity, and the asset identity is consulted last and lazily - a
    caller that did not prebuild the index gets one built for it, so a direct
    call to `find_existing_project` is still correct. The index itself,
    `project_assets_index`, is one statement for the whole report, read before
    any candidate is judged, so containment per row is a dictionary lookup and
    the cost does not move with the candidate count.

    **Determinism, because one asset may legitimately belong to two projects.**
    Ties break on `(lower(project.name), project.id)`, so row order can never
    decide which project owns a bundle, and that is the order the `Project` scan
    already uses, so the two answers agree. A membership row whose project row is
    missing is ignored, so a dangling row cannot claim a bundle.

    **The create door, not just the report.** `_create_one` computes the model
    set BEFORE it asks `find_existing_project`, so a `POST` that dodges the UI is
    refused as `skipped` / `already_created` carrying the existing project's id,
    rather than creating a second project over the same files. The report itself
    still writes nothing: the hand-made project is never stamped with a marker,
    which is what makes the identity a read and not a silent adoption.

    **Extended by Mf-ao6r.** The compound and the split need no new identity.
    `find_existing_project` is unchanged: the compound row is a marker hit on its
    own key, and a re-offered group is a marker hit on ITS key, because its create
    writes an ordinary six-key marker. A hand-made project over the group's own
    files still answers `assets`, which is
    `test_a_hand_made_project_over_the_mount_files_reports_already_created_by_assets`.

    The ordering of the create path is deliberate: exclusion validation
    (`_exclusion_prefixes`) runs before `find_existing_project`, so an
    already-created compound row whose group list moved under the operator answers
    `unknown_group` / `not_a_foreign_group` / `not_separable_group` rather than
    `already_created`. The failure this prevents is a request that changes nothing
    while the response reads as success; the cost is that a stale group name masks
    the idempotency answer, and that is a recorded tradeoff (residual 7 of
    Decision 24).

    The consequence of "never touches the row" is that a split is taken once. A
    compound created WITH a group included can never be split afterwards: a later
    create carrying an exclusion is `already_created` and the marker is not
    rewritten - `test_a_second_create_of_a_compound_row_is_already_created`
    asserts the project's `metadata_json` is byte-identical after a second create
    naming a different exclusion. The split is therefore a decision taken at the
    only moment it is free.

12. **`library_scan.RESERVED_DIRS` already owns `bundles/`.** The scanner prunes
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

13. **Three commits per created bundle, and the idempotency that makes the
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

14. **The report is bounded, and says so.** The walk stops at
    `MAX_SCAN_ENTRIES` (200 000 entries) and each document read is capped at
    `BUNDLE_DOC_MAX_BYTES` (64 KiB) - on the declared size *and* on the stream
    for a zip member, because a crafted archive can lie about the former.
    `limit` (default 200, 1..1000) bounds the returned `bundles`; `truncated` is
    `true` when rows were withheld or the walk was cut short, and the counts
    still describe everything that was found, so a truncated page never reads as
    a smaller library. `path` scopes the walk, and every key stays
    storage-root-relative, so a scoped report and a whole-storage report name
    the same bundle and a create pass can find a row a scoped report showed.

15. **A directory that contains bundles is a container, not a bundle.**
    Claiming a child before its parent is not enough on its own: `import/` can
    hold a loose model plus an image and a text file at depth 0, so R3 accepts
    it on its *own* files even with the depth bound, and one confirmation would
    make a project named `import` that swallows the library - the exact failure
    the "never create a project you cannot attach assets to" constraint was
    written to prevent, inverted. So `iter_bundle_candidates` keeps the strict
    ancestors of every bundle it claims and skips a candidate of **inferred
    confidence** - `low`, or the `medium` of an id in the name (Decision 20) -
    whose key is in that set, *without* claiming it, so the rejected container's
    own files still bubble up to a legitimate ancestor instead of being
    stripped. A folder candidate of inferred confidence that strictly contains a
    zip-only candidate is dropped as well once the archive pass has run.
    **High-confidence candidates are exempt**, and the exemption is a fact rather
    than a convenience: their documents sit at depth 0, so they provably attach
    only their own models - `test_a_parent_attaches_only_its_own_models` pins
    `assets_total == 13` while the project attaches 1 - and demoting one would
    silently lose a real bundle to a spurious subfolder. An inferred confidence
    is a guess, and a guess must never swallow a subtree, including a subtree
    made of other inferred candidates.

    The attach set is unchanged: a created project still attaches exactly the
    model files its own snapshot names. Independently of detection, `_create_one`
    refuses a candidate of inferred confidence whose registered assets under the
    key outnumber the assets its own snapshot names (the `len(assets) >
    len(own_assets)` difference *is* another bundle's assets), returning `failed`
    with reason `contains_bundles` before `project_service.create_project` runs.
    Nothing is written and no half-created project can exist, because the guard
    precedes the project row. It is `failed`, never `blocked`: `blocked` keeps
    meaning database readiness (Mf-um7x), and a detection mistake must not
    masquerade as an unresolved `Folder`.

    **R7 is this rule's ONE bounded exception, and it re-judges nothing.** A
    compound row is created by `_compound_pass` at the very end of
    `_build_candidates`, AFTER the `contained_keys` strip has run, so it is never
    itself judged by `iter_bundle_candidates` and never enters that set as a
    candidate the strip could drop. It never rewrites a verdict either: the pass
    skips any key that is already a row (`key in offered`) and any key that sits
    strictly inside one (`_strictly_inside_any`), both before a snapshot is even
    built, so a directory some rule offered keeps its row, its detector and its
    confidence untouched. Absorbing rows is the only thing it does to the row
    set, and **it may absorb a STRONG row only in the Leg B shape** - the shape
    where the folder holds models of its own and this rule is exactly what made
    them invisible (Decision 23). Both halves, and the LOW case the guard alone
    protects, are pinned by name:
    `test_service_a_bundle_that_is_already_offered_is_never_compounded` gives a
    `thingiverse-readme`/`high` parent every predicate R7 asks for and asserts it
    stays byte-for-byte as it was, and
    `test_service_an_offered_low_folder_is_never_recompounded` gives an OFFERED
    LOW row the Leg B shape, where the offered-key guard is the only thing
    protecting it, and
    `test_service_a_git_dump_keeps_its_separate_product_rows` asserts endorphin's
    eight rows survive with neither a compound row nor a rewritten one. So a
    refused container stays refused, and a compound row is an ordinary folder row
    like any other: `blocked` / `unresolved_directory` on the live library until
    its `Folder` resolves, creatable on exactly the same terms. **Reversal:** every
    row a compound absorbed is named `member-of-bundle` in the refusal ledger - the
    depth-1 ones additionally as a group on the row itself - and the verdict is
    recomputed from disk on every read, so removing or widening R7 is a rule-table
    edit plus its own witnesses. Nothing about it is stored, migrated or
    re-judged, and `contains_bundles` is untouched: a compound answers that belt
    because its snapshot is the whole subtree rather than a claimed-children view,
    not because it is exempt from it.

    **The ONE bounded exception above is now the first of two (`Mf-ao6r`).** A
    path the ledger names `member-of-bundle` may be OFFERED AGAIN only when a
    created project's own `_bundle` marker records that the operator excluded that
    group; the ledger itself is unchanged, the walk still names the path
    `member-of-bundle`, and the re-offer is a report-level act bounded by the
    markers in the database. Nothing an operator did not explicitly exclude is
    ever re-offered: an absorbed key with no marker behind it answers
    `unknown bundle`
    (`test_a_re_offered_group_creates_its_own_project_and_only_when_re_offered`),
    and the row list never names a directory the ledger also refuses
    (`test_no_directory_is_both_a_row_and_a_refusal`).

    **The `contains_bundles` sentence above gets its FIRST WITNESS here.**
    `test_a_registered_asset_that_vanished_from_the_disk_still_answers_the_belt`
    registers the tree, deletes one model from disk after registration, and the
    create answers `failed` / `contains_bundles` with zero projects - which is
    only true while `assets` and `own_assets` stay unfiltered by the exclusion.
    Filter either by the excluded prefixes and every excluded create reads as a
    container; exempt the compound row from the belt and the witness creates a
    project. That is the sense in which a compound answers the belt because its
    snapshot is the whole subtree rather than a claimed-children view, not because
    it is exempt from it.

16. **A document says what it is; the filename is a tie-break.** Detection was
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

17. **A nested archive is a member, never a candidate, and never evidence.**
    A `.zip` strictly below a detected bundle's directory belongs to that
    bundle. It is counted as an `archive` and nothing else: it never becomes a
    second row, never pairs a sibling directory through its entry root, and
    never supplies a model, image or text that would make the enclosing
    directory a bundle. `bundle_detect.is_bundle_member` is the one predicate;
    membership is strict and raw, so `<name>.zip` beside `<name>/` is the
    directory's twin (Decision 4), not one of its files. **The rule is opaque:
    the archive's entries are never read as evidence for the enclosing bundle.**
    Reading a central directory per bundle per report would be bounded and
    cheap, and extracting is never allowed, so cost is not the reason. The
    reason is correctness: the Clockwork directory's own twelve STLs already
    are the bundle's models, and counting the same payload again inside
    `Clockwork_v1.8.zip` would double the bundle's model count; and a directory
    whose only payload is a nested archive must NOT become a bundle, which is
    this ticket's acceptance. A directory that really is only a zip's contents
    has a correct path: `POST /import/archive` extracts in place (Decision 6),
    the next scan registers the models, and they then count as the directory's
    own. The member filter runs AFTER the archive-only container strip on
    purpose: a LOW folder that holds only a zip-only bundle is a container, not
    a bundle, so that zip is not "inside a detected bundle" and stays its own
    row. **The strip exempts a paired LOW candidate (Decision 18).** The Clockwork
    directory is a real LOW candidate, and its own nested zip is archive-only at
    strip time, so a strip that treated it as any other LOW row would drop the
    directory and leave the zip as the only row - this decision inverted. A
    promoted candidate is therefore never a strip *target*, on the same
    justification that already exempts HIGH and `pair_only` rows: it is verified
    evidence, and the archive's membership does not depend on the row. It does
    not swallow a subtree either - a guess that *contains* a promoted candidate is
    still dropped below - so the exemption costs no container. The member filter
    then removes the zip, because the directory it now belongs to is one
    `bundle_dirs` names, and it also prunes a promotion whose own pairing archive
    turned out to be a member of another bundle.
    `test_service_a_nested_archive_is_a_member_not_a_second_bundle` pins it.
    Duplicate identity still sees a member (its bytes are bytes the walk finds),
    but a member is never on the deletable side: it is removed from the group,
    and a group left with fewer than two non-member paths disappears. The
    residual is honest: an outside copy whose only twin is a member is either
    still offered as its own archive bundle (when it was the keep) or was
    already suppressed (when it was not). The report never invites deleting a
    file that is part of a live bundle, and nothing is extracted or written.

18. **A verified archive makes an offered-nowhere directory a low bundle, and
    only that directory.** A directory no detector offers on its own, but for
    which a pairing has verified that an archive beside it is *this* directory's
    provenance, is offered at `generic-bundle` / `low`. The qualification is that
    verified pairing - on the entry-root route (`Mf-6b82`) or the stem route
    (`Mf-it2a`) - together with at least one model at the bundle's own depth in
    one of R3's three shapes (`_own_depth_model_count`); the archive supplies the
    identity the listing cannot, and the confidence badge says the shape was
    inferred rather than read. What the directory holds beside its models no
    longer matters: images and text are neither required nor disqualifying,
    because a verified archive already proved what the directory is. Every
    provenance field the project would carry (creator, licence, source URL,
    description) stays `None`, because an entry root or a stem is evidence of
    which thing this is, never of who made it; the row's `archive` field is the
    identity, not a claim about authorship.

    The own-depth/cardinality bound is now the *only* shape guard, and it is
    load-bearing: the archive says WHICH thing a directory is, never that a pile
    of things is one thing. It is reused deliberately, so the `Mf-h0p8` depth
    bound and the `Mf-snd8` cardinality rule apply here for free - a pile of two
    or more unnamed model-bearing subdirectories, or models buried at depth 2,
    is refused even when its archive verifies, and an ancestor can never qualify
    on its children's models. Without a pairing the same shape stays silent,
    because a pile of models and a render is also what a half-extracted import
    looks like - that is the reason *this* rule does not fire, and it is aimed at
    this rule alone.

    **Amended by `Mf-hvck`.** R5 is a separate signal and does not loosen this
    shape rule (Decision 21). A folder whose NAME reads like a title a person
    chose is offered on that name at `low` even when its tree is nothing but
    models and a render; it still needs one model at its own depth, and it is
    still refused when it holds a bundle, sits inside one, or is already a row.
    This is the first of the two sentences that rule falsified - the other is the
    residual-silence paragraph in Decision 20.

    The container rule still wins: a directory that *holds* bundles keeps the
    blocked `archive_only` row and creates nothing, so a pile of models with one
    zip somewhere inside it never qualifies. And a pairing qualifies the
    directory it names and no descendant. The two orders cover the containment
    cases: a containing guess loses to the promoted candidate (the strip drops
    it), and a promoted candidate inside a bundle never becomes a row at all,
    because the archive it paired with is a member of that bundle and the member
    filter prunes both. Two further bounds come from the stem route: a stem twin
    of a directory some rule already detects keeps the names-only answer and is
    never promoted, because there the archive is that directory's provenance;
    and a stem twin that fails containment is consumed rather than retried under
    the entry-root name, so a wrapper the operator already named is not
    re-litigated by a weaker guess (Decision 19).

    Both failure modes are stated plainly rather than argued away: a false
    positive invites a project for something that is not a bundle and attaches
    its models to it; a false negative leaves the owner hand-making a project,
    which is exactly what happened for the Clockwork directory. Duplicate
    suppression (Decision 5) stays upstream, so a byte-identical copy can never
    promote a second bundle. The fixture-built numbers below belong to the
    fixtures; the owner's library figure is his measurement, not this
    repository's.

    **Amended by `Mf-v3i3`: a promoted row never swallows a stronger candidate,
    on an owner decision of 2026-11-04.** The paragraph above claimed that "the
    container rule still wins ... so a pile of models with one zip somewhere
    inside it never qualifies". That held for a directory holding a *claimed*
    bundle, and it did not hold for an archive-only child: the promotion pass
    runs after the zip-only pass, and its only refusals were an existing row for
    the same key and `claimed_ancestors`, which only the folder pass populates.
    An archive that is a row of its own was therefore invisible to it, and the
    member filter then deleted that row as a member of the promoted directory.
    The directory was offered, the real bundle inside it vanished, and the
    report hid the thing the operator had on disk. That is the superseding
    condition this amendment closes.

    The owner's ruling, with his reason: he sometimes extracts a file and copies
    the zip in beside it, so such a parent is a container he made by hand and
    the child is the real bundle. Prefer the child. The rule as shipped refuses
    the promotion when the directory's subtree holds a candidate whose
    confidence is in `bundle_detect.STRONG_CONFIDENCES` - `medium` or `high`,
    i.e. a candidate a *stronger* rule produced - and the parent then gets **no
    row at all**. Explicitly not a `pair_only` row: a pair-only key joins
    `bundle_dirs`, so `_nested_archives` would mark the strong child a member
    and the member filter would delete it, which is the same swallow by another
    route.

    The residual, stated fully rather than argued away: the parent is silent.
    The refused pair's archive attaches to no row - the stem or entry-root route
    already consumed it as a pairing, and only a row can carry a pairing as
    provenance - and it is not treated as a member either, so it appears nowhere
    in the report. That is the silence a consumed pairing gets when the directory
    it named is not offered, and it is deliberately not the treatment an unpaired
    archive gets: an unpaired archive is offered as a row of its own whenever it
    detects. The child keeps its `blocked` / `archive_only` row, whose remedy is the
    import door (`POST /import/zip` or `/import/archive`, Decision 6), and no
    creatability rule changes: creation still needs a resolved `Folder` and
    registered models, and an archive row is never creatable from the report.

    This is a confidence comparison, not a shape test, and **the Clockwork
    directory is untouched**. Its own nested `Clockwork_v1.8.zip` holds a model,
    a render and a text, so it is itself a LOW `generic-bundle` candidate - a
    member-strength candidate, not a stronger one - and the promotion therefore
    still fires, the directory still owns the row and the zip is still removed
    as a member. Decision 17's carve-out is the reason the discriminator is a
    confidence comparison rather than "contains a zip", and Decision 17 is left
    exactly as written.

    The bound is on the promotion, not on the ordering: the refusal sits after
    the `claimed_ancestors` branch deliberately, so a directory holding a
    *claimed* child keeps its blocked `archive_only` row - the container rule at
    its existing site - and both the pile row and the child stay visible. One
    honesty note: the swallow also corrupted duplicate reporting, because
    `_without_nested_members` removed a duplicate group containing a row that
    should not have been a member; the reachable shapes now report the group
    again.

    `test_service_a_promoted_row_does_not_swallow_a_nested_high_bundle` and
    `test_service_a_promoted_row_does_not_swallow_a_nested_medium_bundle` pin
    it; `test_service_a_promoted_row_still_wins_over_a_nested_low_member` and
    `test_service_a_pile_with_a_claimed_child_and_a_verified_twin_keeps_its_blocked_row`
    bound the other side. The Decision 17 witnesses -
    `test_service_an_archive_pairs_with_the_directory_its_entries_are_rooted_at`
    and `test_service_a_nested_archive_is_a_member_not_a_second_bundle` - must
    keep passing (the ticket's quoted
    `test_service_the_clockwork_pair_is_one_low_bundle` does not exist; those
    are the real symbols). The mutation that reproduces the bug is deleting the
    new refusal.

19. **Two routes, one containment predicate.** The directory an archive belongs
    to is decided on two routes: the **entry-root route**, where the archive's
    own common entry root names a same-parent directory, and the **stem route**,
    where a `<name>.zip` beside `<name>/` states the same thing by filename. Both
    routes decide whether the payload fits through **one** predicate,
    `bundle_detect.archive_payload_contained`, so they cannot disagree about what
    an archive's payload is or about when it fits a folder.

    **The wrapper is decided once, for the whole archive.** With an entry root
    the predicate is the strict strip-and-compare it always was: every entry's
    first segment must match the named root, and there is deliberately no
    identity shortcut on that route, because trying one would silently widen the
    rule that produced the root. With no entry root - the stem route, where
    nobody has named the wrapper - identity is tried first (the archive's paths
    are already the folder's paths), else exactly **one** `common_entry_root`
    strip. Never per entry and never two strips: `a/files/x.stl` must not verify
    against a directory holding `x.stl`, because that would pair a wrapper's
    wrapper with anything nested one level deeper.

    **One `__MACOSX` predicate.** Both routes drop AppleDouble cruft through
    `is_macosx_artifact`, the same predicate `common_entry_root` applies, so the
    two layers agree on what the payload is and an archive of cruft alone has
    verified nothing.

    **`twin_verified` per route.** An entry-root pairing and a promoted or
    pair-only stem pairing are **re-verified at report time** through that same
    predicate, so a `false` there means the bytes on disk changed since the scan -
    the old "always true by construction" claim is gone. A plain twin of a
    directory some rule detects keeps the names-only basename comparison as an
    operator remark, where either answer is information.

    **Stem first, and a detected twin keeps the old answer.** The stem rule has
    first refusal. When its directory is already a detected candidate the pairing
    is skipped exactly as before: the archive is that directory's provenance,
    never a pairing. When no rule offers that directory, the stem twin is
    verified by containment and joins the same `pairs` map with no entry root, so
    the promotion pass offers the directory at `generic-bundle` / `low` exactly
    as it does for the entry-root route. **A stem twin that fails verification is
    consumed**: it does not fall through to the entry-root pass, because the stem
    is the operator's own statement of which directory the archive is, and a
    failed check must never be retried under a weaker name.

    **The entry-root route and its generic-root refusal are untouched.**
    `files` / `images` / `models` / `stl` remain refusals for the route that
    *uses* the root to name a directory - which is precisely why the stem route
    is the one that had to grow, as the measured fault shows.

    **The fault, per bucket.** On the owner's library (2026-11-03), 39 of 131
    directories in `import/` were offered by nothing. Three buckets among them -
    18 directories in all - were one root-cause family: a directory with a
    verified archive twin that the pairing rules refused to promote.

    * Models only, nothing else, twin zip - **6 directories** (`SnVB623`,
      `SnVD623`, `Creality__Ender_3__Filament_holder`): the fault was
      `archive_owned_facts`' `counts.image < 1` leg. R3's image role was demanded
      of a directory whose archive already proved what it is.
    * Models and text, no images, twin zip - **8 directories**
      (`ultimate-card-box-v4-model_files`, `razer-cat-ears-remix-model_files`):
      the same line's `counts.text` leg. A README that shipped with the
      extraction disqualified the directory from the "no text" offer.
    * Models and images, twin zip - **4 directories** (`Cinderwing3D - Crystal
      Dragon`): **not** the predicate. Two independent refusals.
      `_build_candidates`' stem first refusal found the twin directory and
      `continue`d before the entry-root pass could ever see it, and
      `archive_owned` was only ever set by the entry-root pass - so a directory
      carrying BOTH models and images, exactly the shape the predicate was
      written for, produced no row at all. The second, independent refusal is
      the generic-root exclusion (`is_generic_container_root`): these archives are
      rooted at `files/`, so even with the stem refusal out of the way the
      entry-root route refuses them. `Cinderwing3D - Crystal Dragon` is the whole
      story in one directory: it had models and images, so had the pairing fired
      `Mf-t12a` would already have promoted it; its archive is rooted at `files/`,
      so the entry root could never name it; and its stem was consumed before any
      pass could verify it.

20. **A Thingiverse id in the name is provenance, and it is the only provenance
    a name can buy.** R4 reads `^(\d{4,})_` off the name and buys exactly one
    thing with it: a `source_url`, and the `thing_id` derived back out of that
    URL. Every other provenance field stays `None` - by construction, not by
    discipline.

    **The fault, measured.** On the owner's live library (131 top-level
    directories, 8 of them named with a leading numeric id), 7 of the 8 are
    detected and one is not: `5390184_ARTICULATED_ice_dragon__fixed_/`, holding a
    single `files/ARTICULATED_ice_dragon_fixed_p1.stl`. It was invisible for a
    *structural* reason, not a conservative one. R1 and R2 need a README or a
    LICENSE document; R3 needs the model+image+text trio; the archive-pairing
    rules need an archive it has no twin of - so every rule looks for evidence
    this folder does not carry, except the one signal it does carry. `NNNNNNN_Title`
    is the Thingiverse export convention, not a coincidence: the zip the operator
    extracted roots its own entries at exactly that name, which is why the id can
    be read as provenance at all. R4 reads it and adds exactly one row on that
    library. Mutation: delete the R4 block from `detect_bundle` and that snapshot
    returns `None` - with no image and no text, R3 cannot fire either - so the
    report comes back empty and every assertion in the witness reds.

    **The bound is the regex, and each bound is load-bearing.** `^(\d{4,})_` is
    three decisions in seven characters, and each one has a named mutation and a
    witness name. A digit floor: widening `\d{4,}` to `\d+` admits `539_x`, a
    part number and not an export. A mandatory separator: making the `_` optional
    (`_?`) admits `2024-10-31_backup`, so every dated backup directory would claim
    a source URL. An anchor: the killable mutant is the *combined* unanchor - drop
    the `^` **and** switch `.match` for `.search` - which admits
    `my_5390184_thing`, the operator's own naming that merely quotes an id.
    Dropping the `^` alone and switching to `.search` with the `^` kept are both
    provable no-ops (`.match` already anchors at position 0, and `^` already
    anchors inside `.search`), so a test that mutated only one of the two would
    prove nothing - which is why the table names all seven strings rather than
    only the three that fail.

    **Both lookalikes are decided explicitly, and neither is an accident of the
    regex.** `2024_parts` **is** offered, at `medium`, with `source_url`
    `https://www.thingiverse.com/thing:2024`: a four-digit run followed by `_` is
    the export shape, and the cost of that false positive is one row a human can
    dismiss or fix. The residual risk is stated rather than hidden -
    `find_existing_project` compares the normalised `url_utils.source_key`, not
    the raw URL, so a genuine project imported from `thing:2024` can make this row
    read `already_created`. That is visible, reversible, and never a write.
    `2024-10-31_backup` is **not** offered, because the separator must be `_` and
    a date-led backup name is not an export. `my_5390184_thing` is refused for
    want of an anchor, `539_x` for want of a digit floor, `606cdf0d5199b_thing`
    because its leading run is not all digits, and `ARTICULATED_ice_5390184`
    because the id is neither at the start nor followed by a separator.

    **Creator and licence stay null, always.** The id says where a thing came
    from, never who made it or what it may be reused for. A guessed licence is a
    legal statement we are not entitled to make: `cc-nc` forbids the commercial
    use `cc-sa` allows, so the two differ materially and neither may be assumed
    from a numeric prefix. Inheriting a creator or a licence from a sibling bundle
    would be worse than guessing - it would launder a guess into an attribution.
    `_name_only_facts` therefore has **no parameter** that could set them, and the
    project is deliberately left visibly unattributed so that a human supplies
    it. The one thing the id may carry is `source_url` / `thing_id`, and
    `thing_id` is read *back* out of `source_url` through the shipped
    `import_resolvers.thingiverse_thing_id_from_url`, so the URL and the id cannot
    diverge - `thingiverse_thing_id_from_url(source_url) == thing_id` by
    construction rather than by two code paths agreeing. Mutation: stamp a creator
    or a licence onto the row and the `None` assertions in both witness tests red.

    **Extended:** 2026-11-05 - ticket `Mf-nivh`: the detector still attributes
    nothing; an explicit operator action fills only an empty creator/licence - see
    the `Mf-nivh` entry in the Extended log at the top of this ADR.

    **R4 outranks R3 in the order, and never competes with R1 or R2.** An
    id-named directory that also satisfies R3's model+image+text trio must read
    `medium`: the id is a fact about the operator-visible name, the trio is a fact
    about the tree, and the name is the more specific statement of which thing
    this is. Both high rules still win outright, and their parsed `thing_id` is
    the document's, never the name's - a bundle whose README names `2493386` and
    whose directory is called `5390184_...` reports `2493386`. Mutations: move the
    R4 block after R3 and an id-named trio comes back `low` / `generic`; move it
    before R1 and the same bundle comes back `medium` carrying the wrong id. Both
    reds are pinned.

    **R4 always beats the archive-promotion pass, because both gate on the same
    predicate.** The promotion pass skips any key already in `found`, and R4's own
    gate is the same `_own_depth_model_count(snapshot) >= 1` bound, so an id-named
    directory is *always* R4-detected and *never* promoted: one row, and its stem
    twin is attached as provenance instead of being a pairing. Mutation: drop the
    `twin in detected` skip on the pairing pass **and** the `found` skip on the
    promotion pass and a second, `archive_owned` candidate appears for the same
    key; removing either skip alone changes nothing, because the other still
    protects the row.

    The measured consequence is in `twin_verified`. A plain stem twin of a
    directory some rule detects keeps the **names-only** basename comparison as an
    operator remark (Decision 19), where before R4 that directory was promoted
    through a **containment-verified** pairing - so the same on-disk pair can now
    report the names-only answer. That delta is the price of the earlier, better
    answer: a `medium` row carrying a source URL is worth more than a `low` row
    carrying a stronger verification string, and it is names-only on both routes
    anyway - no bytes are read either way.

    **An id-named pile that still carries a model at its own depth gets
    `pair_only` from a verified twin; one that does not stays absent.** Two
    conditions make the visible row, and the second is a bound this document
    already states twice. First, this rule's own widening: `claimed_ancestors`
    now covers an inferred `medium`, so an id-shaped directory holding child
    bundles is declined in the folder pass. Second, the own-depth bound: the
    promotion pass asks `archive_owned_facts`, which answers `None` unless
    `_own_depth_model_count` is at least one, and the `claimed_ancestors` branch
    is only reached after that answer. With both - the `5390184_pile` witness
    writes its own loose model beside `Child_A` and `Child_B` - the verified stem
    twin records the directory in `pair_only_specs` instead of `found`: a
    `blocked` / `archive_only` row that is visible, creates nothing, and whose
    facts read `low` / `generic-bundle`. **A refusal is never stamped with the id
    provenance it was refused for** - we refused the row *because* we decided it
    is a pile and not a bundle, so the row says nothing about a Thingiverse id.
    A pile by this document's own definition - two or more unnamed model-bearing
    subdirectories, no model at depth 0 and none under `files/` - has no
    own-depth model at all, so `archive_owned_facts` refuses it *before* the
    container rule is consulted: a verified twin buys it no row, and it stays
    absent exactly as it would with no archive (`RepRap-MultiFilament` is that
    shape). Mutation: un-widen the `claimed_ancestors` refusal and the pile is a
    creatable `medium` / `ready` row beside its own two children, so the report
    gains a third row and create-all makes three projects instead of two.

    **The archive-snapshot widening is accepted.** `_zip_snapshot` names an
    archive's snapshot from the zip's **stem**, so `detect_bundle` sees an
    id-shaped name for a `5390184_x.zip` exactly as it does for a directory, and
    R4 therefore fires on archive snapshots too - one row per id-named archive,
    `medium`, `blocked` / `archive_only`. The ticket said "directory name"; this
    is a deliberate widening of that wording, accepted, and it is harmless
    *precisely* because Decision 6 already makes a zip-only row uncreatable: a
    zip-only bundle can only be created through `POST /import/zip` and a scan, so
    the extra row creates nothing and invites nothing. Mutation: gate R4 on
    directories only and the same zip reads `generic-bundle` / `low` with no URL -
    the medium reading is what the widening buys, and the create stays blocked
    either way, which is exactly why it costs nothing.

    **Both guard sites and the create-time belt are the whole regression surface,
    so all three comparisons are one set.** The three `== CONFIDENCE_LOW` tests in
    `bundle_service` are now membership of `bundle_detect.INFERRED_CONFIDENCES`.
    An un-widened `claimed_ancestors` refusal lets a `medium` pile be a creatable
    row beside both of its children. An un-widened `contained_keys` strip lets a
    `medium` folder swallow a vendor zip as a "member", so the archive disappears
    from the report entirely. An un-widened `_create_one` belt writes a `Project`
    row for a candidate whose registered assets are another bundle's. Each has one
    witness test, and each witness reds under exactly its own mutation, so the
    three sites cannot be widened by accident or left behind one at a time. A
    fourth inferred confidence could not escape any of them by omission, because
    the guards ask about the set rather than about a list of literals.

    **Residual silence, stated rather than hidden.** A doc-less, image-less,
    model-only folder **without** a numeric id is still not offered *by this
    rule*, and that is R3 unchanged rather than an oversight: a random folder of
    STLs is genuinely indistinguishable from a bundle, and the owner's 117
    low-confidence rows already show what a weak rule costs. It is offered when
    the folder's NAME reads like a title (`Mf-hvck`, Decision 21), which is the
    one signal R3 and R4 both leave on the table, and it is still only `low`. A
    folder whose models sit only at depth **2 or deeper** stays invisible under
    all three rules, because R5 reuses the same `_own_depth_model_count` bound
    instead of inventing a second one: the sentence that once called this shape
    `Mf-hvck`'s territory was wrong about where it would be solved, and it is
    still nobody's rule. This is the second of the two sentences `Mf-hvck`
    corrected; the first is Decision 18's silence sentence.

21. **A title-shaped name is a `low` bundle, and the rule is a service
    post-pass rather than a detection branch.** The owner's decision
    (2026-11-03) was to offer the no-evidence folders only when the name reads
    like a title, not a part. The measurement that forced it: 11 of the 131
    directories in `import/` hold models and nothing else - no document, no
    image, no archive twin. Nine carry a name a person chose (`Rocket Filement
    support`, `Stagetop`, `LittleTup`, `Taffi`, `GavinIsABot`, `mac deckbox`,
    `skull mask`, `printable carrage`, `Filament guide`); two carry the payload
    directory a hand extraction leaves behind (`files`, `source`), and offering
    those would be the pile problem in a smaller shape.

    **"Reads like a title" is three checks in a fixed order, written down rather
    than felt.** `is_title_name` judges the DISPLAY name (`display_name`),
    whitespace-collapsed, so the judgement is about the name an operator sees and
    never about the raw segment: `17_ClockworkMonster` is judged as
    `ClockworkMonster`. First the exclusion list, matched case-folded on the
    whole segment and never as a prefix and never fuzzily:

    `files`, `source`, `src`, `images`, `img`, `renders`, `textures`, `docs`,
    `stl`, `stls`, `models`, `meshes`, `parts`

    (`TITLE_PART_NAMES`). Then branch A: any non-alphanumeric character is a
    space or a separator, which is a person's phrasing - the signal for `Rocket
    Filement support`, `mac deckbox`, `skull mask`, `printable carrage` and
    `Filament guide`. Else branch B: exactly one capitalised word (`_TITLE_WORD`,
    `[A-Z][A-Za-z0-9]*` by `fullmatch`), **longer than**
    `TITLE_SINGLE_WORD_FLOOR` (4) and carrying at least one lowercase letter -
    the signal for `Stagetop`, `LittleTup`, `GavinIsABot` and `Taffi`. The floor
    is `> 4` and not `>= 4` because `Taffi` is a measured title at five letters
    while a pile member called `Beta` is four; the lowercase clause is what
    refuses an ALL-CAPS label, because `STAGETOP` is a product code and not a
    title; and branch B's leading `[A-Z]` is what refuses every ordinary
    lowercase directory name, which is why `residual`, `clockworkmonster` and
    `plainparts` stay invisible. Mutations, each pinned: relax the floor to `<`
    and `Beta` becomes a row; drop the lowercase clause and `STAGETOP` does; drop
    the separator branch and five of the nine vanish.

    **Branch C - a vendor-ish suffix - is declined deliberately.** A suffix table
    (`_v2`, `-remix`, `thingiverse`) is a guess about names someone thought of,
    and it buys nothing the other two branches do not already buy: every measured
    name that carries a suffix also carries the separator branch A reads, and a
    suffix table would silently widen the rule to names nobody measured. The
    ticket listed it as an option and not a requirement; declining it keeps the
    vocabulary closed and every exclusion explicit.

    **`files` and `source` fail every branch twice over, and that is the whole of
    the 11 -> 9.** The exclusion refuses them first; with the list emptied,
    branch A still finds no separator and branch B's `fullmatch` is `None` on a
    leading lowercase character. So the two part names are the only reason eleven
    measured directories become nine rows, and deleting the list would not offer
    them anyway - which is why the list is tested by name against a LITERAL
    tuple, not against itself, so an entry cannot be deleted without a red.

    **At least one model at the folder's own depth, and the name alone is never
    enough.** `title_name_facts` reuses `_own_depth_model_count` - the same bound
    R3 and R4 use - so a pile of two unnamed model-bearing subdirectories and a
    folder whose models sit at depth 2 stay refused exactly as they are under
    R3. Loosening it would promote piles on their children's models, which is the
    one thing every inferred rule here refuses to do. The row is built by
    `_name_only_facts` with **no** `thing_id`, so creator, licence, licence URL,
    source URL, thing id and description are all `None` by construction rather
    than by a test: a name proves nothing about who made a thing or what it may
    be reused for. It reports `detector` `title-name`, `metadata_shape`
    `title-name` and confidence `low`, so a name-derived row is visibly
    distinguishable on the wire from a vendor row's
    `thingiverse-readme`/`root-docs`/`high`.

    **One switch, read in one place.** `TITLE_NAME_BUNDLES_ENABLED` is read inside
    `title_name_facts` and nowhere else, so turning the rule off is one edit and
    the scan, the report and the create pass all follow: the post-pass asks for
    facts, gets `None`, and adds no candidate at all - not a candidate without a
    row.

    **The rule is a post-pass in `_build_candidates`, and the placement is
    load-bearing.** It runs after the folder pass, after pairing, after the
    zip-only pass and after the archive promotion, and before the
    `contained_keys` strip. Inside `detect_bundle` it would inherit the
    deepest-first walk, and that walk is exactly what lets a child's row pre-empt
    its parent's: measured, moving it there migrates `LittleTup`'s row into
    `LittleTup/articulated-dragon-claws` and turns the promoted
    `17_ClockworkMonster` row into `title-name`, losing its archive-owned row -
    16 witnesses red, 12 service and 4 pure. Running after the promotion is
    load-bearing in the other direction: a directory a verified archive owns
    keeps its stronger `archive_owned` row and is skipped here, which is what
    lets `Cinderwing3D - Crystal Dragon` and
    `ultimate-card-box-v4-model_files` keep reading `generic-bundle`. The pass is
    shallowest-first and every accepted key joins the taken set, so a title
    parent claims its title child and never the other way round.

    **The container rule applies at the new site through `_relates_to_any`, with
    three legs.** A key is skipped when it IS a row (claimed, promoted or
    pair-only), when it sits INSIDE a row (a name-only guess must never re-offer
    a bundle's own subtree as a second thing), or when it HOLDS a row (a folder
    of bundles is a pile whatever its name says). The third leg is the one this
    site needs and the one the older sites do not already express:
    `contained_keys` covers archive-only, promoted and pair-only rows, but a
    title parent holding an ordinary claimed bundle is refused by nothing else.
    Mutation: drop that leg and `import/Skull Mask` - its own loose model plus a
    claimed `Inner` bundle - becomes a creatable pile, and the two older
    `Unsorted`/`Wrapper` witnesses red with it.

    **Folders only, and that is the asymmetry against R4.** R4 fires on archive
    snapshots because `_zip_snapshot` names a snapshot from the zip's stem, and
    that widening is harmless because Decision 6 already makes a zip-only row
    uncreatable. R5 is deliberately narrower: it is a post-pass over the
    directory walk, so an archive is never offered on its name alone. A
    title-shaped name on an archive is the vendor's own filename, which the
    stem-twin and entry-root routes already use as EVIDENCE for a directory;
    turning it into a row of its own would count the same signal twice and add
    archive-only noise that can never create.

    **Stagetop is a caveat, not a blocker, and `Mf-5dnx` owns the class.** The
    owner reports `Stagetop` as a COMPOUND project - one folder holding several
    distinct things - so this rule WILL offer it as one `low` bundle and that
    will be wrong. Offering it anyway is the owner's instruction: a human can
    reject the row in the queue, and a silently dropped row is a thing nobody can
    review. This rule is explicitly not the answer for compound layouts; the
    shape has still to be explained, and `Mf-5dnx` tracks it.

    **Amended by `Mf-8xrc`.** The shape has now been explained and the class has
    its own rule: R7 `compound-bundle` (Decision 23) runs after this pass, and
    `import/Stagetop` is ONE `compound-bundle` / `low` row with groups 45 and 15
    instead of the eleven rows its children carry on the measured library. The
    warning above turned out to be unnecessary in the measured library - this rule
    never offers the parent at all, because it HOLDS rows and `_relates_to_any`'s
    holds-a-taken-key leg skips it (the refused ledger named it
    `R3:trio-no-image`, from the folder pass; after R7 the folder is OFFERED, and
    the before/after artifacts record the change) - but the caveat was right about
    the class, and R7 is the rule it says it was not. With
    `COMPOUND_BUNDLES_ENABLED` off, every absorbed row comes back unchanged, which
    `test_service_the_switch_off_restores_every_absorbed_row` pins on the
    fixture's eleven child rows.

    **The scope is wider than the measured eleven, and the risks are stated
    rather than hidden.** A title-named folder holding models plus an image, or
    models plus a text file, is also offered - shapes R3 refuses for want of the
    trio - because a decoration gate was refused as non-monotonic: adding a
    `notes.txt` would flip a row from offered to refused. A five-letter
    capitalised word is admitted because `Taffi` is exactly five, so a pile
    member called `Alpha` becomes a `low` row while `Beta` is floor-refused. The
    exclusion is a closed set: `Foo/files` is refused but `Foo/files 2` and
    `Fooparts` are not, and there is no fuzzy matching by design. A
    browser-duplicate name (`Fates End Tower (1)`) reads as a title and would be
    offered if it held models. `_relates_to_any` is O(dirs x bundle keys) inside
    an already-bounded walk - fine at household scale, and stated rather than
    measured. What cannot be measured here is the effect on the owner's live
    library beyond the eleven directories the ticket counted; this ADR's counts
    predate the rule.

22. **A single-root archive with models and images is a `low` bundle, and the
    rule is archive-only.** `Mf-tcrw` adds R6 `archive-bundle` / `low` /
    `archive-root`. An archive is a vendor artifact: someone zipped it
    deliberately, so a missing README is the vendor's choice rather than evidence
    against bundling. R3 needs a text file, and a tower archive that ships none is
    refused for a document the vendor never intended to include. R6 fills exactly
    that hole: at least one model **and** at least one image, every payload entry
    under a **single root entry directory**, and `counts.text == 0`. The
    confidence stays `low` because nothing states provenance - the row is built by
    `_name_only_facts`, so creator, licence, licence URL, source URL, thing id and
    description are `None` **by construction**, not by a test. An archive's shape
    proves it is one thing; it says nothing about who made it or what it may be
    reused for.

    **The single root is the pile guard, and it replaces `_own_depth_model_count`
    here deliberately.** R3's own-depth bound is what misses
    `Fates End - DiceJails.zip`: 8 models under **four** subdirectories
    (`DJ1_WoodenCrate/`, `DJ2_StoneJail/`, `DJ3_GelatinousCube/`, `DJ4_Mimic/`)
    inside one root (`_DiceJails/`), 4 images - one render beside each jail's own
    models - and no text, so the own-depth count is **0** even though the archive
    is plainly one thing. The archive IS the wrapper, so a single common entry
    root says "one thing" where a directory rule needs a model at its own depth.
    `common_entry_root` is reused rather than re-implemented, so an `__MACOSX`
    prefix cannot make a real bundle rootless. This is not a loosening of R3: the
    clause is consulted only when every stronger rule refused, never by
    `detect_bundle` and never on a directory.

    **The measured delta on the owner's library is 26 archives to 6 consumed to
    20 new rows.** 26 archives have this composition. Six are consumed by
    stronger rules and render no R6 row: four stem twins (`Cinderwing3D - Crystal
    Dragon.zip`, `Cinderwing3D Dragon Rose Valentine.s day.zip`, `SnVB623.zip`,
    `SnVD623.zip`), `17_Fates End - ClockworkMonster Tower.zip` by entry-root
    pairing, and its byte-identical `(1)` copy as `duplicate`. That leaves exactly
    20 new LOW rows: 19 previously `R3:trio-no-text` towers and rails (including
    `beltedz_rails.zip`) plus `Fates End - DiceJails.zip`. Those are the rows the
    owner asked to be able to review. R6 decides an ARCHIVE's composition and
    nothing about which directory it belongs to, so a pairing still runs first and
    keeps its answer: the six above never reach the clause. The mirror case is
    visible and deliberate - an archive whose namesake directory sits under
    ANOTHER parent is not a stem twin (Decision 6's same-parent bound), so it is
    offered on its own composition as well as its namesake being offered on its
    own (R5), two uncreatable rows for what may be one thing, which the operator
    rejects in the queue rather than the code guessing.

    **The restraint is a model and an image, and model-only archives stay
    refused.** Nine measured archives are models and nothing else: six with
    entries at the zip root (`606df4f71f663_fates-end-dice-tower-free-wizard-tower.zip`,
    `Articulated_Axolotl.zip`, `ImageToStl.com_trans+-+coo+pwr+-+black.zip`,
    `MTG+DECK+BOX+-+GOBLIN+CONSTRUCT+100++CARDS+AND+DICE+HOLDER+-+SUPPORTFREE.zip`,
    `MTG+Icy+Manipulator+Deck+box+100++Single+Sleeved.zip`,
    `Print In Place FIDGET GRENADE.zip`) and three under a single root
    (`articulated-dragon-claws20200216-24469-1e5maw5.zip`,
    `creeper-headphone-stand20220605-111-e4jv0b.zip`,
    `trading-card-game-deck-box-80-or-110-cards20230404-16215-f0wdcb.zip`). They
    stay refused: nothing in them is evidence they are one thing rather than a
    dump, and the measured cost of a weak rule is the 68 low-confidence rows
    already in the queue. The `text == 0` scoping is the other half of the
    restraint: a listing that ships a document is R3's business, so
    `Fates End_Bonus_Delivery-Banks.zip` (33 models, 3 images, one text, one root
    `_Bonus_Delivery`, own-depth 0) stays refused exactly as it was.

    **The guards, each measured.** `All Pokemon.zip` stays refused as a pile: 607
    root entry directories, 2096 images, 607 text, and 2 files the detector
    classifies as models. The 3255 the ticket counted is `.obj` + `.blend` +
    `.stl`, and the 1626 `.obj`, 1627 `.blend` and 226 `.fbx` beside those 2
    `.stl` are not extensions the import door accepts (only `.stl`, `.step`,
    `.stp`, `.3mf`, `.lbrn` and `.lbrn2` are). A text file keeps R6 away and the
    roots would too. `Fates End Dice Jails 5-8.zip` (8 models, 4 images, 4 roots,
    no text) is the one multi-root near-miss and stays refused: every part would
    qualify alone and the archive is still four things.
    A HIGH archive (README pair carrying both markers) keeps `high`; R6 is a
    wrapper around `detect_bundle_decision`, so R1/R2/R4/R3 precedence is
    untouched. A `.zip` nested below a detected bundle is still a member, never a
    candidate (`Mf-74x5` unchanged). And when the clause declines, the refusal
    returned is the **primary** one - `R3:no-own-depth-model` for `All Pokemon.zip`
    and `Fates End Dice Jails 5-8.zip`, `R3:trio-no-image` for the models-only
    archives - so no new `REFUSAL_*` word exists and the refusal ledger's
    vocabulary is unchanged.

23. **One folder holding one product in several parts is ONE `low` row, and the
    rule is a post-pass with a vocabulary of shapes rather than of refusals.**
    `Mf-8xrc` adds R7 `compound-bundle` / `low` / `compound`. The owner's
    explanation (2026-11-05) is the whole premise: *"StageTop is a folder that
    contains lots of parts to make one customizable thing ... So is 'The
    Rocket'. It's one project with lots of variants. Not lots of separate
    projects."* A compound layout has two honest answers - one row with a
    breakdown, or one row per part - and the ticket chose the breakdown, because
    a row can name the parts and say they belong together while eleven separate
    rows on the measured library cannot.

    **The ticket's four discriminators, quoted verbatim, because each one is
    decided below.** *"VARIANT FAMILY: groups share a family token
    (The_Rocket--<variant>--Side_Spool_Mount, 8 groups) or repeat the same model
    count per group (8 x exactly 12). Strong signal of one product with
    variants."* [the "12" is the double count corrected in the declined-leg
    clause below and in Decision 25: the real shape is 8 x exactly 6] *"VERSION WAVES: groups share a product token with each other
    ('StageTop Wave One - Personal Use', 'Wave 1.5'), often with a second level
    of nesting (Core / Accessories / Legs / PlayTiles)."* *"REPO DUMP, must be
    refused: repository metadata present (.git, .github, _layouts, _sass, docs)
    and/or each group carrying its own thing id - endorphin is a git checkout
    holding five DIFFERENT products, and today's behaviour (descend, offer each)
    is correct. Do not regress it."* *"FOREIGN GROUP, flagged not split: a group
    whose name carries a vendor thing id or whose root has its own README/LICENSE
    is not part of this product (xbox360_shell's 'Wallmount for Lenovo Tab M10
    Plus Gen3 - 5461913'). Name it on the row; the operator splits it at
    confirmation (C2)."*

    **The clauses, in the order `compound_bundle_facts` decides them.** (1) The
    switch `COMPOUND_BUNDLES_ENABLED`, read inside that function and nowhere
    else. (2) Repository metadata: `has_repo_markers` asks whether the
    snapshot's FIRST path segment is one of `COMPOUND_REPO_MARKERS`, and
    `bundle_service._inside_repository_dump` independently refuses a key that is
    a marker-bearing root or sits anywhere below one, so no payload directory of
    a checkout can be compounded either. (3) At least one model-bearing group:
    `compound_groups` is every depth-1 first segment holding at least one model
    anywhere below it, sorted by `(casefold, name)` so the row is a function of
    the tree rather than of readdir order, and models at depth 0 are the folder's
    own and never a group. Clause (3) is an explicit statement of a condition both
    legs already imply (`if not groups: return None` and `len(groups) >= 1`), kept
    so a folder with no groups reads as "no groups" rather than as a leg that
    failed quietly. A top-level `__MACOSX` entry is still skipped when groups are
    named, but the `._*` copies under it no longer count as models: the count
    asks the library scan's own basename predicate (`is_counted_model`), the scan
    registers neither the sidecars nor a hidden name, and the Rocket therefore
    reads 6 per group and 48 in total (Mf-94od, Decision 25). (4) Leg A
    **or** Leg B. (5) Two or more groups carrying their own thing id refuse -
    `COMPOUND_OWN_ID`, 5+ digits, the recall predicate - and that check sits BEFORE
    the family evidence so the reasons come in the measured order. (6) Leg A
    additionally needs the family token; Leg B needs none.

    **A token is a maximal alphanumeric run of the casefolded name, and the
    predicate is written inline rather than stamped.** `_compound_tokens` splits
    on anything non-alphanumeric in Python, because the separators a vendor chose
    (`_`, `--`, ` - `) say nothing about the words they separate - the same choice
    `is_title_name` makes when its branches are written out instead of encoded in
    a table. `rule_set_tables` therefore records the tables a rule READS
    (`COMPOUND_GLUE_TOKENS`, `COMPOUND_TOKEN_MIN_LENGTH`, the two id regexes and
    the rest), not the Python predicate that consumes them: the stamp proves what
    DATA a row was decided from, not a copy of the code that decides it. The glue
    table is closed at four words and the measured witness is `the` (all eight
    Rocket names carry it), though `the` is refused twice over - it is also three
    characters, so the length floor refuses it with the table empty. `with` is the
    only glue word the length floor does not also refuse, and emptying the table
    is what makes `with widget`/`with gadget` a compound; `and` and `for` are the
    same shape of word, and a longer stopword list would be a guess rather than a
    measurement.

    **The marker table is decided by the walk's own pruning.**
    `COMPOUND_REPO_MARKERS` is a set of entry NAMES, never a directory listing,
    because `library_scan._walk_storage` prunes hidden DIRECTORIES and yields
    hidden FILES:

    | Marker | Reachable from the snapshot? | Witness |
    |---|---|---|
    | `.git` | as a FILE only - a `.git` directory is pruned before the module sees it | endorphin holds a `.git` directory and it never reaches `has_repo_markers` |
    | `.github` | not at all - a hidden directory, pruned | endorphin holds one, unreachable |
    | `.gitmodules` | as a file | none on endorphin; it is the file form the walk yields, beside a linked worktree's `.git` file |
    | `_config.yml` | as a file | endorphin |
    | `_includes` | as a directory name - the first segment of the files inside it | endorphin |
    | `_layouts` | as a directory name | endorphin, and `test_repository_metadata_refuses_a_compound` pins it as `_layouts/page.html` |
    | `_sass` | as a directory name | endorphin |
    | `gemfile` | as a file, casefolded | endorphin, and the same test pins `Gemfile` |
    | `gemfile.lock` | as a file | endorphin |

    **`docs` was DELETED from that list, and the deletion is not the mirror of
    the additions.** `docs` is an ordinary payload directory name - it is one of
    R5's `TITLE_PART_NAMES`, a name this ADR already reads as "part" and not
    "product" - and no measured tree is distinguished by it, while every vendor
    export that happens to ship `docs/` would be refused as a checkout for
    holding one. The six markers endorphin actually reaches (`_config.yml`,
    `_includes`, `_layouts`, `_sass`, `Gemfile`, `Gemfile.lock`) are witnesses of
    **visibility** on ONE tree rather than six independent measurements of what a
    checkout is: what they prove is that the walk can see those shapes at all.
    And endorphin is refused **twice over** - by these markers and by two groups
    carrying their own thing ids (`3D Printer Electronics Enclosure - 5414731`,
    `Ender 5 Dual Y Stepper NEMA 17 Mount - 4591932`) - so removing either leg
    alone leaves it refused, which is why neither leg is load-bearing by itself
    and why a name with no witness could go.

    **Leg A is the ticket's rule sentence, and it has TWO measured witnesses.**
    No model at the folder's own depth - `root_model_count`, which counts
    STRICTLY depth 0 and is deliberately not `_own_depth_model_count`, because
    that function answers 15 for `import/Stagetop` (the `Wave 1.5` models, which
    are a child's pile) and would make every wrapper a product of its own - plus
    at least `COMPOUND_MIN_GROUPS` (2) groups sharing a family token.
    `import/Stagetop` is the first: token `wave`, 60 models as 45 + 15.
    `import/Rocket Filement support` is the second: eight groups of exactly 6
    (six real `.stl` each; the 48 AppleDouble `._*` sidecars are not library
    content) sharing five words, of which `_compound_family_token` returns the
    first surviving one in sorted order, `mount`. Two, and no more, on the
    measured tree.

    **Leg B is the ticket's "fix the loss", and it has exactly ONE measured
    witness.** The folder holds models of its own, EVERY group below it is
    foreign, its own models are the strict majority, and at least one group
    carries a real vendor id (`COMPOUND_FOREIGN_ID`, 6-8 digits). The witness is
    `import/xbox360_shell`: 27 own models against one foreign group of 4, so the
    27 that were invisible while the parent was refused as a container are covered
    by a row again. The id requirement is what stops a root-heavy folder holding
    two unrelated README-bearing sub-projects from merging on nothing but "most
    of the models are at the top", and the strict majority is the anti-swallow
    floor. Leg B asks for **no** token:
    `test_leg_b_needs_every_group_foreign_and_dominant_own_models` builds a
    passing shape whose groups share no word at all, which is the point - the
    vendor id and the majority are its evidence, and a token would be a third
    guess stacked on two.

    **The two id regexes are deliberately separate constants pointing in
    opposite directions.** `COMPOUND_FOREIGN_ID` is 6-8 digits and precision: its
    question is "is this group somebody ELSE's thing", and its floor is six
    because five-digit runs are ordinary part numbers, so a false positive puts a
    foreign flag on a real part. `COMPOUND_OWN_ID` is 5+ digits and recall: its
    question is "is this group a whole product of its own", it is broader on
    purpose, and the cost of over-matching is only that a compound is declined. A
    refusal may afford to be wrong; a flag may not.

    **Foreign is two mechanical questions about the group's OWN root.** The
    group's name carries a vendor id, or the group's own depth-1 document stems to
    README / LICENSE / LICENCE (`COMPOUND_GROUP_DOC_STEMS`): `<group>/README.txt`
    yes, `<group>/docs/README.txt` no, because a document one level down is
    payload rather than a claim of ownership.

    **The equal-model-count leg was DECLINED, and it is the ticket's own second
    half of VARIANT FAMILY.** "or repeat the same model count per group (8 x
    exactly 12)" is not implemented, and the ticket's own example number was made
    of the double count this ADR's Decision 25 removes: the real shape is 8 x
    exactly 6. It has no measured witness of its own: the
    Rocket matches it AND shares tokens, so the leg adds nothing to the only shape
    that would exercise it, and an equal count with no shared token is a pile of
    same-sized things rather than one product. It is declined for the reason R5's
    suffix table is declined - a table of counts is a guess about sizes somebody
    chose, and this rule keeps its evidence to words and ids.

    **The row is `low` and name-derived: nothing here states provenance.** It is
    built by `_name_only_facts`, which has no parameter that could set them, so
    the title is `display_name` and creator, licence, licence URL, source URL,
    thing id and description are ALL `None` **by construction** rather than by a
    test. A folder holding one product in two editions says nothing about who made
    it or what it may be reused for, and the groups inherit the same honesty:
    `BundleGroup` carries a name, a model count and a `foreign` flag, and nothing
    else.

    **One group is a wrapper, not a product in several parts.**
    `COMPOUND_MIN_GROUPS` is 2, because a single group carries no "several parts"
    evidence at all; `test_a_single_model_bearing_group_is_not_compound` pins the
    floor and, in the same test, the two bounds Leg B still needs - the vendor id,
    and a STRICT majority (3 own against 3 foreign is refused, not compounded).
    The SAME bound is the own-id refusal threshold (`own_ids >=
    COMPOUND_MIN_GROUPS`), so it answers both questions on purpose: two parts make
    a family and two products make a pile, and changing it moves both;
    `test_two_groups_with_their_own_vendor_ids_refuse_the_leg_b_shape` builds a
    shape that is Leg B on every other count and reds if the constant is raised.

    **The hidden-directory limitation is stated rather than papered over.** The
    walk prunes hidden DIRECTORIES, so a checkout whose ONLY marker is `.git/` is
    caught by the own-id leg or not at all - a paid checkout with two groups, two
    editions and no ids on either would be compounded. `include_hidden` exists in
    `library_scan._walk_storage`, but a rule about one folder's contents is not
    the place to widen the library's pruning for every other caller.

    **The absorption is deliberately low-suppresses-high, because owner decision
    2 says so.** `import/xbox360_shell`'s foreign `Wallmount ...` row is
    `thingiverse-readme` / `high` and is absorbed by a `low` compound row: "one
    row, with the foreign group flagged". Three things keep that evidence
    recoverable rather than lost - the group NAME stays on the row
    (`BundleGroup("Wallmount for Lenovo Tab M10 Plus Gen3 - 5461913", 4, True)`),
    the absorbed key is named `member-of-bundle` in the refusal ledger, and
    detection is recomputed from disk at create time, so the high evidence was
    never overwritten, only unoffered. C2 owns the created project's membership
    and the flagged group's split and C3 owns the presentation; neither is in this
    slice, and this ADR says only what detection reports.

    **Two numbers in the plan were wrong, and the disk wins.** Stagetop is **60**
    parts, not 56: the plan's 56 with groups 41/15 was a `.stl`-only count, while
    `MODEL_SUFFIXES` is `IMPORT_ALLOWED_EXTS` minus `.zip` and therefore includes
    `.step` - the four `.step` files (`PlayTile - Blank - Corner.step`,
    `- Half.step`, `- Full.step`, `- Quarter.step`)
    live in `StageTop Wave One - Personal Use/PlayTiles/PlayTile - Blank/`, and
    measured by the detector's own table the groups are 45 (41 `.stl` + 4
    `.step`) and 15. The ticket's "60 parts" was RIGHT and the plan had drifted;
    it is pinned as `counts.model == 60` with groups `45`/`15`. The genuine ticket
    drift runs the other way: endorphin yields **8** separate product rows, not
    the ticket's 5 (2 `thingiverse-readme`/`high`, 3 `title-name`/`low`, 3
    `generic-bundle`/`low`), and 8 is the correct behaviour - "five DIFFERENT
    products" is what that checkout is *about*, not what is on it. Stagetop also
    yields **eleven** rows today, not the ten the plan's summary table said,
    because `StageTop Wave One - Personal Use/PlayTiles/Accessory` is a
    `title-name` row of its own beside the ten `generic-bundle` ones.

    **Repository metadata is checked on every ANCESTOR inside the walk, not only
    on the candidate.** `_inside_repository_dump` walks a key's parents through
    `_parent` and refuses the key if any of them bears a marker, so nothing
    strictly below a checkout can be compounded. Without that leg
    `import/endorphin/STLs` - three `stage-N` directories that share the token
    `stage` and are three `title-name`/`low` rows today - would be compounded into
    one row and swallow three rows of a checkout holding five different products.

    **A compound may only absorb rows no stronger than its own shape
    generalises.** A Leg A family - models only in subdirectories - replaces LOW
    guesses, and the eleven Stagetop children (eleven in the fixture too, since
    the fixture is the measured tree, `PlayTiles/Accessory` included) and the
    eight Rocket variants are exactly that. Absorbing a STRONG row needs the Leg
    B shape, which is why
    `_compound_absorbs` asks `root_model_count` and refuses when the absorbed set
    holds a `STRONG_CONFIDENCES` row and the folder has no model of its own. The
    witness is `vendor/` holding two Thingiverse exports that share the word
    "bundle": that is a pile of two whole products, and R7 must leave both `high`
    rows alone - `test_service_a_bundle_that_is_already_offered_is_never_compounded`
    pins it on the parent that IS offered, while
    `test_service_a_leg_a_family_never_absorbs_a_high_row` pins it on
    `vendor/Alpha Bundle` + `vendor/Beta Bundle`, where nothing is offered at the
    parent, Leg A's token leg passes on the shared word, and only the absorption
    bound refuses. Without the bound the rule would lower two undisputed `high`
    verdicts to one `low` row.

    **The measurement, quoted from the artifacts.** before/after on the owner's
    library, base `585f12917cd2b12c0c5f91f435a1fe8dffb55796`, root
    `/mnt/ceph/3D_models`, `--limit 400`, rule set `2026-11-04.2` /
    `072a1505299394483222ec7cd103c9d85c9008345827b8ea21288857905f3e09` ->
    `2026-11-05.1` /
    `2dcd2b7064aee98a815fee6b8d1d6a26d9f11646571406f99ca35b0a7170b032`: rows
    **201 -> 184**, `high` 112 -> 111, `low` 88 -> 72, `medium` 1 -> 1. Added (3):
    `import/Stagetop` (Leg A, 60 models, groups 45/15), `import/Rocket Filement
    support` (Leg A, 96 models, 8 groups of 12 in the artifact's own reading -
    Mf-94od then showed that 48 of those 96 were AppleDouble `._*` sidecars and
    the library content is 48 models in 8 groups of 6, so Decision 25 restates
    the figures in THIS decision that describe current behaviour: the Leg A
    counts, the 8 x 6 witness and residual 5. The artifact reading above stays
    as it was taken), `import/xbox360_shell` (Leg B, 31
    models, 27 own + the foreign group of 4). Removed (20): the eleven Stagetop
    children and the eight Rocket variants (absorbed) plus the one `high`
    `import/xbox360_shell/Wallmount for Lenovo Tab M10 Plus Gen3 - 5461913` row
    (absorbed, named on the compound row and in the ledger). The ledger moves
    exactly as the rule says: 20 paths that had no entry now read
    `member-of-bundle`, 3 parents leave it because they are offered, and 25
    entries change rule - 22 archives from `twin-consumed` /
    `promotion:no-own-depth-model` to `nested-member`, which is the Mf-74x5 answer
    for a zip inside a detected bundle, and 3 mid-level directories from
    `R3:no-own-depth-model` to `member-of-bundle`. `import/endorphin` is
    unchanged: the same 8 rows, no compound row, no rewritten verdict. `--diff`
    prints "comparable: NO - the rule set changed" and "the row delta below is
    caused by the rule change, not by the corpus" while still naming every row,
    and that is the correct verdict on a detector change rather than an error: a
    like-for-like corpus comparison is exactly what a rule-set stamp exists to
    refuse. The database counts read **NOT MEASURED**, `assets` and `projects`
    both `null`, because the development database is empty - nulls, not zeros.

24. **Creating from a compound row is an ATTACH decision, and an excluded
    group's survival is justified by the created project's own marker, not by the
    ledger.** `Mf-ao6r` answers the question Decision 23 left open - what a create
    does with the groups a compound row names - and the clause list is short: one
    project per compound row, never one per group; every included part attached;
    the confirmation list shows every group with its part count; and only a
    foreign, separable group may be excluded. Nothing detection reports changes;
    the rule set and its stamp are untouched by THIS decision (`2026-11-05.1` /
    `2dcd2b70...`; Mf-94od's later bump to `2026-11-05.2` is Decision 25's). All
    four clauses are pinned:
    `test_service_creating_from_a_compound_row_makes_one_project_with_every_part`
    (one project, 60 parts, and a set equality of attached `Asset.storage_path`
    against every registered model),
    `test_service_xbox360_shell_creates_one_project_with_all_31_parts` (27 own
    parts plus the foreign 4), the seven `BundleReviewView compound groups` vitest
    cases (the confirmation list, with each group's part count), and
    `test_the_three_exclusion_refusals` together with
    `test_an_exclusion_of_a_group_that_was_never_a_row_is_refused` (the fourth).

    **What the exclusion changes, and what it leaves alone.** A create with an
    excluded group attaches the included parts only, and the compound row read
    back after the create is unchanged as far as detection is concerned - same
    counts, same groups, same cover asset, same status - because the exclusion is
    an attach decision and is stored nowhere near the detector
    (`test_excluding_the_foreign_group_creates_one_project_of_its_own_27`). The
    excluded group then comes back on the next report as a row of its own, with
    `detector` and `confidence` recomputed from its own documents rather than
    copied from the compound's `low`: the xbox mount re-offers as
    `thingiverse-readme` / `high`
    (`test_the_excluded_group_is_offered_as_its_own_row`,
    `test_a_re_offered_group_creates_its_own_project_and_only_when_re_offered`).
    A second create of the compound is `already_created`, and a second create of
    the split is a marker hit on its own key
    (`test_a_second_create_of_a_compound_row_is_already_created`,
    `test_a_second_create_of_the_split_group_is_already_created`), while the
    create audit names the exclusions every time, `[]` included
    (`test_the_create_audit_records_the_excluded_groups`).

    **`groups` versus `separable`, and why the field belongs to the service.** The
    wire row's `BundleGroupOut` carries the detector's three facts (`name`,
    `model_count`, `foreign`) plus a REQUIRED `separable`: "was this directory a
    row of its own before R7 absorbed it?", i.e. is there a row for its parts to
    fall back to? That question is a SERVICE fact - the pure detector never sees
    the row set or the ledger - which is why `BundleFacts.groups` /
    `BundleGroup` stay three-field and `RULE_SET_VERSION` / `RULE_SET_STAMP` are
    untouched. A defaulted `separable` would read falsy on a payload that forgot
    it and silently switch every exclusion checkbox off, which is why it is
    required rather than defaulted. Separability is NEVER derived from the
    refusals ledger: `_reconcile_refusals` labels every directory strictly inside
    an offered one `member-of-bundle`, so a ledger-derived answer would make every
    group separable and `not_separable_group` dead code.

    **The attach set.** It is every registered MODEL asset under the key that is
    not under an excluded prefix. The prefix carries a trailing slash on purpose,
    so a FILE literally named `<key>/<name>` is never excluded - only what sits
    inside the group's directory is the group - and the tests compare sets of
    `Asset.storage_path` rather than counts, because a count can pass on the wrong
    27 assets. The belt is restated here because it is the one thing an exclusion
    may not touch: `assets` and `own_assets` are NEVER filtered by the exclusion.
    Filtering makes `len(assets) > len(own_assets)` true for every excluded create
    and turns every exclusion into `failed` / `contains_bundles`; "exempt the
    compound row from the belt" is the named anti-fix that was refused, and the
    belt stays live on a `low` compound row
    (`test_a_registered_asset_that_vanished_from_the_disk_still_answers_the_belt`).

    **The marker shape, verbatim.** Every marker carries the six keys `key`,
    `detector`, `confidence`, `rel_dir`, `archive`, `twin_verified`, plus
    `enrichment` when a page supplied one. A compound row carries three more:
    `shape: "compound"`; `groups`, a list of
    `{name, model_count, foreign, included, attached}` in the row's own group
    order; and `cover`, `{asset_id, rel_path, reason, group}`. `model_count` is
    what the SCAN registers (since Mf-94od the count asks the scan's own basename
    predicate, so an AppleDouble `._*` sidecar names no model); `attached` is how
    many registered MODEL assets of that group are in the attach set. On a group
    with no exclusion the two agree - the Rocket reads 6 and 6 - and they differ
    only by an EXCLUSION, which `attached` carries alone. The account still adds
    up, and `test_a_non_compound_marker_is_unchanged_in_both_directions` pins the
    six-, six- and nine-key split in both directions.

    The marker's `cover.rel_path` is STORAGE-root-relative on every leg, and that
    is a settled invariant rather than a coincidence, because `_cover`'s own legs
    do not share a key space: the three registered legs report an `asset.path`,
    which is already storage-relative, while `largest-image-unregistered` reports
    `facts.cover_entry`, an entry relative to the bundle directory. `_create_one`
    therefore normalises once and uses that single path for both the marker's
    `rel_path` and `cover.group`'s attribution, so a later reader can attribute
    the face without knowing which leg won. Both directions are pinned:
    `test_the_marker_names_the_group_of_an_unregistered_cover` (the unregistered
    leg, where `group` would read `None` without the normalisation) and
    `test_excluding_the_foreign_group_creates_one_project_of_its_own_27` (the
    registered leg, `group: null` for a cover that belongs to no group).

    **Count equals registration, and that is now an invariant rather than a
    coincidence.** Before Mf-94od the Rocket's row counted 96 while its project
    held 48, because a suffix-only role made an AppleDouble `._a.stl` a MODEL
    while `library_scan` skipped the name: the two doors disagreed about what a
    model file is, which is exactly the invariant Decision 2 states. The
    detector now counts through the scan's own basename predicate, so the row's
    `model_count` IS what the scan registers (48 on the Rocket, six per group)
    and `attached` differs only where an exclusion removed a group. Pinned by
    `test_the_row_counts_exactly_what_the_scan_registered`, which asserts the row,
    the registered model set and the project's `storage_path` set are the same 48
    with no `._` path in any of them, so a future `library_scan` change reds with
    a name instead of silently shifting 48 -> 96.

    **Three refusals, all `failed`, never `blocked`.** `unknown_group` means no
    group carries that name, matched exactly and case-sensitively because a group
    name is a path segment; `not_a_foreign_group` means the group is this
    product's own; and `not_separable_group` means a FOREIGN group that was never
    a row of its own - R9's case, where R3 needs the trio, R1 the markers and R5 a
    title-shaped name, so excluding it would leave its parts in no project and in
    NO row, which is the ticket's vanish. `blocked` keeps meaning database
    readiness (Mf-um7x; Decision 15), and an unusable group name is a malformed
    request rather than a blocked one. Duplicate names are idempotent
    (`dict.fromkeys`) and an empty list is a no-op on any row, a non-compound one
    included. `test_the_three_exclusion_refusals` names all three and the edges;
    R9's case has its own witness,
    `test_an_exclusion_of_a_group_that_was_never_a_row_is_refused`, whose Leg B
    fixture's foreign group is named for a vendor id, holds four models and
    nothing else, and therefore reads `separable: false`.

    **The cover, re-picked and named.** `_cover` is reused verbatim, never a
    second cover rule: largest registered image, then `facts.cover_entry` (an
    image the documents name but the scan has not registered), then the largest
    registered renderable model, then none, with `reason` naming which leg won and
    `rel_path` naming the file. With an exclusion the pick is recomputed over the
    INCLUDED assets, and a facts-level `cover_entry` under an excluded prefix is
    dropped through `dataclasses.replace` so the face can never be the thing the
    operator just removed. Note the identity that comparison needs: `cover_entry`
    is bundle-relative while an excluded prefix is storage-relative, so the key is
    prepended before the two are compared. The re-pick and the group attribution
    are pinned by
    `test_excluding_the_foreign_group_creates_one_project_of_its_own_27`,
    `test_service_xbox360_shell_creates_one_project_with_all_31_parts` and
    `test_the_cover_never_picks_an_unregistered_image_under_an_excluded_group`;
    the real-library outcome is a PREDICTION, not a measurement: all three
    compound trees have 0 images and 0 text, so a Stagetop create lands on the
    `model-preview` leg and the face is the largest registered `.stl`/`.step`
    part, with `reason` naming the source kind and `rel_path` naming the part.
    That is the ticket's own worry - dozens of parts and an arbitrary one as the
    face - answered by naming the source rather than by hiding the choice. The
    measured artefacts cannot settle it because they carry no cover at all
    (`bundle_serialize`'s `BASELINE_ROW_KEYS` has 8 keys), which is why this is a
    prediction and not a measurement (residual 3).

    **One request cannot both exclude and create the split.** `[{the compound row
    with the mount excluded}, {the mount key}]` in ONE request gives the compound
    `created` and the mount `failed` / `unknown bundle`, because the report is
    recomputed once before any item runs; the mount row appears on the NEXT report
    read, and the review hook already refetches after a create. The limitation is
    exactly what keeps the marker the single, once-written justification: there is
    no path in which the create pass reads a marker the same request has just
    written. `test_a_split_sent_with_its_parent_in_one_request_fails_on_the_second_item`
    pins it.

    **Other bounded limitations, recorded rather than witnessed.** A renamed
    directory un-dismisses (Decision 8) and also un-re-offers, because the marker
    keys on the path; deleting the compound project takes the exclusion with it,
    so the compound row returns whole and re-creating it attaches every part; and
    a re-offered row is `blocked` / `unresolved_directory` until a `Folder` row
    for the group exists, exactly like any other row
    (`test_a_re_offered_group_without_a_folder_row_is_blocked_and_creates_nothing`).

    **The C2/C3 boundary.** The grouped PROJECT presentation and the variant
    filter are C3 (`Mf-aaqi`). The review queue's confirmation list is the only
    new UI this decision adds, plus one wire field and one wire fact (a group's
    `separable`, and the `exclude_groups` a create carries).

    **The residuals, RECORDED, NOT PINNED - a weaker class than the rest of this
    decision, and the batch must be read that way:**

    1. A re-offered row's interaction with `limit` / `truncated` and the counts
       block is not measured.
    2. Only the xbox mount is a measured witness for a re-offered group keeping
       its ORIGINAL detector and confidence. The reason is not that the other
       groups were never rows - the Rocket's eight variants and Stagetop's
       `Wave 1.5` were all `generic-bundle` rows and are `separable: true` - but
       that only a FOREIGN group may be excluded, and the xbox mount is the only
       foreign group among the three measured trees. A separable non-foreign
       group can therefore never reach the re-offer, so no second witness for
       this fact is reachable from these fixtures.
    3. Real-library behaviour is never measured - the development database holds
       0 projects - and the Stagetop cover is a prediction, not a measurement.
    4. The cover re-pick is pinned; the CLIENT's primed thumbnail is not, because
       it was rendered for the un-excluded cover (Mf-15xj's window).
    5. The AppleDouble gap between `model_count` and `attached` is CLOSED by
       Mf-94od (Decision 25): the two numbers now share one predicate and differ
       only by an exclusion. The residual that replaces it is narrower - no
       `._*`-heavy tree other than the Rocket is measured, so a hidden-name shape
       this ADR has not seen is untested rather than known-safe.
    6. A compound created WITH a group included can never be split later.
    7. The converse of the validation ordering: a stale group name masks
       `already_created`.
    8. There is no re-offered-row dedupe; it is unreachable by marker identity.
    9. A `pair_only`-only group reads non-separable and is refused.
    10. Nested-absorption refusals under a group that was never a row are not
        witnessed.
    11. The re-offer pays one `find_existing_project` per row, and that cost is
        not measured; nor is `_compound_splits`'s, which is one full `Project`
        scan plus a marker parse on EVERY report read, splits or no splits. The
        measured `184 -> 184, 0 changed` is therefore conditional on the
        development database's empty project table (residual 3).
    12. A re-offered row is `blocked` / `unresolved_directory` until a `Folder`
        row exists (pinned only for the missing-row direction).
    13. `separable` is computed at render time and re-checked at create time, so a
        group list that moved between the two can diverge; the only divergence
        direction is a checkbox that then refuses, which is the safe one.

    **One consequence of the confirmation surface, stated because a reader will
    find it.** The exclusion checkbox lives only in the detail pane's "Create
    project"; the bulk doors ("Create selected", "Create low as name-only") send
    no `exclude_groups` and therefore mean "include every group", so an exclusion
    toggled in the detail pane does not travel with a bulk create. That is pinned
    by the seven `BundleReviewView compound groups` vitest cases, the last of
    which is the bulk-door witness.

25. **The model count asks the library scan's own basename predicate, so one
    predicate decides what is a file in both doors.** `Mf-94od` fixes the measured
    disagreement behind the batch-47 finding: `classify_entry` decides a role from
    the suffix alone (Decision 2, deliberately), so a bare `._a.stl` beside
    `a.stl` was a MODEL while `library_scan` skipped it by name; a Mac-made tree
    with AppleDouble sidecars therefore read up to twice its real part count on
    the row while the project the same row created held the real files. The
    Rocket was the witness: 8 groups, each six real `.stl` plus six `._*.stl`,
    read as 96 models in 8 groups of 12 while the scan registered 48 and the
    project attached 48. It was never a new bug - git history shows the scan's
    dot-name skip predates the detector's suffix table.

    **The fix is one extracted predicate, asked by both doors.** `library_scan`
    gains `HIDDEN_NAME_PREFIX = "."` and `is_skipped_name(name, *,
    include_hidden=False)`, a VERBATIM extraction of the expression
    `_register_tree` used to inline (`name in IGNORED_FILENAMES or (not
    include_hidden and name.startswith('.'))`); `_register_tree` calls it, and
    `_walk_storage`'s hidden-DIRECTORY prune keeps its own separate test
    (`test_a_hidden_directory_is_never_walked`, which reds if the prune is
    deleted) because it answers a different question, about a directory rather
    than a name; the registration side of the basename rule is pinned end to end
    by `test_a_nested_macosx_tree_registers_its_dead_file_and_skips_the_sidecar`,
    so the scan's behaviour is bit-identical and only the detector's count moves.
    `bundle_detect` re-exports
    the predicate (`bundle_detect.is_skipped_name is
    library_scan.is_skipped_name`), STAMPS the two tables it is built from by
    reading them from `library_scan` directly rather than copying them here (a
    captured alias would freeze a stale table while the predicate kept reading the
    module that owns it), and
    adds `is_counted_entry(rel_path)` / `is_counted_model(rel_path, size)`;
    `classify_entry` stays SUFFIX-ONLY and `is_macosx_artifact` stays
    TOP-LEVEL-ONLY, because each answers a different question.

    **The predicate is the scanner's actual rule, and the junk vocabulary is
    deliberately NOT the one asked.** `entry_policy.is_junk_entry` was the
    scheduler's first proposal and the plan gate measured it wrong: the scan
    REGISTERS a non-dot file under a nested `__MACOSX/` (measured:
    `g/__MACOSX/g/plain.stl` -> REGISTERED), so routing the detector through the
    junk predicate would UNDER-count a real part; and `is_junk_entry` answers
    `False` for a plain `.hidden.stl`, so it would miss the hidden name the scan
    does skip. The basename predicate is the rule both doors can truthfully share.

    **Five counting sites ask it, and a skipped entry is counted in NO bucket.**
    `_counts` asks `is_counted_entry` and counts a skipped name in neither MODEL
    nor OTHER (the buckets are the REPORTED counts, so OTHER included);
    `_own_depth_model_count`, `_model_entries`, `root_model_count` and
    `compound_groups` ask `is_counted_model`; each of the three tests below
    asserts all five together so no one site can drift alone.

    **Evidence keeps the WALK reading everywhere it is consulted, because
    evidence is not a count.** `_cover_entry` and the document candidates are the
    obvious consumers. The less obvious one is the VERDICT: `detect_bundle_decision`'s
    R3 trio gate and its `R3:trio-no-image`/`-no-text` reasons ask
    `_walk_has_role`, not the narrowed buckets, so a bundle whose only preview and
    only document are AppleDouble sidecars is still OFFERED. Reading the counts
    there would refuse a real bundle whose only preview is `._cover.png` - a worse
    error than a cover pick that is never registered, and an unregistered cover
    already has a named reason (`largest-image-unregistered`). So on the measured
    library no row's offer/refusal moves - 0 added, 0 removed, and no detector,
    confidence, status or refusal change - and the one verdict the R3 LADDER can
    move is the one it should: a tree whose only own-depth model is a sidecar now
    refuses `R3:no-own-depth-model` instead of offering a phantom part (risk R3,
    pinned, and it cost no measured row). All three evidence consumers are pinned
    rather than asserted in prose
    (`test_an_appledouble_preview_is_still_evidence_and_the_cover_rule_sees_it`,
    `test_a_sidecar_only_preview_still_offers_the_row`,
    `test_the_document_candidates_keep_the_walk_reading_too`).

    **Two rules decide FROM the counts, so they move with them - deliberately.**
    R6's archive trio (`single_root_archive_facts`: text, model and image
    presence) and R7's group leg (`compound_groups`, `root_model_count`, and the
    compound absorb guard in `bundle_service`) read the narrowed buckets, not the
    walk, because they ask what the library HOLDS rather than whether a preview
    exists: a resource fork is not a document, not a preview and not a part of a
    product. Both directions are pinned
    (`test_the_count_driven_doors_read_the_narrowed_counts_on_purpose`): a sidecar
    `._README.txt` no longer keeps R6 out of the missing-document hole, and
    groups whose models are all sidecars name no group at all. Neither direction
    fired on the measured tree.

    **The tables and the stamp move with the rule.** `SKIPPED_FILENAMES` and
    `HIDDEN_NAME_PREFIX` join `rule_set_tables()` by value and the declared-table
    key set, `RULE_SET_VERSION` becomes `2026-11-05.2` and the derived stamp
    `0f317ee0fa2ff200a703a8c10be1c6f565d50d4263b3629a1004dffee7c0c911`,
    re-pinned in `test_the_rule_set_stamp_is_pinned_so_a_table_edit_cannot_go_unnoticed`.
    The serialised artifact gains the row's role `counts` (plus the row's own
    `groups` and its `metadata_shape` as `shape`) and the schema moves to
    `bundle-report/3`, because the baseline row carried no count at all and a
    96 -> 48 rule change was invisible in the artifact that recorded it;
    `diff_artifacts`/`format_diff` name a `counts_changed` block, compare it PER
    ROW (one schema-2 row must not suppress the deltas the other rows prove), and
    say "counts not recorded" for an older artifact rather than inventing a delta.

    **The measurement, and what the diff can and cannot say.** before/after on
    `/mnt/ceph/3D_models` (read-only), rule set `2026-11-05.1` -> `2026-11-05.2`:
    the Rocket row moves 96 -> 48 models and its eight group counts 12 -> 6;
    `import/Stagetop` is unchanged at 60 with 0 AppleDouble on disk; 0 rows added,
    0 removed, and no detector, confidence, status or refusal change. One row
    outside the Rocket moves a NON-model count - `Mini_Statue_of_the_Goddess_Hylia`
    loses `other_count` 1 -> 0, because a file named `.png` under its `images/` is
    a hidden NAME with no suffix and now falls in no bucket; its model count and
    its verdict are untouched. `--diff` reports NOT COMPARABLE by rule set, and that is the R6
    precedent rather than an error - a rule-set stamp exists to refuse a
    like-for-like comparison across a rule change. Row-level artifacts live under
    `.plans/measurements/` and are never committed.

    **One residual stays, ticketed rather than widened.** A zip LISTING's
    `__MACOSX/foo/plain.stl` is still counted although extraction refuses it: the
    junk vocabulary lives in a FastAPI-bound module mirrored by `zipJunk.ts` under
    `test_junk_vocabulary_mirror.py`, and moving it to a dependency-free home is
    its own ticket. A second, narrower conditional is named rather than hidden:
    `is_counted_entry` asks the scan's DEFAULT hidden door, so the two numbers
    agree only while both doors skip hidden names - opening the MOUNT door with
    `IMPORT_MOUNT_INCLUDE_HIDDEN=1` makes that scan register the `._*` sidecars the
    detector still refuses, which separates `model_count` from `attached` again.
    That flag is off by default and no measured tree is imported through it, so it
    is a named conditional and an untested one. The count's own edges are
    otherwise shared with the walk - hidden directories pruned for both doors,
    `RESERVED_DIRS` the same walk, symlinks and non-regular entries dropped for
    both, `Thumbs.db`/`.DS_Store` OTHER-or-skipped by name, and
    `.git`/`.gitmodules` read as NAMES by `has_repo_markers`, untouched. The
    handler that keeps the predicate honest is a BASENAME, so it can only prune
    what the walk offers it: a ZIP INTERIOR is never walked (the archive is read
    as a listing), and a `.secret/x.stl` inside one is still counted - the
    `__MACOSX` residual above is the same door, not a second one.

26. **The zip-as-project door records the cover it picked and a `_bundle`
    IDENTITY, and it is the one door that narrows the cover rule to images.**
    `Mf-qbji` closes two gaps one door along from this ADR's own create path.
    `POST /import/project` with `entries` set (`project_import.import_into_project`,
    `wants_zip = body.entries is not None`) extracts exactly those entries, finds
    or creates the project and attaches every part - and used to leave
    `cover_asset_id` NULL and `metadata_json` `{}` on every archive measured, so
    the card was blank while the images sat in the extraction, and no project made
    this way had a key for a later reader to diff against. Both are now recorded,
    for this door only.

    **The cover asks the ONE rule, over the rows already in hand.** `_cover` is
    split into `cover_for_registered(assets, cover_entry=None, *,
    allow_model_preview=True)`, which holds the ordered chain, and a one-line
    `_cover(assets, facts)` that delegates with `facts.cover_entry` - so the report
    and the zip door cannot drift apart, which is the anti-fix `_create_one` names
    ("a second rule here would be free to disagree with the report's"). The door
    calls it through `registered_from_rows`, the same mapping `registered_assets`
    now uses, because `_zip_assets` hands the extracted rows back at the call
    site: re-reading the table for bytes already in hand would be a second query
    for the same rows. It passes `cover_entry=None`, which kills the unregistered
    leg by construction - `largest-image-unregistered` names an entry a WALK
    found, and no walk runs here. Live legs: registered image ->
    `largest-image` (`-size, path`, the rule's own ordering, reused and not
    re-invented); no image -> `none`.

    **Images only, and the ticket clause that forces it.** The detection create
    path also has a `model-preview` leg, and this door deliberately does NOT use
    it. The ticket's acceptance says "gives it a cover when the archive contains a
    registered image, and no cover (not a broken one) when it does not", and its
    fix text says "pick a cover from the extracted images ... (largest registered
    image)" - three textual signals for images-only, so an archive with no image
    records `cover: {asset_id: None, rel_path: None, reason: "none", group: None}`
    and the project's column stays NULL: never a broken cover, never a fabricated
    one. **The honest consequence is named rather than hidden: a models-only
    archive gets a BLANK CARD here, while a detection-created project from the
    same files would get a rendered model, because `Mf-15xj` derived
    `model-preview` instead of reporting none.** That is a decision, not an
    oversight, and it is one line to reverse: `_zip_project_facts` calls
    `cover_for_registered(..., allow_model_preview=False)`, so dropping that
    keyword (or passing `True`) adopts the model leg for this door and needs no
    other change. Both legs are pinned
    (`test_a_zip_project_gets_a_cover_from_its_largest_registered_image`,
    `test_a_zip_with_no_image_gets_no_cover_rather_than_a_broken_one`), and the
    parity is pinned on the leg the doors share
    (`test_the_two_doors_agree_on_the_image_leg_and_only_there`: the same rows
    through `cover_for_registered` answer the same asset and reason on the image
    leg, and `model-preview` against `none` on the models-only one).

    **The key is an IDENTITY; `rel_dir` is the walk root. They are two different
    facts and the marker keeps them apart.** `_bundle.key` is
    `"zip-project:" + canonical source URL`: this door has no bundle directory,
    so a path-shaped key would be a lie AND would collide with a detection key,
    and `find_existing_project` compares marker keys by exact equality. It is
    stable across restarts, one per page - the same identity
    `find_project_by_source` already uses - and it is **provably disjoint from
    every detection key, because no `Asset.storage_path` can contain `:`**:
    `asset_service.render_storage_path` maps every composed segment through
    `sanitize_path_segment`, whose invalid set `_INVALID_SEGMENT_RE` includes `:`,
    so no storage path can carry the separator. That claim rests on an assertion
    rather than on this sentence
    (`test_no_storage_path_can_contain_the_identity_keys_separator` imports a zip
    whose folder AND filename both carry a colon, asserts every resulting
    `storage_path` is colon-free, and pins the sanitiser and the regex directly).
    `rel_dir` is the other fact: the STORAGE-relative directory the parts share,
    recorded **only** when every selected entry sits under one non-empty inner
    root, else `None` - a mixed-root or flat archive has no honest walk root, and
    a guessed one (the storage root, or the `Unassigned` bucket every unfiled
    upload shares) would make the next unfiled upload look like drift. A ONE-part
    selection records `None` as well: a single part shares a directory with
    nothing, its own model directory is not a root anything spans, and recording
    it would move the field on a re-import that extracted one new part (measured:
    `"Thing"` -> `"Thing/three"` before the guard). **A later
    reader must therefore walk `rel_dir` when it is present and fall back to
    project membership when it is `None`; it must never prefix-query the key,
    which is not a path** (`Mf-8i04`).

    **The marker is the detection shape with honest nulls and `source` naming the
    door.** Written through `project_service.create_project(metadata=...)`, i.e.
    merged into the same INSERT as the project row, so row and marker land
    together (Decision 3's reasoning). Fields: `key` as above; a NEW `source` =
    `"zip-project-import"`; `detector`, `confidence`, `archive`, `twin_verified`
    all `None`, because no detection ran; `rel_dir` as above; `groups` = one entry
    per non-empty top folder of the SELECTED entries - the folder
    `zip_service.resolve_zip_folder_id` groups the project's sections by, so the
    marker and the visible sections name the same folders (they can still differ in
    content: a group whose every entry failed to extract has no row in its section)
    - each `{name, model_count,
    foreign: false, included: true, attached}`, where `model_count` counts the
    selection's model entries and `attached` the ones that produced a row (the
    same scan-count/attach-set split `_create_one` records, and on this door the
    two can differ only by a per-entry extraction failure - an absent member, an
    oversize member, a directory member, a name conflict;
    `test_a_group_records_what_was_selected_and_what_was_attached` pins one);
    `shape: "compound"` iff there are groups; and `cover: {asset_id, rel_path,
    reason, group}`, written even when the reason is `none` because this door has
    no walk to re-derive it, with `group` resolved by basename and `None` when
    zero or several entries match. **The selection is filtered through
    `entry_policy.is_junk_entry` first**, because junk (an OS-metadata directory, a
    `._*` sidecar) is in `plan.requested` but never in `plan.writable`: it creates
    no folder, no asset and no section, so a group named for it would point at a
    section that does not exist and inflate `model_count` with a file no scan
    counts - the Mf-94od bug class one door along. **The door name does not go into
    `detector`**:
    that field's vocabulary is detection-rule names, and a door there would
    corrupt every later reader of it. Nothing reads the new `source` yet, and the
    existing readers ignore what they do not know (`_bundle_marker` returns the
    object, `_compound_splits` reads only `key` and `groups`), pinned by
    `test_the_readers_still_read_the_new_marker`.

    **A re-import fills only what is empty.** On an existing project
    (`created == False`) the cover is filled only when `Project.cover_asset_id` is
    empty, and the marker is merged only when there is no `_bundle` or its `key`
    equals this door's - and then only when the merge changes the stored bytes, so
    a re-import that records nothing new writes nothing. A human's cover and a
    detection marker are never
    overwritten - the marker is the ONLY justification `_compound_splits` accepts
    for re-offering an excluded path, so clobbering it would silently change
    report behaviour
    (`test_a_re_import_never_clobbers_a_human_cover_or_a_detection_marker`, three
    cases: seeded cover survives, a `_bundle` keyed to a bundle directory survives
    byte-identically, an empty project gains both). Both writes land in
    `find_or_create_import_project`'s own session, so the whole fill costs ONE
    transaction (measured: 1 statement when nothing changes, 3 when something is
    written - the SELECT the door already made, then the UPDATE and its commit);
    `set_project_metadata_marker` is deliberately not used (measured 2 statements
    of its own, its own session, `updated_at` churn, and it cannot fill a cover).
    `project_service.bundle_marker_of` is the one implementation of the `_bundle`
    read, and `bundle_service._bundle_marker` delegates to it, because
    `bundle_service` already imports `project_service`: the arrow points this way,
    so the layering is a DAG and no cycle can form.

    **Two branches are untouched, and pinned.** The archive-as-single-asset door
    (`entries is None`) and the gated branch pass no cover and no marker: neither
    is the archive door, and a gated import stores no files, so claiming a cover
    for it would describe files that do not exist
    (`test_the_whole_zip_as_one_asset_path_is_unchanged`,
    `test_a_gated_zip_import_records_no_cover_and_no_bundle_marker`). This door
    still runs no detection and enriches nothing beyond page metadata - the honest
    difference between "I have this archive, put it in a project" and "detect,
    enrich, confirm".

    **No migration, no wire change.** `_bundle` lives inside the existing
    `metadata_json TEXT` column and already reaches the client through
    `ProjectOut.metadata_json`, so no schema, serialiser, route or migration moves.
    **Measured cost, not estimated:** one zip import through this door costs the
    before-state count + 1 statement when a cover is picked (the `get_asset`
    guard inside `create_project`); the marker is free because it rides the create
    INSERT; an archive with no registered image now costs +0, because no cover is
    written. The row-level before/after artifacts are
    `.plans/measurements/2026-11-06-zip-project-mfqbji-{before,after}.json`, the
    same four synthetic zips, and a harness rebuilt from the before artifact's
    recorded description because the original was not kept.

27. **A bundle folder that grows after its project exists is surfaced, never
    automated - computed on every read, honest in three states, and MODEL-scoped.**
    `Mf-8i04` answers the owner's question ("can it add the new stuff to an
    existing project?") today's answer being "no, and silently": nothing watches
    a folder for growth, and the already-created row stays hidden because the
    marker key does not change when files appear inside the folder. The feature
    is a PROMPT with the existing bulk-membership action behind it, never a
    silent attach - membership is deliberate in a household vault. One new
    contract module, `bundle_drift.py`, answers both surfaces (the project route
    and the queue row), so they cannot disagree about the same folder.

    **Drift is computed on every read, never stored.** A stored count is a false
    count the moment a rescan registers the next part, and the parts it names are
    rows the app already holds (`Asset.storage_path` and `ProjectAsset`), so
    there is nothing to keep in sync and no migration - no column, no index, no
    `models.py`. The read costs the project row plus ONE prefix SELECT on the
    project view, and ONE report-wide `(id, storage_path, size)` index on the
    queue side. Both are measured in the before/after pair rather than quoted:
    `GET /bundles/detect` ran 8 statements before this change and 9 after on the
    same harness (the one added statement is the report-wide index, constant in
    the number of candidates), `GET /projects` stayed at 2, and the new route
    runs 3 in the suite's authenticated harness - the caller's auth lookup, the
    project row, and one prefix SELECT that also carries the membership flag.
    That is the project row's pair plus one query when the state is `known`, the
    pair alone for `not_applicable` and for `unknown` with no root (no prefix
    query at all), and the same 3 for a STALE root that answers `unknown` - the
    presence probe reads that one SELECT. Pinned by
    `test_a_grown_bundle_folder_reports_the_parts_that_are_not_in_the_project`,
    `test_a_hand_made_project_reports_no_drift_question`,
    `test_a_zip_identity_marker_reports_unknown_rather_than_zero` and
    `test_a_renamed_bundle_folder_reports_unknown_rather_than_a_false_zero`.

    **Three states, exhaustive, because count honesty has two owners.**
    `not_applicable` (no `_bundle` marker - a hand-made project or malformed
    metadata) renders no notice and a `null` count. `known` is the honest number:
    `0` renders NO notice at all, never a rendered zero, and `N > 0` renders one
    notice and one bulk add whose argument is the count's own evidence
    (`count == len(asset_ids)`). `unknown` (a marker that names no path this
    server may query, or names a root no registered row lives under) renders an
    en dash U+2013. The server picks the state and
    never formats a number; the client formats, with the one shared formatter
    (ADR 0011's en dash rule).

    **`known` also requires a walk root that still holds a registered row.** A
    renamed folder leaves the marker's creation-time `rel_dir` pointing at a
    path no registered row lives under, and a `0` there is exactly the false zero
    the ticket forbids, so a stale root answers `unknown` (an en dash) instead.
    The probe is free on the queue - it reads the report-wide registered list
    the row already holds - and on the route it is part of the existing prefix
    SELECT, not a query of its own. The tradeoff, stated honestly: presence is
    asked of REGISTERED ROWS - any row counts, so a root holding only a
    `cover.jpg` still reads `known`/0 - not of the folder table, because the
    route may not spend a statement on it, so a bundle root whose registered
    rows were all deleted (or a folder that exists on disk with no registered
    row under it) now answers `unknown` where it used to answer `known`/0 - the
    conservative direction, an en dash instead of a zero the server cannot
    support.

    **The walk root has two key spaces, and the identity key is never
    prefix-queried.** `walk_root(marker)` is `rel_dir` when it names a
    non-empty path (both doors record it); otherwise, for a marker that carries
    no `source` - only the detection door omits it - the marker's `key`, which
    `_create_one` writes as the row's storage-relative `rel_path`; otherwise
    nothing. A zip-project marker's `key` is an IDENTITY
    (`zip-project:<canonical url>`) and its `rel_dir` may legitimately be `None`
    (Decision 26: mixed-root, flat and one-part selections), so a prefix query
    on it matches nothing - the false zero this ticket forbids - and the state is
    `unknown`, never `0`.

    **Drift is MODEL-scoped, and the over-count is measured.** The set is
    registered `bundle_detect.is_counted_model` rows under the root, minus the
    prefix of any group the marker records as `included: False`, minus this
    project's membership. Counting every asset reads the `cover.jpg`/`notes.txt`
    files creation deliberately never attaches: on the measured compound tree
    the naive all-asset difference is **26 at creation against an honest 0**, and
    **27 against 1** after one new part joined
    (`.plans/measurements/2026-11-06-bundle-drift-mf8i04-{before,after}.json`).
    The predicate is the scan's own basename model rule (Decision 23's Mf-94od
    change), which is why an AppleDouble `._a.stl` never counts as a part. It is
    deliberately NOT the report's containment set: that one is the candidate's
    own `_own_paths` tested for a model SUFFIX, so the two diverge in both
    directions - that one counts a `._a.stl` copy and excludes a nested child
    bundle's paths, while drift does the opposite in both places. The first
    divergence is pinned (`test_drift_is_model_scoped_so_support_files_are_never_drift`);
    the second is the accepted nested-bundle cost recorded below; and the
    excluded-group subtraction is a third place drift is deliberately STRICTER.

    **`groups` is the wrong basis for the notice.** The marker's group list is a
    creation-time snapshot: a non-compound detection marker has no `groups` yet
    is drift-able, and a brand-new subfolder is not a group at all - a per-group
    notice would silently miss exactly the growth this ticket exists for (a false
    zero). Hence ONE project-scoped notice, which is also one bulk add and one
    click. **An excluded group is subtracted** because its models stay registered
    and unattached by design; without the subtraction the prompt would never
    clear and "Add them" would attach exactly what the operator excluded. This is
    deliberately STRICTER than `find_existing_project`'s containment, which
    ignores the exclusion; the divergence is pinned by its own test rather than
    left to be discovered.

    **`bundle_serialize.py` is deliberately NOT extended.** The drift count
    depends on membership rows the artifact never records, so carrying it would
    make two artifacts of one tree at different project states read as a rule
    change - a false "changed" in the very comparer that exists to catch
    unreported change. A future drift-predicate change is measured with a
    bundle-drift before/after artifact instead (the pair above is that
    measurement), so the omission is recorded rather than left as an omission.

    **The ledgers move with the route.** One new `READ` route
    (`GET /project/{project_id}/bundle-drift`) moves the permission matrix
    `101 -> 102` routes (11 public, 36 -> 37 read, 23 contribute, 13 write,
    18 admin), `test_permissions_matrix.py` gains its caller, `CALLS` and
    `EXPECTED_STATUS` entries and the bumped size pin, and `docs/PERMISSIONS.md`
    gains its table row. `audit.assert_audit_coverage` skips GETs, so no audit
    action or `AUDIT.md` row appears. Deliberately NOT a field on
    `GET /projects`: that list is one statement for many projects with its own
    statement pin, and App refetches it on every `assetsVersion` bump; the client
    asks the per-project route only when the project carries a `_bundle` marker,
    so a hand-made project costs no request at all.

28. **A claimed ancestor whose whole payload sits in ONE child is that child's
    wrapper, and it is offered as one `low` row.** `Mf-ny89` answers an owner
    ruling of 2026-11-09, taken on the live deployment and quoted verbatim:
    "Fates End - Wizard Tower which as previously extracted, now shows as Fates
    End - Wizard Tower, but it claims it's a zip even though it's extracted and
    it won't let me create it as a project." And his expectation, also verbatim:
    "one row, key `import/01_Fates End - Wizard Tower`, titled from the
    directory/archive, 15 models / 3 images / 1 text, status READY, creatable -
    with `01_Wizard` as a group inside it, because folders are 1:1 with disk and a
    subfolder of the project is a section, not a separate product." The tree is
    `import/01_Fates End - Wizard Tower` holding nothing but `01_Wizard`, which
    holds all twenty of the tower's files, beside the outer
    `import/01_Fates End - Wizard Tower.zip` that declares exactly those twenty.
    `Wizard_Dice_v5.4_fixes.txt` inside the child is what lets R3 offer the child a
    row of its own; the claim then strips all twenty files out of the parent's own
    view, the folder pass records the parent in `claimed_ancestors` alongside that
    strip, and Decision 18's branch answered with the container row - a `blocked`
    / `archive_only` line nobody could create from, which is the sentence the owner
    read as "it claims it's a zip even though it's extracted". This decision
    changes that ONE answer. It is the third step of the lineage Decisions 17 and
    18 started - 17 makes a nested archive opaque, 18 lets a verified archive name
    a directory, this one says which of the two rows a wrapper earns - and both
    of them are left exactly as written, including 18's guard, which this decision
    reuses rather than redefines.

    **The single-subtree promotion.** The `claimed_ancestors` leg of the pairing
    loop now asks one shape question before it answers: does this directory's whole
    payload sit inside one child? `bundle_service._single_claimed_child_subtree`
    answers it - the child's name, or `None` - and when it answers, the directory is
    offered in `found` as an `archive_owned` row keyed at the DIRECTORY, the same
    row Decision 18 promotes elsewhere, instead of entering `pair_only_specs`.
    Which listing each half of the question is asked of is the whole design, and it
    is the Mf-wd3v trap named for the second time in this file. `work` is asked
    ONLY "empty?", never "what shape?": `work[rel_dir]` is the post-claim view
    `bundle_service._forget_claimed_subtree` has already emptied, and an emptied
    listing answers "nothing" to every shape question, which is how a verified pair
    with real models behind it came to be refused for want of facts. Emptiness is
    the one question that view answers honestly - it IS what the claim left behind,
    and it is the same fact that put `rel_dir` in `claimed_ancestors`, since
    `_forget_claimed_subtree` has exactly one call site and runs together with the
    marking, so the two are always created as a pair. `folded` is asked ONLY "one
    first segment?": it is the unclaimed full-subtree listing, so it is the only
    listing that says what the directory actually holds, and it is read for a
    segment NAME and nothing else, because the counts, cover and roles it would also
    describe are the caller's decision, not evidence for this one. No walk, no
    stat, no store - a pure read of two listings the pass already built.

    **Confidence stays `low`: promotion is not new evidence about the shape.**
    `bundle_detect.archive_owned_facts` is the one facts function and it is the same
    call the non-ancestor promotion makes, so `detector` stays `generic-bundle`, the
    badge still says the shape was inferred rather than read, and every provenance
    field the row could carry (`creator`, `licence`, `licence_url`, `source_url`,
    `thing_id`, `description`) stays `None` for Decision 18's reason: a stem is
    evidence of WHICH thing this is, never of who made it. Promotion adds an
    ownership judgement - this directory is its child's wrapper - and an ownership
    judgement is infrastructure, not a naming guess, so it cannot raise a badge that
    measures naming evidence. Decision 7's orthogonality is what makes that
    coherent: what promotion changed is `status`, which the database owns, and not
    `confidence`, which the rules own.

    **The child's dissolve, and the honesty of it.** Every non-archive candidate
    strictly below the promoted parent is dissolved with `REFUSAL_MEMBER_OF_BUNDLE`
    and `override=True` - the absorption shape `bundle_service._compound_pass`
    writes when R7 takes the rows a container was hiding, reused rather than
    re-invented so that this module has ONE way for a row to be taken into a bigger
    one. The word is not new and that is the point. The child's
    `member-of-bundle` entry was ALREADY on the ledger before this change, by an
    indirect route: the archive-containment container strip dropped the LOW child
    because its subtree held an archive-only row, and `bundle_service._reconcile_refusals`
    relabelled the drop `member-of-bundle` because the directory sat strictly inside
    the row that survived. What was wrong was the ownership, not the vocabulary -
    the payload belonged to no row (the parent was `blocked` with `assets_total` 0),
    so the ledger named a bundle that did not exist and pointed at nothing a create
    could attach. Now it names the row that really owns those files. Archive rows
    below the parent are deliberately NOT dissolved: a zip inside a detected bundle
    is a member and `REFUSAL_NESTED_MEMBER` is the word that says so, the member
    pass still owns that answer, and the dissolution's `not candidate.is_archive`
    filter is what leaves it alone.

    **The dissolve is a deferred commit, and that is load-bearing.** The branch
    RECORDS the keys it intends to take; the ledger entries and the row removals are
    applied later, once the nested-member pass has had its chance to veto the
    promotion itself - `bundle_service._nested_archives` can empty the promoted
    directory's `pairs`, and the prune after it drops an `archive_owned` candidate
    whose pairing is gone. Dissolving at branch time therefore let a promotion that
    was going to be unwound take a row with it: the child lost its row, the parent
    lost its row, and no row owned the payload - the `assets_total` 0 / nothing
    creatable symptom this ticket was filed about, re-created one level up, with the
    ledger naming a bundle that no longer existed. The invariant the commit point
    enforces is **no `member-of-bundle` entry may name a row that does not exist**,
    and applying it after the prune is what makes that structural rather than lucky;
    `test_service_a_promotion_the_nested_member_prune_vetoes_takes_no_rows_at_all`
    pins it, and moving the commit back into the branch reds that test alone.

    **One route cannot reach this branch: the entry root.** The pairing above this
    branch is reached by the STEM rule in practice. `bundle_service._entry_root_pair`
    judges containment with `entry_root_verified(zip, root, work.get(folder, ()))` -
    the EMPTIED post-claim view - so a claimed ancestor fails containment and never
    enters `pairs` at all (measured: `promotion:no-own-depth-model` and
    `container-rule`, the child keeping its row). That asymmetry predates this
    decision and Mf-wd3v left it untouched; it is recorded here because the
    surrounding text says a pairing qualifies "on the entry-root route or the stem
    route", and only the stem route can actually promote a wrapper.

    **The `groups: []` residual, and why filling it was refused.** The promoted row
    ships `groups: []`. `groups` is R7's field alone: `BundleFacts.groups` defaults
    to `()` and has exactly ONE writer in the module,
    `bundle_detect.compound_bundle_facts` returning `replace(facts, groups=groups)`;
    `archive_owned_facts` never sets it. So the operator's literal sentence -
    "`01_Wizard` as a group inside it" - is not what the wire says, and the Review
    view renders a compound breakdown only for a row that HAS groups, so no panel
    names `01_Wizard` on this row. Teaching `archive_owned_facts` to emit
    `BundleGroup`s was REFUSED, not deferred: `BundleGroup` is
    `{name, model_count, foreign}` and the report derives `separable` from R7's
    absorbed set, so emitting one means deciding `foreign` (a vendor thing id in the
    name, or a group carrying its own documents) and `separable` (whether R7 claimed
    it) - both of which belong to R7's ATTACH decision (Decision 23's shape
    predicates, Decision 24's excluded-group split), not to a wrapper promotion that
    has no opinion about either. **This is a reading, and it is labelled as one
    deviating from the literal wire reading so the owner can overrule it:** the
    reading is that `01_Wizard` survives as a group because it survives on disk -
    every member of the created project keeps `01_Wizard` inside its
    `Asset.storage_path`, and the project's folder sections are derived from exactly
    those paths - so the subfolder really is a section of the project and not a
    separate product, which is the half of his sentence that carries the intent.
    What the literal reading would require him to see is the child NAMED on the row:
    a breakdown listing `01_Wizard` and its part count beneath the tower's row, the
    way `import/Stagetop`'s two `Wave` groups are listed under theirs. He does not
    see that today; what he sees is one `ready` row titled from the directory whose
    cover, counts and create all run through `01_Wizard`.

    **A stronger child still wins, and the parent keeps the `pair_only` row
    Decision 18 gave it.** `bundle_service._holds_stronger_candidate` is UNCHANGED -
    same subtree scan, same `bundle_detect.STRONG_CONFIDENCES` membership test, same
    docstring - and it now sits as a LEG of the promotion test
    (`single_child is not None and not _holds_stronger_candidate(folder, found)`)
    rather than as a guard standing after it. **It must not be hoisted above the
    `claimed_ancestors` branch, and hoisting remains forbidden.** The reason is the
    shape of the two strips, not taste: the container strip asks
    `key.startswith(candidate.key + "/")` over `contained_keys`, so it drops
    candidates that CONTAIN a contained key and never ones nested INSIDE one, and
    `bundle_service._nested_archives` returns archives only - which together mean a
    strong candidate below a `pair_only` parent is never stripped by either. A hoist
    would therefore not merely refuse the promotion, it would take the parent's
    `pair_only` row down with it, and that row is Decision 18's visible answer for a
    container the operator has on disk. "The parent loses entirely" would be a
    regression, not a fix: one child is not automatically a weaker one - a claimed
    Thingiverse export earns `high` from its document pair and a claimed id-named
    folder earns `medium` from its name - so the strong child has to lose the
    promotion while still answering as the pair-only container it always answered
    as, and the fall-through to `pair_only_specs` IS that answer. Witnessed in both
    strengths: `test_service_a_verified_twin_never_promotes_a_directory_holding_a_strong_bundle`
    pins `high` (`import/Q`'s claimed export keeps its own row while
    `by_key[CLAIMED_PARENT_DIR]` still answers `pair_only=True, archive_owned=False`
    and its twin answers `twin-consumed`), and the new
    `test_service_a_single_medium_child_keeps_its_own_row_and_its_parent_stays_pair_only`
    pins `medium` (`import/M` over R4's `5390184_ARTICULATED_ice_dragon__fixed_`:
    `_single_claimed_child_subtree` answers YES and the strength leg answers NO, so
    the parent stays `blocked` / `archive_only` and the dragon keeps its own `ready`
    `medium` row). The two together are what stop either leg being "tidied" into the
    other.

    **The witnesses, and which leg each one actually pins.**
    `test_service_the_measured_live_tower_is_promoted_at_its_real_counts` is the
    live tree at its measured counts;
    `test_service_a_verified_twin_of_a_claimed_ancestor_is_promoted_on_its_single_child`
    is the same shape at the fixture's own counts;
    `test_service_a_promoted_emptied_claimed_ancestor_holds_no_archive_row_of_its_own`
    pins the interlock - the emptied ancestor is the tree's one row and no archive
    row survives below it for that row to swallow - and pins with it why `W`'s LOW
    child is promoted while `Q`'s HIGH child is not; and
    `test_service_a_tower_whose_child_holds_no_archive_of_its_own_is_still_one_row`
    is the witness that pins the DISSOLUTION itself, because in the live tree the
    child happens to hold a candidate-shaped archive and is therefore ALSO dropped by
    the archive-containment strip, so deleting the dissolution alone reds nothing
    there. The two shape negatives are
    `test_service_a_claimed_ancestor_holding_two_claimed_children_is_not_promoted`
    (two claimed child subtrees) and
    `test_service_a_claimed_ancestor_with_a_file_of_its_own_is_not_promoted` (one
    subtree plus a loose `leftover.stl` of the parent's own). The measured subtlety:
    the loose-file shape is refused by BOTH legs - a file at the parent's own depth
    makes `work` non-empty AND adds a second first segment to `folded` - so it
    cannot discriminate between them. The PILE is what pins the `folded` leg: its
    `files/` name is in `bundle_detect.R3_MODEL_DIRS`, so its models count as
    own-depth, the inherited `Mf-snd8` cardinality bound never runs, and the archive
    answers `twin-consumed` rather than `promotion:no-own-depth-model` - which is
    exactly what proves the refusal came from reading two segments off `folded`. And
    only a PARTLY claimed single child pins the `work` leg:
    `test_service_a_parent_whose_single_child_is_only_partly_claimed_is_not_promoted`
    holds `A/` and nothing else, so `folded` answers "one segment" YES while the
    claim left `A/own0.stl` and `A/own1.stl` standing under that one segment - and
    promoting there would dissolve a claimed grandchild into a row that does not own
    it, Decision 18's swallow wearing the shape rule's clothes, with no other test
    in the file reding when it happens.

    **The R7 absorbability residual: monitored, not disproved.** A promoted parent
    is a real row, so R7 can now see it as absorbable - the absorbing side's only
    row-set guard is `not candidate.pair_only`, and while this directory was
    pair-only it was exempt by that filter alone. `bundle_detect.compound_bundle_facts`
    is untouched, so no NEW compound key can appear anywhere: the set of rows R7 can
    build is exactly what Decision 23 described. But a compound that could already
    form above a tower would now take the tower's row instead of leaving it alone,
    and take the operator's only creatable line with it. The live fixture asserts
    `scan.absorbed == ()` on precisely that tree, which is a measurement of the tree
    we have and not a proof about trees we do not have - so this is recorded as an
    open residual under observation, and the thing to look for is a compound row
    whose absorbed set contains an `archive_owned` key.

    **The measured before/after, on the live-shape fixture.** Before: ONE row keyed
    at the directory, `blocked` / `archive_only`, `assets_total` **0** (while its own
    `model_count`/`image_count`/`text_count` already read 15/3/1 off the unclaimed
    listing - a row advertising parts it could not attach), cover reason
    `largest-image-unregistered` with `asset_id: null`, and the child refused
    `member-of-bundle`. After: the same single row, `ready`, `assets_total` **20**,
    15 models / 3 images / 1 text, cover reason `largest-image` carrying a real
    `asset_id` and a `rel_path` that runs through `01_Wizard` - because the child's
    payload IS the row. The refusal ledger is byte-identical across the change, four
    entries both sides: `import` `R3:no-own-depth-model`, the twin
    `twin-consumed`, `01_Wizard` `member-of-bundle`, and
    `01_Wizard/01_Wizard_v5.4.zip` `nested-member`. That is the shape of the change:
    a row moved, no verdict did. Every count above is fixture-derived, read off the
    files the fixture wrote - and **the number `21` in the ticket note is wrong**,
    the product of a loose `LIKE '<prefix>%'` that also matched the sibling
    `import/01_Fates End - Wizard Tower.zip`; it is named here because an ADR reader
    who starts from 21 will otherwise spend an afternoon chasing a file that is not
    inside the tower. The mutation that reproduces the bug is deleting the
    `_single_claimed_child_subtree` leg of the branch - the row goes back to
    `blocked` / `archive_only` with `assets_total` 0 and nothing creatable in the
    tree. Deleting the `_holds_stronger_candidate` leg beside it is Decision 18's
    swallow in new clothes, and it reds the two strength witnesses, not the tower
    one: a LOW child is exactly what this branch exists to promote.

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
* **A verified archive widens the offer, and the widening is accepted.** A
  directory the format rules would have refused - models alone, models plus a
  README, models plus images - is now offered at `low` when an archive beside it
  verifies, and a directory that holds a claimed child bundle and still carries
  a model at its own depth gains a visible `blocked` / `archive_only` row where
  it previously vanished: visible, not creatable. A pile of flat depth-0 models
  with a matching twin would now be promoted, because the archive proves it is
  one thing. That is this ticket's accepted trade, stated rather than hidden; the
  own-depth/cardinality guard still refuses every pile of unnamed model-bearing
  subdirectories - and a refused pile produces no row at all, not even the
  blocked one, because `archive_owned_facts` answers `None` before the container
  rule is consulted - so the widening cannot promote a pile of things on its
  members' models.
* **A stem pairing costs one extra central-directory listing per undetected stem
  twin.** At report time each such twin's archive is read once to re-verify
  containment, uncached, on the `READ` route that is already the most expensive
  `GET` in the product. That cost has not been measured on the owner's
  169-archive library, so this document does not pretend to a number, and the
  standing caveat is unchanged: a declared zip size is header data, so a lying
  header makes a pairing wrong and never a bundle corrupt.
* **A container whose own payload an archive verifies is still refused, and
  stays visible when it has a model of its own to judge.** The bullet above - a
  container directory is no longer offered - still holds: a verified pairing
  makes such a directory visible as a blocked `archive_only` row rather than
  creatable, and only when the directory still carries a model at its own depth,
  because that bound is the only shape guard the promotion has (Decision 18); a
  container with no model at its own depth is absent, not blocked. `Mf-6b82`'s
  generic-root refusal is untouched for the route that uses the entry root to
  name a directory.
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
* **An asset-matched row chips "In project", not "Created".** The chip is a
  claim about who made the project, so it follows `existing_project_match`: the
  shared-files identity reads "In project" and carries the detail line's own
  sentence as its hover `title`, so the chip and the panel cannot disagree. The
  bucket and the count are unchanged because they key on `existing_project_id`,
  not on the words: the row is still hidden with the created rows, and an
  asset-only queue still says "Every detected bundle has a project."
* **A nested archive is opaque, so a directory whose only payload is a zip is not
  a bundle.** Its entries never count as evidence, which is what keeps the
  Clockwork directory one bundle and a zip-only directory from becoming one on
  the zip's contents. The import door extracts in place when the operator wants
  those models counted. A duplicate whose only twin is a bundle member is not
  reported as reclaimable when the member was the suppressed copy, which is the
  safe direction and is stated rather than hidden.
* **A `low` rule now covers 60 parts, 48 parts and 27 models on a shared word and
  a majority, and that is the accepted trade.** Three rows replace twenty on the
  owner's library - the eleven Stagetop children, the eight Rocket variants and
  the one foreign `high` row - for 201 -> 184 rows overall; nothing is deleted, no
  file is moved and the disk is unchanged. On that library all three new rows are
  `blocked` / `unresolved_directory`, so a blocked row stays blocked because the
  folder does not resolve and not because a detector said so; and where such a
  folder does resolve, what a create attaches is its own snapshot, which is
  exactly how `xbox360_shell`'s 27 previously invisible models come back. The
  judgement behind the row is the weakest this document sanctions - a token two
  group names share, or a majority of models at the top plus one vendor id - and
  it is deliberately correctable rather than final: C2 (`Mf-ao6r`) owns the
  created project's membership and the flagged group's split and C3 (`Mf-aaqi`)
  owns grouping the members by folder, so the operator's remedy for a wrong
  compound row is the same as for any other `low` row - reject it, dismiss it, or
  fix the created project.

* **A grown bundle folder is no longer silent, and adding is still deliberate.**
  The project view shows one notice with one bulk add when the folder holds parts
  the project does not, the Review queue's already-created row says the same
  number in its own words, and both clear the moment the operator adds them - no
  create action is re-offered, and a fresh project shows no notice at all rather
  than a zero.
* **The accepted wrong direction is a nested bundle.** A separate bundle living
  inside an already-created bundle folder is counted by the recursive prefix
  (its models are registered under the root and are not members), so such a
  folder would prompt for another bundle's parts. Accepted and recorded: the
  report already refuses nested rows as `member-of-bundle` and R7 absorption
  usually claims the subtree as groups, so the shape is rare; the fix would need
  a per-row walk, which the report must not do.
* **`bundle_serialize.py` stays one rule set deep.** The artifact records what
  the rules say about a tree, not what the database says about a project, so a
  drift count in it would turn a membership change into a rule change in
  `diff_artifacts`; a future predicate change is measured with a bundle-drift
  before/after artifact instead.
* **The read costs what a read should.** The queue gains exactly one statement
  per report (one report-wide registered index, constant in the number of
  candidates) and the project route costs the project row plus one prefix SELECT
  - no per-part query, no stored count, no cache to invalidate.
* **A wrapper of one claimed child is now a row, and its child's name lives on disk
  rather than on the wire.** `import/01_Fates End - Wizard Tower` answers one
  creatable `low` row where it used to answer a blocked `archive_only` line with 0
  assets: nothing new was detected, nothing was deleted, no file moved, and the
  refusal ledger is unchanged - the promoted row reads `ready` because its folder
  resolves and its models are registered (Decision 7), never because a rule got more
  confident. Two accepted costs travel with it and both are argued in Decision 28
  rather than left to be discovered: the row ships `groups: []`, so the child
  reaches the operator as the folder section every member's `storage_path` still
  carries instead of as a named breakdown on the row (a reading of the owner's
  sentence, and labelled one), and a promoted parent is now absorbable by R7, which
  the `not candidate.pair_only` filter used to forbid - monitored by
  `scan.absorbed == ()` on the live fixture, not proven impossible.
## Alternatives considered

- **A `bundle_candidates` table, populated by a scan, confirmed later.**
  Rejected: it needs a migration, a staleness policy for rows whose directory
  has since moved, a sweeper for abandoned rows, and a second source of truth
  next to the disk. The report is a pure function of the tree and the rows, so
  the table would exist only to make the answer older.
- **Exclusions stored in `appconfig`** (one map of `key -> excluded group
  names`) instead of derived from the created project's marker. Rejected for the
  `bundle_candidates` reason above - the staleness policy and the sweeper that
  bullet names are exactly what an exclusion map would need as well, because a
  stored exclusion goes stale the moment the project it describes is deleted -
  and for a second reason that bullet cannot state: an exclusion is a property
  of the project that was created, so it should live and die with that project,
  not outlive it in a settings key that a second operator's next create could
  inherit.
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
  **Re-scoped by `Mf-it2a`:** the stem route applies the same declared-size
  containment, through the same predicate, so both pairing routes compare header
  sizes and neither reads bytes.
  **Re-scoped by `Mf-z4vv`:** content hashing is adopted for duplicate identity
  and reporting only - the `duplicates` list and `duplicates_text`, size-first,
  and only within equal-size groups - and never for `twin_verified`, pairing or
  an integrity claim.
- **Literal full-set containment over every registered asset of the bundle**
  (models, images and text). Rejected, and this is the reading a reviewer has to
  sign off on. A created project only ever holds the models, so an owner's
  hand-made project over the bundle's files holds the models and not the previews
  or the documents the scan also registered, and the rule would report that
  project as NOT owning the bundle it was made from. It would be correct about
  the wrong set, and the bug this ticket exists to fix would survive in exactly
  the case that motivated it. The containment set is therefore the models a
  create would attach, pinned by
  `test_service_a_project_holding_the_models_owns_a_bundle_that_also_has_images`.
- **The weaker intersection rule, where a project holding one of the bundle's
  files claims it.** Rejected: one shared file is enough to hide a bundle behind
  a project that has nothing to do with it - a generic `cube.stl`, a reused
  render, a hand-copied document - and the row would leave the queue as already
  created without anyone deciding it should. Containment keeps the claim to a
  project that represents the whole bundle, and
  `test_service_a_project_holding_some_of_the_files_does_not_claim_the_bundle`
  is the witness: 11 of 12 models claims nothing.

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
  precedence - R1 before R2 before R4 before R3, and a marker-less README falling to
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
  every provenance field `None`, and equally renders a models-only listing and
  one carrying a README and no image - images and text are decoration once the
  archive has proved the directory - while still refusing a pile of two unnamed
  model-bearing subdirectories and models at depth 2 (the `Mf-h0p8` depth bound
  and the `Mf-snd8` cardinality rule hold here too); `detect_bundle` still
  returns `None` for the very listing `archive_owned_facts` offers, which pins
  that the low offer is the service's verified pairing and never a looser R3; and
  `archive_payload_contained`, the one predicate both routes use, is proved from
  literals: an archive whose paths are the folder's pairs by identity, an archive
  under one wrapper pairs after exactly one `common_entry_root` strip, two strips
  are refused (`a/files/x.stl` against a folder holding `x.stl`), `__MACOSX`
  cruft is dropped on both routes and an all-cruft archive verifies nothing, a
  declared-size mismatch and a name mismatch each pair nothing, and the
  entry-root route refuses the identity shortcut - an archive whose paths happen
  to be the folder's, but which does not sit under the named root, is `false`.
  In `tests/test_bundle_service.py`,
  against a temporary root: the real
  Clockwork pair - `import/17_ClockworkMonster/` (12 STL, 2 PNG, no text),
  `17_Fates End - ClockworkMonster Tower.zip` and its byte-identical ` (1)`
  duplicate - is exactly one row keyed at the directory, `generic-bundle` /
  `low`, `ready`, `assets_total` 14 (the scan registers every file, so a preview
  is an asset even though the import door would not take it), with
  `twin_verified: true` and the non-duplicate name as `archive.rel_path`, neither
  zip a row of its own, and `create` writing one project with the directory's 12
  models attached (Decision 18); the same 14 files with no archive beside them,
  under a name R5 refuses, are offered by nothing, `iter_bundle_candidates` is
  empty and `create` answers `unknown bundle` with no project - the silence the
  rule needs, now that the title-shaped spelling of the same 14 files is a
  `title-name` row instead; a pile of two
  real bundles plus its own loose model and render, paired with an archive rooted
  at the pile and containing exactly those two loose files, is refused: the
  children are `ready` rows and the pile is one `blocked`/`archive_only` row that
  creates nothing, so create-all makes two projects and none named `Pile`; and a
  pairing rooted at `Pile` never offers `Pile/Sub`, a models-and-images directory
  of its own with no archive; a generic
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
  promoted directory holding a nested archive is one row with that archive as a
  member (never a second row, never a pairing, never a duplicate-to-delete), and
  the directory survives a strip that would otherwise see only that member; a
  LOW container holding a promoted directory is still dropped - the container
  strip fires for a promoted candidate too, so `Collection` disappears while the
  paired directory it wraps is offered.
- The same two files also pin the two pairing routes and the fault they fix
  (`Mf-it2a`). In `tests/test_bundle_detect.py`, from literals: the stem route's
  wrapper decision is made once - identity, one strip, never two - and the
  entry-root route's strict strip refuses the identity shortcut, both through
  `archive_payload_contained`; and `archive_owned_facts` refuses a pile of two
  unnamed model-bearing subdirectories even when a pairing would verify the
  directory, which pins that own-depth/cardinality is the only shape guard left
  and that emptying it promotes the pile. In `tests/test_bundle_service.py`,
  against temporary roots built from real directory names and shapes only: the
  four owner-library pairs - `SnVB623` and `SnVD623` (models only),
  `ultimate-card-box-v4-model_files` (models plus a README, no image) and
  `Cinderwing3D - Crystal Dragon` (models plus images, its archive rooted at
  `files/`) - are each offered at `generic-bundle` / `low`, `ready`, with
  `twin_verified: true` and only their own models attached, and neither zip a row
  of its own; the `RepRap-MultiFilament` pile of two unnamed model-bearing
  subdirectories is itself offered by nothing even though an archive beside it
  verifies - its child `Alpha` is R5's `low` row and `Beta` is floor-refused, so
  deleting the stem branch leaves those four directories as `title-name` rows
  instead of `archive_owned` ones - which reds the pair witness all the same -
  and removing the own-depth bound promotes the pile; the `endorphin` pile holding
  two claimed bundles plus its own loose model and render, with a verifying stem
  twin, is one `blocked` / `archive_only` row whose children are `ready`, and
  create-all makes two projects and none named after the pile - removing the
  `claimed_ancestors` refusal promotes it; an archive differing by one declared
  size and an archive whose names differ each pair nothing - no row and no
  `twin_verified`, under the names R5 refuses that this witness uses - and a
  `_stem_pair_verified` that returns `True` would promote the first; a stem twin
  in a different parent and an entry root naming a directory in another parent
  attach nothing to the namesake, which R5 now offers as a `low` row of its own,
  so each witness moved from "no candidates" to "exactly this one, unpaired" -
  with `Mf-6b82`'s two guard
  tests (`test_service_a_nested_archive_cannot_pair_a_sibling_directory`,
  `test_service_a_pairing_names_the_directory_not_its_descendants`) staying
  green unchanged; and a promoted stem row whose directory has since gained one
  extra file still reports `twin_verified: true`, because the basename sets are
  no longer equal while containment still holds - the dispatch test that would
  fail if a promoted stem row fell back to the names-only comparison.
- The duplicate-archive contract (`Mf-z4vv`) is pinned at three levels. In
  `tests/test_bundle_detect.py`, from literals: `equal_size_archive_groups` keeps
  only sizes that occur twice; `duplicate_groups` puts the shortest name first
  and reports `reclaimable_bytes == size`, while a lone digest and two equal-size
  archives with different digests produce no group;
  `duplicate_archive_paths` excludes the authoritative path; and
  `duplicate_report_text` is empty without groups and otherwise carries one
  `keep` line, one `duplicate` line, the digest and the reclaimable header. In
  `tests/test_bundle_service.py`, against a temporary root: four constructed
  duplicate groups (payloads 400/300/200/100, each original plus a `copyfile`
  copy) report exactly four groups of two paths with
  `reclaimable_bytes == size ==` the file's `stat` size and a primary that is the
  shortest name, `duplicates_total_bytes` equals the sum of the four sizes, the
  text carries `keep`/`duplicate` lines and the digest, no copy is a bundle row
  and every original is; an equal-`stat`-size pair differing by one payload byte
  is no duplicate and both stay candidates in the report and in
  `iter_bundle_candidates`; a counter around `_content_digest` proves the
  size-first filter reads only the one shared-size pair (`2` calls) while a
  library of four same-size archives is read exactly once each (`4` calls) and
  still yields no group; and `create_bundles` refuses the suppressed copy as
  `unknown bundle` without writing a project. In `tests/test_bundles_api.py`,
  end to end: a byte-identical copy beside a bundle reports one group whose
  `paths` name both files, whose `sha256` is 64 hex characters and whose
  `reclaimable_bytes` equals the file size, the copy is not a bundle key, and
  `duplicates_text` names both paths.
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
- The same two files pin the nested-archive rule (`Mf-74x5`). In
  `tests/test_bundle_detect.py`: `is_bundle_member` is strict (a twin beside the
  directory is not a member), segment-bounded (`AB/` is not under `A/`) and
  raw/case-sensitive; a snapshot whose only entry is `Clockwork_v1.8.zip` is not
  a bundle and has no archive-owned facts, and a trio plus a nested zip counts
  one model and one archive. In `tests/test_bundle_service.py`, against a
  temporary root: the real-name Clockwork directory plus
  `Clockwork_v1.8.zip` is exactly one row, the nested zip is a member of the
  bundle's own listing, `model_count` stays 12, and the nested zip is not in
  `duplicates`; a directory holding only that zip is not a bundle while the zip
  itself is offered; a byte-identical shorter-named copy outside the bundle does
  not make the member deletable, and the outside copy is still offered; and a
  nested `Clockwork v1.8 Download.zip` rooted at a subdirectory of the bundle
  produces no second row at all, because the member filter drops the candidate
  the pairing had just promoted.
- The same two files also pin R4 (`Mf-ey4q`). In `tests/test_bundle_detect.py`,
  from literals: the ticket's own snapshot -
  `5390184_ARTICULATED_ice_dragon__fixed_` with a single
  `files/ARTICULATED_ice_dragon_fixed_p1.stl` - is `medium` /
  `thingiverse-id-name` / `id-name`, with the derived `source_url`, the `thing_id`
  read back out of that URL, and creator, licence, licence URL and description
  all `None`; the same shape under a non-id name is offered by nothing; R4
  outranks R3 on one identical trio (the id-named one reads `medium`, the plain
  one stays `low` with no URL) and yields to both high rules, whose `thing_id` is
  the document's `2493386` and not the name's; R4's at-least-one-own-depth-model
  gate refuses a photos-and-notes directory and a pile of two unnamed
  model-bearing subdirectories; the seven-string regex table pins the digit
  floor, the mandatory separator and the anchor, with three killable mutants (the
  combined unanchor being the only one of the two unanchor forms that is
  killable); `2024_parts` is the accepted lookalike, offered at `medium` with
  `.../thing:2024`, while `2024-10-31_backup` is refused; and
  `INFERRED_CONFIDENCES` equals exactly `{medium, low}` and does not contain
  `high`. In `tests/test_bundle_service.py`, against a real temporary tree: the
  id-named export is one `medium` row with `counts.medium == 1` that creates a
  project without `name_only`, keeping the derived URL with `NULL` attribution
  and a `_bundle` marker reading `thingiverse-id-name` / `medium`; the same
  create with `name_only` discards the URL; the non-id doc-less folder under a
  name R5 refuses is still refused, with `unknown bundle` and no project; an
  id-named pile beside its two
  children is absent from the report while both children are `ready`; that pile
  with its stem twin is one `blocked` / `archive_only` row reading `low` /
  `generic-bundle` with no URL, and create-all makes two projects and none named
  after the pile; an id-named directory with a stem twin is one `medium` row
  whose archive attaches nothing; an id-named `medium` folder holding a vendor
  zip is stripped, so the zip stays its own `blocked` / `archive_only` row; a
  `medium` candidate whose registered assets include another bundle's is refused
  `contains_bundles` with no `Project` row written; and an id-named zip is an
  `archive_only` `medium` row that creates nothing.
- The same two files also pin R5 (`Mf-hvck`). In `tests/test_bundle_detect.py`,
  from literals: the nine measured names are accepted by the branch that catches
  each - five by the separator branch (`Rocket Filement support`, `mac deckbox`,
  `skull mask`, `printable carrage`, `Filament guide`) and four by the
  single-word branch (`Stagetop`, `LittleTup`, `GavinIsABot`, `Taffi`) - while
  `Beta`/`Tup`/`Sub` are floor-refused, `STAGETOP`/`ARTICULATED`/`ALLSTL` are
  refused for want of a lowercase letter, and `residual`/`clockworkmonster`/
  `xbox360shell` are refused by the leading `[A-Z]`; every required part name is
  refused in six casing and whitespace variants swept from a LITERAL tuple, so
  deleting an entry reds the sweep instead of shrinking it, plus a subset test on
  the constant (the required set cannot be quietly reduced) and a
  constant-driven test (an added entry cannot be a title); with the list
  monkeypatched to empty, `files` and `source` are still refused and the nine
  still pass, which is the 11 -> 9 proof; `title_name_facts` is `None` with the
  switch off, `None` for a part name and for a lowercase word, and `None` for a
  pile of two unnamed model-bearing subdirectories and for models at depth 2,
  while one own-depth model is enough; its facts read
  `title-name`/`title-name`/`low` with creator, licence, licence URL, source URL,
  thing id and description all `None` and the cover and model entries correct;
  `detect_bundle` still returns `None` for the very listing `title_name_facts`
  offers, which pins that R5 is not a detection branch; and on the dragon
  snapshot R4 reads `medium` with the derived URL while `title_name_facts` reads
  `low` with no URL. In `tests/test_bundle_service.py`, against a real temporary
  tree: the eleven measured directories are built and exactly the nine are
  offered, each `title-name`/`title-name`/`low`, `ready`, with four `NULL`
  provenance fields and at least one model, and one of them creates a project
  attaching exactly its own models; `files` and `source` are absent from the
  report and from `iter_bundle_candidates`; a name-derived row and a vendor row
  in one report read `title-name`/`title-name`/`low` with four `NULL`s against
  `thingiverse-readme`/`root-docs`/`high` with the documents' creator, licence
  and URL; a title-named folder holding a claimed folder bundle and one holding a
  zip-only bundle are both refused, the first by `_relates_to_any`'s
  holds-a-taken-key leg alone and the second by that leg and the
  `contained_keys` strip; two `build_report` calls are equal and leave the
  filesystem snapshot and the table counts unchanged; the switch off removes
  exactly the title rows; and the id-named dragon folder stays one `medium` row
  with its URL (`low == 0`, `total == 1`). The mutation that matters is the
  placement: moving R5 into `detect_bundle` reds 16 witnesses - 12 service and 4
  pure - because the deepest-first walk hands `LittleTup`'s row to its child and
  loses the promoted Clockwork row.
- The same two files also pin the fourth identity (`Mf-cf3c`). In
  `tests/test_bundle_service.py`, against a temporary root:
  `test_service_a_hand_made_project_owns_a_bundle_by_its_files` builds the
  owner's shape - an R5 `title-name` row over `import/17_ClockworkMonster` and a
  hand-made project holding its twelve models - and asserts the row reports that
  project's id and name with `existing_project_match` `assets`, stays `ready`,
  counts into `already_created`, and leaves the project's metadata without a
  `_bundle` marker, so the read route writes nothing;
  `test_service_a_project_holding_the_models_owns_a_bundle_that_also_has_images`
  pins the reading of "every registered asset" - 14 registered assets, of which
  the project holds the 12 models, is still claimed -
  `test_service_a_project_holding_some_of_the_files_does_not_claim_the_bundle`
  holds 11 of the 12 models and asserts the row stays pending with no identity
  and no `already_created`;
  `test_service_an_empty_asset_list_never_claims_a_bundle` proves an empty
  index, an empty containment set and an asset id with no membership each answer
  `None` and leave a real bundle unclaimed;
  `test_service_the_four_identities_keep_their_precedence` gives every project a
  weaker identity as well and pins `marker`, `source_url`, `name`, `assets` in
  that order, passing a poisoned membership index on the marker case so a path
  that reads it raises instead of returning;
  `test_service_one_project_asset_query_serves_every_candidate` listens on
  `before_cursor_execute` and counts exactly one `project_asset` statement on a
  two-candidate root and again on a five-candidate one;
  `test_service_the_clockwork_parent_is_promoted_and_reports_its_owner` holds
  the `Mf-v3i3` interaction and this decision true at once - the promoted parent
  is the only row and that row reports the hand-made project by `assets`;
  `test_service_a_create_for_an_asset_owned_bundle_is_a_skip` posts the create
  and asserts `skipped` / `already_created` with the existing id, twelve members
  unchanged and one `Project` row. In
  `curiarium/web/src/ui/review/bundleRows.test.ts` the `createdProjectLine` cases
  pin the two asset lines, the flat line for the other three identities and the
  old line for a payload that omits the field, and the
  `describe("a row already in a project by shared files")` block pins that such
  a row is hidden by default, visible behind `Show created`, not counted by
  `needsDecision` and not creatable. The two pre-existing witnesses that keep the
  pairing rule honest stay green unchanged -
  `test_service_an_archive_pairs_with_the_directory_its_entries_are_rooted_at`
  and `test_service_a_nested_archive_is_a_member_not_a_second_bundle` - because
  the containment set is read after the row set is decided, never before.
- **The asset identity costs one statement per report, and the measured library
  is a reproduction rather than the owner's.** The measurement was taken on a
  synthetic library built to the ticket's shape in a temporary directory - 123
  detected candidates and 91 projects over 369 assets - against the base build
  (`c453e1a`) on the same tree. The base build ran 371 SQL statements for the
  whole report, none of them `project_asset`, and reported `already_created` 0:
  every one of the 123 candidates was offered as new work, which is the ticket's
  bug at scale. With this decision the report runs 372 statements, exactly ONE of
  which is the `project_asset` membership query, and reports `already_created`
  91 of 123. The identity therefore costs one statement per report, not one per
  candidate: the number does not move with the candidate count. Wall clock, best
  of six interleaved runs, was 233.7 ms for the base build against 212.3 ms with
  the change; that difference is run-to-run noise and NOT an improvement, because
  the per-candidate `Project` scans dominate - 246 project statements either way
  - and this change adds one statement. **The figure the ticket asks for is the
  one on the owner's own 123-candidate / 91-project library, and that figure is
  UNVERIFIED here:** the live deployment is off-limits to this work, and this
  checkout's `curiarium.db` holds 0 projects, so the shape was reproduced and the
  machine was not.
- **R6's witnesses are named, and each one reds a different mutation.** In
  `tests/test_bundle_detect.py`, from literal snapshots built from the measured
  archive layouts:
  `test_single_root_archive_offers_the_fates_end_tower_shape` (14 models, 3
  previews and the vendor's own nested deliverable zip, one root, no text:
  `archive-bundle` / `low` / `archive-root`, every provenance field `None`, the
  largest preview the cover, and R3's `trio-no-text` refusal as the hole it
  fills);
  `test_the_clause_catches_the_dice_jails_shape_r3s_depth_bound_misses` (8
  models across four subdirectories of one root: the own-depth count asserted as
  `0` **first**, then
  `detect_bundle_decision(...).facts is None` and
  `archive_bundle_decision(...).facts is not None`);
  `test_models_only_archives_stay_refused_at_the_zip_root_and_under_one_root`
  (the restraint, both refused at `R3:trio-no-image`);
  `test_three_hundred_root_directories_are_a_pile_and_stay_refused`,
  `test_the_four_root_near_miss_stays_refused` and
  `test_the_all_pokemon_pile_keeps_its_primary_refusal` (the pile guard and the
  primary refusal unchanged);
  `test_a_loose_file_archive_has_no_wrapper_and_stays_refused` (no single root,
  no bundle); `test_a_high_archive_and_r3s_trio_are_returned_unchanged` (the
  wrapper can never lower a stronger row);
  `test_macosx_cruft_cannot_make_a_real_bundle_rootless`; and
  `test_a_text_file_hands_the_listing_back_to_r3_even_at_depth_two` (the
  `text == 0` scoping, with the untexted twin of the same listing offered). In
  `tests/test_bundle_service.py`, against temporary storage roots with real zip
  bytes:
  `test_service_a_single_root_archive_without_a_readme_is_a_low_row` (the row
  through `build_report`, which is the `_candidate_facts` routing witness),
  `test_service_a_matching_archive_beside_an_offered_twin_stays_consumed`,
  `test_service_an_archive_rooted_at_an_offered_sibling_is_consumed_by_it`,
  `test_service_byte_identical_matching_archives_yield_one_row_and_one_duplicate`,
  `test_service_a_matching_zip_inside_a_bundle_is_still_a_member`,
  `test_service_models_only_and_multi_root_archives_stay_refused` and
  `test_service_every_archive_is_offered_or_refused_with_a_named_rule` (the
  Mf-9ygi ledger invariant with the new rows in the mix).
- **R7's witnesses are named, and each one reds a different mutation.** In
  `tests/test_bundle_detect.py`, from literal snapshots built from the measured
  trees: `test_a_compound_folder_is_low_name_derived_and_names_every_group` (the
  60-part Stagetop tree as `compound-bundle` / `low` / `compound` with groups
  `45`/`15`, family token `wave`, and every provenance field `None` - dropping the
  group breakdown reds it, and a `.stl`-only count turns 60 into the plan's stale
  56); `test_detect_bundle_still_refuses_the_snapshot_compound_facts_offers` (the
  same listing answers `None` from `detect_bundle`, which is what pins the
  placement); `test_repository_metadata_refuses_a_compound` (both reachable
  marker shapes, a `Gemfile` file and `_layouts/page.html`, plus a NEGATIVE
  witness - a `Gemfile` one level INSIDE a group does not refuse the folder,
  because the marker leg is the folder's own first segments only);
  `test_two_groups_with_their_own_vendor_ids_are_not_compound` (the own-id
  refusal, with the shared token `octopus` asserted to exist first so the refusal
  cannot be the Leg A token leg wearing its name);
  `test_two_groups_with_their_own_vendor_ids_refuse_the_leg_b_shape` (a Leg B
  shape whose two groups both carry 7-digit ids, so only the own-id threshold - the
  same `COMPOUND_MIN_GROUPS` - refuses it, which reds if that constant is raised);
  `test_a_family_token_must_be_a_word` (a shared token only binds when it is a
  product word: `the` is refused twice over, by glue and by the length floor,
  `with` is glue and the only glue word the length floor does not also refuse,
  `files` a part name, `abc` below the length floor, `2024` all digits, each
  refused while `wander` fires, so the table cannot pass by refusing everything);
  `test_compound_groups_are_a_function_of_the_tree_not_of_readdir_order`
  (`Zeta Parts` / `alpha parts` list in casefold order, which raw order would
  reverse); `test_apple_double_cruft_is_not_a_group` (a top-level `__MACOSX` is
  never a group, while `_counts.model` still counts the cruft);
  `test_a_foreign_group_is_named_and_never_split` (31 models as 27 + 4, one group,
  `foreign` true); `test_a_foreign_group_is_known_by_its_own_root_document`
  (`README.txt`, `LICENSE.txt` and `LICENCE` at the group's own root all read
  foreign, `<group>/docs/README.txt` does not, and the deep case then refused);
  `test_leg_b_does_not_merge_two_readme_bearing_unrelated_groups` and
  `test_leg_b_needs_every_group_foreign_and_dominant_own_models` (Leg B's three
  bounds, one shape each, the passing one sharing no token);
  `test_a_single_model_bearing_group_is_not_compound` (the group floor, the vendor
  id and the strict majority);
  `test_root_models_are_strictly_at_depth_zero_and_the_wave_15_leg_is_not_one_of_them`
  (`root_model_count` 0 while `_own_depth_model_count` answers 15 on the same
  Stagetop listing, which is the leg R7 must not use); and
  `test_compound_facts_is_none_when_the_switch_is_off` (one switch, the 48-part
  Rocket while it is on, and token `mount`). In `tests/test_bundle_service.py`,
  through `build_report` and `iter_bundle_candidates` against temporary roots:
  `test_service_stagetop_is_one_row_with_two_groups` and
  `test_service_the_rocket_is_one_row_with_eight_groups` (eleven child rows in the
  fixture - it is the measured tree, `PlayTiles/Accessory` included - and one row
  out; eight in, one out), `test_service_a_git_dump_keeps_its_separate_product_rows`
  (endorphin's rows survive and no compound row appears),
  `test_service_xbox360_shell_has_a_row_covering_its_own_models_and_naming_the_foreign_group`
  (the 27 own models reachable through `_own_paths`, the group named, the absorbed
  key `member-of-bundle` in the ledger),
  `test_service_a_compound_row_creates_one_project_with_every_part` (a resolved
  compound row creates ONE project attaching all 31 models, so "creatable on
  exactly the same terms" is a measured claim; `own_assets == assets`, so the
  `contains_bundles` belt cannot fire, and splitting the foreign group is C2's job
  (`Mf-ao6r`)),
  `test_service_a_folder_with_one_model_bearing_group_is_not_compound`,
  `test_service_a_bundle_that_is_already_offered_is_never_compounded` (the overlap
  guard, on a `high` parent that answers every R7 predicate),
  `test_service_an_offered_low_folder_is_never_recompounded` (the same guard on an
  OFFERED LOW row that is Leg B's shape, where deleting `key in offered` is the
  mutation and nothing else protects the row),
  `test_service_a_leg_a_family_never_absorbs_a_high_row` (two undisputed HIGH
  Thingiverse exports sharing the word `bundle`, refused only by the STRONG-row
  half of `_compound_absorbs`) and
  `test_service_the_switch_off_restores_every_absorbed_row` (the switch off
  returns exactly the eleven child rows the fixture writes). In
  `tests/test_bundles_api.py`,
  `test_the_compound_row_serves_its_groups` (the live route serves 60 models as
  `45`/`15`, and an ordinary row answers `groups: []` rather than a missing key)
  and `test_the_committed_openapi_fixture_declares_the_group_component` (the
  committed fixture declares `BundleGroupOut`'s three required fields, so a
  skipped regeneration cannot leave stale-but-valid JSON).
- **Decision 25's witnesses are named, over one predicate and two doors.** In
  `tests/test_bundle_detect.py`:
  `test_a_bare_appledouble_sidecar_is_not_a_model` (`a.stl` + `._a.stl` is one
  part), `test_a_nested_macosx_appledouble_sidecar_is_not_a_model` (the measured
  Rocket shape at depth 3, the two groups reading one each) and
  `test_a_hidden_model_name_is_not_counted` (`.hidden.stl`, with
  `entry_policy.is_junk_entry` asserted NOT junk in the same test - the reason the
  fix cannot be wired to that predicate). Each asserts
  `_counts().model`, `_model_entries`, `compound_groups`, `root_model_count` AND
  `_own_depth_model_count` together, so no counting site can drift alone.
  `test_bundle_detect_asks_the_scan_its_own_predicate` asserts the identity with
  `library_scan.is_skipped_name` plus the seven-path truth table (including a
  non-dot file under a nested `__MACOSX`, which the scan REGISTERS) and
  `include_hidden=True`; `test_an_appledouble_preview_is_still_evidence_and_the_cover_rule_sees_it`
  and `test_a_sidecar_only_preview_still_offers_the_row` pin the deliberate
  evidence-view divergence at every place that consults it - the cover pick, R3's
  trio gate, the `trio-no-image`/`-no-text` REASONS, and the verdict that does
  move (a sidecar-only model refuses `R3:no-own-depth-model`);
  `test_the_document_candidates_keep_the_walk_reading_too` pins the document
  candidates, and
  `test_the_count_driven_doors_read_the_narrowed_counts_on_purpose` pins the two
  count-driven doors (R6's trio, R7's group leg) in both directions; and
  `test_the_rule_set_stamp_is_pinned_so_a_table_edit_cannot_go_unnoticed` /
  `test_every_row_deciding_table_is_inside_the_derived_stamp` pin
  `2026-11-05.2` and assert `ROW_DECIDING_TABLES` gained `SKIPPED_FILENAMES` and
  `HIDDEN_NAME_PREFIX`, the latter swept by rebinding `library_scan` itself
  (`RULE_SET_TABLE_HOMES`) so the stamp provably tracks the tables the predicate
  reads. In `tests/test_library_scan.py`,
  `test_is_skipped_name_is_the_one_rule_both_doors_ask` pins the truth table,
  `test_a_nested_macosx_tree_registers_its_dead_file_and_skips_the_sidecar` pins
  the premise the fix rests on through `_register_tree` (a non-dot file under a
  nested `__MACOSX` REGISTERS, the `._` sidecar does not, and the ignored counter
  says one), and `test_a_hidden_directory_is_never_walked` pins the walk's own
  prune, open and closed; the pre-existing `.hidden.stl` registration tests stay
  green untouched. In
  `tests/test_bundle_service.py`,
  `test_the_row_counts_exactly_what_the_scan_registered` (Rocket row 48, groups
  `[(6, 6)] * 8`, `other_count` 0 so a skipped name is in no bucket, registered
  models == project `storage_path` set == 48, no `._` path in either) and
  `test_the_stagetop_row_still_reads_60_with_no_appledouble_on_disk` (the
  non-regression witness) run through `TestClient(main.app)`. In
  `tests/test_bundle_serialize.py`,
  `test_a_row_carries_its_counts_so_a_rule_change_is_visible` and
  `test_the_diff_names_a_count_change_and_admits_an_old_artifact_without_one`
  pin the schema-3 row and the honest "not recorded" for a schema-2 artifact;
  `test_one_schema_2_row_does_not_suppress_the_deltas_the_others_prove`,
  `test_a_diff_with_no_common_row_does_not_invent_a_counts_verdict` and
  `test_the_unchanged_summary_does_not_survive_a_count_change` pin the three
  edges of that block - a partial artifact, no common rows, and the summary that
  must not read "unchanged" when the only movement is a count.
- **Decision 24's witnesses are named, and each one reds a different mutation.**
  In `tests/test_bundle_service.py`, against temporary roots built from the
  measured trees:
  `test_service_creating_from_a_compound_row_makes_one_project_with_every_part`
  (the Stagetop row creates ONE project holding all 60 parts, asserted by set
  equality over `Asset.storage_path` against every registered model, with the
  marker naming both groups 45/15 - attaching one group reds it);
  `test_the_row_counts_exactly_what_the_scan_registered`
  (the Rocket's row counts 48 and attaches 48, six per group, and no `._` path is
  in either set - the count-equals-registration invariant Decision 25 records);
  `test_service_xbox360_shell_creates_one_project_with_all_31_parts` (27 own
  parts plus the foreign 4, cover `model-preview` on the mount's
  `bracket-0.stl` with `cover.group` naming the mount - the 40-byte tie broken by
  path order, so dropping the foreign group from the attach set reds the 31
  relation);
  `test_excluding_the_foreign_group_creates_one_project_of_its_own_27` (the
  exclusion is an attach decision: 27 attached, the mount's parts absent, the
  cover re-picked to `shell-0.stl` with `cover.group` `None`, and the compound
  row's counts, groups, cover asset and status unchanged after the create -
  filtering the belt reds it);
  `test_the_excluded_group_is_offered_as_its_own_row` (the row comes back at both
  scopes, the ledger still says `member-of-bundle`, and the scoped read names the
  same absolute key - dropping the second loop in `_report_from_scan` reds it);
  `test_no_directory_is_both_a_row_and_a_refusal` (the offered set and the ledger
  are disjoint before and after - dropping the `reoffered_relative` filter reds
  it);
  `test_a_dismissed_re_offered_group_is_not_stale_and_a_vanished_key_is` (a
  dismissed re-offer still names a live row - building `live_keys` from
  `scan.candidates` alone reds it);
  `test_a_re_offered_group_creates_its_own_project_and_only_when_re_offered`
  (absorbed-but-not-excluded is `unknown bundle`, the split's marker carries only
  the six keys and its OWN `high` detector, and a second tree leaves its group
  alone);
  `test_a_re_offered_group_without_a_folder_row_is_blocked_and_creates_nothing`
  (AC-3e: a re-offer resolves on exactly the terms of any other row);
  `test_a_second_create_of_a_compound_row_is_already_created` (a marker hit, and
  the marker byte-identical even when the second create names a different
  exclusion);
  `test_a_second_create_of_the_split_group_is_already_created` (two rows, both
  `already_created`, `counts.already_created == 2`);
  `test_a_hand_made_project_over_the_mount_files_reports_already_created_by_assets`
  (the `assets` identity still answers for a re-offered row with no marker, no
  source and another name);
  `test_the_compound_marker_names_the_groups_and_their_counts` (three compound
  markers, group order and counts matching the row, `attached` adding up to what
  the account needs);
  `test_a_non_compound_marker_is_unchanged_in_both_directions` (six keys ordinary,
  six on the split, nine on the compound - writing `shape` / `groups` / `cover`
  unconditionally reds it);
  `test_an_exclusion_of_a_group_that_was_never_a_row_is_refused` (the R9 shape,
  `separable: false`, `not_separable_group`, no project);
  `test_the_three_exclusion_refusals` (all three reasons, the case-sensitive
  `unknown_group`, the file-named-group trailing slash, the duplicate-name
  idempotence, the empty-list no-op and validation before the marker check);
  `test_a_split_sent_with_its_parent_in_one_request_fails_on_the_second_item`
  (the same-request limitation: `unknown bundle` on item two and `ready` on the
  next read);
  `test_the_create_audit_records_the_excluded_groups` (always present, `[]`
  included);
  `test_a_registered_asset_that_vanished_from_the_disk_still_answers_the_belt`
  (the belt is live and unfiltered - filtering either list reds it);
  `test_the_cover_never_picks_an_unregistered_image_under_an_excluded_group`
  (the facts-level `cover_entry` is dropped so the face is never the excluded
  group's own image);
  `test_the_marker_names_the_group_of_an_unregistered_cover` (the marker's
  `cover.rel_path` is normalised to storage-relative, so `cover.group` names the
  group for the unregistered leg too - comparing the bundle-relative entry reads
  `None`); `test_a_pre_c2_marker_re_offers_nothing` (a six-key marker written
  before this decision re-offers nothing, so treating an absent `groups` as
  "everything was excluded" reds it); and
  `test_a_refused_container_is_not_creatable_by_key`
  (endorphin is `unknown bundle`, so a refused checkout cannot become one
  project). In `curiarium/web/src/ui/review/BundleReviewView.test.tsx` the seven
  `BundleReviewView compound groups` cases pin the confirmation surface: every
  group's name and part count with the singular "1 part", the `unrelated` marker
  on a foreign group, no checkbox for a non-foreign or a non-separable group, one
  checked-by-default `Include <group>` box, `exclude_groups` carrying the name
  verbatim when it is unchecked, `exclude_groups` ABSENT (not present-and-empty)
  when nothing was unchecked, no group block and the old body for `groups: []`,
  and the bulk doors still sending no `exclude_groups` after an exclusion was
  toggled. The measurement was re-run after the change: `--diff` against
  `2026-11-05-bundles-mf8xrc-after.json` reports `comparable: YES`, 0 added, 0
  removed, 0 changed and the same stamp, because C2 changes what CREATING does,
  not what is DETECTED.
- The zip-as-project door's cover and marker (Decision 26) are pinned in
  `tests/test_import_project.py` against the door itself, not against the rule:
  `test_a_zip_project_gets_a_cover_from_its_largest_registered_image` (the
  created row's `cover_asset_id` is the LARGER of two real PNGs, and the marker's
  `cover` is `{asset_id, rel_path, reason: largest-image, group: None}`);
  `test_a_zip_with_no_image_gets_no_cover_rather_than_a_broken_one` (models-only
  archive: `cover_asset_id` NULL and `reason: "none"` - the images-only ruling);
  `test_the_two_doors_agree_on_the_image_leg_and_only_there` (the same rows
  through `bundle_service.cover_for_registered` answer the same asset and reason
  on the image leg, and `model-preview` against the marker's `none` on the
  models-only one - the divergence asserted, not described);
  `test_the_zip_project_marker_names_the_door_its_key_and_its_groups` (a
  single-root zip answers `rel_dir: "Thing"` with one `Thing` group of two
  attached models, a mixed-root zip answers `rel_dir: None` with `PartA` and
  `PartB` groups and its root-level preview in none);
  `test_a_re_import_never_clobbers_a_human_cover_or_a_detection_marker` (the
  three fill-only-what-is-empty cases plus a fourth: this door's own marker is a
  free slot, so the same page imported again refreshes it while the identity key
  stays put);
  `test_a_group_records_what_was_selected_and_what_was_attached` (a requested but
  absent member is `failed`, so `model_count` 3 against `attached` 2 - the
  scan-count/attach-set split with a real failure behind it);
  `test_an_explicit_project_target_gains_an_empty_cover_and_the_marker` (the
  `project_id` branch of the fill, which the other imports do not reach);
  `test_the_readers_still_read_the_new_marker` (`_bundle_marker` returns it,
  `_compound_splits` offers no split for it, `find_existing_project` cannot
  false-match the identity key, and `GET /projects` serves it as a dict);
  `test_the_whole_zip_as_one_asset_path_is_unchanged` and
  `test_a_gated_zip_import_records_no_cover_and_no_bundle_marker` (the two
  branches that must not widen); and
  `test_no_storage_path_can_contain_the_identity_keys_separator` (a zip whose
  folder and filename both carry a colon still registers colon-free storage
  paths, with the sanitiser and `_INVALID_SEGMENT_RE` pinned directly). The
  row-level before/after artifacts are
  `.plans/measurements/2026-11-06-zip-project-mfqbji-{before,after}.json`; the
  statement cost is measured in the same pair rather than quoted.

- Bundle drift (Decision 27) is pinned from literals in
  `tests/test_bundle_drift.py`:
  `test_walk_root_reads_each_marker_shape` (the detection key, the zip
  `rel_dir`, and the identity key with no `rel_dir` answering `unknown` - never
  prefix-queried), `test_classify_is_exhaustive_over_the_three_states` (`{}` and
  a malformed marker are `not_applicable`, never zero; a root that holds a row
  is `known` and one that holds none is `unknown`),
  `test_excluded_prefixes_subtract_an_included_false_group` (only an explicit
  `included: False`, de-duplicated in marker order),
  `test_drift_is_model_scoped_so_support_files_are_never_drift` (the fake set
  against the naive over-count it replaces),
  `test_the_asset_ids_are_ordered_by_path_not_row_order` (shuffled input, one
  ordered answer),
  `test_a_marker_whose_root_holds_no_registered_row_answers_unknown` (a renamed
  folder's stale `rel_dir` is `unknown`, and the probe is asked once, of the walk
  root) and `test_has_rows_under_respects_the_path_boundary` (a sibling
  `import/Alpha2` never counts for `import/Alpha`, and an empty or missing path
  counts for no root). The two surfaces are pinned against the database:
  `test_a_grown_bundle_folder_reports_the_parts_that_are_not_in_the_project`
  (AC-1: `known`/1 naming the new id, adding exactly it clears to `known`/0, and
  the route measures three statements including the caller's session lookup),
  `test_a_fresh_bundle_project_has_nothing_to_report` (AC-4: the compound shape
  answers `known`/0 while the naive difference reads 8 == the model count),
  `test_a_hand_made_project_reports_no_drift_question` (AC-6:
  `not_applicable`/`null`),
  `test_a_zip_identity_marker_reports_unknown_rather_than_zero` (AC-2) and
  `test_a_renamed_bundle_folder_reports_unknown_rather_than_a_false_zero` (a
  folder renamed after creation leaves the marker's root stale, so the route
  answers `unknown`/`null` for the same three statements `known` costs, while
  the new path holds the parts and the old one holds none); and on
  the queue `test_service_a_grown_bundle_row_reports_the_new_parts` (AC-3/AC-7:
  still a marker match with the same `existing_project_id`, `known`/1, then
  `known`/0 after the add, with the second create still
  `skipped`/`already_created` and one `Project`),
  `test_service_a_fresh_bundle_project_reports_no_new_parts` (AC-4 on both
  surfaces), `test_service_an_excluded_group_is_not_drift` (AC-5: the excluded
  group's four models stay registered and unattached, `known`/0, and no offered
  id sits under its prefix),
  `test_service_the_drift_index_is_one_statement_per_report` (AC-8: one
  registered-index statement for two candidates and for five, with the report's
  total statement count pinned),
  `test_service_a_stale_walk_root_reports_unknown_rather_than_a_zero` (the
  queue's half of the stale root: a marker-matched row whose root holds no
  registered row answers `unknown`/`null`, and the whole report stays at eight
  statements - a probe issuing its own SELECT per candidate would be nine) and
  `test_a_hand_made_project_over_the_mount_files_reports_already_created_by_assets`
  (an asset-matched row is `not_applicable`/`null`, never `0`: a project proved
  by shared files has no marker folder to difference against). On the client
  `ProjectLanding.test.tsx`'s `ProjectLanding bundle drift` cases pin that a
  failed read stays the en dash and toasts nothing, and that a late answer for a
  project that is no longer open is dropped rather than painted.
  `test_the_report_model_declares_every_key_the_service_computes`
  names the two declared drift keys so the response model cannot silently drop
  them, and the permission/OpenAPI ledgers are pinned by
  `test_the_matrix_is_the_documented_size` (102 routes, 37 read) and
  `tests/test_openapi_contract.py` against the three regenerated fixtures. The
  row-level before/after artifacts are
  `.plans/measurements/2026-11-06-bundle-drift-mf8i04-{before,after}.json`; the
  statement cost is measured in the tests above rather than quoted.

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
  decompressed data - instead of names and declared sizes. Hashing for the
  duplicate report does not trip this: `duplicates` / `duplicates_text` are not
  `twin_verified` and never enter pairing, so a content hash reaching
  `twin_verified`, the pairing decision or an integrity claim still does;
- duplicate reporting starts deleting, renaming or moving a file, or stops being
  size-first - an archive whose size occurs once gets hashed, or a byte-identical
  copy becomes a second bundle row or a second `twin_verified` archive;
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
  high-confidence candidate starts being demoted for containing one; **closed
  2026-11-04 by the owner decision recorded in Decision 18 (`Mf-v3i3`)** - the
  archive-only case this condition was tripped by is now refused (the promotion
  is silent rather than swallowing the strong child) and is pinned by tests; the
  condition reopens if the promotion starts swallowing a stronger candidate
  again, which the bullet below states in its own right;
- creation stops refusing a candidate of inferred confidence whose registered
  assets outnumber its own snapshot's, or that refusal starts reporting
  `blocked` instead of `failed`;
- detection starts preferring a document by its filename again, beyond the fixed
  tie-break order `_root_pair_paths` / `_sibling_pair_paths` establish;
- the pair merge stops requiring all four provenance fields before it reports
  high confidence, or a named-pair merge starts reaching outside its own pair;
- `archive_owned_facts` starts requiring an image, or starts refusing a
  directory because it holds a text file, again;
- a third pairing route appears, or either route stops deciding containment
  through `bundle_detect.archive_payload_contained`;
- the stem route starts stripping twice, strips per entry, or pairs a directory
  outside the archive's own parent;
- a stem twin that fails containment starts falling through to the entry-root
  route instead of being consumed;
- `twin_verified` starts claiming "always true" again for a verified pairing, or
  a promoted stem row loses its report-time containment re-verification;
- `_own_depth_model_count` stops being the only shape guard on
  `archive_owned_facts`, or its cardinality bound widens to let a pile through;
- R4, or any other name-only rule, starts writing a creator, a licence or a
  licence URL, or inherits one from a sibling bundle;
- R3's trio is weakened for a folder that has no id in its name - a model alone,
  or a model plus one other role, starts being offered as a `generic-bundle`;
- the id regex widens past `^(\d{4,})_` - a bare digit run with no separator, an
  optional separator, or an unanchored match;
- a confidence that rests on inference stops being added to
  `INFERRED_CONFIDENCES`, or a container guard goes back to comparing a
  confidence literal instead of membership of that set;
- a promoted `archive_owned` row starts swallowing a candidate of `medium` or
  `high` confidence again (`bundle_detect.STRONG_CONFIDENCES`), or
  `STRONG_CONFIDENCES` widens to admit a confidence that rests on shape
  inference alone;
- the asset identity stops being consulted last - a marker, a source URL or a
  name match stops winning over containment;
- the marker stops returning from inside the `Project` loop, or a marker match
  starts reading the membership index;
- containment becomes an intersection - a project holding some of a bundle's
  files starts claiming it - or an empty containment set starts claiming a
  bundle, or a membership row whose project row is missing stops being ignored;
- the containment set widens from the models a create would attach to every
  registered asset of the bundle;
- the tie-break between two projects holding the same files stops being
  `(lower(project.name), project.id)`;
- the membership index starts being built per candidate instead of once per
  report;
- `existing_project_match` stops being reported, or the client stops
  distinguishing the assets identity from the app-created one.
- R7 starts absorbing a row a STRONGER rule offered outside the Leg B shape - a
  Leg A family (models only in subdirectories) starts replacing a `high` or
  `medium` verdict, which is the two-Thingiverse-exports-under-`vendor/` pile and
  a lower-confidence rule overruling a higher one;
- R7 starts firing without a family token on Leg A, or without the vendor id on
  Leg B, or with a single model-bearing group - each leg's evidence is what makes
  the row a claim rather than a guess;
- the compound row stops carrying its group breakdown, or a group's `foreign` flag
  stops being derived from the name's vendor id and the group's OWN root document
  (a `docs/README.txt` or any deeper document starts counting as ownership);
- the compound row becomes creatable from a directory that does not resolve, i.e.
  it stops being an ordinary folder row and its creatability starts keying on the
  detector again;
- a compound row starts being judged by the container strip (`iter_bundle_candidates`
  drops it for holding its own parts), or starts rewriting the verdict of a
  directory another rule offered - the `key in offered` / `_strictly_inside_any`
  guards are the whole of that promise;
- repository metadata stops being checked on the ancestors inside the walk, so a
  payload directory of a checkout can be compounded on a shared word.
- the zip-as-project door grows its own copy of the cover preference instead of
  asking `bundle_service.cover_for_registered`, or it starts offering the
  `model-preview` leg without the ticket clause that forbids it being withdrawn
  (`allow_model_preview=False` in `_zip_project_facts`);
- `_bundle.key` for this door stops being an identity - becomes a storage path,
  drops the `zip-project:` prefix, or starts doing double duty as `rel_dir` - so a
  detection key and an import key can be compared as if they were one space;
- a re-import starts writing a cover onto a project that has one, or a `_bundle`
  over a marker keyed to a bundle directory (the `_absorb_import_facts`
  fill-only-what-is-empty rules);
- the archive-as-single-asset door (`entries is None`) or the gated door starts
  recording a cover or a `_bundle` marker.
- the drift stops subtracting project membership, or stops being model-scoped,
  or the identity key starts being prefix-queried, or a fresh bundle project
  starts reporting drift (Decision 27);
- a stale walk root starts answering `known`/0 again, or the presence probe
  starts reading a stored fact (a count, a cached path) instead of the
  registered rows a read already holds; equally if it starts costing a query per
  candidate on the report.
