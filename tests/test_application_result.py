import unittest

from sovereign.application import application_result_view, json_value
from sovereign.protocol import ProtocolNode
from sovereign.session import SessionResult
from sovereign.transport import TransportDelivery


class ApplicationResultViewTests(unittest.TestCase):
    def test_success_value_is_recursively_json_safe(self):
        node = ProtocolNode({"type": "example"})
        node.refresh_hashes()

        view = application_result_view(SessionResult(
            "ok", value={"node": node, "values": {"a", "b"}},
        ))

        self.assertTrue(view.ok)
        self.assertEqual(view.payload["status"], "ok")
        self.assertIsInstance(view.payload["value"]["node"], dict)
        self.assertEqual(view.payload["value"]["values"], ["a", "b"])

    def test_error_and_delivery_failure_have_stable_serializable_shapes(self):
        error = application_result_view(SessionResult("error", reason="bad"))
        delivered = application_result_view(
            SessionResult("ok"),
            [TransportDelivery(False, "push", "peer", "offline")],
        )

        self.assertFalse(error.ok)
        self.assertEqual(error.payload, {"status": "error", "reason": "bad"})
        self.assertEqual(
            delivered.payload["delivery_errors"][0],
            {"effect_type": "push", "target": "peer", "reason": "offline"},
        )

    def test_unknown_internal_value_is_rejected(self):
        with self.assertRaisesRegex(TypeError, "not JSON-serializable"):
            json_value(object())
        with self.assertRaisesRegex(TypeError, "non-finite"):
            json_value(float("nan"))


if __name__ == "__main__":
    unittest.main()
