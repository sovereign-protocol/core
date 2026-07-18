"""
Sovereign session component.

Offered API:
  Session(address)
  start_discussion(topic_uuid)
  add_peer(peer_addr, topic_uuid)
  handle_join(message)
  handle_announce(message)
  handle_leave(message)
  apply_peer_subtree(peer_addr, subtree, parent_uuid)
  watch_topic(peer_addr, topic_uuid)
  unwatch_topic(peer_addr, topic_uuid)
  leave()
  get_network_info()
  peer_discusses_node(peer_addr, node_uuid)
  accept_peer_node(peer_addr, node_uuid, adopt_absence=False)
  reconcile_peer_changes(peer_addr, topic_uuid, node_is_eligible=None,
    node_adopt_mode=None, allow_wholesale_replace=False)
    Generic peer-content reconciliation (the "adopt incoming changes"
    mechanism): walks a peer's transitions for one topic and adopts
    whatever isn't blocked by a local keep-mine/pushed-back decision. Apps
    supply only their own eligibility policy via the two callables - the
    walk itself, and the keep-mine/pushed-back guards, are app-agnostic.
  protocol operation wrappers: create_child, modify, delete, copy, move,
    set_perspective_state

Used API:
  protocol.ProtocolState and protocol.PRSPNode only.

Transport contract:
  Session never sends data. It returns SessionEffect values that a server or
  transport adapter can execute using HTTP, local memory, relay, or another
  mechanism.
"""

from __future__ import annotations

import time
import threading
import uuid as uuid_mod
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable

from protocol import PRSPNode, ProtocolState, collect_subtree_uuids, stable_hash
from trace_log import TraceLogger


_LOCAL_REVISION_ORIGIN = object()


def _session_locked(method):
    """Serialize access to Session's mutable protocol and peer state."""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return locked


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
    def __init__(self, protocol: ProtocolState, lock: threading.RLock):
        self._protocol = protocol
        self._lock = lock

    def __contains__(self, node_uuid: str) -> bool:
        with self._lock:
            return node_uuid in self._protocol.index

    def __iter__(self):
        with self._lock:
            return iter(tuple(self._protocol.index))

    def __len__(self) -> int:
        with self._lock:
            return len(self._protocol.index)

    def get(self, node_uuid: str, default=None) -> PRSPNode | None:
        with self._lock:
            node = self._protocol.index.get(node_uuid)
            return Session._snapshot_node(node) if node else default

    def __getitem__(self, node_uuid: str) -> PRSPNode:
        with self._lock:
            node = self._protocol.index[node_uuid]
            return Session._snapshot_node(node)

    def keys(self):
        with self._lock:
            return tuple(self._protocol.index)

    def values(self):
        with self._lock:
            return tuple(
                Session._snapshot_node(node)
                for node in self._protocol.index.values()
            )

    def items(self):
        with self._lock:
            return tuple(
                (node_uuid, Session._snapshot_node(node))
                for node_uuid, node in self._protocol.index.items()
            )


class ReadOnlyProtocolView:
    def __init__(self, protocol: ProtocolState, lock: threading.RLock):
        self._protocol = protocol
        self._lock = lock
        self.index = ReadOnlyProtocolIndex(protocol, lock)

    @property
    def root(self) -> PRSPNode:
        with self._lock:
            return Session._snapshot_node(self._protocol.root)

    @property
    def author(self) -> str:
        with self._lock:
            return self._protocol.author


class Session:
    def __init__(self, address: str, trace: TraceLogger | None = None):
        self.lock = threading.RLock()
        self.address = address
        self.trace = trace or TraceLogger.disabled()
        self._protocol = ProtocolState(author=address)
        self.protocol = ReadOnlyProtocolView(self._protocol, self.lock)
        self.members: set[str] = {address}
        self.peer_topics: dict[str, str] = {}
        self.peer_topic_sets: dict[str, set[str]] = {}
        self.peer_fetch_topic_sets: dict[str, set[str]] = {}
        self.peer_perspectives: dict[str, PRSPNode] = {}
        self.peer_status: dict[str, dict[str, Any]] = {}
        # Which channel type last successfully delivered to/from a peer
        # address - purely informational (the only transport-shaped thing
        # an app is allowed to surface to its UI, per the connect-channel
        # design), never read by any sync/reconciliation logic itself.
        self.peer_channel: dict[str, str] = {}
        # Canonical addr -> identity_key registry. Knowledge, not
        # registration: an entry records "this address belongs to this
        # identity", a fact that stays true even after the peer's
        # registration (members/peer_topic_sets/...) is torn down - so
        # remove_peer deliberately leaves it alone. Written the instant
        # any channel learns the fact (set_peer_identity_key), never
        # re-derived from cached content on demand.
        self.peer_identity_key: dict[str, str] = {}
        self.peer_sync_state: dict[str, dict[str, Any]] = {}
        # Exact local node revisions each peer has confirmed fetching.
        # Ephemeral for direct HTTP; relay acknowledgements are rebuilt from
        # durable relay heads after restart.
        self.peer_observed_node_revisions: dict[str, dict[str, str]] = {}
        self.active_topic_uuids: set[str] = set()
        self.app_metadata: dict[str, Any] = {}
        # Read-only observation: addresses/topics we poll for their cached
        # perspective only. Deliberately kept separate from
        # members/peer_topic_sets - an observed address is never a peer, so
        # it's invisible to health checks, network info, and sync effects.
        self.observed_topics: dict[str, set[str]] = {}

    @property
    def active_topic_uuid(self) -> str | None:
        return sorted(self.active_topic_uuids)[0] if self.active_topic_uuids else None

    # Identity - a session-owned meta-topic. Any app gets "who am I"/"who is
    # this peer" for free through these, instead of reimplementing its own
    # profile node and lookup logic.

    def _folder(self, parent: PRSPNode, name: str,
               node_type: str = "folder") -> PRSPNode:
        for child in parent.children:
            if child.data.get("name") == name and child.data.get("type") in ("folder", node_type):
                return child
        return self.create_child(parent.uuid, {"type": node_type, "name": name}, {}).value

    @property
    @_session_locked
    def identity(self) -> PRSPNode:
        # The shared_user_data folder only ever holds this session's own
        # identity node - no address match needed to find "mine" among
        # others, unlike the old address-keyed lookup this replaced.
        container = self._folder(self.protocol.root, "shared_user_data")
        for child in container.children:
            if child.data.get("type") == "shared_user_profile":
                return self._ensure_identity_defaults(child)
        return self.create_child(
            container.uuid,
            {
                "type": "shared_user_profile",
                "name": "public_profile",
                "version": 1,
                "identity_key": str(uuid_mod.uuid4()),
                "email": "",
                "display_name": "",
                "picture": "",
            },
            {},
        ).value

    def _ensure_identity_defaults(self, node: PRSPNode) -> PRSPNode:
        data = dict(node.data)
        changed = False
        if data.get("type") != "shared_user_profile":
            data["type"] = "shared_user_profile"
            changed = True
        if data.get("name") != "public_profile":
            data["name"] = "public_profile"
            changed = True
        if not data.get("version"):
            data["version"] = 1
            changed = True
        if not data.get("identity_key"):
            data["identity_key"] = str(uuid_mod.uuid4())
            changed = True
        if "email" not in data:
            data["email"] = ""
            changed = True
        if "display_name" not in data:
            data["display_name"] = ""
            changed = True
        if "picture" not in data:
            data["picture"] = ""
            changed = True
        if changed:
            self.modify(node.uuid, data, node.weights)
            return self.get_node(node.uuid) or node
        return node

    @_session_locked
    def set_identity(self, display_name: str, picture: str = "",
                     email: str | None = None) -> SessionResult:
        profile = self.identity
        data = dict(profile.data)
        data.update({
            "type": "shared_user_profile",
            "name": "public_profile",
            "display_name": display_name or "",
            "picture": picture or "",
        })
        if email is not None:
            data["email"] = email
        return self.modify(profile.uuid, data, profile.weights)

    def find_peer_identity(self, identity_key: str) -> PRSPNode | None:
        # Searches across every cached peer perspective's values, not one
        # peer's cache keyed by address - this is what lets identity survive
        # a peer's address changing, since lookup never depends on which
        # dict key the matching tree happens to be cached under.
        for tree in self.peer_perspectives.values():
            found = self._find_identity_in_tree(tree, identity_key)
            if found:
                return found
        return None

    def peer_identity(self, peer_addr: str) -> PRSPNode | None:
        # Address-scoped lookup: the bootstrap step for going from "a peer
        # address we're tracking" to "their identity" - find_peer_identity
        # alone can't do this once it's keyed by identity_key instead of
        # address, since a bare address gives no identity_key to search for.
        tree = self.peer_perspectives.get(peer_addr)
        return self._find_identity_in_tree(tree) if tree else None

    def set_peer_identity_key(self, peer_addr: str, identity_key: str) -> None:
        # The single writer for the addr -> identity_key registry - every
        # code path that learns an address's identity (connect-token
        # snapshot, direct profile pull, relay discovery) funnels through
        # here, so the fact is recorded once, immediately, decoupled from
        # whether/when full content has been cached.
        if not peer_addr or not identity_key:
            return
        if self.peer_identity_key.get(peer_addr) == identity_key:
            return
        self.peer_identity_key[peer_addr] = identity_key
        self.trace_event(
            "session.set_peer_identity_key",
            peer_addr=peer_addr,
            identity_key=identity_key,
        )

    def addresses_for_identity(self, identity_key: str) -> list[str]:
        return sorted(
            addr for addr, key in self.peer_identity_key.items()
            if key == identity_key
        )

    def _local_revision_origin(self, data: dict | None = None) -> str | None:
        # Do not use the identity property here: identity creation itself
        # goes through create_child and would recurse. Search only what
        # already exists, with the new profile's key as bootstrap fallback.
        profile = self._find_identity_in_tree(self._protocol.root)
        if profile and profile.data.get("identity_key"):
            return profile.data["identity_key"]
        if (data or {}).get("type") == "shared_user_profile":
            return (data or {}).get("identity_key")
        return None

    def apply_peer_identity_snapshot(self, peer_addr: str, identity: dict) -> None:
        # A connect token carries the sender's identity inline so it's
        # visible immediately, without waiting on whichever channel(s) end
        # up actually usable to fetch it - deliberately unconditional
        # (never goes through reconcile_peer_changes/keep_mine/pushed_back):
        # an identity assertion isn't collaborative content with divergence
        # to resolve, it's just "the latest thing this peer said about
        # themselves." Degrades gracefully (no-op) on anything it doesn't
        # recognize, rather than raising - a peer running a newer identity
        # version should never be able to break an older one's connect flow.
        if not isinstance(identity, dict) or identity.get("data", {}).get("version") != 1:
            return
        try:
            node = PRSPNode.from_dict(identity)
        except (ValueError, KeyError):
            return
        self.apply_peer_subtree(peer_addr, node, None)

    @staticmethod
    def _find_identity_in_tree(node: PRSPNode | None,
                               identity_key: str | None = None) -> PRSPNode | None:
        if not node:
            return None
        if (node.data.get("type") == "shared_user_profile"
                and (identity_key is None or node.data.get("identity_key") == identity_key)):
            return node
        for child in node.children:
            found = Session._find_identity_in_tree(child, identity_key)
            if found:
                return found
        return None

    @staticmethod
    def is_identity_node(node: PRSPNode | None) -> bool:
        if not node:
            return False
        return node.data.get("type") == "shared_user_profile"

    # Read-only protocol access / persistence

    @_session_locked
    def load_protocol_root(self, root: PRSPNode) -> None:
        self._protocol.root = root
        self._protocol.index = {}
        self._protocol.index_subtree(root)
        self.protocol = ReadOnlyProtocolView(self._protocol, self.lock)

    @_session_locked
    def export_protocol_root(self) -> dict:
        return self._protocol.root.to_dict()

    def root_uuid(self) -> str:
        return self._protocol.root.uuid

    def has_node(self, node_uuid: str | None) -> bool:
        return bool(node_uuid and node_uuid in self._protocol.index)

    @_session_locked
    def node_state_hash(self, node_uuid: str) -> str | None:
        node = self._protocol.index.get(node_uuid)
        return node.state_hash if node else None

    def cached_peer_topic_state_hash(self, peer_addr: str, topic_uuid: str) -> str | None:
        cached = self.peer_perspectives.get(peer_addr)
        topic = self._find_in_tree(cached, topic_uuid) if cached else None
        return topic.state_hash if topic else None

    # Discussion/session state

    @_session_locked
    def start_discussion(self, topic_uuid: str) -> SessionResult:
        if topic_uuid not in self._protocol.index:
            return SessionResult("error", reason="topic not found")
        self.active_topic_uuids.add(topic_uuid)
        return SessionResult("ok", value=topic_uuid)

    @_session_locked
    def note_relay_peer_topic(self, peer_addr: str, topic_uuid: str) -> None:
        # Relay peers (peer_addr like "relay:<identity>") never go through
        # add_peer - that would also add them to self.members, which
        # pending_sync_effects/sync_effects iterate to decide who to push
        # real HTTP effects to, and a relay identity isn't HTTP-reachable at
        # that address. But kanban_logic's auto-adopt gates candidates on
        # peer_topic_sets (_peer_discusses_node) to know a cached peer
        # perspective actually applies to a given topic - without this, a
        # relay-sourced cache update would be silently invisible to
        # auto-adopt even though the data arrived correctly.
        self.peer_topic_sets.setdefault(peer_addr, set()).add(topic_uuid)

    def note_peer_channel(self, peer_addr: str, channel_type: str) -> None:
        self.peer_channel[peer_addr] = channel_type

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
        # "member" here specifically means "a real, HTTP-reachable address
        # worth telling other peers about" - peer_topic_sets alone isn't
        # enough evidence of that. A relay pseudo-address (e.g. "relay:B")
        # also lives in peer_topic_sets (note_relay_peer_topic - deliberate,
        # so kanban's auto-adopt/eligibility checks still recognize it) but
        # was never registered via add_peer specifically to keep it out of
        # self.members and everything self.members feeds (pending_sync_effects,
        # mesh-propagation via handle_join/join_discussion's own member
        # loops). Without this filter, mentioning a relay identity in one
        # topic_members exchange would propagate it into every peer's own
        # self.members via handle_join's blind add_peer loop, not just the
        # two sides actually using that relay channel.
        members = {self.address} if topic_uuid in self.active_topic_uuids else set()
        for peer, topics in self.peer_topic_sets.items():
            if topic_uuid in topics and peer in self.members:
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

    @staticmethod
    def topic_members_from_map(topic_members: dict,
                               topic_uuids: list[str]) -> dict[str, set[str]]:
        return {
            topic_uuid: set(topic_members.get(topic_uuid, []))
            for topic_uuid in topic_uuids
        }

    @_session_locked
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
        # Also tells each peer about the others, so the mesh survives our
        # departure - otherwise identical to disconnect().
        return self._leave_all(announce=True)

    def disconnect(self) -> SessionResult:
        return self._leave_all(announce=False)

    def _leave_all(self, announce: bool) -> SessionResult:
        peers = sorted(self.members - {self.address})
        effects = []
        if announce:
            for peer in peers:
                topic_uuids = set(self.peer_topic_sets.get(peer) or [])
                others = [other for other in peers if other != peer]
                if not topic_uuids or not others:
                    continue
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
        self._clear_all_peer_state()
        return SessionResult("ok", effects=effects)

    def _clear_all_peer_state(self) -> None:
        # Bulk counterpart of remove_peer - clears every per-peer structure
        # for *every* peer (including relay pseudo-addresses, which live in
        # peer_topic_sets/peer_perspectives without ever being members).
        # peer_identity_key is deliberately kept, for the same reason
        # remove_peer keeps it: it's knowledge, not registration.
        self.members = {self.address}
        self.peer_topics.clear()
        self.peer_topic_sets.clear()
        self.peer_fetch_topic_sets.clear()
        self.peer_perspectives.clear()
        self.peer_status.clear()
        self.peer_sync_state.clear()
        self.peer_channel.clear()
        self.observed_topics.clear()
        self.active_topic_uuids.clear()

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

    def handle_sync_status(self, message: dict) -> SessionResult:
        from_addr = message.get("from_addr")
        summary = message.get("summary") or {}
        topics = summary.get("topics") or {}
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

        effects = []
        for topic_uuid, topic_state_hash in sorted(topics.items()):
            cached_state_hash = self.cached_peer_topic_state_hash(from_addr, topic_uuid)
            if cached_state_hash != topic_state_hash:
                effects.append(SessionEffect(
                    "pull_subtree",
                    from_addr,
                    {"node_uuid": topic_uuid, "topic_uuid": topic_uuid},
                ))

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
        delivered = (
            response.get("status") == "ok"
            and response.get("delivered_sync_hash") == my_summary["sync_hash"]
        )
        if delivered:
            state["last_delivered_sync_hash"] = my_summary["sync_hash"]
            state["retry_after"] = None
            state["retry_delay"] = 1.0
            for topic_uuid, state_hash in my_summary["topics"].items():
                topic = self._protocol.index.get(topic_uuid)
                if topic and topic.state_hash == state_hash:
                    self.record_peer_observations(
                        peer_addr, self.node_revision_map(topic),
                    )

        peer_summary = response.get("my_summary") or {}
        if peer_summary.get("sync_hash"):
            state["last_received_sync_hash"] = peer_summary.get("sync_hash")
        effects = self._pull_effects_for_peer_summary(peer_addr, peer_summary)
        self.trace_event(
            "session.handle_sync_response",
            peer_addr=peer_addr,
            delivered_sync_hash=response.get("delivered_sync_hash"),
            current_sync_hash=my_summary["sync_hash"],
            delivered=delivered,
            peer_sync_hash=peer_summary.get("sync_hash"),
            effects=[effect.type for effect in effects],
        )
        return SessionResult("ok", effects=effects)

    @staticmethod
    def node_revision(node: PRSPNode) -> str:
        # state_hash excludes structural position; parent_uuid makes moves
        # independently acknowledgeable too. Origin distinguishes identical
        # content independently authored by two clients.
        return (
            f"{node.state_hash}@{node.parent_uuid or ''}"
            f"@{node.revision_origin_identity or ''}"
        )

    @classmethod
    def node_revision_map(cls, root: PRSPNode) -> dict[str, str]:
        revisions = {root.uuid: cls.node_revision(root)}
        for child in root.children:
            revisions.update(cls.node_revision_map(child))
        return revisions

    @_session_locked
    def record_peer_observations(self, peer_addr: str,
                                 revisions: dict[str, str]) -> bool:
        if not peer_addr or not isinstance(revisions, dict):
            return False
        known = self.peer_observed_node_revisions.setdefault(peer_addr, {})
        changed = False
        for node_uuid, revision in revisions.items():
            if not node_uuid or not isinstance(revision, str):
                continue
            if known.get(node_uuid) != revision:
                known[node_uuid] = revision
                changed = True
        return changed

    def peer_observed_node(self, peer_addr: str, node: PRSPNode) -> bool:
        return (
            self.peer_observed_node_revisions.get(peer_addr, {}).get(node.uuid)
            == self.node_revision(node)
        )

    def handle_join(self, message: dict) -> SessionResult:
        from_addr = message.get("from_addr")
        topic_uuids = self._message_topic_uuids(message)
        pull_topic_uuids = message.get("pull_topic_uuids") or topic_uuids
        topic_members = self.topic_members_from_map(
            message.get("topic_members") or {}, topic_uuids,
        )
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
            # Bug fix: accepting a join means we're now actively discussing
            # this topic too, not just tracking the joiner as a peer -
            # without this, the side that only ever *generated* a share
            # token (never called start_discussion themselves, since
            # generating a token is a pure client-side operation) never
            # gets its own board marked active. active_topic_uuids gates
            # auto-adopt (_is_active_discussion_node), so that side's
            # auto-adopt silently never ran for this board at all - not a
            # classification bug, the check was never reached in the first
            # place. No-ops harmlessly if we don't have this topic locally
            # yet (start_discussion just returns an error result, ignored).
            self.start_discussion(topic_uuid)
        for topic_uuid in topic_uuids:
            for member in sorted(topic_members.get(topic_uuid, set())):
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
                if not tree:
                    continue
                if tree.uuid == topic_uuid:
                    # The cache root is the topic itself - dropping children
                    # can't remove it, so drop the whole perspective.
                    self.peer_perspectives.pop(from_addr, None)
                    continue
                self._remove_uuid_from_tree(tree, topic_uuid)
                self._refresh_tree_hashes(tree)
            return SessionResult("ok")
        self.remove_peer(from_addr)
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

    @_session_locked
    def apply_peer_subtree(self, peer_addr: str,
                           subtree: PRSPNode,
                           parent_uuid: str | None) -> None:
        # Identity topics are always applied with the profile node as the
        # subtree root (connect-token snapshot, direct profile pull, relay
        # poll alike), so a root-only check is enough to make this the one
        # choke point where every channel's identity discovery lands in
        # the addr -> identity_key registry.
        if self.is_identity_node(subtree) and subtree.data.get("identity_key"):
            self.set_peer_identity_key(peer_addr, subtree.data["identity_key"])
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
            self.prune_deleted_nodes()
            return

        subtree_uuids = collect_subtree_uuids(subtree)
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
                self.prune_deleted_nodes()
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
                self.prune_deleted_nodes()
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
                self.prune_deleted_nodes()
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
            self.prune_deleted_nodes()
            return

        old_state_hash = target.state_hash
        self._remove_duplicate_subtree_uuids(cached, subtree_uuids, subtree.uuid)
        target.created_at = subtree.created_at
        target.updated_at = subtree.updated_at
        target.content_hash = subtree.content_hash
        target.state_hash = subtree.state_hash
        target.previous_hash = subtree.previous_hash
        target.previous_parent_uuid = subtree.previous_parent_uuid
        target.deleted = subtree.deleted
        target.perspective_state = subtree.perspective_state
        target.weights = subtree.weights
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
        self.prune_deleted_nodes()

    def get_cached_peer_subtree(self, peer_addr: str, node_uuid: str) -> PRSPNode | None:
        tree = self.peer_perspectives.get(peer_addr)
        if not tree:
            return None
        node = self._find_in_tree(tree, node_uuid)
        return PRSPNode.from_dict(node.to_dict()) if node else None

    def peer_discusses_node(self, peer_addr: str, node_uuid: str) -> bool:
        return any(
            self._is_descendant_or_self(topic_uuid, node_uuid)
            for topic_uuid in self.peer_topic_sets.get(peer_addr, set())
        )

    def watch_topic(self, peer_addr: str, topic_uuid: str) -> None:
        self.observed_topics.setdefault(peer_addr, set()).add(topic_uuid)

    def unwatch_topic(self, peer_addr: str, topic_uuid: str) -> bool:
        topics = self.observed_topics.get(peer_addr)
        if not topics or topic_uuid not in topics:
            return False
        topics.discard(topic_uuid)
        if not topics:
            self.observed_topics.pop(peer_addr, None)
            # Only drop the cached perspective if nothing else still needs
            # it (a real peer relationship for a different topic keeps it).
            if peer_addr not in self.peer_topic_sets:
                self.peer_perspectives.pop(peer_addr, None)
        return True

    def observed_topic_pairs(self) -> list[tuple[str, str]]:
        return sorted(
            (peer_addr, topic_uuid)
            for peer_addr, topic_uuids in self.observed_topics.items()
            for topic_uuid in topic_uuids
        )

    def analyze_peer_transitions(self, peer_addr: str,
                                 node_uuid: str | None = None) -> list[dict]:
        peer_root = self.peer_perspectives.get(peer_addr)
        if not peer_root:
            return []
        compare_uuid = node_uuid or peer_root.uuid
        peer_node = self._find_in_tree(peer_root, compare_uuid)
        local_node = self._protocol.index.get(compare_uuid)
        if not local_node and not peer_node:
            return []
        # Nodes are matched by uuid across the whole subtree, not by
        # structural position, so a node that moved to a different parent
        # on one side is still compared against its own counterpart (and
        # its move is attributable via previous_parent_uuid) instead of
        # showing up as an unrelated local_missing/peer_missing pair.
        local_by_uuid = self._flatten_by_uuid(local_node) if local_node else {}
        peer_by_uuid = self._flatten_by_uuid(peer_node) if peer_node else {}
        other_uuids = sorted((set(local_by_uuid) | set(peer_by_uuid)) - {compare_uuid})
        events = []
        for uuid in [compare_uuid, *other_uuids]:
            local = local_by_uuid.get(uuid)
            event = self._analyze_transition_node(
                peer_addr,
                local,
                peer_by_uuid.get(uuid),
                is_topic_root=(uuid == compare_uuid),
            )
            event["peer_observed_local_revision"] = bool(
                local and self.peer_observed_node(peer_addr, local)
            )
            events.append(event)
        return events

    @staticmethod
    def _flatten_by_uuid(node: PRSPNode) -> dict[str, PRSPNode]:
        out = {node.uuid: node}
        for child in node.children:
            out.update(Session._flatten_by_uuid(child))
        return out

    # App-facing protocol wrappers

    @_session_locked
    def create_child(self, parent_uuid: str, data: dict,
                     weights: dict[str, float] | None = None) -> SessionResult:
        revision_origin = self._local_revision_origin(data)
        result = self._protocol.create_child(
            parent_uuid, data, weights, revision_origin,
        )
        if not result.ok:
            return SessionResult("error", reason=result.reason)
        child = result.value
        self.trace_event(
            "protocol.create_child",
            parent_uuid=parent_uuid,
            node_uuid=child.uuid,
            node_type=child.data.get("type"),
            state_hash=child.state_hash,
            revision_origin_identity=child.revision_origin_identity,
        )
        return SessionResult("ok", value=self._snapshot_node(child),
                             effects=self.sync_effects(parent_uuid))

    @_session_locked
    def modify(self, node_uuid: str, data: dict,
               weights: dict[str, float] | None = None,
               revision_origin_identity: str | None | object = _LOCAL_REVISION_ORIGIN) -> SessionResult:
        before = self._protocol.index.get(node_uuid)
        old_state_hash = before.state_hash if before else None
        origin = (
            self._local_revision_origin(data)
            if revision_origin_identity is _LOCAL_REVISION_ORIGIN
            else revision_origin_identity
        )
        result = self._protocol.modify(
            node_uuid, data, weights, origin,
        )
        after = self._protocol.index.get(node_uuid)
        self.trace_event(
            "protocol.modify",
            node_uuid=node_uuid,
            old_state_hash=old_state_hash,
            state_hash=after.state_hash if after else None,
            revision_origin_identity=(
                after.revision_origin_identity if after else None
            ),
            ok=result.ok,
            reason=result.reason,
        )
        return self._operation_result(result, node_uuid)

    @_session_locked
    def delete(self, node_uuid: str) -> SessionResult:
        node = self._protocol.index.get(node_uuid)
        parent_uuid = node.parent_uuid if node else None
        old_state_hash = node.state_hash if node else None
        result = self._protocol.delete(
            node_uuid, self._local_revision_origin(),
        )
        if result.ok:
            self.prune_deleted_nodes()
        self.trace_event(
            "protocol.delete",
            node_uuid=node_uuid,
            parent_uuid=parent_uuid,
            old_state_hash=old_state_hash,
            ok=result.ok,
            reason=result.reason,
        )
        return self._operation_result(result, parent_uuid or node_uuid)

    @_session_locked
    def set_perspective_state(self, node_uuid: str, state: str) -> SessionResult:
        node = self._protocol.index.get(node_uuid)
        old_state_hash = node.state_hash if node else None
        result = self._protocol.set_perspective_state(node_uuid, state)
        self.trace_event(
            "protocol.set_perspective_state",
            node_uuid=node_uuid,
            state=state,
            old_state_hash=old_state_hash,
            ok=result.ok,
            reason=result.reason,
        )
        return self._operation_result(result, node_uuid)

    @staticmethod
    def keep_mine_active(local_node: PRSPNode, peer_node: PRSPNode) -> bool:
        # `pushed_back` is a standing decision - exempt from staleness so it
        # survives further peer churn on purpose (see BACKLOG.md item 10).
        if local_node.perspective_state == "pushed_back":
            return True
        if local_node.perspective_state != "kept_mine":
            return False
        # `kept_mine` is only ever a decision about *content* - if a move
        # has appeared since (in either direction), the merged transition
        # type can stay a clean "peer_made_changes" even though keep_mine
        # never considered the move at all, so it must not mask it. See the
        # worked example in BACKLOG.md item 10 for why this can't be folded
        # into a single "does the hash chain still connect" check.
        if Session._classify_move(local_node, peer_node) != "in_agreement":
            return False
        if Session._classify_content(local_node, peer_node) == "divergence":
            return False
        return True

    @staticmethod
    def peer_pushed_back(peer_node: PRSPNode | None) -> bool:
        return bool(peer_node and peer_node.perspective_state == "pushed_back")

    @staticmethod
    def _subtree_has_kept_mine(node: PRSPNode) -> bool:
        # A wholesale subtree replace may only run when nothing under it has
        # a local perspective decision - a wholesale replace would silently
        # overwrite a node the user explicitly decided to keep.
        if node.perspective_state != "none":
            return True
        return any(Session._subtree_has_kept_mine(child) for child in node.children)

    @staticmethod
    def _subtree_has_pushed_back(node: PRSPNode) -> bool:
        # Same guard, but over the *incoming peer* subtree: a wholesale
        # replace bypasses the per-node peer_pushed_back check entirely, so
        # a peer's pushed_back node anywhere in what would be replaced must
        # block it the same way a local keep-mine decision already does.
        if node.perspective_state == "pushed_back":
            return True
        return any(Session._subtree_has_pushed_back(child) for child in node.children)

    @_session_locked
    def copy(self, source_uuid: str, destination_uuid: str) -> SessionResult:
        result = self._protocol.copy(
            source_uuid, destination_uuid, self._local_revision_origin(),
        )
        if not result.ok:
            return SessionResult("error", reason=result.reason)
        clone = result.value
        return SessionResult("ok", value=self._snapshot_node(clone),
                             effects=self.sync_effects(destination_uuid))

    @_session_locked
    def move(self, source_uuid: str, destination_uuid: str) -> SessionResult:
        result = self._protocol.move(
            source_uuid, destination_uuid, self._local_revision_origin(),
        )
        return self._operation_result(result, source_uuid)

    @_session_locked
    def move_child(self, source_uuid: str, destination_uuid: str,
                   index: int | None = None) -> SessionResult:
        node = self._protocol.index.get(source_uuid)
        old_parent_uuid = node.parent_uuid if node else None
        old_state_hash = node.state_hash if node else None
        result = self._protocol.move_child(
            source_uuid, destination_uuid, index,
            self._local_revision_origin(),
        )
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

    @_session_locked
    def adopt_subtree(self, tree: PRSPNode, parent_uuid: str,
                      remove_descendant_duplicates: bool = False) -> SessionResult:
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
                             effects=self.sync_effects(adopted.uuid))

    @_session_locked
    def replace_subtree(self, tree: PRSPNode) -> SessionResult:
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
                             effects=self.sync_effects(replaced.uuid))

    def accept_peer_node(self, peer_addr: str, node_uuid: str,
                         adopt_absence: bool = False) -> SessionResult:
        if adopt_absence:
            return self.delete(node_uuid)
        peer = self.get_cached_peer_subtree(peer_addr, node_uuid)
        if not peer:
            return SessionResult("error", reason="peer node not found")
        local = self._protocol.index.get(node_uuid)
        parent_uuid = peer.parent_uuid if peer.parent_uuid in self._protocol.index else None
        if not parent_uuid and local:
            parent_uuid = local.parent_uuid
        if not parent_uuid or parent_uuid not in self._protocol.index:
            return SessionResult("error", reason="local parent not found")
        return self.adopt_subtree(peer, parent_uuid, remove_descendant_duplicates=True)

    @_session_locked
    def reconcile_peer_changes(
        self,
        peer_addr: str,
        topic_uuid: str,
        node_is_eligible: Callable[[PRSPNode, str], bool] | None = None,
        node_adopt_mode: Callable[[PRSPNode], str] | None = None,
        allow_wholesale_replace: bool = False,
    ) -> bool:
        # Generic "adopt incoming changes" walk - every app on this protocol
        # wants the same thing (adopt whatever a peer changed for one topic,
        # respecting local keep-mine/pushed-back decisions); the only thing
        # that's ever genuinely app-specific is which individual nodes are
        # eligible to auto-adopt (node_is_eligible) and whether a given node
        # should be merged field-only or grafted as a whole subtree
        # (node_adopt_mode - "shallow" vs "full", default "full").
        node_is_eligible = node_is_eligible or (lambda node, event_type: True)
        node_adopt_mode = node_adopt_mode or (lambda node: "full")

        peer_topic = self.get_cached_peer_subtree(peer_addr, topic_uuid)
        if not peer_topic:
            self.trace_event("session.reconcile_skip", reason="no_cached_subtree",
                              peer_addr=peer_addr, topic_uuid=topic_uuid)
            return False
        local_topic = self._protocol.index.get(topic_uuid)
        if not local_topic:
            self.trace_event("session.reconcile_skip", reason="no_local_topic",
                              peer_addr=peer_addr, topic_uuid=topic_uuid)
            return False
        if peer_topic.state_hash == local_topic.state_hash:
            self.trace_event("session.reconcile_skip", reason="hashes_equal",
                              peer_addr=peer_addr, topic_uuid=topic_uuid)
            return False

        peer_events = self.analyze_peer_transitions(peer_addr, topic_uuid)
        self.trace_event(
            "session.reconcile_peer_events",
            peer_addr=peer_addr,
            topic_uuid=topic_uuid,
            events=[{"type": e["type"], "node_uuid": e.get("node_uuid")} for e in peer_events],
        )
        if not any(event["type"] != "in_agreement" for event in peer_events):
            self.trace_event("session.reconcile_skip", reason="all_in_agreement",
                              peer_addr=peer_addr, topic_uuid=topic_uuid)
            return False

        top_event = peer_events[0] if peer_events else None
        self.trace_event(
            "session.reconcile_start",
            peer_addr=peer_addr,
            topic_uuid=topic_uuid,
            local_state_hash=local_topic.state_hash,
        )

        # The topic root's own event only ever decides this wholesale-replace
        # shortcut - it must NOT gate whether the per-node loop below runs at
        # all. A root of "in_agreement" or "local_made_changes" just means
        # the topic's own top-level fields didn't change (or we're ahead
        # there) - a descendant node can still independently have a real
        # peer_made_changes/local_missing_node event that the per-node
        # loop's own guards already know how to evaluate safely.
        if (top_event and top_event["type"] == "peer_made_changes" and allow_wholesale_replace
                and not self._subtree_has_kept_mine(local_topic)
                and not self._subtree_has_pushed_back(peer_topic)):
            self.trace_event(
                "session.reconcile_replace_subtree",
                peer_addr=peer_addr,
                topic_uuid=topic_uuid,
                local_state_hash=top_event.get("local_state_hash"),
                peer_state_hash=top_event.get("peer_state_hash"),
            )
            self.replace_subtree(peer_topic)
            self.trace_event("session.reconcile_done", peer_addr=peer_addr,
                              topic_uuid=topic_uuid, changed=True)
            return True

        changed = False
        for event in peer_events:
            if event["type"] not in ("peer_made_changes", "local_missing_node"):
                continue
            peer_node = self.get_cached_peer_subtree(peer_addr, event["node_uuid"])
            local_node = self._protocol.index.get(event["node_uuid"])
            reference_node = local_node or peer_node
            if not reference_node:
                continue
            if local_node and self.keep_mine_active(local_node, peer_node):
                continue
            if peer_node and self.peer_pushed_back(peer_node):
                continue
            if not node_is_eligible(reference_node, event["type"]):
                continue
            if (event["type"] == "peer_made_changes"
                    and ((local_node and self._subtree_has_kept_mine(local_node))
                         or (peer_node and self._subtree_has_pushed_back(peer_node)))):
                # A full subtree adopt of an *existing* local node would
                # silently overwrite a kept-mine decision (or ignore a
                # peer's own pushed-back one) on a descendant several
                # levels down - the node-level keep_mine_active/
                # peer_pushed_back guards above only ever check this node's
                # own perspective_state, not its children's. A brand-new
                # node (local_missing_node) has no local content to protect
                # so isn't subject to this - only an update to something
                # that already exists locally can clobber something.
                continue
            self.trace_event(
                "session.reconcile_node",
                peer_addr=peer_addr,
                topic_uuid=topic_uuid,
                node_uuid=event["node_uuid"],
                event_type=event["type"],
                peer_state_hash=event.get("peer_state_hash"),
            )
            if node_adopt_mode(reference_node) == "shallow" and event["type"] == "peer_made_changes" and peer_node:
                # Update the node's own fields only - never cascade into its
                # children, so an allowed shallow change can't smuggle in a
                # filtered-out descendant change underneath it.
                result = self.modify(
                    event["node_uuid"], peer_node.data, peer_node.weights,
                    revision_origin_identity=peer_node.revision_origin_identity,
                )
            else:
                result = self.accept_peer_node(peer_addr, event["node_uuid"])
            changed = changed or result.status == "ok"
        self.trace_event("session.reconcile_done", peer_addr=peer_addr,
                          topic_uuid=topic_uuid, changed=changed)
        return changed

    @_session_locked
    def remove_subtree_uuids(self, root_uuid: str, uuids: set[str]) -> SessionResult:
        result = self._protocol.remove_subtree_uuids(root_uuid, uuids)
        return self._operation_result(result, root_uuid)

    @_session_locked
    def prune_deleted_nodes(self) -> None:
        deleted_uuids = self._collect_deleted_uuids(self._protocol.root)
        if not deleted_uuids:
            return
        resolved = set()
        for node_uuid in sorted(deleted_uuids):
            topic_uuid = self._topic_for_node(node_uuid)
            peers = self.peers_for_topic(topic_uuid) if topic_uuid else []
            if all(
                self._peer_topic_confirms_deletion(peer, topic_uuid, node_uuid)
                for peer in peers
            ):
                resolved.add(node_uuid)
        if not resolved:
            return
        self._protocol.remove_subtree_uuids(self._protocol.root.uuid, resolved)
        for node_uuid in resolved:
            self.trace_event(
                "session.deleted_node_pruned",
                node_uuid=node_uuid,
            )

    @staticmethod
    def _collect_deleted_uuids(node: PRSPNode) -> set[str]:
        out = set()
        if node.deleted:
            out.add(node.uuid)
        for child in node.children:
            out.update(Session._collect_deleted_uuids(child))
        return out

    @_session_locked
    def get_node(self, node_uuid: str) -> PRSPNode | None:
        node = self._protocol.index.get(node_uuid)
        return self._snapshot_node(node) if node else None

    @_session_locked
    def get_subtree(self, node_uuid: str) -> dict | None:
        node = self._protocol.index.get(node_uuid)
        if not node:
            return None
        return {
            "subtree": node.to_dict(),
            "parent_uuid": node.parent_uuid,
        }

    @_session_locked
    def snapshot_subtree_state(self, node_uuid: str) -> tuple[str, dict] | None:
        """Return a hash and its exact matching wire snapshot atomically."""
        node = self._protocol.index.get(node_uuid)
        if not node:
            return None
        return node.state_hash, {
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
                    "content_hash": tree.content_hash,
                    "state_hash": tree.state_hash,
                    "root_uuid": tree.uuid,
                    "topic_uuid": self.peer_topics.get(addr),
                    "topic_uuids": sorted(self.peer_topic_sets.get(addr) or []),
                    "status": self.peer_status.get(addr, self._new_peer_status()),
                    "channel": self.peer_channel.get(addr),
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
                             effects=self.sync_effects(changed_uuid))

    def trace_event(self, kind: str, **fields: Any) -> None:
        self.trace.event(kind, **fields)

    def sync_summary(self, peer_addr: str) -> dict[str, Any]:
        topics = {}
        for topic_uuid in sorted(self.peer_topic_sets.get(peer_addr) or []):
            topic = self._protocol.index.get(topic_uuid)
            if not topic:
                continue
            topics[topic_uuid] = topic.state_hash
        sync_hash = stable_hash({"topics": topics})
        return {
            "topics": topics,
            "sync_hash": sync_hash,
        }

    def pending_sync_effects(self, now: float | None = None) -> list[SessionEffect]:
        # The periodic sweep: every peer we owe a sync, minus those still in
        # a retry backoff.
        return self.sync_effects(respect_retry=True, now=now)

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

    def _peer_topic_confirms_deletion(self, peer_addr: str,
                                      topic_uuid: str,
                                      node_uuid: str) -> bool:
        cache = self.peer_perspectives.get(peer_addr)
        if not cache:
            return False
        topic = self._find_in_tree(cache, topic_uuid)
        if not topic:
            return False
        node = self._find_in_tree(topic, node_uuid)
        if node is None:
            return True
        return bool(node.deleted)

    def sync_effects(self, changed_uuid: str | None = None,
                     respect_retry: bool = False,
                     now: float | None = None) -> list[SessionEffect]:
        # The one sync-status builder. Two callers, two optional filters:
        # a local change syncs only the peers on the affected topic
        # (changed_uuid); the periodic sweep skips peers still backing off
        # (respect_retry). Everything else - the summary, the
        # already-delivered check, the effect shape - is common.
        effects = []
        changed_topics = set(self._topics_for_change(changed_uuid))
        if changed_uuid and not changed_topics:
            return []
        now = time.time() if now is None else now
        for peer in sorted(self.members - {self.address}):
            if changed_topics and not (changed_topics & self.peer_topic_sets.get(peer, set())):
                continue
            state = self.peer_sync_state.setdefault(peer, self._new_peer_sync_state())
            if respect_retry:
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
            # Last topic gone - nothing left to track this peer for.
            self.remove_peer(peer_addr)

    @_session_locked
    def remove_peer(self, peer_addr: str) -> None:
        # The single per-peer teardown: every path that stops tracking a
        # peer (reconnect superseding an old address, handle_leave, the
        # last-topic case above) goes through here. They used to each pop
        # their own subset, and the subsets had drifted apart - which is
        # how stale peer_channel entries survived a leave.
        #
        # peer_identity_key is deliberately NOT cleared: it's knowledge
        # ("this address belongs to identity X"), not registration, and it
        # stays true after teardown. Clearing it here would erase the very
        # evidence relay's redundancy check needs to keep suppressing this
        # address on later polls - the self-erasing-evidence flip-flop,
        # one level up. Reconnect-replace (accept_connect_token) forgets
        # superseded addresses explicitly instead.
        self.peer_topic_sets.pop(peer_addr, None)
        self.peer_fetch_topic_sets.pop(peer_addr, None)
        self.peer_topics.pop(peer_addr, None)
        self.peer_perspectives.pop(peer_addr, None)
        self.peer_status.pop(peer_addr, None)
        self.peer_sync_state.pop(peer_addr, None)
        self.peer_channel.pop(peer_addr, None)
        self.members.discard(peer_addr)

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

    def _analyze_transition_node(self, peer_addr: str,
                                 local_node: PRSPNode | None,
                                 peer_node: PRSPNode | None,
                                 is_topic_root: bool = False) -> dict:
        if not local_node:
            # A node that only the peer still has, and only as a tombstone,
            # isn't something to adopt - both sides already agree there's
            # nothing live here. Without this, a deleted node that has been
            # pruned locally keeps reappearing as "missing" every time the
            # peer's cache is compared, so auto-adopt re-materializes the
            # tombstone only for the next prune pass to remove it again -
            # an endless adopt/prune churn.
            if peer_node.deleted:
                return self._transition_event("in_agreement", peer_addr, None, peer_node)
            return self._transition_event("local_missing_node", peer_addr, None, peer_node)
        if not peer_node:
            if local_node.deleted:
                return self._transition_event("in_agreement", peer_addr, local_node, None)
            return self._transition_event("peer_missing_node", peer_addr, local_node, None)

        # A topic's own root node is grafted at a different parent in every
        # peer's local tree (see attach_topic) - that's an artifact of how
        # topics are shared, not a move either peer made, so only content is
        # comparable at that level.
        if is_topic_root:
            event_type = self._classify_content(local_node, peer_node)
        else:
            event_type = self._classify_node(local_node, peer_node)
        return self._transition_event(event_type, peer_addr, local_node, peer_node)

    @staticmethod
    def _classify_content(local_node: PRSPNode, peer_node: PRSPNode) -> str:
        if local_node.state_hash == peer_node.state_hash:
            return "in_agreement"
        if peer_node.state_hash == local_node.previous_hash:
            return "local_made_changes"
        if local_node.state_hash == peer_node.previous_hash:
            return "peer_made_changes"
        return "divergence"

    @staticmethod
    def _classify_move(local_node: PRSPNode, peer_node: PRSPNode) -> str:
        if local_node.parent_uuid == peer_node.parent_uuid:
            return "in_agreement"
        peer_moved_from_local = local_node.parent_uuid == peer_node.previous_parent_uuid
        local_moved_from_peer = peer_node.parent_uuid == local_node.previous_parent_uuid
        if peer_moved_from_local and local_moved_from_peer:
            if peer_node.updated_at > local_node.updated_at:
                return "peer_made_changes"
            if local_node.updated_at > peer_node.updated_at:
                return "local_made_changes"
            return "divergence"
        if peer_moved_from_local:
            return "peer_made_changes"
        if local_moved_from_peer:
            return "local_made_changes"
        return "divergence"

    @staticmethod
    def _classify_node(local_node: PRSPNode, peer_node: PRSPNode) -> str:
        content = Session._classify_content(local_node, peer_node)
        move = Session._classify_move(local_node, peer_node)
        if content == "divergence" or move == "divergence":
            return "divergence"
        if content == move:
            return content
        if content == "in_agreement":
            return move
        if move == "in_agreement":
            return content
        return "divergence"

    @staticmethod
    def _transition_event(event_type: str, peer_addr: str,
                          local_node: PRSPNode | None,
                          peer_node: PRSPNode | None) -> dict:
        node = local_node or peer_node
        local_origin = (
            local_node.revision_origin_identity if local_node else None
        )
        peer_origin = peer_node.revision_origin_identity if peer_node else None
        if event_type in (
            "peer_made_changes", "local_missing_node", "divergence",
        ):
            origin = peer_origin
        elif event_type in ("local_made_changes", "peer_missing_node"):
            origin = local_origin
        else:
            origin = peer_origin or local_origin
        return {
            "type": event_type,
            "peer_addr": peer_addr,
            "node_uuid": node.uuid if node else None,
            "local_state_hash": local_node.state_hash if local_node else None,
            "peer_state_hash": peer_node.state_hash if peer_node else None,
            "local_revision": (
                Session.node_revision(local_node) if local_node else None
            ),
            "peer_revision": (
                Session.node_revision(peer_node) if peer_node else None
            ),
            "origin_identity": origin,
            "local_revision_origin_identity": local_origin,
            "peer_revision_origin_identity": peer_origin,
            "keep_mine_active": (
                Session.keep_mine_active(local_node, peer_node)
                if local_node and peer_node else None
            ),
        }

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
