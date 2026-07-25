"""
Pure Sovereign Protocol tree model.

Offered API:
  ProtocolNode
    Tree node with stable content/state hashes and checked serialization.

  ProtocolState(author)
    Owns one local protocol tree and offers atomic operations and topic
    attachment.

  ProtocolResult
    Lightweight result object for protocol operations.

Used API:
  Python standard library only.

Not included:
  Session membership, peer cache, HTTP, transport effects, persistence, UI,
  server lifecycle, thread pools.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid as uuid_mod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .versions import PROTOCOL_SCHEMA_VERSION


_REVISION_ORIGIN_UNSET = object()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def stable_hash(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:20]


# content_hash is the node's OWN identity: its data/weights/deleted only, with
# no descendants and no uuid. It answers "is this node's own version
# different?" and drives revisions, divergence, adoption and rollback. The uuid
# is deliberately excluded - it is the CRDT primary key, matched outside the
# hash, which is what lets two clients' edits to "the same node" be comparable.
def content_hash(data: dict, weights: dict, deleted: bool) -> str:
    return stable_hash({
        "data": data,
        "weights": weights,
        "deleted": deleted,
    })


# state_hash is the recursive subtree fingerprint: this node's content_hash plus
# each child's (uuid, state_hash). It answers "did anything under here change /
# do two subtrees match exactly?" and drives polling, relay publication and
# transfer validation. Child uuids are folded in (not just their content) so
# that swapping a child for an identical-content node with a different uuid
# still changes the parent - a structural change must be visible to validation.
# Pairs are sorted, so sibling order stays out of the hash (order is not synced).
def state_hash(node_content_hash: str, child_uuid_state_hashes: list[list[str]]) -> str:
    return stable_hash({
        "content_hash": node_content_hash,
        "children": child_uuid_state_hashes,
    })


def collect_subtree_uuids(node: "ProtocolNode") -> set[str]:
    out = {node.uuid}
    for child in node.children:
        out.update(collect_subtree_uuids(child))
    return out


@dataclass
class ProtocolResult:
    ok: bool
    value: Any = None
    reason: str | None = None


class UnsupportedProtocolVersion(ValueError):
    """Raised when a serialized protocol tree uses another schema."""


class ProtocolNode:
    def __init__(self, data: dict,
                 weights: dict[str, float] | None = None,
                 parent_uuid: str | None = None,
                 revision_origin: str | None = None,
                 revision_seq: int = 0):
        self.uuid = str(uuid_mod.uuid4())
        self.created_at = now_iso()
        self.updated_at = now_iso()
        self.weights = copy.deepcopy(weights or {})
        self.data = copy.deepcopy(data)
        self.parent_uuid = parent_uuid
        self.deleted = False
        self.children: list[ProtocolNode] = []
        self.content_hash = ""
        self.state_hash = ""
        self.refresh_hashes()
        # base_hash is a snapshot of this node's own content_hash at the start
        # of the current originator's wave. It stays fixed while the same
        # originator successively edits this node, and advances only when
        # another origin starts a new wave (see _begin_revision). It tracks
        # content_hash, not state_hash, so a descendant change never disturbs
        # an ancestor's wave.
        self.base_hash = self.content_hash
        self.base_parent_uuid = self.parent_uuid
        # Protocol metadata, deliberately excluded from content/state hashes.
        # It identifies the client that started this revision wave, even
        # when another peer merely forwards or adopts its latest state.
        self.revision_origin = revision_origin
        # Origin-local logical revision number. It orders successive
        # revisions from the same author without comparing wall clocks.
        # Forwarders preserve it unchanged; it is deliberately excluded
        # from content/state hashes, which describe semantic state only.
        self.revision_seq = revision_seq

    def recompute_content_hash(self) -> str:
        return content_hash(self.data, self.weights, self.deleted)

    def recompute_state_hash(self) -> str:
        return state_hash(
            self.content_hash,
            sorted([child.uuid, child.state_hash] for child in self.children),
        )

    def refresh_hashes(self) -> None:
        self.content_hash = self.recompute_content_hash()
        self.state_hash = self.recompute_state_hash()

    def refresh_hashes_deep(self) -> None:
        for child in self.children:
            child.refresh_hashes_deep()
        self.refresh_hashes()

    def live_children(self) -> list["ProtocolNode"]:
        return [child for child in self.children if not child.deleted]

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "content_hash": self.content_hash,
            "state_hash": self.state_hash,
            "base_hash": self.base_hash,
            "base_parent_uuid": self.base_parent_uuid,
            "revision_origin": self.revision_origin,
            "revision_seq": self.revision_seq,
            "weights": copy.deepcopy(self.weights),
            "data": copy.deepcopy(self.data),
            "parent_uuid": self.parent_uuid,
            "deleted": self.deleted,
            "children": [child.to_dict() for child in self.children],
        }

    @classmethod
    def from_dict(cls, payload: dict, repair_hashes: bool = False) -> "ProtocolNode":
        if "revision_origin_identity" in payload:
            raise UnsupportedProtocolVersion(
                "unsupported legacy field 'revision_origin_identity'; "
                "expected 'revision_origin'"
            )
        node = cls.__new__(cls)
        node.uuid = payload["uuid"]
        node.created_at = payload["created_at"]
        node.updated_at = payload["updated_at"]
        node.content_hash = payload["content_hash"]
        node.state_hash = payload["state_hash"]
        node.base_hash = payload.get("base_hash", payload["content_hash"])
        node.base_parent_uuid = payload.get("base_parent_uuid", payload.get("parent_uuid"))
        node.revision_origin = payload.get("revision_origin")
        revision_seq = payload.get("revision_seq", 0)
        if (isinstance(revision_seq, bool)
                or not isinstance(revision_seq, int)
                or revision_seq < 0):
            raise ValueError("revision_seq must be a non-negative integer")
        node.revision_seq = revision_seq
        node.weights = copy.deepcopy(payload.get("weights", {}))
        node.data = copy.deepcopy(payload["data"])
        node.parent_uuid = payload.get("parent_uuid")
        node.deleted = bool(payload.get("deleted", False))
        node.children = [
            cls.from_dict(child, repair_hashes=repair_hashes)
            for child in payload.get("children", [])
        ]
        computed_content_hash = node.recompute_content_hash()
        if node.content_hash != computed_content_hash:
            if repair_hashes:
                node.content_hash = computed_content_hash
            else:
                raise ValueError(f"invalid content_hash for node {node.uuid}")
        computed_state_hash = node.recompute_state_hash()
        if node.state_hash != computed_state_hash:
            if repair_hashes:
                node.state_hash = computed_state_hash
            else:
                raise ValueError(f"invalid state_hash for node {node.uuid}")
        return node


def protocol_tree_envelope(node: ProtocolNode) -> dict:
    """Return the versioned wire envelope for one protocol subtree."""
    return {
        "protocol_schema_version": PROTOCOL_SCHEMA_VERSION,
        "subtree": node.to_dict(),
        "parent_uuid": node.parent_uuid,
    }


def protocol_node_from_envelope(payload: dict,
                                repair_hashes: bool = False) -> ProtocolNode:
    """Validate a protocol envelope and decode its checked subtree."""
    if not isinstance(payload, dict):
        raise ValueError("protocol envelope must be an object")
    version = payload.get("protocol_schema_version")
    if version != PROTOCOL_SCHEMA_VERSION:
        raise UnsupportedProtocolVersion(
            f"unsupported protocol schema version {version!r}; "
            f"expected {PROTOCOL_SCHEMA_VERSION}"
        )
    subtree = payload.get("subtree")
    if not isinstance(subtree, dict):
        raise ValueError("protocol envelope is missing subtree")
    return ProtocolNode.from_dict(subtree, repair_hashes=repair_hashes)


class ProtocolState:
    def __init__(self, author: str):
        self.author = author
        self.root = ProtocolNode({"label": author})
        self.index: dict[str, ProtocolNode] = {}
        self.index_subtree(self.root)

    # Atomic operations

    def create_child(self, parent_uuid: str, data: dict,
                     weights: dict | None = None,
                     revision_origin: str | None = None,
                     revision_seq: int = 0) -> ProtocolResult:
        parent = self.index.get(parent_uuid)
        if not parent:
            return ProtocolResult(False, reason="parent not found")
        child = ProtocolNode(
            data, weights, parent_uuid, revision_origin, revision_seq,
        )
        parent.children.append(child)
        self.index_subtree(child)
        self.cascade_hash(parent_uuid)
        return ProtocolResult(True, child)

    def modify(self, node_uuid: str, data: dict,
               weights: dict | None = None,
               revision_origin: str | None | object = _REVISION_ORIGIN_UNSET,
               revision_seq: int | None = None) -> ProtocolResult:
        node = self.index.get(node_uuid)
        if not node:
            return ProtocolResult(False, reason="node not found")
        new_data = copy.deepcopy(data)
        new_weights = copy.deepcopy(weights or {})
        changed = node.data != new_data or node.weights != new_weights
        node.data = new_data
        node.weights = new_weights
        if changed:
            node.updated_at = now_iso()
            self._begin_revision(node, revision_origin, revision_seq)
        self.cascade_hash(node_uuid)
        return ProtocolResult(True, True)

    def delete(self, node_uuid: str,
               revision_origin: str | None | object = _REVISION_ORIGIN_UNSET,
               revision_seq: int | None = None) -> ProtocolResult:
        ok = self._delete_impl(node_uuid, revision_origin, revision_seq)
        return ProtocolResult(ok, ok, None if ok else "delete failed")

    def copy(self, source_uuid: str, destination_uuid: str,
             revision_origin: str | None = None,
             revision_seq: int = 0) -> ProtocolResult:
        source = self.index.get(source_uuid)
        destination = self.index.get(destination_uuid)
        if not source or not destination:
            return ProtocolResult(False, reason="source or destination not found")
        clone = self.clone_subtree(
            source, destination.uuid, revision_origin, revision_seq,
        )
        destination.children.append(clone)
        self.index_subtree(clone)
        self.cascade_hash(destination.uuid)
        return ProtocolResult(True, clone)

    def move(self, source_uuid: str, destination_uuid: str,
             revision_origin: str | None | object = _REVISION_ORIGIN_UNSET,
             revision_seq: int | None = None) -> ProtocolResult:
        ok = self._move_impl(
            source_uuid, destination_uuid, revision_origin, revision_seq,
        )
        return ProtocolResult(ok, ok, None if ok else "move failed")

    def move_child(self, source_uuid: str, destination_uuid: str,
                   index: int | None = None,
                   revision_origin: str | None | object = _REVISION_ORIGIN_UNSET,
                   revision_seq: int | None = None) -> ProtocolResult:
        # NOTE on `index`: sibling *position* is not part of any hash
        # (content/state hashes sort their children), so it never syncs -
        # it only affects this local list's order. An app that wants order
        # to replicate must carry it in node data and sort on read, the way
        # an application's own ordering field can do. Passing `index`
        # alone will silently look right locally and wrong on every peer.
        ok = self._move_child_impl(
            source_uuid, destination_uuid, index, revision_origin, revision_seq,
        )
        return ProtocolResult(ok, ok, None if ok else "move failed")

    def adopt_own_fields(self, node_uuid: str, source: ProtocolNode,
                         adopt_move: bool = True) -> ProtocolResult:
        # Shallow adopt: make an existing node's OWN revision identical to
        # `source` (data, weights, deleted, base and origin) while leaving its
        # children untouched. Children are independent revision decisions - a
        # container adopt must not smuggle in a filtered-out card change, and a
        # container deletion propagates only this node's own `deleted` flag,
        # so each child's deletion is still adopted through its own per-node
        # event (a card the recipient keeps under a not_owner policy survives).
        node = self.index.get(node_uuid)
        if not node:
            return ProtocolResult(False, reason="node not found")
        # A node's own parent is part of its own identity (base_parent_uuid
        # tracks it), so adopt the node's own move too - just not its children.
        # Do it ATOMICALLY: if the remote parent isn't present locally yet or
        # the move isn't applicable (would cycle), defer the whole adoption
        # rather than copy content/base onto a node still at the old parent -
        # that would publish a hybrid revision that never existed remotely.
        # _move_child_impl checks its guards before mutating, so a False return
        # leaves the tree untouched. `adopt_move` is False for a topic root,
        # whose parent is the peer's own local container (an attachment
        # artifact, not a shared position) - the caller knows which node is a
        # topic root; the protocol does not.
        if adopt_move and source.parent_uuid and source.parent_uuid != node.parent_uuid:
            if source.parent_uuid not in self.index:
                return ProtocolResult(False, reason="destination parent not present")
            if not self._move_child_impl(
                    node_uuid, source.parent_uuid, None,
                    source.revision_origin, source.revision_seq):
                return ProtocolResult(False, reason="move not applicable")
        node.data = copy.deepcopy(source.data)
        node.weights = copy.deepcopy(source.weights)
        node.deleted = source.deleted
        node.revision_origin = source.revision_origin
        node.revision_seq = source.revision_seq
        # Preserve the source timestamp for protocol-v1 migrated revisions
        # whose sequence is zero and therefore still use the legacy fallback.
        node.updated_at = source.updated_at
        self.cascade_hash(node_uuid)
        # Adoption copies the complete remote revision, including its base -
        # set it after cascade so recomputing content_hash doesn't reset it.
        # The base parent is only meaningful when we adopt the move; a topic
        # root's parent is a local artifact, so leave it as-is.
        node.base_hash = source.base_hash
        if adopt_move:
            node.base_parent_uuid = source.base_parent_uuid
        return ProtocolResult(True, node)

    def adopt_subtree(self, tree: ProtocolNode, parent_uuid: str,
                      remove_descendant_duplicates: bool = False) -> ProtocolResult:
        if parent_uuid not in self.index:
            return ProtocolResult(False, reason="parent not found")
        adopted = ProtocolNode.from_dict(tree.to_dict())
        ok = self._adopt_subtree_impl(
            adopted,
            parent_uuid,
            remove_descendant_duplicates=remove_descendant_duplicates,
        )
        return ProtocolResult(ok, adopted if ok else None,
                              None if ok else "adopt failed")

    def remove_subtree_uuids(self, root_uuid: str, uuids: set[str]) -> ProtocolResult:
        root = self.index.get(root_uuid)
        if not root:
            return ProtocolResult(False, reason="root not found")
        changed = self._remove_subtree_uuids_impl(root, set(uuids))
        return ProtocolResult(True, changed)

    # Topic / tree helpers

    def attach_topic(self, tree: ProtocolNode, parent_uuid: str | None = None) -> ProtocolResult:
        existing = self.index.get(tree.uuid)
        if existing:
            return ProtocolResult(True, existing.uuid)
        parent = self.index.get(parent_uuid) if parent_uuid else self.root
        if not parent:
            return ProtocolResult(False, reason="parent not found")
        tree.parent_uuid = parent.uuid
        parent.children = [
            child for child in parent.children if child.uuid != tree.uuid
        ]
        parent.children.append(tree)
        self.index = {}
        self.index_subtree(self.root)
        self.cascade_hash(tree.uuid)
        return ProtocolResult(True, tree.uuid)

    # Internal tree mechanics

    def index_subtree(self, node: ProtocolNode) -> None:
        self.index[node.uuid] = node
        for child in node.children:
            self.index_subtree(child)

    def deindex_subtree(self, node: ProtocolNode) -> None:
        for child in node.children:
            self.deindex_subtree(child)
        self.index.pop(node.uuid, None)

    def cascade_hash(self, node_uuid: str | None) -> None:
        # Recompute hashes from node_uuid up to the root. A descendant change
        # only moves ancestors' subtree (state) hash - their own content_hash
        # is unchanged - and it never touches any base_hash: base advances
        # solely via _begin_revision on the directly edited node. This is why
        # a card edit no longer manufactures a revision of its column/board.
        current_uuid = node_uuid
        while current_uuid:
            node = self.index.get(current_uuid)
            if not node:
                break
            node.refresh_hashes()
            current_uuid = node.parent_uuid

    @staticmethod
    def _begin_revision(node: ProtocolNode,
                        revision_origin: str | None | object,
                        revision_seq: int | None = None) -> None:
        if revision_origin is _REVISION_ORIGIN_UNSET:
            return
        if node.revision_origin != revision_origin:
            # Snapshot the node's own content at the wave start. Callers run
            # this before the edit's hashes are recomputed, so content_hash
            # here is still the pre-edit value.
            node.base_hash = node.content_hash
            node.base_parent_uuid = node.parent_uuid
        node.revision_origin = revision_origin
        if revision_seq is not None:
            node.revision_seq = revision_seq

    def clone_subtree(self, node: ProtocolNode, parent_uuid: str | None,
                      revision_origin: str | None = None,
                      revision_seq: int = 0) -> ProtocolNode:
        clone = ProtocolNode(
            copy.deepcopy(node.data),
            copy.deepcopy(node.weights),
            parent_uuid,
            revision_origin,
            revision_seq,
        )
        for child in node.children:
            clone.children.append(self.clone_subtree(
                child, clone.uuid, revision_origin, revision_seq,
            ))
        clone.refresh_hashes_deep()
        return clone

    def _delete_impl(self, node_uuid: str,
                     revision_origin: str | None | object = _REVISION_ORIGIN_UNSET,
                     revision_seq: int | None = None) -> bool:
        node = self.index.get(node_uuid)
        if not node or node.parent_uuid is None or node.deleted:
            return False
        self._mark_deleted_cascade(node, revision_origin, revision_seq)
        node.refresh_hashes_deep()
        self.cascade_hash(node.parent_uuid)
        return True

    def _mark_deleted_cascade(self, node: ProtocolNode,
                              revision_origin: str | None | object = _REVISION_ORIGIN_UNSET,
                              revision_seq: int | None = None) -> None:
        if not node.deleted:
            self._begin_revision(node, revision_origin, revision_seq)
            node.deleted = True
            node.updated_at = now_iso()
        for child in node.children:
            self._mark_deleted_cascade(child, revision_origin, revision_seq)

    def _move_impl(self, source_uuid: str, destination_uuid: str,
                   revision_origin: str | None | object = _REVISION_ORIGIN_UNSET,
                   revision_seq: int | None = None) -> bool:
        return self._move_child_impl(
            source_uuid, destination_uuid, None, revision_origin, revision_seq,
        )

    def _move_child_impl(self, source_uuid: str, destination_uuid: str,
                          index: int | None = None,
                          revision_origin: str | None | object = _REVISION_ORIGIN_UNSET,
                          revision_seq: int | None = None) -> bool:
        node = self.index.get(source_uuid)
        destination = self.index.get(destination_uuid)
        if not node or not destination or node.parent_uuid is None:
            return False
        if node.uuid == destination.uuid or self.is_descendant(node, destination.uuid):
            return False
        old_parent = self.index.get(node.parent_uuid)
        if not old_parent:
            return False
        old_parent.children = [
            child for child in old_parent.children if child.uuid != node.uuid
        ]
        # A same-parent call is a reorder, not a move: leave the compound
        # move base alone.
        if node.parent_uuid != destination.uuid:
            self._begin_revision(node, revision_origin, revision_seq)
        node.parent_uuid = destination.uuid
        node.updated_at = now_iso()
        insert_at = len(destination.children) if index is None else max(0, min(index, len(destination.children)))
        destination.children.insert(insert_at, node)
        self.cascade_hash(old_parent.uuid)
        self.cascade_hash(destination.uuid)
        return True

    def _adopt_subtree_impl(self, adopted: ProtocolNode, parent_uuid: str,
                             remove_descendant_duplicates: bool = False) -> bool:
        parent = self.index.get(parent_uuid)
        if not parent:
            return False
        existing = self.index.get(adopted.uuid)
        if existing and parent_uuid in self.collect_subtree_uuids(existing):
            # The destination lives inside the subtree we'd be replacing, so
            # detaching `existing` would take the destination with it and
            # leave nothing to re-attach to - the node would vanish from the
            # tree. Refuse before mutating anything rather than fail halfway
            # through. No current caller can reach this (accept_peer_node
            # verifies the parent in the local index) - the guard makes that
            # an invariant of the function instead of a property of its
            # callers.
            return False
        touched_parent_uuids = {parent.uuid}
        adopted.parent_uuid = parent.uuid
        if remove_descendant_duplicates:
            duplicates = self.collect_subtree_uuids(adopted) - {adopted.uuid}
            self._remove_subtree_uuids_impl(self.root, duplicates)
        if existing:
            old_parent = self.index.get(existing.parent_uuid)
            self.deindex_subtree(existing)
            if old_parent:
                old_parent.children = [
                    child for child in old_parent.children
                    if child.uuid != adopted.uuid
                ]
                touched_parent_uuids.add(old_parent.uuid)
        parent = self.index.get(parent_uuid)
        if not parent:
            return False
        parent.children = [
            child for child in parent.children
            if child.uuid != adopted.uuid
        ]
        parent.children.append(adopted)
        self.index_subtree(adopted)
        for touched_uuid in touched_parent_uuids:
            self.cascade_hash(touched_uuid)
        return True

    collect_subtree_uuids = staticmethod(collect_subtree_uuids)

    def _remove_subtree_uuids_impl(self, root: ProtocolNode, uuids: set[str]) -> bool:
        changed = False
        kept = []
        for child in root.children:
            if child.uuid in uuids:
                self.deindex_subtree(child)
                changed = True
                continue
            changed = self._remove_subtree_uuids_impl(child, uuids) or changed
            kept.append(child)
        root.children = kept
        if changed:
            self.cascade_hash(root.uuid)
        return changed

    @staticmethod
    def is_descendant(root: ProtocolNode, uuid: str) -> bool:
        return any(
            child.uuid == uuid or ProtocolState.is_descendant(child, uuid)
            for child in root.children
        )
