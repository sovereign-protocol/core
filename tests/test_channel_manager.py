import threading
import unittest

from sovereign.channel import (
    BlobChannel,
    Channel,
    ChannelAcceptance,
    ChannelManager,
    ChannelResult,
    Invitation,
    LivenessChannel,
    ManagedChannel,
    PairingChannel,
    PollCycleResult,
    PollingChannel,
    PollingEndpoint,
)
from sovereign.collaboration import CollaborationService
from sovereign.locking import RELAY_IO_LOCK_ORDER, OrderedRLock
from sovereign.mailbox_channel import MailboxChannel
from sovereign.relay_storage import LocalFolderRelayStorage, SftpRelayStorage
from sovereign import RelayStorage
from sovereign.session import Session
from sovereign.versions import CHANNEL_DESCRIPTOR_VERSION


class _Channel:
    def __init__(self, kind, descriptor_type, *, offered=None, accept=None):
        self.kind = kind
        self.descriptor_types = frozenset({descriptor_type})
        self.offered = offered
        self.accept_result = accept
        self.accepted = []
        self.blob_reads = []
        self.closed = False

    def offer_descriptor(self, topics, options):
        if options.get("fail"):
            return ChannelResult.error("offer failed")
        return ChannelResult.success(self.offered)

    def accept_descriptor(self, descriptor, invitation):
        self.accepted.append((descriptor, invitation))
        return self.accept_result or ChannelResult.error("unavailable")

    def attach_topics(self, topics, options=None):
        return ChannelResult.success()

    def detach_topics(self, topics):
        return ChannelResult.success()

    def status(self):
        return {"kind": self.kind}

    def close(self):
        self.closed = True

    def read_blob(
        self, blob_id, allow_remote=True, *, peer_addr=None, topic_uuid=None,
    ):
        self.blob_reads.append((blob_id, peer_addr, topic_uuid))
        return self.kind.encode()

    def peer_liveness_for_address(self, peer_addr, topic_uuid=None):
        # Only the channel can say whether a peer is reachable, so a stand-in
        # has to answer too or its peers all read as offline.
        return {"state": "alive"}


class _MinimalThirdPartyChannel:
    """Exactly the required Channel contract, with no optional capabilities."""

    kind = "minimal"
    descriptor_types = frozenset({"minimal"})

    def offer_descriptor(self, topics, options):
        return ChannelResult.success()

    def accept_descriptor(self, descriptor, invitation):
        return ChannelResult.error("not configured")

    def attach_topics(self, topics, options=None):
        return ChannelResult.success()

    def detach_topics(self, topics):
        return ChannelResult.success()

    def status(self):
        return {"configured": False}

    def close(self):
        pass



class _PollingChannel(_Channel):
    class Endpoint:
        poll_interval_seconds = 3

        def has_active_relationship(self):
            return True

        def poll_once(self, after_apply=None):
            return PollCycleResult(ok=True)

        def polling_diagnostics(self):
            return {"identity": "test"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.endpoint = self.Endpoint()

    def polling_endpoints(self):
        return [self.endpoint]


class _CloseableStorage:
    def __init__(self):
        self.close_count = 0

    def close(self):
        self.close_count += 1


class ChannelManagerTests(unittest.TestCase):
    def test_shipped_relay_backends_satisfy_storage_contract(self):
        self.assertIsInstance(LocalFolderRelayStorage("relay"), RelayStorage)
        self.assertIsInstance(
            SftpRelayStorage("host", "user", "/relay"),
            RelayStorage,
        )

    def test_the_shipped_channel_satisfies_its_declared_contracts(self):
        mailbox = MailboxChannel(type("Manager", (), {
            "all_connections": lambda self: [],
        })())

        self.assertIsInstance(mailbox, Channel)
        self.assertIsInstance(mailbox, BlobChannel)
        self.assertIsInstance(mailbox, LivenessChannel)
        self.assertIsInstance(mailbox, ManagedChannel)
        self.assertIsInstance(mailbox, PairingChannel)
        self.assertIsInstance(mailbox, PollingChannel)
        self.assertFalse(hasattr(mailbox, "persistence_lock"))

    def test_mailbox_close_skips_unconfigured_storage_and_is_repeatable(self):
        storage = _CloseableStorage()
        # Closing takes each connection's relay I/O lock, so a stand-in has
        # to carry one exactly as RelayLogic does.
        configured = type("Connection", (), {
            "storage": storage,
            "_io_lock": OrderedRLock(RELAY_IO_LOCK_ORDER, "fake._io_lock"),
        })()
        unconfigured = type("Connection", (), {
            "storage": None,
            "_io_lock": OrderedRLock(RELAY_IO_LOCK_ORDER, "fake._io_lock"),
        })()
        mailbox = MailboxChannel(type("Manager", (), {
            "all_connections": lambda self: [configured, unconfigured],
        })())

        mailbox.close()
        mailbox.close()

        self.assertEqual(storage.close_count, 1)
        self.assertIsNone(configured.storage)

    def test_mailbox_close_waits_for_in_flight_relay_io(self):
        storage = _CloseableStorage()
        connection = type("Connection", (), {
            "storage": storage,
            "_io_lock": threading.RLock(),
        })()
        mailbox = MailboxChannel(type("Manager", (), {
            "all_connections": lambda self: [connection],
        })())
        entered = threading.Event()
        release = threading.Event()

        def poll_phase():
            with connection._io_lock:
                entered.set()
                release.wait(timeout=2)
                self.assertIs(connection.storage, storage)

        poller = threading.Thread(target=poll_phase)
        closer = threading.Thread(target=mailbox.close)
        poller.start()
        self.assertTrue(entered.wait(timeout=2))
        closer.start()
        self.assertIs(connection.storage, storage)
        release.set()
        poller.join(timeout=2)
        closer.join(timeout=2)

        self.assertFalse(poller.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(storage.close_count, 1)
        self.assertIsNone(connection.storage)

    def test_minimal_third_party_channel_loses_no_implicit_functionality(self):
        session = Session("http://a")
        manager = ChannelManager(session)
        channel = _MinimalThirdPartyChannel()

        manager.register(channel)

        self.assertIsInstance(channel, Channel)
        self.assertNotIsInstance(channel, ManagedChannel)
        self.assertNotIsInstance(channel, BlobChannel)
        self.assertNotIsInstance(channel, LivenessChannel)
        self.assertNotIsInstance(channel, PairingChannel)
        self.assertNotIsInstance(channel, PollingChannel)
        self.assertEqual(manager.status()["minimal"], {"configured": False})
        self.assertEqual(
            CollaborationService(session, manager).channels_payload()["channels"],
            [],
        )
        self.assertFalse(hasattr(manager, "persistence_guard"))

    def test_compose_token_uses_registered_offers_and_identity(self):
        session = Session("http://a")
        manager = ChannelManager(session)
        manager.register(_Channel("other", "other", offered={
            "type": "other", "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
            "address": "http://a",
        }))
        manager.register(_Channel("mailbox", "relay"))

        result = manager.compose_token(["topic-1"], {
            "topic-1": {"kind": "other"},
            session.identity.uuid: {"kind": "other"},
        })

        self.assertTrue(result.ok)
        self.assertEqual(
            result.value["topic_uuids"],
            sorted(["topic-1", session.identity.uuid]),
        )
        self.assertEqual([item["type"] for item in result.value["channels"]], ["other"])
        self.assertEqual(
            set(result.value["topic_channels"]),
            {"topic-1", session.identity.uuid},
        )

    def test_explicit_channel_offer_error_is_not_silently_ignored(self):
        session = Session("http://a")
        manager = ChannelManager(session)
        manager.register(_Channel("mailbox", "relay"))

        result = manager.compose_token(
            ["topic-1"], {
                "topic-1": {"kind": "mailbox", "fail": True},
                session.identity.uuid: {"kind": "mailbox", "fail": True},
            },
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "offer failed")

    def test_compose_token_rejects_unknown_channel_option(self):
        session = Session("http://a")
        manager = ChannelManager(session)
        manager.register(_Channel("other", "other"))

        result = manager.compose_token(
            ["topic-1"], {
                "topic-1": {"kind": "nosuch"},
                session.identity.uuid: {"kind": "nosuch"},
            },
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 400)

    def test_compose_token_requires_an_explicit_channel(self):
        manager = ChannelManager(Session("http://a"))
        manager.register(_Channel("other", "other"))

        result = manager.compose_token(["topic-1"])

        self.assertFalse(result.ok)
        self.assertEqual(
            result.reason, "topic-to-channel mapping is missing or incomplete",
        )

    def test_accept_routes_multi_channel_token_by_topic_mapping(self):
        session = Session("http://a")
        peer = Session("http://b")
        manager = ChannelManager(session)
        direct = _Channel("other", "other", accept=ChannelResult.success(
            ChannelAcceptance("relay:B", {"status": "ok"}),
        ))
        mailbox = _Channel("mailbox", "relay", accept=ChannelResult.success(
            ChannelAcceptance("relay:B", {"status": "ok"}),
        ))
        manager.register(direct)
        manager.register(mailbox)

        result = manager.accept_invitation(
            peer.identity.to_dict(), ["topic-1", peer.identity.uuid], [
                {
                    "channel_id": "profile",
                    "type": "relay",
                    "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
                },
                {
                    "channel_id": "topic",
                    "type": "other",
                    "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
                },
            ],
            {
                peer.identity.uuid: "profile",
                "topic-1": "topic",
            },
        )

        self.assertTrue(result.ok)
        self.assertEqual(mailbox.accepted[0][1].topic_uuids, (peer.identity.uuid,))
        self.assertEqual(direct.accepted[0][1].topic_uuids, ("topic-1",))

    def test_accept_uses_the_single_selected_channel(self):
        session = Session("http://a")
        peer = Session("http://b")
        manager = ChannelManager(session)
        mailbox = _Channel("mailbox", "relay", accept=ChannelResult.success(
            ChannelAcceptance("relay:B", {"status": "ok"}),
        ))
        manager.register(_Channel("other", "other"))
        manager.register(mailbox)

        result = manager.accept_invitation(
            peer.identity.to_dict(), ["topic-1", peer.identity.uuid], [
                {
                    "channel_id": "only",
                    "type": "relay",
                    "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
                },
            ],
            {"topic-1": "only", peer.identity.uuid: "only"},
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.value["channels_used"], ["mailbox"])
        self.assertEqual(
            session.peer_channel_for_topic("relay:B", "topic-1"), "mailbox",
        )
        self.assertEqual(len(mailbox.accepted), 1)

    def test_accept_replaces_prior_address_for_same_identity(self):
        session = Session("http://a")
        peer = Session("http://b-old")
        identity = peer.identity.to_dict()
        session.note_indirect_peer_topic("http://b-old", "topic-1")
        session.apply_peer_identity_snapshot("http://b-old", identity)
        manager = ChannelManager(session)
        manager.register(_Channel("other", "other", accept=ChannelResult.success(
            ChannelAcceptance("http://b-new", {"status": "ok"}),
        )))

        result = manager.accept_invitation(
            identity, ["topic-1", peer.identity.uuid], [{
            "channel_id": "only", "type": "other",
            "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
        }], {"topic-1": "only", peer.identity.uuid: "only"})

        self.assertTrue(result.ok)
        self.assertNotIn("http://b-old", session.peer_topic_sets)
        self.assertIn("http://b-old", session.peer_identity_key)
        self.assertEqual(
            session.peer_channel_for_topic("http://b-new", "topic-1"), "other",
        )

    def test_accept_replaces_only_overlapping_topics_for_same_identity(self):
        session = Session("http://a")
        peer = Session("http://b")
        identity = peer.identity.to_dict()
        session.note_indirect_peer_topic("relay:B", "topic-relay")
        session.apply_peer_identity_snapshot("relay:B", identity)
        session.bind_peer_topic_channel("relay:B", "topic-relay", "mailbox")
        manager = ChannelManager(session)
        manager.register(_Channel(
            "other", "other",
            accept=ChannelResult.success(ChannelAcceptance(
                "http://b", {"status": "ok"},
            )),
        ))

        result = manager.accept_invitation(
            identity, ["topic-other", peer.identity.uuid], [{
            "channel_id": "only", "type": "other",
            "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
        }], {"topic-other": "only", peer.identity.uuid: "only"})

        self.assertTrue(result.ok)
        self.assertEqual(
            session.peer_channel_for_topic("relay:B", "topic-relay"),
            "mailbox",
        )
        self.assertEqual(
            session.peer_channel_for_topic("http://b", "topic-other"),
            "other",
        )

    def test_accept_preserves_identity_knowledge_for_indirect_old_address(self):
        session = Session("http://a")
        peer = Session("http://b")
        identity = peer.identity.to_dict()
        identity_key = identity["data"]["identity_key"]
        old_addr = "opaque-mailbox-address"
        session.note_indirect_peer_topic(old_addr, "topic-1")
        session.apply_peer_identity_snapshot(old_addr, identity)
        manager = ChannelManager(session)
        manager.register(_Channel(
            "other", "other",
            accept=ChannelResult.success(ChannelAcceptance(
                "http://b-new", {"status": "ok"},
            )),
        ))

        result = manager.accept_invitation(
            identity, ["topic-1", peer.identity.uuid], [{
            "channel_id": "only", "type": "other",
            "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
        }], {"topic-1": "only", peer.identity.uuid: "only"})

        self.assertTrue(result.ok)
        self.assertEqual(session.peer_identity_key[old_addr], identity_key)

    def test_rejects_descriptor_collision(self):
        manager = ChannelManager(Session("http://a"))
        manager.register(_Channel("first", "same"))
        with self.assertRaisesRegex(ValueError, "already handled"):
            manager.register(_Channel("second", "same"))

    def test_only_polling_channels_contribute_endpoints(self):
        manager = ChannelManager(Session("http://a"))
        manager.register(_Channel("other", "other"))
        manager.register(_PollingChannel("mailbox", "relay"))

        endpoints = manager.polling_endpoints()
        self.assertEqual(endpoints, [manager.channel("mailbox").endpoint])
        self.assertIsInstance(endpoints[0], PollingEndpoint)

        manager.close()
        self.assertTrue(all(channel.closed for channel in manager.channels()))

    def test_polling_channel_must_return_complete_endpoint_contracts(self):
        class InvalidPollingChannel(_Channel):
            def polling_endpoints(self):
                return [object()]

        manager = ChannelManager(Session("http://a"))
        manager.register(InvalidPollingChannel("other", "other"))

        with self.assertRaisesRegex(TypeError, "invalid polling endpoint"):
            manager.polling_endpoints()

    def test_blob_read_uses_only_the_explicit_topic_route(self):
        session = Session("http://a")
        direct = _Channel("other", "other")
        mailbox = _Channel("mailbox", "relay")
        manager = ChannelManager(session)
        manager.register(direct)
        manager.register(mailbox)
        session.bind_peer_topic_channel("relay:B", "topic-1", "mailbox")

        data = manager.read_topic_blob(
            "sha256:" + "0" * 64, "relay:B", "topic-1",
        )

        self.assertEqual(data, b"mailbox")
        self.assertEqual(direct.blob_reads, [])
        self.assertEqual(len(mailbox.blob_reads), 1)
        self.assertIsNone(manager.read_topic_blob(
            "sha256:" + "0" * 64, "http://b", "topic-1",
        ))

    def test_presence_is_topic_scoped_and_unrouted_means_offline(self):
        session = Session("http://a")
        manager = ChannelManager(session)
        manager.register(_Channel("other", "other"))
        peer_tree = Session("http://b").identity
        session.apply_peer_subtree("http://b", peer_tree, None)
        session.note_indirect_peer_topic("http://b", "topic-1")
        session.bind_peer_topic_channel("http://b", "topic-2", "other")

        unrouted = manager.network_info("topic-1")["peers"]["http://b"]
        routed = manager.network_info("topic-2")["peers"]["http://b"]

        self.assertEqual(unrouted["status"]["state"], "offline")
        self.assertEqual(
            unrouted["channel_liveness"]["state"], "unrouted",
        )
        self.assertEqual(routed["status"]["state"], "online")
        self.assertEqual(routed["channel"], "other")

    def _offer_manager(self, session, assigned_topics=()):
        class Manager:
            def __init__(self):
                self.session = session
                self.assigned = list(assigned_topics)
                self.assignments = []
                self.accepted = []

            def target_descriptor(self, target_id):
                return {"type": "relay", "identity": "A", "target_id": target_id}

            def target_for_topic(self, topic_uuid):
                return "target" if topic_uuid in self.assigned else None

            def assign_topics_target(self, topics, target_id):
                self.assignments.append((topics, target_id))
                self.assigned.extend(topics)
                return type("Result", (), {"status": "ok", "value": target_id})()

            def accept_descriptor(self, descriptor, topics, inviter_uuid):
                self.accepted.append((descriptor, topics, inviter_uuid))
                return type("Result", (), {"status": "ok", "value": "target"})()

        return Manager()

    def test_mailbox_offer_refuses_a_topic_the_channel_is_not_used_for(self):
        # Composing an invitation is not how a board gets bound to a channel.
        # It used to be, so asking for a token once bound it for good.
        session = Session("http://a")
        manager = self._offer_manager(session)
        channel = MailboxChannel(manager)

        offered = channel.offer_descriptor(("topic",), {"target_id": "target"})

        self.assertFalse(offered.ok)
        self.assertIn("home", offered.reason)
        self.assertEqual(manager.assignments, [])

    def test_mailbox_offer_accepts_with_inviter_identity_once_in_use(self):
        session = Session("http://a")
        identity_uuid = session.identity.uuid
        manager = self._offer_manager(
            session, assigned_topics=["topic", identity_uuid],
        )
        channel = MailboxChannel(manager)

        offered = channel.offer_descriptor(
            ("topic", identity_uuid), {"target_id": "target"},
        )

        self.assertTrue(offered.ok, offered.reason)
        self.assertEqual(manager.assignments, [])

        invitation = Invitation({"uuid": "inviter"}, ("topic",))
        accepted = channel.accept_descriptor(offered.value, invitation)
        self.assertTrue(accepted.ok)
        self.assertEqual(accepted.value.peer_addr, "relay:A")
        self.assertEqual(manager.accepted[0][2], "inviter")


class _MailboxManagerStub:
    """Just enough RelayManager for the channel's topic bookkeeping."""

    def __init__(self, session, targets):
        self.session = session
        self.targets = {
            target_id: list(topics) for target_id, topics in targets.items()
        }
        self.deleted = []

    @staticmethod
    def _ok(value=None):
        return type("Result", (), {"status": "ok", "value": value, "reason": None})()

    def list_targets(self):
        return [
            {
                "id": target_id,
                "name": target_id,
                "backend": "local",
                "topic_uuids": sorted(topics),
            }
            for target_id, topics in sorted(self.targets.items())
        ]

    def target_for_topic(self, topic_uuid):
        for target_id, topics in self.targets.items():
            if topic_uuid in topics:
                return target_id
        return None

    def assign_topic_target(self, topic_uuid, target_id):
        for topics in self.targets.values():
            if topic_uuid in topics:
                topics.remove(topic_uuid)
        if target_id:
            self.targets[target_id].append(topic_uuid)
        return self._ok(target_id or "")

    def delete_target(self, target_id):
        self.deleted.append(target_id)
        self.targets.pop(target_id, None)
        return self._ok(target_id)

    def all_connections(self):
        return []


class MailboxTopicReleaseTests(unittest.TestCase):
    def _session_with_both_channels(self):
        session = Session("http://a")
        session.note_indirect_peer_topic("relay:B", "board")
        session.bind_peer_topic_channel("relay:B", "board", "mailbox")
        session.note_indirect_peer_topic("http://c", "board")
        session.bind_peer_topic_channel("http://c", "board", "other")
        return session

    def test_stopping_a_mailbox_channel_returns_the_topic_to_private(self):
        # A topic no channel carries has no members, so the application can
        # tell "was on this board" from "is on this board" - which is what
        # lets a participant be taken off a card and not put back.
        session = self._session_with_both_channels()
        channel = MailboxChannel(
            _MailboxManagerStub(session, {"target": ["board"]}),
        )

        result = channel.detach_instance_topics(("board",), "target")

        self.assertTrue(result.ok)
        self.assertNotIn("relay:B", session.peer_topic_sets)

    def test_stopping_a_mailbox_channel_keeps_peers_the_direct_one_carries(self):
        session = self._session_with_both_channels()
        channel = MailboxChannel(
            _MailboxManagerStub(session, {"target": ["board"]}),
        )

        channel.detach_instance_topics(("board",), "target")

        self.assertIn("board", session.peer_topic_sets["http://c"])

    def test_deleting_a_mailbox_channel_releases_the_topics_it_carried(self):
        session = self._session_with_both_channels()
        manager = _MailboxManagerStub(session, {"target": ["board"]})
        channel = MailboxChannel(manager)

        result = channel.delete_instance("target")

        self.assertTrue(result.ok)
        self.assertEqual(manager.deleted, ["target"])
        self.assertNotIn("relay:B", session.peer_topic_sets)

    def test_a_channel_still_holding_topics_can_be_deleted(self):
        # It used to refuse with "channel is still used by: <uuid>". The
        # assignment is invisible to the user and nothing clears it when the
        # peers go, so the refusal made channels undeletable in practice.
        session = self._session_with_both_channels()
        manager = ChannelManager(session)
        manager.register(MailboxChannel(
            _MailboxManagerStub(session, {"target": ["board"]}),
        ))
        service = CollaborationService(session, manager)

        result = service.delete_channel("mailbox:target")

        self.assertTrue(result.ok, result.reason)
        self.assertEqual(service.channels_payload()["channels"], [])


if __name__ == "__main__":
    unittest.main()
