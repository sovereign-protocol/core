import unittest

from sovereign.protocol import ProtocolNode, ProtocolState


class ProtocolTests(unittest.TestCase):
    def test_hashes_ignore_child_order(self):
        first = ProtocolNode({"name": "root"})
        a = ProtocolNode({"name": "a"}, parent_uuid=first.uuid)
        b = ProtocolNode({"name": "b"}, parent_uuid=first.uuid)
        first.children = [a, b]
        first.refresh_hashes_deep()

        second = ProtocolNode({"name": "root"})
        a2 = ProtocolNode.from_dict(a.to_dict())
        b2 = ProtocolNode.from_dict(b.to_dict())
        a2.parent_uuid = second.uuid
        b2.parent_uuid = second.uuid
        second.children = [b2, a2]
        second.refresh_hashes_deep()

        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.state_hash, second.state_hash)

    def test_from_dict_rejects_mismatched_hash(self):
        node = ProtocolNode({"name": "node"})
        payload = node.to_dict()
        payload["data"]["name"] = "tampered"

        with self.assertRaises(ValueError):
            ProtocolNode.from_dict(payload)

    def test_from_dict_rejects_legacy_revision_origin_field(self):
        payload = ProtocolNode({"name": "node"}).to_dict()
        payload["revision_origin_identity"] = payload.pop("revision_origin")

        with self.assertRaisesRegex(ValueError, "unsupported legacy field"):
            ProtocolNode.from_dict(payload)

    def test_new_node_starts_settled(self):
        node = ProtocolNode({"name": "node"})

        # base_hash snapshots the node's OWN content (content_hash), not the
        # recursive subtree (state_hash).
        self.assertEqual(node.base_hash, node.content_hash)
        self.assertEqual(node.base_parent_uuid, node.parent_uuid)

    def test_same_origin_modifications_keep_compound_base_hash(self):
        state = ProtocolState("si-a")
        child = state.create_child(
            state.root.uuid, {"name": "child"}, {}, "identity-a",
        ).value
        old_hash = child.content_hash

        state.modify(child.uuid, {"name": "first"}, {}, "identity-a")
        first_hash = child.state_hash
        state.modify(child.uuid, {"name": "second"}, {}, "identity-a")

        self.assertEqual(child.base_hash, old_hash)
        self.assertNotEqual(child.state_hash, first_hash)

    def test_different_origin_starts_new_revision_wave(self):
        state = ProtocolState("si-a")
        child = state.create_child(
            state.root.uuid, {"name": "child"}, {}, "identity-a",
        ).value
        state.modify(child.uuid, {"name": "first"}, {}, "identity-a")
        previous_actual = child.content_hash

        state.modify(child.uuid, {"name": "second"}, {}, "identity-b")

        self.assertEqual(child.base_hash, previous_actual)
        self.assertEqual(child.revision_origin, "identity-b")

    def test_move_child_tracks_base_parent_uuid(self):
        state = ProtocolState("si-a")
        first = state.create_child(state.root.uuid, {"name": "first"}, {}).value
        second = state.create_child(state.root.uuid, {"name": "second"}, {}).value
        child = state.create_child(first.uuid, {"name": "child"}, {}).value

        state.move_child(child.uuid, second.uuid)

        self.assertEqual(child.base_parent_uuid, first.uuid)
        self.assertEqual(child.parent_uuid, second.uuid)

    def test_noop_modify_keeps_base_hash(self):
        state = ProtocolState("si-a")
        child = state.create_child(state.root.uuid, {"name": "child"}, {}).value
        state.modify(child.uuid, {"name": "renamed"}, {})
        base_hash_before = child.base_hash

        # Writing identical data (e.g. saving a card without editing it)
        # must not change the compound revision base.
        state.modify(child.uuid, {"name": "renamed"}, {})

        self.assertEqual(child.base_hash, base_hash_before)

    def test_descendant_change_does_not_revision_ancestor(self):
        # The core of the node_hash/subtree_hash split: editing a child moves
        # the parent's subtree (state) hash but never its own content hash or
        # its revision base - so a card edit can't manufacture a column/board
        # revision or false divergence.
        state = ProtocolState("si-a")
        parent = state.create_child(
            state.root.uuid, {"name": "parent"}, {}, "identity-a",
        ).value
        child = state.create_child(
            parent.uuid, {"name": "child"}, {}, "identity-a",
        ).value
        content_before = parent.content_hash
        state_before = parent.state_hash
        base_before = parent.base_hash

        state.modify(child.uuid, {"name": "edited"}, {}, "identity-b")

        self.assertEqual(parent.content_hash, content_before)
        self.assertNotEqual(parent.state_hash, state_before)
        self.assertEqual(parent.base_hash, base_before)

    def test_container_base_survives_descendant_edit(self):
        # Regression for the finding-#2 container-rollback bug: renaming a
        # container sets its wave base; a later descendant edit must NOT slide
        # that base (which previously broke rolling back the rename).
        state = ProtocolState("si-a")
        column = state.create_child(
            state.root.uuid, {"name": "col"}, {}, "identity-a",
        ).value
        card = state.create_child(
            column.uuid, {"name": "card"}, {}, "identity-a",
        ).value
        state.modify(column.uuid, {"name": "col-renamed"}, {}, "identity-b")
        base_after_rename = column.base_hash

        state.modify(card.uuid, {"name": "card-edited"}, {}, "identity-a")

        self.assertEqual(column.base_hash, base_after_rename)

    def test_subtree_hash_includes_child_identity(self):
        # subtree_hash folds in child uuids, so swapping a child for an
        # identical-content node with a different uuid still changes the
        # parent - required for transfer validation to catch a structural swap.
        parent = ProtocolNode({"name": "parent"})
        child_a = ProtocolNode({"name": "same"}, parent_uuid=parent.uuid)
        parent.children = [child_a]
        parent.refresh_hashes()
        hash_with_a = parent.state_hash

        child_b = ProtocolNode({"name": "same"}, parent_uuid=parent.uuid)
        self.assertNotEqual(child_a.uuid, child_b.uuid)
        self.assertEqual(child_a.content_hash, child_b.content_hash)
        self.assertEqual(child_a.state_hash, child_b.state_hash)
        parent.children = [child_b]
        parent.refresh_hashes()

        self.assertNotEqual(parent.state_hash, hash_with_a)

    def test_adopt_own_fields_keeps_children_and_copies_revision(self):
        # Shallow adopt: update a container's own fields (and origin/base)
        # without disturbing its local children - a container adopt must not
        # smuggle away a card the recipient is keeping.
        state = ProtocolState("si-a")
        column = state.create_child(
            state.root.uuid, {"name": "col"}, {}, "identity-a",
        ).value
        card = state.create_child(
            column.uuid, {"name": "card"}, {}, "identity-a",
        ).value
        source = ProtocolNode({"name": "col-renamed"}, revision_origin="identity-b")
        source.refresh_hashes()

        state.adopt_own_fields(column.uuid, source)

        self.assertEqual(column.data["name"], "col-renamed")
        self.assertIn(card, column.children)
        self.assertIn(card.uuid, state.index)
        self.assertEqual(column.revision_origin, "identity-b")
        self.assertEqual(column.base_hash, source.base_hash)

    def test_adopt_own_fields_propagates_deletion_without_cascading(self):
        # A container deletion propagates only the container's own `deleted`
        # flag; children are left for their own per-node (eligibility-checked)
        # deletion events, so a kept card survives under a not_owner policy.
        state = ProtocolState("si-a")
        column = state.create_child(
            state.root.uuid, {"name": "col"}, {}, "identity-a",
        ).value
        card = state.create_child(
            column.uuid, {"name": "card"}, {}, "identity-a",
        ).value
        source = ProtocolNode({"name": "col"}, revision_origin="identity-b")
        source.deleted = True
        source.refresh_hashes()

        state.adopt_own_fields(column.uuid, source)

        self.assertTrue(column.deleted)
        self.assertFalse(card.deleted)
        self.assertIn(card.uuid, state.index)

    def test_adopt_own_fields_preserves_source_timestamp(self):
        # Adoption copies the source revision's updated_at, not the adopter's
        # clock - Session orders forwarded same-origin revisions by updated_at,
        # so a fresh stamp could make a stale adopted copy look newer.
        state = ProtocolState("si-a")
        node = state.create_child(
            state.root.uuid, {"name": "n"}, {}, "identity-a",
        ).value
        source = ProtocolNode({"name": "n2"}, revision_origin="identity-b")
        source.updated_at = "2020-01-01T00:00:00.000+00:00"
        source.refresh_hashes()

        state.adopt_own_fields(node.uuid, source)

        self.assertEqual(node.updated_at, "2020-01-01T00:00:00.000+00:00")
        self.assertEqual(node.data["name"], "n2")

    def test_adopt_own_fields_defers_move_to_absent_parent(self):
        # A move to a parent not present locally defers the WHOLE adoption
        # (atomic) rather than adopting content onto a node left at the old
        # parent - that would be a hybrid revision that never existed remotely.
        state = ProtocolState("si-a")
        column = state.create_child(
            state.root.uuid, {"name": "col"}, {}, "identity-a",
        ).value
        card = state.create_child(
            column.uuid, {"name": "card"}, {}, "identity-a",
        ).value
        before_parent = card.parent_uuid
        before_content = card.content_hash
        source = ProtocolNode(
            {"name": "card-edited"}, parent_uuid="missing-parent",
            revision_origin="identity-a",
        )
        source.refresh_hashes()

        result = state.adopt_own_fields(card.uuid, source)

        self.assertFalse(result.ok)
        self.assertEqual(card.parent_uuid, before_parent)
        self.assertEqual(card.content_hash, before_content)
        self.assertEqual(card.data["name"], "card")

    def test_revision_origin_survives_roundtrip_and_only_real_node_edits_replace_it(self):
        state = ProtocolState("si-a")
        parent = state.create_child(
            state.root.uuid, {"name": "parent"}, {}, "identity-a",
        ).value
        child = state.create_child(
            parent.uuid, {"name": "child"}, {}, "identity-a",
        ).value

        state.modify(child.uuid, {"name": "changed"}, {}, "identity-c")

        # The child's actual editor is C. The parent's hash changed only
        # because of the descendant, so its own revision still belongs to A.
        self.assertEqual(child.revision_origin, "identity-c")
        self.assertEqual(parent.revision_origin, "identity-a")
        restored = ProtocolNode.from_dict(child.to_dict())
        self.assertEqual(restored.revision_origin, "identity-c")

        # A no-op must not relabel C's revision as B's.
        state.modify(child.uuid, {"name": "changed"}, {}, "identity-b")
        self.assertEqual(child.revision_origin, "identity-c")

    def test_delete_and_copy_record_the_client_that_performed_the_operation(self):
        state = ProtocolState("si-a")
        parent = state.create_child(
            state.root.uuid, {"name": "parent"}, {}, "identity-a",
        ).value
        child = state.create_child(
            parent.uuid, {"name": "child"}, {}, "identity-a",
        ).value

        clone = state.copy(
            parent.uuid, state.root.uuid, "identity-b",
        ).value
        self.assertTrue(all(
            node.revision_origin == "identity-b"
            for node in (clone, *clone.children)
        ))

        state.delete(parent.uuid, "identity-c")
        self.assertTrue(all(
            node.revision_origin == "identity-c"
            for node in (parent, child)
        ))

    def test_same_parent_move_keeps_base_parent_uuid(self):
        state = ProtocolState("si-a")
        first = state.create_child(state.root.uuid, {"name": "first"}, {}).value
        second = state.create_child(state.root.uuid, {"name": "second"}, {}).value
        child = state.create_child(first.uuid, {"name": "child"}, {}).value
        state.create_child(second.uuid, {"name": "sibling"}, {})
        state.move_child(child.uuid, second.uuid)

        # A reorder within the same parent (kanban does this on every
        # within-column drag) must not consume the real move history.
        state.move_child(child.uuid, second.uuid, 0)

        self.assertEqual(child.base_parent_uuid, first.uuid)
        self.assertEqual(child.parent_uuid, second.uuid)

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
