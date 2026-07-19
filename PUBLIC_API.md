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
`ApplicationResultView`, `ApplicationServices`, `ApplicationSpec`, `Channel`,
`ChannelAcceptance`, `ChannelManager`, `ChannelResult`,
`EffectDeliveryChannel`, `IncompatibleApplicationFacade`, `Invitation`,
`PollingChannel`, `PollingEndpoint`, `ProtocolNode`, `ProtocolResult`,
`ProtocolState`, `Session`, `SessionEffect`, `SessionResult`,
`UnsupportedProtocolVersion`, `application_result_view`, `avatar_attachment`,
`json_value`, `protocol_node_from_envelope`, and `protocol_tree_envelope`.

An application module exports:

- `APPLICATION_MANIFEST: ApplicationManifest`
- `create_application(services: ApplicationServices) -> ApplicationInstance`

Applications may register shared topic roots through `ApplicationRegistration`.
They return domain mutations as `SessionResult` and `SessionEffect`; Core owns
effect delivery.

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

S-Kanban's current facade is `s_kanban.KanbanFacade`, API version `1`. It exposes
read-only board, column, card, user, profile, and transition queries. Personal
Cockpit consumes it without declaring S-Kanban as a package dependency.

## Channel extension API

`Channel`, `EffectDeliveryChannel`, `PollingChannel`, and `PollingEndpoint` are
the supported extension contracts. `ChannelManager` owns registration,
descriptor negotiation, token composition, effect routing, and polling.
Concrete HTTP, mailbox, relay-manager, storage, Starlette controller, and server
modules are Core implementation details in `0.x`.

## Protocol and session API

`ProtocolNode`, `ProtocolState`, their envelope helpers, `Session`,
`SessionResult`, and `SessionEffect` are public. The hash and wire semantics are
normative in `SPECIFICATION_S_PROTOCOL.md`; direct mutation of internal indexes,
peer caches, locks, or application registries is unsupported.
