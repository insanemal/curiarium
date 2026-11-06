"""Whether a bundle folder has grown since its project was made (Mf-8i04).

The one contract both surfaces ask: the per-project route
(``GET /project/{project_id}/bundle-drift``) and the Review queue's
already-created row. Two surfaces that computed this separately could disagree
about the same folder, and the number is the ACT button's own evidence, so they
share these functions instead.

Drift is a set difference over rows the app already holds - the assets
registered under the marker's walk root, minus the project's membership rows -
computed on every read and never stored. A stored count is a false count the
moment a rescan registers the next part.

**The walk root has two key spaces.** A detection marker's ``key`` is the
storage-relative path ``bundle_service._create_one`` wrote
(``key = row["rel_path"]``) and its ``rel_dir`` is the same path; a zip-project
marker's ``key`` is an IDENTITY (``zip-project:<canonical url>``) - a prefix
query on it matches nothing, which is the false zero ADR 0010 Decision 26
forbids - while its ``rel_dir`` may legitimately be ``None``. So the walk root
is ``rel_dir`` when it names one, otherwise the detection door's own ``key``,
otherwise nothing at all: ``unknown`` is the honest answer, never ``0``.

**The set is MODEL-scoped, by the scan's own rule.** ``registered minus member``
over every asset reads the support files a create never attaches (every
``cover.jpg`` and ``notes.txt``) as drift, so a fresh project would prompt
immediately; the measured compound tree reads 26 naive against an honest 0. The
predicate is ``bundle_detect.is_counted_model`` - the basename model rule the
scan itself registers by (Mf-94od) - and it is deliberately NOT described as
"the set ``find_existing_project`` uses": that containment is the candidate's
own ``_own_paths`` tested for a model SUFFIX, so the two diverge in both
directions (that one counts an AppleDouble ``._a.stl`` and excludes a nested
child bundle's paths; this one does the opposite in both places), and
``test_drift_is_model_scoped_so_support_files_are_never_drift`` pins the first.
The nested-bundle breadth is accepted and recorded in the ADR, and the
excluded-group subtraction below is a third place drift is deliberately
STRICTER than that containment.

**Three states, exhaustive**, because count honesty has two owners (the server
picks the state, the client formats the number):

* ``not_applicable`` - no ``_bundle`` marker (a hand-made project, or malformed
  metadata): no notice, and no count at all;
* ``known`` - a walk root exists and still holds a registered row, so ``count``
  is the honest number and ``0`` renders as no notice, never as a rendered zero;
* ``unknown`` - the marker names no path this module may prefix-query (a
  zip-project identity with no ``rel_dir``), or it names a root no registered
  row still lives under: the client renders an en dash.

**A stale walk root is ``unknown``, not a zero.** ``rel_dir`` is a creation-time
path that nothing rewrites when the folder is renamed, so a root that still
holds no registered row is a directory the library no longer sees, and a ``0``
there is exactly the false zero the ticket forbids: the state is ``unknown``.
Presence is asked of REGISTERED ROWS, not of the folder table, because the route
may not spend a statement on it. The recorded tradeoff: a bundle root whose
registered rows were all deleted (or a folder that exists on disk with nothing
registered under it) now answers ``unknown`` - an en dash - where it used to
answer ``known``/0. That is the conservative direction, an en dash instead of a
zero the server cannot support, and the smallest version that closes the rename
hole.

**An excluded group is subtracted.** A compound create with ``exclude_groups``
leaves that group's models registered and unattached by design, so including
them would prompt forever and "Add them" would attach exactly what the operator
excluded. This is deliberately STRICTER than ``find_existing_project``'s
containment, which ignores the exclusion; the divergence is pinned by its own
test rather than left to be discovered.

Pure except for ``project_drift``, the one DB door: literals in, an ordered set
out, so every rule above is testable without a database.
"""

from typing import Callable, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlmodel import Session, func, select

import bundle_detect
import project_service
from models import Asset, Project, ProjectAsset

DRIFT_NOT_APPLICABLE = "not_applicable"
DRIFT_KNOWN = "known"
DRIFT_UNKNOWN = "unknown"


def _non_empty_string(value: object) -> Optional[str]:
    """``value`` stripped when it is a non-empty string, otherwise ``None``.

    A marker is JSON from a column a human may have edited, so every read here
    has to survive a number, a list or a blank string without trusting it.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def walk_root(marker: Mapping[str, object]) -> Optional[str]:
    """The storage-relative subtree a marker's assets live under, or ``None``.

    ``rel_dir`` first: both doors record it, and a zip-project may set it while
    its ``key`` is an identity. Then the detection door's ``key`` - that door is
    the only one that never writes a ``source``, and ``_create_one`` puts the
    row's storage-relative path there. A marker carrying a ``source`` has an
    identity ``key``, so with no ``rel_dir`` there is no path to walk: ``None``,
    never the key (Decision 26).
    """
    root = _non_empty_string(marker.get("rel_dir"))
    if root is not None:
        return root
    if marker.get("source"):
        return None
    return _non_empty_string(marker.get("key"))


def has_rows_under(registered: Iterable[Tuple[str, str, int]], root: str) -> bool:
    """Whether ANY registered row lives under ``root``; the presence probe.

    A root nothing is registered under is a directory the library no longer
    sees - a renamed folder leaves the marker's ``rel_dir`` pointing at a path
    the library has moved away from - and ``0`` there is the false zero the
    ticket forbids, so the caller answers ``unknown`` instead.

    Path-boundary honest: ``import/Alpha2/x.stl`` is not under ``import/Alpha``.
    """
    prefix = root + "/"
    for _asset_id, storage_path, _size in registered:
        if isinstance(storage_path, str) and storage_path.startswith(prefix):
            return True
    return False


def classify(marker: object, *, root_present: Callable[[str], bool]) -> Tuple[str, Optional[str]]:
    """``(state, walk_root)`` for one ``_bundle`` marker; the three cases, exhaustive.

    ``walk_root`` is ``None`` unless the state is ``known``, so a caller cannot
    prefix-query a state that has no honest root. Anything that is not a
    non-empty dict - ``{}`` for a project with no marker, a string a malformed
    metadata column produced - is ``not_applicable``: there is no question to
    answer, which is not the same answer as zero.

    A walk root that exists but holds no registered row (``root_present`` says
    ``False``) is ``unknown``, never ``known``/0: a renamed folder leaves the
    marker's creation-time ``rel_dir`` pointing at a path nothing is registered
    under, and a zero there would claim a measurement of a directory the library
    no longer sees. ``root_present`` is consulted AT MOST ONCE - and only when
    there is a root - so an identity-key marker still costs nothing.

    Mutation: drop the presence check and a renamed folder answers ``known``/0
    forever.
    """
    if not isinstance(marker, Mapping) or not marker:
        return DRIFT_NOT_APPLICABLE, None
    root = walk_root(marker)
    if root is None:
        return DRIFT_UNKNOWN, None
    if not root_present(root):
        return DRIFT_UNKNOWN, None
    return DRIFT_KNOWN, root


def excluded_prefixes(root: str, marker: Mapping[str, object]) -> Tuple[str, ...]:
    """The ``root/<group>/`` prefixes of every group the marker records as excluded.

    ``groups`` is the marker's creation-time snapshot; only an explicit
    ``included: False`` subtracts (a group created before the flags existed, or
    one included as usual, subtracts nothing). De-duplicated and order-stable so
    the same marker always yields the same tuple.
    """
    groups = marker.get("groups")
    if not isinstance(groups, list):
        return ()
    prefixes: List[str] = []
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        if group.get("included") is not False:
            continue
        name = _non_empty_string(group.get("name"))
        if name is None:
            continue
        prefixes.append(f"{root}/{name}/")
    return tuple(dict.fromkeys(prefixes))


def drift_asset_ids(
    registered: Iterable[Tuple[str, str, int]],
    *,
    root: str,
    prefixes: Sequence[str] = (),
    is_member: Optional[Callable[[str], bool]] = None,
) -> List[str]:
    """The registered MODEL ids under ``root`` this project does not hold.

    ``registered`` is ``(asset id, storage path, size)`` rows, already read by the
    caller (one statement for a whole report). A row is drift only when all of:

    * its ``storage_path`` is a non-empty string, and
    * the path is under ``root + "/"``, and
    * the path is not under an excluded prefix (an empty tuple drops nothing:
      ``str.startswith(())`` is ``False``), and
    * the project does not already hold the asset, and
    * ``bundle_detect.is_counted_model`` calls it a model the scan would
      register - the measured rule that keeps ``cover.jpg``/``notes.txt`` and
      AppleDouble ``._*.stl`` copies from ever reading as new parts.

    Ordered by ``(storage_path, asset id)``: the answer is a property of the
    disk, never of the row order a query happened to return.
    """
    prefix = root + "/"
    # `str.startswith(())` is False, so an empty tuple drops nothing.
    excluded = tuple(prefixes)
    keep: List[Tuple[str, str]] = []
    for asset_id, storage_path, size in registered:
        if not isinstance(storage_path, str) or not storage_path:
            continue
        if not storage_path.startswith(prefix):
            continue
        if storage_path.startswith(excluded):
            continue
        if is_member is not None and is_member(asset_id):
            continue
        if not bundle_detect.is_counted_model(storage_path, int(size or 0)):
            continue
        keep.append((storage_path, asset_id))
    return [asset_id for _, asset_id in sorted(keep)]


def project_drift(session: Session, project: Project) -> Tuple[str, Optional[int], List[str]]:
    """``(state, count, asset ids)`` for one project: the route's one DB door.

    The marker is read from the project row the caller already holds, so the
    non-``known`` states cost no statement at all. ONE prefix SELECT answers both
    questions ``known`` asks - whether the root still holds a registered row (the
    presence probe ``classify`` needs) and, for each row it does hold, whether
    this project already holds the asset (a correlated ``EXISTS`` flag) - and it
    is run lazily from inside the presence callable, so ``not_applicable`` and
    ``unknown`` with no root still run no prefix query. The id list is then built
    by ``drift_asset_ids`` with that same membership flag, so the route and the
    report cannot answer differently. ``count`` is ``len(asset_ids)``: the number
    and the action are the same set.

    The measured cost, in the suite's authenticated harness: ``known`` is 3
    statements (the caller's auth lookup, the project row, one prefix SELECT),
    ``not_applicable`` and ``unknown``-with-no-root are 2, and a STALE root is 3
    and answers ``unknown`` - the same SELECT the presence probe reads.
    """
    marker = project_service.bundle_marker_of(project.metadata_json)
    rows: List[Tuple[str, str, int, bool]] = []

    def root_present(root: str) -> bool:
        """Run the one prefix SELECT (once) and answer the presence question."""
        if not rows:
            prefix = root + "/"
            membership = (
                select(ProjectAsset.asset_id)
                .where(
                    ProjectAsset.project_id == project.id,
                    ProjectAsset.asset_id == Asset.id,
                )
                .exists()
                .label("is_member")
            )
            statement = (
                select(Asset.id, Asset.storage_path, Asset.size, membership)
                .where(
                    Asset.storage_path.is_not(None),
                    func.substr(Asset.storage_path, 1, len(prefix)) == prefix,
                )
                .order_by(Asset.storage_path, Asset.id)
            )
            rows.extend(
                (row[0], row[1], int(row[2] or 0), bool(row[3])) for row in session.exec(statement)
            )
        return bool(rows)

    state, root = classify(marker, root_present=root_present)
    if state != DRIFT_KNOWN or root is None:
        return state, None, []
    registered = [(asset_id, storage_path, size) for asset_id, storage_path, size, _member in rows]
    members = {asset_id for asset_id, _storage_path, _size, is_member in rows if is_member}
    asset_ids = drift_asset_ids(
        registered,
        root=root,
        prefixes=excluded_prefixes(root, marker),
        is_member=members.__contains__,
    )
    return DRIFT_KNOWN, len(asset_ids), asset_ids
