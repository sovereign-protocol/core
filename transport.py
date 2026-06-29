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
  invite_to_discuss(peer_addr, topic_uuid)
  leave_discussion()
  p2p_ping(payload)
  p2p_join(payload)
  p2p_announce(payload)
  p2p_leave(payload)
  p2p_subtree(node_uuid)

Used API:
  session.Session and session.SessionEffect.
  protocol.PRSPNode only for checked wire decoding.

HTTP contract:
  POST /p2p/ping
  POST /p2p/join
  POST /p2p/announce
  POST /p2p/leave
  GET  /p2p/subtree/{uuid}
  POST /api/join_discussion
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from protocol import PRSPNode
from session import Session, SessionEffect, SessionResult


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
                 logger=None):
        self.session = session
        self.http = http_client or RequestsJsonHttpClient()
        self.logger = logger or self._default_logger

    # Outbound effects

    def execute_effects(
            self, effects: list[SessionEffect]) -> list[TransportDelivery]:
        return [self.execute_effect(effect) for effect in effects]

    def execute_effect(self, effect: SessionEffect) -> TransportDelivery:
        try:
            if effect.type == "send_ping":
                return self._send_ping(effect)
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
            self.logger(f"[transport] {effect.type} failed: {exc}")
            return TransportDelivery(
                False, effect.type, effect.target, str(exc)
            )

    def _send_ping(self, effect: SessionEffect) -> TransportDelivery:
        response = self.http.post_json(
            self._url(effect.target, "/p2p/ping"),
            effect.payload,
            timeout=3,
        )
        return TransportDelivery(True, effect.type, effect.target,
                                 response=response)

    def _pull_subtree_effect(self, effect: SessionEffect) -> TransportDelivery:
        node_uuid = effect.payload["node_uuid"]
        payload = self.fetch_subtree(effect.target, node_uuid)
        subtree = PRSPNode.from_dict(payload["subtree"])
        self.session.apply_peer_subtree(
            effect.target,
            subtree,
            payload.get("parent_uuid"),
        )
        return TransportDelivery(True, effect.type, effect.target,
                                 response=payload)

    def _announce_peer(self, effect: SessionEffect) -> TransportDelivery:
        targets = effect.payload.get("new_addrs")
        if targets is None:
            targets = [effect.payload.get("new_addr")]
        responses = []
        for new_addr in targets:
            if not new_addr:
                continue
            responses.append(self.http.post_json(
                self._url(effect.target, "/p2p/announce"),
                {
                    "new_addr": new_addr,
                    "topic_uuid": effect.payload["topic_uuid"],
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
        return self.http.get_json(
            self._url(peer_addr, f"/p2p/subtree/{node_uuid}"),
            timeout=5,
        )

    def join_discussion(self, peer_addr: str, topic_uuid: str) -> dict:
        if not topic_uuid:
            return {"status": "error", "reason": "topic_uuid is required"}
        try:
            peer_addr = peer_addr.rstrip("/")
            tree_payload = self.fetch_subtree(peer_addr, topic_uuid)
            tree = PRSPNode.from_dict(tree_payload["subtree"])
            accepted = self.session.accept_topic_invitation(tree)
            if accepted.status != "ok":
                return {"status": "error", "reason": accepted.reason}
            self.session.apply_peer_subtree(
                peer_addr,
                PRSPNode.from_dict(tree_payload["subtree"]),
                tree_payload.get("parent_uuid"),
            )

            response = self.http.post_json(
                self._url(peer_addr, "/p2p/join"),
                {
                    "from_addr": self.session.address,
                    "topic_uuid": topic_uuid,
                    "known_members": sorted(self.session.members),
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

        for member in response.get("members", []):
            self.session.add_peer(member, topic_uuid)
        self.session.add_peer(peer_addr, topic_uuid)
        self.execute_effects(self.session._sync_effects(topic_uuid))
        return {
            "status": "ok",
            "members": response.get("members", []),
            "adopted_root_uuid": accepted.value,
        }

    def invite_to_discuss(self, peer_addr: str, topic_uuid: str) -> dict:
        if not topic_uuid:
            return {"status": "error", "reason": "topic_uuid is required"}
        try:
            return self.http.post_json(
                self._url(peer_addr, "/api/join_discussion"),
                {"address": self.session.address, "topic_uuid": topic_uuid},
                timeout=15,
            )
        except TransportHttpError as exc:
            return {
                "status": "error",
                "reason": str(exc),
                "remote_status": exc.status_code,
            }
        except Exception as exc:
            return {"status": "error", "reason": str(exc)}

    def leave_discussion(self) -> list[TransportDelivery]:
        result = self.session.leave()
        return self.execute_effects(result.effects)

    # Incoming P2P endpoints

    def p2p_ping(self, payload: dict) -> tuple[dict, int]:
        return self._handle_session_result(self.session.handle_ping(payload))

    def p2p_join(self, payload: dict) -> tuple[dict, int]:
        return self._handle_session_result(self.session.handle_join(payload))

    def p2p_announce(self, payload: dict) -> tuple[dict, int]:
        return self._handle_session_result(self.session.handle_announce(payload))

    def p2p_leave(self, payload: dict) -> tuple[dict, int]:
        return self._handle_session_result(self.session.handle_leave(payload))

    def p2p_subtree(self, node_uuid: str) -> tuple[dict, int]:
        result = self.session.get_subtree(node_uuid)
        if result is None:
            return {"status": "error", "reason": "not found"}, 404
        return result, 200

    # Internals

    def _handle_session_result(
            self, result: SessionResult) -> tuple[dict, int]:
        if result.status != "ok":
            return {"status": "error", "reason": result.reason}, 409
        deliveries = self.execute_effects(result.effects)
        failed = [delivery for delivery in deliveries if not delivery.ok]
        payload = {"status": "ok"}
        if isinstance(result.value, dict):
            payload.update(result.value)
        elif result.value is not None:
            payload["value"] = result.value
        if failed:
            payload["delivery_errors"] = [
                {
                    "effect_type": item.effect_type,
                    "target": item.target,
                    "reason": item.reason,
                }
                for item in failed
            ]
        return payload, 200

    @staticmethod
    def _url(peer_addr: str | None, path: str) -> str:
        if not peer_addr:
            raise ValueError("target address is required")
        return f"{peer_addr.rstrip('/')}{path}"

    @staticmethod
    def _default_logger(message: str) -> None:
        print(message, flush=True)
