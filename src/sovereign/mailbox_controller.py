"""HTTP controller for the concrete mailbox channel and its named targets."""

from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def build_routes(channel, runtime) -> list[Route]:
    logic = channel.manager

    async def api_status(request: Request):
        return JSONResponse(await asyncio.to_thread(logic.status_payload))

    async def api_delete_topic(request: Request):
        data = await request.json()
        result = logic.delete_topic(data.get("topic_uuid", ""))
        if result.status != "ok":
            return JSONResponse(
                {"status": "error", "reason": result.reason}, status_code=400,
            )
        return JSONResponse({"status": "ok", "topic_uuid": result.value})

    async def api_blob_gc(request: Request):
        return JSONResponse(await asyncio.to_thread(logic.blob_gc_report))

    async def api_targets(request: Request):
        if request.method == "GET":
            return JSONResponse({"targets": logic.list_targets()})
        data = await request.json()
        target_id = str(data.get("target_id") or "").strip()
        operation = logic.update_target if target_id else logic.create_target
        args = (target_id, data, True) if target_id else (data, True)
        result = await asyncio.to_thread(operation, *args)
        if result.status != "ok":
            return JSONResponse(
                {"status": "error", "reason": result.reason}, status_code=409,
            )
        runtime.notify_change("mailbox-target")
        return JSONResponse({"status": "ok", "target_id": result.value})

    async def api_test_target(request: Request):
        data = await request.json()
        target_id = data.get("target_id", "")
        result = await asyncio.to_thread(logic.test_target, target_id)
        status = 200 if result.status == "ok" else 409
        connection = logic.connection_for_target(target_id)
        return JSONResponse({
            "status": result.status,
            "target_id": result.value,
            "reason": result.reason,
            "timing": connection.timing.status_payload() if connection else None,
        }, status_code=status)

    async def api_delete_target(request: Request):
        data = await request.json()
        result = logic.delete_target(data.get("target_id", ""))
        if result.status != "ok":
            return JSONResponse(
                {"status": "error", "reason": result.reason}, status_code=409,
            )
        runtime.notify_change("mailbox-target")
        return JSONResponse({"status": "ok", "target_id": result.value})

    async def api_assign_topic(request: Request):
        data = await request.json()
        result = logic.assign_topic_target(
            data.get("topic_uuid", ""), data.get("target_id") or None,
        )
        if result.status != "ok":
            return JSONResponse(
                {"status": "error", "reason": result.reason}, status_code=409,
            )
        runtime.notify_change("mailbox-target")
        return JSONResponse({"status": "ok", "target_id": result.value})

    prefix = "/api/channels/mailbox"
    return [
        Route(f"{prefix}/status", api_status),
        Route(f"{prefix}/topics/delete", api_delete_topic, methods=["POST"]),
        Route(f"{prefix}/blob-gc", api_blob_gc),
        Route(f"{prefix}/targets", api_targets, methods=["GET", "POST"]),
        Route(f"{prefix}/targets/test", api_test_target, methods=["POST"]),
        Route(f"{prefix}/targets/delete", api_delete_target, methods=["POST"]),
        Route(f"{prefix}/topics/assign", api_assign_topic, methods=["POST"]),
    ]
