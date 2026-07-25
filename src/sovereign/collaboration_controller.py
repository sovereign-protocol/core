"""Core HTTP API for session-wide collaboration and channel management."""

from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def build_routes(runtime) -> list[Route]:
    service = runtime.collaboration

    def response(result):
        if not result.ok:
            return JSONResponse(
                {"status": "error", "reason": result.reason},
                status_code=result.status_code,
            )
        if isinstance(result.value, dict):
            return JSONResponse(result.value)
        return JSONResponse({"status": "ok", "value": result.value})

    async def api_channels(request: Request):
        if request.method == "GET":
            return JSONResponse(service.channels_payload())
        values = await request.json()
        result = await asyncio.to_thread(service.configure_channel, values)
        if result.ok:
            runtime.notify_change("channel-configuration")
        return response(result)

    async def api_test_channel(request: Request):
        values = await request.json()
        return response(await asyncio.to_thread(service.test_channel, values))

    async def api_delete_channel(request: Request):
        values = await request.json()
        result = await asyncio.to_thread(
            service.delete_channel, values.get("channel_ref", ""),
        )
        if result.ok:
            runtime.notify_change("channel-configuration")
        return response(result)

    async def api_topic_sharing(request: Request):
        return response(await asyncio.to_thread(
            service.topic_sharing_payload,
            request.path_params["topic_uuid"],
        ))

    async def api_topic_channel(request: Request):
        values = await request.json()
        result = await asyncio.to_thread(
            service.set_topic_channel,
            request.path_params["topic_uuid"],
            values.get("channel_ref", ""),
            values.get("action") == "use",
        )
        if result.ok:
            runtime.notify_change("topic-channel")
        return response(result)

    async def api_invitation(request: Request):
        values = await request.json()
        return response(await asyncio.to_thread(
            service.compose_invitation,
            values.get("topic_uuid", ""),
            values.get("channel_ref", "http"),
        ))

    async def api_accept_invitation(request: Request):
        values = await request.json()
        result = await asyncio.to_thread(
            service.accept_invitation, values.get("token") or {},
        )
        if result.ok:
            runtime.notify_change("invitation")
        return response(result)

    return [
        Route("/api/core/channels", api_channels, methods=["GET", "POST"]),
        Route("/api/core/channels/test", api_test_channel, methods=["POST"]),
        Route("/api/core/channels/delete", api_delete_channel, methods=["POST"]),
        Route(
            "/api/core/topics/{topic_uuid}/sharing",
            api_topic_sharing,
        ),
        Route(
            "/api/core/topics/{topic_uuid}/channels",
            api_topic_channel,
            methods=["POST"],
        ),
        Route("/api/core/invitations", api_invitation, methods=["POST"]),
        Route(
            "/api/core/invitations/accept",
            api_accept_invitation,
            methods=["POST"],
        ),
    ]
