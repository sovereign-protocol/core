# Design: content-addressed blob attachments

**Status: avatar MVP implemented.** Plaintext for v1; encryption is deliberately
deferred. This is a protocol/transport feature (any node can carry blob
references); the first consumer is the shared user-profile picture. Relay GC
is intentionally report-only until destructive concurrency tests exist.

_Written 2026-07-19. A blob follows every transport connection that publishes
its referencing topic. This also covers identity topics, which are not scoped
to one board._

## Goal

Attach binary blobs (images, PDFs, …) to a node. Bytes are stored on every
**relay connection** carrying the referencing topic, served **peer-to-peer over
HTTP** for a direct connection, or kept **local** when solo. Blobs are **content-addressed**,
so identical files are shared (multi-referenced) and are **garbage-collected
when the last reference is removed**.

## Core model: reference in the node, bytes in a separate store

- **Content-addressed.** A blob's identity is `sha256(bytes)`. The node stores
  only a **reference** in its `data`:
  `data.attachments = [{id, role, blob_id, name, size, mime}]`, where `blob_id`
  is `sha256:<64 lowercase hex characters>`. References have stable IDs and a
  canonical order; `role="avatar"` selects the profile picture. Because it
  lives in `data`,
  the reference is part of `content_hash` — so an attachment change is *content*
  (divergence-tracked; two people attaching different files is a real conflict
  to resolve), and identical bytes **dedup to one blob** automatically. That is
  the "multi-referenced" property for free.
- **Every peer has a local blob store**, `blob_store.py`:
  `write_blob(bytes) -> hash`, `read_blob(hash) -> bytes | None`,
  `has_blob(hash) -> bool`, `delete_blob(hash)`, `iter_hashes()`. Stored under
  `data/blobs/<hash[:2]>/<hash>` (sharded). **Verifies the hash on read** so a
  corrupt or tampered blob is caught (content-addressing = integrity).
- **Bytes move out-of-band.** Snapshots (JSON) carry only references. The avatar
  MVP eagerly caches the small blobs advertised by a received relay head, so a
  client can immediately display and safely republish them. Larger future
  attachment roles can use lazy fetching without changing the reference model.

## Where the bytes live (follows the board's transport)

- **Relay board** → a `blobs/<hash>` namespace next to `topics/` and
  `identities/`. `RelayStorage` (local + sftp, and later webdav) gains
  `write_blob` / `read_blob` / `has_blob` / `delete_blob`. When a peer publishes
  a topic snapshot, it also uploads any referenced blob the relay doesn't have
  yet; a peer that receives a reference to a missing blob fetches it from the
  relay on demand.
- **Direct / HTTP peer** → `GET /api/blob/{hash}` serves from the local store.
  A resolver records the peer/relay connection from which a reference arrived,
  verifies fetched bytes, and caches them locally. A peer must possess or fetch
  a blob before republishing its reference through another transport.
- **Local only** → it simply stays in the local store.

## Upload / attach flow

```
POST /api/blob            (raw request body)  -> {blob_id, size, mime} # stores locally
GET  /api/blob/{hash}                          -> bytes                # serves from local store
POST /api/kanban/profile/avatar {attachment}                          # adds the avatar ref
POST /api/kanban/profile/avatar {remove: true}                        # removes the avatar ref
```

Upload is a two-step: the client uploads bytes (gets a hash), then attaches the
`{hash, name, size, mime}` reference to the card through a normal card update.

## Garbage collection ("deleted when the last reference is removed")

- **Local (straightforward mark-sweep).** Referenced hashes = the union of
  `data.attachments[*].blob_id` over every **live** local and cached peer node. Any local blob not
  in that set is deletable. Run it where tombstones are pruned (it's the same
  lifecycle — a pruned node drops its references). A short grace period avoids a
  race with an in-flight attach.
- **Relay.** Every publisher writes a manifest for the blobs referenced by its
  retained current/previous snapshots. Uploads acquire a short-lived lease,
  upload and verify the bytes, publish the referencing snapshot/head, then
  release the lease. One configured owner per relay target performs two mark
  scans separated by a grace period. Only blobs absent from both scans and all
  live leases are collectible. The first release ships this scanner in
  report-only mode; destructive deletion is enabled only after concurrency and
  crash tests pass.

A protocol helper `referenced_blob_hashes(root) -> set[str]` (walk live nodes'
`data.attachments`) backs both sweeps and keeps the logic app-agnostic.

## Encryption (deferred, but designed for)

v1 stores plaintext, matching today's relay (plaintext JSON). Blobs are the
*ideal* place to add **client-side encryption** later: they're immutable, so you
encrypt once, address by the hash of the ciphertext, and the store only ever
sees opaque bytes. Encryption requires wrapped keys delivered outside the
plaintext relay data; putting a raw key in node data would not protect against
the relay. The store remains encryption-agnostic, but encrypted references will
need explicit algorithm/key metadata in a later format version.

## Deferred decisions

- **Max blob size** (reject uploads above it) and total per-target budget
  awareness (basic SFTP hosting is small).
- **Thumbnails:** v1 = none (fetch full blob on click) is simplest; a small
  inline thumbnail (separate tiny blob, or a data-URI capped at N KB in the
  reference) is a nice-to-have that avoids fetching big images to preview.
- **Destructive relay GC:** the complete manifest/lease/two-scan analysis is
  implemented, but deletion remains disabled until concurrency/crash tests.

## Phase plan (suite green after each)

1. **`blob_store.py`** — local content-addressed store, sharded layout, verify
   on read. Tests: round-trip, dedup, hash-mismatch rejection, delete.
2. **Protocol convention + helper** — canonical `data.attachments` references and
   `referenced_blob_hashes(root)`; no protocol schema change (it's `data`).
3. **Resolver + HTTP** — upload, serve and fetch-through with source tracking.
4. **Profile avatar** — upload/remove controls and `role="avatar"` rendering;
   the legacy picture URL remains a display fallback during this cutover.
5. **Relay backend** — blob namespace, upload-before-head, manifests and leases
   on local + SFTP storage; lazy verified fetch of missing bytes.
6. **GC** — complete local mark-sweep; relay report-only two-scan collector.
7. **Later consumers** — generic card/comment attachment UI and APIs.

## Effort

Sizeable — roughly 4–6 sessions, comparable to the relay-targets feature. Phase
1–3 (local store + HTTP path) is a usable slice on its own; phases 4–5 (relay +
GC) are the bulk and the distributed-GC subtlety.
