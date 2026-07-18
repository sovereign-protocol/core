# Revisions and reactions

## Compound revisions

Every node perspective is described by three protocol values:

```text
(base_hash, state_hash, revision_origin)
```

`state_hash` identifies the node's actual current version. `base_hash` is
the version from which the current originator started its uninterrupted
wave of edits. Successive edits by that same originator change only
`state_hash`; this lets a peer that was offline see the latest result as
one compound change.

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

Session owns generic comparison and staging:

- equal actual hashes are in agreement;
- a locally initiated difference remains `in_transition` until the other
  client has had an opportunity to observe that local revision;
- an incoming peer revision is actionable immediately;
- competing revisions are a confirmed `divergence` immediately;
- a client that observed a revision but continues publishing its previous
  version is also confirmed as not aligned.

Applications do not implement these transport semantics. They decide only
whether an incoming revision is eligible for automatic adoption.

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
