# Sovereign Core public API (`0.x`)

This document defines the supported Python import surface for application and
channel authors. Before `1.0`, breaking changes remain possible and increment
the owning version; compatibility is never inferred from package version alone.

## Core application API

The names exported by `sovereign.__all__` are public. Applications should import
these contracts from `sovereign`, not from host, controller, transport, relay,
or persistence modules. All other Python modules and names are implementation
details unless this document explicitly says otherwise.

The current exports are: `ApplicationFacade`, `ApplicationFacadeLookup`,
`ApplicationInstance`, `ApplicationManifest`, `ApplicationRegistration`,
`ApplicationResultView`, `ApplicationServices`, `ApplicationSpec`,
`BlobChannel`, `Channel`, `ChannelAcceptance`, `ChannelResult`,
`IncompatibleApplicationFacade`, `Invitation`, `LivenessChannel`,
`ManagedChannel`, `PairingChannel`, `PersistenceParticipant`, `PollCycleResult`,
`PollingChannel`, `PollingEndpoint`, `ProtocolNode`, `ProtocolResult`,
`ProtocolState`, `RelayStorage`, `Session`, `SessionEffect`, `SessionResult`,
`UnsupportedProtocolVersion`, `application_json_response`,
`application_result_view`, `avatar_attachment`, `canonical_attachments`,
`desktop_main`, `json_value`,
`protocol_node_from_envelope`, `protocol_tree_envelope`, and `run_desktop`.

An application module exports:

- `APPLICATION_MANIFEST: ApplicationManifest`
- `create_application(services: ApplicationServices) -> ApplicationInstance`

Applications may register shared topic roots through `ApplicationRegistration`.
They return domain mutations as `SessionResult`. A `SessionEffect` is a
lifecycle signal Core carries out - today only the release of a topic's
channels when its sharing ends - and never a message to a peer: a channel
publishes and polls on its own schedule, not on a caller's.

`ApplicationServices` deliberately exposes no channel manager. Applications
receive a read-only collaboration view and an effect-delivery callable; channel
configuration, invitations and topic/channel bindings are Core-only.

## Attachments

Core owns blob storage, transfer and collection; applications own what an
attachment *means*. `canonical_attachments` validates and normalizes a list of
attachment references, and `avatar_attachment` selects the `avatar` role from
one. Any node whose `data["attachments"]` holds canonical references is found by
Core's publication, peer-fetch and garbage-collection walkers, so an application
adds a new kind of attachment - a card's file, a document's exhibit - without
any Core change. Bytes are uploaded to Core's `/api/blob` endpoint, which owns
the size limit and content addressing; only the reference reaches the
application.

## Desktop window

Serving a host into its own window is Core's work, not an application's: it
owns the runtime, the server and the shutdown. `run_desktop` starts the host
on loopback and shows the window until it is closed; `desktop_main` wraps it
as a command line. An application supplies only what is its own - which module
to start and what to title the window - and declares the `pywebview`
dependency itself, so a headless install stays headless.

Because the port is chosen at start-up, the session file is not derived from
it and is not placed under the working directory. It goes to a per-user
application directory, so state survives a different port or launch location.
An explicit `storage_file` still wins.

## Optional application facades

Cross-application dependencies are optional and late-bound. A producer may put
one `ApplicationFacade` on its `ApplicationInstance`. Its
`facade_api_version` is owned by that producer and is independent from its data
schema and distribution versions.

A consumer calls `services.facades.find(application_id, expected_version)` at
use time. The result is:

- the producer's public API object when active and version-compatible;
- `None` when the producer is inactive;
- `IncompatibleApplicationFacade` when the producer is active with another API
  version.

Consumers must remain usable when an optional producer is absent. They must not
import the producer's logic, controller, or persistence modules. In `0.x`, a
producer exposes at most one facade version at a time; compatibility adapters
belong on the consumer side.

S-Kanban's current facade is `s_kanban.KanbanFacade`, API version `1`. It
exposes detached query snapshots plus explicit board, card, agenda, reaction,
and policy commands. Personal Cockpit consumes it without declaring S-Kanban
as a package dependency.

## Channel extension API

`Channel` is the required extension contract. A channel opts into independent
capabilities by also satisfying:

- `ManagedChannel`: named instance configuration, topic bindings, and
  instance-scoped detach;
- `LivenessChannel`: routed peer reachability;
- `BlobChannel`: remote blob reads through an explicit peer/topic route;
- `PairingChannel`: sibling-client pairing and conflict resolution;
- `PersistenceParticipant`: a context-manager lock Core enters while saving;
- `PollingChannel`: discovery of independently scheduled `PollingEndpoint`
  objects.

A `PollingEndpoint` provides `poll_interval_seconds`,
`has_active_relationship()`, `polling_diagnostics()`, and one complete
`poll_once(after_apply)` operation. The endpoint owns transport ordering,
timing calibration, response publication, failure handling, and diagnostic
tracing. It calls `after_apply` after applying remote state and before
publishing its response. `poll_once` returns `PollCycleResult`; Core schedules
the endpoint and advances the fixed cadence but does not inspect transport
implementation fields. Standard diagnostic keys are `identity`, `backend`,
`state_file`, and `poll_interval_seconds`; extensions may add keys.

Core owns registration, descriptor negotiation, invitation composition, and
poll scheduling. Concrete mailbox, relay-manager, storage backend, Starlette
controller, and server modules are Core implementation details in `0.x`.
`RelayStorage` is the backend contract used by polling endpoints; it includes
explicit connection closure.

## Protocol and session API

`ProtocolNode`, `ProtocolState`, their envelope helpers, `Session`,
`SessionResult`, and `SessionEffect` are public. The hash and wire semantics are
normative in `SPECIFICATION_S_PROTOCOL.md`; direct mutation of internal indexes,
peer caches, locks, or application registries is unsupported.
Session peer/topic properties are detached snapshots. Applications store local
state only through `application_metadata(application_id)`.
