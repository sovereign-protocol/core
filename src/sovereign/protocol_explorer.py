"""
Manual app logic.

Functionality:
  Human-operated generic perspective editor for the new stack. It exposes
  atomic protocol operations through Session. The host executes lifecycle
  effects returned by those operations.
"""

from __future__ import annotations

from .session import Session, SessionResult


class ManualLogic:
    def __init__(self, session: Session, config: dict):
        self.session = session
        self.config = config

    def state(self) -> dict:
        return {
            "root": self.session.protocol.root.to_dict(),
            "network": self.session.get_network_info(),
            "peers": {
                addr: tree.to_dict()
                for addr, tree in sorted(
                    self.session.peer_perspectives_for_topic().items(),
                )
            },
        }

    def start_discussion(self, topic_uuid: str) -> SessionResult:
        return self.session.start_discussion(topic_uuid)

    def create_child(self, parent_uuid: str, data: dict,
                     weights: dict[str, float] | None = None) -> SessionResult:
        return self.session.create_child(parent_uuid, data, weights)

    def modify(self, node_uuid: str, data: dict,
               weights: dict[str, float] | None = None) -> SessionResult:
        return self.session.modify(node_uuid, data, weights)

    def delete(self, node_uuid: str) -> SessionResult:
        return self.session.delete(node_uuid)

    def copy(self, source_uuid: str, destination_uuid: str) -> SessionResult:
        return self.session.copy(source_uuid, destination_uuid)

    def move(self, source_uuid: str, destination_uuid: str) -> SessionResult:
        return self.session.move(source_uuid, destination_uuid)

    def accept_peer_node(self, source_addr: str, node_uuid: str,
                         adopt_absence: bool = False) -> SessionResult:
        if adopt_absence:
            return self.session.delete(node_uuid)
        peer = self.session.get_cached_peer_subtree(source_addr, node_uuid)
        if not peer:
            return SessionResult("error", reason="peer node not found")
        local = self.session.protocol.index.get(node_uuid)
        if local:
            parent_uuid = local.parent_uuid
        else:
            parent_uuid = peer.parent_uuid
        if not parent_uuid or parent_uuid not in self.session.protocol.index:
            return SessionResult("error", reason="local parent not found")

        return self.session.adopt_subtree(peer, parent_uuid)
