import asyncio
import json
import threading
import unittest
from types import SimpleNamespace

from sovereign.application import (
    application_composite_response, application_mutation_json_response,
    application_result_view, application_snapshot_response, json_value,
)
from sovereign.protocol import ProtocolNode
from sovereign.session import Session, SessionResult


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

    def test_mutation_id_is_applied_once_and_returns_confirmed_revision(self):
        session = Session("local")
        calls = []
        services = SimpleNamespace(
            session=session,
            current_revision=session.current_view_revision,
            persist_confirmed_change=lambda _kind: None,
            deliver_effects=lambda effects: calls.append(("effects", effects)),
        )

        def operation():
            calls.append("operation")
            session.application_metadata("test")["value"] = 1
            return SessionResult("ok", value="done")

        first = asyncio.run(application_mutation_json_response(
            services, operation, mutation_id="mutation-1",
            invalidates=("view",),
        ))
        second = asyncio.run(application_mutation_json_response(
            services, operation, mutation_id="mutation-1",
            invalidates=("view",),
        ))
        first_payload = json.loads(first.body)
        second_payload = json.loads(second.body)

        self.assertEqual(calls.count("operation"), 1)
        self.assertEqual(first_payload, second_payload)
        self.assertEqual(first_payload["revision"], 1)
        self.assertEqual(first_payload["invalidates"], ["view"])

    def test_snapshot_data_and_revision_are_read_under_one_session_lock(self):
        session = Session("local")
        with session.lock:
            session.application_metadata("test")["value"] = 3
        session.advance_view_revision()
        services = SimpleNamespace(session=session)

        response = application_snapshot_response(
            services,
            lambda: dict(session.application_metadata("test")),
        )

        self.assertEqual(json.loads(response.body), {"value": 3, "revision": 1})

    def test_composite_observation_and_merge_do_not_hold_session_lock(self):
        session = Session("local")
        services = SimpleNamespace(session=session)
        session_was_available = []
        snapshots = []

        def assert_session_available():
            acquired = session.lock.acquire(blocking=False)
            session_was_available.append(acquired)
            if acquired:
                session.lock.release()

        def observer(snapshot):
            snapshots.append(snapshot)
            def transport_thread():
                assert_session_available()

            thread = threading.Thread(target=transport_thread)
            thread.start()
            thread.join(1)
            return {"live": True}

        def merger(snapshot, observed):
            assert_session_available()
            return {**snapshot, **observed}

        response = application_composite_response(
            services,
            lambda: {"status": "ok"},
            observer,
            merger,
        )

        self.assertEqual(session_was_available, [True, True])
        self.assertEqual(snapshots, [{"status": "ok"}])
        self.assertEqual(
            json.loads(response.body),
            {"status": "ok", "live": True, "revision": 0},
        )

    def test_mutation_releases_session_before_channel_guarded_persistence(self):
        session = Session("local")
        manager_lock = threading.Lock()
        mutation_entered = threading.Event()
        manager_held = threading.Event()
        channel_finished = threading.Event()
        responses = []
        failures = []

        services = SimpleNamespace(
            session=session,
            current_revision=session.current_view_revision,
            persist_confirmed_change=lambda _kind: (
                manager_lock.acquire(),
                manager_lock.release(),
            ),
            deliver_effects=lambda _effects: None,
        )

        def operation():
            mutation_entered.set()
            self.assertTrue(manager_held.wait(1))
            return SessionResult("ok")

        def channel_operation():
            self.assertTrue(mutation_entered.wait(1))
            with manager_lock:
                manager_held.set()
                with session.lock:
                    channel_finished.set()

        def mutate():
            try:
                responses.append(asyncio.run(
                    application_mutation_json_response(
                        services, operation, mutation_id="no-lock-inversion",
                    )
                ))
            except BaseException as exc:
                failures.append(exc)

        channel = threading.Thread(target=channel_operation, daemon=True)
        mutation = threading.Thread(target=mutate, daemon=True)
        channel.start()
        mutation.start()
        mutation.join(2)
        channel.join(2)

        self.assertFalse(mutation.is_alive(), "session/channel lock inversion")
        self.assertFalse(channel.is_alive(), "session/channel lock inversion")
        self.assertFalse(failures)
        self.assertTrue(channel_finished.is_set())
        self.assertEqual(json.loads(responses[0].body)["revision"], 1)


if __name__ == "__main__":
    unittest.main()
