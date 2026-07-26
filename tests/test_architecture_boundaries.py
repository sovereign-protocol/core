"""Boundaries Core enforces on itself and on the example it ships.

These are source scans rather than integration tests, so each one runs in
the repository that owns the source it guards and fails in the pull request
that breaks it. The equivalents for S-Kanban and Personal Cockpit live in
those repositories.
"""

import ast
import unittest
from pathlib import Path

import sovereign
from sovereign.session import Session


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "src" / "sovereign"
EXAMPLES = ROOT / "examples"
APPLICATION_PACKAGES = ("personal_cockpit", "s_agreement", "s_kanban")


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_core_contains_no_application_dependency_or_vocabulary(self):
        for path in CORE.rglob("*.py"):
            raw_source = path.read_text(encoding="utf-8")
            source = raw_source.lower()
            tree = ast.parse(raw_source, filename=str(path))
            imports = imported_modules(path)
            self.assertFalse(any(
                name == package or name.startswith(f"{package}.")
                for name in imports
                for package in APPLICATION_PACKAGES
            ), str(path))
            for package in APPLICATION_PACKAGES:
                self.assertNotIn(package, source, str(path))
            self.assertNotIn("kanban", source, str(path))
            string_constants = {
                node.value.lower() for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            self.assertTrue(
                {"agreement", "agreement_section", "agreement_clause"}.isdisjoint(
                    string_constants,
                ),
                str(path),
            )

    def test_core_tests_run_without_any_application_installed(self):
        # The source rule above was already enforced; the tests were not. One
        # of them imported S-Kanban, which passed in the pre-split repository
        # where every package was installed and failed for anyone who
        # installed Core alone - exactly the dependency direction Core
        # forbids. Everything under tests/ ships with Core, so everything
        # under tests/ is checked.
        shipped = sorted(path for path in (ROOT / "tests").rglob("*.py"))
        self.assertTrue(shipped, "no Core tests found")

        for path in shipped:
            imports = imported_modules(path)
            offending = sorted(
                name for name in imports
                for package in APPLICATION_PACKAGES
                # S-Agreement ships inside Core as its conformance example, so
                # it is the one application a Core test may import.
                if package != "s_agreement"
                and (name == package or name.startswith(f"{package}."))
            )
            self.assertEqual(offending, [], f"{path} imports {offending}")

    def test_protocol_depends_only_on_standard_library_and_its_version_constant(self):
        imports = imported_modules(CORE / "protocol.py")
        allowed = {
            "__future__", "copy", "dataclasses", "datetime", "hashlib",
            "json", "typing", "uuid", "versions",
        }
        self.assertEqual(imports - allowed, set())

    def test_session_does_not_depend_on_host_channels_or_storage(self):
        imports = imported_modules(CORE / "session.py")
        forbidden = {
            "application", "app_server", "channel", "host", "http_channel",
            "mailbox_channel", "relay_logic", "relay_storage", "transport",
            "starlette", "requests", "paramiko",
        }
        for name in imports:
            self.assertFalse(
                any(name == item or name.startswith(f"{item}.") for item in forbidden),
                name,
            )

    def test_relay_storage_does_not_depend_on_session_channels_or_apps(self):
        imports = imported_modules(CORE / "relay_storage.py")
        forbidden = {
            "application", "channel", "host", "session", "s_kanban",
            "personal_cockpit",
        }
        self.assertTrue(imports.isdisjoint(forbidden))

    def test_public_root_exports_only_documented_contracts(self):
        documentation = (ROOT / "PUBLIC_API.md").read_text(encoding="utf-8")
        self.assertTrue(sovereign.__all__)
        for name in sovereign.__all__:
            self.assertTrue(hasattr(sovereign, name), name)
            if name != "__version__":
                self.assertIn(name, documentation, name)

    def test_the_ranking_orders_louder_transitions_first(self):
        # Applications must read this ranking rather than copy it; each
        # application repository asserts that about its own source.
        priority = Session.TRANSITION_PRIORITY

        self.assertGreater(priority["divergence"], priority["peer_made_changes"])
        self.assertGreater(priority["peer_made_changes"], priority["in_transition"])
        self.assertEqual(priority["in_agreement"], 0)


class ShippedExampleTests(unittest.TestCase):
    """The example application is held to the rules applications must follow."""

    def _example_sources(self):
        paths = sorted(EXAMPLES.glob("*/src/**/*.py"))
        self.assertTrue(paths, "no example application sources found")
        return paths

    def test_example_imports_core_only_through_its_public_root(self):
        public_names = set(sovereign.__all__)
        for path in self._example_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            violations = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    violations.extend(
                        alias.name for alias in node.names
                        if alias.name == "sovereign"
                        or alias.name.startswith("sovereign.")
                    )
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module.startswith("sovereign."):
                        violations.append(module)
                    elif module == "sovereign":
                        violations.extend(
                            f"sovereign.{alias.name}"
                            for alias in node.names
                            if alias.name == "*" or alias.name not in public_names
                        )
            self.assertEqual(violations, [], str(path))

    def test_example_does_not_reach_for_private_channel_services(self):
        for path in self._example_sources():
            source = path.read_text(encoding="utf-8")
            self.assertNotIn('config.get("_channel_manager")', source, str(path))
            self.assertNotIn('config.get("_relay_manager")', source, str(path))
            self.assertNotIn("channel_manager", source, str(path))

    def test_example_reads_the_ranking_rather_than_copying_it(self):
        for path in self._example_sources():
            source = path.read_text(encoding="utf-8")
            if "TRANSITION_PRIORITY" not in source:
                continue
            self.assertIn("Session.TRANSITION_PRIORITY", source, str(path))
            for literal in ('"divergence": 5', '"divergence": 6'):
                self.assertNotIn(literal, source, f"{path} re-declares the ranking")


if __name__ == "__main__":
    unittest.main()
