# Optimistic Session View

## Boundary

Sovereign separates three kinds of state:

1. `ProtocolState` contains confirmed, shareable protocol data.
2. `Session` contains the confirmed local perspective, peer perspectives,
   application metadata, and the authoritative browser-view revision.
3. `SessionView` contains browser-local confirmed snapshots plus pending human
   intentions.

Pending intentions never enter `ProtocolState`, persistence, hashes, or peer
publication.

## Confirmed Session contract

Every browser-visible confirmed state has a monotonic runtime
`Session.current_view_revision()`. `Session.read_snapshot(builder)` holds the
Session lock while it builds the view and reads that revision, so a payload
cannot be labelled with a revision from another state.

Application mutations use `ApplicationServices.mutation_response`. The
application supplies an unevaluated operation callback so that validation,
mutation, persistence, and the view-revision advance occur under one Session
transaction boundary. Views use `ApplicationServices.snapshot_response`.

The browser supplies a bounded client-generated `mutation_id`. Session keeps a
bounded runtime ledger of definitive results. Repeating the ID returns the
original result and does not execute the intention twice.

Peer delivery follows the confirmed local commit. Peer availability or human
reaction never controls local confirmation.

## Browser contract

`shared-session.js` computes:

```text
visible state = confirmed snapshots + ordered pending projections
```

An application defines named authoritative views and submits intentions with:

- a conflict key;
- a command name and JSON arguments;
- an action that sends the mutation ID;
- an optional pure optimistic projection;
- the views invalidated by confirmation.

Intentions sharing a conflict key execute in order. Independent keys may
execute concurrently. Whenever a confirmed snapshot arrives, pending
projections are replayed over it.

A definitive rejection removes that intention and rebases later intentions.
A timeout or transport failure is uncertain, not rejected: the projection
stays visible while `SessionView` checks the mutation ledger and retries the
same ID. Once confirmation and an authoritative snapshot arrive, removing the
pending projection causes no visible change.

Operations whose result cannot be predicted still use the same interface.
They omit the data projection and expose pending status to the application.

## Application responsibilities

Backend applications own:

- command validation;
- translation to Session operations;
- authoritative view builders;
- invalidation declarations.

Browser applications own:

- pure domain projections for predictable intentions;
- rendering;
- human-facing success and rejection messages.

Applications do not own queues, retries, revision polling, rollback closures,
or mutation deduplication.

## Failure semantics

- HTTP conflict or validated rejection: definitive; remove the intention.
- Timeout, abort, or lost response: uncertain; retain and reconcile.
- Duplicate mutation ID: return the recorded result.
- Snapshot failure after confirmation: retain the projection and retry the
  snapshot.
- Browser restart: discard pending memory and fetch fresh confirmed snapshots.
- Core restart: the runtime mutation ledger resets; clients fetch confirmed
  state before deciding whether an uncertain command still needs attention.

The last case deliberately avoids persisting browser interaction bookkeeping
inside the protocol. Durable cross-restart command receipts can be added at
the Session persistence layer if a future interface preserves pending browser
intentions across restarts.
