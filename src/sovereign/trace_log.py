from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TraceLogger:
    _LEVELS = {"off": 0, "events": 1, "timing": 2}

    def __init__(self, path: str | None = None, node: str | None = None,
                 level: str = "events"):
        self.level = self.normalize_level(level)
        self.path = path if self.level != "off" else None
        self.node = node
        self._lock = threading.Lock()
        if self.path:
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory:
                os.makedirs(directory, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def event(self, kind: str, *, required_level: str = "events",
              **fields: Any) -> None:
        required = self.normalize_level(required_level)
        if (
            not self.path
            or self._LEVELS[self.level] < self._LEVELS[required]
        ):
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
        raw_environment_level = os.environ.get("SOVEREIGN_TRACE")
        level = cls.normalize_level(
            raw_environment_level
            if raw_environment_level is not None
            else config.get("trace_level", "events")
        )
        if level == "off":
            return cls.disabled()
        path = config.get("trace_log_file") or os.environ.get(
            "SOVEREIGN_TRACE_LOG",
        )
        if not path and raw_environment_level is not None:
            path = str(Path.cwd() / "data" / f"trace_{port}.jsonl")
        return cls(path, node=address, level=level)

    @classmethod
    def disabled(cls) -> "TraceLogger":
        return cls(level="off")

    @classmethod
    def normalize_level(cls, value: Any) -> str:
        if value is None:
            return "off"
        normalized = str(value).strip().lower()
        if normalized in {"", "0", "false", "no", "off", "none"}:
            return "off"
        if normalized == "timing":
            return "timing"
        # Backward compatibility: SOVEREIGN_TRACE=1/true and any other
        # previously accepted non-empty value enable ordinary event tracing.
        return "events"

    @classmethod
    def _jsonable(cls, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(key): cls._jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._jsonable(item) for item in value]
        return str(value)
