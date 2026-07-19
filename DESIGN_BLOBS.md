# Design: content-addressed blob attachments

**Status: planned.** Build **after** card history. Plaintext for v1 (client-side
encryption is a deliberate later layer — see "Encryption"). This is a
protocol/transport feature (any node can carry attachments); kanban cards are
the first consumer.

_Written 2026-07-19. Reuses the per-board relay target
([DESIGN_IDENTITY_AND_TRANSPORT.md], relay-targets work) to decide where a
board's blobs live._

## Goal

Attach binary blobs (images, PDFs, …) to a node. Bytes are stored on the
board's **relay** when it uses one, served **peer-to-peer over HTTP** for a
direct connection, or kept **local** when solo. Blobs are **content-addressed**,
so identical files are shared (multi-referenced) and are **garbage-collected
when the last reference is removed**.

## Core model: reference in the node, bytes in a separate store

- **Content-addressed.** A blob's identity is `sha256(bytes)`. The node stores
  only a **reference** in its `data`:
  `data.attachments = [{hash, name, size, mime}]`. Because it lives in `data`,
  the reference is part of `content_hash` — so an attachment change is *content*
  (divergence-tracked; two people attaching different files is a real conflict
  to resolve), and identical bytes **dedup to one blob** automatically. That is
  the "multi-referenced" property for free.
- **Every peer has a local blob store**, `blob_store.py`:
  `write_blob(bytes) -> hash`, `read_blob(hash) -> bytes | None`,
  `has_blob(hash) -> bool`, `delete_blob(hash)`, `iter_hashes()`. Stored under
  `data/blobs/<hash[:2]>/<hash>` (sharded). **Verifies the hash on read** so a
  corrupt or tampered blob is caught (content-addressing = integrity).
- **Bytes move out-of-band from the poll loop.** Snapshots (JSON) carry only
  references; the bytes are fetched **lazily** — on open / thumbnail — never
  inline in a snapshot and never during the relay poll.

## Where the bytes live (follows the board's transport)

- **Relay board** → a `blobs/<hash>` namespace next to `topics/` and
  `identities/`. `RelayStorage` (local + sftp, and later webdav) gains
  `write_blob` / `read_blob` / `has_blob` / `delete_blob`. When a peer publishes
  a topic snapshot, it also uploads any referenced blob the relay doesn't have
  yet; a peer that receives a reference to a missing blob fetches it from the
  relay on demand.
- **Direct / HTTP peer** → `GET /api/blob/{hash}` serves from the local store;
  the receiver fetches from whichever peer advertises it.
- **Local only** → it simply stays in the local store.

## Upload / attach flow

```
POST /api/blob            (multipart)         -> {hash, size, mime}   # stores locally
GET  /api/blob/{hash}                          -> bytes                # serves from local store
POST /api/kanban/cards/attach   {card_uuid, hash, name}               # adds a data.attachments ref
POST /api/kanban/cards/detach   {card_uuid, hash}                     # removes the ref
```

Upload is a two-step: the client uploads bytes (gets a hash), then attaches the
`{hash, name, size, mime}` reference to the card through a normal card update.

## Garbage collection ("deleted when the last reference is removed")

- **Local (straightforward mark-sweep).** Referenced hashes = the union of
  `data.attachments[*].hash` over every **live** local node. Any local blob not
  in that set is deletable. Run it where tombstones are pruned (it's the same
  lifecycle — a pruned node drops its references). A short grace period avoids a
  race with an in-flight attach.
- **Relay (the hard, distributed part — same shape as tombstone GC).** A blob on
  the relay is referenced if **any** node in **any** topic on that relay
  references it. The relay host scans referenced hashes across topics' latest
  snapshots and collects the rest — **conservatively** (grace period /
  confirmation-gated), because a peer may be about to publish a node referencing
  a blob it hasn't uploaded yet. This can be **deferred**: until it's built,
  relay blobs simply accumulate (bounded by real usage; manual cleanup
  possible), which is acceptable for an MVP.

A protocol helper `referenced_blob_hashes(root) -> set[str]` (walk live nodes'
`data.attachments`) backs both sweeps and keeps the logic app-agnostic.

## Encryption (deferred, but designed for)

v1 stores plaintext, matching today's relay (plaintext JSON). Blobs are the
*ideal* place to add **client-side encryption** later: they're immutable, so you
encrypt once, address by the hash of the ciphertext, and the store only ever
sees opaque bytes — which would make even an untrusted backend (or a broadly
scoped credential) safe for personal photos. The key would travel with the
node's `data` or the identity channel, not the store. Keep the reference shape
(`{hash, name, size, mime}`) and the store API encryption-agnostic so this slots
in without a format change to the reference itself.

## Decisions to confirm before building

- **Max blob size** (reject uploads above it) and total per-target budget
  awareness (basic SFTP hosting is small).
- **Thumbnails:** v1 = none (fetch full blob on click) is simplest; a small
  inline thumbnail (separate tiny blob, or a data-URI capped at N KB in the
  reference) is a nice-to-have that avoids fetching big images to preview.
- **Relay GC now or deferred** (accumulate for MVP vs. build the conservative
  scan).

## Phase plan (suite green after each)

1. **`blob_store.py`** — local content-addressed store, sharded layout, verify
   on read. Tests: round-trip, dedup, hash-mismatch rejection, delete.
2. **Protocol convention + helper** — `data.attachments` reference shape and
   `referenced_blob_hashes(root)`; no protocol schema change (it's `data`).
3. **HTTP upload/serve + kanban attach/detach** — `POST /api/blob`,
   `GET /api/blob/{hash}`, card attach/detach, references in `board_payload`.
4. **Relay backend** — `blobs/` namespace on `RelayStorage` (local + sftp);
   publish referenced blobs alongside the topic snapshot; lazy fetch of missing
   blobs on apply.
5. **GC** — local mark-sweep tied into the prune lifecycle; relay scan-based GC
   (conservative) or explicitly deferred.
6. **UI** — attach button, attachment list with size/type, download/preview, and
   an availability indicator (present locally vs. fetch-on-click).
7. **Tests** across the above, including a two-client attach → fetch over relay,
   dedup of the same file on two cards, and GC after the last reference is
   deleted.

## Effort

Sizeable — roughly 4–6 sessions, comparable to the relay-targets feature. Phase
1–3 (local store + HTTP path) is a usable slice on its own; phases 4–5 (relay +
GC) are the bulk and the distributed-GC subtlety.
