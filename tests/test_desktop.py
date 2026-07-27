"""The desktop window, including the case a frozen build actually hits.

A windowed build (`console=False`) leaves sys.stdout and sys.stderr as None.
Nothing in this repository's tests ran into that, because a test process
always has real streams - so uvicorn's log configuration crashed the moment
either executable was double-clicked, while CI stayed green building them.
"""

import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from sovereign import desktop
from sovereign.application import ApplicationInstance, ApplicationManifest


ALIASES = {
    "example": {
        "app_module": "sovereign.protocol_explorer_application",
        "application_id": "protocol-explorer",
        "asset_package": "sovereign.assets",
        "ui_file": "manual.html",
        "css_file": "manual.css",
    },
}


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.real = sys.stdout, sys.stderr
        self.addCleanup(self._restore)

    def _restore(self):
        sys.stdout, sys.stderr = self.real

    def test_absent_streams_are_replaced(self):
        sys.stdout = None
        sys.stderr = None

        desktop.ensure_streams()

        self.assertIsNotNone(sys.stdout)
        self.assertIsNotNone(sys.stderr)
        # The point is that libraries can ask and write without raising, not
        # what the answer is. On Windows the answer is surprising: NUL is a
        # character device, so isatty() reports True and uvicorn decides to
        # colourise. The escape codes go to NUL along with everything else.
        self.assertIsInstance(sys.stdout.isatty(), bool)
        sys.stdout.write("discarded")
        sys.stderr.write("discarded")

    def test_existing_streams_are_left_alone(self):
        # Running from a terminal must not lose its output.
        replacement = io.StringIO()
        sys.stdout = replacement

        desktop.ensure_streams()

        self.assertIs(sys.stdout, replacement)

    def test_uvicorn_accepts_a_log_configuration_without_a_console(self):
        # The exact failure: uvicorn's default formatter asks stdout whether
        # it is a tty, and None has no isatty. It surfaced as "Unable to
        # configure formatter 'default'" with no mention of stdout at all.
        import uvicorn

        sys.stdout = None
        sys.stderr = None
        desktop.ensure_streams()

        config = uvicorn.Config(
            lambda: None, host=desktop.LOOPBACK, port=9999, log_level="error",
        )

        self.assertIsNotNone(config)


class DarkTitlebarTests(unittest.TestCase):
    """background_color paints the content area; the titlebar is the OS's
    to draw and answers to DwmSetWindowAttribute instead - fixing one left
    the other white. Exercised without a real Windows handle: ctypes.windll
    does not exist off Windows at all, so the platform and the call both
    have to be faked to test the logic on any runner, including the Linux
    leg of this repository's own CI matrix.
    """

    @staticmethod
    def _window(hwnd=12345):
        window = unittest.mock.Mock()
        window.native.Handle.ToInt32.return_value = hwnd
        return window

    def test_does_nothing_off_windows(self):
        window = self._window()
        with patch.object(sys, "platform", "linux"):
            desktop._use_dark_titlebar(window)

        window.native.Handle.ToInt32.assert_not_called()

    def test_asks_dwm_for_dark_mode_using_the_real_window_handle(self):
        import ctypes

        window = self._window(hwnd=98765)
        fake_dwmapi = unittest.mock.Mock()
        fake_dwmapi.DwmSetWindowAttribute.return_value = 0
        fake_windll = unittest.mock.Mock(dwmapi=fake_dwmapi)

        with patch.object(sys, "platform", "win32"), \
                patch.object(ctypes, "windll", fake_windll, create=True):
            desktop._use_dark_titlebar(window)

        fake_dwmapi.DwmSetWindowAttribute.assert_called_once()
        args = fake_dwmapi.DwmSetWindowAttribute.call_args[0]
        self.assertEqual(args[0], 98765)
        self.assertEqual(args[1], desktop._DWMWA_USE_IMMERSIVE_DARK_MODE)

    def test_falls_back_to_the_legacy_attribute_when_the_new_one_is_rejected(self):
        import ctypes

        window = self._window()
        fake_dwmapi = unittest.mock.Mock()
        # Non-zero is DWM's failure return; older Windows builds reject the
        # stable attribute number and only accept the legacy one.
        fake_dwmapi.DwmSetWindowAttribute.return_value = 1
        fake_windll = unittest.mock.Mock(dwmapi=fake_dwmapi)

        with patch.object(sys, "platform", "win32"), \
                patch.object(ctypes, "windll", fake_windll, create=True):
            desktop._use_dark_titlebar(window)

        self.assertEqual(fake_dwmapi.DwmSetWindowAttribute.call_count, 2)
        second_call_attribute = fake_dwmapi.DwmSetWindowAttribute.call_args_list[1][0][1]
        self.assertEqual(
            second_call_attribute, desktop._DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY,
        )

    def test_a_missing_native_handle_does_not_crash_the_open_window(self):
        # A plain white titlebar is cosmetic; raising here would take down a
        # window that otherwise opened successfully.
        window = unittest.mock.Mock()
        window.native.Handle.ToInt32.side_effect = AttributeError("no handle")

        with patch.object(sys, "platform", "win32"):
            desktop._use_dark_titlebar(window)  # must not raise


class ServerTests(unittest.TestCase):
    def test_free_port_is_actually_bindable(self):
        port = desktop.free_port()

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((desktop.LOOPBACK, port))  # must not raise

    def test_waiting_gives_up_rather_than_hanging(self):
        never = type("Server", (), {"started": False})()

        self.assertFalse(desktop._wait_until_serving(never, deadline=0.0))

    def test_waiting_returns_as_soon_as_the_server_is_up(self):
        started = type("Server", (), {"started": True})()

        self.assertTrue(
            desktop._wait_until_serving(started, deadline=float("inf")),
        )


class CheckOnlyTests(unittest.TestCase):
    def test_check_only_builds_everything_and_opens_no_window(self):
        # webview is absent on a headless runner; if check_only reached the
        # window this would raise ImportError rather than return.
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(sys.modules, {"webview": None}):
                result = desktop.run_desktop(
                    "example", "Example", ALIASES,
                    str(self._config(tmp)), port=9998, check_only=True,
                )

        self.assertEqual(result, 0)

    def test_check_only_reports_a_broken_application_rather_than_passing(self):
        # A smoke check that cannot fail is worse than none, because it reads
        # as proof the executable starts.
        with tempfile.TemporaryDirectory() as tmp:
            broken = {"example": {**ALIASES["example"], "app_module": "no_such_module"}}
            with self.assertRaises(ModuleNotFoundError):
                desktop.run_desktop(
                    "example", "Example", broken,
                    str(self._config(tmp)), port=9997, check_only=True,
                )

    def test_the_check_flag_is_accepted_and_not_taken_for_a_config_path(self):
        seen = {}

        def fake_run(*args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs
            return 0

        with patch.object(desktop, "run_desktop", fake_run):
            result = desktop.desktop_main(["--check"], "example", "Example", ALIASES)

        self.assertEqual(result, 0)
        self.assertTrue(seen["kwargs"]["check_only"])
        # The config path argument must still be None, not "--check".
        self.assertIsNone(seen["args"][3])

    def test_a_config_path_still_works_alongside_the_flag(self):
        seen = {}

        with patch.object(desktop, "run_desktop", lambda *a, **k: seen.update(
            args=a, kwargs=k,
        ) or 0):
            desktop.desktop_main(
                ["--check", "host.json"], "example", "Example", ALIASES,
            )

        self.assertEqual(seen["args"][3], "host.json")
        self.assertTrue(seen["kwargs"]["check_only"])

    @staticmethod
    def _config(directory):
        path = Path(directory) / "host.json"
        path.write_text(
            '{"storage_file": %s}' % repr(
                str(Path(directory) / "state.json"),
            ).replace("'", '"'),
            encoding="utf-8",
        )
        return path


class WindowAppearanceTests(unittest.TestCase):
    def _open_window(self, config_extra=None):
        created = {}

        class FakeWebview:
            @staticmethod
            def create_window(title, url, **kwargs):
                created.update(title=title, url=url, **kwargs)
                # Real create_window returns a window; run_desktop hooks its
                # `events.shown` for the dark-titlebar fix, which needs `+=`
                # support that plain Mock does not provide.
                return unittest.mock.MagicMock()

            @staticmethod
            def start():
                pass

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host.json"
            settings = {"storage_file": str(Path(tmp) / "state.json")}
            settings.update(config_extra or {})
            path.write_text(
                "{%s}" % ", ".join(
                    f'"{key}": "{value}"' for key, value in settings.items()
                ).replace("\\", "/"),
                encoding="utf-8",
            )
            with patch.dict(sys.modules, {"webview": FakeWebview}):
                desktop.run_desktop(
                    "example", "Example", ALIASES, str(path), port=9996,
                )
        return created

    def test_the_window_is_dark_before_the_page_paints(self):
        # pywebview defaults to white. Every application here is dark (U4),
        # so the default shows a white frame until the page loads and again
        # behind the content while resizing.
        created = self._open_window()

        self.assertEqual(created["background_color"], desktop.WINDOW_BACKGROUND)
        self.assertNotEqual(created["background_color"].upper(), "#FFFFFF")

    def test_an_application_can_choose_its_own_window_background(self):
        created = self._open_window({"window_background": "#1c1a17"})

        self.assertEqual(created["background_color"], "#1c1a17")

    def test_the_window_carries_the_supplied_title(self):
        created = self._open_window()

        self.assertEqual(created["title"], "Example")


class WindowSessionTests(unittest.TestCase):
    """Exercise the real server lifecycle with only the window faked.

    WindowAppearanceTests above fakes `start()` as a no-op and reads back what
    `create_window` was given. These let the server actually serve: the fake
    window fetches its own URL, which is what makes the frozen build's
    blank-window failure visible, and the session has to reach disk on close.
    No port is pinned, so `free_port` is exercised too.
    """

    def _fake_webview(self, opened: dict):
        import types
        import urllib.request

        module = types.ModuleType("webview")

        def create_window(title, url, **kwargs):
            opened["title"] = title
            opened["url"] = url
            # Real pywebview returns a window object, and run_desktop hooks its
            # events.shown to darken the native titlebar. Returning None made
            # this fake diverge from the thing it stands in for, so a change
            # that is correct against pywebview broke it.
            return MagicMock()

        def start():
            # The window would load this URL, so proving it serves here is
            # what makes the frozen build's blank-window failure visible.
            with urllib.request.urlopen(opened["url"], timeout=10) as response:
                opened["status"] = response.status
                opened["body"] = response.read(2048).decode("utf-8", "replace")

        module.create_window = create_window
        module.start = start
        return module

    def _run(self, opened: dict, directory: str):
        config_file = Path(directory) / "host.json"
        config_file.write_text(
            json.dumps({
                "storage_file": str(Path(directory) / "state.json"),
                "blob_store_dir": str(Path(directory) / "blobs"),
            }),
            encoding="utf-8",
        )
        with patch.dict(sys.modules, {"webview": self._fake_webview(opened)}):
            return desktop.run_desktop(
                "example", "Example", ALIASES, str(config_file),
            )

    def test_window_gets_a_served_page_and_the_session_is_saved_on_close(self):
        opened: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            code = self._run(opened, tmp)

            self.assertEqual(code, 0)
            self.assertEqual(opened["status"], 200)
            self.assertIn("<html", opened["body"].lower())
            # Closing the window must leave the session on disk, or the next
            # launch silently starts empty.
            self.assertTrue((Path(tmp) / "state.json").is_file())

    def test_the_window_opens_on_loopback(self):
        opened: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            self._run(opened, tmp)

        self.assertTrue(opened["url"].startswith(f"http://{desktop.LOOPBACK}:"))


class DataDirectoryTests(unittest.TestCase):
    """Where a frozen build keeps state, which is the whole risk here.

    Every test that reaches `desktop_config` redirects `data_directory` to a
    temporary one, because the real call creates the per-user directory as a
    side effect and a test suite must not leave state in it.
    """

    def _config(self, directory, config_path=None):
        with patch.object(desktop, "data_directory", return_value=Path(directory)):
            return desktop.desktop_config("example", ALIASES, "Example", config_path)

    def test_storage_is_not_derived_from_the_port(self):
        # A window picks a free port each start, so a port-derived filename
        # would lose the previous session every time.
        with tempfile.TemporaryDirectory() as tmp:
            first = self._config(tmp)
            second = self._config(tmp)

        self.assertEqual(first["storage_file"], second["storage_file"])
        for port in (9105, 51789):
            self.assertNotIn(str(port), first["storage_file"])

    def test_storage_does_not_depend_on_the_working_directory(self):
        # An executable started from the shell has no meaningful cwd, so an
        # absolute per-user path is the only thing that survives.
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)

            self.assertTrue(Path(config["storage_file"]).is_absolute())
            self.assertTrue(config["storage_file"].startswith(tmp))

    def test_an_explicit_storage_file_still_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            chosen = str(Path(tmp) / "chosen.json")
            config_file = Path(tmp) / "config.json"
            config_file.write_text(
                json.dumps({"storage_file": chosen}), encoding="utf-8",
            )
            config = self._config(tmp, str(config_file))

        self.assertEqual(config["storage_file"], chosen)

    def test_data_directory_is_user_scoped_and_named(self):
        directory = desktop.data_directory("Example")

        self.assertTrue(directory.is_absolute())
        self.assertEqual(directory.name, "Example")

    def test_binding_stays_on_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)

        self.assertEqual(config["bind_host"], desktop.LOOPBACK)

    def test_windows_uses_local_appdata(self):
        with patch.object(sys, "platform", "win32"), \
                patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\x\AppData\Local"}):
            directory = desktop.data_directory("Example")

        self.assertEqual(directory, Path(r"C:\Users\x\AppData\Local") / "Example")


class EntryPointTests(unittest.TestCase):
    def test_missing_pywebview_explains_the_install(self):
        # The window is the only thing pywebview is needed for, so its absence
        # must read as an install hint rather than a crash.
        with patch.object(desktop, "run_desktop", side_effect=ImportError("no webview")):
            code = desktop.desktop_main([], "example", "Example", ALIASES)

        self.assertEqual(code, 1)

    def test_too_many_arguments_are_rejected(self):
        code = desktop.desktop_main(
            ["one.json", "two.json"], "example", "Example", ALIASES,
        )

        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
