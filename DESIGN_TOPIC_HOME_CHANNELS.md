# Every topic has a home channel

Status: **built.** Agreed and implemented 2026-07-28.

## 1. What this replaces

Today a channel assignment is a hidden side effect and the profile is a special
case. Two concrete failures found by testing on 2026-07-28, both fixed or
half-fixed already, both symptoms of the same gap:

- Composing an invitation *assigned* the topic to the relay it went over, so
  pressing "Get token" once bound a board to a channel for good. Fixed: the
  decision is now "Use for this topic", taken first (see CHANGELOG, 0.1.3
  unreleased).
- The profile topic rides **every** relay, because its handler is registered
  with `assignment_scoped=False` (`src/sovereign/session.py:240`). Inviting over
  a second relay moved it, silently unpublishing the profile from the first.
  Measured, not inferred:

  ```
  t1 -> A | t2 -> B                  boards keep their own relay
  invite over A  ->  identity on A
  invite over B  ->  identity on B   profile MOVES
  identity published to A?  False    A's peers lose the profile
  t1 still on A?            True     but their board still syncs from A
  ```

The partial fix now in `main` keeps the profile on the invitation's route by
assigning it in `offer_descriptor`
(`src/sovereign/mailbox_channel.py:34-49`). **That is a stopgap and this plan
deletes it.**

## 2. The model

Seven rules, as specified by the maintainer:

1. Identity is a topic. Not a category, not an exception.
2. Each topic can have a **home channel**.
3. An invitation sends at least two topics: the identity and e.g. a board.
4. Before inviting, a channel must exist for **each** topic in the invitation.
5. Manage Channels is where the identity's home is chosen.
6. Stopping or moving the identity's channel **breaks previous invitations** —
   those clients can no longer see current data. Same logic as any other topic.
7. Stopping a channel removes every topic on it.

Rule 7 is already how `main` behaves for boards after the 2026-07-28 work;
this extends it to identity.

### Decisions taken

- **Token compatibility**: acceptable to break. `CONNECT_TOKEN_VERSION`
  (`src/sovereign/versions.py:12`) gets bumped; `accept_token` rejects
  mismatches outright, so tokens will not pass between old and new clients.
  Accepted deliberately at alpha.
- **Default home for identity**: attach automatically to the first channel
  created, **and show it**. The user can then move it. This avoids a new user
  hitting "place your identity first" before their first invitation works.
- **Warnings**: warn when deleting a channel that hosts the identity, and when
  reassigning identity to a different channel. The user still decides — the
  warning names the consequence, it does not block.

### Why the profile cannot simply ride every relay

Because siblings can write it. A sibling editing the profile and publishing to
one relay leaves the others stale, and reconciling N slots for one mutable
topic is exactly what one-relay-per-topic exists to avoid.

There was a live bug proving it: `resolve_sibling_alarm` returned on the
**first** connection holding the alarm, while `sibling_alarms()` enumerates per
*(topic, relay_identity)*. A topic in alarm on two connections could only be
half-resolved — the alarm reappeared on the next poll. **Fixed** on
2026-07-28, with a test that reaches the state through `pair_all_topics`
(`tests/test_sibling_clients.py`, `AlarmOnTwoConnectionsTests`) rather than
through the unscoped profile handler, so it survives Phase 1.

Note that first delivery of the profile is not at stake either way: the token
carries it inline (`src/sovereign/channel.py:187`). The relay copy exists only
so peers see *later* edits.

## 3. Phase 0 — retire the direct HTTP channel (do this first)

### Why first

HTTP is the one channel that cannot have a home: no assignment, no polling, no
publication slot. Every rule above would need "…except direct". That exception
already exists in two places and would multiply:

- `src/sovereign/assets/shared.js:1778` — `channel.type !== "direct"` gates
  "Use for this topic"
- `src/sovereign/assets/shared.js:1798` — `channel.in_use || channel.type ===
  "direct"` gates "Get invitation"

Building Phase 1 first means writing more of these and then deleting them.

### Why at all

Direct HTTP needs a routable address between peers — port forwarding or a VPN.
For this project's users that is effectively never. The relay covers both real
cases: SFTP across the internet, a shared directory across a LAN.

### Scope

| Target | Size | Note |
|---|---|---|
| `src/sovereign/http_channel.py` | 178 lines | deletes outright |
| `src/sovereign/transport.py` | 644 lines | mostly the 9 P2P handlers |
| `src/sovereign/session.py` | `members` 35 refs, `SessionEffect` 15 refs | the real work |

The effect path — `pull_subtree`, `send_sync_status`, `announce_peer`,
`send_leave` — is threaded through 8+ sites in `session.py`. Relay peers already
bypass it (`note_indirect_peer_topic` deliberately keeps them out of `members`),
so the two paths are already separate. That is what makes this tractable.

**Keep the channel abstraction.** It is load-bearing now that relay targets are
first-class, and local-vs-SFTP already exercises it.

### The cost that bit — done

`MemoryHttpClient` was the in-process fast transport for multi-client tests.
It is gone from all three repositories, replaced by
`s-kanban/tests/relay_clients.py` and its two smaller equivalents: a shared
temp folder, one relay target per client, and an explicit `sync()` that
publishes and polls. 90 s-kanban tests, 19 s-agreement tests and core's
Explorer test now run over the route users actually take. The suite is
green — `sync()` costs about 0.2s per two-client test, ~17s added to
s-kanban.

**Do not keep HTTP alive only for tests.** The suite would stop exercising the
path users actually take, which is worse than either option.

Four things the port established, all of which the deletion depends on:

- **A relay peer is not a member.** `note_indirect_peer_topic` keeps it out of
  `Session.members` on purpose, so every `assertIn(addr, session.members)`
  became `peer_topic_sets`. Its address is `relay:<identity>`, never a URL.
- **The invitee is seen through the heartbeat, not a handshake.** HTTP's join
  exchanged identities both ways. Over a relay the inviter learns who accepted
  from the profile in their presence file, which is what puts them on the
  board at all.
- **Only a client with a registered application can publish.** The Protocol
  Explorer registers none, so it can only ever be the invitee. Core's own test
  registers a stub application for the inviting side rather than depending on
  a real one.
- **Three tests asserted a property of the transport, not of the design.**
  "No implicit HTTP mesh" has no relay counterpart: everyone on a relay who
  has been given a topic sees everyone else publishing it, and must, or the
  board shows anonymous authors. They were rewritten to assert what survives —
  a board shared onward carries only that board.

### Verified before deleting anything

1. **Is a shared-directory relay adequate on a LAN?** Answered by the
   maintainer on 2026-07-28: proceed. Polling at 3s is accepted as the only
   LAN path; `poll_interval_seconds` is tunable per target if it disappoints.
2. **What replaces `MemoryHttpClient`?** A local-folder relay fixture with a
   forced poll. Built, and the whole suite is on it. See above.

### The cut itself — made, 2026-07-28

All of it, with every suite green. What went, and where:

| Where | What goes |
|---|---|
| `http_channel.py`, `transport.py` | both files, outright |
| `app_server.py` | `adapter`/`http_channel` fields and wiring; `/p2p/*`, `/api/join_discussion`, `/api/observe_topic`, `/api/unwatch_topic`, `/api/invite_to_discuss`, `/api/leave`; the `pending_sync_effects` tick; the `members`/`peer_fetch_topic_sets`/`peer_status`/`observed_topics` half of session persistence |
| `session.py` | `handle_join`/`handle_announce`/`handle_leave`/`handle_sync_status`/`handle_sync_response`; `leave`/`disconnect`/`_leave_all`; `sync_effects`/`pending_sync_effects`/`sync_summary`/`_sync_summary_for_topics`/`_pull_effects_for_peer_summary`/`_sync_status_effect`/`_topics_by_channel`/`_topics_for_change`; `add_peer`/`add_peer_topics`/`set_peer_fetch_topics`/`fetch_topic_uuids`/`remove_peer_fetch_topic`; `mark_peer_reachable`/`mark_peer_unreachable`/`record_sync_failure`; `topic_members`/`topic_members_by_topic`/`topic_members_from_map`; `watch_topic`/`unwatch_topic`/`observed_topic_pairs`; the `members`, `peer_fetch_topic_sets`, `peer_topics`, `peer_status`, `peer_sync_state`, `observed_topics` registries |
| `channel.py` | `EffectDeliveryChannel`, `execute_effect`, `execute_effects`, `_effect_channel`, `join_discussion`/`observe_topic`/`invite_to_discuss` passthroughs |
| `collaboration.py` | everything in `execute_effects` except the release-topic branch |
| `relay_logic.py` | `_is_redundant_relay_peer` — it exists to prefer a live direct channel over a relay one, and there will be no direct channel |
| `assets/shared.js` | `channel.type !== "direct"` (:1778) and `channel.type === "direct"` (:1798) |

**`SessionEffect` stays.** After the cut it carries exactly one type,
`release_topic_channels`, which is an application lifecycle effect and not a
transport one. That is what keeps `deliver_effects` and every application's
`result.effects` contract unchanged — **no production change is needed in
s-kanban, s-agreement or personal-cockpit.**

The two production changes outside Core were both smaller than expected:
S-Kanban's `users()` read `Session.members`, and four methods across S-Kanban
and S-Agreement returned sync effects nothing could deliver.

Four things the cut settled that the map did not anticipate:

- **Reachability had to move, not just go.** Session's network info carried a
  retry/failure record that only meant anything for a live connection. It is
  now read from the heartbeat beside a peer's publications and added by
  `ChannelManager.network_info`, which is the only place that can know.
- **`application_result_view` keeps its `deliveries` argument and ignores
  it.** Every application controller passes it. There is no per-recipient
  outcome to report when nothing is delivered on the call, so
  `delivery_errors` left the payload and the parameter stayed for the
  contract.
- **A channel ref is always `kind:instance` now.** `_channel_instance` had a
  bare-`"http"` case; a bare kind names nothing to publish into.
- **The Protocol Explorer lost its connect UI.** It registers no application,
  so it has nothing to publish and can only ever be an invitee. Core's own
  test for that now registers a stub application for the inviting side.

Tests rewritten: `test_transport.py` (deleted), `test_app_server.py`,
`test_channel_manager.py`, `test_session.py`, `test_core_profile.py`,
`test_relay_logic.py`, and the live two-server `test_kanban_integration.py`,
which now stands up a shared folder and connects through it. Those live tests
have to wait for a poll where they used to read the answer back from the
request that caused it - which is the honest shape of the system.

## 4. Phase 1 — topic home channels — made, 2026-07-28

Once every channel behaves the same way:

- **`src/sovereign/session.py:240`** — drop `assignment_scoped=False` from the
  profile handler. It becomes scoped like everything else. Check
  `SharedTopicRegistry.local_topic_uuids` and `has_assignment_scoped_handlers`
  for assumptions that some handler is unscoped.
- **`src/sovereign/mailbox_channel.py:34-49`** — delete the identity-assignment
  stopgap. Identity needs a home like any topic; `offer_descriptor` refuses
  without one.
- **`src/sovereign/channel.py:159` and `:237`** — the "select exactly one
  channel" restrictions invert. An invitation normally carries two channels now
  (identity's and the board's).
- **`accept_token` / `accept_invitation`** — multi-channel tokens are currently
  refused as ambiguous (`tests/test_channel_manager.py`,
  `test_accept_rejects_ambiguous_multi_channel_token`). With a topic→channel
  mapping in the token they stop being ambiguous; the rejection becomes
  "mapping missing or incomplete".
- **Token shape** — add the topic→channel mapping, bump
  `CONNECT_TOKEN_VERSION`.
- **Manage Channels UI** — show the identity's home, allow moving it, warn on
  move and on deleting its host channel.
- **First-channel behaviour** — auto-attach identity, and show that it happened.

The connect token is version 2. Its `topic_channels` object maps every invited
topic UUID to a `channel_id` on one descriptor in `channels`; acceptance rejects
a missing, partial, or dangling mapping before calling a channel. Relay presence
is bootstrap-only for a newly discovered peer profile. Later profile edits are
applied from the identity topic publication, so a heartbeat on an application
topic cannot silently bypass the selected home.

### Consequence to make visible, not hide

Under rule 6, moving or stopping the identity's channel breaks existing
invitations. That is intended. It must be *stated* at the moment of the action —
silent breakage is the failure class this whole arc is removing.

## 5. Verification

Same standard as the 2026-07-28 work:

- A throwaway venv with packages installed **non-editable**, the way CI does.
- New behaviour gets a test that is checked to fail against the previous code,
  not merely to pass against the new.
- Anything about layout or feel gets looked at in a running client, because
  tests cannot see it.

## 6. Open question deliberately not answered

Whether "publish-only topic" should become a general category or stay "the
profile, specifically". Today it would have exactly one member. Leave it
concrete until a second member actually appears.
