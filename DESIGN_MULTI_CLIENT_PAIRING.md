# Multi-client pairing — design doc

One user, several clients — desktop, laptop, a second window — reaching the
same environment. A client is paired by importing one token carrying the
**topic–identity–channel** relationship it needs. From that moment it is a
copy of its siblings: same client id, same topics, same channel, publishing
into the same place. Nobody outside can tell how many machines are behind it.

The whole design rests on one constraint that peers do not have and siblings
do: **a person works at one client at a time.** Two people genuinely edit the
same board at the same moment and must be reconciled node by node. One person
does not. That single fact collapses sibling synchronisation from a per-node
merge problem to a topic-level question with two answers.

Status tags: **[DONE]** (built and tested), **[PROPOSED]** (decided here,
not built), **[OPEN]** (unresolved). Sections 1 to 4 are implemented; the
open questions in section 8 are not.

This doc assumes `DESIGN_IDENTITY_AND_TRANSPORT.md`. It is an alternative to
`DESIGN_MULTI_CLIENT_IDENTITY.md`, which makes each client a peer of its
siblings under a shared `account_key`; this one makes them indistinguishable
from outside and reuses one publication identity. §7 compares them.

## 1. The pairing token

**[DONE]** A token kind carrying what a client needs to take part, and
nothing else (`RelayManager.compose_pairing_token`):

| Field | What |
|---|---|
| `client_id` | The relay peer id every client of this user publishes under |
| `channel` | One channel descriptor, as today |
| `topic_uuids` | Everything the account owns, not a selected subset |
| `profile` | The account's `shared_user_profile` subtree |

It is deliberately not a connect token with extra fields. `compose_token`
(channel.py:138) and `accept_invitation` (204) treat what they handle as a
relationship with *another person*; accepting a pairing token through that
path registers your own laptop as a stranger and then trips the
reconnect-replace loop, which unbinds an identity's other addresses from the
covered topics. Your laptop would evict your desktop from its own boards.

**[DONE]** Pairing therefore has its own compose/accept pair, and the peer
path refuses a pairing token (`accept_token`, channel.py). Leaving that implicit
is the failure to avoid: a pairing descriptor reaching a peer token is a
routing bug that presents as a working connection.

**[OPEN] How the token travels.** Same posture as connect tokens — out of
band, QR or paste. The blast radius is larger: a connect token grants one
relationship, a pairing token grants the whole environment.

## 2. The rule

A client holds two facts about each topic, both local:

- `published[topic_uuid]` — the state hash it last put on the relay.
- `current_hash` — the state hash it holds now.

And it can read one fact from the relay: what the slot holds.

**[DONE]** The entire sibling protocol:

```
relay == current          →  already in agreement; nothing to do
relay == published        →  nothing happened; publish if current differs
relay != published
    and current == published  →  a sibling built on my published state.
                                 Take it.
    and current != published  →  a sibling built on something older than
                                 what I hold. ALARM.
```

The first line is a definition rather than an optimisation: if the slot holds
exactly what this client holds, nothing has diverged, whatever the local
record says about who published it. Both clients hold the account profile
identically from the moment they pair, so without it the client that had not
recorded publishing it alarms over content nothing changed — and stops
syncing that topic.

The second line is the one that makes this work. If everything I had was
published, then whatever a sibling wrote was written on top of it — they
could not have started anywhere else, because there was nowhere else to
start. Taking it loses nothing. This is a fast-forward established from two
local facts, without inspecting a single node.

The third line is the plane case. I hold work the relay never saw, so the
sibling's state descends from an older version of mine. There is no correct
automatic answer, and the human is the only one who knows which side matters.

### 2.1 Why the peer machinery does not apply

`_classify_content` (session.py:2304) reasons per node about ancestry because
peer reconciliation is **selective**: Alice adopts Bob's card and declines his
column deletion, so each node needs its own verdict. Siblings never select.
The question is not "which of these nodes do I want" but "did my own work
reach the relay before someone continued from it", and that is one question
per topic, not one per node.

This also removes the hazard that `revision_origin` sharing would otherwise
create. `_same_origin_sequence_order` (session.py:2282) runs before every
base-hash test and treats a higher `revision_seq` under the same origin as
proof of descent — sound for one author, false for two clients advancing
independent counters, and the failure is silent. §2's gate performs exactly
the check that comparator was inferring, so with the gate in front the
classifier is no longer the thing deciding. See §6 for what remains.

## 3. Publishing, presence, and doing nothing

**[DONE]** `publish_due_topics` compares the topic's current state hash
against what it last published and skips when they match. A client with
nothing new writes no content. There is no "active client" mode to build and
no flag to track: *activity is not a state, it is just having changes.*

**[DONE]** `write_presence()` runs every poll tick regardless. That is
precisely the "I am here and I have nothing to say" signal this design wants,
and its own docstring gives the reason: `head.json`'s `updated_at` cannot
distinguish "nothing to publish" from "stopped running"; presence can.

**[DONE]** `published[topic_uuid]` is verified against the relay before it is
trusted (`_relay_holds_our_publication`, relay_logic.py:851). This began as a
fix for wiped relays, where a peer sat on a stale local flag and republished
nothing. Under this design it is load-bearing: §2's whole decision rests on
that value being a fact about the relay rather than a local claim about it.

## 4. What has to change

### 4.1 Poll before publish

**[DONE]** The tick now runs `write_presence` → `poll_and_apply` →
`publish_after_poll`. It previously published first, which is harmless when
each peer owns its own slot and inverts the safety property when one is
shared:

- The laptop holds unpublished plane work, so `current != published`. Its
  first tick in the office writes that state over the desktop's work before
  it has looked at anything. It then polls, finds its own state on the relay,
  and sees nothing wrong.
- The desktop polls later, finds the relay differs from its published state,
  checks itself, finds `current == published` — it published everything — and
  by §2 correctly concludes the change is safe to take. It adopts the plane
  state and discards the home work.

Both clients followed the rule and the work is gone. Poll-before-publish is
therefore not an ordering preference here; it is the property that makes §2
sound.

### 4.2 Reading your own slot

**[DONE]** `poll_and_apply` used to skip `peer_id == self.identity`, which
with one id per user skips exactly the publication §2 needs. It now fetches
that head and runs §2 against it (`_reconcile_sibling_publication`). The
sibling's subtree is cached under a `sibling:` address, never a `relay:` one,
so it reaches neither the participant lists nor the deletion quorum.

### 4.3 Taking a sibling's state

**[DONE]** `Session.adopt_sibling_topic` carries out the take. It exists
because per-node reconciliation deliberately does not, and session.py is
explicit about why: *"Reconciliation is always per node. There is deliberately no
wholesale-subtree replace."*

`reconcile_peer_changes(addr, topic, node_is_eligible=lambda *_: True)` gets
most of the way, but its loop acts only on `peer_made_changes` and
`local_missing_node` (session.py:1610). It skips `peer_missing_node`, so a
node the sibling deleted and pruned would quietly survive locally — the taken
state would be *nearly* the sibling's, which is worse than either extreme.
A sibling take needs absence adoption in the same pass.

That per-node deliberateness is right for peers and wrong here: the whole
point of §2 is that the decision was already made once, for the topic.

### 4.4 The alarm

**[DONE]** Two decisions, offered at the client:

- **Align with the sibling** — take the relay's state (§4.3). Everything
  unpublished on this client is lost, so the client must first let the person
  save what they need.
- **Overwrite the relay** — publish this client's state, discarding what the
  sibling wrote.

Decisions belong to the application, the implementation to Session. That
matches the existing split: Session exposes `accept_peer_node` /
`rollback_peer_node` as primitives, applications declare which node types may
be reacted to, and the UI presents the choice. The alarm is a new
presentation over machinery that exists.

**[DONE] What "save what you need" means.** The alarm names the storage
file and the person copies it. No export, no diff view, no automation - this
case is rare and a machine guessing at it is worse than a file path.

## 5. Automation, later

**[PROPOSED, deferred]** Some alarms could resolve themselves — a divergence
touching disjoint node sets could take both sides, and one where a sibling
only added nodes could merge without a question. Deliberately not built.
There is no evidence yet about how often the alarm fires or how people answer
it, and an automatic rule written before that evidence exists is a guess that
loses data quietly. The alarm is crude, visible and lossless; that is the
right starting point.

## 6. What remains of the identity question

With §2's gate in front, sharing one publication identity across clients is
no longer the hazard it is without it — the gate answers "was their state
built on everything I had" directly, instead of letting the sequence
comparator infer it.

What survives is presentation. Inside the alarm, per-node classification will
still label genuinely divergent nodes as "your sibling changed this", because
`revision_origin` and `revision_seq` travel *with the node revision* rather
than the holder (`adopt_own_fields` copies "data, weights, deleted, base and
origin", protocol.py:324) and both clients write the same origin. Two options:

- **[PROPOSED]** The alarm presents *differences*, not verdicts — "these
  nodes differ" rather than "the sibling changed these" — and the mislabelling
  never surfaces.
- **[OPEN]** Or `revision_origin` becomes per-client (`_local_revision_origin`
  returns something other than the profile `identity_key`, session.py:456),
  purely so the labels are honest. Note this costs nothing in correctness
  once the gate exists, and it does not break catch-up: an unedited local copy
  carries its *author's* origin, so a client that is merely behind still
  matches origins and still fast-forwards.

## 7. Compared with `DESIGN_MULTI_CLIENT_IDENTITY.md`

| | Siblings as peers (that doc) | Copies (this doc) |
|---|---|---|
| What peers see | N addresses under one `account_key` | One participant |
| Core assumption | None; clients may edit concurrently | One human, one client at a time |
| Reconciliation | Per node, existing classifier | Per topic, two local facts |
| New identifier | `account_key`, above `identity_key` | None required (§6) |
| Peer graph | Promoted into the protocol tree (§1.5, most of that doc's cost) | Carried in the token; **[OPEN]**, §8 |
| Concurrent sibling edits | Divergence, per node | Alarm, per topic |

That doc solves the general case and pays for it. This one takes the domain
constraint seriously and gets a much smaller design, at the cost of being
wrong if the constraint is ever false — two windows open on one machine, or
an automation editing on a client's behalf while the person works elsewhere.

## 8. Open questions

1. **Presence cannot stay per-id.** `_presence_path(peer_id)` is
   `identities/<peer_id>/presence.json` (relay_storage.py:254, 558) — one file
   per relay identity. Clients sharing an id write the same file, so peers
   still get a correct "this account is alive" signal, but siblings cannot see
   *each other*, which §3's "write presence to say I am connected" is for.
   Needs a client-scoped path beneath the id, with the id-level reading
   derived from the newest.

2. **Does the peer graph travel?** The token carries topics, identity and
   channel — not `peer_topic_sets`, `peer_topic_channel` or
   `peer_identity_key`, which live in session metadata, are not
   `ProtocolNode`s, and cannot move through the sync machinery.
   `DESIGN_MULTI_CLIENT_IDENTITY.md` §1.5 answers this by promoting them into
   the tree, which is most of that doc's cost. Sharing one publication
   identity may make much of it derivable instead. Not worked out; the
   largest unresolved item.

3. **Pairing onto a non-empty client.** Importing onto an empty client is a
   fetch — nothing to lose, no alarm needed beyond a confirmation. Importing
   onto a client that already holds content is a merge of two histories with
   no common ancestor, where §2's `published[topic_uuid]` is unset and every
   node differs. Probably pairing should refuse and offer to clear, but that
   is a decision, not a detail.

4. **A fresh client must poll before its first publish.** Today it happens to
   be safe — an empty client has no registered topics, so `publish_due_topics`
   writes nothing — but §4.1 makes this a rule the pairing flow must
   guarantee rather than inherit by accident.

5. **Two clients on one machine need explicit paths.**
   `default_relay_state_file` is keyed by relay identity *and* storage
   location — both of which siblings share by design — so two of them on one
   machine land on the same state file and share the very bookkeeping
   (`published`, `applied`) that tells them apart. `storage_file` and
   `relay_state_file` must both be set per client. Folding the storage file
   into that key would fix it, at the cost of renaming every existing
   installation's state file once, which discards `desired` and `shared` and
   idles the relay until the person re-shares. Not resolved.

6. **Removing a client.** A paired client keeps the id and the channel
   forever. Revocation means rotating both and re-pairing everything that
   should keep working — the same answer as for a leaked relay credential,
   and the same unsatisfying one. Noted because "add a client" and "remove a
   client" are usually specified together and only the first is designed here.
