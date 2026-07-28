import tempfile
import unittest
from pathlib import Path

from sovereign import app_server
from sovereign.protocol_explorer import ManualLogic
from sovereign.protocol import ProtocolNode
from sovereign.session import Session


def sync(*runtimes) -> None:
    """Move work between clients the only way a relay can: each publishes
    what changed, then each reads what the others left. Twice, because a
    client given a topic in the first round has nothing of its own to
    publish until it has grafted it."""
    for _ in range(2):
        for runtime in runtimes:
            runtime.relay.write_presence()
            runtime.relay.publish_due_topics()
        for runtime in runtimes:
            runtime.relay.poll_and_apply()


class ManualLogicTests(unittest.TestCase):
    def test_manual_logic_uses_supplied_session(self):
        session = Session("http://a")
        logic = ManualLogic(session, {})

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

    def test_the_explorer_caches_an_invited_topic_without_claiming_it(self):
        # The Explorer registers no topic handler, so an invited topic must
        # arrive as a cached peer perspective and a pending invitation, and
        # keep arriving as its author changes it. The inviter here is a stub
        # application, not a real one: Core must not depend on any.
        with tempfile.TemporaryDirectory() as tmp, \
                tempfile.TemporaryDirectory() as relay_root:
            left = self._manual_runtime(8141, tmp, relay_root)
            right = self._manual_runtime(8142, tmp, relay_root)
            topic = left.logic.create_child(
                left.session.protocol.root.uuid,
                {"type": "notes", "name": "topic"},
                {},
            ).value
            left.session.shared_topics.register(
                "test-notes", {"notes"}, lambda: [topic.uuid],
                left.session.accept_topic_invitation,
            )
            left.logic.start_discussion(topic.uuid)

            invite = self._connect(left, right, topic.uuid)

            self.assertEqual(invite["status"], "ok")
            self.assertIn(topic.uuid, left.session.active_topic_uuids)
            self.assertNotIn(topic.uuid, right.session.active_topic_uuids)
            self.assertNotIn(topic.uuid, right.session.protocol.index)
            self.assertIn(topic.uuid, right.session.pending_topic_invitations)
            self.assertIsNotNone(right.session.get_cached_peer_subtree(
                left.peer_addr, topic.uuid,
            ))

            child_result = left.logic.create_child(
                topic.uuid,
                {"name": "child"},
                {},
            )
            sync(left, right)

            self.assertEqual(child_result.status, "ok")
            cached = right.session.get_cached_peer_subtree(
                left.peer_addr,
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
    def _manual_runtime(port: int, directory: str, relay_root: str | None = None):
        config = app_server.load_config(None, "manual")
        config["storage_file"] = str(Path(directory) / f"{port}.json")
        if relay_root is None:
            return app_server.create_runtime(port, config)
        config["relay_state_directory"] = str(Path(directory) / f"relay-{port}")
        runtime = app_server.create_runtime(port, config)
        created = runtime.relay_manager.create_target({
            "name": f"relay {port}", "backend": "local", "root": relay_root,
        })
        if created.status != "ok":
            raise RuntimeError(created.reason)
        runtime.relay_target = created.value
        runtime.relay = runtime.relay_manager.connection_for_target(created.value)
        # How the other client's registries name this one: a relay peer is a
        # publication identity, not an address anybody can reach.
        runtime.peer_addr = f"relay:{runtime.relay.identity}"
        return runtime

    @staticmethod
    def _connect(host, guest, topic_uuid: str) -> dict:
        attached = host.mailbox_channel.attach_topics(
            [topic_uuid], {"target_id": host.relay_target},
        )
        if not attached.ok:
            return {"status": "error", "reason": attached.reason}
        identity_uuid = host.session.identity.uuid
        token = host.channel_manager.compose_token(
            [topic_uuid], {
                topic_uuid: {
                    "kind": "mailbox", "target_id": host.relay_target,
                },
                identity_uuid: {
                    "kind": "mailbox", "target_id": host.relay_target,
                },
            },
        )
        if not token.ok:
            return {"status": "error", "reason": token.reason}
        result = guest.channel_manager.accept_token(token.value)
        if not result.ok:
            return {"status": "error", "reason": result.reason}
        sync(host, guest)
        return result.value


if __name__ == "__main__":
    unittest.main()
