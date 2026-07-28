"""Core-owned collaboration services.

Applications receive only :class:`ApplicationCollaborationView`.  Channel
registration, configuration, topic bindings, invitation negotiation and
effect delivery remain private to Core.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .channel import ChannelManager, ChannelResult


@dataclass(frozen=True)
class ApplicationCollaborationView:
    """The deliberately small, read-only collaboration surface for apps."""

    _network_info: Callable[[str | None], dict]
    _peer_liveness: Callable[[str, str | None], dict | None]

    def network_info(self, topic_uuid: str | None = None) -> dict:
        return self._network_info(topic_uuid)

    def peer_liveness_for_address(
        self, peer_addr: str, topic_uuid: str | None = None,
    ) -> dict | None:
        return self._peer_liveness(peer_addr, topic_uuid)


class CollaborationService:
    """Session-level owner of channels, bindings and invitations."""

    RELEASE_TOPIC_EFFECT = "release_topic_channels"

    def __init__(self, session, channel_manager: ChannelManager):
        self.session = session
        self._channels = channel_manager
        self.application_view = ApplicationCollaborationView(
            self.network_info,
            self.peer_liveness_for_address,
        )

    def network_info(self, topic_uuid: str | None = None) -> dict:
        return self._channels.network_info(topic_uuid)

    def peer_liveness_for_address(
        self, peer_addr: str, topic_uuid: str | None = None,
    ) -> dict | None:
        return self._channels.peer_liveness_for_address(peer_addr, topic_uuid)

    def execute_effects(self, effects: Iterable[Any]) -> list[Any]:
        """Execute application results without exposing channel machinery."""
        ordinary = []
        deliveries = []
        for effect in effects:
            if getattr(effect, "type", "") == self.RELEASE_TOPIC_EFFECT:
                topic_uuid = str(
                    (getattr(effect, "payload", {}) or {}).get("topic_uuid")
                    or getattr(effect, "target", "")
                    or ""
                ).strip()
                if topic_uuid:
                    released = self.release_topic(topic_uuid)
                    if released.ok and released.value:
                        deliveries.extend(released.value)
                continue
            ordinary.append(effect)
        if ordinary:
            deliveries.extend(self._channels.execute_effects(ordinary))
        return deliveries

    def release_topic(self, topic_uuid: str) -> ChannelResult:
        deliveries = []
        for channel in self._channels.channels():
            result = channel.detach_topics((topic_uuid,))
            if not result.ok:
                return result
            if isinstance(result.value, (list, tuple)):
                deliveries.extend(result.value)
        return ChannelResult.success(deliveries)

    def channels_payload(self) -> dict:
        channel_types = []
        instances = []
        for channel in self._channels.channels():
            descriptor = getattr(channel, "management_descriptor", None)
            if not descriptor:
                continue
            described = descriptor()
            channel_types.extend(described.get("types") or [])
            instances.extend(described.get("instances") or [])
        return {
            "status": "ok",
            "types": channel_types,
            "channels": instances,
        }

    def topic_sharing_payload(self, topic_uuid: str) -> ChannelResult:
        topic = self.session.get_node(topic_uuid)
        if not topic or not self.session.supports_shared_topic(topic):
            return ChannelResult.error("application topic not found", 404)
        network = self.network_info(topic_uuid)
        peers = []
        identities = {
            item.get("address"): item
            for item in self.session.known_identities()
            if item.get("address")
        }
        for peer_addr, info in sorted((network.get("peers") or {}).items()):
            if not self.session.peer_discusses_node(peer_addr, topic_uuid):
                continue
            identity = identities.get(peer_addr) or {}
            peers.append({
                "address": peer_addr,
                "name": identity.get("name") or peer_addr,
                "picture": identity.get("picture") or "",
                "identity_uuid": identity.get("uuid") or "",
                "channel": info.get("channel") or "",
                "status": info.get("status") or {},
                "channel_liveness": info.get("channel_liveness"),
            })

        bindings = []
        for channel in self._channels.channels():
            topic_bindings = getattr(channel, "topic_bindings", None)
            if topic_bindings:
                bindings.extend(topic_bindings(topic_uuid))
        return ChannelResult.success({
            "status": "ok",
            "topic_uuid": topic_uuid,
            "people": peers,
            "channels": bindings,
        })

    def configure_channel(self, values: dict) -> ChannelResult:
        channel = self._managed_channel(values.get("kind"))
        if not channel:
            return ChannelResult.error("unknown managed channel", 400)
        operation = (
            channel.update_instance
            if str(values.get("id") or "").strip()
            else channel.create_instance
        )
        return operation(values)

    def test_channel(self, values: dict) -> ChannelResult:
        channel = self._managed_channel(values.get("kind"))
        if not channel:
            return ChannelResult.error("unknown managed channel", 400)
        return channel.test_instance(values)

    def delete_channel(self, channel_ref: str) -> ChannelResult:
        """Remove a channel from this client, releasing whatever it carried.

        Deliberately unconditional. This used to refuse while any topic was
        still assigned to the channel, which turned the assignment - something
        the user never sees, and which nothing clears when the peers go away -
        into a lock on the channel list, reported as a bare topic uuid. A
        topic left with no channel is simply private again, and that is what
        deleting a channel means.
        """
        channel, instance_id = self._channel_instance(channel_ref)
        if not channel or not instance_id:
            return ChannelResult.error("channel not found", 404)
        return channel.delete_instance(instance_id)

    def set_topic_channel(
        self, topic_uuid: str, channel_ref: str, enabled: bool,
    ) -> ChannelResult:
        topic = self.session.get_node(topic_uuid)
        if not topic or not self.session.supports_shared_topic(topic):
            return ChannelResult.error("application topic not found", 404)
        channel, instance_id = self._channel_instance(channel_ref)
        if not channel:
            return ChannelResult.error("channel not found", 404)
        if enabled:
            options = {"instance_id": instance_id} if instance_id else {}
            return channel.attach_topics((topic_uuid,), options)
        if instance_id:
            detach_instance = getattr(channel, "detach_instance_topics", None)
            if not detach_instance:
                return ChannelResult.error("channel instance cannot be detached", 400)
            return detach_instance((topic_uuid,), instance_id)
        return channel.detach_topics((topic_uuid,))

    def compose_invitation(
        self, topic_uuid: str, channel_ref: str,
    ) -> ChannelResult:
        if not str(topic_uuid or "").strip():
            return ChannelResult.error("select a topic first", 400)
        topic = self.session.get_node(topic_uuid)
        if not topic or not self.session.supports_shared_topic(topic):
            return ChannelResult.error("application topic not found", 404)
        channel, instance_id = self._channel_instance(channel_ref)
        if not channel:
            return ChannelResult.error("channel not found", 404)
        options = {}
        if instance_id:
            options[channel.kind] = {"target_id": instance_id}
        elif channel.kind == "http":
            options[channel.kind] = {}
        else:
            return ChannelResult.error("channel instance is required", 400)
        return self._channels.compose_token((topic_uuid,), options)

    def accept_invitation(self, token: dict) -> ChannelResult:
        return self._channels.accept_token(token)

    # ---- pairing -------------------------------------------------------

    def compose_pairing_token(self, target_id: str = "") -> ChannelResult:
        manager = self._relay_manager()
        if not manager:
            return ChannelResult.error("no mailbox channel", 404)
        result = manager.compose_pairing_token(target_id)
        if result.status != "ok":
            return ChannelResult.error(result.reason or "could not pair", 409)
        return ChannelResult.success(result.value)

    def accept_pairing_token(self, token: dict) -> ChannelResult:
        manager = self._relay_manager()
        if not manager:
            return ChannelResult.error("no mailbox channel", 404)
        result = manager.accept_pairing_token(token or {})
        if result.status != "ok":
            return ChannelResult.error(result.reason or "could not pair", 409)
        return ChannelResult.success(result.value)

    # ---- sibling alarms ------------------------------------------------

    def _relay_manager(self):
        channel = self._channels.channel("mailbox")
        return getattr(channel, "manager", None) if channel else None

    def sibling_alarms_payload(self) -> dict:
        """Topics where another client of this user published something that
        this client's own unpublished work was not built on.

        Session decides a topic is in this state; what to do about it is the
        application's to ask. The title is included because "one of your
        topics" is not something a person can act on.
        """
        manager = self._relay_manager()
        alarms = manager.sibling_alarms() if manager else []
        described = []
        for alarm in alarms:
            node = self.session.get_node(alarm["topic_uuid"])
            data = node.data if node else {}
            described.append({
                **alarm,
                "title": data.get("name") or data.get("title") or "",
            })
        return {"status": "ok", "alarms": described}

    def resolve_sibling_alarm(self, topic_uuid: str,
                              decision: str) -> ChannelResult:
        manager = self._relay_manager()
        if not manager:
            return ChannelResult.error("no mailbox channel", 404)
        result = manager.resolve_sibling_alarm(topic_uuid, decision)
        if result.status != "ok":
            return ChannelResult.error(result.reason or "could not resolve", 409)
        return ChannelResult.success(result.value)

    def _managed_channel(self, kind: Any):
        channel = self._channels.channel(str(kind or "").strip())
        return channel if channel and hasattr(channel, "management_descriptor") else None

    def _channel_instance(self, channel_ref: str):
        normalized = str(channel_ref or "").strip()
        if normalized == "http":
            return self._channels.channel("http"), ""
        kind, separator, instance_id = normalized.partition(":")
        if not separator:
            return None, ""
        channel = self._managed_channel(kind)
        return channel, instance_id
