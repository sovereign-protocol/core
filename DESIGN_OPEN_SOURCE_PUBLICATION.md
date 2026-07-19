# Open-source publication and collaboration plan

Status: **DESIGN FOR REVIEW — no public repository created**

This document begins after the architecture and license decisions are approved.
It defines when the work is safe and understandable enough to publish, how the
first public release is framed, and how external collaboration is governed.

## 1. Publication principle

Publish early enough that S-Agreement and later features can be developed in
public, but only after the repository/license boundary is real.

The publication blocker is not product polish. It is avoiding:

- Mixed or unclear licensing.
- Secrets or personal environment data in Git history.
- A misleading architectural story.
- An application/core dependency inversion that contributors immediately copy.
- A setup process that cannot be reproduced outside the original machine.

## 2. Initial public scope

### Sovereign Core alpha

Promises:

- Application-neutral protocol and Session implementation.
- Direct HTTP and Local/SFTP mailbox channels.
- Explicit perspective/divergence mechanics.
- Generic application registration.
- Content-addressed blobs.
- Tested Windows development path.

Non-promises:

- Stable `1.0` APIs or wire compatibility.
- Cryptographic identity, signatures, or encryption.
- NAT traversal.
- Hosted relay service.
- Automatic failover.
- Security against an untrusted local process or malicious invited peer.

### S-Kanban alpha

Promises:

- Local-first Kanban operation.
- Collaboration without a central application server.
- Direct LAN/VPN and bring-your-own mailbox relay synchronization.
- Explicit divergence visibility and human-controlled reactions.

Non-promises:

- Trello/Jira compatibility.
- Mobile application.
- Multi-tenant hosted SaaS.
- Production-grade untrusted-Internet exposure.

## 3. Narrative accuracy

Use these claims:

- “Collaboration without a central application server.”
- “Your local state remains authoritative.”
- “Bring your own direct network or mailbox storage.”
- “Content-addressed, hash-validated state.”
- “Conflicts are made visible instead of silently overwritten.”

Do not claim:

- “No servers” — SFTP/WebDAV may use servers.
- “Data never leaves your devices” — relay data leaves devices by choice.
- “Cryptographically verifiable history” — hashes validate state, not an immutable
  signed history.
- “Encrypted relay” — encryption is deferred.
- “True P2P over the Internet” — current direct HTTP requires reachability such as
  LAN or VPN.
- “Digital signatures/non-repudiation” — not implemented.

Suggested positioning:

> S-Kanban is a local-first Kanban application that collaborates without a
> central application server. It is the first reference application for
> Sovereign Core, where every participant keeps an explicit local perspective
> and decides how differences converge.

## 4. Publication gates

All P0 gates are mandatory. P1 gates are strongly recommended for the first
public announcement. P2 may follow publicly.

### P0 — mandatory

#### Architecture and licenses

- [ ] Architecture R0–R7 and the R8 repository-split rehearsal completed.
- [ ] Core, S-Kanban, and Personal Cockpit repositories pass clean-install and
      cross-repository integration tests.
- [ ] Core contains no application imports, names, or node-type assumptions.
- [ ] Licenses and inbound contribution policy approved.
- [ ] Dependency/license inventory completed.

#### Secrets and privacy

- [ ] Scan the complete Git history, not only the working tree.
- [ ] Remove **secrets**: credentials, tokens, private keys, machine-specific
      usernames, and unnecessary absolute paths.
- [ ] Rotate every credential that may ever have been committed.
- [ ] Replace local configs with documented `.example` files.
- [ ] Ignore editor/agent local settings and runtime state.
- [ ] Review screenshots, traces, test fixtures, and design documents for personal
      data.

**Author email is authorship metadata, not a secret.** Commit author names and
addresses appear in every commit and are deliberately preserved by the repository
split procedure (`DESIGN_REPOSITORY_LICENSING.md` §7 step 5, "preserve
authorship, dates"). They cannot be removed without the history rewrite that the
same document forbids. Decision: **the founder's commit identity is published
deliberately.** Do not treat it as a secret-scan finding.

### Verified history scan results

A scan was performed against the current history. Findings:

- **No real SFTP credential was ever committed.** The three commits touching
  `relay_sftp*` (`3f5e82f`, `7612fb8`, `5448740`) modified only
  `relay_sftp_manual.json.example`, which contains the placeholders
  `your-server.example.com` and `your-username`. Real SFTP configs have always
  been covered by the `relay_sftp_*.json` ignore rule.
- **The tracked configs are clean.** `debug_A.json`, `debug_B.json`,
  `relay_manual_A.json`, and `relay_manual_B.json` contain only relative paths
  (`./data/...`) and identity labels. They are usable as-is or trivially
  convertible to `.example` files; they are not a privacy finding.
- **No absolute personal paths in tracked files.** A tracked-file search for
  `C:\Data`, `<user-profile path>`, `/home/<user>/` and `/Users/` returned nothing.

Remaining real item:

- **`.claude/` is not ignored** — fixed by adding it to `.gitignore`. Note that
  `.claude/launch.json` is tracked and references `relay_sftp_manual_A.json`,
  an intentionally ignored file; decide whether that file ships at all.

A dedicated secret-history scanner should still be run immediately before remote
creation, to confirm this result independently.

#### Security posture

- [ ] Publish an explicit threat model.
- [ ] State that Base64 connect tokens are not encryption.
- [ ] Document that current relay tokens may contain a bearer SFTP credential.
- [ ] Require a dedicated, jailed, least-privilege relay account in documentation.
- [ ] Document local binding/exposure defaults and supported trusted-network use.
- [ ] Add private vulnerability reporting before broad promotion if available.

#### Reproducibility

- [ ] Fresh clone to running application in five minutes or less.
- [ ] Tested on a clean supported Python installation.
- [ ] Dependencies installed through `pyproject.toml`/lock or documented ranges.
- [ ] Tests run with one documented command.
- [ ] Core wheel and source distribution build successfully.
- [ ] Kanban installs against the released Core artifact, not only an editable
      checkout.

### P1 — first announcement quality

- [ ] README with accurate positioning and architecture diagram.
- [ ] 30–90 second two-client demo.
- [ ] Quickstart for direct HTTP and local-folder relay.
- [ ] SFTP setup guide with credential warning.
- [ ] Screenshots and known limitations.
- [ ] `good first issue` tasks that are genuinely bounded.
- [ ] Public roadmap distinguishing Core and application priorities.
- [ ] CI required on pull requests.
- [ ] Changelog and tagged alpha release.

### P2 — public follow-up

- [ ] Linux/macOS verification.
- [ ] Automated executable builds.
- [ ] WebDAV backend.
- [ ] Expanded protocol conformance suite.
- [ ] More polished documentation site.
- [ ] Additional maintainers and CODEOWNERS when earned.

## 5. Configuration and history cleanup

Recommended public pattern:

```text
config/
    kanban.example.json
    relay-local.example.json
    relay-sftp.example.json

.gitignore:
    config/*.local.json
    data/
    traces/
    *.log
    .venv/
    .claude/
```

Examples must contain placeholders, relative paths where possible, and no real
hostnames/usernames.

Use a dedicated secret-history scanner before creating remotes. Any discovered
secret must be rotated; deleting it from the latest commit is insufficient.

## 6. CI and protected main

### Required checks

Core:

- Unit tests.
- Protocol/session conformance tests.
- Import-boundary tests.
- Package build and clean-install smoke test.
- Python syntax/lint checks selected during implementation.
- **Dependency and license inventory (SBOM).** `DESIGN_REPOSITORY_LICENSING.md`
  §9 requires recording package, version range, direct/transitive status,
  license, source URL, runtime-versus-development use, and bundling status for
  every dependency. Without a CI job this audit is stale after the first
  dependency bump, so it must be generated and diffed automatically rather than
  written once by hand.

Kanban:

- Logic and integration tests.
- Core compatibility matrix for the declared range.
- Browser JavaScript syntax check.
- Application startup smoke test.
- Package/executable check when distribution is added.

### Initial branch policy

- Default branch `main`.
- Pull request required for external contributions.
- Block force-push and branch deletion.
- Required CI checks.
- Conversations resolved before merge.
- Founder may bypass/merge own work while sole maintainer.
- Squash merge by default; delete merged branches.

Do not require an independent approval until a second trusted maintainer exists.

## 7. Contribution workflow

### Discussions

Use for:

- Product direction.
- Protocol semantics.
- Architectural alternatives.
- Early proposals without an implementation commitment.

### Issues

Use for defined outcomes and accepted design work.

Required before implementation:

- Protocol or wire changes.
- New dependencies.
- New channel/storage backends.
- Public API changes.
- Security-sensitive changes.
- Large UI/product features.

Small bugs, documentation, and bounded tests may go directly to pull requests.

### Pull requests

Template questions:

1. What changes and why?
2. Which repository/layer owns it?
3. Does it change protocol, persistence, tokens, or public APIs?
4. How was it tested?
5. Does it preserve local-first operation and user sovereignty?
6. Are documentation, migrations, and compatibility notes updated?
7. Is provenance/licensing of new code/assets clear?

## 8. Maintainer acceptance criteria

Evaluate contributions against:

- Correctness.
- Architectural ownership and dependency direction.
- Preservation of explicit sovereign perspectives.
- No mandatory central application server.
- Simplicity and maintainability.
- Generality only when proposed for Core.
- Backward-compatibility policy.
- Test and documentation quality.
- Security and privacy impact.

“Useful to Kanban” is not sufficient reason to add something to Core. A Core
abstraction should be exercised by at least two applications or two channel
implementations, unless it is an unavoidable protocol primitive.

## 9. Issue labels

Recommended common labels:

- `bug`
- `documentation`
- `good first issue`
- `help wanted`
- `needs discussion`
- `security`
- `breaking change`
- `architecture`
- `protocol`
- `session`
- `channel`
- `application`
- `ui`

Repository-specific labels can be added after real usage shows a need.

## 10. First public roadmap

### Core `0.1`

- Publish package boundaries and protocol specification.
- Direct HTTP and Local/SFTP mailbox channels.
- Application registration and host.
- Conformance suite with Kanban and Agreement skeleton.

### Kanban `0.1-alpha`

- Repeatable two-client setup.
- Board/card collaboration and explicit reactions.
- Relay targets and profile avatars.
- Documented limitations and threat model.

### Next public development

- Build S-Agreement as the second real application.
- Let it challenge and stabilize the Core public API.
- Implement WebDAV only after the mailbox storage contract is public and tested.
- Defer Core `1.0` until at least two real applications have exercised it.

### Post-`1.0` candidates (not promised)

- **S-Identity** — an optional application owning rich identity data (email,
  phone, organisation, roles, personas, contact cards, per-application
  visibility). Core keeps only the minimal profile; S-Identity is consumed
  through A4 facades under the A5 rule and must degrade gracefully when absent.
- Additional Personal Cockpit source adapters over published application facades.

## 11. Launch sequence

1. Create private/local split repositories.
2. Complete license/security/reproducibility gates.
3. Create GitHub organization or final owner account.
4. Push all three repositories privately for one release rehearsal if desired.
5. Enable branch/security settings and CI.
6. Make Sovereign Core public first.
7. Publish its `0.1.0` source/wheel artifact.
8. Make S-Kanban public against that exact Core release.
9. Tag S-Kanban `0.1.0-alpha.1`.
10. Make Personal Cockpit public against its declared Core and application-facade
    compatibility ranges.
11. Publish demo and invitation to contributors.
12. Open a small number of curated issues; avoid an unprioritized backlog dump.

## 12. Publication decisions

- **O1 — PROVISIONAL:** lead initially with the useful Kanban application and
  provide a clear path into Core architecture. Revisit immediately before launch:
  S-Agreement may become the lead application if it offers the clearer,
  demonstrable expression of sovereign perspectives by then. Changing the lead
  application does not reopen architecture or repository decisions.
- **O2 — ACCEPTED:** support Windows 10/11 explicitly; label Linux/macOS
  experimental/unverified until CI and manual checks justify a stronger claim.
- **O3 — ACCEPTED:** publish Core source/wheel and application source alpha first;
  do not let executable packaging delay opening the source.
- **O4 — ACCEPTED:** use GitHub Discussions first; add no separate chat initially.
- **O5 — ACCEPTED:** SFTP bearer credentials are an experimental, prominently
  documented limitation under the C2 safeguards.
- **O6 — ACCEPTED:** use R2 names after availability/domain/trademark checks.
- **O7 — ACCEPTED:** publish accepted near-term work, not every exploratory
  backlog item.

## 13. Final go/no-go review

Before making either repository public, produce a short signed-off report:

- Exact commit/tag being published.
- License and contribution policy decisions.
- Secret/history scan results.
- Dependency/license audit results.
- Full test and clean-install results.
- Supported platforms.
- Known security limitations.
- Core/Kanban compatibility versions.
- Remaining open questions explicitly accepted as alpha limitations.
