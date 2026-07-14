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
                   private_key_path=None, private_key_passphrase=None)
    Both implement the same five methods:
    write_snapshot(topic_uuid, peer_id, state_hash, payload)
    read_head(topic_uuid, peer_id) -> dict | None
    read_snapshot(topic_uuid, peer_id, state_hash) -> dict | None
    list_peers(topic_uuid) -> list[str]
    list_topics() -> list[str]

Used API:
  LocalFolderRelayStorage: Python standard library only.
  SftpRelayStorage: paramiko (lazily imported - only needed if this class is
    actually instantiated, so a local-folder-only setup never needs it
    installed).

Layout (identical on both backends):
  <root>/topics/<topic_uuid>/peers/<peer_id>/head.json
  <root>/topics/<topic_uuid>/peers/<peer_id>/snapshots/<state_hash>.json

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
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class LocalFolderRelayStorage:
    def __init__(self, root: str):
        self.root = Path(root)

    def write_snapshot(self, topic_uuid: str, peer_id: str, state_hash: str,
                       payload: dict) -> None:
        peer_dir = self._peer_dir(topic_uuid, peer_id)
        snapshots_dir = peer_dir / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(snapshots_dir / f"{state_hash}.json", payload)
        head = {
            "peer": peer_id,
            "topic": topic_uuid,
            "hash": state_hash,
            "updated_at": now_iso(),
            "snapshot": f"snapshots/{state_hash}.json",
        }
        self._write_json(peer_dir / "head.json", head)

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
    def __init__(self, host: str, username: str, remote_root: str,
                port: int = 22, password: str | None = None,
                private_key_path: str | None = None,
                private_key_passphrase: str | None = None,
                connect_timeout: float = 10.0):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.private_key_path = private_key_path
        self.private_key_passphrase = private_key_passphrase
        self.connect_timeout = connect_timeout
        # Kept as a plain posix-style string, not pathlib.Path - remote SFTP
        # paths are always forward-slash regardless of the host OS this
        # process happens to be running on (matters on Windows).
        self.root = remote_root.rstrip("/") or "/"
        self._client = None
        self._sftp = None

    def write_snapshot(self, topic_uuid: str, peer_id: str, state_hash: str,
                       payload: dict) -> None:
        peer_dir = self._peer_dir(topic_uuid, peer_id)
        snapshots_dir = posixpath.join(peer_dir, "snapshots")
        self._write_json(posixpath.join(snapshots_dir, f"{state_hash}.json"), payload)
        head = {
            "peer": peer_id,
            "topic": topic_uuid,
            "hash": state_hash,
            "updated_at": now_iso(),
            "snapshot": f"snapshots/{state_hash}.json",
        }
        self._write_json(posixpath.join(peer_dir, "head.json"), head)

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

    def _peer_dir(self, topic_uuid: str, peer_id: str) -> str:
        return posixpath.join(self.root, "topics", topic_uuid, "peers", peer_id)

    # Connection handling - lazy connect, retry once on failure (a dropped
    # SSH connection between poll cycles is the common case, not the
    # exception, for a long-running background loop).

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
            "timeout": self.connect_timeout,
        }
        if self.private_key_path:
            connect_kwargs["key_filename"] = self.private_key_path
            if self.private_key_passphrase:
                connect_kwargs["passphrase"] = self.private_key_passphrase
        if self.password:
            connect_kwargs["password"] = self.password
        client.connect(**connect_kwargs)
        self._client = client
        self._sftp = client.open_sftp()

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

    def _with_retry(self, operation):
        import paramiko

        try:
            return operation(self._sftp_client())
        except (OSError, paramiko.SSHException):
            self._reset_connection()
            return operation(self._sftp_client())

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
