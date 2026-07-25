import sys
import types
import unittest
from unittest.mock import patch

from starlette.routing import Route

from sovereign.application import (
    ApplicationFacade,
    ApplicationInstance,
    ApplicationManifest,
    ApplicationServices,
)
from sovereign.host import ApplicationHost
from sovereign.protocol import ProtocolNode
from sovereign.session import Session
from sovereign.topic_registry import ApplicationRegistration


class _Adapter:
    def execute_effects(self, _effects):
        pass


def _services(session):
    return ApplicationServices(
        session=session,
        collaboration=object(),
        deliver_effects=_Adapter().execute_effects,
        blob_store=object(),
        trace=None,
        notify_change=lambda _kind: None,
        collect_local_blobs=lambda: [],
    )


def _application_module(
    application_id,
    *,
    root_type=None,
    route_path=None,
    on_close=None,
    facade_api_version=None,
    facade_api=None,
):
    manifest = ApplicationManifest(application_id, application_id, 1)
    module = types.ModuleType(f"test_{application_id}")
    module.APPLICATION_MANIFEST = manifest

    def create_application(services):
        registration = None
        if root_type:
            registration = ApplicationRegistration(
                application_id=application_id,
                root_types=frozenset({root_type}),
                list_topics=lambda: [],
                accept_invitation=services.session.accept_topic_invitation,
                assignment_scoped=True,
                mount_invitation=True,
            )
        controllers = ()
        if route_path:
            async def endpoint(_request):
                return None
            controllers = (Route(route_path, endpoint),)
        return ApplicationInstance(
            manifest,
            object(),
            registration,
            controllers,
            close_callback=on_close,
            facade=(
                ApplicationFacade(
                    application_id,
                    facade_api_version,
                    facade_api,
                )
                if facade_api_version is not None else None
            ),
        )

    module.create_application = create_application
    return module


class ApplicationHostTests(unittest.TestCase):
    def test_facade_lookup_is_late_bound_versioned_and_removed_on_deactivate(self):
        session = Session("http://a")
        api = object()
        module = _application_module(
            "notes", facade_api_version=2, facade_api=api,
        )
        services = _services(session)
        with patch.dict(sys.modules, {"test_notes": module}):
            host = ApplicationHost(services)
            self.assertIsNone(host.services.facades.find("notes", 2))
            host.activate("test_notes")
            self.assertIs(host.services.facades.find("notes", 2), api)
            with self.assertRaisesRegex(ValueError, "expected 1"):
                host.services.facades.find("notes", 1)
            host.deactivate("notes")
            self.assertIsNone(host.services.facades.find("notes", 2))

    def test_application_summaries_drive_shell_navigation(self):
        session = Session("http://a")
        notes = _application_module("notes")
        tasks = _application_module("tasks")
        services = _services(session)
        with patch.dict(
            sys.modules, {"test_notes": notes, "test_tasks": tasks},
        ):
            host = ApplicationHost(services, ["test_notes", "test_tasks"])

            summaries = host.application_summaries()
            self.assertEqual(
                [item["application_id"] for item in summaries], ["notes", "tasks"],
            )
            self.assertEqual(
                [item["asset_prefix"] for item in summaries],
                ["/apps/notes", "/apps/tasks"],
            )
            # The shell needs to know which application "/" will serve. Note
            # primary is resolved from the constructor's specs only, so an
            # application activated later is never promoted to primary.
            self.assertEqual(
                [item["primary"] for item in summaries], [True, False],
            )

            # Deactivating removes a navigation entry rather than leaving a
            # link that would 404.
            host.deactivate("notes")
            self.assertEqual(
                [item["application_id"] for item in host.application_summaries()],
                ["tasks"],
            )

    def test_activate_and_deactivate_remove_runtime_surface_but_keep_data(self):
        session = Session("http://a")
        topic = session.create_child(
            session.protocol.root.uuid, {"type": "notes", "name": "kept"}, {},
        ).value
        closed = []
        module = _application_module(
            "notes", root_type="notes", route_path="/api/notes/ping",
            on_close=lambda: closed.append(True),
        )
        with patch.dict(sys.modules, {"test_notes": module}):
            host = ApplicationHost(_services(session), ["test_notes"])
            self.assertEqual(session.registered_application_ids(), ("notes",))
            self.assertEqual(
                [route.path for route in host.controller_routes()],
                ["/api/notes/ping"],
            )
            host.deactivate("notes")

        self.assertEqual(session.registered_application_ids(), ())
        self.assertEqual(host.controller_routes(), [])
        self.assertIsNotNone(session.get_subtree(topic.uuid))
        self.assertEqual(closed, [True])

    def test_activation_rejects_root_type_owned_by_another_application(self):
        session = Session("http://a")
        first = _application_module("first", root_type="agreement")
        second = _application_module("second", root_type="agreement")
        with patch.dict(sys.modules, {
            "test_first": first,
            "test_second": second,
        }):
            host = ApplicationHost(_services(session), ["test_first"])
            with self.assertRaisesRegex(ValueError, "already handled"):
                host.activate("test_second")

        self.assertEqual(session.registered_application_ids(), ("first",))

    def test_activation_rejects_duplicate_application_id(self):
        session = Session("http://a")
        first = _application_module("notes", root_type="notes")
        duplicate = _application_module("notes", root_type="other-notes")
        with patch.dict(sys.modules, {
            "test_notes_first": first,
            "test_notes_duplicate": duplicate,
        }):
            host = ApplicationHost(_services(session), ["test_notes_first"])
            with self.assertRaisesRegex(ValueError, "already active"):
                host.activate("test_notes_duplicate")

        self.assertEqual(session.registered_application_ids(), ("notes",))

    def test_activation_mounts_matching_topic_cached_before_app_loaded(self):
        session = Session("http://a")
        cached = ProtocolNode({"type": "agreement", "name": "Cached"})
        session.apply_peer_subtree("relay:B", cached, None)
        session.note_pending_topic_invitation(cached.uuid)
        module = _application_module("agreement", root_type="agreement")

        self.assertIsNone(session.get_subtree(cached.uuid))
        with patch.dict(sys.modules, {"test_agreement": module}):
            ApplicationHost(_services(session), ["test_agreement"])

        self.assertIsNotNone(session.get_subtree(cached.uuid))

    def test_activation_does_not_mount_passively_observed_topic(self):
        session = Session("http://a")
        cached = ProtocolNode({"type": "agreement", "name": "Not invited"})
        session.apply_peer_subtree("relay:B", cached, None)
        module = _application_module("agreement", root_type="agreement")

        with patch.dict(sys.modules, {"test_agreement": module}):
            ApplicationHost(_services(session), ["test_agreement"])

        self.assertIsNone(session.get_subtree(cached.uuid))

    def test_route_must_be_inside_application_namespace(self):
        session = Session("http://a")
        module = _application_module("notes", route_path="/api/wrong/ping")
        with patch.dict(sys.modules, {"test_notes": module}):
            with self.assertRaisesRegex(ValueError, "must be under"):
                ApplicationHost(_services(session), ["test_notes"])

    def test_bind_rejects_collision_with_core_route(self):
        session = Session("http://a")
        module = _application_module("network", route_path="/api/network")
        with patch.dict(sys.modules, {"test_network": module}):
            host = ApplicationHost(_services(session), ["test_network"])
        core_route = Route("/api/network", lambda _request: None)

        with self.assertRaisesRegex(ValueError, "collides with a Core route"):
            host.bind_starlette(object(), [core_route])


if __name__ == "__main__":
    unittest.main()
