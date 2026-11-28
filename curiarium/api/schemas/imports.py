"""Web imports: the two read-only preflights and the models the workflows use.

No route writes an import any more (Mf-y2u3 parts 2/4 and 3/4): every import is
an `import-link`, `import-zip` or `extract` operation. What a route still answers
is `POST /import/inspect` (a URL's filename, mime and whether it is a zip) and
`POST /import/zip/entries` (that zip's listing); both write nothing. The request
models and `ArchiveExtractOut` are what the operation step bodies build.
"""

from typing import List, Optional

from pydantic import BaseModel

__all__ = [
  "ArchiveExtractOut",
  "ImportInspectOut",
  "ImportProjectRequest",
  "ImportRequest",
  "ImportZipEntriesOut",
  "ImportZipEntriesRequest",
  "ImportZipExtractRequest",
  "ZipEntryOut",
]


class ImportRequest(BaseModel):
  url: str
  title: Optional[str] = None
  notes: Optional[str] = None
  tags: List[str] = []
  folder_id: Optional[str] = None
  filename: Optional[str] = None
  makerworld_cookie: Optional[str] = None
  thingiverse_cookie: Optional[str] = None


class ImportInspectOut(BaseModel):
  filename: str
  mime: str
  is_zip: bool


class ZipEntryOut(BaseModel):
  """One zip member as an entries listing reports it.

  `junk` is the server's verdict (`entry_policy.is_junk_entry`) that the member
  is OS metadata - `__MACOSX/`, `.DS_Store`, `Thumbs.db`, an AppleDouble `._*` -
  and both extract workflows refuse it: `import-zip` reports it in `failed`, and
  `extract` names it in the plan's `excluded`. The server decides it; the browser
  only uses it to choose the modal's default ticks.
  """

  path: str
  size: int
  junk: bool


class ArchiveExtractOut(BaseModel):
  """What an in-library extract did, or that it had nothing left to do.

  Two outcomes write nothing, and `(files, already_present)` tells them apart.
  `already_present=True` is the idempotent one: every destination already
  existed. `already_present=False` with `files=0` is the junk-only one: the
  selection had nothing writable, every member being OS metadata or
  directory-like. Neither wrote a file, started a scan or wrote an audit row.
  `destination` is library-relative, the twin directory beside the zip.
  """

  path: str
  destination: str
  files: int
  bytes: int
  already_present: bool
  scan_started: bool


class ImportZipEntriesRequest(ImportRequest):
  pass


class ImportZipEntriesOut(BaseModel):
  filename: str
  entries: List[ZipEntryOut]


class ImportZipExtractRequest(ImportRequest):
  entries: List[str]


class ImportProjectRequest(ImportRequest):
  """A website import that lands in a project (Mf-56z9).

  `entries=None` means "do not unzip": the whole file (or a whole zip) becomes
  one asset. A list - even an empty one - means "extract these entries", exactly
  like `/import/zip`. `project_id` targets an existing project; without it the
  import finds the project for this model page or creates one.
  """

  project_id: Optional[str] = None
  project_name: Optional[str] = None
  entries: Optional[List[str]] = None

