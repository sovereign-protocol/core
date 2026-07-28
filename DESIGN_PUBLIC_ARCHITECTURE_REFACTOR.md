# Public architecture refactor

Status: **IMPLEMENTED**, and partly superseded on 2026-07-28: the direct HTTP
channel was retired, so `DirectHttpChannel` and `EffectDeliveryChannel` are
gone and `MailboxChannel` is the only implementation of the channel contract.
The boundary decisions this document settled all held — which is why removing
one channel touched no application. See `DESIGN_TOPIC_HOME_CHANNELS.md` §3.

This document defines the bounded architectural refactor to complete before
publishing Sovereign Core and S-Kanban. It is intentionally driven by two real
applications: S-Kanban and a minimal S-Agreement proof. It does not include
unrelated optimization or feature work.

Status labels:

- **AGREED** — already accepted in discussion.
- **RECOMMENDED** — proposed here for review.
- **OPEN** — a decision is required before implementation.
- **DEFERRED** — explicitly outside the publication refactor.

Related documents:

- `DESIGN_REFACTOR_DECISION_LOG.md`
- `DESIGN_REPOSITORY_LICENSING.md`
- `DESIGN_OPEN_SOURCE_PUBLICATION.md`
- `DESIGN_IDENTITY_AND_TRANSPORT.md`
- `ARCHITECTURE_REVIEW.md`

## 1. Objective

Create a reusable, application-neutral foundation that can host S-Kanban and
S-Agreement without modifying protocol, Session, channel, or host code when a
new application is added.

The result must establish enforceable dependency boundaries before third-party
code is accepted and before separate licenses are applied.

This is not a general rewrite. Existing synchronization semantics and persisted
data remain authoritative unless a separately approved design decision changes
them.

## 2. Scope

### Included

1. Stable Python package boundaries and public imports.
2. Protocol vocabulary and serialization cleanup while compatibility is not yet
   required.
3. Application registration and lifecycle contracts.
4. Generic application host and persistence ownership.
5. Channel selection, token composition, and channel lifecycle ownership.
6. Separation of mailbox channel logic from Local/SFTP/WebDAV storage backends.
7. Separation of application logic from Starlette controllers and UI assets.
8. A minimal S-Agreement conformance application.
9. Boundary, packaging, and cross-application tests.

### Deferred

- WebDAV implementation. The interface is included; the backend is not.
- Relay or blob encryption.
- Automatic channel failover.
- Runtime installation of new Python packages.
- Live hot-reload of application code.
- Performance work identified in `ARCHITECTURE_REVIEW.md`.
- UI redesign and a shared frontend component system.
- Finished S-Agreement product features or signing workflow.
- Changing revision/reaction semantics beyond separately approved designs.

## 3. Target layers and dependency rule

Dependencies point downward only.

```text
Application UI
    ↓
Application controllers
    ↓
Application logic and policy
    ↓
ApplicationHost / ChannelManager
    ↓
Session
    ↓
Protocol
```

Channel implementations are services used by the host and Session effects; they
do not import application modules:

```text
ChannelManager
    ├── DirectHttpChannel
    └── MailboxChannel
            ├── LocalFolderStorage
            ├── SftpStorage
            └── WebDavStorage       [deferred implementation]
```

### Invariants

1. `protocol` imports only the Python standard library.
2. `session` imports protocol and core contracts, never Starlette, Requests,
   Paramiko, or an application.
3. A channel may call only documented Session APIs and never inspect Kanban or
   Agreement node types. **Known exception to remove:** the mailbox channel
   currently appends the local identity topic by hand
   (`relay_logic.py`, `relay_topic_uuids`: `sorted(topic_uuids) + [self.session.identity.uuid]`).
   Identity is a Core concept, not an application one, so this is not a layering
   violation today — but it is exactly the special-casing the registry exists to
   remove. R3a replaces it with a Core-owned registration so no channel names a
   node type at all.
4. Storage backends know bytes, paths, manifests, and timestamps—not Session,
   topics, identities, or applications.
5. The host discovers and wires applications; Session never imports application
   packages.
6. Applications import only the published core API, not internal modules.
7. Controllers translate HTTP to application/core calls. Application logic does
   not construct Starlette responses or routes.
8. UI files call application/controller APIs and contain no transport-specific
   persistence logic.

## 4. Important terminology correction

**RECOMMENDED:** do not model HTTP, Local, SFTP, and WebDAV as four equivalent
transport plugins.

- HTTP is a live request/response **channel**.
- Relay is an asynchronous mailbox **channel**.
- Local folder, SFTP, and WebDAV are interchangeable **storage backends** used by
  the mailbox channel.

This distinction preserves one common channel lifecycle without forcing a
filesystem backend to pretend it can send live Session messages.

## 5. Proposed core packages

The names below are provisional; the repository plan is in
`DESIGN_REPOSITORY_LICENSING.md`.

```text
sovereign_core/
    protocol/
        node.py
        state.py
        results.py
        serialization.py
    session/
        session.py
        effects.py
        transitions.py
        topics.py
        identity.py
    blobs/
        store.py
        references.py
    channels/
        base.py
        manager.py
        tokens.py
        http.py
        mailbox.py
        storage/
            base.py
            local.py
            sftp.py
            webdav.py       # interface placeholder only
    host/
        application.py
        runtime.py
        persistence.py
        http_api.py
        lifecycle.py
    diagnostics/
        trace.py
```

One repository may publish one distribution containing these subpackages at
first. Independent packages should be created only if they acquire genuinely
independent release cycles.

## 6. Protocol boundary

### Responsibilities

- PRSP node/tree representation.
- Content and subtree hashing.
- Serialization validation.
- Atomic tree operations.
- Protocol result values.

### Publication stabilization

The following must be decided before the first public compatibility promise:

1. **ACCEPTED P1 — rename:** perform the previously noted
   `revision_origin_identity` → `revision_origin` rename now, because it is
   persisted and transmitted.
2. **ACCEPTED P2 — protocol identifier:** formal name **Sovereign Protocol**,
   short name **S-Protocol**. Retire “PRSP.” Perspective sovereignty remains the
   defining semantic principle, not part of the name.

   **Persistence consequence — must be in R1 scope.** Retiring the name is not
   only a code rename: `SESSION_FORMAT = "prsp-session-v1"` (`app_server.py`) is
   written into every saved session and gate-checked on load. Renaming it changes
   a persisted discriminator, which P4 permits but which must be done
   deliberately, with a clear rejection message for the old value. The
   `PRSPNode` class name is internal and not serialized, so it may be renamed
   freely.
3. **ACCEPTED P3 — schema version:** define a protocol envelope version
   independent from application, token, channel-descriptor, persistence-envelope,
   package, **Core public-profile schema**, and application facade/API versions
   — eight domains. The Core-profile domain was made explicit during R1 review:
   A1 makes it Core data, not application data.
   The facade version is required as soon as A4 facade lookup exists, because a
   consumer application must know which facade contract it is calling; it is
   neither the producer's data-schema version nor its distribution version.

   Current state to correct in R1: the connect token and the channel descriptor
   both use a bare `"version": 1` (`app_server.py`, token composition and
   `accept_connect_token` validation), so two domains are indistinguishable on
   the wire. `SESSION_FORMAT = "prsp-session-v1"` (`app_server.py`) is the
   persistence-envelope discriminator and additionally carries the retired
   protocol name — see P2 below. No application data-schema version exists yet.
4. **ACCEPTED P4 — compatibility:** no stability promise during `0.x`. Breaking
   persisted-data changes are allowed and must be rejected/explained clearly;
   automatic one-way migrations are not required at this stage.
5. **ACCEPTED P5 — canonical specification:** publish a concise normative
   wire/hash specification plus golden fixtures and executable conformance tests.

Protocol does not own peer identities, networking, reactions, auto-adopt policy,
application schemas, or UI concepts.

## 7. Session boundary

### Responsibilities

- Own one sovereign local protocol tree.
- Track peer identities, relationships, perspectives, observations, and state.
- Classify agreement, transition, and divergence generically.
- Produce effects; never perform I/O.
- Offer generic adopt and rollback operations.
- Register application topic handlers at runtime.

### Application registration

The newly added `SharedTopicRegistry` is the first slice. It should evolve into
an explicit registration owned by Session:

```python
ApplicationRegistration(
    application_id: str,
    root_types: frozenset[str],
    list_topics: Callable[[], Iterable[str]],
    accept_invitation: Callable[[PRSPNode], SessionResult],
    on_peer_update: Callable[[], SessionResult] | None,
)
```

Required behavior:

- Register and unregister without persisting Python callbacks.
- Reject root-type ownership collisions.
- Allow a cached unknown topic to mount after its application registers.
- Keep application metadata namespaced by stable `application_id`.
- Do not let Session discover/import packages.

### Callback threading contract

The registration callbacks are invoked from channel worker threads, not only
from request handlers: the mailbox poll thread requests registered topics on
every publish cycle. The registry copies handlers and releases its own lock
before invocation. Session then invokes callbacks while holding its reentrant
Session lock, so application tree reads observe one atomic protocol state without
a registry/session lock inversion. The contract states:

- Callbacks may run concurrently on channel worker threads and must be
  reentrant-safe.
- Callbacks must not block on long I/O; they are on the publish path.
- Session provides atomicity for protocol-tree reads during one callback. A
  callback must not release that protection by starting asynchronous work against
  mutable Session state.
- Callbacks must not call back into registration/unregistration.

The current direct `SharedTopicRegistry.local_topic_uuids()` channel call is an
R3 implementation detail to replace with a Session-owned wrapper that supplies
this locking guarantee.

### Lifecycle decision

**ACCEPTED S1:** ApplicationHost imports installed application code at process
startup. Installed applications can then be activated and deactivated at runtime
through Session registration/unregistration. Deactivation removes handlers and
background work but never application data from the protocol tree. Installing or
updating Python packages requires a process restart; code hot-loading and a plugin
installer are deferred.

## 8. ApplicationHost boundary

ApplicationHost is reusable foundation, not S-Kanban code.

### Responsibilities

- Construct Session and services.
- Load configured application plugins.
- Register applications with Session.
- Register application controllers and static assets with the web framework.
- Own persistence of protocol state and local metadata.
- Start/stop channel workers.
- Dispatch Session effects through ChannelManager.
- Notify application hooks after peer updates.
- Coordinate orderly shutdown.

### It must replace

- Hard-coded `EXTRA_MODULE_CONFIG_FILES` discovery.
- Free-form `create_logic`/`build_routes` probing spread through
  `app_server.py`.
- Relay being loaded as an “extra application.”
- Application-specific update-hook assumptions in the relay loop.

### Proposed application plugin contract

```python
class ApplicationPlugin(Protocol):
    manifest: ApplicationManifest

    def create(self, services: ApplicationServices) -> ApplicationInstance: ...


class ApplicationInstance(Protocol):
    def registration(self) -> ApplicationRegistration: ...
    def controllers(self) -> Iterable[Controller]: ...
    def close(self) -> None: ...
```

`ApplicationServices` exposes supported public services such as Session,
ChannelManager, BlobStore, trace logging, and local metadata. It must not expose
the entire mutable host configuration as an unstructured dictionary.

**Current anti-pattern being replaced.** Services are passed today by mutating
the shared config dict with private keys — `config["_relay_manager"]` and
`config["_blob_store"]`, read back via `config.get(...)` in `app_server.py`,
`kanban_logic.py` and `relay_logic.py`. This is exactly the unstructured mutable
dictionary above: it has no type, no lifetime, no read-only guarantee, and any
application can overwrite another's service handle. `ApplicationServices`
replaces it with an explicit, typed, read-only object.

### Application-to-application dependencies (A5)

An application may depend on another application only through a published facade
obtained by generic host lookup, and only **optionally**.

> **Test:** if the producer application is deactivated at runtime, the consumer
> must still start and behave coherently with reduced function. If it cannot,
> the capability belongs in Core, or the two applications should be one.

Allowed: Personal Cockpit consuming a Kanban facade and simply showing no Kanban
entries when Kanban is absent or inactive.

Disallowed during `0.x`: a mandatory dependency such as routing every
application's profile through an Identity application, which would break every
other application on deactivation and would silently turn H2's "zero to many
applications" into "one to many, and one is always required."

Mandatory dependencies would require manifest `requires:` declarations,
topological activation ordering, refuse-to-deactivate semantics, and facade
version negotiation. All deferred past `1.0`.

### Open questions

1. **ACCEPTED H1:** retain Starlette as the supported host/controller framework
   during `0.x`. Protocol, Session, and application logic remain independent of
   it. Framework neutrality has no second implementation to justify it.
2. **ACCEPTED H2:** one ApplicationHost owns one Session, identity, and
   ChannelManager and may run zero to many applications simultaneously. Each app
   receives a namespaced route/asset space. Personal Cockpit is a standalone
   application that can aggregate Kanban, Agreement, and future sources.

   **Namespacing must become explicit.** Routes are flat-by-convention today
   (`/api/kanban/...`, `/api/manual/...` are registered directly into one
   Starlette route list, with nothing preventing collision). R3 must define:
   the reserved Core prefix (`/api/` core endpoints such as `/api/blob`), the
   per-application prefix derived from `application_id`, the asset mount point,
   and the host's behaviour on collision — reject at registration time with a
   named error rather than allowing last-registration-wins.
3. **ACCEPTED H3:** installed applications are listed explicitly in configuration
   and provide manifests. Add Python entry-point discovery only when third-party
   installation is demonstrated.

## 9. Channel architecture

### ChannelManager responsibilities

- Registry of available channel implementations.
- Compose channel descriptors into connect tokens.
- Validate received descriptors.
- Apply local policy (`relay_only`, offered/accepted channel kinds).
- Select exactly one active channel per peer identity.
- Own reconnect/replacement and channel shutdown.
- Route Session effects to the selected channel.
- Expose channel status without application knowledge.
- Associate application topics with relay targets generically.

The current RelayManager target registry and topic assignment map are a partial
implementation of this responsibility.

### Channel contract

The contract should describe lifecycle and capabilities, not force identical I/O:

```python
class Channel(Protocol):
    kind: str

    def offer_descriptor(self) -> dict | None: ...
    def accept_descriptor(self, descriptor: dict, invitation: Invitation) -> Result: ...
    def attach_topics(self, topic_uuids: Iterable[str]) -> Result: ...
    def detach_topics(self, topic_uuids: Iterable[str]) -> Result: ...
    def status(self) -> ChannelStatus: ...
    def close(self) -> None: ...
```

Additional behavior may be expressed through narrow capability protocols:

- `EffectDeliveryChannel` for direct Session messages — two consumers today
  (HTTP delivers; mailbox does not), so the split is justified.
- `PollingChannel` for scheduled publish/poll work — same justification,
  inverted.
- ~~`TargetedChannel` for named storage targets.~~ **Dropped for now.** It has
  exactly one implementation (mailbox) and one consumer, which violates §16's own
  "no abstract interface without two concrete consumers" rule. Named-target
  management stays concrete on the mailbox channel until a second targeted
  channel exists. Re-introduce it with WebDAV only if targeting genuinely differs
  per backend — which it should not, since backends sit *below* the channel.

This avoids a large base class full of methods that are meaningless to half the
implementations.

### Mailbox storage contract

`LocalFolderStorage`, `SftpStorage`, and future `WebDavStorage` implement the same
storage API. The contract includes atomic or best-effort-replace semantics,
manifests, snapshots, presence, blobs, listing, deletion, and timing samples.

**ACCEPTED C1:** storage exposes domain-named mailbox operations such as
`read_head`, `write_snapshot`, and `write_presence`, rather than pretending to be
a generic virtual filesystem. This lets each backend satisfy explicit atomicity,
timestamp, and listing requirements without leaking backend differences upward.

### Security question

**ACCEPTED C2:** SFTP bearer credentials remain an experimental alpha path. The
token is treated as a secret; documentation requires a dedicated jailed,
least-privilege account; SFTP is excluded from the default beginner quickstart;
and documentation states that token credentials and relay content are not
encrypted. Scoped provisioning may be revisited with a future backend.

## 10. Application boundary

### S-Kanban package

Proposed structure:

```text
s_kanban/
    manifest.py
    logic/
        kanban.py
        policies.py
    controllers/
        kanban.py
    static/
        kanban.html
        kanban.css
    config/
        examples/
```

Application logic owns:

- Kanban node schemas and containers.
- Board/column/card operations.
- Auto-adopt eligibility policy.
- Card comments, agenda, and participants.
- Presentation-specific difference descriptions.

It must not own connect-token semantics, channel selection, persistence files,
relay targets, or process lifecycle.

Controllers own HTTP parsing/status codes and return serializable application
views. They may use Starlette while application logic remains framework-neutral.

### Personal Cockpit aggregation application

Personal Cockpit (formerly Board of Boards) is a standalone application, not an
S-Kanban auxiliary view. It collects and presents information from multiple
installed/active sources such as Kanban, Agreement, and future applications.

It remains entirely above Core. Core must not gain dashboard concepts or source-
specific summary schemas.

**ACCEPTED A4 — REQUIRED BEFORE R8.** ApplicationHost offers generic
lookup of active applications' public facades. Personal Cockpit owns adapters
that consume those facades and produce cockpit entries, never source-app
internals or raw protocol trees. Source applications and Core know nothing about
Personal Cockpit. Missing or inactive sources simply provide no cockpit data.

During the private monorepo phases, the existing file may retain its direct
`KanbanLogic` import until the public facade contract exists. Before R8, that
import must be replaced by an optional, version-checked Kanban facade obtained
through generic host lookup. Personal Cockpit then moves mechanically to its own
repository as accepted in G2. The seven methods it currently consumes define the
initial facade coverage to design and test.

### S-Agreement conformance application

Before repository separation, implement only enough to prove the boundary:

- Register `agreement` as an application topic root.
- Create agreement → section → clause nodes.
- Accept an invited agreement beneath its local application container.
- Expose one minimal document view or test controller.
- Synchronize over direct HTTP and mailbox relay.
- Surface Session transition classifications.
- No finished negotiation UI, expiry policy, or sign-off workflow.

Acceptance rule: adding this proof must require no conditional branch, import, or
node-type check in Protocol, Session, channels, or ApplicationHost.

**Required test — multi-level nested adoption.** The proof must invite and adopt
a *three-level* structure (agreement → section → clause) in one pass, not just a
two-level one. `reconcile_peer_changes` walks events uuid-sorted rather than
parents-first (`ARCHITECTURE_REVIEW.md` S-6; the consequence is documented inline
in `kanban_logic.py`'s auto-adopt eligibility comment as S-6/K-4). A brand-new
container and its children can therefore fail to adopt in the same pass, because
the children have no local parent yet. Until S-6 is resolved, "adding an
application requires no core change" is unproven for structures deeper than the
Kanban case that happens to work. If the test fails, S-6 becomes R7 scope rather
than a deferred performance/correctness note.

## 11. Shared identity/profile decision

**ACCEPTED A1:** Core owns one minimal public profile shared across every active
application. It contains the stable identity key, display name, and optional
avatar attachment. Email/contact information and application-specific participant
attributes remain application data and are not automatically shared as profile.

The owner authors their profile and peers automatically adopt it; profile changes
are not negotiated like application content. Applications consume the Core profile
rather than maintaining independent identities.

### 11.0 B1 resolution — the profile surface must move into Core

A1 is confirmed, not reopened. The implementation currently contradicts it:

| Concern | Owner today | Owner required |
|---|---|---|
| `identity_key`, identity node, `revision_origin`, peer identity registry | Core (`session.py`) | Core — **immovable** |
| Minimal profile fields (display name, avatar reference) | Core node, but edited only through S-Kanban | Core |
| Profile HTTP surface (`/api/kanban/profile`, `/api/kanban/profile/avatar`) | **S-Kanban** | **Core** |
| Accepting an invited profile topic (`kanban_logic.py` join flow) | **S-Kanban** | **Core** |

The identity layer is immovable because `Session._local_revision_origin` reads
`identity_key` from the profile node: **every revision in the protocol is stamped
with it**. Identity therefore cannot be lifted into an application.

Consequence of leaving it as-is: with zero applications, or with S-Agreement or
Protocol Explorer only, there is no code path that accepts an invited profile
topic — the shared profile silently stops working, contradicting H2. It is also a
license-boundary problem, since L3 places the profile in LGPL Core while the code
sits in future-Apache S-Kanban. Both are resolved by phase **R3a**.

### 11.2 Rich identity is a future application, not Core

Core owns only the minimal profile above. Rich identity data — email, phone,
organisation, roles, multiple personas, contact cards, per-application visibility
policy, vouching, key rotation — is explicitly **not** Core. It becomes an
optional **S-Identity** application after `1.0`, consumed through A4 facades under
the A5 rule, degrading gracefully to the Core minimal profile when absent.

It must not become a mandatory dependency of other applications (A5): a profile
that every application requires is Core by definition.

### 11.1 Host shell and Protocol Explorer

**ACCEPTED A2:** the current Manual application becomes **Protocol Explorer**, a
Core diagnostic/example application for generic tree inspection and application
lifecycle testing. Its UI/API is explicitly outside `0.x` compatibility promises.

**ACCEPTED A3:** Core owns only the generic host shell: application launcher,
identity, connection status, navigation, and minimal shared styling/scripts. Each
application owns its domain UI and assets. No separate shared UI package is
created yet.

## 12. Blob boundary

`BlobStore` is generic foundation. Applications own references and presentation.

- Core owns content addressing, validation, storage, transfer, and GC mechanics.
- Applications create semantic attachment descriptors such as avatar or document.
- Channel implementations transport referenced blobs.
- Blob bytes are never embedded in protocol nodes.

No encryption change is part of this refactor.

## 13. Current-to-target file mapping

| Current file | Target ownership | Required change |
|---|---|---|
| `protocol.py` | Core protocol | Split only after public API is defined |
| `session.py` | Core session | Replace direct topic registry field with documented registration API |
| `topic_registry.py` | Core session | Generalize to application registration |
| `transport.py` | Core HTTP channel | Split channel implementation from generic interfaces |
| `relay_storage.py` | Core mailbox storage | Split interface and Local/SFTP implementations |
| `relay_logic.py` | Core mailbox channel/manager | Remove Starlette routes and extra-app hooks |
| `blob_store.py` | Core blobs | Publish stable interface |
| `trace_log.py` | Core diagnostics | Package cleanup |
| `trace_view.py` | Core development tool | Decide whether shipped or example-only |
| `app_server.py` | Core host | Decompose runtime, persistence, core controllers, lifecycle |
| `kanban_logic.py` | S-Kanban | Separate logic from controllers/routes; **surrender the profile routes and profile-topic accept path to Core (R3a)** |
| `boardofboards_logic.py` | Personal Cockpit app | Rename during extraction; replace direct `KanbanLogic` import with optional, version-checked public facade before R8 |
| `kanban.html/.css` | S-Kanban | Move as application assets |
| `boardofboards.html/.css` | Personal Cockpit app | Rename and move as standalone application assets |
| `manual_logic/html/css` | Core Protocol Explorer | Rename during extraction; diagnostic/example, non-stable API |
| `shared.css` / shared browser helpers | Core host shell | Keep only generic launcher/identity/connection/navigation assets |

## 14. Refactoring phases

Each phase must leave all existing tests green and create a reviewable commit.

### R0 — establish the baseline

- Commit the completed generic-topic relay decoupling.
- Record the full test count and supported Python version.
- Freeze unrelated feature development until R7.

**Recorded baseline (R0):**

| Item | Value |
|---|---|
| Full suite | **398 passed** |
| Python | **3.14.2** (development machine; supported range to be declared in R2's `pyproject.toml`) |
| Relay ⊥ application | Verified: `relay_logic.py` imports no application module |
| `protocol.py` imports | Verified: standard library only |
| `session.py` imports | Verified: no Starlette, Requests, Paramiko, or application module |
| Secret history scan | Verified: no credential ever committed; only the `.example` placeholder |

Exit: clean working tree apart from explicitly excluded local files; full suite
green.

Note: the recorded Python version is the machine used, not a support claim. R2
must declare `requires-python` and CI must test the declared floor, since 3.14 is
recent enough that an unstated floor would strand most contributors.

### R1 — stabilize names and wire versions

- Apply accepted P1–P5.
- Rename persisted/wire fields before compatibility is promised:
  - `revision_origin_identity` → `revision_origin` (P1).
  - `SESSION_FORMAT` persistence discriminator, which currently embeds the
    retired protocol name (`"prsp-session-v1"`), with an explicit rejection
    message for the old value (P2/P4).
- Define version ownership across **eight** domains: package, protocol envelope,
  session persistence envelope, connect token, channel descriptor, Core public-
  profile schema, application data schema, and application facade/API.
- Give the connect token and the channel descriptor **distinguishable** version
  fields; today both are a bare `"version": 1`.
- Add the `0.x` incompatibility policy and golden serialization fixtures; do not
  build automatic migrations yet.

Exit: vocabulary and compatibility policy approved; a session saved by the
previous format is rejected with a clear, tested message.

### R2 — create package layout inside the existing repository

- Introduce `src/` packages and `pyproject.toml` without splitting Git history.
- Move modules mechanically with compatibility imports only if useful during the
  phase.
- Make tests import installed package paths.

Exit: editable installation and clean installation both pass.

**Implemented R2 record:**

| Distribution | Import package | Monorepo source root |
|---|---|---|
| `sovereign-protocol 0.1.0` | `sovereign` | `src/sovereign` |
| `s-kanban 0.1.0a1` | `s_kanban` | `applications/s-kanban/src/s_kanban` |
| `personal-cockpit 0.1.0a1` | `personal_cockpit` | `applications/personal-cockpit/src/personal_cockpit` |

- Declared Python floor: **3.10**. CI coverage of that floor remains an R8
  publication gate; the development verification ran on Python 3.14.2.
- Browser assets are package resources, not working-directory-relative files.
- Runtime state now defaults to the caller's `data/` directory, never inside an
  installed package.
- Root `app_server.py` and `trace_view.py` remain compatibility launchers;
  installed commands are `sovereign-host` and `sovereign-trace-view`.
- Editable installs: all three distributions built and installed successfully.
- Clean install: all three wheels installed into an isolated target; imports,
  packaged assets, and construction of a Kanban runtime succeeded outside the
  repository.
- Full suite after extraction: **410 passed**.

Personal Cockpit's direct `s_kanban` dependency remains an explicitly temporary
private-monorepo dependency under §10. R6 must replace it with optional facade
lookup before R8/repository publication.

### R3 — formalize application registration and host

- Implement ApplicationRegistration and ApplicationHost.
- Replace hard-coded module/config discovery.
- Move persistence and lifecycle behind host services.
- Support zero to many configured application instances in one runtime.

Exit: Kanban starts through its manifest; Session imports no app.

**Implemented R3 record:**

- `ApplicationManifest`, typed read-only `ApplicationServices`,
  `ApplicationInstance`, and `ApplicationHost` are now Core contracts.
- Applications are loaded only from the explicit `applications` configuration;
  filename discovery and relay-as-extra-app loading are gone. Zero to many
  active applications are supported, with one optional primary UI alias.
- Activation registers the application's topic ownership and namespaced routes;
  deactivation removes callbacks/routes but deliberately retains protocol data.
  Root-type, application-id, application-route, and Core-route collisions fail
  activation.
- Session owns registration locking and callback invocation. A newly activated
  application mounts matching, explicitly invited topics that arrived in the
  peer cache while its type was unknown. Passively observed relay topics remain
  cache-only, preserving the token consent boundary.
- Kanban, Personal Cockpit, and Protocol Explorer use manifests and factories.
  Application HTTP routes live below `/api/<application-id>` and assets below
  `/apps/<application-id>`.
- Relay is constructed once as a Core service and supplied through
  `ApplicationServices`; application discovery is no longer a channel hook.
- Protocol Explorer is explicitly a local/HTTP diagnostic surface and registers
  no mailbox topic type. This narrows the earlier A2 claim instead of inventing
  a generic shared node type without a real semantic owner.
- Legacy `runtime.logic` and direct module factories remain internal test/CLI
  compatibility aliases only; the host is authoritative. Removal can follow
  after the controller split.

Verification: lifecycle retention, registration collision, route namespace,
Core-route collision, and unknown-topic late-load behavior have dedicated tests.
Full suite after R3: **416 passed**.

### R3a — move the profile surface into Core (B1)

Small, mandatory, and separated from R3 so it can be reviewed on its own.

- Move the profile routes (`/api/kanban/profile`, `/api/kanban/profile/avatar`)
  to Core-owned endpoints under the reserved Core prefix.
- Move acceptance of an invited profile topic out of the S-Kanban join flow into
  Core, so it works with zero or non-Kanban applications.
- Register identity/profile as a Core-owned topic handler, replacing the mailbox
  channel's hand-appended identity topic (§3 invariant 3).
- Leave rich profile data out; that is the future S-Identity application (§11.2).

Exit: a host running **no** applications, and a host running only the Agreement
proof, both accept an invited profile topic and render display name and avatar.
No `/api/kanban/*` route serves a Core concept.

**Implemented R3a record:**

- Core registers `shared_user_profile` as an unscoped, cache-only shared-topic
  handler. Channels enumerate it through the registry and contain no profile
  node-type branch or hand-appended identity UUID.
- `CoreProfileService` owns profile editing and presentation. Core endpoints are
  `GET|POST /api/core/profile` and `POST /api/core/profile/avatar`; the former
  S-Kanban routes no longer exist and its UI calls the Core endpoints.
- Direct HTTP invitation dispatch is Core-owned and routes mounting through the
  shared-topic registry. S-Kanban no longer owns a `join_discussion` override.
- Peer profiles remain cache-only and pairwise: accepting a profile never
  grafts over the local sovereign identity and never cross-introduces peers from
  unrelated application topics.
- Zero-application profile editing/rendering and invitation acceptance are
  covered explicitly. Full suite after R3a: **413 passed**.

### R4 — formalize channels

- Introduce ChannelManager and capability protocols.
- Move token composition/acceptance and exclusivity from free server functions.
- Convert direct HTTP and relay to registered channels.
- Split relay routes from mailbox logic.
- Keep Local/SFTP as mailbox storage backends.

Exit: host and apps contain no channel-specific selection branches.

**Implemented 2026-07-19.**

- `ChannelManager` now owns registration order, descriptor validation, token
  composition/acceptance, reconnect replacement, selected-channel persistence,
  effect routing, polling endpoints, status, blob lookup, and shutdown.
- `DirectHttpChannel` and `MailboxChannel` implement the common lifecycle;
  `EffectDeliveryChannel`, `PollingChannel`, and `PollingEndpoint` keep their
  unequal I/O capabilities explicit.
- Local-folder and SFTP descriptors remain mailbox provisioning variants on the
  existing `0.x` wire. They are not presented as separate channels.
- Mailbox HTTP routes moved from `relay_logic.py` to `mailbox_controller.py` and
  now live below `/api/channels/mailbox`. Applications receive only
  `ChannelManager`; private manager lookup through application config is gone.
- The app-owned legacy `board_target` migration was removed in accordance with
  P4. There is no deployed-data compatibility promise during `0.x`.
- Network views obtain channel status and optional liveness from the manager.
  The selected channel is persisted, preventing restarted peers from appearing
  as "local."
- Core blob forwarding now uses `X-Sovereign-Blob-Hop`; the former application-
  named header is intentionally unsupported under P4.

Verification: **421 tests passed**. Dedicated channel tests cover offer errors,
unknown options, descriptor collisions, policy, fallback order, reconnect
replacement, per-peer effect routing, polling capabilities, and concrete HTTP
and mailbox adapters.

### R5 — separate controllers from application logic

- Move Starlette imports and route factories out of Kanban, Personal Cockpit, and
  Manual logic.
- Define serializable view/result boundaries.
- Move assets into application packages.

Exit: application logic tests require no Starlette request/response objects.

**Implemented 2026-07-19.**

- S-Kanban and Personal Cockpit now separate `logic.py`, `controller.py`, and
  `application.py`. Protocol Explorer has the equivalent Core modules.
- Domain logic imports neither Starlette, host contracts, nor controller
  modules. Manifests and `create_application` factories live in the composition
  modules, so dependencies point controller → logic, never logic → controller.
- Configured application entry points now name the composition modules
  (`s_kanban.application`, `personal_cockpit.application`, and
  `sovereign.protocol_explorer_application`). The old internal module names are
  intentionally unsupported under P4.
- `ApplicationResultView`, `application_result_view`, and `json_value` define a
  framework-neutral JSON boundary. Controllers validate both query views and
  action results before constructing Starlette responses; unknown objects and
  non-finite numbers fail explicitly.
- Kanban unsharing no longer accepts a runtime or performs delivery I/O. It
  returns Session effects for the controller to deliver. Session topic-leave
  effects are emitted only for live members, while indirect peers are still
  removed from local tracking.
- Browser assets were already moved into their owning application packages by
  R2; R5 retains and verifies that ownership.
- `S-Kanban.spec` now collects the installed Core and application packages,
  replacing obsolete flat-module and loose-asset declarations. A full frozen
  executable build remains part of the R6 packaging smoke work.
- Controller tests are separate from Protocol Explorer logic tests. An AST
  boundary test rejects Starlette, host/controller imports, route factories,
  application factories, and runtime parameters in all three domain modules.

Verification: **427 tests passed**.

### R6 — enforce boundaries

- Add AST/import tests for forbidden dependency directions.
- Add channel contract tests.
- Add package build/install smoke tests.
- Document the supported public API; mark internals private.
- Define and test the application facade/API version contract.
- Replace Personal Cockpit's direct Kanban import with an optional Kanban facade
  lookup and verify graceful behavior when Kanban is inactive.

Exit: architectural violations fail CI.

**Implemented R6 record:**

- Core owns a late-bound facade registry. `ApplicationFacade` carries an
  application-owned `facade_api_version`; lookup returns `None` for an inactive
  producer and rejects an incompatible active version explicitly.
- S-Kanban exposes facade API 1 for its seven read/query operations. Personal
  Cockpit consumes it without importing or depending on S-Kanban, and remains
  usable when Kanban is inactive. Its legacy launcher activates both apps.
- The application launch catalog moved out of Core into the application
  launcher. Core contains no Kanban or Personal Cockpit knowledge.
- `PUBLIC_API.md` and `sovereign.__all__` define the supported `0.x` surface.
  Shipped applications import Core only through that public root.
- AST tests reject forbidden Core/app/Session/storage dependency directions and
  private Core imports. Runtime tests enforce the HTTP and mailbox contracts.
- All three wheels are built in temporary directories, installed without
  dependencies into a new isolated virtual environment, and imported with
  packaged assets verified. CI additionally installs those wheels with their
  declared runtime dependencies and runs `pip check`. Stale generated
  application build trees were removed.
- Required Windows jobs run the full suite on Python 3.10 and 3.14, making the
  supported platform and declared floor executable contracts. Python 3.14 on
  Ubuntu remains an explicitly experimental, non-blocking signal.
- A clean PyInstaller build from `S-Kanban.spec` completed in temporary output
  directories and the frozen executable reached its command-line entry point.
  This is a buildability check only: committing the spec and running the smoke
  test do not satisfy G4/G5 or authorize executable distribution.

**Post-review R6 hardening:** required CI now covers Windows 10/11 semantics on
Python 3.10 and 3.14; Ubuntu is an experimental non-blocking signal. CI resolves
wheel dependencies in a clean environment and runs `pip check`. Boundary scans
are recursive and validate exact public-root imports. Channel reconnect logic
uses direct-versus-indirect Session membership rather than a relay address
prefix.

Verification: **441 tests passed**, plus the dependency-resolving wheel-install
smoke.

### R7 — prove with minimal S-Agreement

- Implement the conformance application described above.
- Run direct HTTP and relay two-client tests.
- Confirm no core change is required.

Exit: two independent applications run on the same installed core.

**Implemented R7 record:**

- Added the independently packaged `s-agreement 0.1.0a1` conformance
  application. It owns agreement, section, and clause data, its controllers,
  and a deliberately minimal document view.
- `agreement` is registered as an application topic root. Invitations mount
  beneath S-Agreement's local container through the existing generic Session
  registration contract.
- The application exposes Session transition classifications but defines no
  auto-adoption policy. Expiry, sign-off, and the finished negotiation UI remain
  explicitly deferred product work.
- Two-client tests prove invitation and updates over direct HTTP and the local
  mailbox backend. A deterministic child-before-parent test proves a new
  agreement → section → clause structure converges in one reconciliation pass.
- No Protocol, Session, channel, or ApplicationHost production code changed.
  The suspected S-6 failure does not occur: when the missing parent is reached,
  `accept_peer_node` grafts its complete cached subtree, including descendants
  skipped earlier in that same pass.
- Packaging and boundary checks now include all four monorepo distributions and
  reject imports among any application packages.

Verification: **445 tests passed**, plus the isolated four-wheel install smoke
and a clean frozen-launcher build/entry-point smoke on Windows.

### R8 — split repositories and rehearse release

- Follow `DESIGN_REPOSITORY_LICENSING.md`.
- Run cross-repository integration against pinned and editable core versions.
- Rehearse the mandatory publication checks in
  `DESIGN_OPEN_SOURCE_PUBLICATION.md`.

Exit: Core, S-Kanban, and Personal Cockpit repositories can be published without
history rewriting afterward.

#### Tooling

Two local-only commands. Neither creates nor pushes a public repository, and the
rehearsal strips the clone's remote so an accidental `git push` has no target.

| Command | Does |
|---|---|
| `tools/rehearse_repository_split.py <out> --filter-repo <path>` | Clones, runs `git-filter-repo` per repository plan, applies path renames, swaps in each release `pyproject.toml`, and writes `LICENSE` plus `LICENSES/` from digest-pinned official texts |
| `tools/verify_repository_split.py <out>` | Checks required files, clean tree, absent remotes, scans **every** commit for forbidden paths and secret patterns, builds four wheels, asserts packaged license payloads, installs pinned and editable, and runs each repository's tests |

Published-repository scaffolding lives in `release/repositories/<name>/` and is
filtered into place; `release-pyproject.toml` becomes the repository's real
`pyproject.toml` during the split.

The split produces **four** distributions, not three: S-Agreement travels inside
Core as `examples/s-agreement/`, per the licensing plan's "minimal non-product
example applications" clause, and therefore carries Core's LGPL rather than an
application Apache-2.0 license.

#### What the rehearsal proves, and what it cannot

Automated: file presence, remote absence, whole-history path and secret scanning,
four-wheel build, packaged license payloads, pinned **and** editable cross-repository
installation, and each repository's tests in isolation.

Not automated — these remain human gates and must not be inferred from a passing
rehearsal:

- **G5 legal review.** One review of the code/documentation/contribution license
  package before public contribution; a second focused review before any frozen
  executable. A green rehearsal is evidence for that review, not a substitute.
- **G2 name availability.** Repository, distribution, domain, and trademark
  checks for `sovereign-protocol`, `s-kanban`, `personal-cockpit`.
- **Credential rotation.** The scanner proves no secret is *present*; it cannot
  prove no credential ever needs rotating.
- **O1 lead-application choice**, still provisional.
- **Dependency licence audit** (`DESIGN_REPOSITORY_LICENSING.md` §9). The
  inventory exists at `release/repositories/core/dependency-inventory.json`;
  confirming it is complete and accurate is a human step.

#### Evidence

`verify_repository_split.py` writes `R8_VERIFICATION.json` into the rehearsal
output directory. Record the run here before ticking the R8 publication gate.

- **Status: PASSED, reproduced on a clean machine 2026-07-19.**

Run: `tools/rehearse_repository_split.py` then `verify_repository_split.py`,
Windows 11, Python 3.14.2, after the User-Agent fix below.

| Check | Result |
|---|---|
| Distributions built | 4 wheels — `sovereign_core-0.1.0`, `s_agreement-0.1.0a1`, `s_kanban-0.1.0a1`, `personal_cockpit-0.1.0a1` |
| Cross-repository install | pinned (`sovereign-protocol==0.1.0`) and editable, both green |
| Per-repository tests | passed in all three isolated repositories |
| Packaged license payloads | present in every wheel |
| Remotes | absent in all three clones |
| Filtered-history scan | passed |

Independently re-verified rather than taken from the tool's own report: three
repositories, zero remotes each, clean trees, history preserved (68 / 56 / 21
commits); `LICENSE` is LGPL for Core and Apache-2.0 for both applications, as
L1/L3 require; 177 distinct historical paths across all repositories contain no
`.claude/`, `data/`, `traces/`, or non-example `relay_sftp_*`; and a scan of all
145 commits for private-key blocks, GitHub tokens, and Windows user-profile or
workspace-root paths returned zero hits. The literal path patterns are defined
in `tools/verify_repository_split.py` and are deliberately not reproduced in a
published document, which would otherwise contain the strings it scans for.

- Reproducibility defect, fixed: the license hosts reject urllib's default
  User-Agent with `HTTP 403`, so a clean-machine rehearsal could not fetch the
  official texts and only succeeded with a pre-populated `--license-cache` that
  is not in the repository. `download_verified` now sends a tool User-Agent and
  writes the cache after the digest matches, so the first run populates it and
  later runs can work offline. The verified run above exercised this path and
  populated the cache from cold.

## 14.1 Review follow-ups

Findings from the implementation reviews, recorded so they survive independently
of any one conversation. Each is either closed by a phase or carries a target
phase. Severity uses the review scale: Blocker / High / Medium / Low.

### Closed by the R2/R3/R3a/R4 batch

Do not reopen these; the earlier entries were verified in commits `bb2924c`,
`c02e1b7`, and `05deeb9`; the R4 entries are covered by the 421-test record
above.

| ID | Finding | How it was closed |
|---|---|---|
| B1 | Profile surface owned by S-Kanban, so a zero-application or non-Kanban host had no profile accept path | Profile moved to Core: `/api/core/profile{,/avatar}`; proven by `tests/test_core_profile.py`, which asserts `/api/kanban/profile` is absent from a zero-application host |
| S2f | S1 runtime activation/deactivation had no implementation path (`unregister` never called) | `ApplicationHost.activate()/deactivate()`; deactivation calls `Session.unregister_application`, which unregisters the topic handler |
| S4f | The mailbox channel hand-appended the identity topic, special-casing a node type | `relay_topic_uuids` now calls `Session.shared_topic_uuids(scoped)`; the channel names no node type |
| D12 | Route namespacing and collision policy undefined | `ApplicationHost._validate_routes` enforces `api_prefix`/`asset_prefix` and rejects duplicate, cross-application, and Core-reserved collisions with named errors |
| F1 | Applications read private channel services from host config | Applications receive only a read-only collaboration view and effect-delivery callable; `ChannelManager` is not part of `ApplicationServices` or the public package exports |
| S3f | Protocol Explorer had no mailbox topic handler despite an overly broad lifecycle claim | A2 was narrowed explicitly: Protocol Explorer is a local/HTTP diagnostic; mailbox conformance belongs to real applications, beginning with S-Agreement |

### Closed by R6

| ID | Finding | How it was closed |
|---|---|---|
| F2 | The Python `>=3.10` floor was not executed | CI now runs Python 3.10 and 3.14 |
| F3 | Clean wheel installation was not proven | `test_package_build.py` installs all three wheels in a new isolated venv |
| F4 | Ignored build trees contained stale routes | Generated application build trees were removed; smoke builds use temporary directories |
| F5 | Personal Cockpit imported S-Kanban internals | Optional version-checked facade lookup; no package/import dependency remains |

### Closed by R7

| ID | Finding | How it was closed |
|---|---|---|
| S-6 | UUID-sorted reconciliation was suspected to prevent one-pass adoption of a new nested subtree | The S-Agreement conformance test forces clause-before-section processing. The clause is initially skipped, then grafted with the complete section subtree when its parent event is processed, so one pass succeeds without a Core change. |

### Open

| ID | Sev | Finding | Evidence | Target |
|---|---|---|---|---|
| — | — | No open implementation-review finding remains before R8. | — | — |

## 15. Verification matrix

| Boundary | Required proof |
|---|---|
| Protocol | Existing protocol/hash tests plus golden wire fixtures |
| Session | Existing reaction/transition tests with non-Kanban nodes |
| Application registration | Collision, unregister, unknown-topic-late-load tests |
| HTTP channel | Existing join/sync/reconnect tests through Channel contract |
| Mailbox channel | Local and fake-SFTP contract tests, timing and restart tests |
| Host | Start/stop, persistence, application discovery, clean install |
| Kanban | Existing logic, integration, comments, overview, and UI syntax tests |
| Agreement proof | Create/invite/sync/diverge over HTTP and mailbox |
| Boundaries | Forbidden-import test and no app strings/types in core |
| Packaging | Build wheel, install in empty environment, run smoke test |

## 16. Non-goals and safeguards

- Do not redesign synchronization while moving it.
- Do not introduce an abstract interface without two concrete consumers, except
  the WebDAV storage seam already justified by Local/SFTP.
- Do not preserve obsolete internal imports indefinitely.
- Do not create many independently versioned core distributions yet.
- Do not split repositories until the dependency graph already works locally as
  if they were separate.
- Do not publish a “stable” API before the Agreement proof exercises it.

## 17. Decisions required before R0/R1

- [x] P1: `revision_origin` rename.
- [x] P2: Sovereign Protocol (S-Protocol); retire PRSP.
- [x] P3: independent version domains; initial numbers remain an R1 detail.
- [x] P4: no `0.x` stability or automatic migrations; reject incompatibility.
- [x] P5: normative Markdown specification plus fixtures/conformance tests.
- [x] S1: runtime activation/deactivation; package changes require restart.
- [x] H1: Starlette as the `0.x` host/controller framework.
- [x] H2: zero-to-many applications per host; Personal Cockpit standalone.
- [x] H3: explicit configuration/manifests; entry-point discovery deferred.
- [x] C1: mailbox-specific storage API.
- [x] C2: experimental bearer SFTP credential posture for public alpha.
- [x] A1: minimal Core public profile; app-specific/contact data stays in apps.
      **B1 resolution:** the profile HTTP surface and profile-topic accept path
      move from S-Kanban to Core in R3a (§11.0); rich identity becomes a future
      optional S-Identity application (§11.2).
- [x] A2: Manual becomes Core's non-stable Protocol Explorer. **Scope note:**
      it registers no topic handler and is intentionally a local/HTTP-only
      diagnostic surface. Mailbox conformance belongs to real applications,
      beginning with S-Agreement.
- [x] A3: Core host shell versus application-owned domain UI.
- [x] A4: Personal Cockpit adapters over public application facades; separation
      and removal of the direct Kanban import are required before R8 (§10).
- [x] A5: application-to-application dependencies are optional, late-bound
      facade lookups only; mandatory app→app dependencies disallowed in `0.x`.
