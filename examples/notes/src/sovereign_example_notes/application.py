"""Manifest and host wiring — the four things Core asks an application for."""

from sovereign import ApplicationInstance, ApplicationManifest, ApplicationServices

from .controller import build_routes
from .logic import APPLICATION_ID, NotesLogic


APPLICATION_MANIFEST = ApplicationManifest(
    application_id=APPLICATION_ID,
    display_name="Example Notes",
    data_schema_version=1,
    asset_package="sovereign_example_notes.assets",
    ui_file="notes.html",
    css_file="notes.css",
)


def create_application(services: ApplicationServices) -> ApplicationInstance:
    logic = NotesLogic(services.session, dict(services.settings))
    return ApplicationInstance(
        manifest=APPLICATION_MANIFEST,
        logic=logic,
        registration=logic.application_registration(),
        controllers=tuple(build_routes(logic, services)),
    )
