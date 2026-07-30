# Changelog

## 0.1.5

- Enforce the lock order `relay manager -> relay I/O -> Session` at runtime,
  including a rejection of two locks from the same layer: nothing orders one
  relay connection's I/O lock against another's.
- Deliver application effects only after relay and Session transactions end.
- Separate Core component metadata ownership from Session persistence.
- **`Session.application_metadata()` now requires the caller to hold
  `Session.lock`.** It returns the live namespace so applications can
  read-modify-write nested structures; an unlocked write could race
  persistence deep-copying the same dictionary. Core's response helpers hold
  the lock already, so only applications calling from outside a request need
  to open their own transaction.
- Add atomic snapshot-observe-merge responses for transport-decorated views.
- Remove the obsolete `PersistenceParticipant` lock-sharing contract.
- The channel poll tick now asserts that peer-update reconciliation runs
  inside the Session transaction, replacing a lock that could never be
  contended, and calls the runtime's persistence and effect delivery
  directly instead of probing for them.

## Unreleased

- Fixed optimistic Session view confirmations that refreshed application data
  without notifying subscribers to redraw when pending state changed in the
  same batch.
- Core now provides an optimistic `SessionView`: confirmed snapshots remain
  separate from pending human intentions, mutations carry retry-safe IDs, and
  timeouts reconcile instead of rolling the visible state back. Session-owned
  view revisions make application snapshots atomic with their revision.
- **Fixed: changing a field back to an earlier value no longer creates a
  false divergence.** Relay observation now supplies the causal direction
  when current and base content hashes form a cycle.
- Browser liveness reads now use the channel poll's in-memory presence cache
  instead of performing relay/SFTP I/O, and Core exposes a lightweight local
  change revision for revision-gated application refreshes.

- **Fixed: stopping use of a relay channel now withdraws that client's
  publication.** The removal is serialized with publishing and leaves other
  clients' publications intact. Manage Channels now lists every assigned
  topic and can stop all use without deleting the channel.
- **Fixed: relay heartbeats now describe which topics they actually carry.**
  A client using the same relay for another board or only for identity traffic
  no longer appears online for a board it has moved elsewhere. Agenda rows
  also take their visible drop position before persistence and sync finish.
- **Fixed: relay presence and shutdown could remain stale or race.** The shell
  refreshes peer liveness without requiring a browser reload, and mailbox
  shutdown now waits for in-flight relay I/O before closing its storage.
- **Fixed: dragging an agenda item could show the drop position but leave the
  order unchanged in the desktop window.** The shared shell now uses one
  mouse-drag path instead of competing with WebView2's native HTML drag.
- **Fixed: shutting down a CLI host failed when a retired or unconfigured
  mailbox connection had no storage backend to close.** Mailbox shutdown now
  skips absent storage and is safe to repeat.
- **Fixed: the topic selector menu was centered on short titles and could open
  beyond the window's left edge.** It now aligns with the topic field's left
  edge.
- Core is now version `0.1.4`; Session registries are private, controller
  result handling is centralized, and pairing/storage capability contracts
  are explicit.
- Core `0.1.3` distinguished the connect-token v2 and public
  API changes from the published `0.1.2` contract. Release-contract snapshots
  now guard public exports and format versions.
- The shipped Notes example now uses the host's real asset routes, mounts the
  shared shell safely, and activates accepted topics.
- Removed the unused `requests` runtime dependency and its exclusive
  transitive dependencies from the reviewed inventory.
- Channel extensibility remains public through explicit management, liveness,
  blob, persistence, and polling capability protocols. Polling endpoints now
  own their complete cycle and diagnostics behind `poll_once`; the host only
  schedules them.
- **Every topic now has one visible home channel, including the Core
  identity.** The first channel created becomes the identity home
  automatically; Manage Channels shows it, lets the user move it, and warns
  that moving or deleting it breaks earlier invitations. Deleting a channel
  continues to remove every topic assigned to it.
  - Connect tokens are now version 2 and carry an explicit topic-to-channel
    mapping. An invitation can therefore route the identity and application
    topic through different relay targets without ambiguity; missing or
    incomplete mappings are rejected.
  - The profile handler is assignment-scoped like every application topic,
    and invitation composition no longer moves the identity as a hidden side
    effect.
  - Relay presence remains the one-time bootstrap by which an inviter learns
    who accepted. Once known, later profile changes are accepted only from the
    identity topic's home channel, so the heartbeat cannot bypass the home.
- **Several clients for one user, over one publication identity.** A pairing
  token carries the client id, one channel descriptor, every topic the account
  owns and its profile; a client that imports it becomes a copy of its
  siblings, and nobody outside can tell how many machines are behind one
  participant. Synchronisation between them is per topic rather than per node,
  from two local facts: if everything this client had was published, whatever
  a sibling wrote was written on top of it and is taken automatically; if this
  client holds unpublished work, nothing syncs and the person is asked. See
  `DESIGN_MULTI_CLIENT_PAIRING.md`.
  - The channel tick now polls before it publishes. Publishing first is
    harmless when each peer owns its own slot; with a shared one it writes
    over a sibling before comparing, and the sibling then correctly concludes
    the result is safe to adopt. Both clients follow the rule and the work is
    gone.
  - `Session.adopt_sibling_topic` takes a sibling's version of a topic whole,
    including nodes the sibling deleted. `reconcile_peer_changes` leaves those
    alone on purpose, because a *peer's* deletion is a separate decision; for
    one person's own clients the decision was already made, for the topic.
  - A sibling is never a peer: its version is cached under a `sibling:`
    address, so it reaches neither the participant lists nor the deletion
    quorum in `prune_deleted_nodes`.
  - The peer path refuses a pairing token explicitly. Admitted there it would
    register the user's own laptop as another person, and the reconnect-replace
    loop would then unbind the desktop from the very topics the token covers -
    a failure that presents as a working connection.
  - Content already identical on both sides is never an alarm, whatever the
    local record of publishing says. Both clients hold the account profile
    identically from the moment they pair, and a client that had not recorded
    publishing it would otherwise raise an alarm over content nothing had
    changed - and stop syncing that topic.
  - Pairing uses whichever connection actually has a relay, not just the
    implicit one. A relay added through Manage channels is a *target* with
    its own connection; the implicit connection holds storage only when the
    process was started with `relay_root` in a config file, which the
    packaged executable never is. Looking only at that one answered "no relay
    channel to pair over" to every user who had configured a relay the
    ordinary way.
  - A pairing token carries **every** relay this client has, not a chosen
    one. A sibling is a copy of this client, so whatever this one can reach
    it must reach too. Picking one refused with "several relays are
    configured - say which one to pair over" as soon as a second relay
    existed, which is the ordinary state for anyone using more than one.
    Accepting is additive: relays the client already has are re-keyed to the
    sibling identity and the rest are registered as ordinary targets, so
    generating a token again later *adds* the new channels instead of
    replacing what is there. Relays publishing under different identities are
    refused outright rather than silently covered by one of them, which would
    leave the sibling a *peer* of itself wherever the guess was wrong.
  - A **My other clients** section in the Manage channels dialog, beside the
    channel list, with a "Generate pairing token" button; and one paste field
    in the Sharing pane that takes either kind of token and routes by the
    marker the server sets. Pairing sits with the channels because that is
    what the token carries - this client's channels, not whichever board
    happens to be open - and is deliberately not a channel row action,
    because an invite token connects you to another person while a pairing
    token makes a second machine into you, and side by side as row actions
    those read as variations of one thing.
  - New: `POST /api/core/siblings/pairing`, `/pairing/accept`,
    `GET /api/core/siblings/alarms`, `POST /api/core/siblings/alarms/resolve`.
    The alarm names the storage file so the person can copy it before choosing;
    there is deliberately no export and no automatic merge.
  - Two clients on one machine must be given distinct `storage_file` **and**
    `relay_state_file` paths. The default state path is keyed by relay
    identity and storage location, both of which siblings share by design, so
    the default collides and the two would share the bookkeeping that tells
    them apart.
- **Fixed: a wiped relay stayed empty.** `publish_due_topics` skipped a topic
  whenever its locally persisted `published` record said that state was
  already on the server. Nothing ever falsified that record, so a relay that
  was wiped, rotated, moved, or restored from an older backup left every peer
  silently sitting on a stale flag, republishing nothing until its own content
  happened to change. The relay looked healthy - presence heartbeats are
  unconditional - while carrying no content at all, and anyone arriving
  afterwards synced nothing. The relay is now asked whether it still lists our
  publication before the skip is honoured; a listing that fails answers "yes",
  since an unreachable relay is not evidence of a missing publication.
- **A peer the relay no longer lists drops out of the connection view.** Its
  cached perspective for that topic is forgotten, so it leaves the network
  info the Sharing pane is built from, and it returns by itself once it
  publishes again. Deliberately narrow: `peer_topic_sets` is kept, so the
  peer keeps its vote in `prune_deleted_nodes` and a peer that is merely
  quiet cannot have its deletions pruned and then re-proposed on return.
  Content is untouched - cards keep naming the person as owner or member,
  because removing those references is a deliberate act, not something a
  missing directory should decide.
- `Session.forget_peer_topic_perspective(peer_addr, topic_uuid)` — drops a
  peer's cached content for one topic while keeping the relationship.
- **"Stop using" a relay channel now makes the topic private again.** The
  direct channel already did: its detach calls `Session.leave_topic`. The
  mailbox channel only cleared the topic's target assignment, so everyone it
  had been carrying stayed a member of the topic forever - the application
  went on listing them as people on the board, and could not tell "was here"
  from "is here". A mailbox detach now releases exactly the peers it was
  carrying, found through `peer_channel_for_topic`, so a topic also shared
  over a direct connection keeps those peers. This is the deliberate opposite
  of the entry above: a peer going quiet for a poll keeps the relationship;
  the user saying "stop using this channel here" ends it. Content is
  untouched either way - a card keeps naming the person, and taking them off
  it stays the user's own act.
- **Fixed: a channel could not be deleted while any topic was assigned to
  it.** `delete_channel` refused with `channel is still used by: <uuid>`. The
  assignment is invisible in the interface and nothing cleared it when the
  peers went away, so the refusal accumulated until a channel could not be
  removed at all - and explained itself with a bare uuid. Composing an invite
  token used to be enough to cause it - see the next entry. Deletion is now
  unconditional; `delete_target` already released the assignments, and the
  topics it held go back to private the same way "stop using" sends them.
- **Inviting someone to a channel no longer decides to publish there.**
  `MailboxChannel.offer_descriptor` assigned the topic to the target as a
  side effect of composing, so pressing "Get token" once - and never sending
  it - bound the board to that channel for good, with nothing on screen
  saying so and nothing ever clearing it. The order is now the other way
  round: "Use for this topic" is the decision, taken first and revocable from
  the same row, and composing refuses for a channel the topic is not on.
  "Get token" is renamed **Get invitation** and is only drawn for a channel
  in use, or for a direct connection, which has nothing to assign. The
  identity topic is the one exception - it is not a board, is never "used
  for" anything, and has to travel over whatever route the invitation takes
  or the invitee cannot see who invited them, so it follows the decision
  rather than needing one of its own. Polling follows use for the same
  reason: an outstanding invitation to a channel you have stopped using is an
  invitation to a channel you are no longer polling.
- **The direct HTTP channel is gone.** It needed a routable address between
  peers - port forwarding or a VPN - which for this project's users is
  effectively never, and the relay already covers both real cases: SFTP
  across the internet, a shared folder across a LAN. It was also the one
  channel that could not have a home for a topic: no assignment, no polling,
  no publication slot, so every rule in `DESIGN_TOPIC_HOME_CHANNELS.md` would
  have needed an "…except direct" exception. Two already existed. Deleted:
  `http_channel.py`, `transport.py`, the `/p2p/*` endpoints,
  `/api/join_discussion`, `/api/observe_topic`, `/api/unwatch_topic`,
  `/api/invite_discuss`, `/api/leave`, and the `relay_only` policy that
  existed only to switch direct HTTP off.
  - **Session no longer describes how anything moves.** The message handlers
    (`handle_join`, `handle_announce`, `handle_leave`, `handle_sync_status`,
    `handle_sync_response`), the sync-effect builders, the observation
    (`watch_topic`) surface, and the reachability bookkeeping
    (`members`, `peer_status`, `peer_sync_state`, `peer_fetch_topic_sets`,
    `peer_topics`) are all gone. What is left is the tree, the peer
    perspectives cached against it, and the reconciliation between them. A
    peer relationship is now one flat fact - `peer_topic_sets`: which topics
    we track it for.
  - **`SessionEffect` stays, carrying one type.** `release_topic_channels`
    is an application lifecycle signal, not a message, so `deliver_effects`
    and every application's `result.effects` contract are unchanged and no
    application repository needed a production change. `ChannelManager` no
    longer routes effects at all; `EffectDeliveryChannel` is withdrawn from
    the public API.
  - **Reachability is now the channel's answer, not the session's.**
    Session's network info carried a retry/failure record that only meant
    anything for a live connection. Whether a peer is reachable is now read
    from the heartbeat beside its publications, and added by
    `ChannelManager.network_info`.
  - **A peer is a publication identity, not a URL.** Peer addresses are
    `relay:<identity>` throughout. `advertise_host` survives only because
    the session address is built from it; nobody is told to connect to it.
  - The Protocol Explorer loses its address-based connect, watch and leave
    controls - it has no channel of its own to publish over, so it can only
    ever be an invitee.
- **Multi-client tests now run over a relay, not an in-process transport.**
  `MemoryHttpClient` answered a peer's message by calling the other runtime's
  handler directly. It was instant, and it exercised a route no user takes:
  direct HTTP needs a routable address between peers, which for this
  project's users means port forwarding or a VPN. The fixture it replaces
  (`s-kanban/tests/relay_clients.py`, and smaller equivalents in Core and
  S-Agreement) gives each client its own relay target on one shared folder
  and makes the test say when a cycle happens. This is the prerequisite for
  retiring the direct channel - see `DESIGN_TOPIC_HOME_CHANNELS.md` section
  3. Three tests changed what they assert, because "no implicit HTTP mesh"
  is a property of that transport and not of the design: everyone given a
  topic on a relay sees everyone else publishing it, and has to, or the board
  shows anonymous authors. What survives, and is now what they check, is that
  a board shared onward carries only that board.
- **Fixed: answering a sibling alarm only answered it on one relay.**
  `sibling_alarms` reports per (topic, relay), but the person is asked once,
  about their work. `resolve_sibling_alarm` stopped at the first connection
  holding the alarm, so with the topic on two relays - which is the ordinary
  state after pairing, since a pairing token carries every relay this client
  has and puts the whole account on each - the answer was carried out on one
  and the alarm came back on the next poll. It is now carried out on every
  connection holding it.
- **Fixed: a host with no topic could not be given one.** The shell disabled
  the connection button whenever no topic was selected, which on a first run
  is always - and the invite-token form lives behind that button. There was
  therefore no way to accept an invitation, so a fresh install could create
  topics but never join one. The button now always opens the pane, and with
  no topic the token form opens with it.

No wire or persistence-format change. `publish_due_topics` costs one extra
directory listing per published topic per poll, on the path that would
otherwise have skipped.

## 0.1.2 - 2026-07-26

- **Fixed: a windowed executable crashed on launch.** A frozen build with no
  console leaves `sys.stdout` and `sys.stderr` as `None`, and uvicorn's
  default log configuration asks `sys.stdout.isatty()` whether to colourise.
  That raised inside `logging.config.dictConfig` and surfaced as
  `ValueError: Unable to configure formatter 'default'`, with nothing in the
  message naming stdout. Every `console=False` build was affected, and it
  failed before the window appeared, so nothing was visible to the user
  except a traceback. `run_desktop` now gives the process discard streams
  when it has none.
- `desktop_main` accepts `--check`: it builds the runtime and the server,
  then returns without opening a window. A frozen build can run it on a
  machine with no desktop session, which is what lets CI prove the
  executable starts rather than only that it links.

No API removal, wire or persistence change. `run_desktop` gains an optional
`check_only` argument.

## 0.1.1 - 2026-07-26

- Blob transfer emits trace events. A blob cached, a referenced blob the
  relay does not hold, and a malformed identifier are now all recorded.
  Previously this path was silent, so an avatar reference that synced
  while its bytes did not left nothing in the logs to find.
- A publication held back because a referenced blob is missing locally is
  traced rather than only printed.

No API, wire or persistence change.

## 0.1.0 - 2026-07-26

- Initial public-alpha architecture.
- S-Protocol tree, Session perspectives, transitions, adopt and rollback.
- Direct HTTP and Local/SFTP mailbox channels.
- Generic application host, profile, protocol explorer, and blob storage.
