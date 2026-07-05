import unittest

from protocol import AtomicOperation, PRSPNode, ProtocolState


class ProtocolTests(unittest.TestCase):
    def test_hashes_ignore_child_order(self):
        first = PRSPNode({"name": "root"})
        a = PRSPNode({"name": "a"}, parent_uuid=first.uuid)
        b = PRSPNode({"name": "b"}, parent_uuid=first.uuid)
        first.children = [a, b]
        first.refresh_hashes_deep()

        second = PRSPNode({"name": "root"})
        a2 = PRSPNode.from_dict(a.to_dict())
        b2 = PRSPNode.from_dict(b.to_dict())
        a2.parent_uuid = second.uuid
        b2.parent_uuid = second.uuid
        second.children = [b2, a2]
        second.refresh_hashes_deep()

        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.state_hash, second.state_hash)

    def test_from_dict_rejects_mismatched_hash(self):
        node = PRSPNode({"name": "node"})
        payload = node.to_dict()
        payload["data"]["name"] = "tampered"

        with self.assertRaises(ValueError):
            PRSPNode.from_dict(payload)

    def test_new_node_starts_settled(self):
        node = PRSPNode({"name": "node"})

        self.assertEqual(node.previous_hash, node.state_hash)
        self.assertEqual(node.previous_parent_uuid, node.parent_uuid)

    def test_modify_tracks_previous_hash(self):
        state = ProtocolState("si-a")
        child = state.create_child(state.root.uuid, {"name": "child"}, {}).value
        old_hash = child.state_hash

        state.modify(child.uuid, {"name": "renamed"}, {})

        self.assertEqual(child.previous_hash, old_hash)
        self.assertNotEqual(child.state_hash, old_hash)

    def test_move_child_tracks_previous_parent_uuid(self):
        state = ProtocolState("si-a")
        first = state.create_child(state.root.uuid, {"name": "first"}, {}).value
        second = state.create_child(state.root.uuid, {"name": "second"}, {}).value
        child = state.create_child(first.uuid, {"name": "child"}, {}).value

        state.move_child(child.uuid, second.uuid)

        self.assertEqual(child.previous_parent_uuid, first.uuid)
        self.assertEqual(child.parent_uuid, second.uuid)

    def test_reaffirm_sets_reaffirmed_markers_without_touching_true_history(self):
        state = ProtocolState("si-a")
        parent = state.create_child(state.root.uuid, {"name": "parent"}, {}).value
        child = state.create_child(parent.uuid, {"name": "child"}, {}).value
        state.modify(child.uuid, {"name": "renamed"}, {})
        state_hash_before = child.state_hash
        content_hash_before = child.content_hash
        previous_hash_before = child.previous_hash
        previous_parent_uuid_before = child.previous_parent_uuid

        result = state.reaffirm(child.uuid)

        self.assertTrue(result.ok)
        self.assertTrue(child.is_reaffirmed())
        self.assertEqual(child.reaffirmed_hash, child.state_hash)
        self.assertEqual(child.reaffirmed_parent_uuid, child.parent_uuid)
        # True edit history must stay untouched - it's still needed to
        # classify correctly against peers who know nothing about this
        # reaffirm.
        self.assertEqual(child.previous_hash, previous_hash_before)
        self.assertEqual(child.previous_parent_uuid, previous_parent_uuid_before)
        self.assertEqual(child.state_hash, state_hash_before)
        self.assertEqual(child.content_hash, content_hash_before)

    def test_reaffirm_toggles_off_on_second_call(self):
        state = ProtocolState("si-a")
        child = state.create_child(state.root.uuid, {"name": "child"}, {}).value
        state.modify(child.uuid, {"name": "renamed"}, {})

        self.assertTrue(state.reaffirm(child.uuid).ok)
        self.assertTrue(child.is_reaffirmed())

        self.assertTrue(state.reaffirm(child.uuid).ok)
        self.assertFalse(child.is_reaffirmed())
        self.assertIsNone(child.reaffirmed_hash)
        self.assertIsNone(child.reaffirmed_parent_uuid)

    def test_delete_cascades_flag_and_keeps_descendants_indexed(self):
        state = ProtocolState("si-a")
        column = state.create_child(state.root.uuid, {"name": "column"}, {}).value
        card = state.create_child(column.uuid, {"name": "card"}, {}).value

        self.assertTrue(state.delete(column.uuid).ok)

        self.assertIn(column.uuid, state.index)
        self.assertIn(card.uuid, state.index)
        self.assertTrue(state.index[column.uuid].deleted)
        self.assertTrue(state.index[card.uuid].deleted)
        self.assertEqual(state.root.live_children(), [])

    def test_deleted_flag_changes_hash_and_cascades_to_parent(self):
        state = ProtocolState("si-a")
        column = state.create_child(state.root.uuid, {"name": "column"}, {}).value
        root_hash_before = state.root.state_hash

        state.delete(column.uuid)

        self.assertNotEqual(state.root.state_hash, root_hash_before)

    def test_atomic_create_modify_delete(self):
        state = ProtocolState("si-a")
        child = state.create_child(state.root.uuid, {"name": "child"}, {}).value

        self.assertIn(child.uuid, state.index)
        self.assertTrue(state.modify(child.uuid, {"name": "renamed"}, {}).ok)
        self.assertEqual(state.index[child.uuid].data["name"], "renamed")
        self.assertTrue(state.delete(child.uuid).ok)
        self.assertIn(child.uuid, state.index)
        self.assertTrue(state.index[child.uuid].deleted)
        self.assertNotIn(child.uuid, [c.uuid for c in state.root.live_children()])

    def test_copy_uses_fresh_uuids(self):
        state = ProtocolState("si-a")
        parent = state.create_child(state.root.uuid, {"name": "parent"}, {}).value
        child = state.create_child(parent.uuid, {"name": "child"}, {}).value

        clone = state.copy(parent.uuid, state.root.uuid).value

        self.assertNotEqual(clone.uuid, parent.uuid)
        self.assertEqual(clone.data, parent.data)
        self.assertEqual(clone.children[0].data, child.data)
        self.assertNotEqual(clone.children[0].uuid, child.uuid)

    def test_move_prevents_cycles_and_duplicate_locations(self):
        state = ProtocolState("si-a")
        left = state.create_child(state.root.uuid, {"name": "left"}, {}).value
        right = state.create_child(state.root.uuid, {"name": "right"}, {}).value
        moving = state.create_child(left.uuid, {"name": "moving"}, {}).value

        self.assertFalse(state.move(left.uuid, moving.uuid).ok)
        self.assertTrue(state.move(moving.uuid, right.uuid).ok)
        self.assertNotIn(moving.uuid, [child.uuid for child in left.children])
        self.assertIn(moving.uuid, [child.uuid for child in right.children])
        self.assertIs(state.index[moving.uuid], moving)

    def test_proposal_freezes_subtree_and_integrates(self):
        state = ProtocolState("si-a")
        topic = state.create_child(state.root.uuid, {"name": "topic"}, {}).value
        proposal = state.propose(topic.uuid, [
            AtomicOperation.create_child(topic.uuid, {"name": "proposed"}, {})
        ]).value

        self.assertIsNone(state.create_child(topic.uuid, {"name": "blocked"}, {}).value)
        self.assertTrue(state.integrate_proposal(proposal.uuid).ok)
        self.assertIn("proposed", [child.data["name"] for child in topic.children])

    def test_accepted_proposal_locks_until_finalized(self):
        proposer = ProtocolState("si-a")
        accepter = ProtocolState("si-b")
        topic = proposer.create_child(proposer.root.uuid, {"name": "topic"}, {}).value
        accepter.attach_topic(PRSPNode.from_dict(topic.to_dict()))
        proposal = proposer.propose(topic.uuid, [
            AtomicOperation.create_child(topic.uuid, {"name": "accepted"}, {})
        ]).value

        self.assertTrue(accepter.accept_proposal(proposal).ok)
        self.assertIsNone(accepter.create_child(topic.uuid, {"name": "blocked"}, {}).value)

        self.assertTrue(proposer.integrate_proposal(proposal.uuid).ok)
        final = proposer.find_proposal(proposal.uuid)
        self.assertTrue(accepter.reconcile_final_proposal(final).ok)
        self.assertIsNotNone(accepter.create_child(topic.uuid, {"name": "allowed"}, {}).value)

    def test_objection_blocks_integration(self):
        proposer = ProtocolState("si-a")
        objector = ProtocolState("si-b")
        topic = proposer.create_child(proposer.root.uuid, {"name": "topic"}, {}).value
        objector.attach_topic(PRSPNode.from_dict(topic.to_dict()))
        proposal = proposer.propose(topic.uuid, [
            AtomicOperation.create_child(topic.uuid, {"name": "blocked"}, {})
        ]).value
        self.assertTrue(objector.object_to_proposal(proposal).ok)
        proposer.observe_proposal(objector.find_proposal(proposal.uuid))

        self.assertFalse(proposer.integrate_proposal(proposal.uuid).ok)

    def test_final_marker_gc_after_acknowledgement(self):
        proposer = ProtocolState("si-a")
        accepter = ProtocolState("si-b")
        topic = proposer.create_child(proposer.root.uuid, {"name": "topic"}, {}).value
        accepter.attach_topic(PRSPNode.from_dict(topic.to_dict()))
        proposal = proposer.propose(topic.uuid, [
            AtomicOperation.create_child(topic.uuid, {"name": "accepted"}, {})
        ]).value
        accepter.accept_proposal(proposal)
        proposer.observe_proposal(accepter.find_proposal(proposal.uuid))

        self.assertTrue(proposer.integrate_proposal(proposal.uuid).ok)
        self.assertIsNotNone(proposer.find_proposal(proposal.uuid))
        accepter.reconcile_final_proposal(proposer.find_proposal(proposal.uuid))
        proposer.acknowledge_final_proposal_cleanup(proposal.uuid, accepter.author)

        self.assertIsNone(proposer.find_proposal(proposal.uuid))

    def test_propose_records_base_hash_only_for_modify_targets(self):
        state = ProtocolState("si-a")
        column = state.create_child(state.root.uuid, {"name": "To Do"}, {}).value

        proposal = state.propose(column.uuid, [
            AtomicOperation.create_child(column.uuid, {"name": "New card"}, {}),
        ]).value

        self.assertEqual(proposal.base_hashes, {})

    def test_accept_proposal_blocked_when_target_own_fields_changed_elsewhere(self):
        proposer = ProtocolState("si-a")
        accepter = ProtocolState("si-b")
        column = proposer.create_child(proposer.root.uuid, {"name": "To Do"}, {}).value
        accepter.attach_topic(PRSPNode.from_dict(column.to_dict()))
        proposal = proposer.propose(column.uuid, [
            AtomicOperation.modify(column.uuid, {"name": "Renamed"}, {})
        ]).value

        accepter.modify(column.uuid, {"name": "Renamed by accepter first"}, {})

        result = accepter.accept_proposal(proposal)

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "base hash mismatch")

    def test_accept_proposal_not_blocked_by_unrelated_child_change(self):
        proposer = ProtocolState("si-a")
        accepter = ProtocolState("si-b")
        column = proposer.create_child(proposer.root.uuid, {"name": "To Do"}, {}).value
        proposer.create_child(column.uuid, {"name": "Card"}, {})
        accepter.attach_topic(PRSPNode.from_dict(proposer.index[column.uuid].to_dict()))

        proposal = proposer.propose(column.uuid, [
            AtomicOperation.modify(column.uuid, {"name": "Renamed"}, {})
        ]).value

        # The accepter independently edits the card underneath the column
        # (unrelated to the rename) before ever seeing the proposal.
        accepter_column = accepter.index[column.uuid]
        accepter_card_uuid = accepter_column.children[0].uuid
        accepter.modify(accepter_card_uuid, {"name": "Card edited by accepter"}, {})

        result = accepter.accept_proposal(proposal)

        self.assertTrue(result.ok)

    def test_propose_blocked_when_nested_inside_active_proposal(self):
        state = ProtocolState("si-a")
        topic = state.create_child(state.root.uuid, {"name": "topic"}, {}).value
        child = state.create_child(topic.uuid, {"name": "child"}, {}).value
        state.propose(topic.uuid, [
            AtomicOperation.create_child(topic.uuid, {"name": "top-level"}, {})
        ])

        result = state.propose(child.uuid, [
            AtomicOperation.create_child(child.uuid, {"name": "nested"}, {})
        ])

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "nested proposal blocked")

    def test_propose_blocked_when_it_would_enclose_active_proposal(self):
        state = ProtocolState("si-a")
        topic = state.create_child(state.root.uuid, {"name": "topic"}, {}).value
        child = state.create_child(topic.uuid, {"name": "child"}, {}).value
        grandchild = state.create_child(child.uuid, {"name": "grandchild"}, {}).value
        state.propose(grandchild.uuid, [
            AtomicOperation.create_child(grandchild.uuid, {"name": "deep"}, {})
        ])

        result = state.propose(topic.uuid, [
            AtomicOperation.create_child(topic.uuid, {"name": "enclosing"}, {})
        ])

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "nested proposal blocked")

    def test_propose_allowed_on_same_node_as_active_proposal(self):
        state = ProtocolState("si-a")
        topic = state.create_child(state.root.uuid, {"name": "topic"}, {}).value
        state.propose(topic.uuid, [
            AtomicOperation.create_child(topic.uuid, {"name": "first"}, {})
        ])

        result = state.propose(topic.uuid, [
            AtomicOperation.create_child(topic.uuid, {"name": "second"}, {})
        ])

        self.assertTrue(result.ok)

    def test_propose_not_blocked_by_unrelated_active_proposal(self):
        state = ProtocolState("si-a")
        topic = state.create_child(state.root.uuid, {"name": "topic"}, {}).value
        child1 = state.create_child(topic.uuid, {"name": "child1"}, {}).value
        child2 = state.create_child(topic.uuid, {"name": "child2"}, {}).value
        state.propose(child1.uuid, [
            AtomicOperation.create_child(child1.uuid, {"name": "on-child1"}, {})
        ])

        result = state.propose(child2.uuid, [
            AtomicOperation.create_child(child2.uuid, {"name": "on-child2"}, {})
        ])

        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
