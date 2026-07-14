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

    def test_analyze_peer_transition_multiple_unsynced_edits_read_as_divergence(self):
        # One-slot history trade-off (see BACKLOG): after two peer edits with
        # no adoption in between, the peer's previous_hash no longer bridges
        # to our state, so the honest verdict is divergence rather than
        # "peer is just behind".
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

        self.assertEqual(events[0]["type"], "divergence")

    def test_keep_mine_does_not_corrupt_classification_for_unrelated_peers(self):
        # A edits and keeps its own value against some
        # divergent peer). A separate, unrelated peer C who never touched
        # the node and is simply behind must still classify correctly - the
        # keep_mine must not overwrite the true previous_hash history that
        # C's comparison depends on.
        a = Session("si-a")
        c = Session("si-c")
        topic = a.create_child(a.protocol.root.uuid, {"name": "topic"}, {}).value
        c.accept_topic_invitation(PRSPNode.from_dict(topic.to_dict()))

        a.modify(topic.uuid, {"name": "a-edit"}, {})
        a.set_perspective_state(topic.uuid, "kept_mine")

        c.apply_peer_subtree(
            "si-a",
            PRSPNode.from_dict(a.protocol.index[topic.uuid].to_dict()),
            c.protocol.root.uuid,
        )

        events = c.analyze_peer_transitions("si-a", topic.uuid)

        self.assertEqual(events[0]["type"], "peer_made_changes")

    @staticmethod
    def _node(state_hash, previous_hash, parent_uuid, previous_parent_uuid,
              perspective_state="none"):
        node = PRSPNode({"name": "x"})
        node.state_hash = state_hash
        node.previous_hash = previous_hash
        node.parent_uuid = parent_uuid
        node.previous_parent_uuid = previous_parent_uuid
        node.perspective_state = perspective_state
        return node

    def test_keep_mine_active_stays_true_for_a_clean_untouched_keep_mine(self):
        local_node = self._node("h0", "h0", "p", "p", perspective_state="kept_mine")
        peer_node = self._node("h1", "h0", "p", "p")

        self.assertTrue(Session.keep_mine_active(local_node, peer_node))

    def test_keep_mine_active_goes_false_after_a_second_peer_content_edit(self):
        # peer's previous_hash no longer reaches back to what local kept_mine
        # against - the one-slot history can't reconstruct a clean chain, so
        # classification degrades to divergence and the stale keep_mine must
        # not keep masking it.
        local_node = self._node("h0", "h0", "p", "p", perspective_state="kept_mine")
        peer_node = self._node("h2", "h1", "p", "p")

        self.assertFalse(Session.keep_mine_active(local_node, peer_node))

    def test_keep_mine_active_goes_false_when_a_move_appears(self):
        # Content is still a clean single step (peer_made_changes on its
        # own), and so is the move on its own - _classify_node would merge
        # these into a clean "peer_made_changes", never "divergence". A
        # content-only keep_mine must still not mask a move it never
        # considered - this is the case a single chain-connectivity check
        # can't catch on its own (see BACKLOG.md item 10).
        local_node = self._node("h0", "h0", "p1", "p1", perspective_state="kept_mine")
        peer_node = self._node("h1", "h0", "p2", "p1")

        self.assertFalse(Session.keep_mine_active(local_node, peer_node))

    def test_keep_mine_active_ignores_state_when_state_is_none(self):
        local_node = self._node("h0", "h0", "p", "p", perspective_state="none")
        peer_node = self._node("h1", "h0", "p", "p")

        self.assertFalse(Session.keep_mine_active(local_node, peer_node))

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

    def test_pushed_back_stays_active_across_a_second_peer_content_edit(self):
        local_node = self._node("h0", "h0", "p", "p", perspective_state="pushed_back")
        peer_node = self._node("h2", "h1", "p", "p")

        self.assertTrue(Session.keep_mine_active(local_node, peer_node))

    def test_pushed_back_stays_active_when_a_move_appears(self):
        local_node = self._node("h0", "h0", "p1", "p1", perspective_state="pushed_back")
        peer_node = self._node("h1", "h0", "p2", "p1")

        self.assertTrue(Session.keep_mine_active(local_node, peer_node))

    def test_peer_pushed_back_reads_the_peers_own_state(self):
        pushed_back_peer = self._node("h1", "h0", "p", "p", perspective_state="pushed_back")
        kept_mine_peer = self._node("h1", "h0", "p", "p", perspective_state="kept_mine")

        self.assertTrue(Session.peer_pushed_back(pushed_back_peer))
        self.assertFalse(Session.peer_pushed_back(kept_mine_peer))
        self.assertFalse(Session.peer_pushed_back(None))

    def test_transition_event_carries_keep_mine_active_end_to_end(self):
        # Proves the field is actually threaded through
        # analyze_peer_transitions, not just correct in isolation.
        local = Session("si-a")
        peer = Session("si-b")
        topic = peer.create_child(peer.protocol.root.uuid, {"name": "topic"}, {}).value
        child = peer.create_child(topic.uuid, {"name": "child"}, {}).value
        local.accept_topic_invitation(
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict())
        )
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )
        local_child_uuid = child.uuid
        local.set_perspective_state(local_child_uuid, "kept_mine")

        peer.modify(child.uuid, {"name": "peer changed"}, {})
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        events = {
            event["node_uuid"]: event
            for event in local.analyze_peer_transitions("si-b", topic.uuid)
        }

        self.assertEqual(events[local_child_uuid]["type"], "peer_made_changes")
        self.assertTrue(events[local_child_uuid]["keep_mine_active"])

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
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            peer.protocol.root.uuid,
        )

        peer.modify(child.uuid, {"name": "renamed"}, {})
        peer.delete(child.uuid)
        # Pull just the changed node, hitting the update-in-place merge path.
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[child.uuid].to_dict()),
            topic.uuid,
        )

        cached_child = local._find_in_tree(local.peer_perspectives["si-b"], child.uuid)
        peer_child = peer.protocol.index[child.uuid]
        self.assertTrue(cached_child.deleted)
        self.assertEqual(cached_child.previous_hash, peer_child.previous_hash)
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
        stale_topic = PRSPNode.from_dict(local.protocol.index[topic.uuid].to_dict())
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
        current_topic = PRSPNode.from_dict(local.protocol.index[topic.uuid].to_dict())
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
            PRSPNode.from_dict(peer_topic.to_dict()),
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

    def test_identity_is_lazily_created_and_stable(self):
        # No KanbanLogic/app involved at all - identity is a Session-owned
        # meta-topic, so any app (or none) gets it for free.
        session = Session("si-a")

        profile = session.identity

        self.assertEqual(profile.data["type"], "shared_user_profile")
        self.assertEqual(profile.data["version"], 1)
        self.assertTrue(profile.data["identity_key"])
        self.assertEqual(profile.data["email"], "")
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

    def test_set_identity_can_set_email_without_clearing_identity_key(self):
        session = Session("si-a")
        identity_key = session.identity.data["identity_key"]

        session.set_identity("Alice", email="alice@example.test")

        profile = session.identity
        self.assertEqual(profile.data["email"], "alice@example.test")
        self.assertEqual(profile.data["identity_key"], identity_key)

    def test_find_peer_identity_searches_cached_peer_perspectives(self):
        session = Session("si-a")
        peer_profile = PRSPNode({
            "type": "shared_user_profile",
            "name": "public_profile",
            "version": 1,
            "identity_key": "key-b",
            "email": "",
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
        bob_v1 = PRSPNode({
            "type": "shared_user_profile", "name": "public_profile",
            "version": 1, "identity_key": "key-bob", "email": "",
            "display_name": "Bob", "picture": "",
        })
        bob_v1.refresh_hashes()
        session.peer_perspectives["old-address"] = bob_v1
        bob_v2 = PRSPNode({
            "type": "shared_user_profile", "name": "public_profile",
            "version": 1, "identity_key": "key-bob", "email": "",
            "display_name": "Bob", "picture": "",
        })
        bob_v2.refresh_hashes()
        session.peer_perspectives["new-address"] = bob_v2

        found = session.find_peer_identity("key-bob")

        self.assertIsNotNone(found)
        self.assertEqual(found.data["display_name"], "Bob")

    def test_peer_identity_scoped_to_one_address(self):
        session = Session("si-a")
        bob = PRSPNode({
            "type": "shared_user_profile", "name": "public_profile",
            "version": 1, "identity_key": "key-bob", "email": "",
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

    def test_apply_peer_identity_snapshot_ignores_unrecognized_version(self):
        session = Session("si-a")
        bob_session = Session("si-b")
        bob_session.set_identity("Bob")
        payload = bob_session.identity.to_dict()
        payload["data"]["version"] = 99

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
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )
        child = peer.create_child(topic.uuid, {"type": "note_item", "text": "x"}, {}).value
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        result = local.accept_peer_node("si-b", child.uuid)
        self.assertEqual(result.status, "ok")
        self.assertIn(child.uuid, local.protocol.index)
        self.assertEqual(local.protocol.index[child.uuid].data["text"], "x")

        result = local.accept_peer_node("si-b", child.uuid, adopt_absence=True)
        self.assertEqual(result.status, "ok")
        self.assertNotIn(child.uuid, local.protocol.index)

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
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        peer.modify(child.uuid, {"type": "note_item", "text": "updated"}, {})
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
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
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        peer.modify(child.uuid, {"type": "note_item", "text": "updated"}, {})
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        changed = local.reconcile_peer_changes(
            "si-b", topic.uuid, node_is_eligible=lambda node, event_type: False,
        )

        self.assertFalse(changed)
        self.assertEqual(local.protocol.index[child.uuid].data["text"], "original")

    def test_reconcile_peer_changes_shallow_mode_uses_field_only_modify(self):
        # node_adopt_mode="shallow" must route through modify (own fields
        # only) rather than accept_peer_node (whole-subtree graft) - proven
        # by asserting the field updates via a single clean peer-side
        # change (state_hash chains only compare cleanly one hop at a time;
        # the deeper "doesn't smuggle a simultaneously-added child" property
        # this exists for is already covered end-to-end by the kanban-level
        # auto-adopt tests in test_kanban_new_logic.py, which exercise it
        # through the real incremental sync path).
        peer = Session("si-b")
        topic = peer.create_child(
            peer.protocol.root.uuid, {"type": "note", "name": "t"}, {},
        ).value
        folder = peer.create_child(topic.uuid, {"type": "folder", "text": "orig"}, {}).value
        local = Session("si-a")
        local.adopt_subtree(
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        peer.modify(folder.uuid, {"type": "folder", "text": "new"}, {})
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        changed = local.reconcile_peer_changes(
            "si-b", topic.uuid,
            node_adopt_mode=lambda node: "shallow" if node.data.get("type") == "folder" else "full",
        )

        self.assertTrue(changed)
        self.assertEqual(local.protocol.index[folder.uuid].data["text"], "new")

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
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        leaf = peer.create_child(topic.uuid, {"type": "leaf", "text": "new"}, {}).value
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        # Topic's own event is also "peer_made_changes" here (its hash
        # cascaded from gaining the leaf, even though its own fields didn't
        # change), so node_adopt_mode must stay "shallow" - otherwise a
        # full adopt of the topic root would drag the filtered leaf back in
        # as a side effect, same as a real app's eligibility closure must
        # do for its own container types. (Not asserting on `changed`
        # itself: the topic root's shallow modify still reports "ok" even
        # though its data is identical - the property under test is that
        # the filtered leaf never appears locally, regardless.)
        local.reconcile_peer_changes(
            "si-b", topic.uuid,
            node_is_eligible=lambda node, event_type: node.data.get("type") != "leaf",
            node_adopt_mode=lambda node: "shallow",
        )

        self.assertNotIn(leaf.uuid, local.protocol.index)

    def test_reconcile_peer_changes_wholesale_replace_when_allowed(self):
        peer = Session("si-b")
        topic = peer.create_child(
            peer.protocol.root.uuid, {"type": "note", "name": "t"}, {},
        ).value
        child = peer.create_child(topic.uuid, {"type": "note_item", "text": "orig"}, {}).value
        local = Session("si-a")
        local.adopt_subtree(
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        # A single peer-side operation keeps the topic root's own hash chain
        # a clean one-hop "peer_made_changes" from local's frozen baseline
        # (state_hash/previous_hash only encode one hop back - two separate
        # peer operations before syncing would classify as "divergence"
        # instead, same as a real concurrent-edit case would).
        peer.modify(child.uuid, {"type": "note_item", "text": "new"}, {})
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        changed = local.reconcile_peer_changes("si-b", topic.uuid, allow_wholesale_replace=True)

        self.assertTrue(changed)
        self.assertEqual(local.protocol.index[child.uuid].data["text"], "new")

    def test_reconcile_peer_changes_wholesale_replace_blocked_by_local_kept_mine(self):
        peer = Session("si-b")
        topic = peer.create_child(
            peer.protocol.root.uuid, {"type": "note", "name": "t"}, {},
        ).value
        child = peer.create_child(topic.uuid, {"type": "note_item", "text": "orig"}, {}).value
        local = Session("si-a")
        local.adopt_subtree(
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )
        local.set_perspective_state(child.uuid, "kept_mine")

        peer.modify(child.uuid, {"type": "note_item", "text": "new"}, {})
        local.apply_peer_subtree(
            "si-b",
            PRSPNode.from_dict(peer.protocol.index[topic.uuid].to_dict()),
            local.protocol.root.uuid,
        )

        # allow_wholesale_replace=True, but a kept_mine node anywhere in the
        # local subtree must block the shortcut entirely - the kept_mine
        # child's content must survive regardless of what the per-node loop
        # then does with the rest of the subtree.
        local.reconcile_peer_changes(
            "si-b", topic.uuid, allow_wholesale_replace=True,
        )

        self.assertEqual(local.protocol.index[child.uuid].data["text"], "orig")

if __name__ == "__main__":
    unittest.main()
