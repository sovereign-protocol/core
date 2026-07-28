import unittest

from sovereign.channel import (
    Channel,
    ChannelAcceptance,
    ChannelManager,
    ChannelResult,
    EffectDeliveryChannel,
    Invitation,
    PollingChannel,
)
from sovereign.collaboration import CollaborationService
from sovereign.http_channel import DirectHttpChannel
from sovereign.mailbox_channel import MailboxChannel
from sovereign.session import Session, SessionEffect
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


class _DeliveryChannel(_Channel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.delivered = []

    def execute_effect(self, effect):
        return ("one", effect)

    def execute_effects(self, effects):
        self.delivered.extend(effects)
        return [("many", item) for item in effects]


class _PollingChannel(_Channel):
    def polling_endpoints(self):
        return ["poller"]


class ChannelManagerTests(unittest.TestCase):
    def test_concrete_channels_satisfy_declared_capability_contracts(self):
        direct = DirectHttpChannel("http://a", object())
        mailbox = MailboxChannel(type("Manager", (), {
            "all_connections": lambda self: [],
        })())

        self.assertIsInstance(direct, Channel)
        self.assertIsInstance(direct, EffectDeliveryChannel)
        self.assertNotIsInstance(direct, PollingChannel)
        self.assertIsInstance(mailbox, Channel)
        self.assertIsInstance(mailbox, PollingChannel)
        self.assertNotIsInstance(mailbox, EffectDeliveryChannel)

    def test_compose_token_uses_registered_offers_and_identity(self):
        session = Session("http://a")
        manager = ChannelManager(session)
        manager.register(_Channel("http", "http", offered={
            "type": "http", "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
            "address": "http://a",
        }))
        manager.register(_Channel("mailbox", "relay"))

        result = manager.compose_token(["topic-1"], {"http": {}})

        self.assertTrue(result.ok)
        self.assertEqual(
            result.value["topic_uuids"],
            sorted(["topic-1", session.identity.uuid]),
        )
        self.assertEqual([item["type"] for item in result.value["channels"]], ["http"])

    def test_explicit_channel_offer_error_is_not_silently_ignored(self):
        manager = ChannelManager(Session("http://a"))
        manager.register(_Channel("mailbox", "relay"))

        result = manager.compose_token(
            ["topic-1"], {"mailbox": {"fail": True}},
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "offer failed")

    def test_compose_token_rejects_unknown_channel_option(self):
        manager = ChannelManager(Session("http://a"))
        manager.register(_Channel("http", "http"))

        result = manager.compose_token(
            ["topic-1"], {"htpt": {"enabled": True}},
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 400)

    def test_compose_token_requires_an_explicit_channel(self):
        manager = ChannelManager(Session("http://a"))
        manager.register(_Channel("http", "http"))

        result = manager.compose_token(["topic-1"])

        self.assertFalse(result.ok)
        self.assertEqual(
            result.reason, "select exactly one channel for the invitation",
        )

    def test_accept_rejects_ambiguous_multi_channel_token(self):
        session = Session("http://a")
        peer = Session("http://b")
        manager = ChannelManager(session)
        direct = _Channel("http", "http", accept=ChannelResult.error("offline"))
        mailbox = _Channel("mailbox", "relay", accept=ChannelResult.success(
            ChannelAcceptance("relay:B", {"status": "ok"}),
        ))
        manager.register(direct)
        manager.register(mailbox)

        result = manager.accept_invitation(
            peer.identity.to_dict(), ["topic-1"], [
                {"type": "relay", "descriptor_version": CHANNEL_DESCRIPTOR_VERSION},
                {"type": "http", "descriptor_version": CHANNEL_DESCRIPTOR_VERSION},
            ],
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "token must select exactly one channel")
        self.assertEqual(len(direct.accepted), 0)
        self.assertEqual(len(mailbox.accepted), 0)

    def test_accept_uses_the_single_selected_channel(self):
        session = Session("http://a")
        peer = Session("http://b")
        manager = ChannelManager(session)
        mailbox = _Channel("mailbox", "relay", accept=ChannelResult.success(
            ChannelAcceptance("relay:B", {"status": "ok"}),
        ))
        manager.register(_Channel("http", "http"))
        manager.register(mailbox)

        result = manager.accept_invitation(
            peer.identity.to_dict(), ["topic-1"], [
                {"type": "relay", "descriptor_version": CHANNEL_DESCRIPTOR_VERSION},
            ],
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
        session.add_peer("http://b-old", "topic-1")
        session.apply_peer_identity_snapshot("http://b-old", identity)
        manager = ChannelManager(session)
        manager.register(_Channel("http", "http", accept=ChannelResult.success(
            ChannelAcceptance("http://b-new", {"status": "ok"}),
        )))

        result = manager.accept_invitation(identity, ["topic-1"], [{
            "type": "http", "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
        }])

        self.assertTrue(result.ok)
        self.assertNotIn("http://b-old", session.members)
        self.assertIn("http://b-old", session.peer_identity_key)
        self.assertEqual(
            session.peer_channel_for_topic("http://b-new", "topic-1"), "http",
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
            "http", "http",
            accept=ChannelResult.success(ChannelAcceptance(
                "http://b", {"status": "ok"},
            )),
        ))

        result = manager.accept_invitation(identity, ["topic-http"], [{
            "type": "http", "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
        }])

        self.assertTrue(result.ok)
        self.assertEqual(
            session.peer_channel_for_topic("relay:B", "topic-relay"),
            "mailbox",
        )
        self.assertEqual(
            session.peer_channel_for_topic("http://b", "topic-http"),
            "http",
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
            "http", "http",
            accept=ChannelResult.success(ChannelAcceptance(
                "http://b-new", {"status": "ok"},
            )),
        ))

        result = manager.accept_invitation(identity, ["topic-1"], [{
            "type": "http", "descriptor_version": CHANNEL_DESCRIPTOR_VERSION,
        }])

        self.assertTrue(result.ok)
        self.assertEqual(session.peer_identity_key[old_addr], identity_key)

    def test_rejects_descriptor_collision(self):
        manager = ChannelManager(Session("http://a"))
        manager.register(_Channel("first", "same"))
        with self.assertRaisesRegex(ValueError, "already handled"):
            manager.register(_Channel("second", "same"))

    def test_capabilities_route_effects_and_polling(self):
        manager = ChannelManager(Session("http://a"))
        manager.register(_DeliveryChannel("http", "http"))
        manager.register(_PollingChannel("mailbox", "relay"))

        effect = SessionEffect(
            "pull_subtree", "peer-b", {"topic_uuid": "topic-1"},
            channel_kind="http",
        )
        self.assertEqual(manager.execute_effects([effect]), [("many", effect)])
        self.assertEqual(manager.polling_endpoints(), ["poller"])

        manager.close()
        self.assertTrue(all(channel.closed for channel in manager.channels()))

    def test_effects_route_through_the_channel_selected_for_each_peer(self):
        session = Session("http://a")
        direct = _DeliveryChannel("http", "http")
        alternate = _DeliveryChannel("alternate", "alternate")
        manager = ChannelManager(session)
        manager.register(direct)
        manager.register(alternate)
        effects = [
            SessionEffect(
                "pull_subtree", "peer-c", {"topic_uuid": "topic-1"},
                channel_kind="alternate",
            ),
            SessionEffect(
                "pull_subtree", "peer-b", {"topic_uuid": "topic-1"},
                channel_kind="http",
            ),
        ]

        deliveries = manager.execute_effects(effects)

        self.assertEqual(alternate.delivered, [effects[0]])
        self.assertEqual(direct.delivered, [effects[1]])
        self.assertEqual([item[1] for item in deliveries], effects)

    def test_effect_without_explicit_channel_is_rejected(self):
        manager = ChannelManager(Session("http://a"))
        manager.register(_DeliveryChannel("http", "http"))

        with self.assertRaisesRegex(RuntimeError, "no explicit channel"):
            manager.execute_effects([
                SessionEffect("pull_subtree", "peer-b", {"topic_uuid": "topic-1"}),
            ])

    def test_blob_read_uses_only_the_explicit_topic_route(self):
        session = Session("http://a")
        direct = _Channel("http", "http")
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
        manager.register(_DeliveryChannel("http", "http"))
        peer_tree = Session("http://b").identity
        session.apply_peer_subtree("http://b", peer_tree, None)
        session.note_indirect_peer_topic("http://b", "topic-1")
        session.bind_peer_topic_channel("http://b", "topic-2", "http")

        unrouted = manager.network_info("topic-1")["peers"]["http://b"]
        routed = manager.network_info("topic-2")["peers"]["http://b"]

        self.assertEqual(unrouted["status"]["state"], "offline")
        self.assertEqual(
            unrouted["channel_liveness"]["state"], "unrouted",
        )
        self.assertEqual(routed["status"]["state"], "online")
        self.assertEqual(routed["channel"], "http")

    def test_direct_channel_enforces_policy_and_preserves_read_only_invite(self):
        class Adapter:
            def __init__(self):
                self.invites = []

            def invite_to_discuss(self, *args, **kwargs):
                self.invites.append((args, kwargs))
                return {"status": "ok"}

        adapter = Adapter()
        disabled = DirectHttpChannel(
            "http://a", adapter, offer_enabled=False, accept_enabled=False,
        )
        self.assertIsNone(disabled.offer_descriptor(("topic",), {}).value)
        rejected = disabled.accept_descriptor(
            {"address": "http://b"}, Invitation(None, ("topic",)),
        )
        self.assertFalse(rejected.ok)

        direct = DirectHttpChannel("http://a", adapter)
        direct.invite_to_discuss("http://b", "topic", read_only=True)
        self.assertEqual(adapter.invites[0][1], {"read_only": True})

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
        self.assertIn("use this channel", offered.reason)
        self.assertEqual(manager.assignments, [])

    def test_mailbox_offer_accepts_with_inviter_identity_once_in_use(self):
        session = Session("http://a")
        identity_uuid = session.identity.uuid
        manager = self._offer_manager(session, assigned_topics=["topic"])
        channel = MailboxChannel(manager)

        offered = channel.offer_descriptor(
            ("topic", identity_uuid), {"target_id": "target"},
        )

        self.assertTrue(offered.ok, offered.reason)
        # Only the identity topic is assigned here: it is not a board, so it
        # follows the invitation's route rather than needing its own decision.
        self.assertEqual(manager.assignments, [([identity_uuid], "target")])

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
        session.add_peer("http://c", "board")
        session.bind_peer_topic_channel("http://c", "board", "http")
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
