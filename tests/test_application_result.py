import unittest

from sovereign.application import application_result_view, json_value
from sovereign.protocol import ProtocolNode
from sovereign.session import SessionResult


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

    def test_an_error_has_a_stable_serializable_shape(self):
        error = application_result_view(SessionResult("error", reason="bad"))

        self.assertFalse(error.ok)
        self.assertEqual(error.payload, {"status": "error", "reason": "bad"})

    def test_result_contains_no_delivery_payload(self):
        view = application_result_view(SessionResult("ok"))

        self.assertTrue(view.ok)
        self.assertEqual(view.payload, {"status": "ok"})

    def test_unknown_internal_value_is_rejected(self):
        with self.assertRaisesRegex(TypeError, "not JSON-serializable"):
            json_value(object())
        with self.assertRaisesRegex(TypeError, "non-finite"):
            json_value(float("nan"))


if __name__ == "__main__":
    unittest.main()
