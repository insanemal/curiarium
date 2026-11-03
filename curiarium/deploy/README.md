# Deployment templates

Placeholder-only templates for the platforms Curiarium is deployed on, each paired with a walkthrough.

## What this directory is

Every supported target has a directory here, `curiarium/deploy/<target>/`, holding one template, and a
walkthrough beside it at `curiarium/docs/guides/<target>.md` that turns that template into a running
deployment. The pair is the contribution: a template without a guide is a file nobody can fill in, a guide
without a template is prose nobody can paste. Landing one without the other leaves the target unfinished, and
a reviewer should ask for the missing half.

The two existing pairs:

| Target | Template | Walkthrough |
|---|---|---|
| Cosmos Cloud | [cosmos/compose.json](cosmos/compose.json) | [cosmos.md](../docs/guides/cosmos.md) |
| TrueNAS SCALE | carried inline in the guide (predates this directory) | [truenas.md](../docs/guides/truenas.md) |

Templates are strict JSON. A JSON file has no comments, so nothing a comment would have said belongs in the
template: every substitution, every trap and every "why" lives in the guide next to it.

## What belongs here, and what does not

Belongs:

- One placeholder-only template per target, in `curiarium/deploy/<target>/`.
- The matching walkthrough, in `curiarium/docs/guides/<target>.md`.
- Nothing else. No scripts, no helper files, no per-target `.env`.

Does not belong:

- A working private manifest. The known-good manifests for specific hosts stay outside the repository.
- Real hosts, addresses, domains, dataset or directory paths, usernames, tokens or secrets — in the template
  *or* in the guide, including inside code fences.
- Screenshots. They date, they cannot be reviewed as text, and a broken image reference fails a repository
  guard.
- Build scripts, CI wiring, or anything that runs. A template is pasted by a person, not executed by us.

## The contract

Every target satisfies all five clauses. Each one is checkable line by line against the template and the guide.

1. **Placeholders only.** Every operator value is an `<ANGLE_BRACKET>` placeholder; never a real host, address,
   domain, dataset path, username or secret. Permitted literals only: reserved `example.com`-class names in
   prose, empty-string functional defaults such as `VITE_API_URL=""`, the published registry path
   `ghcr.io/insanemal/curiarium-{api,web}:edge`, and the pinned values named in clauses 3 and 4. If a value
   would differ between two operators, it is a placeholder.

2. **An env table.** The guide names every value the operator must supply, what it does, and how to obtain it.
   The shared table below is the baseline: a target adds its own rows (platform-specific paths, route names,
   provider settings) and may not quietly drop a row that still applies. A value the target does not use says
   so.

3. **The volumes that must persist: `/app/storage` (the library) and `/app/data` (the SQLite database).** Both,
   always. A backup of one without the other is a broken restore: the database knows about files that the
   library no longer has, or the library holds files the database has never heard of. `FILE_STORAGE=/app/storage`
   and `DB_URL=sqlite:////app/data/curiarium.db` are pinned, not operator-tunable — moving either inside the
   container is not a supported configuration, because the images, the entrypoint and the backup guidance all
   assume those two paths.

4. **The single port that must be routed: `web:5173`.** All paths go to the web container, which proxies `/api/`
   and `/auth/` to `api:8000`. The API is never routed or published directly: it has no session handling of its
   own in front of it, its OpenAPI surface is not meant to be public, and the web container is what carries the
   security headers and the upload size limit. The API listens on `8000` inside the compose network only, which
   is why `API_PORT` stays `8000` in every template — the web container's nginx proxies to `api:8000` by name.

5. **Verification that ends in something observable** — a status code, a header, or a JSON field, never "it
   should work". A step that cannot fail is not a verification step. The generic pattern is in
   [Verifying a deployment](#verifying-a-deployment); a target's guide repeats it with its own addresses.

## The shared env table

Columns: the value, what it does, and how to obtain it. "Leave the default" means the row may be deleted from
the template and the image's built-in default applies.

| Value | What it does | How to obtain it |
|---|---|---|
| `PUID` | Numeric UID the API writes files as. A file owned by anyone else cannot be renamed into the trash, so deletes answer 409. | The UID that owns the two host directories; `id -u <USER>` on the host. |
| `PGID` | Numeric GID the API writes files as. | The GID that owns the two host directories; `getent group <GROUP>` on the host. |
| `CHOWN_MODE` | How much ownership repair the entrypoint does: `minimal` (the two root directories) or `recursive` (the whole data tree). | Leave `minimal`. Use `recursive` only for a tree that another account wrote, such as a copied mount. |
| `FILE_STORAGE` | Where the library lives inside the container. | Pinned to `/app/storage`. Not operator-tunable. |
| `DB_URL` | Where the SQLite database lives inside the container. | Pinned to `sqlite:////app/data/curiarium.db`. Not operator-tunable. |
| `API_PORT` | The port the API listens on inside the compose network. | Pinned at `8000`: the web container proxies to `api:8000` by name. |
| `PUBLIC_URL` | The absolute origin the browser uses. It is also the origin the OIDC redirect URI is derived from. | The origin you will actually browse to, `https://curiarium.example.com`-style: scheme and host, no path, no trailing slash. Empty only for a plain-HTTP deployment reached by address. |
| `CORS_ORIGINS` | Comma-separated extra origins the API allows a browser to call it from. | Each origin the UI is loaded from, when it differs from `PUBLIC_URL`. Empty means same-origin only. |
| `VITE_API_URL` | Absolute API base URL written into the web container's runtime config. | Leave empty: the web container derives it from `PUBLIC_URL`. Set it only for a split-origin deployment, and read the split-origin note in [cosmos.md](../docs/guides/cosmos.md) first. |
| `VITE_ALLOWED_HOSTS` | Extra hostnames accepted by the Vite dev server and `vite preview` only. The shipped web container is nginx with `server_name _;`, which answers any `Host`. | Leave empty in this deployment: nothing in the container reads it, so it constrains nothing there. |
| `AUTH_USERNAME` | The break-glass local account name. | Choose it. It is inert from the moment an enabled administrator exists. |
| `AUTH_PASSWORD` | The break-glass local password. | Chosen by the operator, then deleted once a real administrator exists. Never the published sample: the API refuses to boot while it is. |
| `AUTH_SECRET` | Signs every access token. Rotating it logs every session out. | Generate it with `openssl rand -hex 32` (at least 32 bytes). Never rotate it silently — rotating it invalidates every session. |
| `AUTH_MODE` | Which login paths exist: `auto`, `local`, `both`, `oidc`, `off`. | Choose it from the login paths you want. `both` when SSO is on and local login must survive. Empty means `auto`; an unknown value refuses the boot. |
| `AUTH_ACCESS_TOKEN_TTL` | Access-token lifetime, in seconds. Access tokens are short-lived JWTs. | Leave the default of `900`. |
| `AUTH_REFRESH_TTL_SECONDS` | Refresh-token lifetime, in seconds. Refresh tokens are opaque, stored hashed, and rotated once per use. | Leave the default of `2592000`. |
| `AUTH_FIRST_RUN_SETUP` | Opens the browser first-run administrator window (ADR 0009). Both routes answer 404 while it is off. | `1` for the first administrator, then unset and restart. The window closes itself at the first enabled administrator, but it stays advertised in the boot log until the flag is gone. While it is open anyone who can reach the API can create that administrator: finish in one session, on a trusted network. |
| `AUTH_ALLOW_INSECURE_DEFAULTS` | Lets the API start while the published sample credentials are still in place. | `0` on any host another person can reach. Only a throwaway local deployment sets it. |
| `MAX_UPLOAD_MB` | Direct upload cap in MB; `0` disables it. | Match the body-size cap of whatever proxy sits in front. |
| `IMPORT_MAX_MB` | Response-size cap in MB for one link import. | Leave the default unless your imports are larger. |
| `IMPORT_ZIP_MAX_MB` | Cumulative expanded-size budget in MB for the entries selected from one zip import. | Leave the default unless your archives are larger. |
| `IMPORT_TIMEOUT_SECONDS` | Time budget for one remote request. | Leave the default of `30`. |
| `IMPORT_MAX_REDIRECTS` | Redirect hops followed while fetching a remote import. | Leave the default of `5`. |
| `IMPORT_HTML_MAX_KB` | Resolver HTML size cap in KB. | Leave the default of `4096`. |
| `ASSET_FILE_MAX_MB` | Per-supporting-file cap in MB. The cap in force is the smaller of this and `IMPORT_MAX_MB`. | Leave the default; empty makes it follow `IMPORT_MAX_MB`. |
| `ASSET_FILE_TOTAL_MAX_MB` | Total size in MB of one model's supporting files; `0` disables the check. | Leave the default of `2048`. |
| `ASSET_FILE_MAX_COUNT` | Maximum supporting files on one model; `0` disables the check. | Leave the default of `50`. |
| `MAKERWORLD_COOKIE` | Optional server-side MakerWorld session cookie for link imports. A browser-supplied value wins, and cookies are never persisted. | Paste a session cookie only if you want imports to run without one. Empty otherwise. |
| `THINGIVERSE_COOKIE` | Same, for Thingiverse. | Paste a session cookie, or leave empty. |
| `LOGIN_THROTTLE_ENABLED` | Counts failed logins per submitted username and locks out with escalating backoff. | Leave `true`. Turning it off is a deliberate exposure. |
| `LOGIN_MAX_FAILURES` | Failures allowed inside the window before a lockout. | Leave the default of `5`. |
| `LOGIN_FAILURE_WINDOW_SECONDS` | The counting window, in seconds. | Leave the default of `900`. |
| `LOGIN_LOCKOUT_SECONDS` | The first lockout, in seconds. | Leave the default of `300`. |
| `LOGIN_MAX_LOCKOUT_SECONDS` | The ceiling the backoff escalates to, in seconds. | Leave the default of `3600`. |
| `OIDC_ENABLED` | Turns the four OIDC routes on. While it is off they answer 503 and local password login is unchanged. Turning SSO on does **not** turn password login off. | `true` only once the provider is configured. |
| `OIDC_ISSUER` | Provider origin, used to fetch the discovery document and to validate `iss`. | The provider's bare origin: scheme and host, no path, no trailing slash, never the discovery URL. The `https` scheme is mandatory — a plain-HTTP issuer is refused. |
| `OIDC_CLIENT_ID` | Client identifier registered with the provider. | Copy it from the provider's client registration. |
| `OIDC_CLIENT_SECRET` | Client secret used for the code exchange. | Issued by the provider, pasted not invented. A confidential client is the only kind that has one; a public client is refused. |
| `OIDC_SCOPES` | Scopes requested at sign-in. | Leave `openid profile email`. |
| `OIDC_GROUPS_CLAIM` | The claim whose value is read as group membership. | The claim the provider actually emits. Providers differ: see the claims trap in [cosmos.md](../docs/guides/cosmos.md). |
| `OIDC_ALLOWED_GROUPS` | Group allow-list: a principal must carry one of these. | The group values the claim carries. At least one of the two allow-lists must be populated, or the boot refuses a configuration that would admit every account the provider will authenticate. |
| `OIDC_ALLOWED_DOMAINS` | Email-domain allow-list. | The domains you accept, comma-separated. **The two allow-lists compose with AND**: naming domains alongside groups narrows the gate, it does not widen it. |
| `OIDC_EDITOR_GROUPS` | Groups that escalate a first sign-in to editor — the highest role a claim set can produce. | The group values that should get editor. Empty leaves everyone at `OIDC_DEFAULT_ROLE`. |
| `OIDC_ADMIN_GROUPS` | Would grant admin from a claim. | **Must stay empty — claims can never mint an admin.** A non-empty value refuses the boot; create the administrator locally instead. |
| `OIDC_DEFAULT_ROLE` | The role a first SSO sign-in is provisioned with. It applies at provisioning only. | `viewer` (the default) or `editor`. **`admin` is refused at boot.** |
| `SECURITY_HEADERS_ENABLED` | Turns the bundled web container's header block off entirely. | Leave `true` unless a proxy in front already sets them, or the setup cannot tolerate them. |
| `SECURITY_HEADERS_HSTS` | The `Strict-Transport-Security` value. | Empty means the built-in default, `max-age` only — no `includeSubDomains`, no `preload`. Literal `off` omits that one header. |
| `SECURITY_HEADERS_CSP` | The `Content-Security-Policy`. The built-in policy's `connect-src` and `img-src` allow the configured absolute API origin. | Empty unless you know the sources you need. An override is only needed to add further sources beyond the derived API origin. |
| `SECURITY_HEADERS_REFERRER_POLICY` | The `Referrer-Policy`. | Empty means the built-in default. Literal `off` omits it. |
| `SECURITY_HEADERS_FRAME_OPTIONS` | The `X-Frame-Options`. | Empty means the built-in default. Literal `off` omits it. |
| `SECURITY_HEADERS_PERMISSIONS_POLICY` | The `Permissions-Policy`. | Empty means the built-in default. Literal `off` omits it. |
| `SECURITY_HEADERS_CONTENT_TYPE_OPTIONS` | The `X-Content-Type-Options`. | Empty means the built-in default. Literal `off` omits it. |
| `SNAPSHOT_POLICY_PREDELETE` | What the folder-delete guard does: `warn` proceeds without a snapshot and reports it. | `warn` or `strict`. |
| `SNAPSHOT_POLICY_PREPURGE` | The same, for the trash purge. | `strict` (the default) or `warn`. |
| `SNAPSHOT_POLICY_PREBULK` | The same, for a bulk asset delete. | `strict` (the default) or `warn`. |
| `SNAPSHOT_REQUIRE_CAPABILITY` | Makes `strict` refuse when the volume has no snapshot method, instead of recording an audited absence. | `false` unless you know the library volume is snapshottable. |
| `SNAPSHOT_SCHEDULE_ENABLED` | Starts the scheduled library snapshot thread from the API's lifespan. Off by default, so nothing is snapshotted behind the operator's back. | `true` only when the library volume supports snapshots. |
| `SNAPSHOT_SCHEDULE_HOURS` | Take a scheduled snapshot when the newest one is older than this many hours. | `24`, or your retention window. |
| `SNAPSHOT_SCHEDULE_CHECK_SECONDS` | How often the thread asks whether one is due. | Leave the default of `300`. |
| `SNAPSHOT_SCHEDULE_START_DELAY_SECONDS` | Delay before the first tick, clear of migrations and the boot guards. | Leave the default of `60`. |
| `SNAPSHOT_BTRFS_SIDECAR_URL` | The privileged sidecar that snapshots a BTRFS library volume. The API container holds no `CAP_SYS_ADMIN`, so it asks the sidecar. | Your sidecar's origin, or empty. Leave both sidecar values empty to disable the provider. |
| `SNAPSHOT_BTRFS_SIDECAR_TOKEN` | Bearer token the API presents to that sidecar. | Issued by the sidecar's own configuration, pasted not invented. |

Every key in this table is traceable to `curiarium/api/.env.example`, which is the source of truth: if a key
here and a key there disagree, that file wins and this table is the bug. `API_IMAGE` and `WEB_IMAGE` are the two
exceptions — they are not environment variables in a template, they are the `image:` fields of the two services,
and the only published paths are the ones clause 1 names.

A scheduled snapshot is a filesystem snapshot of the library volume. It does **not** cover the SQLite database,
so pair it with a database backup; a snapshot of `/app/storage` alone is half a restore.

## Verifying a deployment

The same four steps for every target, with the target's own addresses filled in. Each one ends in something you
can read off the screen.

1. Bring the stack up and confirm both services are running, not restarting.
2. Confirm the web port answers. From another machine, not the host itself:

   ```bash
   curl -sI https://<PUBLIC_URL>/ | head -n 1
   ```

   The first line is `HTTP/1.1 200 OK` (or `HTTP/2 200`). A `502` here means the web container is up and its
   upstream is not.

3. Confirm the API is reachable **through** the web container, not directly:

   ```bash
   curl -sI https://<PUBLIC_URL>/api/storage/capabilities | head -n 1
   ```

   Two legitimate outcomes. `HTTP/1.1 401 Unauthorized` is the expected answer when a front gate is on — the
   platform's own route authentication refusing an anonymous request before Curiarium ever sees it — and it
   still proves the path reached the API rather than the SPA fallback. `HTTP/1.1 200 OK` means the request
   arrived authenticated. A `200` with an HTML body, or a `404`, means the route is not reaching `/api/` at all.
   Never test the API by publishing its port: if you can reach `8000` from outside the compose network, the
   template is wrong.

4. Confirm an authenticated endpoint returns the expected shape. Sign in through the UI, then:

   ```bash
   curl -s -b <COOKIE_JAR> https://<PUBLIC_URL>/api/storage/capabilities | jq '.volumes | length'
   ```

   A number of at least `1`. The response is a JSON object whose `volumes` array describes each mounted volume,
   including whether it is `usable` for snapshots. An empty array, or a body that is not JSON, is a failure: the
   route answered, but not from the API.

## Checklist for adding the next platform

1. Read [the contract](#the-contract) and the shared env table. If the target cannot satisfy a clause, that is a
   discussion, not a local exception.
2. Copy the closest existing target — `cp -r curiarium/deploy/cosmos curiarium/deploy/<target>` — and the guide
   beside it. Start from something that already works on a similar platform rather than from a blank page.
3. Replace every placeholder with an `<ANGLE_BRACKET>` placeholder for the new platform, and delete the rows the
   target does not use. Keep `FILE_STORAGE`, `DB_URL` and the two volume targets exactly as they are.
4. Write the guide, mirroring the shape of the existing ones: title, tested-version line, prerequisites,
   numbered steps, a `## Verify` section, and the failure modes actually seen — with the reason for each, not
   just the symptom.
5. Add the row to the documentation map and the guides list in [../docs/README.md](../docs/README.md).
6. Add the pair to the table at the top of this file.
7. Run the placeholder grep. Nothing may match: no address literals, no host paths, no `root` logins, no
   secrets, and no registry path outside the three published ones.
8. Verify from a clean install — empty directories, no leftover container, no cached image — and record what you
   saw, not what you expected.
