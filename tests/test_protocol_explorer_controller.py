import asyncio
import json
import unittest

from sovereign.protocol_explorer import ManualLogic
from sovereign.protocol_explorer_controller import build_routes
from sovereign.session import Session


class FakeCollaboration:
    """Stands in for CollaborationService, whose one effect is a lifecycle
    signal - releasing a topic's channels - and which returns nothing a
    controller reads."""

    def __init__(self):
        self.effects = []

    def execute_effects(self, effects):
        self.effects.extend(effects)
        return []


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class FakeRuntime:
    def __init__(self, session):
        self.session = session
        self.collaboration = FakeCollaboration()
        self.deliver_effects = self.collaboration.execute_effects
        self.config = {}
        self.notified = False

    def notify_change(self, kind="changed"):
        self.notified = True


class ProtocolExplorerControllerTests(unittest.TestCase):
    def test_routes_are_registered_under_protocol_explorer_namespace(self):
        session = Session("http://a")
        routes = build_routes(ManualLogic(session, {}), FakeRuntime(session))

        paths = {route.path for route in routes}
        self.assertIn("/api/protocol-explorer/state", paths)
        self.assertIn("/api/protocol-explorer/create_child", paths)
        self.assertIn("/api/protocol-explorer/accept_peer_node", paths)

    def test_modify_route_rejects_invalid_weight_as_json_error(self):
        session = Session("http://a")
        logic = ManualLogic(session, {})
        node = logic.create_child(
            session.protocol.root.uuid, {"name": "A"}, {},
        ).value
        runtime = FakeRuntime(session)
        endpoint = self._endpoint(
            build_routes(logic, runtime), "/api/protocol-explorer/modify",
        )

        response = asyncio.run(endpoint(FakeRequest({
            "node_uuid": node.uuid,
            "data": {"name": "A"},
            "weights": {"r": None},
        })))

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["reason"], "weight 'r' must be a number")

    @staticmethod
    def _endpoint(routes, path):
        return next(route.endpoint for route in routes if route.path == path)


if __name__ == "__main__":
    unittest.main()
