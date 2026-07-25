"""Starlette controller for Core's non-stable Protocol Explorer."""

from __future__ import annotations

import asyncio
from typing import Any

from .application import application_result_view, json_value
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def build_routes(logic, runtime, config: dict) -> list[Route]:
    async def api_state(request: Request):
        return JSONResponse(json_value(logic.state()))

    async def api_start_discussion(request: Request):
        data = await request.json()
        return await _json_result(
            runtime, logic.start_discussion(data.get("topic_uuid")),
        )

    async def api_create_child(request: Request):
        try:
            data = await request.json()
            result = logic.create_child(
                data["parent_uuid"],
                _object(data.get("data")),
                _weights(data.get("weights")),
            )
            return await _json_result(runtime, result)
        except Exception as exc:
            return _error_response(exc)

    async def api_modify(request: Request):
        try:
            data = await request.json()
            result = logic.modify(
                data["node_uuid"],
                _object(data.get("data")),
                _weights(data.get("weights")),
            )
            return await _json_result(runtime, result)
        except Exception as exc:
            return _error_response(exc)

    async def api_delete(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.delete(data["node_uuid"]))

    async def api_copy(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.copy(
            data["source_uuid"], data["destination_uuid"],
        ))

    async def api_move(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.move(
            data["source_uuid"], data["destination_uuid"],
        ))

    async def api_accept_peer_node(request: Request):
        data = await request.json()
        return await _json_result(runtime, logic.accept_peer_node(
            data["source_addr"],
            data["node_uuid"],
            bool(data.get("adopt_absence")),
        ))

    return [
        Route("/api/protocol-explorer/state", api_state),
        Route("/api/protocol-explorer/start_discussion", api_start_discussion,
              methods=["POST"]),
        Route("/api/protocol-explorer/create_child", api_create_child,
              methods=["POST"]),
        Route("/api/protocol-explorer/modify", api_modify, methods=["POST"]),
        Route("/api/protocol-explorer/delete", api_delete, methods=["POST"]),
        Route("/api/protocol-explorer/copy", api_copy, methods=["POST"]),
        Route("/api/protocol-explorer/move", api_move, methods=["POST"]),
        Route("/api/protocol-explorer/accept_peer_node", api_accept_peer_node,
              methods=["POST"]),
    ]


async def _json_result(runtime, result) -> JSONResponse:
    deliveries = []
    if result.status == "ok":
        deliveries = await asyncio.to_thread(
            runtime.deliver_effects, result.effects,
        )
        runtime.notify_change()
    view = application_result_view(result, deliveries)
    return JSONResponse(view.payload, status_code=200 if view.ok else 409)


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
    weights = {}
    for key, item in value.items():
        if item is None:
            raise ValueError(f"weight '{key}' must be a number")
        try:
            weights[str(key)] = float(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"weight '{key}' must be a number") from exc
    return weights


def _error_response(error: Exception) -> JSONResponse:
    return JSONResponse(
        {"status": "error", "reason": str(error)}, status_code=400,
    )
