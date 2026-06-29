import unittest

from protocol import PRSPNode
from session import Session


class SessionTests(unittest.TestCase):
    def test_start_discussion_tracks_topic_and_members(self):
        session = Session("si-a")
        topic = session.create_child(
            session.protocol.root.uuid,
            {"name": "topic"},
            {},
        ).value

        result = session.start_discussion(topic.uuid)

        self.assertEqual(result.status, "ok")
        self.assertEqual(session.active_topic_uuid, topic.uuid)
        self.assertEqual(session.members, {"si-a"})

    def test_start_discussion_rejects_different_topic_while_active(self):
        session = Session("si-a")
        first = session.create_child(
            session.protocol.root.uuid,
            {"name": "first"},
            {},
        ).value
        second = session.create_child(
            session.protocol.root.uuid,
            {"name": "second"},
            {},
        ).value
        session.start_discussion(first.uuid)

        result = session.start_discussion(second.uuid)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.reason, "already discussing a different topic")
        self.assertEqual(session.active_topic_uuid, first.uuid)

    def test_local_change_returns_send_ping_effects(self):
        session = Session("si-a")
        topic = session.create_child(
            session.protocol.root.uuid,
            {"name": "topic"},
            {},
        ).value
        session.start_discussion(topic.uuid)
        session.add_peer("si-b", topic.uuid)

        result = session.create_child(topic.uuid, {"name": "child"}, {})

        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.effects), 1)
        self.assertEqual(result.effects[0].type, "send_ping")
        self.assertEqual(result.effects[0].target, "si-b")
        self.assertEqual(result.effects[0].payload["topic_uuid"], topic.uuid)

    def test_handle_ping_without_peer_cache_pulls_topic(self):
        session = Session("si-a")

        result = session.handle_ping({
            "from_addr": "si-b",
            "topic_uuid": "topic-1",
            "topic_state_hash": "remote-hash",
            "changed_uuid": "changed-1",
        })

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.effects[0].type, "pull_subtree")
        self.assertEqual(result.effects[0].target, "si-b")
        self.assertEqual(result.effects[0].payload["node_uuid"], "topic-1")

    def test_handle_ping_with_peer_cache_pulls_changed_subtree(self):
        session = Session("si-a")
        session.apply_peer_subtree("si-b", PRSPNode({"name": "topic"}), None)

        result = session.handle_ping({
            "from_addr": "si-b",
            "topic_uuid": "topic-1",
            "topic_state_hash": "remote-hash",
            "changed_uuid": "changed-1",
        })

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.effects[0].payload["node_uuid"], "changed-1")

    def test_handle_join_tracks_known_members_and_returns_pull_effects(self):
        session = Session("si-a")

        result = session.handle_join({
            "from_addr": "si-b",
            "topic_uuid": "topic-1",
            "known_members": ["si-c"],
        })

        self.assertEqual(result.status, "ok")
        self.assertEqual(session.members, {"si-a", "si-b", "si-c"})
        self.assertEqual(session.peer_topics["si-b"], "topic-1")
        self.assertEqual(session.peer_topics["si-c"], "topic-1")
        self.assertEqual(
            [(effect.type, effect.target, effect.payload["node_uuid"])
             for effect in result.effects],
            [
                ("pull_subtree", "si-b", "topic-1"),
                ("pull_subtree", "si-c", "topic-1"),
            ],
        )

    def test_handle_join_preserves_existing_peer_cache(self):
        session = Session("si-a")
        cached = PRSPNode({"name": "cached-topic"})
        session.apply_peer_subtree("si-b", cached, None)

        result = session.handle_join({
            "from_addr": "si-b",
            "topic_uuid": "topic-1",
            "known_members": [],
        })

        self.assertEqual(result.status, "ok")
        self.assertIsNotNone(session.get_cached_peer_subtree(
            "si-b",
            cached.uuid,
        ))
        self.assertEqual(result.effects[0].type, "pull_subtree")
        self.assertEqual(result.effects[0].payload["node_uuid"], "topic-1")

    def test_handle_announce_preserves_existing_peer_cache(self):
        session = Session("si-a")
        cached = PRSPNode({"name": "cached-topic"})
        session.apply_peer_subtree("si-b", cached, None)

        result = session.handle_announce({
            "new_addr": "si-b",
            "topic_uuid": "topic-1",
        })

        self.assertEqual(result.status, "ok")
        self.assertIsNotNone(session.get_cached_peer_subtree(
            "si-b",
            cached.uuid,
        ))
        self.assertEqual(result.effects[0].type, "pull_subtree")
        self.assertEqual(result.effects[0].payload["node_uuid"], "topic-1")

    def test_accept_topic_invitation_attaches_topic_under_root(self):
        inviter = Session("si-a")
        topic = inviter.create_child(
            inviter.protocol.root.uuid,
            {"name": "topic"},
            {},
        ).value

        invited = Session("si-b")
        result = invited.accept_topic_invitation(PRSPNode.from_dict(topic.to_dict()))

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value, topic.uuid)
        self.assertEqual(invited.active_topic_uuid, topic.uuid)
        self.assertIn(topic.uuid, invited.protocol.index)
        self.assertEqual(invited.protocol.index[topic.uuid].parent_uuid,
                         invited.protocol.root.uuid)

    def test_apply_peer_subtree_updates_peer_cache_without_http(self):
        session = Session("si-a")
        topic = PRSPNode({"name": "topic"})
        child = PRSPNode({"name": "child"}, parent_uuid=topic.uuid)
        topic.children.append(child)
        topic.refresh_hashes()

        session.apply_peer_subtree("si-b", topic, None)

        cached = session.get_cached_peer_subtree("si-b", child.uuid)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.data["name"], "child")

    def test_analyze_peer_transition_detects_intentional_change(self):
        local = Session("si-a")
        peer = Session("si-b")
        topic = local.create_child(local.protocol.root.uuid, {"name": "topic"}, {}).value
        peer.accept_topic_invitation(PRSPNode.from_dict(topic.to_dict()))
        peer.modify(topic.uuid, {"name": "peer-topic"}, {})
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        events = local.analyze_peer_transitions("si-b", topic.uuid)

        self.assertEqual(events[0]["type"], "intentional_change")
        self.assertEqual(events[0]["node_uuid"], topic.uuid)

    def test_analyze_peer_transition_detects_accepted_change(self):
        local = Session("si-a")
        peer = Session("si-b")
        topic = local.create_child(local.protocol.root.uuid, {"name": "topic"}, {}).value
        local.modify(topic.uuid, {"name": "new-topic"}, {})
        peer.accept_topic_invitation(PRSPNode.from_dict(
            local.protocol.index[topic.uuid].to_dict()
        ))
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        events = local.analyze_peer_transitions("si-b", topic.uuid)

        self.assertEqual(events[0]["type"], "accepted_change")

    def test_analyze_peer_transition_detects_conflict(self):
        local = Session("si-a")
        peer = Session("si-b")
        topic = local.create_child(local.protocol.root.uuid, {"name": "topic"}, {}).value
        peer.accept_topic_invitation(PRSPNode.from_dict(topic.to_dict()))
        local.modify(topic.uuid, {"name": "local"}, {})
        peer.modify(topic.uuid, {"name": "peer"}, {})
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        events = local.analyze_peer_transitions("si-b", topic.uuid)

        self.assertEqual(events[0]["type"], "conflict")

    def test_leave_returns_transport_effects_and_clears_session(self):
        session = Session("si-a")
        session.add_peer("si-b", "topic-1")
        session.add_peer("si-c", "topic-1")

        result = session.leave()

        self.assertEqual(session.members, {"si-a"})
        self.assertEqual(session.peer_topics, {})
        self.assertEqual(
            sorted((effect.type, effect.target) for effect in result.effects),
            [
                ("announce_peer", "si-b"),
                ("announce_peer", "si-c"),
                ("send_leave", "si-b"),
                ("send_leave", "si-c"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
