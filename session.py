"""
Sovereign session component.

Offered API:
  Session(address)
  start_discussion(topic_uuid)
  add_peer(peer_addr, topic_uuid)
  handle_ping(message)
  handle_join(message)
  handle_announce(message)
  handle_leave(message)
  apply_peer_subtree(peer_addr, subtree, parent_uuid)
  leave()
  get_network_info()
  protocol operation wrappers: create_child, modify, delete, copy, move,
    propose, amend_proposal, take_back_proposal, respond_to_proposal,
    integrate_proposal, reconcile_integrations

Used API:
  protocol.ProtocolState, protocol.PRSPNode, protocol.AtomicOperation,
  and protocol.Proposal only.

Transport contract:
  Session never sends data. It returns SessionEffect values that a server or
  transport adapter can execute using HTTP, local memory, relay, or another
  mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from protocol import AtomicOperation, PRSPNode, Proposal, ProtocolState


@dataclass(frozen=True)
class SessionEffect:
    type: str
    target: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionResult:
    status: str
    value: Any = None
    reason: str | None = None
    effects: list[SessionEffect] = field(default_factory=list)


class Session:
    def __init__(self, address: str):
        self.address = address
        self.protocol = ProtocolState(author=address)
        self.members: set[str] = {address}
        self.peer_topics: dict[str, str] = {}
        self.peer_perspectives: dict[str, PRSPNode | None] = {}
        self.active_topic_uuid: str | None = None

    # Discussion/session state

    def start_discussion(self, topic_uuid: str) -> SessionResult:
        if topic_uuid not in self.protocol.index:
            return SessionResult("error", reason="topic not found")
        if self.active_topic_uuid and self.active_topic_uuid != topic_uuid:
            return SessionResult(
                "error",
                reason="already discussing a different topic",
            )
        self.active_topic_uuid = topic_uuid
        return SessionResult("ok", value=topic_uuid)

    def add_peer(self, peer_addr: str, topic_uuid: str) -> None:
        self.members.add(peer_addr)
        self.peer_topics[peer_addr] = topic_uuid
        if self.active_topic_uuid is None:
            self.active_topic_uuid = topic_uuid

    def accept_topic_invitation(self, tree: PRSPNode) -> SessionResult:
        result = self.protocol.attach_topic(tree)
        if not result.ok:
            return SessionResult("error", reason=result.reason)
        self.active_topic_uuid = result.value
        return SessionResult("ok", value=result.value)

    def leave(self) -> SessionResult:
        peers = sorted(self.members - {self.address})
        peer_topics = dict(self.peer_topics)
        effects = []
        for peer in peers:
            topic_uuid = peer_topics.get(peer)
            if not topic_uuid:
                continue
            others = [other for other in peers if other != peer]
            if others:
                effects.append(SessionEffect(
                    "announce_peer",
                    peer,
                    {"new_addrs": others, "topic_uuid": topic_uuid},
                ))
        for peer in peers:
            effects.append(SessionEffect(
                "send_leave",
                peer,
                {"from_addr": self.address},
            ))
        self.members = {self.address}
        self.peer_topics.clear()
        self.peer_perspectives.clear()
        self.active_topic_uuid = None
        return SessionResult("ok", effects=effects)

    # Incoming session messages

    def handle_ping(self, message: dict) -> SessionResult:
        from_addr = message.get("from_addr")
        topic_uuid = message.get("topic_uuid")
        topic_state_hash = message.get("topic_state_hash")
        changed_uuid = message.get("changed_uuid")
        if not from_addr:
            return SessionResult("error", reason="missing from_addr")
        if not topic_uuid:
            return SessionResult("error", reason="missing topic_uuid")
        if self.active_topic_uuid and self.active_topic_uuid != topic_uuid:
            return SessionResult("error", reason="already discussing a different topic")

        self.add_peer(from_addr, topic_uuid)
        cached = self.peer_perspectives.get(from_addr)
        cached_state_hash = cached.state_hash if cached else None
        pull_uuid = topic_uuid if cached is None else changed_uuid
        if not pull_uuid and cached_state_hash != topic_state_hash:
            pull_uuid = topic_uuid

        effects = []
        if pull_uuid:
            effects.append(SessionEffect(
                "pull_subtree",
                from_addr,
                {"node_uuid": pull_uuid, "topic_uuid": topic_uuid},
            ))
        return SessionResult("ok", effects=effects)

    def handle_join(self, message: dict) -> SessionResult:
        from_addr = message.get("from_addr")
        topic_uuid = message.get("topic_uuid")
        known_members = set(message.get("known_members") or [])
        if not from_addr:
            return SessionResult("error", reason="missing from_addr")
        if not topic_uuid:
            return SessionResult("error", reason="missing topic_uuid")
        if self.active_topic_uuid and self.active_topic_uuid != topic_uuid:
            return SessionResult("error", reason="already discussing a different topic")

        self.add_peer(from_addr, topic_uuid)
        for member in sorted(known_members):
            if member == self.address:
                continue
            self.add_peer(member, topic_uuid)

        effects = [
            SessionEffect("pull_subtree", from_addr,
                          {"node_uuid": topic_uuid, "topic_uuid": topic_uuid})
        ]
        for member in sorted(known_members):
            if member not in (self.address, from_addr):
                effects.append(SessionEffect(
                    "pull_subtree",
                    member,
                    {"node_uuid": topic_uuid, "topic_uuid": topic_uuid},
                ))
        return SessionResult(
            "ok",
            value={"members": sorted(self.members)},
            effects=effects,
        )

    def handle_announce(self, message: dict) -> SessionResult:
        new_addr = message.get("new_addr")
        topic_uuid = message.get("topic_uuid")
        if not new_addr:
            return SessionResult("error", reason="missing new_addr")
        if not topic_uuid:
            return SessionResult("error", reason="missing topic_uuid")
        if self.active_topic_uuid and self.active_topic_uuid != topic_uuid:
            return SessionResult("error", reason="already discussing a different topic")
        self.add_peer(new_addr, topic_uuid)
        return SessionResult(
            "ok",
            effects=[SessionEffect(
                "pull_subtree",
                new_addr,
                {"node_uuid": topic_uuid, "topic_uuid": topic_uuid},
            )],
        )

    def handle_leave(self, message: dict) -> SessionResult:
        from_addr = message.get("from_addr")
        self.members.discard(from_addr)
        self.peer_topics.pop(from_addr, None)
        self.peer_perspectives.pop(from_addr, None)
        return SessionResult("ok")

    # Peer cache

    def apply_peer_subtree(self, peer_addr: str,
                           subtree: PRSPNode,
                           parent_uuid: str | None) -> None:
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
                subtree.parent_uuid = parent.uuid
                parent.children = [
                    child for child in parent.children
                    if child.uuid != subtree.uuid
                ]
                parent.children.append(subtree)
                self._refresh_tree_hashes(cached)
                return
            self.peer_perspectives[peer_addr] = subtree
            return

        self._remove_duplicate_subtree_uuids(cached, subtree_uuids, subtree.uuid)
        target.created_at = subtree.created_at
        target.updated_at = subtree.updated_at
        target.content_hash = subtree.content_hash
        target.state_hash = subtree.state_hash
        target.previous_state_hash = subtree.previous_state_hash
        target.weights = subtree.weights
        target.proposals = subtree.proposals
        target.data = subtree.data
        target.children = subtree.children
        self._refresh_tree_hashes(cached)

    def get_cached_peer_subtree(self, peer_addr: str, node_uuid: str) -> PRSPNode | None:
        tree = self.peer_perspectives.get(peer_addr)
        if not tree:
            return None
        node = self._find_in_tree(tree, node_uuid)
        return PRSPNode.from_dict(node.to_dict()) if node else None

    def analyze_peer_transitions(self, peer_addr: str,
                                 node_uuid: str | None = None) -> list[dict]:
        peer_root = self.peer_perspectives.get(peer_addr)
        if not peer_root:
            return []
        compare_uuid = node_uuid or peer_root.uuid
        peer_node = self._find_in_tree(peer_root, compare_uuid)
        local_node = self.protocol.index.get(compare_uuid)
        return self._analyze_transition_node(peer_addr, local_node, peer_node)

    # App-facing protocol wrappers

    def create_child(self, parent_uuid: str, data: dict,
                     weights: dict[str, float] | None = None) -> SessionResult:
        result = self.protocol.create_child(parent_uuid, data, weights)
        if not result.ok:
            return SessionResult("error", reason=result.reason)
        return SessionResult("ok", value=result.value,
                             effects=self._sync_effects(parent_uuid))

    def modify(self, node_uuid: str, data: dict,
               weights: dict[str, float] | None = None) -> SessionResult:
        result = self.protocol.modify(node_uuid, data, weights)
        return self._operation_result(result, node_uuid)

    def delete(self, node_uuid: str) -> SessionResult:
        node = self.protocol.index.get(node_uuid)
        parent_uuid = node.parent_uuid if node else None
        result = self.protocol.delete(node_uuid)
        return self._operation_result(result, parent_uuid or node_uuid)

    def copy(self, source_uuid: str, destination_uuid: str) -> SessionResult:
        result = self.protocol.copy(source_uuid, destination_uuid)
        if not result.ok:
            return SessionResult("error", reason=result.reason)
        return SessionResult("ok", value=result.value,
                             effects=self._sync_effects(destination_uuid))

    def move(self, source_uuid: str, destination_uuid: str) -> SessionResult:
        result = self.protocol.move(source_uuid, destination_uuid)
        return self._operation_result(result, self.active_topic_uuid or destination_uuid)

    def propose(self, top_uuid: str,
                operations: list[dict | AtomicOperation]) -> SessionResult:
        result = self.protocol.propose(
            top_uuid,
            self._coerce_operations(operations),
        )
        if not result.ok:
            return SessionResult("error", reason=result.reason)
        return SessionResult("ok", value=result.value,
                             effects=self._sync_effects(top_uuid))

    def amend_proposal(self, proposal_uuid: str,
                       operations: list[dict]) -> SessionResult:
        return SessionResult("error", reason="proposal amendment is not implemented")

    def take_back_proposal(self, proposal_uuid: str) -> SessionResult:
        result = self.protocol.take_back_proposal(proposal_uuid)
        return self._operation_result(result, self.active_topic_uuid)

    def respond_to_proposal(self, proposal_uuid: str, response: str,
                            source_addr: str | None = None) -> SessionResult:
        proposal = self._find_visible_proposal(proposal_uuid, source_addr)
        if not proposal:
            return SessionResult("error", reason="proposal not found")
        if response == "accept":
            result = self.protocol.accept_proposal(proposal)
        elif response == "object":
            result = self.protocol.object_to_proposal(proposal)
        else:
            return SessionResult("error", reason="unknown proposal response")
        return self._operation_result(result, self.active_topic_uuid)

    def integrate_proposal(self, proposal_uuid: str) -> SessionResult:
        result = self.protocol.integrate_proposal(proposal_uuid)
        return self._operation_result(result, self.active_topic_uuid)

    def reconcile_integrations(self) -> SessionResult:
        changed = False
        for tree in self.peer_perspectives.values():
            if not tree:
                continue
            for proposal in self._iter_tree_proposals(tree):
                if proposal.status not in ("integrated", "taken_back"):
                    continue
                result = self.protocol.reconcile_final_proposal(proposal)
                changed = changed or result.ok
        return SessionResult(
            "ok",
            value=changed,
            effects=self._sync_effects(self.active_topic_uuid) if changed else [],
        )

    def get_subtree(self, node_uuid: str) -> dict | None:
        node = self.protocol.index.get(node_uuid)
        if not node:
            return None
        return {
            "subtree": node.to_dict(),
            "parent_uuid": node.parent_uuid,
        }

    def get_network_info(self) -> dict:
        return {
            "address": self.address,
            "root_uuid": self.protocol.root.uuid,
            "root_content_hash": self.protocol.root.content_hash,
            "root_state_hash": self.protocol.root.state_hash,
            "members": sorted(self.members),
            "peer_addresses": sorted(self.peer_perspectives.keys()),
            "topic_uuid": self.active_topic_uuid,
            "peers": {
                addr: {
                    "content_hash": tree.content_hash if tree else None,
                    "state_hash": tree.state_hash if tree else None,
                    "root_uuid": tree.uuid if tree else None,
                    "topic_uuid": self.peer_topics.get(addr),
                }
                for addr, tree in self.peer_perspectives.items()
            },
        }

    # Internals

    def _operation_result(self, result, changed_uuid: str | None) -> SessionResult:
        if not result.ok:
            return SessionResult("error", reason=result.reason)
        return SessionResult("ok", value=True,
                             effects=self._sync_effects(changed_uuid))

    def _sync_effects(self, changed_uuid: str | None) -> list[SessionEffect]:
        topic_uuid = self.active_topic_uuid
        if not topic_uuid:
            return []
        topic = self.protocol.index.get(topic_uuid)
        if not topic:
            return []
        effects = []
        for peer in sorted(self.members - {self.address}):
            if self.peer_topics.get(peer) != topic_uuid:
                continue
            effects.append(SessionEffect(
                "send_ping",
                peer,
                {
                    "from_addr": self.address,
                    "topic_uuid": topic_uuid,
                    "topic_state_hash": topic.state_hash,
                    "changed_uuid": changed_uuid or topic_uuid,
                },
            ))
        return effects

    @staticmethod
    def _coerce_operations(
            operations: list[dict | AtomicOperation]) -> list[AtomicOperation]:
        out = []
        for operation in operations:
            if isinstance(operation, AtomicOperation):
                out.append(operation)
            else:
                out.append(AtomicOperation.from_dict(operation))
        return out

    def _find_visible_proposal(self, proposal_uuid: str,
                               source_addr: str | None) -> Proposal | None:
        local = self.protocol.find_proposal(proposal_uuid)
        if local:
            return local
        trees = []
        if source_addr:
            trees.append(self.peer_perspectives.get(source_addr))
        else:
            trees.extend(self.peer_perspectives.values())
        for tree in trees:
            if not tree:
                continue
            for proposal in self._iter_tree_proposals(tree):
                if proposal.uuid == proposal_uuid:
                    return proposal
        return None

    @staticmethod
    def _iter_tree_proposals(root: PRSPNode):
        for proposal in root.proposals:
            yield proposal
        for child in root.children:
            yield from Session._iter_tree_proposals(child)

    def _analyze_transition_node(self, peer_addr: str,
                                 local_node: PRSPNode | None,
                                 peer_node: PRSPNode | None) -> list[dict]:
        if not local_node and not peer_node:
            return []
        if not local_node:
            return [self._transition_event(
                "missing_local_node",
                peer_addr,
                None,
                peer_node,
            )]
        if not peer_node:
            return [self._transition_event(
                "missing_peer_node",
                peer_addr,
                local_node,
                None,
            )]

        child_events = []
        local_children = {child.uuid: child for child in local_node.children}
        peer_children = {child.uuid: child for child in peer_node.children}
        for child_uuid in sorted(set(local_children) | set(peer_children)):
            child_events.extend(self._analyze_transition_node(
                peer_addr,
                local_children.get(child_uuid),
                peer_children.get(child_uuid),
            ))

        if local_node.state_hash == peer_node.state_hash:
            event_type = (
                "agreement"
                if peer_node.previous_state_hash == local_node.state_hash
                else "accepted_change"
            )
        elif peer_node.previous_state_hash == local_node.state_hash:
            event_type = "intentional_change"
        elif local_node.previous_state_hash == peer_node.state_hash:
            event_type = "local_intentional_change"
        elif child_events and not any(
                event["type"] == "conflict" for event in child_events):
            event_type = "parallel_independent_changes"
        else:
            event_type = "conflict"

        return [
            self._transition_event(event_type, peer_addr, local_node, peer_node)
        ] + child_events

    @staticmethod
    def _transition_event(event_type: str, peer_addr: str,
                          local_node: PRSPNode | None,
                          peer_node: PRSPNode | None) -> dict:
        node = local_node or peer_node
        return {
            "type": event_type,
            "peer_addr": peer_addr,
            "node_uuid": node.uuid if node else None,
            "local_state_hash": local_node.state_hash if local_node else None,
            "local_previous_state_hash": (
                local_node.previous_state_hash if local_node else None
            ),
            "peer_state_hash": peer_node.state_hash if peer_node else None,
            "peer_previous_state_hash": (
                peer_node.previous_state_hash if peer_node else None
            ),
        }

    @staticmethod
    def _collect_subtree_uuids(node: PRSPNode) -> set[str]:
        out = {node.uuid}
        for child in node.children:
            out.update(Session._collect_subtree_uuids(child))
        return out

    @staticmethod
    def _remove_uuid_from_tree(root: PRSPNode, uuid: str) -> bool:
        original_len = len(root.children)
        root.children = [child for child in root.children if child.uuid != uuid]
        removed = len(root.children) != original_len
        for child in root.children:
            removed = Session._remove_uuid_from_tree(child, uuid) or removed
        return removed

    @staticmethod
    def _remove_duplicate_subtree_uuids(root: PRSPNode,
                                        subtree_uuids: set[str],
                                        keep_uuid: str) -> None:
        for uuid in subtree_uuids:
            if uuid != keep_uuid:
                Session._remove_uuid_from_tree(root, uuid)

    @staticmethod
    def _find_in_tree(root: PRSPNode, uuid: str | None) -> PRSPNode | None:
        if uuid is None:
            return None
        if root.uuid == uuid:
            return root
        for child in root.children:
            found = Session._find_in_tree(child, uuid)
            if found:
                return found
        return None

    @staticmethod
    def _refresh_tree_hashes(node: PRSPNode) -> None:
        for child in node.children:
            Session._refresh_tree_hashes(child)
        node.refresh_hashes()
