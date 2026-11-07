"""Web imports, zip inspection/entry selection and project imports (Mf-avo9 split)."""

from typing import List, Literal, Optional

from pydantic import BaseModel

from .assets import AssetOut
from .projects import ProjectOut

__all__ = [
  "ArchiveEntriesOut",
  "ArchiveExtractOut",
  "ArchiveExtractRequest",
  "ArchiveProjectOut",
  "ArchiveProjectRequest",
  "ImportInspectOut",
  "ImportProjectOut",
  "ImportProjectRequest",
  "ImportRequest",
  "ImportZipEntriesOut",
  "ImportZipEntriesRequest",
  "ImportZipExtractRequest",
  "ImportZipResult",
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
  and both server extract doors refuse it: `/import/zip` reports it in `failed`,
  `/import/archive` leaves it out of the extraction. The server decides it; the
  browser only uses it to choose the modal's default ticks.
  """

  path: str
  size: int
  junk: bool


class ArchiveEntriesOut(BaseModel):
  """One archive the library already holds, as the entries door lists it.

  `path` echoes the request's library-relative path (normalised), so a client can
  show what it read without re-deriving it.
  """

  path: str
  entries: List[ZipEntryOut]


class ArchiveExtractRequest(BaseModel):
  """An archive already in the library; `entries=None` means every member."""

  path: str
  entries: Optional[List[str]] = None


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


class ArchiveProjectRequest(BaseModel):
  """The archive whose extraction the operator declared to be one project.

  `entries=None` means every member the archive holds, exactly the selection
  `POST /import/archive` takes when it is given `None`; a list is the subset the
  operator extracted. The names are checked against the archive, so a pick the
  archive does not hold is a 400 rather than a silently missing member.
  """

  path: str
  name: Optional[str] = None
  entries: Optional[List[str]] = None


class ArchiveProjectOut(BaseModel):
  """The verdict of the extract-and-create door, or the project it made.

  Every verdict is a 200: this door has nothing to refuse on policy grounds, so
  `status` is the whole verdict and a client never has to guess from a status
  code. `partial` and `nothing_resolved` create no project and carry no `project`;
  `planned`/`resolved` are the two set sizes the comparison was made over and
  `failed` names every planned entry that never became a registered asset, so
  "how many entries failed" is a fact from the server rather than an inference.
  `twin` is library-relative and is named so the operator can go and look at
  where the files live.
  """

  status: Literal["created", "already_created", "partial", "nothing_resolved"]
  archive: str
  twin: str
  planned: int
  resolved: int
  failed: List[str] = []
  project: Optional[ProjectOut] = None


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


class ImportZipResult(BaseModel):
  assets: List[AssetOut]
  failed: List[str] = []


class ImportProjectOut(BaseModel):
  """An import into a project: the project, what changed and why it may be empty.

  `assets` are the assets the import itself produced (already serialized), and
  `added`/`skipped` are their member ids. `failed` lists zip entry paths that
  could not be extracted, never member-id failures. `gated` is true when the
  site refused the files and only the page details were stored; `message` then
  explains how to attach them.
  """

  project: ProjectOut
  created: bool
  assets: List[AssetOut] = []
  added: List[str] = []
  skipped: List[str] = []
  failed: List[str] = []
  gated: bool = False
  message: str = ""
