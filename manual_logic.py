"""
Manual app logic.

Functionality:
  Human-operated generic perspective editor for the new stack. It exposes
  atomic protocol operations through Session and leaves transport to runtime.

Offered API:
  create_logic(session, config)
  build_routes(logic, runtime, config)
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from session import Session, SessionResult


class ManualLogic:
    def __init__(self, session: Session, config: dict):
        self.session = session
        self.config = config

    def state(self) -> dict:
        return {
            "root": self.session.protocol.root.to_dict(),
            "network": self.session.get_network_info(),
            "peers": {
                addr: tree.to_dict() if tree else None
                for addr, tree in sorted(self.session.peer_perspectives.items())
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


def create_logic(session: Session, config: dict) -> ManualLogic:
    return ManualLogic(session, config)


def build_routes(logic: ManualLogic, runtime, config: dict) -> list[Route]:
    async def api_state(request: Request):
        logic.session.reconcile_integrations()
        return JSONResponse(logic.state())

    async def api_start_discussion(request: Request):
        data = await request.json()
        result = logic.start_discussion(data.get("topic_uuid"))
        return _json_result(runtime, result)

    async def api_invite(request: Request):
        try:
            data = await request.json()
            topic_uuid = data.get("topic_uuid")
            result = logic.start_discussion(topic_uuid)
            if result.status != "ok":
                return _json_result(runtime, result)
            invite = runtime.adapter.invite_to_discuss(
                data["address"].strip().rstrip("/"),
                topic_uuid,
            )
            if invite.get("status") == "ok":
                runtime.notify_change()
                return JSONResponse(invite)
            return JSONResponse(invite, status_code=409)
        except Exception as exc:
            return JSONResponse(
                {"status": "error", "reason": str(exc)},
                status_code=500,
            )

    async def api_create_child(request: Request):
        data = await request.json()
        result = logic.create_child(
            data["parent_uuid"],
            _object(data.get("data")),
            _weights(data.get("weights")),
        )
        return _json_result(runtime, result)

    async def api_modify(request: Request):
        data = await request.json()
        result = logic.modify(
            data["node_uuid"],
            _object(data.get("data")),
            _weights(data.get("weights")),
        )
        return _json_result(runtime, result)

    async def api_delete(request: Request):
        data = await request.json()
        result = logic.delete(data["node_uuid"])
        return _json_result(runtime, result)

    async def api_copy(request: Request):
        data = await request.json()
        result = logic.copy(data["source_uuid"], data["destination_uuid"])
        return _json_result(runtime, result)

    async def api_move(request: Request):
        data = await request.json()
        result = logic.move(data["source_uuid"], data["destination_uuid"])
        return _json_result(runtime, result)

    return [
        Route("/api/manual/state", api_state),
        Route("/api/manual/start_discussion", api_start_discussion,
              methods=["POST"]),
        Route("/api/manual/invite", api_invite, methods=["POST"]),
        Route("/api/manual/create_child", api_create_child, methods=["POST"]),
        Route("/api/manual/modify", api_modify, methods=["POST"]),
        Route("/api/manual/delete", api_delete, methods=["POST"]),
        Route("/api/manual/copy", api_copy, methods=["POST"]),
        Route("/api/manual/move", api_move, methods=["POST"]),
    ]


def _json_result(runtime, result: SessionResult) -> JSONResponse:
    if result.status != "ok":
        return JSONResponse(
            {"status": "error", "reason": result.reason},
            status_code=409,
        )
    deliveries = runtime.adapter.execute_effects(result.effects)
    runtime.notify_change()
    payload: dict[str, Any] = {"status": "ok"}
    if hasattr(result.value, "to_dict"):
        payload["value"] = result.value.to_dict()
    elif result.value is not None:
        payload["value"] = result.value
    errors = [delivery for delivery in deliveries if not delivery.ok]
    if errors:
        payload["delivery_errors"] = [
            {
                "effect_type": item.effect_type,
                "target": item.target,
                "reason": item.reason,
            }
            for item in errors
        ]
    return JSONResponse(payload)


def _object(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def _weights(value: Any) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("expected weights object")
    return {str(key): float(item) for key, item in value.items()}
