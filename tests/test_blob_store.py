import tempfile
import unittest
from pathlib import Path

from blob_store import (
    BlobStore, blob_id_for, canonical_attachments, is_valid_image,
    referenced_blob_ids,
)
from protocol import PRSPNode


class BlobStoreTests(unittest.TestCase):
    def test_content_addressed_round_trip_and_deduplication(self):
        with tempfile.TemporaryDirectory() as root:
            store = BlobStore(root)
            data = b"same bytes"

            first = store.write_blob(data)
            second = store.write_blob(data)

            self.assertEqual(first, blob_id_for(data))
            self.assertEqual(second, first)
            self.assertEqual(store.read_blob(first), data)
            self.assertEqual(store.iter_blob_ids(), [first])

    def test_gc_keeps_references_and_removes_old_orphans(self):
        with tempfile.TemporaryDirectory() as root:
            store = BlobStore(root, grace_seconds=10)
            kept = store.write_blob(b"kept")
            removed = store.write_blob(b"removed")

            result = store.collect({kept}, now=10**12)

            self.assertEqual(result, [removed])
            self.assertTrue(store.has_blob(kept))
            self.assertFalse(store.has_blob(removed))

    def test_reference_collection_ignores_deleted_subtrees(self):
        live_id = blob_id_for(b"live")
        deleted_id = blob_id_for(b"deleted")
        root = PRSPNode(data={"attachments": [{
            "id": "a", "role": "avatar", "blob_id": live_id,
            "name": "a.png", "size": 4, "mime": "image/png",
        }]})
        deleted = PRSPNode(data={"attachments": [{
            "id": "b", "blob_id": deleted_id, "name": "b", "size": 1,
        }]})
        deleted.deleted = True
        root.children.append(deleted)

        self.assertEqual(referenced_blob_ids(root), {live_id})

    def test_malformed_reference_is_ignored_and_image_magic_is_checked(self):
        blob_id = blob_id_for(b"x")
        self.assertEqual(canonical_attachments([{
            "id": "bad", "blob_id": blob_id, "size": "not-a-number",
        }]), [])
        self.assertTrue(is_valid_image(b"GIF89a...", "image/gif"))
        self.assertFalse(is_valid_image(b"not a gif", "image/gif"))


if __name__ == "__main__":
    unittest.main()
