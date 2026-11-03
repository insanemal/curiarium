import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, List, Optional
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from fastapi import BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from asset_file_service import managed_asset_file_path
from asset_service import (
    AssetFilenameError,
    AssetOrigin,
    cleanup_asset,
    create_asset_record,
    ensure_model_thumbnail,
    finalize_asset_record,
    managed_asset_path,
    resolve_asset_file,
    save_thumb,
)
from config import IMPORT_MAX_BYTES, IMPORT_ZIP_MAX_BYTES
from db import engine
from file_utils import mime_from_content_type, sanitize_filename, sanitize_path_segment
from folder_service import validate_parent_folder
from models import Asset, AssetFile, Folder, Project
from schemas import ImportZipExtractRequest
from zip_junk import is_junk_entry

ATTRIBUTION_FILENAME = "ATTRIBUTION.txt"


def normalize_zip_entry_path(name: str) -> Optional[str]:
    cleaned = (name or "").replace("\\", "/").lstrip("/")
    if not cleaned or cleaned.endswith("/"):
        return None
    parts = [part for part in cleaned.split("/") if part]
    if not parts or any(part == ".." for part in parts):
        return None
    parts = [part for part in parts if part != "."]
    return "/".join(parts)


def sanitize_folder_name(name: str) -> str:
    cleaned = (name or "").replace("\x00", "").strip()
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    return cleaned or "folder"


def read_zip_text_entry(zip_path: Path, entry_path: str, max_bytes: int) -> Optional[str]:
    """Read one member as text without extracting anything, or ``None``.

    This is the only read path in the module that never extracts: bundle
    detection has to look inside an archive the library has not imported, and
    writing those bytes to disk as a side effect of a read would make the report
    a mutation. The cap is enforced twice on purpose - a declared
    ``file_size`` over ``max_bytes`` refuses before the stream is opened, and the
    stream itself is read for at most ``max_bytes + 1`` bytes, because the
    declaration is metadata a crafted archive can lie about and the bytes are the
    only truth. Anything over the cap, a bad archive and an unreadable file all
    answer ``None`` rather than raising.
    """
    normalized = normalize_zip_entry_path(entry_path)
    if not normalized:
        return None
    try:
        with ZipFile(zip_path) as zf:
            info = None
            for candidate in zf.infolist():
                if normalize_zip_entry_path(candidate.filename) == normalized:
                    info = candidate
                    break
            if info is None or int(info.file_size or 0) > max_bytes:
                return None
            with zf.open(info) as stream:
                payload = stream.read(max_bytes + 1)
    except (BadZipFile, OSError):
        return None
    if len(payload) > max_bytes:
        return None
    return payload.decode("utf-8", errors="replace")


STREAM_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ZipSelectionPlan:
    """What one entry selection resolves to, before a single byte is decompressed.

    ``entries`` is every normalised member of the archive, ``ordered`` is the
    request's picks after normalisation and de-duplication, ``oversized`` are the
    picks whose *declared* size is over ``IMPORT_MAX_BYTES`` and
    ``declared_total`` sums the rest. The declaration is metadata a crafted
    archive can lie about, so this plan rejects the obviously-too-big before any
    write and ``stream_zip_entry`` re-checks the bytes it actually reads.

    ``junk`` is every normalised member ``zip_junk.is_junk_entry`` calls OS
    metadata. Junk is left out of ``declared_total`` (a junk-only selection must
    not 413 on a cumulative cap) but stays in ``ordered``: the in-library archive
    door expects ``ordered`` to carry every explicitly selected name.
    """

    entries: dict[str, ZipInfo]
    ordered: List[str]
    oversized: FrozenSet[str]
    junk: FrozenSet[str]
    declared_total: int


class ZipEntryTooLarge(ValueError):
    """One member's own bytes crossed ``IMPORT_MAX_BYTES`` while streaming.

    A ``ValueError`` on purpose: the pre-existing extractor treats any
    non-``HTTPException`` failure as one bad entry (``failed``) rather than a bad
    request, and that contract must not change.
    """


class ZipBudgetExceeded(Exception):
    """This member's bytes crossed the selection's remaining budget.

    ``total_written`` is how many bytes this member had written when it crossed;
    the caller adds whatever the selection spent on earlier members, because the
    running total is the caller's, not one member's.
    """

    def __init__(self, total_written: int) -> None:
        super().__init__(f"selected zip entries expand beyond {total_written} bytes")
        self.total_written = total_written


def _normalised_members(zf: ZipFile) -> dict[str, ZipInfo]:
    """Every member the normaliser accepts, keyed by its normalised name."""
    members: dict[str, ZipInfo] = {}
    for info in zf.infolist():
        name = normalize_zip_entry_path(info.filename)
        if not name:
            continue
        members[name] = info
    return members


def all_zip_entry_names(zf: ZipFile) -> List[str]:
    """Every member name in archive order, normalised and de-duplicated.

    This is the ``entries: null`` selection for the in-library extraction door.
    De-duplication is deliberate: two raw members can normalise to one path
    (``/a.stl`` and ``a.stl``) and only one file can be written there.
    ``list_zip_entries`` reports the raw members instead, so an entries listing
    can show one such path twice where this returns it once.
    """
    names: List[str] = []
    seen: set[str] = set()
    for info in zf.infolist():
        name = normalize_zip_entry_path(info.filename)
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def plan_zip_selection(zf: ZipFile, selections: List[str]) -> ZipSelectionPlan:
    """Resolve one request's entries and refuse an empty or over-budget selection.

    Shared by both extractors so the two doors cannot disagree about what a
    selection means or when it is too big. The details are the exact strings the
    zip-import route has always answered with, because existing clients depend on
    them.
    """
    entries = _normalised_members(zf)
    junk = frozenset(name for name in entries if is_junk_entry(name))
    ordered: List[str] = []
    seen: set[str] = set()
    for raw in selections:
        name = normalize_zip_entry_path(raw)
        if not name or name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    if not ordered:
        raise HTTPException(status_code=400, detail="No zip entries selected")
    oversized: set[str] = set()
    declared_total = 0
    for entry_name in ordered:
        info = entries.get(entry_name)
        if not info or info.is_dir():
            continue
        declared = int(info.file_size or 0)
        if declared > IMPORT_MAX_BYTES:
            oversized.add(entry_name)
            continue
        if entry_name in junk:
            continue
        declared_total += declared
    if declared_total > IMPORT_ZIP_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Selected zip entries expand to {declared_total} bytes; "
                f"the limit is {IMPORT_ZIP_MAX_BYTES} bytes (IMPORT_ZIP_MAX_MB)"
            ),
        )
    return ZipSelectionPlan(
        entries=entries,
        ordered=ordered,
        oversized=frozenset(oversized),
        junk=junk,
        declared_total=declared_total,
    )


def stream_zip_entry(zf: ZipFile, info: ZipInfo, dest: Path, *, remaining_bytes: int) -> int:
    """Stream one member into ``dest`` and return the bytes written.

    Both caps are enforced on the bytes, never on the declaration: the member's
    own size against ``IMPORT_MAX_BYTES`` (``ZipEntryTooLarge``) and what is left
    of the selection's budget (``ZipBudgetExceeded``). The chunk size is fixed so
    a lying archive cannot make the copy allocate its real size up front.
    """
    size = 0
    with zf.open(info) as src, open(dest, "wb") as out:
        while True:
            chunk = src.read(STREAM_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > IMPORT_MAX_BYTES:
                raise ZipEntryTooLarge("Extracted file exceeds size limit")
            if size > remaining_bytes:
                raise ZipBudgetExceeded(size)
            out.write(chunk)
    return size


def list_zip_entries(zip_path: Path) -> List[dict]:
    entries: List[dict] = []
    with ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = normalize_zip_entry_path(info.filename)
            if not name:
                continue
            entries.append({"path": name, "size": int(info.file_size or 0), "junk": is_junk_entry(name)})
    entries.sort(key=lambda entry: entry["path"])
    return entries


def get_or_create_folder(session: Session, parent_id: Optional[str], parent_key: str, name: str, cache: dict) -> str:
    key = f"{parent_key}/{name}"
    cached = cache.get(key)
    if cached:
        return cached
    existing = session.exec(select(Folder).where(Folder.parent_id == parent_id, Folder.name == name)).first()
    if existing:
        cache[key] = existing.id
        return existing.id
    folder = Folder(name=name, tags_json="[]", parent_id=parent_id)
    session.add(folder)
    session.commit()
    session.refresh(folder)
    cache[key] = folder.id
    return folder.id


def resolve_zip_folder_id(session: Session, base_folder_id: Optional[str], entry_path: str, cache: dict) -> Optional[str]:
    segments = [segment for segment in entry_path.split("/")[:-1] if segment]
    if not segments:
        return base_folder_id
    target_id = base_folder_id
    parent_key = base_folder_id or "root"
    for segment in segments:
        safe = sanitize_folder_name(segment)
        if not safe:
            continue
        target_id = get_or_create_folder(session, target_id, parent_key, safe, cache)
        parent_key = f"{parent_key}/{safe}"
    return target_id


def extract_zip_entries_to_assets(
    zip_path: Path,
    selections: List[str],
    body: ImportZipExtractRequest,
    *,
    origin: Optional[AssetOrigin] = None,
) -> tuple[List[Asset], List[str]]:
    assets: List[Asset] = []
    failed: List[str] = []
    created_ids: List[str] = []
    total_written = 0
    with ZipFile(zip_path) as zf, Session(engine) as session:
        plan = plan_zip_selection(zf, selections)
        if body.folder_id:
            validate_parent_folder(session, body.folder_id)
        folder_cache: dict = {}
        for entry_name in plan.ordered:
            info = plan.entries.get(entry_name)
            # Junk (OS metadata) fails here, before any byte is decompressed and
            # before `resolve_zip_folder_id`: it creates no Folder row, no asset
            # row and no audit row, consumes no name slot, and can become neither
            # a project member nor a cover.
            if not info or info.is_dir() or entry_name in plan.oversized or entry_name in plan.junk:
                failed.append(entry_name)
                continue
            target_folder_id = resolve_zip_folder_id(session, body.folder_id, entry_name, folder_cache)
            filename = sanitize_filename(os.path.basename(entry_name))
            mime = mime_from_content_type("", filename)
            asset: Optional[Asset] = None
            try:
                # The archive title names the project and never the files: a None
                # title makes `create_asset_record` fall back to
                # `model_name_from_filename`, so every entry keeps its own stem.
                asset = create_asset_record(
                    filename,
                    mime,
                    title=None,
                    notes=body.notes,
                    tags=body.tags,
                    folder_id=target_folder_id,
                    origin=origin,
                )
                created_ids.append(asset.id)
                dest = managed_asset_path(asset)
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    size = stream_zip_entry(zf, info, dest, remaining_bytes=IMPORT_ZIP_MAX_BYTES - total_written)
                except ZipBudgetExceeded as overspend:
                    # The archive lied about its declared sizes; abandon the whole
                    # selection rather than keep filling the storage.
                    total_written += overspend.total_written
                    for created_id in created_ids:
                        cleanup_asset(created_id)
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"Selected zip entries expand beyond {total_written} bytes; "
                            f"the limit is {IMPORT_ZIP_MAX_BYTES} bytes (IMPORT_ZIP_MAX_MB)"
                        ),
                    ) from overspend
                total_written += size
                if (mime or "").startswith("image/") and dest.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
                    save_thumb(asset.id, dest)
                elif dest.suffix.lower() == ".3mf":
                    ensure_model_thumbnail(asset.id, dest)
                refreshed = finalize_asset_record(asset.id, size, mime)
                assets.append(refreshed or asset)
            except HTTPException as exc:
                # A name conflict or an entry with no extension is one bad entry;
                # every other HTTPException (a bad storage template, say) applies
                # to the whole request. The check is by exception class, not by
                # status: a storage-template failure is also a 400, and the 413
                # overspend raise above lives inside this same `try`.
                if asset is not None:
                    cleanup_asset(asset.id)
                if exc.status_code == 409 or isinstance(exc, AssetFilenameError):
                    failed.append(entry_name)
                else:
                    raise
            except Exception:
                if asset is not None:
                    cleanup_asset(asset.id)
                failed.append(entry_name)
    return assets, failed


def dedupe_arcnames(entries: List[tuple[str, Path]]) -> List[tuple[str, Path]]:
    """Make every archive member name unique, keeping the first occurrence.

    Two assets can land on the same arcname (same folder label, same file name),
    and so can a primary file and a supporting file; a zip with duplicate member
    names extracts as "which one won". The first entry keeps its name and each
    later collision becomes `name (2).ext`, `name (3).ext`, ... in input order,
    so the archive is deterministic for a given selection.
    """
    used: set[str] = set()
    result: List[tuple[str, Path]] = []
    for arcname, path in entries:
        candidate = arcname
        stem, extension = _split_arcname(arcname)
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{stem} ({suffix}){extension}"
            suffix += 1
        used.add(candidate.casefold())
        result.append((candidate, path))
    return result


def _split_arcname(arcname: str) -> tuple[str, str]:
    """`a/b/c.stl` -> (`a/b/c`, `.stl`); a dotless name keeps no extension."""
    head, _, tail = arcname.rpartition("/")
    stem, dot, extension = tail.rpartition(".")
    if not dot or not stem:
        return arcname, ""
    return (f"{head}/{stem}" if head else stem), f".{extension}"


def arcname_for_asset(asset: Asset, folder_label_map: Optional[dict] = None) -> str:
    folder_part = "unassigned"
    if asset.folder_id:
        folder_part = (folder_label_map or {}).get(asset.folder_id, asset.folder_id)
    # folder labels are user input: an unsanitised "../.." here becomes a
    # traversal entry for whoever extracts the downloaded zip
    return f"{sanitize_path_segment(folder_part, 'unassigned')}/{sanitize_path_segment(asset.filename, 'file')}"


_ORIGIN_LABELS = {"imported": "imported", "own": "own creation"}


def _manifest_value(raw: Optional[str], fallback: str) -> str:
    """One line of the manifest: trimmed, newline-free, never empty."""
    text = " ".join((raw or "").split())
    return text or fallback


def attribution_manifest(
    project_name: str,
    creator: Optional[str],
    source_url: Optional[str],
    licence: Optional[str],
    licence_url: Optional[str],
    origin: str,
) -> str:
    """The `ATTRIBUTION.txt` body for a project download.

    The content is deterministic (no timestamps, no environment-derived values),
    so tests can assert the exact text; the archive's zip metadata is not
    byte-stable, exactly like its member entries.
    """
    # Project fields are free text from `PATCH /project/{id}`: a value like
    # "CC0\nSource: https://evil.example" must never forge an extra manifest line,
    # which is what `_manifest_value`'s whitespace collapse prevents.
    name = _manifest_value(project_name, "untitled project")
    creator_value = _manifest_value(creator, "not provided")
    source_value = _manifest_value(source_url, "not provided")
    licence_value = _manifest_value(licence, "unknown")
    licence_url_value = _manifest_value(licence_url, "not provided")
    origin_value = _manifest_value(origin, "unknown")
    origin_label = _ORIGIN_LABELS.get(origin_value, origin_value)
    return (
        "Curiarium attribution\n"
        "\n"
        f"Project: {name}\n"
        f"Creator: {creator_value}\n"
        f"Source: {source_value}\n"
        f"Licence: {licence_value}\n"
        f"Licence URL: {licence_url_value}\n"
        f"Origin: {origin_label}\n"
        "\n"
        "The files in this archive come from the project named above. Their terms of\n"
        "use are described by the licence listed here; when the licence is unknown,\n"
        "assume no permission to redistribute and contact the creator first.\n"
    )


def zip_assets_response(
    assets: List[Asset],
    download_name: str,
    background: BackgroundTasks,
    folder_label_map: Optional[dict] = None,
    allow_empty: bool = False,
    project: Optional[Project] = None,
):
    """Stream the resolved assets as a zip; `allow_empty` is the project contract.

    A tag/folder/asset_ids selection that resolves to nothing is a 404 (the caller
    asked for files that are not there), while an existing project with no members
    is not a 404: the project exists, its file set is simply empty.

    A project request always carries a root-level `ATTRIBUTION.txt`, written as the
    first entry, so an existing project with no resolvable files yields an archive
    holding only the manifest rather than a literally empty archive.
    """
    files: List[tuple[str, Path]] = []
    supporting: dict[str, List[AssetFile]] = {}
    asset_ids = [asset.id for asset in assets]
    if asset_ids:
        with Session(engine) as session:
            for row in session.exec(
                select(AssetFile)
                .where(AssetFile.asset_id.in_(asset_ids), AssetFile.role == "supporting")
                .order_by(AssetFile.position, AssetFile.filename, AssetFile.id)
            ):
                supporting.setdefault(row.asset_id, []).append(row)
    for a in assets:
        path = resolve_asset_file(a)
        if not path:
            continue
        files.append((arcname_for_asset(a, folder_label_map), path))
        folder_part = "unassigned"
        if a.folder_id:
            folder_part = (folder_label_map or {}).get(a.folder_id, a.folder_id)
        for related in supporting.get(a.id, []):
            related_path = managed_asset_file_path(related)
            if related_path.exists():
                files.append((
                    f"{sanitize_path_segment(folder_part, 'unassigned')}/"
                    f"{sanitize_path_segment(a.name, 'model')}/supporting/"
                    f"{sanitize_path_segment(related.filename, 'file')}",
                    related_path,
                ))
    if not files and not allow_empty:
        raise HTTPException(status_code=404, detail="No files available for download")
    unique_files = dedupe_arcnames(files)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp_path = Path(tmp.name)
    tmp.close()
    with ZipFile(tmp_path, "w", ZIP_DEFLATED) as zf:
        if project is not None:
            zf.writestr(
                ATTRIBUTION_FILENAME,
                attribution_manifest(
                    project.name,
                    project.creator,
                    project.source_url,
                    project.licence,
                    project.licence_url,
                    project.origin,
                ),
            )
        for arc, fs_path in unique_files:
            zf.write(fs_path, arcname=arc)

    background.add_task(lambda p=tmp_path: p.unlink(missing_ok=True))
    return FileResponse(tmp_path, media_type="application/zip", filename=download_name)
