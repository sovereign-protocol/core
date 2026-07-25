# Repository and licensing separation

Status: **DESIGN FOR REVIEW — no repository split or license applied**

This is an engineering and project-governance plan, not legal advice. Final
license application and distribution obligations should be reviewed by a
qualified open-source licensing professional before the first public release.

## 1. Agreed direction

- **AGREED:** foundation and applications should have separate governance and
  release boundaries.
- **AGREED:** use separate repositories rather than publishing the current
  mixed repository as the long-term structure.
- **AGREED IN PRINCIPLE:** weak copyleft for the reusable foundation and a
  permissive license for applications.
- **AGREED:** `LGPL-3.0-or-later` for Sovereign Core and `Apache-2.0` for
  application code.
- **AGREED:** documentation and the normative S-Protocol specification use
  `CC-BY-4.0`; executable fixtures/examples retain their repository software
  license.
- **AGREED:** establish the technical boundary before inviting external code.

## 2. Proposed repositories

### `sovereign-protocol/core`

Purpose: the application-neutral reference implementation and host foundation.

Contains:

- Protocol implementation and specification.
- Session, perspectives, transitions, adopt/rollback mechanics.
- Opaque identities and application registration contracts.
- ChannelManager, direct HTTP channel, mailbox channel.
- Local/SFTP mailbox storage and WebDAV interface seam.
- Blob store/transfer/GC mechanics.
- Generic ApplicationHost, persistence, core HTTP/P2P controllers.
- Diagnostics and conformance tests.
- Minimal non-product example applications where useful.

License: `LGPL-3.0-or-later`.

### `sovereign-protocol/s-kanban`

Purpose: reference product built on Sovereign Core.

Contains:

- S-Kanban application logic and policy.
- Controllers and frontend assets.
- Application configuration and executable packaging.
- Kanban-specific documentation and tests.

License: `Apache-2.0`.

### `sovereign-protocol/personal-cockpit`

Purpose: standalone cross-application aggregation app.

Contains Personal Cockpit logic, controllers, assets, and optional adapters over
versioned public application facades. It must start coherently when any source
application is absent or inactive.

License: `Apache-2.0`.

### Later repositories

- `sovereign-protocol/s-agreement` — create only when the conformance skeleton becomes a
  real product.
- A reusable profile or UI package — only after ownership decisions in the
  architecture design.

Do not create a repository per Python subpackage at the start.

## 3. Why the split happens after the boundary refactor

Splitting the current files immediately would put unresolved dependencies across
repositories:

- `app_server.py` is generic host infrastructure but also serves application
  assets and discovers hard-coded extra modules.
- `relay_logic.py` is application-neutral but still owns Starlette routes and
  extra-application hooks.
- Application logic modules still construct Starlette routes/responses.
- Core and application tests currently rely on one flat import namespace.

The code should first run in one repository using the exact package APIs it will
use after separation. The physical split is then mechanical rather than an
architectural debugging exercise.

## 4. License interpretation relevant to packaging

The intended policy is:

- Modifications to the LGPL-covered core remain available under LGPL terms when
  distributed.
- Applications using the core may remain under their own terms, including
  Apache-2.0, if LGPL combined-work requirements are satisfied.
- Network use alone does not add AGPL-style source-offer obligations; LGPL is not
  AGPL.

“Only core modifications must be shared” is a useful shorthand but not the full
compliance rule. Distribution must also preserve notices/license texts and allow
the recipient to use a compatible modified library in the combined work.

A separately installed Python dependency is the preferred architecture because
users can replace it. Bundled/frozen executables need a specific compliance plan.

## 5. License decisions still open

### L1 — exact LGPL expression

**ACCEPTED:** `LGPL-3.0-or-later`. This favors long-term compatibility while
allowing recipients to use a later GNU LGPL version.

### L2 — future commercial/alternative licensing

**ACCEPTED:** no planned proprietary dual-license. Contributors retain copyright
and certify contribution rights through DCO 1.1; no CLA. Later relicensing would
therefore require permission from affected copyright holders.

### L3 — profile and UI boundary

**ACCEPTED:** the minimal public profile and generic host shell belong to Core and
use `LGPL-3.0-or-later`. Every application's domain UI/assets use that
application's `Apache-2.0` license. No separate shared UI/profile distribution is
created yet.

### L4 — documentation license

**ACCEPTED:** documentation and the normative S-Protocol specification use
`CC-BY-4.0`, making them easy to quote, translate, teach, and use for independent
implementations with attribution. Code, golden fixtures, and executable examples
remain under their repository's software license.

## 6. Copyright and inbound contributions

Before accepting pull requests, each repository should state:

- Copyright remains with contributors.
- Contributions are submitted under that repository's license.
- Developer Certificate of Origin sign-off is required, if L2 adopts DCO.
- Contributors must have the right to submit the code and assets.
- Generated/copied code and third-party assets require disclosed provenance.

Recommended initial mechanism:

```text
Developer Certificate of Origin 1.1
Signed-off-by line required on every commit
No CLA
```

The project should not casually move a contributor's code from an Apache
application repository into the LGPL core or vice versa. Review license
compatibility and preserve notices/provenance for every such move.

## 7. Repository split procedure

Perform this only after architecture phase R7 passes.

1. Tag the last private monorepo baseline.
2. Make two protected copies/backups.
3. Create Core history by filtering to core paths and shared history.
4. Create Kanban history by filtering to application paths and shared history.
5. Preserve authorship, dates, and relevant design decisions.
6. Add repository-specific `LICENSE`, `NOTICE`, `README`, and `pyproject.toml`.
7. Remove files irrelevant to each repository rather than leaving mixed-license
   copies.
8. Configure S-Kanban to depend on a released or editable Sovereign Core package.
9. Run a cross-repository clean-install test.
10. Review generated source distributions/wheels to ensure license files are
    included.
11. Only then create public GitHub remotes.

Do not publish the mixed history and rewrite it publicly afterward unless a real
security incident requires it.

## 8. Versioning and compatibility

### Accepted initial versions

- Sovereign Core begins at `0.1.0`.
- S-Kanban uses tag/display version `v0.1.0-alpha.1` and Python package version
  `0.1.0a1`.
- S-Kanban declares an explicit compatible Core range.

### Version domains

These versions are independent:

- Python distribution version.
- Protocol/wire schema version.
- Session persistence envelope version.
- Connect-token version.
- Channel-descriptor version.
- Core public-profile schema version.
- Application data-schema version.
- Application facade/API version — the contract a consuming application calls
  through host facade lookup. Required once A4 facades exist; declared in the
  application manifest and checked at lookup.

Do not reuse one integer as all eight meanings. The connect token and the
channel descriptor currently both serialize a bare `"version": 1`; R1 must make
them distinguishable.

### Compatibility policy

Recommended for `0.x`:

- Breaking public Python API changes require release notes.
- Persisted data changes require an explicit incompatibility warning during the
  current `0.x` stage; automatic migrations are not required yet.
- Wire changes should support at least clear rejection of unsupported versions.
- Compatibility across all historical `0.x` versions is not promised.
- `1.0` is the first stability commitment.

## 9. Dependency and distribution audit

Before release, record for every dependency:

- Package and version range.
- Direct/transitive status.
- License and source URL.
- Runtime versus development-only use.
- Whether it is bundled in executable distributions.

Current direct dependencies include Requests, Starlette, Uvicorn, and Paramiko.
Their licenses and transitive dependencies must be captured in release artifacts.
CI installation plus `pip check` verifies that declared dependency ranges resolve;
it does not replace this manual license/source/bundling audit, which remains a
release gate.

For frozen executables:

- Include all required license/notice texts.
- Provide or point to the corresponding LGPL Core source for the exact version.
- Ensure users can replace/recombine the compatible Core library, or obtain legal
  review of the chosen distribution method.
- Document how configuration and data remain accessible outside the executable.

## 10. Governance boundary

### Sovereign Core

- Conservative changes.
- Protocol/wire changes require an accepted design issue.
- Compatibility and conformance tests are mandatory.
- Maintainer review required for public API, serialization, identity, Session,
  channel, or security changes.

### Applications

- Faster experimentation.
- Application schemas/UI may evolve within declared Core compatibility.
- Core changes proposed by an application must be justified generically and
  proven against at least two applications.

Initial governance:

- Founder is sole merger/maintainer.
- Contributors propose through forks and pull requests.
- No write access granted solely to reduce review workload.
- Maintainer roles can be added after repeated trusted contributions.

## 11. GitHub organization and names

**ACCEPTED G1:** create a **Sovereign Protocol** GitHub organization, initially
owned and merged solely by the founder.

**ACCEPTED G2, subject to availability/trademark checks:**

- Repositories: `core`, `s-kanban`, `personal-cockpit`, later `s-agreement`.
- Core distribution: `sovereign-protocol`; Python import namespace: `sovereign`.
- Application distribution/executable: `s-kanban`; product name: `S-Kanban`.

Check repository, package, domain, and trademark availability before creation;
only availability conflicts reopen the naming decision.

**Availability outcome, checked before the first release.** The Core
distribution was going to be `sovereign-core`; that name is taken on PyPI by
an unrelated, actively maintained project, so it became `sovereign-protocol`,
which is free and matches the organisation. Every application's dependency
declaration moved with it - had they shipped naming `sovereign-core`,
installing an application would have resolved the requirement against a
stranger's package and failed. Verified free at the same time: `s-kanban`,
`personal-cockpit`, `s-agreement`.

The import namespace `sovereign` is also taken on PyPI, by an Envoy control
plane, but distribution and import names are independent so nothing is
blocked. The two simply cannot be installed side by side, since each would
own a top-level `sovereign` module. Kept for `0.x` under P4 and recorded in
`DESIGN_REFACTOR_DECISION_LOG.md` under G2.

## 11.1 Source and executable release order

**ACCEPTED G4:** publish the Core source distribution/wheel and S-Kanban source
alpha first. Add the Windows executable afterward, once a focused review confirms
LGPL notices, exact Core source availability, and library replacement/recombination
requirements.

**ACCEPTED G5:** complete one professional review of the code/documentation/
contribution license package before public contribution, and a second focused
review before distributing a frozen executable.

`S-Kanban.spec` may be committed and exercised for reproducible build testing.
That technical verification is not a release artifact and does not open the
executable-distribution gate above; the focused LGPL review remains mandatory.

## 12. Files required in each public repository

- `README.md`
- `LICENSE`
- `NOTICE` where required/useful
- `CONTRIBUTING.md`
- `CODE_OF_CONDUCT.md`
- `SECURITY.md`
- `GOVERNANCE.md`
- `ROADMAP.md`
- `ARCHITECTURE.md`
- `CHANGELOG.md`
- `pyproject.toml`
- `.gitignore`
- `.gitattributes`
- `.github/CODEOWNERS`
- Pull-request and issue templates
- CI workflows

Core additionally needs protocol/specification and compatibility documentation.
Kanban additionally needs user quickstart, screenshots/demo, and Core compatibility
statement.

## 13. Decisions required before repository creation

- [x] L1: `LGPL-3.0-or-later`.
- [x] L2: no proprietary dual license; DCO 1.1; no CLA.
- [x] L3: Core profile/shell LGPL; application UIs Apache-2.0.
- [x] L4: documentation/specification `CC-BY-4.0`; code/fixtures retain software
      license.
- [x] G1: Sovereign Protocol GitHub organization; founder sole initial owner.
- [x] G2: `core`, `s-kanban`, `personal-cockpit`, later `s-agreement`; names
      remain subject to availability/trademark checks.
- [x] G3: Core `0.1.0`; Kanban `v0.1.0-alpha.1` / `0.1.0a1`.
- [x] G4: source/wheel first; executable after LGPL packaging review.
- [x] G5: license review before publication and focused executable review later.

## 14. Primary references

- GNU LGPL v3 text: <https://www.gnu.org/licenses/lgpl-3.0.html>
- GNU GPL/LGPL licensing FAQ: <https://www.gnu.org/licenses/gpl-faq.html>
- Apache License 2.0 text: <https://www.apache.org/licenses/LICENSE-2.0>
- Apache guidance for applying Apache-2.0:
  <https://www.apache.org/legal/apply-license>
- Developer Certificate of Origin 1.1:
  <https://developercertificate.org/>
- SPDX license identifiers: <https://spdx.org/licenses/>
