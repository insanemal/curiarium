# Cosmos Cloud Installation Guide

Not yet validated against a named Cosmos Cloud release: the template, the env table and the route are
version-independent, but the two Cosmos behaviours this guide depends on most — the `role` claim and the map-form
`environment` — are **unverified** and labelled as such. The claims caveat below applies to every Cosmos version,
which is why it is stated as a mechanism rather than a version note.

The template this guide walks through is [`curiarium/deploy/cosmos/compose.json`](../../deploy/cosmos/compose.json).
The contract it satisfies — placeholders only, the env table, the two persistent volumes, the one routed port, a
verification that ends in something observable — is [`curiarium/deploy/README.md`](../../deploy/README.md).
TrueNAS is the sibling example, and reads the same way: [truenas.md](truenas.md).

## Prerequisites

- A Cosmos Cloud server, and the ability to create a Docker Compose application on it.
- Two directories on the host for the two persistent volumes, on storage that survives a container replacement
  and is included in your backup.
- Shell access to the host, with `sudo`, to create those directories and check their ownership.
- A route from the machines that will use Curiarium to the web container's port `5173`.
- Optional: an OIDC client registered in Cosmos, if you want SSO. Step 5 covers it; the local password login
  works without it.

## 1. Create the two directories

`/app/storage` and `/app/data` inside the containers are bind mounts of two directories on the host. Create them
before the application starts, owned by the UID and GID you will put in `PUID` and `PGID`:

```console
sudo mkdir -p <STORAGE_DIR> <DATA_DIR>
sudo chown -R <PUID>:<PGID> <STORAGE_DIR> <DATA_DIR>
ls -ld <STORAGE_DIR> <DATA_DIR>
```

`ls` should show your UID and GID owning both. The API runs as `PUID:PGID` and its entrypoint re-applies
ownership of those two directories on start (the default `CHOWN_MODE=minimal`), so this is the starting state —
but an account that cannot write its own library root will fail on the first upload, and one that cannot rename a
directory cannot move it to the trash, so its delete answers 409.

Both directories are required. `/app/storage` holds the library; `/app/data` holds the SQLite database. Back them
up together: a restore of one without the other leaves a database describing files that are not there, or files
the database has never heard of.

## 2. Create the application in Cosmos Cloud and paste the template

In Cosmos Cloud, open the server's applications view, create a new Docker Compose application, name it
`curiarium`, and paste the contents of `compose.json` into the compose editor. Save, but do not start it yet.

The template declares exactly two services, `api` and `web`, and nothing else. The service names are load-bearing:
the web container's nginx proxies `/api/` and `/auth/` to the host `api` on port `8000`, so renaming `api`
answers every API call with 502.

The `<…>` values are substituted as the template is pasted in; strict JSON forbids comments, so the
substitutions are documented here. The
placeholder classes are: the two host directories (`<STORAGE_DIR>`, `<DATA_DIR>`) in the `volumes` list; the
ownership pair (`<PUID>`, `<PGID>`, `<CHOWN_MODE>`); the origin (`<PUBLIC_URL>`); the credential set
(`<AUTH_USERNAME>`, `<AUTH_PASSWORD>`, `<AUTH_SECRET>`, `<AUTH_MODE>`, `<AUTH_ACCESS_TOKEN_TTL>`,
`<AUTH_REFRESH_TTL_SECONDS>`, `<AUTH_FIRST_RUN_SETUP>`, `<AUTH_ALLOW_INSECURE_DEFAULTS>`); the caps and throttle
counters; the SSO set (`<OIDC_*>`); the header switch (`<SECURITY_HEADERS_ENABLED>`); and the snapshot policies.
`FILE_STORAGE`, `DB_URL`, `API_PORT` and the image references are literals and must stay as they are.

```json
{
  "services": {
    "api": {
      "image": "ghcr.io/insanemal/curiarium-api:edge",
      "restart": "unless-stopped",
      "environment": {
        "PUID": "<PUID>",
        "PGID": "<PGID>",
        "CHOWN_MODE": "<CHOWN_MODE>",
        "FILE_STORAGE": "/app/storage",
        "DB_URL": "sqlite:////app/data/curiarium.db",
        "API_PORT": "8000",
        "PUBLIC_URL": "<PUBLIC_URL>",
        "CORS_ORIGINS": "",
        "VITE_API_URL": "",
        "VITE_ALLOWED_HOSTS": "",
        "AUTH_USERNAME": "<AUTH_USERNAME>",
        "AUTH_PASSWORD": "<AUTH_PASSWORD>",
        "AUTH_SECRET": "<AUTH_SECRET>",
        "AUTH_MODE": "<AUTH_MODE>",
        "AUTH_ACCESS_TOKEN_TTL": "<AUTH_ACCESS_TOKEN_TTL>",
        "AUTH_REFRESH_TTL_SECONDS": "<AUTH_REFRESH_TTL_SECONDS>",
        "AUTH_FIRST_RUN_SETUP": "<AUTH_FIRST_RUN_SETUP>",
        "AUTH_ALLOW_INSECURE_DEFAULTS": "<AUTH_ALLOW_INSECURE_DEFAULTS>",
        "MAX_UPLOAD_MB": "<MAX_UPLOAD_MB>",
        "IMPORT_MAX_MB": "<IMPORT_MAX_MB>",
        "IMPORT_ZIP_MAX_MB": "<IMPORT_ZIP_MAX_MB>",
        "IMPORT_TIMEOUT_SECONDS": "<IMPORT_TIMEOUT_SECONDS>",
        "IMPORT_MAX_REDIRECTS": "<IMPORT_MAX_REDIRECTS>",
        "IMPORT_HTML_MAX_KB": "<IMPORT_HTML_MAX_KB>",
        "ASSET_FILE_MAX_MB": "<ASSET_FILE_MAX_MB>",
        "ASSET_FILE_TOTAL_MAX_MB": "<ASSET_FILE_TOTAL_MAX_MB>",
        "ASSET_FILE_MAX_COUNT": "<ASSET_FILE_MAX_COUNT>",
        "MAKERWORLD_COOKIE": "",
        "THINGIVERSE_COOKIE": "",
        "LOGIN_THROTTLE_ENABLED": "<LOGIN_THROTTLE_ENABLED>",
        "LOGIN_MAX_FAILURES": "<LOGIN_MAX_FAILURES>",
        "LOGIN_FAILURE_WINDOW_SECONDS": "<LOGIN_FAILURE_WINDOW_SECONDS>",
        "LOGIN_LOCKOUT_SECONDS": "<LOGIN_LOCKOUT_SECONDS>",
        "LOGIN_MAX_LOCKOUT_SECONDS": "<LOGIN_MAX_LOCKOUT_SECONDS>",
        "OIDC_ENABLED": "<OIDC_ENABLED>",
        "OIDC_ISSUER": "<OIDC_ISSUER>",
        "OIDC_CLIENT_ID": "<OIDC_CLIENT_ID>",
        "OIDC_CLIENT_SECRET": "<OIDC_CLIENT_SECRET>",
        "OIDC_SCOPES": "<OIDC_SCOPES>",
        "OIDC_GROUPS_CLAIM": "<OIDC_GROUPS_CLAIM>",
        "OIDC_ALLOWED_GROUPS": "",
        "OIDC_ALLOWED_DOMAINS": "",
        "OIDC_EDITOR_GROUPS": "",
        "OIDC_ADMIN_GROUPS": "",
        "OIDC_DEFAULT_ROLE": "<OIDC_DEFAULT_ROLE>",
        "SECURITY_HEADERS_ENABLED": "<SECURITY_HEADERS_ENABLED>",
        "SECURITY_HEADERS_HSTS": "",
        "SECURITY_HEADERS_CSP": "",
        "SECURITY_HEADERS_REFERRER_POLICY": "",
        "SECURITY_HEADERS_FRAME_OPTIONS": "",
        "SECURITY_HEADERS_PERMISSIONS_POLICY": "",
        "SECURITY_HEADERS_CONTENT_TYPE_OPTIONS": "",
        "SNAPSHOT_POLICY_PREDELETE": "<SNAPSHOT_POLICY_PREDELETE>",
        "SNAPSHOT_POLICY_PREPURGE": "<SNAPSHOT_POLICY_PREPURGE>",
        "SNAPSHOT_POLICY_PREBULK": "<SNAPSHOT_POLICY_PREBULK>",
        "SNAPSHOT_REQUIRE_CAPABILITY": "<SNAPSHOT_REQUIRE_CAPABILITY>",
        "SNAPSHOT_SCHEDULE_ENABLED": "<SNAPSHOT_SCHEDULE_ENABLED>",
        "SNAPSHOT_SCHEDULE_HOURS": "<SNAPSHOT_SCHEDULE_HOURS>",
        "SNAPSHOT_SCHEDULE_CHECK_SECONDS": "<SNAPSHOT_SCHEDULE_CHECK_SECONDS>",
        "SNAPSHOT_SCHEDULE_START_DELAY_SECONDS": "<SNAPSHOT_SCHEDULE_START_DELAY_SECONDS>",
        "SNAPSHOT_BTRFS_SIDECAR_URL": "",
        "SNAPSHOT_BTRFS_SIDECAR_TOKEN": ""
      },
      "volumes": [
        "<STORAGE_DIR>:/app/storage",
        "<DATA_DIR>:/app/data"
      ]
    },
    "web": {
      "image": "ghcr.io/insanemal/curiarium-web:edge",
      "restart": "unless-stopped",
      "depends_on": [
        "api"
      ],
      "environment": {
        "PUBLIC_URL": "<PUBLIC_URL>",
        "VITE_API_URL": "",
        "VITE_ALLOWED_HOSTS": "",
        "SECURITY_HEADERS_ENABLED": "<SECURITY_HEADERS_ENABLED>",
        "SECURITY_HEADERS_HSTS": "",
        "SECURITY_HEADERS_CSP": "",
        "SECURITY_HEADERS_REFERRER_POLICY": "",
        "SECURITY_HEADERS_FRAME_OPTIONS": "",
        "SECURITY_HEADERS_PERMISSIONS_POLICY": "",
        "SECURITY_HEADERS_CONTENT_TYPE_OPTIONS": ""
      }
    }
  }
}
```

**Unverified:** whether Cosmos accepts a map-form `environment` object and the short `host:container` bind
syntax without complaint. Both are standard compose, and the template uses them because a map keeps the
name/value pairs readable and reviewable; if the Cosmos editor rejects either, convert `environment` to a list of
`KEY=value` strings and the volumes to the long form, and change the template and this guide together so the two
do not drift.

## 3. Fill the env placeholders

Every value in the template is a JSON string: `"1"`, `"false"`, `"<PUBLIC_URL>"` and `""` are quoted, and a bare
`true` or a bare `8000` is not valid here. Delete a row to take the image's built-in default — the shared table in
[`../../deploy/README.md`](../../deploy/README.md) gives the default and the meaning for every key.

The rows that decide whether the deployment works at all:

| Placeholder | Set it to |
|---|---|
| `<PUID>`, `<PGID>` | The UID and GID that own `<STORAGE_DIR>` and `<DATA_DIR>`, from step 1. |
| `<CHOWN_MODE>` | `minimal`. Use `recursive` only for a tree another account wrote. |
| `<STORAGE_DIR>`, `<DATA_DIR>` (in `volumes`) | The two host directories from step 1, as absolute paths. |
| `<PUBLIC_URL>` | The origin you browse to: scheme and host, no path, no trailing slash, `https://curiarium.example.com`-style. The web container derives its API base URL from it, and the OIDC redirect URI is derived from it. |
| `<AUTH_SECRET>` | A fresh 32-byte hex string, `openssl rand -hex 32`-generated. Never a phrase, never reused from another deployment. |
| `<AUTH_MODE>` | `local` for password login only (SSO refused), `both` when SSO is on and local login must survive, `oidc` to refuse local passwords. `auto`, the default, is the legacy rule — auth is on iff the credential pair is set — and it does not refuse SSO: with `OIDC_ENABLED` on, SSO works under `auto`. |
| `<AUTH_USERNAME>`, `<AUTH_PASSWORD>` | A break-glass local account. Delete `AUTH_PASSWORD` once a real administrator exists; the API refuses to boot while it is still the published sample value. |
| `<AUTH_FIRST_RUN_SETUP>` | `1` for the first administrator (step 6), then `0` and restart. |
| `<AUTH_ALLOW_INSECURE_DEFAULTS>` | `0`. |
| `<AUTH_ACCESS_TOKEN_TTL>`, `<AUTH_REFRESH_TTL_SECONDS>` | `900` and `2592000`. |
| `<MAX_UPLOAD_MB>` | The same ceiling as the body-size cap of whatever sits in front. |
| `<LOGIN_THROTTLE_ENABLED>` and the four `LOGIN_*` counters | `true`, `5`, `900`, `300`, `3600`. |
| `<SECURITY_HEADERS_ENABLED>` | `true` unless a proxy in front already sets them. |
| The `SNAPSHOT_*` rows | `warn`, `strict`, `strict`, `false`, `false`, `24`, `300`, `60`. See [snapshot capability](#snapshot-capability-depends-on-the-mount) before turning the schedule on. |
| The `OIDC_*` rows | Step 5. With `OIDC_ENABLED` left `false` the four OIDC routes answer 503 and nothing else changes. |

Leave the `""` rows empty unless you know why you are filling them. `CORS_ORIGINS`, `VITE_API_URL` and
`VITE_ALLOWED_HOSTS` empty mean "same origin", which is what a Cosmos route to `web:5173` gives you.
`OIDC_ADMIN_GROUPS` must stay empty in every configuration — a claim can never mint an admin, and a non-empty
value refuses the boot.

## 4. Set the route to container `web`, port `5173`

Point the Cosmos route (proxy) for the application at the container `web` and its port `5173`. Not `api`, not
`8000`.

The template publishes no ports, and that is deliberate. Everything goes to the web container, which serves the UI
and proxies `/api/` and `/auth/` to `api:8000` inside the compose network. Publishing the API's port instead would
bypass the web container's security headers and its upload size limit, and expose the API surface directly. If a
request reaches port `8000` from outside the compose network, the template is wrong.

`/auth/` is passed through untouched because the OIDC redirect URI lives there; `/api/` is the historical prefix
and is rewritten. This is why the redirect in step 5 is not under `/api/`.

## 5. Configure the OIDC client in Cosmos

Cosmos is both the platform and the identity provider here. Register a client for Curiarium in Cosmos's SSO
settings, then fill the `OIDC_*` rows.

- **Issuer: the bare origin.** Scheme and host of the Cosmos server, no path, no trailing slash — never the
  discovery URL. The app appends the discovery path itself. A path segment produces a discovery fetch that fails,
  and the boot validator only checks the scheme prefix, so the mistake surfaces at the first sign-in rather than at
  start. A trailing slash is stripped before use, but do not rely on that: write the bare origin.
- **Confidential client, with a secret.** Paste the secret the provider issues; do not invent one. A public
  client has no secret, and the boot refuses a half-configuration: issuer, client id and client secret must all be
  set.
- **Redirect URI: register it literally as `<PUBLIC_URL>/auth/oidc/callback`.** Not `/api/auth/...`. It is not a
  setting — the API derives it from `PUBLIC_URL` — so the registered value and `PUBLIC_URL` must match exactly,
  including the trailing-less origin.
- **Scopes:** `openid profile email`.
- **The claim and the allow-lists:** read [trap (a)](#a-cosmos-sends-a-singular-role-and-no-groups) before filling
  these in. In short, `OIDC_GROUPS_CLAIM` is `role`, and the allow/editor lists carry both encodings.
- **`OIDC_DEFAULT_ROLE`:** `viewer`, or `editor` if a first sign-in should be able to change the library. `admin`
  is refused at boot.
- **`AUTH_MODE`:** `both` if password login must survive alongside SSO, `oidc` to refuse it.

At least one allow-list must be populated, or the boot refuses a configuration that would admit every account the
provider will authenticate.

## 6. Create the first administrator

The administrator cannot come from SSO: a claim set can produce at most editor, so the first admin is created
locally, with a password.

1. Set `AUTH_FIRST_RUN_SETUP` to `1`, save, and restart the application.
2. Open `<PUBLIC_URL>` in a browser. The one-time setup is offered; pick a strong password there, because the
   server enforces its own policy.
3. Set `AUTH_FIRST_RUN_SETUP` back to `0`, save, and restart. Delete `AUTH_PASSWORD` from the template at the same
   time if the account is no longer needed as a break-glass login.

While the window is open, anyone who can reach the API can create that administrator: there is no credential to
present, because none exists yet. Finish in one session, on a trusted network. The window closes itself at the
first enabled administrator, so leaving the flag on is not a hole — but the routes stay advertised and the boot
log keeps telling you to unset it. Do not rely on the route gate from step 4 to stand in for this.

## Verify

Step 7, and the only step that ends in something you can read off the screen instead of "it should work". The
generic pattern is in [`../../deploy/README.md`](../../deploy/README.md#verifying-a-deployment); the commands below
are that pattern with this target's addresses.

Both services show as running in the application view, and neither is restarting.

The web container answers. Run this from another machine, not the host:

```bash
curl -sI https://<PUBLIC_URL>/ | head -n 1
```

The first line is `HTTP/1.1 200 OK` (or `HTTP/2 200`). A `502` means the web container is up and its upstream is
not.

The API is reachable **through** the web container:

```bash
curl -sI https://<PUBLIC_URL>/api/storage/capabilities | head -n 1
```

`HTTP/1.1 401 Unauthorized` is the expected answer with a Cosmos route that has authentication enabled, and it is
not a failure — see [trap (b)](#b-the-route-gate-answers-401-to-plain-curl). It proves the path reached the API
rather than the SPA fallback. A `200` carrying an HTML body, or a `404`, means `/api/` is not being proxied at all.

Then, signed in through the UI, ask the same route for its body:

```bash
curl -s -b <COOKIE_JAR> https://<PUBLIC_URL>/api/storage/capabilities | jq '.volumes | length'
```

A number of at least `1`. The response is a JSON object whose `volumes` array has one entry per filesystem: a
single entry carrying both the `library` and `database` roles when `<STORAGE_DIR>` and `<DATA_DIR>` sit on the same
filesystem, or two entries when they do not. Each entry carries `path`, `fstype`, `mount_point`, `snapshot_method`,
`usable` and `reason`.

## Failure modes

### (a) Cosmos sends a singular `role`, and no `groups`

Cosmos emits **no `groups` claim in any version**, and its client settings offer no field to add one. It does emit
a **singular `role` claim** — a claim its own discovery document does not advertise. That is why
`OIDC_GROUPS_CLAIM` must be set to `role`, and why every group list carries **both encodings**: `admin` and
`user` **and** `1` and `2`. Decode an `id_token` from your own realm before you rely on either — the claim value
in the token is the authority, not the provider's settings page and not its discovery document.

The mechanism matters, because it decides whether the group axis works at all on your version. `groups_from()`
(`curiarium/api/oidc.py`, lines 593 to 600) reads the configured claim and accepts only a `str` or a `list`;
anything else — an integer, an object, a boolean — returns an empty tuple. So on a version that emits `role` as a
**bare numeric**, nothing matches: no principal is admitted by group, and none escalates to editor. The group axis
is dead on that version, and the fallback is the domain axis: `OIDC_ALLOWED_DOMAINS` plus `OIDC_DEFAULT_ROLE`.

Two further warnings about the same rows. First, the two allow-lists compose with **AND**, so naming domains
alongside groups narrows the gate rather than widening it: a principal must satisfy both. If you populate
`OIDC_ALLOWED_DOMAINS` while the group axis is dead, you have admitted nobody — remove one of the two. Second,
`OIDC_EDITOR_GROUPS` is the highest a claim set can produce; `OIDC_ADMIN_GROUPS` must stay empty, and
`OIDC_DEFAULT_ROLE` may never be `admin`. Both are boot refusals, not warnings.

Check what your provider actually emits before filling the lists in. The claim value in the ID token is the
authority; the settings page is not.

### (b) The route gate answers 401 to plain `curl`

A Cosmos route with authentication enabled refuses an anonymous request with 401 before Curiarium ever sees it.
Plain `curl` against `/api/...` therefore gets 401 even when every part of the deployment is working. Verification
from outside needs a session — a browser, or a cookie jar from one — or must go through the application's own UI.

This is also why the 401 in the verification step above is expected rather than a failure, and why a 401 from a
`curl` run is not evidence of a broken route: the status line proves the request reached the API path. If you need
to tell the two apart, compare the response body — the gate's refusal and the API's `{"detail": ...}` do not look
the same — or turn the route's authentication off for the length of one test and turn it back on.

### (c) A split origin breaks every image — `Mf-ce8l`

If the API ends up on a different origin from the web UI — `VITE_API_URL` pointing at a different scheme, host or
port than `PUBLIC_URL` — the browser loads the UI from one origin and fetches images from the other. The built-in
CSP's `img-src` does not include that origin, so thumbnails and cover images are blocked: the element exists,
`naturalWidth` is 0, and `decode()` rejects with "The source image cannot be decoded". Every other API call works,
which is what makes it look like a storage bug.

This is **`Mf-ce8l`**, and this guide does not fix it. Either keep the API on the same origin — leave
`VITE_API_URL` empty so the web container derives `<PUBLIC_URL>/api`, which is what the template does — or treat
adding the API origin to `SECURITY_HEADERS_CSP` as a deliberate deployment decision and record it here for that
target.

### Snapshot capability depends on the mount

`GET /storage/capabilities` reports what the mounted volume actually is rather than implying it. A volume that
cannot be snapshotted says so, with a `reason`.

`usable: false` on a volume you expected to snapshot means the mount, not the app: a bind mount from a filesystem
with no snapshot method — ext4, tmpfs, a network share — reports no method, and that is an audited absence rather
than a failure, so `Empty trash` keeps working by default. ZFS snapshots need the API image that carries the
userland tools, `ghcr.io/insanemal/curiarium-api:edge-zfs` instead of the `:edge` image the template pins, plus
access to the pool; the plain `edge` image reports that `zfs` could not be run even when the pool is set up
correctly. This template declares no `devices` entry, so a ZFS-backed deployment needs that change made
deliberately.

A scheduled snapshot covers the library volume only. It does **not** cover the SQLite database, so pair it with a
database backup: a snapshot of `/app/storage` alone is half a restore.
