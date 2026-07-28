"""HTTP surface: read the list, add a note."""

from __future__ import annotations

import asyncio

from sovereign import application_json_response, json_value
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def build_routes(logic, services) -> list[Route]:
    async def api_state(_request: Request):
        return JSONResponse(json_value(logic.state()))

    async def api_create_note(request: Request):
        data = await request.json()
        result = await asyncio.to_thread(logic.create_note, data.get("text", ""))
        return await application_json_response(
            services, result, change_kind="example-notes",
        )

    return [
        Route("/api/example-notes/state", api_state),
        Route("/api/example-notes/notes", api_create_note, methods=["POST"]),
    ]
