"""Public contracts for applications hosted by Sovereign Core."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Mapping

from .topic_registry import ApplicationRegistration

if TYPE_CHECKING:
    from .channel import ChannelManager


_APPLICATION_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class ApplicationManifest:
    application_id: str
    display_name: str
    data_schema_version: int
    asset_package: str | None = None
    ui_file: str | None = None
    css_file: str | None = None

    def __post_init__(self) -> None:
        if not _APPLICATION_ID_RE.fullmatch(self.application_id):
            raise ValueError(
                "application_id must use lowercase letters, digits, and hyphens"
            )
        if not self.display_name.strip():
            raise ValueError("application display_name is required")
        if self.data_schema_version < 1:
            raise ValueError("application data_schema_version must be positive")
        if bool(self.ui_file) != bool(self.asset_package):
            raise ValueError("ui_file and asset_package must be declared together")

    @property
    def api_prefix(self) -> str:
        return f"/api/{self.application_id}"

    @property
    def asset_prefix(self) -> str:
        return f"/apps/{self.application_id}"


@dataclass(frozen=True)
class ApplicationServices:
    """Typed, read-only services supplied to one active application."""

    session: Any
    channel_manager: "ChannelManager"
    blob_store: Any
    trace: Any
    notify_change: Callable[[str], None]
    collect_local_blobs: Callable[[], list[str]]
    settings: Mapping[str, Any] = MappingProxyType({})

    def with_settings(self, settings: Mapping[str, Any] | None) -> "ApplicationServices":
        return replace(self, settings=MappingProxyType(dict(settings or {})))


@dataclass
class ApplicationInstance:
    manifest: ApplicationManifest
    logic: Any
    registration: ApplicationRegistration | None
    controllers: tuple[Any, ...] = ()
    close_callback: Callable[[], None] | None = None

    def close(self) -> None:
        if self.close_callback:
            self.close_callback()


@dataclass(frozen=True)
class ApplicationSpec:
    module: str
    settings: Mapping[str, Any] = MappingProxyType({})

    @classmethod
    def from_value(cls, value: str | Mapping[str, Any]) -> "ApplicationSpec":
        if isinstance(value, str):
            module = value.strip()
            settings = {}
        elif isinstance(value, Mapping):
            module = str(value.get("module") or "").strip()
            settings = value.get("settings") or {}
        else:
            raise ValueError("application entry must be a module string or object")
        if not module:
            raise ValueError("application module is required")
        if not isinstance(settings, Mapping):
            raise ValueError("application settings must be an object")
        return cls(module, MappingProxyType(dict(settings)))


JsonValue = (
    bool | int | float | str | None
    | list["JsonValue"] | dict[str, "JsonValue"]
)


@dataclass(frozen=True)
class ApplicationResultView:
    """Framework-neutral, JSON-safe presentation of an application action."""

    payload: dict[str, JsonValue]
    ok: bool


def application_result_view(result, deliveries=()) -> ApplicationResultView:
    if result.status != "ok":
        return ApplicationResultView(
            {"status": "error", "reason": str(result.reason or "unknown error")},
            False,
        )
    payload: dict[str, JsonValue] = {"status": "ok"}
    if result.value is not None:
        payload["value"] = json_value(result.value)
    errors = [item for item in deliveries if not item.ok]
    if errors:
        payload["delivery_errors"] = [
            {
                "effect_type": str(item.effect_type),
                "target": json_value(item.target),
                "reason": str(item.reason or "delivery failed"),
            }
            for item in errors
        ]
    return ApplicationResultView(payload, True)


def json_value(value: Any) -> JsonValue:
    """Convert public application values or reject a leaky internal object."""
    if isinstance(value, float) and not math.isfinite(value):
        raise TypeError("application result contains a non-finite number")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "to_dict"):
        return json_value(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, set):
        return [json_value(item) for item in sorted(value, key=repr)]
    raise TypeError(
        f"application result value is not JSON-serializable: {type(value).__name__}"
    )
