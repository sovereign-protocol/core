import re
import unittest
from pathlib import Path

from session import Session


class ProtocolBoundaryTests(unittest.TestCase):
    def test_session_returns_snapshots_from_create_child(self):
        session = Session("local")

        child = session.create_child(
            session.root_uuid(),
            {"name": "original"},
            {},
        ).value
        child.data["name"] = "mutated outside protocol"

        stored = session.protocol.index[child.uuid]
        self.assertEqual(stored.data["name"], "original")

    def test_protocol_facade_does_not_expose_mutations(self):
        session = Session("local")

        self.assertFalse(hasattr(session.protocol, "modify"))
        self.assertFalse(hasattr(session.protocol, "attach_topic"))
        self.assertFalse(hasattr(session.protocol, "index_subtree"))

    def test_app_logic_does_not_mutate_protocol_directly(self):
        root = Path(__file__).resolve().parents[1]
        app_files = [
            root / "kanban_logic.py",
            root / "manual_logic.py",
        ]
        forbidden = [
            r"\.(?:children|parent_uuid|content_hash|state_hash|base_hash|base_parent_uuid|data|weights)\s*=",
            r"\.(?:data|weights)\s*\[",
            r"\.children\.append\s*\(",
            r"\.children\.(?:clear|extend|insert|pop|remove)\s*\(",
            r"session\.protocol\.(?:create_child|modify|delete|copy|move|attach_topic|deindex_subtree|index_subtree|cascade_hash|adopt_subtree|replace_subtree|remove_subtree_uuids)\s*\(",
        ]

        violations = []
        for path in app_files:
            text = path.read_text(encoding="utf-8")
            for pattern in forbidden:
                for match in re.finditer(pattern, text):
                    line = text.count("\n", 0, match.start()) + 1
                    violations.append(f"{path.name}:{line}: {match.group(0)}")

        self.assertEqual([], violations)
