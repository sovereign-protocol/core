"""Mailbox channel over Local/SFTP storage backends."""

from __future__ import annotations

import threading
from typing import Any, Iterable, Mapping

from .channel import ChannelAcceptance, ChannelResult, Invitation


class MailboxChannel:
    kind = "mailbox"
    descriptor_types = frozenset({"relay", "sftp"})

    def __init__(self, manager):
        self.manager = manager
        self.persistence_lock = getattr(manager, "_manager_lock", threading.RLock())

    def offer_descriptor(
        self, topic_uuids: tuple[str, ...], options: Mapping[str, Any],
    ) -> ChannelResult:
        target_id = str(options.get("target_id") or "").strip()
        if not target_id:
            return ChannelResult.success()
        descriptor = self.manager.target_descriptor(target_id)
        if not descriptor:
            return ChannelResult.error("mailbox target not found", 404)
        assigned = self.manager.assign_topics_target(list(topic_uuids), target_id)
        if assigned.status != "ok":
            return ChannelResult.error(assigned.reason or "could not assign topics")
        return ChannelResult.success(descriptor)

    def accept_descriptor(
        self, descriptor: dict, invitation: Invitation,
    ) -> ChannelResult:
        relay_identity = str(descriptor.get("identity") or "").strip()
        if not relay_identity:
            return ChannelResult.error("relay descriptor missing identity")
        if not invitation.inviter_identity_uuid:
            return ChannelResult.error("token missing inviter identity")
        try:
            accepted = self.manager.accept_descriptor(
                descriptor,
                list(invitation.topic_uuids),
                invitation.inviter_identity_uuid,
            )
        except Exception as exc:
            return ChannelResult.error(
                f"mailbox unavailable: {type(exc).__name__}: {exc}",
            )
        if accepted.status != "ok":
            return ChannelResult.error(
                accepted.reason or "mailbox unavailable",
            )
        return ChannelResult.success(ChannelAcceptance(
            f"relay:{relay_identity}",
            {"status": "ok", "reason": None, "target_id": accepted.value},
        ))

    def attach_topics(
        self, topic_uuids: Iterable[str], options: Mapping[str, Any] | None = None,
    ) -> ChannelResult:
        target_id = str(
            (options or {}).get("instance_id")
            or (options or {}).get("target_id")
            or ""
        ).strip()
        if not target_id:
            return ChannelResult.error("mailbox target_id is required")
        result = self.manager.assign_topics_target(list(topic_uuids), target_id)
        return (
            ChannelResult.success(result.value)
            if result.status == "ok"
            else ChannelResult.error(result.reason or "could not assign topics")
        )

    def detach_topics(self, topic_uuids: Iterable[str]) -> ChannelResult:
        for topic_uuid in topic_uuids:
            result = self.manager.assign_topic_target(str(topic_uuid), None)
            if result.status != "ok":
                return ChannelResult.error(result.reason or "could not detach topic")
        return ChannelResult.success()

    def status(self) -> dict:
        return self.manager.status_payload()

    def management_descriptor(self) -> dict:
        instances = []
        for target in self.manager.list_targets():
            backend = target.get("backend") or "local"
            instances.append({
                **target,
                "ref": f"{self.kind}:{target['id']}",
                "kind": self.kind,
                "type": f"{backend}_relay",
                "description": (
                    "SFTP mailbox relay"
                    if backend == "sftp" else "Local folder mailbox relay"
                ),
                "available": True,
                "built_in": False,
                "removable": True,
            })
        return {
            "types": [
                {
                    "id": "sftp_relay",
                    "kind": self.kind,
                    "name": "SFTP Relay",
                    "description": "Exchange topic publications through an SFTP mailbox.",
                    "action": "configure",
                    "fields": [
                        {"name": "name", "label": "Name", "type": "text", "required": True},
                        {"name": "host", "label": "Host", "type": "text", "required": True},
                        {"name": "port", "label": "Port", "type": "number", "default": 22},
                        {"name": "username", "label": "Username", "type": "text", "required": True},
                        {"name": "password", "label": "Password", "type": "password"},
                        {"name": "root", "label": "Remote path", "type": "text", "default": "/"},
                        {
                            "name": "poll_interval_seconds",
                            "label": "Poll every (seconds)",
                            "type": "number",
                            "default": 3,
                        },
                    ],
                },
                {
                    "id": "local_relay",
                    "kind": self.kind,
                    "name": "Local Folder Relay",
                    "description": "Exchange publications through a shared local folder.",
                    "action": "configure",
                    "fields": [
                        {"name": "name", "label": "Name", "type": "text", "required": True},
                        {"name": "root", "label": "Folder", "type": "text", "required": True},
                        {
                            "name": "poll_interval_seconds",
                            "label": "Poll every (seconds)",
                            "type": "number",
                            "default": 3,
                        },
                    ],
                },
            ],
            "instances": instances,
        }

    def topic_bindings(self, topic_uuid: str) -> list[dict]:
        selected = self.manager.target_for_topic(topic_uuid)
        return [
            {
                **instance,
                "in_use": instance.get("id") == selected,
            }
            for instance in self.management_descriptor()["instances"]
        ]

    @staticmethod
    def _target_values(values: Mapping[str, Any]) -> dict | None:
        prepared = dict(values)
        channel_type = str(prepared.pop("type", "") or "")
        if channel_type not in {"sftp_relay", "local_relay"}:
            return None
        prepared.pop("kind", None)
        prepared.pop("id", None)
        prepared["backend"] = "sftp" if channel_type == "sftp_relay" else "local"
        return prepared

    @staticmethod
    def _result(result) -> ChannelResult:
        return (
            ChannelResult.success(result.value)
            if result.status == "ok"
            else ChannelResult.error(result.reason or "channel operation failed")
        )

    def create_instance(self, values: Mapping[str, Any]) -> ChannelResult:
        prepared = self._target_values(values)
        if prepared is None:
            return ChannelResult.error("unsupported channel type", 400)
        return self._result(
            self.manager.create_target(prepared, verify=True)
        )

    def update_instance(self, values: Mapping[str, Any]) -> ChannelResult:
        target_id = str(values.get("id") or "").strip()
        prepared = self._target_values(values)
        if prepared is None:
            return ChannelResult.error("unsupported channel type", 400)
        return self._result(self.manager.update_target(
            target_id, prepared, verify=True,
        ))

    def test_instance(self, values: Mapping[str, Any]) -> ChannelResult:
        prepared = self._target_values(values)
        if prepared is None:
            return ChannelResult.error("unsupported channel type", 400)
        return self._result(
            self.manager.verify_target_values(prepared)
        )

    def delete_instance(self, instance_id: str) -> ChannelResult:
        return self._result(self.manager.delete_target(instance_id))

    def polling_endpoints(self):
        return self.manager.all_connections()

    def close(self) -> None:
        for connection in self.manager.all_connections():
            reset = getattr(connection.storage, "_reset_connection", None)
            if reset:
                reset()

    def list_targets(self):
        return self.manager.list_targets()

    def target_for_topic(self, topic_uuid: str):
        return self.manager.target_for_topic(topic_uuid)

    def assign_topic_target(self, topic_uuid: str, target_id: str | None):
        return self.manager.assign_topic_target(topic_uuid, target_id)

    def detach_instance_topics(
        self, topic_uuids: Iterable[str], instance_id: str,
    ) -> ChannelResult:
        for topic_uuid in topic_uuids:
            topic_uuid = str(topic_uuid)
            if self.manager.target_for_topic(topic_uuid) != instance_id:
                return ChannelResult.error("channel is not used by topic", 409)
            result = self.manager.assign_topic_target(topic_uuid, None)
            if result.status != "ok":
                return ChannelResult.error(result.reason or "could not detach topic")
        return ChannelResult.success()

    def peer_liveness(self, peer_id: str, target_id: str | None = None):
        return self.manager.peer_liveness(peer_id, target_id)

    def peer_liveness_for_address(
        self, peer_addr: str, topic_uuid: str | None = None,
    ):
        prefix = "relay:"
        if not peer_addr.startswith(prefix):
            return None
        target_id = self.target_for_topic(topic_uuid) if topic_uuid else None
        return self.peer_liveness(peer_addr[len(prefix):], target_id)

    def leave_topic(self, topic_uuid: str):
        self.detach_topics([topic_uuid])
        return []

    def disconnect(self):
        return []

    def leave(self):
        return []

    def read_blob(
        self, blob_id: str, allow_remote: bool = True, *,
        peer_addr: str | None = None, topic_uuid: str | None = None,
    ):
        if not allow_remote or not peer_addr or not topic_uuid:
            return None
        target_id = self.target_for_topic(topic_uuid)
        connection = (
            self.manager.connection_for_target(target_id)
            if target_id else None
        )
        return connection.read_blob(blob_id) if connection else None
