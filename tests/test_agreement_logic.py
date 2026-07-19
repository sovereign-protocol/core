import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from s_agreement.application import APPLICATION_MANIFEST
from s_agreement.logic import AgreementLogic
from sovereign import Session
from sovereign import app_server
from sovereign.relay_logic import RelayLogic


class MemoryHttpClient:
    def __init__(self, runtimes):
        self.runtimes = runtimes

    def get_json(self, url: str, timeout: float = 5) -> dict:
        runtime, path = self._split(url)
        if path.startswith("/p2p/subtree/"):
            payload, status = runtime.adapter.p2p_subtree(path.rsplit("/", 1)[1])
            if status != 200:
                raise RuntimeError(payload.get("reason", "not found"))
            return payload
        raise RuntimeError(f"unexpected GET {path}")

    def post_json(self, url: str, payload: dict,
                  timeout: float = 5) -> dict:
        runtime, path = self._split(url)
        handlers = {
            "/p2p/join": runtime.adapter.p2p_join,
            "/p2p/sync_status": runtime.adapter.p2p_sync_status,
            "/p2p/announce": runtime.adapter.p2p_announce,
            "/p2p/leave": runtime.adapter.p2p_leave,
        }
        handler = handlers.get(path)
        if not handler:
            raise RuntimeError(f"unexpected POST {path}")
        response, status = handler(payload)
        if status != 200:
            raise RuntimeError(response.get("reason", "request failed"))
        return response

    def _split(self, url: str):
        for address in sorted(self.runtimes, key=len, reverse=True):
            if url.startswith(address):
                return self.runtimes[address], url[len(address):]
        raise RuntimeError(f"unknown address in {url}")


class AgreementLogicTests(unittest.TestCase):
    def test_manifest_and_minimal_document_tree(self):
        runtime = self.runtime(9401)

        agreement_uuid = runtime.logic.create_agreement("Working agreement").value
        section_uuid = runtime.logic.create_section(
            agreement_uuid, "Responsibilities",
        ).value
        clause_uuid = runtime.logic.create_clause(
            section_uuid, "Each participant reviews proposed changes.",
        ).value
        payload = runtime.logic.document_payload()

        self.assertEqual(APPLICATION_MANIFEST.application_id, "agreement")
        self.assertEqual(payload["agreement"]["uuid"], agreement_uuid)
        self.assertEqual(payload["agreement"]["children"][0]["uuid"], section_uuid)
        self.assertEqual(
            payload["agreement"]["children"][0]["children"][0]["uuid"],
            clause_uuid,
        )

    def test_titles_and_text_stay_editable_after_creation(self):
        runtime = self.runtime(9403)
        agreement_uuid = runtime.logic.create_agreement("Draft").value
        section_uuid = runtime.logic.create_section(agreement_uuid, "Scpoe").value
        clause_uuid = runtime.logic.create_clause(section_uuid, "Frist draft.").value

        self.assertEqual(
            runtime.logic.rename_agreement(agreement_uuid, "Service terms").status, "ok",
        )
        self.assertEqual(
            runtime.logic.rename_section(section_uuid, "Scope").status, "ok",
        )
        self.assertEqual(
            runtime.logic.update_clause(clause_uuid, "First draft.").status, "ok",
        )

        payload = runtime.logic.document_payload()
        section = payload["agreement"]["children"][0]
        self.assertEqual(payload["agreement"]["data"]["title"], "Service terms")
        self.assertEqual(section["data"]["title"], "Scope")
        self.assertEqual(section["children"][0]["data"]["text"], "First draft.")

    def test_renaming_rejects_blank_titles_and_unknown_nodes(self):
        runtime = self.runtime(9404)
        agreement_uuid = runtime.logic.create_agreement("Draft").value
        section_uuid = runtime.logic.create_section(agreement_uuid, "Scope").value

        self.assertEqual(runtime.logic.rename_agreement(agreement_uuid, "  ").status, "error")
        self.assertEqual(runtime.logic.rename_section(section_uuid, "").status, "error")
        self.assertEqual(runtime.logic.rename_section("missing", "Scope").status, "error")
        # A section uuid is not an agreement uuid; the type guard must hold.
        self.assertEqual(runtime.logic.rename_agreement(section_uuid, "Nope").status, "error")

    def test_deleting_a_section_removes_its_clauses(self):
        runtime = self.runtime(9405)
        agreement_uuid = runtime.logic.create_agreement("Draft").value
        kept_uuid = runtime.logic.create_section(agreement_uuid, "Kept").value
        removed_uuid = runtime.logic.create_section(agreement_uuid, "Removed").value
        clause_uuid = runtime.logic.create_clause(removed_uuid, "Goes away.").value
        runtime.logic.create_clause(kept_uuid, "Stays.")

        self.assertEqual(runtime.logic.delete_section(removed_uuid).status, "ok")

        payload = runtime.logic.document_payload()
        sections = payload["agreement"]["children"]
        live = [item for item in sections if not item["deleted"]]
        self.assertEqual([item["uuid"] for item in live], [kept_uuid])
        # Deleting a container prunes its descendants out of the index rather
        # than tombstoning each one, so the clause is gone, not flagged.
        self.assertNotIn(clause_uuid, runtime.session.protocol.index)

    def test_deleting_a_single_clause_leaves_its_siblings(self):
        runtime = self.runtime(9406)
        agreement_uuid = runtime.logic.create_agreement("Draft").value
        section_uuid = runtime.logic.create_section(agreement_uuid, "Scope").value
        first_uuid = runtime.logic.create_clause(section_uuid, "First.").value
        second_uuid = runtime.logic.create_clause(section_uuid, "Second.").value

        self.assertEqual(runtime.logic.delete_clause(first_uuid).status, "ok")

        payload = runtime.logic.document_payload()
        clauses = payload["agreement"]["children"][0]["children"]
        live = [item["uuid"] for item in clauses if not item["deleted"]]
        self.assertEqual(live, [second_uuid])

    def test_document_payload_carries_mailbox_targets_for_the_sharing_ui(self):
        runtime = self.runtime(9402)
        agreement_uuid = runtime.logic.create_agreement("Service terms").value

        empty = runtime.logic.document_payload()
        self.assertEqual(empty["channel_targets"], [])
        self.assertIsNone(empty["channel_target_id"])

        target_id = runtime.channel_manager.channel("mailbox").manager.create_target(
            {"name": "Local folder", "backend": "local",
             "root": str(Path(runtime._test_tmp.name) / "relay")},
            verify=False,
        ).value
        runtime.channel_manager.channel("mailbox").manager.assign_topic_target(
            agreement_uuid, target_id,
        )
        payload = runtime.logic.document_payload()

        self.assertEqual(
            [item["id"] for item in payload["channel_targets"]], [target_id],
        )
        self.assertEqual(payload["channel_target_id"], target_id)

    def test_direct_http_invitation_and_transition_visibility(self):
        left = self.runtime(9402)
        right = self.runtime(9403)
        client = MemoryHttpClient({left.address: left, right.address: right})
        left.adapter.http = client
        right.adapter.http = client
        agreement_uuid = left.logic.create_agreement("Shared agreement").value
        section_uuid = left.logic.create_section(agreement_uuid, "Scope").value
        clause_uuid = left.logic.create_clause(section_uuid, "Initial text").value
        left.session.start_discussion(agreement_uuid)

        accepted = right.channel_manager.accept_invitation(
            left.session.identity.to_dict(),
            [agreement_uuid],
            [{
                "type": "http",
                "descriptor_version": 1,
                "address": left.address,
            }],
        )

        self.assertTrue(accepted.ok, accepted.reason)
        self.assertIn(agreement_uuid, [item.uuid for item in right.logic.agreements()])
        changed = left.logic.update_clause(clause_uuid, "Proposed replacement")
        left.channel_manager.execute_effects(changed.effects)
        events = right.logic.transition_events(agreement_uuid)
        clause_events = [event for event in events if event["node_uuid"] == clause_uuid]
        self.assertEqual(len(clause_events), 1)
        self.assertIn(clause_events[0]["type"], {"peer_made_changes", "in_transition"})

    def test_three_level_new_structure_adopts_in_one_pass_child_first(self):
        left = self.runtime(9404)
        right = self.runtime(9405)
        client = MemoryHttpClient({left.address: left, right.address: right})
        left.adapter.http = client
        right.adapter.http = client
        agreement_uuid = left.logic.create_agreement("Nested agreement").value
        left.session.start_discussion(agreement_uuid)
        accepted = right.channel_manager.accept_invitation(
            left.session.identity.to_dict(),
            [agreement_uuid],
            [{
                "type": "http",
                "descriptor_version": 1,
                "address": left.address,
            }],
        )
        self.assertTrue(accepted.ok, accepted.reason)

        section = left.logic.create_section(agreement_uuid, "New section")
        section_uuid = section.value
        left.channel_manager.execute_effects(section.effects)
        clause = left.logic.create_clause(section_uuid, "Nested clause")
        clause_uuid = clause.value
        left.channel_manager.execute_effects(clause.effects)
        events = right.session.analyze_peer_transitions(
            left.address, agreement_uuid,
        )
        incoming = [
            event for event in events
            if event["node_uuid"] in {section_uuid, clause_uuid}
        ]
        child_first = sorted(
            incoming, key=lambda event: event["node_uuid"] != clause_uuid,
        )

        with patch.object(
            right.session, "analyze_peer_transitions", return_value=child_first,
        ):
            adopted = right.logic.adopt_peer_changes(
                left.address, agreement_uuid,
            )

        self.assertEqual(adopted.status, "ok")
        self.assertTrue(adopted.value)
        self.assertIn(section_uuid, right.session.protocol.index)
        self.assertIn(clause_uuid, right.session.protocol.index)
        self.assertEqual(
            right.session.protocol.index[clause_uuid].parent_uuid, section_uuid,
        )

    def test_mailbox_invitation_mounts_agreement_without_core_special_case(self):
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            session_a = Session("addr-a")
            logic_a = AgreementLogic(session_a)
            session_a.register_application(logic_a.application_registration())
            agreement_uuid = logic_a.create_agreement("Relayed agreement").value
            section_uuid = logic_a.create_section(agreement_uuid, "Scope").value
            clause_uuid = logic_a.create_clause(section_uuid, "Mailbox clause").value
            relay_a = RelayLogic(
                session_a, self.relay_config(relay_root, "A", state_dir),
            )
            relay_a.mark_topics_shared([agreement_uuid])
            relay_a.publish_due_topics()
            descriptor = relay_a.channel_descriptor()

            session_b = Session("addr-b")
            logic_b = AgreementLogic(session_b)
            session_b.register_application(logic_b.application_registration())
            relay_b = RelayLogic(
                session_b,
                {"relay_state_file": str(Path(state_dir) / "state-B.json")},
            )
            self.assertTrue(relay_b.adopt_storage_from_descriptor(descriptor))
            relay_b.mark_topics_desired([agreement_uuid])

            applied = relay_b.poll_and_apply()

            self.assertIn((agreement_uuid, "A"), applied)
            self.assertIn(agreement_uuid, [item.uuid for item in logic_b.agreements()])
            self.assertIn(clause_uuid, session_b.protocol.index)

            updated = logic_a.update_clause(clause_uuid, "Updated through mailbox")
            self.assertEqual(updated.status, "ok")
            relay_a.publish_due_topics()
            self.assertIn((agreement_uuid, "A"), relay_b.poll_and_apply())
            events = logic_b.transition_events(agreement_uuid)
            self.assertTrue(any(
                event["node_uuid"] == clause_uuid
                and event["type"] != "in_agreement"
                for event in events
            ))
            adopted = logic_b.adopt_peer_changes("relay:A", agreement_uuid)
            self.assertTrue(adopted.value)
            self.assertEqual(
                session_b.protocol.index[clause_uuid].data["text"],
                "Updated through mailbox",
            )

    @staticmethod
    def relay_config(relay_root: str, identity: str, state_dir: str) -> dict:
        return {
            "relay_root": relay_root,
            "relay_identity": identity,
            "relay_state_file": str(Path(state_dir) / f"state-{identity}.json"),
        }

    @staticmethod
    def runtime(port: int):
        directory = tempfile.TemporaryDirectory()
        config = app_server.load_config(None, "agreement", {
            "agreement": {
                "app_module": "s_agreement.application",
                "application_id": "agreement",
                "asset_package": "s_agreement.assets",
                "ui_file": "agreement.html",
                "css_file": "agreement.css",
            },
        })
        config["storage_file"] = str(Path(directory.name) / f"{port}.json")
        runtime = app_server.create_runtime(port, config)
        runtime._test_tmp = directory
        return runtime


if __name__ == "__main__":
    unittest.main()
