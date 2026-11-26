FROM python:3.12-slim AS base
WORKDIR /app

# git is a runtime feature here, not a convenience (Mf-ky16): the proposal
# engine asks a checkout what it tracks (`proposal_engine.git_state`), and in
# the shipped image that probe returned `None` for a real checkout, so the
# engine fell back to a structural guess and answered "split into 8 projects"
# for `import/endorphin`, which is ONE project. Splitting a project is the
# failure mode the whole bundle-detection effort exists to prevent.
#
# It goes in `base` and not in `runtime` because `runtime` is a SIBLING of the
# test stages (`FROM base AS runtime` beside `FROM lint AS test`) and `make
# verify-api` builds `--target test`, so a git installed only in `runtime`
# could never make the in-image capability assertion (`tests/test_git_image.py`)
# pass -- the test would keep skipping, which is the exact trap this ticket is
# about. `gosu` rides the same line because it was the only other apt package
# and one apt layer is one cache entry.
#
# Measured cost (Mf-ky16): +98.07 MB plain, +95.84 MB with INSTALL_ZFS=1 (runtime target).
RUN apt-get update \
  && apt-get install -y --no-install-recommends gosu git \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

FROM base AS testdeps
COPY requirements-dev.txt requirements-dev.lock ./
RUN pip install --no-cache-dir -r requirements-dev.lock

FROM testdeps AS lint
COPY . .
RUN ruff check .

FROM lint AS test
CMD ["python", "-m", "pytest", "-q"]

FROM base AS runtime

# The published image is deliberately slim, so the ZFS userspace is opt-in
# (Mf-qirh): INSTALL_ZFS=1 builds the `-zfs` variant the ZFS snapshot provider
# needs, and the default stays the plain image every other deployment runs.
ARG INSTALL_ZFS=0

# `zfsutils-linux` lives in Debian's `contrib`, which the slim base does not
# enable, so a plain install answers "has no installation candidate". Both apt
# source layouts are handled -- the deb822 `debian.sources` of newer bases and
# the one-line `sources.list` of older ones -- and an already-enabled `contrib`
# is left alone, so the step is safe to re-run.
RUN if [ "$INSTALL_ZFS" = "1" ]; then \
      set -eu; \
      if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sources=/etc/apt/sources.list.d/debian.sources; \
        sed -i -E '/^Components:.*[[:space:]]contrib([[:space:]]|$)/!s/^Components:(.*)$/Components:\1 contrib/' "$sources"; \
      elif [ -f /etc/apt/sources.list ]; then \
        sources=/etc/apt/sources.list; \
        sed -i -E '/[[:space:]]contrib([[:space:]]|$)/!s/^(deb[[:space:]].*[[:space:]])main([[:space:]]|$)/\1main contrib\2/' "$sources"; \
      else \
        echo "INSTALL_ZFS=1: no Debian apt sources to enable contrib in" >&2; \
        exit 1; \
      fi; \
      if ! grep -qE '(^|[[:space:]])contrib([[:space:]]|$)' "$sources"; then \
        echo "INSTALL_ZFS=1: could not enable the contrib component in $sources" >&2; \
        exit 1; \
      fi; \
      apt-get update; \
      apt-get install -y --no-install-recommends zfsutils-linux; \
      rm -rf /var/lib/apt/lists/*; \
    fi

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

COPY . .
RUN mkdir -p /app/storage /app/data

EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
# uvicorn's access log records the full request line, query string included, and
# that now carries a short-lived signed file grant (M3-7bb7).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
