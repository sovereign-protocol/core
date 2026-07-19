#!/usr/bin/env python3
"""
Application server component.

Functionality:
  Bind the Core HTTP surface, explicit applications, persistence, Session,
  and transport services.
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
  collect_channel_descriptors(runtime) -> list[dict]
    The configured Core relay descriptor, prepended by {"type": "http", ...}
    unless the
    config sets "offer_http_channel": false (which advertises relay-only
    tokens, so an accepter can never race a direct connection alongside
    relay - the http server still serves the local UI).
    `relay_only: true` applies the same omission as a client-wide policy.
  accept_connect_token(runtime, identity, topic_uuids, channels) -> dict
    The negotiation step behind POST /api/connect. Exactly one channel is
    ever selected: http is tried first (its join doubles as the
    reachability probe), relay only as a fallback - the accepting side
    decides, regardless of the order the token lists candidates in. A
    relay-only client skips HTTP completely. An
    unrecognized (type, version) is silently skipped, not an error. A
    relay accepter with no storage of its own provisions it from the
    token (see DESIGN_IDENTITY_AND_TRANSPORT.md §1.6). Accepting a token
    for an already-known identity supersedes that peer's old address
    rather than accumulating a second one. The http channel reuses the
    exact same Core dispatch as /api/join_discussion.

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
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .protocol import ProtocolNode, UnsupportedProtocolVersion
from .application import ApplicationServices
from .host import ApplicationHost
from .profile import CoreProfileService
from .blob_store import (
    BlobStore, SAFE_IMAGE_MIMES, blob_hex, canonical_attachments,
    is_valid_image, referenced_blob_ids,
)
from .session import Session, SessionEffect
from .trace_log import TraceLogger
from .transport import HttpTransportAdapter
from .relay_logic import RelayManager, build_routes as build_relay_routes
from .versions import (
    CHANNEL_DESCRIPTOR_VERSION,
    CONNECT_TOKEN_VERSION,
    PROTOCOL_SCHEMA_VERSION,
    SESSION_ENVELOPE_FORMAT,
    SESSION_ENVELOPE_VERSION,
)


DEFAULT_CONFIG = {
    "app_module": None,
    "ui_file": None,
    "css_file": None,
    "storage_file": None,
    "debug": True,
    "peer_sync_interval_seconds": 2,
    "trace_log_file": None,
    # advertise_host is what we tell peers to reach us at (e.g. a Tailscale
    # IP or a public domain); bind_host is which network interface uvicorn
    # actually listens on ("0.0.0.0" to accept from any interface). Both
    # default to loopback-only, matching same-machine-only behavior.
    "advertise_host": "127.0.0.1",
    "bind_host": "127.0.0.1",
    # Keep the local browser UI available, but disable direct HTTP as a peer
    # transport in both directions. Relay descriptors remain usable.
    "relay_only": False,
    "applications": None,
    "primary_application_id": None,
}
LEGACY_APPLICATION_ALIASES = {
    "kanban": {
        "app_module": "s_kanban.logic",
        "application_id": "kanban",
        "asset_package": "s_kanban.assets",
        "ui_file": "kanban.html",
        "css_file": "kanban.css",
    },
    "manual": {
        "app_module": "sovereign.protocol_explorer",
        "application_id": "protocol-explorer",
        "asset_package": "sovereign.assets",
        "ui_file": "manual.html",
        "css_file": "manual.css",
    },
    "boardofboards": {
        "app_module": "personal_cockpit.logic",
        "application_id": "personal-cockpit",
        "asset_package": "personal_cockpit.assets",
        "ui_file": "boardofboards.html",
        "css_file": "boardofboards.css",
    },
}

_SAVE_LOCKS: dict[str, threading.Lock] = {}
_SAVE_LOCKS_GUARD = threading.Lock()


@dataclass
class AppRuntime:
    port: int
    address: str
    config: dict[str, Any]
    session: Session
    adapter: HttpTransportAdapter
    blob_store: BlobStore
    profile: CoreProfileService
    relay_manager: RelayManager
    host: ApplicationHost | None = None
    logic: Any = None
    relay_wakeup: asyncio.Event | None = None
    relay_loop: asyncio.AbstractEventLoop | None = None

    def persist(self) -> None:
        storage_file = self.config.get("storage_file")
        if storage_file:
            if self.relay_manager is not None:
                # Target registry and board assignments live in app_metadata;
                # snapshot them while the manager cannot mutate them.
                with self.relay_manager._manager_lock:
                    save_session_to_file(self.session, storage_file)
            else:
                save_session_to_file(self.session, storage_file)
        # Grace time protects a new upload until its reference is committed.
        # Running after persisted mutations completes local GC automatically.
        self.collect_local_blobs()

    def notify_change(self, kind: str = "changed") -> None:
        # Name kept for its many call sites; `kind` is retained as a
        # readable marker of what triggered the save. Browsers learn about
        # changes by polling (every UI does its own interval) - there is no
        # push channel, by design.
        self.persist()
        # Local edits should publish immediately instead of waiting for the
        # next poll timeout. Relay-originated persistence must not wake the
        # loop recursively; that cycle already publishes its response.
        if kind not in ("relay", "network") and self.relay_wakeup and self.relay_loop:
            if self.relay_loop.is_running():
                self.relay_loop.call_soon_threadsafe(self.relay_wakeup.set)

    def collect_local_blobs(self) -> list[str]:
        with self.session.lock:
            referenced = referenced_blob_ids(self.session.protocol.root)
            for tree in self.session.peer_perspectives.values():
                referenced.update(referenced_blob_ids(tree))
            # Keep the mark and sweep atomic with respect to references being
            # added by local edits or incoming session updates.
            return self.blob_store.collect(referenced)


def parse_target(target: str) -> tuple[int, str | None]:
    if ":" not in target:
        return int(target), None
    port_text, app_name = target.split(":", 1)
    if not app_name:
        raise ValueError("app name is required after ':'")
    return int(port_text), app_name


def app_default_config(app_name: str) -> dict:
    known = LEGACY_APPLICATION_ALIASES.get(app_name)
    if known:
        config = dict(known)
        config["applications"] = [{"module": known["app_module"]}]
        config["primary_application_id"] = known["application_id"]
        return config
    return {
        "app_module": f"{app_name}_logic",
        "applications": [{"module": f"{app_name}_logic"}],
        "primary_application_id": app_name,
    }


def load_config(config_path: str | None = None,
                app_name: str | None = None) -> dict:
    config = dict(DEFAULT_CONFIG)
    if app_name:
        config.update(app_default_config(app_name))
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


def save_session_to_file(session: Session, path: str, logger=print) -> None:
    absolute_path = os.path.abspath(path)
    directory = os.path.dirname(absolute_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with _SAVE_LOCKS_GUARD:
        lock = _SAVE_LOCKS.setdefault(absolute_path, threading.Lock())
    with lock:
        tmp_path = f"{absolute_path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        with session.lock:
            protocol_snapshot = session.export_protocol_root()
            try:
                ProtocolNode.from_dict(protocol_snapshot)
            except ValueError as exc:
                logger(
                    "[persistence] in-memory session has invalid hashes before save "
                    f"{absolute_path}: {exc}"
                )
                repaired = ProtocolNode.from_dict(protocol_snapshot, repair_hashes=True)
                session.load_protocol_root(repaired)
                protocol_snapshot = repaired.to_dict()
            snapshot = {
                "format": SESSION_ENVELOPE_FORMAT,
                "version": SESSION_ENVELOPE_VERSION,
                "protocol_schema_version": PROTOCOL_SCHEMA_VERSION,
                "protocol_root": protocol_snapshot,
                "session": _session_metadata(session),
            }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, sort_keys=True, indent=2)
            f.write("\n")
        try:
            _replace_with_retry(tmp_path, absolute_path, logger)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


def _replace_with_retry(source: str, destination: str, logger=print) -> None:
    for attempt in range(12):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            if attempt == 11:
                raise
            if attempt >= 2:
                logger(
                    "[persistence] save replace blocked, retrying "
                    f"{attempt + 1}/11 for {destination}: {exc}"
                )
            time.sleep(0.08 * (attempt + 1))


def load_session_from_file(session: Session, path: str, logger=print) -> bool:
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    envelope_error = _session_envelope_error(payload)
    if envelope_error:
        logger(f"[persistence] unsupported session format {path}: {envelope_error}")
        return False
    protocol_payload = payload["protocol_root"]
    try:
        root = ProtocolNode.from_dict(protocol_payload)
    except UnsupportedProtocolVersion as exc:
        logger(f"[persistence] unsupported protocol schema {path}: {exc}")
        return False
    except ValueError as exc:
        logger(f"[persistence] repairing invalid stored session {path}: {exc}")
        root = ProtocolNode.from_dict(protocol_payload, repair_hashes=True)
    core_schema_error = session.validate_core_tree(root)
    if core_schema_error:
        logger(f"[persistence] unsupported Core data schema {path}: {core_schema_error}")
        return False
    session.load_protocol_root(root)
    _restore_session_metadata(session, payload.get("session", {}))
    if root.to_dict() != protocol_payload:
        logger(f"[persistence] saving repaired session {path}")
        save_session_to_file(session, path, logger=logger)
    return True


def _is_session_envelope(payload: dict) -> bool:
    return _session_envelope_error(payload) is None


def _session_envelope_error(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return "expected a JSON object"
    if payload.get("format") == "prsp-session-v1":
        return "legacy format 'prsp-session-v1' is not supported"
    if payload.get("format") != SESSION_ENVELOPE_FORMAT:
        return f"expected format '{SESSION_ENVELOPE_FORMAT}'"
    if payload.get("version") != SESSION_ENVELOPE_VERSION:
        return f"unsupported envelope version {payload.get('version')!r}"
    if payload.get("protocol_schema_version") != PROTOCOL_SCHEMA_VERSION:
        return (
            "unsupported protocol schema version "
            f"{payload.get('protocol_schema_version')!r}"
        )
    if "protocol_root" not in payload:
        return "missing protocol_root"
    return None


def _session_metadata(session: Session) -> dict:
    return {
        "members": sorted(session.members - {session.address}),
        "active_topic_uuids": sorted(session.active_topic_uuids),
        "peer_topics": dict(sorted(session.peer_topics.items())),
        "peer_topic_sets": {
            addr: sorted(topics)
            for addr, topics in sorted(session.peer_topic_sets.items())
        },
        "peer_fetch_topic_sets": {
            addr: sorted(topics)
            for addr, topics in sorted(session.peer_fetch_topic_sets.items())
        },
        "peer_status": dict(sorted(session.peer_status.items())),
        "peer_identity_key": dict(sorted(session.peer_identity_key.items())),
        "observed_topics": {
            addr: sorted(topics)
            for addr, topics in sorted(session.observed_topics.items())
        },
        "app_metadata": session.app_metadata,
    }


def _restore_session_metadata(session: Session, metadata: dict) -> None:
    session.active_topic_uuids = {
        uuid for uuid in metadata.get("active_topic_uuids", [])
        if session.has_node(uuid)
    }
    session.peer_topics.clear()
    session.peer_topic_sets.clear()
    session.peer_status.clear()
    session.peer_sync_state.clear()
    session.app_metadata = dict(metadata.get("app_metadata") or {})
    session.observed_topics = {
        addr: set(topics)
        for addr, topics in (metadata.get("observed_topics") or {}).items()
        if isinstance(topics, list)
    }
    # Restored verbatim, deliberately NOT filtered by membership: the
    # registry is knowledge about an address, not registration state. A
    # relay pseudo-address suppressed as redundant lives in no other
    # restored structure - its registry entry is exactly what keeps it
    # suppressed after a restart (relay's own "applied" hash bookkeeping
    # persists too, so an unchanged identity topic never re-applies, and
    # a lost mapping could never be re-learned from content).
    session.peer_identity_key = {
        addr: key
        for addr, key in (metadata.get("peer_identity_key") or {}).items()
        if isinstance(addr, str) and isinstance(key, str) and addr and key
    }
    session.members = {session.address}
    peer_topic_sets = metadata.get("peer_topic_sets") or {}
    peer_fetch_topic_sets = metadata.get("peer_fetch_topic_sets") or {}
    # Only peers that were actually real members before persisting (i.e.
    # went through add_peer, not e.g. Session.note_relay_peer_topic) get
    # restored via add_peer_topics/set_peer_fetch_topics - those calls are
    # what repopulate session.members. A relay pseudo-address legitimately
    # has its own peer_topic_sets entry (by design - kanban's eligibility
    # checks need it) without ever having been a real member; restoring it
    # through the member-registering path would re-leak it into
    # session.members on every single restart, independent of whatever
    # caused it to end up in peer_topic_sets in the first place.
    saved_members = set(metadata.get("members", []))
    for peer in sorted(saved_members | set(peer_topic_sets) | set(peer_fetch_topic_sets)):
        topics = [
            topic for topic in peer_topic_sets.get(peer, [])
            if session.has_node(topic)
        ]
        fetch_topics = [
            topic for topic in peer_fetch_topic_sets.get(peer, [])
            if session.has_node(topic)
        ]
        if peer in saved_members:
            if topics:
                session.add_peer_topics(peer, set(topics))
            if fetch_topics:
                session.set_peer_fetch_topics(peer, set(fetch_topics))
        else:
            if topics:
                session.peer_topic_sets[peer] = set(topics)
            if fetch_topics:
                session.peer_fetch_topic_sets[peer] = set(fetch_topics)
    for peer, topic in (metadata.get("peer_topics") or {}).items():
        if peer in session.members and topic in session.peer_topic_sets.get(peer, set()):
            session.peer_topics[peer] = topic
    for peer, status in (metadata.get("peer_status") or {}).items():
        if peer in session.members and isinstance(status, dict):
            session.peer_status[peer] = {
                "state": status.get("state", "online"),
                "failures": int(status.get("failures", 0) or 0),
                "last_seen": status.get("last_seen"),
                "last_error": status.get("last_error"),
            }
    # peer_sync_state is deliberately not persisted/restored: it exists only
    # to avoid re-sending a sync_status peers already have. peer_perspectives
    # (the actual cache it's tracking) isn't persisted either, so restoring a
    # stale "already delivered" hash across a restart would make a session
    # believe it's fully synced with a peer while holding an empty cache -
    # permanently skipping the very sync that would repopulate it.


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
    if config.get("relay_only", False):
        # A policy change must not revive persisted direct peers or observers.
        # Relay pseudo-peers are not members and therefore survive this cleanup.
        for peer_addr in sorted(session.members - {session.address}):
            session.remove_peer(peer_addr)
        session.observed_topics.clear()
    adapter = HttpTransportAdapter(session, trace=trace)
    relay_manager = RelayManager(session, config, blob_store=blob_store)
    runtime = AppRuntime(
        port=port,
        address=address,
        config=config,
        session=session,
        adapter=adapter,
        blob_store=blob_store,
        profile=CoreProfileService(session),
        relay_manager=relay_manager,
    )
    services = ApplicationServices(
        session=session,
        adapter=adapter,
        blob_store=blob_store,
        trace=trace,
        relay_manager=relay_manager,
        notify_change=runtime.notify_change,
        collect_local_blobs=runtime.collect_local_blobs,
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


def collect_channel_descriptors(runtime: AppRuntime) -> list[dict]:
    # Transitional descriptor collector until R4's ChannelManager. Application
    # modules are never probed for channels.
    channels = []
    if (not runtime.config.get("relay_only", False)
            and runtime.config.get("offer_http_channel", True)):
        channels.append({
            "type": "http",
            "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
            "address": runtime.address,
        })
    manager = _runtime_relay_manager(runtime)
    descriptor = manager.channel_descriptor() if manager else None
    if descriptor:
        channels.append(descriptor)
    return channels


def _runtime_relay_manager(runtime):
    return getattr(runtime, "relay_manager", None)


def _dispatch_join_discussion(runtime: AppRuntime, address: str,
                              topic_uuid: str | None,
                              topic_uuids: list[str] | None) -> dict:
    # Core owns invitation dispatch. Applications contribute only their
    # registered validation/mounting callback through Session.shared_topics.
    return runtime.adapter.join_discussion(address, topic_uuid, topic_uuids)


def accept_connect_token(runtime: AppRuntime, identity: dict | None,
                         topic_uuids: list[str], channels: list[dict]) -> dict:
    # The negotiation step: exactly one channel is ever selected per peer,
    # chosen by the accepting side regardless of what order the token lists
    # candidates in (so the offering side can't influence the outcome) -
    # http is tried first since the join call is itself the reachability
    # probe (no separate probe needed), and relay is only attempted as a
    # fallback if http wasn't offered, was disallowed by relay_only, was
    # self-referential, or failed. No
    # failure-detection/fallback state machine after this point (this only
    # ever runs once, at accept time) - a channel that later stops working
    # just stops delivering until the user reconnects with a fresh token.
    http_channel = next(
        (c for c in channels
         if c.get("type") == "http"
         and c.get("descriptor_version") == CHANNEL_DESCRIPTOR_VERSION),
        None,
    )
    relay_channel = next(
        (c for c in channels
         if c.get("type") in ("relay", "sftp")
         and c.get("descriptor_version") == CHANNEL_DESCRIPTOR_VERSION),
        None,
    )

    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    selected_type: str | None = None
    selected_addr: str | None = None

    if http_channel and runtime.config.get("relay_only", False):
        errors["http"] = "disabled by local relay-only policy"
    elif http_channel:
        address = str(http_channel.get("address") or "").strip().rstrip("/")
        if address and address != runtime.address:
            join_result = _dispatch_join_discussion(runtime, address, None, topic_uuids)
            results["http"] = join_result
            if join_result.get("status") == "ok":
                selected_type, selected_addr = "http", address
            else:
                errors["http"] = join_result.get("reason") or "join failed"

    if selected_type is None and relay_channel:
        # Both file-mailbox backends (local folder, sftp) share the same
        # "relay:<identity>" peer-address namespace and the same accept
        # mechanism - they differ only in where the storage actually
        # writes/reads bytes (relay_storage.py), not in how a token is
        # accepted.
        channel_type = relay_channel.get("type")
        relay_manager = _runtime_relay_manager(runtime)
        # accept_descriptor verifies the advertised storage, registers it as a
        # target, provisions a connection from the token (DESIGN §1.6), and
        # persists the desired topics + board assignments atomically. Absent a
        # relay manager (relay not configured on this instance) the relay
        # channel is simply unusable and we fall through to the error below.
        relay_identity = str(relay_channel.get("identity") or "").strip()
        inviter_identity_uuid = (
            str(identity.get("uuid") or "").strip()
            if isinstance(identity, dict) else ""
        )
        if not relay_identity:
            errors[channel_type] = "relay descriptor missing identity"
        elif not inviter_identity_uuid:
            errors[channel_type] = "token missing inviter identity"
        elif relay_manager:
            try:
                storage_result = relay_manager.accept_descriptor(
                    relay_channel,
                    topic_uuids,
                    inviter_identity_uuid,
                )
            except Exception as exc:
                errors[channel_type] = (
                    f"relay unavailable: {type(exc).__name__}: {exc}"
                )
            else:
                if storage_result.status != "ok":
                    errors[channel_type] = storage_result.reason or "relay unavailable"
                else:
                    results[channel_type] = {"status": "ok", "reason": None}
                    selected_type, selected_addr = channel_type, f"relay:{relay_identity}"

    if not selected_type or not selected_addr:
        detail = "; ".join(
            f"{channel}: {reason}" for channel, reason in sorted(errors.items())
        )
        reason = "no usable channel in token"
        if detail:
            reason = f"{reason} ({detail})"
        runtime.session.trace_event(
            "transport.connect_token_rejected",
            offered_channel_types=[
                channel.get("type") for channel in channels
                if isinstance(channel, dict)
            ],
            errors=errors,
        )
        return {"status": "error", "reason": reason, "errors": errors}

    # Reconnect replacement: if this identity is already known under other
    # addresses (e.g. a fresh token after their old one stopped working),
    # those old registrations are superseded, not kept alongside the new
    # one - "closed before initiating a new one."
    #
    # A relay pseudo-address (relay:<identity>) is a special case: it is
    # never a *superseded* address (there's only ever one relay address
    # per identity, so a later relay token reuses the same one), but a
    # *concurrent* channel that relay's poll-loop redundancy check owns.
    # So we still tear down its registration here (no stale duplicate at
    # connect time), but must NOT forget its identity_key - that entry is
    # exactly what _is_redundant_relay_peer reads to keep relay:<id>
    # suppressed on every later poll, and relay's `applied` bookkeeping
    # means the unchanged identity topic never re-applies to re-teach it.
    # Forgetting it (as a real dead address legitimately is) reopened the
    # duplicate for good - caught live when relay discovered the peer
    # before the http connect completed.
    identity_key = identity.get("data", {}).get("identity_key") if isinstance(identity, dict) else None
    if identity_key:
        for old_addr in runtime.session.addresses_for_identity(identity_key):
            if old_addr != selected_addr:
                runtime.session.remove_peer(old_addr)
                if not old_addr.startswith("relay:"):
                    runtime.session.peer_identity_key.pop(old_addr, None)

    if isinstance(identity, dict):
        runtime.session.apply_peer_identity_snapshot(selected_addr, identity)
    runtime.session.note_peer_channel(selected_addr, selected_type)
    runtime.session.trace_event(
        "transport.connect_token_selected",
        selected_type=selected_type,
        selected_addr=selected_addr,
    )

    return {"status": "ok", "channels_used": [selected_type], "results": results}


def build_core_routes(runtime: AppRuntime) -> list[Route]:
    def direct_http_disabled() -> JSONResponse | None:
        if not runtime.config.get("relay_only", False):
            return None
        return JSONResponse(
            {
                "status": "error",
                "reason": "direct HTTP transport disabled by relay-only policy",
            },
            status_code=403,
        )

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

    async def api_protocol(request: Request):
        await refresh_shared_peer_topics(runtime)
        await drain_peer_update_hook(runtime)
        return JSONResponse(runtime.session.export_protocol_root())

    async def api_network(request: Request):
        return JSONResponse(runtime.session.get_network_info())

    async def profile_result(result) -> JSONResponse:
        if result.status != "ok":
            return JSONResponse(
                {"status": "error", "reason": result.reason}, status_code=409,
            )
        deliveries = await asyncio.to_thread(
            runtime.adapter.execute_effects, result.effects,
        )
        runtime.notify_change("profile")
        payload = {"status": "ok", **runtime.profile.view()}
        errors = [item for item in deliveries if not item.ok]
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

    async def api_join_discussion(request: Request):
        if denied := direct_http_disabled():
            return denied
        try:
            data = await request.json()
            result = await asyncio.to_thread(
                _dispatch_join_discussion,
                runtime,
                data["address"].strip().rstrip("/"),
                data.get("topic_uuid"),
                data.get("topic_uuids"),
            )
            if result.get("status") == "ok":
                runtime.notify_change()
                return JSONResponse(result)
            return JSONResponse(result, status_code=409)
        except Exception as exc:
            return JSONResponse(
                {"status": "error", "reason": str(exc)},
                status_code=500,
            )

    async def api_connect_token(request: Request):
        # A token is composed explicitly (POST) from a set of topics and an
        # optional relay target. With a target, the token carries that
        # target's relay descriptor and assigning the topics to it arms the
        # relay loop (assignment replaced the old mark_topics_shared trigger).
        # Without a target it is an HTTP-only token for a direct peer
        # connection (e.g. on a LAN) that never touches a relay.
        manager = _runtime_relay_manager(runtime)
        data = await request.json()
        topic_uuids = sorted({
            str(value).strip()
            for value in data.get("topic_uuids", [])
            if str(value).strip()
        })
        if not topic_uuids:
            return JSONResponse(
                {"status": "error", "reason": "choose at least one topic"},
                status_code=400,
            )
        target_id = str(data.get("target_id") or "")
        channels = []
        if (not runtime.config.get("relay_only", False)
                and runtime.config.get("offer_http_channel", True)):
            channels.append({
                "type": "http",
                "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
                "address": runtime.address,
            })
        if target_id:
            if not manager:
                return JSONResponse(
                    {"status": "error", "reason": "relay manager is not available"},
                    status_code=409,
                )
            descriptor = manager.target_descriptor(target_id)
            if not descriptor:
                return JSONResponse(
                    {"status": "error", "reason": "relay target not found"},
                    status_code=404,
                )
            # Validate and assign the complete topic selection as one
            # operation; a bad UUID must not partially reassign earlier
            # topics before the request fails.
            result = manager.assign_topics_target(topic_uuids, target_id)
            if result.status != "ok":
                return JSONResponse(
                    {"status": "error", "reason": result.reason}, status_code=409,
                )
            channels.append(descriptor)
        if not channels:
            return JSONResponse(
                {"status": "error", "reason": "no channel available - assign a relay target"},
                status_code=409,
            )
        runtime.notify_change("connect-token")
        identity = runtime.session.identity
        return JSONResponse({
            "token_version": CONNECT_TOKEN_VERSION,
            "identity": identity.to_dict(),
            "topic_uuids": sorted({*topic_uuids, identity.uuid}),
            "channels": channels,
        })

    async def api_connect(request: Request):
        try:
            data = await request.json()
            token = data.get("token") or {}
            if (not isinstance(token, dict)
                    or token.get("token_version") != CONNECT_TOKEN_VERSION):
                return JSONResponse(
                    {"status": "error", "reason": "unrecognized token version"},
                    status_code=400,
                )
            result = await asyncio.to_thread(
                accept_connect_token,
                runtime,
                token.get("identity"),
                token.get("topic_uuids") or [],
                token.get("channels") or [],
            )
            if result.get("status") == "ok":
                runtime.notify_change()
                return JSONResponse(result)
            return JSONResponse(result, status_code=409)
        except Exception as exc:
            return JSONResponse(
                {"status": "error", "reason": str(exc)},
                status_code=500,
            )

    async def api_observe_topic(request: Request):
        if denied := direct_http_disabled():
            return denied
        # Read-only counterpart of /api/join_discussion: caches the target's
        # perspective for repeated viewing without ever registering as a
        # peer of theirs. Generic across apps - never merges into the
        # caller's own protocol tree, so there's nothing for any app to
        # gatekeep here.
        try:
            data = await request.json()
            result = await asyncio.to_thread(
                runtime.adapter.observe_topic,
                data["address"].strip().rstrip("/"),
                data.get("topic_uuid"),
                data.get("topic_uuids"),
            )
            if result.get("status") == "ok":
                runtime.notify_change()
                return JSONResponse(result)
            return JSONResponse(result, status_code=409)
        except Exception as exc:
            return JSONResponse(
                {"status": "error", "reason": str(exc)},
                status_code=500,
            )

    async def api_unwatch_topic(request: Request):
        # Purely local bookkeeping - the observed peer never knew we were
        # watching, so there's nothing to tell them when we stop.
        try:
            data = await request.json()
            removed = runtime.session.unwatch_topic(
                data["address"].strip().rstrip("/"),
                data["topic_uuid"],
            )
            if removed:
                runtime.notify_change()
            return JSONResponse({"status": "ok", "removed": removed})
        except Exception as exc:
            return JSONResponse(
                {"status": "error", "reason": str(exc)},
                status_code=500,
            )

    async def api_invite_discuss(request: Request):
        if denied := direct_http_disabled():
            return denied
        try:
            data = await request.json()
            result = await asyncio.to_thread(
                runtime.adapter.invite_to_discuss,
                data["address"].strip().rstrip("/"),
                data.get("topic_uuid"),
                data.get("topic_uuids"),
            )
            if result.get("status") == "ok":
                runtime.notify_change()
                return JSONResponse(result)
            return JSONResponse(result, status_code=409)
        except Exception as exc:
            return JSONResponse(
                {"status": "error", "reason": str(exc)},
                status_code=500,
            )

    async def api_leave(request: Request):
        try:
            data = await request.json()
        except Exception:
            data = {}
        topic_uuid = data.get("topic_uuid")
        if topic_uuid:
            await asyncio.to_thread(runtime.adapter.leave_topic, topic_uuid)
        else:
            await asyncio.to_thread(runtime.adapter.leave_discussion)
        runtime.notify_change()
        return JSONResponse({"status": "ok"})

    async def p2p_sync_status(request: Request):
        if denied := direct_http_disabled():
            return denied
        payload = await request.json()
        response, status = await asyncio.to_thread(
            runtime.adapter.p2p_sync_status,
            payload,
        )
        if status == 200:
            await drain_peer_update_hook(runtime)
            runtime.notify_change()
        return JSONResponse(response, status_code=status)

    async def p2p_join(request: Request):
        if denied := direct_http_disabled():
            return denied
        payload = await request.json()
        response, status = await asyncio.to_thread(
            runtime.adapter.p2p_join,
            payload,
        )
        if status == 200:
            await drain_peer_update_hook(runtime)
            runtime.notify_change()
        return JSONResponse(response, status_code=status)

    async def p2p_announce(request: Request):
        if denied := direct_http_disabled():
            return denied
        payload = await request.json()
        response, status = await asyncio.to_thread(
            runtime.adapter.p2p_announce,
            payload,
        )
        if status == 200:
            await drain_peer_update_hook(runtime)
            runtime.notify_change()
        return JSONResponse(response, status_code=status)

    async def p2p_leave(request: Request):
        payload = await request.json()
        response, status = await asyncio.to_thread(
            runtime.adapter.p2p_leave,
            payload,
        )
        if status == 200:
            runtime.notify_change()
        return JSONResponse(response, status_code=status)

    async def p2p_subtree(request: Request):
        if denied := direct_http_disabled():
            return denied
        response, status = runtime.adapter.p2p_subtree(
            request.path_params["uuid"]
        )
        return JSONResponse(response, status_code=status)

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

    def reference_for_blob(blob_id: str) -> dict | None:
        with runtime.session.lock:
            roots = [runtime.session.protocol.root, *runtime.session.peer_perspectives.values()]
            for root in roots:
                stack = [root]
                while stack:
                    node = stack.pop()
                    for item in canonical_attachments(node.data.get("attachments")):
                        if item["blob_id"] == blob_id:
                            return item
                    stack.extend(node.children)
        return None

    def resolve_blob(blob_id: str, allow_peer_fetch: bool) -> bytes | None:
        local = runtime.blob_store.read_blob(blob_id)
        if local is not None:
            return local
        manager = _runtime_relay_manager(runtime)
        if manager is not None:
            fetched = manager.read_blob(blob_id)
            if fetched is not None:
                runtime.blob_store.write_blob(fetched)
                return fetched
        if not allow_peer_fetch:
            return None
        import requests
        for peer in sorted(runtime.session.members - {runtime.session.address}):
            if not peer.startswith(("http://", "https://")):
                continue
            try:
                response = requests.get(
                    f"{peer.rstrip('/')}/api/blob/{blob_id}",
                    headers={"X-S-Kanban-Blob-Hop": "1"}, timeout=10,
                )
                if response.status_code != 200:
                    continue
                if runtime.blob_store.write_blob(response.content) != blob_id:
                    continue
                return response.content
            except Exception:
                continue
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
            request.headers.get("x-s-kanban-blob-hop") != "1",
        )
        if data is None:
            return JSONResponse(
                {"status": "error", "reason": "blob not found"}, status_code=404,
            )
        reference = reference_for_blob(blob_id) or {}
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
        Route("/shared.js", serve_shared_js),
        Route("/api/protocol", api_protocol),
        Route("/api/network", api_network),
        Route("/api/core/profile", api_core_profile, methods=["GET", "POST"]),
        Route(
            "/api/core/profile/avatar",
            api_core_profile_avatar,
            methods=["POST"],
        ),
        Route("/api/join_discussion", api_join_discussion, methods=["POST"]),
        Route("/api/connect_token", api_connect_token, methods=["POST"]),
        Route("/api/connect", api_connect, methods=["POST"]),
        Route("/api/observe_topic", api_observe_topic, methods=["POST"]),
        Route("/api/unwatch_topic", api_unwatch_topic, methods=["POST"]),
        Route("/api/invite_discuss", api_invite_discuss, methods=["POST"]),
        Route("/api/leave", api_leave, methods=["POST"]),
        Route("/api/blob", api_blob_upload, methods=["POST"]),
        Route("/api/blob/gc", api_blob_gc, methods=["POST"]),
        Route("/api/blob/{blob_id}", api_blob_get),
        Route("/p2p/sync_status", p2p_sync_status, methods=["POST"]),
        Route("/p2p/join", p2p_join, methods=["POST"]),
        Route("/p2p/announce", p2p_announce, methods=["POST"]),
        Route("/p2p/leave", p2p_leave, methods=["POST"]),
        Route("/p2p/subtree/{uuid}", p2p_subtree),
    ]


async def run_peer_update_hook(runtime: AppRuntime) -> bool:
    host = getattr(runtime, "host", None)
    if not host:
        return False
    return await asyncio.to_thread(host.notify_peer_update)


async def drain_peer_update_hook(runtime: AppRuntime, passes: int = 4) -> None:
    for _ in range(passes):
        changed = await run_peer_update_hook(runtime)
        if not changed:
            break


async def refresh_shared_peer_topics(runtime: AppRuntime) -> None:
    effects = []
    for peer, topic_uuids in sorted(runtime.session.peer_topic_sets.items()):
        if peer == runtime.session.address:
            continue
        for topic_uuid in sorted(topic_uuids):
            effects.append(SessionEffect(
                "pull_subtree",
                peer,
                {"node_uuid": topic_uuid, "topic_uuid": topic_uuid},
            ))
    if effects:
        await asyncio.to_thread(runtime.adapter.execute_effects, effects)


def build_app(runtime: AppRuntime) -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette):
        sync_task = asyncio.create_task(peer_sync_loop(runtime))
        observer_task = asyncio.create_task(observer_sync_loop(runtime))
        relay_task = asyncio.create_task(relay_poll_loop(runtime))
        blob_gc_task = asyncio.create_task(local_blob_gc_loop(runtime))
        try:
            yield
        finally:
            sync_task.cancel()
            observer_task.cancel()
            relay_task.cancel()
            blob_gc_task.cancel()
            for task in (sync_task, observer_task, relay_task, blob_gc_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if runtime.host:
                runtime.host.close()
            runtime.persist()

    core_routes = build_core_routes(runtime)
    relay_routes = build_relay_routes(
        runtime.relay_manager, runtime, runtime.config,
    )
    application_routes = runtime.host.controller_routes() if runtime.host else []
    app = Starlette(
        debug=bool(runtime.config.get("debug", True)),
        routes=core_routes + relay_routes + application_routes,
        lifespan=lifespan,
    )
    if runtime.host:
        runtime.host.bind_starlette(app, [*core_routes, *relay_routes])
    return app


async def peer_sync_loop(runtime: AppRuntime) -> None:
    interval = float(runtime.config.get("peer_sync_interval_seconds", 2))
    while True:
        await asyncio.sleep(max(1.0, interval))
        effects = runtime.session.pending_sync_effects()
        if not effects:
            continue
        deliveries = await asyncio.to_thread(runtime.adapter.execute_effects, effects)
        if any(delivery.ok for delivery in deliveries):
            await drain_peer_update_hook(runtime)
            runtime.notify_change("network")


async def local_blob_gc_loop(runtime: AppRuntime) -> None:
    grace = float(runtime.config.get("blob_gc_grace_seconds", 60))
    interval = float(runtime.config.get("blob_gc_interval_seconds", max(60, grace)))
    while True:
        await asyncio.sleep(max(10.0, interval))
        await asyncio.to_thread(runtime.collect_local_blobs)


async def observer_sync_loop(runtime: AppRuntime) -> None:
    # Re-polls read-only observed topics on our own schedule, since an
    # observed peer never learns we exist and so never pings/pushes to us.
    interval = float(runtime.config.get("observer_sync_interval_seconds", 5))
    while True:
        await asyncio.sleep(max(1.0, interval))
        pairs = runtime.session.observed_topic_pairs()
        if not pairs:
            continue
        changed = False
        for peer_addr, topic_uuid in pairs:
            result = await asyncio.to_thread(
                runtime.adapter.observe_topic, peer_addr, topic_uuid,
            )
            changed = changed or result.get("status") == "ok"
        if changed:
            runtime.notify_change("network")


async def relay_tick(runtime: AppRuntime, due_only: bool = False) -> bool:
    # One relay cycle: presence + publish + poll, then - crucially - the
    # app's adoption hook. Without that drain, relay-applied peer content
    # only ever reached the cache; adoption ran solely from UI polls and
    # http p2p endpoints, so a headless relay-only session synced content
    # it never adopted (and never republished merged). Factored out of the
    # loop so this behavior is testable without spinning the loop.
    manager = _runtime_relay_manager(runtime)
    if manager is None:
        return False
    changed = False
    # A client can run several relay connections at once. Their network I/O
    # is independent and must be concurrent: one old or slow target must not
    # postpone every other target. RelayManager gives the connections a
    # shared lock around the brief Session mutation sections.
    now = time.monotonic()
    next_due = runtime.config.setdefault("_relay_next_due", {})
    due_connections = []
    for relay in manager.all_connections():
        connection_key = id(relay)
        scheduled_for = next_due.get(connection_key)
        was_due = scheduled_for is None or now >= scheduled_for
        if due_only and not was_due:
            continue
        if not relay.has_active_relationship():
            # No relay token issued or accepted on this connection, and no
            # relay peer yet - stay fully idle (no files written for no one).
            continue
        due_connections.append((relay, scheduled_for, was_due))

    async def first_pass(relay):
        cycle_started = time.monotonic()
        try:
            calibrate = getattr(relay, "calibrate_timing_if_due", None)
            if calibrate:
                await asyncio.to_thread(calibrate)
            cycle_started = time.monotonic()
            await asyncio.to_thread(relay.write_presence)
            published_before = await asyncio.to_thread(relay.publish_due_topics)
            applied = await asyncio.to_thread(relay.poll_and_apply)
        except Exception as exc:
            print(f"[relay] sync failed: {exc}", flush=True)
            return relay, [], [], False, time.monotonic() - cycle_started
        duration = time.monotonic() - cycle_started
        return relay, published_before, applied, True, duration

    first_results = await asyncio.gather(
        *(first_pass(relay) for relay, _scheduled, _was_due in due_connections),
    )
    adoption_started = time.monotonic()
    had_applied = any(
        applied for _relay, _published, applied, ok, _duration in first_results if ok
    )
    if had_applied:
        await drain_peer_update_hook(runtime)
    adoption_duration = time.monotonic() - adoption_started if had_applied else 0.0

    async def response_pass(relay, applied):
        # Publish the reaction/acknowledgement in this same relay cycle. This
        # is essential when auto-adopt is off too: the unchanged local
        # snapshot's head still needs to say "I have seen your revision."
        started = time.monotonic()
        try:
            published = await asyncio.to_thread(relay.publish_due_topics) if applied else []
            return published, time.monotonic() - started
        except Exception as exc:
            print(f"[relay] response publish failed: {exc}", flush=True)
            return [], time.monotonic() - started

    response_results = await asyncio.gather(*(
        response_pass(relay, applied)
        for relay, _published, applied, ok, _duration in first_results if ok
    ))
    response_index = 0
    schedule_by_relay = {
        id(relay): (scheduled_for, was_due)
        for relay, scheduled_for, was_due in due_connections
    }
    for relay, published_before, applied, ok, _duration in first_results:
        connection_key = id(relay)
        scheduled_for, was_due = schedule_by_relay[connection_key]
        stable_interval = float(getattr(relay, "poll_interval_seconds", 3))
        if not ok:
            next_due[connection_key] = time.monotonic() + stable_interval
            continue
        published_after, response_duration = response_results[response_index]
        response_index += 1
        record_duration = getattr(relay, "record_cycle_duration", None)
        if record_duration:
            record_duration(
                _duration + response_duration
                + (adoption_duration if applied else 0.0)
            )
        if published_before or published_after:
            calculate_delay = getattr(relay, "response_check_delay", None)
            delay = calculate_delay() if calculate_delay else stable_interval
            next_due[connection_key] = time.monotonic() + max(0.05, float(delay))
        elif was_due or scheduled_for is None:
            next_due[connection_key] = time.monotonic() + stable_interval
        # If a local edit woke this connection before its calculated answer
        # check and it had nothing to publish, retain the earlier deadline.
        if published_before or published_after or applied:
            changed = True
    if changed:
        runtime.notify_change("relay")
    return changed


async def relay_poll_loop(runtime: AppRuntime) -> None:
    # RelayManager is always a Core service. With no configured or accepted
    # target it has no active relationship, so this loop remains idle without
    # relying on application discovery or a private config handle.
    runtime.relay_loop = asyncio.get_running_loop()
    runtime.relay_wakeup = asyncio.Event()
    while True:
        manager = _runtime_relay_manager(runtime)
        connections = manager.all_connections() if manager else []
        now = time.monotonic()
        next_due = runtime.config.setdefault("_relay_next_due", {})
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
                runtime.relay_wakeup.wait(), timeout=max(0.05, interval),
            )
            woke_for_change = True
            runtime.relay_wakeup.clear()
        except asyncio.TimeoutError:
            pass
        await relay_tick(runtime, due_only=not woke_for_change)


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) not in (1, 2):
        print("Usage: python app_server.py <port[:app]> [config.json]")
        raise SystemExit(1)
    port, app_name = parse_target(argv[0])
    config = load_config(argv[1] if len(argv) == 2 else None, app_name)
    runtime = create_runtime(port, config)
    app = build_app(runtime)
    print(f"SI node: {runtime.address}")
    print(f"Root: {runtime.session.root_uuid()}")
    print(f"Applications: {', '.join(runtime.host.instances) if runtime.host else ''}")
    print(f"Storage: {runtime.config.get('storage_file')}")
    if runtime.session.trace.enabled:
        print(f"Trace: {runtime.session.trace.path}")
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
