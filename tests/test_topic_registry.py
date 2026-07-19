import unittest

from protocol import ProtocolNode
from topic_registry import SharedTopicRegistry


class SharedTopicRegistryTests(unittest.TestCase):
    def test_enumerates_topics_and_routes_invitation_by_root_type(self):
        registry = SharedTopicRegistry()
        topic = ProtocolNode({"type": "agreement", "title": "Terms"})
        accepted = []
        registry.register(
            "S-Agreement",
            {"agreement"},
            lambda: [topic],
            lambda tree: accepted.append(tree.uuid),
        )

        self.assertEqual(registry.local_topic_uuids(), [topic.uuid])
        self.assertTrue(registry.supports(topic))
        registry.accept_invited_topic(topic)
        self.assertEqual(accepted, [topic.uuid])

    def test_root_type_can_only_belong_to_one_application(self):
        registry = SharedTopicRegistry()
        registry.register("first", {"agreement"}, lambda: [], lambda tree: None)

        with self.assertRaisesRegex(ValueError, "already handled"):
            registry.register("second", {"agreement"}, lambda: [], lambda tree: None)

    def test_reregistering_owner_replaces_its_types_and_unregister_removes_it(self):
        registry = SharedTopicRegistry()
        registry.register("app", {"old"}, lambda: [], lambda tree: None)
        registry.register("app", {"new"}, lambda: [], lambda tree: None)

        self.assertFalse(registry.supports(ProtocolNode({"type": "old"})))
        self.assertTrue(registry.supports(ProtocolNode({"type": "new"})))
        registry.unregister("app")
        self.assertFalse(registry.supports(ProtocolNode({"type": "new"})))


if __name__ == "__main__":
    unittest.main()
