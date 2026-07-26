import asyncio
import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from sovereign import app_server
from sovereign.application import ApplicationInstance, ApplicationManifest
from sovereign.channel import ChannelManager
from sovereign.http_channel import DirectHttpChannel
from sovereign.mailbox_channel import MailboxChannel
from sovereign.protocol import ProtocolNode
from sovereign.session import Session
from sovereign.topic_registry import ApplicationRegistration
from sovereign.trace_log import TraceLogger
from starlette.responses import JSONResponse
from starlette.routing import Route


def _fake_application_module(
    application_id="fake",
    logic_factory=lambda services: object(),
    route_paths=(),
    topic_root_type=None,
):
    module = types.SimpleNamespace()
    module.APPLICATION_MANIFEST = ApplicationManifest(
        application_id=application_id,
        display_name=application_id,
        data_schema_version=1,
    )

    def create_application(services):
        logic = logic_factory(services)
        registration = None
        if topic_root_type:
            # Core only treats a node as shareable when some application has
            # claimed its root type, so anything exercising invitations needs
            # a registered owner. Core ships no such application of its own -
            # the Protocol Explorer registers nothing - so the stub stands in
            # for one rather than the suite depending on S-Kanban.
            registration = ApplicationRegistration(
                application_id=application_id,
                root_types=frozenset({topic_root_type}),
                list_topics=lambda: [
                    node.uuid
                    for node in services.session.protocol.root.children
                    if node.data.get("type") == topic_root_type
                ],
                accept_invitation=lambda tree: services.session.adopt_subtree(
                    tree, services.session.protocol.root.uuid,
                ),
                assignment_scoped=True,
                mount_invitation=True,
            )
        routes = []
        for path in route_paths:
            async def endpoint(_request, current_path=path):
                return JSONResponse({"status": "ok", "path": current_path})
            routes.append(Route(path, endpoint))
        return ApplicationInstance(
            module.APPLICATION_MANIFEST, logic, registration, tuple(routes),
        )

    module.create_application = create_application
    return module


def _runtime_with_topic(port, config, root_type="fake-topic"):
    """Build a runtime whose stub application owns one shareable topic."""
    module = _fake_application_module(topic_root_type=root_type)
    module_name = f"fake_topic_module_{port}"
    with patch.dict("sys.modules", {module_name: module}):
        runtime = app_server.create_runtime(port, {
            **config, "applications": [{"module": module_name}],
        })
    topic = runtime.session.create_child(
        runtime.session.protocol.root.uuid, {"type": root_type, "name": "Topic"},
    )
    assert topic.status == "ok", topic.reason
    return runtime, topic.value


class _FakeRelayManager:
    """Minimal stand-in for RelayManager. The channel poll tick only needs the list of
    connections to iterate; accept_connect_token only needs accept_descriptor
    (which the real manager implements by verifying, registering, provisioning
    a connection from the token, and persisting desired topics/assignments)."""

    def __init__(self, connections=(), accept_result=None):
        self._connections = list(connections)
        self.accept_calls = []
        self._accept_result = accept_result

    def all_connections(self):
        return list(self._connections)

    def polling_endpoints(self):
        return self.all_connections()

    def accept_descriptor(self, descriptor, topic_uuids, inviter_identity_uuid=None):
        self.accept_calls.append((descriptor, list(topic_uuids), inviter_identity_uuid))
        if self._accept_result is not None:
            return self._accept_result
        return types.SimpleNamespace(status="ok", reason=None, value="target-1")


class AppServerTests(unittest.TestCase):
    def _accept_connect_token(
        self, runtime, identity, topic_uuids, channels,
    ):
        manager = getattr(runtime, "channel_manager", None)
        if manager is None:
            manager = ChannelManager(runtime.session)
            direct_adapter = types.SimpleNamespace(
                join_discussion=lambda address, topic_uuid, topics: (
                    app_server._dispatch_join_discussion(
                        runtime, address, topic_uuid, topics,
                    )
                ),
            )
            manager.register(DirectHttpChannel(
                runtime.address,
                direct_adapter,
                offer_enabled=not runtime.config.get("relay_only", False),
                accept_enabled=not runtime.config.get("relay_only", False),
            ))
            relay_manager = getattr(runtime, "relay_manager", None)
            if relay_manager is not None:
                manager.register(MailboxChannel(relay_manager))
        result = manager.accept_invitation(identity, topic_uuids, channels)
        if result.ok:
            return result.value
        payload = {"status": "error", "reason": result.reason}
        if isinstance(result.value, dict):
            payload.update(result.value)
        return payload

    def test_parse_target_accepts_port_and_optional_app(self):
        self.assertEqual(app_server.parse_target("8001"), (8001, None))
        self.assertEqual(app_server.parse_target("8001:manual"), (8001, "manual"))

    def test_app_name_sets_default_module_and_files(self):
        # "manual" is Core's own built-in alias (the Protocol Explorer), so
        # this exercises alias resolution without naming an application that
        # ships from a different repository.
        config = app_server.load_config(None, "manual")

        self.assertEqual(config["app_module"], "sovereign.protocol_explorer_application")
        self.assertEqual(config["ui_file"], "manual.html")
        self.assertEqual(config["css_file"], "manual.css")

    def test_caller_supplied_alias_extends_the_built_in_table(self):
        # Applications reach the host by passing their own alias table, which
        # is how S-Kanban and Personal Cockpit are launched. Core must merge
        # it over the built-ins rather than ignoring or replacing them.
        aliases = {"demo": {
            "app_module": "demo_app.application",
            "application_id": "demo",
            "asset_package": "demo_app.assets",
            "ui_file": "demo.html",
            "css_file": "demo.css",
        }}

        config = app_server.load_config(None, "demo", aliases)
        built_in = app_server.load_config(None, "manual", aliases)

        self.assertEqual(config["app_module"], "demo_app.application")
        self.assertEqual(config["ui_file"], "demo.html")
        self.assertEqual(built_in["ui_file"], "manual.html")

    def test_explicit_config_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.json"
            path.write_text(json.dumps({
                "app_module": "custom_logic",
                "ui_file": "custom.html",
            }), encoding="utf-8")

            config = app_server.load_config(str(path), "manual")

        self.assertEqual(config["app_module"], "custom_logic")
        self.assertEqual(config["ui_file"], "custom.html")
        self.assertEqual(config["css_file"], "manual.css")

    def test_load_config_uses_explicit_application_list_without_file_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host.json"
            path.write_text(json.dumps({
                "applications": [
                    {"module": "first_app.application"},
                    {"module": "second_app.application"},
                ],
                "primary_application_id": "first",
            }), encoding="utf-8")

            config = app_server.load_config(str(path))

        self.assertEqual(
            [item["module"] for item in config["applications"]],
            ["first_app.application", "second_app.application"],
        )
        self.assertEqual(config["primary_application_id"], "first")

    def test_relay_only_runtime_drops_restored_direct_transport_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            saved = Session("http://127.0.0.1:8126")
            saved.add_peer("http://peer", "topic-1")
            saved.observed_topics["http://observer"] = {"topic-2"}
            app_server.save_session_to_file(saved, str(path))

            runtime = app_server.create_runtime(8126, {
                "app_module": None,
                "storage_file": str(path),
                "relay_only": True,
            })

        self.assertEqual(runtime.session.members, {runtime.address})
        self.assertNotIn("http://peer", runtime.session.peer_topic_sets)
        self.assertEqual(runtime.session.observed_topics, {})

    def test_persistence_roundtrip_uses_session_protocol_root(self):
        session = Session("http://a")
        child = session.create_child(
            session.protocol.root.uuid,
            {"name": "saved"},
            {},
        ).value

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app_server.save_session_to_file(session, str(path))
            payload = json.loads(path.read_text(encoding="utf-8"))

            loaded = Session("http://b")
            self.assertTrue(app_server.load_session_from_file(loaded, str(path)))

        self.assertEqual(payload["format"], "sovereign-session")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["protocol_schema_version"], 2)
        self.assertIn(child.uuid, loaded.protocol.index)
        self.assertEqual(loaded.protocol.index[child.uuid].data["name"], "saved")
        self.assertEqual(
            loaded.local_revision_seq,
            session.local_revision_seq,
        )

    def test_persistence_upgrades_v1_session_with_zero_revision_sequences(self):
        session = Session("http://a")
        child = session.create_child(
            session.protocol.root.uuid,
            {"name": "legacy"},
            {},
        ).value

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app_server.save_session_to_file(session, str(path))
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["protocol_schema_version"] = 1
            payload["session"].pop("local_revision_seq")

            def remove_revision_seq(node):
                node.pop("revision_seq")
                for nested in node.get("children", []):
                    remove_revision_seq(nested)

            remove_revision_seq(payload["protocol_root"])
            path.write_text(json.dumps(payload), encoding="utf-8")

            loaded = Session("http://b")
            self.assertTrue(app_server.load_session_from_file(loaded, str(path)))
            upgraded = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(loaded.local_revision_seq, 0)
        self.assertEqual(loaded.protocol.index[child.uuid].revision_seq, 0)
        self.assertEqual(upgraded["protocol_schema_version"], 2)
        self.assertIn("revision_seq", upgraded["protocol_root"])
        self.assertIn(
            "revision_seq",
            upgraded["protocol_root"]["children"][0],
        )

    def test_persistence_roundtrip_restores_discussion_metadata(self):
        session = Session("http://a")
        topic = session.create_child(
            session.protocol.root.uuid,
            {"name": "topic"},
            {},
        ).value
        session.start_discussion(topic.uuid)
        session.add_peer("http://b", topic.uuid)
        session.bind_peer_topic_channel("http://b", topic.uuid, "http")
        session.mark_peer_unreachable("http://b", "timeout")
        session.app_metadata["kanban"] = {"selected_board_uuid": "board-1"}
        session.watch_topic("http://c", "topic-99")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app_server.save_session_to_file(session, str(path))

            loaded = Session("http://a")
            self.assertTrue(app_server.load_session_from_file(loaded, str(path)))

        self.assertEqual(loaded.members, {"http://a", "http://b"})
        self.assertEqual(loaded.active_topic_uuids, {topic.uuid})
        self.assertEqual(loaded.peer_topic_sets["http://b"], {topic.uuid})
        self.assertEqual(loaded.peer_status["http://b"]["state"], "offline")
        self.assertEqual(
            loaded.peer_channel_for_topic("http://b", topic.uuid), "http",
        )
        self.assertEqual(loaded.app_metadata["kanban"]["selected_board_uuid"], "board-1")
        self.assertEqual(loaded.observed_topics, {"http://c": {"topic-99"}})

    def test_persistence_roundtrip_does_not_promote_relay_peer_to_a_member(self):
        # Regression, found live: a relay pseudo-address (e.g. "relay:B")
        # has its own peer_topic_sets entry by design (Session.
        # note_indirect_peer_topic) without ever having been a real member.
        # The restore path used to treat every peer_topic_sets key as
        # membership-worthy, re-adding it to session.members (and from
        # there, pending_sync_effects would try - and fail - to push a real
        # HTTP effect to it) on every single restart.
        session = Session("http://a")
        topic = session.create_child(
            session.protocol.root.uuid, {"name": "topic"}, {},
        ).value
        session.start_discussion(topic.uuid)
        session.note_indirect_peer_topic("relay:B", topic.uuid)
        session.bind_peer_topic_channel("relay:B", topic.uuid, "mailbox")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app_server.save_session_to_file(session, str(path))

            loaded = Session("http://a")
            self.assertTrue(app_server.load_session_from_file(loaded, str(path)))

        self.assertEqual(loaded.members, {"http://a"})
        self.assertNotIn("relay:B", loaded.members)
        self.assertEqual(loaded.peer_topic_sets.get("relay:B"), {topic.uuid})
        self.assertEqual(
            loaded.peer_channel_for_topic("relay:B", topic.uuid), "mailbox",
        )

    def test_restore_keeps_routed_remote_profile_topic_for_refetch(self):
        session = Session("http://a")
        remote_profile_uuid = "remote-profile-topic"
        session.note_indirect_peer_topic("relay:B", remote_profile_uuid)
        session.bind_peer_topic_channel(
            "relay:B", remote_profile_uuid, "mailbox",
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app_server.save_session_to_file(session, str(path))
            loaded = Session("http://a")
            self.assertTrue(app_server.load_session_from_file(
                loaded, str(path),
            ))

        self.assertEqual(
            loaded.peer_topic_sets["relay:B"], {remote_profile_uuid},
        )
        self.assertEqual(
            loaded.peer_channel_for_topic(
                "relay:B", remote_profile_uuid,
            ),
            "mailbox",
        )

    def test_persistence_roundtrip_restores_peer_identity_registry(self):
        # The registry must survive restarts even for addresses that live
        # in no other restored structure: a relay pseudo-address suppressed
        # as redundant has been fully removed as a peer, and its registry
        # entry is the only thing keeping it suppressed after a restart
        # (relay's own "applied" hash bookkeeping persists too, so its
        # unchanged identity topic never re-applies to re-teach the fact).
        session = Session("http://a")
        topic = session.create_child(
            session.protocol.root.uuid, {"name": "topic"}, {},
        ).value
        session.start_discussion(topic.uuid)
        session.add_peer("http://b", topic.uuid)
        session.set_peer_identity_key("http://b", "key-bob")
        session.set_peer_identity_key("relay:B", "key-bob")  # suppressed, no peer state

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app_server.save_session_to_file(session, str(path))

            loaded = Session("http://a")
            self.assertTrue(app_server.load_session_from_file(loaded, str(path)))

        self.assertEqual(loaded.peer_identity_key.get("http://b"), "key-bob")
        self.assertEqual(loaded.peer_identity_key.get("relay:B"), "key-bob")
        self.assertNotIn("relay:B", loaded.members)

    def test_persistence_does_not_restore_peer_sync_state(self):
        # peer_perspectives (the peer cache) is never persisted, so it's
        # empty again after every restart. If last_delivered_sync_hash
        # survived a restart while the cache didn't, a session with an
        # unchanged board would never re-send a sync_status to a peer whose
        # data it no longer has cached - permanently stuck with no way to
        # repopulate the cache. peer_sync_state must come back fresh so a
        # sync is always attempted at least once after restart.
        session = Session("http://a")
        topic = session.create_child(
            session.protocol.root.uuid,
            {"name": "topic"},
            {},
        ).value
        session.start_discussion(topic.uuid)
        session.add_peer("http://b", topic.uuid)
        session.peer_sync_state["http://b"]["last_delivered_sync_hash"] = "stale-hash"
        session.peer_sync_state["http://b"]["retry_after"] = 9999999999.0

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app_server.save_session_to_file(session, str(path))

            loaded = Session("http://a")
            self.assertTrue(app_server.load_session_from_file(loaded, str(path)))

        self.assertEqual(
            loaded.peer_sync_state.get("http://b", {}).get("last_delivered_sync_hash"),
            None,
        )
        self.assertEqual(
            loaded.peer_sync_state.get("http://b", {}).get("retry_after"),
            None,
        )

    def test_load_rejects_legacy_root_only_files(self):
        session = Session("http://a")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.json"
            path.write_text(json.dumps(session.protocol.root.to_dict()), encoding="utf-8")

            loaded = Session("http://b")
            loaded_root = loaded.root_uuid()
            self.assertFalse(app_server.load_session_from_file(
                loaded,
                str(path),
                logger=lambda _: None,
            ))

        self.assertEqual(loaded.root_uuid(), loaded_root)

    def test_load_rejects_legacy_prsp_session_with_clear_reason(self):
        session = Session("http://a")
        legacy = {
            "format": "prsp-session-v1",
            "protocol_root": session.protocol.root.to_dict(),
            "session": {},
        }
        logs = []

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            loaded = Session("http://b")

            self.assertFalse(app_server.load_session_from_file(
                loaded, str(path), logger=logs.append,
            ))

        self.assertIn("legacy format 'prsp-session-v1'", logs[0])

    def test_runtime_refuses_to_overwrite_incompatible_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.json"
            original = json.dumps({"format": "prsp-session-v1"})
            path.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "refusing to start"):
                app_server.create_runtime(8129, {
                    "app_module": None,
                    "storage_file": str(path),
                })

            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_runtime_rejects_contact_data_in_core_profile(self):
        session = Session("http://a")
        session.identity
        root = ProtocolNode.from_dict(session.export_protocol_root())
        profile = next(
            node for node in root.children[0].children
            if node.data.get("type") == "shared_user_profile"
        )
        profile.data["email"] = "alice@example.test"
        root.refresh_hashes_deep()
        payload = {
            "format": "sovereign-session",
            "version": 1,
            "protocol_schema_version": 1,
            "protocol_root": root.to_dict(),
            "session": {},
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid-profile.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "refusing to start"):
                app_server.create_runtime(8130, {
                    "app_module": None,
                    "storage_file": str(path),
                })

    def test_save_retries_when_replace_is_temporarily_locked(self):
        session = Session("http://a")
        logs = []

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            real_replace = app_server.os.replace
            calls = {"count": 0}

            def flaky_replace(source, destination):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise PermissionError("locked")
                return real_replace(source, destination)

            with patch("sovereign.app_server.os.replace", flaky_replace), \
                    patch("sovereign.app_server.time.sleep", lambda _: None):
                app_server.save_session_to_file(
                    session,
                    str(path),
                    logger=logs.append,
                )

            self.assertEqual(calls["count"], 2)
            self.assertTrue(path.exists())
            self.assertEqual(logs, [])

    def test_save_logs_after_repeated_replace_locks(self):
        session = Session("http://a")
        logs = []

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            real_replace = app_server.os.replace
            calls = {"count": 0}

            def flaky_replace(source, destination):
                calls["count"] += 1
                if calls["count"] <= 3:
                    raise PermissionError("locked")
                return real_replace(source, destination)

            with patch("sovereign.app_server.os.replace", flaky_replace), \
                    patch("sovereign.app_server.time.sleep", lambda _: None):
                app_server.save_session_to_file(
                    session,
                    str(path),
                    logger=logs.append,
                )

            self.assertEqual(calls["count"], 4)
            self.assertTrue(path.exists())
            self.assertIn("save replace blocked", logs[0])

    def test_create_runtime_builds_session_adapter_and_loads_logic(self):
        module = _fake_application_module(
            logic_factory=lambda services: {
                "address": services.session.address,
                "name": services.settings["name"],
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "applications": [{
                    "module": "fake_logic", "settings": {"name": "demo"},
                }],
                "storage_file": str(Path(tmp) / "state.json"),
            }
            with patch.dict("sys.modules", {"fake_logic": module}):
                runtime = app_server.create_runtime(8123, config)

        self.assertEqual(runtime.address, "http://127.0.0.1:8123")
        self.assertEqual(runtime.logic["name"], "demo")
        self.assertIs(runtime.adapter.session, runtime.session)

    def test_create_runtime_advertises_configured_host(self):
        module = _fake_application_module()

        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "applications": [{"module": "fake_advertise_logic"}],
                "storage_file": str(Path(tmp) / "state.json"),
                "advertise_host": "100.64.1.2",
            }
            with patch.dict("sys.modules", {"fake_advertise_logic": module}):
                runtime = app_server.create_runtime(8125, config)

        self.assertEqual(runtime.address, "http://100.64.1.2:8125")
        self.assertEqual(runtime.session.address, "http://100.64.1.2:8125")

    def test_build_app_contains_core_and_app_routes(self):
        module = _fake_application_module()

        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "applications": [{"module": "fake_routes_logic"}],
                "storage_file": str(Path(tmp) / "state.json"),
            }
            with patch.dict("sys.modules", {"fake_routes_logic": module}):
                runtime = app_server.create_runtime(8124, config)
                app = app_server.build_app(runtime)

        paths = {route.path for route in app.routes}
        self.assertIn("/api/protocol", paths)
        self.assertIn("/p2p/sync_status", paths)
        self.assertIn("/p2p/subtree/{uuid}", paths)

    def test_build_app_mounts_explicit_applications_alongside_primary(self):
        primary = _fake_application_module("primary")
        extra = _fake_application_module("extra", route_paths=["/api/extra/ping"])

        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "applications": [
                    {"module": "fake_primary_logic"},
                    {"module": "fake_extra_logic"},
                ],
                "primary_application_id": "primary",
                "storage_file": str(Path(tmp) / "state.json"),
            }
            with patch.dict("sys.modules", {
                "fake_primary_logic": primary,
                "fake_extra_logic": extra,
            }):
                runtime = app_server.create_runtime(8126, config)
                app = app_server.build_app(runtime)

        paths = {route.path for route in app.routes}
        self.assertIn("/api/extra/ping", paths)
        self.assertEqual(set(runtime.host.instances), {"primary", "extra"})
        self.assertIs(runtime.logic, runtime.host.instances["primary"].logic)

    def test_drain_peer_update_hook_stops_after_no_change(self):
        calls = {"count": 0}

        class Logic:
            def on_peer_update(self):
                calls["count"] += 1
                return types.SimpleNamespace(status="ok", value=False, effects=[])

        logic = Logic()
        def notify_peer_update():
            logic.on_peer_update()
            return False
        runtime = types.SimpleNamespace(
            host=types.SimpleNamespace(notify_peer_update=notify_peer_update),
            adapter=None,
        )

        asyncio.run(app_server.drain_peer_update_hook(runtime, passes=4))

        self.assertEqual(calls["count"], 1)

    def test_channel_poll_tick_drains_adoption_hook_after_apply(self):
        # Regression (review A-4): relay-applied peer content only reached
        # the cache; adoption ran solely from UI polls and http p2p
        # endpoints, so a headless relay-only session never adopted what it
        # synced. The tick must drain the app's on_peer_update hook.
        hook_calls = {"count": 0}
        notifications = []
        publish_calls = {"count": 0}

        class FakeRelay:
            def has_active_relationship(self):
                return True

            def write_presence(self):
                pass

            def publish_due_topics(self):
                publish_calls["count"] += 1
                return []

            def poll_and_apply(self):
                return [("topic-1", "A")]

        class Logic:
            def on_peer_update(self):
                hook_calls["count"] += 1
                return types.SimpleNamespace(status="ok", value=False, effects=[])

        logic = Logic()
        def notify_peer_update():
            logic.on_peer_update()
            return False

        runtime = types.SimpleNamespace(
            config={},
            channel_manager=_FakeRelayManager([FakeRelay()]),
            host=types.SimpleNamespace(notify_peer_update=notify_peer_update),
            adapter=None,
            notify_change=lambda kind=None: notifications.append(kind),
        )

        changed = asyncio.run(app_server.channel_poll_tick(runtime))

        self.assertTrue(changed)
        self.assertEqual(hook_calls["count"], 1)
        self.assertEqual(publish_calls["count"], 2)
        self.assertEqual(notifications, ["channel"])

    def test_channel_poll_tick_traces_each_relay_phase(self):
        class FakeRelay:
            identity = "relay-a"
            poll_interval_seconds = 3

            def has_active_relationship(self):
                return True

            def calibrate_timing_if_due(self):
                pass

            def write_presence(self):
                pass

            def publish_due_topics(self):
                return ["topic-1"]

            def poll_and_apply(self):
                return [("topic-1", "peer-b")]

        class Logic:
            def on_peer_update(self):
                return types.SimpleNamespace(
                    status="ok", value=False, effects=[],
                )

        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "relay-trace.jsonl"
            session = Session(
                "http://a",
                trace=TraceLogger(
                    str(trace_path), node="http://a", level="timing",
                ),
            )
            logic = Logic()
            runtime = types.SimpleNamespace(
                session=session,
                config={},
                channel_manager=_FakeRelayManager([FakeRelay()]),
                host=types.SimpleNamespace(
                    notify_peer_update=lambda: logic.on_peer_update(),
                ),
                adapter=None,
                notify_change=lambda kind=None: None,
            )

            asyncio.run(app_server.channel_poll_tick(runtime))
            records = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
            ]

        starts = [
            record for record in records
            if record["kind"] == "relay.cycle_start"
        ]
        phases = [
            record for record in records
            if record["kind"] == "relay.phase"
        ]
        completed = [
            record for record in records
            if record["kind"] == "relay.cycle_done"
        ]

        self.assertEqual(len(starts), 1)
        self.assertEqual(
            [record["phase"] for record in phases],
            [
                "calibrate_timing",
                "write_presence",
                "publish_before_poll",
                "poll_and_apply",
                "adopt_peer_updates",
                "publish_response",
            ],
        )
        self.assertEqual(len(completed), 1)
        cycle_id = starts[0]["cycle_id"]
        self.assertTrue(all(
            record["cycle_id"] == cycle_id
            and record["ok"]
            and record["duration_ms"] >= 0
            for record in [*phases, *completed]
        ))
        self.assertEqual(
            next(
                record for record in phases
                if record["phase"] == "poll_and_apply"
            )["applied_count"],
            1,
        )

    def test_events_trace_omits_timing_but_keeps_relay_failures(self):
        class FakeRelay:
            identity = "relay-a"
            poll_interval_seconds = 3

            def has_active_relationship(self):
                return True

            def write_presence(self):
                raise RuntimeError("relay unavailable")

            def publish_due_topics(self):
                raise AssertionError("must stop after failure")

            def poll_and_apply(self):
                raise AssertionError("must stop after failure")

        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "relay-trace.jsonl"
            session = Session(
                "http://a",
                trace=TraceLogger(
                    str(trace_path), node="http://a", level="events",
                ),
            )
            runtime = types.SimpleNamespace(
                session=session,
                config={},
                channel_manager=_FakeRelayManager([FakeRelay()]),
                host=types.SimpleNamespace(notify_peer_update=lambda: False),
                adapter=None,
                notify_change=lambda kind=None: None,
            )

            asyncio.run(app_server.channel_poll_tick(runtime))
            records = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(
            [record["kind"] for record in records],
            ["relay.phase", "relay.cycle_done"],
        )
        self.assertTrue(all(record["ok"] is False for record in records))

    def test_channel_poll_tick_idle_without_active_relationship(self):
        class FakeRelay:
            def has_active_relationship(self):
                return False

            def write_presence(self):
                raise AssertionError("must stay idle")

        runtime = types.SimpleNamespace(
            config={}, channel_manager=_FakeRelayManager([FakeRelay()]),
            host=types.SimpleNamespace(notify_peer_update=lambda: False), adapter=None,
            notify_change=lambda kind=None: None,
        )

        self.assertFalse(asyncio.run(app_server.channel_poll_tick(runtime)))

    def test_channel_poll_tick_runs_independent_connection_io_concurrently(self):
        first_started = threading.Event()
        second_started = threading.Event()
        overlapped = []

        class FakeRelay:
            poll_interval_seconds = 3

            def __init__(self, mine, other):
                self.mine = mine
                self.other = other

            def has_active_relationship(self):
                return True

            def write_presence(self):
                self.mine.set()
                overlapped.append(self.other.wait(timeout=0.5))

            def publish_due_topics(self):
                return []

            def poll_and_apply(self):
                return []

        runtime = types.SimpleNamespace(
            config={}, channel_manager=_FakeRelayManager([
                FakeRelay(first_started, second_started),
                FakeRelay(second_started, first_started),
            ]), host=types.SimpleNamespace(notify_peer_update=lambda: False), adapter=None,
            notify_change=lambda kind=None: None,
        )

        asyncio.run(app_server.channel_poll_tick(runtime))

        self.assertEqual(overlapped, [True, True])

    def test_channel_poll_tick_keeps_poll_cadence_after_publish(self):
        response_delay_calls = {"count": 0}

        class FakeRelay:
            poll_interval_seconds = 3

            def has_active_relationship(self):
                return True

            def write_presence(self):
                pass

            def publish_due_topics(self):
                return ["topic-1"]

            def poll_and_apply(self):
                return []

            def response_check_delay(self):
                response_delay_calls["count"] += 1
                return 0.4

        relay = FakeRelay()
        runtime = types.SimpleNamespace(
            config={}, channel_manager=_FakeRelayManager([relay]),
            host=types.SimpleNamespace(notify_peer_update=lambda: False), adapter=None,
            notify_change=lambda kind=None: None,
        )
        started = app_server.time.monotonic()

        asyncio.run(app_server.channel_poll_tick(runtime))

        scheduled = runtime.config["_channel_next_due"][id(relay)]
        self.assertEqual(response_delay_calls["count"], 1)
        self.assertGreaterEqual(scheduled, started + 2.9)
        self.assertLessEqual(scheduled, started + 3.1)

    def test_poll_deadline_advances_from_cadence_and_skips_missed_slots(self):
        self.assertEqual(
            app_server._advance_poll_deadline(None, 10.0, 3.0, 11.0),
            13.0,
        )
        self.assertEqual(
            app_server._advance_poll_deadline(
                10.0, 10.2, 3.0, 12.0,
            ),
            13.0,
        )
        self.assertEqual(
            app_server._advance_poll_deadline(
                10.0, 10.2, 3.0, 13.5,
            ),
            16.0,
        )

    def test_early_local_wake_keeps_existing_response_deadline(self):
        class FakeRelay:
            poll_interval_seconds = 3

            def has_active_relationship(self):
                return True

            def write_presence(self):
                pass

            def publish_due_topics(self):
                return []

            def poll_and_apply(self):
                return []

        relay = FakeRelay()
        existing_due = app_server.time.monotonic() + 10
        runtime = types.SimpleNamespace(
            config={
                "_channel_next_due": {id(relay): existing_due},
            },
            channel_manager=_FakeRelayManager([relay]),
            host=types.SimpleNamespace(notify_peer_update=lambda: False), adapter=None,
            notify_change=lambda kind=None: None,
        )

        asyncio.run(app_server.channel_poll_tick(runtime, due_only=False))

        self.assertEqual(runtime.config["_channel_next_due"][id(relay)], existing_due)

    def _connect_token_endpoint(self, runtime):
        app = app_server.build_app(runtime)
        return next(
            route.endpoint for route in app.routes
            if getattr(route, "path", None) == "/api/core/invitations"
        )

    @staticmethod
    def _get_request(query: str):
        from starlette.requests import Request
        return Request({
            "type": "http", "method": "GET", "path": "/api/connect_token",
            "query_string": query.encode(), "headers": [],
        })

    @staticmethod
    def _post_request(path: str, payload: dict):
        from starlette.requests import Request
        body = json.dumps(payload).encode()
        delivered = False

        async def receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        return Request({
            "type": "http", "method": "POST", "path": path,
            "query_string": b"", "headers": [(b"content-type", b"application/json")],
        }, receive)

    def test_post_connect_token_composes_one_target_and_assigns_selected_board(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as relay_root:
            runtime, board = _runtime_with_topic(8210, {
                "storage_file": str(Path(tmp) / "state.json"),
                "relay_state_directory": tmp,
            })
            app = app_server.build_app(runtime)
            endpoint = next(
                route.endpoint for route in app.routes
                if getattr(route, "path", None) == "/api/core/invitations"
            )
            manager = runtime.relay_manager
            target_id = manager.create_target({
                "name": "Company", "backend": "local", "root": relay_root,
            }).value

            response = asyncio.run(endpoint(self._post_request(
                "/api/core/invitations",
                {
                    "channel_ref": f"mailbox:{target_id}",
                    "topic_uuid": board.uuid,
                },
            )))
            payload = json.loads(response.body)

            self.assertEqual(payload["token_version"], 1)
            self.assertEqual(payload["topic_uuids"], sorted([board.uuid, runtime.session.identity.uuid]))
            self.assertEqual([channel["type"] for channel in payload["channels"]], ["relay"])
            self.assertEqual(payload["channels"][0]["target_id"], target_id)
            self.assertEqual(manager.target_for_topic(board.uuid), target_id)
            shared = manager.connection_for_target(target_id)._state["shared"]
            self.assertIn(board.uuid, shared)
            self.assertIn(runtime.session.identity.uuid, shared)

    def test_post_connect_token_invalid_board_does_not_partially_assign(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as relay_root:
            runtime, board = _runtime_with_topic(8211, {
                "storage_file": str(Path(tmp) / "state.json"),
                "relay_state_directory": tmp,
            })
            app = app_server.build_app(runtime)
            endpoint = next(
                route.endpoint for route in app.routes
                if getattr(route, "path", None) == "/api/core/invitations"
            )
            manager = runtime.relay_manager
            target_id = manager.create_target({
                "name": "Company", "backend": "local", "root": relay_root,
            }).value
            # Without this the 404 below would still arrive in a runtime that
            # can share nothing at all, and the test would pass while proving
            # nothing about the unknown uuid.
            self.assertTrue(runtime.session.supports_shared_topic(board))

            response = asyncio.run(endpoint(self._post_request(
                "/api/core/invitations",
                {
                    "channel_ref": f"mailbox:{target_id}",
                    "topic_uuid": "zzz-invalid",
                },
            )))

            self.assertEqual(response.status_code, 404)
            self.assertIsNone(manager.target_for_topic(board.uuid))
            self.assertNotIn(
                board.uuid,
                manager.connection_for_target(target_id)._state["shared"],
            )

    def test_post_relay_target_with_id_edits_and_persists_target(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as root_a, tempfile.TemporaryDirectory() as root_b:
            storage_file = str(Path(tmp) / "state.json")
            config = {
                "app_module": None,
                "storage_file": storage_file,
                "relay_state_directory": tmp,
            }
            runtime = app_server.create_runtime(8212, config)
            app = app_server.build_app(runtime)
            endpoint = next(
                route.endpoint for route in app.routes
                if getattr(route, "path", None) == "/api/core/channels"
            )
            manager = runtime.relay_manager
            target_id = manager.create_target({
                "name": "Old", "backend": "local", "root": root_a,
            }).value

            response = asyncio.run(endpoint(self._post_request(
                "/api/core/channels", {
                    "id": target_id, "kind": "mailbox",
                    "type": "local_relay", "name": "New", "root": root_b,
                    "poll_interval_seconds": 9,
                },
            )))

            self.assertEqual(response.status_code, 200)
            target = next(item for item in manager.list_targets() if item["id"] == target_id)
            self.assertEqual(target["name"], "New")
            self.assertEqual(target["root"], root_b)
            self.assertEqual(target["poll_interval_seconds"], 9)
            self.assertTrue(Path(storage_file).exists())

    def test_accepting_two_relay_tokens_registers_two_coexisting_targets(self):
        from sovereign.relay_logic import RelayManager

        with tempfile.TemporaryDirectory() as root_b, tempfile.TemporaryDirectory() as root_c, tempfile.TemporaryDirectory() as state_dir:
            session = Session("http://a")
            manager = RelayManager(session, {"relay_state_directory": state_dir})
            runtime = types.SimpleNamespace(
                address="http://a", session=session,
                config={}, relay_manager=manager,
            )
            inviter_b = Session("http://b")
            inviter_c = Session("http://c")

            result_b = self._accept_connect_token(
                runtime, inviter_b.identity.to_dict(), ["board-b", inviter_b.identity.uuid],
                [{"type": "relay", "descriptor_version": 1, "root": root_b, "identity": "B"}],
            )
            result_c = self._accept_connect_token(
                runtime, inviter_c.identity.to_dict(), ["board-c", inviter_c.identity.uuid],
                [{"type": "relay", "descriptor_version": 1, "root": root_c, "identity": "C"}],
            )

            self.assertEqual(result_b["status"], "ok")
            self.assertEqual(result_c["status"], "ok")
            self.assertEqual(len(manager.list_targets()), 2)
            self.assertNotEqual(manager.target_for_topic("board-b"), manager.target_for_topic("board-c"))
            self.assertEqual(len([conn for conn in manager.all_connections() if conn.storage]), 2)

    def test_connect_token_without_target_yields_http_only_token(self):
        # A target-less POST is the direct-connection (e.g. LAN) token: no
        # relay target chosen, so only the http channel is offered. This is
        # the path the http integration flow uses.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, board = _runtime_with_topic(8203, {
                "storage_file": str(Path(tmp) / "state.json"),
            })
            endpoint = self._connect_token_endpoint(runtime)
            response = asyncio.run(endpoint(self._post_request(
                "/api/core/invitations",
                {"topic_uuid": board.uuid, "channel_ref": "http"},
            )))

        payload = json.loads(response.body)
        self.assertEqual(
            [channel["type"] for channel in payload["channels"]], ["http"],
        )
        self.assertEqual(
            payload["topic_uuids"],
            sorted([board.uuid, runtime.session.identity.uuid]),
        )

    def test_connect_token_requires_at_least_one_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = app_server.create_runtime(8204, {
                "app_module": None,
                "storage_file": str(Path(tmp) / "state.json"),
            })
            endpoint = self._connect_token_endpoint(runtime)
            response = asyncio.run(endpoint(self._post_request(
                "/api/core/invitations",
                {"topic_uuid": "", "channel_ref": "http"},
            )))

        self.assertEqual(response.status_code, 400)

    def test_connect_rejects_legacy_bare_token_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = app_server.create_runtime(8205, {
                "app_module": None,
                "storage_file": str(Path(tmp) / "state.json"),
            })
            app = app_server.build_app(runtime)
            endpoint = next(
                route.endpoint for route in app.routes
                if getattr(route, "path", None) == "/api/core/invitations/accept"
            )
            response = asyncio.run(endpoint(self._post_request(
                "/api/core/invitations/accept", {"token": {"version": 1}},
            )))

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"unrecognized token version", response.body)

    def test_accept_connect_token_uses_http_channel_and_records_it(self):
        session = Session("http://a")
        bob_session = Session("http://b")
        bob_session.set_identity("Bob")
        identity_payload = bob_session.identity.to_dict()
        runtime = types.SimpleNamespace(
            address="http://a", session=session, config={},
        )
        with patch.object(
            app_server, "_dispatch_join_discussion",
            return_value={"status": "ok", "members": ["http://b"]},
        ) as dispatch:
            result = self._accept_connect_token(
                runtime,
                identity=identity_payload,
                topic_uuids=["topic-1"],
                channels=[{"type": "http", "descriptor_version": 1, "address": "http://b"}],
            )

        dispatch.assert_called_once_with(runtime, "http://b", None, ["topic-1"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["channels_used"], ["http"])
        self.assertEqual(
            session.peer_channel_for_topic("http://b", "topic-1"), "http",
        )
        cached = session.find_peer_identity(identity_payload["data"]["identity_key"])
        self.assertIsNotNone(cached)
        self.assertEqual(cached.data["display_name"], "Bob")

    def test_accept_connect_token_uses_relay_channel_and_records_it(self):
        # accept_descriptor is the manager's single entry point: it verifies,
        # registers the target, provisions a connection from the token, and
        # persists desired topics + board assignment. ChannelManager routes
        # to it and records the selected channel. (The
        # provisioning behavior itself is covered against the real manager by
        # test_accepting_two_relay_tokens_registers_two_coexisting_targets.)
        session = Session("http://a")
        manager = _FakeRelayManager()
        runtime = types.SimpleNamespace(
            address="http://a", session=session,
            config={}, relay_manager=manager,
        )
        channel = {
            "type": "relay", "descriptor_version": 1, "root": "x", "identity": "B",
            "poll_interval_seconds": 8,
        }
        inviter = Session("http://b")

        result = self._accept_connect_token(
            runtime, identity=inviter.identity.to_dict(),
            topic_uuids=["topic-1"], channels=[channel],
        )

        self.assertEqual(manager.accept_calls, [(
            channel, ["topic-1"], inviter.identity.uuid,
        )])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["channels_used"], ["mailbox"])
        self.assertEqual(
            session.peer_channel_for_topic("relay:B", "topic-1"), "mailbox",
        )

    def test_accept_connect_token_passes_inviter_identity_to_manager(self):
        # The inviter's identity uuid rides along so the manager can record
        # its identity topic separately from board topics.
        session = Session("http://a")
        inviter = Session("http://b")
        manager = _FakeRelayManager()
        runtime = types.SimpleNamespace(
            address="http://a", session=session,
            config={}, relay_manager=manager,
        )

        self._accept_connect_token(
            runtime, identity=inviter.identity.to_dict(),
            topic_uuids=["topic-1", inviter.identity.uuid],
            channels=[{"type": "relay", "descriptor_version": 1, "root": "x", "identity": "B"}],
        )

        descriptor, topics, inviter_uuid = manager.accept_calls[0]
        self.assertEqual(inviter_uuid, inviter.identity.uuid)

    def test_relay_token_missing_channel_identity_has_no_manager_side_effect(self):
        session = Session("http://a")
        inviter = Session("http://b")
        manager = _FakeRelayManager()
        runtime = types.SimpleNamespace(
            address="http://a", session=session,
            config={}, relay_manager=manager,
        )

        result = self._accept_connect_token(
            runtime, inviter.identity.to_dict(), ["topic-1"],
            [{"type": "relay", "descriptor_version": 1, "root": "x"}],
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(manager.accept_calls, [])

    def test_accept_connect_token_rejects_unreachable_relay_before_connected(self):
        session = Session("http://a")
        inviter = Session("http://b")
        manager = _FakeRelayManager(accept_result=types.SimpleNamespace(
            status="error",
            reason="relay unavailable: AuthenticationException: bad credentials",
            value=None,
        ))
        runtime = types.SimpleNamespace(
            address="http://a", session=session,
            config={}, relay_manager=manager,
        )

        result = self._accept_connect_token(
            runtime, identity=inviter.identity.to_dict(), topic_uuids=["topic-1"],
            channels=[{
                "type": "sftp", "descriptor_version": 1, "host": "example.test",
                "root": "/relay", "identity": "B",
            }],
        )

        self.assertEqual(result["status"], "error")
        self.assertIn("AuthenticationException", result["reason"])
        self.assertNotIn("relay:B", session.peer_topic_channel)

    def test_accept_connect_token_uses_sftp_channel_and_records_it(self):
        # The sftp channel type shares the manager accept path and the
        # "relay:<identity>" peer-address namespace with the local relay
        # channel - same accept mechanism, different storage backend.
        session = Session("http://a")
        manager = _FakeRelayManager()
        inviter = Session("http://b")
        runtime = types.SimpleNamespace(
            address="http://a", session=session,
            config={}, relay_manager=manager,
        )

        result = self._accept_connect_token(
            runtime, identity=inviter.identity.to_dict(), topic_uuids=["topic-1"],
            channels=[{
                "type": "sftp", "descriptor_version": 1, "host": "example.test",
                "port": 22, "root": "/relay", "identity": "B",
            }],
        )

        self.assertEqual([topics for _d, topics, _i in manager.accept_calls], [["topic-1"]])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["channels_used"], ["mailbox"])
        self.assertEqual(
            session.peer_channel_for_topic("relay:B", "topic-1"), "mailbox",
        )

    def test_accept_connect_token_rejects_multiple_channels(self):
        session = Session("http://a")
        manager = _FakeRelayManager()
        runtime = types.SimpleNamespace(
            address="http://a", session=session,
            config={}, relay_manager=manager,
        )
        with patch.object(
            app_server, "_dispatch_join_discussion",
            return_value={"status": "ok"},
        ):
            result = self._accept_connect_token(
                runtime, identity=None, topic_uuids=["topic-1"],
                channels=[
                    {"type": "http", "descriptor_version": 1, "address": "http://b"},
                    {"type": "relay", "descriptor_version": 1, "root": "x", "identity": "B"},
                ],
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "token must select exactly one channel")
        self.assertNotIn("http://b", session.peer_topic_channel)
        self.assertNotIn("relay:B", session.peer_topic_channel)
        self.assertEqual(manager.accept_calls, [])

    def test_accept_connect_token_does_not_fall_back_when_token_is_ambiguous(self):
        session = Session("http://a")
        inviter = Session("http://b")
        runtime = types.SimpleNamespace(
            address="http://a", session=session,
            config={}, relay_manager=_FakeRelayManager(),
        )
        with patch.object(
            app_server, "_dispatch_join_discussion",
            return_value={"status": "error", "reason": "unreachable"},
        ):
            result = self._accept_connect_token(
                runtime, identity=inviter.identity.to_dict(), topic_uuids=["topic-1"],
                channels=[
                    {"type": "http", "descriptor_version": 1, "address": "http://b"},
                    {"type": "relay", "descriptor_version": 1, "root": "x", "identity": "B"},
                ],
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "token must select exactly one channel")
        self.assertNotIn("relay:B", session.peer_topic_channel)
        self.assertNotIn("http://b", session.peer_topic_channel)

    def test_accept_connect_token_relay_only_still_rejects_ambiguous_token(self):
        session = Session("http://a")
        inviter = Session("http://b")
        runtime = types.SimpleNamespace(
            address="http://a", session=session,
            config={"relay_only": True}, relay_manager=_FakeRelayManager(),
        )
        with patch.object(app_server, "_dispatch_join_discussion") as direct_join:
            result = self._accept_connect_token(
                runtime, identity=inviter.identity.to_dict(), topic_uuids=["topic-1"],
                channels=[
                    {"type": "http", "descriptor_version": 1, "address": "http://b"},
                    {"type": "relay", "descriptor_version": 1, "root": "x", "identity": "B"},
                ],
            )

        direct_join.assert_not_called()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "token must select exactly one channel")
        self.assertNotIn("relay:B", session.peer_topic_channel)

    def test_accept_connect_token_relay_only_rejects_http_only_token(self):
        session = Session("http://a")
        runtime = types.SimpleNamespace(
            address="http://a", session=session, config={"relay_only": True},
        )

        with patch.object(app_server, "_dispatch_join_discussion") as direct_join:
            result = self._accept_connect_token(
                runtime, identity=None, topic_uuids=["topic-1"],
                channels=[{"type": "http", "descriptor_version": 1, "address": "http://b"}],
            )

        direct_join.assert_not_called()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["errors"]["http"], "disabled by local relay-only policy")

    def test_relay_only_rejects_inbound_p2p_join(self):
        runtime = types.SimpleNamespace(
            config={"relay_only": True},
            http_channel=types.SimpleNamespace(accept_enabled=False),
            adapter=types.SimpleNamespace(
                p2p_join=lambda payload: (_ for _ in ()).throw(
                    AssertionError("direct adapter must not be called")
                ),
            ),
        )
        endpoint = next(
            route.endpoint for route in app_server.build_core_routes(runtime)
            if route.path == "/p2p/join"
        )
        request = self._get_request("")

        response = asyncio.run(endpoint(request))

        self.assertEqual(response.status_code, 403)
        self.assertIn(b"relay-only policy", response.body)

    def test_accept_connect_token_reconnect_replaces_old_channel(self):
        session = Session("http://a")
        bob_session = Session("http://b-old")
        bob_session.set_identity("Bob")
        identity_payload = bob_session.identity.to_dict()
        runtime = types.SimpleNamespace(address="http://a", session=session, config={})

        # Simulate an already-established peer registration under the old
        # address, as a prior real join (accept_connect_token doesn't itself
        # populate members/peer_topic_sets - that's join_discussion's job,
        # mocked out below) would have produced.
        session.add_peer("http://b-old", "topic-1")
        session.apply_peer_identity_snapshot("http://b-old", identity_payload)
        session.bind_peer_topic_channel("http://b-old", "topic-1", "http")

        with patch.object(
            app_server, "_dispatch_join_discussion",
            return_value={"status": "ok", "members": ["http://b-new"]},
        ):
            result = self._accept_connect_token(
                runtime, identity=identity_payload, topic_uuids=["topic-1"],
                channels=[{"type": "http", "descriptor_version": 1, "address": "http://b-new"}],
            )

        self.assertEqual(result["status"], "ok")
        self.assertNotIn("http://b-old", session.members)
        self.assertNotIn("http://b-old", session.peer_topic_sets)
        self.assertNotIn("http://b-old", session.peer_perspectives)
        self.assertNotIn("http://b-old", session.peer_status)
        self.assertNotIn("http://b-old", session.peer_topic_channel)
        self.assertEqual(
            session.peer_channel_for_topic("http://b-new", "topic-1"), "http",
        )
        cached = session.find_peer_identity(identity_payload["data"]["identity_key"])
        self.assertIsNotNone(cached)
        # Endpoint identity remains learned history, not an active route.
        self.assertIn("http://b-old", session.peer_identity_key)
        self.assertEqual(
            session.peer_identity_key.get("http://b-new"),
            identity_payload["data"]["identity_key"],
        )

    def test_accept_connect_token_keeps_relay_identity_key_when_selecting_http(self):
        # Regression, caught live: relay's poll loop can discover a peer
        # (setting peer_identity_key["relay:B"]) *before* the http connect
        # completes. When the http connect then runs, its reconnect-replace
        # step must tear down the relay registration but NOT forget the
        # relay address's identity_key - that entry is what relay's
        # redundancy check reads to keep relay:B suppressed forever after.
        # Forgetting it (as a genuinely dead http address is forgotten)
        # left the relay duplicate reopened for good, since relay never
        # re-applies the unchanged identity topic to re-teach the key.
        session = Session("http://a")
        bob_session = Session("http://b")
        bob_session.set_identity("Bob")
        identity_payload = bob_session.identity.to_dict()
        identity_key = identity_payload["data"]["identity_key"]
        runtime = types.SimpleNamespace(address="http://a", session=session, config={})

        # Relay discovered Bob first, as its poll loop would have.
        session.note_indirect_peer_topic("relay:B", "topic-1")
        session.apply_peer_identity_snapshot("relay:B", identity_payload)
        self.assertEqual(session.peer_identity_key.get("relay:B"), identity_key)

        with patch.object(
            app_server, "_dispatch_join_discussion",
            return_value={"status": "ok", "members": ["http://b"]},
        ):
            result = self._accept_connect_token(
                runtime, identity=identity_payload, topic_uuids=["topic-1"],
                channels=[{"type": "http", "descriptor_version": 1, "address": "http://b"}],
            )

        self.assertEqual(result["status"], "ok")
        # Relay registration torn down (no stale duplicate at connect)...
        self.assertNotIn("relay:B", session.peer_topic_sets)
        self.assertNotIn("relay:B", session.peer_perspectives)
        # ...but its identity_key retained, so redundancy stays detectable.
        self.assertEqual(session.peer_identity_key.get("relay:B"), identity_key)
        self.assertEqual(session.peer_identity_key.get("http://b"), identity_key)

    def test_accept_connect_token_skips_unrecognized_channel_type_or_version(self):
        session = Session("http://a")
        runtime = types.SimpleNamespace(address="http://a", session=session, config={})

        result = self._accept_connect_token(
            runtime, identity=None, topic_uuids=["topic-1"],
            channels=[
                {"type": "carrier_pigeon", "descriptor_version": 1},
                {"type": "http", "descriptor_version": 99, "address": "http://b"},
            ],
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["channels_used"] if "channels_used" in result else [], [])

    def test_accept_connect_token_skips_relay_channel_when_not_configured_locally(self):
        session = Session("http://a")
        runtime = types.SimpleNamespace(address="http://a", session=session, config={})

        result = self._accept_connect_token(
            runtime, identity=None, topic_uuids=["topic-1"],
            channels=[{"type": "relay", "descriptor_version": 1, "root": "x", "identity": "B"}],
        )

        self.assertEqual(result["status"], "error")

    def test_connect_routes_are_registered(self):
        module = _fake_application_module("connect-routes")

        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "applications": [{"module": "fake_connect_routes_logic"}],
                "storage_file": str(Path(tmp) / "state.json"),
            }
            with patch.dict("sys.modules", {"fake_connect_routes_logic": module}):
                runtime = app_server.create_runtime(8127, config)
                app = app_server.build_app(runtime)

        paths = {route.path for route in app.routes}
        self.assertIn("/api/core/invitations", paths)
        self.assertIn("/api/core/invitations/accept", paths)


if __name__ == "__main__":
    unittest.main()
