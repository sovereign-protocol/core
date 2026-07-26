import tempfile
import unittest
from pathlib import Path

from sovereign import app_server
from sovereign.protocol_explorer import ManualLogic, create_logic
from sovereign.protocol import ProtocolNode
from sovereign.session import Session


class MemoryHttpClient:
    def __init__(self, runtimes):
        self.runtimes = runtimes

    def get_json(self, url: str, timeout: float = 5) -> dict:
        runtime, path = self._split_url(url)
        if path.startswith("/p2p/subtree/"):
            payload, status = runtime.adapter.p2p_subtree(path.rsplit("/", 1)[1])
            if status != 200:
                raise RuntimeError(payload.get("reason", "not found"))
            return payload
        raise RuntimeError(f"unexpected GET {path}")

    def post_json(self, url: str, payload: dict,
                  timeout: float = 5) -> dict:
        runtime, path = self._split_url(url)
        if path == "/api/join_discussion":
            return runtime.channel_manager.join_discussion(
                payload["address"],
                payload.get("topic_uuid"),
                payload.get("topic_uuids"),
            )
        if path == "/p2p/join":
            response, status = runtime.adapter.p2p_join(payload)
            if status != 200:
                raise RuntimeError(response.get("reason", "join failed"))
            return response
        if path == "/p2p/sync_status":
            response, status = runtime.adapter.p2p_sync_status(payload)
            if status != 200:
                raise RuntimeError(response.get("reason", "sync failed"))
            return response
        if path == "/p2p/announce":
            response, status = runtime.adapter.p2p_announce(payload)
            if status != 200:
                raise RuntimeError(response.get("reason", "announce failed"))
            return response
        if path == "/p2p/leave":
            response, status = runtime.adapter.p2p_leave(payload)
            if status != 200:
                raise RuntimeError(response.get("reason", "leave failed"))
            return response
        raise RuntimeError(f"unexpected POST {path}")

    def _split_url(self, url: str):
        for address in sorted(self.runtimes, key=len, reverse=True):
            if url.startswith(address):
                return self.runtimes[address], url[len(address):]
        raise RuntimeError(f"unknown address in {url}")


class ManualLogicTests(unittest.TestCase):
    def test_create_logic_returns_manual_logic(self):
        session = Session("http://a")

        logic = create_logic(session, {})

        self.assertIsInstance(logic, ManualLogic)
        self.assertIs(logic.session, session)

    def test_atomic_operations_update_session(self):
        session = Session("http://a")
        logic = ManualLogic(session, {})
        root_uuid = session.protocol.root.uuid

        child_result = logic.create_child(root_uuid, {"name": "child"}, {})
        child = child_result.value
        modify_result = logic.modify(child.uuid, {"name": "changed"}, {})
        copy_result = logic.copy(child.uuid, root_uuid)
        delete_result = logic.delete(child.uuid)

        self.assertEqual(child_result.status, "ok")
        self.assertEqual(modify_result.status, "ok")
        self.assertEqual(copy_result.status, "ok")
        self.assertEqual(delete_result.status, "ok")
        self.assertNotIn(child.uuid, session.protocol.index)
        self.assertIn(copy_result.value.uuid, session.protocol.index)

    def test_start_discussion_sets_active_topic(self):
        session = Session("http://a")
        logic = ManualLogic(session, {})
        topic = logic.create_child(
            session.protocol.root.uuid,
            {"name": "topic"},
            {},
        ).value

        result = logic.start_discussion(topic.uuid)

        self.assertEqual(result.status, "ok")
        self.assertEqual(session.active_topic_uuid, topic.uuid)

    def test_state_includes_root_network_and_peers(self):
        session = Session("http://a")
        logic = ManualLogic(session, {})

        state = logic.state()

        self.assertEqual(state["root"]["uuid"], session.protocol.root.uuid)
        self.assertEqual(state["network"]["address"], "http://a")
        self.assertEqual(state["peers"], {})




    def test_app_server_manual_defaults_load_manual_module(self):
        config = app_server.load_config(None, "manual")
        with tempfile.TemporaryDirectory() as tmp:
            config["storage_file"] = str(Path(tmp) / "state.json")
            runtime = app_server.create_runtime(8130, config)
            app = app_server.build_app(runtime)

        self.assertIsInstance(runtime.logic, ManualLogic)
        paths = {route.path for route in app.routes}
        self.assertIn("/api/protocol-explorer/state", paths)

    def test_two_clients_invite_and_sync_child_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            left = self._manual_runtime(8141, tmp)
            right = self._manual_runtime(8142, tmp)
            client = MemoryHttpClient({
                left.address: left,
                right.address: right,
            })
            left.adapter.http = client
            right.adapter.http = client

            topic = left.logic.create_child(
                left.session.protocol.root.uuid,
                {"name": "topic"},
                {},
            ).value
            left.logic.start_discussion(topic.uuid)

            invite = left.adapter.invite_to_discuss(right.address, topic.uuid)

            self.assertEqual(invite["status"], "ok")
            self.assertEqual(left.session.active_topic_uuid, topic.uuid)
            self.assertNotIn(topic.uuid, right.session.active_topic_uuids)
            self.assertIn(right.session.identity.uuid, right.session.active_topic_uuids)
            self.assertNotIn(topic.uuid, right.session.protocol.index)
            self.assertIn(topic.uuid, right.session.pending_topic_invitations)
            self.assertIsNotNone(right.session.get_cached_peer_subtree(
                left.address, topic.uuid,
            ))
            self.assertIn(right.address, left.session.members)
            self.assertIn(left.address, right.session.members)

            child_result = left.logic.create_child(
                topic.uuid,
                {"name": "child"},
                {},
            )
            deliveries = left.adapter.execute_effects(child_result.effects)

            self.assertEqual(child_result.status, "ok")
            self.assertTrue(all(delivery.ok for delivery in deliveries))
            cached = right.session.get_cached_peer_subtree(
                left.address,
                child_result.value.uuid,
            )
            self.assertIsNotNone(cached)
            self.assertEqual(cached.data["name"], "child")

    def test_accept_peer_node_replaces_local_version(self):
        left = Session("http://a")
        right = Session("http://b")
        logic = ManualLogic(left, {})
        local = logic.create_child(
            left.protocol.root.uuid,
            {"name": "local"},
            {},
        ).value
        right_topic = right.accept_topic_invitation(
            ProtocolNode.from_dict(local.to_dict())
        )
        self.assertEqual(right_topic.status, "ok")
        peer_node = right.get_node(local.uuid)
        right.modify(peer_node.uuid, {"name": "peer"}, {})
        left.apply_peer_subtree(
            "http://b",
            right.get_node(local.uuid),
            right.root_uuid(),
        )

        result = logic.accept_peer_node("http://b", local.uuid)

        self.assertEqual(result.status, "ok")
        self.assertEqual(left.protocol.index[local.uuid].data["name"], "peer")

    def test_accept_peer_absence_deletes_local_node(self):
        session = Session("http://a")
        logic = ManualLogic(session, {})
        node = logic.create_child(
            session.protocol.root.uuid,
            {"name": "local-only"},
            {},
        ).value

        result = logic.accept_peer_node("http://b", node.uuid, adopt_absence=True)

        self.assertEqual(result.status, "ok")
        self.assertNotIn(node.uuid, session.protocol.index)


    @staticmethod
    def _manual_runtime(port: int, directory: str):
        config = app_server.load_config(None, "manual")
        config["storage_file"] = str(Path(directory) / f"{port}.json")
        return app_server.create_runtime(port, config)


if __name__ == "__main__":
    unittest.main()
