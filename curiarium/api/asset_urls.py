"""The two URL shapes an asset has, in one place.

Both the asset/project serializers (``routers/presenters.py``) and the bundle
report (``bundle_service.py``) need to say where an asset's bytes are served
from. If each built its own f-strings they would drift - the bundle report would
offer a cover URL the asset page never produces, or a ``?v=`` token for a
thumbnail that does not exist - and a client would have to handle both. So the
shapes live here and every caller is a delegation.
"""

from typing import Optional
from urllib.parse import quote


def primary_url(asset_id: str, filename: Optional[str]) -> str:
    """The served path for an asset's own bytes.

    One shape for the asset serializer and the bundle cover: the query-less
    ``/file/<id>/<filename>`` route, with the filename percent-encoded because a
    vendor name may contain anything. An absent filename is encoded as the empty
    string, which the route already serves.
    """
    return f"/file/{asset_id}/{quote(filename or '', safe='')}"


def thumb_url(asset_id: Optional[str], thumb_version: int) -> Optional[str]:
    """The versioned thumbnail URL for an asset, or None when nothing was generated.

    The `?v=` token is the row's `thumb_version` (opaque to clients, still ``\\d+``,
    so `AssetOut`/`ProjectOut` are unchanged), bumped on every thumbnail write.
    Version 0 means "no thumbnail", so the absence is a row fact and this helper
    can never do I/O: a page of a thousand assets must not `stat()` a thousand
    files to decide whether a URL exists.

    ``thumb_version`` is an ``int`` because every caller normalises a possibly
    absent column with ``int(... or 0)`` first: this function is a pure format,
    not a place to re-decide what NULL means.
    """
    if not asset_id or thumb_version <= 0:
        return None
    return f"/thumb/{asset_id}.jpg?v={thumb_version}"
