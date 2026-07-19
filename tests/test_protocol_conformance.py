import json
import unittest
from pathlib import Path

from protocol import (
    UnsupportedProtocolVersion,
    protocol_node_from_envelope,
    protocol_tree_envelope,
)


FIXTURES = Path(__file__).with_name("fixtures")


class ProtocolConformanceTests(unittest.TestCase):
    def test_protocol_tree_v1_golden_fixture_roundtrips_exactly(self):
        payload = json.loads(
            (FIXTURES / "protocol_tree_v1.json").read_text(encoding="utf-8")
        )

        node = protocol_node_from_envelope(payload)

        self.assertEqual(protocol_tree_envelope(node), payload)
        self.assertEqual(node.content_hash, "c1db147d1403df61d211")
        self.assertEqual(node.state_hash, "d20f3b241193d82f3847")

    def test_protocol_envelope_rejects_missing_or_unknown_version(self):
        payload = json.loads(
            (FIXTURES / "protocol_tree_v1.json").read_text(encoding="utf-8")
        )
        payload.pop("protocol_schema_version")

        with self.assertRaises(UnsupportedProtocolVersion):
            protocol_node_from_envelope(payload)

        payload["protocol_schema_version"] = 99
        with self.assertRaises(UnsupportedProtocolVersion):
            protocol_node_from_envelope(payload)


if __name__ == "__main__":
    unittest.main()
