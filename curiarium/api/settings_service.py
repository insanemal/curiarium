import logging
from typing import Optional

from sqlmodel import Session

from db import engine

# `mfa` imports `auth` only, and `auth` imports neither this module nor `db`, so
# the key constant can live with the TOTP code it names without a cycle.
from mfa import MFA_REQUIRE_ADMIN_KEY
from models import AppConfig

log = logging.getLogger(__name__)

MOUNT_IMPORT_ENABLED_KEY = "mount_import_enabled"
MOUNT_IMPORT_COPY_KEY = "mount_import_copy"
STORAGE_TEMPLATE_KEY = "storage_path_template"

# A file lands directly in the directory of the folder it belongs to, so the
# library's own directory tree is the layout and no second level is invented
# beneath it. This affects NEW WRITES ONLY: an existing file keeps the path it
# was written at, and re-arranging an existing tree is the approval-gated job of
# Mf-v9eg - never a silent migration. `{model}` stays a valid token: an install
# that deliberately saves the upstream one-directory-per-model layout
# (`RETIRED_DEFAULT_STORAGE_TEMPLATE` below) keeps rendering it. No schema
# change, no migration.
DEFAULT_STORAGE_TEMPLATE = "{folder}/{filename}"
# The default this key shipped with. A stored value equal to it is read as
# "unset" so an install that never chose a layout follows the new default
# instead of being pinned to the old one by the value `POST /settings/storage`
# once wrote while it was still the default. The marker below is what tells that
# pre-upgrade row apart from a retired default an operator picked on purpose.
RETIRED_DEFAULT_STORAGE_TEMPLATE = "{folder}/{model}/{filename}"
# Written only by `set_storage_template`, which is what `POST /settings/storage`
# calls, so "a value exists" plus "the marker is absent" means "stored before
# this change" rather than "chosen". No schema change, no migration.
STORAGE_TEMPLATE_CHOSEN_KEY = "storage_path_template_chosen"
# The retired-default read is logged once per process: it explains a layout
# change to anyone reading the log, and a per-request line would be noise.
_retired_default_logged = False
# One key holds the whole dismissal map: a bundle key is a rel path, so a row per
# bundle would be a working set we never read one row at a time (Mf-vwqf).
BUNDLE_DISMISSALS_KEY = "bundle.dismissals"
# One key holds the whole enrich map, keyed by `thingiverse:<id>`: a cached page
# is a working set read whole, exactly like the dismissals above (Mf-nivh).
BUNDLE_ENRICHMENTS_KEY = "bundle.enrichments"


def get_setting(key: str) -> Optional[str]:
    with Session(engine) as s:
        row = s.get(AppConfig, key)
        return row.value if row else None


def _apply_setting(session: Session, key: str, value: str) -> None:
    """Upsert one setting on the caller's session; the caller commits."""
    row = session.get(AppConfig, key)
    if row:
        row.value = value
    else:
        row = AppConfig(key=key, value=value)
        session.add(row)


def set_setting(key: str, value: str, *, session: Optional[Session] = None) -> None:
    """Store one setting.

    With `session` given, the write joins the caller's transaction so a settings
    change and its audit row commit together; without it, a session is opened
    and committed here, which is the behaviour every non-request caller uses.
    """
    if session is not None:
        _apply_setting(session, key, value)
        return
    with Session(engine) as own:
        _apply_setting(own, key, value)
        own.commit()


def get_bool_setting(key: str, default: bool) -> bool:
    raw = get_setting(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def set_bool_setting(key: str, value: bool, *, session: Optional[Session] = None) -> None:
    set_setting(key, "1" if value else "0", session=session)


def get_mount_import_enabled(default: bool) -> bool:
    return get_bool_setting(MOUNT_IMPORT_ENABLED_KEY, default)


def set_mount_import_enabled(value: bool, *, session: Optional[Session] = None) -> None:
    set_bool_setting(MOUNT_IMPORT_ENABLED_KEY, value, session=session)


def get_mount_import_copy(default: bool) -> bool:
    return get_bool_setting(MOUNT_IMPORT_COPY_KEY, default)


def set_mount_import_copy(value: bool, *, session: Optional[Session] = None) -> None:
    set_bool_setting(MOUNT_IMPORT_COPY_KEY, value, session=session)


def get_storage_template() -> str:
    """The layout new files are written in.

    A stored retired default (``RETIRED_DEFAULT_STORAGE_TEMPLATE``) is read as
    unset when ``STORAGE_TEMPLATE_CHOSEN_KEY`` is absent: that is the row an
    install which never chose a layout carries, written before this change. A
    retired default saved after this change carries the marker, is therefore a
    choice, and wins - an operator who deliberately wants the upstream
    one-directory-per-model layout simply saves ``{folder}/{model}/{filename}``.

    This affects NEW WRITES ONLY. An existing file keeps the path it already
    has: nothing on disk moves, and the returned template is never applied
    backwards to the library. Re-arranging an existing tree is the
    approval-gated job of Mf-v9eg - never silent, never a migration. No schema
    change, no migration.
    """
    global _retired_default_logged

    stored = get_setting(STORAGE_TEMPLATE_KEY)
    if not stored:
        return DEFAULT_STORAGE_TEMPLATE
    if stored.strip() == RETIRED_DEFAULT_STORAGE_TEMPLATE:
        # Only this branch pays the second read: the common path is one row.
        if get_setting(STORAGE_TEMPLATE_CHOSEN_KEY) != "1":
            if not _retired_default_logged:
                _retired_default_logged = True
                log.info(
                    "storage template: the retired default %r is being read as unset, so new writes use %r; "
                    "nothing on disk moves, and re-arranging an existing tree is the approval-gated job of Mf-v9eg",
                    RETIRED_DEFAULT_STORAGE_TEMPLATE,
                    DEFAULT_STORAGE_TEMPLATE,
                )
            return DEFAULT_STORAGE_TEMPLATE
    return stored


def set_storage_template(value: str, *, session: Optional[Session] = None) -> None:
    """Store a template, plus the marker that says an operator chose it.

    Storing a template is an explicit choice, so a retired spelling picked on
    purpose must keep working; the marker is what tells that apart from a value
    `POST /settings/storage` wrote back when it was still the default. Both rows
    are written together: on the caller's session when one is given (the request
    commits once, with its audit row), otherwise in one session committed here.
    """
    if session is not None:
        _apply_setting(session, STORAGE_TEMPLATE_KEY, value)
        _apply_setting(session, STORAGE_TEMPLATE_CHOSEN_KEY, "1")
        return
    with Session(engine) as own:
        _apply_setting(own, STORAGE_TEMPLATE_KEY, value)
        _apply_setting(own, STORAGE_TEMPLATE_CHOSEN_KEY, "1")
        own.commit()


def get_mfa_require_admin(default: bool = False) -> bool:
    """Whether admins must prove a second factor; owned here so the key has one home."""
    return get_bool_setting(MFA_REQUIRE_ADMIN_KEY, default)


def set_mfa_require_admin(value: bool, *, session: Optional[Session] = None) -> None:
    set_bool_setting(MFA_REQUIRE_ADMIN_KEY, value, session=session)
