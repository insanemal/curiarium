"""Orchestration for "import a URL into a project" (Mf-56z9).

Nothing here is HTTP plumbing and nothing here is a pydantic boundary: the route
normalises the URL, the import service performs the fetch, and this module
decides which project the result belongs to. Outbound calls go through `seams`
so the suite keeps patching the names it pins on `main`.

Two orderings are load-bearing:

* the files are imported BEFORE the project is found or created, so a hard
  failure cannot leave an empty junk project behind - while an EXPLICIT
  `project_id` is proved to exist first, so a stale id cannot leave the asset it
  just downloaded orphaned behind a later 404;
* a gated outcome stores no files and issues no second request - the page's own
  metadata is all it keeps.

The project is reused when the same page is imported twice, but the files are
not: a repeat import writes fresh asset rows and adds them as extra members,
because the filename and size are only known once the body has been streamed.
No member-level de-duplication exists on this path - this is an accepted
limitation of Mf-56z9, not something another flow covers.

The zip-as-project door now records the cover it picked and the `_bundle`
identity it created; the single-asset and gated doors record neither.
"""

import posixpath
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePath
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlparse
from zipfile import BadZipFile

from fastapi import HTTPException

import bundle_detect
import bundle_service
import entry_policy
import project_service
import seams
import zip_service
from asset_service import AssetOrigin
from import_metadata import PageMetadata, parse_page_metadata
from import_policy import GATED_IMPORT_DETAIL
from import_resolvers import CredentialRefusal
from models import Asset
from schemas import ImportProjectRequest
from url_utils import canonical_url

# The site itself refusing anonymous access is a gated outcome; any other status
# is the error it always was (a free model with no downloadable file stays 400).
GATED_STATUS_CODES = frozenset({401, 403})

# The zip-as-project door's `_bundle` identity. The key is an IDENTITY, never a
# storage path: this door has no bundle directory, so a path-shaped key would be
# a lie AND would collide with a detection key (`find_existing_project` compares
# marker keys exactly). It cannot collide: `render_storage_path` runs every
# composed segment through `sanitize_path_segment`, whose invalid set includes
# `:`, so no `Asset.storage_path` can contain the separator - pinned by
# `test_no_storage_path_can_contain_the_identity_keys_separator`.
ZIP_PROJECT_KEY_PREFIX = "zip-project:"
ZIP_PROJECT_SOURCE = "zip-project-import"


@dataclass(frozen=True)
class ImportOutcome:
    """What one import did: the project, the assets and the per-step results."""

    project_id: str
    created: bool
    assets: List[Asset] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    gated: bool = False
    message: str = ""


def _host_for_name(source_url: str) -> str:
    """The host to name an unnamed import after; total on garbage."""
    try:
        return (urlparse(source_url).hostname or "").lower() or "the web"
    except ValueError:
        return "the web"


def _is_gated(page, metadata: PageMetadata, error: HTTPException) -> bool:
    """Whether a failed fetch means "behind a login/purchase" rather than "no file".

    Both halves require a page this import actually read: the model page was
    public and only its FILES were refused. Without one there are no details to
    import, and a 403 at that point can equally be bot protection or a hotlink
    guard on a free model, so the original error stands (today's behaviour).

    With a page, an explicit boolean flag in it is the primary detector, and a
    401/403 from the site is the real one - that is the site refusing anonymous
    access while the page itself was readable.

    A ``CredentialRefusal`` is the third answer and never a gated one: the site
    refused the credential this server sent, so the operator's fix is the cookie
    remedy and not a purchase. The page's own flag is still checked FIRST, so a
    paid page that also refuses the credential keeps its details-only outcome.
    """
    if page is None:
        return False
    if metadata.gated:
        return True
    return not isinstance(error, CredentialRefusal) and error.status_code in GATED_STATUS_CODES


def _zip_assets(attempt, body: ImportProjectRequest, origin: Optional[AssetOrigin]) -> Tuple[List[Asset], List[str]]:
    """Extract the selected entries, always removing the downloaded temp file.

    Extracting a zip is its own door (`CreatedVia`), even inside a project
    import: the route names the request "link-import", and every row this
    function creates is honestly "zip-import".
    """
    tmp_path = attempt.tmp_path
    zip_origin = None if origin is None else replace(origin, created_via="zip-import")
    try:
        if Path(attempt.filename).suffix.lower() != ".zip":
            raise HTTPException(status_code=415, detail="Imported file is not a zip")
        try:
            return zip_service.extract_zip_entries_to_assets(tmp_path, body.entries, body, origin=zip_origin)
        except BadZipFile:
            raise HTTPException(status_code=400, detail="Invalid zip file") from None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _selected_entries(entries: List[str]) -> List[str]:
    """The selection as the extractor read it: normalised, de-duplicated, in order.

    `entry_policy.plan_selection` is the extractor's own rule for what a selection
    means, and `_requested_names` is the same normalise-and-dedupe over the same
    function, so asking it here keeps the marker's groups about the entries that
    were actually selected instead of the spellings the client sent.
    """
    seen: set[str] = set()
    selected: List[str] = []
    for entry in entries:
        normalized = entry_policy.normalize_zip_entry_path(entry)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        selected.append(normalized)
    return selected


def _entry_group(entry: str) -> Optional[str]:
    """The archive folder one entry lands in, or None for a root-level file.

    `zip_service.resolve_zip_folder_id` creates exactly these folders, so the
    marker's groups and the project's own sections name the same folders. They
    can still differ in content: a group whose every entry failed to extract has
    no row in that section.
    """
    segments = [segment for segment in entry.split("/")[:-1] if segment]
    return segments[0] if segments else None


def _marker_groups(entries: Sequence[str], failed: Sequence[str]) -> List[dict]:
    """One group per archive folder that holds a selected entry, with its counts.

    `model_count` is what the SELECTION names and `attached` is what came out of
    it - the same split the detection door records between the scan's count and
    its attach set. The caller has already filtered the selection to non-junk
    entries, so every entry here is one the extractor was asked to write; an
    entry is in `failed` exactly when the extractor wrote no row for it (an absent
    member, an oversize member, a directory member, a name conflict - every path in
    `zip_service.extract_zip_entries_to_assets` that creates no asset appends
    there). Counting a group's non-refused model entries
    is therefore the count of its extracted MODEL rows without a basename join
    that two folders could confuse. `foreign` and `included` are constants here:
    this door has no foreign-group vocabulary and never excludes.
    """
    refused = set(failed)
    per_group: dict[str, dict] = {}
    for entry in entries:
        group = _entry_group(entry)
        if group is None:
            continue
        counts = per_group.setdefault(group, {"model_count": 0, "attached": 0})
        if PurePath(entry).suffix.lower() not in bundle_detect.MODEL_SUFFIXES:
            continue
        counts["model_count"] += 1
        if entry not in refused:
            counts["attached"] += 1
    return [
        {
            "name": name,
            "model_count": counts["model_count"],
            "foreign": False,
            "included": True,
            "attached": counts["attached"],
        }
        for name, counts in sorted(per_group.items())
    ]


def _shared_rel_dir(entries: Sequence[str], assets: Sequence[Asset]) -> Optional[str]:
    """The one STORAGE directory every part sits under, or None.

    `rel_dir` is the walk root a later reader may descend (`_bundle.key` is an
    identity, not a path, so a prefix query on the key finds nothing). It is only
    recorded when the selection is genuinely one tree - every selected entry under
    ONE non-empty archive folder - because a mixed-root or flat archive has no
    honest walk root and a guessed one (the storage root, or the `Unassigned`
    bucket every unfiled upload shares) would make the next unfiled upload look
    like drift. A one-part selection records None too, because a single part shares
    a directory with nothing and its own model directory is not a root anything
    spans - recording it would move this field on a re-import that extracts one
    new part. Three of the four measured zips answer None; that is the truth about
    them, not a gap.
    """
    roots = {_entry_group(entry) for entry in entries}
    if len(roots) != 1 or None in roots:
        return None
    paths = [asset.storage_path for asset in assets if asset.storage_path]
    if not paths or len(paths) != len(assets):
        return None
    if len(paths) < 2:
        # "The directory the parts share" needs more than one part: a one-part
        # selection would record that part's own model directory, which is not a
        # root anything spans, and a re-import that extracts one new part would
        # move the field instead of leaving it alone.
        return None
    common = posixpath.commonpath(sorted({posixpath.dirname(path) for path in paths}))
    # A flat-template archive whose dirs share no segment gives `""`; the honest
    # answer is None, not `"."`.
    return common or None


def _cover_group_for_entries(entries: Sequence[str], filename: Optional[str]) -> Optional[str]:
    """The archive folder the cover came from, when exactly one entry names it.

    Best effort by BASENAME, because nothing on this path records which entry an
    asset row was written from (`create_asset_record` may also re-render a
    colliding filename). Zero matches and several matches both record None: on a
    provenance field a miss is cheaper than a guess.
    """
    matches = {_entry_group(entry) for entry in entries if entry_policy.entry_filename(entry) == filename}
    if len(matches) != 1:
        return None
    return matches.pop()


def _zip_project_facts(
    source_url: str, entries: Sequence[str], assets: Sequence[Asset], failed: Sequence[str]
) -> Tuple[Optional[str], dict]:
    """The cover id and the `_bundle` marker the zip-as-project door records.

    The cover is the detection create path's own rule, asked over the rows this
    door just created (`bundle_service.cover_for_registered`, through
    `bundle_service.registered_from_rows`) - never a re-query and never a second
    copy of the preference. `allow_model_preview=False` is the ONE deliberate
    divergence from that path: this door offers an image cover or none, because
    its ticket says so ("a cover when the archive contains a registered image, and
    no cover (not a broken one) when it does not"), so a models-only archive gets a
    blank card here where a detection-created project from the same files would
    get a rendered model. ADR 0010 Decision 26 records the clause and the
    one-line change that would adopt the model leg.

    The marker is the shape `bundle_service._create_one` writes, with honest
    nulls where no walk ran and `source` naming THIS door. The door name does not
    go into `detector`: that field's vocabulary is detection-rule names, and a
    door there would corrupt every later reader.
    """
    # Junk is in `plan.requested` but never in `plan.writable`, so it creates no
    # folder, no asset and no section: a group named for it would point at a
    # section `resolve_zip_folder_id` never made and inflate `model_count`, and a
    # junk sibling would otherwise veto `_shared_rel_dir` and turn the cover-group
    # lookup into "several matches".
    entries = [entry for entry in entries if not entry_policy.is_junk_entry(entry)]
    cover = bundle_service.cover_for_registered(
        bundle_service.registered_from_rows(list(assets)), None, allow_model_preview=False
    )
    groups = _marker_groups(entries, failed)
    cover_filename = next((asset.filename for asset in assets if asset.id == cover["asset_id"]), None)
    marker = {
        "key": ZIP_PROJECT_KEY_PREFIX + source_url,
        "source": ZIP_PROJECT_SOURCE,
        "detector": None,
        "confidence": None,
        "rel_dir": _shared_rel_dir(entries, assets),
        "archive": None,
        "twin_verified": None,
    }
    if groups:
        marker["shape"] = "compound"
        marker["groups"] = groups
    marker["cover"] = {
        "asset_id": cover["asset_id"],
        "rel_path": cover["rel_path"],
        "reason": cover["reason"],
        "group": _cover_group_for_entries(entries, cover_filename),
    }
    return cover["asset_id"], {"_bundle": marker}


def import_into_project(url: str, body: ImportProjectRequest, *, origin: Optional[AssetOrigin] = None) -> ImportOutcome:
    """Import `url` (already normalised) and attach the result to a project."""
    if body.project_id:
        project_service.require_project(body.project_id)
    wants_zip = body.entries is not None
    if wants_zip:
        attempt = seams.download_zip_attempt(url, body)
    else:
        attempt = seams.import_asset_attempt(url, body, origin=origin)

    page = attempt.page
    metadata = parse_page_metadata(page.html, page.url) if page else PageMetadata()
    source_url = canonical_url(page.url if page else url)
    # A zip import's title names a NEW project (the gated branch shares this
    # variable); on the single-file door it names the asset, not the project.
    suggested_name = (
        body.project_name
        or (body.title if wants_zip else None)
        or metadata.title
        or f"Imported model from {_host_for_name(source_url)}"
    )

    if attempt.error is not None:
        if not _is_gated(page, metadata, attempt.error):
            raise attempt.error
        project_id, created = project_service.find_or_create_import_project(
            project_id=body.project_id,
            suggested_name=suggested_name,
            source_url=source_url,
            creator=metadata.creator,
            licence=metadata.licence,
            licence_url=metadata.licence_url,
        )
        # Only a project that has no files is "details only": marking one that
        # already has members would tell the user its files are missing.
        if project_service.project_with_stats(project_id)[1] == 0:
            project_service.set_project_metadata_marker(project_id, {"_files": "user-supplied"})
        return ImportOutcome(project_id=project_id, created=created, gated=True, message=GATED_IMPORT_DETAIL)

    if wants_zip:
        assets, failed = _zip_assets(attempt, body, origin)
    elif attempt.asset is not None:
        assets, failed = [attempt.asset], []
    else:
        raise HTTPException(status_code=400, detail="No downloadable model file found. Use a direct download link.")

    cover_asset_id: Optional[str] = None
    bundle_marker: Optional[dict] = None
    if wants_zip:
        entries = _selected_entries(body.entries or [])
        cover_asset_id, bundle_marker = _zip_project_facts(source_url, entries, assets, failed)

    project_id, created = project_service.find_or_create_import_project(
        project_id=body.project_id,
        suggested_name=suggested_name,
        source_url=source_url,
        creator=metadata.creator,
        licence=metadata.licence,
        licence_url=metadata.licence_url,
        cover_asset_id=cover_asset_id,
        bundle_marker=bundle_marker,
    )
    added, skipped, _member_failures = project_service.add_assets(project_id, [asset.id for asset in assets])
    return ImportOutcome(
        project_id=project_id,
        created=created,
        assets=assets,
        added=added,
        skipped=skipped,
        failed=failed,
    )
