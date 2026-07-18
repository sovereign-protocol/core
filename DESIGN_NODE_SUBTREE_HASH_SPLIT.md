# Design: node_hash / subtree_hash split

**Status: fully implemented (2026-07-19), suite green (374).** All phases done.
This is a **breaking protocol change** — see "Hard cutover" below. All peers
must upgrade together; there is no cross-version interop.

## Implementation notes (what actually shipped)

- The field names stay `content_hash` / `state_hash` (they already fit:
  content = the node's own content; state = the whole subtree). Only the
  *computations* changed: `content_hash` dropped children (own-only);
  `state_hash` now folds in `sorted((child_uuid, child_state_hash))`.
- `base_hash` and all per-node classification (`_classify_content`) and
  observation (`node_revision`) now key on `content_hash` (D1). `cascade_hash`
  no longer touches `base_hash` and lost `preserve_base_uuid` — a descendant
  change can no longer disturb an ancestor's wave. This is the actual fix for
  both #2 symptoms (ancestor false-divergence and clobbered container-rollback
  base).
- The reconcile fast-paths (skip-if-equal, wholesale-replace) stay on
  `state_hash` (D2). Wholesale-replace is already dead on the live path
  (kanban passes `allow_wholesale_replace=False`), so it was left untouched.
- **Phases 3–4 done (D3).** Shallow-adopt is now centralized in
  `ProtocolState.adopt_own_fields` + `Session.accept_peer_node`: adopting an
  *existing* node updates only its own fields (data/weights/deleted, origin and
  base), adopts the node's own **move** (re-parenting it via the move logic,
  since `base_parent_uuid` is part of its own identity), and leaves its
  children untouched; only a brand-new node grafts a whole subtree. The event
  type (`peer_made_changes` vs `local_missing_node`) picks between them, so the
  `node_adopt_mode` callable and kanban's `adopt_mode` / container
  special-casing are gone. Two behaviors fell out of doing it properly:
  container **deletion now propagates** (as a shallow `deleted` flag, children
  handled by their own eligibility-checked events — a kept card survives), and
  shallow adopt now **preserves the remote revision's base** (adopt_subtree
  already did; the old container path did not).

_Written 2026-07-19. Follows [DESIGN_REVISIONS_AND_REACTIONS.md] (compound
revisions, staging, reactions), whose semantics this aligns the hash model to._

## Problem

Today `content_hash` and `state_hash` are **both recursive** (each folds in
descendants), so they are effectively two copies of one concept rather than
two distinct ones. A node's *revision identity* (its `base_hash` and the
`state_hash` used in classification) therefore changes whenever **any**
descendant changes. Consequences:

- A card edit manufactures a spurious "revision" of its column and board.
- A peer several same-origin edits behind sees ancestors as
  `divergence`/`in_transition` until per-node adoption heals it (noise).
- A container's own-edit wave `base` is **clobbered** by a later descendant
  edit — which silently breaks rolling back that container's own change.
- Kanban carries container shallow-adoption / divergence-suppression
  workarounds purely to paper over the above.

## Core idea: two hashes, two questions

| Hash | Represents | Answers | Used for |
|---|---|---|---|
| **node_hash** | this node's own `data` / `weights` / `deleted` only (no children, no uuid) | "is *this node's* version different?" | revision identity, `base_hash` waves, per-node classification, adoption, rollback, observation |
| **subtree_hash** | `node_hash` + `sorted((child_uuid, child_subtree_hash))` | "did *anything under here* change / do two subtrees match exactly?" | relay publish dirty-check, topic agreement + wholesale-replace, transfer/integrity validation |

Repurpose the existing, nearly-unused `content_hash` as **`node_hash`** (drop
children from its recomputation). Keep `state_hash` as **`subtree_hash`** (and
strengthen it — decision D4). `content_hash` is currently computed, serialized,
validated on load, and asserted in exactly one test — nothing classifies or
dedups on it — so repurposing it is cheap.

### The simplification this unlocks

Because a descendant edit no longer changes an ancestor's `node_hash`:

- **`cascade_hash` stops touching `base_hash` entirely.** It becomes purely
  "recompute `subtree_hash` up the ancestor chain." `base_hash` is only ever
  set by `_begin_revision` on the *directly edited* node. The
  `preserve_base_uuid` parameter and the ancestor base-sliding — the whole
  source of finding #2 — are **deleted**, not patched.
- `node_hash` needs **no** upward cascade (own content only); only
  `subtree_hash` cascades.

## Four decisions (decide up front — cheap to half-implement, hard to debug)

**D1 — Observation tracks `node_hash`.** `node_revision` (the
"peer observed my revision" key that drives the `in_transition`→`divergence`
staging) must use `node_hash`, consistent with classification. If it stayed on
`subtree_hash`, a container would be *classified* on its own content but
*observed* on its whole subtree, and the staging logic would give wrong
answers.

**D2 — The topic root forks.** The board's own-field divergence
(objective/name) uses `node_hash`. But two `reconcile` fast-paths must stay on
`subtree_hash`: the "whole board identical → skip" check (already compares
`state_hash`), **and** the wholesale-replace trigger. Base wholesale-replace on
the `subtree_hash` relationship explicitly — *not* on the root's `node_hash`
transition event — or it would fire on a board-name change.

**D3 — Adopt becomes shallow-by-default.** With `node_hash` identity, "adopt a
node" means adopt *its own content* (a field-level `modify`); children are
independent decisions. The `node_adopt_mode` "shallow vs full" split collapses
to: **shallow for an existing node, full-graft only for a brand-new subtree**
(`local_missing_node`). The kanban container special-casing in
`accept_peer_node` / `rollback_peer_node` is removed — it becomes the norm.
Re-specify this; don't just delete the branches.

**D4 — `subtree_hash` commits to structure.** Fold
`sorted((child_uuid, child_subtree_hash))` pairs (sorted by uuid to keep
sibling *order* out of the hash, consistent with "order isn't synced"). This
closes the gap where swapping a child for an identical-content node with a
different uuid leaves the parent hash unchanged — which matters because
`subtree_hash` is used for transfer validation. Keep uuid **out** of
`node_hash`: the uuid is the CRDT primary key, matched via `_flatten_by_uuid`
*outside* the hash, which is exactly what makes two clients' edits to "the same
card" comparable.

## Hard cutover (not a migration)

`content_hash`'s *meaning* changes (recursive → own-only) while the field name
stays. An upgraded peer and a non-upgraded peer compute **different**
`node_hash` for the same node → permanent false divergence for the entire
mixed-version window; old persisted state and old relay snapshots carry stale
hashes. It self-heals only if the load/apply path **recomputes** hashes rather
than hard-raising on the mismatch (confirm this in Phase 1). For the current
2–3 person, wipe-and-restart setup this is acceptable — but there is **no
cross-version interop**: everyone upgrades together.

## Usage audit (route every call site deliberately)

Route to **node_hash**: `_classify_content`, `_classify_move` (base/state
comparisons), `node_revision`, the `base_hash` snapshot in `_begin_revision`
and the `from_dict` fallback, `validate_rollback_target` (same-wave base
check), kanban reaction/adopt paths.

Keep on **subtree_hash** (today's `state_hash`): `publish_due_topics` /
`node_state_hash`, the `reconcile` "skip if equal" check, the wholesale-replace
decision (D2), transfer validation in `from_dict`, network-info topic
fingerprints, relay head/snapshot dedup.

## Phase plan (full suite green after each)

1. **protocol.py.** ✅ DONE. `content_hash` → own-only; `state_hash` folds in
   sorted `(child_uuid, child_state_hash)`; `base_hash` snapshots
   `content_hash`; `cascade_hash` recomputes only `state_hash` upward and no
   longer touches `base_hash` (dropped `preserve_base_uuid`). `from_dict` still
   validates both strictly — the cutover is a wipe-and-restart, so no legacy
   payloads are loaded (see Hard cutover).
2. **session.py classification + observation.** ✅ DONE. `_classify_content`
   and `node_revision` route to `content_hash` (D1); reconcile fast-paths stay
   on `state_hash`; the dead wholesale-replace path was left as-is (D2).
3. **reconcile + adopt semantics (D3).** ✅ DONE. `ProtocolState.adopt_own_fields`
   + shallow-by-default `Session.accept_peer_node` (own fields + own move,
   children kept; full graft only for a new node). `node_adopt_mode` removed
   from `reconcile_peer_changes`.
4. **kanban_logic cleanup.** ✅ DONE. Dropped `adopt_mode` and the
   `accept_peer_node`/`rollback_peer_node` container special-casing (now thin
   delegates to session).
5. **tests + docs.** ✅ DONE. Base assertions moved from `state_hash` to
   `content_hash`; two kanban tests updated (a card creation now leaves the
   board `in_agreement`); added `test_descendant_change_does_not_revision_
   ancestor`, `test_container_base_survives_descendant_edit`,
   `test_subtree_hash_includes_child_identity`.

## Verification

- **Unit:** a card edit leaves its column/board `node_hash` unchanged but
  changes their `subtree_hash`; a column rename changes the column `node_hash`;
  swapping a child's uuid (same content) changes the parent `subtree_hash`;
  a container's own-edit `base_hash` survives a later descendant edit.
- **Live:** A makes three successive card edits and B catches up → **no**
  ancestor conflict indicators; rename a column, then edit a card inside it,
  then roll back the rename → works; a card change still publishes and syncs
  over the relay (subtree_hash still bumps).
