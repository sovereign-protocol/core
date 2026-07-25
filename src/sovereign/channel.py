"""Application-neutral channel contracts and selection manager."""

from __future__ import annotations

import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

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
class EffectDeliveryChannel(Protocol):
    def execute_effect(self, effect): ...
    def execute_effects(self, effects): ...


@runtime_checkable
class PollingEndpoint(Protocol):
    poll_interval_seconds: float

    def has_active_relationship(self) -> bool: ...
    def write_presence(self) -> None: ...
    def publish_due_topics(self) -> list[Any]: ...
    def poll_and_apply(self) -> list[Any]: ...


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
        channel_options: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> ChannelResult:
        topics = self._topics(topic_uuids)
        if not topics:
            return ChannelResult.error("choose at least one topic", 400)
        options_by_kind = channel_options or {}
        if not isinstance(options_by_kind, Mapping):
            return ChannelResult.error("channel_options must be an object", 400)
        channels = self.channels()
        unknown = sorted(set(options_by_kind) - {item.kind for item in channels})
        if unknown:
            return ChannelResult.error(
                f"unknown channel option: {unknown[0]}", 400,
            )
        descriptors = []
        for channel in channels:
            options = options_by_kind.get(channel.kind) or {}
            if not isinstance(options, Mapping):
                return ChannelResult.error(
                    f"options for channel {channel.kind!r} must be an object", 400,
                )
            offered = channel.offer_descriptor(topics, options)
            if not offered.ok:
                if channel.kind in options_by_kind:
                    return offered
                continue
            if offered.value:
                descriptors.append(dict(offered.value))
        if not descriptors:
            return ChannelResult.error("no channel available", 409)
        identity = self.session.identity
        return ChannelResult.success({
            "token_version": CONNECT_TOKEN_VERSION,
            "identity": identity.to_dict(),
            "topic_uuids": sorted({*topics, identity.uuid}),
            "channels": descriptors,
        })

    def accept_token(self, token: dict) -> ChannelResult:
        if (
            not isinstance(token, dict)
            or token.get("token_version") != CONNECT_TOKEN_VERSION
        ):
            return ChannelResult.error("unrecognized token version", 400)
        return self.accept_invitation(
            token.get("identity"),
            token.get("topic_uuids") or [],
            token.get("channels") or [],
        )

    def accept_invitation(
        self,
        identity: dict | None,
        topic_uuids: Iterable[Any],
        descriptors: Iterable[Any],
    ) -> ChannelResult:
        invitation = Invitation(identity, self._topics(topic_uuids))
        offered = [item for item in descriptors if isinstance(item, dict)]
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        selected_channel = None
        selected = None

        # Registration order is preference order. Direct HTTP is registered
        # before mailbox, preserving direct-first/fallback behavior without a
        # channel-type branch in the host.
        for channel in self.channels():
            descriptor = next((
                item for item in offered
                if item.get("type") in channel.descriptor_types
                and item.get("descriptor_version") == CHANNEL_DESCRIPTOR_VERSION
            ), None)
            if descriptor is None:
                continue
            accepted = channel.accept_descriptor(descriptor, invitation)
            if accepted.ok:
                selected_channel = channel
                selected = accepted.value
                results[channel.kind] = dict(selected.details)
                break
            errors[channel.kind] = accepted.reason or "channel unavailable"

        if selected_channel is None or not isinstance(selected, ChannelAcceptance):
            detail = "; ".join(
                f"{kind}: {reason}" for kind, reason in sorted(errors.items())
            )
            reason = "no usable channel in token"
            if detail:
                reason = f"{reason} ({detail})"
            self.session.trace_event(
                "transport.connect_token_rejected",
                offered_channel_types=[item.get("type") for item in offered],
                errors=errors,
            )
            return ChannelResult.error(reason, 409).with_value({"errors": errors})

        selected_addr = selected.peer_addr
        identity_key = (
            identity.get("data", {}).get("identity_key")
            if isinstance(identity, dict) else None
        )
        if identity_key:
            for old_addr in self.session.addresses_for_identity(identity_key):
                if old_addr == selected_addr:
                    continue
                was_direct_member = old_addr in self.session.members
                self.session.remove_peer(old_addr)
                # Direct members are superseded endpoints and may be forgotten.
                # Indirect observations remain valid identity knowledge used to
                # suppress redundant channel representations. This is a Session
                # relationship distinction, not an address-format convention.
                if was_direct_member:
                    self.session.peer_identity_key.pop(old_addr, None)

        if isinstance(identity, dict):
            self.session.apply_peer_identity_snapshot(selected_addr, identity)
        self.session.note_peer_channel(selected_addr, selected_channel.kind)
        self.session.trace_event(
            "transport.connect_token_selected",
            selected_type=selected_channel.kind,
            selected_addr=selected_addr,
        )
        return ChannelResult.success({
            "status": "ok",
            "channels_used": [selected_channel.kind],
            "results": results,
        })

    def _effect_channel(self, effect):
        target = str(getattr(effect, "target", "") or "")
        selected_kind = self.session.peer_channel.get(target)
        selected = self.channel(selected_kind) if selected_kind else None
        if isinstance(selected, EffectDeliveryChannel):
            return selected
        if selected is not None:
            raise RuntimeError(
                f"selected channel {selected.kind!r} cannot deliver Session effects"
            )
        channel = next((
            item for item in self.channels()
            if isinstance(item, EffectDeliveryChannel)
        ), None)
        if channel is None:
            raise RuntimeError("no effect-delivery channel is registered")
        return channel

    def execute_effect(self, effect):
        return self._effect_channel(effect).execute_effect(effect)

    def execute_effects(self, effects):
        effects = list(effects)
        if not effects:
            return []
        grouped: dict[str, tuple[Any, list[tuple[int, Any]]]] = {}
        for index, effect in enumerate(effects):
            channel = self._effect_channel(effect)
            grouped.setdefault(channel.kind, (channel, []))[1].append((index, effect))
        deliveries = [None] * len(effects)
        for channel, indexed_effects in grouped.values():
            produced = list(channel.execute_effects([
                effect for _index, effect in indexed_effects
            ]))
            if len(produced) != len(indexed_effects):
                raise RuntimeError(
                    f"channel {channel.kind!r} returned an invalid delivery count"
                )
            for (index, _effect), delivery in zip(indexed_effects, produced):
                deliveries[index] = delivery
        return deliveries

    def polling_endpoints(self) -> list[PollingEndpoint]:
        endpoints = []
        for channel in self.channels():
            if isinstance(channel, PollingChannel):
                endpoints.extend(channel.polling_endpoints())
        return endpoints

    def status(self) -> dict:
        return {
            channel.kind: channel.status()
            for channel in self.channels()
        }

    def network_info(
        self, topic_uuid: str | None = None, *, include_channel_status: bool = False,
    ) -> dict:
        """Return Session network state enriched by channel capabilities."""
        info = self.session.get_network_info()
        for addr, peer_info in (info.get("peers") or {}).items():
            liveness = self.peer_liveness_for_address(addr, topic_uuid)
            if liveness is not None:
                peer_info["channel_liveness"] = liveness
        if include_channel_status:
            info["channels"] = self.status()
        return info

    def peer_liveness_for_address(
        self, peer_addr: str, topic_uuid: str | None = None,
    ) -> dict | None:
        kind = self.session.peer_channel.get(peer_addr)
        channel = self.channel(kind) if kind else None
        liveness = getattr(channel, "peer_liveness_for_address", None)
        return liveness(peer_addr, topic_uuid) if liveness else None

    def close(self) -> None:
        for channel in reversed(self.channels()):
            channel.close()

    @contextmanager
    def persistence_guard(self):
        with ExitStack() as stack:
            for channel in self.channels():
                lock = getattr(channel, "persistence_lock", None)
                if lock is not None:
                    stack.enter_context(lock)
            yield

    def read_blob(self, blob_id: str, allow_remote: bool = True):
        # Mailbox is registered after HTTP and is cheaper/more deterministic
        # for a shared attachment, so consult channel capabilities in reverse
        # registration order.
        for channel in reversed(self.channels()):
            reader = getattr(channel, "read_blob", None)
            if not reader:
                continue
            data = reader(blob_id, allow_remote=allow_remote)
            if data is not None:
                return data
        return None

    # Named targets remain a concrete mailbox feature until a genuinely
    # distinct second targeted channel exists. Apps use these manager-level
    # operations without selecting or importing the mailbox implementation.
    def _target_channel(self):
        channel = self.channel("mailbox")
        if channel is None:
            raise RuntimeError("mailbox channel is not registered")
        return channel

    def list_targets(self):
        return self._target_channel().list_targets()

    def target_for_topic(self, topic_uuid: str):
        return self._target_channel().target_for_topic(topic_uuid)

    def assign_topic_target(self, topic_uuid: str, target_id: str | None):
        return self._target_channel().assign_topic_target(topic_uuid, target_id)

    def peer_liveness(self, peer_id: str, target_id: str | None = None):
        return self._target_channel().peer_liveness(peer_id, target_id)

    def leave_topic(self, topic_uuid: str):
        deliveries = []
        for channel in self.channels():
            method = getattr(channel, "leave_topic", None)
            if method:
                deliveries.extend(method(topic_uuid) or [])
        return deliveries

    def disconnect(self):
        deliveries = []
        for channel in self.channels():
            method = getattr(channel, "disconnect", None)
            if method:
                deliveries.extend(method() or [])
        return deliveries

    def leave(self):
        deliveries = []
        for channel in self.channels():
            method = getattr(channel, "leave", None)
            if method:
                deliveries.extend(method() or [])
        return deliveries

    def _http_channel(self):
        channel = self.channel("http")
        if channel is None:
            raise RuntimeError("direct HTTP channel is not registered")
        return channel

    def join_discussion(self, address, topic_uuid=None, topic_uuids=None):
        return self._http_channel().join_discussion(
            address, topic_uuid, topic_uuids,
        )

    def invite_to_discuss(
        self, address, topic_uuid=None, topic_uuids=None, read_only=False,
    ):
        return self._http_channel().invite_to_discuss(
            address, topic_uuid, topic_uuids, read_only=read_only,
        )

    def observe_topic(self, address, topic_uuid=None, topic_uuids=None):
        return self._http_channel().observe_topic(
            address, topic_uuid, topic_uuids,
        )
