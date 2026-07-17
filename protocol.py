"""
Pure Sovereign Perspective protocol.

Offered API:
  PRSPNode
    Tree node with stable content/state hashes and checked serialization.

  ProtocolState(author)
    Owns one local PRSP tree and offers atomic operations and topic
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


PERSPECTIVE_STATES = ("none", "kept_mine", "pushed_back")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def stable_hash(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:20]


def content_hash(data: dict, weights: dict, deleted: bool,
                 child_content_hashes: list[str]) -> str:
    return stable_hash({
        "data": data,
        "weights": weights,
        "deleted": deleted,
        "children": child_content_hashes,
    })


def state_hash(node_content_hash: str, child_state_hashes: list[str]) -> str:
    return stable_hash({
        "content_hash": node_content_hash,
        "children": child_state_hashes,
    })


def collect_subtree_uuids(node: "PRSPNode") -> set[str]:
    out = {node.uuid}
    for child in node.children:
        out.update(collect_subtree_uuids(child))
    return out


@dataclass
class ProtocolResult:
    ok: bool
    value: Any = None
    reason: str | None = None


class PRSPNode:
    def __init__(self, data: dict,
                 weights: dict[str, float] | None = None,
                 parent_uuid: str | None = None):
        self.uuid = str(uuid_mod.uuid4())
        self.created_at = now_iso()
        self.updated_at = now_iso()
        self.weights = copy.deepcopy(weights or {})
        self.data = copy.deepcopy(data)
        self.parent_uuid = parent_uuid
        self.deleted = False
        self.children: list[PRSPNode] = []
        self.content_hash = ""
        self.state_hash = ""
        self.refresh_hashes()
        self.previous_hash = self.state_hash
        self.previous_parent_uuid = self.parent_uuid
        self.perspective_state = "none"

    def recompute_content_hash(self) -> str:
        return content_hash(
            self.data,
            self.weights,
            self.deleted,
            sorted(child.content_hash for child in self.children),
        )

    def recompute_state_hash(self) -> str:
        return state_hash(
            self.content_hash,
            sorted(child.state_hash for child in self.children),
        )

    def refresh_hashes(self) -> None:
        self.content_hash = self.recompute_content_hash()
        self.state_hash = self.recompute_state_hash()

    def refresh_hashes_deep(self) -> None:
        for child in self.children:
            child.refresh_hashes_deep()
        self.refresh_hashes()

    def live_children(self) -> list["PRSPNode"]:
        return [child for child in self.children if not child.deleted]

    def is_kept_mine(self) -> bool:
        return self.perspective_state == "kept_mine"

    def is_pushed_back(self) -> bool:
        return self.perspective_state == "pushed_back"

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "content_hash": self.content_hash,
            "state_hash": self.state_hash,
            "previous_hash": self.previous_hash,
            "previous_parent_uuid": self.previous_parent_uuid,
            "perspective_state": self.perspective_state,
            "weights": copy.deepcopy(self.weights),
            "data": copy.deepcopy(self.data),
            "parent_uuid": self.parent_uuid,
            "deleted": self.deleted,
            "children": [child.to_dict() for child in self.children],
        }

    @classmethod
    def from_dict(cls, payload: dict, repair_hashes: bool = False) -> "PRSPNode":
        node = cls.__new__(cls)
        node.uuid = payload["uuid"]
        node.created_at = payload["created_at"]
        node.updated_at = payload["updated_at"]
        node.content_hash = payload["content_hash"]
        node.state_hash = payload["state_hash"]
        node.previous_hash = payload.get("previous_hash", payload["state_hash"])
        node.previous_parent_uuid = payload.get("previous_parent_uuid", payload.get("parent_uuid"))
        perspective_state = payload.get("perspective_state", "none")
        node.perspective_state = perspective_state if perspective_state in PERSPECTIVE_STATES else "none"
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


class ProtocolState:
    def __init__(self, author: str):
        self.author = author
        self.root = PRSPNode({"label": author})
        self.index: dict[str, PRSPNode] = {}
        self.index_subtree(self.root)

    # Atomic operations

    def create_child(self, parent_uuid: str, data: dict,
                     weights: dict | None = None) -> ProtocolResult:
        parent = self.index.get(parent_uuid)
        if not parent:
            return ProtocolResult(False, reason="parent not found")
        child = PRSPNode(data, weights, parent_uuid)
        parent.children.append(child)
        self.index_subtree(child)
        self.cascade_hash(parent_uuid)
        return ProtocolResult(True, child)

    def modify(self, node_uuid: str, data: dict,
               weights: dict | None = None) -> ProtocolResult:
        node = self.index.get(node_uuid)
        if not node:
            return ProtocolResult(False, reason="node not found")
        node.data = copy.deepcopy(data)
        node.weights = copy.deepcopy(weights or {})
        node.updated_at = now_iso()
        # cascade_hash records previous_hash - and only when the state_hash
        # actually changed, so a no-op modify cannot destroy the one-slot
        # history a lagging peer still needs for classification.
        self.cascade_hash(node_uuid)
        return ProtocolResult(True, True)

    def delete(self, node_uuid: str) -> ProtocolResult:
        ok = self._delete_impl(node_uuid)
        return ProtocolResult(ok, ok, None if ok else "delete failed")

    def set_perspective_state(self, node_uuid: str, state: str) -> ProtocolResult:
        ok = self._set_perspective_state_impl(node_uuid, state)
        return ProtocolResult(ok, ok, None if ok else "set_perspective_state failed")

    def _set_perspective_state_impl(self, node_uuid: str, state: str) -> bool:
        if state not in PERSPECTIVE_STATES:
            return False
        node = self.index.get(node_uuid)
        if not node:
            return False
        node.perspective_state = state
        node.updated_at = now_iso()
        return True

    def copy(self, source_uuid: str, destination_uuid: str) -> ProtocolResult:
        source = self.index.get(source_uuid)
        destination = self.index.get(destination_uuid)
        if not source or not destination:
            return ProtocolResult(False, reason="source or destination not found")
        clone = self.clone_subtree(source, destination.uuid)
        destination.children.append(clone)
        self.index_subtree(clone)
        self.cascade_hash(destination.uuid)
        return ProtocolResult(True, clone)

    def move(self, source_uuid: str, destination_uuid: str) -> ProtocolResult:
        ok = self._move_impl(source_uuid, destination_uuid)
        return ProtocolResult(ok, ok, None if ok else "move failed")

    def move_child(self, source_uuid: str, destination_uuid: str,
                   index: int | None = None) -> ProtocolResult:
        # NOTE on `index`: sibling *position* is not part of any hash
        # (content/state hashes sort their children), so it never syncs -
        # it only affects this local list's order. An app that wants order
        # to replicate must carry it in node data and sort on read, the way
        # kanban's `data.order` + _place_in_order does. Passing `index`
        # alone will silently look right locally and wrong on every peer.
        ok = self._move_child_impl(source_uuid, destination_uuid, index)
        return ProtocolResult(ok, ok, None if ok else "move failed")

    def adopt_subtree(self, tree: PRSPNode, parent_uuid: str,
                      remove_descendant_duplicates: bool = False) -> ProtocolResult:
        if parent_uuid not in self.index:
            return ProtocolResult(False, reason="parent not found")
        adopted = PRSPNode.from_dict(tree.to_dict())
        ok = self._adopt_subtree_impl(
            adopted,
            parent_uuid,
            remove_descendant_duplicates=remove_descendant_duplicates,
        )
        return ProtocolResult(ok, adopted if ok else None,
                              None if ok else "adopt failed")

    def replace_subtree(self, tree: PRSPNode) -> ProtocolResult:
        local = self.index.get(tree.uuid)
        if not local or not local.parent_uuid:
            return ProtocolResult(False, reason="node not found")
        return self.adopt_subtree(tree, local.parent_uuid)

    def remove_subtree_uuids(self, root_uuid: str, uuids: set[str]) -> ProtocolResult:
        root = self.index.get(root_uuid)
        if not root:
            return ProtocolResult(False, reason="root not found")
        changed = self._remove_subtree_uuids_impl(root, set(uuids))
        return ProtocolResult(True, changed)

    # Topic / tree helpers

    def attach_topic(self, tree: PRSPNode, parent_uuid: str | None = None) -> ProtocolResult:
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

    def index_subtree(self, node: PRSPNode) -> None:
        self.index[node.uuid] = node
        for child in node.children:
            self.index_subtree(child)

    def deindex_subtree(self, node: PRSPNode) -> None:
        for child in node.children:
            self.deindex_subtree(child)
        self.index.pop(node.uuid, None)

    def cascade_hash(self, node_uuid: str | None) -> None:
        current_uuid = node_uuid
        while current_uuid:
            node = self.index.get(current_uuid)
            if not node:
                break
            old_state_hash = node.state_hash
            node.refresh_hashes()
            if node.state_hash != old_state_hash:
                node.previous_hash = old_state_hash
                node.perspective_state = "none"
            current_uuid = node.parent_uuid

    def clone_subtree(self, node: PRSPNode, parent_uuid: str | None) -> PRSPNode:
        clone = PRSPNode(
            copy.deepcopy(node.data),
            copy.deepcopy(node.weights),
            parent_uuid,
        )
        for child in node.children:
            clone.children.append(self.clone_subtree(child, clone.uuid))
        clone.refresh_hashes_deep()
        return clone

    def _delete_impl(self, node_uuid: str) -> bool:
        node = self.index.get(node_uuid)
        if not node or node.parent_uuid is None or node.deleted:
            return False
        self._mark_deleted_cascade(node)
        node.refresh_hashes_deep()
        self.cascade_hash(node.parent_uuid)
        return True

    def _mark_deleted_cascade(self, node: PRSPNode) -> None:
        if not node.deleted:
            node.previous_hash = node.state_hash
            node.deleted = True
            node.perspective_state = "none"
            node.updated_at = now_iso()
        for child in node.children:
            self._mark_deleted_cascade(child)

    def _move_impl(self, source_uuid: str, destination_uuid: str) -> bool:
        return self._move_child_impl(source_uuid, destination_uuid, None)

    def _move_child_impl(self, source_uuid: str, destination_uuid: str,
                          index: int | None = None) -> bool:
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
        # A same-parent call is a reorder, not a move: leave the one-slot
        # move history (and any keep_mine) alone so a lagging peer can still
        # attribute the last real move.
        if node.parent_uuid != destination.uuid:
            node.previous_parent_uuid = node.parent_uuid
            node.perspective_state = "none"
        node.parent_uuid = destination.uuid
        node.updated_at = now_iso()
        insert_at = len(destination.children) if index is None else max(0, min(index, len(destination.children)))
        destination.children.insert(insert_at, node)
        self.cascade_hash(old_parent.uuid)
        self.cascade_hash(destination.uuid)
        return True

    def _adopt_subtree_impl(self, adopted: PRSPNode, parent_uuid: str,
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
            # through. No current caller can reach this (replace_subtree
            # uses the node's own parent; accept_peer_node verifies the
            # parent in the local index) - the guard makes that an invariant
            # of the function instead of a property of its callers.
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

    def _remove_subtree_uuids_impl(self, root: PRSPNode, uuids: set[str]) -> bool:
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
    def is_descendant(root: PRSPNode, uuid: str) -> bool:
        return any(
            child.uuid == uuid or ProtocolState.is_descendant(child, uuid)
            for child in root.children
        )
