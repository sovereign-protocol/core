"""Two clients of one user, publishing under one identity.

The design is DESIGN_MULTI_CLIENT_PAIRING.md. Its whole content is one rule,
applied per topic from two local facts and one relay read:

    relay == published                          nothing happened
    relay != published, current == published    the sibling built on mine
    relay != published, current != published    alarm

These tests are written as the two clients, not as the machinery.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from sovereign import app_server
from sovereign.protocol import ProtocolNode
from sovereign.relay_logic import RelayLogic
from sovereign.session import Session
from sovereign.topic_registry import ApplicationRegistration


def register_notes_app(session: Session) -> list[ProtocolNode]:
    """A stand-in application, listing its topics the way a real one does.

    Real applications enumerate their own tree - S-Kanban's `boards()` walks
    its container - so a topic that arrives by sync is listed without anyone
    registering it. A stub that only returns what it was handed hides that,
    and a grafted topic then silently never publishes.
    """
    extra: list[ProtocolNode] = []

    def list_topics() -> list[ProtocolNode]:
        found = {}
        pending = [session.protocol.root]
        while pending:
            node = pending.pop()
            if node.data.get("type") == "notes" and not node.deleted:
                found[node.uuid] = node
            pending.extend(node.children)
        for node in extra:
            found.setdefault(node.uuid, node)
        return list(found.values())

    session.register_application(ApplicationRegistration(
        application_id="notes",
        root_types=frozenset({"notes"}),
        list_topics=list_topics,
        accept_invitation=session.accept_topic_invitation,
        assignment_scoped=True,
        mount_invitation=True,
    ))
    return extra


class Client:
    """One client: its own session, its own state file, a shared identity."""

    def __init__(self, relay_root: str, state_dir: str, name: str,
                 identity: str = "USER"):
        self.session = Session(f"addr-{name}")
        self.topics = register_notes_app(self.session)
        self.relay = RelayLogic(self.session, {
            "relay_root": relay_root,
            "relay_identity": identity,
            "relay_state_file": str(Path(state_dir) / f"state-{name}.json"),
        })

    def own(self, topic: ProtocolNode) -> None:
        """Adopt a topic uuid this client did not create itself."""
        self.topics.append(topic)
        self.relay.set_scoped_topics({node.uuid for node in self.topics})
        self.relay.mark_topics_desired([topic.uuid])

    def create(self, name: str) -> ProtocolNode:
        topic = self.session.create_child(
            self.session.root_uuid(), {"type": "notes", "name": name}, {},
        ).value
        self.own(topic)
        return topic

    def note(self, topic_uuid: str, name: str, text: str) -> ProtocolNode:
        return self.session.create_child(
            topic_uuid, {"type": "note", "name": name, "text": text}, {},
        ).value

    def edit(self, node_uuid: str, text: str) -> None:
        node = self.session.protocol.index[node_uuid]
        data = dict(node.data)
        data["text"] = text
        self.session.modify(node_uuid, data, node.weights)

    def tick(self) -> None:
        """One channel cycle, in the order app_server runs it: poll, then
        publish. Publishing first is what section 4.1 forbids."""
        self.relay.write_presence()
        self.relay.poll_and_apply()
        self.relay.publish_due_topics()

    def texts(self, topic_uuid: str) -> dict:
        topic = self.session.protocol.index.get(topic_uuid)
        return {
            child.data.get("name"): child.data.get("text")
            for child in topic.live_children()
        } if topic else {}


class SiblingClientTests(unittest.TestCase):
    def paired(self, relay_root, state_dir):
        """Two clients on one identity, both holding the same topic."""
        desktop = Client(relay_root, state_dir, "desktop")
        topic = desktop.create("plan")
        desktop.note(topic.uuid, "first", "written on the desktop")
        desktop.tick()

        laptop = Client(relay_root, state_dir, "laptop")
        laptop.own(topic)
        laptop.tick()
        return desktop, laptop, topic

    def test_a_second_client_receives_the_topic(self):
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            _desktop, laptop, topic = self.paired(relay_root, state_dir)

            self.assertEqual(
                laptop.texts(topic.uuid), {"first": "written on the desktop"},
            )

    def test_work_on_one_client_reaches_the_other(self):
        # The ordinary case, and the one that must never ask a question: the
        # laptop published everything before the desktop touched anything.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop, laptop, topic = self.paired(relay_root, state_dir)

            desktop.note(topic.uuid, "second", "added at home")
            desktop.tick()
            laptop.tick()

            self.assertEqual(laptop.texts(topic.uuid), {
                "first": "written on the desktop",
                "second": "added at home",
            })
            self.assertEqual(laptop.relay.sibling_alarm_topics(), [])

    def test_a_deletion_on_one_client_reaches_the_other(self):
        # reconcile_peer_changes leaves absences alone, deliberately, because
        # a peer's deletion is a separate decision. For a sibling it is not.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop, laptop, topic = self.paired(relay_root, state_dir)
            gone = desktop.note(topic.uuid, "second", "added at home")
            desktop.tick()
            laptop.tick()
            self.assertIn("second", laptop.texts(topic.uuid))

            desktop.session.delete(gone.uuid)
            desktop.tick()
            laptop.tick()

            self.assertEqual(
                laptop.texts(topic.uuid), {"first": "written on the desktop"},
            )

    def test_unpublished_work_raises_the_alarm_instead_of_being_overwritten(self):
        # The plane: the laptop edits offline, the desktop edits at home, and
        # the laptop is opened in the office. Neither version descends from
        # the other and nobody but the person can say which matters.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop, laptop, topic = self.paired(relay_root, state_dir)
            note = laptop.texts(topic.uuid)
            self.assertEqual(note, {"first": "written on the desktop"})
            first_uuid = next(
                child.uuid for child
                in laptop.session.protocol.index[topic.uuid].live_children()
            )

            laptop.edit(first_uuid, "edited on the plane")   # never published
            desktop.edit(first_uuid, "edited at home")
            desktop.tick()

            laptop.tick()

            self.assertEqual(
                laptop.relay.sibling_alarm_topics(), [topic.uuid],
            )
            self.assertEqual(
                laptop.texts(topic.uuid), {"first": "edited on the plane"},
            )

    def test_the_alarm_stops_the_client_publishing_over_the_sibling(self):
        # Without this the alarm would be cosmetic: the next tick would
        # overwrite the very state the person is being asked about.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop, laptop, topic = self.paired(relay_root, state_dir)
            first_uuid = next(
                child.uuid for child
                in laptop.session.protocol.index[topic.uuid].live_children()
            )
            laptop.edit(first_uuid, "edited on the plane")
            desktop.edit(first_uuid, "edited at home")
            desktop.tick()

            laptop.tick()
            laptop.tick()
            desktop.tick()

            self.assertEqual(
                desktop.texts(topic.uuid), {"first": "edited at home"},
            )

    def test_taking_the_siblings_version_resolves_the_alarm(self):
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop, laptop, topic = self.paired(relay_root, state_dir)
            first_uuid = next(
                child.uuid for child
                in laptop.session.protocol.index[topic.uuid].live_children()
            )
            laptop.edit(first_uuid, "edited on the plane")
            desktop.edit(first_uuid, "edited at home")
            desktop.tick()
            laptop.tick()
            self.assertEqual(laptop.relay.sibling_alarm_topics(), [topic.uuid])

            laptop.relay.take_sibling_version(topic.uuid)

            self.assertEqual(
                laptop.texts(topic.uuid), {"first": "edited at home"},
            )
            self.assertEqual(laptop.relay.sibling_alarm_topics(), [])

    def test_keeping_this_clients_version_resolves_the_alarm(self):
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop, laptop, topic = self.paired(relay_root, state_dir)
            first_uuid = next(
                child.uuid for child
                in laptop.session.protocol.index[topic.uuid].live_children()
            )
            laptop.edit(first_uuid, "edited on the plane")
            desktop.edit(first_uuid, "edited at home")
            desktop.tick()
            laptop.tick()

            laptop.relay.keep_local_version(topic.uuid)
            laptop.tick()
            desktop.tick()

            self.assertEqual(laptop.relay.sibling_alarm_topics(), [])
            self.assertEqual(
                desktop.texts(topic.uuid), {"first": "edited on the plane"},
            )

    def test_the_alarm_is_offered_with_a_name_and_a_file_to_copy(self):
        # "One of your topics is in conflict" is not something a person can
        # act on, and taking the sibling's version is unrecoverable unless
        # they know which file to copy first.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop, laptop, topic = self.paired(relay_root, state_dir)
            first_uuid = next(
                child.uuid for child
                in laptop.session.protocol.index[topic.uuid].live_children()
            )
            laptop.edit(first_uuid, "edited on the plane")
            desktop.edit(first_uuid, "edited at home")
            desktop.tick()
            laptop.tick()

            alarms = laptop.relay.sibling_alarm_topics()
            described = [
                {
                    "topic_uuid": item,
                    "title": laptop.session.get_node(item).data.get("name"),
                }
                for item in alarms
            ]

            self.assertEqual(described, [
                {"topic_uuid": topic.uuid, "title": "plan"},
            ])

    def test_identical_content_is_never_an_alarm(self):
        # Caught by running it: both clients hold the account profile
        # identically from the moment they pair, but the client that never
        # recorded publishing it saw "relay != published, current !=
        # published" and alarmed over content that was already the same on
        # both sides - and then stopped syncing that topic.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop, laptop, topic = self.paired(relay_root, state_dir)
            relay = laptop.relay

            # Exactly the state the bug needed: the slot holds what this
            # client holds, and the local record of publishing it is gone.
            relay._state["published"].pop(topic.uuid, None)
            relay.poll_and_apply()

            self.assertEqual(relay.sibling_alarm_topics(), [])
            self.assertEqual(
                laptop.texts(topic.uuid), {"first": "written on the desktop"},
            )

    def test_a_sibling_is_not_a_peer(self):
        # The whole reason siblings cache under their own address prefix. A
        # client of mine showing up as a participant would be wrong in the
        # Sharing pane and worse in prune_deleted_nodes, where it would hold
        # a vote on my own deletions.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop, laptop, topic = self.paired(relay_root, state_dir)
            desktop.note(topic.uuid, "second", "added at home")
            desktop.tick()
            laptop.tick()

            network = laptop.session.get_network_info()

            self.assertEqual(network["peers"], {})
            self.assertEqual(network["peer_addresses"], [])
            self.assertEqual(laptop.session.peers_for_topic(topic.uuid), [])
            self.assertEqual(
                [item["address"] for item in laptop.session.known_identities()],
                [laptop.session.address],
            )


class PairingTokenTests(unittest.TestCase):
    """A second client that starts knowing nothing."""

    def runtime(self, port, name, state_dir, relay_root=None):
        directory = tempfile.TemporaryDirectory()
        config = {
            "applications": [],
            "storage_file": str(Path(directory.name) / f"{name}.json"),
            "relay_state_file": str(Path(state_dir) / f"state-{name}.json"),
        }
        if relay_root:
            config["relay_root"] = relay_root
            config["relay_identity"] = "USER"
        runtime = app_server.create_runtime(port, config)
        runtime._test_tmp = directory
        runtime._topics = register_notes_app(runtime.session)
        return runtime

    def test_a_paired_client_takes_the_identity_channel_and_topics(self):
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop = self.runtime(8811, "desktop", state_dir, relay_root)
            topic = desktop.session.create_child(
                desktop.session.root_uuid(),
                {"type": "notes", "name": "plan"}, {},
            ).value
            desktop._topics.append(topic)
            desktop.relay_manager.primary.set_scoped_topics({topic.uuid})
            desktop.relay_manager.primary.publish_due_topics()
            # A client with no peers is deliberately idle - it writes nothing
            # for nobody - so issuing the token has to arm it, or the sibling
            # arrives to an empty slot and never receives anything.
            self.assertFalse(
                desktop.relay_manager.primary.has_active_relationship(),
            )

            token = desktop.collaboration.compose_pairing_token()
            self.assertTrue(token.ok)
            self.assertTrue(
                desktop.relay_manager.primary.has_active_relationship(),
            )

            laptop = self.runtime(8812, "laptop", state_dir)
            self.assertNotEqual(
                laptop.session.identity.uuid, desktop.session.identity.uuid,
            )

            accepted = laptop.collaboration.accept_pairing_token(token.value)

            self.assertTrue(accepted.ok, getattr(accepted, "reason", ""))
            self.assertEqual(
                laptop.relay_manager.primary.identity,
                desktop.relay_manager.primary.identity,
            )
            self.assertEqual(
                laptop.session.identity.uuid, desktop.session.identity.uuid,
            )
            self.assertIn(
                topic.uuid, laptop.relay_manager.primary._state["desired"],
            )

    def test_a_paired_client_then_receives_the_content(self):
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop = self.runtime(8813, "desktop", state_dir, relay_root)
            topic = desktop.session.create_child(
                desktop.session.root_uuid(),
                {"type": "notes", "name": "plan"}, {},
            ).value
            desktop._topics.append(topic)
            desktop.session.create_child(
                topic.uuid, {"type": "note", "name": "first", "text": "start"}, {},
            )
            desktop.relay_manager.primary.set_scoped_topics({topic.uuid})
            desktop.relay_manager.primary.publish_due_topics()
            token = desktop.collaboration.compose_pairing_token().value

            laptop = self.runtime(8814, "laptop", state_dir)
            laptop.collaboration.accept_pairing_token(token)
            laptop.relay_manager.primary.poll_and_apply()

            adopted = laptop.session.protocol.index.get(topic.uuid)
            self.assertIsNotNone(adopted)
            self.assertEqual(
                [child.data["text"] for child in adopted.live_children()],
                ["start"],
            )

    def test_work_flows_back_to_the_client_that_issued_the_token(self):
        # Caught by running it: the issuer has no desired topics and no
        # target assignments, so its poll enumerated nothing. It published
        # to its sibling and never looked at its own slot - sync ran one way
        # only, and edits made on the second client never came home.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop = self.runtime(8825, "desktop", state_dir, relay_root)
            topic = desktop.session.create_child(
                desktop.session.root_uuid(),
                {"type": "notes", "name": "plan"}, {},
            ).value
            desktop._topics.append(topic)
            note = desktop.session.create_child(
                topic.uuid, {"type": "note", "name": "first", "text": "start"}, {},
            ).value
            desktop.relay_manager.primary.set_scoped_topics(set())
            token = desktop.collaboration.compose_pairing_token().value
            desktop.relay_manager.primary.publish_due_topics()

            laptop = self.runtime(8826, "laptop", state_dir)
            laptop.collaboration.accept_pairing_token(token)
            laptop.relay_manager.primary.poll_and_apply()

            held = laptop.session.protocol.index[note.uuid]
            laptop.session.modify(
                note.uuid, {**held.data, "text": "edited on the laptop"},
                held.weights,
            )
            laptop.relay_manager.primary.publish_due_topics()

            desktop.relay_manager.primary.poll_and_apply()

            self.assertEqual(
                desktop.session.protocol.index[note.uuid].data["text"],
                "edited on the laptop",
            )

    def test_a_take_is_reported_so_the_cycle_persists_it(self):
        # Caught by running it, and the worst failure found: the take was
        # applied in memory but reported nothing, so the tick never persisted
        # the session. The content survived until the process ended, and the
        # client then published the state it had reverted to over the
        # sibling's - losing the work on both sides at once.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop = self.runtime(8821, "desktop", state_dir, relay_root)
            topic = desktop.session.create_child(
                desktop.session.root_uuid(),
                {"type": "notes", "name": "plan"}, {},
            ).value
            desktop._topics.append(topic)
            desktop.session.create_child(
                topic.uuid, {"type": "note", "name": "first", "text": "start"}, {},
            )
            token = desktop.collaboration.compose_pairing_token().value
            desktop.relay_manager.primary.publish_due_topics()

            laptop = self.runtime(8822, "laptop", state_dir)
            laptop.collaboration.accept_pairing_token(token)

            applied = laptop.relay_manager.primary.poll_and_apply()

            self.assertTrue(
                applied,
                "the take reported nothing, so nothing would be persisted",
            )

    def test_a_taken_topic_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop = self.runtime(8823, "desktop", state_dir, relay_root)
            topic = desktop.session.create_child(
                desktop.session.root_uuid(),
                {"type": "notes", "name": "plan"}, {},
            ).value
            desktop._topics.append(topic)
            desktop.session.create_child(
                topic.uuid, {"type": "note", "name": "first", "text": "start"}, {},
            )
            token = desktop.collaboration.compose_pairing_token().value
            desktop.relay_manager.primary.publish_due_topics()

            laptop = self.runtime(8824, "laptop", state_dir)
            laptop.collaboration.accept_pairing_token(token)
            storage_file = laptop.config["storage_file"]
            # The real cycle, not a hand-written save: persistence is the
            # tick's job and only happens when the cycle reports work, which
            # is exactly what was missing.
            asyncio.run(app_server.channel_poll_tick(laptop))

            restarted = app_server.create_runtime(8824, {
                "applications": [],
                "storage_file": storage_file,
                "relay_state_file": str(Path(state_dir) / "state-laptop.json"),
            })
            register_notes_app(restarted.session)

            adopted = restarted.session.protocol.index.get(topic.uuid)
            self.assertIsNotNone(adopted, "the taken topic did not survive")
            self.assertEqual(
                [child.data["text"] for child in adopted.live_children()],
                ["start"],
            )

    def test_the_paired_client_id_survives_a_restart(self):
        # Caught by running it: the id was set in memory only, so the second
        # client came back as a *peer* of its siblings - publishing into a
        # slot of its own beside theirs. Everything still looked healthy;
        # the two simply stopped being the same client.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop = self.runtime(8819, "desktop", state_dir, relay_root)
            topic = desktop.session.create_child(
                desktop.session.root_uuid(),
                {"type": "notes", "name": "plan"}, {},
            ).value
            desktop._topics.append(topic)
            token = desktop.collaboration.compose_pairing_token().value

            laptop = self.runtime(8820, "laptop", state_dir)
            laptop.collaboration.accept_pairing_token(token)
            storage_file = laptop.config["storage_file"]
            app_server.save_session_to_file(laptop.session, storage_file)

            restarted = app_server.create_runtime(8820, {
                "applications": [],
                "storage_file": storage_file,
                "relay_state_file": str(Path(state_dir) / "state-laptop.json"),
            })

            self.assertEqual(
                restarted.relay_manager.primary.identity,
                desktop.relay_manager.primary.identity,
            )

    def test_pairing_publishes_topics_no_relay_target_was_assigned(self):
        # Caught by running it: target assignment scopes what a *peer* may be
        # shown, and an unassigned board is scoped out of publishing. A
        # sibling is not a peer - it needs the whole environment - so the
        # token promised every topic while the slot received only the
        # profile, and the second client stayed permanently empty.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop = self.runtime(8818, "desktop", state_dir, relay_root)
            topic = desktop.session.create_child(
                desktop.session.root_uuid(),
                {"type": "notes", "name": "plan"}, {},
            ).value
            desktop._topics.append(topic)
            # No target assignment at all, which is the state of any board
            # the person never explicitly put on a relay.
            desktop.relay_manager.primary.set_scoped_topics(set())
            self.assertNotIn(
                topic.uuid, desktop.relay_manager.primary.relay_topic_uuids(),
            )

            desktop.collaboration.compose_pairing_token()

            self.assertIn(
                topic.uuid, desktop.relay_manager.primary.relay_topic_uuids(),
            )
            desktop.relay_manager.primary.publish_due_topics()
            self.assertEqual(
                desktop.relay_manager.primary.storage.list_peers(topic.uuid),
                ["USER"],
            )

    def test_the_peer_path_refuses_a_pairing_token(self):
        # The dangerous confusion: accepted as a connect token it registers
        # the user's own client as another person, and the reconnect-replace
        # loop then unbinds the first client from the covered topics.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop = self.runtime(8815, "desktop", state_dir, relay_root)
            topic = desktop.session.create_child(
                desktop.session.root_uuid(),
                {"type": "notes", "name": "plan"}, {},
            ).value
            desktop._topics.append(topic)
            desktop.relay_manager.primary.set_scoped_topics({topic.uuid})
            token = desktop.collaboration.compose_pairing_token().value

            laptop = self.runtime(8816, "laptop", state_dir)
            refused = laptop.collaboration.accept_invitation(token)

            self.assertFalse(refused.ok)
            self.assertIn("pairing token", refused.reason)

    def test_pairing_works_when_the_relay_is_a_configured_target(self):
        # How the relay is actually set up in the application: added through
        # Manage channels, which registers a *target* and gives it its own
        # connection. The primary connection only ever has storage when the
        # process was started with relay_root in a config file, which the
        # packaged executable never is - so looking only at primary reported
        # "no relay channel to pair over" to every real user.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            client = self.runtime(8828, "client", state_dir)
            self.assertIsNone(client.relay_manager.primary.storage)

            created = client.relay_manager.create_target({
                "backend": "local",
                "name": "My relay",
                "root": relay_root,
            })
            self.assertEqual(created.status, "ok", created.reason)

            token = client.collaboration.compose_pairing_token()

            self.assertTrue(token.ok, getattr(token, "reason", ""))
            self.assertEqual(token.value["token_kind"], "pairing")
            self.assertEqual(token.value["channel"]["root"], relay_root)

    def test_a_target_configured_relay_pairs_end_to_end(self):
        # The whole flow the way the application sets it up: relay added as a
        # target on the first client, token pasted into a second one that has
        # nothing at all.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop = self.runtime(8829, "desktop", state_dir)
            desktop.relay_manager.create_target({
                "backend": "local", "name": "My relay", "root": relay_root,
            })
            topic = desktop.session.create_child(
                desktop.session.root_uuid(),
                {"type": "notes", "name": "plan"}, {},
            ).value
            desktop._topics.append(topic)
            desktop.session.create_child(
                topic.uuid, {"type": "note", "name": "first", "text": "start"}, {},
            )
            token = desktop.collaboration.compose_pairing_token()
            self.assertTrue(token.ok, getattr(token, "reason", ""))
            paired = desktop.relay_manager._pairing_connection()
            paired.publish_due_topics()

            laptop = self.runtime(8830, "laptop", state_dir)
            accepted = laptop.collaboration.accept_pairing_token(token.value)
            self.assertTrue(accepted.ok, getattr(accepted, "reason", ""))
            laptop.relay_manager.primary.poll_and_apply()

            adopted = laptop.session.protocol.index.get(topic.uuid)
            self.assertIsNotNone(adopted)
            self.assertEqual(
                [child.data["text"] for child in adopted.live_children()],
                ["start"],
            )

    def test_the_shell_offers_pairing_and_routes_a_pasted_token(self):
        # The pane is one paste field for two kinds of token, so the marker
        # the shell branches on has to be the one the server sets, and the
        # button has to exist to produce it.
        shared_js = (
            Path(__file__).resolve().parents[1]
            / "src" / "sovereign" / "assets" / "shared.js"
        ).read_text(encoding="utf-8")

        self.assertIn("shellPairClientBtn", shared_js)
        self.assertIn("/api/core/siblings/pairing", shared_js)
        self.assertIn("/api/core/siblings/pairing/accept", shared_js)
        self.assertIn('token.token_kind === "pairing"', shared_js)

        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            desktop = self.runtime(8827, "desktop", state_dir, relay_root)
            token = desktop.collaboration.compose_pairing_token().value
            self.assertEqual(token["token_kind"], "pairing")

    def test_the_pairing_path_refuses_a_connection_token(self):
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            client = self.runtime(8817, "client", state_dir, relay_root)

            refused = client.collaboration.accept_pairing_token(
                {"token_version": 1, "identity": {}, "topic_uuids": []},
            )

            self.assertFalse(refused.ok)
            self.assertIn("connection token", refused.reason)


class SiblingAlarmOverTheServiceTests(unittest.TestCase):
    """The layer the HTTP route actually calls."""

    def runtime(self, relay_root, state_dir, port, name):
        directory = tempfile.TemporaryDirectory()
        runtime = app_server.create_runtime(port, {
            "applications": [],
            "storage_file": str(Path(directory.name) / f"{name}.json"),
            "relay_root": relay_root,
            "relay_identity": "USER",
            "relay_state_file": str(Path(state_dir) / f"state-{name}.json"),
        })
        runtime._test_tmp = directory
        runtime._topics = register_notes_app(runtime.session)
        return runtime

    def relay_of(self, runtime):
        return runtime.relay_manager.primary

    def tick(self, runtime):
        relay = self.relay_of(runtime)
        relay.write_presence()
        relay.poll_and_apply()
        relay.publish_due_topics()

    def diverged_pair(self, relay_root, state_dir):
        desktop = self.runtime(relay_root, state_dir, 8801, "desktop")
        topic = desktop.session.create_child(
            desktop.session.root_uuid(), {"type": "notes", "name": "plan"}, {},
        ).value
        desktop._topics.append(topic)
        note = desktop.session.create_child(
            topic.uuid, {"type": "note", "name": "first", "text": "start"}, {},
        ).value
        self.relay_of(desktop).set_scoped_topics({topic.uuid})
        self.tick(desktop)

        laptop = self.runtime(relay_root, state_dir, 8802, "laptop")
        laptop._topics.append(topic)
        self.relay_of(laptop).set_scoped_topics({topic.uuid})
        self.relay_of(laptop).mark_topics_desired([topic.uuid])
        self.tick(laptop)

        def rewrite(runtime, text):
            node = runtime.session.protocol.index[note.uuid]
            runtime.session.modify(
                note.uuid, {**node.data, "text": text}, node.weights,
            )

        rewrite(laptop, "edited on the plane")   # never published
        rewrite(desktop, "edited at home")
        self.tick(desktop)
        self.tick(laptop)
        return desktop, laptop, topic, note

    def test_the_service_reports_the_alarm_with_its_title(self):
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            _desktop, laptop, topic, _note = self.diverged_pair(
                relay_root, state_dir,
            )

            payload = laptop.collaboration.sibling_alarms_payload()

            self.assertEqual(payload["status"], "ok")
            self.assertEqual(
                [(item["topic_uuid"], item["title"]) for item in payload["alarms"]],
                [(topic.uuid, "plan")],
            )

    def test_the_service_carries_out_both_decisions(self):
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            _desktop, laptop, topic, note = self.diverged_pair(
                relay_root, state_dir,
            )

            refused = laptop.collaboration.resolve_sibling_alarm(
                topic.uuid, "something_else",
            )
            self.assertFalse(refused.ok)

            taken = laptop.collaboration.resolve_sibling_alarm(
                topic.uuid, "take_sibling",
            )

            self.assertTrue(taken.ok)
            self.assertEqual(
                laptop.session.protocol.index[note.uuid].data["text"],
                "edited at home",
            )
            self.assertEqual(
                laptop.collaboration.sibling_alarms_payload()["alarms"], [],
            )


if __name__ == "__main__":
    unittest.main()
