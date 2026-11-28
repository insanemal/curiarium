"""Web imports: the two read-only preflights, and nothing that writes.

Every import is an `import-link`, `import-zip` or `extract` operation now (Mf-y2u3
parts 2/4 and 3/4): `POST /import`, `POST /import/zip`, `POST /import/project`,
`POST /import/archive` and `GET /import/archive/entries` are gone, so no route
both fetches or reads an archive and writes in one opaque call. What stays is what
a client needs to REVIEW a fetch before it is planned - `POST /import/inspect` and
`POST /import/zip/entries` fetch a URL and write nothing.

Split from main.py in Mf-x9dx; behaviour-preserving move.
"""

from pathlib import Path
from typing import Optional
from zipfile import BadZipFile

from fastapi import APIRouter, Depends, HTTPException

import seams
from auth import AuthContext
from file_utils import build_import_filename, mime_from_content_type
from import_policy import refuse_paid_or_gated
from permissions import require_contribute
from schemas import (
    ImportInspectOut,
    ImportRequest,
    ImportZipEntriesOut,
    ImportZipEntriesRequest,
)
from zip_service import list_zip_entries

router = APIRouter()


@router.post("/import/inspect", response_model=ImportInspectOut)
def inspect_import_link(body: ImportRequest, _: Optional[AuthContext] = Depends(require_contribute)):
    refuse_paid_or_gated(body.url)
    url = seams.normalize_import_url(body.url)
    resp, final_url = seams.open_import_response(url, body)
    with resp:
        filename = build_import_filename(final_url, resp.headers, body.filename)
        mime = mime_from_content_type(resp.headers.get("Content-Type", ""), filename)
    is_zip = Path(filename).suffix.lower() == ".zip"
    return ImportInspectOut(filename=filename, mime=mime, is_zip=is_zip)


@router.post("/import/zip/entries", response_model=ImportZipEntriesOut)
def list_import_zip_entries(body: ImportZipEntriesRequest, _: Optional[AuthContext] = Depends(require_contribute)):
    refuse_paid_or_gated(body.url)
    url = seams.normalize_import_url(body.url)
    tmp_path, filename, _ = seams.download_import_to_temp(url, body)
    try:
        if Path(filename).suffix.lower() != ".zip":
            raise HTTPException(status_code=415, detail="Imported file is not a zip")
        try:
            entries = list_zip_entries(tmp_path)
        except BadZipFile:
            raise HTTPException(status_code=400, detail="Invalid zip file") from None
        if not entries:
            raise HTTPException(status_code=400, detail="No files found in zip")
        return ImportZipEntriesOut(filename=filename, entries=entries)
    finally:
        tmp_path.unlink(missing_ok=True)
