import asyncio
import json
import unittest

from sovereign.protocol_explorer import ManualLogic
from sovereign.protocol_explorer_controller import build_routes
from sovereign.session import Session
from sovereign.transport import TransportDelivery


class FakeAdapter:
    def __init__(self):
        self.effects = []

    def execute_effects(self, effects):
        self.effects.extend(effects)
        return [
            TransportDelivery(True, effect.type, effect.target)
            for effect in effects
        ]

    def invite_to_discuss(self, peer_addr, topic_uuid, read_only=False):
        return {
            "status": "ok",
            "peer_addr": peer_addr,
            "topic_uuid": topic_uuid,
            "read_only": read_only,
        }


class FailingInviteAdapter(FakeAdapter):
    def invite_to_discuss(self, peer_addr, topic_uuid, read_only=False):
        raise RuntimeError("invite exploded")


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class FakeRuntime:
    def __init__(self, session):
        self.session = session
        self.channel_manager = FakeAdapter()
        self.config = {}
        self.notified = False

    def notify_change(self, kind="changed"):
        self.notified = True


class ProtocolExplorerControllerTests(unittest.TestCase):
    def test_routes_are_registered_under_protocol_explorer_namespace(self):
        session = Session("http://a")
        routes = build_routes(ManualLogic(session, {}), FakeRuntime(session), {})

        paths = {route.path for route in routes}
        self.assertIn("/api/protocol-explorer/state", paths)
        self.assertIn("/api/protocol-explorer/create_child", paths)
        self.assertIn("/api/protocol-explorer/invite", paths)
        self.assertIn("/api/protocol-explorer/accept_peer_node", paths)

    def test_invite_route_returns_json_error_on_exception(self):
        session = Session("http://a")
        logic = ManualLogic(session, {})
        topic = logic.create_child(
            session.protocol.root.uuid, {"name": "topic"}, {},
        ).value
        runtime = FakeRuntime(session)
        runtime.channel_manager = FailingInviteAdapter()
        endpoint = self._endpoint(
            build_routes(logic, runtime, {}), "/api/protocol-explorer/invite",
        )

        response = asyncio.run(endpoint(FakeRequest({
            "address": "http://b", "topic_uuid": topic.uuid,
        })))

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["reason"], "invite exploded")

    def test_modify_route_rejects_invalid_weight_as_json_error(self):
        session = Session("http://a")
        logic = ManualLogic(session, {})
        node = logic.create_child(
            session.protocol.root.uuid, {"name": "A"}, {},
        ).value
        runtime = FakeRuntime(session)
        endpoint = self._endpoint(
            build_routes(logic, runtime, {}), "/api/protocol-explorer/modify",
        )

        response = asyncio.run(endpoint(FakeRequest({
            "node_uuid": node.uuid,
            "data": {"name": "A"},
            "weights": {"r": None},
        })))

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["reason"], "weight 'r' must be a number")

    def test_invite_can_add_another_active_topic(self):
        session = Session("http://a")
        logic = ManualLogic(session, {})
        first = logic.create_child(
            session.protocol.root.uuid, {"name": "first"}, {},
        ).value
        second = logic.create_child(
            session.protocol.root.uuid, {"name": "second"}, {},
        ).value
        runtime = FakeRuntime(session)
        logic.start_discussion(first.uuid)
        endpoint = self._endpoint(
            build_routes(logic, runtime, {}), "/api/protocol-explorer/invite",
        )

        response = asyncio.run(endpoint(FakeRequest({
            "address": "http://b", "topic_uuid": second.uuid,
        })))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(session.active_topic_uuids, {first.uuid, second.uuid})

    @staticmethod
    def _endpoint(routes, path):
        return next(route.endpoint for route in routes if route.path == path)


if __name__ == "__main__":
    unittest.main()
