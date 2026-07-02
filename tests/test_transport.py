import unittest

from protocol import PRSPNode
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


class TransportTests(unittest.TestCase):
    def test_send_ping_effect_posts_to_peer(self):
        session = Session("http://a")
        http = FakeHttpClient()
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)
        session.add_peer("http://b/", "topic")

        result = adapter.execute_effect(SessionEffect(
            "send_ping",
            "http://b/",
            {
                "from_addr": "http://a",
                "topic_uuid": "topic",
                "topic_state_hash": "hash",
                "changed_uuid": "changed",
            },
        ))

        self.assertTrue(result.ok)
        self.assertEqual(http.posts[0][0], "http://b/p2p/ping")
        self.assertEqual(http.posts[0][1]["changed_uuid"], "changed")
        self.assertEqual(session.peer_status["http://b/"]["state"], "online")

    def test_health_ping_marks_peer_on_hold_after_failure(self):
        session = Session("http://a")
        topic = session.create_child(session.protocol.root.uuid, {"name": "topic"}, {}).value
        session.start_discussion(topic.uuid)
        session.add_peer("http://b", topic.uuid)
        http = FailingHttpClient()
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)

        result = adapter.check_peer_health("http://b", timeout=0.1)

        self.assertFalse(result.ok)
        self.assertEqual(session.peer_status["http://b"]["state"], "on_hold")

    def test_health_ping_recovers_peer(self):
        session = Session("http://a")
        topic = session.create_child(session.protocol.root.uuid, {"name": "topic"}, {}).value
        session.start_discussion(topic.uuid)
        session.add_peer("http://b", topic.uuid)
        session.mark_peer_unreachable("http://b", "timeout")
        http = FakeHttpClient()
        http.get_responses[f"http://b/p2p/subtree/{topic.uuid}"] = {
            "subtree": topic.to_dict(),
            "parent_uuid": None,
        }
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)

        result = adapter.check_peer_health("http://b", timeout=0.1)

        self.assertTrue(result.ok)
        self.assertEqual(session.peer_status["http://b"]["state"], "online")
        self.assertIsNotNone(session.get_cached_peer_subtree("http://b", topic.uuid))

    def test_pull_subtree_effect_updates_session_peer_cache(self):
        session = Session("http://a")
        http = FakeHttpClient()
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)
        topic = PRSPNode({"name": "topic"})
        http.get_responses["http://b/p2p/subtree/topic"] = {
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

    def test_pull_subtree_repairs_stale_hashes_from_peer(self):
        session = Session("http://a")
        http = FakeHttpClient()
        logs = []
        adapter = HttpTransportAdapter(session, http, logger=logs.append)
        topic = PRSPNode({"name": "topic"})
        child = PRSPNode({"name": "child"}, parent_uuid=topic.uuid)
        topic.children.append(child)
        topic.refresh_hashes_deep()
        payload = topic.to_dict()
        payload["content_hash"] = "stale"
        payload["state_hash"] = "stale"
        http.get_responses["http://b/p2p/subtree/topic"] = {
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

    def test_p2p_ping_executes_session_pull_effect(self):
        session = Session("http://a")
        http = FakeHttpClient()
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)
        topic = PRSPNode({"name": "topic"})
        changed = PRSPNode({"name": "changed"}, parent_uuid=topic.uuid)
        topic.children.append(changed)
        topic.refresh_hashes_deep()
        http.get_responses["http://b/p2p/subtree/topic"] = {
            "subtree": topic.to_dict(),
            "parent_uuid": None,
        }

        payload, status = adapter.p2p_ping({
            "from_addr": "http://b",
            "topic_uuid": "topic",
            "topic_state_hash": "remote-hash",
            "changed_uuid": "changed",
        })

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertIsNotNone(session.get_cached_peer_subtree(
            "http://b",
            changed.uuid,
        ))

    def test_join_discussion_fetches_topic_then_joins_peer(self):
        session = Session("http://a")
        http = FakeHttpClient()
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)
        topic = PRSPNode({"name": "topic"})
        http.get_responses[f"http://b/p2p/subtree/{topic.uuid}"] = {
            "subtree": topic.to_dict(),
            "parent_uuid": None,
        }
        http.post_responses["http://b/p2p/join"] = {
            "status": "ok",
            "members": ["http://b", "http://c"],
        }
        peer_topic = PRSPNode.from_dict(topic.to_dict())
        peer_topic.data["name"] = "peer-topic"
        peer_topic.refresh_hashes()
        http.get_responses[f"http://c/p2p/subtree/{topic.uuid}"] = {
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


if __name__ == "__main__":
    unittest.main()
