"""
Transport adapter component.

Functionality:
  Move session messages between peers. The adapter does not interpret tree or
  proposal semantics; it asks Session what a message means and executes the
  returned SessionEffect values.

Offered API:
  HttpTransportAdapter(session, http_client=None, logger=None)
  execute_effect(effect)
  execute_effects(effects)
  fetch_subtree(peer_addr, node_uuid)
  join_discussion(peer_addr, topic_uuid)
  observe_topic(peer_addr, topic_uuid)
  invite_to_discuss(peer_addr, topic_uuid)
  leave_discussion()
  p2p_join(payload)
  p2p_announce(payload)
  p2p_leave(payload)
  p2p_subtree(node_uuid)

Used API:
  session.Session and session.SessionEffect.
  protocol.ProtocolNode only for checked wire decoding.

HTTP contract:
  POST /p2p/sync_status
  POST /p2p/join
  POST /p2p/announce
  POST /p2p/leave
  GET  /p2p/subtree/{uuid}
  POST /api/join_discussion
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Protocol

from protocol import (
    ProtocolNode, UnsupportedProtocolVersion, protocol_node_from_envelope,
)
from session import Session, SessionEffect, SessionResult
from trace_log import TraceLogger


class JsonHttpClient(Protocol):
    def get_json(self, url: str, timeout: float = 5) -> dict:
        ...

    def post_json(self, url: str, payload: dict,
                  timeout: float = 5) -> dict:
        ...


class RequestsJsonHttpClient:
    def get_json(self, url: str, timeout: float = 5) -> dict:
        import requests

        response = requests.get(url, timeout=timeout)
        return self._checked_json(response)

    def post_json(self, url: str, payload: dict,
                  timeout: float = 5) -> dict:
        import requests

        response = requests.post(url, json=payload, timeout=timeout)
        return self._checked_json(response)

    @staticmethod
    def _checked_json(response) -> dict:
        try:
            payload = response.json()
        except ValueError:
            payload = {"reason": response.text}
        if response.status_code >= 400:
            reason = payload.get("reason") or payload.get("error")
            raise TransportHttpError(response.status_code, reason, payload)
        return payload


class TransportHttpError(RuntimeError):
    def __init__(self, status_code: int, reason: str | None, payload: dict):
        super().__init__(reason or f"HTTP {status_code}")
        self.status_code = status_code
        self.payload = payload


@dataclass(frozen=True)
class TransportDelivery:
    ok: bool
    effect_type: str
    target: str | None = None
    reason: str | None = None
    response: dict[str, Any] | None = None


class HttpTransportAdapter:
    def __init__(self, session: Session,
                 http_client: JsonHttpClient | None = None,
                 logger=None,
                 trace: TraceLogger | None = None):
        self.session = session
        self.http = http_client or RequestsJsonHttpClient()
        self.logger = logger or self._default_logger
        self.trace = trace or getattr(session, "trace", TraceLogger.disabled())

    # Outbound effects

    def execute_effects(
            self, effects: list[SessionEffect]) -> list[TransportDelivery]:
        return [self.execute_effect(effect) for effect in effects]

    def execute_effect(self, effect: SessionEffect) -> TransportDelivery:
        self.trace.event(
            "transport.execute_effect",
            effect_type=effect.type,
            target=effect.target,
            payload=effect.payload,
        )
        try:
            if effect.type == "send_sync_status":
                return self._send_sync_status(effect)
            if effect.type == "pull_subtree":
                return self._pull_subtree_effect(effect)
            if effect.type == "announce_peer":
                return self._announce_peer(effect)
            if effect.type == "send_leave":
                return self._send_leave(effect)
            return TransportDelivery(
                False, effect.type, effect.target, "unknown effect"
            )
        except Exception as exc:
            if effect.type == "pull_subtree" and effect.target:
                node_uuid = effect.payload.get("node_uuid")
                topic_uuid = effect.payload.get("topic_uuid")
                if (
                    node_uuid
                    and topic_uuid
                    and node_uuid != topic_uuid
                    and self._is_not_found(exc)
                ):
                    try:
                        return self._pull_subtree_effect(SessionEffect(
                            "pull_subtree",
                            effect.target,
                            {
                                "node_uuid": topic_uuid,
                                "topic_uuid": topic_uuid,
                            },
                        ))
                    except Exception as fallback_exc:
                        exc = fallback_exc
            self.logger(f"[transport] {effect.type} failed: {exc}")
            self.trace.event(
                "transport.execute_effect_failed",
                effect_type=effect.type,
                target=effect.target,
                payload=effect.payload,
                reason=str(exc),
            )
            if effect.type == "send_sync_status" and effect.target:
                self.session.record_sync_failure(effect.target, str(exc))
            if effect.type == "pull_subtree" and effect.target:
                node_uuid = effect.payload.get("node_uuid")
                topic_uuid = effect.payload.get("topic_uuid")
                if node_uuid == topic_uuid and self._is_not_found(exc):
                    self.session.remove_peer_fetch_topic(effect.target, topic_uuid)
            return TransportDelivery(
                False, effect.type, effect.target, str(exc)
            )

    def _send_sync_status(self, effect: SessionEffect) -> TransportDelivery:
        summary = effect.payload.get("summary") or {}
        sync_hash = summary.get("sync_hash")
        self.trace.event(
            "transport.send_sync_status",
            target=effect.target,
            sync_hash=sync_hash,
            topic_uuids=sorted((summary.get("topics") or {}).keys()),
        )
        response = self.http.post_json(
            self._url(effect.target, "/p2p/sync_status"),
            effect.payload,
            timeout=10,
        )
        follow_up = self.session.handle_sync_response(effect.target, response)
        follow_up_deliveries = self.execute_effects(follow_up.effects)
        self.trace.event(
            "transport.send_sync_status_ok",
            target=effect.target,
            sync_hash=sync_hash,
            response_status=response.get("status"),
            delivered_sync_hash=response.get("delivered_sync_hash"),
            follow_up_effects=[delivery.effect_type for delivery in follow_up_deliveries],
        )
        return TransportDelivery(True, effect.type, effect.target,
                                 response=response)

    def _pull_subtree_effect(self, effect: SessionEffect) -> TransportDelivery:
        node_uuid = effect.payload["node_uuid"]
        self.trace.event(
            "transport.pull_subtree",
            target=effect.target,
            node_uuid=node_uuid,
            topic_uuid=effect.payload.get("topic_uuid"),
        )
        payload = self.fetch_subtree(effect.target, node_uuid)
        subtree = self._decode_wire_subtree(payload, effect.target)
        self.session.apply_peer_subtree(
            effect.target,
            subtree,
            payload.get("parent_uuid"),
        )
        self.trace.event(
            "transport.pull_subtree_ok",
            target=effect.target,
            node_uuid=node_uuid,
            returned_uuid=subtree.uuid,
            parent_uuid=payload.get("parent_uuid"),
            state_hash=subtree.state_hash,
        )
        return TransportDelivery(True, effect.type, effect.target,
                                 response=payload)

    def _announce_peer(self, effect: SessionEffect) -> TransportDelivery:
        # Session.leave always emits plural new_addrs; "new_addr" below is
        # only the per-recipient wire field, not an accepted input shape.
        targets = effect.payload.get("new_addrs") or []
        responses = []
        for new_addr in targets:
            if not new_addr:
                continue
            responses.append(self.http.post_json(
                self._url(effect.target, "/p2p/announce"),
                {
                    "new_addr": new_addr,
                    "topic_uuid": effect.payload.get("topic_uuid"),
                    "topic_uuids": effect.payload.get("topic_uuids"),
                },
                timeout=3,
            ))
        return TransportDelivery(True, effect.type, effect.target,
                                 response={"responses": responses})

    def _send_leave(self, effect: SessionEffect) -> TransportDelivery:
        response = self.http.post_json(
            self._url(effect.target, "/p2p/leave"),
            effect.payload,
            timeout=2,
        )
        return TransportDelivery(True, effect.type, effect.target,
                                 response=response)

    # Direct transport actions

    def fetch_subtree(self, peer_addr: str, node_uuid: str) -> dict:
        self.trace.event(
            "transport.fetch_subtree",
            peer_addr=peer_addr,
            node_uuid=node_uuid,
        )
        return self.http.get_json(
            self._url(peer_addr, f"/p2p/subtree/{node_uuid}"),
            timeout=5,
        )

    def join_discussion(self, peer_addr: str, topic_uuid: str | None = None,
                        topic_uuids: list[str] | None = None) -> dict:
        topic_uuids = self._topic_uuids(topic_uuid, topic_uuids)
        if not topic_uuids:
            return {"status": "error", "reason": "topic_uuid is required"}
        try:
            peer_addr = peer_addr.rstrip("/")
            adopted = []
            for topic_uuid in topic_uuids:
                tree_payload = self.fetch_subtree(peer_addr, topic_uuid)
                tree = self._decode_wire_subtree(tree_payload, peer_addr)
                # The grafted tree and the cached peer copy must be distinct
                # objects, or divergence checks between "our copy" and
                # "their copy" would compare a node against itself. Copy
                # before grafting: accept_topic_invitation re-parents `tree`
                # into our own container, so a copy taken afterwards would
                # hold our parent, not the peer's wire state. Decoding twice
                # (the previous approach) also re-verified every hash for
                # nothing.
                cached_copy = copy.deepcopy(tree)
                accepted = self.session.accept_topic_invitation(tree)
                if accepted.status != "ok":
                    return {"status": "error", "reason": accepted.reason}
                adopted.append(accepted.value)
                self.session.apply_peer_subtree(
                    peer_addr,
                    cached_copy,
                    tree_payload.get("parent_uuid"),
                )

            response = self.http.post_json(
                self._url(peer_addr, "/p2p/join"),
                {
                    "from_addr": self.session.address,
                    "topic_uuid": topic_uuids[0],
                    "topic_uuids": topic_uuids,
                    "topic_members": self.session.topic_members_by_topic(topic_uuids),
                },
                timeout=10,
            )
            if response.get("status") != "ok":
                return response
        except TransportHttpError as exc:
            return {
                "status": "error",
                "reason": str(exc),
                "remote_status": exc.status_code,
            }
        except Exception as exc:
            return {"status": "error", "reason": str(exc)}

        topic_members = self.session.topic_members_from_map(
            response.get("topic_members") or {}, topic_uuids,
        )
        all_members = set()
        for topic_uuid, members in topic_members.items():
            for member in members:
                all_members.add(member)
                if member != self.session.address:
                    self.session.add_peer(member, topic_uuid)
        if peer_addr != self.session.address:
            self.session.add_peer_topics(peer_addr, topic_uuids)
            for topic_uuid in topic_uuids:
                all_members.add(peer_addr)
                topic_members.setdefault(topic_uuid, set()).add(peer_addr)
        for topic_uuid, members in sorted(topic_members.items()):
            for member in sorted(members):
                if member in (self.session.address, peer_addr):
                    continue
                self.execute_effect(SessionEffect(
                    "pull_subtree",
                    member,
                    {"node_uuid": topic_uuid, "topic_uuid": topic_uuid},
                ))
        for topic_uuid in topic_uuids:
            self.execute_effects(self.session.sync_effects(topic_uuid))
        return {
            "status": "ok",
            "members": sorted(all_members),
            "topic_uuids": topic_uuids,
            "adopted_root_uuid": adopted[0] if adopted else None,
            "adopted_root_uuids": adopted,
            "topic_members": {
                topic_uuid: sorted(members)
                for topic_uuid, members in sorted(topic_members.items())
            },
        }

    def observe_topic(self, peer_addr: str, topic_uuid: str | None = None,
                      topic_uuids: list[str] | None = None) -> dict:
        # Read-only counterpart to join_discussion: caches the peer's
        # subtree (peer_perspectives only, never merged into our own tree)
        # and never calls /p2p/join, so the target never learns we exist
        # and never expects anything back from us - no membership, no
        # adopting, no ping/sync obligations either direction.
        topic_uuids = self._topic_uuids(topic_uuid, topic_uuids)
        if not topic_uuids:
            return {"status": "error", "reason": "topic_uuid is required"}
        peer_addr = peer_addr.rstrip("/")
        self.trace.event(
            "transport.observe_topic",
            peer_addr=peer_addr,
            topic_uuids=topic_uuids,
        )
        try:
            for uuid in topic_uuids:
                tree_payload = self.fetch_subtree(peer_addr, uuid)
                tree = self._decode_wire_subtree(tree_payload, peer_addr)
                self.session.apply_peer_subtree(
                    peer_addr,
                    tree,
                    tree_payload.get("parent_uuid"),
                )
                self.session.watch_topic(peer_addr, uuid)
        except TransportHttpError as exc:
            return {
                "status": "error",
                "reason": str(exc),
                "remote_status": exc.status_code,
            }
        except Exception as exc:
            return {"status": "error", "reason": str(exc)}
        return {"status": "ok", "topic_uuids": topic_uuids}

    def invite_to_discuss(self, peer_addr: str, topic_uuid: str | None = None,
                          topic_uuids: list[str] | None = None,
                          read_only: bool = False) -> dict:
        topic_uuids = self._topic_uuids(topic_uuid, topic_uuids)
        if not topic_uuids:
            return {"status": "error", "reason": "topic_uuid is required"}
        self.trace.event(
            "transport.invite_to_discuss",
            peer_addr=peer_addr,
            topic_uuids=topic_uuids,
            read_only=read_only,
        )
        # A read-only invite asks the invitee's own server to observe us
        # (cache-only, never becomes our peer) instead of fully joining -
        # same distinction as observe_topic vs join_discussion, just
        # initiated by us instead of them.
        target_path = "/api/observe_topic" if read_only else "/api/join_discussion"
        try:
            response = self.http.post_json(
                self._url(peer_addr, target_path),
                {
                    "address": self.session.address,
                    "topic_uuid": topic_uuids[0],
                    "topic_uuids": topic_uuids,
                },
                timeout=15,
            )
            self.trace.event(
                "transport.invite_to_discuss_ok",
                peer_addr=peer_addr,
                topic_uuids=topic_uuids,
                response_status=response.get("status"),
                response_topic_uuids=response.get("topic_uuids"),
            )
            return response
        except TransportHttpError as exc:
            self.trace.event(
                "transport.invite_to_discuss_failed",
                peer_addr=peer_addr,
                topic_uuids=topic_uuids,
                reason=str(exc),
                remote_status=exc.status_code,
            )
            return {
                "status": "error",
                "reason": str(exc),
                "remote_status": exc.status_code,
            }
        except Exception as exc:
            self.trace.event(
                "transport.invite_to_discuss_failed",
                peer_addr=peer_addr,
                topic_uuids=topic_uuids,
                reason=str(exc),
            )
            return {"status": "error", "reason": str(exc)}

    def leave_discussion(self) -> list[TransportDelivery]:
        result = self.session.leave()
        return self.execute_effects(result.effects)

    def disconnect(self) -> list[TransportDelivery]:
        result = self.session.disconnect()
        return self.execute_effects(result.effects)

    def leave_topic(self, topic_uuid: str) -> list[TransportDelivery]:
        result = self.session.leave_topic(topic_uuid)
        return self.execute_effects(result.effects)

    # Incoming P2P endpoints

    def p2p_sync_status(self, payload: dict) -> tuple[dict, int]:
        self.trace.event("transport.p2p_sync_status", payload=payload)
        result = self.session.handle_sync_status(payload)
        from_addr = payload.get("from_addr")
        incoming_summary = payload.get("summary") or {}
        # my_summary is deliberately recomputed *after* the pulls above run,
        # and replaces the one in result.value (computed before them).
        return self._handle_session_result(
            result,
            # `summary` in result.value is just an echo of what the peer
            # sent us - no caller reads it back, so it stays off the wire.
            merge_value=False,
            extra_fields={
                "my_summary": self.session.sync_summary(from_addr),
                "delivered_sync_hash": incoming_summary.get("sync_hash"),
            },
            partial_on_failure=True,
        )

    def p2p_join(self, payload: dict) -> tuple[dict, int]:
        self.trace.event("transport.p2p_join", payload=payload)
        return self._handle_session_result(self.session.handle_join(payload))

    def p2p_announce(self, payload: dict) -> tuple[dict, int]:
        self.trace.event("transport.p2p_announce", payload=payload)
        return self._handle_session_result(self.session.handle_announce(payload))

    def p2p_leave(self, payload: dict) -> tuple[dict, int]:
        self.trace.event("transport.p2p_leave", payload=payload)
        return self._handle_session_result(self.session.handle_leave(payload))

    def p2p_subtree(self, node_uuid: str) -> tuple[dict, int]:
        result = self.session.get_subtree(node_uuid)
        if result is None:
            self.trace.event(
                "transport.p2p_subtree_not_found",
                node_uuid=node_uuid,
            )
            return {"status": "error", "reason": "not found"}, 404
        self.trace.event(
            "transport.p2p_subtree_ok",
            node_uuid=node_uuid,
            parent_uuid=result.get("parent_uuid"),
            state_hash=(result.get("subtree") or {}).get("state_hash"),
        )
        return result, 200

    # Internals

    def _decode_wire_subtree(self, payload: dict, peer_addr: str | None) -> ProtocolNode:
        try:
            return protocol_node_from_envelope(payload)
        except UnsupportedProtocolVersion:
            raise
        except ValueError as exc:
            self.logger(
                "[transport] repairing invalid subtree from "
                f"{peer_addr or 'peer'}: {exc}"
            )
            return protocol_node_from_envelope(payload, repair_hashes=True)

    @staticmethod
    def _topic_uuids(topic_uuid: str | None,
                     topic_uuids: list[str] | None) -> list[str]:
        values = list(topic_uuids or [])
        if topic_uuid:
            values.append(topic_uuid)
        return sorted(str(value) for value in set(values) if value)

    @staticmethod
    def _is_not_found(error: Exception) -> bool:
        if isinstance(error, TransportHttpError):
            return error.status_code == 404
        return str(error).strip().lower() == "not found"

    def _handle_session_result(
            self, result: SessionResult,
            extra_fields: dict[str, Any] | None = None,
            merge_value: bool = True,
            partial_on_failure: bool = False) -> tuple[dict, int]:
        # The one incoming-endpoint shaper: run the session's effects,
        # collect any delivery failures, and shape the response. The three
        # options exist only for /p2p/sync_status, which reports "partial"
        # when some effect failed and answers with its own fields rather
        # than the session result's value.
        if result.status != "ok":
            return {"status": "error", "reason": result.reason}, 409
        deliveries = self.execute_effects(result.effects)
        failed = [delivery for delivery in deliveries if not delivery.ok]
        payload = {"status": "partial" if failed and partial_on_failure else "ok"}
        if merge_value:
            if isinstance(result.value, dict):
                payload.update(result.value)
            elif result.value is not None:
                payload["value"] = result.value
        if extra_fields:
            payload.update(extra_fields)
        if failed:
            payload["delivery_errors"] = [
                {
                    "effect_type": item.effect_type,
                    "target": item.target,
                    "reason": item.reason,
                }
                for item in failed
            ]
        self.trace.event(
            "transport.session_result",
            status=result.status,
            effect_count=len(result.effects),
            failed_count=len(failed),
            payload=payload,
        )
        return payload, 200

    @staticmethod
    def _url(peer_addr: str | None, path: str) -> str:
        if not peer_addr:
            raise ValueError("target address is required")
        return f"{peer_addr.rstrip('/')}{path}"

    @staticmethod
    def _default_logger(message: str) -> None:
        print(message, flush=True)
