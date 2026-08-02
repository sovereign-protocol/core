"""Application-neutral channel contracts and selection manager."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import (
    Any, Callable, Iterable, Mapping, Protocol,
    runtime_checkable,
)

from .versions import CHANNEL_DESCRIPTOR_VERSION, CONNECT_TOKEN_VERSION


@dataclass(frozen=True)
class Invitation:
    identity: dict | None
    topic_uuids: tuple[str, ...]

    @property
    def inviter_identity_uuid(self) -> str:
        if not isinstance(self.identity, dict):
            return ""
        return str(self.identity.get("uuid") or "").strip()


@dataclass(frozen=True)
class ChannelAcceptance:
    peer_addr: str
    details: Mapping[str, Any]


@dataclass(frozen=True)
class ChannelResult:
    status: str
    value: Any = None
    reason: str | None = None
    status_code: int = 409

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @classmethod
    def success(cls, value: Any = None) -> "ChannelResult":
        return cls("ok", value=value, status_code=200)

    @classmethod
    def error(cls, reason: str, status_code: int = 409) -> "ChannelResult":
        return cls("error", reason=reason, status_code=status_code)

    def with_value(self, value: Any) -> "ChannelResult":
        return ChannelResult(self.status, value, self.reason, self.status_code)


@runtime_checkable
class Channel(Protocol):
    kind: str
    descriptor_types: frozenset[str]

    def offer_descriptor(
        self, topic_uuids: tuple[str, ...], options: Mapping[str, Any],
    ) -> ChannelResult: ...

    def accept_descriptor(
        self, descriptor: dict, invitation: Invitation,
    ) -> ChannelResult: ...

    def attach_topics(
        self, topic_uuids: Iterable[str], options: Mapping[str, Any] | None = None,
    ) -> ChannelResult: ...

    def detach_topics(self, topic_uuids: Iterable[str]) -> ChannelResult: ...

    def status(self) -> dict: ...

    def close(self) -> None: ...


@runtime_checkable
class ManagedChannel(Channel, Protocol):
    """A channel whose named instances can be configured through Core."""

    def management_descriptor(self) -> dict: ...

    def topic_bindings(self, topic_uuid: str) -> list[dict]: ...

    def create_instance(self, values: Mapping[str, Any]) -> ChannelResult: ...

    def update_instance(self, values: Mapping[str, Any]) -> ChannelResult: ...

    def test_instance(self, values: Mapping[str, Any]) -> ChannelResult: ...

    def delete_instance(self, instance_id: str) -> ChannelResult: ...

    def detach_instance_topics(
        self, topic_uuids: Iterable[str], instance_id: str,
    ) -> ChannelResult: ...


@runtime_checkable
class LivenessChannel(Channel, Protocol):
    """A channel that can describe one routed peer's current reachability."""

    def peer_liveness_for_address(
        self, peer_addr: str, topic_uuid: str | None = None,
    ) -> dict | None: ...


@runtime_checkable
class BlobChannel(Channel, Protocol):
    """A channel that can retrieve a blob through an explicit topic route."""

    def read_blob(
        self, blob_id: str, allow_remote: bool = True, *,
        peer_addr: str | None = None, topic_uuid: str | None = None,
    ) -> bytes | None: ...


@runtime_checkable
class PairingChannel(Channel, Protocol):
    """A channel that pairs sibling clients and reports sibling conflicts."""

    def compose_pairing_token(self, target_id: str = "") -> ChannelResult: ...

    def accept_pairing_token(self, token: dict) -> ChannelResult: ...

    def sibling_alarms(self) -> list[dict]: ...

    def resolve_sibling_alarm(
        self, topic_uuid: str, decision: str,
    ) -> ChannelResult: ...


@dataclass(frozen=True)
class PollCycleResult:
    """Structured result of one complete endpoint-owned polling cycle."""

    ok: bool
    changed: bool = False
    published_before: tuple[Any, ...] = ()
    published_after: tuple[Any, ...] = ()
    applied: tuple[Any, ...] = ()
    duration_seconds: float = 0.0
    work_duration_seconds: float = 0.0
    error: str | None = None


@runtime_checkable
class PollingEndpoint(Protocol):
    """One independently scheduled channel connection.

    ``poll_once`` owns the complete transport cycle, diagnostics, and response
    publication. ``after_apply`` lets Core drain application reconciliation
    after remote state is applied and before the endpoint publishes its
    acknowledgement.
    """

    poll_interval_seconds: float

    def has_active_relationship(self) -> bool: ...

    def poll_once(
        self, after_apply: Callable[[], Any] | None = None,
    ) -> PollCycleResult: ...

    def polling_diagnostics(self) -> Mapping[str, Any]: ...


@runtime_checkable
class PollingChannel(Protocol):
    def polling_endpoints(self) -> Iterable[PollingEndpoint]: ...


class ChannelManager:
    """Own channel registration, token negotiation, and exclusivity."""

    def __init__(self, session):
        self.session = session
        self._channels: dict[str, Channel] = {}
        self._descriptor_owner: dict[str, str] = {}
        self._lock = threading.RLock()

    def register(self, channel: Channel) -> None:
        if not isinstance(channel, Channel):
            raise TypeError("invalid channel implementation")
        with self._lock:
            if channel.kind in self._channels:
                raise ValueError(f"channel {channel.kind!r} is already registered")
            for descriptor_type in channel.descriptor_types:
                owner = self._descriptor_owner.get(descriptor_type)
                if owner:
                    raise ValueError(
                        f"descriptor type {descriptor_type!r} is already handled by {owner!r}"
                    )
            self._channels[channel.kind] = channel
            for descriptor_type in channel.descriptor_types:
                self._descriptor_owner[descriptor_type] = channel.kind

    def channel(self, kind: str) -> Channel | None:
        with self._lock:
            return self._channels.get(kind)

    def channels(self) -> tuple[Channel, ...]:
        with self._lock:
            return tuple(self._channels.values())

    @staticmethod
    def _topics(values: Iterable[Any]) -> tuple[str, ...]:
        return tuple(sorted({
            str(value).strip() for value in values if str(value).strip()
        }))

    def compose_token(
        self,
        topic_uuids: Iterable[Any],
        topic_channels: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> ChannelResult:
        topics = self._topics(topic_uuids)
        if not topics:
            return ChannelResult.error("choose at least one topic", 400)
        identity = self.session.identity
        invitation_topics = self._topics((*topics, identity.uuid))
        if not isinstance(topic_channels, Mapping):
            return ChannelResult.error(
                "topic-to-channel mapping is missing or incomplete", 400,
            )
        routes = {
            str(topic_uuid): route
            for topic_uuid, route in topic_channels.items()
        }
        if set(routes) != set(invitation_topics):
            return ChannelResult.error(
                "topic-to-channel mapping is missing or incomplete", 400,
            )
        channels = self.channels()
        channels_by_kind = {channel.kind: channel for channel in channels}
        grouped_routes: list[dict[str, Any]] = []
        ordered_topics = (
            identity.uuid,
            *(topic_uuid for topic_uuid in invitation_topics
              if topic_uuid != identity.uuid),
        )
        for topic_uuid in ordered_topics:
            route = routes[topic_uuid]
            if not isinstance(route, Mapping):
                return ChannelResult.error(
                    "topic-to-channel mapping is missing or incomplete", 400,
                )
            kind = str(route.get("kind") or "").strip()
            channel = channels_by_kind.get(kind)
            if channel is None:
                return ChannelResult.error(
                    f"unknown channel option: {kind or topic_uuid}", 400,
                )
            options = {
                str(key): value
                for key, value in route.items()
                if key != "kind"
            }
            group = next((
                item for item in grouped_routes
                if item["channel"] is channel and item["options"] == options
            ), None)
            if group is None:
                group = {
                    "channel": channel,
                    "options": options,
                    "topics": [],
                }
                grouped_routes.append(group)
            group["topics"].append(topic_uuid)

        descriptors = []
        topic_channel_ids = {}
        for index, group in enumerate(grouped_routes, start=1):
            channel = group["channel"]
            route_topics = tuple(group["topics"])
            offered = channel.offer_descriptor(
                route_topics, group["options"],
            )
            if not offered.ok:
                return offered
            if not offered.value:
                return ChannelResult.error("no channel available", 409)
            channel_id = f"channel-{index}"
            descriptors.append({
                **dict(offered.value),
                "channel_id": channel_id,
            })
            for topic_uuid in route_topics:
                topic_channel_ids[topic_uuid] = channel_id
        if not descriptors:
            return ChannelResult.error("no channel available", 409)
        return ChannelResult.success({
            "token_version": CONNECT_TOKEN_VERSION,
            "identity": identity.to_dict(),
            "topic_uuids": list(invitation_topics),
            "channels": descriptors,
            "topic_channels": topic_channel_ids,
        })

    def accept_token(self, token: dict) -> ChannelResult:
        if (
            not isinstance(token, dict)
            or token.get("token_version") != CONNECT_TOKEN_VERSION
        ):
            return ChannelResult.error("unrecognized token version", 400)
        if token.get("token_kind"):
            # A pairing token carries this user's own publication identity.
            # Admitted here it would register the user's own second client as
            # another person, and accept_invitation's reconnect-replace loop
            # would then unbind the first client from exactly the topics the
            # token covers. Refused explicitly rather than by accident: that
            # failure presents as a working connection.
            return ChannelResult.error(
                "that is a pairing token - use it to add one of your own"
                " clients, not to connect to someone else",
                400,
            )
        return self.accept_invitation(
            token.get("identity"),
            token.get("topic_uuids") or [],
            token.get("channels") or [],
            token.get("topic_channels"),
        )

    def accept_invitation(
        self,
        identity: dict | None,
        topic_uuids: Iterable[Any],
        descriptors: Iterable[Any],
        topic_channels: Mapping[str, Any] | None = None,
    ) -> ChannelResult:
        invitation = Invitation(identity, self._topics(topic_uuids))
        offered = [item for item in descriptors if isinstance(item, dict)]
        if (
            not invitation.inviter_identity_uuid
            or invitation.inviter_identity_uuid not in invitation.topic_uuids
        ):
            return ChannelResult.error("token missing identity topic", 400)
        if not isinstance(topic_channels, Mapping):
            return ChannelResult.error(
                "topic-to-channel mapping is missing or incomplete", 400,
            )
        topic_channel_ids = {
            str(topic_uuid): str(channel_id or "").strip()
            for topic_uuid, channel_id in topic_channels.items()
        }
        if (
            set(topic_channel_ids) != set(invitation.topic_uuids)
            or not all(topic_channel_ids.values())
        ):
            return ChannelResult.error(
                "topic-to-channel mapping is missing or incomplete", 400,
            )

        results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        candidates: dict[str, tuple[Channel, dict]] = {}
        for descriptor in offered:
            channel_id = str(descriptor.get("channel_id") or "").strip()
            owner = self._descriptor_owner.get(str(descriptor.get("type") or ""))
            if (
                channel_id
                and
                owner
                and descriptor.get("descriptor_version")
                == CHANNEL_DESCRIPTOR_VERSION
            ):
                if channel_id in candidates:
                    return ChannelResult.error(
                        "token contains duplicate channel identifiers", 400,
                    )
                candidates[channel_id] = (self.channel(owner), descriptor)
        referenced_ids = set(topic_channel_ids.values())
        if not referenced_ids or not referenced_ids.issubset(candidates):
            self.session.trace_event(
                "transport.connect_token_rejected",
                offered_channel_types=[item.get("type") for item in offered],
                errors=errors,
            )
            return ChannelResult.error(
                "topic-to-channel mapping is missing or incomplete", 400,
            ).with_value({"errors": errors})

        accepted_routes = []
        for channel_id, (selected_channel, descriptor) in candidates.items():
            route_topics = self._topics(
                topic_uuid
                for topic_uuid, mapped_channel_id in topic_channel_ids.items()
                if mapped_channel_id == channel_id
            )
            if not route_topics:
                continue
            route_invitation = Invitation(identity, route_topics)
            accepted = selected_channel.accept_descriptor(
                descriptor, route_invitation,
            )
            selected = accepted.value
            if not accepted.ok or not isinstance(selected, ChannelAcceptance):
                errors[channel_id] = accepted.reason or "channel unavailable"
                self.session.trace_event(
                    "transport.connect_token_rejected",
                    offered_channel_types=[item.get("type") for item in offered],
                    errors=errors,
                )
                return ChannelResult.error(
                    f"{selected_channel.kind}: {errors[channel_id]}", 409,
                ).with_value({"errors": errors})
            results[channel_id] = dict(selected.details)
            accepted_routes.append((
                selected_channel, selected, route_topics,
            ))

        identity_key = (
            identity.get("data", {}).get("identity_key")
            if isinstance(identity, dict) else None
        )
        for selected_channel, selected, route_topics in accepted_routes:
            selected_addr = selected.peer_addr
            if identity_key:
                for old_addr in self.session.addresses_for_identity(identity_key):
                    if old_addr != selected_addr:
                        self.session.remove_peer_topics(
                            old_addr, route_topics,
                        )
            if isinstance(identity, dict):
                self.session.apply_peer_identity_snapshot(selected_addr, identity)
            self.session.bind_peer_topics_channel(
                selected_addr, route_topics, selected_channel.kind,
            )

        channel_kinds = sorted({
            channel.kind for channel, _selected, _topics in accepted_routes
        })
        self.session.trace_event(
            "transport.connect_token_selected",
            selected_types=channel_kinds,
            selected_addrs=sorted({
                selected.peer_addr
                for _channel, selected, _topics in accepted_routes
            }),
        )
        return ChannelResult.success({
            "status": "ok",
            "channels_used": channel_kinds,
            "results": results,
        })


    def polling_endpoints(self) -> list[PollingEndpoint]:
        endpoints = []
        for channel in self.channels():
            if isinstance(channel, PollingChannel):
                for endpoint in channel.polling_endpoints():
                    if not isinstance(endpoint, PollingEndpoint):
                        raise TypeError(
                            f"channel {channel.kind!r} returned an invalid"
                            " polling endpoint"
                        )
                    endpoints.append(endpoint)
        return endpoints

    def status(self) -> dict:
        return {
            channel.kind: channel.status()
            for channel in self.channels()
        }

    def network_info(
        self, topic_uuid: str | None = None, *, include_channel_status: bool = False,
    ) -> dict:
        """Return Session network state enriched by channel capabilities.

        Whether a peer is reachable is a question only the channel can
        answer, and it answers it the only way a mailbox can: by how recent
        the heartbeat beside their publications is.
        """
        info = self.session.get_network_info()
        for addr, peer_info in (info.get("peers") or {}).items():
            channel_kind = (
                self.session.peer_channel_for_topic(addr, topic_uuid)
                if topic_uuid else None
            )
            peer_info["channel"] = channel_kind or ""
            liveness = self.peer_liveness_for_address(addr, topic_uuid)
            if liveness is not None:
                peer_info["channel_liveness"] = liveness
            if not channel_kind:
                peer_info["status"] = {
                    "state": "offline",
                    "last_error": "No channel is selected for this topic",
                }
            else:
                alive = (liveness or {}).get("state") == "alive"
                peer_info["status"] = {
                    "state": "online" if alive else "offline",
                    "last_error": (
                        None if alive
                        else "This peer is not currently reachable"
                    ),
                }
        if include_channel_status:
            info["channels"] = self.status()
        return info

    def peer_liveness_for_address(
        self, peer_addr: str, topic_uuid: str | None = None,
    ) -> dict | None:
        kind = (
            self.session.peer_channel_for_topic(peer_addr, topic_uuid)
            if topic_uuid else None
        )
        if not kind:
            return {"state": "unrouted"}
        channel = self.channel(kind)
        if not isinstance(channel, LivenessChannel):
            return None
        return channel.peer_liveness_for_address(peer_addr, topic_uuid)

    def close(self) -> None:
        for channel in reversed(self.channels()):
            channel.close()

    def read_topic_blob(
        self, blob_id: str, peer_addr: str, topic_uuid: str,
        allow_remote: bool = True,
    ):
        if not allow_remote:
            return None
        channel_kind = self.session.peer_channel_for_topic(
            peer_addr, topic_uuid,
        )
        if not channel_kind:
            return None
        channel = self.channel(channel_kind)
        if not isinstance(channel, BlobChannel):
            return None
        return channel.read_blob(
            blob_id,
            allow_remote=True,
            peer_addr=peer_addr,
            topic_uuid=topic_uuid,
        )
