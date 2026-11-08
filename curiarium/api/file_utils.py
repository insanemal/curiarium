import mimetypes
import os
import re
from pathlib import PurePosixPath
from typing import Dict, Optional
from urllib.parse import unquote, urlparse

from fastapi import HTTPException

from config import IMPORT_ALLOWED_EXTS

# The library's own answer for the extensions a 3D-print library is made of.
# `mimetypes` cannot be trusted for these, and both halves are measured, not
# assumed: the published image is `python:3.12-slim`, which ships no
# /etc/mime.types, so the stdlib table answers None for .stl/.3mf/.step/.stp and
# `application/octet-stream` for .obj; and a host that DOES ship that file
# disagrees in the same way for a different reason — it says
# `application/vnd.ms-3mfdocument` for .3mf and `application/p21` for .step/.stp,
# and neither says "model" to a `model/` prefix. Every value here is therefore a
# `model/*` value, deliberately: `asset_file_service.preview_media_type` falls
# back to guessing from the filename for `application/octet-stream` rows and
# serves `text/*` inline, so a `text/*` value here would make a stored document
# previewable that is refused today.
#
# These five suffixes are the same set `web/src/lib/assetKinds.ts` and
# `web/src/ui/asset-grid/assetGridMeta.ts` call a model, and the same values
# `web/src/lib/zipUtils.ts` writes into an archive; `scripts/check_model_table.py`
# refuses the build when the copies drift. The set is NOT
# `bundle_detect.MODEL_SUFFIXES`, and must not become it: detection's set also
# carries .lbrn/.lbrn2 and omits .obj, and detection is the thing this whole
# area of the codebase refuses to consult.
MODEL_MIME_BY_SUFFIX: Dict[str, str] = {
    ".stl": "model/stl",
    ".3mf": "model/3mf",
    ".step": "model/step",
    ".stp": "model/step",
    ".obj": "model/obj",
}

# Frozen at import, on purpose. A test simulates a host that knows no model mime
# by emptying `MODEL_MIME_BY_SUFFIX`; that must stop the *scan* from knowing, and
# must not reach `is_model_path`, whose whole job is to answer without the mime
# column. Two names, two lifetimes — do not collapse them into one.
MODEL_SUFFIXES = frozenset(MODEL_MIME_BY_SUFFIX)


def sanitize_filename(name: str) -> str:
    name = (name or "").replace("\x00", "").strip()
    name = name.replace("/", "_").replace("\\", "_")
    name = os.path.basename(name)
    return name or "imported-file"


def sanitize_path_segment(value: str, fallback: str = "item") -> str:
    """Make a value safe to use as a single path component in a zip arcname.

    Folder and model names are user input; without this a name like ``../../x``
    produces a zip that escapes whatever directory it is extracted into.
    """
    value = (value or "").replace("\x00", "").replace("/", "_").replace("\\", "_")
    value = value.strip().strip(".").strip()
    return value or fallback


def parse_content_disposition(cd: str) -> Optional[str]:
    if not cd:
        return None
    match = re.search(r"filename\*=([^']*)''([^;]+)", cd, re.IGNORECASE)
    if match:
        return unquote(match.group(2))
    match = re.search(r'filename="?([^\";]+)"?', cd, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def build_import_filename(url: str, headers, override: Optional[str]) -> str:
    if override:
        name = sanitize_filename(override)
    else:
        dispo = headers.get("Content-Disposition", "")
        name = parse_content_disposition(dispo) or ""
        if not name:
            name = os.path.basename(urlparse(url).path)
        name = sanitize_filename(name)

    content_type = headers.get("Content-Type", "")
    base, ext = os.path.splitext(name)
    ext = ext.lower()
    if not ext:
        url_ext = os.path.splitext(urlparse(url).path)[1].lower()
        ext = url_ext or ""
    if not ext and content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        ext = guessed or ""
    if ext and not name.lower().endswith(ext):
        name = f"{base or 'imported-file'}{ext}"
    if not ext:
        raise HTTPException(status_code=415, detail="Unable to determine file extension")
    if ext not in IMPORT_ALLOWED_EXTS:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {ext}")
    return name


def mime_from_content_type(content_type: str, filename: str) -> str:
    base = (content_type or "").split(";")[0].strip().lower()
    if not base or base in {"application/octet-stream", "binary/octet-stream"}:
        guessed, _ = mimetypes.guess_type(filename)
        return guessed or base or "application/octet-stream"
    return base


def is_html_content_type(content_type: str) -> bool:
    base = (content_type or "").split(";")[0].strip().lower()
    return base in {"text/html", "application/xhtml+xml"}


def is_json_content_type(content_type: str) -> bool:
    base = (content_type or "").split(";")[0].strip().lower()
    return base in {"application/json", "text/json", "application/ld+json"}


def model_mime_for_path(path: "os.PathLike | str") -> Optional[str]:
    """The mime the library promises for a model path, or None. Scan side.

    Reads the mutable ``MODEL_MIME_BY_SUFFIX`` — that is the name a test empties to
    reproduce a host that knows no model mime.
    """
    suffix = PurePosixPath(str(path)).suffix.lower()
    return MODEL_MIME_BY_SUFFIX.get(suffix)


def is_model_path(path: "os.PathLike | str") -> bool:
    """Whether a path names a model, by extension only. Decision side.

    Reads the frozen ``MODEL_SUFFIXES``, never ``MODEL_MIME_BY_SUFFIX``: a test that
    empties the table to simulate a mime-blind host must blind the *scan*, not this
    answer, whose whole job is to need no mime column. The suffix is taken the same
    way as in `model_mime_for_path` — last suffix, lowercased, posix separators — so
    the two names can never disagree about what a path is.
    """
    suffix = PurePosixPath(str(path)).suffix.lower()
    return suffix in MODEL_SUFFIXES


def guess_mime_from_path(path: os.PathLike) -> str:
    known = model_mime_for_path(path)
    if known:
        return known
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"
