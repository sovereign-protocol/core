from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TraceLogger:
    def __init__(self, path: str | None = None, node: str | None = None):
        self.path = path
        self.node = node
        self._lock = threading.Lock()
        if path:
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def event(self, kind: str, **fields: Any) -> None:
        if not self.path:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "node": self.node,
            "kind": kind,
        }
        record.update({key: self._jsonable(value) for key, value in fields.items()})
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    @classmethod
    def from_config(cls, config: dict, port: int, address: str) -> "TraceLogger":
        path = config.get("trace_log_file") or os.environ.get("SKANBAN_TRACE_LOG")
        if not path and os.environ.get("SKANBAN_TRACE"):
            path = str(Path.cwd() / "data" / f"trace_{port}.jsonl")
        return cls(path, node=address)

    @classmethod
    def disabled(cls) -> "TraceLogger":
        return cls()

    @classmethod
    def _jsonable(cls, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(key): cls._jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._jsonable(item) for item in value]
        return str(value)
