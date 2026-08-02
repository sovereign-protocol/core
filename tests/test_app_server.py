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
from sovereign.channel import ChannelManager, PollCycleResult
from sovereign.host import PeerUpdateOutcome
from sovereign.mailbox_channel import MailboxChannel
from sovereign.protocol import ProtocolNode
from sovereign.relay_logic import RelayLogic
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
            # for one rather than the suite depending on S-Initiative.
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
        self, runtime, identity, topic_uuids, channels, topic_channels=None,
    ):
        manager = getattr(runtime, "channel_manager", None)
        if manager is None:
            manager = ChannelManager(runtime.session)
            relay_manager = getattr(runtime, "relay_manager", None)
            if relay_manager is not None:
                manager.register(MailboxChannel(relay_manager))
        topic_uuids = list(topic_uuids)
        inviter_uuid = (
            str(identity.get("uuid") or "")
            if isinstance(identity, dict) else ""
        )
        if inviter_uuid and inviter_uuid not in topic_uuids:
            topic_uuids.append(inviter_uuid)
        for index, channel in enumerate(channels, start=1):
            channel.setdefault("channel_id", f"channel-{index}")
        if topic_channels is None and len(channels) == 1:
            topic_channels = {
                topic_uuid: channels[0]["channel_id"]
                for topic_uuid in topic_uuids
            }
        result = manager.accept_invitation(
            identity, topic_uuids, channels, topic_channels,
        )
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
        # is how S-Initiative and S-Cockpit are launched. Core must merge
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
        session.note_indirect_peer_topic("relay:B", topic.uuid)
        session.bind_peer_topic_channel("relay:B", topic.uuid, "mailbox")
        with session.lock:
            session.application_metadata("kanban")["selected_board_uuid"] = (
                "board-1"
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app_server.save_session_to_file(session, str(path))

            loaded = Session("http://a")
            self.assertTrue(app_server.load_session_from_file(loaded, str(path)))

        self.assertEqual(loaded.active_topic_uuids, {topic.uuid})
        self.assertEqual(loaded.peer_topic_sets["relay:B"], {topic.uuid})
        self.assertEqual(
            loaded.peer_channel_for_topic("relay:B", topic.uuid), "mailbox",
        )
        with loaded.lock:
            self.assertEqual(
                loaded.application_metadata("kanban")["selected_board_uuid"],
                "board-1",
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
        # The registry must survive restarts even for an address that lives
        # in no other restored structure. It is knowledge - "this address
        # belongs to this identity" - and a lost mapping could never be
        # re-learned from content.
        session = Session("http://a")
        topic = session.create_child(
            session.protocol.root.uuid, {"name": "topic"}, {},
        ).value
        session.start_discussion(topic.uuid)
        session.note_indirect_peer_topic("relay:B", topic.uuid)
        session.set_peer_identity_key("relay:B", "key-bob")
        session.set_peer_identity_key("relay:gone", "key-bob")  # no peer state

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            app_server.save_session_to_file(session, str(path))

            loaded = Session("http://a")
            self.assertTrue(app_server.load_session_from_file(loaded, str(path)))

        self.assertEqual(loaded.peer_identity_key.get("relay:B"), "key-bob")
        self.assertEqual(loaded.peer_identity_key.get("relay:gone"), "key-bob")
        self.assertNotIn("relay:gone", loaded.peer_topic_sets)

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

    def test_create_runtime_builds_session_and_loads_logic(self):
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
        self.assertIs(runtime.channel_manager.session, runtime.session)

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

    def test_configured_header_title_renames_the_primary_application(self):
        module = _fake_application_module()

        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "applications": [{"module": "fake_titled_logic"}],
                "primary_application_id": "fake",
                "header_title": "s-cockpit - D",
                "storage_file": str(Path(tmp) / "state.json"),
            }
            with patch.dict("sys.modules", {"fake_titled_logic": module}):
                runtime = app_server.create_runtime(8128, config)

        [summary] = runtime.application_summaries()
        self.assertEqual(summary["display_name"], "s-cockpit - D")

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
        self.assertIn("/api/network", paths)
        self.assertIn("/api/core/revision", paths)
        self.assertIn("/api/core/mutations/{mutation_id}", paths)
        self.assertIn("/shared-session.js", paths)
        self.assertIn("/api/core/invitations", paths)

    def test_notify_change_advances_the_browser_revision(self):
        module = _fake_application_module()
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "applications": [{"module": "fake_revision_logic"}],
                "storage_file": str(Path(tmp) / "state.json"),
            }
            with patch.dict("sys.modules", {"fake_revision_logic": module}):
                runtime = app_server.create_runtime(8150, config)

            before = runtime.current_revision()
            runtime.notify_change("test")

        self.assertEqual(runtime.current_revision(), before + 1)

    def test_blob_sweep_runs_after_releasing_the_session_lock(self):
        session = Session("local")
        lock_available = []

        class BlobStore:
            def collect(self, _referenced):
                def probe():
                    acquired = session.lock.acquire(blocking=False)
                    lock_available.append(acquired)
                    if acquired:
                        session.lock.release()

                thread = threading.Thread(target=probe)
                thread.start()
                thread.join(1)
                return []

        runtime = types.SimpleNamespace(session=session, blob_store=BlobStore())

        self.assertEqual(app_server.AppRuntime.collect_local_blobs(runtime), [])
        self.assertEqual(lock_available, [True])

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
            return PeerUpdateOutcome(changed=False)
        runtime = types.SimpleNamespace(
            host=types.SimpleNamespace(notify_peer_update=notify_peer_update),
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
        session = Session("local")

        class FakeRelay:
            poll_interval_seconds = 3

            def has_active_relationship(self):
                return True

            def poll_once(self, after_apply=None):
                # poll_and_apply invokes the callback inside its Session
                # transaction; a stand-in that skipped the lock would not
                # exercise the contract the real caller establishes.
                with session.lock:
                    after_apply()
                return PollCycleResult(
                    ok=True,
                    changed=True,
                    applied=(("topic-1", "A"),),
                )

            def polling_diagnostics(self):
                return {"identity": "fake"}

        class Logic:
            def on_peer_update(self):
                hook_calls["count"] += 1
                return types.SimpleNamespace(status="ok", value=False, effects=[])

        logic = Logic()
        def notify_peer_update():
            logic.on_peer_update()
            return PeerUpdateOutcome(changed=False)

        runtime = types.SimpleNamespace(
            config={},
            session=session,
            channel_manager=_FakeRelayManager([FakeRelay()]),
            host=types.SimpleNamespace(notify_peer_update=notify_peer_update),
            persist_confirmed_change=lambda kind=None: notifications.append(kind),
        )

        changed = asyncio.run(app_server.channel_poll_tick(runtime))

        self.assertTrue(changed)
        self.assertEqual(hook_calls["count"], 1)
        self.assertEqual(notifications, ["channel"])
        self.assertEqual(runtime.session.current_view_revision(), 1)

    def test_runtime_delivers_effects_through_the_collaboration_service(self):
        # The deferred-effect tests below drive a stand-in runtime, so the
        # real AppRuntime must be checked to actually carry this callable -
        # otherwise the whole deferral path raises AttributeError the first
        # time an application returns an effect.
        delivered = []
        runtime = app_server.AppRuntime(
            port=0,
            address="local",
            config={},
            session=Session("local"),
            blob_store=None,
            profile=None,
            relay_manager=None,
            channel_manager=None,
            collaboration=types.SimpleNamespace(
                execute_effects=lambda effects: delivered.extend(effects) or [],
            ),
            mailbox_channel=None,
        )

        runtime.deliver_effects(("effect-1", "effect-2"))

        self.assertEqual(delivered, ["effect-1", "effect-2"])

    def test_peer_update_effects_run_after_poll_and_session_locks_are_released(self):
        session = Session("local")
        effect = types.SimpleNamespace(
            type="release_topic_channels",
            target="topic-1",
            channel_kind=None,
            payload={"topic_uuid": "topic-1"},
        )
        delivered = []
        session_available = []

        class FakeRelay:
            poll_interval_seconds = 3

            def has_active_relationship(self):
                return True

            def poll_once(self, after_apply=None):
                with session.lock:
                    after_apply()
                return PollCycleResult(ok=True, changed=True)

        def deliver_effects(effects):
            acquired = session.lock.acquire(blocking=False)
            session_available.append(acquired)
            if acquired:
                session.lock.release()
            delivered.extend(effects)

        runtime = types.SimpleNamespace(
            config={},
            session=session,
            channel_manager=_FakeRelayManager([FakeRelay()]),
            host=types.SimpleNamespace(notify_peer_update=lambda: (
                types.SimpleNamespace(
                    changed=False,
                    effects=(effect, effect),
                )
            )),
            deliver_effects=deliver_effects,
            persist_confirmed_change=lambda _kind=None: None,
        )

        self.assertTrue(asyncio.run(app_server.channel_poll_tick(runtime)))
        self.assertEqual(session_available, [True])
        self.assertEqual(delivered, [effect])
        self.assertEqual(session.current_view_revision(), 2)

    def test_polling_endpoint_traces_its_complete_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "relay-trace.jsonl"
            session = Session(
                "http://a",
                trace=TraceLogger(
                    str(trace_path), node="http://a", level="timing",
                ),
            )
            relay = RelayLogic(session, {})
            relay.calibrate_timing_if_due = lambda: None
            relay.write_presence = lambda: None
            relay.poll_and_apply = lambda _after_apply=None: [
                ("topic-1", "peer-b"),
            ]
            relay.publish_due_topics = lambda: ["topic-1"]

            result = relay.poll_once(lambda: None)
            records = [
                record
                for line in trace_path.read_text(encoding="utf-8").splitlines()
                if (record := json.loads(line))["kind"].startswith("relay.")
            ]

        self.assertTrue(result.ok, result.error)
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
                # Poll precedes publish: with one publication identity per
                # user, writing before looking is a lost update rather than
                # merely a stale one.
                "poll_and_apply",
                "publish_after_poll",
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

    def test_endpoint_events_trace_omits_timing_but_keeps_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace_path = Path(tmp) / "relay-trace.jsonl"
            session = Session(
                "http://a",
                trace=TraceLogger(
                    str(trace_path), node="http://a", level="events",
                ),
            )
            relay = RelayLogic(session, {})
            relay.calibrate_timing_if_due = lambda: None
            relay.write_presence = lambda: (
                (_ for _ in ()).throw(RuntimeError("relay unavailable"))
            )

            result = relay.poll_once()
            records = [
                record
                for line in trace_path.read_text(encoding="utf-8").splitlines()
                if (record := json.loads(line))["kind"].startswith("relay.")
            ]

        self.assertFalse(result.ok)
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
            host=types.SimpleNamespace(notify_peer_update=lambda: False),
            persist_confirmed_change=lambda kind=None: None,
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

            def poll_once(self, after_apply=None):
                self.mine.set()
                overlapped.append(self.other.wait(timeout=0.5))
                return PollCycleResult(ok=True)

            def polling_diagnostics(self):
                return {"identity": "fake"}

        runtime = types.SimpleNamespace(
            config={}, channel_manager=_FakeRelayManager([
                FakeRelay(first_started, second_started),
                FakeRelay(second_started, first_started),
            ]), host=types.SimpleNamespace(notify_peer_update=lambda: False),
            persist_confirmed_change=lambda kind=None: None,
        )

        asyncio.run(app_server.channel_poll_tick(runtime))

        self.assertEqual(overlapped, [True, True])

    def test_channel_poll_tick_keeps_poll_cadence_after_endpoint_publish(self):
        class FakeRelay:
            poll_interval_seconds = 3

            def has_active_relationship(self):
                return True

            def poll_once(self, after_apply=None):
                return PollCycleResult(
                    ok=True,
                    changed=True,
                    published_before=("topic-1",),
                    acknowledgement_wait_seconds=0.4,
                )

            def polling_diagnostics(self):
                return {"identity": "fake"}

        relay = FakeRelay()
        runtime = types.SimpleNamespace(
            config={}, session=Session("local"),
            channel_manager=_FakeRelayManager([relay]),
            host=types.SimpleNamespace(
                notify_peer_update=lambda: PeerUpdateOutcome(changed=False),
            ),
            persist_confirmed_change=lambda kind=None: None,
        )
        started = app_server.time.monotonic()

        asyncio.run(app_server.channel_poll_tick(runtime))

        scheduled = runtime.config["_channel_next_due"][id(relay)]
        self.assertGreaterEqual(scheduled, started + 2.9)
        self.assertLessEqual(scheduled, started + 3.1)

    def test_poll_deadline_advances_from_the_cadence_not_from_completion(self):
        # A cycle that finishes inside its slot keeps the fixed cadence, so
        # polling does not drift later and later by however long the work took.
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

    def test_a_cycle_that_overruns_its_slot_does_not_halve_the_poll_rate(self):
        # Rounding a missed deadline up to the next whole slot meant a cycle
        # overrunning its interval by a fraction polled at half the rate: a
        # measured 3.33s cycle against a 3s interval ran every 6.00s, and
        # every propagation through that client paid it. Overrunning work is
        # followed by the next cycle, not by the rest of an empty slot.
        self.assertEqual(
            app_server._advance_poll_deadline(
                10.0, 10.0, 3.0, 13.33,
            ),
            13.33,
        )
        # Still never faster than the interval when there is time to spare.
        self.assertEqual(
            app_server._advance_poll_deadline(
                10.0, 10.2, 3.0, 13.5,
            ),
            13.5,
        )
        self.assertEqual(
            app_server._advance_poll_deadline(
                10.0, 10.2, 3.0, 11.0,
            ),
            13.0,
        )

    def test_early_local_wake_keeps_existing_response_deadline(self):
        class FakeRelay:
            poll_interval_seconds = 3

            def has_active_relationship(self):
                return True

            def poll_once(self, after_apply=None):
                return PollCycleResult(ok=True)

            def polling_diagnostics(self):
                return {"identity": "fake"}

        relay = FakeRelay()
        existing_due = app_server.time.monotonic() + 10
        runtime = types.SimpleNamespace(
            config={
                "_channel_next_due": {id(relay): existing_due},
            },
            channel_manager=_FakeRelayManager([relay]),
            host=types.SimpleNamespace(notify_peer_update=lambda: False),
            persist_confirmed_change=lambda kind=None: None,
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

    def test_post_connect_token_refuses_a_channel_not_used_for_the_board(self):
        # Inviting someone to a channel is a decision to publish the board
        # there, and that decision is "use this channel for this board" -
        # taken first, and visible. Composing used to make it silently, so
        # asking for a token once bound the board to that channel for good.
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

            self.assertEqual(response.status_code, 409)
            self.assertIn("use this channel", json.loads(response.body)["reason"])
            self.assertIsNone(manager.target_for_topic(board.uuid))

    def test_post_connect_token_composes_once_the_channel_is_in_use(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as relay_root:
            runtime, board = _runtime_with_topic(8212, {
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
            manager.assign_topic_target(board.uuid, target_id)

            response = asyncio.run(endpoint(self._post_request(
                "/api/core/invitations",
                {
                    "channel_ref": f"mailbox:{target_id}",
                    "topic_uuid": board.uuid,
                },
            )))
            payload = json.loads(response.body)

            self.assertEqual(payload["token_version"], 2)
            self.assertEqual(payload["topic_uuids"], sorted([board.uuid, runtime.session.identity.uuid]))
            self.assertEqual([channel["type"] for channel in payload["channels"]], ["relay"])
            self.assertEqual(
                set(payload["topic_channels"]),
                {board.uuid, runtime.session.identity.uuid},
            )
            self.assertEqual(payload["channels"][0]["target_id"], target_id)
            # The first channel became the identity's visible home.
            self.assertEqual(
                manager.target_for_topic(runtime.session.identity.uuid), target_id,
            )
            shared = manager.connection_for_target(target_id)._state["shared"]
            self.assertIn(board.uuid, shared)
            self.assertIn(runtime.session.identity.uuid, shared)

    def test_post_connect_token_carries_identity_and_board_home_channels(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as identity_root,
            tempfile.TemporaryDirectory() as board_root,
        ):
            runtime, board = _runtime_with_topic(8213, {
                "storage_file": str(Path(tmp) / "state.json"),
                "relay_state_directory": tmp,
            })
            endpoint = self._connect_token_endpoint(runtime)
            manager = runtime.relay_manager
            identity_target = manager.create_target({
                "name": "Identity", "backend": "local", "root": identity_root,
            }).value
            board_target = manager.create_target({
                "name": "Board", "backend": "local", "root": board_root,
            }).value
            manager.assign_topic_target(board.uuid, board_target)

            response = asyncio.run(endpoint(self._post_request(
                "/api/core/invitations",
                {
                    "channel_ref": f"mailbox:{board_target}",
                    "topic_uuid": board.uuid,
                },
            )))
            payload = json.loads(response.body)
            descriptors = {
                item["channel_id"]: item["target_id"]
                for item in payload["channels"]
            }

            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(descriptors), 2)
            self.assertEqual(
                descriptors[payload["topic_channels"][runtime.session.identity.uuid]],
                identity_target,
            )
            self.assertEqual(
                descriptors[payload["topic_channels"][board.uuid]],
                board_target,
            )
            self.assertEqual(
                manager.target_for_topic(runtime.session.identity.uuid),
                identity_target,
            )

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

    def test_manage_channels_reports_and_moves_the_identity_home(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as root_a,
            tempfile.TemporaryDirectory() as root_b,
        ):
            runtime, _board = _runtime_with_topic(8214, {
                "storage_file": str(Path(tmp) / "state.json"),
                "relay_state_directory": tmp,
            })
            first = runtime.relay_manager.create_target({
                "name": "First", "backend": "local", "root": root_a,
            }).value
            second = runtime.relay_manager.create_target({
                "name": "Second", "backend": "local", "root": root_b,
            }).value

            initial = runtime.collaboration.channels_payload()
            self.assertEqual(
                initial["identity_channel_ref"], f"mailbox:{first}",
            )
            moved = runtime.collaboration.set_topic_channel(
                runtime.session.identity.uuid,
                f"mailbox:{second}",
                True,
            )
            after = runtime.collaboration.channels_payload()

            self.assertTrue(moved.ok, moved.reason)
            self.assertEqual(
                after["identity_channel_ref"], f"mailbox:{second}",
            )
            self.assertEqual(
                [
                    item["id"] for item in after["channels"]
                    if item["identity_home"]
                ],
                [second],
            )
            deleted = runtime.collaboration.delete_channel(
                f"mailbox:{second}",
            )
            self.assertTrue(deleted.ok, deleted.reason)
            self.assertIsNone(runtime.relay_manager.target_for_topic(
                runtime.session.identity.uuid,
            ))

    def test_manage_channels_reports_topics_and_can_stop_all_use(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as relay_root,
        ):
            runtime, board = _runtime_with_topic(8215, {
                "storage_file": str(Path(tmp) / "state.json"),
                "relay_state_directory": tmp,
            })
            target_id = runtime.relay_manager.create_target({
                "name": "T5", "backend": "local", "root": relay_root,
            }).value
            runtime.relay_manager.assign_topic_target(board.uuid, target_id)

            channel = next(
                item for item in runtime.collaboration.channels_payload()["channels"]
                if item["id"] == target_id
            )
            stopped = runtime.collaboration.stop_channel(
                f"mailbox:{target_id}",
            )
            after = next(
                item for item in runtime.collaboration.channels_payload()["channels"]
                if item["id"] == target_id
            )

            self.assertEqual(
                {(item["title"], item["identity"])
                 for item in channel["assigned_topics"]},
                {("My identity", True), (board.data["name"], False)},
            )
            self.assertTrue(stopped.ok, stopped.reason)
            self.assertEqual(after["assigned_topics"], [])
            self.assertFalse(after["identity_home"])
            self.assertIsNone(
                runtime.relay_manager.target_for_topic(board.uuid),
            )
            self.assertIsNone(runtime.relay_manager.target_for_topic(
                runtime.session.identity.uuid,
            ))

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

    def test_connect_token_without_a_channel_has_nothing_to_offer(self):
        # Every channel is a place to publish, so an invitation naming none
        # has no route to describe. There is no channel-less token left.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, board = _runtime_with_topic(8203, {
                "storage_file": str(Path(tmp) / "state.json"),
            })
            endpoint = self._connect_token_endpoint(runtime)
            response = asyncio.run(endpoint(self._post_request(
                "/api/core/invitations", {"topic_uuid": board.uuid},
            )))

        self.assertGreaterEqual(response.status_code, 400)

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
            channel, sorted(["topic-1", inviter.identity.uuid]),
            inviter.identity.uuid,
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

        self.assertEqual(
            [topics for _d, topics, _i in manager.accept_calls],
            [sorted(["topic-1", inviter.identity.uuid])],
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["channels_used"], ["mailbox"])
        self.assertEqual(
            session.peer_channel_for_topic("relay:B", "topic-1"), "mailbox",
        )

    def test_accept_connect_token_rejects_multiple_channels_without_mapping(self):
        session = Session("http://a")
        inviter = Session("http://b")
        manager = _FakeRelayManager()
        runtime = types.SimpleNamespace(
            address="http://a", session=session,
            config={}, relay_manager=manager,
        )
        result = self._accept_connect_token(
            runtime, identity=inviter.identity.to_dict(),
            topic_uuids=["topic-1"],
            channels=[
                {"type": "relay", "descriptor_version": 1, "root": "x", "identity": "B"},
                {
                    "type": "sftp", "descriptor_version": 1, "host": "h",
                    "username": "u", "root": "/", "identity": "B",
                },
            ],
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(
            result["reason"],
            "topic-to-channel mapping is missing or incomplete",
        )
        self.assertNotIn("relay:B", session.peer_topic_channel)
        self.assertEqual(manager.accept_calls, [])

    def test_accept_connect_token_routes_each_topic_through_its_mapping(self):
        session = Session("http://a")
        inviter = Session("http://b")
        manager = _FakeRelayManager()
        runtime = types.SimpleNamespace(
            address="http://a", session=session,
            config={}, relay_manager=manager,
        )
        board_channel = {
            "channel_id": "board-channel",
            "type": "relay", "descriptor_version": 1,
            "root": "board", "identity": "B",
        }
        identity_channel = {
            "channel_id": "identity-channel",
            "type": "relay", "descriptor_version": 1,
            "root": "identity", "identity": "B",
        }

        result = self._accept_connect_token(
            runtime,
            identity=inviter.identity.to_dict(),
            topic_uuids=["topic-1", inviter.identity.uuid],
            channels=[identity_channel, board_channel],
            topic_channels={
                inviter.identity.uuid: "identity-channel",
                "topic-1": "board-channel",
            },
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [call[1] for call in manager.accept_calls],
            [[inviter.identity.uuid], ["topic-1"]],
        )

    def test_accept_connect_token_reconnect_replaces_old_channel(self):
        session = Session("http://a")
        bob_session = Session("http://b-old")
        bob_session.set_identity("Bob")
        identity_payload = bob_session.identity.to_dict()
        runtime = types.SimpleNamespace(
            address="http://a", session=session,
            config={}, relay_manager=_FakeRelayManager(),
        )

        # Bob was already reachable under an earlier publication identity,
        # as a prior accepted token would have left him.
        session.note_indirect_peer_topic("relay:B-old", "topic-1")
        session.apply_peer_identity_snapshot("relay:B-old", identity_payload)
        session.bind_peer_topic_channel("relay:B-old", "topic-1", "mailbox")

        result = self._accept_connect_token(
            runtime, identity=identity_payload, topic_uuids=["topic-1"],
            channels=[{
                "type": "relay", "descriptor_version": 1, "root": "x",
                "identity": "B",
            }],
        )

        self.assertEqual(result["status"], "ok")
        self.assertNotIn("relay:B-old", session.peer_topic_sets)
        self.assertNotIn("relay:B-old", session.peer_perspectives)
        self.assertNotIn("relay:B-old", session.peer_topic_channel)
        self.assertEqual(
            session.peer_channel_for_topic("relay:B", "topic-1"), "mailbox",
        )
        cached = session.find_peer_identity(identity_payload["data"]["identity_key"])
        self.assertIsNotNone(cached)
        # The old address stays learned history, not an active route.
        self.assertIn("relay:B-old", session.peer_identity_key)
        self.assertEqual(
            session.peer_identity_key.get("relay:B"),
            identity_payload["data"]["identity_key"],
        )

    def test_accept_connect_token_skips_unrecognized_channel_type_or_version(self):
        session = Session("http://a")
        runtime = types.SimpleNamespace(address="http://a", session=session, config={})

        result = self._accept_connect_token(
            runtime, identity=None, topic_uuids=["topic-1"],
            channels=[
                {"type": "carrier_pigeon", "descriptor_version": 1},
                {"type": "relay", "descriptor_version": 99, "root": "x"},
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


class ChannelPublishTickTests(unittest.TestCase):
    """A local edit only needs the way out, not a full cycle.

    A cycle publishes last, so an edit that woke the loop queued behind a
    heartbeat write and an inbound poll before it could leave - measured at a
    floor of roughly 1.5s per change, of which about 1.3s was inbound work the
    edit did not depend on.
    """

    def runtime(self, connections):
        return types.SimpleNamespace(
            config={}, session=Session("local"),
            channel_manager=_FakeRelayManager(connections),
            host=None,
            persist_confirmed_change=lambda kind=None: None,
        )

    def test_a_local_edit_publishes_without_polling_first(self):
        class Relay:
            poll_interval_seconds = 3

            def __init__(self):
                self.polled = 0
                self.published = 0

            def has_active_relationship(self):
                return True

            def poll_once(self, after_apply=None):
                self.polled += 1
                return PollCycleResult(ok=True, changed=False)

            def publish_once(self):
                self.published += 1
                return PollCycleResult(
                    ok=True, changed=True, published_before=("topic-1",),
                )

            def polling_diagnostics(self):
                return {"identity": "fake"}

        relay = Relay()

        self.assertTrue(
            asyncio.run(app_server.channel_publish_tick(self.runtime([relay]))),
        )
        self.assertEqual(relay.published, 1)
        self.assertEqual(
            relay.polled, 0,
            "the inbound poll is not what a local edit is waiting for",
        )

    def test_an_endpoint_without_a_publish_path_still_gets_a_full_cycle(self):
        # A channel predating publish_once must keep working exactly as
        # before rather than silently never publishing again.
        class OldRelay:
            poll_interval_seconds = 3

            def __init__(self):
                self.polled = 0

            def has_active_relationship(self):
                return True

            def poll_once(self, after_apply=None):
                self.polled += 1
                return PollCycleResult(ok=True, changed=False)

            def polling_diagnostics(self):
                return {"identity": "old"}

        relay = OldRelay()
        runtime = self.runtime([relay])

        asyncio.run(app_server.channel_publish_tick(runtime))

        self.assertEqual(relay.polled, 1)

    def test_nothing_to_publish_reports_no_change(self):
        class Quiet:
            poll_interval_seconds = 3

            def has_active_relationship(self):
                return True

            def poll_once(self, after_apply=None):
                raise AssertionError("must not poll")

            def publish_once(self):
                return PollCycleResult(ok=True, changed=False)

            def polling_diagnostics(self):
                return {"identity": "quiet"}

        self.assertFalse(
            asyncio.run(app_server.channel_publish_tick(self.runtime([Quiet()]))),
        )
