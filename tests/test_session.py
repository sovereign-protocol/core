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

    def test_start_discussion_allows_multiple_topics(self):
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

        self.assertEqual(result.status, "ok")
        self.assertEqual(session.active_topic_uuids, {first.uuid, second.uuid})

    def test_local_change_returns_sync_status_effects(self):
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
        self.assertEqual(result.effects[0].type, "send_sync_status")
        self.assertEqual(result.effects[0].target, "si-b")
        self.assertEqual(
            result.effects[0].payload["summary"]["topics"][topic.uuid],
            session.node_state_hash(topic.uuid),
        )

    def test_local_change_outside_topic_does_not_ping_topic_peers(self):
        session = Session("si-a")
        topic = session.create_child(
            session.protocol.root.uuid,
            {"name": "topic"},
            {},
        ).value
        session.start_discussion(topic.uuid)
        session.add_peer("si-b", topic.uuid)

        result = session.modify(session.protocol.root.uuid, {"name": "local setting"}, {})

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.effects, [])

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
        self.assertEqual(session.get_network_info()["peer_status"]["si-b"]["state"], "online")

    def test_peer_can_be_marked_offline_and_recovered(self):
        session = Session("si-a")
        session.add_peer("si-b", "topic-1")

        changed = session.mark_peer_unreachable("si-b", "timeout")

        self.assertTrue(changed)
        self.assertEqual(session.get_network_info()["peer_status"]["si-b"]["state"], "offline")
        self.assertEqual(session.get_network_info()["peer_status"]["si-b"]["last_error"], "timeout")

        recovered = session.mark_peer_reachable("si-b")

        self.assertTrue(recovered)
        self.assertEqual(session.get_network_info()["peer_status"]["si-b"]["state"], "online")

    def test_handle_ping_with_peer_cache_pulls_changed_subtree(self):
        session = Session("si-a")
        topic = PRSPNode({"name": "topic"})
        topic.uuid = "topic-1"
        topic.refresh_hashes()
        session.apply_peer_subtree("si-b", topic, None)

        result = session.handle_ping({
            "from_addr": "si-b",
            "topic_uuid": "topic-1",
            "topic_state_hash": "remote-hash",
            "changed_uuid": "changed-1",
        })

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.effects[0].payload["node_uuid"], "changed-1")

    def test_handle_ping_without_cached_topic_pulls_topic_root(self):
        session = Session("si-a")
        session.apply_peer_subtree("si-b", PRSPNode({"name": "other"}), None)

        result = session.handle_ping({
            "from_addr": "si-b",
            "topic_uuid": "topic-1",
            "topic_state_hash": "remote-hash",
            "changed_uuid": "changed-1",
        })

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.effects[0].payload["node_uuid"], "topic-1")

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
            ],
        )

    def test_handle_join_uses_members_per_topic(self):
        session = Session("si-a")

        result = session.handle_join({
            "from_addr": "si-b",
            "topic_uuids": ["topic-1", "topic-2"],
            "topic_members": {
                "topic-1": ["si-c"],
                "topic-2": ["si-d"],
            },
        })

        self.assertEqual(result.status, "ok")
        self.assertIn("si-c", session.peer_topic_sets)
        self.assertIn("si-d", session.peer_topic_sets)
        self.assertEqual(session.peer_topic_sets["si-c"], {"topic-1"})
        self.assertEqual(session.peer_topic_sets["si-d"], {"topic-2"})
        self.assertEqual(
            [(effect.target, effect.payload["topic_uuid"])
             for effect in result.effects],
            [
                ("si-b", "topic-1"),
                ("si-b", "topic-2"),
            ],
        )

    def test_handle_join_can_limit_pull_topics(self):
        session = Session("si-a")

        result = session.handle_join({
            "from_addr": "si-b",
            "topic_uuids": ["owned-by-a", "owned-by-b"],
            "pull_topic_uuids": ["owned-by-b"],
            "topic_members": {
                "owned-by-a": ["si-a", "si-b"],
                "owned-by-b": ["si-a", "si-b"],
            },
        })

        self.assertEqual(result.status, "ok")
        self.assertIn("owned-by-a", session.peer_topic_sets["si-b"])
        self.assertIn("owned-by-b", session.peer_topic_sets["si-b"])
        self.assertEqual(
            [(effect.target, effect.payload["topic_uuid"])
             for effect in result.effects],
            [("si-b", "owned-by-b")],
        )
        self.assertEqual(
            session.fetch_topic_uuids("si-b"),
            ["owned-by-b"],
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

    def test_accept_topic_invitation_attaches_topic_under_other_perspectives(self):
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
        parent = invited.protocol.index[invited.protocol.index[topic.uuid].parent_uuid]
        self.assertEqual(parent.data, {
            "type": "folder",
            "name": "other_perspectives",
        })

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

    def test_analyze_peer_transition_detects_peer_made_changes(self):
        local = Session("si-a")
        peer = Session("si-b")
        topic = local.create_child(local.protocol.root.uuid, {"name": "topic"}, {}).value
        peer.accept_topic_invitation(PRSPNode.from_dict(topic.to_dict()))
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )
        peer.modify(topic.uuid, {"name": "peer-topic"}, {})
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        events = local.analyze_peer_transitions("si-b", topic.uuid)

        self.assertEqual(events[0]["type"], "peer_made_changes")
        self.assertEqual(events[0]["node_uuid"], topic.uuid)

    def test_analyze_peer_transition_detects_in_agreement(self):
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

        self.assertEqual(events[0]["type"], "in_agreement")

    def test_analyze_peer_transition_detects_divergence(self):
        local = Session("si-a")
        peer = Session("si-b")
        topic = local.create_child(local.protocol.root.uuid, {"name": "topic"}, {}).value
        peer.accept_topic_invitation(PRSPNode.from_dict(topic.to_dict()))
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )
        local.modify(topic.uuid, {"name": "local"}, {})
        peer.modify(topic.uuid, {"name": "peer"}, {})
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        events = local.analyze_peer_transitions("si-b", topic.uuid)

        self.assertEqual(events[0]["type"], "divergence")

    def test_analyze_peer_transition_detects_local_missing_node(self):
        local = Session("si-a")
        peer = Session("si-b")
        topic = local.create_child(local.protocol.root.uuid, {"name": "topic"}, {}).value
        peer.accept_topic_invitation(PRSPNode.from_dict(topic.to_dict()))
        child = peer.create_child(topic.uuid, {"name": "peer child"}, {}).value
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        events = local.analyze_peer_transitions("si-b", child.uuid)

        self.assertEqual(events[0]["type"], "local_missing_node")

    def test_analyze_peer_transition_detects_peer_missing_node(self):
        local = Session("si-a")
        peer = Session("si-b")
        topic = local.create_child(local.protocol.root.uuid, {"name": "topic"}, {}).value
        peer.accept_topic_invitation(PRSPNode.from_dict(topic.to_dict()))
        child = local.create_child(topic.uuid, {"name": "local child"}, {}).value
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        events = local.analyze_peer_transitions("si-b", child.uuid)

        self.assertEqual(events[0]["type"], "peer_missing_node")

    def test_analyze_peer_transition_detects_peer_made_changes_after_multiple_edits(self):
        local = Session("si-a")
        peer = Session("si-c")
        topic = local.create_child(local.protocol.root.uuid, {"name": "topic"}, {}).value
        peer.accept_topic_invitation(PRSPNode.from_dict(topic.to_dict()))
        local.apply_peer_subtree(
            "si-c",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        peer.modify(topic.uuid, {"name": "peer-first"}, {})
        peer.modify(topic.uuid, {"name": "peer-second"}, {})
        local.apply_peer_subtree(
            "si-c",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        events = local.analyze_peer_transitions("si-c", topic.uuid)

        self.assertEqual(events[0]["type"], "peer_made_changes")

    def test_delete_tombstone_blocks_re_adoption_until_peer_cache_confirms_absence(self):
        local = Session("si-a")
        topic = local.create_child(local.protocol.root.uuid, {"name": "topic"}, {}).value
        child = local.create_child(topic.uuid, {"name": "child"}, {}).value
        stale_topic = PRSPNode.from_dict(local.protocol.index[topic.uuid].to_dict())
        local.start_discussion(topic.uuid)
        local.add_peer("si-b", topic.uuid)

        result = local.delete(child.uuid)

        self.assertEqual(result.status, "ok")
        self.assertEqual(local.deleted_node_uuids[child.uuid], topic.uuid)
        self.assertNotIn(child.uuid, local.protocol.index)

        local.apply_peer_subtree("si-b", stale_topic, local.protocol.root.uuid)
        self.assertEqual(local.analyze_peer_transitions("si-b", child.uuid), [])
        self.assertEqual(
            local.adopt_subtree(stale_topic.children[0], stale_topic.uuid).status,
            "error",
        )

        local.replace_subtree(stale_topic)
        self.assertNotIn(child.uuid, local.protocol.index)
        self.assertIn(child.uuid, local.deleted_node_uuids)

        current_topic = PRSPNode.from_dict(local.protocol.index[topic.uuid].to_dict())
        local.apply_peer_subtree("si-b", current_topic, local.protocol.root.uuid)

        self.assertNotIn(child.uuid, local.deleted_node_uuids)

    def test_delete_without_topic_peers_does_not_keep_tombstone(self):
        local = Session("si-a")
        topic = local.create_child(local.protocol.root.uuid, {"name": "topic"}, {}).value
        child = local.create_child(topic.uuid, {"name": "child"}, {}).value
        local.start_discussion(topic.uuid)

        result = local.delete(child.uuid)

        self.assertEqual(result.status, "ok")
        self.assertNotIn(child.uuid, local.deleted_node_uuids)

    def test_ping_compares_cached_topic_not_aggregate_peer_cache(self):
        local = Session("si-a")
        peer = Session("si-b")
        first = peer.create_child(peer.protocol.root.uuid, {"name": "first"}, {}).value
        second = peer.create_child(peer.protocol.root.uuid, {"name": "second"}, {}).value
        local.add_peer("si-b", first.uuid)
        local.add_peer("si-b", second.uuid)
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[first.uuid].to_dict()),
            peer.protocol.root.uuid,
        )
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[second.uuid].to_dict()),
            peer.protocol.root.uuid,
        )

        result = local.handle_ping({
            "from_addr": "si-b",
            "topic_uuid": first.uuid,
            "topic_state_hash": first.state_hash,
            "changed_uuid": None,
        })

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.effects, [])

    def test_ping_ignores_changed_uuid_when_cached_topic_hash_matches(self):
        local = Session("si-a")
        peer = Session("si-b")
        topic = peer.create_child(peer.protocol.root.uuid, {"name": "topic"}, {}).value
        child = peer.create_child(topic.uuid, {"name": "child"}, {}).value
        local.add_peer("si-b", topic.uuid)
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            peer.protocol.root.uuid,
        )

        result = local.handle_ping({
            "from_addr": "si-b",
            "topic_uuid": topic.uuid,
            "topic_state_hash": peer.protocol.index[topic.uuid].state_hash,
            "changed_uuid": child.uuid,
        })

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.effects, [])

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

    def test_leave_topic_removes_only_that_topic(self):
        session = Session("si-a")
        session.add_peer("si-b", "topic-1")
        session.add_peer("si-b", "topic-2")
        session.add_peer("si-c", "topic-1")

        result = session.leave_topic("topic-1")

        self.assertEqual(session.peer_topic_sets["si-b"], {"topic-2"})
        self.assertNotIn("si-c", session.members)
        self.assertEqual(
            sorted((effect.target, effect.payload["topic_uuids"])
                   for effect in result.effects),
            [
                ("si-b", ["topic-1"]),
                ("si-c", ["topic-1"]),
            ],
        )

    def test_handle_leave_removes_only_named_topics(self):
        session = Session("si-a")
        session.add_peer("si-b", "topic-1")
        session.add_peer("si-b", "topic-2")

        result = session.handle_leave({
            "from_addr": "si-b",
            "topic_uuids": ["topic-1"],
        })

        self.assertEqual(result.status, "ok")
        self.assertIn("si-b", session.members)
        self.assertEqual(session.peer_topic_sets["si-b"], {"topic-2"})


if __name__ == "__main__":
    unittest.main()
