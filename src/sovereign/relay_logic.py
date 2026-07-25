"""
File-mailbox relay sync.

Functionality:
  Store-and-forward sync for peers who are never online at the same time,
  and/or not directly reachable (no inbound NAT traversal needed). Each peer
  identity publishes its current view of every registered application topic into a shared
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
      relay_sftp_private_key_passphrase). UI-created SFTP targets persist
      their password in the local session envelope; environment-variable
      and password-file lookup are intentionally not used.

  This is deliberately not built on Session.add_peer/pending_sync_effects/
  HttpTransportAdapter - those assume every registered member is directly
  HTTP-reachable, which is exactly the constraint this feature removes. It
  only calls two existing Session methods: get_subtree (to publish) and
  apply_peer_subtree (to apply what's downloaded) - the same two calls the
  live HTTP transport already makes, so a relay identity is reconciled
  identically to a real HTTP peer. It never adopts, judges, or merges on its
  own account - it is pure store-and-forward.

  Applications register their topic root types, local-topic enumerator and
  invitation handler with Session.shared_topics. Relay publishes those roots
  without importing application code. *Polling*, by contrast, is
  driven by whatever topics actually exist in the relay storage, not by
  this session's own topic list - otherwise a peer who's never seen a given
  topic before could never learn about it this way, which would defeat the
  point of a standalone (no prior direct P2P join) relay path.

  Bookkeeping (which hash we last published/applied per topic/peer) is
  local-only sync state, kept in its own JSON file next to the session's
  own storage file - never written into S-Protocol data, since that would make it
  content needing its own sync, which defeats the purpose.

Offered API:
  RelayManager(session, config, blob_store)
  RelayManager.channel_descriptor()
    Advertises relay as a connectable channel for Core invitations, if
    configured - {"type": "relay", "descriptor_version": 1, "root": ...,
    "identity": ...}.
  RelayLogic
    relay_topic_uuids() -> list[str]
    publish_due_topics() -> list[str]
    poll_and_apply() -> list[tuple[str, str]]
    status_payload() -> dict
    channel_descriptor() -> dict | None
    mark_topics_desired(topic_uuids) -> SessionResult
      Records topic_uuids as "desired" - the consent step that lets
      poll_and_apply graft a not-yet-locally-known topic into our own tree
      list the first time it shows up in the relay, instead of merely
      caching it as an (invisible, unowned) peer perspective forever. This
      is how two peers can share a topic via relay alone, with no live HTTP
      join ever required - called by the unified invitation accept flow
      (app_server.py) once it decides the relay channel is usable, same as
      the HTTP channel's own accept path is called for the http channel.
    delete_topic(topic_uuid) -> SessionResult
      Storage/bookkeeping cleanup only (mailbox topic-delete endpoint) -
      never touches a peer's own already-adopted local topic, if any.
    write_presence() -> None
      Heartbeat, called once per poll tick (app_server.py's
      channel_poll_loop) regardless of whether any topic content changed -
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
  protocol.ProtocolNode, session.Session and its shared-topic registry,
  relay_storage.LocalFolderRelayStorage,
  relay_storage.SftpRelayStorage.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import statistics
import threading
import time
import uuid as uuid_mod
from collections import deque
from functools import wraps
from pathlib import Path
from typing import Any

from .blob_store import blob_hex, referenced_blob_ids
from .protocol import ProtocolNode, protocol_node_from_envelope
from .session import Session, SessionResult
from .relay_storage import LocalFolderRelayStorage, SftpRelayStorage, now_iso
from .versions import CHANNEL_DESCRIPTOR_VERSION


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
        Path.cwd() / "data"
        / f"relay_state_{app_name}_{safe_identity}_{fingerprint}.json"
    )


PRESENCE_LIVENESS_MARGIN = 2.0
MIN_RELAY_POLL_SECONDS = 1.0
MAX_RELAY_POLL_SECONDS = 300.0
TIMING_CALIBRATION_SECONDS = 300.0

# Relay polling, token handling, and UI requests can all persist the same
# connection bookkeeping.  Serialize writers by absolute file name, even
# when two RelayLogic objects temporarily refer to that file during startup.
_STATE_SAVE_LOCKS: dict[str, threading.Lock] = {}
_STATE_SAVE_LOCKS_GUARD = threading.Lock()


def _relay_io_locked(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._io_lock:
            return method(self, *args, **kwargs)
    return locked


def _manager_locked(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._manager_lock:
            return method(self, *args, **kwargs)
    return locked


class RelayTiming:
    """Transient timing model for one relay storage location.

    All timestamps used to compare clients stay in the relay server's clock.
    Local wall time is translated only for scheduling and diagnostics.
    """

    def __init__(self, timestamp_resolution_seconds: float = 1.0):
        self.timestamp_resolution_seconds = max(
            0.0, float(timestamp_resolution_seconds),
        )
        self._offset_samples = deque(maxlen=30)
        self._roundtrip_samples = deque(maxlen=30)
        self._cycle_samples = deque(maxlen=30)
        self._peer_presence: dict[str, tuple[float, float]] = {}
        self._last_probe_monotonic: float | None = None
        self._lock = threading.RLock()

    @staticmethod
    def _spread(samples) -> float:
        values = list(samples)
        return max(values) - min(values) if len(values) > 1 else 0.0

    def observe_server_clock(self, started_monotonic: float, started_wall: float,
                             ended_monotonic: float, ended_wall: float,
                             server_mtime: float | None,
                             roundtrip_seconds: float | None = None) -> None:
        if server_mtime is None:
            return
        operation_seconds = max(0.0, ended_monotonic - started_monotonic)
        # An integer SFTP mtime represents an interval one resolution wide;
        # its midpoint is the least biased estimate of the server's clock.
        server_midpoint = float(server_mtime) + self.timestamp_resolution_seconds / 2
        local_midpoint = (started_wall + ended_wall) / 2
        uncertainty = operation_seconds / 2 + self.timestamp_resolution_seconds / 2
        with self._lock:
            self._offset_samples.append((server_midpoint - local_midpoint, uncertainty))
            if roundtrip_seconds is not None:
                self._roundtrip_samples.append(max(0.0, float(roundtrip_seconds)))
                self._last_probe_monotonic = ended_monotonic

    def observe_cycle(self, duration_seconds: float) -> None:
        with self._lock:
            self._cycle_samples.append(max(0.0, float(duration_seconds)))

    def observe_peer_presence(self, peer_id: str, server_mtime: float | None,
                              poll_interval_seconds: float) -> None:
        if not peer_id or server_mtime is None:
            return
        with self._lock:
            self._peer_presence[peer_id] = (
                float(server_mtime), max(0.1, float(poll_interval_seconds)),
            )

    def probe_due(self, now_monotonic: float | None = None) -> bool:
        now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
        with self._lock:
            return (
                self._last_probe_monotonic is None
                or now_monotonic - self._last_probe_monotonic
                >= TIMING_CALIBRATION_SECONDS
            )

    def _summary(self) -> dict:
        with self._lock:
            offsets = list(self._offset_samples)
            roundtrips = list(self._roundtrip_samples)
            cycles = list(self._cycle_samples)
            peers = dict(self._peer_presence)
        offset = statistics.median(value for value, _uncertainty in offsets) if offsets else None
        uncertainty = max(
            (min(item[1] for item in offsets) if offsets else 0.0),
            self.timestamp_resolution_seconds / 2,
        )
        roundtrip = min(roundtrips) if roundtrips else None
        roundtrip_jitter = self._spread(roundtrips)
        relay_cycle = statistics.median(cycles) if cycles else None
        cycle_jitter = self._spread(cycles)
        return {
            "offset": offset,
            "uncertainty": uncertainty,
            "roundtrip": roundtrip,
            "roundtrip_jitter": roundtrip_jitter,
            "relay_cycle": relay_cycle,
            "cycle_jitter": cycle_jitter,
            "peers": peers,
            "sample_count": len(roundtrips),
            "cycle_sample_count": len(cycles),
        }

    def server_now(self, local_wall: float | None = None) -> float | None:
        summary = self._summary()
        if summary["offset"] is None:
            return None
        return (time.time() if local_wall is None else local_wall) + summary["offset"]

    def response_check_delay(self, stable_interval_seconds: float,
                             published_server_time: float | None = None,
                             local_wall: float | None = None) -> float:
        """When a response to a just-published revision should be present."""
        stable = max(0.1, float(stable_interval_seconds))
        summary = self._summary()
        if summary["offset"] is None or not summary["peers"]:
            return stable
        server_now = (time.time() if local_wall is None else local_wall) + summary["offset"]
        published_at = (
            server_now if published_server_time is None else published_server_time
        )
        jitter = max(summary["roundtrip_jitter"], summary["cycle_jitter"])
        relay_work = max(
            summary["roundtrip"] or 0.0,
            summary["relay_cycle"] or 0.0,
            0.05,
        )
        response_margin = relay_work + jitter + summary["uncertainty"]
        candidates = []
        for peer_mtime, peer_interval in summary["peers"].values():
            age = server_now - peer_mtime
            stale_after = PRESENCE_LIVENESS_MARGIN * (stable + peer_interval)
            if age > stale_after:
                continue
            if peer_mtime >= published_at - summary["uncertainty"]:
                peer_poll = peer_mtime
            else:
                periods = max(1, math.ceil((published_at - peer_mtime) / peer_interval))
                peer_poll = peer_mtime + periods * peer_interval
            candidates.append(max(0.05, peer_poll + response_margin - server_now))
        return min(candidates) if candidates else stable

    def status_payload(self) -> dict:
        summary = self._summary()

        def milliseconds(value):
            return round(value * 1000, 1) if value is not None else None

        return {
            "calibrated": summary["offset"] is not None,
            "roundtrip_ms": milliseconds(summary["roundtrip"]),
            "roundtrip_jitter_ms": milliseconds(summary["roundtrip_jitter"]),
            "relay_cycle_ms": milliseconds(summary["relay_cycle"]),
            "relay_cycle_jitter_ms": milliseconds(summary["cycle_jitter"]),
            "server_clock_offset_ms": milliseconds(summary["offset"]),
            "clock_uncertainty_ms": milliseconds(summary["uncertainty"]),
            "samples": summary["sample_count"],
            "cycle_samples": summary["cycle_sample_count"],
        }


class RelayLogic:
    def __init__(self, session: Session, config: dict, blob_store=None):
        self.session = session
        self._io_lock = threading.RLock()
        self._session_lock = session.lock
        self.blob_store = blob_store
        self._manager = None
        try:
            lease_seconds = float(config.get("relay_blob_lease_seconds", 300.0))
        except (TypeError, ValueError):
            lease_seconds = 300.0
        self.blob_lease_seconds = max(30.0, lease_seconds)
        self.identity = config.get("relay_identity") or self.session.identity.uuid
        self.storage = self._build_storage(config)
        adopted_descriptor = None
        if self.storage is None:
            adopted_descriptor = self.session.app_metadata.get(
                "relay_adopted_storage_descriptor",
            )
            self.storage = self._storage_from_descriptor(adopted_descriptor)
        self.timing = RelayTiming(
            getattr(self.storage, "mtime_resolution_seconds", 1.0),
        )
        self._last_publish_server_time: float | None = None
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
        # None preserves the legacy implicit-connection behavior (all local
        # topics + broad discovery). RelayManager sets an explicit set for
        # every registered target, including the empty set.
        self._scoped_topic_uuids: set[str] | None = None
        # Re-mark previously shared application topics as active discussions.
        self._activate_shared_topics()

    @staticmethod
    def _normalize_poll_interval(value: Any, fallback: float) -> float:
        try:
            interval = float(value)
        except (TypeError, ValueError):
            interval = fallback
        return min(MAX_RELAY_POLL_SECONDS, max(MIN_RELAY_POLL_SECONDS, interval))

    @_relay_io_locked
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
            # Credentials come from this config only - there is no
            # environment-variable or secret-file lookup, so do not assume
            # one exists. Prefer key authentication:
            # relay_sftp_private_key_path, or neither key nor password, in
            # which case paramiko falls back to the agent and the default
            # ~/.ssh identities. relay_sftp_password is the least safe
            # option because it puts the secret in a file on disk; the
            # repository ignores relay_sftp_*.json for exactly that reason,
            # and C2/O5 keep this whole path experimental.
            return SftpRelayStorage(
                host=host,
                port=int(config.get("relay_sftp_port", 22)),
                username=config.get("relay_sftp_username"),
                remote_root=config.get("relay_sftp_root", "/"),
                password=config.get("relay_sftp_password") or None,
                private_key_path=config.get("relay_sftp_private_key_path"),
                private_key_passphrase=(
                    config.get("relay_sftp_private_key_passphrase") or None
                ),
            )
        root = config.get("relay_root")
        return LocalFolderRelayStorage(root) if root else None

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

    @_relay_io_locked
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
        self.timing = RelayTiming(
            getattr(self.storage, "mtime_resolution_seconds", 1.0),
        )
        self._last_publish_server_time = None
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

    @_relay_io_locked
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

    @_relay_io_locked
    def relay_topic_uuids(self) -> list[str]:
        # The registry applies target assignment only to scoped application
        # topics. Core-owned topics (currently the public profile) opt out and
        # ride every relationship, so this channel names no node type.
        return self.session.shared_topic_uuids(
            self._scoped_topic_uuids,
        )

    @_relay_io_locked
    def set_scoped_topics(self, topic_uuids: set[str] | list[str]) -> None:
        self._scoped_topic_uuids = {str(topic) for topic in topic_uuids if topic}

    @_relay_io_locked
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
                "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
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
            "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
            "root": str(self.storage.root),
            "identity": self.identity,
            "poll_interval_seconds": self.poll_interval_seconds,
        }

    @_relay_io_locked
    def mark_topics_desired(self, topic_uuids: list[str]) -> SessionResult:
        # Recording topic_uuids as "desired" is the consent step:
        # poll_and_apply below will only ever graft a topic into our own
        # local tree if it's in this set, so merely sharing a relay_root
        # with someone never exposes their topics to us - we still need to
        # be handed a token first, same as a live join.
        if not isinstance(topic_uuids, list) or not topic_uuids:
            return SessionResult("error", reason="no topic_uuids given")
        desired = set(self._state.setdefault("desired", []))
        desired.update(str(uuid) for uuid in topic_uuids)
        self._state["desired"] = sorted(desired)
        self._save_state()
        return SessionResult("ok", value=topic_uuids)

    @_relay_io_locked
    def unmark_topics_desired(self, topic_uuids: list[str]) -> SessionResult:
        if not isinstance(topic_uuids, list) or not topic_uuids:
            return SessionResult("error", reason="no topic_uuids given")
        remove = {str(uuid) for uuid in topic_uuids}
        self._state["desired"] = [
            topic for topic in self._state.get("desired", []) if topic not in remove
        ]
        self._save_state()
        return SessionResult("ok", value=sorted(remove))

    @_relay_io_locked
    def mark_topics_shared(self, topic_uuids: list[str]) -> SessionResult:
        # The issuer-side counterpart of mark_topics_desired: recording that
        # we've offered these topics to someone via a relay-bearing connect
        # token. This is the only signal available before an accepter shows
        # up (a drop-box relay has no back-channel announcing acceptance),
        # and it's what arms has_active_relationship() so the issuer starts
        # publishing - otherwise the accepter could never graft a topic the
        # issuer never got around to publishing. Does not affect what/where
        # publish_due_topics writes; only whether the loop runs at all.
        if not isinstance(topic_uuids, list) or not topic_uuids:
            return SessionResult("error", reason="no topic_uuids given")
        shared = set(self._state.setdefault("shared", []))
        shared.update(str(uuid) for uuid in topic_uuids)
        self._state["shared"] = sorted(shared)
        self._save_state()
        self._activate_shared_topics()
        return SessionResult("ok", value=topic_uuids)

    @_relay_io_locked
    def unmark_topics_shared(self, topic_uuids: list[str]) -> SessionResult:
        # The unshare counterpart (review R-3): without this, `shared` only
        # ever grew - unsharing a topic never stopped relay publishing it,
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

    def _activate_shared_topics(self) -> None:
        # Relay token issuance is the relay equivalent of a direct join: the
        # application topic becomes an active discussion on the issuer too.
        for topic in self._state.get("shared", []):
            node = self.session.protocol.index.get(topic)
            if node is not None and self.session.supports_shared_topic(node):
                self.session.start_discussion(topic)

    @_relay_io_locked
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
        active_desired = set(self._state.get("desired", [])) - set(
            self._state.get("identity_topics", [])
        )
        if self._state.get("shared") or active_desired:
            return True
        with self._session_lock:
            if self._scoped_topic_uuids is None:
                return any(
                    addr.startswith("relay:")
                    for addr in self.session.peer_topic_sets
                )
            relevant = (
                self._scoped_topic_uuids
                | set(self._state.get("shared", []))
                | active_desired
            )
            return any(
                addr.startswith("relay:") and bool(relevant & set(topics))
                for addr, topics in self.session.peer_topic_sets.items()
            )

    @_relay_io_locked
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
            # Scoped polling deliberately does not enumerate every topic on
            # a shared target. Carry the public profile in the heartbeat so
            # a peer discovered on an assigned topic can still be named
            # without scanning that peer's separate identity topic.
            "profile": self.session.identity.to_dict(),
            "updated_at": now_iso(),
            "poll_interval_seconds": self.poll_interval_seconds,
        }
        started_monotonic = time.monotonic()
        started_wall = time.time()
        mtime = self.storage.write_presence(self.identity, payload)
        ended_monotonic = time.monotonic()
        ended_wall = time.time()
        self.timing.observe_server_clock(
            started_monotonic, started_wall,
            ended_monotonic, ended_wall,
            mtime,
        )
        if mtime is not None:
            self._own_presence_mtime = mtime

    @_relay_io_locked
    def calibrate_timing(self, samples: int = 1) -> dict:
        if not self.storage:
            return self.timing.status_payload()
        probe = getattr(self.storage, "timing_probe", None)
        if not probe:
            return self.timing.status_payload()
        for _ in range(max(1, int(samples))):
            started_monotonic = time.monotonic()
            started_wall = time.time()
            server_mtime, roundtrip = probe()
            ended_monotonic = time.monotonic()
            ended_wall = time.time()
            self.timing.observe_server_clock(
                started_monotonic, started_wall,
                ended_monotonic, ended_wall,
                server_mtime, roundtrip,
            )
        return self.timing.status_payload()

    @_relay_io_locked
    def calibrate_timing_if_due(self) -> None:
        if self.timing.probe_due():
            self.calibrate_timing(1)

    @_relay_io_locked
    def record_cycle_duration(self, duration_seconds: float) -> None:
        self.timing.observe_cycle(duration_seconds)

    @_relay_io_locked
    def response_check_delay(self) -> float:
        return self.timing.response_check_delay(
            self.poll_interval_seconds,
            self._last_publish_server_time,
        )

    @_relay_io_locked
    def known_peer_identities(self) -> list[str]:
        # Peers we've actually exchanged something with, per our own applied
        # bookkeeping - a reasonable "who should I even be checking
        # liveness for" set, without needing a separate identity registry.
        return sorted({
            peer_id
            for peers in self._state.get("applied", {}).values()
            for peer_id in peers
        })

    @_relay_io_locked
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

    @_relay_io_locked
    def publish_due_topics(self) -> list[str]:
        if not self.storage:
            return []
        published = []
        for topic_uuid in self.relay_topic_uuids():
            # Reads under the shared session lock: a sibling connection's
            # poll_and_apply may be grafting a topic into the same tree
            # concurrently (the channel poll tick runs connection I/O in parallel),
            # and an unlocked walk here could observe a half-grafted subtree
            # or a dict mutated mid-iteration.
            with self._session_lock:
                current_hash = self.session.node_state_hash(topic_uuid)
            if current_hash is None:
                continue
            observed = self._state.get("observed", {}).get(topic_uuid, {})
            observed_publications = self._state.get(
                "observed_publications", {},
            ).get(topic_uuid, {})
            observed_digest = hashlib.sha256(
                json.dumps(
                    {
                        "node_revisions": observed,
                        "publications": observed_publications,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:20]
            if (self._state["published"].get(topic_uuid) == current_hash
                    and self._state["published_observations"].get(topic_uuid)
                    == observed_digest):
                continue
            # Re-read hash and subtree together so the snapshot we write is
            # the one current_hash actually names (a concurrent apply between
            # the two reads would otherwise mismatch head hash and payload).
            with self._session_lock:
                current_hash = self.session.node_state_hash(topic_uuid)
                payload = self.session.get_subtree(topic_uuid)
            if current_hash is None or payload is None:
                continue
            content_changed = (
                self._state["published"].get(topic_uuid) != current_hash
            )
            publication_seq = int(
                self._state.setdefault("publication_seq", {}).get(
                    topic_uuid, 0,
                )
            ) + 1
            ack_publication_seq = int(
                self._state.setdefault("ack_publication_seq", {}).get(
                    topic_uuid, 0,
                )
            )
            if content_changed:
                ack_publication_seq = publication_seq
            # Reserve and persist the sequence before external I/O. A crash
            # may leave a harmless gap, but can never reuse a generation that
            # another process may already have observed on the relay.
            self._state["publication_seq"][topic_uuid] = publication_seq
            self._state["ack_publication_seq"][topic_uuid] = (
                ack_publication_seq
            )
            self._save_state()
            payload["_relay_observed"] = observed
            payload["_relay_observed_publications"] = observed_publications
            payload["_relay_publication_seq"] = publication_seq
            payload["_relay_ack_publication_seq"] = ack_publication_seq
            # Observation-only heads are acknowledgements. Requesting an
            # acknowledgement for those would create an endless ack-of-ack
            # loop, so only semantic topic publications request one.
            payload["_relay_ack_requested"] = content_changed
            blob_ids = referenced_blob_ids(payload.get("subtree"))
            leased: list[str] = []
            publish_ready = True
            try:
                for blob_id in sorted(blob_ids):
                    if self.storage.has_blob(blob_id):
                        continue
                    data = self.blob_store.read_blob(blob_id) if self.blob_store else None
                    if data is None:
                        print(f"[relay] snapshot publish deferred: missing {blob_id}")
                        publish_ready = False
                        break
                    self.storage.write_blob_lease(blob_id, self.identity, {
                        "blob_id": blob_id,
                        "peer": self.identity,
                        "expires_at": (
                            self.timing.server_now() or time.time()
                        ) + self.blob_lease_seconds,
                    })
                    leased.append(blob_id)
                    self.storage.write_blob(blob_id, data)
                if not publish_ready:
                    continue
                # The head is the commit point: every referenced blob is
                # durable before another client can discover the snapshot.
                self.storage.write_snapshot(
                    topic_uuid, self.identity, current_hash, payload, blob_ids=blob_ids,
                )
            finally:
                for blob_id in leased:
                    self.storage.delete_blob_lease(blob_id, self.identity)
            self._state["published"][topic_uuid] = current_hash
            self._state["published_observations"][topic_uuid] = observed_digest
            published.append(topic_uuid)
            self.session.trace_event(
                "relay.publication_published",
                relay_identity=self.identity,
                topic_uuid=topic_uuid,
                state_hash=current_hash,
                publication_seq=publication_seq,
                ack_requested=content_changed,
                ack_publication_seq=ack_publication_seq,
                observed_publications=observed_publications,
            )
        if published:
            self._last_publish_server_time = self.timing.server_now()
            self._save_state()
        return published

    def _cache_blobs(self, blob_ids) -> None:
        # Avatar blobs are small, so the MVP eagerly caches them. Besides
        # making rendering immediate, this makes a client a safe bridge when
        # it republishes an adopted profile through a different relay target.
        # Larger future attachments can add a lazy policy without changing
        # the manifest format. Callers already hold the io lock.
        if self.blob_store is None or not self.storage:
            return
        for blob_id in blob_ids or []:
            try:
                blob_hex(blob_id)
            except ValueError:
                continue
            if self.blob_store.has_blob(blob_id):
                continue
            blob_data = self.storage.read_blob(blob_id)
            if blob_data is not None:
                self.blob_store.write_blob(blob_data)

    @_relay_io_locked
    def read_blob(self, blob_id: str) -> bytes | None:
        if not self.storage:
            return None
        try:
            blob_hex(blob_id)
        except ValueError:
            return None
        return self.storage.read_blob(blob_id)

    @_relay_io_locked
    def blob_gc_report(self) -> dict:
        """Complete relay mark scan; deliberately reports but does not delete."""
        if not self.storage:
            return {
                "existing": [], "referenced": [], "leased": [],
                "candidates": [], "collectible": [],
            }
        referenced: set[str] = set()
        for topic_uuid in self.storage.list_topics():
            for peer_id in self.storage.list_peers(topic_uuid):
                head = self.storage.read_head(topic_uuid, peer_id) or {}
                for field in ("blobs", "previous_blobs"):
                    for blob_id in head.get(field) or []:
                        try:
                            blob_hex(blob_id)
                        except ValueError:
                            continue
                        referenced.add(blob_id)
        now = self.timing.server_now() or time.time()
        leased = set()
        for blob_id, leases in self.storage.list_blob_leases().items():
            for item in leases:
                try:
                    live = float(item.get("expires_at") or 0) > now
                except (AttributeError, TypeError, ValueError):
                    live = False
                if live:
                    leased.add(blob_id)
                    break
        existing = set(self.storage.list_blob_ids())
        unreferenced = existing - referenced - leased
        previous = set(self._state.get("blob_gc_candidates", []))
        collectible = unreferenced & previous
        self._state["blob_gc_candidates"] = sorted(unreferenced)
        self._save_state()
        return {
            "existing": sorted(existing),
            "referenced": sorted(referenced),
            "leased": sorted(leased),
            "candidates": sorted(unreferenced - collectible),
            "collectible": sorted(collectible),
        }

    def _is_redundant_relay_peer(
        self, peer_addr: str, topic_uuid: str,
    ) -> bool:
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
            and self.session.peer_channel_for_topic(addr, topic_uuid)
            not in {None, "mailbox"}
            and (
                self.session.peer_status.get(addr, {}).get("state")
                == "online"
            )
            for addr in self.session.addresses_for_identity(identity_key)
        )

    @_relay_io_locked
    def poll_and_apply(self) -> list[tuple[str, str]]:
        # Discovers topics from what's actually in the relay, not from
        # relay_topic_uuids() (this session's own local topics) - otherwise
        # a peer who's never seen a topic before could never learn about it
        # this way, which defeats the point of a standalone relay path.
        if not self.storage:
            return []
        applied: set[tuple[str, str]] = set()
        bookkeeping_changed = False
        # A peer appears under every topic it shares with us; its presence
        # file is per-identity, not per-topic, so read it once per cycle
        # instead of re-fetching (an SFTP round-trip) for each topic.
        presence_cache: dict[str, tuple[dict | None, float | None]] = {}
        # A peer's profile arrives on every topic it shares with us, but its
        # attachments only need fetching once per cycle.
        profile_blobs_read: set[str] = set()

        def read_presence(peer_id: str) -> tuple[dict | None, float | None]:
            if peer_id not in presence_cache:
                presence_cache[peer_id] = self.storage.read_presence_with_mtime(peer_id)
            return presence_cache[peer_id]

        if self._scoped_topic_uuids is None:
            topic_uuids = self.storage.list_topics()
        else:
            # Explicit targets never enumerate unrelated discussions that
            # happen to share the same SFTP root. Desired topics come from
            # accepted tokens; scoped topics are locally assigned application topics.
            topic_uuids = sorted(
                self._scoped_topic_uuids
                | set(self._state.get("desired", []))
            )
        for topic_uuid in topic_uuids:
            for peer_id in self.storage.list_peers(topic_uuid):
                if peer_id == self.identity:
                    continue
                peer_addr = f"relay:{peer_id}"
                presence, _mtime = read_presence(peer_id)
                peer_interval = float(
                    (presence or {}).get("poll_interval_seconds")
                    or self.poll_interval_seconds
                )
                self.timing.observe_peer_presence(peer_id, _mtime, peer_interval)
                profile = (presence or {}).get("profile")
                if isinstance(profile, dict):
                    with self._session_lock:
                        self.session.apply_peer_identity_snapshot(peer_addr, profile)
                    # The heartbeat carries the profile itself, so its avatar
                    # never passes through the topic-head path that fetches
                    # blobs. Without this the reference syncs but the bytes
                    # never arrive, and the peer renders as bare initials.
                    if peer_id not in profile_blobs_read:
                        profile_blobs_read.add(peer_id)
                        self._cache_blobs(sorted(referenced_blob_ids(profile)))
                with self._session_lock:
                    redundant = self._is_redundant_relay_peer(
                        peer_addr, topic_uuid,
                    )
                    if redundant:
                        self.session.remove_peer(peer_addr)
                if redundant:
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
                    continue
                head = self.storage.read_head(topic_uuid, peer_id)
                if not head:
                    continue
                raw_publication_seq = head.get("publication_seq", 0)
                publication_seq = (
                    raw_publication_seq
                    if (
                        isinstance(raw_publication_seq, int)
                        and not isinstance(raw_publication_seq, bool)
                        and raw_publication_seq >= 0
                    )
                    else 0
                )
                raw_ack_publication_seq = head.get(
                    "ack_publication_seq",
                    publication_seq
                    if head.get("ack_requested", False)
                    else 0,
                )
                ack_publication_seq = (
                    raw_ack_publication_seq
                    if (
                        isinstance(raw_ack_publication_seq, int)
                        and not isinstance(raw_ack_publication_seq, bool)
                        and 0 <= raw_ack_publication_seq <= publication_seq
                    )
                    else 0
                )
                received = self._state.setdefault(
                    "received_publications", {},
                ).setdefault(topic_uuid, {})
                previous_received_seq = int(received.get(peer_id, 0))
                received_changed = publication_seq > previous_received_seq
                if received_changed:
                    received[peer_id] = publication_seq
                    bookkeeping_changed = True

                raw_observed_seq = (
                    (head.get("observed_publications") or {}).get(
                        self.identity, 0,
                    )
                )
                observed_by_peer_seq = (
                    raw_observed_seq
                    if (
                        isinstance(raw_observed_seq, int)
                        and not isinstance(raw_observed_seq, bool)
                        and raw_observed_seq >= 0
                    )
                    else 0
                )
                peer_observed = self._state.setdefault(
                    "peer_observed_publications", {},
                ).setdefault(topic_uuid, {})
                previous_peer_observed_seq = int(
                    peer_observed.get(peer_id, 0),
                )
                peer_observation_changed = (
                    observed_by_peer_seq > previous_peer_observed_seq
                )
                if peer_observation_changed:
                    peer_observed[peer_id] = observed_by_peer_seq
                    bookkeeping_changed = True
                    self.session.trace_event(
                        "relay.publication_acknowledged",
                        relay_identity=self.identity,
                        topic_uuid=topic_uuid,
                        peer_id=peer_id,
                        publication_seq=observed_by_peer_seq,
                        current_publication_seq=self._state.get(
                            "publication_seq", {},
                        ).get(topic_uuid, 0),
                    )
                if received_changed or peer_observation_changed:
                    self.session.trace_event(
                        "relay.publication_head_received",
                        relay_identity=self.identity,
                        topic_uuid=topic_uuid,
                        peer_id=peer_id,
                        publication_seq=publication_seq,
                        previous_publication_seq=previous_received_seq,
                        state_hash=head.get("hash"),
                        ack_requested=bool(head.get("ack_requested", False)),
                        ack_publication_seq=ack_publication_seq,
                        observed_local_publication_seq=observed_by_peer_seq,
                    )
                observed_publications_for_topic = self._state.setdefault(
                    "observed_publications", {},
                ).setdefault(topic_uuid, {})
                needs_publication_ack = ack_publication_seq > int(
                    observed_publications_for_topic.get(peer_id, 0),
                )
                self._cache_blobs(head.get("blobs"))
                observed_for_me = (head.get("observed") or {}).get(self.identity, {})
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
                with self._session_lock:
                    cached_topic = self.session.get_cached_peer_subtree(
                        peer_addr, topic_uuid,
                    )
                    wants_graft = (
                        topic_uuid in self._state.get("desired", [])
                        and self.session.protocol.index.get(topic_uuid) is None
                        and self.session.shared_topic_invitation_requires_mount(
                            cached_topic,
                        )
                    )
                if state_hash == last_seen and not wants_graft:
                    # The cached perspective already represents this head's
                    # semantic state, so its observation metadata is safe to
                    # expose immediately.  For a changed hash this must wait
                    # until the matching snapshot is applied below; otherwise
                    # reconciliation briefly combines a fresh acknowledgement
                    # with stale peer content and reports false divergence.
                    with self._session_lock:
                        observations_changed = (
                            self.session.record_peer_observations(
                                peer_addr, observed_for_me,
                            )
                        )
                    if observations_changed:
                        applied.add((topic_uuid, peer_id))
                    if needs_publication_ack:
                        observed_publications_for_topic[peer_id] = (
                            ack_publication_seq
                        )
                        bookkeeping_changed = True
                        applied.add((topic_uuid, peer_id))
                    continue
                payload = self.storage.read_snapshot(topic_uuid, peer_id, state_hash)
                if not payload:
                    continue
                payload_publication_seq = payload.get(
                    "_relay_publication_seq", 0,
                )
                if (
                    publication_seq > 0
                    and payload_publication_seq != publication_seq
                ):
                    # The publisher advanced an unchanged-hash snapshot
                    # between our head and snapshot reads. Do not combine
                    # metadata from two generations; the fixed poll cadence
                    # will fetch the new head promptly.
                    self.session.trace_event(
                        "relay.publication_snapshot_race",
                        relay_identity=self.identity,
                        topic_uuid=topic_uuid,
                        peer_id=peer_id,
                        head_publication_seq=publication_seq,
                        snapshot_publication_seq=payload_publication_seq,
                        state_hash=state_hash,
                    )
                    continue
                subtree = protocol_node_from_envelope(payload)
                peer_copy = copy.deepcopy(subtree)
                # Registering peer_topic_sets (not add_peer - see
                # Session.note_indirect_peer_topic) is what lets application
                # reconciliation recognize this cache as discussing the topic.
                with self._session_lock:
                    self.session.note_indirect_peer_topic(peer_addr, topic_uuid)
                    self.session.bind_peer_topic_channel(
                        peer_addr, topic_uuid, "mailbox",
                    )
                    if wants_graft:
                        # Applications own validation, mounting and defaults.
                        # Unknown types remain visible only as peer cache and
                        # are retried if a matching application is loaded.
                        if self.session.shared_topic_handler_for(subtree):
                            self.session.accept_shared_topic_invitation(subtree)
                        else:
                            self.session.note_pending_topic_invitation(topic_uuid)
                    self.session.apply_peer_subtree(
                        peer_addr, peer_copy, payload.get("parent_uuid"),
                    )
                    self.session.record_peer_observations(
                        peer_addr, observed_for_me,
                    )
                self._state["observed"].setdefault(topic_uuid, {})[peer_id] = (
                    self.session.node_revision_map(peer_copy)
                )
                if needs_publication_ack:
                    observed_publications_for_topic[peer_id] = (
                        ack_publication_seq
                    )
                self._state["applied"].setdefault(topic_uuid, {})[peer_id] = state_hash
                applied.add((topic_uuid, peer_id))
                self.session.trace_event(
                    "relay.publication_cached",
                    relay_identity=self.identity,
                    topic_uuid=topic_uuid,
                    peer_id=peer_id,
                    publication_seq=publication_seq,
                    state_hash=state_hash,
                    acknowledgement_pending=needs_publication_ack,
                )
        if applied or bookkeeping_changed:
            self._save_state()
        return sorted(applied)

    @_relay_io_locked
    def status_payload(self) -> dict:
        # Union of locally-owned topics (which we publish) and any topic
        # we've ever applied something from (which may not be one of our
        # own topics at all - e.g. a topic only known via a peer's relay
        # snapshot) - reporting only the former hid exactly the kind of
        # state-file collision bug this diagnostic is meant to catch.
        topic_uuids = set(self.relay_topic_uuids()) | set(self._state["applied"])
        return {
            "configured": self.storage is not None,
            "identity": self.identity,
            "root": str(self.storage.root) if self.storage else None,
            "state_file": self._state_path,
            "timing": self.timing.status_payload(),
            "desired": list(self._state.get("desired", [])),
            "presence": {
                peer_id: self.peer_liveness(peer_id)
                for peer_id in self.known_peer_identities()
            },
            "topics": {
                topic_uuid: {
                    "published_hash": self._state["published"].get(topic_uuid),
                    "publication_seq": self._state.get(
                        "publication_seq", {},
                    ).get(topic_uuid, 0),
                    "ack_publication_seq": self._state.get(
                        "ack_publication_seq", {},
                    ).get(topic_uuid, 0),
                    "received_publications": self._state.get(
                        "received_publications", {},
                    ).get(topic_uuid, {}),
                    "observed_publications": self._state.get(
                        "observed_publications", {},
                    ).get(topic_uuid, {}),
                    "peer_observed_publications": self._state.get(
                        "peer_observed_publications", {},
                    ).get(topic_uuid, {}),
                    "applied": self._state["applied"].get(topic_uuid, {}),
                }
                for topic_uuid in sorted(topic_uuids)
            },
        }

    @_relay_io_locked
    def delete_topic(self, topic_uuid: str) -> SessionResult:
        # Purely a storage/bookkeeping cleanup - never touches whatever a
        # peer may have already grafted into their own local application tree
        # from this topic (local content deletion is a separate application
        # decision this transport layer has no business making).
        if not self.storage:
            return SessionResult("error", reason="relay not configured")
        self.storage.delete_topic(topic_uuid)
        self._state["published"].pop(topic_uuid, None)
        self._state["published_observations"].pop(topic_uuid, None)
        self._state["observed"].pop(topic_uuid, None)
        self._state["publication_seq"].pop(topic_uuid, None)
        self._state["ack_publication_seq"].pop(topic_uuid, None)
        self._state["received_publications"].pop(topic_uuid, None)
        self._state["observed_publications"].pop(topic_uuid, None)
        self._state["peer_observed_publications"].pop(topic_uuid, None)
        self._state["applied"].pop(topic_uuid, None)
        self._state["desired"] = [t for t in self._state.get("desired", []) if t != topic_uuid]
        self._state["identity_topics"] = [
            t for t in self._state.get("identity_topics", []) if t != topic_uuid
        ]
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
            data.setdefault("publication_seq", {})
            data.setdefault("ack_publication_seq", {})
            data.setdefault("received_publications", {})
            data.setdefault("observed_publications", {})
            data.setdefault("peer_observed_publications", {})
            data.setdefault("desired", [])
            data.setdefault("identity_topics", [])
            data.setdefault("shared", [])
            # `applied` is deliberately NOT restored (see _save_state) - it
            # always starts empty so a restart re-fetches and re-caches
            # every peer's content.
            data["applied"] = {}
            return data
        return {
            "published": {}, "published_observations": {}, "observed": {},
            "publication_seq": {}, "ack_publication_seq": {},
            "received_publications": {},
            "observed_publications": {}, "peer_observed_publications": {},
            "applied": {}, "desired": [], "identity_topics": [], "shared": [],
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
        path = Path(self._state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path = str(path.resolve())
        with _STATE_SAVE_LOCKS_GUARD:
            save_lock = _STATE_SAVE_LOCKS.setdefault(absolute_path, threading.Lock())
        with save_lock:
            # Copy while holding the writer lock so JSON serialization does
            # not follow nested dictionaries that another save is replacing.
            persisted = copy.deepcopy({
                key: value for key, value in self._state.items() if key != "applied"
            })
            tmp_path = path.with_name(
                f"{path.name}.{os.getpid()}.{uuid_mod.uuid4().hex}.tmp"
            )
            try:
                with tmp_path.open("w", encoding="utf-8") as f:
                    json.dump(persisted, f, sort_keys=True, indent=2)
                    f.write("\n")
                # Windows can briefly deny replacement while another thread,
                # process, virus scanner, or indexer still has the destination
                # open.  Match the retry policy used by main session saves.
                for attempt in range(12):
                    try:
                        os.replace(tmp_path, path)
                        break
                    except PermissionError as exc:
                        if attempt == 11:
                            raise
                        if attempt >= 2:
                            print(
                                "[relay] state save replace blocked, retrying "
                                f"{attempt + 1}/11 for {path}: {exc}",
                                flush=True,
                            )
                        time.sleep(0.08 * (attempt + 1))
            finally:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass


def _relay_fingerprint(storage) -> str:
    # Stable key for a connection: two targets that resolve to the same
    # server+root are the same connection (natural dedup). Reuses the exact
    # fingerprint the per-location state file is already keyed on
    # (default_relay_state_file), so a connection and its state file always
    # agree on identity.
    if storage is None:
        return "unconfigured"
    return _storage_fingerprint(RelayLogic._config_from_storage(storage))


class RelayManager:
    """Owns every relay connection this client runs at once.

    A RelayLogic is one connection to one target (one storage + state file);
    the manager holds them keyed by storage fingerprint. Today it holds a
    single implicit connection built from the startup config or an accepted
    token - identical to the previous single-storage behavior - but the
    surface is already the "many connections" one so per-topic targets can
    layer on without touching callers again.
    """

    def __init__(self, session: Session, config: dict, blob_store=None):
        self.session = session
        self.config = config
        self.blob_store = blob_store
        self._session_lock = session.lock
        # Registry iteration/mutation has its own lock. Protocol snapshots
        # use Session.lock; keeping the two distinct avoids an io->session vs
        # session->io lock inversion when credentials are refreshed.
        self._manager_lock = threading.RLock()
        self.connections: dict[str, RelayLogic] = {}
        # The implicit connection: built from the flat config (or a persisted
        # adopted-descriptor) exactly as before. Registered by fingerprint.
        self.primary = RelayLogic(session, config, blob_store=blob_store)
        self.primary._manager = self
        self.primary._session_lock = self._session_lock
        self._primary_fingerprint = _relay_fingerprint(self.primary.storage)
        self.connections[self._primary_fingerprint] = self.primary
        self._bootstrap_registry()
        for target in self.list_targets():
            self.ensure_connection(self.target_descriptor(target["id"]))
        self.refresh_scopes()

    def _target_registry(self) -> dict[str, dict]:
        registry = self.session.app_metadata.setdefault("relay_targets", {})
        if not isinstance(registry, dict):
            registry = {}
            self.session.app_metadata["relay_targets"] = registry
        return registry

    def _topic_target_map(self) -> dict[str, str]:
        mapping = self.session.app_metadata.setdefault("relay_topic_targets", {})
        if not isinstance(mapping, dict):
            mapping = {}
            self.session.app_metadata["relay_topic_targets"] = mapping
        return mapping

    @staticmethod
    def _record_from_descriptor(descriptor: dict, name: str | None = None) -> dict:
        channel_type = descriptor.get("type")
        if channel_type == "sftp":
            return {
                "name": name or descriptor.get("target_name") or f"{descriptor.get('host', '')} relay",
                "backend": "sftp",
                "host": descriptor.get("host") or "",
                "port": int(descriptor.get("port", 22)),
                "username": descriptor.get("username") or "",
                "root": descriptor.get("root", "/"),
                "password": descriptor.get("password") or "",
                "poll_interval_seconds": RelayLogic._normalize_poll_interval(
                    descriptor.get("poll_interval_seconds"), 3.0,
                ),
            }
        return {
            "name": name or descriptor.get("target_name") or "Local relay",
            "backend": "local",
            "root": descriptor.get("root") or "",
            "poll_interval_seconds": RelayLogic._normalize_poll_interval(
                descriptor.get("poll_interval_seconds"), 3.0,
            ),
        }

    @staticmethod
    def _descriptor_from_record(record: dict) -> dict:
        if record.get("backend") == "sftp":
            return {
                "type": "sftp", "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
                "host": record.get("host") or "",
                "port": int(record.get("port", 22)),
                "username": record.get("username") or "",
                "root": record.get("root", "/"),
                "password": record.get("password") or "",
                "poll_interval_seconds": record.get("poll_interval_seconds", 3),
                "target_name": record.get("name") or "Relay",
            }
        return {
            "type": "relay", "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
            "root": record.get("root") or "",
            "poll_interval_seconds": record.get("poll_interval_seconds", 3),
            "target_name": record.get("name") or "Local relay",
        }

    def _bootstrap_registry(self) -> None:
        registry = self._target_registry()
        # Older versions marked the target imported from JSON as protected.
        # Targets are now owned by the persisted registry, including migrated
        # ones, so every target can be edited and deleted in the UI.
        for record in registry.values():
            record.pop("configured", None)
        migration_key = "relay_startup_target_migrated"
        if self.session.app_metadata.get(migration_key):
            return
        self.session.app_metadata[migration_key] = True
        if not self.primary.storage:
            return
        descriptor = self.primary.channel_descriptor()
        if not descriptor:
            return
        fingerprint = _relay_fingerprint(self.primary.storage)
        target_id = next(
            (
                item_id for item_id, record in registry.items()
                if _relay_fingerprint(
                    RelayLogic._storage_from_descriptor(self._descriptor_from_record(record))
                ) == fingerprint
            ),
            None,
        )
        if target_id is None:
            target_id = str(uuid_mod.uuid4())
            registry[target_id] = self._record_from_descriptor(
                descriptor, "Imported relay",
            )
        # Migrate durable relay intent to explicit assignments. New topics
        # remain unassigned, as required by the explicit-target model.
        mapping = self._topic_target_map()
        for topic_uuid in set(self.primary._state.get("shared", [])) | set(
            self.primary._state.get("desired", [])
        ):
            node = self.session.protocol.index.get(topic_uuid)
            if node and self.session.supports_shared_topic(node):
                mapping.setdefault(topic_uuid, target_id)

    @_manager_locked
    def list_targets(self) -> list[dict]:
        assignments = self._topic_target_map()
        result = []
        for target_id, record in sorted(
            self._target_registry().items(),
            key=lambda item: str(item[1].get("name", "")).lower(),
        ):
            public = {key: value for key, value in record.items() if key != "password"}
            public.update({
                "id": target_id,
                "has_password": bool(record.get("password")),
                "topic_uuids": sorted(
                    topic_uuid for topic_uuid, assigned in assignments.items()
                    if assigned == target_id
                ),
            })
            connection = self.connection_for_target(target_id)
            if connection:
                public["timing"] = connection.timing.status_payload()
            result.append(public)
        return result

    @_manager_locked
    def target_descriptor(self, target_id: str) -> dict | None:
        record = self._target_registry().get(target_id)
        if not record:
            return None
        descriptor = self._descriptor_from_record(record)
        descriptor["identity"] = self.primary.identity
        descriptor["target_id"] = target_id
        return descriptor

    def ensure_connection(self, descriptor: dict | None) -> RelayLogic | None:
        storage = RelayLogic._storage_from_descriptor(descriptor)
        if storage is None:
            return None
        fingerprint = _relay_fingerprint(storage)
        with self._manager_lock:
            existing = self.connections.get(fingerprint)
            if existing:
                with existing._io_lock:
                    previous_storage = existing.storage
                    existing.storage = storage
                    close_previous = getattr(previous_storage, "_reset_connection", None)
                    if close_previous and previous_storage is not storage:
                        close_previous()
                existing.adopt_poll_interval_from_descriptor(descriptor or {})
                return existing
            record = self._record_from_descriptor(descriptor or {})
            connection_config = {
                "app_module": self.config.get("app_module"),
                "relay_identity": self.config.get("relay_identity") or self.session.identity.uuid,
                "relay_poll_interval_seconds": record.get("poll_interval_seconds", 3),
                "relay_blob_lease_seconds": self.config.get("relay_blob_lease_seconds", 300),
            }
            state_directory = self.config.get("relay_state_directory")
            if state_directory:
                safe_fingerprint = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]
                connection_config["relay_state_file"] = str(
                    Path(state_directory) / f"relay-{safe_fingerprint}.json"
                )
            if record.get("backend") == "sftp":
                connection_config.update({
                    "relay_backend": "sftp",
                    "relay_sftp_host": record.get("host"),
                    "relay_sftp_port": record.get("port", 22),
                    "relay_sftp_username": record.get("username"),
                    "relay_sftp_root": record.get("root", "/"),
                    "relay_sftp_password": record.get("password"),
                })
            else:
                connection_config.update({
                    "relay_backend": "local", "relay_root": record.get("root"),
                })
            connection = RelayLogic(
                self.session, connection_config, blob_store=self.blob_store,
            )
            connection._session_lock = self._session_lock
            connection._manager = self
            self.connections[fingerprint] = connection
            return connection

    def connection_for_target(self, target_id: str) -> RelayLogic | None:
        descriptor = self.target_descriptor(target_id)
        storage = RelayLogic._storage_from_descriptor(descriptor)
        with self._manager_lock:
            return self.connections.get(_relay_fingerprint(storage)) if storage else None

    def refresh_scopes(self) -> None:
        with self._manager_lock:
            topics_by_fingerprint: dict[str, set[str]] = {}
            for topic_uuid, target_id in self._topic_target_map().items():
                descriptor = self.target_descriptor(target_id)
                storage = RelayLogic._storage_from_descriptor(descriptor)
                if storage:
                    topics_by_fingerprint.setdefault(_relay_fingerprint(storage), set()).add(topic_uuid)
            for fingerprint, connection in self.connections.items():
                if fingerprint == "unconfigured":
                    continue
                connection.set_scoped_topics(topics_by_fingerprint.get(fingerprint, set()))

    def _descriptor_from_target_values(
        self, values: dict, password_fallback: str = "",
    ) -> SessionResult:
        backend = values.get("backend", "sftp")
        try:
            port = int(values.get("port") or 22)
        except (TypeError, ValueError):
            return SessionResult("error", reason="port must be a number")
        if not 1 <= port <= 65535:
            return SessionResult("error", reason="port must be between 1 and 65535")
        descriptor = {
            "type": "sftp" if backend == "sftp" else "relay",
            "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
            "host": str(values.get("host") or "").strip(),
            "port": port,
            "username": str(values.get("username") or "").strip(),
            "root": str(values.get("root") or ("/" if backend == "sftp" else "")).strip(),
            "password": values.get("password") or password_fallback,
            "poll_interval_seconds": values.get("poll_interval_seconds", 3),
        }
        if backend == "sftp" and (not descriptor["host"] or not descriptor["username"]):
            return SessionResult("error", reason="host and username are required")
        storage = RelayLogic._storage_from_descriptor(descriptor)
        if not storage:
            return SessionResult("error", reason="relay target is not usable")
        return SessionResult("ok", value=(descriptor, storage))

    @staticmethod
    def _verify_target_storage(storage) -> SessionResult:
        try:
            checker = getattr(storage, "verify_access", None)
            checker() if checker else storage.list_topics()
        except Exception as exc:
            return SessionResult(
                "error", reason=f"relay unavailable: {type(exc).__name__}: {exc}",
            )
        return SessionResult("ok")

    def verify_target_values(self, values: dict) -> SessionResult:
        """Check that a target's entered values reach a working relay, without
        persisting it - the "Test" button before a target is saved."""
        prepared = self._descriptor_from_target_values(values)
        if prepared.status != "ok":
            return prepared
        _descriptor, storage = prepared.value
        return self._verify_target_storage(storage)

    def create_target(self, values: dict, verify: bool = True) -> SessionResult:
        prepared = self._descriptor_from_target_values(values)
        if prepared.status != "ok":
            return prepared
        descriptor, storage = prepared.value
        if verify:
            verified = self._verify_target_storage(storage)
            if verified.status != "ok":
                return verified
        with self._manager_lock:
            target_id = str(uuid_mod.uuid4())
            self._target_registry()[target_id] = self._record_from_descriptor(
                descriptor, str(values.get("name") or "Relay target").strip(),
            )
            self.ensure_connection(self.target_descriptor(target_id))
            self.refresh_scopes()
        return SessionResult("ok", value=target_id)

    def update_target(self, target_id: str, values: dict,
                      verify: bool = True) -> SessionResult:
        with self._manager_lock:
            previous_record = self._target_registry().get(target_id)
            if not previous_record:
                return SessionResult("error", reason="relay target not found")
            previous_record = dict(previous_record)
        prepared = self._descriptor_from_target_values(
            values, str(previous_record.get("password") or ""),
        )
        if prepared.status != "ok":
            return prepared
        descriptor, storage = prepared.value
        if verify:
            verified = self._verify_target_storage(storage)
            if verified.status != "ok":
                return verified

        with self._manager_lock:
            registry = self._target_registry()
            current_record = registry.get(target_id)
            if not current_record:
                return SessionResult("error", reason="relay target not found")
            old_descriptor = self._descriptor_from_record(current_record)
            old_storage = RelayLogic._storage_from_descriptor(old_descriptor)
            old_fingerprint = _relay_fingerprint(old_storage) if old_storage else None
            old_connection = (
                self.connections.get(old_fingerprint) if old_fingerprint else None
            )
            assigned_topics = {
                topic_uuid for topic_uuid, assigned in self._topic_target_map().items()
                if assigned == target_id
            }
            old_intent = {"shared": set(), "desired": set(), "identity_topics": set()}
            if old_connection:
                with old_connection._io_lock:
                    for key in old_intent:
                        old_intent[key] = set(old_connection._state.get(key, []))

            registry[target_id] = self._record_from_descriptor(
                descriptor,
                str(values.get("name") or current_record.get("name") or "Relay target").strip(),
            )
            new_connection = self.ensure_connection(self.target_descriptor(target_id))
            if not new_connection:
                registry[target_id] = current_record
                return SessionResult("error", reason="relay connection could not be created")

            new_fingerprint = _relay_fingerprint(new_connection.storage)
            if old_connection and old_connection is not new_connection:
                other_old_reference = any(
                    item_id != target_id
                    and _relay_fingerprint(RelayLogic._storage_from_descriptor(
                        self._descriptor_from_record(item)
                    )) == old_fingerprint
                    for item_id, item in registry.items()
                )
                if other_old_reference:
                    shared = old_intent["shared"] & assigned_topics
                    desired = old_intent["desired"] & assigned_topics
                    identity_topics = old_intent["identity_topics"]
                    if shared:
                        old_connection.unmark_topics_shared(sorted(shared))
                    if desired:
                        old_connection.unmark_topics_desired(sorted(desired))
                else:
                    shared = old_intent["shared"]
                    desired = old_intent["desired"]
                    identity_topics = old_intent["identity_topics"]
                if shared:
                    new_connection.mark_topics_shared(sorted(shared))
                if desired:
                    new_connection.mark_topics_desired(sorted(desired))
                if identity_topics:
                    with new_connection._io_lock:
                        merged = set(new_connection._state.get("identity_topics", []))
                        merged.update(identity_topics)
                        new_connection._state["identity_topics"] = sorted(merged)
                        new_connection._save_state()
                if not other_old_reference:
                    self._retire_connection(old_fingerprint, old_connection)
            self.connections[new_fingerprint] = new_connection
            self.refresh_scopes()
        return SessionResult("ok", value=target_id)

    @_manager_locked
    def register_descriptor(self, descriptor: dict) -> SessionResult:
        storage = RelayLogic._storage_from_descriptor(descriptor)
        if not storage:
            return SessionResult("error", reason="relay descriptor is not usable")
        fingerprint = _relay_fingerprint(storage)
        for target_id, record in self._target_registry().items():
            existing = RelayLogic._storage_from_descriptor(self._descriptor_from_record(record))
            if existing and _relay_fingerprint(existing) == fingerprint:
                # A token identifies a location; it must not silently edit
                # this client's saved password or polling preference. Those
                # are changed only through the target editor.
                self.ensure_connection(self.target_descriptor(target_id))
                return SessionResult("ok", value=target_id)
        target_id = str(uuid_mod.uuid4())
        self._target_registry()[target_id] = self._record_from_descriptor(descriptor)
        self.ensure_connection(descriptor)
        self.refresh_scopes()
        return SessionResult("ok", value=target_id)

    def accept_descriptor(self, descriptor: dict, topic_uuids: list[str],
                          inviter_identity_uuid: str | None = None) -> SessionResult:
        if not isinstance(topic_uuids, list) or not topic_uuids:
            return SessionResult("error", reason="no topic_uuids given")
        if not str(descriptor.get("identity") or "").strip():
            return SessionResult("error", reason="relay descriptor missing identity")
        storage = RelayLogic._storage_from_descriptor(descriptor)
        if not storage:
            return SessionResult("error", reason="relay descriptor is not usable")
        try:
            checker = getattr(storage, "verify_access", None)
            checker() if checker else storage.list_topics()
        except Exception as exc:
            return SessionResult(
                "error", reason=f"relay unavailable: {type(exc).__name__}: {exc}",
            )
        registered = self.register_descriptor(descriptor)
        if registered.status != "ok":
            return registered
        target_id = registered.value
        connection = self.connection_for_target(target_id)
        if not connection:
            return SessionResult("error", reason="relay connection could not be created")
        desired = connection.mark_topics_desired(topic_uuids)
        if desired.status != "ok":
            return desired
        if inviter_identity_uuid:
            with connection._io_lock:
                identity_topics = set(connection._state.get("identity_topics", []))
                identity_topics.add(inviter_identity_uuid)
                connection._state["identity_topics"] = sorted(identity_topics)
                connection._save_state()
        application_topics = [
            topic_uuid for topic_uuid in topic_uuids
            if topic_uuid not in {inviter_identity_uuid, self.session.identity.uuid}
        ]
        # An application topic can live on only one target. Clean durable intent from its
        # previous connection before moving the mapping; otherwise an old
        # accepted target keeps polling the topic through its `desired` set.
        with self._manager_lock:
            mapping = self._topic_target_map()
            previous_connections = []
            for topic_uuid in application_topics:
                previous_id = mapping.get(topic_uuid)
                if previous_id and previous_id != target_id:
                    previous = self.connection_for_target(previous_id)
                    if previous and previous is not connection:
                        previous_connections.append((previous, topic_uuid))
            for previous, topic_uuid in previous_connections:
                previous.unmark_topics_shared([topic_uuid])
                previous.unmark_topics_desired([topic_uuid])
            for topic_uuid in application_topics:
                mapping[topic_uuid] = target_id
            self.refresh_scopes()
        return SessionResult("ok", value=target_id)

    @_manager_locked
    def assign_topic_target(self, topic_uuid: str, target_id: str | None) -> SessionResult:
        topic = self.session.get_node(topic_uuid)
        if not topic or not self.session.supports_shared_topic(topic):
            return SessionResult("error", reason="application topic not found")
        if target_id and target_id not in self._target_registry():
            return SessionResult("error", reason="relay target not found")
        mapping = self._topic_target_map()
        previous_id = mapping.get(topic_uuid)
        next_connection = self.connection_for_target(target_id) if target_id else None
        if previous_id and previous_id != target_id:
            previous = self.connection_for_target(previous_id)
            if previous and previous is not next_connection:
                previous.unmark_topics_shared([topic_uuid])
                previous.unmark_topics_desired([topic_uuid])
        if target_id:
            mapping[topic_uuid] = target_id
        else:
            mapping.pop(topic_uuid, None)
        self.refresh_scopes()
        if target_id:
            if next_connection:
                next_connection.mark_topics_shared([topic_uuid])
        return SessionResult("ok", value=target_id or "")

    @_manager_locked
    def assign_topics_target(self, topic_uuids: list[str],
                             target_id: str | None) -> SessionResult:
        normalized = list(dict.fromkeys(str(item) for item in topic_uuids if item))
        if not normalized:
            return SessionResult("error", reason="choose at least one topic")
        if target_id and target_id not in self._target_registry():
            return SessionResult("error", reason="relay target not found")
        # Validate the complete request before changing any mapping or relay
        # state. A bad topic late in a multi-topic token must not leave the
        # earlier topics silently reassigned.
        for topic_uuid in normalized:
            topic = self.session.get_node(topic_uuid)
            if not topic or not self.session.supports_shared_topic(topic):
                return SessionResult(
                    "error", reason=f"application topic not found: {topic_uuid}",
                )
        for topic_uuid in normalized:
            result = self.assign_topic_target(topic_uuid, target_id)
            if result.status != "ok":
                return result
        return SessionResult("ok", value=normalized)

    @_manager_locked
    def target_for_topic(self, topic_uuid: str) -> str | None:
        return self._topic_target_map().get(topic_uuid)

    def test_target(self, target_id: str) -> SessionResult:
        descriptor = self.target_descriptor(target_id)
        connection = self.connection_for_target(target_id)
        if not descriptor or not connection or not connection.storage:
            return SessionResult("error", reason="relay target not found")
        try:
            with connection._io_lock:
                checker = getattr(connection.storage, "verify_access", None)
                checker() if checker else connection.storage.list_topics()
                connection.calibrate_timing(5)
        except Exception as exc:
            return SessionResult(
                "error", reason=f"relay unavailable: {type(exc).__name__}: {exc}",
            )
        return SessionResult("ok", value=target_id)

    def _retire_connection(self, fingerprint: str | None,
                           connection: RelayLogic) -> None:
        with connection._io_lock:
            connection._state["shared"] = []
            connection._state["desired"] = []
            connection._state["identity_topics"] = []
            connection._save_state()
            storage = connection.storage
            close_storage = getattr(storage, "_reset_connection", None)
            if close_storage:
                close_storage()
            connection.storage = None
            connection._scoped_topic_uuids = set()
        if fingerprint:
            self.connections.pop(fingerprint, None)
        if connection is self.primary:
            self._primary_fingerprint = "unconfigured"
            self.connections.setdefault("unconfigured", connection)

    @_manager_locked
    def delete_target(self, target_id: str) -> SessionResult:
        registry = self._target_registry()
        record = registry.get(target_id)
        if not record:
            return SessionResult("error", reason="relay target not found")
        descriptor = self.target_descriptor(target_id)
        storage = RelayLogic._storage_from_descriptor(descriptor)
        fingerprint = _relay_fingerprint(storage) if storage else None
        connection = self.connections.get(fingerprint) if fingerprint else None
        mapping = self._topic_target_map()
        for topic_uuid in [uuid for uuid, assigned in mapping.items() if assigned == target_id]:
            mapping.pop(topic_uuid, None)
        registry.pop(target_id)
        remaining_same_connection = any(
            _relay_fingerprint(
                RelayLogic._storage_from_descriptor(self._descriptor_from_record(item))
            ) == fingerprint
            for item in registry.values()
        ) if fingerprint else False
        if connection and not remaining_same_connection:
            self._retire_connection(fingerprint, connection)
        self.refresh_scopes()
        return SessionResult("ok", value=target_id)

    def all_connections(self) -> list[RelayLogic]:
        with self._manager_lock:
            return list(self.connections.values())

    def has_any_active_relationship(self) -> bool:
        return any(conn.has_active_relationship() for conn in self.all_connections())

    def peer_liveness(self, peer_id: str, target_id: str | None = None) -> dict:
        # The same identity may exist on an old and a current target. An
        # alive heartbeat on any connection is stronger evidence than a
        # stale heartbeat on another; never let insertion order decide.
        if target_id:
            connection = self.connection_for_target(target_id)
            return connection.peer_liveness(peer_id) if connection else {"state": "unknown"}
        known = []
        for conn in self.all_connections():
            result = conn.peer_liveness(peer_id)
            if result.get("state") != "unknown":
                known.append(result)
        if not known:
            return {"state": "unknown"}
        alive = [item for item in known if item.get("state") == "alive"]
        candidates = alive or known
        return min(
            candidates,
            key=lambda item: abs(float(item.get("last_seen_seconds_ago", float("inf")))),
        )

    @property
    def storage(self):
        # Back-compat shim for single-storage diagnostic readers:
        # the primary connection's storage.
        return self.primary.storage

    def channel_descriptor(self) -> dict | None:
        return self.primary.channel_descriptor()

    def status_payload(self) -> dict:
        conns = self.all_connections()
        if len(conns) == 1:
            return conns[0].status_payload()
        return {"connections": [conn.status_payload() for conn in conns]}

    def read_blob(self, blob_id: str, exclude: RelayLogic | None = None) -> bytes | None:
        for connection in self.all_connections():
            if connection is exclude:
                continue
            try:
                data = connection.read_blob(blob_id)
            except Exception:
                continue
            if data is not None:
                return data
        return None

    def blob_gc_report(self) -> dict:
        reports = []
        for connection in self.all_connections():
            try:
                report = connection.blob_gc_report()
                report["connection"] = _relay_fingerprint(connection.storage)
                reports.append(report)
            except Exception as exc:
                reports.append({
                    "connection": _relay_fingerprint(connection.storage),
                    "error": str(exc),
                })
        return {"mode": "report-only", "connections": reports}

    def delete_topic(self, topic_uuid: str) -> SessionResult:
        for conn in self.all_connections():
            if topic_uuid in conn._state.get("published", {}) or topic_uuid in conn._state.get("applied", {}):
                return conn.delete_topic(topic_uuid)
        return self.primary.delete_topic(topic_uuid)
