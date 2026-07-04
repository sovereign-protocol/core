import unittest

from protocol import PRSPNode, stable_hash
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
        remote_topic = PRSPNode({"name": "remote"})
        summary = {
            "topics": {remote_topic.uuid: remote_topic.state_hash},
            "deleted": {},
        }
        summary["sync_hash"] = stable_hash(summary)
        http.get_responses[f"http://b/p2p/subtree/{remote_topic.uuid}"] = {
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

    def test_health_ping_marks_peer_offline_after_failure(self):
        session = Session("http://a")
        topic = session.create_child(session.protocol.root.uuid, {"name": "topic"}, {}).value
        session.start_discussion(topic.uuid)
        session.add_peer("http://b", topic.uuid)
        http = FailingHttpClient()
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)

        result = adapter.check_peer_health("http://b", timeout=0.1)

        self.assertFalse(result.ok)
        self.assertEqual(session.peer_status["http://b"]["state"], "offline")

    def test_health_ping_prunes_not_found_fetch_topic(self):
        session = Session("http://a")
        topic = session.create_child(session.protocol.root.uuid, {"name": "topic"}, {}).value
        session.start_discussion(topic.uuid)
        session.add_peer("http://b", topic.uuid)
        http = FakeHttpClient()
        http.post_responses["http://b/p2p/ping"] = {"status": "ok"}
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)

        result = adapter.check_peer_health("http://b", timeout=0.1)

        self.assertTrue(result.ok)
        self.assertEqual(session.fetch_topic_uuids("http://b"), [])

    def test_health_ping_recovers_peer(self):
        session = Session("http://a")
        topic = session.create_child(session.protocol.root.uuid, {"name": "topic"}, {}).value
        session.start_discussion(topic.uuid)
        session.add_peer("http://b", topic.uuid)
        session.mark_peer_unreachable("http://b", "timeout")
        http = FakeHttpClient()
        http.post_responses["http://b/p2p/ping"] = {
            "status": "ok",
            "topic_state_hash": topic.state_hash,
        }
        http.get_responses[f"http://b/p2p/subtree/{topic.uuid}"] = {
            "subtree": topic.to_dict(),
            "parent_uuid": None,
        }
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)

        result = adapter.check_peer_health("http://b", timeout=0.1)

        self.assertTrue(result.ok)
        self.assertEqual(session.peer_status["http://b"]["state"], "online")
        self.assertIsNotNone(session.get_cached_peer_subtree("http://b", topic.uuid))

    def test_health_ping_skips_fetch_when_cached_topic_hash_matches(self):
        session = Session("http://a")
        topic = session.create_child(session.protocol.root.uuid, {"name": "topic"}, {}).value
        session.start_discussion(topic.uuid)
        session.add_peer("http://b", topic.uuid)
        session.apply_peer_subtree(
            "http://b",
            PRSPNode.from_dict(topic.to_dict()),
            None,
        )
        http = FakeHttpClient()
        http.post_responses["http://b/p2p/ping"] = {
            "status": "ok",
            "topic_state_hash": topic.state_hash,
        }
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)

        result = adapter.check_peer_health("http://b", timeout=0.1)

        self.assertTrue(result.ok)
        self.assertEqual(len(http.posts), 1)
        self.assertEqual(http.gets, [])

    def test_health_ping_fetches_when_cached_topic_hash_differs(self):
        session = Session("http://a")
        topic = session.create_child(session.protocol.root.uuid, {"name": "topic"}, {}).value
        session.start_discussion(topic.uuid)
        session.add_peer("http://b", topic.uuid)
        stale = PRSPNode.from_dict(topic.to_dict())
        stale.data = {"name": "stale"}
        stale.refresh_hashes_deep()
        session.apply_peer_subtree("http://b", stale, None)
        http = FakeHttpClient()
        http.post_responses["http://b/p2p/ping"] = {
            "status": "ok",
            "topic_state_hash": topic.state_hash,
        }
        http.get_responses[f"http://b/p2p/subtree/{topic.uuid}"] = {
            "subtree": topic.to_dict(),
            "parent_uuid": None,
        }
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)

        result = adapter.check_peer_health("http://b", timeout=0.1)

        self.assertTrue(result.ok)
        self.assertEqual(len(http.posts), 1)
        self.assertEqual(len(http.gets), 1)

    def test_health_fetches_remote_only_topic_when_cache_missing(self):
        session = Session("http://a")
        remote_topic = PRSPNode({"name": "remote"})
        session.set_peer_fetch_topics("http://b", {remote_topic.uuid})
        http = FakeHttpClient()
        http.post_responses["http://b/p2p/ping"] = {
            "status": "ok",
            "topic_state_hash": remote_topic.state_hash,
        }
        http.get_responses[f"http://b/p2p/subtree/{remote_topic.uuid}"] = {
            "subtree": remote_topic.to_dict(),
            "parent_uuid": None,
        }
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)

        result = adapter.check_peer_health("http://b", timeout=0.1)

        self.assertTrue(result.ok)
        self.assertEqual(len(http.posts), 1)
        self.assertIsNotNone(session.get_cached_peer_subtree(
            "http://b",
            remote_topic.uuid,
        ))

    def test_health_skips_fetch_for_unchanged_remote_only_topic(self):
        session = Session("http://a")
        remote_topic = PRSPNode({"name": "remote"})
        session.set_peer_fetch_topics("http://b", {remote_topic.uuid})
        session.apply_peer_subtree(
            "http://b",
            PRSPNode.from_dict(remote_topic.to_dict()),
            None,
        )
        http = FakeHttpClient()
        http.post_responses["http://b/p2p/ping"] = {
            "status": "ok",
            "topic_state_hash": remote_topic.state_hash,
        }
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)

        result = adapter.check_peer_health("http://b", timeout=0.1)

        self.assertTrue(result.ok)
        self.assertEqual(len(http.posts), 1)
        self.assertEqual(http.gets, [])

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

    def test_pull_subtree_falls_back_to_topic_when_changed_node_missing(self):
        session = Session("http://a")
        topic = PRSPNode({"name": "topic"})
        http = MissingChangedHttpClient("http://b/p2p/subtree/missing-child")
        http.get_responses["http://b/p2p/subtree/topic"] = {
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

    def test_join_discussion_does_not_flatten_topic_members(self):
        session = Session("http://a")
        http = FakeHttpClient()
        adapter = HttpTransportAdapter(session, http, logger=lambda _: None)
        topic_one = PRSPNode({"name": "one"})
        topic_two = PRSPNode({"name": "two"})
        http.get_responses[f"http://b/p2p/subtree/{topic_one.uuid}"] = {
            "subtree": topic_one.to_dict(),
            "parent_uuid": None,
        }
        http.get_responses[f"http://b/p2p/subtree/{topic_two.uuid}"] = {
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
            "subtree": topic_one.to_dict(),
            "parent_uuid": None,
        }
        http.get_responses[f"http://d/p2p/subtree/{topic_two.uuid}"] = {
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
