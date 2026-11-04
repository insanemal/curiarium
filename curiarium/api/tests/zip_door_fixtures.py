"""The one archive both import doors are measured against, plus the two seams a test needs.

`DOOR_ARCHIVE` is Mf-znu1's exact reported shape: a payload at two depths and the
five pieces of OS metadata a macOS or Windows archiver adds on its own, including
payload-named files *inside* the junk directories. That last part is the point:
`library_scan._walk_storage` already prunes dot-names, so an archive whose junk is
all dot-files cannot prove that the archive door wrote junk - only a payload-named
file under `__MACOSX/` or `$RECYCLE.BIN/` shows up in a scan as an asset.
"""

from pathlib import Path
from typing import Dict, Optional
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import main

DOOR_PAYLOAD = {"part.stl", "a/b/nested.stl"}
DOOR_JUNK = {
    "__MACOSX/extra.stl",
    "__MACOSX/._part.stl",
    "$RECYCLE.BIN/S-1-5-21-1/trashed.stl",
    ".DS_Store",
    "Thumbs.db",
}
DOOR_ARCHIVE: Dict[str, bytes] = {
    "part.stl": bytes(range(32, 37)),                              # 5
    "a/b/nested.stl": bytes(range(32, 37)),                        # 5
    "__MACOSX/extra.stl": bytes(range(32, 52)),                    # 20
    "__MACOSX/._part.stl": bytes(range(32, 40)),                   # 8
    "$RECYCLE.BIN/S-1-5-21-1/trashed.stl": bytes(range(32, 52)),    # 20
    ".DS_Store": bytes(range(32, 38)),                             # 6
    "Thumbs.db": bytes(range(32, 38)),                             # 6
}
# The payload declares 10 bytes and the junk declares 60: the sizes the two
# budget symptoms are measured against.
DOOR_PAYLOAD_BYTES = 10
DOOR_JUNK_BYTES = 60


def write_door_archive(path: Path, entries: Optional[Dict[str, bytes]] = None) -> Path:
    """Write `entries` (default DOOR_ARCHIVE) to `path` with ZIP_DEFLATED."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, payload in (DOOR_ARCHIVE if entries is None else entries).items():
            archive.writestr(name, payload)
    return path


def install_zip_download(monkeypatch: pytest.MonkeyPatch, archive: Path, entries: Optional[Dict[str, bytes]] = None) -> None:
    """Serve a fresh copy of the archive to every `/import/zip` route call.

    The import route unlinks its temp file in its own `finally`, so a single
    pre-written path would be gone for a second request in the same test; the
    fake rewrites it instead (moved here from `test_zip_junk.py`).
    """
    monkeypatch.setattr(main, "normalize_import_url", lambda url: url)
    monkeypatch.setattr(
        main,
        "download_import_to_temp",
        lambda url, body: (write_door_archive(archive, entries), "models.zip", "application/zip"),
    )


def files_under(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
