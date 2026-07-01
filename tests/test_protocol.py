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

    def test_previous_state_hash_is_serialized_but_not_part_of_state_hash(self):
        node = PRSPNode({"name": "node"})
        payload = node.to_dict()
        payload["previous_state_hash"] = "different-history"

        loaded = PRSPNode.from_dict(payload)

        self.assertEqual(loaded.previous_state_hash, "different-history")
        self.assertEqual(loaded.state_hash, node.state_hash)

    def test_modify_tracks_previous_state_hash(self):
        state = ProtocolState("si-a")
        child = state.create_child(state.root.uuid, {"name": "child"}, {}).value
        old_child_hash = child.state_hash
        old_root_hash = state.root.state_hash

        state.modify(child.uuid, {"name": "changed"}, {})

        self.assertEqual(child.previous_state_hash, old_child_hash)
        self.assertEqual(state.root.previous_state_hash, old_root_hash)
        self.assertIn(old_child_hash, child.state_ancestor_hashes)
        self.assertIn(old_root_hash, state.root.state_ancestor_hashes)
        self.assertNotEqual(child.state_hash, old_child_hash)
        self.assertNotEqual(state.root.state_hash, old_root_hash)

    def test_modify_keeps_bounded_state_ancestor_hashes(self):
        state = ProtocolState("si-a")
        child = state.create_child(state.root.uuid, {"name": "child"}, {}).value
        seen = []

        for index in range(20):
            seen.append(child.state_hash)
            state.modify(child.uuid, {"name": f"changed-{index}"}, {})

        self.assertEqual(len(child.state_ancestor_hashes), 16)
        self.assertEqual(child.state_ancestor_hashes, seen[-1:-17:-1])

    def test_new_clone_resets_previous_state_hash_to_current(self):
        state = ProtocolState("si-a")
        parent = state.create_child(state.root.uuid, {"name": "parent"}, {}).value
        state.create_child(parent.uuid, {"name": "child"}, {})

        clone = state.copy(parent.uuid, state.root.uuid).value

        self.assertEqual(clone.previous_state_hash, clone.state_hash)
        self.assertEqual(clone.state_ancestor_hashes, [])
        self.assertEqual(
            clone.children[0].previous_state_hash,
            clone.children[0].state_hash,
        )
        self.assertEqual(clone.children[0].state_ancestor_hashes, [])

    def test_atomic_create_modify_delete(self):
        state = ProtocolState("si-a")
        child = state.create_child(state.root.uuid, {"name": "child"}, {}).value

        self.assertIn(child.uuid, state.index)
        self.assertTrue(state.modify(child.uuid, {"name": "renamed"}, {}).ok)
        self.assertEqual(state.index[child.uuid].data["name"], "renamed")
        self.assertTrue(state.delete(child.uuid).ok)
        self.assertNotIn(child.uuid, state.index)

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


if __name__ == "__main__":
    unittest.main()
