# Locking and composite-read architecture

## Boundary

Core has three synchronization domains:

1. `RelayManager` owns connection/configuration registries.
2. `RelayLogic` owns one connection's transport cycle and I/O bookkeeping.
3. `Session` owns protocol state, peer snapshots, reconciliation, application
   metadata, and the confirmed view revision.

Applications may mutate only through `Session`. They receive detached Core
component metadata and detached response snapshots; they never participate in
relay locking.

## Lock order

The only legal nested order is:

```text
RelayManager (10) -> Relay I/O (20) -> Session (30)
```

`OrderedRLock` enforces this order in normal/debug Python runs, including a
rejection of two distinct locks from the same layer: nothing orders one
connection's relay I/O lock against another's.

Session methods acquire Session.lock themselves. The one exception is
`application_metadata()`, which hands out a live namespace and therefore
asserts that its caller already holds the lock - see "Application metadata".

Filesystem, SFTP, persistence, blob collection, and application-effect
delivery must not begin while Session is held.

## Incoming relay transaction

One poll cycle performs:

```text
transport read
  -> Session transaction:
       apply peer snapshot
       run application reconciliation
       commit the resulting Session state
  -> release Session and relay locks
  -> deliver accumulated application effects
  -> persist and advance the confirmed view revision
```

Reconciliation stays in the Session transaction so readers cannot observe the
gap between an incoming perspective and the application's automatic reaction.
Effects are deduplicated and delivered only after the transaction returns.

Every endpoint in one tick shares a single reconciliation closure, so the drain
loop and the collected effects are reached from several poll threads. Session
lock holds them apart - the same lock that makes the reconciled state and its
revision visible together - so the closure asserts it rather than taking a
second lock that could never be contended. A polling endpoint that calls the
callback outside its Session transaction is a bug and fails loudly.

## Component metadata

Relay configuration has one owner: `RelayManager`.

- `Session.component_metadata()` returns a detached snapshot.
- `RelayManager` mutates its manager-owned dictionaries while holding its lock.
- Successful configuration operations publish replacements through
  `Session.update_component_metadata()`.
- Persistence snapshots Session directly; no shared persistence lock or
  `PersistenceParticipant` contract exists.

## Application metadata

Application metadata is nested and applications read-modify-write it, which a
snapshot-and-replace API cannot express without copying whole subtrees. So
`Session.application_metadata()` returns the live namespace and asserts the
caller holds Session.lock, which is what keeps a write from racing
`persistence_metadata()` deep-copying the same dictionary.

An application whose requests already run inside a Session transaction
(`mutation_response`, `snapshot_response`, `composite_response`) satisfies this
for free. One that does not must snapshot for reads and open its own
transaction for writes.

## Application reads

Ordinary application snapshots use `snapshot_response(builder)`.

Views that combine Session state with live channel observations use
`composite_response(snapshot_builder, observer, merger)`:

```text
Session lock: authoritative detached snapshot + confirmed revision
no Session lock: transport observation
no Session lock: merge observation into detached snapshot
```

The builder must not call transport. The observer must not read mutable Session
state. The merger receives detached values only. GET handlers never reconcile,
create domain nodes, or change selection metadata.

## Relay I/O lock scope

The per-connection relay I/O lock intentionally continues to cover a complete
poll cycle. Splitting read and apply would allow two cycles to interleave and
corrupt departed-peer and publication-sequence bookkeeping. A future split is
safe only with either a separate per-connection cycle mutex or fully
sequence-ordered idempotent applies. It is not required to remove the current
deadlock cycle because effect delivery and metadata ownership no longer create
reverse edges.

