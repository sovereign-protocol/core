"""Content-addressed blob storage and protocol-level reference helpers."""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from pathlib import Path


BLOB_ID_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
SAFE_IMAGE_MIMES = {
    "image/gif", "image/jpeg", "image/png", "image/webp",
}


def blob_id_for(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def blob_hex(blob_id: str) -> str:
    match = BLOB_ID_RE.fullmatch(str(blob_id or ""))
    if not match:
        raise ValueError("invalid blob id")
    return match.group(1)


def canonical_attachments(value) -> list[dict]:
    """Return valid, deterministic attachment references."""
    out = {}
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            blob_hex(item.get("blob_id", ""))
        except ValueError:
            continue
        attachment_id = str(item.get("id") or "").strip()
        if not attachment_id:
            continue
        try:
            size = max(0, int(item.get("size") or 0))
        except (TypeError, ValueError):
            continue
        normalized = {
            "id": attachment_id,
            "role": str(item.get("role") or "attachment"),
            "blob_id": str(item["blob_id"]),
            "name": str(item.get("name") or "blob"),
            "size": size,
            "mime": str(item.get("mime") or "application/octet-stream").lower(),
        }
        out[attachment_id] = normalized
    return sorted(out.values(), key=lambda item: item["id"])


def is_valid_image(data: bytes, mime: str) -> bool:
    """Validate the image formats the UI is allowed to render inline."""
    mime = str(mime or "").lower()
    if mime == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if mime == "image/webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def referenced_blob_ids(root) -> set[str]:
    """Collect blob IDs from every live node in an S-Protocol subtree."""
    if root is None:
        return set()
    if isinstance(root, dict):
        if root.get("deleted", False):
            return set()
        data = root.get("data") if isinstance(root.get("data"), dict) else {}
        children = root.get("children") if isinstance(root.get("children"), list) else []
    else:
        if getattr(root, "deleted", False):
            return set()
        data = getattr(root, "data", {})
        children = getattr(root, "children", [])
    found = {
        item["blob_id"]
        for item in canonical_attachments(data.get("attachments"))
    }
    for child in children:
        found.update(referenced_blob_ids(child))
    return found


def avatar_attachment(node_or_data) -> dict | None:
    data = getattr(node_or_data, "data", node_or_data) or {}
    return next((
        item for item in canonical_attachments(data.get("attachments"))
        if item["role"] == "avatar"
    ), None)


class BlobStore:
    def __init__(self, root: str | Path, grace_seconds: float = 60.0):
        self.root = Path(root)
        self.grace_seconds = max(0.0, float(grace_seconds))

    def _path(self, blob_id: str) -> Path:
        digest = blob_hex(blob_id)
        return self.root / digest[:2] / digest

    def write_blob(self, data: bytes) -> str:
        blob_id = blob_id_for(data)
        path = self._path(blob_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            if self.read_blob(blob_id) is None:
                raise ValueError("existing blob is corrupt")
            return blob_id
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return blob_id

    def read_blob(self, blob_id: str) -> bytes | None:
        path = self._path(blob_id)
        if not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if blob_id_for(data) != blob_id:
            return None
        return data

    def has_blob(self, blob_id: str) -> bool:
        return self.read_blob(blob_id) is not None

    def delete_blob(self, blob_id: str) -> bool:
        path = self._path(blob_id)
        if not path.is_file():
            return False
        path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return True

    def iter_blob_ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        found = []
        for shard in self.root.iterdir():
            if not shard.is_dir() or len(shard.name) != 2:
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

    def collect(self, referenced: set[str], now: float | None = None) -> list[str]:
        now = time.time() if now is None else float(now)
        removed = []
        for blob_id in self.iter_blob_ids():
            if blob_id in referenced:
                continue
            path = self._path(blob_id)
            try:
                if now - path.stat().st_mtime < self.grace_seconds:
                    continue
                if self.delete_blob(blob_id):
                    removed.append(blob_id)
            except OSError:
                # A reader, antivirus scanner, or concurrent process may
                # briefly hold the file on Windows. The next sweep retries.
                continue
        return removed
