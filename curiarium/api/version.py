"""The product version. This file is the single source of truth.

It is mirrored into curiarium/web/package.json and package-lock.json and
the copies are compared by curiarium/scripts/check_version.py (Mf-cvoo).
"""

import os

__version__ = "5.2.0-rc.1"

# Which commit an image was built from. Floating tags (`edge`) all report the
# same __version__, so the publish workflow passes the commit sha (cut to 7 characters here) as the
# BUILD_SHA build arg. A plain `docker build` or a dev checkout reports "dev".
BUILD = os.environ.get("BUILD_SHA", "").strip()[:7] or "dev"
