"""Packaging and asset invariants for what Core actually ships.

The pre-split repository checked all four distributions at once by
hard-coding each version. That could only run where every package was
installed, and the literals went stale the moment one distribution
released on its own. Each repository now asserts its own layout, and the
version assertion states the invariant that matters - metadata and module
agree - rather than a number that has to be edited on every release.
"""

import ast
import importlib.metadata
import importlib.util
import unittest
from importlib.resources import files
from pathlib import Path

import sovereign


ROOT = Path(__file__).resolve().parents[1]
SHARED_JS = files("sovereign.assets").joinpath("shared.js").read_text(encoding="utf-8")


class PackageLayoutTests(unittest.TestCase):
    def test_distribution_and_module_versions_agree(self):
        self.assertEqual(
            importlib.metadata.version("sovereign-protocol"), sovereign.__version__,
        )

    def test_old_flat_core_modules_are_not_importable(self):
        for module_name in (
            "protocol", "session", "transport", "relay_logic",
            "relay_storage", "blob_store", "topic_registry", "versions",
        ):
            self.assertIsNone(importlib.util.find_spec(module_name), module_name)

    def test_installed_browser_assets_are_available(self):
        self.assertTrue(files("sovereign.assets").joinpath("shared.js").is_file())
        self.assertTrue(files("sovereign.assets").joinpath("manual.html").is_file())

    def test_package_sources_live_under_the_declared_src_root(self):
        # Asserting where the imported module loaded from only holds for an
        # editable install: CI installs a wheel, so __file__ points into
        # site-packages. The invariant is this repository's layout - the
        # source sits under src/, and no flat copy survives beside it for an
        # import to pick up ahead of the installed package.
        self.assertTrue((ROOT / "src" / "sovereign" / "__init__.py").is_file())
        self.assertFalse((ROOT / "sovereign").exists())

    def test_applications_cannot_receive_channel_manager(self):
        from sovereign.application import ApplicationServices

        self.assertNotIn("channel_manager", ApplicationServices.__dataclass_fields__)

    def test_shared_peer_renderer_does_not_depend_on_channel_rows(self):
        peers_renderer = SHARED_JS.split("_renderPeersList() {", 1)[1].split(
            "async _renderConnTargets() {", 1,
        )[0]
        self.assertNotIn("channel.", peers_renderer)

    def test_topic_app_headers_delegate_navigation_and_creation_to_cockpit(self):
        self.assertNotIn("shellCreateTopicBtn", SHARED_JS)
        self.assertIn('app.role === "aggregator"', SHARED_JS)

    def test_open_collaboration_pane_refreshes_with_polled_topic_state(self):
        refresh = SHARED_JS.split("refresh() {", 1)[1].split("},", 1)[0]
        self.assertIn("this.refreshCollaborationPane()", refresh)
        pane_refresh = SHARED_JS.split("refreshCollaborationPane() {", 1)[1].split(
            "\n  },", 1,
        )[0]
        self.assertIn("if (!pane || pane.hidden) return", pane_refresh)
        self.assertIn("this._renderAgenda()", pane_refresh)
        self.assertIn("agenda.contains(document.activeElement)", pane_refresh)

    def test_domain_logic_modules_do_not_depend_on_host_or_http_controllers(self):
        paths = [
            ROOT / "src" / "sovereign" / "protocol_explorer.py",
            *sorted(ROOT.glob("examples/*/src/*/logic.py")),
        ]
        self.assertGreaterEqual(len(paths), 2, paths)
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imports = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            } | {
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            }
            self.assertFalse(
                any(
                    name == "starlette"
                    or name.startswith("starlette.")
                    or name.endswith(".controller")
                    or name.endswith("_controller")
                    or name == "sovereign.application"
                    for name in imports
                ),
                str(path),
            )
            self.assertFalse(any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in {"build_routes", "create_application"}
                for node in tree.body
            ), str(path))
            self.assertFalse(any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and any(arg.arg == "runtime" for arg in node.args.args)
                for node in ast.walk(tree)
            ), str(path))


class ShippedExampleAssetTests(unittest.TestCase):
    """The example's assets are held to the rules every application follows.

    These used to check S-Agreement, which shipped inside Core. It became a
    product and moved out, so they check the minimal example that replaced it.
    """

    def setUp(self):
        self.notes = files("sovereign_example_notes.assets").joinpath(
            "notes.html",
        ).read_text(encoding="utf-8")

    def test_example_assets_are_packaged(self):
        assets = files("sovereign_example_notes.assets")
        self.assertTrue(assets.joinpath("notes.html").is_file())
        self.assertTrue(assets.joinpath("notes.css").is_file())

    def test_example_delegates_topic_creation_to_the_shell(self):
        self.assertNotIn("onCreateTopic", self.notes)
        self.assertIn("SovereignShell.setTopicSelector", self.notes)

    def test_example_never_navigates_to_the_bare_root_with_a_query(self):
        # "/" serves whichever application is primary, so a root-relative link
        # lands somewhere that depends on host configuration.
        for number, line in enumerate(self.notes.splitlines(), start=1):
            for pattern in ('href = `/?', 'href="/?', "href='/?"):
                self.assertNotIn(pattern, line, f"notes.html:{number}")


if __name__ == "__main__":
    unittest.main()
