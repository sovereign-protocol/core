"""HTTP surface: read the list, add a note."""

from __future__ import annotations

import asyncio

from sovereign import application_result_view, json_value
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def build_routes(logic, services, config: dict) -> list[Route]:
    async def api_state(_request: Request):
        return JSONResponse(json_value(logic.state()))

    async def api_create_note(request: Request):
        data = await request.json()
        result = await asyncio.to_thread(logic.create_note, data.get("text", ""))
        deliveries = []
        if result.status == "ok":
            # Effects reach peers through the host, which owns transport. The
            # application never touches a channel itself.
            deliveries = await asyncio.to_thread(
                services.deliver_effects, result.effects,
            )
            services.notify_change("example-notes")
        view = application_result_view(result, deliveries)
        return JSONResponse(view.payload, status_code=200 if view.ok else 409)

    return [
        Route("/api/example-notes/state", api_state),
        Route("/api/example-notes/notes", api_create_note, methods=["POST"]),
    ]
