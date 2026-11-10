"""One door in, one identity check, one write, for the doors that make a project.

Three doors create a project from what a human pointed at: `POST /projects` makes one
from a typed form; the bundle door makes one from a directory the detector read; the
archive door makes one from a `.zip` that twins an extracted directory. Before this
module each of them decided SEPARATELY whether the thing already existed, and two of
them decided by different rules — so the same folder, reached by two doors, produced
two projects with two names, and the operator was told twice.

A fourth writer exists and is NOT this module's: `project_import`'s zip-project door
(`find_or_create_import_project`) brings its own dedupe on a canonicalised URL and
writes its own flat, identity-keyed `_bundle`. It is out of scope and this module is
built to tolerate it — a marker with no `origin` reads as a detection marker, and a
`zip-project:<url>` key can never equal a candidate's storage path.

This module owns both halves of the answer for the three doors above, and it is the
only module that owns either:

  * `find_existing_project` — the dedupe. Four identities, four legs. The marker and
    twin legs answer from inside ONE walk of the Project table and the name leg records
    there and answers after the source-URL read, which outranks it; the source-URL leg
    is a second read, as it always was, because it goes through
    `project_service.find_project_by_source`. Every leg is skipped when its identity is
    falsy: an empty identity is not an identity, and matching on one matches
    everything.
  * `member_asset_ids` — the member set. Which kinds a door attaches is a parameter
    (`kinds`), never a test inside this module, so the day the bundle door stops
    deciding kinds by suffix (Mf-wsdo) changes one body here and not two doors.
  * `create_project_from_members` — one dedupe pass, then
    `project_service.create_project_with_members`, in one transaction. Either the
    project and every member row land, or nothing does.

Two rules the shape of this module exists to keep:

  * The four identities are the ONLY ones, and a door asks only with the ones it holds.
    A door that wants a fifth verdict ("the same basename") belongs in the web layer's
    `blocking_reason` mapping, not here: a fifth identity means a fifth way to merge
    two products, and `existing_project_match` on the wire is the answer a user sees.
    The same rule is why `Dedupe.assets` is a flag a door sets rather than a leg that
    always runs.
  * `project_service` is the only project-creating import, and this module never
    names a detection table. Which suffixes are MODELS is a fact about the
    detector, and the detector lives in `bundle_detect`; a door that imports it
    through here could not be tested without it.

Marker namespace
----------------
One namespace, `_bundle`, discriminated by `origin`. `detection` is the bundle door's
marker and it is the shape every reader already knows (flat top level: `key`,
`detector`, `rel_dir`, `archive`, `groups`, `cover`), because `bundle_drift` and the
compound belt read it there and no migration is allowed. `archive_extract` is the
archive door's marker, and its door-specific payload lives under
`_bundle["archive_extract"]`.

Why a sub-block rather than two namespaces: both doors spell two of the same field
names to mean different things. Detection's `archive` is the twin it paired with —
provenance for `twin_verified`, never an identity — while the archive door's is the
archive it read, which IS its identity, and only the second is something the dedupe
may read. Detection's `groups` is `{name, model_count, foreign, included, attached}`
and the archive door's is `{name, count}`; read flat into one dict, `_compound_splits`
would feed the create-time belt an excluded-group list that describes a different
thing entirely.

The legacy `_archive_project` block is still READ (`_read_marker` falls back to it) so
rows written before this module are still found. Nothing writes it. No migration.
"""

from pathlib import PurePath
from typing import Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from sqlmodel import Session, func, select

import project_service
from models import Asset, Project, ProjectAsset

# The one marker namespace, and the field that says which door wrote it.
BUNDLE_MARKER = "_bundle"
ORIGIN_FIELD = "origin"
ORIGIN_DETECTION = "detection"
ORIGIN_ARCHIVE_EXTRACT = "archive_extract"
# Every door this module knows. `manual` is `POST /projects`: a form, so it has no
# identity to match on and it writes no marker at all.
ORIGIN_MANUAL = "manual"
ORIGINS = (ORIGIN_DETECTION, ORIGIN_ARCHIVE_EXTRACT, ORIGIN_MANUAL)

# The block rows written before this module carried the archive door's payload in, and
# is still read. Nothing writes it.
LEGACY_ARCHIVE_MARKER = "_archive_project"

# What a match was made on. THE four identities, and `existing_project_match` on the
# wire is one of these. The archive door's twin identity deliberately reports
# MATCH_MARKER, not a fifth value: it is a marker match seen from the other door, and
# a new value here would be a new merge rule wearing a new name.
MATCH_MARKER = "marker"
MATCH_SOURCE_URL = "source_url"
MATCH_NAME = "name"
MATCH_ASSETS = "assets"

# `create_project_from_members` verdicts.
STATUS_CREATED = "created"
STATUS_ALREADY_CREATED = "already_created"
STATUS_REFUSED = "refused"


class ExistingProject(NamedTuple):
    """A project that already covers what was asked for, and the identity that said so."""

    project: Project
    match: str


class MemberCandidate(NamedTuple):
    """One file a door is offering as a member, in the shape this module can read.

    The doors each carry their own richer row type (`RegisteredAsset` on the bundle
    side, `ResolvedMember` on the archive side); this is the two fields the member-set
    rule actually needs, and mapping to it is what keeps the rule out of both doors.
    """

    asset_id: str
    path: str


class Dedupe(NamedTuple):
    """The identities a door can answer "already exists" with.

    A door passes the identities it actually holds and leaves the rest `None`: a door
    that holds no source URL must not match on one, and `POST /projects` holds none of
    them, which is why its `dedupe` is `None` outright. `assets` is a field and not a
    standing behaviour for exactly that reason: file containment is an identity the
    detection door holds (it is asking about the folder a scan registered) and the
    archive door does NOT - what it holds is the operator's click on one archive, and
    letting it match on "some project already holds all these files" would let an
    unrelated hand-made project swallow an Extract click. The ids are this module's to
    compute (they are the member set it is about to write), so a door asks for the leg
    rather than supplying ids it might compute differently.
    """

    key: Optional[str] = None
    archive: Optional[str] = None
    source_url: Optional[str] = None
    name: Optional[str] = None
    assets: bool = False


class CreateOutcome(NamedTuple):
    """What one create attempt decided.

    `status` is `created`, `already_created` or `refused`. A refusal carries a `reason`
    and no project id, and it is the only way this module declines: a refused attempt
    has written nothing, so there is never an empty project left behind.
    """

    status: str
    project_id: Optional[str] = None
    name: Optional[str] = None
    match: Optional[str] = None
    members: Tuple[str, ...] = ()
    reason: Optional[str] = None


def _normalised(value: Optional[str]) -> Optional[str]:
    """The identity as it was written, or None when there is nothing to match on.

    It does NOT canonicalise, and that is deliberate. The other side of every
    comparison below is a string a door stored verbatim - a candidate's storage path in
    ``_bundle["key"]``, the library-relative path the archive door recorded in its
    payload - and a reader that folded or trimmed while the writer did not would compare
    two different spellings of one path. Base ran BOTH sides through the door's own
    ``_normalised``; a fold here on the ask alone was measured to both miss (a library
    archive literally named ``a\\b.zip`` on Linux - legal, and what a Windows-authored
    archive produces - stored that spelling and was asked with ``a/b.zip``, so a second
    click wrote a second ``Project``) and falsely match (the same fold made ``a\\b.zip``
    answer ``already_created`` carrying the project made from a DIFFERENT file,
    ``a/b.zip``). Pinned by
    ``test_an_archive_whose_name_contains_a_backslash_is_still_found_on_the_second_click``.

    Blank still is not an identity: whitespace-only collapses to None so its leg is
    skipped, because matching on an empty string matches everything. The value that
    reaches a comparison is untouched.
    """
    if value is None or not value.strip():
        return None
    return value


def _read_marker(metadata_json: Optional[str]) -> dict:
    """The one read of a project's provenance; every public reader is a view of it.

    Understands three shapes and no more:

      * a detection marker — flat top level (`key`, `detector`, `rel_dir`, `archive`,
        `groups`, `cover`), because that is the shape `bundle_drift` and the
        create-time belt read and no migration is allowed;
      * an archive-extract marker — `key`/`rel_dir` at the top level and the door's
        payload under `_bundle["archive_extract"]`;
      * a pre-marker archive-extract row — the whole payload in the legacy
        `_archive_project` block, which is still READ and never written.

    An absent `origin` is a detection marker. That is what every row written before
    this module says, and it stays true for the zip-project door (`project_import`),
    whose marker is flat and identity-keyed and which this module does not own.

    The column is parsed ONCE, through `project_service.metadata_of`, and both blocks
    are read off that one dict: this runs per project per candidate on the report's
    hot path, and a project with no `_bundle` — the common case — would otherwise pay
    for the parse twice.
    """
    metadata = project_service.metadata_of(metadata_json)
    marker = metadata.get(BUNDLE_MARKER)
    if isinstance(marker, dict) and marker:
        origin = marker.get(ORIGIN_FIELD)
        if origin not in (ORIGIN_DETECTION, ORIGIN_ARCHIVE_EXTRACT):
            origin = ORIGIN_DETECTION
        block = marker.get(ORIGIN_ARCHIVE_EXTRACT)
        payload = block if isinstance(block, dict) else {}
        return {
            ORIGIN_FIELD: origin,
            "key": marker.get("key"),
            "rel_dir": marker.get("rel_dir"),
            # Only the archive door's own `archive` is an IDENTITY. A detection
            # marker carries the same field name for a different purpose — the twin
            # it paired with, as provenance for `twin_verified` — and reading that
            # as a dedupe identity would hand every detected project a second key
            # no door asked for, merging a `.zip` row with a folder project on a
            # remark about what the folder looked like.
            "archive": payload.get("archive") if origin == ORIGIN_ARCHIVE_EXTRACT else None,
            "payload": payload,
        }
    legacy = metadata.get(LEGACY_ARCHIVE_MARKER)
    if isinstance(legacy, dict):
        # The archive door's pre-marker rows: keyed and matched on the archive path,
        # rooted at the twin they were extracted into.
        archive = _normalised(legacy.get("archive"))
        return {
            ORIGIN_FIELD: ORIGIN_ARCHIVE_EXTRACT,
            "key": archive_identity(archive) if archive else None,
            "rel_dir": _normalised(legacy.get("twin")),
            "archive": archive,
            "payload": legacy,
        }
    return {}


def marker_of(metadata_json: Optional[str]) -> dict:
    """What a project's provenance says, whatever door wrote it.

    `key` is the identity leg, `archive` the twin leg, `rel_dir` what
    `bundle_drift` walks, `payload` the door's own block. `{}` when the row carries
    no provenance — and a malformed metadata column reads as `{}` rather than
    raising, because a reader of a project must not be taken down by a row
    something else wrote.
    """
    return _read_marker(metadata_json)


def archive_extract_payload_of(metadata_json: Optional[str]) -> dict:
    """The archive door's own payload, or `{}` — the only reader of it.

    Reads `_bundle["archive_extract"]` and falls back to the legacy `_archive_project`
    block, so a row written before this namespace existed still tells its story. This
    replaces the door's own reader: the door writes the payload and no longer knows
    how to find it again.
    """
    read = _read_marker(metadata_json)
    if read.get(ORIGIN_FIELD) != ORIGIN_ARCHIVE_EXTRACT:
        return {}
    payload = read.get("payload")
    return payload if isinstance(payload, dict) else {}


def archive_identity(archive: Optional[str]) -> Optional[str]:
    """The `_bundle.key` an archive-extract create writes, from the archive's path.

    Prefixed because `key` is matched against a candidate's storage path, and an
    UNPREFIXED archive path would collide with the `.zip` row that carries it: measured
    with the prefix dropped, that row answers `existing_project_match == "marker"`, the
    report's `counts.already_created` goes 0 -> 1 for it, and the web layer hides
    already_created rows behind the "Show created" toggle - which is the only place the
    Extract door a `.zip` row exists to offer is ever shown. Pinned, with that
    mutation, by
    `test_the_zip_row_keeps_its_extract_door_after_its_archive_became_a_project`.

    It borrows the zip-project door's trick and its safety argument rather than its
    mechanism: `zip-project:<url>` is safe because no `Asset.storage_path` can contain
    the separator (`test_no_storage_path_can_contain_the_identity_keys_separator`), and
    `walk_root` never falls back to that key only because that door also writes a
    `source` field - which an archive-extract marker does not, so this marker's `rel_dir`
    is what keeps a drift walk off the identity. `_create_one`'s twin is promoted into
    `rel_dir` for that reason, and it is never blank: the door refuses the one input that
    could make it so -
    `test_an_archive_with_no_usable_twin_name_refuses_like_the_extract_door_does` is the
    400 that says so.
    """
    normalised = _normalised(archive)
    return f"archive:{normalised}" if normalised else None


def project_assets_index(session: Session) -> Dict[str, Tuple[str, ...]]:
    """Every asset's project memberships as ``{asset id: (project id, ...)}``.

    Loaded once per report: deciding whether a project contains a bundle's files
    is a dictionary lookup, not a table scan per candidate, so the cost is one
    statement for the whole report however many bundles are offered.

    Each tuple is ordered by ``(asset id, project id)`` so nothing downstream
    depends on row order, and only assets with a ``storage_path`` are indexed -
    the same refusal the rest of the report applies, because a row with no path
    is not a file this bundle could contain.
    """
    index: Dict[str, List[str]] = {}
    statement = (
        select(ProjectAsset)
        .join(Asset, Asset.id == ProjectAsset.asset_id)
        .where(Asset.storage_path.is_not(None))
        .order_by(ProjectAsset.asset_id, ProjectAsset.project_id)
    )
    for membership in session.exec(statement):
        index.setdefault(membership.asset_id, []).append(membership.project_id)
    return {asset_id: tuple(project_ids) for asset_id, project_ids in index.items()}


def _asset_owner(
    index: Mapping[str, Tuple[str, ...]],
    asset_ids: Sequence[str],
    projects: Mapping[str, Project],
) -> Optional[Project]:
    """The one project that holds every wanted asset, or None.

    Containment, not intersection: an asset id with no membership contributes
    nothing, so a project holding only SOME of the files can never reach
    ``len(asset_ids)`` and a large project that happens to hold one file cannot
    claim a bundle it does not represent. An empty wanted set owns nothing. A
    membership row whose project row is missing is ignored, so a dangling row
    can never claim a bundle.

    Ties break on ``(lower(project.name), project.id)``: row order must never
    decide which project owns a bundle.
    """
    wanted = tuple(dict.fromkeys(asset_ids))
    if not wanted:
        return None
    counts: Dict[str, int] = {}
    for asset_id in wanted:
        for project_id in index.get(asset_id, ()):
            counts[project_id] = counts.get(project_id, 0) + 1
    owners = [
        projects[project_id]
        for project_id, count in counts.items()
        if count == len(wanted) and project_id in projects
    ]
    if not owners:
        return None
    return min(owners, key=lambda project: (project.name.lower(), project.id))


def find_existing_project(
    session: Session,
    *,
    key: Optional[str] = None,
    archive: Optional[str] = None,
    source_url: Optional[str] = None,
    name: str = "",
    assets: Sequence[str] = (),
    asset_index: Optional[Mapping[str, Tuple[str, ...]]] = None,
) -> Optional[ExistingProject]:
    """The project this create already became, in the order that cannot lie.

    THE dedupe: no door writes its own. Four identities, strongest first, and a
    leg is skipped when the caller holds no identity for it — a door with no
    source URL must not match on one, and a door with no key must not match on
    an empty key (which is every project whose marker carries no key).

      * the marker key is the identity a previous create wrote. An archive-extract
        create writes the archive it read, as an `archive:`-prefixed IDENTITY rather
        than as a path (see `archive_identity`), so a second click on the same archive
        is answered here without the path ever meeting a candidate's key;
      * `archive` is the same fact seen from the OTHER door: the directory row that
        twins an extracted archive names the archive nobody has extracted again, and
        the archive door recorded that path in its payload. It reports `MATCH_MARKER`,
        not a fifth value — a fifth value would be a fifth merge rule wearing a new
        name;
      * the source URL covers a project imported by hand from the same page;
      * an exact casefolded name covers a project made before the marker existed;
      * asset containment covers a project a human made by hand over the bundle's
        own files.

    The marker returns from inside the loop on purpose: it is proof, so it is
    also the cheapest answer - a marker match never reads the membership index.
    The whole Project table is walked ONCE for the three legs that read it: marker and
    twin return from inside that loop, and the name leg RECORDS there and answers only
    after the source-URL read, because a marker or a source URL outranks a name. One
    walk is what the report's per-candidate cost depends on. The source-URL leg is a
    SECOND read, as it always was: it goes through
    `project_service.find_project_by_source`, and it is reached only when no marker
    answered. The asset leg needs `ProjectAsset` and `Asset`, and it is the only leg that
    may read the membership index; `asset_index` is the report-wide index the caller
    built once.

    Every leg is skipped when its identity is falsy. The marker leg checks `key`
    itself, not only `marker.get("key")`: a project whose marker has no key (a row
    written before the key was required, or a door that writes no key) would otherwise
    match any candidate asking with no key, and a door asks with no key when it has
    none — `test_asking_with_no_key_matches_no_project` is that case named directly,
    because the hazard is a match on `None == None` and no other leg can show it.
    """
    wanted_key = _normalised(key)
    wanted_archive = _normalised(archive)
    wanted_name = (name or "").strip().casefold()
    projects_by_id: Dict[str, Project] = {}
    by_name: Optional[Project] = None
    for project in session.exec(select(Project).order_by(func.lower(Project.name), Project.id)):
        projects_by_id[project.id] = project
        marker = marker_of(project.metadata_json)
        # `wanted_key and` is load-bearing: `marker.get("key")` is None for a row
        # whose marker carries no key, and `None == None` is a match that hands
        # every keyless candidate the first project in the table.
        if wanted_key and marker.get("key") == wanted_key:
            return ExistingProject(project, MATCH_MARKER)
        if wanted_archive and marker.get("archive") == wanted_archive:
            return ExistingProject(project, MATCH_MARKER)
        if by_name is None and wanted_name and project.name.strip().casefold() == wanted_name:
            by_name = project
    existing = project_service.find_project_by_source(session, source_url)
    if existing is not None:
        return ExistingProject(existing, MATCH_SOURCE_URL)
    if by_name is not None:
        return ExistingProject(by_name, MATCH_NAME)
    wanted = tuple(dict.fromkeys(asset for asset in assets if asset))
    if not wanted:
        # An empty containment set can never claim a bundle.
        return None
    # Lazy: a marker, source or name hit never pays for the index, and a caller
    # that did not prebuild one still gets the asset identity.
    index = asset_index if asset_index is not None else project_assets_index(session)
    owner = _asset_owner(index, wanted, projects_by_id)
    return ExistingProject(owner, MATCH_ASSETS) if owner is not None else None


def member_asset_ids(
    candidates: Sequence[MemberCandidate], *, kinds: Optional[Sequence[str]] = None
) -> List[str]:
    """THE member-set seam: which offered files a create attaches, as asset ids.

    `kinds` is the whole question, and it is a parameter and never a test in here:

      * the archive door passes nothing — every entry it resolved is a member, because
        what it read WAS the file set, not a guess about one;
      * the bundle door passes `bundle_detect.MODEL_SUFFIXES`, which its caller hands
        down — models only, because support files are attached by other rules and the
        detector's table is the thing that decides.

    So the day the bundle door stops answering "which kinds attach" by suffix
    (Mf-wsdo) changes the argument at one call site and the body here, and neither
    door grows a suffix test back. Order is the caller's, and first-seen order is
    what `create_project_with_members` writes as `position`.
    """
    if kinds is None:
        return [candidate.asset_id for candidate in candidates]
    wanted = set(kinds)
    return [
        candidate.asset_id
        for candidate in candidates
        if _suffix(candidate.path) in wanted
    ]


def _suffix(path: Optional[str]) -> str:
    """The lowercase extension a `kinds` table is tested against.

    The only such test in this module, and it is reached only through
    `member_asset_ids` — a door hands a table down and this is what reads it. The
    leading dot is kept, because that is the shape every `kinds` table in the
    library is written in (`bundle_detect.MODEL_SUFFIXES`, the import allow-list),
    and a seam that quietly restated `.stl` as `stl` would match nothing and say
    nothing.
    """
    return PurePath(path or "").suffix.lower()


def create_project_from_members(
    session: Session,
    *,
    name: str,
    origin: str,
    members: Sequence[MemberCandidate] = (),
    marker: Optional[dict] = None,
    dedupe: Optional[Dedupe] = None,
    cover_asset_id: Optional[str] = None,
    kinds: Optional[Sequence[str]] = None,
    require_members: bool = True,
    refuse: Optional[str] = None,
    description: Optional[str] = None,
    creator: Optional[str] = None,
    source_url: Optional[str] = None,
    project_origin: str = "own",
    licence: Optional[str] = None,
    licence_url: Optional[str] = None,
    asset_index: Optional[Mapping[str, Tuple[str, ...]]] = None,
) -> CreateOutcome:
    """One dedupe pass, then one insert, in one transaction.

    The order is the contract:

      1. ask `find_existing_project` — once, with the identities the door holds. If it
         answers, NOTHING is written and the answer is `already_created` with the
         project and the identity that matched;
      2. honour `refuse`, a reason the door established from its OWN rules before
         asking (the bundle door's contains-bundles belt). It is asked second so a row
         that both already exists and would be refused reports `already_created`: the
         operator already has the project, and that is the more useful sentence;
      3. refuse an empty member set, so a door can never leave a project with no files
         and no way to get them;
      4. write it — `project_service.create_project_with_members`, which is the only
         place the Project row and its `project_asset` rows are committed, and they
         commit together.

    `origin` is the DOOR (`detection` | `archive_extract` | `manual`); it picks the
    marker sub-block and is not the `Project.origin` column, which is `project_origin`.
    `marker=None` writes no marker: a door with no detection payload — `POST /projects`
    — must not claim one, because a marker is what makes
    `GET /project/{id}/bundle-drift` answer `known` rather than `not_applicable`, and a
    hand-made project has no detection to be drifted from.
    """
    if origin not in ORIGINS:
        raise ValueError(f"unknown create door {origin!r}")

    asset_ids = member_asset_ids(members, kinds=kinds)

    if dedupe is not None:
        existing = find_existing_project(
            session,
            key=dedupe.key,
            archive=dedupe.archive,
            source_url=dedupe.source_url,
            name=dedupe.name,
            assets=asset_ids if dedupe.assets else (),
            asset_index=asset_index,
        )
        if existing is not None:
            return CreateOutcome(
                status=STATUS_ALREADY_CREATED,
                project_id=existing.project.id,
                name=existing.project.name,
                match=existing.match,
                members=tuple(asset_ids),
            )

    if refuse:
        return CreateOutcome(status=STATUS_REFUSED, reason=refuse, members=tuple(asset_ids))
    if require_members and not asset_ids:
        return CreateOutcome(
            status=STATUS_REFUSED, reason="no member files resolved", members=()
        )

    stored = None
    if marker is not None:
        # `origin` is written LAST so a door cannot overwrite its own discriminator
        # through the payload it hands in: the namespace is this module's to stamp.
        stored = {BUNDLE_MARKER: {**marker, ORIGIN_FIELD: origin}}
        block = marker.get(ORIGIN_ARCHIVE_EXTRACT)
        if origin == ORIGIN_ARCHIVE_EXTRACT and isinstance(block, dict):
            # The twin is what a drift walk needs, and the archive door knows it only
            # as a field of its own payload; promote it so `bundle_drift.walk_root`
            # reads one field for both doors.
            stored[BUNDLE_MARKER].setdefault("rel_dir", _normalised(block.get("twin")))

    project = project_service.create_project_with_members(
        session,
        name,
        description,
        cover_asset_id,
        asset_ids,
        creator=creator,
        source_url=source_url,
        licence=licence,
        licence_url=licence_url,
        origin=project_origin,
        metadata=stored,
    )
    return CreateOutcome(
        status=STATUS_CREATED,
        project_id=project.id,
        name=project.name,
        members=tuple(asset_ids),
    )
