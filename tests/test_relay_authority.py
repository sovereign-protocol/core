"""The relay decides what the relay holds.

Two behaviours, one principle: a peer must not believe its own persisted
record of what is on a relay over what the relay currently reports. A relay
can be wiped, rotated, moved, or restored from an older backup, and none of
those events reach the peers that publish into it.
"""

import tempfile
import unittest
from pathlib import Path

from sovereign.protocol import ProtocolNode
from sovereign.relay_logic import RelayLogic
from sovereign.session import Session
from sovereign.topic_registry import ApplicationRegistration


def register_notes_app(session: Session) -> list[ProtocolNode]:
    """Register a topic type without depending on a real application.

    Both sides need this: the publisher so its topic is publishable at all,
    and the receiver so the arriving subtree has a handler and is grafted
    rather than parked as a pending invitation. Without the graft the topic
    is not in the receiver's own tree, and peer_discusses_node - which the
    Sharing pane's view is filtered by - answers False for every peer.
    """
    topics: list[ProtocolNode] = []
    session.register_application(ApplicationRegistration(
        application_id="notes",
        root_types=frozenset({"notes"}),
        list_topics=lambda: list(topics),
        accept_invitation=session.accept_topic_invitation,
        assignment_scoped=True,
        mount_invitation=True,
    ))
    return topics


def register_topic(session: Session, name: str) -> ProtocolNode:
    topics = register_notes_app(session)
    topic = session.create_child(
        session.root_uuid(), {"type": "notes", "name": name}, {},
    ).value
    topics.append(topic)
    return topic


def relay_config(relay_root: str, identity: str, state_dir: str) -> dict:
    return {
        "relay_root": relay_root,
        "relay_identity": identity,
        "relay_state_file": str(Path(state_dir) / f"state-{identity}.json"),
    }


def wipe(relay_root: str) -> None:
    """Everything a relay holds, gone - the case the peers cannot observe."""
    for child in sorted(Path(relay_root).iterdir()):
        if child.is_dir():
            for path in sorted(child.rglob("*"), reverse=True):
                path.rmdir() if path.is_dir() else path.unlink()
            child.rmdir()
        else:
            child.unlink()


class RepublishesAfterTheRelayLosesItTests(unittest.TestCase):
    def test_a_wiped_relay_is_republished_without_any_local_change(self):
        # The failure this replaces: `published` claimed the snapshot was
        # already on the server, so every peer stayed silent and the relay
        # carried no content until somebody happened to make an edit.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            session = Session("addr-a")
            topic = register_topic(session, "plan")
            relay = RelayLogic(session, relay_config(relay_root, "A", state_dir))
            relay.set_scoped_topics({topic.uuid})
            self.assertIn(topic.uuid, relay.publish_due_topics())
            self.assertEqual(relay.storage.list_peers(topic.uuid), ["A"])

            wipe(relay_root)
            self.assertEqual(relay.storage.list_peers(topic.uuid), [])

            self.assertIn(topic.uuid, relay.publish_due_topics())
            self.assertEqual(relay.storage.list_peers(topic.uuid), ["A"])
            self.assertIsNotNone(relay.storage.read_head(topic.uuid, "A"))

    def test_a_peer_arriving_after_a_wipe_still_receives_the_topic(self):
        # The point of republishing: the relay is a mailbox, so content that
        # is not on it cannot be found by anyone who was not already synced.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            session_a = Session("addr-a")
            topic = register_topic(session_a, "plan")
            relay_a = RelayLogic(
                session_a, relay_config(relay_root, "A", state_dir),
            )
            relay_a.set_scoped_topics({topic.uuid})
            relay_a.publish_due_topics()

            wipe(relay_root)
            relay_a.publish_due_topics()

            session_b = Session("addr-b")
            register_notes_app(session_b)
            relay_b = RelayLogic(
                session_b, relay_config(relay_root, "B", state_dir),
            )
            relay_b.set_scoped_topics({topic.uuid})
            relay_b.mark_topics_desired([topic.uuid])

            self.assertIn((topic.uuid, "A"), relay_b.poll_and_apply())

    def test_an_intact_relay_is_not_republished(self):
        # The check must not turn every tick into a write; the skip is what
        # keeps an idle topic quiet.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            session = Session("addr-a")
            topic = register_topic(session, "plan")
            relay = RelayLogic(session, relay_config(relay_root, "A", state_dir))
            relay.set_scoped_topics({topic.uuid})
            relay.publish_due_topics()

            self.assertNotIn(topic.uuid, relay.publish_due_topics())

    def test_an_unreachable_relay_is_not_treated_as_having_lost_it(self):
        # Absence of evidence only. Republishing the world on every transient
        # listing error would be its own harm.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            session = Session("addr-a")
            traced = []
            session.trace_event = lambda kind, **fields: traced.append(kind)
            topic = register_topic(session, "plan")
            relay = RelayLogic(session, relay_config(relay_root, "A", state_dir))
            relay.set_scoped_topics({topic.uuid})
            relay.publish_due_topics()

            def unreachable(_topic_uuid):
                raise OSError("relay is not reachable")

            relay.storage.list_peers = unreachable

            self.assertNotIn(topic.uuid, relay.publish_due_topics())
            self.assertIn("relay.publication_presence_unknown", traced)


class ForgetsPeersTheRelayNoLongerListsTests(unittest.TestCase):
    def peers_on_topic(self, session: Session, topic_uuid: str) -> list[str]:
        """Who the Sharing pane would show: network info is built from the
        cached perspectives, so this is the view under test."""
        return sorted(
            addr for addr in (session.get_network_info()["peers"] or {})
            if session.peer_discusses_node(addr, topic_uuid)
        )

    def connect(self, relay_root, state_dir):
        session_a = Session("addr-a")
        topic = register_topic(session_a, "plan")
        relay_a = RelayLogic(session_a, relay_config(relay_root, "A", state_dir))
        relay_a.set_scoped_topics({topic.uuid})
        relay_a.publish_due_topics()

        session_b = Session("addr-b")
        register_notes_app(session_b)
        relay_b = RelayLogic(session_b, relay_config(relay_root, "B", state_dir))
        relay_b.set_scoped_topics({topic.uuid})
        relay_b.mark_topics_desired([topic.uuid])
        relay_b.poll_and_apply()
        return topic, session_b, relay_b

    def remove_from_relay(self, relay_root: str, topic_uuid: str,
                          peer_id: str) -> None:
        peer_dir = (
            Path(relay_root) / "topics" / topic_uuid / "peers" / peer_id
        )
        for path in sorted(peer_dir.rglob("*"), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        peer_dir.rmdir()

    def test_a_peer_removed_from_the_relay_leaves_the_connection_view(self):
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            topic, session_b, relay_b = self.connect(relay_root, state_dir)
            self.assertEqual(
                self.peers_on_topic(session_b, topic.uuid), ["relay:A"],
            )

            self.remove_from_relay(relay_root, topic.uuid, "A")
            relay_b.poll_and_apply()

            self.assertEqual(self.peers_on_topic(session_b, topic.uuid), [])
            self.assertIsNone(
                session_b.get_cached_peer_subtree("relay:A", topic.uuid),
            )

    def test_the_relationship_survives_so_the_deletion_quorum_is_intact(self):
        # The distinction the whole change rests on: what a peer publishes is
        # a view, but whether it is party to the topic is not. Dropping the
        # relationship would remove its vote in prune_deleted_nodes, letting a
        # deletion be pruned while it is merely quiet - and then re-proposed
        # as a new node when it returns.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            topic, session_b, relay_b = self.connect(relay_root, state_dir)

            self.remove_from_relay(relay_root, topic.uuid, "A")
            relay_b.poll_and_apply()

            self.assertIn("relay:A", session_b.peers_for_topic(topic.uuid))

    def test_a_returning_peer_is_seen_again(self):
        # Refilling the cache requires `applied` to have been cleared with it:
        # the returning hash is unchanged, so a surviving "already applied"
        # record would skip the very re-fetch that repopulates the cache.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            session_a = Session("addr-a")
            topic = register_topic(session_a, "plan")
            relay_a = RelayLogic(
                session_a, relay_config(relay_root, "A", state_dir),
            )
            relay_a.set_scoped_topics({topic.uuid})
            relay_a.publish_due_topics()

            session_b = Session("addr-b")
            register_notes_app(session_b)
            relay_b = RelayLogic(
                session_b, relay_config(relay_root, "B", state_dir),
            )
            relay_b.set_scoped_topics({topic.uuid})
            relay_b.mark_topics_desired([topic.uuid])
            relay_b.poll_and_apply()

            self.remove_from_relay(relay_root, topic.uuid, "A")
            relay_b.poll_and_apply()
            self.assertEqual(self.peers_on_topic(session_b, topic.uuid), [])

            relay_a.publish_due_topics()
            relay_b.poll_and_apply()

            self.assertEqual(
                self.peers_on_topic(session_b, topic.uuid), ["relay:A"],
            )
            self.assertIsNotNone(
                session_b.get_cached_peer_subtree("relay:A", topic.uuid),
            )

    def test_a_peer_that_is_still_listed_is_left_alone(self):
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            topic, session_b, relay_b = self.connect(relay_root, state_dir)

            relay_b.poll_and_apply()
            relay_b.poll_and_apply()

            self.assertEqual(
                self.peers_on_topic(session_b, topic.uuid), ["relay:A"],
            )

    def test_the_first_poll_after_start_up_forgets_nobody(self):
        # There is no earlier observation to have departed from, and the
        # cache does not survive a restart in the first place.
        with tempfile.TemporaryDirectory() as relay_root, \
                tempfile.TemporaryDirectory() as state_dir:
            topic, _session_b, _relay_b = self.connect(relay_root, state_dir)

            session_c = Session("addr-c")
            register_notes_app(session_c)
            relay_c = RelayLogic(
                session_c, relay_config(relay_root, "C", state_dir),
            )
            relay_c.set_scoped_topics({topic.uuid})
            relay_c.mark_topics_desired([topic.uuid])

            relay_c.poll_and_apply()

            self.assertEqual(
                self.peers_on_topic(session_c, topic.uuid), ["relay:A"],
            )


if __name__ == "__main__":
    unittest.main()
