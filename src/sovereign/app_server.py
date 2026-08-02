#!/usr/bin/env python3
"""
Application server component.

Functionality:
  Bind the Core HTTP surface, explicit applications, Session, and transport
  services. Persistence mechanics live in persistence.py.
  Protocol meaning stays in protocol.py/session.py. Browsers learn about
  changes by polling - there is no push channel.

Offered API:
  parse_target(target)
  load_config(config_path=None, app_name=None)
  default_storage_file(config, port)
  save_session_to_file(session, path)
  load_session_from_file(session, path)
  create_runtime(port, config)
  build_app(runtime)
  main(argv=None)
  Channel composition, acceptance, and exclusivity are owned by ChannelManager.
Expected app module API:
  APPLICATION_MANIFEST: ApplicationManifest
  create_application(services: ApplicationServices) -> ApplicationInstance

The server keeps HTTP and browser concerns here. It does not implement tree
operations directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .application import ApplicationServices
from .channel import ChannelManager
from .collaboration import CollaborationService
from .host import ApplicationHost
from .mailbox_channel import MailboxChannel
from .collaboration_controller import build_routes as build_collaboration_routes
from .profile import CoreProfileService
from .blob_store import (
    BlobStore, SAFE_IMAGE_MIMES, blob_hex, canonical_attachments,
    is_valid_image, referenced_blob_ids,
)
from .session import Session, SessionEffect
from .trace_log import TraceLogger
from .relay_logic import RelayManager
from .persistence import (
    load_session_from_file, save_session_to_file,
)


DEFAULT_CONFIG = {
    "app_module": None,
    "ui_file": None,
    "css_file": None,
    "storage_file": None,
    "debug": True,
    "peer_sync_interval_seconds": 2,
    "trace_log_file": None,
    "trace_level": "events",
    # bind_host is which network interface uvicorn listens on for the local
    # browser UI. advertise_host survives only because the session address
    # is built from it; no peer is ever told to connect to it, because no
    # peer connects to anything - a channel publishes and polls.
    "advertise_host": "127.0.0.1",
    "bind_host": "127.0.0.1",
    "applications": None,
    "primary_application_id": None,
}
CORE_APPLICATION_ALIASES = {
    "manual": {
        "app_module": "sovereign.protocol_explorer_application",
        "application_id": "protocol-explorer",
        "asset_package": "sovereign.assets",
        "ui_file": "manual.html",
        "css_file": "manual.css",
    },
}


@dataclass
class AppRuntime:
    port: int
    address: str
    config: dict[str, Any]
    session: Session
    blob_store: BlobStore
    profile: CoreProfileService
    relay_manager: RelayManager
    channel_manager: ChannelManager
    collaboration: CollaborationService
    mailbox_channel: MailboxChannel
    host: ApplicationHost | None = None
    logic: Any = None
    channel_wakeup: asyncio.Event | None = None
    channel_loop: asyncio.AbstractEventLoop | None = None

    def persist(self) -> None:
        storage_file = self.config.get("storage_file")
        if storage_file:
            save_session_to_file(self.session, storage_file)
        # Grace time protects a new upload until its reference is committed.
        # Running after persisted mutations completes local GC automatically.
        self.collect_local_blobs()

    def _wake_channels(self, kind: str) -> None:
        # Local edits should publish immediately instead of waiting for the
        # next polling timeout. Channel-originated persistence must not wake
        # the loop recursively; that cycle already publishes its response.
        if kind not in ("channel", "network") and self.channel_wakeup and self.channel_loop:
            if self.channel_loop.is_running():
                self.channel_loop.call_soon_threadsafe(self.channel_wakeup.set)

    def persist_confirmed_change(self, kind: str = "changed") -> None:
        """Persist a revision that Session already confirmed atomically.

        Application mutations advance the visible revision while holding
        Session.lock, then call this after releasing it. Persistence snapshots
        Session directly; channel configuration now writes through Session's
        locked component-metadata API and needs no cross-layer guard.
        """
        self.persist()
        self._wake_channels(kind)

    def notify_change(self, kind: str = "changed") -> None:
        # Name kept for its many call sites; `kind` is retained as a
        # readable marker of what triggered the save. Browsers learn about
        # changes by polling (every UI does its own interval) - there is no
        # push channel, by design.
        self.persist()
        self.session.advance_view_revision()
        self._wake_channels(kind)

    def current_revision(self) -> int:
        return self.session.current_view_revision()

    def application_summaries(self) -> list[dict]:
        """Active applications as named by this host's shared header."""
        summaries = self.host.application_summaries() if self.host else []
        configured_title = self.config.get("header_title")
        if not isinstance(configured_title, str) or not configured_title.strip():
            return summaries
        title = configured_title.strip()
        return [
            {**summary, "display_name": title}
            if summary.get("primary") else summary
            for summary in summaries
        ]

    def deliver_effects(self, effects) -> list[Any]:
        """Execute application effects through the Core-owned channel service.

        Effects can enter channel management, so callers must already have
        released Session and relay I/O locks - see the lock order in
        DESIGN_LOCKING_AND_COMPOSITE_READS.md.
        """
        return self.collaboration.execute_effects(effects)

    def collect_local_blobs(self) -> list[str]:
        with self.session.lock:
            referenced = referenced_blob_ids(self.session.protocol.root)
            for tree in self.session.peer_perspectives_for_topic().values():
                referenced.update(referenced_blob_ids(tree))
        # Filesystem enumeration and deletion can be slow on Windows and
        # never belongs inside the Session transaction. The grace period
        # protects a blob uploaded after this detached reference snapshot;
        # the next sweep sees any reference committed meanwhile.
        return self.blob_store.collect(referenced)


def parse_target(target: str) -> tuple[int, str | None]:
    if ":" not in target:
        return int(target), None
    port_text, app_name = target.split(":", 1)
    if not app_name:
        raise ValueError("app name is required after ':'")
    return int(port_text), app_name


def app_default_config(
    app_name: str, application_aliases: dict[str, dict] | None = None,
) -> dict:
    aliases = dict(CORE_APPLICATION_ALIASES)
    aliases.update(application_aliases or {})
    known = aliases.get(app_name)
    if known:
        config = dict(known)
        config["applications"] = list(
            known.get("applications") or [{"module": known["app_module"]}]
        )
        config["primary_application_id"] = known["application_id"]
        return config
    return {
        "app_module": f"{app_name}_logic",
        "applications": [{"module": f"{app_name}_logic"}],
        "primary_application_id": app_name,
    }


def load_config(config_path: str | None = None,
                app_name: str | None = None,
                application_aliases: dict[str, dict] | None = None) -> dict:
    config = dict(DEFAULT_CONFIG)
    if app_name:
        config.update(app_default_config(app_name, application_aliases))
    if not config_path and app_name:
        app_config_path = Path.cwd() / f"{app_name}_config.json"
        config_path = str(app_config_path) if app_config_path.exists() else None
    if config_path:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {config_path}")
        with path.open(encoding="utf-8") as f:
            overrides = json.load(f)
        config.update(overrides)
        if "applications" not in overrides and "app_module" in overrides:
            config["applications"] = [{"module": overrides["app_module"]}]
            config["primary_application_id"] = overrides.get(
                "primary_application_id",
            )
    config["applications"] = list(config.get("applications") or [])
    return config


def default_storage_file(config: dict, port: int) -> str:
    app_name = str(config.get("app_module") or "app").replace(".", "_")
    return str(Path.cwd() / "data" / f"{app_name}_{port}.json")


def create_runtime(port: int, config: dict) -> AppRuntime:
    config = dict(config)
    if config.get("applications") is None:
        module_name = config.get("app_module")
        config["applications"] = ([{"module": module_name}] if module_name else [])
    if config.get("storage_file") is None:
        config["storage_file"] = default_storage_file(config, port)
    advertise_host = config.get("advertise_host") or "127.0.0.1"
    address = f"http://{advertise_host}:{port}"
    trace = TraceLogger.from_config(config, port, address)
    session = Session(address, trace=trace)
    storage_file = config["storage_file"]
    loaded = load_session_from_file(session, storage_file)
    if os.path.exists(storage_file) and not loaded:
        raise RuntimeError(
            "refusing to start with incompatible session data: "
            f"{storage_file}"
        )
    blob_root = config.get("blob_store_dir")
    if not blob_root:
        storage_path = Path(config["storage_file"])
        blob_root = storage_path.with_name(f"{storage_path.stem}_blobs")
    blob_store = BlobStore(
        blob_root,
        grace_seconds=float(config.get("blob_gc_grace_seconds", 60)),
    )
    relay_manager = RelayManager(session, config, blob_store=blob_store)
    channel_manager = ChannelManager(session)
    mailbox_channel = MailboxChannel(relay_manager)
    channel_manager.register(mailbox_channel)
    collaboration = CollaborationService(session, channel_manager)
    runtime = AppRuntime(
        port=port,
        address=address,
        config=config,
        session=session,
        blob_store=blob_store,
        profile=CoreProfileService(session),
        relay_manager=relay_manager,
        channel_manager=channel_manager,
        collaboration=collaboration,
        mailbox_channel=mailbox_channel,
    )
    services = ApplicationServices(
        session=session,
        collaboration=collaboration.application_view,
        deliver_effects=collaboration.execute_effects,
        blob_store=blob_store,
        trace=trace,
        notify_change=runtime.notify_change,
        collect_local_blobs=runtime.collect_local_blobs,
        current_revision=runtime.current_revision,
        persist_confirmed_change=runtime.persist_confirmed_change,
    )
    runtime.host = ApplicationHost(
        services,
        config.get("applications") or [],
        config.get("primary_application_id"),
    )
    # Transitional primary-app alias. Application discovery, lifecycle and
    # hooks are owned exclusively by ApplicationHost.
    runtime.logic = runtime.host.primary_logic
    return runtime


def build_core_routes(runtime: AppRuntime) -> list[Route]:
    async def serve_ui(request: Request):
        return HTMLResponse(runtime.host.read_primary_asset("ui") if runtime.host else "")

    async def serve_css(request: Request):
        return Response(runtime.host.read_primary_asset("css") if runtime.host else "",
                        media_type="text/css")

    async def serve_shared_css(request: Request):
        return Response(files("sovereign.assets").joinpath("shared.css").read_text(encoding="utf-8"),
                        media_type="text/css")

    async def serve_shared_js(request: Request):
        return Response(files("sovereign.assets").joinpath("shared.js").read_text(encoding="utf-8"),
                        media_type="application/javascript")

    async def serve_shared_api_js(request: Request):
        return Response(
            files("sovereign.assets").joinpath("shared-api.js").read_text(
                encoding="utf-8",
            ),
            media_type="application/javascript",
        )

    async def serve_shared_session_js(request: Request):
        return Response(
            files("sovereign.assets").joinpath("shared-session.js").read_text(
                encoding="utf-8",
            ),
            media_type="application/javascript",
        )

    async def api_protocol(request: Request):
        # No pull here any more. What a peer is publishing arrives on the
        # channel's own poll; this route reports what has already arrived.
        await drain_peer_update_hook(runtime)
        return JSONResponse(runtime.session.export_protocol_root())

    async def api_network(request: Request):
        return JSONResponse(await asyncio.to_thread(
            runtime.channel_manager.network_info,
            include_channel_status=True,
        ))

    async def profile_result(result) -> JSONResponse:
        if result.status != "ok":
            return JSONResponse(
                {"status": "error", "reason": result.reason}, status_code=409,
            )
        await asyncio.to_thread(
            runtime.collaboration.execute_effects, result.effects,
        )
        # The edit is saved and the next poll publishes it. Nothing is
        # delivered on this request, so nothing about delivery is reported.
        runtime.notify_change("profile")
        return JSONResponse({"status": "ok", **runtime.profile.view()})

    async def api_core_applications(request: Request):
        return JSONResponse({
            "status": "ok",
            "applications": runtime.application_summaries(),
        })

    async def api_core_revision(request: Request):
        return JSONResponse({"revision": runtime.current_revision()})

    async def api_core_mutation_status(request: Request):
        mutation_id = request.path_params["mutation_id"]
        result = runtime.session.mutation_result(mutation_id)
        return JSONResponse(result or {
            "status": "unknown",
            "mutation_id": mutation_id,
            "revision": runtime.current_revision(),
        })

    async def api_core_profile(request: Request):
        if request.method == "GET":
            return JSONResponse({"status": "ok", **runtime.profile.view()})
        data = await request.json()
        return await profile_result(runtime.profile.set_profile(
            data.get("name", data.get("display_name", "")),
            data.get("picture") if "picture" in data else None,
        ))

    async def api_core_profile_avatar(request: Request):
        data = await request.json()
        reference = None if data.get("remove") else data.get("attachment")
        if reference is not None:
            normalized = canonical_attachments([reference])
            if not normalized:
                return JSONResponse(
                    {"status": "error", "reason": "invalid attachment"},
                    status_code=400,
                )
            reference = normalized[0]
            blob_data = runtime.blob_store.read_blob(reference["blob_id"])
            if blob_data is None:
                return JSONResponse(
                    {"status": "error", "reason": "uploaded blob not found"},
                    status_code=409,
                )
            if not is_valid_image(blob_data, reference["mime"]):
                return JSONResponse(
                    {"status": "error", "reason": "invalid image data"},
                    status_code=400,
                )
            reference["size"] = len(blob_data)
        response = await profile_result(runtime.profile.set_avatar(reference))
        if response.status_code < 400:
            runtime.collect_local_blobs()
        return response

    async def api_blob_upload(request: Request):
        limit = int(runtime.config.get("max_blob_size_bytes", 20 * 1024 * 1024))
        content_length = int(request.headers.get("content-length") or 0)
        if content_length > limit:
            return JSONResponse(
                {"status": "error", "reason": "blob is too large"}, status_code=413,
            )
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                return JSONResponse(
                    {"status": "error", "reason": "blob is too large"}, status_code=413,
                )
            chunks.append(chunk)
        if not size:
            return JSONResponse(
                {"status": "error", "reason": "blob is empty"}, status_code=400,
            )
        payload = b"".join(chunks)
        blob_id = await asyncio.to_thread(runtime.blob_store.write_blob, payload)
        return JSONResponse({
            "status": "ok",
            "blob_id": blob_id,
            "size": size,
            "mime": (request.headers.get("content-type") or "application/octet-stream")
                .split(";", 1)[0].strip().lower(),
        })

    def blob_references_and_routes(
        blob_id: str,
    ) -> tuple[list[dict], list[tuple[str, str]]]:
        references = []
        routes = []
        with runtime.session.lock:
            roots = [
                (None, runtime.session.protocol.root),
                *runtime.session.peer_perspectives_for_topic().items(),
            ]
            for peer_addr, root in roots:
                stack = [root]
                while stack:
                    node = stack.pop()
                    for item in canonical_attachments(node.data.get("attachments")):
                        if item["blob_id"] == blob_id:
                            references.append(item)
                            if peer_addr:
                                for topic_uuid in (
                                    runtime.session.peer_topics_for_node(
                                        peer_addr, node.uuid,
                                    )
                                ):
                                    if (
                                        runtime.session.peer_channel_for_topic(
                                            peer_addr, topic_uuid,
                                        )
                                    ):
                                        routes.append((peer_addr, topic_uuid))
                    stack.extend(node.children)
        return references, sorted(set(routes))

    def resolve_blob(blob_id: str, allow_peer_fetch: bool) -> bytes | None:
        local = runtime.blob_store.read_blob(blob_id)
        if local is not None:
            return local
        if not allow_peer_fetch:
            return None
        _references, routes = blob_references_and_routes(blob_id)
        for peer_addr, topic_uuid in routes:
            fetched = runtime.channel_manager.read_topic_blob(
                blob_id, peer_addr, topic_uuid,
            )
            if (
                fetched is not None
                and runtime.blob_store.write_blob(fetched) == blob_id
            ):
                return fetched
        return None

    async def api_blob_get(request: Request):
        blob_id = request.path_params["blob_id"]
        try:
            blob_hex(blob_id)
        except ValueError:
            return JSONResponse(
                {"status": "error", "reason": "invalid blob id"}, status_code=400,
            )
        data = await asyncio.to_thread(
            resolve_blob,
            blob_id,
            request.headers.get("x-sovereign-blob-hop") != "1",
        )
        if data is None:
            return JSONResponse(
                {"status": "error", "reason": "blob not found"}, status_code=404,
            )
        references, _routes = blob_references_and_routes(blob_id)
        reference = references[0] if references else {}
        mime = str(reference.get("mime") or "application/octet-stream").lower()
        if mime not in SAFE_IMAGE_MIMES or not is_valid_image(data, mime):
            mime = "application/octet-stream"
        return Response(data, media_type=mime, headers={
            "Content-Disposition": "inline" if mime in SAFE_IMAGE_MIMES else "attachment",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "public, max-age=31536000, immutable",
        })

    async def api_blob_gc(request: Request):
        removed = await asyncio.to_thread(runtime.collect_local_blobs)
        return JSONResponse({"status": "ok", "removed": removed})

    return [
        Route("/", serve_ui),
        Route("/styles.css", serve_css),
        Route("/shared.css", serve_shared_css),
        Route("/shared-api.js", serve_shared_api_js),
        Route("/shared-session.js", serve_shared_session_js),
        Route("/shared.js", serve_shared_js),
        Route("/api/protocol", api_protocol),
        Route("/api/network", api_network),
        Route("/api/core/applications", api_core_applications),
        Route("/api/core/revision", api_core_revision),
        Route("/api/core/mutations/{mutation_id}", api_core_mutation_status),
        Route("/api/core/profile", api_core_profile, methods=["GET", "POST"]),
        Route(
            "/api/core/profile/avatar",
            api_core_profile_avatar,
            methods=["POST"],
        ),
        Route("/api/blob", api_blob_upload, methods=["POST"]),
        Route("/api/blob/gc", api_blob_gc, methods=["POST"]),
        Route("/api/blob/{blob_id}", api_blob_get),
    ]


async def run_peer_update_hook(runtime: AppRuntime) -> bool:
    host = runtime.host
    if not host:
        return False
    outcome = await asyncio.to_thread(host.notify_peer_update)
    if outcome.effects:
        await asyncio.to_thread(runtime.deliver_effects, outcome.effects)
        runtime.session.advance_view_revision()
    return outcome.changed


async def drain_peer_update_hook(runtime: AppRuntime, passes: int = 4) -> None:
    for _ in range(passes):
        changed = await run_peer_update_hook(runtime)
        if not changed:
            break


def build_app(runtime: AppRuntime) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette):
        channel_task = asyncio.create_task(channel_poll_loop(runtime))
        blob_gc_task = asyncio.create_task(local_blob_gc_loop(runtime))
        try:
            yield
        finally:
            channel_task.cancel()
            blob_gc_task.cancel()
            for task in (channel_task, blob_gc_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if runtime.host:
                runtime.host.close()
            runtime.channel_manager.close()
            runtime.persist()

    core_routes = build_core_routes(runtime)
    collaboration_routes = build_collaboration_routes(runtime)
    application_routes = runtime.host.controller_routes() if runtime.host else []
    app = Starlette(
        debug=bool(runtime.config.get("debug", True)),
        routes=core_routes + collaboration_routes + application_routes,
        lifespan=lifespan,
    )
    if runtime.host:
        runtime.host.bind_starlette(app, [*core_routes, *collaboration_routes])
    return app


async def local_blob_gc_loop(runtime: AppRuntime) -> None:
    grace = float(runtime.config.get("blob_gc_grace_seconds", 60))
    interval = float(runtime.config.get("blob_gc_interval_seconds", max(60, grace)))
    while True:
        await asyncio.sleep(max(10.0, interval))
        await asyncio.to_thread(runtime.collect_local_blobs)


def _advance_poll_deadline(scheduled_for: float | None,
                           cycle_started: float,
                           interval_seconds: float,
                           now: float) -> float:
    """Advance a fixed polling cadence without idling for a whole slot.

    Rounding a missed deadline up to the next whole slot turned a cycle that
    overran its interval by a fraction into one that ran at half the rate: a
    client whose cycle took 3.33s against a 3s interval polled every 6.00s,
    measured, and every propagation through it paid that. There is no gradual
    degradation to observe on the way there - one millisecond over the
    interval doubles the period.

    So a deadline already in the past becomes now: the cadence still cannot
    run faster than the interval, but work that overruns it is followed by
    the next cycle rather than by a wait for the remainder of a slot nobody
    is keeping time with.
    """
    interval = max(0.05, float(interval_seconds))
    deadline = (
        scheduled_for if scheduled_for is not None else cycle_started
    ) + interval
    return max(deadline, now)


async def channel_poll_tick(runtime: AppRuntime, due_only: bool = False) -> bool:
    """Schedule one cycle on every due endpoint.

    Transport ordering, response publication, timing calibration, and
    diagnostics belong to ``PollingEndpoint.poll_once``. The host supplies
    only the application-reconciliation callback that must run after remote
    state is applied and before an endpoint publishes its response.
    """
    endpoints = runtime.channel_manager.polling_endpoints()
    now = time.monotonic()
    next_due = runtime.config.setdefault("_channel_next_due", {})
    due_endpoints = []
    for endpoint in endpoints:
        connection_key = id(endpoint)
        scheduled_for = next_due.get(connection_key)
        was_due = scheduled_for is None or now >= scheduled_for
        if due_only and not was_due:
            continue
        if not endpoint.has_active_relationship():
            continue
        due_endpoints.append((endpoint, scheduled_for, was_due))

    view_confirmed = threading.Event()
    deferred_effects: dict[tuple, Any] = {}

    def effect_key(effect) -> tuple:
        return (
            str(getattr(effect, "type", "")),
            str(getattr(effect, "target", "") or ""),
            str(getattr(effect, "channel_kind", "") or ""),
            json.dumps(
                getattr(effect, "payload", {}) or {},
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
        )

    def after_apply() -> None:
        # Every endpoint in this tick shares one closure, so the drain loop
        # and deferred_effects are reached from several poll threads. What
        # serializes them is Session.lock, which poll_and_apply holds across
        # this call - that is also what makes the reconciled state and its
        # visible revision observable together. Asserting it is cheaper than
        # a second lock, and states the contract a caller has to honour.
        runtime.session.lock.assert_owned()
        host = runtime.host
        if not host:
            runtime.session.advance_view_revision()
            view_confirmed.set()
            return
        for _ in range(4):
            outcome = host.notify_peer_update()
            for effect in outcome.effects:
                deferred_effects.setdefault(effect_key(effect), effect)
            if not outcome.changed:
                break
        runtime.session.advance_view_revision()
        view_confirmed.set()

    async def run_endpoint(endpoint, scheduled_for, was_due):
        started = time.monotonic()
        try:
            result = await asyncio.to_thread(
                endpoint.poll_once, after_apply,
            )
        except Exception as exc:
            print(f"[channel] poll failed: {exc}", flush=True)
            result = None
        return endpoint, scheduled_for, was_due, started, result

    results = await asyncio.gather(*(
        run_endpoint(endpoint, scheduled_for, was_due)
        for endpoint, scheduled_for, was_due in due_endpoints
    ))
    changed = False
    for endpoint, scheduled_for, was_due, started, result in results:
        connection_key = id(endpoint)
        finished_at = time.monotonic()
        if (
            was_due
            or scheduled_for is None
            or finished_at >= scheduled_for
        ):
            next_due[connection_key] = _advance_poll_deadline(
                scheduled_for,
                started,
                endpoint.poll_interval_seconds,
                finished_at,
            )
        if result is None:
            continue
        changed = result.changed or changed
        if not result.ok:
            print(
                f"[channel] poll failed: {result.error or 'unknown error'}",
                flush=True,
            )
            continue
    if deferred_effects:
        # Reconciliation above was atomic with incoming Session state, but
        # effects can enter channel management. Deliver them only after every
        # poll_once has released both Session and per-connection I/O locks.
        await asyncio.to_thread(
            runtime.deliver_effects, tuple(deferred_effects.values()),
        )
        runtime.session.advance_view_revision()
        changed = True
    if changed:
        # Incoming state was already made visible atomically by after_apply.
        # A result can also change through publication/acknowledgement alone,
        # in which case it still needs one visible revision.
        if not view_confirmed.is_set():
            runtime.session.advance_view_revision()
        runtime.persist_confirmed_change("channel")
    return changed


async def channel_publish_tick(runtime: AppRuntime) -> bool:
    """Push local work out without waiting for an inbound poll.

    A cycle publishes last, so a local edit woke the loop and then queued
    behind a heartbeat write and a full poll before it could leave. Inbound
    state is not what the edit is waiting for, and the regular cadence is
    still the only thing that reads it - this only shortens the way out.

    Endpoints that cannot publish on their own fall back to a full cycle, so
    a channel predating this keeps working exactly as before.
    """
    endpoints = [
        endpoint for endpoint in runtime.channel_manager.polling_endpoints()
        if endpoint.has_active_relationship()
    ]
    if not endpoints:
        return False

    async def run(endpoint):
        publish = getattr(endpoint, "publish_once", None)
        if publish is None:
            return None
        try:
            return await asyncio.to_thread(publish)
        except Exception as exc:
            print(f"[channel] publish failed: {exc}", flush=True)
            return None

    results = await asyncio.gather(*(run(endpoint) for endpoint in endpoints))
    if any(result is None for result in results):
        # At least one endpoint has no publish-only path; a full cycle is the
        # only way to serve it, and it publishes for the others too.
        return await channel_poll_tick(runtime, due_only=False)
    changed = any(result.changed for result in results if result)
    if changed:
        runtime.session.advance_view_revision()
        runtime.persist_confirmed_change("channel")
    return changed


async def channel_poll_loop(runtime: AppRuntime) -> None:
    # Polling channels are always Core services. With no active relationship,
    # this loop remains idle without relying on application discovery.
    runtime.channel_loop = asyncio.get_running_loop()
    runtime.channel_wakeup = asyncio.Event()
    while True:
        connections = runtime.channel_manager.polling_endpoints()
        now = time.monotonic()
        next_due = runtime.config.setdefault("_channel_next_due", {})
        delays = [
            next_due.get(id(relay), now) - now
            for relay in connections if relay.has_active_relationship()
        ]
        interval = min(delays) if delays else float(
            runtime.config.get("relay_poll_interval_seconds", 3),
        )
        woke_for_change = False
        try:
            await asyncio.wait_for(
                runtime.channel_wakeup.wait(), timeout=max(0.05, interval),
            )
            woke_for_change = True
            runtime.channel_wakeup.clear()
        except asyncio.TimeoutError:
            pass
        if woke_for_change:
            # A local edit only needs the way out. Polling stays on its own
            # cadence rather than being pulled forward by every keystroke.
            await channel_publish_tick(runtime)
        else:
            await channel_poll_tick(runtime, due_only=True)


def main(
    argv: list[str] | None = None,
    application_aliases: dict[str, dict] | None = None,
) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) not in (1, 2):
        print("Usage: python app_server.py <port[:app]> [config.json]")
        raise SystemExit(1)
    port, app_name = parse_target(argv[0])
    config = load_config(
        argv[1] if len(argv) == 2 else None, app_name, application_aliases,
    )
    runtime = create_runtime(port, config)
    app = build_app(runtime)
    print(f"SI node: {runtime.address}")
    print(f"Root: {runtime.session.root_uuid()}")
    print(f"Applications: {', '.join(runtime.host.instances) if runtime.host else ''}")
    print(f"Storage: {runtime.config.get('storage_file')}")
    if runtime.session.trace.enabled:
        print(
            f"Trace: {runtime.session.trace.path} "
            f"({runtime.session.trace.level})"
        )
    try:
        uvicorn.run(
            app,
            host=config.get("bind_host") or "127.0.0.1",
            port=port,
            log_level="error",
        )
    except KeyboardInterrupt:
        runtime.persist()


if __name__ == "__main__":
    main()
