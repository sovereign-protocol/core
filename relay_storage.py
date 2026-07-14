"""
Relay storage backends.

Functionality:
  Store and retrieve peer snapshot/head files for the file-mailbox relay
  (see relay_logic.py). Each peer identity only ever writes into its own
  folder - a storage backend never merges or interprets content, it just
  reads and writes bytes at a path.

Offered API:
  LocalFolderRelayStorage(root)
    write_snapshot(topic_uuid, peer_id, state_hash, payload)
    read_head(topic_uuid, peer_id) -> dict | None
    read_snapshot(topic_uuid, peer_id, state_hash) -> dict | None
    list_peers(topic_uuid) -> list[str]
    list_topics() -> list[str]

Used API:
  Python standard library only.

Layout on disk (or on any future storage backend with the same shape):
  <root>/topics/<topic_uuid>/peers/<peer_id>/head.json
  <root>/topics/<topic_uuid>/peers/<peer_id>/snapshots/<state_hash>.json

A future WebDAV/SFTP backend implements the same four methods against a
remote endpoint instead of a local path - nothing above this module needs to
change to support that.
"""

from __future__ import annotations

import json
import os
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
