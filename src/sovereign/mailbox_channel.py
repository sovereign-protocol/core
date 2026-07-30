"""Mailbox channel over Local/SFTP storage backends."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .channel import ChannelAcceptance, ChannelResult, Invitation


class MailboxChannel:
    kind = "mailbox"
    descriptor_types = frozenset({"relay", "sftp"})

    def __init__(self, manager):
        self._manager = manager

    def offer_descriptor(
        self, topic_uuids: tuple[str, ...], options: Mapping[str, Any],
    ) -> ChannelResult:
        target_id = str(options.get("target_id") or "").strip()
        if not target_id:
            return ChannelResult.success()
        descriptor = self._manager.target_descriptor(target_id)
        if not descriptor:
            return ChannelResult.error("mailbox target not found", 404)
        # Composing an invitation never assigns. Every topic, including the
        # identity, must already have this channel as its visible home.
        unassigned = sorted(
            topic_uuid for topic_uuid in topic_uuids
            if self._manager.target_for_topic(topic_uuid) != target_id
        )
        if unassigned:
            return ChannelResult.error(
                "every invited topic needs this channel as its home before inviting",
                409,
            )
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
            accepted = self._manager.accept_descriptor(
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
        result = self._manager.assign_topics_target(list(topic_uuids), target_id)
        return (
            ChannelResult.success(result.value)
            if result.status == "ok"
            else ChannelResult.error(result.reason or "could not assign topics")
        )

    def detach_topics(self, topic_uuids: Iterable[str]) -> ChannelResult:
        for topic_uuid in topic_uuids:
            result = self._manager.assign_topic_target(str(topic_uuid), None)
            if result.status != "ok":
                return ChannelResult.error(result.reason or "could not detach topic")
            self._release_topic_peers(str(topic_uuid))
        return ChannelResult.success()

    def _release_topic_peers(self, topic_uuid: str) -> None:
        """Stop tracking the peers this channel was carrying for a topic.

        Deliberately not the case _forget_departed_relay_peers describes,
        which keeps peer_topic_sets when a peer merely goes quiet for a poll.
        This is the user saying "stop using this channel here", so the
        relationship goes with it: the topic is private again and its people
        stop being members of it. The content is untouched - cards keep naming
        the person as owner or participant, and removing those is the user's
        own act, not something a detach may decide.
        """
        session = self._manager.session
        departing = [
            peer_addr
            for peer_addr in session.peer_addresses(topic_uuid)
            if session.peer_channel_for_topic(peer_addr, topic_uuid) == self.kind
        ]
        for peer_addr in departing:
            session.remove_peer_topics(peer_addr, [topic_uuid])

    def status(self) -> dict:
        return self._manager.status_payload()

    def management_descriptor(self) -> dict:
        instances = []
        for target in self._manager.list_targets():
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
        selected = self._manager.target_for_topic(topic_uuid)
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
            self._manager.create_target(prepared, verify=True)
        )

    def update_instance(self, values: Mapping[str, Any]) -> ChannelResult:
        target_id = str(values.get("id") or "").strip()
        prepared = self._target_values(values)
        if prepared is None:
            return ChannelResult.error("unsupported channel type", 400)
        return self._result(self._manager.update_target(
            target_id, prepared, verify=True,
        ))

    def test_instance(self, values: Mapping[str, Any]) -> ChannelResult:
        prepared = self._target_values(values)
        if prepared is None:
            return ChannelResult.error("unsupported channel type", 400)
        return self._result(
            self._manager.verify_target_values(prepared)
        )

    def delete_instance(self, instance_id: str) -> ChannelResult:
        # delete_target already drops the topic assignments; the peers those
        # assignments were carrying have to go the same way a "stop using"
        # would send them, or the topic stays populated by a channel that no
        # longer exists.
        assigned = [
            target.get("topic_uuids") or []
            for target in self._manager.list_targets()
            if target.get("id") == instance_id
        ]
        result = self._result(self._manager.delete_target(instance_id))
        if result.ok:
            for topic_uuid in [item for topics in assigned for item in topics]:
                self._release_topic_peers(str(topic_uuid))
        return result

    def polling_endpoints(self):
        return self._manager.all_connections()

    def close(self) -> None:
        for connection in self._manager.all_connections():
            # A cancelled asyncio.to_thread poll keeps running until its
            # current transport call returns. Close under the same lock as
            # relay I/O so storage cannot disappear between a phase's guard
            # and its backend call.
            with connection._io_lock:
                storage = connection.storage
                if storage is None:
                    continue
                storage.close()
                # Shutdown may be entered again after a partial lifespan failure.
                # Mark the resource closed so a second pass remains a no-op.
                if connection.storage is storage:
                    connection.storage = None

    def list_targets(self):
        return self._manager.list_targets()

    def target_for_topic(self, topic_uuid: str):
        return self._manager.target_for_topic(topic_uuid)

    def assign_topic_target(self, topic_uuid: str, target_id: str | None):
        return self._manager.assign_topic_target(topic_uuid, target_id)

    def detach_instance_topics(
        self, topic_uuids: Iterable[str], instance_id: str,
    ) -> ChannelResult:
        for topic_uuid in topic_uuids:
            topic_uuid = str(topic_uuid)
            if self._manager.target_for_topic(topic_uuid) != instance_id:
                return ChannelResult.error("channel is not used by topic", 409)
            result = self._manager.assign_topic_target(topic_uuid, None)
            if result.status != "ok":
                return ChannelResult.error(result.reason or "could not detach topic")
            self._release_topic_peers(topic_uuid)
        return ChannelResult.success()

    def peer_liveness(
        self, peer_id: str, target_id: str | None = None,
        topic_uuid: str | None = None,
    ):
        return self._manager.peer_liveness(peer_id, target_id, topic_uuid)

    def peer_liveness_for_address(
        self, peer_addr: str, topic_uuid: str | None = None,
    ):
        prefix = "relay:"
        if not peer_addr.startswith(prefix):
            return None
        target_id = self.target_for_topic(topic_uuid) if topic_uuid else None
        return self.peer_liveness(
            peer_addr[len(prefix):], target_id, topic_uuid,
        )

    def compose_pairing_token(self, target_id: str = "") -> ChannelResult:
        return self._result(self._manager.compose_pairing_token(target_id))

    def accept_pairing_token(self, token: dict) -> ChannelResult:
        return self._result(self._manager.accept_pairing_token(token))

    def sibling_alarms(self) -> list[dict]:
        return self._manager.sibling_alarms()

    def resolve_sibling_alarm(
        self, topic_uuid: str, decision: str,
    ) -> ChannelResult:
        return self._result(
            self._manager.resolve_sibling_alarm(topic_uuid, decision),
        )

    def read_blob(
        self, blob_id: str, allow_remote: bool = True, *,
        peer_addr: str | None = None, topic_uuid: str | None = None,
    ):
        if not allow_remote or not peer_addr or not topic_uuid:
            return None
        target_id = self.target_for_topic(topic_uuid)
        connection = (
            self._manager.connection_for_target(target_id)
            if target_id else None
        )
        return connection.read_blob(blob_id) if connection else None
