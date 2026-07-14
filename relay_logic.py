"""
File-mailbox relay sync.

Functionality:
  Store-and-forward sync for peers who are never online at the same time,
  and/or not directly reachable (no inbound NAT traversal needed). Each peer
  identity publishes its current view of every board it has into a shared
  storage location (see relay_storage.py - a local folder or a remote SFTP
  server, selected via config), and periodically polls every other known
  identity's published view, applying whatever changed through the
  *existing* sovereign-perspective reconciliation machinery.

  Storage backend selection (config["relay_backend"], default "local"):
    "local" - config["relay_root"] (local folder path).
    "sftp"  - config["relay_sftp_host"] (required to activate), plus
      relay_sftp_port (default 22), relay_sftp_username, relay_sftp_root
      (remote path, default "/"), and either relay_sftp_password or
      relay_sftp_private_key_path (+ optional
      relay_sftp_private_key_passphrase). The password/passphrase never
      need to live in the config file itself - each is resolved in
      priority order: the config key directly (only for local, throwaway
      testing), then an env var (SKANBAN_SFTP_PASSWORD /
      SKANBAN_SFTP_PRIVATE_KEY_PASSPHRASE), then a "<key>_file" config
      entry pointing at a file holding just the secret (works even when
      the server wasn't launched from the same shell an env var was set
      in). Credentials never appear in a channel_descriptor() payload
      either way - that only ever advertises host/port/root/identity (see
      relay_storage.py's module docstring for the plaintext-content/
      trust-on-first-use tradeoffs of this MVP pass).

  This is deliberately not built on Session.add_peer/pending_sync_effects/
  HttpTransportAdapter - those assume every registered member is directly
  HTTP-reachable, which is exactly the constraint this feature removes. It
  only calls two existing Session methods: get_subtree (to publish) and
  apply_peer_subtree (to apply what's downloaded) - the same two calls the
  live HTTP transport already makes, so a relay identity is reconciled
  identically to a real HTTP peer. It never adopts, judges, or merges on its
  own account - it is pure store-and-forward.

  board_uuid IS topic_uuid (kanban_logic.py's boards are topic roots
  directly), so every board this session has is *published* automatically -
  no separate topic allow-list to keep in sync. *Polling*, by contrast, is
  driven by whatever topics actually exist in the relay storage, not by
  this session's own board list - otherwise a peer who's never seen a given
  board before could never learn about it this way, which would defeat the
  point of a standalone (no prior direct P2P join) relay path.

  Bookkeeping (which hash we last published/applied per topic/peer) is
  local-only sync state, kept in its own JSON file next to the session's
  own storage file - never written into PRSP data, since that would make it
  content needing its own sync, which defeats the purpose.

Offered API:
  create_logic(session, config) -> RelayLogic     (extra-app-module hook)
  build_routes(logic, runtime, config) -> list[Route]  (extra-app-module hook)
  channel_descriptor(runtime, config) -> dict | None  (extra-app-module hook)
    Advertises relay as a connectable channel for /api/connect_token, if
    configured - {"type": "relay", "version": 1, "root": ..., "identity": ...}.
  RelayLogic
    relay_topic_uuids() -> list[str]
    publish_due_topics() -> list[str]
    poll_and_apply() -> list[tuple[str, str]]
    status_payload() -> dict
    channel_descriptor() -> dict | None
    mark_topics_desired(topic_uuids) -> SessionResult
      Records topic_uuids as "desired" - the consent step that lets
      poll_and_apply graft a not-yet-locally-known board into our own board
      list the first time it shows up in the relay, instead of merely
      caching it as an (invisible, unowned) peer perspective forever. This
      is how two peers can share a board via relay alone, with no live HTTP
      join ever required - called by the unified /api/connect accept flow
      (app_server.py) once it decides the relay channel is usable, same as
      the HTTP channel's own accept path is called for the http channel.

Used API:
  kanban_logic.KanbanLogic (boards/user_profile only), protocol.PRSPNode,
  session.Session, relay_storage.LocalFolderRelayStorage,
  relay_storage.SftpRelayStorage.
"""

from __future__ import annotations

import copy
import json
import os
import re
import uuid as uuid_mod
from pathlib import Path
from typing import Any

from kanban_logic import KanbanLogic
from protocol import PRSPNode
from session import Session, SessionResult
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from relay_storage import LocalFolderRelayStorage, SftpRelayStorage


def default_relay_state_file(config: dict, identity: str) -> str:
    # Keyed by relay_identity, not port - app_server.py's config dict never
    # actually carries a "port" key, so that would collide across every
    # instance sharing one codebase checkout (exactly the "same machine,
    # local folder" scenario this feature is meant to support).
    app_name = str(config.get("app_module") or "app").replace(".", "_")
    safe_identity = re.sub(r"[^A-Za-z0-9_-]+", "_", identity).strip("_") or "default"
    return str(Path(__file__).with_name("data") / f"relay_state_{app_name}_{safe_identity}.json")


class RelayLogic:
    def __init__(self, session: Session, config: dict):
        self.session = session
        self.kanban = KanbanLogic(session, config)
        self.identity = config.get("relay_identity") or self.kanban.user_profile().uuid
        self.storage = self._build_storage(config)
        self._state_path = config.get("relay_state_file") or default_relay_state_file(config, self.identity)
        self._state = self._load_state()

    @staticmethod
    def _build_storage(config: dict):
        backend = config.get("relay_backend", "local")
        if backend == "sftp":
            host = config.get("relay_sftp_host")
            if not host:
                return None
            # relay_sftp_host is a bare hostname for paramiko/getaddrinfo,
            # not a URL - an "sftp://" (or any other "scheme://") prefix
            # pasted in from an FTP client's connection string is an easy
            # mistake that fails DNS resolution outright, so strip it
            # defensively rather than let every user hit that once.
            if "://" in host:
                host = host.split("://", 1)[1]
            # Three ways to supply the secret, in priority order, none of
            # which require writing it directly into a config file that
            # could end up committed: an explicit config value (lowest
            # priority - only for local, throwaway testing), an environment
            # variable, or a path to a file holding just the secret (works
            # even when the process reading it wasn't started from the same
            # shell that set an env var - e.g. a file the user edits
            # directly, that this code never needs to be told the contents
            # of ahead of time).
            return SftpRelayStorage(
                host=host,
                port=int(config.get("relay_sftp_port", 22)),
                username=config.get("relay_sftp_username"),
                remote_root=config.get("relay_sftp_root", "/"),
                password=RelayLogic._secret(
                    config, "relay_sftp_password", "SKANBAN_SFTP_PASSWORD",
                ),
                private_key_path=config.get("relay_sftp_private_key_path"),
                private_key_passphrase=RelayLogic._secret(
                    config, "relay_sftp_private_key_passphrase",
                    "SKANBAN_SFTP_PRIVATE_KEY_PASSPHRASE",
                ),
            )
        root = config.get("relay_root")
        return LocalFolderRelayStorage(root) if root else None

    @staticmethod
    def _secret(config: dict, config_key: str, env_var: str) -> str | None:
        value = config.get(config_key)
        if value:
            return value
        value = os.environ.get(env_var)
        if value:
            return value
        file_path = config.get(f"{config_key}_file")
        if file_path:
            return Path(file_path).read_text(encoding="utf-8").strip()
        return None

    def relay_topic_uuids(self) -> list[str]:
        return [board.uuid for board in self.kanban.boards()]

    def channel_descriptor(self) -> dict | None:
        if not self.storage:
            return None
        if isinstance(self.storage, SftpRelayStorage):
            return {
                "type": "sftp",
                "version": 1,
                "host": self.storage.host,
                "port": self.storage.port,
                "root": self.storage.root,
                "identity": self.identity,
            }
        return {
            "type": "relay",
            "version": 1,
            "root": str(self.storage.root),
            "identity": self.identity,
        }

    def mark_topics_desired(self, topic_uuids: list[str]) -> SessionResult:
        # Recording topic_uuids as "desired" is the consent step:
        # poll_and_apply below will only ever graft a topic into our own
        # board list if it's in this set, so merely sharing a relay_root
        # with someone never exposes their boards to us - we still need to
        # be handed a token first, same as a live join.
        if not isinstance(topic_uuids, list) or not topic_uuids:
            return SessionResult("error", reason="no topic_uuids given")
        desired = set(self._state.setdefault("desired", []))
        desired.update(str(uuid) for uuid in topic_uuids)
        self._state["desired"] = sorted(desired)
        self._save_state()
        return SessionResult("ok", value=topic_uuids)

    def publish_due_topics(self) -> list[str]:
        if not self.storage:
            return []
        published = []
        for topic_uuid in self.relay_topic_uuids():
            current_hash = self.session.node_state_hash(topic_uuid)
            if current_hash is None:
                continue
            if self._state["published"].get(topic_uuid) == current_hash:
                continue
            payload = self.session.get_subtree(topic_uuid)
            if payload is None:
                continue
            self.storage.write_snapshot(topic_uuid, self.identity, current_hash, payload)
            self._state["published"][topic_uuid] = current_hash
            published.append(topic_uuid)
        if published:
            self._save_state()
        return published

    def poll_and_apply(self) -> list[tuple[str, str]]:
        # Discovers topics from what's actually in the relay, not from
        # relay_topic_uuids() (this session's own local boards) - otherwise
        # a peer who's never seen a board before could never learn about it
        # this way, which defeats the point of a standalone relay path.
        if not self.storage:
            return []
        applied = []
        for topic_uuid in self.storage.list_topics():
            for peer_id in self.storage.list_peers(topic_uuid):
                if peer_id == self.identity:
                    continue
                head = self.storage.read_head(topic_uuid, peer_id)
                if not head:
                    continue
                state_hash = head.get("hash")
                if not state_hash:
                    continue
                last_seen = self._state["applied"].get(topic_uuid, {}).get(peer_id)
                # A token can arrive after this exact hash was already
                # cached (e.g. the topic was seen - and skipped, since it
                # wasn't desired yet - on an earlier poll). Bookkeeping alone
                # would then permanently skip it as "nothing changed," even
                # though grafting is still pending - so an unchanged hash
                # only short-circuits when there's nothing left to do.
                wants_graft = (
                    topic_uuid in self._state.get("desired", [])
                    and self.session.protocol.index.get(topic_uuid) is None
                )
                if state_hash == last_seen and not wants_graft:
                    continue
                payload = self.storage.read_snapshot(topic_uuid, peer_id, state_hash)
                if not payload:
                    continue
                subtree = PRSPNode.from_dict(payload["subtree"])
                peer_addr = f"relay:{peer_id}"
                # Registering peer_topic_sets (not add_peer - see
                # Session.note_relay_peer_topic) is what lets kanban_logic's
                # auto-adopt recognize this cache as discussing the topic;
                # without it, the apply below would be invisible to adoption.
                self.session.note_relay_peer_topic(peer_addr, topic_uuid)
                is_new_board = wants_graft and subtree.data.get("type") == "kanban_board"
                if is_new_board:
                    # Graft the original object as our own board; cache a
                    # deep copy separately so the two don't become the same
                    # object (which would make future divergence checks
                    # between "our board" and "the peer's cache" trivially
                    # always agree) - same split the live-join accept path
                    # already uses (kanban_logic.py's join_discussion).
                    self.kanban.accept_relay_board(subtree)
                    self.session.apply_peer_subtree(
                        peer_addr, copy.deepcopy(subtree), payload.get("parent_uuid"),
                    )
                else:
                    self.session.apply_peer_subtree(
                        peer_addr, subtree, payload.get("parent_uuid"),
                    )
                self._state["applied"].setdefault(topic_uuid, {})[peer_id] = state_hash
                applied.append((topic_uuid, peer_id))
        if applied:
            self._save_state()
        return applied

    def status_payload(self) -> dict:
        # Union of locally-owned boards (which we publish) and any topic
        # we've ever applied something from (which may not be one of our
        # own boards at all - e.g. a board only known via a peer's relay
        # snapshot) - reporting only the former hid exactly the kind of
        # state-file collision bug this diagnostic is meant to catch.
        topic_uuids = set(self.relay_topic_uuids()) | set(self._state["applied"])
        return {
            "configured": self.storage is not None,
            "identity": self.identity,
            "root": str(self.storage.root) if self.storage else None,
            "state_file": self._state_path,
            "desired": list(self._state.get("desired", [])),
            "topics": {
                topic_uuid: {
                    "published_hash": self._state["published"].get(topic_uuid),
                    "applied": self._state["applied"].get(topic_uuid, {}),
                }
                for topic_uuid in sorted(topic_uuids)
            },
        }

    def _load_state(self) -> dict[str, Any]:
        path = Path(self._state_path)
        if path.is_file():
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("published", {})
            data.setdefault("applied", {})
            data.setdefault("desired", [])
            return data
        return {"published": {}, "applied": {}, "desired": []}

    def _save_state(self) -> None:
        path = Path(self._state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid_mod.uuid4().hex}.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(self._state, f, sort_keys=True, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)


def create_logic(session: Session, config: dict) -> RelayLogic:
    logic = RelayLogic(session, config)
    config["_relay_logic_instance"] = logic
    return logic


def channel_descriptor(runtime, config: dict) -> dict | None:
    logic = config.get("_relay_logic_instance")
    return logic.channel_descriptor() if logic else None


def build_routes(logic: RelayLogic, runtime, config: dict) -> list[Route]:
    async def api_relay_status(request: Request):
        return JSONResponse(logic.status_payload())

    return [
        Route("/api/relay/status", api_relay_status),
    ]
