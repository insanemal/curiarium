import mimetypes
import os
import re
from pathlib import PurePosixPath
from typing import AbstractSet, Dict, Optional
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


class FilenameHasPath(ValueError):
    """A filename renderer was handed a path it cannot honestly flatten.

    ``name`` is the value as received, so a door can name it in its 400.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"{name!r} carries a path; a filename renderer cannot flatten it")
        self.name = name


def sanitize_filename(name: str) -> str:
    """The library's single filename renderer: one segment, or a refusal.

    A filename is a leaf. A value carrying a separator names a place, not a
    file, and this renderer has nowhere to put the rest of the path - the
    caller does. ``FilenameHasPath`` says so instead of inventing a flat name:
    the 2026-11-24 mangling was exactly the silent flattening this refuses, and
    deleting it is the fix. The earlier ``os.path.basename`` call here was a
    no-op *after* the separators had been replaced, so every path-carrying name
    was quietly rewritten into a name the sender never used.

    ``.`` and ``..`` are returned unchanged: dot policy belongs to
    ``sanitize_path_segment`` and ``entry_policy.entry_member_segments``, which
    refuse them deliberately, and a renderer that renamed them would hide that
    from the door that has to answer for it.
    """
    cleaned = (name or "").replace("\x00", "").strip()
    if "/" in cleaned or "\\" in cleaned:
        raise FilenameHasPath(name)
    return cleaned or "imported-file"


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


def _import_filename(name: str) -> str:
    """Render one import filename, refusing a path with a 400 that names it."""
    try:
        return sanitize_filename(name)
    except FilenameHasPath as exc:
        raise HTTPException(status_code=400, detail=f"Filename must not contain a path: {exc.name!r}") from None


def build_import_filename(url: str, headers, override: Optional[str]) -> str:
    """The filename a fetched URL or a caller's override lands under.

    This door has no folder placement to offer - it answers a URL, not a
    directory tree - so a name carrying a path cannot be honoured. It refuses
    instead of mangling it: a flattened name is a file the sender never named.
    """
    if override:
        name = _import_filename(override)
    else:
        dispo = headers.get("Content-Disposition", "")
        name = parse_content_disposition(dispo) or ""
        if not name:
            name = os.path.basename(urlparse(url).path)
        name = _import_filename(name)

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


# --- bundle-member paths (Mf-1agz) ------------------------------------------
# The path vocabulary a bundle's membership is judged by. It lives here because
# these are path predicates like every other one in this module, and because the
# detector, the archive door and the service all have to agree on them.

# Directory names that say nothing about what a bundle IS: they are the shape a
# download or a hand extraction lands in, not a name a vendor chose. The live
# library measured 51 of 57 archive entry roots pointing at one of these, so a
# root-matching rule without this guard would pair half the library to the wrong
# directory. An EXACT casefolded set, never a prefix or a fuzzy match: `my files/`
# and `files_and_docs/` are real bundle roots, and loose matching is exactly the
# mis-pairing the guard exists to stop.
GENERIC_CONTAINER_ROOTS = frozenset({"files", "images", "models", "stl"})

# The directory rsync and hand extraction leave behind: an AppleDouble artifact,
# never a vendor's choice. Named rather than grown into a junk list, because the
# next junk name would be a guess. Public here because `bundle_detect` binds it
# as `_MACOSX_ROOT` for its rule-set stamp (Mf-1agz).
MACOSX_ROOT = "__macosx"


def is_macosx_artifact(rel_path: str) -> bool:
    """Whether this entry is AppleDouble cruft rather than a file of the bundle.

    Only the first ``/`` segment decides, so ``__MACOSX/17_Thing/._a.stl`` and a
    bare top-level ``__MACOSX`` both count. The one place this vocabulary lives:
    both the pure root rule and the disk-side pairing rule skip through it, so
    they cannot disagree about what an archive's payload is.
    """
    head, _separator, _rest = (rel_path or "").partition("/")
    return head.casefold() == MACOSX_ROOT


def is_bundle_member(rel_path: str, bundle_dirs: AbstractSet[str]) -> bool:
    """Whether ``rel_path`` is a file of one of the detected bundle directories.

    Membership is strict and raw: the path must sit BELOW the directory, so a
    ``<name>.zip`` beside a ``<name>/`` is that directory's twin (provenance),
    not one of its files, and the comparison is case-sensitive exactly like the
    container strip and ``_forget_claimed_subtree`` it has to agree with. This
    is the one place the Mf-74x5 rule is expressed: a nested archive is a
    supporting file of the enclosing bundle, never a bundle of its own.
    """
    head, separator, _rest = (rel_path or "").rpartition("/")
    while separator:
        if head in bundle_dirs:
            return True
        head, separator, _rest = head.rpartition("/")
    return False


def is_generic_container_root(name: str) -> bool:
    """Whether ``name`` says nothing about which bundle an archive holds."""
    return (name or "").strip().casefold() in GENERIC_CONTAINER_ROOTS
