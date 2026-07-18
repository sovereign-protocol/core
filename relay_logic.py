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
    delete_topic(topic_uuid) -> SessionResult
      Storage/bookkeeping cleanup only (POST /api/relay/delete_topic) -
      never touches a peer's own already-adopted local board, if any.
    write_presence() -> None
      Heartbeat, called once per poll tick (app_server.py's
      relay_poll_loop) regardless of whether any topic content changed -
      head.json's own "updated_at" is ambiguous between "fine, nothing to
      publish" and "stopped running," this isn't.
    peer_liveness(peer_id) -> dict
      {"state": "alive"|"stale"|"unknown", "last_seen_seconds_ago": float,
      "threshold_seconds": float, "peer_poll_interval_seconds": float}.
      Compares the peer's presence file's server-side mtime against our
      own last write_presence() mtime - both readings come from the same
      storage backend's clock, so this is skew-free between machines with
      no explicit clock offset ever computed. "stale" means no heartbeat
      within a margin over both sides' poll intervals, not a live
      reachability check (relay has no such thing) - closer to a chat
      app's "last seen" than an online/offline dot.
    known_peer_identities() -> list[str]
      Peers we've actually applied something from, per our own bookkeeping.

Used API:
  kanban_logic.KanbanLogic (boards/user_profile only), protocol.PRSPNode,
  session.Session, relay_storage.LocalFolderRelayStorage,
  relay_storage.SftpRelayStorage.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
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

from relay_storage import LocalFolderRelayStorage, SftpRelayStorage, now_iso


def _storage_fingerprint(config: dict) -> str:
    backend = config.get("relay_backend", "local")
    if backend == "sftp":
        host = config.get("relay_sftp_host") or ""
        if "://" in host:
            host = host.split("://", 1)[1]
        parts = [
            "sftp", host, str(config.get("relay_sftp_port", 22)),
            config.get("relay_sftp_username") or "",
            config.get("relay_sftp_root", "/"),
        ]
    else:
        parts = ["local", config.get("relay_root") or ""]
    return "|".join(parts)


def default_relay_state_file(config: dict, identity: str) -> str:
    # Keyed by relay_identity AND a fingerprint of which storage
    # backend/location this identity is actually talking to - not just
    # identity alone. Otherwise switching backends (or root path) while
    # keeping the same identity string silently inherits stale bookkeeping
    # from a totally different, unrelated storage location - found live:
    # an SFTP-backed instance using identity "A" reused a local-folder
    # test's leftover "already published/applied" state, making it look
    # like syncing had already succeeded against a server it had never
    # actually contacted.
    app_name = str(config.get("app_module") or "app").replace(".", "_")
    safe_identity = re.sub(r"[^A-Za-z0-9_-]+", "_", identity).strip("_") or "default"
    fingerprint = hashlib.sha256(_storage_fingerprint(config).encode("utf-8")).hexdigest()[:12]
    return str(
        Path(__file__).with_name("data")
        / f"relay_state_{app_name}_{safe_identity}_{fingerprint}.json"
    )


PRESENCE_LIVENESS_MARGIN = 2.0
MIN_RELAY_POLL_SECONDS = 1.0
MAX_RELAY_POLL_SECONDS = 300.0


class RelayLogic:
    def __init__(self, session: Session, config: dict):
        self.session = session
        self.kanban = KanbanLogic(session, config)
        self.identity = config.get("relay_identity") or self.kanban.user_profile().uuid
        self.storage = self._build_storage(config)
        adopted_descriptor = None
        if self.storage is None:
            adopted_descriptor = self.session.app_metadata.get(
                "relay_adopted_storage_descriptor",
            )
            self.storage = self._storage_from_descriptor(adopted_descriptor)
        self.poll_interval_seconds = self._normalize_poll_interval(
            config.get(
                "relay_poll_interval_seconds",
                (adopted_descriptor or {}).get("poll_interval_seconds", 3),
            ),
            3.0,
        )
        # Set on every write_presence() call to the storage backend's own
        # server-side mtime for our own just-written heartbeat - this is
        # "what does the server consider *now*", used as the reference
        # point for peer_liveness() instead of this process's own wall
        # clock. Comparing two server-reported mtimes (ours and a peer's)
        # cancels out clock skew between machines entirely, with no
        # explicit offset calculation needed - see peer_liveness().
        self._own_presence_mtime: float | None = None
        # An explicit pin (tests, or a user who set it) overrides the
        # location-derived default - kept so adopt_storage_from_descriptor
        # honors the pin instead of recomputing a data/ path.
        self._configured_state_file = config.get("relay_state_file")
        state_config = self._config_from_storage(self.storage) if self.storage else config
        self._state_path = self._configured_state_file or default_relay_state_file(
            state_config, self.identity,
        )
        self._state = self._load_state()
        # Re-mark any previously-shared boards as active discussions, so a
        # restart doesn't silently drop the issuer's auto-adopt for them
        # (see _activate_shared_boards).
        self._activate_shared_boards()

    @staticmethod
    def _normalize_poll_interval(value: Any, fallback: float) -> float:
        try:
            interval = float(value)
        except (TypeError, ValueError):
            interval = fallback
        return min(MAX_RELAY_POLL_SECONDS, max(MIN_RELAY_POLL_SECONDS, interval))

    def adopt_poll_interval_from_descriptor(self, descriptor: dict) -> float:
        self.poll_interval_seconds = self._normalize_poll_interval(
            descriptor.get("poll_interval_seconds"), self.poll_interval_seconds,
        )
        return self.poll_interval_seconds

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

    @staticmethod
    def _storage_from_descriptor(descriptor: dict):
        # Mirror of _build_storage, but reading a token's channel
        # descriptor instead of the flat local config - this is how a pure
        # accepter builds storage pointed at the inviter's advertised
        # location + credentials, with no relay config of its own.
        if not isinstance(descriptor, dict):
            return None
        channel_type = descriptor.get("type")
        if channel_type == "relay":
            root = descriptor.get("root")
            return LocalFolderRelayStorage(root) if root else None
        if channel_type == "sftp":
            host = descriptor.get("host")
            if not host:
                return None
            if "://" in host:
                host = host.split("://", 1)[1]
            return SftpRelayStorage(
                host=host,
                port=int(descriptor.get("port", 22)),
                username=descriptor.get("username"),
                remote_root=descriptor.get("root", "/"),
                password=descriptor.get("password"),
            )
        return None

    def adopt_storage_from_descriptor(self, descriptor: dict) -> bool:
        # Accepter entry point: build our single relay storage from a
        # token's advertised location when we don't already have one, so a
        # client with no relay server of its own can still ride an
        # inviter's token into the inviter's space. Single-storage model -
        # if we already host a relay, that one is kept (returns False).
        if self.storage is not None:
            return False
        storage = self._storage_from_descriptor(descriptor)
        if storage is None:
            return False
        self._install_adopted_storage(storage, descriptor)
        return True

    def _install_adopted_storage(self, storage, descriptor: dict | None = None) -> None:
        self.storage = storage
        if descriptor:
            # A token-provisioned accepter has no local relay config to load
            # on its next start. Keep the accepted descriptor with the
            # already-persisted session metadata so storage, credentials,
            # and the host's poll interval survive restart.
            self.session.app_metadata["relay_adopted_storage_descriptor"] = dict(descriptor)
        # Re-key bookkeeping to the real location. The boot-time
        # _state_path was fingerprinted from empty config (no storage);
        # published/applied hashes are meaningless against a different
        # server, so recompute the path from the adopted location and
        # reload - same identity+location fingerprint guard that keeps one
        # identity from inheriting stale bookkeeping across storages.
        pseudo_config = self._config_from_storage(storage)
        self._state_path = self._configured_state_file or default_relay_state_file(
            pseudo_config, self.identity,
        )
        self._state = self._load_state()

    def ensure_usable_storage(self, descriptor: dict) -> SessionResult:
        """Probe relay access and atomically adopt token storage when needed."""
        candidate = self.storage
        should_adopt = candidate is None
        if candidate is None:
            candidate = self._storage_from_descriptor(descriptor)
        if candidate is None:
            return SessionResult("error", reason="relay descriptor is not usable")
        try:
            verify = getattr(candidate, "verify_access", None)
            if verify:
                verify()
            else:
                candidate.list_topics()
        except Exception as exc:
            return SessionResult(
                "error",
                reason=f"relay unavailable: {type(exc).__name__}: {exc}",
            )
        if should_adopt:
            # A failed token must not leave unusable storage installed and
            # prevent a later corrected token from being adopted.
            self._install_adopted_storage(candidate, descriptor)
        return SessionResult("ok")

    @staticmethod
    def _config_from_storage(storage) -> dict:
        # A minimal config shaped just enough for default_relay_state_file's
        # fingerprint (_storage_fingerprint) to key off the adopted
        # location - not a full config, only the fields the fingerprint
        # reads.
        if isinstance(storage, SftpRelayStorage):
            return {
                "relay_backend": "sftp",
                "relay_sftp_host": storage.host,
                "relay_sftp_port": storage.port,
                "relay_sftp_username": storage.username,
                "relay_sftp_root": storage.root,
            }
        return {"relay_backend": "local", "relay_root": str(storage.root)}

    def relay_topic_uuids(self) -> list[str]:
        # Includes our own identity node alongside owned boards: identity is
        # otherwise only ever delivered once, inline in a connect token at
        # accept time (Session.apply_peer_identity_snapshot) - a later
        # display-name/picture edit would never reach an already-connected
        # relay peer without this, since relay has no other identity-sync
        # path. poll_and_apply already caches any non-"kanban_board" topic
        # via apply_peer_subtree without attempting to graft it, so no
        # change is needed on the receiving side.
        return [board.uuid for board in self.kanban.boards()] + [self.session.identity.uuid]

    def channel_descriptor(self) -> dict | None:
        if not self.storage:
            return None
        if isinstance(self.storage, SftpRelayStorage):
            # Deliberately carries the SFTP username + password so an
            # accepter with no relay config of its own can build storage
            # straight from the token (adopt_storage_from_descriptor) and
            # publish into this inviter's space - the whole point of a
            # token over a shared config file. This reverses the earlier
            # "descriptor never carries credentials" rule: safe only
            # because the relay account is chroot-jailed to its root and
            # the token is a bearer credential shared over a trusted
            # channel (DESIGN_IDENTITY_AND_TRANSPORT.md §1.6). The private
            # key path/passphrase are never included - we embed a
            # password, never a key.
            return {
                "type": "sftp",
                "version": 1,
                "host": self.storage.host,
                "port": self.storage.port,
                "root": self.storage.root,
                "username": self.storage.username,
                "password": self.storage.password,
                "identity": self.identity,
                "poll_interval_seconds": self.poll_interval_seconds,
            }
        return {
            "type": "relay",
            "version": 1,
            "root": str(self.storage.root),
            "identity": self.identity,
            "poll_interval_seconds": self.poll_interval_seconds,
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

    def mark_topics_shared(self, topic_uuids: list[str]) -> SessionResult:
        # The issuer-side counterpart of mark_topics_desired: recording that
        # we've offered these topics to someone via a relay-bearing connect
        # token. This is the only signal available before an accepter shows
        # up (a drop-box relay has no back-channel announcing acceptance),
        # and it's what arms has_active_relationship() so the issuer starts
        # publishing - otherwise the accepter could never graft a board the
        # issuer never got around to publishing. Does not affect what/where
        # publish_due_topics writes; only whether the loop runs at all.
        if not isinstance(topic_uuids, list) or not topic_uuids:
            return SessionResult("error", reason="no topic_uuids given")
        shared = set(self._state.setdefault("shared", []))
        shared.update(str(uuid) for uuid in topic_uuids)
        self._state["shared"] = sorted(shared)
        self._save_state()
        self._activate_shared_boards()
        return SessionResult("ok", value=topic_uuids)

    def unmark_topics_shared(self, topic_uuids: list[str]) -> SessionResult:
        # The unshare counterpart (review R-3): without this, `shared` only
        # ever grew - unsharing a board never stopped relay publishing it,
        # and has_active_relationship() stayed armed forever once anything
        # had ever been shared.
        if not isinstance(topic_uuids, list) or not topic_uuids:
            return SessionResult("error", reason="no topic_uuids given")
        remove = {str(uuid) for uuid in topic_uuids}
        self._state["shared"] = [
            topic for topic in self._state.get("shared", [])
            if topic not in remove
        ]
        self._save_state()
        return SessionResult("ok", value=sorted(remove))

    def _activate_shared_boards(self) -> None:
        # A board this session has shared via a relay token is one it is
        # actively discussing - the relay analog of the /p2p/join that
        # marks a topic active on the HTTP path (Session.handle_join).
        # Without this the *issuer's own* board never enters
        # active_topic_uuids, so kanban's auto-adopt (gated on
        # _is_active_discussion_node) never runs for incoming relay changes
        # to it - they surface as a diff with no Adopt button (auto-adopt
        # is expected to handle it) yet nothing ever adopts. Caught live:
        # A shared a board over relay, B edited a card, and A was stuck
        # showing the change with no way to accept it. Idempotent; also
        # called from __init__ so a restart re-activates shared boards
        # (active_topic_uuids is persisted, but a board shared before this
        # fix existed would not have been recorded active).
        for topic in self._state.get("shared", []):
            node = self.session.protocol.index.get(topic)
            if node is not None and node.data.get("type") == "kanban_board":
                self.session.start_discussion(topic)

    def has_active_relationship(self) -> bool:
        # The relay loop's gate: is there any reason to publish/poll/write
        # presence at all? True once this session has issued a relay token
        # (shared), accepted one (desired), or already has a relay peer
        # registered - the concrete "an active connection exists" predicate.
        # A fresh boot with none of these leaves the loop fully idle (no
        # files written for no one). Requires storage: nothing to do without
        # a place to read/write.
        if not self.storage:
            return False
        if self._state.get("shared") or self._state.get("desired"):
            return True
        return any(
            addr.startswith("relay:")
            for addr in self.session.peer_topic_sets
        )

    def write_presence(self) -> None:
        # A heartbeat, written every poll tick regardless of whether any
        # topic content changed - distinct from head.json's "updated_at"
        # (which only moves when content changes, so silence there is
        # ambiguous between "peer is fine, nothing to publish" and "peer
        # stopped running"). Also refreshes _own_presence_mtime, the
        # reference point peer_liveness() compares every peer's own
        # heartbeat mtime against.
        if not self.storage:
            return
        payload = {
            "identity": self.identity,
            "updated_at": now_iso(),
            "poll_interval_seconds": self.poll_interval_seconds,
        }
        mtime = self.storage.write_presence(self.identity, payload)
        if mtime is not None:
            self._own_presence_mtime = mtime

    def known_peer_identities(self) -> list[str]:
        # Peers we've actually exchanged something with, per our own applied
        # bookkeeping - a reasonable "who should I even be checking
        # liveness for" set, without needing a separate identity registry.
        return sorted({
            peer_id
            for peers in self._state.get("applied", {}).values()
            for peer_id in peers
        })

    def peer_liveness(self, peer_id: str) -> dict:
        if not self.storage or self._own_presence_mtime is None:
            return {"state": "unknown"}
        content, mtime = self.storage.read_presence_with_mtime(peer_id)
        if content is None or mtime is None:
            return {"state": "unknown"}
        peer_interval = float(content.get("poll_interval_seconds") or self.poll_interval_seconds)
        # Both mtimes come from the same server clock, so this difference
        # is skew-free regardless of how far apart the two machines' own
        # wall clocks actually are - no explicit offset ever computed or
        # stored. A negative distance (their heartbeat is newer than our
        # own last one) just means they're doing great; only a distance
        # past the margin indicates they've gone quiet.
        distance = self._own_presence_mtime - mtime
        threshold = PRESENCE_LIVENESS_MARGIN * (self.poll_interval_seconds + peer_interval)
        return {
            "state": "alive" if distance <= threshold else "stale",
            "last_seen_seconds_ago": round(distance, 3),
            "threshold_seconds": round(threshold, 3),
            "peer_poll_interval_seconds": peer_interval,
        }

    def publish_due_topics(self) -> list[str]:
        if not self.storage:
            return []
        published = []
        for topic_uuid in self.relay_topic_uuids():
            current_hash = self.session.node_state_hash(topic_uuid)
            if current_hash is None:
                continue
            observed = self._state.get("observed", {}).get(topic_uuid, {})
            observed_digest = hashlib.sha256(
                json.dumps(observed, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:20]
            if (self._state["published"].get(topic_uuid) == current_hash
                    and self._state["published_observations"].get(topic_uuid)
                    == observed_digest):
                continue
            payload = self.session.get_subtree(topic_uuid)
            if payload is None:
                continue
            payload["_relay_observed"] = observed
            self.storage.write_snapshot(topic_uuid, self.identity, current_hash, payload)
            self._state["published"][topic_uuid] = current_hash
            self._state["published_observations"][topic_uuid] = observed_digest
            published.append(topic_uuid)
        if published:
            self._save_state()
        return published

    def _is_redundant_relay_peer(self, peer_addr: str) -> bool:
        # True once peer_addr's identity is *currently* reachable through
        # a live non-relay peer. Pure registry lookup - no content
        # walking, so it's cheap enough to just re-run every poll. The
        # registry entry for peer_addr only exists once its identity topic
        # has been seen at least once (identity discovery can lag a poll
        # behind), so a brand-new relay peer is never mistaken for
        # redundant before there's evidence either way. The members check
        # makes this self-correcting: if the direct peer is later removed,
        # relay resumes on the next poll instead of staying suppressed
        # forever.
        identity_key = self.session.peer_identity_key.get(peer_addr)
        if not identity_key:
            return False
        return any(
            addr != peer_addr
            and not addr.startswith("relay:")
            and addr in self.session.members
            for addr in self.session.addresses_for_identity(identity_key)
        )

    def poll_and_apply(self) -> list[tuple[str, str]]:
        # Discovers topics from what's actually in the relay, not from
        # relay_topic_uuids() (this session's own local boards) - otherwise
        # a peer who's never seen a board before could never learn about it
        # this way, which defeats the point of a standalone relay path.
        if not self.storage:
            return []
        applied: set[tuple[str, str]] = set()
        for topic_uuid in self.storage.list_topics():
            for peer_id in self.storage.list_peers(topic_uuid):
                if peer_id == self.identity:
                    continue
                peer_addr = f"relay:{peer_id}"
                if self._is_redundant_relay_peer(peer_addr):
                    # This identity is already reachable through a live,
                    # preferred (non-relay) channel - the ongoing poll
                    # loop runs independently of connect-time channel
                    # selection, so without this it would keep
                    # re-registering a second, relay-sourced view of a
                    # peer we already have a better connection to (the
                    # exact "two channels for one peer" state connect-time
                    # selection exists to prevent). remove_peer clears any
                    # content an earlier cycle registered before this
                    # became detectable; the identity registry entry the
                    # check reads survives it (knowledge, not
                    # registration - see Session.remove_peer), so the
                    # suppression holds on every later poll without any
                    # bookkeeping here.
                    self.session.remove_peer(peer_addr)
                    continue
                head = self.storage.read_head(topic_uuid, peer_id)
                if not head:
                    continue
                observed_for_me = (head.get("observed") or {}).get(self.identity, {})
                if self.session.record_peer_observations(peer_addr, observed_for_me):
                    applied.add((topic_uuid, peer_id))
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
                self._state["observed"].setdefault(topic_uuid, {})[peer_id] = (
                    self.session.node_revision_map(subtree)
                )
                self._state["applied"].setdefault(topic_uuid, {})[peer_id] = state_hash
                applied.add((topic_uuid, peer_id))
        if applied:
            self._save_state()
        return sorted(applied)

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
            "presence": {
                peer_id: self.peer_liveness(peer_id)
                for peer_id in self.known_peer_identities()
            },
            "topics": {
                topic_uuid: {
                    "published_hash": self._state["published"].get(topic_uuid),
                    "applied": self._state["applied"].get(topic_uuid, {}),
                }
                for topic_uuid in sorted(topic_uuids)
            },
        }

    def delete_topic(self, topic_uuid: str) -> SessionResult:
        # Purely a storage/bookkeeping cleanup - never touches whatever a
        # peer may have already grafted into their own local board list
        # from this topic (that's kanban_logic's own board-delete, a
        # separate, app-level decision this has no business making).
        if not self.storage:
            return SessionResult("error", reason="relay not configured")
        self.storage.delete_topic(topic_uuid)
        self._state["published"].pop(topic_uuid, None)
        self._state["published_observations"].pop(topic_uuid, None)
        self._state["observed"].pop(topic_uuid, None)
        self._state["applied"].pop(topic_uuid, None)
        self._state["desired"] = [t for t in self._state.get("desired", []) if t != topic_uuid]
        self._state["shared"] = [t for t in self._state.get("shared", []) if t != topic_uuid]
        self._save_state()
        return SessionResult("ok", value=topic_uuid)

    def _load_state(self) -> dict[str, Any]:
        path = Path(self._state_path)
        if path.is_file():
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("published", {})
            data.setdefault("published_observations", {})
            data.setdefault("observed", {})
            data.setdefault("desired", [])
            data.setdefault("shared", [])
            # `applied` is deliberately NOT restored (see _save_state) - it
            # always starts empty so a restart re-fetches and re-caches
            # every peer's content.
            data["applied"] = {}
            return data
        return {
            "published": {}, "published_observations": {}, "observed": {},
            "applied": {}, "desired": [], "shared": [],
        }

    def _save_state(self) -> None:
        # `applied` is deliberately never persisted. It tracks "I've already
        # applied peer hash X into peer_perspectives" - but peer_perspectives
        # (the cache) is itself in-memory only, wiped on restart. Persisting
        # `applied` while the cache it describes does not persist means a
        # restart believes it's fully synced with a peer while holding an
        # empty cache, so poll_and_apply's "hash unchanged since last
        # applied" check skips the very re-fetch that would repopulate it -
        # the peer silently vanishes until it happens to change something.
        # Caught live: A restarted and lost sight of B entirely. `published`
        # DOES persist (it tracks snapshots that stay on the server, which
        # does persist); `desired`/`shared` persist (durable consent/intent).
        # Same lesson as not persisting Session.peer_sync_state.
        persisted = {k: v for k, v in self._state.items() if k != "applied"}
        path = Path(self._state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid_mod.uuid4().hex}.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(persisted, f, sort_keys=True, indent=2)
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
        # SFTP liveness checks are blocking and share the serialized storage
        # connection with the relay poll worker. Never block the event loop.
        payload = await asyncio.to_thread(logic.status_payload)
        return JSONResponse(payload)

    async def api_relay_delete_topic(request: Request):
        data = await request.json()
        result = logic.delete_topic(data.get("topic_uuid", ""))
        if result.status != "ok":
            return JSONResponse({"status": "error", "reason": result.reason}, status_code=400)
        return JSONResponse({"status": "ok", "topic_uuid": result.value})

    return [
        Route("/api/relay/status", api_relay_status),
        Route("/api/relay/delete_topic", api_relay_delete_topic, methods=["POST"]),
    ]
