# Revisions and reactions

## Compound revisions

Every node perspective is described by three protocol values:

```text
(base_hash, content_hash, revision_origin)
```

`content_hash` identifies the node's own current version - its own fields
only, excluding descendants. (The recursive subtree fingerprint is a separate
value, `state_hash`, used for sync/transfer, not for revisions - see
[DESIGN_NODE_SUBTREE_HASH_SPLIT.md].) `base_hash` is the version from which the
current originator started its uninterrupted wave of edits. Successive edits by
that same originator change only `content_hash`; this lets a peer that was
offline see the latest result as one compound change.

Adoption copies the complete remote revision, including its base and
origin. It never changes the origin to the adopting client. A local edit
of a foreign revision starts a new wave whose base is the previously
adopted state and whose origin is the local client. Competing local edits
made before either side adopts the other are divergences.

The originator's published head is authoritative among snapshots carrying
that origin. A delayed same-origin snapshot may be visible temporarily,
but must not supersede the originator's current head and is corrected by
the next poll.

## Transition staging

Session owns generic comparison and staging, and reports them as two
independent fields. `type` is the *relation* between the two versions -
`in_agreement`, `peer_made_changes`, `local_made_changes`,
`local_missing_node`, `peer_missing_node`, `divergence`. `stage` is *whose
turn it is*:

- `settled` - equal actual hashes;
- `in_flight` - a locally initiated difference the other client has not yet
  had an opportunity to observe;
- `awaiting_peer` - a client observed my revision and continues publishing
  its previous version, so it is answering rather than lagging;
- `awaiting_me` - an incoming peer revision, actionable immediately;
- `conflict` - competing revisions, confirmed immediately.

These were one field until the two meanings started contradicting each
other in the interface. Collapsed together, an uncontested local edit
became a "divergence" on its author's screen the moment the peer merely
observed it, while the peer - the side actually holding a decision - saw
the milder "in transition" for the same fact. Under a topic set to never
auto-adopt, that is the normal steady state rather than an exception, so
every one-sided edit turned red for its author within one round trip.
Only `conflict` is something to resolve; the rest describe progress.

Applications do not implement these transport semantics. They decide only
whether an incoming revision is eligible for automatic adoption.

`Session.transition_rank(event)` ranks one transition against another,
relation first and stage second, so a node carrying several transitions
leads with the peer that is actually waiting. Applications read it rather
than copying the tables.

## Reactions

Reactions are whole-version operations, not flags stored on nodes.

### Adopt

Adopt selects a newer or competing revision originating from another
client and replaces the local node version with it. The adopted origin is
preserved.

### Roll back

Roll back cancels an unaccepted local compound change by restoring an
earlier revision with the same local origin. Another client's perspective
is merely where that earlier local revision is still visible; it is not
the author of the rollback target.

```text
before: A = 0-3 origin A, B = 0-1 origin A
after:  A = 0-1 origin A, B = 0-1 origin A
```

The exact earlier revision is restored. No historical revision store is
required.

### No persistent reaction state

`perspective_state`, `kept_mine`, and `pushed_back` are removed from the
protocol and wire format. Doing nothing already retains and publishes the
local perspective. Kanban may choose not to auto-adopt, but that is
application policy and is not transmitted as node state.

With multiple peers, identical target revisions are shown once. If peers
hold different revisions, each concrete whole-version choice is shown.

## Naming follow-up

Rename the implementation field `revision_origin_identity` to
`revision_origin`. This is intentionally separate from the semantic change
above so the review can distinguish behavior from naming cleanup.
