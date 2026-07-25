# Identity, transport exclusivity, and relay scoping — design doc

Written up from a live-testing session that surfaced three related bugs, all
tracing back to one root cause: addresses, not identities, are the unit of
"who is this peer" throughout the stack. Status tags per item: **[DONE]**
(implemented and tested this session), **[PROPOSED]** (agreed in design
discussion, not yet built), **[DEFERRED]** (agreed, not needed for the
current scope, kept only as a note for later), **[OPEN]** (raised, not yet
resolved).

## 1. Protocol, high level

### 1.1 Layering

- **Session** (`session.py`) — protocol-level peer/identity bookkeeping.
  Generic across apps; knows nothing about boards, cards, or what "identity
  data" looks like.
- **Transport** (`transport.py`, plus `accept_connect_token` in
  `app_server.py`) — owns addresses. Decides which channel is open for a
  given peer, and enforces that only one ever is.
- **RelayLogic** (`relay_logic.py` + `relay_storage.py`) — one specific
  transport *backend*: a poll-based shared drop-box (SFTP or local folder),
  as opposed to live HTTP. An alternative delivery mechanism, not a
  different protocol layer.
- **App layer** (`kanban_logic.py`) — owns *content* sync policy: which
  topics exist (boards), how conflicts on them are resolved (selective
  auto-adopt), and — separately — identity *data* sync policy (always
  publish own, always adopt peer's, no conflict resolution at all).

### 1.2 Identity model

Every Session already has one stable, self-generated **identity_key**
(`session.identity.data.identity_key`, lazily created, stable for the
Session's lifetime). The problem this doc addresses: today, "does this
address belong to an identity I already know" is answered by re-deriving it
from whatever content happens to be cached under that address
(`Session.peer_identity`, walking a cached tree looking for a
`shared_user_profile` node) — not by looking it up. That forces every
consumer (relay's redundancy check, kanban's `users()` list, divergence
classification) to reconstruct the same fact independently, at different
times, with different available evidence — which is exactly what produced
the duplicate-avatar and stuck-partial-cache bugs this session.

**[DONE]** `Session.peer_identity_key: dict[addr, identity_key]` — one
canonical, address-independent registry. Written the *instant* any channel
learns the fact (connect-token accept, relay discovery of a peer's identity
topic) — never re-derived from cached content on demand. Persisted across
restarts via session metadata. It is *knowledge, not registration*:
`remove_peer` tears down a peer's registration but leaves the registry
entry, because "this address belongs to identity X" stays true after
teardown — only reconnect-replace explicitly forgets superseded addresses.

**Identity-key vs. identity-data split**, agreed explicitly:
- **identity_key** — a bare, opaque, stable string. Transport/Session-level,
  generic across every app built on this protocol. Used purely for peer
  bookkeeping (dedup, channel exclusivity, "who is this address").
- **identity data** (name, picture, email, ...) — app-level content, because
  different apps may represent "who is this person" differently. Kanban
  syncs this through its own dedicated topic, with a *fixed* policy: always
  publish your own, always adopt the peer's latest — no keep-mine/push-back,
  no divergence UI, categorically different from how a board is synced.

**Deliberately not merged at the wire level.** `identity_key` stays a bare
protocol-level primitive; identity *data* stays app content, kept apart on
purpose rather than folded into one generic "identity" wire concept —
different apps built on this protocol may represent "who is this person"
completely differently, so the protocol layer has no business knowing its
shape. Concretely, this means kanban_logic keeps sorting board topics from
identity-data topics itself (`join_discussion`'s existing type-sniffing) —
that doesn't go away, and isn't expected to.

### 1.3 Channel exclusivity (transport-owned)

- A offers N channel candidates in a connect token (`http`, `relay`/`sftp`).
- B selects **exactly one**, preferring `http`, falling back to relay only
  if `http` wasn't offered, was self-referential, or its join failed.
  Selection is enforced by the accepting side regardless of what order the
  token lists candidates in.
- Opening a second channel for an already-known peer is refused. Accepting a
  *new* token for an already-known identity (a reconnect) replaces the old
  channel registration rather than accumulating alongside it.
- **[DONE]** Implemented in `accept_connect_token` (`app_server.py`).
  **[OPEN]**: this logic conceptually belongs to "transport," not
  `app_server.py`'s free function — possible future relocation, not
  committed.
- **[PROPOSED, explicitly deferred]** No auto-failover: if the active
  channel dies, nothing switches automatically. The user re-initiates with a
  fresh token. Matches the "manual reconnect" philosophy chosen for MVP.
- **[DONE] `"offer_http_channel": false`** — a config opt-out (read by
  `collect_channel_descriptors`) that omits the http channel from generated
  tokens entirely, so a token advertises only relay. Useful to isolate the
  relay path for testing: with no http channel offered, no direct HTTP peer
  ever enters `members`, so relay's always-on broadcast can't race a
  concurrent direct connection to the same peer — the single most reliable
  way to sidestep the discovery race while exercising relay behavior. The
  http server itself keeps running for the local browser UI; it's just not
  offered as a *connect* channel. (Note: this doesn't change relay's
  always-on publish/poll — both sides still write to the shared root from
  boot, independent of any token; see §1.4.)

- **[DONE] `"relay_only": true`** — a client-wide transport policy. It
  omits HTTP from generated tokens, refuses HTTP channels in received tokens,
  rejects direct `/p2p` traffic, disables direct join/invite/observe actions,
  and drops restored HTTP peers while leaving the local browser UI running.

### 1.4 Relay protocol specifics

Verified against the current implementation (not assumed) — several pieces
of the "obvious" mental model turned out not to match what's actually
running:

| Expectation | Reality today |
|---|---|
| Token names where each side should publish | **[FIXED for the no-config accepter]** was: only carried *A's* root and B had to be pre-configured to the same server. Now the token also carries A's host/port/root **and scoped credentials**, and an accepter with no relay storage builds one from the token (§1.6). Still pre-config-dependent only if the accepter *already* hosts its own relay pointed elsewhere (documented limitation) |
| Token has an expiration | No expiration field anywhere, on token or channel descriptor |
| A polls "B's folder" specifically, once B accepts | `poll_and_apply` scans *every* topic from *every* peer_id on the shared root — but see the "active relationship" row below: the loop no longer runs at all until a relay relationship exists |
| Publish/poll triggered by token exchange | **[FIXED]** was: both ran continuously from app boot, independent of any handshake. Now the `relay_poll_loop` is gated on `has_active_relationship()` — see below |
| Heartbeat gates whether topic content gets checked | `write_presence`/`peer_liveness` is a fully separate subsystem, used only for the diagnostic `status_payload()` — polling never consults it |
| Consent (the token) restricts what's *published* | Today it only restricts what gets *grafted* as an owned board (`desired`); *discovery and caching* of everything you own, in full, is unconditional and effectively public to anyone who can read the shared root |

That last row is a real privacy gap, not a hypothetical: if I share a board
with X and separately with Y, connecting to Y over relay causes Y's session
to discover and fully cache my board-with-X too, the moment it notices my
identity tag published it — Y was never invited to that board.

**[DONE] Relay loop gated on an active relationship.** `relay_poll_loop`
(app_server.py) skips its entire tick — presence, publish, poll — unless
`RelayLogic.has_active_relationship()` is true. A relationship is active
once this session has:
- **issued** a relay-bearing token (`_state["shared"]`, set by
  `mark_topics_shared` from `api_connect_token`), **or**
- **accepted** one (`_state["desired"]`, set by `mark_topics_desired`), **or**
- already has a `relay:`-prefixed peer in `session.peer_topic_sets`.

A fresh boot with none of these writes *nothing* to the relay — no more
"publishing for no one," and no files appear before a connection is
initiated. **Why issuing must arm (the deadlock):** a drop-box relay has no
acceptance back-channel, so the issuer must publish *before* it learns
anyone accepted, or the accepter could never graft the board (and the issuer
never discovers the accepter). "A live peer is already known" is therefore
too strict a definition of active — generating the token is the earliest and
only available "I'm initiating a connection" signal. This gates *whether*
the loop runs; it does **not** change *which* topics get published (still all
owned boards + identity) — narrowing that to only the shared/desired topics
is the separate, still-deferred recipient-scoping item (the privacy gap
above).

**[DEFERRED, not MVP] Token expiration.** Not a protocol-level concept —
there's no "expired" signal, no special handling on the reading side at
all. It's purely a *writer-side* housekeeping decision: the App, when
generating a token, decides how long it's willing to keep publishing for
this relationship (and can choose to extend it), solely to bound how many
places a session ends up publishing to forever. When A stops refreshing,
B simply observes A as offline/stale through the *existing*
`peer_liveness`/staleness machinery — the same thing that already happens
for a genuine network outage. No new state, no new event, nothing to
enforce on B's side. Since none of the actual bugs fixed this session
depend on this existing, and it doesn't interact with `peer_identity_key`
or channel exclusivity, it's deferred out of the current scope entirely —
noted here so it isn't forgotten, not because it's blocking anything.

**[PROPOSED] Recipient-scoped publishing.** Replace "publish everything I
own under my identity tag" with "publish, for each peer relationship, only
what's relevant to that relationship" — scoped not by a separate storage
location, but by *content*: each publish names its intended recipient by
identity_key.

**[PROPOSED] Nugget.** A single small, per-relationship summary (hashes per
topic, plus room for an ack/last-message pointer later) that a poller reads
*first*, cheaply, before ever fetching full topic content. This generalizes
a pattern that already exists per-topic (`head.json` is already a cheap
pointer read before the expensive `read_snapshot()`) into one bundle per
relationship — the same shape `sync_summary`/`send_sync_status` already use
on the direct HTTP channel.

**[PROPOSED] One shared root per meeting.** No need for physically separate
folders *within* a relationship (an earlier, over-engineered version of this
proposal) — scoping-by-content inside one root achieves the same isolation
with less structure. Which root a given meeting uses is the inviter's, learned
from the token, not pre-configured on the accepter — see §1.6.

### 1.5 Mutual discovery

- If the selected channel is `http`: live, synchronous — B's `/p2p/join`
  POST to A carries B's own identity inline, A learns B's identity in the
  same request/response cycle. Already symmetric.
- If the selected channel is relay: no live push exists. B, having learned
  A's identity_key from A's *original* token, starts publishing content
  scoped "for A." A, polling broadly by design, finds it on its own next
  cycle. **No reciprocal token is required for basic mutual discovery** —
  the broad-poll design already makes this eventual and asymmetric-but-fine.
- **[OPEN]** A reciprocal token *would* matter if B needs to offer A a
  choice back (e.g. "I also have an http address, want to upgrade off
  relay?") — a genuine symmetric-renegotiation need the current one-sided
  flow doesn't support. Manual (copy-paste) with the current poll-only
  SFTP/local backend; would become automatic with a relay backend capable of
  actual message delivery, not just a shared drop-box. Not scoped for MVP.

### 1.6 Relay bootstrapping: meet in the inviter's space

**[PARTIALLY BUILT.]** The accepter-provisioning slice (a pure accepter
builds its single storage from the token) plus the credential-in-token
reversal are **[DONE]**; the full per-relationship *multi*-storage
generalization is still **[PROPOSED]**. What's built: `RelayLogic.
adopt_storage_from_descriptor` / `_storage_from_descriptor` build storage
from a token descriptor when the accepter has none, wired into
`accept_connect_token`'s relay branch; `channel_descriptor` now carries the
SFTP username+password. So an accepter with zero relay config now rides the
inviter's token straight into the inviter's space — no shared config file.

**The model.** Every relay meeting happens in the **inviter's** relay space:

- A generates a token → A publishes its perspective into **A's own root**.
- B accepts A's token → B publishes B's perspective into **A's advertised
  root** (under B's own identity tag, e.g. `.../peers/<B>/`), learned
  entirely from the token. A then just reads its own directory and finds B's
  perspective appear.
- B needs its *own* relay config only if B wants to *be* an inviter (issue
  tokens of its own). A pure accepter configures nothing — it rides the
  inviter's token straight into the inviter's space.

Your relay root is therefore "where I invite people"; accepting someone's
invite means going to publish in *their* space, not expecting them to come
to yours. This is what makes tokens meaningfully better than shared config
files: the accepter needs zero prior knowledge of the inviter's relay setup.

**Architectural change it requires.** `RelayLogic`/storage stops being a
single static per-process instance (built once from local config at boot,
relay_logic.py:161-176). It becomes **per-relationship**, keyed by the
advertised root: accepting A's token spins up a storage pointed at A's
host/root; accepting C's token adds another; plus your own root if you
invite. A session can be publishing into several roots at once.

**Built so far — single storage, token-provisioned [DONE].** The first
slice keeps *one* storage: a pure accepter with no storage builds it from
the token (`adopt_storage_from_descriptor`, no-op if it already hosts a
relay); bookkeeping is re-keyed to the adopted location via the existing
identity+location fingerprint. **Known limitation by scope:** if the
accepter already has its own storage but the token advertises a *different*
root, the token's location is ignored (its own is used) — a silent mismatch
that won't sync. Supported topology is "everyone shares one relay server";
true multi-root storage (a session talking to several servers at once) is
the deferred next slice.

**The credential decision — [DONE].** For B to *write* into A's SFTP
space, B must authenticate to A's server. This **reversed** the earlier
decision that a channel descriptor never carries credentials — the old
`test_sftp_channel_descriptor_never_includes_credentials` is replaced by
`test_sftp_channel_descriptor_includes_credentials`, and `channel_descriptor`
now embeds the SFTP username+password (never the private key/passphrase — a
password is carried, never a key). The token is now a **bearer credential**,
made safe by *scoping*, not by hiding (you cannot hide a credential from the
party whose software must use it):

- The credential is for a **dedicated SFTP account chroot-jailed to the
  `/relay` folder only** — no shell, no access to anything else on the
  server. A leaked token's worst case is read/write to the shared relay
  dropbox (annoying, wipeable), never the server or other data. That is
  exactly the trust already extended by sharing a board.
- Third-party protection is **out-of-band trusted delivery** of the token
  (copy-paste over a channel you trust), same posture as any "secret link" —
  encrypting the blob buys nothing, since the decryption key would have to
  travel a more-secure channel to the same recipient.
- **Operational precondition — [VERIFIED].** The jailed account must be
  unable to traverse above `/relay` (some panels set a "default directory"
  that is a hint, not a hard jail). Confirmed on the IONOS shared-hosting
  account in use: a dedicated SFTP user was created scoped to `/relay`, and
  `cd ..` cannot climb out of it (a true chroot — the jail root appears as
  `/`). The credential model's safety rests on this, and it holds.

Consistent with the "one personal server, trusted peers" threat model the
storage layer already documents. Not scoped into any current implementation
work — captured here as the agreed target.

---

## 2. Implementation detail — where, what, why

### `session.py`

| Item | Status | What | Why (result when it runs) |
|---|---|---|---|
| `peer_identity_key: dict[str, str]` | **DONE** | `Session.__init__` attribute, `addr -> identity_key`. Persisted via `_session_metadata` (see app_server row below) | The canonical registry this whole doc is about. Read by transport (exclusivity/reconnect-replace) and relay (redundancy) — one source instead of independent content re-derivations. |
| `set_peer_identity_key(addr, identity_key)` | **DONE** | Single writer entry point; last-write-wins, no conflict resolution (matches the "identity is an assertion, not collaborative content" policy `apply_peer_identity_snapshot` already documents); traced (`session.set_peer_identity_key`) | Every code path that learns an address's identity funnels through the `apply_peer_subtree` choke point (root-node `is_identity_node` check), which calls this — covering connect-token snapshots, direct profile pulls, and relay discovery with one hook. The fact is recorded the moment content carrying it first arrives, decoupled from the rest of the caching machinery. |
| `addresses_for_identity(identity_key) -> list[str]` | **DONE** (replaces the proposed `preferred_address_for_identity` shape) | Sorted list of all addresses currently mapped to that key | Callers apply their own filter (reconnect-replace: everything except the new address; relay: non-relay AND currently in `members`) — cheaper and more flexible than baking one preference order into Session. |
| `remove_peer(peer_addr) -> None` | **DONE** | Full teardown of one peer's *registration*: `members`, `peer_topic_sets`, `peer_fetch_topic_sets`, `peer_topics`, `peer_perspectives`, `peer_status`, `peer_sync_state`, `peer_channel` | **Deliberately does NOT clear `peer_identity_key`** (revising this doc's earlier draft): the registry is knowledge ("this address belongs to identity X"), which stays true after teardown. Clearing it here would erase the very evidence relay's redundancy check reads on later polls — the self-erasing-evidence flip-flop, one level up. Reconnect-replace forgets superseded addresses explicitly instead. |
| `find_peer_address` / `find_direct_peer_address` | **REMOVED** | The interim content-walking lookups built earlier this session | Fully superseded by the registry; deleted outright rather than kept as fallbacks. (`find_peer_identity`/`peer_identity` remain — they return identity *data* nodes for the app layer, a different job.) |

### `app_server.py`

| Item | Status | What | Why |
|---|---|---|---|
| `accept_connect_token` exclusive channel selection | **DONE** | Tries `http` candidate first (the join call doubles as the reachability probe); falls back to `relay`/`sftp` only if `http` absent/self-addressed/failed; never both | Guarantees exactly one channel per peer from the moment of connection — the invariant the rest of this doc assumes. |
| Reconnect replaces old channel | **DONE** | On accept, loops over `addresses_for_identity(identity_key)` and, for every address other than the newly selected one, calls `remove_peer`; additionally pops the registry entry **only for non-relay (real) addresses** | "Closed before initiating a new one" — a fresh token supersedes a dead direct address, forgetting it from the registry. A `relay:<id>` pseudo-address is deliberately exempt from the pop: it's a *concurrent* channel (only ever one per identity, never actually superseded), and its registry entry is what `_is_redundant_relay_peer` reads to keep it suppressed on every later poll. **Live regression:** when relay discovered the peer *before* the http connect finished, popping `relay:<id>`'s key here reopened the duplicate permanently (relay's `applied` bookkeeping never re-applies the unchanged identity topic to re-teach the key). |
| Identity assertion timing | **DONE** (via the choke point, not a separate call) | `accept_connect_token`'s existing `apply_peer_identity_snapshot(selected_addr, identity)` lands in `apply_peer_subtree`, whose root-node hook records the registry entry | No separate `set_peer_identity_key` call needed at accept time — the snapshot already carries the identity inline, so the fact is recorded before any topic sync begins. |
| Registry persistence | **DONE** | `_session_metadata` saves `peer_identity_key`; `_restore_session_metadata` restores it verbatim (str→str sanity filter only, deliberately NOT filtered by membership) | `peer_perspectives` is never persisted, but relay's `applied` hash bookkeeping is — after a restart, an unchanged identity topic never re-applies, so a lost mapping could never be re-learned from content. A suppressed relay address lives in no other restored structure; its registry entry is exactly what keeps it suppressed across restarts. Safe under the "never persist a flag for state that isn't itself persisted" rule: this is a plain fact about an address, not an "already synced" claim about non-persisted cache. |
| Token expiration | **DEFERRED, not MVP** | Would be App-set (at token generation) `expires_at`, refreshed on A's normal publish cycle — same pattern `write_presence` already uses | Purely writer-side housekeeping (bound how many relationships a session keeps publishing to forever) — no reader-side protocol change at all, since letting it lapse just means B observes A through the *existing* staleness/offline machinery, same as a real outage. Doesn't interact with `peer_identity_key` or channel exclusivity, so it's safely independent of the rest of this doc — noted so it isn't forgotten, not scheduled. |

### `relay_logic.py`

| Item | Status | What | Why |
|---|---|---|---|
| `_is_redundant_relay_peer` over the registry | **DONE** | Pure lookup: `peer_identity_key.get(relay_addr)` → redundant iff any *other, non-relay* address with the same key is *currently in `session.members`*. The persisted `redundant_peers` set and the per-call `checked_this_call` memo (both interim fixes from earlier this session) are deleted; `_load_state` no longer knows a `redundant_peers` field | Cheap and side-effect-free enough to just re-run per (topic, peer) every poll. The evidence (registry entry) survives `remove_peer`, so no stickiness bookkeeping is needed. The `members` liveness condition is an improvement over the persisted verdict: if the direct peer is later removed, relay *resumes* on the next poll for newly published hashes — previously suppression held forever even with no direct channel left. (Unchanged hashes don't re-apply after resume — `applied` bookkeeping still short-circuits them — acceptable under manual-reconnect MVP, where a fresh token re-delivers identity inline anyway.) |
| Recipient-scoped `relay_topic_uuids()` | **PROPOSED** | Instead of "every board I own + my identity," compute per-relationship what's relevant to publish | Closes the privacy gap in §1.4 — a peer relationship no longer implicitly grants visibility into every other board you own. |
| Nugget write/read | **PROPOSED** | New per-relationship summary file, read before any per-topic `head.json`/snapshot fetch | One cheap read covers a whole relationship instead of one `read_head()` per topic per peer; room to add ack/last-message later. |

### `relay_storage.py`

| Item | Status | What | Why |
|---|---|---|---|
| Layout: `topics/<uuid>/peers/<peer_id>/{head.json, snapshots/}` | **current** | Topic-first, one head per (topic, publisher), no recipient scoping | This is *why* scoping isn't possible today without a bigger storage change — the path has no concept of "who this is for." |
| Layout: `identities/<peer_id>/for/<recipient_identity_key>/{nugget.json, topics/<uuid>/snapshots/<hash>.json}` | **[PROPOSED]** | Publisher-first, then recipient-scoped | A poller only ever looks under `for/<their own identity_key>/` — content not addressed to them is invisible without needing access control, just by not being where they'd look. **[OPEN]**: this is a breaking storage-format change; needs either a migration or an explicit "pre-1.0, fine to wipe and restart" call. |

### `kanban_logic.py`

| Item | Status | What | Why |
|---|---|---|---|
| `join_discussion` type-sniffing (`_is_kanban_board_topic`/`_is_shared_user_topic`) | **current** | Classifies fetched topics by `data.type`, rejects anything else, only grafts board topics | Exists because identity currently travels through the *same* generic `topic_uuids` fetch pipeline as board content — the app layer has to sort them out itself. |
| Dedicated identity-data sync path | **[PROPOSED]** | New method (e.g. `sync_identity_data()`), called from the existing update loop, that unconditionally takes the newest identity data per known peer — no divergence classification, no adopt-tools UI, ever | Implements the "always publish own, always adopt peer's, no judgement" policy agreed in §1.2, distinctly from board's selective per-node auto-adopt. |
| `join_discussion` type-sniffing | **Stays as-is, decided** | No wire-format merge of identity_key and identity data — kanban_logic keeps sorting board topics from identity-data topics itself | Identity data is app-defined content; the protocol layer has no business knowing its shape, so this sorting genuinely belongs at the app layer, not something to push down into transport. Not a gap to close — a deliberate boundary. |

---

## 3. Decisions considered and dropped

- **Ack-gated, one-message-per-poll-cycle ordering.** Proposed as a way to
  get reliable, orderly communication over relay: don't publish the next
  thing until the last one is acknowledged. Dropped — the sync model here
  is hash/state-based, not message-based, and already converges correctly
  regardless of the order changes are observed in (that's the point of
  `state_hash` reconciliation). Ack-gating would only add latency (a full
  poll round-trip per update, minimum) and a new stall failure mode (a lost
  ack blocks everything after it) without fixing anything actually broken.
  Would be worth revisiting *only* if a genuinely message-shaped feature
  shows up later (chat, comments, notifications) where sequence itself
  carries meaning — not for board/identity state sync.

## 4. Implemented publication sequencing and polling cadence

- Every publisher persists one monotonically increasing `publication_seq`
  per topic. Every relay head and snapshot carry that generation.
- A semantic publication becomes the acknowledgement target. Later
  acknowledgement-only heads retain that target but do not request an
  acknowledgement of themselves, preventing ack-of-ack loops while still
  allowing late peers to confirm the content they actually fetched.
- `observed_publications` reports the exact semantic publication fetched by
  each peer. Node-revision observations remain authoritative for adoption
  and divergence; publication sequences provide transport diagnostics.
- Regular polling advances from its configured cadence. Slow I/O skips
  consumed slots, and local-edit wakeups do not move the regular deadline.
  The conservative response estimate is acknowledgement timing only.
- `SOVEREIGN_TRACE=events` records semantic changes and failures;
  `SOVEREIGN_TRACE=timing` additionally records successful relay cycles and
  phase durations. `off` disables tracing, while legacy `1`/`true` values
  map to `events`.

## 5. Open questions

1. **Resolved, and deferred out of MVP entirely.** Expiration needs no
   protocol-level handling at all: `expires_at` would just be another
   self-published, periodically-refreshed value (`write_presence`'s
   pattern), read-only to everyone but its owner. When A stops refreshing
   it, B observes A through the *existing* staleness/offline machinery —
   the same thing that already happens for a genuine network outage, not a
   new "expired" event or state. Nothing changes for already-synced
   content either way — going stale has never rolled back what was already
   pulled in, for direct or relay peers alike, and expiration doesn't
   change that. Since it needs no new mechanism and doesn't interact with
   `peer_identity_key` or channel exclusivity, it's out of scope for now —
   noted for later, not scheduled.
2. **Resolved** — wipe and restart. No migration path needed for the
   `relay_storage.py` layout change; this is pre-1.0 test data.
3. **Resolved — decided against.** Identity *data* is app-defined content
   and varies by app; it must not be mixed with the protocol's own
   `identity_key`. Kanban keeps treating boards and identity-data topics
   differently in `join_discussion` — that sniffing is a deliberate app-layer
   boundary, not a gap to close. See §1.2/§2.
