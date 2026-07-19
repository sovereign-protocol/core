# Architecture/publication decision log

Status: **DECISIONS RESOLVED — O1 lead application remains intentionally provisional**

This is the short review index for the three detailed plans. Record accepted
answers here; the detailed documents remain the source of rationale and scope.

Documents:

- `DESIGN_PUBLIC_ARCHITECTURE_REFACTOR.md`
- `DESIGN_REPOSITORY_LICENSING.md`
- `DESIGN_OPEN_SOURCE_PUBLICATION.md`

## 1. Decisions that block refactoring

| ID | Question | Recommendation | Status/decision |
|---|---|---|---|
| P1 | Rename `revision_origin_identity`? | Rename to `revision_origin` before public persistence/wire compatibility | **ACCEPTED** |
| P2 | Public protocol name? | Formal name “Sovereign Protocol”; short name “S-Protocol”; retire PRSP | **ACCEPTED** |
| P3 | Version domains? | Independent versions for package, protocol, persistence, token, channel descriptor, app schema, and **application facade/API** (seventh domain, required once A4 facades exist) | **ACCEPTED** |
| P4 | `0.x` compatibility? | Permit documented breaking changes; no automatic migrations for now; reject incompatible data clearly; promise stability only at `1.0` | **ACCEPTED** |
| P5 | Normative specification? | Markdown wire/hash specification plus golden fixtures and executable conformance tests | **ACCEPTED** |
| S1 | Meaning of app load/unload? | Installed apps can activate/deactivate at runtime without deleting data; installing/updating code requires restart | **ACCEPTED** |
| H1 | Host framework? | Support Starlette directly during `0.x`; do not abstract an unused second framework | **ACCEPTED** |
| H2 | Apps per process? | One host/session/identity can run zero to many applications; Personal Cockpit is a standalone cross-application aggregator | **ACCEPTED** |
| H3 | App discovery? | Explicit configuration and manifests first; Python entry-point discovery later | **ACCEPTED** |
| C1 | Mailbox storage API shape? | Domain-specific mailbox operations, not a generic virtual filesystem | **ACCEPTED** |
| C2 | SFTP bearer credential? | Allow only as an explicitly experimental alpha path with dedicated jailed accounts; keep it out of the default quickstart | **ACCEPTED** |
| A1 | Shared human profile data? | Core owns one minimal public profile: stable identity, display name, optional avatar; app-specific/contact data stays in apps | **ACCEPTED — see B1 resolution below** |
| A2 | Manual app ownership? | Keep in Core as the non-stable diagnostic/example “Protocol Explorer” | **ACCEPTED** |
| A3 | Shared browser assets? | Core owns the generic host shell; every application owns its actual UI/assets; no separate UI package yet | **ACCEPTED** |
| A4 | Personal Cockpit source integration? | Personal Cockpit adapters consume other applications’ public facades through generic host lookup; Core and source apps remain cockpit-neutral | **ACCEPTED — REQUIRED BEFORE R8**: it may remain in the private monorepo during refactoring, but its direct Kanban import must be removed before repository separation |
| A5 | May an application depend on another application? | Only optional, late-bound, consumer-side facade dependencies. Mandatory app→app dependencies are disallowed during `0.x` | **ACCEPTED** |

Implementation must not start before this section is resolved, except for
committing/documenting the already completed generic-topic decoupling baseline.

### B1 resolution — profile ownership (confirms A1, does not reopen it)

Review found a contradiction between A1/H2 and the implementation: Core owns the
identity *node* (`session.identity`, `is_identity_node`, `peer_identity_key`),
but S-Kanban owns the entire profile *surface* — `/api/kanban/profile`,
`/api/kanban/profile/avatar`, and the only accept path for an invited profile
topic. With zero applications, or with S-Agreement/Protocol Explorer only, the
shared profile has no acceptance path at all.

Accepted resolution:

1. Identity key, identity node, `revision_origin`, and the peer identity registry
   stay in Core. They are immovable: every revision is stamped with
   `identity_key` via `Session._local_revision_origin`.
2. The **minimal public profile** (display name, optional avatar) stays in Core
   exactly as A1 states. The existing profile routes and profile-topic accept
   path **relocate from S-Kanban to Core** (phase R3a).
3. **Rich identity data** — email, phone, organisation, roles, personas, contact
   cards, per-application visibility — is explicitly *not* Core. It becomes a
   future optional **S-Identity** application, consumed through A4 facades with
   graceful fallback to the Core minimal profile. Post-`1.0`; not a prerequisite.

### A5 rationale — application dependency rule

Implied by S1 (runtime deactivation), H2 (zero to many applications) and A4
(facade lookup); recorded here so it is explicit rather than inferred.

> An application may depend on another application only through a published
> facade obtained by generic host lookup, and only **optionally**.
> Test: if the producer application is deactivated at runtime, the consumer must
> still start and behave coherently with reduced function. If it cannot, the
> capability belongs in Core or the two applications should be one.

Mandatory app→app dependencies would require manifest `requires:` declarations,
topological activation ordering, refuse-to-deactivate semantics, and facade
version negotiation. All are deferred past `1.0`.

## 2. Decisions that block repository creation

| ID | Question | Recommendation | Status/decision |
|---|---|---|---|
| L1 | Exact Core license? | `LGPL-3.0-or-later`, subject to professional review | **ACCEPTED** |
| L2 | Inbound contribution model? | No planned proprietary dual-license; DCO 1.1, no CLA | **ACCEPTED** |
| L3 | Profile/UI license boundary? | Minimal profile and host shell follow Core’s LGPL; application UIs follow their application license | **ACCEPTED** |
| L4 | Documentation license? | Documentation and normative S-Protocol specification use `CC-BY-4.0`; code/fixtures retain repository software license | **ACCEPTED** |
| R1 | Repository owner? | Sovereign Protocol GitHub organization; founder is sole initial owner/merger | **ACCEPTED** |
| R2 | Names? | Organization repos `core`, `s-kanban`, `personal-cockpit`, later `s-agreement`; distribution `sovereign-core`; import `sovereign` | **ACCEPTED, subject to availability/trademark checks** |
| R3 | Initial versions? | Core `0.1.0`; Kanban tag `v0.1.0-alpha.1` / Python `0.1.0a1`, with explicit Core range | **ACCEPTED** |
| R4 | Executable compliance? | Publish Core wheel/source and Kanban source first; add Windows executable after focused LGPL packaging review | **ACCEPTED** |
| R5 | Legal review timing? | License/contribution review before publication; frozen-executable review before executable distribution | **ACCEPTED** |

## 3. Decisions that block public announcement

| ID | Question | Recommendation | Status/decision |
|---|---|---|---|
| O1 | First audience/lead application? | Provisionally lead with useful Kanban and a path to Core; reconsider S-Agreement once demonstrable | **PROVISIONALLY ACCEPTED — may change before launch** |
| O2 | Supported platforms? | Explicit Windows 10/11 support; label Linux/macOS experimental/unverified until tested | **ACCEPTED** |
| O3 | Initial artifact? | Core source/wheel and Kanban source alpha first; executable later | **ACCEPTED** |
| O4 | Community channel? | GitHub Discussions first; no additional chat initially | **ACCEPTED** |
| O5 | Relay credential posture? | Experimental SFTP with prominent threat model, bearer-secret warning, and jailed-account requirement | **ACCEPTED** |
| O6 | Public naming? | Use R2 names after repository/package/domain/trademark checks | **ACCEPTED** |
| O7 | Roadmap visibility? | Publish accepted near-term work, not the full exploratory backlog | **ACCEPTED** |

## 4. Suggested review order

1. P1–P5: protocol name, vocabulary, and compatibility.
2. S1, H1–H3: application lifecycle and host.
3. C1–C2: channel/storage and relay security posture.
4. A1–A4: profile, Protocol Explorer, UI, and Personal Cockpit integration.
5. L1–L4 and R1–R5: licenses, contribution terms, names, and repositories.
6. O1–O7: launch audience and operational choices.

Resolve one group at a time. A decision may revise the detailed documents before
the next group is discussed.
