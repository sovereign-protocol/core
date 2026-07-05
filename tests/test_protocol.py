import unittest

from protocol import PRSPNode, ProtocolState


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

    def test_noop_modify_keeps_previous_hash(self):
        state = ProtocolState("si-a")
        child = state.create_child(state.root.uuid, {"name": "child"}, {}).value
        state.modify(child.uuid, {"name": "renamed"}, {})
        previous_hash_before = child.previous_hash

        # Writing identical data (e.g. saving a card without editing it)
        # must not consume the one-slot history a lagging peer relies on.
        state.modify(child.uuid, {"name": "renamed"}, {})

        self.assertEqual(child.previous_hash, previous_hash_before)

    def test_same_parent_move_keeps_previous_parent_uuid(self):
        state = ProtocolState("si-a")
        first = state.create_child(state.root.uuid, {"name": "first"}, {}).value
        second = state.create_child(state.root.uuid, {"name": "second"}, {}).value
        child = state.create_child(first.uuid, {"name": "child"}, {}).value
        state.create_child(second.uuid, {"name": "sibling"}, {})
        state.move_child(child.uuid, second.uuid)

        # A reorder within the same parent (kanban does this on every
        # within-column drag) must not consume the real move history.
        state.move_child(child.uuid, second.uuid, 0)

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
        self.assertTrue(child.reaffirmed)
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
        self.assertFalse(child.reaffirmed)

    def test_reaffirm_is_cleared_by_a_later_cascaded_change(self):
        state = ProtocolState("si-a")
        parent = state.create_child(state.root.uuid, {"name": "parent"}, {}).value
        child = state.create_child(parent.uuid, {"name": "child"}, {}).value

        self.assertTrue(state.reaffirm(parent.uuid).ok)
        self.assertTrue(parent.is_reaffirmed())

        # A change to a descendant cascades parent's state_hash upward -
        # the parent's reaffirm should no longer apply to this new state,
        # with no explicit clearing needed anywhere but cascade_hash.
        state.modify(child.uuid, {"name": "child changed"}, {})

        self.assertFalse(parent.is_reaffirmed())

    def test_reaffirm_is_cleared_by_a_later_move(self):
        state = ProtocolState("si-a")
        first = state.create_child(state.root.uuid, {"name": "first"}, {}).value
        second = state.create_child(state.root.uuid, {"name": "second"}, {}).value
        child = state.create_child(first.uuid, {"name": "child"}, {}).value

        self.assertTrue(state.reaffirm(child.uuid).ok)
        self.assertTrue(child.is_reaffirmed())

        state.move_child(child.uuid, second.uuid)

        self.assertFalse(child.is_reaffirmed())

    def test_reaffirm_is_cleared_by_a_later_delete(self):
        state = ProtocolState("si-a")
        child = state.create_child(state.root.uuid, {"name": "child"}, {}).value

        self.assertTrue(state.reaffirm(child.uuid).ok)
        self.assertTrue(child.is_reaffirmed())

        state.delete(child.uuid)

        self.assertFalse(child.is_reaffirmed())

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

if __name__ == "__main__":
    unittest.main()
