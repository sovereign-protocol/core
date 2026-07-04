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

import time
from dataclasses import dataclass, field
from typing import Any

from protocol import AtomicOperation, PRSPNode, Proposal, ProtocolState, stable_hash
from trace_log import TraceLogger


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


class ReadOnlyProtocolIndex:
    def __init__(self, protocol: ProtocolState):
        self._protocol = protocol

    def __contains__(self, node_uuid: str) -> bool:
        return node_uuid in self._protocol.index

    def __iter__(self):
        return iter(self._protocol.index)

    def __len__(self) -> int:
        return len(self._protocol.index)

    def get(self, node_uuid: str, default=None) -> PRSPNode | None:
        node = self._protocol.index.get(node_uuid)
        return Session._snapshot_node(node) if node else default

    def __getitem__(self, node_uuid: str) -> PRSPNode:
        node = self._protocol.index[node_uuid]
        return Session._snapshot_node(node)

    def keys(self):
        return self._protocol.index.keys()

    def values(self):
        for node in self._protocol.index.values():
            yield Session._snapshot_node(node)

    def items(self):
        for node_uuid, node in self._protocol.index.items():
            yield node_uuid, Session._snapshot_node(node)


class ReadOnlyProtocolView:
    def __init__(self, protocol: ProtocolState):
        self._protocol = protocol
        self.index = ReadOnlyProtocolIndex(protocol)

    @property
    def root(self) -> PRSPNode:
        return Session._snapshot_node(self._protocol.root)

    @property
    def author(self) -> str:
        return self._protocol.author


class Session:
    def __init__(self, address: str, trace: TraceLogger | None = None):
        self.address = address
        self.trace = trace or TraceLogger.disabled()
        self._protocol = ProtocolState(author=address)
        self.protocol = ReadOnlyProtocolView(self._protocol)
        self.members: set[str] = {address}
        self.peer_topics: dict[str, str] = {}
        self.peer_topic_sets: dict[str, set[str]] = {}
        self.peer_fetch_topic_sets: dict[str, set[str]] = {}
        self.peer_perspectives: dict[str, PRSPNode | None] = {}
        self.peer_status: dict[str, dict[str, Any]] = {}
        self.peer_sync_state: dict[str, dict[str, Any]] = {}
        self.shared_state_by_peer: dict[str, dict[str, list[str]]] = {}
        self.active_topic_uuids: set[str] = set()
        self.app_metadata: dict[str, Any] = {}
        self.deleted_node_uuids: dict[str, str] = {}

    @property
    def active_topic_uuid(self) -> str | None:
        return sorted(self.active_topic_uuids)[0] if self.active_topic_uuids else None

    @active_topic_uuid.setter
    def active_topic_uuid(self, value: str | None) -> None:
        self.active_topic_uuids = {value} if value else set()

    # Read-only protocol access / persistence

    def load_protocol_root(self, root: PRSPNode) -> None:
        self._protocol.root = root
        self._protocol.index = {}
        self._protocol.index_subtree(root)
        self.protocol = ReadOnlyProtocolView(self._protocol)

    def export_protocol_root(self) -> dict:
        return self._protocol.root.to_dict()

    def root_uuid(self) -> str:
        return self._protocol.root.uuid

    def has_node(self, node_uuid: str | None) -> bool:
        return bool(node_uuid and node_uuid in self._protocol.index)

    def node_state_hash(self, node_uuid: str) -> str | None:
        node = self._protocol.index.get(node_uuid)
        return node.state_hash if node else None

    def cached_peer_topic_state_hash(self, peer_addr: str, topic_uuid: str) -> str | None:
        cached = self.peer_perspectives.get(peer_addr)
        topic = self._find_in_tree(cached, topic_uuid) if cached else None
        return topic.state_hash if topic else None

    def node_content_hash(self, node_uuid: str) -> str | None:
        node = self._protocol.index.get(node_uuid)
        return node.content_hash if node else None

    def node_uuids(self) -> list[str]:
        return sorted(self._protocol.index)

    # Discussion/session state

    def start_discussion(self, topic_uuid: str) -> SessionResult:
        if topic_uuid not in self._protocol.index:
            return SessionResult("error", reason="topic not found")
        self.active_topic_uuids.add(topic_uuid)
        return SessionResult("ok", value=topic_uuid)

    def add_peer(self, peer_addr: str, topic_uuid: str,
                 fetch_from_peer: bool = True) -> None:
        if peer_addr == self.address:
            self.active_topic_uuids.add(topic_uuid)
            return
        self.members.add(peer_addr)
        self.peer_topic_sets.setdefault(peer_addr, set()).add(topic_uuid)
        if fetch_from_peer:
            self.peer_fetch_topic_sets.setdefault(peer_addr, set()).add(topic_uuid)
        self.peer_topics.setdefault(peer_addr, topic_uuid)
        self.peer_status.setdefault(peer_addr, self._new_peer_status())
        self.peer_sync_state.setdefault(peer_addr, self._new_peer_sync_state())
        self.active_topic_uuids.add(topic_uuid)

    def add_peer_topics(self, peer_addr: str, topic_uuids: list[str] | set[str],
                        fetch_from_peer: bool = True) -> None:
        for topic_uuid in sorted(set(topic_uuids)):
            self.add_peer(peer_addr, topic_uuid, fetch_from_peer=fetch_from_peer)

    def set_peer_fetch_topics(self, peer_addr: str, topic_uuids: list[str] | set[str]) -> None:
        if peer_addr == self.address:
            return
        self.members.add(peer_addr)
        self.peer_fetch_topic_sets[peer_addr] = set(topic_uuids)
        self.peer_status.setdefault(peer_addr, self._new_peer_status())
        self.peer_sync_state.setdefault(peer_addr, self._new_peer_sync_state())

    def fetch_topic_uuids(self, peer_addr: str) -> list[str]:
        topics = self.peer_fetch_topic_sets.get(peer_addr)
        if topics is None:
            topics = self.peer_topic_sets.get(peer_addr, set())
        return sorted(topics)

    def remove_peer_fetch_topic(self, peer_addr: str, topic_uuid: str) -> None:
        topics = self.peer_fetch_topic_sets.get(peer_addr)
        if not topics:
            return
        topics.discard(topic_uuid)

    def topic_members(self, topic_uuid: str) -> set[str]:
        members = {self.address} if topic_uuid in self.active_topic_uuids else set()
        for peer, topics in self.peer_topic_sets.items():
            if topic_uuid in topics:
                members.add(peer)
        return members

    def peers_for_topic(self, topic_uuid: str) -> list[str]:
        return sorted(
            peer for peer, topics in self.peer_topic_sets.items()
            if topic_uuid in topics
        )

    def topic_members_by_topic(self, topic_uuids: list[str] | set[str]) -> dict[str, list[str]]:
        return {
            topic_uuid: sorted(self.topic_members(topic_uuid))
            for topic_uuid in sorted(set(topic_uuids))
        }

    def accept_topic_invitation(self, tree: PRSPNode,
                                parent_uuid: str | None = None) -> SessionResult:
        result = self._protocol.attach_topic(
            tree,
            parent_uuid or self._other_perspectives_uuid(),
        )
        if not result.ok:
            return SessionResult("error", reason=result.reason)
        self.active_topic_uuids.add(result.value)
        return SessionResult("ok", value=result.value)

    def leave(self) -> SessionResult:
        peers = sorted(self.members - {self.address})
        peer_topics = {
            peer: set(self.peer_topic_sets.get(peer) or [])
            for peer in peers
        }
        effects = []
        for peer in peers:
            topic_uuids = peer_topics.get(peer) or set()
            if not topic_uuids:
                continue
            others = [other for other in peers if other != peer]
            if others:
                effects.append(SessionEffect(
                    "announce_peer",
                    peer,
                    {
                        "new_addrs": others,
                        "topic_uuids": sorted(topic_uuids),
                        "topic_uuid": sorted(topic_uuids)[0],
                    },
                ))
        for peer in peers:
            effects.append(SessionEffect(
                "send_leave",
                peer,
                {"from_addr": self.address},
            ))
        self.members = {self.address}
        self.peer_topics.clear()
        self.peer_topic_sets.clear()
        self.peer_fetch_topic_sets.clear()
        self.peer_perspectives.clear()
        self.peer_status.clear()
        self.peer_sync_state.clear()
        self.shared_state_by_peer.clear()
        self.active_topic_uuids.clear()
        return SessionResult("ok", effects=effects)

    def disconnect(self) -> SessionResult:
        peers = sorted(self.members - {self.address})
        effects = [
            SessionEffect("send_leave", peer, {"from_addr": self.address})
            for peer in peers
        ]
        self.members = {self.address}
        self.peer_topics.clear()
        self.peer_topic_sets.clear()
        self.peer_fetch_topic_sets.clear()
        self.peer_perspectives.clear()
        self.peer_status.clear()
        self.peer_sync_state.clear()
        self.shared_state_by_peer.clear()
        self.active_topic_uuids.clear()
        return SessionResult("ok", effects=effects)

    def leave_topic(self, topic_uuid: str) -> SessionResult:
        peers = sorted(
            peer for peer, topics in self.peer_topic_sets.items()
            if topic_uuid in topics
        )
        effects = [
            SessionEffect(
                "send_leave",
                peer,
                {
                    "from_addr": self.address,
                    "topic_uuid": topic_uuid,
                    "topic_uuids": [topic_uuid],
                },
            )
            for peer in peers
        ]
        self.active_topic_uuids.discard(topic_uuid)
        for peer in peers:
            self._remove_peer_topic(peer, topic_uuid)
        return SessionResult("ok", effects=effects)

    # Incoming session messages

    def handle_ping(self, message: dict) -> SessionResult:
        from_addr = message.get("from_addr")
        topic_uuid = message.get("topic_uuid")
        topic_state_hash = message.get("topic_state_hash")
        changed_uuid = message.get("changed_uuid")
        deleted_node_uuids = message.get("deleted_node_uuids") or []
        if not from_addr:
            return SessionResult("error", reason="missing from_addr")
        if not topic_uuid:
            return SessionResult("error", reason="missing topic_uuid")

        local_topic_state_hash = self.node_state_hash(topic_uuid)
        self.add_peer(from_addr, topic_uuid)
        self.mark_peer_reachable(from_addr)
        tombstones_changed = self._apply_remote_tombstones(
            topic_uuid,
            deleted_node_uuids,
        )
        cached = self.peer_perspectives.get(from_addr)
        cached_topic = self._find_in_tree(cached, topic_uuid) if cached else None
        cached_state_hash = cached_topic.state_hash if cached_topic else None
        pull_uuid = None
        if not (message.get("health_check") and topic_state_hash is None):
            if cached_topic is None:
                pull_uuid = topic_uuid
            elif cached_state_hash != topic_state_hash:
                pull_uuid = changed_uuid or topic_uuid

        effects = []
        if pull_uuid:
            effects.append(SessionEffect(
                "pull_subtree",
                from_addr,
                {"node_uuid": pull_uuid, "topic_uuid": topic_uuid},
            ))
        if tombstones_changed:
            effects.extend(self._sync_effects(topic_uuid))
        self.trace_event(
            "session.handle_ping",
            from_addr=from_addr,
            topic_uuid=topic_uuid,
            topic_state_hash=topic_state_hash,
            cached_state_hash=cached_state_hash,
            changed_uuid=changed_uuid,
            deleted_node_uuids=deleted_node_uuids,
            pull_uuid=pull_uuid,
            effects=[effect.type for effect in effects],
        )
        return SessionResult("ok", value={
            "topic_uuid": topic_uuid,
            "topic_state_hash": local_topic_state_hash,
        }, effects=effects)

    def handle_sync_status(self, message: dict) -> SessionResult:
        from_addr = message.get("from_addr")
        summary = message.get("summary") or {}
        topics = summary.get("topics") or {}
        deleted = summary.get("deleted") or {}
        incoming_sync_hash = summary.get("sync_hash")
        if not from_addr:
            return SessionResult("error", reason="missing from_addr")
        if not isinstance(topics, dict):
            return SessionResult("error", reason="invalid topics")

        for topic_uuid in sorted(topics):
            self.add_peer(from_addr, topic_uuid, fetch_from_peer=topic_uuid not in self._protocol.index)
        self.mark_peer_reachable(from_addr)
        state = self.peer_sync_state.setdefault(from_addr, self._new_peer_sync_state())
        state["last_received_sync_hash"] = incoming_sync_hash

        tombstones_changed = False
        if isinstance(deleted, dict):
            for topic_uuid, node_uuids in sorted(deleted.items()):
                tombstones_changed = (
                    self._apply_remote_tombstones(topic_uuid, node_uuids or [])
                    or tombstones_changed
                )

        effects = []
        for topic_uuid, topic_state_hash in sorted(topics.items()):
            cached_state_hash = self.cached_peer_topic_state_hash(from_addr, topic_uuid)
            if cached_state_hash != topic_state_hash:
                effects.append(SessionEffect(
                    "pull_subtree",
                    from_addr,
                    {"node_uuid": topic_uuid, "topic_uuid": topic_uuid},
                ))
        if tombstones_changed:
            effects.extend(self._sync_effects(None))

        self.trace_event(
            "session.handle_sync_status",
            from_addr=from_addr,
            incoming_sync_hash=incoming_sync_hash,
            changed_topics=[
                topic_uuid
                for topic_uuid, topic_state_hash in sorted(topics.items())
                if self.cached_peer_topic_state_hash(from_addr, topic_uuid) != topic_state_hash
            ],
            effects=[effect.type for effect in effects],
        )
        return SessionResult("ok", value={
            "summary": summary,
            "my_summary": self.sync_summary(from_addr),
        }, effects=effects)

    def handle_sync_response(self, peer_addr: str, response: dict) -> SessionResult:
        if response.get("status") not in ("ok", "partial"):
            return SessionResult("error", reason=response.get("reason") or "sync failed")
        self.mark_peer_reachable(peer_addr)
        my_summary = self.sync_summary(peer_addr)
        state = self.peer_sync_state.setdefault(peer_addr, self._new_peer_sync_state())
        if response.get("delivered_sync_hash") == my_summary["sync_hash"]:
            state["last_delivered_sync_hash"] = my_summary["sync_hash"]
            state["retry_after"] = None
            state["retry_delay"] = 1.0

        peer_summary = response.get("my_summary") or {}
        if peer_summary.get("sync_hash"):
            state["last_received_sync_hash"] = peer_summary.get("sync_hash")
        effects = self._pull_effects_for_peer_summary(peer_addr, peer_summary)
        self.trace_event(
            "session.handle_sync_response",
            peer_addr=peer_addr,
            delivered_sync_hash=response.get("delivered_sync_hash"),
            current_sync_hash=my_summary["sync_hash"],
            delivered=response.get("delivered_sync_hash") == my_summary["sync_hash"],
            peer_sync_hash=peer_summary.get("sync_hash"),
            effects=[effect.type for effect in effects],
        )
        return SessionResult("ok", effects=effects)

    def handle_join(self, message: dict) -> SessionResult:
        from_addr = message.get("from_addr")
        topic_uuids = self._message_topic_uuids(message)
        pull_topic_uuids = message.get("pull_topic_uuids") or topic_uuids
        topic_members = message.get("topic_members") or {}
        legacy_known_members = set(message.get("known_members") or [])
        if not from_addr:
            return SessionResult("error", reason="missing from_addr")
        if not topic_uuids:
            return SessionResult("error", reason="missing topic_uuid")

        pull_topic_set = set(pull_topic_uuids)
        for topic_uuid in topic_uuids:
            self.add_peer(
                from_addr,
                topic_uuid,
                fetch_from_peer=topic_uuid in pull_topic_set,
            )
        for topic_uuid in topic_uuids:
            members = set(topic_members.get(topic_uuid) or legacy_known_members)
            for member in sorted(members):
                if member == self.address:
                    continue
                self.add_peer(
                    member,
                    topic_uuid,
                    fetch_from_peer=member == from_addr and topic_uuid in pull_topic_set,
                )

        effects = []
        for topic_uuid in pull_topic_uuids:
            effects.append(SessionEffect(
                "pull_subtree",
                from_addr,
                {"node_uuid": topic_uuid, "topic_uuid": topic_uuid},
            ))
        response_topic_members = self.topic_members_by_topic(topic_uuids)
        return SessionResult(
            "ok",
            value={
                "members": sorted(self.members),
                "topic_uuids": topic_uuids,
                "topic_uuid": topic_uuids[0],
                "topic_members": response_topic_members,
            },
            effects=effects,
        )

    def handle_announce(self, message: dict) -> SessionResult:
        new_addr = message.get("new_addr")
        topic_uuids = self._message_topic_uuids(message)
        if not new_addr:
            return SessionResult("error", reason="missing new_addr")
        if not topic_uuids:
            return SessionResult("error", reason="missing topic_uuid")
        self.add_peer_topics(new_addr, topic_uuids)
        effects = [
            SessionEffect(
                "pull_subtree",
                new_addr,
                {"node_uuid": topic_uuid, "topic_uuid": topic_uuid},
            )
            for topic_uuid in topic_uuids
        ]
        return SessionResult(
            "ok",
            effects=effects,
        )

    def handle_leave(self, message: dict) -> SessionResult:
        from_addr = message.get("from_addr")
        topic_uuids = self._message_topic_uuids(message)
        if topic_uuids:
            for topic_uuid in topic_uuids:
                self._remove_peer_topic(from_addr, topic_uuid)
                tree = self.peer_perspectives.get(from_addr)
                if tree:
                    self._remove_uuid_from_tree(tree, topic_uuid)
                    self._refresh_tree_hashes(tree)
            return SessionResult("ok")
        self.members.discard(from_addr)
        self.peer_topics.pop(from_addr, None)
        self.peer_topic_sets.pop(from_addr, None)
        self.peer_fetch_topic_sets.pop(from_addr, None)
        self.peer_perspectives.pop(from_addr, None)
        self.peer_status.pop(from_addr, None)
        self.peer_sync_state.pop(from_addr, None)
        self.shared_state_by_peer.pop(from_addr, None)
        return SessionResult("ok")

    def mark_peer_reachable(self, peer_addr: str) -> bool:
        status = self.peer_status.setdefault(peer_addr, self._new_peer_status())
        changed = status.get("state") != "online" or status.get("failures", 0) != 0
        status.update({
            "state": "online",
            "failures": 0,
            "last_seen": time.time(),
            "last_error": None,
        })
        return changed

    def mark_peer_unreachable(self, peer_addr: str, reason: str | None = None) -> bool:
        status = self.peer_status.setdefault(peer_addr, self._new_peer_status())
        was_offline = status.get("state") == "offline"
        status.update({
            "state": "offline",
            "failures": int(status.get("failures", 0)) + 1,
            "last_error": reason or "unreachable",
        })
        return not was_offline

    # Peer cache

    def apply_peer_subtree(self, peer_addr: str,
                           subtree: PRSPNode,
                           parent_uuid: str | None) -> None:
        cached = self.peer_perspectives.get(peer_addr)
        if cached is None:
            self.peer_perspectives[peer_addr] = subtree
            self.trace_event(
                "session.apply_peer_subtree",
                peer_addr=peer_addr,
                node_uuid=subtree.uuid,
                parent_uuid=parent_uuid,
                state_hash=subtree.state_hash,
                action="new_cache",
            )
            self.prune_resolved_tombstones()
            self._sync_shared_state_for_tree(peer_addr, subtree)
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
                self.trace_event(
                    "session.apply_peer_subtree",
                    peer_addr=peer_addr,
                    node_uuid=subtree.uuid,
                    parent_uuid=parent_uuid,
                    state_hash=subtree.state_hash,
                    action="append_to_parent",
                )
                self.prune_resolved_tombstones()
                self._sync_shared_state_for_tree(peer_addr, subtree)
                return
            if cached.data.get("type") == "peer_cache_root":
                subtree.parent_uuid = cached.uuid
                cached.children = [
                    child for child in cached.children
                    if child.uuid != subtree.uuid
                ]
                cached.children.append(subtree)
                self._refresh_tree_hashes(cached)
                self.trace_event(
                    "session.apply_peer_subtree",
                    peer_addr=peer_addr,
                    node_uuid=subtree.uuid,
                    parent_uuid=parent_uuid,
                    state_hash=subtree.state_hash,
                    action="append_to_cache_root",
                )
                self.prune_resolved_tombstones()
                self._sync_shared_state_for_tree(peer_addr, subtree)
                return
            if cached.uuid != subtree.uuid:
                aggregate = self._peer_cache_root(peer_addr)
                existing = cached
                existing.parent_uuid = aggregate.uuid
                subtree.parent_uuid = aggregate.uuid
                aggregate.children = [existing, subtree]
                aggregate.refresh_hashes_deep()
                self.peer_perspectives[peer_addr] = aggregate
                self.trace_event(
                    "session.apply_peer_subtree",
                    peer_addr=peer_addr,
                    node_uuid=subtree.uuid,
                    parent_uuid=parent_uuid,
                    state_hash=subtree.state_hash,
                    action="create_cache_root",
                )
                self.prune_resolved_tombstones()
                self._sync_shared_state_for_tree(peer_addr, subtree)
                return
            self.peer_perspectives[peer_addr] = subtree
            self.trace_event(
                "session.apply_peer_subtree",
                peer_addr=peer_addr,
                node_uuid=subtree.uuid,
                parent_uuid=parent_uuid,
                state_hash=subtree.state_hash,
                action="replace_cache",
            )
            self.prune_resolved_tombstones()
            self._sync_shared_state_for_tree(peer_addr, subtree)
            return

        old_state_hash = target.state_hash
        self._remove_duplicate_subtree_uuids(cached, subtree_uuids, subtree.uuid)
        target.created_at = subtree.created_at
        target.updated_at = subtree.updated_at
        target.content_hash = subtree.content_hash
        target.state_hash = subtree.state_hash
        target.weights = subtree.weights
        target.proposals = subtree.proposals
        target.data = subtree.data
        target.children = subtree.children
        if parent_uuid and target.parent_uuid != parent_uuid:
            old_parent = self._find_in_tree(cached, target.parent_uuid)
            new_parent = self._find_in_tree(cached, parent_uuid)
            if old_parent and new_parent:
                old_parent.children = [
                    child for child in old_parent.children
                    if child.uuid != target.uuid
                ]
                target.parent_uuid = new_parent.uuid
                new_parent.children = [
                    child for child in new_parent.children
                    if child.uuid != target.uuid
                ]
                new_parent.children.append(target)
        self._refresh_tree_hashes(cached)
        self.trace_event(
            "session.apply_peer_subtree",
            peer_addr=peer_addr,
            node_uuid=subtree.uuid,
            parent_uuid=parent_uuid,
            old_state_hash=old_state_hash,
            state_hash=subtree.state_hash,
            action="update_existing",
        )
        self.prune_resolved_tombstones()
        self._sync_shared_state_for_tree(peer_addr, subtree)

    def get_cached_peer_subtree(self, peer_addr: str, node_uuid: str) -> PRSPNode | None:
        if node_uuid in self.deleted_node_uuids:
            return None
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
        local_node = self._protocol.index.get(compare_uuid)
        return self._analyze_transition_node(peer_addr, local_node, peer_node)

    # App-facing protocol wrappers

    def create_child(self, parent_uuid: str, data: dict,
                     weights: dict[str, float] | None = None) -> SessionResult:
        result = self._protocol.create_child(parent_uuid, data, weights)
        if not result.ok:
            return SessionResult("error", reason=result.reason)
        child = result.value
        self.trace_event(
            "protocol.create_child",
            parent_uuid=parent_uuid,
            node_uuid=child.uuid,
            node_type=child.data.get("type"),
            state_hash=child.state_hash,
        )
        return SessionResult("ok", value=self._snapshot_node(child),
                             effects=self._sync_effects(parent_uuid))

    def modify(self, node_uuid: str, data: dict,
               weights: dict[str, float] | None = None) -> SessionResult:
        before = self._protocol.index.get(node_uuid)
        old_state_hash = before.state_hash if before else None
        result = self._protocol.modify(node_uuid, data, weights)
        after = self._protocol.index.get(node_uuid)
        self.trace_event(
            "protocol.modify",
            node_uuid=node_uuid,
            old_state_hash=old_state_hash,
            state_hash=after.state_hash if after else None,
            ok=result.ok,
            reason=result.reason,
        )
        return self._operation_result(result, node_uuid)

    def delete(self, node_uuid: str) -> SessionResult:
        node = self._protocol.index.get(node_uuid)
        parent_uuid = node.parent_uuid if node else None
        old_state_hash = node.state_hash if node else None
        topic_uuid = self._topic_for_node(node_uuid)
        result = self._protocol.delete(node_uuid)
        if result.ok:
            self._prune_shared_state_node(node_uuid)
        if result.ok and topic_uuid:
            self.deleted_node_uuids[node_uuid] = topic_uuid
            self.prune_resolved_tombstones()
        self.trace_event(
            "protocol.delete",
            node_uuid=node_uuid,
            parent_uuid=parent_uuid,
            topic_uuid=topic_uuid,
            old_state_hash=old_state_hash,
            ok=result.ok,
            reason=result.reason,
        )
        return self._operation_result(result, parent_uuid or node_uuid)

    def copy(self, source_uuid: str, destination_uuid: str) -> SessionResult:
        result = self._protocol.copy(source_uuid, destination_uuid)
        if not result.ok:
            return SessionResult("error", reason=result.reason)
        clone = result.value
        return SessionResult("ok", value=self._snapshot_node(clone),
                             effects=self._sync_effects(destination_uuid))

    def move(self, source_uuid: str, destination_uuid: str) -> SessionResult:
        result = self._protocol.move(source_uuid, destination_uuid)
        return self._operation_result(result, source_uuid)

    def move_child(self, source_uuid: str, destination_uuid: str,
                   index: int | None = None) -> SessionResult:
        node = self._protocol.index.get(source_uuid)
        old_parent_uuid = node.parent_uuid if node else None
        old_state_hash = node.state_hash if node else None
        result = self._protocol.move_child(source_uuid, destination_uuid, index)
        moved = self._protocol.index.get(source_uuid)
        self.trace_event(
            "protocol.move_child",
            node_uuid=source_uuid,
            old_parent_uuid=old_parent_uuid,
            destination_uuid=destination_uuid,
            index=index,
            old_state_hash=old_state_hash,
            state_hash=moved.state_hash if moved else None,
            ok=result.ok,
            reason=result.reason,
        )
        return self._operation_result(result, source_uuid)

    def adopt_subtree(self, tree: PRSPNode, parent_uuid: str,
                      remove_descendant_duplicates: bool = False) -> SessionResult:
        if tree.uuid in self.deleted_node_uuids:
            self.trace_event(
                "protocol.adopt_subtree",
                node_uuid=tree.uuid,
                parent_uuid=parent_uuid,
                state_hash=tree.state_hash,
                ok=False,
                reason="node deleted",
            )
            return SessionResult("error", reason="node deleted")
        tree = self._without_tombstoned_nodes(tree)
        result = self._protocol.adopt_subtree(
            tree,
            parent_uuid,
            remove_descendant_duplicates=remove_descendant_duplicates,
        )
        if not result.ok:
            self.trace_event(
                "protocol.adopt_subtree",
                node_uuid=tree.uuid,
                parent_uuid=parent_uuid,
                state_hash=tree.state_hash,
                ok=False,
                reason=result.reason,
            )
            return SessionResult("error", reason=result.reason)
        adopted = result.value
        self.trace_event(
            "protocol.adopt_subtree",
            node_uuid=adopted.uuid,
            parent_uuid=parent_uuid,
            state_hash=adopted.state_hash,
            ok=True,
            remove_descendant_duplicates=remove_descendant_duplicates,
        )
        return SessionResult("ok", value=self._snapshot_node(adopted),
                             effects=self._sync_effects(adopted.uuid))

    def replace_subtree(self, tree: PRSPNode) -> SessionResult:
        if tree.uuid in self.deleted_node_uuids:
            return SessionResult("error", reason="node deleted")
        tree = self._without_tombstoned_nodes(tree)
        local = self._protocol.index.get(tree.uuid)
        old_state_hash = local.state_hash if local else None
        result = self._protocol.replace_subtree(tree)
        if not result.ok:
            self.trace_event(
                "protocol.replace_subtree",
                node_uuid=tree.uuid,
                old_state_hash=old_state_hash,
                incoming_state_hash=tree.state_hash,
                ok=False,
                reason=result.reason,
            )
            return SessionResult("error", reason=result.reason)
        replaced = result.value
        self.trace_event(
            "protocol.replace_subtree",
            node_uuid=replaced.uuid,
            old_state_hash=old_state_hash,
            incoming_state_hash=tree.state_hash,
            state_hash=replaced.state_hash,
            ok=True,
        )
        return SessionResult("ok", value=self._snapshot_node(replaced),
                             effects=self._sync_effects(replaced.uuid))

    def remove_subtree_uuids(self, root_uuid: str, uuids: set[str]) -> SessionResult:
        result = self._protocol.remove_subtree_uuids(root_uuid, uuids)
        for uuid in uuids:
            self._prune_shared_state_node(uuid)
        return self._operation_result(result, root_uuid)

    def prune_resolved_tombstones(self) -> None:
        resolved = []
        for node_uuid, topic_uuid in sorted(self.deleted_node_uuids.items()):
            peers = self.peers_for_topic(topic_uuid)
            if all(self._peer_topic_lacks_uuid(peer, topic_uuid, node_uuid)
                   for peer in peers):
                resolved.append(node_uuid)
        for node_uuid in resolved:
            topic_uuid = self.deleted_node_uuids.pop(node_uuid, None)
            self.trace_event(
                "session.tombstone_pruned",
                node_uuid=node_uuid,
                topic_uuid=topic_uuid,
            )

    def propose(self, top_uuid: str,
                operations: list[dict | AtomicOperation]) -> SessionResult:
        result = self._protocol.propose(
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
        result = self._protocol.take_back_proposal(proposal_uuid)
        return self._operation_result(result, self.active_topic_uuid)

    def respond_to_proposal(self, proposal_uuid: str, response: str,
                            source_addr: str | None = None) -> SessionResult:
        proposal = self._find_visible_proposal(proposal_uuid, source_addr)
        if not proposal:
            return SessionResult("error", reason="proposal not found")
        if response == "accept":
            result = self._protocol.accept_proposal(proposal)
        elif response == "object":
            result = self._protocol.object_to_proposal(proposal)
        else:
            return SessionResult("error", reason="unknown proposal response")
        return self._operation_result(result, self.active_topic_uuid)

    def integrate_proposal(self, proposal_uuid: str) -> SessionResult:
        result = self._protocol.integrate_proposal(proposal_uuid)
        return self._operation_result(result, self.active_topic_uuid)

    def reconcile_integrations(self) -> SessionResult:
        changed = False
        for tree in self.peer_perspectives.values():
            if not tree:
                continue
            for proposal in self._iter_tree_proposals(tree):
                if proposal.status not in ("integrated", "taken_back"):
                    continue
                result = self._protocol.reconcile_final_proposal(proposal)
                changed = changed or result.ok
        return SessionResult(
            "ok",
            value=changed,
            effects=self._sync_effects(self.active_topic_uuid) if changed else [],
        )

    def get_node(self, node_uuid: str) -> PRSPNode | None:
        node = self._protocol.index.get(node_uuid)
        return self._snapshot_node(node) if node else None

    def get_subtree(self, node_uuid: str) -> dict | None:
        node = self._protocol.index.get(node_uuid)
        if not node:
            return None
        return {
            "subtree": node.to_dict(),
            "parent_uuid": node.parent_uuid,
        }

    def get_network_info(self) -> dict:
        return {
            "address": self.address,
            "root_uuid": self._protocol.root.uuid,
            "root_content_hash": self._protocol.root.content_hash,
            "root_state_hash": self._protocol.root.state_hash,
            "members": sorted(self.members),
            "peer_addresses": sorted(self.peer_perspectives.keys()),
            "topic_uuid": self.active_topic_uuid,
            "topic_uuids": sorted(self.active_topic_uuids),
            "peers": {
                addr: {
                    "content_hash": tree.content_hash if tree else None,
                    "state_hash": tree.state_hash if tree else None,
                    "root_uuid": tree.uuid if tree else None,
                    "topic_uuid": self.peer_topics.get(addr),
                    "topic_uuids": sorted(self.peer_topic_sets.get(addr) or []),
                    "status": self.peer_status.get(addr, self._new_peer_status()),
                }
                for addr, tree in self.peer_perspectives.items()
            },
            "peer_status": {
                addr: self.peer_status.get(addr, self._new_peer_status())
                for addr in sorted(self.members - {self.address})
            },
            "peer_sync": {
                addr: self.peer_sync_state.get(addr, self._new_peer_sync_state())
                for addr in sorted(self.members - {self.address})
            },
        }

    # Internals

    def _operation_result(self, result, changed_uuid: str | None) -> SessionResult:
        if not result.ok:
            return SessionResult("error", reason=result.reason)
        return SessionResult("ok", value=True,
                             effects=self._sync_effects(changed_uuid))

    def trace_event(self, kind: str, **fields: Any) -> None:
        self.trace.event(kind, **fields)

    def sync_summary(self, peer_addr: str) -> dict[str, Any]:
        topics = {}
        deleted = {}
        for topic_uuid in sorted(self.peer_topic_sets.get(peer_addr) or []):
            topic = self._protocol.index.get(topic_uuid)
            if not topic:
                continue
            topics[topic_uuid] = topic.state_hash
            topic_deleted = self._deleted_nodes_for_topic(topic_uuid)
            if topic_deleted:
                deleted[topic_uuid] = topic_deleted
        sync_hash = stable_hash({"topics": topics, "deleted": deleted})
        return {
            "topics": topics,
            "deleted": deleted,
            "sync_hash": sync_hash,
        }

    def pending_sync_effects(self, now: float | None = None) -> list[SessionEffect]:
        now = time.time() if now is None else now
        effects = []
        for peer in sorted(self.members - {self.address}):
            state = self.peer_sync_state.setdefault(peer, self._new_peer_sync_state())
            retry_after = state.get("retry_after")
            if retry_after is not None and float(retry_after) > now:
                continue
            summary = self.sync_summary(peer)
            if not summary["topics"]:
                continue
            if summary["sync_hash"] == state.get("last_delivered_sync_hash"):
                continue
            effects.append(self._sync_status_effect(peer, summary))
        return effects

    def record_sync_failure(self, peer_addr: str, reason: str | None = None) -> bool:
        changed = self.mark_peer_unreachable(peer_addr, reason)
        state = self.peer_sync_state.setdefault(peer_addr, self._new_peer_sync_state())
        delay = min(max(float(state.get("retry_delay") or 1.0), 1.0) * 2, 60.0)
        state["retry_delay"] = delay
        state["retry_after"] = time.time() + delay
        return changed

    def _pull_effects_for_peer_summary(self, peer_addr: str, summary: dict) -> list[SessionEffect]:
        effects = []
        topics = summary.get("topics") or {}
        deleted = summary.get("deleted") or {}
        if isinstance(deleted, dict):
            for topic_uuid, node_uuids in sorted(deleted.items()):
                self._apply_remote_tombstones(topic_uuid, node_uuids or [])
        for topic_uuid, topic_state_hash in sorted(topics.items()):
            self.add_peer(peer_addr, topic_uuid, fetch_from_peer=topic_uuid not in self._protocol.index)
            if self.cached_peer_topic_state_hash(peer_addr, topic_uuid) != topic_state_hash:
                effects.append(SessionEffect(
                    "pull_subtree",
                    peer_addr,
                    {"node_uuid": topic_uuid, "topic_uuid": topic_uuid},
                ))
        return effects

    def _topic_for_node(self, node_uuid: str) -> str | None:
        topic_uuids = set(self.active_topic_uuids)
        for topics in self.peer_topic_sets.values():
            topic_uuids.update(topics)
        for topic_uuid in sorted(topic_uuids):
            if self._is_descendant_or_self(topic_uuid, node_uuid):
                return topic_uuid
        return None

    def _deleted_nodes_for_topic(self, topic_uuid: str) -> list[str]:
        return sorted(
            node_uuid
            for node_uuid, deleted_topic_uuid in self.deleted_node_uuids.items()
            if deleted_topic_uuid == topic_uuid
        )

    def _apply_remote_tombstones(self, topic_uuid: str,
                                 node_uuids: list[str]) -> bool:
        changed = False
        for node_uuid in sorted(set(str(uuid) for uuid in node_uuids if uuid)):
            if node_uuid == topic_uuid:
                continue
            if self.deleted_node_uuids.get(node_uuid) != topic_uuid:
                self.deleted_node_uuids[node_uuid] = topic_uuid
            node = self._protocol.index.get(node_uuid)
            if node and self._is_descendant_or_self(topic_uuid, node_uuid):
                deleted = self._protocol.delete(node_uuid)
                changed = changed or deleted.ok
                if deleted.ok:
                    self._prune_shared_state_node(node_uuid)
            self.trace_event(
                "session.remote_tombstone",
                topic_uuid=topic_uuid,
                node_uuid=node_uuid,
                changed=changed,
            )
        if node_uuids:
            self.prune_resolved_tombstones()
        return changed

    def _without_tombstoned_nodes(self, tree: PRSPNode) -> PRSPNode:
        if not self.deleted_node_uuids:
            return tree
        payload = tree.to_dict()
        if self._remove_uuids_from_payload(payload, set(self.deleted_node_uuids)):
            return PRSPNode.from_dict(payload, repair_hashes=True)
        return tree

    def _peer_topic_lacks_uuid(self, peer_addr: str,
                               topic_uuid: str,
                               node_uuid: str) -> bool:
        cache = self.peer_perspectives.get(peer_addr)
        if not cache:
            return False
        topic = self._find_in_tree(cache, topic_uuid)
        if not topic:
            return False
        return self._find_in_tree(topic, node_uuid) is None

    def _sync_effects(self, changed_uuid: str | None) -> list[SessionEffect]:
        effects = []
        changed_topics = set(self._topics_for_change(changed_uuid))
        if changed_uuid and not changed_topics:
            return []
        for peer in sorted(self.members - {self.address}):
            if changed_topics and not (changed_topics & self.peer_topic_sets.get(peer, set())):
                continue
            summary = self.sync_summary(peer)
            if not summary["topics"]:
                continue
            state = self.peer_sync_state.setdefault(peer, self._new_peer_sync_state())
            if summary["sync_hash"] == state.get("last_delivered_sync_hash"):
                continue
            effects.append(self._sync_status_effect(peer, summary))
        return effects

    def _sync_status_effect(self, peer_addr: str, summary: dict[str, Any]) -> SessionEffect:
        return SessionEffect(
            "send_sync_status",
            peer_addr,
            {
                "from_addr": self.address,
                "summary": summary,
            },
        )

    def _topics_for_change(self, changed_uuid: str | None) -> list[str]:
        if not self.active_topic_uuids:
            return []
        if not changed_uuid:
            return sorted(self.active_topic_uuids)
        return [
            topic_uuid
            for topic_uuid in sorted(self.active_topic_uuids)
            if self._is_descendant_or_self(topic_uuid, changed_uuid)
        ]

    def _is_descendant_or_self(self, root_uuid: str, node_uuid: str) -> bool:
        root = self._protocol.index.get(root_uuid)
        return bool(root and self._find_in_tree(root, node_uuid))

    def _remove_peer_topic(self, peer_addr: str | None, topic_uuid: str) -> None:
        if not peer_addr:
            return
        topics = self.peer_topic_sets.get(peer_addr)
        if topics is not None:
            topics.discard(topic_uuid)
            if not topics:
                self.peer_topic_sets.pop(peer_addr, None)
        fetch_topics = self.peer_fetch_topic_sets.get(peer_addr)
        if fetch_topics is not None:
            fetch_topics.discard(topic_uuid)
            if not fetch_topics:
                self.peer_fetch_topic_sets.pop(peer_addr, None)
        if self.peer_topics.get(peer_addr) == topic_uuid:
            remaining = sorted(self.peer_topic_sets.get(peer_addr) or [])
            if remaining:
                self.peer_topics[peer_addr] = remaining[0]
            else:
                self.peer_topics.pop(peer_addr, None)
        if not self.peer_topic_sets.get(peer_addr):
            self.members.discard(peer_addr)
            self.peer_perspectives.pop(peer_addr, None)
            self.peer_status.pop(peer_addr, None)
            self.peer_sync_state.pop(peer_addr, None)
            self.shared_state_by_peer.pop(peer_addr, None)

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

    @staticmethod
    def _message_topic_uuids(message: dict) -> list[str]:
        topic_uuids = message.get("topic_uuids")
        if topic_uuids:
            return sorted(str(uuid) for uuid in set(topic_uuids))
        topic_uuid = message.get("topic_uuid")
        return [topic_uuid] if topic_uuid else []

    @staticmethod
    def _snapshot_node(node: PRSPNode) -> PRSPNode:
        return PRSPNode.from_dict(node.to_dict())

    @staticmethod
    def _new_peer_status() -> dict[str, Any]:
        return {
            "state": "online",
            "failures": 0,
            "last_seen": None,
            "last_error": None,
        }

    @staticmethod
    def _new_peer_sync_state() -> dict[str, Any]:
        return {
            "last_delivered_sync_hash": None,
            "last_received_sync_hash": None,
            "retry_after": None,
            "retry_delay": 1.0,
        }

    @staticmethod
    def _peer_cache_root(peer_addr: str) -> PRSPNode:
        root = PRSPNode({"type": "peer_cache_root", "label": peer_addr})
        root.uuid = f"peer-cache:{peer_addr}"
        root.refresh_hashes()
        return root

    def _other_perspectives_uuid(self) -> str:
        for child in self._protocol.root.children:
            if (child.data.get("type") == "folder"
                    and child.data.get("name") == "other_perspectives"):
                return child.uuid
        created = self._protocol.create_child(
            self._protocol.root.uuid,
            {"type": "folder", "name": "other_perspectives"},
            {},
        )
        return created.value.uuid

    def _find_visible_proposal(self, proposal_uuid: str,
                               source_addr: str | None) -> Proposal | None:
        local = self._protocol.find_proposal(proposal_uuid)
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

    def _note_agreement(self, peer_addr: str, node_uuid: str, hash_value: str) -> None:
        per_node = self.shared_state_by_peer.setdefault(peer_addr, {})
        hashes = per_node.get(node_uuid) or []
        if hashes[:1] == [hash_value]:
            return
        per_node[node_uuid] = [hash_value] + [
            value for value in hashes if value != hash_value
        ][:1]

    def _sync_shared_state_for_tree(self, peer_addr: str, node: PRSPNode) -> None:
        local_node = self._protocol.index.get(node.uuid)
        if local_node and local_node.state_hash == node.state_hash:
            self._note_agreement(peer_addr, node.uuid, node.state_hash)
        for child in node.children:
            self._sync_shared_state_for_tree(peer_addr, child)

    def _prune_shared_state_node(self, node_uuid: str) -> None:
        for per_node in self.shared_state_by_peer.values():
            per_node.pop(node_uuid, None)

    def _analyze_transition_node(self, peer_addr: str,
                                 local_node: PRSPNode | None,
                                 peer_node: PRSPNode | None) -> list[dict]:
        node = local_node or peer_node
        if node and node.uuid in self.deleted_node_uuids:
            return []
        if not local_node and not peer_node:
            return []
        if not local_node:
            return [self._transition_event(
                "local_missing_node",
                peer_addr,
                None,
                peer_node,
            )]
        if not peer_node:
            return [self._transition_event(
                "peer_missing_node",
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

        event_type = self._transition_type(peer_addr, local_node.uuid, local_node, peer_node)
        if event_type == "in_agreement":
            self._note_agreement(peer_addr, local_node.uuid, local_node.state_hash)
        if event_type == "cannot_compare" and child_events:
            child_types = {event["type"] for event in child_events}
            if "divergence" in child_types:
                event_type = "divergence"
            elif child_types <= {
                    "in_agreement",
                    "peer_made_changes",
                    "local_made_changes",
                    "local_missing_node",
                    "peer_missing_node",
            } and child_types != {"in_agreement"}:
                event_type = "divergence"

        return [
            self._transition_event(
                event_type,
                peer_addr,
                local_node,
                peer_node,
            )
        ] + child_events

    def _transition_type(self, peer_addr: str, node_uuid: str,
                         local_node: PRSPNode, peer_node: PRSPNode) -> str:
        if local_node.state_hash == peer_node.state_hash:
            return "in_agreement"
        shared_hashes = self.shared_state_by_peer.get(peer_addr, {}).get(node_uuid) or []
        if not shared_hashes:
            return "cannot_compare"
        if peer_node.state_hash in shared_hashes:
            return "local_made_changes"
        if local_node.state_hash in shared_hashes:
            return "peer_made_changes"
        return "divergence"

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
            "peer_state_hash": peer_node.state_hash if peer_node else None,
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
    def _remove_uuids_from_payload(root: dict, uuids: set[str]) -> bool:
        changed = False
        kept = []
        for child in root.get("children", []):
            if child.get("uuid") in uuids:
                changed = True
                continue
            changed = Session._remove_uuids_from_payload(child, uuids) or changed
            kept.append(child)
        root["children"] = kept
        return changed

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

