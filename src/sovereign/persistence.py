"""Atomic Session envelope persistence."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

from .protocol import ProtocolNode, UnsupportedProtocolVersion
from .session import Session
from .versions import (
    PROTOCOL_SCHEMA_VERSION,
    SESSION_ENVELOPE_FORMAT,
    SESSION_ENVELOPE_VERSION,
)


_SAVE_LOCKS: dict[str, threading.Lock] = {}
_SAVE_LOCKS_GUARD = threading.Lock()


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
                    "[persistence] in-memory session has invalid hashes before "
                    f"save {absolute_path}: {exc}"
                )
                repaired = ProtocolNode.from_dict(
                    protocol_snapshot, repair_hashes=True,
                )
                session.load_protocol_root(repaired)
                protocol_snapshot = repaired.to_dict()
            snapshot = {
                "format": SESSION_ENVELOPE_FORMAT,
                "version": SESSION_ENVELOPE_VERSION,
                "protocol_schema_version": PROTOCOL_SCHEMA_VERSION,
                "protocol_root": protocol_snapshot,
                "session": session.persistence_metadata(),
            }
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, sort_keys=True, indent=2)
            handle.write("\n")
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


def load_session_from_file(
    session: Session, path: str, logger=print,
) -> bool:
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    envelope_error = _session_envelope_error(payload)
    if envelope_error:
        logger(
            f"[persistence] unsupported session format {path}: "
            f"{envelope_error}"
        )
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
        logger(
            f"[persistence] unsupported Core data schema {path}: "
            f"{core_schema_error}"
        )
        return False
    session.load_protocol_root(root)
    session.restore_persistence_metadata(payload.get("session", {}))
    if root.to_dict() != protocol_payload:
        logger(f"[persistence] saving repaired session {path}")
        save_session_to_file(session, path, logger=logger)
    return True


def _session_envelope_error(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return "expected a JSON object"
    if payload.get("format") == "prsp-session-v1":
        return "legacy format 'prsp-session-v1' is not supported"
    if payload.get("format") != SESSION_ENVELOPE_FORMAT:
        return f"expected format '{SESSION_ENVELOPE_FORMAT}'"
    if payload.get("version") != SESSION_ENVELOPE_VERSION:
        return f"unsupported envelope version {payload.get('version')!r}"
    if payload.get("protocol_schema_version") not in {
        1, PROTOCOL_SCHEMA_VERSION,
    }:
        return (
            "unsupported protocol schema version "
            f"{payload.get('protocol_schema_version')!r}"
        )
    if "protocol_root" not in payload:
        return "missing protocol_root"
    return None
