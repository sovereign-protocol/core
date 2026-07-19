import unittest

from protocol import ProtocolNode, stable_hash
from session import Session, SessionEffect
from transport import HttpTransportAdapter, TransportHttpError


class FakeHttpClient:
    def __init__(self):
        self.posts = []
        self.gets = []
        self.get_responses = {}
        self.post_responses = {}

    def get_json(self, url: str, timeout: float = 5) -> dict:
        self.gets.append((url, timeout))
        response = self.get_responses.get(url)
        if response is None:
            raise RuntimeError(f"unexpected GET {url}")
        return response

    def post_json(self, url: str, payload: dict,
                  timeout: float = 5) -> dict:
        self.posts.append((url, payload, timeout))
        return self.post_responses.get(url, {"status": "ok"})


class FailingHttpClient(FakeHttpClient):
    def post_json(self, url: str, payload: dict,
                  timeout: float = 5) -> dict:
        raise TransportHttpError(500, "remote failed", {"status": "error"})


class MissingChangedHttpClient(FakeHttpClient):
    def __init__(self, missing_url: str):
        super().__init__()
        self.missing_url = missing_url

    def get_json(self, url: str, timeout: float = 5) -> dict:
        self.gets.append((url, timeout))
        if url == self.missing_url:
            raise RuntimeError("not found")
        response = self.get_responses.get(url)
        if response is None:
            raise RuntimeError(f"unexpected GET {url}")
        return response


class TransportTests(unittest.TestCase):

    def test_send_sync_status_effect_posts_summary_and_confirms_ack(self):
        session = Session("http://a")
        topic = session.create_child(session.protocol.root.uuid, {"name": "topic"}, {}).value
        session.start_discussion(topic.uuid)
        session.add_peer("http://b", topic.uuid)
        summary = session.sync_summary("http://b")
        http = FakeHttpClient()
        http.post_responses["http://b/p2p/sync_status"] = {
            "status": "ok",
            "delivered_sync_hash": summary["sync_hash"],
            "my_summary": {
                "topics": {},
                "deleted": {},
                "sync_hash": stable_hash({"topics": {}, "deleted": {}}),
            },
        }
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)

        result = adapter.execute_effect(SessionEffect(
            "send_sync_status",
            "http://b",
            {"from_addr": "http://a", "summary": summary},
        ))

        self.assertTrue(result.ok)
        self.assertEqual(http.posts[0][0], "http://b/p2p/sync_status")
        self.assertEqual(
            session.peer_sync_state["http://b"]["last_delivered_sync_hash"],
            summary["sync_hash"],
        )

    def test_p2p_sync_status_pulls_changed_topic_and_returns_ack(self):
        session = Session("http://a")
        http = FakeHttpClient()
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)
        remote_topic = ProtocolNode({"name": "remote"})
        summary = {
            "topics": {remote_topic.uuid: remote_topic.state_hash},
            "deleted": {},
        }
        summary["sync_hash"] = stable_hash(summary)
        http.get_responses[f"http://b/p2p/subtree/{remote_topic.uuid}"] = {
            "protocol_schema_version": 1,
            "subtree": remote_topic.to_dict(),
            "parent_uuid": None,
        }

        payload, status = adapter.p2p_sync_status({
            "from_addr": "http://b",
            "summary": summary,
        })

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["delivered_sync_hash"], summary["sync_hash"])
        self.assertIsNotNone(session.get_cached_peer_subtree(
            "http://b",
            remote_topic.uuid,
        ))

    def test_pull_subtree_effect_updates_session_peer_cache(self):
        session = Session("http://a")
        http = FakeHttpClient()
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)
        topic = ProtocolNode({"name": "topic"})
        http.get_responses["http://b/p2p/subtree/topic"] = {
            "protocol_schema_version": 1,
            "subtree": topic.to_dict(),
            "parent_uuid": None,
        }

        result = adapter.execute_effect(SessionEffect(
            "pull_subtree",
            "http://b",
            {"node_uuid": "topic", "topic_uuid": "topic"},
        ))

        self.assertTrue(result.ok)
        cached = session.get_cached_peer_subtree("http://b", topic.uuid)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.data["name"], "topic")

    def test_pull_subtree_falls_back_to_topic_when_changed_node_missing(self):
        session = Session("http://a")
        topic = ProtocolNode({"name": "topic"})
        http = MissingChangedHttpClient("http://b/p2p/subtree/missing-child")
        http.get_responses["http://b/p2p/subtree/topic"] = {
            "protocol_schema_version": 1,
            "subtree": topic.to_dict(),
            "parent_uuid": None,
        }
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)

        result = adapter.execute_effect(SessionEffect(
            "pull_subtree",
            "http://b",
            {"node_uuid": "missing-child", "topic_uuid": "topic"},
        ))

        self.assertTrue(result.ok)
        self.assertEqual(
            [item[0] for item in http.gets],
            ["http://b/p2p/subtree/missing-child", "http://b/p2p/subtree/topic"],
        )
        self.assertIsNotNone(session.get_cached_peer_subtree("http://b", topic.uuid))

    def test_pull_subtree_repairs_stale_hashes_from_peer(self):
        session = Session("http://a")
        http = FakeHttpClient()
        logs = []
        adapter = HttpTransportAdapter(session, http, logger=logs.append)
        topic = ProtocolNode({"name": "topic"})
        child = ProtocolNode({"name": "child"}, parent_uuid=topic.uuid)
        topic.children.append(child)
        topic.refresh_hashes_deep()
        payload = topic.to_dict()
        payload["content_hash"] = "stale"
        payload["state_hash"] = "stale"
        http.get_responses["http://b/p2p/subtree/topic"] = {
            "protocol_schema_version": 1,
            "subtree": payload,
            "parent_uuid": None,
        }

        result = adapter.execute_effect(SessionEffect(
            "pull_subtree",
            "http://b",
            {"node_uuid": "topic", "topic_uuid": "topic"},
        ))

        self.assertTrue(result.ok)
        cached = session.get_cached_peer_subtree("http://b", topic.uuid)
        self.assertIsNotNone(cached)
        self.assertNotEqual(cached.state_hash, "stale")
        self.assertIn("repairing invalid subtree", logs[0])

    def test_join_discussion_fetches_topic_then_joins_peer(self):
        session = Session("http://a")
        http = FakeHttpClient()
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)
        topic = ProtocolNode({"name": "topic"})
        http.get_responses[f"http://b/p2p/subtree/{topic.uuid}"] = {
            "protocol_schema_version": 1,
            "subtree": topic.to_dict(),
            "parent_uuid": None,
        }
        http.post_responses["http://b/p2p/join"] = {
            "status": "ok",
            "members": ["http://b", "http://c"],
            "topic_members": {topic.uuid: ["http://b", "http://c"]},
        }
        peer_topic = ProtocolNode.from_dict(topic.to_dict())
        peer_topic.data["name"] = "peer-topic"
        peer_topic.refresh_hashes()
        http.get_responses[f"http://c/p2p/subtree/{topic.uuid}"] = {
            "protocol_schema_version": 1,
            "subtree": peer_topic.to_dict(),
            "parent_uuid": None,
        }

        result = adapter.join_discussion("http://b", topic.uuid)

        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(session.get_cached_peer_subtree(
            "http://b",
            topic.uuid,
        ))
        c_cached = session.get_cached_peer_subtree("http://c", topic.uuid)
        self.assertIsNotNone(c_cached)
        self.assertEqual(c_cached.data["name"], "peer-topic")
        self.assertEqual(session.active_topic_uuid, topic.uuid)
        self.assertIn("http://b", session.members)
        self.assertIn("http://c", session.members)
        self.assertEqual(http.posts[0][0], "http://b/p2p/join")

    def test_join_discussion_does_not_flatten_topic_members(self):
        session = Session("http://a")
        http = FakeHttpClient()
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)
        topic_one = ProtocolNode({"name": "one"})
        topic_two = ProtocolNode({"name": "two"})
        http.get_responses[f"http://b/p2p/subtree/{topic_one.uuid}"] = {
            "protocol_schema_version": 1,
            "subtree": topic_one.to_dict(),
            "parent_uuid": None,
        }
        http.get_responses[f"http://b/p2p/subtree/{topic_two.uuid}"] = {
            "protocol_schema_version": 1,
            "subtree": topic_two.to_dict(),
            "parent_uuid": None,
        }
        http.post_responses["http://b/p2p/join"] = {
            "status": "ok",
            "members": ["http://b", "http://c", "http://d"],
            "topic_members": {
                topic_one.uuid: ["http://b", "http://c"],
                topic_two.uuid: ["http://b", "http://d"],
            },
        }
        http.get_responses[f"http://c/p2p/subtree/{topic_one.uuid}"] = {
            "protocol_schema_version": 1,
            "subtree": topic_one.to_dict(),
            "parent_uuid": None,
        }
        http.get_responses[f"http://d/p2p/subtree/{topic_two.uuid}"] = {
            "protocol_schema_version": 1,
            "subtree": topic_two.to_dict(),
            "parent_uuid": None,
        }

        result = adapter.join_discussion(
            "http://b",
            topic_uuids=[topic_one.uuid, topic_two.uuid],
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(session.peer_topic_sets["http://c"], {topic_one.uuid})
        self.assertEqual(session.peer_topic_sets["http://d"], {topic_two.uuid})

    def test_observe_topic_caches_perspective_without_becoming_a_peer(self):
        session = Session("http://a")
        http = FakeHttpClient()
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)
        topic = ProtocolNode({"name": "topic"})
        http.get_responses[f"http://b/p2p/subtree/{topic.uuid}"] = {
            "protocol_schema_version": 1,
            "subtree": topic.to_dict(),
            "parent_uuid": None,
        }

        result = adapter.observe_topic("http://b", topic.uuid)

        self.assertEqual(result["status"], "ok")
        cached = session.get_cached_peer_subtree("http://b", topic.uuid)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.data["name"], "topic")
        # The whole point: no /p2p/join call, no membership, no active
        # topic registration - the target never learns we exist.
        self.assertEqual(http.posts, [])
        self.assertNotIn("http://b", session.members)
        self.assertNotIn(topic.uuid, session.active_topic_uuids)
        self.assertNotIn("http://b", session.peer_topic_sets)
        self.assertEqual(session.observed_topics, {"http://b": {topic.uuid}})

    def test_observe_topic_requires_a_topic(self):
        session = Session("http://a")
        adapter = HttpTransportAdapter(session, FakeHttpClient(), logger=lambda _: None)

        result = adapter.observe_topic("http://b")

        self.assertEqual(result["status"], "error")

    def test_leave_discussion_executes_leave_effects(self):
        session = Session("http://a")
        session.add_peer("http://b", "topic")
        http = FakeHttpClient()
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)

        deliveries = adapter.leave_discussion()

        self.assertTrue(all(delivery.ok for delivery in deliveries))
        self.assertIn(("http://b/p2p/leave", {"from_addr": "http://a"}, 2),
                      http.posts)
        self.assertEqual(session.members, {"http://a"})

    def test_invite_to_discuss_returns_structured_error_on_http_failure(self):
        session = Session("http://a")
        adapter = HttpTransportAdapter(
            session,
            FailingHttpClient(),
            logger=lambda _: None,
        )

        result = adapter.invite_to_discuss("http://b", "topic")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "remote failed")
        self.assertEqual(result["remote_status"], 500)

    def test_invite_to_discuss_targets_join_discussion_by_default(self):
        session = Session("http://a")
        http = FakeHttpClient()
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)

        adapter.invite_to_discuss("http://b", "topic-1")

        self.assertEqual(http.posts[0][0], "http://b/api/join_discussion")

    def test_invite_to_discuss_read_only_targets_observe_topic_instead(self):
        session = Session("http://a")
        http = FakeHttpClient()
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)

        adapter.invite_to_discuss("http://b", "topic-1", read_only=True)

        self.assertEqual(http.posts[0][0], "http://b/api/observe_topic")
        self.assertEqual(http.posts[0][1]["address"], "http://a")


if __name__ == "__main__":
    unittest.main()
