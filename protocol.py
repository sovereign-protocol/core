"""
Pure Sovereign Perspective protocol.

Offered API:
  PRSPNode
    Tree node with stable content/state hashes and checked serialization.

  ProtocolState(author)
    Owns one local PRSP tree and offers atomic operations, proposal lifecycle,
    topic attachment, and pure proposal reconciliation helpers.

  AtomicOperation
    Structured operation descriptor for create_child, modify, delete, copy, move.

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


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


def own_hash(data: dict, weights: dict) -> str:
    return stable_hash({"data": data, "weights": weights})


@dataclass
class ProtocolResult:
    ok: bool
    value: Any = None
    reason: str | None = None


@dataclass(frozen=True)
class AtomicOperation:
    type: str
    payload: dict[str, Any]

    @staticmethod
    def create_child(parent_uuid: str, data: dict,
                     weights: dict | None = None) -> "AtomicOperation":
        return AtomicOperation("create_child", {
            "parent_uuid": parent_uuid,
            "data": copy.deepcopy(data),
            "weights": copy.deepcopy(weights or {}),
        })

    @staticmethod
    def modify(node_uuid: str, data: dict,
               weights: dict | None = None) -> "AtomicOperation":
        return AtomicOperation("modify", {
            "node_uuid": node_uuid,
            "data": copy.deepcopy(data),
            "weights": copy.deepcopy(weights or {}),
        })

    @staticmethod
    def delete(node_uuid: str) -> "AtomicOperation":
        return AtomicOperation("delete", {"node_uuid": node_uuid})

    @staticmethod
    def copy(source_uuid: str, destination_uuid: str) -> "AtomicOperation":
        return AtomicOperation("copy", {
            "source_uuid": source_uuid,
            "destination_uuid": destination_uuid,
        })

    @staticmethod
    def move(source_uuid: str, destination_uuid: str) -> "AtomicOperation":
        return AtomicOperation("move", {
            "source_uuid": source_uuid,
            "destination_uuid": destination_uuid,
        })

    @classmethod
    def from_dict(cls, payload: dict) -> "AtomicOperation":
        op_type = payload.get("type")
        data = copy.deepcopy(payload)
        data.pop("type", None)
        return cls(op_type, data)

    def to_dict(self) -> dict:
        payload = copy.deepcopy(self.payload)
        payload["type"] = self.type
        return payload


@dataclass
class Proposal:
    uuid: str
    author: str
    top_uuid: str
    base_hashes: dict[str, str]
    operations: list[AtomicOperation]
    acceptances: dict[str, str] = field(default_factory=dict)
    objections: dict[str, str] = field(default_factory=dict)
    status: str = "active"
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    integrated_at: str | None = None
    final_ack_pending: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "author": self.author,
            "top_uuid": self.top_uuid,
            "base_hashes": copy.deepcopy(self.base_hashes),
            "operations": [op.to_dict() for op in self.operations],
            "acceptances": copy.deepcopy(self.acceptances),
            "objections": copy.deepcopy(self.objections),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "integrated_at": self.integrated_at,
            "final_ack_pending": list(self.final_ack_pending),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Proposal":
        return cls(
            uuid=payload["uuid"],
            author=payload["author"],
            top_uuid=payload["top_uuid"],
            base_hashes=copy.deepcopy(payload.get("base_hashes") or {}),
            operations=[
                AtomicOperation.from_dict(op)
                for op in payload.get("operations", [])
            ],
            acceptances=copy.deepcopy(payload.get("acceptances", {})),
            objections=copy.deepcopy(payload.get("objections", {})),
            status=payload.get("status", "active"),
            created_at=payload.get("created_at", now_iso()),
            updated_at=payload.get("updated_at", now_iso()),
            integrated_at=payload.get("integrated_at"),
            final_ack_pending=list(payload.get("final_ack_pending") or []),
        )


class PRSPNode:
    def __init__(self, data: dict,
                 weights: dict[str, float] | None = None,
                 parent_uuid: str | None = None):
        self.uuid = str(uuid_mod.uuid4())
        self.created_at = now_iso()
        self.updated_at = now_iso()
        self.weights = copy.deepcopy(weights or {})
        self.proposals: list[Proposal] = []
        self.data = copy.deepcopy(data)
        self.parent_uuid = parent_uuid
        self.deleted = False
        self.children: list[PRSPNode] = []
        self.content_hash = ""
        self.state_hash = ""
        self.refresh_hashes()
        self.previous_hash = self.state_hash
        self.previous_parent_uuid = self.parent_uuid
        self.reaffirmed_hash: str | None = None
        self.reaffirmed_parent_uuid: str | None = None

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

    def compute_own_hash(self) -> str:
        return own_hash(self.data, self.weights)

    def refresh_hashes(self) -> None:
        new_content_hash = self.recompute_content_hash()
        new_state_hash = state_hash(
            new_content_hash,
            sorted(child.state_hash for child in self.children),
        )
        self.content_hash = new_content_hash
        self.state_hash = new_state_hash

    def refresh_hashes_deep(self) -> None:
        for child in self.children:
            child.refresh_hashes_deep()
        self.refresh_hashes()

    def live_children(self) -> list["PRSPNode"]:
        return [child for child in self.children if not child.deleted]

    def is_reaffirmed(self) -> bool:
        return (
            self.reaffirmed_hash == self.state_hash
            and self.reaffirmed_parent_uuid == self.parent_uuid
        )

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "content_hash": self.content_hash,
            "state_hash": self.state_hash,
            "previous_hash": self.previous_hash,
            "previous_parent_uuid": self.previous_parent_uuid,
            "reaffirmed_hash": self.reaffirmed_hash,
            "reaffirmed_parent_uuid": self.reaffirmed_parent_uuid,
            "weights": copy.deepcopy(self.weights),
            "proposals": [proposal.to_dict() for proposal in self.proposals],
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
        node.reaffirmed_hash = payload.get("reaffirmed_hash")
        node.reaffirmed_parent_uuid = payload.get("reaffirmed_parent_uuid")
        node.weights = copy.deepcopy(payload.get("weights", {}))
        node.proposals = [
            Proposal.from_dict(proposal)
            for proposal in payload.get("proposals", [])
        ]
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
        if self.change_blocked_by_active_proposal([parent_uuid]):
            return ProtocolResult(False, reason="blocked by active proposal")
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
        if self.change_blocked_by_active_proposal([node_uuid]):
            return ProtocolResult(False, reason="blocked by active proposal")
        node.previous_hash = node.state_hash
        node.data = copy.deepcopy(data)
        node.weights = copy.deepcopy(weights or {})
        node.updated_at = now_iso()
        self.cascade_hash(node_uuid)
        return ProtocolResult(True, True)

    def delete(self, node_uuid: str) -> ProtocolResult:
        if self.change_blocked_by_active_proposal([node_uuid]):
            return ProtocolResult(False, reason="blocked by active proposal")
        ok = self.delete_locked(node_uuid)
        return ProtocolResult(ok, ok, None if ok else "delete failed")

    def reaffirm(self, node_uuid: str) -> ProtocolResult:
        if self.change_blocked_by_active_proposal([node_uuid]):
            return ProtocolResult(False, reason="blocked by active proposal")
        ok = self.reaffirm_locked(node_uuid)
        return ProtocolResult(ok, ok, None if ok else "reaffirm failed")

    def reaffirm_locked(self, node_uuid: str) -> bool:
        node = self.index.get(node_uuid)
        if not node:
            return False
        if (
            node.reaffirmed_hash == node.state_hash
            and node.reaffirmed_parent_uuid == node.parent_uuid
        ):
            node.reaffirmed_hash = None
            node.reaffirmed_parent_uuid = None
        else:
            node.reaffirmed_hash = node.state_hash
            node.reaffirmed_parent_uuid = node.parent_uuid
        node.updated_at = now_iso()
        return True

    def copy(self, source_uuid: str, destination_uuid: str) -> ProtocolResult:
        source = self.index.get(source_uuid)
        destination = self.index.get(destination_uuid)
        if not source or not destination:
            return ProtocolResult(False, reason="source or destination not found")
        if self.change_blocked_by_active_proposal([destination_uuid]):
            return ProtocolResult(False, reason="blocked by active proposal")
        clone = self.clone_subtree(source, destination.uuid)
        destination.children.append(clone)
        self.index_subtree(clone)
        self.cascade_hash(destination.uuid)
        return ProtocolResult(True, clone)

    def move(self, source_uuid: str, destination_uuid: str) -> ProtocolResult:
        if self.change_blocked_by_active_proposal([source_uuid, destination_uuid]):
            return ProtocolResult(False, reason="blocked by active proposal")
        ok = self.move_locked(source_uuid, destination_uuid)
        return ProtocolResult(ok, ok, None if ok else "move failed")

    def move_child(self, source_uuid: str, destination_uuid: str,
                   index: int | None = None) -> ProtocolResult:
        if self.change_blocked_by_active_proposal([source_uuid, destination_uuid]):
            return ProtocolResult(False, reason="blocked by active proposal")
        ok = self.move_child_locked(source_uuid, destination_uuid, index)
        return ProtocolResult(ok, ok, None if ok else "move failed")

    def adopt_subtree(self, tree: PRSPNode, parent_uuid: str,
                      remove_descendant_duplicates: bool = False) -> ProtocolResult:
        if parent_uuid not in self.index:
            return ProtocolResult(False, reason="parent not found")
        affected = [parent_uuid, tree.uuid]
        if self.change_blocked_by_active_proposal(affected):
            return ProtocolResult(False, reason="blocked by active proposal")
        adopted = PRSPNode.from_dict(tree.to_dict())
        ok = self.adopt_subtree_locked(
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
        if self.change_blocked_by_active_proposal(list(uuids)):
            return ProtocolResult(False, reason="blocked by active proposal")
        changed = self.remove_subtree_uuids_locked(root, set(uuids))
        return ProtocolResult(True, changed)

    # Proposals

    def base_hashes_for_operations(self,
                                   operations: list[AtomicOperation]) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for operation in operations:
            if operation.type != "modify":
                continue
            node_uuid = operation.payload.get("node_uuid")
            node = self.index.get(node_uuid)
            if node and node_uuid not in hashes:
                hashes[node_uuid] = node.compute_own_hash()
        return hashes

    def base_hashes_match(self, base_hashes: dict[str, str]) -> bool:
        for node_uuid, recorded_hash in base_hashes.items():
            node = self.index.get(node_uuid)
            if not node or node.compute_own_hash() != recorded_hash:
                return False
        return True

    def propose(self, top_uuid: str,
                operations: list[AtomicOperation]) -> ProtocolResult:
        top = self.index.get(top_uuid)
        if not top:
            return ProtocolResult(False, reason="top not found")
        if self.proposal_inside_active_proposal(top_uuid):
            return ProtocolResult(False, reason="nested proposal blocked")
        if not self.operations_within_top(top, operations):
            return ProtocolResult(False, reason="operations outside top")
        proposal = Proposal(
            uuid=str(uuid_mod.uuid4()),
            author=self.author,
            top_uuid=top_uuid,
            base_hashes=self.base_hashes_for_operations(operations),
            operations=copy.deepcopy(operations),
        )
        top.proposals.append(proposal)
        top.updated_at = now_iso()
        self.cascade_hash(top.uuid)
        return ProtocolResult(True, proposal)

    def accept_proposal(self, proposal: Proposal) -> ProtocolResult:
        top = self.index.get(proposal.top_uuid)
        if not top:
            return ProtocolResult(False, reason="top not found")
        if self.proposal_inside_active_proposal(top.uuid):
            return ProtocolResult(False, reason="blocked by active proposal")
        local = self.find_proposal_on_node(top, proposal.uuid)
        if not local:
            local = Proposal.from_dict(proposal.to_dict())
            local.acceptances = {}
            local.objections = {}
            top.proposals.append(local)
        if not self.base_hashes_match(local.base_hashes):
            return ProtocolResult(False, reason="base hash mismatch")
        local.objections.pop(self.author, None)
        local.acceptances[self.author] = now_iso()
        local.updated_at = now_iso()
        top.updated_at = now_iso()
        self.cascade_hash(top.uuid)
        return ProtocolResult(True, True)

    def object_to_proposal(self, proposal: Proposal) -> ProtocolResult:
        top = self.index.get(proposal.top_uuid)
        if not top:
            return ProtocolResult(False, reason="top not found")
        local = self.find_proposal_on_node(top, proposal.uuid)
        if not local:
            local = Proposal.from_dict(proposal.to_dict())
            local.acceptances = {}
            local.objections = {}
            top.proposals.append(local)
        local.acceptances.pop(self.author, None)
        local.objections[self.author] = now_iso()
        local.updated_at = now_iso()
        top.updated_at = now_iso()
        self.cascade_hash(top.uuid)
        return ProtocolResult(True, True)

    def observe_proposal(self, proposal: Proposal | None) -> None:
        if not proposal:
            return
        top = self.index.get(proposal.top_uuid)
        if not top:
            return
        local = self.find_proposal_on_node(top, proposal.uuid)
        if local:
            local.acceptances.update(proposal.acceptances)
            local.objections.update(proposal.objections)
            local.updated_at = now_iso()
            self.cascade_hash(top.uuid)

    def integrate_proposal(self, proposal_uuid: str) -> ProtocolResult:
        found = self.find_proposal_tuple(proposal_uuid)
        if not found:
            return ProtocolResult(False, reason="proposal not found")
        top, proposal = found
        if proposal.author != self.author or proposal.status != "active":
            return ProtocolResult(False, reason="not author or not active")
        if not self.base_hashes_match(proposal.base_hashes):
            return ProtocolResult(False, reason="base hash mismatch")
        if proposal.objections:
            return ProtocolResult(False, reason="known objections")
        if not self.execute_operations_locked(top, proposal.operations):
            return ProtocolResult(False, reason="operation failed")
        proposal.status = "integrated"
        proposal.integrated_at = now_iso()
        proposal.updated_at = now_iso()
        proposal.final_ack_pending = sorted(proposal.acceptances.keys())
        top.updated_at = now_iso()
        self.cascade_hash(top.uuid if top.uuid in self.index else self.root.uuid)
        return ProtocolResult(True, True)

    def take_back_proposal(self, proposal_uuid: str) -> ProtocolResult:
        found = self.find_proposal_tuple(proposal_uuid)
        if not found:
            return ProtocolResult(False, reason="proposal not found")
        top, proposal = found
        if proposal.author != self.author or proposal.status != "active":
            return ProtocolResult(False, reason="not author or not active")
        proposal.status = "taken_back"
        proposal.updated_at = now_iso()
        proposal.final_ack_pending = sorted(
            set(proposal.acceptances) | set(proposal.objections)
        )
        top.updated_at = now_iso()
        self.cascade_hash(top.uuid)
        return ProtocolResult(True, True)

    def reconcile_final_proposal(self, proposal: Proposal | None) -> ProtocolResult:
        if not proposal or proposal.status not in ("integrated", "taken_back"):
            return ProtocolResult(False, reason="not final proposal")
        top = self.index.get(proposal.top_uuid)
        if not top:
            return ProtocolResult(False, reason="top not found")
        local = self.find_proposal_on_node(top, proposal.uuid)
        if not local:
            return ProtocolResult(False, reason="local proposal not found")
        accepted = self.author in local.acceptances
        if proposal.status == "integrated" and accepted:
            if not self.base_hashes_match(local.base_hashes):
                local.status = "integration_failed"
                local.updated_at = now_iso()
                self.cascade_hash(top.uuid)
                return ProtocolResult(False, reason="base hash mismatch")
            if not self.execute_operations_locked(top, local.operations):
                local.status = "integration_failed"
                local.updated_at = now_iso()
                self.cascade_hash(top.uuid)
                return ProtocolResult(False, reason="operation failed")
        top.proposals = [
            item for item in top.proposals if item.uuid != local.uuid
        ]
        top.updated_at = now_iso()
        self.cascade_hash(top.uuid if top.uuid in self.index else self.root.uuid)
        return ProtocolResult(True, True)

    def acknowledge_final_proposal_cleanup(self, proposal_uuid: str,
                                           peer_author: str) -> ProtocolResult:
        found = self.find_proposal_tuple(proposal_uuid)
        if not found:
            return ProtocolResult(False, reason="proposal not found")
        top, proposal = found
        proposal.final_ack_pending = [
            peer for peer in proposal.final_ack_pending
            if peer != peer_author
        ]
        if proposal.final_ack_pending:
            proposal.updated_at = now_iso()
            self.cascade_hash(top.uuid)
            return ProtocolResult(True, True)
        top.proposals = [
            item for item in top.proposals if item.uuid != proposal_uuid
        ]
        top.updated_at = now_iso()
        self.cascade_hash(top.uuid)
        return ProtocolResult(True, True)

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
        self.cascade_hash(parent.uuid)
        return ProtocolResult(True, tree.uuid)

    def find_proposal(self, proposal_uuid: str) -> Proposal | None:
        found = self.find_proposal_tuple(proposal_uuid)
        return found[1] if found else None

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

    def delete_locked(self, node_uuid: str) -> bool:
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
            node.updated_at = now_iso()
        for child in node.children:
            self._mark_deleted_cascade(child)

    def move_locked(self, source_uuid: str, destination_uuid: str) -> bool:
        return self.move_child_locked(source_uuid, destination_uuid, None)

    def move_child_locked(self, source_uuid: str, destination_uuid: str,
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
        node.previous_parent_uuid = node.parent_uuid
        node.parent_uuid = destination.uuid
        node.updated_at = now_iso()
        insert_at = len(destination.children) if index is None else max(0, min(index, len(destination.children)))
        destination.children.insert(insert_at, node)
        self.cascade_hash(old_parent.uuid)
        self.cascade_hash(destination.uuid)
        return True

    def adopt_subtree_locked(self, adopted: PRSPNode, parent_uuid: str,
                             remove_descendant_duplicates: bool = False) -> bool:
        parent = self.index.get(parent_uuid)
        if not parent:
            return False
        touched_parent_uuids = {parent.uuid}
        adopted.parent_uuid = parent.uuid
        if remove_descendant_duplicates:
            duplicates = self.collect_subtree_uuids(adopted) - {adopted.uuid}
            self.remove_subtree_uuids_locked(self.root, duplicates)
        existing = self.index.get(adopted.uuid)
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

    def collect_subtree_uuids(self, node: PRSPNode) -> set[str]:
        out = {node.uuid}
        for child in node.children:
            out.update(self.collect_subtree_uuids(child))
        return out

    def remove_subtree_uuids_locked(self, root: PRSPNode, uuids: set[str]) -> bool:
        changed = False
        kept = []
        for child in root.children:
            if child.uuid in uuids:
                self.deindex_subtree(child)
                changed = True
                continue
            changed = self.remove_subtree_uuids_locked(child, uuids) or changed
            kept.append(child)
        root.children = kept
        if changed:
            self.cascade_hash(root.uuid)
        return changed

    def execute_operations_locked(self, top: PRSPNode,
                                  operations: list[AtomicOperation]) -> bool:
        if not self.operations_within_top(top, operations):
            return False
        snapshot = self.root.to_dict()
        for operation in operations:
            if not self.execute_operation_locked(operation):
                self.root = PRSPNode.from_dict(snapshot)
                self.index = {}
                self.index_subtree(self.root)
                return False
        return True

    def execute_operation_locked(self, operation: AtomicOperation) -> bool:
        payload = operation.payload
        if operation.type == "create_child":
            parent = self.index.get(payload.get("parent_uuid"))
            if not parent:
                return False
            child = PRSPNode(
                payload.get("data", {}),
                payload.get("weights", {}),
                parent.uuid,
            )
            parent.children.append(child)
            self.index_subtree(child)
            self.cascade_hash(parent.uuid)
            return True
        if operation.type == "modify":
            node = self.index.get(payload.get("node_uuid"))
            if not node:
                return False
            node.previous_hash = node.state_hash
            node.data = copy.deepcopy(payload.get("data", {}))
            node.weights = copy.deepcopy(payload.get("weights", {}))
            node.updated_at = now_iso()
            self.cascade_hash(node.uuid)
            return True
        if operation.type == "delete":
            return self.delete_locked(payload.get("node_uuid"))
        if operation.type == "copy":
            source = self.index.get(payload.get("source_uuid"))
            destination = self.index.get(payload.get("destination_uuid"))
            if not source or not destination:
                return False
            clone = self.clone_subtree(source, destination.uuid)
            destination.children.append(clone)
            self.index_subtree(clone)
            self.cascade_hash(destination.uuid)
            return True
        if operation.type == "move":
            return self.move_locked(
                payload.get("source_uuid"),
                payload.get("destination_uuid"),
            )
        return False

    def change_blocked_by_active_proposal(self,
                                          affected_uuids: list[str]) -> bool:
        active_top_uuids = self.active_proposal_top_uuids(self.root)
        for affected_uuid in affected_uuids:
            affected = self.index.get(affected_uuid)
            if not affected:
                continue
            for top_uuid in active_top_uuids:
                top = self.index.get(top_uuid)
                if not top:
                    continue
                if (affected.uuid == top.uuid
                        or self.is_descendant(top, affected.uuid)
                        or self.is_descendant(affected, top.uuid)):
                    return True
        return False

    def active_proposal_top_uuids(self, node: PRSPNode) -> set[str]:
        out = set()
        if any(proposal.status == "active" for proposal in node.proposals):
            out.add(node.uuid)
        for child in node.children:
            out.update(self.active_proposal_top_uuids(child))
        return out

    def proposal_inside_active_proposal(self, top_uuid: str) -> bool:
        top = self.index.get(top_uuid)
        if not top:
            return False
        for active_uuid in self.active_proposal_top_uuids(self.root):
            if active_uuid == top_uuid:
                continue
            active_top = self.index.get(active_uuid)
            if not active_top:
                continue
            if (self.is_descendant(active_top, top_uuid)
                    or self.is_descendant(top, active_uuid)):
                return True
        return False

    def operations_within_top(self, top: PRSPNode,
                              operations: list[AtomicOperation]) -> bool:
        for operation in operations:
            keys = {
                "create_child": ["parent_uuid"],
                "modify": ["node_uuid"],
                "delete": ["node_uuid"],
                "copy": ["source_uuid", "destination_uuid"],
                "move": ["source_uuid", "destination_uuid"],
            }.get(operation.type)
            if not keys:
                return False
            for key in keys:
                node_uuid = operation.payload.get(key)
                if not node_uuid:
                    return False
                if node_uuid == top.uuid:
                    if operation.type in ("delete", "move"):
                        return False
                    continue
                if not self.is_descendant(top, node_uuid):
                    return False
        return True

    def find_proposal_tuple(self, proposal_uuid: str) -> tuple[PRSPNode, Proposal] | None:
        return self.find_proposal_tuple_in_node(self.root, proposal_uuid)

    def find_proposal_tuple_in_node(self, node: PRSPNode,
                                    proposal_uuid: str) -> tuple[PRSPNode, Proposal] | None:
        for proposal in node.proposals:
            if proposal.uuid == proposal_uuid:
                return node, proposal
        for child in node.children:
            found = self.find_proposal_tuple_in_node(child, proposal_uuid)
            if found:
                return found
        return None

    @staticmethod
    def find_proposal_on_node(node: PRSPNode, proposal_uuid: str) -> Proposal | None:
        for proposal in node.proposals:
            if proposal.uuid == proposal_uuid:
                return proposal
        return None

    @staticmethod
    def is_descendant(root: PRSPNode, uuid: str) -> bool:
        return any(
            child.uuid == uuid or ProtocolState.is_descendant(child, uuid)
            for child in root.children
        )
