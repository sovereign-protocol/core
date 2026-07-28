# Multi-client identity and the common base — design doc

One user is currently one client. There is no supported way to reach the
same environment from a second client, even two windows on the same
machine. This doc works out what it takes to lift that, starting from the
observation that the interesting unit is not "a second peer" but **a common
base**: a replicated substrate every client of one user converges on, from
which relationships, channels and content are projected.

Status tags per item: **[DONE]** (implemented and tested), **[PROPOSED]**
(agreed in design discussion, not yet built), **[DEFERRED]** (agreed, not
needed for the current scope, kept only as a note for later), **[OPEN]**
(raised, not yet resolved). Nothing in this doc is built yet; the **[DONE]**
tags below refer only to pre-existing machinery this design reuses, and say
so explicitly.

This doc assumes `DESIGN_IDENTITY_AND_TRANSPORT.md` and does not restate
it. In particular its §1.2 identity-key/identity-data split, §1.3 channel
exclusivity, and §1.4 relay specifics are load-bearing here.

**Note on stale references in that doc.** Its §1.3 describes token
handling in terms of `accept_connect_token` and
`collect_channel_descriptors` in `app_server.py`. Neither exists any more —
the logic moved into `ChannelManager` (`channel.py`) as `compose_token`
(line 138), `accept_token` (192) and `accept_invitation` (204), which is
the relocation that doc listed as **[OPEN]**. The semantics moved too, in
two ways that matter here and are described in §1.2 and §1.6 below: a token
now carries **exactly one** channel chosen by the *inviter* rather than a
candidate list the accepter ranks, and reconnect-replace is **topic-scoped**
rather than a full peer teardown. This doc cites the current names.

## 1. Protocol, high level

### 1.1 What "one client = one user" actually is

Not a policy, an accident of three independent facts:

1. `identity_key` is `str(uuid4())`, minted lazily per Session
   (`session.py`, the `identity` property). A second client is a second
   Session and therefore a second, unrelated identity.
2. The peer graph — who you know, over which channel, for which topics —
   lives in session metadata, not in the protocol tree (§1.5). Nothing
   syncs it, because the sync machinery only moves `ProtocolNode` trees.
3. Channel exclusivity: a token carries exactly one channel, and accepting
   one for a known identity unbinds that identity's other addresses from
   the covered topics (`accept_invitation`, channel.py:204).

Fact 1 means a second client is a stranger. Fact 2 means even if it
weren't, it would arrive with an empty address book. Fact 3 means that if
both clients did know Alice, they would take her topics from each other.

### 1.2 Why one shared `identity_key` cannot be the answer

The obvious move — copy the `identity_key` to client 2 so both are "the
same user" — is the one thing this design must not do. It breaks three
separate mechanisms, all silently, and two of them lose data.

**It inverts divergence classification.** `_analyze_transition_node`
(session.py:1962) resolves `local_identity = self._local_revision_origin()`
and `peer_identity = self.peer_identity_key.get(peer_addr)`, then passes
both to `_classify_content` (2274) or `_classify_node` (2341). The
comparison tests, in order:

```python
if origin and origin == local_identity:
    return "local_made_changes"
if origin and origin == peer_identity:
    return "peer_made_changes"
```

With one shared key both comparisons are true and the first wins, so every
genuine edit arriving from client 2 classifies as `local_made_changes` and
is discarded. Sync reports healthy and does nothing.

**It collides the ordering primitive.** `revision_seq` is documented in
`protocol.py` as an "origin-local logical revision number... orders
successive revisions from the same author without comparing wall clocks",
and `local_revision_seq` is per-Session state persisted by
`_session_metadata`. Two clients sharing an origin each advance that
counter independently, so two distinct edits both publish as
`(origin=X, seq=7)`. The sequence comparator runs *before* the base-hash
checks and returns a confident wrong order rather than falling through to
`divergence`. That is silent data loss, not a rough edge.

**It makes third parties evict your other client.** `accept_invitation`
(channel.py:204) loops `addresses_for_identity(identity_key)` and, for
every address that is not the newly selected one, calls
`remove_peer_topics(old_addr, invitation.topic_uuids)`. Alice accepting a
token from client 2 reads it as client 1 having moved address and unbinds
client 1 from exactly the topics the token covers.

This is narrower than the wholesale `remove_peer` teardown
`DESIGN_IDENTITY_AND_TRANSPORT.md` §1.3 describes — the current code is
deliberately topic-scoped, "a new direct route for one topic cannot erase
an SFTP route for another". Narrower, but still wrong for siblings, and
wrong in the worst way: the two clients silently trade the shared topics
back and forth on every reconnect, each one working perfectly at the moment
you look at it.

**Underneath all three: `revision_origin` *is* the `identity_key`.**
`_local_revision_origin` returns the profile node's `identity_key`
(session.py). So `identity_key` is doing double duty — peer bookkeeping
*and* CRDT author id. An author is a **client**, not a person. That is the
whole argument for the split in §1.3: the layer that needed a per-person
identifier was never the layer that owns `identity_key`.

### 1.3 Two-level identity — `account_key` and `identity_key`

**[PROPOSED]**

- **`identity_key`** — unchanged. Per-client, unique, opaque, still the
  CRDT author id via `revision_origin`, still the unit of channel
  exclusivity and peer dedup. Nothing that reads it today changes.
- **`account_key`** — new. Stable per *user*, shared by every client of
  that user, copied to a new client at pairing. Never an author id, never
  a channel key. Its only job is to answer "is this client mine".

Two clients are siblings iff `account_key` matches and `identity_key`
differs. That second clause matters: equal `identity_key` means the *same*
client (a restored session), which is a different situation and must not
be confused with a sibling.

The registry shape already anticipates this. `addresses_for_identity`
returns a **list**, and `peer_identity_key` is explicitly documented as
knowledge rather than registration. `addresses_for_account(account_key)`
is its sibling, and `peer_account_key: dict[str, str]` its registry, with
the same single-writer discipline `set_peer_identity_key` already
establishes.

**Third parties learn both.** A connect token and an identity snapshot
carry `account_key` alongside `identity_key`. Alice's session then holds
N clients under one account, which is what makes §1.6's exclusivity
carve-out expressible rather than a special case buried in accept logic.

**[OPEN] How `account_key` is minted and paired.** Candidates: a bare
uuid4 copied by QR/paste (symmetric with how connect tokens already
travel out-of-band); or a keypair with the public half as the account
identifier, which buys signature-verifiable sibling claims. The bare uuid
matches the existing threat model and the `identity_key` precedent; the
keypair is the only version that survives an untrusted relay. Not
resolved. Note this is the same "secret link" posture already accepted for
token credentials in `DESIGN_IDENTITY_AND_TRANSPORT.md` §1.6 — the
argument there applies unchanged, but the blast radius is larger, because
an account key is the whole environment rather than one relay dropbox.

### 1.4 The common base

**[PROPOSED]** The common base is the replicated state every client of one
account converges on. It is deliberately *not* modelled as a peer
relationship:

| | Peer relationship | Common base |
|---|---|---|
| Parties | Two different people | One person, N clients |
| Topic scope | Only invited topics (`peer_topic_sets`) | Everything the account owns |
| Conflict story | Divergence, surfaced for a human decision | Same, but rarer and always resolvable by one person |
| Channel | One per token, chosen by the inviter | One per account, outside that selection |
| Relationships | The thing being scoped | The thing being replicated |

The framing earns its keep on the third row of §1.1. "Which client owns the
Alice channel" has no good answer while clients are peers. With a common
base the question dissolves: the *account* owns the channel, its state
lives in the base, and whichever client is running projects it. Ownership
stops being a race and becomes a lookup.

### 1.5 The peer graph is not in the protocol tree

**This is the central finding of this doc, and the bulk of the work.**

`save_session_to_file` (app_server.py) writes exactly two things — a
`protocol_root` snapshot and a `session` blob built by `_session_metadata`:

```
local_revision_seq, members, active_topic_uuids, peer_topics,
peer_topic_sets, peer_fetch_topic_sets, peer_status, peer_identity_key,
peer_topic_channel, observed_topics, app_metadata
```

None of those are `ProtocolNode`s. They carry no `content_hash`, no
`revision_origin`, no `revision_seq`, and are invisible to divergence
classification. The adopt/merge machinery operates on trees, so **it cannot
move any of this today.** `peer_topic_channel` is literally the
address→topic→channel assignment map, and `peer_topic_sets` is the sharing
scope — precisely the state a second client needs and precisely the state
the protocol cannot carry.

So "reuse the protocol calls" holds for *content* and not at all for
*relationships*. Closing that gap is the actual project.

**[PROPOSED] Promote the peer graph into the tree.** A Core-owned synced
topic holding the account's relationships as protocol nodes. The precedent
exists and is one call — `Session.__init__` already registers the Core
profile as a Core-owned shared topic:

```python
self.shared_topics.register(
    "Sovereign Core profile",
    {"shared_user_profile"},
    lambda: [self.identity],
    self.accept_profile_invitation,
    assignment_scoped=False,
    mount_invitation=False,
)
```

An account topic is the second such registration, with the same
`assignment_scoped=False, mount_invitation=False` shape — it is the
account's own state, never grafted from an invitation. Once it is tree
state it gets content hashes, sequencing, divergence classification, relay
publication and adoption for free, and the reuse claim becomes literally
true rather than mostly true.

### 1.6 The `devices` channel

**[PROPOSED]** A new channel kind carrying the common base between
siblings. `ChannelManager.register` enforces a unique `kind` and one owner
per descriptor type, so it slots in beside `http` and `relay` without
disturbing either. It must implement the `Channel` contract
(`offer_descriptor`, `accept_descriptor`, `attach_topics`,
`detach_topics`, `status`, `close`) and, if drop-box backed, the
`PollingEndpoint` four (`has_active_relationship`, `write_presence`,
`publish_due_topics`, `poll_and_apply`).

**The exclusivity carve-out is mandatory, not cosmetic** — but it is
cheaper than `DESIGN_IDENTITY_AND_TRANSPORT.md` §1.3 implies, because the
code no longer negotiates. Both sides now require exactly one channel per
token: `compose_token` rejects anything but a single entry in
`channel_options` ("select exactly one channel for the invitation"), and
`accept_invitation` rejects a token whose descriptors resolve to anything
but one candidate ("token must select exactly one channel"). The *inviter*
chooses; there is no preference order left to poison.

So the carve-out is two guards rather than a rewrite of a ranking:

- `compose_token` must refuse `devices` in `channel_options` — today it
  validates only that the kind is *registered*, so a caller could select it
  and mint a peer token carrying the sibling channel.
- `accept_invitation` must not admit a `devices` descriptor as a candidate.
  Registration alone currently qualifies it, via `_descriptor_owner`.

Both are one condition each, provided channels can declare which axis they
belong to (§4.1). What must not happen is leaving it implicit: a devices
descriptor reaching a peer token is a routing bug that would look like a
working connection.

Backing it with the existing SFTP/local relay storage is the obvious first
implementation: it is poll-based, already survives NAT, and §1.4's "publish
everything I own under my identity tag" — a privacy gap for real peers — is
exactly correct behaviour between a user's own clients. The recipient
scoping proposed there needs an explicit own-account exemption, or it will
break sibling sync when it lands (§4.3).

### 1.7 Adoption policy — primitive first, safely

**[PROPOSED]** First cut: no merge, no three-way, no sibling-specific
resolution. Siblings run the *existing* classification and the existing
divergence UI, relabelled ("your other client changed this" rather than a
peer's display name).

This is safe to keep primitive **because** §1.3 keeps `identity_key` and
therefore `revision_origin` per-client. Distinct origins mean distinct
`revision_seq` counters, so the collision in §1.2 never arises and
concurrent sibling edits land in `divergence` — crude, visible, lossless.
Sophistication can follow later without a wire change.

**[PROPOSED, and argued against] Always auto-adopt.** Tempting — same
person, therefore no real conflict. The reasoning does not survive contact
with the offline case: two people coordinate socially before editing the
same board, one person on two clients does not, especially offline. Blanket
auto-adopt silently discards one side of exactly the edits the user
knowingly made on both, and unlike a peer divergence there is no prompt to
notice. Note also that the strict fast-forward case — the only genuinely
conflict-free one — is *already* detected and applied as
`peer_made_changes` (`local.content_hash == peer.base_hash`), so blanket
auto-adopt buys essentially nothing except the dangerous cases.

The one place always-adopt is already used is identity *data*, and
`DESIGN_IDENTITY_AND_TRANSPORT.md` §1.2 is explicit that this is
"categorically different from how a board is synced". Extending it to
content is the generalisation that doc declined to make; this doc declines
it too.

### 1.8 Publishing must be an invariant, not a discipline

**[PROPOSED]** "Always publish relevant information to the common base" as
a rule every call site must remember will rot. The evidence is in this
repository's own history: relay-applied peer content "only ever reached the
cache; adoption ran solely from UI polls... it never adopted (and never
republished merged)" — one missed publish path, months of confusing
behaviour.

Invert it. The account topic becomes the **source of truth** for the peer
graph; `peer_topic_sets`, `peer_identity_key`, `peer_topic_channel` and
friends become an in-memory **projection** rebuilt from it, not state
anyone writes directly. Publishing then is not something to remember — it
is the only way to mutate.

This is the pattern the codebase already names. `set_peer_identity_key` is
"the single writer for the addr → identity_key registry — every channel
learns the fact... never re-derived from cached content on demand". Same
shape, one level up: single writer into the tree, everything else derived.

## 2. Implementation detail — where, what, why

### `session.py`

| Item | Status | What | Why |
|---|---|---|---|
| `account_key` in the Core profile node | **PROPOSED** | New field beside `identity_key` in the `shared_user_profile` schema; bumps `CORE_PROFILE_SCHEMA_VERSION` (currently 1) | The only place a stable per-user identifier can live such that it travels with every existing identity snapshot, connect token and relay identity publication — no new transport path needed. |
| `peer_account_key: dict[str, str]` + `set_peer_account_key` | **PROPOSED** | `addr -> account_key` registry, single writer, recorded at the `apply_peer_subtree` choke point exactly as `peer_identity_key` already is | Lets a third party group N addresses under one account without re-deriving from content — the same argument §1.2 of the identity doc makes for `peer_identity_key`, one level up. |
| `addresses_for_account(account_key) -> list[str]` | **PROPOSED** | Sibling of `addresses_for_identity` | Needed by the exclusivity carve-out (§1.6) and by any "my other clients" UI. |
| `is_sibling(addr) -> bool` | **PROPOSED** | `peer_account_key.get(addr) == own account_key and peer_identity_key.get(addr) != own identity_key` | The second clause is load-bearing: equal `identity_key` is the *same* client restored, not a sibling. |
| `_local_revision_origin` | **UNCHANGED, deliberately** | Keeps returning `identity_key` | `revision_origin` is the CRDT author id and an author is a client. Making it account-scoped is exactly the §1.2 collision. Called out here so a later refactor does not "simplify" it. |
| Account topic registration | **PROPOSED** | Second `shared_topics.register(...)` with `assignment_scoped=False, mount_invitation=False`, mirroring the Core profile registration | Account state is the account's own, never grafted from an invitation — the same two flags the profile already sets, for the same reason. |
| Peer graph as projection | **PROPOSED** | `peer_topic_sets`, `peer_fetch_topic_sets`, `peer_topic_channel`, `peer_status`, `members` rebuilt from the account topic rather than written directly | §1.8. The single largest change in this doc, and the one that makes "always publish" structural. |

### `app_server.py`

| Item | Status | What | Why |
|---|---|---|---|
| `_session_metadata` / `_restore_session_metadata` | **PROPOSED** | Peer-graph fields move out of the metadata blob and into `protocol_root`; the blob keeps only genuinely client-local state (`local_revision_seq`, `observed_topics`, `active_topic_uuids`) | The envelope split *is* the design. `local_revision_seq` in particular must stay client-local — it is the per-origin counter, and syncing it would reintroduce §1.2's collision by the back door. |
| `SESSION_ENVELOPE_VERSION` | **PROPOSED** | 1 → 2, with in-place upgrade: read a v1 blob, seed the account topic from it, save v2 | Precedent exists — `_session_envelope_error` already accepts `protocol_schema_version` 1 or 2 and upgrades v1 nodes in place on next save. Same trick, one level up. **[OPEN]**, see §4.2. |

### `channel.py` / new `devices_channel.py`

| Item | Status | What | Why |
|---|---|---|---|
| `DeviceChannel(kind="devices")` | **PROPOSED** | Implements `Channel`; `PollingEndpoint` if drop-box backed | `ChannelManager.register` already enforces unique kind and one owner per descriptor type, so the abstraction takes it without modification — one of the few parts of this design that costs nothing. |
| `compose_token` refuses `devices` | **PROPOSED** | Reject a sibling kind in `channel_options` rather than only validating that it is registered | §1.6. A devices descriptor in a peer token is a routing bug that presents as a working connection. |
| `accept_invitation` ignores `devices` descriptors | **PROPOSED** | Exclude the sibling kind when building `candidates`; `_descriptor_owner` currently qualifies any registered kind | Same guard from the accepter's side. Also the natural place to record `account_key` from the inline identity snapshot. |
| `accept_invitation` sibling exemption | **PROPOSED** | Skip the `remove_peer_topics` reconnect-replace loop when the address is a sibling | Without it, §1.2's third breakage returns the moment two clients of *the peer's* account connect to you — the clients trade topics on every reconnect. |
| Exclusivity axis separation | **OPEN** | Either a `Channel` flag (`peer_selectable: bool`) read by both guards above, or an explicit allow-list in `ChannelManager` | Today "which channels compete" is implicit in `_descriptor_owner` plus the exactly-one rule. Making the axis explicit is two conditions now and a refactor after a third axis appears. §4.1. |

### `relay_logic.py` / `relay_storage.py`

| Item | Status | What | Why |
|---|---|---|---|
| Own-account publication scope | **PROPOSED** | Publish *all* owned topics plus the account topic to the sibling root, ignoring `peer_topic_sets` | Siblings need everything, including boards shared with nobody. This is the current unscoped behaviour, so it needs no work now — but it stops being free the moment recipient scoping lands. |
| Recipient-scoped publishing exemption | **OPEN** | The `[PROPOSED]` recipient scoping in `DESIGN_IDENTITY_AND_TRANSPORT.md` §1.4 must carve out own-account publication | If scoping ships first without this, sibling sync silently narrows to nothing. Sequencing dependency, not a design conflict. §4.3. |
| `has_active_relationship` | **PROPOSED** | Must return true when a sibling relationship exists, independent of any peer token | The relay loop is gated on it. A user with two clients and no peers must still sync — today that state writes nothing at all. |

## 3. Decisions considered and dropped

- **Clients as ordinary peers with an `is_own_device` flag.** The first
  framing tried. Dropped: it leaves "which client owns the Alice channel"
  unanswered, because the flag changes adoption policy without changing
  who holds the registration. The common base dissolves the question
  instead of answering it (§1.4), which is why it is worth the larger
  change in §1.5.

- **One shared `identity_key` per user.** Dropped for the three concrete
  breakages in §1.2. Recorded at length rather than merely rejected,
  because it is the obvious first idea and two of its three failure modes
  are silent.

- **A primary client that owns all channels, siblings as thin clients.**
  Genuinely simpler and needs no protocol change — the UI is already
  framework-free and server-driven, so a second client is a WebView
  pointed at the first (only `bind_host`, pinned to `LOOPBACK` in
  `desktop.py`, stands in the way). Dropped as *the* answer because it
  gives up offline capability on every client but one, which is the
  project's whole thesis. **Kept as a legitimate interim**: it is days of
  work, it solves "I want the board on the laptop while the desktop is
  running", and it buys time to land §1.5 properly. Explicitly not a
  migration path — the two designs share no state.

- **Blanket always-auto-adopt between siblings.** See §1.7. Dropped on the
  offline-edit case, and because the conflict-free path already
  auto-applies.

## 4. Open questions

1. **How is the exclusivity axis expressed?** §1.6 requires the devices
   channel to sit outside peer channel selection, in both `compose_token`
   and `accept_invitation`. A `peer_selectable` flag on the `Channel`
   protocol is the smaller change and keeps the knowledge with the channel;
   an allow-list in `ChannelManager` is the more auditable one and keeps it
   in one place. Note `DESIGN_IDENTITY_AND_TRANSPORT.md` §1.3 listed the
   relocation of this logic out of `app_server.py` as **[OPEN]** — that has
   since happened, so the question is now only about the axis, not the
   location.

2. **Migration, or wipe?** Promoting the peer graph to tree state is a
   breaking session-envelope change. `DESIGN_IDENTITY_AND_TRANSPORT.md`
   §5.2 resolved the analogous relay-layout question with "wipe and
   restart, this is pre-1.0 test data". The same call is available here
   and is much cheaper than an upgrade path — but session files now hold
   real boards, which relay storage did not. Needs an explicit decision,
   not an assumption.

3. **Sequencing against recipient-scoped publishing.** §1.6 and the
   `[PROPOSED]` scoping in the identity doc touch the same publish path in
   opposite directions. Either lands cleanly first if the other knows
   about it; neither survives being retrofitted blind.

4. **Does `account_key` need to be a keypair?** §1.3. A bare uuid matches
   the existing out-of-band-token posture, and the relay dropbox is
   already trusted with board content. But an account key grants an entire
   environment rather than one relationship, and a leaked one is not
   revocable without re-keying every client. The threat model that
   justified bearer tokens does not obviously stretch this far.

5. **What is a sibling allowed to do?** This doc treats all clients of an
   account as equal. Nothing here supports revoking one — a lost phone
   keeps its `account_key` and keeps syncing. Revocation needs either a
   keypair (question 4) or an account-topic membership list that the
   remaining clients agree on, which is a consensus problem this protocol
   has deliberately avoided elsewhere. Not scoped; noted because "add a
   client" and "remove a client" are usually specified together and only
   the first is designed here.
