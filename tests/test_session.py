import unittest

from protocol import ProtocolNode
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

    def test_topic_members_excludes_relay_pseudo_addresses(self):
        # Regression, found live: a relay pseudo-address (e.g. "relay:B")
        # lives in peer_topic_sets (Session.note_relay_peer_topic -
        # deliberate, so kanban's eligibility checks recognize it) but is
        # never registered via add_peer, specifically to keep it out of
        # self.members. topic_members used to union in peer_topic_sets
        # unconditionally, so mentioning that topic anywhere near a real
        # HTTP join (topic_members_by_topic feeds both the outgoing join
        # request and handle_join's response) would leak "relay:B" into
        # self.members via handle_join's blind add_peer loop over whatever
        # the other side reports - not just on the two sides actually using
        # that relay channel, but on every peer who later joins that topic.
        session = Session("si-a")
        topic = session.create_child(session.protocol.root.uuid, {"name": "topic"}, {}).value
        session.start_discussion(topic.uuid)
        session.note_relay_peer_topic("relay:B", topic.uuid)
        session.add_peer("si-c", topic.uuid)

        members = session.topic_members(topic.uuid)

        self.assertEqual(members, {"si-a", "si-c"})
        self.assertNotIn("relay:B", members)
        self.assertNotIn("relay:B", session.topic_members_by_topic([topic.uuid])[topic.uuid])

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

    def test_successful_sync_response_acknowledges_exact_node_revisions(self):
        session = Session("si-a")
        topic = session.create_child(
            session.protocol.root.uuid, {"name": "topic"}, {},
        ).value
        child = session.create_child(topic.uuid, {"name": "card"}, {}).value
        session.start_discussion(topic.uuid)
        session.add_peer("si-b", topic.uuid)
        summary = session.sync_summary("si-b")

        result = session.handle_sync_response("si-b", {
            "status": "ok",
            "delivered_sync_hash": summary["sync_hash"],
            "my_summary": {},
        })

        self.assertEqual(result.status, "ok")
        topic = session.protocol.index[topic.uuid]
        child = session.protocol.index[child.uuid]
        self.assertTrue(session.peer_observed_node("si-b", topic))
        self.assertTrue(session.peer_observed_node("si-b", child))

    def test_partial_sync_response_does_not_acknowledge_nodes(self):
        session = Session("si-a")
        topic = session.create_child(
            session.protocol.root.uuid, {"name": "topic"}, {},
        ).value
        session.start_discussion(topic.uuid)
        session.add_peer("si-b", topic.uuid)
        summary = session.sync_summary("si-b")

        session.handle_sync_response("si-b", {
            "status": "partial",
            "delivered_sync_hash": summary["sync_hash"],
            "my_summary": {},
        })

        self.assertFalse(session.peer_observed_node("si-b", topic))

    def test_node_revision_changes_when_node_moves(self):
        session = Session("si-a")
        first = session.create_child(
            session.protocol.root.uuid, {"name": "first"}, {},
        ).value
        second = session.create_child(
            session.protocol.root.uuid, {"name": "second"}, {},
        ).value
        child = session.create_child(first.uuid, {"name": "card"}, {}).value
        child = session.protocol.index[child.uuid]
        before = session.node_revision(child)

        session.move_child(child.uuid, second.uuid)

        moved = session.protocol.index[child.uuid]
        self.assertNotEqual(before, session.node_revision(moved))

    def test_local_change_outside_topic_does_not_sync_topic_peers(self):
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

    def test_handle_join_ignores_unrelated_members_without_topic_backing(self):
        # A join request that only names "si-c" via an unrelated/legacy
        # global member list - not per-topic topic_members - must not
        # register si-c against this topic. There's no real member data for
        # topic-1 beyond the joiner itself, and treating an absent topic key
        # as "fall back to some other list" is exactly the bug that let a
        # peer from an unrelated topic leak into this one.
        session = Session("si-a")

        result = session.handle_join({
            "from_addr": "si-b",
            "topic_uuid": "topic-1",
        })

        self.assertEqual(result.status, "ok")
        self.assertEqual(session.members, {"si-a", "si-b"})
        self.assertEqual(session.peer_topics["si-b"], "topic-1")
        self.assertNotIn("si-c", session.peer_topics)
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
        cached = ProtocolNode({"name": "cached-topic"})
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
        cached = ProtocolNode({"name": "cached-topic"})
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
        result = invited.accept_topic_invitation(ProtocolNode.from_dict(topic.to_dict()))

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
        topic = ProtocolNode({"name": "topic"})
        child = ProtocolNode({"name": "child"}, parent_uuid=topic.uuid)
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
        peer.accept_topic_invitation(ProtocolNode.from_dict(topic.to_dict()))
        local.apply_peer_subtree(
            "si-b",
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )
        peer.modify(topic.uuid, {"name": "peer-topic"}, {})
        local.apply_peer_subtree(
            "si-b",
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
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
        peer.accept_topic_invitation(ProtocolNode.from_dict(
            local.protocol.index[topic.uuid].to_dict()
        ))
        local.apply_peer_subtree(
            "si-b",
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        events = local.analyze_peer_transitions("si-b", topic.uuid)

        self.assertEqual(events[0]["type"], "in_agreement")

    def test_analyze_peer_transition_detects_divergence(self):
        local = Session("si-a")
        peer = Session("si-b")
        local_identity = local.identity.data["identity_key"]
        peer_identity = peer.identity.data["identity_key"]
        local.set_peer_identity_key("si-b", peer_identity)
        peer.set_peer_identity_key("si-a", local_identity)
        topic = local.create_child(local.protocol.root.uuid, {"name": "topic"}, {}).value
        peer.accept_topic_invitation(ProtocolNode.from_dict(topic.to_dict()))
        local.apply_peer_subtree(
            "si-b",
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )
        local.modify(topic.uuid, {"name": "local"}, {})
        peer.modify(topic.uuid, {"name": "peer"}, {})
        local.apply_peer_subtree(
            "si-b",
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        events = local.analyze_peer_transitions("si-b", topic.uuid)

        self.assertEqual(events[0]["type"], "divergence")

    def test_analyze_peer_transition_detects_local_missing_node(self):
        local = Session("si-a")
        peer = Session("si-b")
        topic = local.create_child(local.protocol.root.uuid, {"name": "topic"}, {}).value
        peer.accept_topic_invitation(ProtocolNode.from_dict(topic.to_dict()))
        child = peer.create_child(topic.uuid, {"name": "peer child"}, {}).value
        local.apply_peer_subtree(
            "si-b",
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        events = local.analyze_peer_transitions("si-b", child.uuid)

        self.assertEqual(events[0]["type"], "local_missing_node")

    def test_analyze_peer_transition_detects_peer_missing_node(self):
        local = Session("si-a")
        peer = Session("si-b")
        topic = local.create_child(local.protocol.root.uuid, {"name": "topic"}, {}).value
        peer.accept_topic_invitation(ProtocolNode.from_dict(topic.to_dict()))
        child = local.create_child(topic.uuid, {"name": "local child"}, {}).value
        local.apply_peer_subtree(
            "si-b",
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        events = local.analyze_peer_transitions("si-b", child.uuid)

        self.assertEqual(events[0]["type"], "in_transition")
        self.assertEqual(events[0]["original_type"], "peer_missing_node")

    def test_analyze_peer_transition_compounds_multiple_unsynced_edits(self):
        local = Session("si-a")
        peer = Session("si-c")
        topic = local.create_child(local.protocol.root.uuid, {"name": "topic"}, {}).value
        peer.accept_topic_invitation(ProtocolNode.from_dict(topic.to_dict()))
        local.apply_peer_subtree(
            "si-c",
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        peer.modify(topic.uuid, {"name": "peer-first"}, {})
        peer.modify(topic.uuid, {"name": "peer-second"}, {})
        local.apply_peer_subtree(
            "si-c",
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        events = local.analyze_peer_transitions("si-c", topic.uuid)

        self.assertEqual(events[0]["type"], "peer_made_changes")

    @staticmethod
    def _node(state_hash, base_hash, parent_uuid, base_parent_uuid,
              origin=None):
        node = ProtocolNode({"name": "x"})
        node.state_hash = state_hash
        node.base_hash = base_hash
        node.parent_uuid = parent_uuid
        node.base_parent_uuid = base_parent_uuid
        node.revision_origin = origin
        return node

    def test_opposing_moves_are_divergence(self):
        local_node = self._node("h0", "h0", "doing", "todo")
        peer_node = self._node("h0", "h0", "todo", "doing")
        local_node.updated_at = "2026-01-01T00:00:00.000+00:00"
        peer_node.updated_at = "2026-01-01T00:00:00.000+00:00"

        self.assertEqual(Session._classify_move(local_node, peer_node), "divergence")
        self.assertEqual(Session._classify_node(local_node, peer_node), "divergence")

    def test_opposing_moves_use_newer_timestamp_as_clean_move(self):
        local_node = self._node("h0", "h0", "todo", "doing")
        peer_node = self._node("h0", "h0", "doing", "todo")
        local_node.updated_at = "2026-01-01T00:00:00.000+00:00"
        peer_node.updated_at = "2026-01-01T00:00:01.000+00:00"

        self.assertEqual(Session._classify_move(local_node, peer_node), "peer_made_changes")

    def test_transition_staging_waits_for_peer_observation(self):
        event = {
            "type": "local_made_changes",
            "node_uuid": "n1",
            "peer_observed_local_revision": False,
        }

        waiting = Session._stage_transition_event(event)
        confirmed = Session._stage_transition_event({
            **event, "peer_observed_local_revision": True,
        })

        self.assertEqual(waiting["type"], "in_transition")
        self.assertEqual(confirmed["type"], "divergence")

    def test_competing_origins_confirm_divergence_immediately(self):
        event = {
            "type": "divergence",
            "node_uuid": "n1",
            "local_revision_origin": "identity-a",
            "peer_revision_origin": "identity-b",
            "peer_observed_local_revision": False,
        }

        self.assertEqual(
            Session._stage_transition_event(event)["type"], "divergence",
        )

    def test_peer_change_is_actionable_without_debounce(self):
        event = {
            "type": "peer_made_changes",
            "node_uuid": "n1",
            "peer_observed_local_revision": False,
        }

        self.assertEqual(
            Session._stage_transition_event(event)["type"], "peer_made_changes",
        )


    def test_apply_peer_subtree_update_propagates_deleted_flag_and_history(self):
        local = Session("si-a")
        peer = Session("si-b")
        topic = peer.create_child(peer.protocol.root.uuid, {"name": "topic"}, {}).value
        child = peer.create_child(topic.uuid, {"name": "child"}, {}).value
        peer.start_discussion(topic.uuid)
        peer.add_peer("si-a", topic.uuid)
        local.add_peer("si-b", topic.uuid)
        local.apply_peer_subtree(
            "si-b",
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            peer.protocol.root.uuid,
        )

        peer.modify(child.uuid, {"name": "renamed"}, {})
        peer.delete(child.uuid)
        # Pull just the changed node, hitting the update-in-place merge path.
        local.apply_peer_subtree(
            "si-b",
            ProtocolNode.from_dict(peer.protocol.index[child.uuid].to_dict()),
            topic.uuid,
        )

        cached_child = local._find_in_tree(local.peer_perspectives["si-b"], child.uuid)
        peer_child = peer.protocol.index[child.uuid]
        self.assertTrue(cached_child.deleted)
        self.assertEqual(cached_child.base_hash, peer_child.base_hash)
        # If the merge left a stale deleted flag, the cache's recomputed
        # topic hash would never match what the peer reports, causing an
        # endless re-pull loop.
        self.assertEqual(
            local.cached_peer_topic_state_hash("si-b", topic.uuid),
            peer.protocol.index[topic.uuid].state_hash,
        )

    def test_delete_flags_node_and_waits_for_peer_confirmation_before_pruning(self):
        local = Session("si-a")
        topic = local.create_child(local.protocol.root.uuid, {"name": "topic"}, {}).value
        child = local.create_child(topic.uuid, {"name": "child"}, {}).value
        stale_topic = ProtocolNode.from_dict(local.protocol.index[topic.uuid].to_dict())
        local.start_discussion(topic.uuid)
        local.add_peer("si-b", topic.uuid)

        result = local.delete(child.uuid)

        self.assertEqual(result.status, "ok")
        self.assertIn(child.uuid, local.protocol.index)
        self.assertTrue(local.protocol.index[child.uuid].deleted)

        # Peer's cache still shows the pre-delete state - not confirmed yet,
        # so the flagged node must stay in the tree.
        local.apply_peer_subtree("si-b", stale_topic, local.protocol.root.uuid)
        self.assertIn(child.uuid, local.protocol.index)

        # Once the peer's cache also shows the node deleted, it can be pruned.
        current_topic = ProtocolNode.from_dict(local.protocol.index[topic.uuid].to_dict())
        local.apply_peer_subtree("si-b", current_topic, local.protocol.root.uuid)

        self.assertNotIn(child.uuid, local.protocol.index)

    def test_delete_without_topic_peers_prunes_immediately(self):
        local = Session("si-a")
        topic = local.create_child(local.protocol.root.uuid, {"name": "topic"}, {}).value
        child = local.create_child(topic.uuid, {"name": "child"}, {}).value
        local.start_discussion(topic.uuid)

        result = local.delete(child.uuid)

        self.assertEqual(result.status, "ok")
        self.assertNotIn(child.uuid, local.protocol.index)

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

    def test_watch_topic_tracks_pairs_independently_of_membership(self):
        session = Session("si-a")

        session.watch_topic("si-b", "topic-1")
        session.watch_topic("si-b", "topic-2")
        session.watch_topic("si-c", "topic-1")

        self.assertEqual(
            session.observed_topic_pairs(),
            [("si-b", "topic-1"), ("si-b", "topic-2"), ("si-c", "topic-1")],
        )
        self.assertNotIn("si-b", session.members)
        self.assertNotIn("si-b", session.peer_topic_sets)

    def test_unwatch_topic_stops_tracking_and_drops_unused_cache(self):
        session = Session("si-a")
        peer_topic = session.create_child(session.protocol.root.uuid, {"name": "topic"}, {}).value
        session.watch_topic("si-b", "topic-1")
        session.apply_peer_subtree(
            "si-b",
            ProtocolNode.from_dict(peer_topic.to_dict()),
            session.protocol.root.uuid,
        )

        removed = session.unwatch_topic("si-b", "topic-1")

        self.assertTrue(removed)
        self.assertEqual(session.observed_topics, {})
        # Nothing else needs si-b's cache, so it's cleaned up too.
        self.assertNotIn("si-b", session.peer_perspectives)

    def test_unwatch_topic_keeps_cache_if_still_a_real_peer(self):
        session = Session("si-a")
        session.watch_topic("si-b", "topic-1")
        session.add_peer("si-b", "topic-2")
        session.peer_perspectives["si-b"] = session.protocol.root

        session.unwatch_topic("si-b", "topic-1")

        self.assertIn("si-b", session.peer_perspectives)

    def test_unwatch_topic_returns_false_when_not_watching(self):
        session = Session("si-a")

        self.assertFalse(session.unwatch_topic("si-b", "topic-1"))

    def test_leave_clears_observed_topics(self):
        session = Session("si-a")
        session.watch_topic("si-b", "topic-1")

        session.leave()

        self.assertEqual(session.observed_topics, {})

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

    def test_remove_peer_clears_only_that_peer(self):
        session = Session("si-a")
        session.add_peer("si-b", "topic-1")
        session.add_peer("si-b", "topic-2")
        session.add_peer("si-c", "topic-1")
        bob_session = Session("si-b")
        bob_session.set_identity("Bob")
        session.apply_peer_identity_snapshot("si-b", bob_session.identity.to_dict())
        session.note_peer_channel("si-b", "http")

        session.remove_peer("si-b")

        self.assertNotIn("si-b", session.members)
        self.assertNotIn("si-b", session.peer_topic_sets)
        self.assertNotIn("si-b", session.peer_fetch_topic_sets)
        self.assertNotIn("si-b", session.peer_topics)
        self.assertNotIn("si-b", session.peer_perspectives)
        self.assertNotIn("si-b", session.peer_status)
        self.assertNotIn("si-b", session.peer_sync_state)
        self.assertNotIn("si-b", session.peer_channel)
        # Unrelated peer untouched.
        self.assertIn("si-c", session.members)
        self.assertEqual(session.peer_topic_sets["si-c"], {"topic-1"})

    def test_remove_peer_on_unknown_address_is_a_no_op(self):
        session = Session("si-a")
        session.add_peer("si-b", "topic-1")

        session.remove_peer("si-nobody")

        self.assertIn("si-b", session.members)

    def test_identity_is_lazily_created_and_stable(self):
        # No KanbanLogic/app involved at all - identity is a Session-owned
        # meta-topic, so any app (or none) gets it for free.
        session = Session("si-a")

        profile = session.identity

        self.assertEqual(profile.data["type"], "shared_user_profile")
        self.assertEqual(profile.data["profile_schema_version"], 1)
        self.assertTrue(profile.data["identity_key"])
        self.assertNotIn("email", profile.data)
        self.assertEqual(profile.data["display_name"], "")
        self.assertEqual(session.identity.uuid, profile.uuid)
        # identity_key is generated once and stays stable across repeated
        # access, not regenerated on every lazy-create check.
        self.assertEqual(session.identity.data["identity_key"], profile.data["identity_key"])

    def test_set_identity_updates_display_fields(self):
        session = Session("si-a")

        result = session.set_identity("Alice", "https://example.test/a.png")

        self.assertEqual(result.status, "ok")
        profile = session.identity
        self.assertEqual(profile.data["display_name"], "Alice")
        self.assertEqual(profile.data["picture"], "https://example.test/a.png")

    def test_core_identity_excludes_contact_information(self):
        session = Session("si-a")
        identity_key = session.identity.data["identity_key"]

        session.set_identity("Alice")

        profile = session.identity
        self.assertNotIn("email", profile.data)
        self.assertEqual(profile.data["identity_key"], identity_key)

    def test_core_identity_rejects_contact_fields(self):
        session = Session("si-a")
        profile = session.identity
        data = dict(profile.data)
        data["email"] = "alice@example.test"

        result = session.modify(profile.uuid, data, profile.weights)

        self.assertEqual(result.status, "error")
        self.assertIn("email", result.reason)
        self.assertNotIn("email", session.identity.data)

    def test_find_peer_identity_searches_cached_peer_perspectives(self):
        session = Session("si-a")
        peer_profile = ProtocolNode({
            "type": "shared_user_profile",
            "name": "public_profile",
            "profile_schema_version": 1,
            "identity_key": "key-b",
            "display_name": "Bob",
            "picture": "",
        })
        peer_profile.refresh_hashes()
        session.peer_perspectives["si-b"] = peer_profile

        found = session.find_peer_identity("key-b")

        self.assertIsNotNone(found)
        self.assertEqual(found.data["display_name"], "Bob")
        self.assertIsNone(session.find_peer_identity("key-c"))

    def test_find_peer_identity_survives_address_change(self):
        # The whole point of keying identity lookup by identity_key instead
        # of address: the same identity, cached under two different
        # peer_perspectives dict keys (simulating Bob reconnecting from a
        # new address), must still resolve to one identity.
        session = Session("si-a")
        bob_v1 = ProtocolNode({
            "type": "shared_user_profile", "name": "public_profile",
            "profile_schema_version": 1, "identity_key": "key-bob",
            "display_name": "Bob", "picture": "",
        })
        bob_v1.refresh_hashes()
        session.peer_perspectives["old-address"] = bob_v1
        bob_v2 = ProtocolNode({
            "type": "shared_user_profile", "name": "public_profile",
            "profile_schema_version": 1, "identity_key": "key-bob",
            "display_name": "Bob", "picture": "",
        })
        bob_v2.refresh_hashes()
        session.peer_perspectives["new-address"] = bob_v2

        found = session.find_peer_identity("key-bob")

        self.assertIsNotNone(found)
        self.assertEqual(found.data["display_name"], "Bob")

    def test_set_peer_identity_key_records_and_overwrites(self):
        session = Session("si-a")

        session.set_peer_identity_key("relay:B", "key-bob")
        self.assertEqual(session.peer_identity_key["relay:B"], "key-bob")

        session.set_peer_identity_key("relay:B", "key-other")
        self.assertEqual(session.peer_identity_key["relay:B"], "key-other")

        # Blank inputs are ignored, never stored.
        session.set_peer_identity_key("", "key-x")
        session.set_peer_identity_key("addr-x", "")
        self.assertNotIn("", session.peer_identity_key)
        self.assertNotIn("addr-x", session.peer_identity_key)

    def test_addresses_for_identity_returns_all_matches_sorted(self):
        session = Session("si-a")
        session.set_peer_identity_key("relay:B", "key-bob")
        session.set_peer_identity_key("http://addr-b", "key-bob")
        session.set_peer_identity_key("http://addr-c", "key-carol")

        self.assertEqual(
            session.addresses_for_identity("key-bob"),
            ["http://addr-b", "relay:B"],
        )
        self.assertEqual(session.addresses_for_identity("key-nobody"), [])

    def test_apply_peer_subtree_records_identity_key_for_profile_roots(self):
        session = Session("si-a")
        bob = ProtocolNode({
            "type": "shared_user_profile", "name": "public_profile",
            "profile_schema_version": 1, "identity_key": "key-bob",
            "display_name": "Bob", "picture": "",
        })
        bob.refresh_hashes()

        session.apply_peer_subtree("relay:B", bob, None)

        self.assertEqual(session.peer_identity_key.get("relay:B"), "key-bob")

    def test_apply_peer_subtree_ignores_non_identity_roots(self):
        session = Session("si-a")
        board = ProtocolNode({"type": "kanban_board", "name": "Board"})
        board.refresh_hashes()

        session.apply_peer_subtree("http://addr-b", board, None)

        self.assertEqual(session.peer_identity_key, {})

    def test_remove_peer_keeps_the_identity_registry_entry(self):
        # Knowledge, not registration: tearing down a peer's registration
        # must not erase the fact that its address belongs to an identity -
        # relay's redundancy check reads exactly this entry on every later
        # poll to keep the address suppressed.
        session = Session("si-a")
        session.add_peer("relay:B", "topic-1")
        session.set_peer_identity_key("relay:B", "key-bob")

        session.remove_peer("relay:B")

        self.assertNotIn("relay:B", session.peer_topic_sets)
        self.assertEqual(session.peer_identity_key.get("relay:B"), "key-bob")

    def test_peer_identity_scoped_to_one_address(self):
        session = Session("si-a")
        bob = ProtocolNode({
            "type": "shared_user_profile", "name": "public_profile",
            "profile_schema_version": 1, "identity_key": "key-bob",
            "display_name": "Bob", "picture": "",
        })
        bob.refresh_hashes()
        session.peer_perspectives["addr-bob"] = bob

        self.assertEqual(session.peer_identity("addr-bob").data["display_name"], "Bob")
        self.assertIsNone(session.peer_identity("addr-nobody"))

    def test_apply_peer_identity_snapshot_caches_unconditionally(self):
        session = Session("si-a")
        bob_session = Session("si-b")
        bob_session.set_identity("Bob")
        payload = bob_session.identity.to_dict()

        session.apply_peer_identity_snapshot("si-b", payload)

        cached = session.find_peer_identity(payload["data"]["identity_key"])
        self.assertIsNotNone(cached)
        self.assertEqual(cached.data["display_name"], "Bob")
        self.assertEqual(
            session.peer_identity_key.get("si-b"),
            payload["data"]["identity_key"],
        )

    def test_apply_peer_identity_snapshot_ignores_unrecognized_version(self):
        session = Session("si-a")
        bob_session = Session("si-b")
        bob_session.set_identity("Bob")
        payload = bob_session.identity.to_dict()
        payload["data"]["profile_schema_version"] = 99

        session.apply_peer_identity_snapshot("si-b", payload)

        self.assertEqual(session.peer_perspectives, {})

    def test_apply_peer_identity_snapshot_ignores_malformed_input(self):
        session = Session("si-a")

        session.apply_peer_identity_snapshot("si-b", {"not": "a real node"})
        session.apply_peer_identity_snapshot("si-c", "not even a dict")

        self.assertEqual(session.peer_perspectives, {})

    def test_is_identity_node(self):
        session = Session("si-a")

        self.assertTrue(session.is_identity_node(session.identity))
        other = session.create_child(
            session.protocol.root.uuid, {"type": "folder", "name": "x"}, {},
        ).value
        self.assertFalse(session.is_identity_node(other))
        self.assertFalse(session.is_identity_node(None))

    # reconcile_peer_changes / accept_peer_node / peer_discusses_node -
    # generic peer-content reconciliation, generalized out of
    # kanban_logic.py's adopt_incoming_changes. Deliberately exercised here
    # with non-kanban node types ("note"/"note_item"/"leaf") to prove the
    # mechanism carries no kanban-specific assumptions.

    def test_peer_discusses_node(self):
        session = Session("si-a")
        topic = session.create_child(
            session.protocol.root.uuid, {"type": "note", "name": "t"}, {},
        ).value
        child = session.create_child(topic.uuid, {"type": "note_item"}, {}).value
        session.peer_topic_sets["si-b"] = {topic.uuid}

        self.assertTrue(session.peer_discusses_node("si-b", child.uuid))
        self.assertFalse(session.peer_discusses_node("si-b", "no-such-uuid"))
        self.assertFalse(session.peer_discusses_node("si-c", child.uuid))

    def test_accept_peer_node_adopts_and_deletes_on_absence(self):
        peer = Session("si-b")
        topic = peer.create_child(
            peer.protocol.root.uuid, {"type": "note", "name": "t"}, {},
        ).value
        local = Session("si-a")
        local.adopt_subtree(
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )
        child = peer.create_child(topic.uuid, {"type": "note_item", "text": "x"}, {}).value
        local.apply_peer_subtree(
            "si-b",
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        result = local.accept_peer_node("si-b", child.uuid)
        self.assertEqual(result.status, "ok")
        self.assertIn(child.uuid, local.protocol.index)
        self.assertEqual(local.protocol.index[child.uuid].data["text"], "x")

        result = local.accept_peer_node("si-b", child.uuid, adopt_absence=True)
        self.assertEqual(result.status, "ok")
        self.assertNotIn(child.uuid, local.protocol.index)

    def test_rollback_restores_my_exact_revision_still_held_by_peer(self):
        local = Session("si-a")
        local_identity = local.identity.data["identity_key"]
        topic = local.create_child(
            local.protocol.root.uuid, {"type": "note", "name": "t"}, {},
        ).value
        child = local.create_child(
            topic.uuid, {"type": "note_item", "text": "original"}, {},
        ).value
        peer_copy = ProtocolNode.from_dict(
            local.protocol.index[topic.uuid].to_dict(),
        )
        local.apply_peer_subtree("si-b", peer_copy, local.protocol.root.uuid)
        peer_child = local.get_cached_peer_subtree("si-b", child.uuid)

        local.modify(child.uuid, {"type": "note_item", "text": "first"}, {})
        local.modify(child.uuid, {"type": "note_item", "text": "second"}, {})
        changed = local.protocol.index[child.uuid]
        self.assertEqual(changed.revision_origin, local_identity)
        self.assertEqual(changed.base_hash, peer_child.base_hash)

        result = local.rollback_peer_node("si-b", child.uuid)

        self.assertEqual(result.status, "ok")
        rolled_back = local.protocol.index[child.uuid]
        self.assertEqual(rolled_back.data["text"], "original")
        self.assertEqual(rolled_back.state_hash, peer_child.state_hash)
        self.assertEqual(rolled_back.base_hash, peer_child.base_hash)
        self.assertEqual(rolled_back.revision_origin, local_identity)

    def test_rollback_rejects_another_origins_revision(self):
        peer = Session("si-b")
        peer.identity
        topic = peer.create_child(
            peer.protocol.root.uuid, {"type": "note", "name": "t"}, {},
        ).value
        local = Session("si-a")
        local.identity
        local.adopt_subtree(
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )
        local.apply_peer_subtree(
            "si-b", ProtocolNode.from_dict(topic.to_dict()), local.protocol.root.uuid,
        )

        result = local.rollback_peer_node("si-b", topic.uuid)

        self.assertEqual(result.status, "error")
        self.assertIn("not mine", result.reason)

    def test_reconcile_peer_changes_no_cached_subtree_is_a_no_op(self):
        session = Session("si-a")
        self.assertFalse(session.reconcile_peer_changes("si-b", "no-such-topic"))

    def test_reconcile_peer_changes_adopts_eligible_change_by_default(self):
        peer = Session("si-b")
        topic = peer.create_child(
            peer.protocol.root.uuid, {"type": "note", "name": "t"}, {},
        ).value
        child = peer.create_child(topic.uuid, {"type": "note_item", "text": "original"}, {}).value
        local = Session("si-a")
        local.adopt_subtree(
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        peer.modify(child.uuid, {"type": "note_item", "text": "updated"}, {})
        local.apply_peer_subtree(
            "si-b",
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        changed = local.reconcile_peer_changes("si-b", topic.uuid)

        self.assertTrue(changed)
        self.assertEqual(local.protocol.index[child.uuid].data["text"], "updated")

    def test_reconcile_peer_changes_respects_node_is_eligible(self):
        peer = Session("si-b")
        topic = peer.create_child(
            peer.protocol.root.uuid, {"type": "note", "name": "t"}, {},
        ).value
        child = peer.create_child(topic.uuid, {"type": "note_item", "text": "original"}, {}).value
        local = Session("si-a")
        local.adopt_subtree(
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        peer.modify(child.uuid, {"type": "note_item", "text": "updated"}, {})
        local.apply_peer_subtree(
            "si-b",
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        changed = local.reconcile_peer_changes(
            "si-b", topic.uuid, node_is_eligible=lambda node, event_type: False,
        )

        self.assertFalse(changed)
        self.assertEqual(local.protocol.index[child.uuid].data["text"], "original")

    def test_reconcile_adopts_an_existing_nodes_own_fields_only(self):
        # Adopting an existing node updates its own fields only (never grafts
        # its whole subtree) - now the default in accept_peer_node, so no
        # adopt-mode hint is passed. The deeper "doesn't smuggle a
        # simultaneously-added child" property is covered end-to-end by the
        # kanban auto-adopt tests in test_kanban_new_logic.py.
        peer = Session("si-b")
        topic = peer.create_child(
            peer.protocol.root.uuid, {"type": "note", "name": "t"}, {},
        ).value
        folder = peer.create_child(topic.uuid, {"type": "folder", "text": "orig"}, {}).value
        local = Session("si-a")
        local.adopt_subtree(
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        peer.modify(folder.uuid, {"type": "folder", "text": "new"}, {})
        local.apply_peer_subtree(
            "si-b",
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        changed = local.reconcile_peer_changes("si-b", topic.uuid)

        self.assertTrue(changed)
        self.assertEqual(local.protocol.index[folder.uuid].data["text"], "new")

    def test_shallow_peer_adoption_preserves_the_remote_revision_origin(self):
        peer = Session("si-b")
        peer_identity_key = peer.identity.data["identity_key"]
        topic = peer.create_child(
            peer.protocol.root.uuid, {"type": "note", "name": "t"}, {},
        ).value
        folder = peer.create_child(
            topic.uuid, {"type": "folder", "text": "orig"}, {},
        ).value
        local = Session("si-a")
        local.identity
        local.adopt_subtree(
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        peer.modify(folder.uuid, {"type": "folder", "text": "new"}, {})
        local.apply_peer_subtree(
            "si-b",
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )
        local.reconcile_peer_changes("si-b", topic.uuid)

        adopted = local.protocol.index[folder.uuid]
        self.assertEqual(adopted.data["text"], "new")
        self.assertEqual(
            adopted.revision_origin, peer_identity_key,
        )

    def test_reconcile_peer_changes_filters_ineligible_new_nodes(self):
        # A brand-new peer node (never seen locally) is always classified
        # "local_missing_node" regardless of hop count - independent
        # coverage of node_is_eligible for that event type specifically,
        # since test_reconcile_peer_changes_respects_node_is_eligible above
        # only covers "peer_made_changes".
        peer = Session("si-b")
        topic = peer.create_child(
            peer.protocol.root.uuid, {"type": "note", "name": "t"}, {},
        ).value
        local = Session("si-a")
        local.adopt_subtree(
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        leaf = peer.create_child(topic.uuid, {"type": "leaf", "text": "new"}, {}).value
        local.apply_peer_subtree(
            "si-b",
            ProtocolNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        # With the node_hash/subtree_hash split the topic root's own event is
        # "in_agreement" (its own fields didn't change; only its subtree hash
        # cascaded from gaining the leaf), and adoption of any existing node is
        # shallow by default - so neither can drag the filtered leaf in. The
        # leaf itself is local_missing_node and blocked by node_is_eligible.
        local.reconcile_peer_changes(
            "si-b", topic.uuid,
            node_is_eligible=lambda node, event_type: node.data.get("type") != "leaf",
        )

        self.assertNotIn(leaf.uuid, local.protocol.index)

if __name__ == "__main__":
    unittest.main()
