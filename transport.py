#!/usr/bin/env python3
"""
Sovereign Perspective Protocol  Transport Layer

Each Sovereign Individual (SI) owns a Perspective (PRSP): a Merkle tree
rooted at themselves. Other SIs get read-only access to subtrees via UUID
as capability key. There is no way to modify another SI's PRSP from outside.

Operations on a PRSP:
  create_child   add a child node; parent hash cascades up
  modify         update data/weights; updated_at set; hash cascades up
  delete         remove node and all descendants; parent hash cascades up
  copy           copy a subtree under another parent with fresh UUIDs
  move           move a subtree under another parent

Sync protocol (delta-first):
  mutate  ping peers {topic_uuid, topic_state_hash, changed_uuid}
  peer:   if state hash mismatch  GET /p2p/subtree/<changed_uuid>
"""

import hashlib
import json
import logging
import os
import threading
import uuid as uuid_mod
import copy as copy_mod
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)


def log_error(message: str, error: Exception | None = None) -> None:
    if error:
        print(f"[transport] {message}: {error}", flush=True)
    else:
        print(f"[transport] {message}", flush=True)


# - -  Utilities - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def stable_hash(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:20]


def content_hash(data: dict, weights: dict, child_content_hashes: list[str]) -> str:
    return stable_hash({
        "data": data,
        "weights": weights,
        "children": child_content_hashes,
    })


def state_hash(node_content_hash: str, child_state_hashes: list[str]) -> str:
    return stable_hash({
        "content_hash": node_content_hash,
        "children": child_state_hashes,
    })


# - -  PRSPNode - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

class PRSPNode:
    """
    A node in a Sovereign Perspective tree.

    Metadata (transport-layer owned):
      uuid, created_at, updated_at, content_hash, state_hash,
      weights, proposals, parent_uuid

    Content (application-layer owned):
      data   arbitrary dict; 'type' key conventionally carries node semantics
    """

    def __init__(self, data: dict,
                 weights: dict[str, float] | None = None,
                 parent_uuid: str | None = None):
        self.uuid:        str               = str(uuid_mod.uuid4())
        self.created_at:  str               = now_iso()
        self.updated_at:  str               = now_iso()
        self.weights:     dict[str, float]  = weights or {}
        self.proposals:   list[dict]        = []
        self.data:        dict              = data
        self.parent_uuid: str | None        = parent_uuid
        self.children:    list[PRSPNode]    = []
        self.content_hash: str              = ""
        self.state_hash:   str              = ""
        self._refresh_hashes()

    # - -  Hashing - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

    def _recompute_content_hash(self) -> str:
        return content_hash(self.data, self.weights,
                            sorted(c.content_hash for c in self.children))

    def _recompute_state_hash(self) -> str:
        return state_hash(self.content_hash,
                          sorted(c.state_hash for c in self.children))

    def _refresh_hashes(self) -> None:
        self.content_hash = self._recompute_content_hash()
        self.state_hash = self._recompute_state_hash()

    # - -  Serialisation - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

    def to_dict(self) -> dict:
        return {
            "uuid":        self.uuid,
            "created_at":  self.created_at,
            "updated_at":  self.updated_at,
            "content_hash": self.content_hash,
            "state_hash":   self.state_hash,
            "weights":     self.weights,
            "proposals":   self.proposals,
            "data":        self.data,
            "parent_uuid": self.parent_uuid,
            "children":    [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PRSPNode":
        node             = cls.__new__(cls)
        node.uuid        = d["uuid"]
        node.created_at  = d["created_at"]
        node.updated_at  = d["updated_at"]
        node.content_hash = d["content_hash"]
        node.state_hash   = d["state_hash"]
        node.weights     = d.get("weights", {})
        node.proposals   = d.get("proposals", [])
        node.data        = d["data"]
        node.parent_uuid = d.get("parent_uuid")
        node.children    = [cls.from_dict(c) for c in d.get("children", [])]
        computed_content_hash = node._recompute_content_hash()
        if node.content_hash != computed_content_hash:
            raise ValueError(
                f"invalid content_hash for node {node.uuid}: "
                f"expected {computed_content_hash}, got {node.content_hash}"
            )
        computed_state_hash = node._recompute_state_hash()
        if node.state_hash != computed_state_hash:
            raise ValueError(
                f"invalid state_hash for node {node.uuid}: "
                f"expected {computed_state_hash}, got {node.state_hash}"
            )
        return node


# - -  TransportLayer - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

class TransportLayer:

    def __init__(self, port: int):
        self.address = f"http://127.0.0.1:{port}"
        self.lock    = threading.RLock()
        self.pool    = ThreadPoolExecutor(max_workers=8, thread_name_prefix="sov")

        # My PRSP  root is the SI node itself
        self.prsp   = PRSPNode(data={"label": self.address})
        self._index: dict[str, PRSPNode] = {}
        self._index_subtree(self.prsp)

        # Network
        self.members:          set[str]                      = {self.address}
        self.peer_perspectives: dict[str, PRSPNode | None]   = {}
        self.peer_topics:       dict[str, str]                = {}

    def save_prsp(self, path: str) -> None:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{path}.tmp"
        with self.lock:
            payload = self.prsp.to_dict()
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)

    def load_prsp(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        with open(path, encoding="utf-8") as f:
            node = PRSPNode.from_dict(json.load(f))
        with self.lock:
            self.prsp = node
            self._index = {}
            self._index_subtree(self.prsp)
        return True

    # - -  Index management - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

    def _index_subtree(self, node: PRSPNode) -> None:
        self._index[node.uuid] = node
        for child in node.children:
            self._index_subtree(child)

    def _deindex_subtree(self, node: PRSPNode) -> None:
        for child in node.children:
            self._deindex_subtree(child)
        self._index.pop(node.uuid, None)

    def _cascade_hash(self, node_uuid: str) -> None:
        """Recompute hashes bottom-up from node_uuid to root."""
        current_uuid = node_uuid
        while current_uuid:
            node = self._index.get(current_uuid)
            if not node:
                break
            node._refresh_hashes()
            current_uuid = node.parent_uuid

    def _ancestor_uuids(self, node_uuid: str | None) -> list[str]:
        out = []
        current_uuid = node_uuid
        while current_uuid:
            out.append(current_uuid)
            node = self._index.get(current_uuid)
            current_uuid = node.parent_uuid if node else None
        return out

    def _lowest_common_ancestor_uuid(self, first_uuid: str, second_uuid: str) -> str | None:
        second_ancestors = set(self._ancestor_uuids(second_uuid))
        for uuid in self._ancestor_uuids(first_uuid):
            if uuid in second_ancestors:
                return uuid
        return None

    def _topic_ancestor_for(self, node_uuid: str) -> str | None:
        topic_uuids = set(self.peer_topics.values())
        for uuid in self._ancestor_uuids(node_uuid):
            if uuid in topic_uuids:
                return uuid
        return None

    def _sync_root_for_structural_change(self, first_parent_uuid: str,
                                         second_parent_uuid: str) -> str:
        common_uuid = self._lowest_common_ancestor_uuid(first_parent_uuid, second_parent_uuid)
        if not common_uuid:
            return second_parent_uuid
        topic_uuid = self._topic_ancestor_for(common_uuid)
        return topic_uuid or common_uuid

    # - -  PRSP Operations - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

    def create_child(self, parent_uuid: str, data: dict,
                     weights: dict[str, float] | None = None) -> "PRSPNode | None":
        with self.lock:
            parent = self._index.get(parent_uuid)
            if not parent or self._change_blocked_by_active_proposal([parent_uuid]):
                return None
            child = PRSPNode(data=data, weights=weights, parent_uuid=parent_uuid)
            parent.children.append(child)
            self._index_subtree(child)
            self._cascade_hash(parent_uuid)
            changed = parent_uuid

        self._trigger_sync(changed)
        return child

    def modify(self, node_uuid: str, data: dict,
               weights: dict[str, float] | None = None) -> bool:
        """Replace data and/or weights. Children/parents untouched. Sets updated_at."""
        with self.lock:
            node = self._index.get(node_uuid)
            if not node or self._change_blocked_by_active_proposal([node_uuid]):
                return False
            node.data = dict(data or {})
            if weights is not None:
                node.weights = dict(weights or {})
            node.updated_at = now_iso()
            self._cascade_hash(node_uuid)
            changed = node_uuid

        self._trigger_sync(changed)
        return True

    def delete(self, node_uuid: str) -> bool:
        """Delete node and all descendants. Cannot delete root."""
        with self.lock:
            node = self._index.get(node_uuid)
            if not node or node.parent_uuid is None:
                return False
            if self._change_blocked_by_active_proposal([node_uuid]):
                return False
            parent = self._index.get(node.parent_uuid)
            if not parent:
                return False
            self._deindex_subtree(node)
            parent.children = [c for c in parent.children if c.uuid != node_uuid]
            self._cascade_hash(parent.uuid)
            changed = parent.uuid

        self._trigger_sync(changed)
        return True

    def copy(self, source_uuid: str, destination_uuid: str) -> PRSPNode | None:
        """Copy source subtree under destination with fresh UUIDs."""
        with self.lock:
            source = self._index.get(source_uuid)
            destination = self._index.get(destination_uuid)
            if (not source or not destination
                    or self._change_blocked_by_active_proposal([destination_uuid])):
                return None
            clone = self._clone_subtree(source, destination.uuid)
            destination.children.append(clone)
            self._index_subtree(clone)
            self._cascade_hash(destination.uuid)
            changed = destination.uuid

        self._trigger_sync(changed)
        return clone

    def move(self, source_uuid: str, destination_uuid: str) -> bool:
        """Move source subtree under destination. Cannot move root or create cycles."""
        with self.lock:
            node = self._index.get(source_uuid)
            destination = self._index.get(destination_uuid)
            if not node or not destination or node.parent_uuid is None:
                return False
            if self._change_blocked_by_active_proposal([source_uuid, destination_uuid]):
                return False
            if node.uuid == destination.uuid or self._is_descendant(node, destination.uuid):
                return False
            old_parent = self._index.get(node.parent_uuid)
            if not old_parent:
                return False
            if old_parent.uuid == destination.uuid:
                return True
            changed = self._sync_root_for_structural_change(old_parent.uuid, destination.uuid)
            old_parent.children = [
                child for child in old_parent.children if child.uuid != node.uuid
            ]
            node.parent_uuid = destination.uuid
            node.updated_at = now_iso()
            destination.children.append(node)
            self._cascade_hash(old_parent.uuid)
            self._cascade_hash(destination.uuid)

        self._trigger_sync(changed)
        return True

    def _clone_subtree(self, node: PRSPNode, parent_uuid: str | None) -> PRSPNode:
        clone = PRSPNode(
            data=copy_mod.deepcopy(node.data),
            weights=copy_mod.deepcopy(node.weights),
            parent_uuid=parent_uuid,
        )
        for child in node.children:
            clone.children.append(self._clone_subtree(child, clone.uuid))
        clone._refresh_hashes()
        return clone

    def _change_blocked_by_active_proposal(self, affected_uuids: list[str]) -> bool:
        active_top_uuids = self._active_proposal_top_uuids(self.prsp)
        for affected_uuid in affected_uuids:
            affected = self._index.get(affected_uuid)
            if not affected:
                continue
            for top_uuid in active_top_uuids:
                top = self._index.get(top_uuid)
                if not top:
                    continue
                if (affected.uuid == top.uuid
                        or self._is_descendant(top, affected.uuid)
                        or self._is_descendant(affected, top.uuid)):
                    return True
        return False

    def _active_proposal_top_uuids(self, node: PRSPNode) -> set[str]:
        out = set()
        for proposal in node.proposals:
            if proposal.get("status") == "active":
                out.add(node.uuid)
        for child in node.children:
            out.update(self._active_proposal_top_uuids(child))
        return out

    # - -  Proposals - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

    def propose(self, top_uuid: str, operations: list[dict]) -> dict | None:
        with self.lock:
            top = self._index.get(top_uuid)
            if not top or self._proposal_inside_active_proposal(top_uuid):
                return None
            if not self._operations_within_top(top, operations):
                return None
            proposal = {
                "uuid": str(uuid_mod.uuid4()),
                "author": self.address,
                "top_uuid": top_uuid,
                "base_state_hash": top.state_hash,
                "operations": copy_mod.deepcopy(operations or []),
                "acceptances": {},
                "objections": {},
                "status": "active",
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
            top.proposals.append(proposal)
            top.updated_at = now_iso()
            changed = top_uuid

        self._trigger_sync(changed)
        return proposal

    def amend_proposal(self, proposal_uuid: str, operations: list[dict]) -> bool:
        with self.lock:
            found = self._find_local_proposal(proposal_uuid)
            if not found:
                return False
            top, proposal = found
            if proposal.get("author") != self.address or proposal.get("status") != "active":
                return False
            if self._proposal_inside_active_proposal(top.uuid):
                return False
            if top.state_hash != proposal.get("base_state_hash"):
                return False
            if not self._operations_within_top(top, operations):
                return False
            proposal["operations"] = copy_mod.deepcopy(operations or [])
            proposal["acceptances"] = {}
            proposal["objections"] = {}
            proposal["updated_at"] = now_iso()
            top.updated_at = now_iso()
            changed = top.uuid

        self._trigger_sync(changed)
        return True

    def take_back_proposal(self, proposal_uuid: str) -> bool:
        with self.lock:
            found = self._find_local_proposal(proposal_uuid)
            if not found:
                return False
            top, proposal = found
            if proposal.get("author") != self.address or proposal.get("status") != "active":
                return False
            if self._proposal_inside_active_proposal(top.uuid):
                return False
            proposal["status"] = "taken_back"
            proposal["updated_at"] = now_iso()
            changed = top.uuid

        self._trigger_sync(changed)
        return True

    def respond_to_proposal(self, proposal_uuid: str, response: str,
                            source_addr: str | None = None) -> bool:
        if response not in ("accept", "object"):
            return False
        with self.lock:
            proposal = self._get_visible_proposal(proposal_uuid, source_addr)
            if not proposal or proposal.get("status") != "active":
                return False
            top_uuid = proposal.get("top_uuid")
            top = self._index.get(top_uuid)
            if not top:
                return False
            if self._proposal_inside_active_proposal(top.uuid):
                return False
            local = self._find_proposal_on_node(top, proposal_uuid)
            if not local:
                local = copy_mod.deepcopy(proposal)
                local["acceptances"] = {}
                local["objections"] = {}
                top.proposals.append(local)
            if response == "accept":
                if top.state_hash != local.get("base_state_hash"):
                    return False
                local.setdefault("objections", {}).pop(self.address, None)
                local.setdefault("acceptances", {})[self.address] = now_iso()
            else:
                local.setdefault("acceptances", {}).pop(self.address, None)
                local.setdefault("objections", {})[self.address] = now_iso()
            local["updated_at"] = now_iso()
            changed = top.uuid

        self._trigger_sync(changed)
        return True

    def integrate_proposal(self, proposal_uuid: str) -> bool:
        with self.lock:
            found = self._find_local_proposal(proposal_uuid)
            if not found:
                return False
            top, proposal = found
            if proposal.get("author") != self.address or proposal.get("status") != "active":
                return False
            if self._proposal_inside_active_proposal(top.uuid):
                return False
            if top.state_hash != proposal.get("base_state_hash"):
                return False
            if self._known_objections(proposal_uuid):
                return False
            if not self._execute_operations_locked(top, proposal.get("operations") or []):
                return False
            proposal["status"] = "integrated"
            proposal["integrated_at"] = now_iso()
            proposal["updated_at"] = now_iso()
            changed = top.uuid if top.uuid in self._index else self.prsp.uuid

        self._trigger_sync(changed)
        return True

    def reconcile_integrations(self) -> bool:
        changed_uuids = []
        with self.lock:
            for _, peer_tree in self.peer_perspectives.items():
                if not peer_tree:
                    continue
                for proposal in self._collect_tree_proposals(peer_tree):
                    status = proposal.get("status")
                    if status not in ("integrated", "taken_back"):
                        continue
                    top = self._index.get(proposal.get("top_uuid"))
                    if not top:
                        continue
                    local = self._find_proposal_on_node(top, proposal.get("uuid"))
                    if not local:
                        continue
                    accepted = self.address in local.get("acceptances", {})
                    if status == "integrated" and accepted:
                        if top.state_hash != local.get("base_state_hash"):
                            local["status"] = "integration_failed"
                            local["updated_at"] = now_iso()
                            changed_uuids.append(top.uuid)
                            continue
                        if not self._execute_operations_locked(top, local.get("operations") or []):
                            local["status"] = "integration_failed"
                            local["updated_at"] = now_iso()
                            changed_uuids.append(top.uuid)
                            continue
                    top.proposals = [
                        p for p in top.proposals if p.get("uuid") != local.get("uuid")
                    ]
                    top.updated_at = now_iso()
                    changed_uuids.append(top.uuid if top.uuid in self._index else self.prsp.uuid)

        for changed_uuid in changed_uuids:
            self._trigger_sync(changed_uuid)
        return bool(changed_uuids)

    def list_proposals(self) -> list[dict]:
        with self.lock:
            out = []
            self._collect_proposal_items(out, self.address, self.prsp, local=True)
            for addr, tree in self.peer_perspectives.items():
                if tree:
                    self._collect_proposal_items(out, addr, tree, local=False)
            return out

    def _proposal_inside_active_proposal(self, top_uuid: str) -> bool:
        top = self._index.get(top_uuid)
        for active_uuid in self._active_proposal_top_uuids(self.prsp):
            active_top = self._index.get(active_uuid)
            if active_top and active_uuid != top_uuid and self._is_descendant(active_top, top_uuid):
                return True
            if top and active_uuid != top_uuid and self._is_descendant(top, active_uuid):
                return False
        return False

    def _operations_within_top(self, top: PRSPNode, operations: list[dict]) -> bool:
        for op in operations or []:
            op_type = op.get("type")
            keys = {
                "create_child": ["parent_uuid"],
                "modify": ["node_uuid"],
                "delete": ["node_uuid"],
                "copy": ["source_uuid", "destination_uuid"],
                "move": ["source_uuid", "destination_uuid"],
            }.get(op_type)
            if not keys:
                return False
            for key in keys:
                node_uuid = op.get(key)
                if not node_uuid:
                    return False
                if node_uuid == top.uuid:
                    if op_type in ("delete", "move"):
                        return False
                    continue
                if not self._is_descendant(top, node_uuid):
                    return False
        return True

    def _execute_operations_locked(self, top: PRSPNode, operations: list[dict]) -> bool:
        if not self._operations_within_top(top, operations):
            return False
        snapshot = self.prsp.to_dict()
        for op in operations:
            if not self._execute_operation_locked(op):
                self.prsp = PRSPNode.from_dict(snapshot)
                self._index = {}
                self._index_subtree(self.prsp)
                return False
        return True

    def _execute_operation_locked(self, op: dict) -> bool:
        op_type = op.get("type")
        if op_type == "create_child":
            parent = self._index.get(op.get("parent_uuid"))
            if not parent:
                return False
            child = PRSPNode(
                data=op.get("data", {}),
                weights=op.get("weights"),
                parent_uuid=parent.uuid,
            )
            parent.children.append(child)
            self._index_subtree(child)
            self._cascade_hash(parent.uuid)
            return True
        if op_type == "modify":
            node = self._index.get(op.get("node_uuid"))
            if not node:
                return False
            if "data" in op:
                node.data = dict(op.get("data") or {})
            if "weights" in op:
                node.weights = dict(op.get("weights") or {})
            node.updated_at = now_iso()
            self._cascade_hash(node.uuid)
            return True
        if op_type == "delete":
            return self._delete_locked(op.get("node_uuid"))
        if op_type == "copy":
            source = self._index.get(op.get("source_uuid"))
            destination = self._index.get(op.get("destination_uuid"))
            if not source or not destination:
                return False
            clone = self._clone_subtree(source, destination.uuid)
            destination.children.append(clone)
            self._index_subtree(clone)
            self._cascade_hash(destination.uuid)
            return True
        if op_type == "move":
            return self._move_locked(op.get("source_uuid"), op.get("destination_uuid"))
        return False

    def _delete_locked(self, node_uuid: str) -> bool:
        node = self._index.get(node_uuid)
        if not node or node.parent_uuid is None:
            return False
        parent = self._index.get(node.parent_uuid)
        if not parent:
            return False
        self._deindex_subtree(node)
        parent.children = [child for child in parent.children if child.uuid != node.uuid]
        self._cascade_hash(parent.uuid)
        return True

    def _move_locked(self, source_uuid: str, destination_uuid: str) -> bool:
        node = self._index.get(source_uuid)
        destination = self._index.get(destination_uuid)
        if not node or not destination or node.parent_uuid is None:
            return False
        if node.uuid == destination.uuid or self._is_descendant(node, destination.uuid):
            return False
        old_parent = self._index.get(node.parent_uuid)
        if not old_parent:
            return False
        old_parent.children = [child for child in old_parent.children if child.uuid != node.uuid]
        node.parent_uuid = destination.uuid
        node.updated_at = now_iso()
        destination.children.append(node)
        self._cascade_hash(old_parent.uuid)
        self._cascade_hash(destination.uuid)
        return True

    def _known_objections(self, proposal_uuid: str) -> dict:
        objections = {}
        found = self._find_local_proposal(proposal_uuid)
        if found:
            objections.update(found[1].get("objections", {}))
        for _, tree in self.peer_perspectives.items():
            if not tree:
                continue
            proposal = self._find_proposal_in_tree(tree, proposal_uuid)
            if proposal:
                objections.update(proposal.get("objections", {}))
        return objections

    def _find_local_proposal(self, proposal_uuid: str) -> tuple[PRSPNode, dict] | None:
        return self._find_proposal_tuple(self.prsp, proposal_uuid)

    def _find_proposal_tuple(self, node: PRSPNode,
                             proposal_uuid: str) -> tuple[PRSPNode, dict] | None:
        for proposal in node.proposals:
            if proposal.get("uuid") == proposal_uuid:
                return node, proposal
        for child in node.children:
            found = self._find_proposal_tuple(child, proposal_uuid)
            if found:
                return found
        return None

    def _find_proposal_in_tree(self, node: PRSPNode, proposal_uuid: str) -> dict | None:
        found = self._find_proposal_tuple(node, proposal_uuid)
        return found[1] if found else None

    @staticmethod
    def _find_proposal_on_node(node: PRSPNode, proposal_uuid: str | None) -> dict | None:
        for proposal in node.proposals:
            if proposal.get("uuid") == proposal_uuid:
                return proposal
        return None

    def _get_visible_proposal(self, proposal_uuid: str,
                              source_addr: str | None) -> dict | None:
        if source_addr in (None, "", self.address):
            found = self._find_local_proposal(proposal_uuid)
            return found[1] if found else None
        tree = self.peer_perspectives.get(source_addr)
        return self._find_proposal_in_tree(tree, proposal_uuid) if tree else None

    def _collect_tree_proposals(self, node: PRSPNode) -> list[dict]:
        out = list(node.proposals)
        for child in node.children:
            out.extend(self._collect_tree_proposals(child))
        return out

    def _collect_proposal_items(self, out: list[dict], addr: str,
                                node: PRSPNode, local: bool) -> None:
        for proposal in node.proposals:
            out.append({
                "addr": addr,
                "local": local,
                "node_uuid": node.uuid,
                "node_label": node.data.get("name")
                              or node.data.get("title")
                              or node.data.get("label")
                              or "Node",
                "proposal": proposal,
            })
        for child in node.children:
            self._collect_proposal_items(out, addr, child, local)

    @staticmethod
    def _is_descendant(root: PRSPNode, uuid: str) -> bool:
        return any(
            child.uuid == uuid or TransportLayer._is_descendant(child, uuid)
            for child in root.children
        )

    def get_subtree(self, node_uuid: str) -> dict | None:
        with self.lock:
            node = self._index.get(node_uuid)
            if not node:
                return None
            return {"subtree": node.to_dict(), "parent_uuid": node.parent_uuid}

    def fetch_peer_subtree(self, peer_addr: str, node_uuid: str) -> PRSPNode:
        r = requests.get(f"{peer_addr}/p2p/subtree/{node_uuid}", timeout=5)
        r.raise_for_status()
        return PRSPNode.from_dict(r.json()["subtree"])

    def get_cached_peer_subtree(self, peer_addr: str, node_uuid: str) -> PRSPNode | None:
        with self.lock:
            tree = self.peer_perspectives.get(peer_addr)
            if not tree:
                return None
            node = self._find_in_tree(tree, node_uuid)
            if not node:
                return None
            return PRSPNode.from_dict(node.to_dict())

    # - -  Sync  outbound - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

    def _trigger_sync(self, changed_uuid: str) -> None:
        with self.lock:
            peers = set(self.members) - {self.address}
            pings = []
            for peer in peers:
                topic_uuid = self.peer_topics.get(peer)
                if not topic_uuid:
                    continue
                topic = self._index.get(topic_uuid)
                if not topic:
                    continue
                pings.append((peer, topic_uuid, topic.state_hash, changed_uuid))

        for peer, topic_uuid, topic_state_hash, changed in pings:
            self.pool.submit(self._ping, peer, topic_uuid, topic_state_hash, changed)

    def _ping(self, peer_addr: str, topic_uuid: str, topic_state_hash: str,
              changed_uuid: str) -> None:
        try:
            requests.post(
                f"{peer_addr}/p2p/ping",
                json={"from_addr": self.address,
                      "topic_uuid": topic_uuid,
                      "topic_state_hash": topic_state_hash,
                      "changed_uuid": changed_uuid},
                timeout=3,
            ).raise_for_status()
        except Exception as e:
            log_error(f"ping to {peer_addr} failed", e)

    # - -  Sync  inbound pull - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

    def _pull_subtree(self, peer_addr: str, node_uuid: str) -> None:
        try:
            r = requests.get(f"{peer_addr}/p2p/subtree/{node_uuid}", timeout=5)
            if r.status_code == 404:
                return
            payload      = r.json()
            subtree      = PRSPNode.from_dict(payload["subtree"])
            parent_uuid  = payload.get("parent_uuid")
            with self.lock:
                self._merge_subtree(peer_addr, subtree, parent_uuid)
        except Exception as e:
            log_error(f"pull_subtree from {peer_addr} failed", e)

    def _merge_subtree(self, peer_addr: str,
                        subtree: PRSPNode, parent_uuid: str | None) -> None:
        """Replace matching node in cached peer PRSP with incoming subtree."""
        cached = self.peer_perspectives.get(peer_addr)
        if cached is None:
            self.peer_perspectives[peer_addr] = subtree
            return

        subtree_uuids = self._collect_subtree_uuids(subtree)
        target = self._find_in_tree(cached, subtree.uuid)
        if target is None:
            for uuid in subtree_uuids:
                self._remove_uuid_from_tree(cached, uuid)
            parent = self._find_in_tree(cached, parent_uuid) if parent_uuid else None
            if parent:
                parent.children = [
                    child for child in parent.children
                    if child.uuid != subtree.uuid
                ]
                subtree.parent_uuid = parent.uuid
                parent.children.append(subtree)
                self._refresh_tree_hashes(cached)
                return
            self.peer_perspectives[peer_addr] = subtree
            return

        # In-place replacement  keeps object identity in the parent's children list
        self._remove_duplicate_subtree_uuids(cached, subtree_uuids, keep_uuid=subtree.uuid)
        target.created_at  = subtree.created_at
        target.updated_at  = subtree.updated_at
        target.content_hash = subtree.content_hash
        target.state_hash   = subtree.state_hash
        target.weights     = subtree.weights
        target.proposals   = subtree.proposals
        target.data        = subtree.data
        target.children    = subtree.children
        self._refresh_tree_hashes(cached)

    def _collect_subtree_uuids(self, node: PRSPNode) -> set[str]:
        out = {node.uuid}
        for child in node.children:
            out.update(self._collect_subtree_uuids(child))
        return out

    def _remove_uuid_from_tree(self, root: PRSPNode, uuid: str) -> bool:
        original_len = len(root.children)
        root.children = [child for child in root.children if child.uuid != uuid]
        removed = len(root.children) != original_len
        for child in root.children:
            removed = self._remove_uuid_from_tree(child, uuid) or removed
        return removed

    def _remove_duplicate_subtree_uuids(self, root: PRSPNode,
                                        subtree_uuids: set[str],
                                        keep_uuid: str) -> None:
        for uuid in subtree_uuids:
            if uuid != keep_uuid:
                self._remove_uuid_from_tree(root, uuid)

    @staticmethod
    def _find_in_tree(root: PRSPNode, uuid: str) -> "PRSPNode | None":
        if root.uuid == uuid:
            return root
        for child in root.children:
            found = TransportLayer._find_in_tree(child, uuid)
            if found:
                return found
        return None

    @staticmethod
    def _refresh_tree_hashes(node: PRSPNode) -> None:
        for child in node.children:
            TransportLayer._refresh_tree_hashes(child)
        node._refresh_hashes()

    # - -  P2P handlers (called by server routes) - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

    def p2p_ping(self, payload: dict) -> tuple[dict, int]:
        from_addr    = payload.get("from_addr")
        topic_uuid = payload.get("topic_uuid")
        topic_state_hash = payload.get("topic_state_hash")
        changed_uuid = payload.get("changed_uuid")
        if not from_addr:
            return {"status": "error", "reason": "missing from_addr"}, 400
        if not topic_uuid:
            return {"status": "error", "reason": "missing topic_uuid"}, 400

        with self.lock:
            active_topic_uuid = next(iter(self.peer_topics.values()), None)
            if active_topic_uuid and active_topic_uuid != topic_uuid:
                return {
                    "status": "error",
                    "reason": "already discussing a different topic",
                }, 409
            self.members.add(from_addr)            # accept unknown pingers
            self.peer_topics[from_addr] = topic_uuid
            cached      = self.peer_perspectives.get(from_addr)
            cached_state_hash = cached.state_hash if cached else None

        if changed_uuid:
            pull_uuid = changed_uuid
        elif cached_state_hash != topic_state_hash:
            pull_uuid = topic_uuid
        else:
            pull_uuid = None
        if pull_uuid:
            self.pool.submit(self._pull_subtree, from_addr, pull_uuid)

        return {"status": "ok"}, 200

    def p2p_join(self, payload: dict) -> tuple[dict, int]:
        from_addr = payload.get("from_addr")
        topic_uuid = payload.get("topic_uuid")
        known_members = set(payload.get("known_members") or [])
        if not from_addr:
            return {"status": "error", "reason": "missing from_addr"}, 400
        if not topic_uuid:
            return {"status": "error", "reason": "missing topic_uuid"}, 400

        with self.lock:
            active_topic_uuid = next(iter(self.peer_topics.values()), None)
            if active_topic_uuid and active_topic_uuid != topic_uuid:
                return {
                    "status": "error",
                    "reason": "already discussing a different topic",
                }, 409
            self.members.add(from_addr)
            self.peer_topics[from_addr] = topic_uuid
            self.peer_perspectives.pop(from_addr, None)
            for member in known_members:
                if member != self.address:
                    self.members.add(member)
                    self.peer_topics[member] = topic_uuid
                    if member != from_addr:
                        self.peer_perspectives.pop(member, None)
            members_snapshot = sorted(self.members)

        self.pool.submit(self._pull_subtree, from_addr, topic_uuid)
        for member in known_members:
            if member not in (self.address, from_addr):
                self.pool.submit(self._pull_subtree, member, topic_uuid)
        return {"status": "ok", "members": members_snapshot}, 200

    def p2p_announce(self, payload: dict) -> tuple[dict, int]:
        new_addr = payload.get("new_addr")
        topic_uuid = payload.get("topic_uuid")
        if not new_addr:
            return {"status": "error", "reason": "missing new_addr"}, 400
        if not topic_uuid:
            return {"status": "error", "reason": "missing topic_uuid"}, 400
        with self.lock:
            active_topic_uuid = next(iter(self.peer_topics.values()), None)
            if active_topic_uuid and active_topic_uuid != topic_uuid:
                return {
                    "status": "error",
                    "reason": "already discussing a different topic",
                }, 409
            self.members.add(new_addr)
            self.peer_topics[new_addr] = topic_uuid
            self.peer_perspectives.pop(new_addr, None)
        self.pool.submit(self._pull_subtree, new_addr, topic_uuid)
        return {"status": "ok"}, 200

    def p2p_leave(self, payload: dict) -> tuple[dict, int]:
        from_addr = payload.get("from_addr")
        with self.lock:
            self.members.discard(from_addr)
            self.peer_perspectives.pop(from_addr, None)
            self.peer_topics.pop(from_addr, None)
        return {"status": "ok"}, 200

    # - -  Network actions (called by server on behalf of UI) - - - - - - - - - - - - - - - - - - - - 

    def invite_to_discuss(self, peer_addr: str, topic_uuid: str) -> dict:
        try:
            if not topic_uuid:
                return {"status": "error", "reason": "topic_uuid is required"}
            log_error(f"inviting {peer_addr} to topic {topic_uuid}")
            r = requests.post(
                f"{peer_addr}/api/join_discussion",
                json={"address": self.address, "topic_uuid": topic_uuid},
                timeout=15,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log_error(f"invite to {peer_addr} failed", e)
            return {"status": "error", "reason": str(e)}

    def join_discussion(self, target_addr: str, topic_uuid: str) -> dict:
        try:
            if not topic_uuid:
                return {"status": "error", "reason": "topic_uuid is required"}
            log_error(f"joining {target_addr} on topic {topic_uuid}")
            with self.lock:
                known_members = sorted(self.members)
            payload = {
                "from_addr": self.address,
                "topic_uuid": topic_uuid,
                "known_members": known_members,
            }
            r = requests.post(
                f"{target_addr}/p2p/join",
                json=payload,
                timeout=10,
            )
            r.raise_for_status()
            members = r.json().get("members", [])

            with self.lock:
                for m in members:
                    self.members.add(m)

            # Announce ourselves to everyone the target knows (except target and self)
            for member in members:
                if member in (self.address, target_addr):
                    continue
                try:
                    announce_payload = {"new_addr": self.address}
                    announce_payload["topic_uuid"] = topic_uuid
                    requests.post(
                        f"{member}/p2p/announce",
                        json=announce_payload,
                        timeout=3,
                    )
                except Exception:
                    log_error(f"announce self to {member} failed")

            # Announce everyone to everyone so a newly connected discussion becomes a mesh.
            all_members = set(members) | {self.address, target_addr}
            for member in all_members:
                if member == self.address:
                    continue
                for other in all_members:
                    if other in (member, self.address):
                        continue
                    try:
                        requests.post(
                            f"{member}/p2p/announce",
                            json={"new_addr": other, "topic_uuid": topic_uuid},
                            timeout=3,
                        )
                    except Exception:
                        log_error(f"announce {other} to {member} failed")

            # Pull trees from all known peers
            for member in all_members:
                if member != self.address:
                    with self.lock:
                        self.peer_topics[member] = topic_uuid
                        self.peer_perspectives.pop(member, None)
                    self.pool.submit(self._pull_subtree, member, topic_uuid)

            return {"status": "ok", "members": members}
        except Exception as e:
            log_error(f"join discussion via {target_addr} failed", e)
            return {"status": "error", "reason": str(e)}

    def do_leave(self) -> None:
        with self.lock:
            peers           = set(self.members) - {self.address}
            peer_topics     = dict(self.peer_topics)
            self.members    = {self.address}
            self.peer_perspectives.clear()
            self.peer_topics.clear()

        for peer in peers:
            topic_uuid = peer_topics.get(peer)
            if not topic_uuid:
                continue
            for other in peers:
                if other == peer:
                    continue
                try:
                    requests.post(
                        f"{peer}/p2p/announce",
                        json={"new_addr": other, "topic_uuid": topic_uuid},
                        timeout=2,
                    )
                except Exception:
                    log_error(f"leave mesh announce to {peer} failed")

        for peer in peers:
            try:
                requests.post(f"{peer}/p2p/leave",
                              json={"from_addr": self.address}, timeout=2)
            except Exception:
                log_error(f"leave notification to {peer} failed")

    # - -  Info - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - 

    def get_network_info(self) -> dict:
        with self.lock:
            topic_uuid = next(iter(self.peer_topics.values()), None)
            peers = {
                addr: {"content_hash": tree.content_hash if tree else None,
                       "state_hash": tree.state_hash if tree else None,
                       "root_uuid": tree.uuid if tree else None,
                       "topic_uuid": self.peer_topics.get(addr)}
                for addr, tree in self.peer_perspectives.items()
            }
            return {
                "address":   self.address,
                "root_uuid": self.prsp.uuid,
                "root_content_hash": self.prsp.content_hash,
                "root_state_hash": self.prsp.state_hash,
                "members":   sorted(self.members),
                "peer_addresses": sorted(self.peer_perspectives.keys()),
                "topic_uuid": topic_uuid,
                "peers":     peers,
            }



