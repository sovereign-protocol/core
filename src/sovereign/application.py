"""Public contracts for applications hosted by Sovereign Core."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .topic_registry import ApplicationRegistration


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
    adapter: Any
    blob_store: Any
    trace: Any
    relay_manager: Any
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
