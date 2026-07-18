# Design: Nextcloud / WebDAV relay backend

**Status: DEFERRED — do not start yet.**
Pick this up only after the SFTP relay backend has been thoroughly tested in
real use (explicit relay targets, multi-board / multi-target, accept flow,
liveness). Rationale: WebDAV and SFTP are both just `RelayStorage`
implementations behind the *same* contract and go through the *same* target /
descriptor / fingerprint / poll plumbing. Validating SFTP end-to-end therefore
de-risks all of that shared machinery, leaving only the WebDAV-specific HTTP
verb + XML layer (Phase 1 below) as genuinely new surface. Building WebDAV
before SFTP is trusted would just mean debugging two things at once.

_Written 2026-07-18 on branch `relay-identity-registry`, after the explicit
relay-targets feature shipped. See [DESIGN_IDENTITY_AND_TRANSPORT.md] for the
token / §1.6 storage-provisioning model this composes with._

## Goal

Let a relay **target** be backed by Nextcloud (or any generic WebDAV server),
not only SFTP or a local folder. Motivation: a friendlier, self-hostable,
sovereignty-preserving option with better credential control (revocable,
scoped app-passwords) than a single SFTP login — while keeping everything on
infrastructure the user controls (explicitly *not* Google/OneDrive; those were
considered and rejected for now on both architectural and sovereignty
grounds).

## Why this is a small feature, not new architecture

The relay was built on a backend-agnostic `RelayStorage` contract — 10 methods,
"never merges or interprets content, just reads and writes bytes at a path"
(`relay_storage.py`): `write_snapshot`, `read_head`, `read_snapshot`,
`list_peers`, `list_topics`, `delete_topic`, `write_presence`,
`read_presence_with_mtime`, `verify_access`, `timing_probe`. Two backends
already implement it (`LocalFolderRelayStorage`, `SftpRelayStorage`), and the
target registry / descriptor / fingerprint code switches on a `backend` value
that a third backend simply extends. WebDAV maps almost 1:1 to the layout and
is in places *simpler* than SFTP (a `DELETE` on a collection recursively
removes a whole topic in one call, vs. SFTP's manual `_rmtree`).

Layout is unchanged (identical on every backend):

```
<root>/topics/<topic_uuid>/peers/<peer_id>/head.json
<root>/topics/<topic_uuid>/peers/<peer_id>/snapshots/<state_hash>.json
<root>/identities/<peer_id>/presence.json
```

Dependency posture: **no new library needed.** `requests` is already a
dependency (used in `transport.py`), and it issues custom verbs
(`requests.request("PROPFIND", …)`); XML responses parse with stdlib
`xml.etree.ElementTree`.

## Plan

### Phase 1 — `WebDavRelayStorage` (the bulk of the work)

New class in `relay_storage.py`, mirroring `SftpRelayStorage`, over a
`requests.Session`:

- **Verbs → contract methods:**
  - `GET` → `_read_json` / `read_snapshot` / `read_head`.
  - `PUT` → `_write_json` (see atomic write below).
  - `MKCOL` → `_mkcol_p`, created up the path like SFTP's `_mkdir_p`.
  - `DELETE` on a collection → `delete_topic` (recursive server-side; no manual
    tree walk).
  - `PROPFIND Depth:1` → `list_peers` / `list_topics`, returning a collection's
    members **and their mtimes in one round-trip** (the performance win over
    SFTP's per-file `stat`).
  - `PROPFIND Depth:0` → `_stat_mtime`.
- **Atomic write:** `PUT` to a `<path>.<uuid>.tmp`, then `MOVE` with
  `Destination:` + `Overwrite: T` — same temp-then-rename discipline used by the
  local and SFTP backends.
- **mtime for liveness:** parse `getlastmodified` (RFC-1123, second
  resolution) from PROPFIND; set `mtime_resolution_seconds = 1.0`.
  `write_presence` does `PUT` then a Depth:0 PROPFIND — the same write-then-
  read-mtime step SFTP uses so the liveness distance stays skew-free.
  `timing_probe` = one PROPFIND round-trip against a probe path.
- **`verify_access`:** MKCOL the root (idempotent) + PUT/DELETE a probe file, so
  a bad URL / credential / permission fails *before* a target is saved or a
  token accepted (same guarantee the SFTP backend gives).
- **Auth / connection:** `requests.Session` with HTTP Basic auth (username +
  app-password), keep-alive, connect timeout, one retry on a dropped
  connection. Simpler than SFTP — HTTP is stateless, no persistent channel to
  reset.

**Config keys** (via the existing `_secret` env/file indirection for the
password, so it need never be written into a committed file):
`relay_webdav_url` (base collection URL, e.g.
`https://cloud.example/remote.php/dav/files/<user>/skanban`),
`relay_webdav_username`, `relay_webdav_password`, optional
`relay_webdav_root` subpath.

### Phase 2 — wire the backend enum (~7 one-branch edits in `relay_logic.py`)

Add a `"webdav"` branch to each place that already switches on `backend`:
`_storage_fingerprint` (→ `webdav|host|path|user`), `_build_storage`,
`_storage_from_descriptor`, `_config_from_storage`, the module
`channel_descriptor`, and `RelayManager._record_from_descriptor` /
`_descriptor_from_record` / `create_target`. Fingerprint keying then gives a
WebDAV target its own per-target state file and correct dedup automatically.

### Phase 3 — UI

`boardofboards.html` create-target form currently hardcodes `backend:"sftp"`
with Host/Port fields. Add a **backend selector** (SFTP / WebDAV) that swaps
Host+Port for a single **Base URL** field; the location-display branch already
keys on `backend`, so extend it. `RelayManager.create_target` already accepts
`backend`.

### Phase 4 — Tests

- **Storage contract tests without a network:** route every HTTP call through
  one internal `_request(method, path, …)`, and in tests substitute an
  **in-memory fake WebDAV transport** (a dict emulating
  PUT/GET/DELETE/MKCOL/MOVE/PROPFIND semantics). Covers path building, atomic
  MOVE, and PROPFIND-list parsing for real — mirrors how the SFTP tests use a
  fake sftp client.
- Fingerprint / descriptor round-trip + `create_target` webdav cases in
  `tests/test_relay_logic.py` / `tests/test_app_server.py`.
- One **manual live config** (`relay_webdav_manual.json`, gitignored) + a
  launch profile for testing against a real Nextcloud — same pattern as the
  SFTP manual configs.

### Phase 5 — Docs

Update `DESIGN_IDENTITY_AND_TRANSPORT.md`'s transport section, add a
`relay_webdav_manual.json.example`, note the backend in the backend-selection
docstring.

## Effort

**~3–4 focused sessions. Medium size, low architectural risk.** Phase 1 is
~60% of the work; Phases 2–3 are mechanical because the seams already exist.
The real risk is not the code but **WebDAV server quirks** — Nextcloud vs.
generic WebDAV differences, PROPFIND XML namespaces (`DAV:` prefixes vary),
URL-encoding of path segments, and `MOVE`/`Overwrite` behavior. The live-test
time in Phase 4 is where that gets shaken out.

## Security decision to make before relying on it for sharing

For SFTP the shared credential is safe because the account is **chroot-jailed**
to `/relay`. A Nextcloud **app-password is not path-scoped** — it grants WebDAV
access to that user's entire files. So for a *shared* target, do **not** hand
out a personal app-password; create a **dedicated Nextcloud user** (or a group
folder) for the relay, exactly analogous to the jailed SFTP account. This is
the WebDAV equivalent of the jail and must be verified on the server before a
WebDAV target is used for sharing.

This is also another argument for the deferred **client-side blob/content
encryption** layer: with it, even a broad credential leak exposes only
ciphertext + paths, which makes the backend's own access scoping far less
load-bearing. See the attachments / content-addressed blob-store discussion
(to be written up separately).

## Precondition before starting (the deferral gate)

Thorough SFTP validation should first confirm, in real use, that the shared
machinery WebDAV also depends on is sound:

- Explicit relay targets: create / test / delete, board→target assignment.
- Multiple boards on one target (app-level separation) and one client running
  several targets at once.
- Token accept flow (§1.6 storage-provisioning) and two coexisting accepted
  targets.
- Presence liveness (alive/stale) and the profile-in-heartbeat name
  resolution under scoped polling.

Once those hold on SFTP, WebDAV is almost entirely Phase 1 plus wiring.
