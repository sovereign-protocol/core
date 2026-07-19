"""Direct HTTP implementation of the Core channel contract."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .channel import ChannelAcceptance, ChannelResult, Invitation
from .versions import CHANNEL_DESCRIPTOR_VERSION


class DirectHttpChannel:
    kind = "http"
    descriptor_types = frozenset({"http"})

    def __init__(
        self,
        address: str,
        adapter,
        *,
        offer_enabled: bool = True,
        accept_enabled: bool = True,
    ):
        self.address = address.rstrip("/")
        self.adapter = adapter
        self.offer_enabled = bool(offer_enabled)
        self.accept_enabled = bool(accept_enabled)

    def offer_descriptor(
        self, topic_uuids: tuple[str, ...], options: Mapping[str, Any],
    ) -> ChannelResult:
        if not self.offer_enabled or options.get("enabled") is False:
            return ChannelResult.success()
        return ChannelResult.success({
            "type": "http",
            "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
            "address": self.address,
        })

    def accept_descriptor(
        self, descriptor: dict, invitation: Invitation,
    ) -> ChannelResult:
        if not self.accept_enabled:
            return ChannelResult.error("disabled by local relay-only policy")
        address = str(descriptor.get("address") or "").strip().rstrip("/")
        if not address:
            return ChannelResult.error("HTTP descriptor missing address")
        if address == self.address:
            return ChannelResult.error("cannot connect to this client itself")
        result = self.adapter.join_discussion(
            address, None, list(invitation.topic_uuids),
        )
        if result.get("status") != "ok":
            return ChannelResult.error(result.get("reason") or "join failed")
        return ChannelResult.success(ChannelAcceptance(address, result))

    def attach_topics(
        self, topic_uuids: Iterable[str], options: Mapping[str, Any] | None = None,
    ) -> ChannelResult:
        return ChannelResult.success(tuple(topic_uuids))

    def detach_topics(self, topic_uuids: Iterable[str]) -> ChannelResult:
        deliveries = []
        for topic_uuid in topic_uuids:
            deliveries.extend(self.adapter.leave_topic(str(topic_uuid)))
        return ChannelResult.success(deliveries)

    def status(self) -> dict:
        return {
            "kind": self.kind,
            "address": self.address,
            "offer_enabled": self.offer_enabled,
            "accept_enabled": self.accept_enabled,
        }

    def execute_effect(self, effect):
        return self.adapter.execute_effect(effect)

    def execute_effects(self, effects):
        return self.adapter.execute_effects(effects)

    def close(self) -> None:
        """Direct HTTP owns no persistent connection resources."""

    def join_discussion(self, address, topic_uuid=None, topic_uuids=None):
        return self.adapter.join_discussion(address, topic_uuid, topic_uuids)

    def invite_to_discuss(
        self, address, topic_uuid=None, topic_uuids=None, read_only=False,
    ):
        return self.adapter.invite_to_discuss(
            address, topic_uuid, topic_uuids, read_only=read_only,
        )

    def observe_topic(self, address, topic_uuid=None, topic_uuids=None):
        return self.adapter.observe_topic(address, topic_uuid, topic_uuids)

    def leave_topic(self, topic_uuid: str):
        return self.adapter.leave_topic(topic_uuid)

    def disconnect(self):
        return self.adapter.disconnect()

    def leave(self):
        return self.adapter.leave_discussion()

    def read_blob(self, blob_id: str, allow_remote: bool = True):
        if not allow_remote:
            return None
        import requests
        for peer in sorted(
            self.adapter.session.members - {self.adapter.session.address}
        ):
            if not peer.startswith(("http://", "https://")):
                continue
            try:
                response = requests.get(
                    f"{peer.rstrip('/')}/api/blob/{blob_id}",
                    headers={"X-Sovereign-Blob-Hop": "1"},
                    timeout=10,
                )
                if response.status_code == 200:
                    return response.content
            except Exception:
                continue
        return None

    def close(self) -> None:
        return None
