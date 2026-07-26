import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sovereign.trace_log import TraceLogger


class TraceLoggerTests(unittest.TestCase):
    def test_environment_value_selects_level_and_default_path(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(
                    os.environ, {"SOVEREIGN_TRACE": "timing"}, clear=True,
                ), \
                patch(
                    "sovereign.trace_log.Path.cwd",
                    return_value=Path(tmp),
                ):
            trace = TraceLogger.from_config({}, 9305, "http://a")

        self.assertTrue(trace.enabled)
        self.assertEqual(trace.level, "timing")
        self.assertEqual(
            trace.path,
            str(Path(tmp) / "data" / "trace_9305.jsonl"),
        )

    def test_legacy_true_value_means_events(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"SOVEREIGN_TRACE": "true"},
            clear=True,
        ):
            trace = TraceLogger.from_config(
                {"trace_log_file": str(Path(tmp) / "trace.jsonl")},
                9305,
                "http://a",
            )

        self.assertEqual(trace.level, "events")

    def test_environment_off_disables_configured_trace_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"SOVEREIGN_TRACE": "off"},
            clear=True,
        ):
            trace = TraceLogger.from_config(
                {"trace_log_file": str(Path(tmp) / "trace.jsonl")},
                9305,
                "http://a",
            )

        self.assertFalse(trace.enabled)
        self.assertEqual(trace.level, "off")

    def test_events_level_filters_timing_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            trace = TraceLogger(str(path), node="http://a", level="events")

            trace.event(
                "relay.phase",
                required_level="timing",
                phase="poll_and_apply",
            )
            trace.event("protocol.modify", node_uuid="node-1")

            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(
            [record["kind"] for record in records],
            ["protocol.modify"],
        )

    def test_timing_level_includes_event_and_timing_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            trace = TraceLogger(str(path), node="http://a", level="timing")

            trace.event("protocol.modify", node_uuid="node-1")
            trace.event(
                "relay.phase",
                required_level="timing",
                phase="poll_and_apply",
            )

            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(
            [record["kind"] for record in records],
            ["protocol.modify", "relay.phase"],
        )


if __name__ == "__main__":
    unittest.main()
