"""Protocol Explorer manifest and host wiring."""

from .application import (
    ApplicationInstance, ApplicationManifest, ApplicationServices,
)
from .protocol_explorer import ManualLogic
from .protocol_explorer_controller import build_routes


APPLICATION_MANIFEST = ApplicationManifest(
    application_id="protocol-explorer",
    display_name="Protocol Explorer",
    data_schema_version=1,
    asset_package="sovereign.assets",
    ui_file="manual.html",
    css_file="manual.css",
)


def create_application(services: ApplicationServices) -> ApplicationInstance:
    logic = ManualLogic(services.session, dict(services.settings))
    return ApplicationInstance(
        manifest=APPLICATION_MANIFEST,
        logic=logic,
        registration=None,
        controllers=tuple(build_routes(logic, services, dict(services.settings))),
    )
