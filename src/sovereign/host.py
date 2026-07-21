"""Application loading and lifecycle for one Sovereign runtime."""

from __future__ import annotations

import importlib
import threading
from dataclasses import replace
from importlib.resources import files
from typing import Any, Iterable

from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from .application import (
    ApplicationFacade, ApplicationInstance, ApplicationManifest,
    ApplicationServices, ApplicationSpec, IncompatibleApplicationFacade,
)


class _ApplicationFacadeRegistry:
    """Host-owned registry; applications receive only its lookup surface."""

    def __init__(self) -> None:
        self._facades: dict[str, ApplicationFacade] = {}
        self._lock = threading.RLock()

    def register(self, facade: ApplicationFacade) -> None:
        with self._lock:
            if facade.application_id in self._facades:
                raise ValueError(
                    f"facade for {facade.application_id!r} is already active"
                )
            self._facades[facade.application_id] = facade

    def unregister(self, application_id: str) -> None:
        with self._lock:
            self._facades.pop(application_id, None)

    def find(self, application_id: str, facade_api_version: int) -> Any | None:
        with self._lock:
            facade = self._facades.get(application_id)
        if facade is None:
            return None
        if facade.facade_api_version != facade_api_version:
            raise IncompatibleApplicationFacade(
                f"application {application_id!r} exposes facade API "
                f"{facade.facade_api_version}, expected {facade_api_version}"
            )
        return facade.api


class ApplicationHost:
    """Load explicit application plugins and own their runtime lifecycle."""

    def __init__(
        self,
        services: ApplicationServices,
        specs: Iterable[str | dict] = (),
        primary_application_id: str | None = None,
    ):
        self._facades = _ApplicationFacadeRegistry()
        self.services = replace(services, facades=self._facades)
        self.primary_application_id = primary_application_id
        self._instances: dict[str, ApplicationInstance] = {}
        self._modules: dict[str, str] = {}
        self._routes: dict[str, tuple[Any, ...]] = {}
        self._route_owner: dict[str, str] = {}
        self._reserved_paths: set[str] = set()
        self._starlette_app = None
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        for value in specs:
            self.activate(ApplicationSpec.from_value(value))
        if self.primary_application_id is None and self._instances:
            self.primary_application_id = next(iter(self._instances))
        if (
            self.primary_application_id is not None
            and self.primary_application_id not in self._instances
        ):
            raise ValueError(
                f"primary application {self.primary_application_id!r} is not active"
            )

    @property
    def instances(self) -> dict[str, ApplicationInstance]:
        with self._lock:
            return dict(self._instances)

    def application_summaries(self) -> list[dict]:
        """Describe active applications for the shared host shell.

        The shell builds navigation from this, so no application needs to
        know another application's identifier or URL. Deactivating an
        application removes its entry instead of breaking a hardcoded link.
        """
        primary = self.primary_application_id
        return [
            {
                "application_id": instance.manifest.application_id,
                "display_name": instance.manifest.display_name,
                "asset_prefix": instance.manifest.asset_prefix,
                "primary": instance.manifest.application_id == primary,
                "icon": instance.manifest.icon,
                "role": instance.manifest.role,
            }
            for instance in self.instances.values()
        ]

    @property
    def primary_instance(self) -> ApplicationInstance | None:
        with self._lock:
            return self._instances.get(self.primary_application_id or "")

    @property
    def primary_logic(self):
        instance = self.primary_instance
        return instance.logic if instance else None

    def activate(self, spec: ApplicationSpec | str | dict) -> ApplicationInstance:
        with self._lifecycle_lock:
            return self._activate(spec)

    def _activate(self, spec: ApplicationSpec | str | dict) -> ApplicationInstance:
        if not isinstance(spec, ApplicationSpec):
            spec = ApplicationSpec.from_value(spec)
        module = importlib.import_module(spec.module)
        manifest = getattr(module, "APPLICATION_MANIFEST", None)
        factory = getattr(module, "create_application", None)
        if not isinstance(manifest, ApplicationManifest) or not callable(factory):
            raise ValueError(
                f"application module {spec.module!r} must export "
                "APPLICATION_MANIFEST and create_application(services)"
            )
        with self._lock:
            if manifest.application_id in self._instances:
                raise ValueError(
                    f"application {manifest.application_id!r} is already active"
                )
        services = self.services.with_settings(spec.settings)
        instance = factory(services)
        if not isinstance(instance, ApplicationInstance):
            raise TypeError(
                f"application {manifest.application_id!r} returned an invalid instance"
            )
        if instance.manifest != manifest:
            raise ValueError(
                f"application {manifest.application_id!r} instance manifest mismatch"
            )
        registered = False
        facade_registered = False
        try:
            routes = tuple(instance.controllers) + self._asset_routes(manifest)
            self._validate_routes(manifest, routes)
            if instance.registration:
                if instance.registration.application_id != manifest.application_id:
                    raise ValueError("application registration id does not match manifest")
                self.services.session.register_application(instance.registration)
                registered = True
                self.services.session.mount_cached_topics(manifest.application_id)
            if instance.facade:
                if instance.facade.application_id != manifest.application_id:
                    raise ValueError("application facade id does not match manifest")
                self._facades.register(instance.facade)
                facade_registered = True
            with self._lock:
                self._instances[manifest.application_id] = instance
                self._modules[manifest.application_id] = spec.module
                self._routes[manifest.application_id] = routes
                for route in routes:
                    self._route_owner[route.path] = manifest.application_id
                if self._starlette_app is not None:
                    self._starlette_app.router.routes.extend(routes)
        except Exception:
            if facade_registered:
                self._facades.unregister(manifest.application_id)
            if registered:
                self.services.session.unregister_application(manifest.application_id)
            instance.close()
            raise
        return instance

    def deactivate(self, application_id: str) -> None:
        with self._lifecycle_lock:
            self._deactivate(application_id)

    def _deactivate(self, application_id: str) -> None:
        with self._lock:
            instance = self._instances.get(application_id)
            if not instance:
                return
            routes = self._routes.get(application_id, ())
            if self._starlette_app is not None:
                remove = {id(route) for route in routes}
                self._starlette_app.router.routes[:] = [
                    route for route in self._starlette_app.router.routes
                    if id(route) not in remove
                ]
            self._instances.pop(application_id, None)
            self._modules.pop(application_id, None)
            self._routes.pop(application_id, None)
            for route in routes:
                self._route_owner.pop(route.path, None)
            if self.primary_application_id == application_id:
                self.primary_application_id = next(iter(self._instances), None)
        if instance.registration:
            self.services.session.unregister_application(application_id)
        self._facades.unregister(application_id)
        instance.close()

    def close(self) -> None:
        for application_id in reversed(tuple(self.instances)):
            self.deactivate(application_id)

    def controller_routes(self) -> list[Any]:
        with self._lock:
            return [
                route
                for application_id in self._instances
                for route in self._routes[application_id]
            ]

    def bind_starlette(self, app, core_routes: Iterable[Any]) -> None:
        core_paths = {route.path for route in core_routes if hasattr(route, "path")}
        overlap = sorted(core_paths & set(self._route_owner))
        if overlap:
            path = overlap[0]
            raise ValueError(
                f"route {path!r} for application {self._route_owner[path]!r} "
                "collides with a Core route"
            )
        with self._lock:
            self._reserved_paths = core_paths
            self._starlette_app = app

    def notify_peer_update(self) -> bool:
        changed = False
        for instance in self.instances.values():
            hook = (
                instance.registration.on_peer_update
                if instance.registration else None
            )
            if not hook:
                continue
            result = hook()
            if getattr(result, "status", None) != "ok":
                continue
            effects = getattr(result, "effects", ())
            if effects:
                self.services.channel_manager.execute_effects(effects)
            changed = bool(getattr(result, "value", False)) or changed
        return changed

    def read_primary_asset(self, kind: str) -> str:
        instance = self.primary_instance
        if not instance:
            return ""
        manifest = instance.manifest
        filename = manifest.ui_file if kind == "ui" else manifest.css_file
        if not filename or not manifest.asset_package:
            return ""
        return files(manifest.asset_package).joinpath(filename).read_text(encoding="utf-8")

    def _validate_routes(
        self, manifest: ApplicationManifest, routes: tuple[Any, ...],
    ) -> None:
        seen = set()
        for route in routes:
            path = getattr(route, "path", "")
            if path.startswith("/api/") and not (
                path == manifest.api_prefix
                or path.startswith(f"{manifest.api_prefix}/")
            ):
                raise ValueError(
                    f"application {manifest.application_id!r} route {path!r} "
                    f"must be under {manifest.api_prefix!r}"
                )
            if not path.startswith("/api/") and not (
                path == manifest.asset_prefix
                or path.startswith(f"{manifest.asset_prefix}/")
            ):
                raise ValueError(
                    f"application {manifest.application_id!r} asset route {path!r} "
                    f"must be under {manifest.asset_prefix!r}"
                )
            if path in seen:
                raise ValueError(
                    f"application {manifest.application_id!r} duplicates route {path!r}"
                )
            owner = self._route_owner.get(path)
            if owner:
                raise ValueError(
                    f"application {manifest.application_id!r} route {path!r} "
                    f"collides with application {owner!r}"
                )
            if path in self._reserved_paths:
                raise ValueError(
                    f"application {manifest.application_id!r} route {path!r} "
                    "collides with a Core route"
                )
            seen.add(path)

    @staticmethod
    def _asset_routes(manifest: ApplicationManifest) -> tuple[Any, ...]:
        if not manifest.asset_package or not manifest.ui_file:
            return ()

        async def ui(_request):
            return HTMLResponse(
                files(manifest.asset_package).joinpath(manifest.ui_file).read_text(
                    encoding="utf-8",
                )
            )

        routes = [Route(manifest.asset_prefix, ui)]
        if manifest.css_file:
            async def css(_request):
                return Response(
                    files(manifest.asset_package).joinpath(manifest.css_file).read_text(
                        encoding="utf-8",
                    ),
                    media_type="text/css",
                )
            routes.append(Route(f"{manifest.asset_prefix}/styles.css", css))
        return tuple(routes)
