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
creation happens from *registered* assets, so the operator path is
``POST /import/zip`` and then a library scan, never a create from an archive.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

import audit
import bundle_service
from auth import AuthContext
from permissions import require_contribute, require_read
from schemas import (
    BundleCreateRequest,
    BundleCreateResponse,
    BundleDismissRequest,
    BundleDismissResponse,
    BundleReportOut,
)

router = APIRouter()


# Bundle endpoints ----------------------------------------------
# Three routes over one detector: read the report, create the confirmed bundles,
# and hide a bundle that is not a project. The keys are storage-root-relative
# paths, so a request names what the report named and nothing is guessed.


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
