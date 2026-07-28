# Changelog

## Unreleased

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
