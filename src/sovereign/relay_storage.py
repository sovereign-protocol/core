"""
Relay storage backends.

Functionality:
  Store and retrieve peer snapshot/head files for the file-mailbox relay
  (see relay_logic.py). Each peer identity only ever writes into its own
  folder - a storage backend never merges or interprets content, it just
  reads and writes bytes at a path.

Offered API:
  LocalFolderRelayStorage(root)
  SftpRelayStorage(host, username, remote_root, port=22, password=None,
                   private_key_path=None, private_key_passphrase=None,
                   connect_timeout=10.0, operation_timeout=10.0,
                   keepalive_seconds=15.0)
    Every wait is bounded: connect_timeout covers the TCP handshake, the
    SSH banner and authentication; operation_timeout bounds each read on
    an established channel; keepalive_seconds makes a silent peer fail
    rather than be waited on. A connection that dies after it is
    established is the case that matters - paramiko's own timeout does
    not cover it, and an unbounded read stalls the whole poll cycle.

    operation_timeout bounds *silence*, not slowness: a large blob keeps
    transferring for as long as data keeps arriving. Ten seconds is
    therefore already generous against a relay whose normal round trip is
    a few hundred milliseconds, and it caps one dropped operation at
    roughly two timeouts rather than the minute the old default allowed.
    Raise it with relay_sftp_operation_timeout for a genuinely slow link.
    Both satisfy RelayStorage, including:
    write_snapshot(topic_uuid, peer_id, state_hash, payload)
    read_head(topic_uuid, peer_id) -> dict | None
    read_snapshot(topic_uuid, peer_id, state_hash) -> dict | None
    list_peers(topic_uuid) -> list[str]
    list_topics() -> list[str]
    delete_publication(topic_uuid, peer_id) -> None
      Removes only one peer's publication from a topic.
    delete_topic(topic_uuid) -> None
      Removes the whole topic subtree (every peer's snapshots under it) -
      not scoped to one peer, since a topic is a shared mailbox namespace,
      not owned by whichever identity happens to publish under it.
    write_presence(peer_id, payload) -> float | None
      Writes a per-identity (not per-topic) heartbeat file and returns the
      storage backend's own server-side mtime for it - the write and the
      mtime read happen back to back deliberately, so a caller can use that
      mtime as "what does the server consider *now*" without any separate
      clock-sync step (see relay_logic.py's liveness design).
    read_presence_with_mtime(peer_id) -> tuple[dict | None, float | None]
      Content and server-side mtime together, in one call - reading them
      separately would risk a race between the two.
    timing_probe() -> tuple[float | None, float]
      Returns relay-server mtime and one metadata-request roundtrip using a
      temporary probe that is removed before the call returns.
    close() -> None
      Releases any persistent backend connection.

Used API:
  LocalFolderRelayStorage: Python standard library only.
  SftpRelayStorage: paramiko (lazily imported - only needed if this class is
    actually instantiated, so a local-folder-only setup never needs it
    installed).

Layout (identical on both backends):
  <root>/topics/<topic_uuid>/peers/<peer_id>/head.json
  <root>/topics/<topic_uuid>/peers/<peer_id>/snapshots/<state_hash>.json
  <root>/identities/<peer_id>/presence.json

SftpRelayStorage deliberately stores plaintext JSON, same as the local
backend - no content encryption yet (MVP scope, agreed with the user: the
priority right now is being able to read the files directly on the server
for debugging; content encryption is a deliberate later layer, not an
oversight). The SFTP/SSH transport itself is still encrypted in transit,
same as any SSH connection - "no encryption" here means no additional
application-level encryption of the JSON content, not an unencrypted wire.
Host key verification uses trust-on-first-use (paramiko's AutoAddPolicy) -
adequate for a single personal server used by one or two people, not a
substitute for real host key pinning at larger scale.
"""

from __future__ import annotations

import json
import os
import posixpath
import stat
import sys
import threading
import time
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from .blob_store import blob_hex, blob_id_for


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@runtime_checkable
class RelayStorage(Protocol):
    """Storage contract required by one relay polling endpoint."""

    mtime_resolution_seconds: float

    def write_snapshot(
        self, topic_uuid: str, peer_id: str, state_hash: str,
        payload: dict, blob_ids: set[str] | None = None,
    ) -> None: ...

    def verify_access(self) -> None: ...
    def write_blob(self, blob_id: str, data: bytes) -> None: ...
    def read_blob(self, blob_id: str) -> bytes | None: ...
    def has_blob(self, blob_id: str) -> bool: ...
    def list_blob_ids(self) -> list[str]: ...
    def write_blob_lease(
        self, blob_id: str, peer_id: str, payload: dict,
    ) -> None: ...
    def delete_blob_lease(self, blob_id: str, peer_id: str) -> None: ...
    def list_blob_leases(self) -> dict[str, list[dict]]: ...
    def read_head(self, topic_uuid: str, peer_id: str) -> dict | None: ...
    def read_snapshot(
        self, topic_uuid: str, peer_id: str, state_hash: str,
    ) -> dict | None: ...
    def list_peers(self, topic_uuid: str) -> list[str]: ...
    def list_topics(self) -> list[str]: ...
    def delete_publication(self, topic_uuid: str, peer_id: str) -> None: ...
    def delete_topic(self, topic_uuid: str) -> None: ...
    def write_presence(self, peer_id: str, payload: dict) -> float | None: ...
    def read_presence_with_mtime(
        self, peer_id: str,
    ) -> tuple[dict | None, float | None]: ...
    def timing_probe(self) -> tuple[float | None, float]: ...
    def close(self) -> None: ...


class LocalFolderRelayStorage:
    mtime_resolution_seconds = 0.001

    def __init__(self, root: str):
        self.root = Path(root)

    def close(self) -> None:
        """Local filesystem storage owns no persistent connection."""

    def write_snapshot(self, topic_uuid: str, peer_id: str, state_hash: str,
                       payload: dict, blob_ids: set[str] | None = None) -> None:
        peer_dir = self._peer_dir(topic_uuid, peer_id)
        snapshots_dir = peer_dir / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        previous = self._read_json(peer_dir / "head.json") or {}
        self._write_json(snapshots_dir / f"{state_hash}.json", payload)
        head = {
            "peer": peer_id,
            "topic": topic_uuid,
            "hash": state_hash,
            "publication_seq": payload.get("_relay_publication_seq", 0),
            "ack_requested": bool(payload.get("_relay_ack_requested", False)),
            "ack_publication_seq": payload.get(
                "_relay_ack_publication_seq", 0,
            ),
            "updated_at": now_iso(),
            "snapshot": f"snapshots/{state_hash}.json",
            "observed": payload.get("_relay_observed", {}),
            "observed_publications": payload.get(
                "_relay_observed_publications", {},
            ),
            "blobs": sorted(blob_ids or set()),
            "previous_blobs": sorted(previous.get("blobs") or []),
        }
        self._write_json(peer_dir / "head.json", head)
        # GC superseded snapshots (review R-4): keep the new one plus the
        # immediately-previous head's, so a lagging peer mid-fetch of the
        # prior hash still finds it; older ones would otherwise accumulate
        # on the server forever.
        keep = {f"{state_hash}.json", f"{previous.get('hash')}.json"}
        for entry in snapshots_dir.iterdir():
            if entry.is_file() and entry.name not in keep:
                entry.unlink()

    def verify_access(self) -> None:
        """Verify that the configured relay root is writable."""
        self.root.mkdir(parents=True, exist_ok=True)
        probe = self.root / f".sovereign-probe-{os.getpid()}-{uuid_mod.uuid4().hex}"
        try:
            probe.write_bytes(b"")
        finally:
            probe.unlink(missing_ok=True)

    def write_blob(self, blob_id: str, data: bytes) -> None:
        if blob_id_for(data) != blob_id:
            raise ValueError("blob hash mismatch")
        digest = blob_hex(blob_id)
        path = self.root / "blobs" / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            if self.read_blob(blob_id) is None:
                raise ValueError("relay blob is corrupt")
            return
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid_mod.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def read_blob(self, blob_id: str) -> bytes | None:
        digest = blob_hex(blob_id)
        path = self.root / "blobs" / digest[:2] / digest
        if not path.is_file():
            return None
        data = path.read_bytes()
        return data if blob_id_for(data) == blob_id else None

    def has_blob(self, blob_id: str) -> bool:
        return self.read_blob(blob_id) is not None

    def list_blob_ids(self) -> list[str]:
        root = self.root / "blobs"
        if not root.is_dir():
            return []
        found = []
        for shard in root.iterdir():
            if not shard.is_dir():
                continue
            for path in shard.iterdir():
                blob_id = f"sha256:{path.name}"
                try:
                    blob_hex(blob_id)
                except ValueError:
                    continue
                if path.is_file():
                    found.append(blob_id)
        return sorted(found)

    def write_blob_lease(self, blob_id: str, peer_id: str, payload: dict) -> None:
        digest = blob_hex(blob_id)
        self._write_json(
            self.root / "blob_leases" / digest / f"{peer_id}.json", payload,
        )

    def delete_blob_lease(self, blob_id: str, peer_id: str) -> None:
        digest = blob_hex(blob_id)
        path = self.root / "blob_leases" / digest / f"{peer_id}.json"
        path.unlink(missing_ok=True)

    def list_blob_leases(self) -> dict[str, list[dict]]:
        root = self.root / "blob_leases"
        out: dict[str, list[dict]] = {}
        if not root.is_dir():
            return out
        for directory in root.iterdir():
            blob_id = f"sha256:{directory.name}"
            try:
                blob_hex(blob_id)
            except ValueError:
                continue
            if directory.is_dir():
                out[blob_id] = [
                    value for path in directory.glob("*.json")
                    if (value := self._read_json(path)) is not None
                ]
        return out

    def read_head(self, topic_uuid: str, peer_id: str) -> dict | None:
        return self._read_json(self._peer_dir(topic_uuid, peer_id) / "head.json")

    def read_snapshot(self, topic_uuid: str, peer_id: str,
                      state_hash: str) -> dict | None:
        path = self._peer_dir(topic_uuid, peer_id) / "snapshots" / f"{state_hash}.json"
        return self._read_json(path)

    def list_peers(self, topic_uuid: str) -> list[str]:
        peers_dir = self.root / "topics" / topic_uuid / "peers"
        if not peers_dir.is_dir():
            return []
        return sorted(entry.name for entry in peers_dir.iterdir() if entry.is_dir())

    def list_topics(self) -> list[str]:
        topics_dir = self.root / "topics"
        if not topics_dir.is_dir():
            return []
        return sorted(entry.name for entry in topics_dir.iterdir() if entry.is_dir())

    def delete_publication(self, topic_uuid: str, peer_id: str) -> None:
        import shutil
        peer_dir = self._peer_dir(topic_uuid, peer_id)
        if peer_dir.is_dir():
            shutil.rmtree(peer_dir)

    def delete_topic(self, topic_uuid: str) -> None:
        import shutil
        topic_dir = self.root / "topics" / topic_uuid
        if topic_dir.is_dir():
            shutil.rmtree(topic_dir)

    def write_presence(self, peer_id: str, payload: dict) -> float | None:
        path = self._presence_path(peer_id)
        self._write_json(path, payload)
        return path.stat().st_mtime

    def read_presence_with_mtime(self, peer_id: str) -> tuple[dict | None, float | None]:
        path = self._presence_path(peer_id)
        if not path.is_file():
            return None, None
        return self._read_json(path), path.stat().st_mtime

    def timing_probe(self) -> tuple[float | None, float]:
        """Return server mtime plus one metadata-request roundtrip."""
        self.root.mkdir(parents=True, exist_ok=True)
        probe = self.root / f".sovereign-timing-{os.getpid()}-{uuid_mod.uuid4().hex}"
        try:
            probe.write_bytes(b"")
            started = time.monotonic()
            mtime = probe.stat().st_mtime
            roundtrip = time.monotonic() - started
            return mtime, roundtrip
        finally:
            probe.unlink(missing_ok=True)

    def _presence_path(self, peer_id: str) -> Path:
        return self.root / "identities" / peer_id / "presence.json"

    def _peer_dir(self, topic_uuid: str, peer_id: str) -> Path:
        return self.root / "topics" / topic_uuid / "peers" / peer_id

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid_mod.uuid4().hex}.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, sort_keys=True, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        if not path.is_file():
            return None
        with path.open(encoding="utf-8") as f:
            return json.load(f)


class SftpRelayStorage:
    # SFTP v3 exposes st_mtime in whole seconds.
    mtime_resolution_seconds = 1.0

    def __init__(self, host: str, username: str, remote_root: str,
                port: int = 22, password: str | None = None,
                private_key_path: str | None = None,
                private_key_passphrase: str | None = None,
                connect_timeout: float = 10.0,
                operation_timeout: float = 10.0,
                keepalive_seconds: float = 15.0):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.private_key_path = private_key_path
        self.private_key_passphrase = private_key_passphrase
        self.connect_timeout = connect_timeout
        # Paramiko's connect timeout covers the TCP handshake and nothing
        # after it, so a connection that dies once established - a laptop
        # that slept, a network that dropped mid-session - leaves every
        # later read blocking on a socket that will never answer. These two
        # are what actually bound that: no single read may stall longer
        # than operation_timeout, and keepalives make a silent peer fail
        # rather than be waited on. Without them one dead relay stalls the
        # poll cycle for as long as the OS keeps the socket open, which
        # observed in practice as phases of over an hour.
        self.operation_timeout = operation_timeout
        self.keepalive_seconds = keepalive_seconds
        # Kept as a plain posix-style string, not pathlib.Path - remote SFTP
        # paths are always forward-slash regardless of the host OS this
        # process happens to be running on (matters on Windows).
        self.root = remote_root.rstrip("/") or "/"
        self._client = None
        self._sftp = None
        # Optional callback(kind, **fields) for transport events worth
        # reporting - set by whoever builds this, left None in tests and for
        # any caller that does not care.
        self.on_event = None
        # Paramiko's SFTP client/channel is not safe for concurrent use.
        # Relay polling and diagnostic/API reads can run in different worker
        # threads, so every operation on this shared connection is serialized.
        self._operation_lock = threading.RLock()

    def close(self) -> None:
        self._reset_connection()

    def write_snapshot(self, topic_uuid: str, peer_id: str, state_hash: str,
                       payload: dict, blob_ids: set[str] | None = None) -> None:
        peer_dir = self._peer_dir(topic_uuid, peer_id)
        snapshots_dir = posixpath.join(peer_dir, "snapshots")
        previous = self._read_json(posixpath.join(peer_dir, "head.json")) or {}
        self._write_json(posixpath.join(snapshots_dir, f"{state_hash}.json"), payload)
        head = {
            "peer": peer_id,
            "topic": topic_uuid,
            "hash": state_hash,
            "publication_seq": payload.get("_relay_publication_seq", 0),
            "ack_requested": bool(payload.get("_relay_ack_requested", False)),
            "ack_publication_seq": payload.get(
                "_relay_ack_publication_seq", 0,
            ),
            "updated_at": now_iso(),
            "snapshot": f"snapshots/{state_hash}.json",
            "observed": payload.get("_relay_observed", {}),
            "observed_publications": payload.get(
                "_relay_observed_publications", {},
            ),
            "blobs": sorted(blob_ids or set()),
            "previous_blobs": sorted(previous.get("blobs") or []),
        }
        self._write_json(posixpath.join(peer_dir, "head.json"), head)
        self._gc_snapshots(
            snapshots_dir,
            keep={f"{state_hash}.json", f"{previous.get('hash')}.json"},
        )

    def _gc_snapshots(self, snapshots_dir: str, keep: set[str]) -> None:
        # Drop superseded snapshots (review R-4), keeping the new head's
        # and the immediately-previous one (a lagging peer may be mid-fetch
        # of that hash). Without this every published revision stayed on the
        # server forever.
        def operation(sftp):
            try:
                attrs = sftp.listdir_attr(snapshots_dir)
            except FileNotFoundError:
                return
            for entry in attrs:
                if stat.S_ISDIR(entry.st_mode or 0) or entry.filename in keep:
                    continue
                try:
                    sftp.remove(posixpath.join(snapshots_dir, entry.filename))
                except FileNotFoundError:
                    pass

        self._with_retry(operation)

    def verify_access(self) -> None:
        """Authenticate and verify write access without leaving relay data."""
        probe = posixpath.join(
            self.root,
            f".sovereign-probe-{os.getpid()}-{uuid_mod.uuid4().hex}",
        )

        def operation(sftp):
            self._mkdir_p(sftp, self.root)
            try:
                with sftp.open(probe, "wb") as f:
                    f.write(b"")
            finally:
                try:
                    sftp.remove(probe)
                except FileNotFoundError:
                    pass

        self._with_retry(operation)

    def write_blob(self, blob_id: str, data: bytes) -> None:
        if blob_id_for(data) != blob_id:
            raise ValueError("blob hash mismatch")
        digest = blob_hex(blob_id)
        path = posixpath.join(self.root, "blobs", digest[:2], digest)
        tmp_path = f"{path}.{uuid_mod.uuid4().hex}.tmp"

        def operation(sftp):
            self._mkdir_p(sftp, posixpath.dirname(path))
            try:
                with sftp.open(path, "rb") as handle:
                    existing = handle.read()
                if blob_id_for(existing) != blob_id:
                    raise ValueError("relay blob is corrupt")
                return
            except FileNotFoundError:
                pass
            with sftp.open(tmp_path, "wb") as handle:
                handle.write(data)
            self._atomic_rename(sftp, tmp_path, path)

        self._with_retry(operation)

    def read_blob(self, blob_id: str) -> bytes | None:
        digest = blob_hex(blob_id)
        path = posixpath.join(self.root, "blobs", digest[:2], digest)

        def operation(sftp):
            try:
                with sftp.open(path, "rb") as handle:
                    data = handle.read()
            except FileNotFoundError:
                return None
            return data if blob_id_for(data) == blob_id else None

        return self._with_retry(operation)

    def has_blob(self, blob_id: str) -> bool:
        return self.read_blob(blob_id) is not None

    def list_blob_ids(self) -> list[str]:
        def operation(sftp):
            root = posixpath.join(self.root, "blobs")
            try:
                shards = sftp.listdir_attr(root)
            except FileNotFoundError:
                return []
            found = []
            for shard in shards:
                if not stat.S_ISDIR(shard.st_mode or 0):
                    continue
                try:
                    entries = sftp.listdir_attr(posixpath.join(root, shard.filename))
                except FileNotFoundError:
                    continue
                for entry in entries:
                    if not stat.S_ISDIR(entry.st_mode or 0) and len(entry.filename) == 64:
                        blob_id = f"sha256:{entry.filename}"
                        try:
                            blob_hex(blob_id)
                        except ValueError:
                            continue
                        found.append(blob_id)
            return sorted(found)

        return self._with_retry(operation)

    def write_blob_lease(self, blob_id: str, peer_id: str, payload: dict) -> None:
        digest = blob_hex(blob_id)
        self._write_json(
            posixpath.join(self.root, "blob_leases", digest, f"{peer_id}.json"),
            payload,
        )

    def delete_blob_lease(self, blob_id: str, peer_id: str) -> None:
        digest = blob_hex(blob_id)
        path = posixpath.join(self.root, "blob_leases", digest, f"{peer_id}.json")

        def operation(sftp):
            try:
                sftp.remove(path)
            except FileNotFoundError:
                pass

        self._with_retry(operation)

    def list_blob_leases(self) -> dict[str, list[dict]]:
        def operation(sftp):
            root = posixpath.join(self.root, "blob_leases")
            try:
                directories = sftp.listdir_attr(root)
            except FileNotFoundError:
                return {}
            out = {}
            for directory in directories:
                if not stat.S_ISDIR(directory.st_mode or 0):
                    continue
                blob_id = f"sha256:{directory.filename}"
                try:
                    blob_hex(blob_id)
                except ValueError:
                    continue
                lease_dir = posixpath.join(root, directory.filename)
                values = []
                for entry in sftp.listdir_attr(lease_dir):
                    if stat.S_ISDIR(entry.st_mode or 0):
                        continue
                    with sftp.open(posixpath.join(lease_dir, entry.filename), "rb") as handle:
                        values.append(json.loads(handle.read().decode("utf-8")))
                out[blob_id] = values
            return out

        return self._with_retry(operation)

    def read_head(self, topic_uuid: str, peer_id: str) -> dict | None:
        return self._read_json(posixpath.join(self._peer_dir(topic_uuid, peer_id), "head.json"))

    def read_snapshot(self, topic_uuid: str, peer_id: str,
                      state_hash: str) -> dict | None:
        path = posixpath.join(self._peer_dir(topic_uuid, peer_id), "snapshots", f"{state_hash}.json")
        return self._read_json(path)

    def list_peers(self, topic_uuid: str) -> list[str]:
        return self._list_dir(posixpath.join(self.root, "topics", topic_uuid, "peers"))

    def list_topics(self) -> list[str]:
        return self._list_dir(posixpath.join(self.root, "topics"))

    def delete_publication(self, topic_uuid: str, peer_id: str) -> None:
        def operation(sftp):
            self._rmtree(sftp, self._peer_dir(topic_uuid, peer_id))

        self._with_retry(operation)

    def delete_topic(self, topic_uuid: str) -> None:
        def operation(sftp):
            self._rmtree(sftp, posixpath.join(self.root, "topics", topic_uuid))

        self._with_retry(operation)

    @staticmethod
    def _rmtree(sftp, path: str) -> None:
        try:
            attrs = sftp.listdir_attr(path)
        except FileNotFoundError:
            return
        for entry in attrs:
            full = posixpath.join(path, entry.filename)
            if stat.S_ISDIR(entry.st_mode or 0):
                SftpRelayStorage._rmtree(sftp, full)
            else:
                sftp.remove(full)
        sftp.rmdir(path)

    def write_presence(self, peer_id: str, payload: dict) -> float | None:
        # One round trip fewer than write-then-stat, and it runs every poll
        # cycle on every client, so the saving is per cycle rather than per
        # change. The mtime must come from the same visit as the write in
        # any case: it is this client's reference point for "what does the
        # server think now", and a separate call could read a newer one
        # written by a sibling in between.
        path = self._presence_path(peer_id)
        payload_bytes = (
            json.dumps(payload, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        tmp_path = f"{path}.{uuid_mod.uuid4().hex}.tmp"

        def operation(sftp):
            self._mkdir_p(sftp, posixpath.dirname(path))
            with sftp.open(tmp_path, "wb") as handle:
                handle.write(payload_bytes)
            self._atomic_rename(sftp, tmp_path, path)
            try:
                return sftp.stat(path).st_mtime
            except FileNotFoundError:
                return None

        return self._with_retry(operation)

    def read_presence_with_mtime(self, peer_id: str) -> tuple[dict | None, float | None]:
        path = self._presence_path(peer_id)
        content = self._read_json(path)
        if content is None:
            return None, None
        return content, self._stat_mtime(path)

    def timing_probe(self) -> tuple[float | None, float]:
        """Measure one SFTP request and remove the clock probe afterwards."""
        probe = posixpath.join(
            self.root,
            f".sovereign-timing-{os.getpid()}-{uuid_mod.uuid4().hex}",
        )

        def operation(sftp):
            self._mkdir_p(sftp, self.root)
            try:
                with sftp.open(probe, "wb") as f:
                    f.write(b"")
                started = time.monotonic()
                mtime = sftp.stat(probe).st_mtime
                roundtrip = time.monotonic() - started
                return mtime, roundtrip
            finally:
                try:
                    sftp.remove(probe)
                except FileNotFoundError:
                    pass

        return self._with_retry(operation)

    def _presence_path(self, peer_id: str) -> str:
        return posixpath.join(self.root, "identities", peer_id, "presence.json")

    def _stat_mtime(self, path: str) -> float | None:
        def operation(sftp):
            try:
                return sftp.stat(path).st_mtime
            except FileNotFoundError:
                return None

        return self._with_retry(operation)

    def _peer_dir(self, topic_uuid: str, peer_id: str) -> str:
        return posixpath.join(self.root, "topics", topic_uuid, "peers", peer_id)

    # Connection handling - lazy connect, retry once on failure (a dropped
    # SSH connection between poll cycles is the common case, not the
    # exception, for a long-running background loop). A read that times out
    # raises TimeoutError, which is an OSError, so it resets and retries on
    # the same path as a dropped connection - bounding one operation at
    # roughly two timeouts rather than leaving it open-ended.

    def _sftp_client(self):
        if self._sftp is None:
            self._connect()
        return self._sftp

    def _connect(self) -> None:
        import paramiko

        client = paramiko.SSHClient()
        # Trust-on-first-use, not pinned host key verification - documented
        # tradeoff at module level, adequate for this MVP's threat model
        # (one personal server, not a public multi-tenant relay).
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs: dict = {
            "hostname": self.host,
            "port": self.port,
            "username": self.username,
            # Three separate timeouts because paramiko bounds three separate
            # waits, and "timeout" is only the first of them: the TCP
            # handshake, then the SSH banner, then authentication. A host
            # that accepts a connection and then says nothing hangs on the
            # second or third regardless of the first.
            "timeout": self.connect_timeout,
            "banner_timeout": self.connect_timeout,
            "auth_timeout": self.connect_timeout,
        }
        if self.private_key_path:
            connect_kwargs["key_filename"] = self.private_key_path
            if self.private_key_passphrase:
                connect_kwargs["passphrase"] = self.private_key_passphrase
        if self.password:
            connect_kwargs["password"] = self.password
        client.connect(**connect_kwargs)
        transport = client.get_transport()
        if transport is not None and self.keepalive_seconds > 0:
            # Makes a silent peer surface as a dead transport instead of an
            # open socket nobody is answering.
            transport.set_keepalive(int(self.keepalive_seconds))
        sftp = client.open_sftp()
        channel = sftp.get_channel()
        if channel is not None and self.operation_timeout > 0:
            # Bounds each read on the channel, not the operation as a whole,
            # so a large blob still transfers for as long as data keeps
            # arriving - it is silence that is refused, not slowness.
            channel.settimeout(self.operation_timeout)
        self._client = client
        self._sftp = sftp

    def _reset_connection(self) -> None:
        try:
            if self._sftp:
                self._sftp.close()
        except Exception:
            pass
        try:
            if self._client:
                self._client.close()
        except Exception:
            pass
        self._client = None
        self._sftp = None

    def _with_retry(self, operation, name: str | None = None):
        import paramiko

        with self._operation_lock:
            started = time.monotonic()
            try:
                result = operation(self._sftp_client())
                # Reported per operation because the phase timings above
                # aggregate several of these, and two clients talking to the
                # same relay were observed 4x apart in one phase with no way
                # to tell a slow link from extra round trips.
                self._emit(
                    "relay.sftp_operation",
                    trace_level="timing",
                    # Every caller names its inner closure `operation`, so the
                    # useful label is the method that built it.
                    operation=name or sys._getframe(1).f_code.co_name,
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                )
                return result
            except paramiko.AuthenticationException:
                self._reset_connection()
                raise
            except (OSError, paramiko.SSHException) as exc:
                # A dropped connection is absorbed here and the phase above
                # still reports ok=True, so without this event a reconnect is
                # visible only as an unexplained spike in a phase duration -
                # which is how the cost had to be found the first time.
                started = time.monotonic()
                self._reset_connection()
                try:
                    return operation(self._sftp_client())
                finally:
                    self._emit(
                        "relay.sftp_reconnect",
                        error_type=type(exc).__name__,
                        error=str(exc),
                        reconnect_ms=round(
                            (time.monotonic() - started) * 1000, 1,
                        ),
                    )

    def _emit(self, kind: str, **fields) -> None:
        """Report a transport event, if anyone is listening.

        A callback rather than a Session reference: storage is deliberately
        ignorant of what is being synchronised, and a reconnect is a fact
        about the link, not about any topic on it.
        """
        callback = self.on_event
        if callback is None:
            return
        try:
            callback(kind, **fields)
        except Exception:
            # Never let reporting a transport fault cause one.
            pass

    def _write_json(self, path: str, data: dict) -> None:
        payload = (json.dumps(data, sort_keys=True, indent=2) + "\n").encode("utf-8")
        tmp_path = f"{path}.{uuid_mod.uuid4().hex}.tmp"

        def operation(sftp):
            self._mkdir_p(sftp, posixpath.dirname(path))
            with sftp.open(tmp_path, "wb") as f:
                f.write(payload)
            self._atomic_rename(sftp, tmp_path, path)

        self._with_retry(operation)

    def _read_json(self, path: str) -> dict | None:
        def operation(sftp):
            try:
                with sftp.open(path, "rb") as f:
                    return json.loads(f.read().decode("utf-8"))
            except FileNotFoundError:
                return None

        return self._with_retry(operation)

    def _list_dir(self, path: str) -> list[str]:
        def operation(sftp):
            try:
                attrs = sftp.listdir_attr(path)
            except FileNotFoundError:
                return []
            return sorted(a.filename for a in attrs if stat.S_ISDIR(a.st_mode or 0))

        return self._with_retry(operation)

    @staticmethod
    def _mkdir_p(sftp, path: str) -> None:
        if not path or path in ("/", ""):
            return
        try:
            sftp.stat(path)
            return
        except FileNotFoundError:
            pass
        SftpRelayStorage._mkdir_p(sftp, posixpath.dirname(path))
        try:
            sftp.mkdir(path)
        except OSError:
            # Lost a race against another writer creating the same
            # directory concurrently - fine, the end state is what matters.
            sftp.stat(path)

    @staticmethod
    def _atomic_rename(sftp, tmp_path: str, final_path: str) -> None:
        # posix_rename is an SFTP protocol extension (atomic, overwrites the
        # destination) that OpenSSH's sftp-server supports but plain SFTP
        # rename does not guarantee - fall back to remove-then-rename for
        # servers that don't support the extension. The fallback has a
        # brief window where final_path doesn't exist, unlike the extension
        # - acceptable here since only the same identity ever writes its
        # own path (no concurrent writer to race against).
        try:
            sftp.posix_rename(tmp_path, final_path)
        except Exception:
            try:
                sftp.remove(final_path)
            except FileNotFoundError:
                pass
            sftp.rename(tmp_path, final_path)
