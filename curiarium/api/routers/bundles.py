"""Bundle detection: a dry-run report from disk, then the confirmed creation (Mf-vwqf).

The report is a READ and nothing else. It walks the storage root and reads at most
three small documents per candidate; it creates no folder, no row and no file, so
it is safe to serve to a viewer and safe to re-request. Like
``GET /storage/capabilities`` it answers from local state only - no network, no
host probing - which is also what keeps the permission sweeps hermetic.

Creation is audited while ``POST /projects`` is exempt, deliberately: a bundle
create is a bulk, disk-derived act (one request may make many projects), and the
event doubles as the operator's creation history. The re-run is idempotent, so the
event is the history, never a duplicate.

A zip with no extracted twin is reported and always ``blocked``/``archive_only``:
creation happens from *registered* assets, so the extraction door is
``POST /import/archive`` - an archive already in the library extracts in place
into its twin directory - and a scan then registers it, never a create from an
archive.

Enrichment is the ONLY route in this module that reaches the network. It is run
on demand by a human and is never part of the report: ``GET /bundles/detect`` is a
disk walk and must stay offline and deterministic.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

import audit
import bundle_service
import import_resolvers
from auth import AuthContext
from permissions import require_contribute, require_read
from schemas import (
    BundleCreateRequest,
    BundleCreateResponse,
    BundleDismissRequest,
    BundleDismissResponse,
    BundleEnrichRequest,
    BundleEnrichResponse,
    BundleReportOut,
)

router = APIRouter()


# Bundle endpoints ----------------------------------------------
# Four routes over one detector: read the report, create the confirmed bundles,
# hide a bundle that is not a project, and ask the vendor for a bundle's missing
# metadata. The keys are storage-root-relative paths, so a request names what the
# report named and nothing is guessed. Enrichment is the only route here that
# networks; the report above it never does.


@router.get("/bundles/detect", response_model=BundleReportOut)
def detect_bundles(
    path: str = "",
    limit: int = Query(default=bundle_service.DEFAULT_LIMIT, ge=1, le=1000),
    include_dismissed: bool = False,
    _: Optional[AuthContext] = Depends(require_read),
):
    """The whole detection report, recomputed from disk now. Writes nothing.

    A READ: no folder healing, no rows, no commits. `path` scopes the walk and
    `limit` bounds the rows; the counts still describe everything found and
    `truncated` says rows were withheld.
    """
    return bundle_service.build_report(path=path, limit=limit, include_dismissed=include_dismissed)


@router.post("/bundles/create", response_model=BundleCreateResponse)
def create_bundles(
    request: Request,
    body: BundleCreateRequest,
    context: Optional[AuthContext] = Depends(require_contribute),
):
    """Create one project per confirmed bundle; partial success is the contract.

    The report is recomputed first and every key is looked up in it, so a key the
    client sends is never trusted on its own. A bundle that is blocked, or that
    already became a project, creates nothing and says so per item.
    """
    items = [
        bundle_service.BundleCreateItem(key=item.key, name=item.name, name_only=item.name_only)
        for item in body.items
    ]
    return bundle_service.create_bundles(items, audit.context_for(request, context))


@router.post("/bundles/dismiss", response_model=BundleDismissResponse)
def dismiss_bundles(
    request: Request,
    body: BundleDismissRequest,
    context: Optional[AuthContext] = Depends(require_contribute),
):
    """Hide or un-hide bundles in the report; a toggle, so dismiss has an undo."""
    return bundle_service.set_dismissals(body.keys, body.dismissed, audit.context_for(request, context))


@router.post("/bundles/enrich", response_model=BundleEnrichResponse)
def enrich_bundles(
    request: Request,
    body: BundleEnrichRequest,
    context: Optional[AuthContext] = Depends(require_contribute),
):
    """Read the vendor pages these bundles name and cache what the pages say.

    On demand by a human, never part of `GET /bundles/detect`: that report is a
    disk walk and must stay offline and deterministic. It fills only empty fields
    and never invents a licence. A per-item `refused` is the bulk partial-success
    contract, so one rate-limited thing does not discard the pages that answered.
    A 429 detail reuses `THINGIVERSE_COOKIE_REMEDY`, so it names the Settings
    paste and the environment variable alike.
    """
    cookie = import_resolvers.resolve_thingiverse_cookie(body)
    return bundle_service.enrich_bundles(
        body.keys,
        cookie=cookie,
        refresh=body.refresh,
        ctx=audit.context_for(request, context),
    )
