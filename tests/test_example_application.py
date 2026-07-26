"""Core's contract, exercised by a real installed application.

Core's unit tests build stub applications, which prove the mechanics but are
written against the same understanding as the code under test. This runs a
genuine installed distribution through the host instead: manifest discovery,
topic registration, routing, effect delivery, invitation composition.

S-Agreement used to serve this purpose from inside Core's repository. It
became a product and moved out, so the minimal notes example took over. If
this file is ever deleted, Core stops proving that anything other than a
mock can implement its contract.
"""

import json
import tempfile
import unittest
from pathlib import Path

from sovereign import app_server
from sovereign.session import Session
from sovereign_example_notes.application import APPLICATION_MANIFEST
from sovereign_example_notes.logic import NOTE, NOTE_LIST, NotesLogic


EXAMPLE_ALIAS = {
    "notes": {
        "app_module": "sovereign_example_notes.application",
        "application_id": APPLICATION_MANIFEST.application_id,
        "asset_package": APPLICATION_MANIFEST.asset_package,
        "ui_file": APPLICATION_MANIFEST.ui_file,
        "css_file": APPLICATION_MANIFEST.css_file,
    },
}


def _runtime(port, directory):
    config = app_server.load_config(None, "notes", EXAMPLE_ALIAS)
    config["storage_file"] = str(Path(directory) / f"{port}.json")
    return app_server.create_runtime(port, config)


class ExampleLogicTests(unittest.TestCase):
    def test_notes_are_children_of_one_list(self):
        logic = NotesLogic(Session("http://a"), {})

        first = logic.create_note("write it down")
        second = logic.create_note("and again")

        self.assertEqual(first.status, "ok")
        self.assertEqual(second.status, "ok")
        note_list = logic.ensure_list()
        self.assertEqual(
            [note.data["text"] for note in logic.notes(note_list)],
            ["write it down", "and again"],
        )
        self.assertEqual(note_list.data["type"], NOTE_LIST)
        self.assertTrue(
            all(note.data["type"] == NOTE for note in logic.notes(note_list)),
        )

    def test_ensure_list_is_idempotent(self):
        logic = NotesLogic(Session("http://a"), {})

        self.assertEqual(logic.ensure_list().uuid, logic.ensure_list().uuid)
        self.assertEqual(len(logic.note_lists()), 1)

    def test_a_new_note_lands_inside_the_shared_topic(self):
        # What syncs is the topic's subtree, so a note parented anywhere else
        # would look right locally and never reach a peer. An empty effect
        # list here is correct - there are no members yet - so the placement
        # is the property worth asserting, not the delivery.
        session = Session("http://a")
        logic = NotesLogic(session, {})
        note_list = logic.ensure_list()

        note = logic.create_note("shared").value

        self.assertEqual(note.parent_uuid, note_list.uuid)
        self.assertIn(note.uuid, session.protocol.index)


class ExampleThroughTheHostTests(unittest.TestCase):
    def test_the_host_discovers_the_manifest_and_mounts_the_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(8401, tmp)
            app = app_server.build_app(runtime)

        self.assertIsInstance(runtime.logic, NotesLogic)
        paths = {route.path for route in app.routes}
        self.assertIn("/api/example-notes/state", paths)
        self.assertIn("/api/example-notes/notes", paths)
        # Core's own routes must still be there alongside the application's.
        self.assertIn("/api/protocol", paths)

    def test_the_registration_makes_the_note_list_shareable(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(8402, tmp)
            note_list = runtime.logic.ensure_list()

            # Core only treats a node as shareable once an application has
            # claimed its root type. This is the whole point of registration.
            self.assertTrue(runtime.session.supports_shared_topic(note_list))
            self.assertIn(
                note_list.uuid, runtime.session.shared_topics.local_topic_uuids(None),
            )

    def test_an_invitation_composes_for_the_example_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(8403, tmp)
            note_list = runtime.logic.ensure_list()

            result = runtime.collaboration.compose_invitation(note_list.uuid, "http")

            self.assertTrue(result.ok, getattr(result, "reason", None))
            self.assertEqual(
                sorted(result.value["topic_uuids"]),
                sorted([note_list.uuid, runtime.session.identity.uuid]),
            )

    def test_state_survives_a_save_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = _runtime(8404, tmp)
            first.logic.create_note("persisted")
            first.persist()

            second = _runtime(8404, tmp)

        note_list = second.logic.ensure_list()
        self.assertEqual(
            [note.data["text"] for note in second.logic.notes(note_list)],
            ["persisted"],
        )

    def test_the_state_endpoint_reports_the_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(8405, tmp)
            runtime.logic.create_note("visible")

            payload = json.loads(json.dumps(runtime.logic.state()))

        self.assertEqual([note["text"] for note in payload["notes"]], ["visible"])
        self.assertEqual(payload["address"], runtime.address)


if __name__ == "__main__":
    unittest.main()
