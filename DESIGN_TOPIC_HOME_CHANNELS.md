# Every topic has a home channel

Status: **planned, not built.** Agreed 2026-07-28. Written to be picked up by a
session that was not present for the discussion.

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

There is a live bug proving it: `resolve_sibling_alarm`
(`src/sovereign/relay_logic.py:2612`) returns on the **first** connection
holding the alarm, while `sibling_alarms()` enumerates per
*(topic, relay_identity)*. A topic in alarm on two connections can only be
half-resolved — the alarm reappears. Only a topic on more than one relay can
trigger this, and under rule 1 nothing can be. **Fix it anyway**: it is wrong on
its own terms and it is three lines.

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

### The cost that will bite

`MemoryHttpClient` is the in-process fast transport for multi-client tests:

| Repo | Test files using it |
|---|---|
| core | 9 |
| s-kanban | 8 (37 uses in `test_kanban_new_logic.py` alone) |
| s-agreement | 2 |
| personal-cockpit | 0 |

These need relay fixtures — temp directory, forced poll — instead of instant
in-process calls. Mechanical but large, and it touches three repositories.

**Do not keep HTTP alive only for tests.** The suite would stop exercising the
path users actually take, which is worse than either option.

### Verify before deleting anything

1. **Is a shared-directory relay adequate on a LAN?** Polling (3s default)
   versus the instant feel of direct sync. If kanban feels dead at 3s, that is
   an argument for keeping a push path, and it is much cheaper to learn before
   deleting 800 lines than after.
2. **What replaces `MemoryHttpClient`?** Most likely a local-folder relay
   fixture with a forced poll. Prototype it against a handful of
   `test_kanban_new_logic.py` cases before committing to the rewrite.

## 4. Phase 1 — topic home channels

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
