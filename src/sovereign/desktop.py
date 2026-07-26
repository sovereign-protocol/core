"""Serve a host into its own window instead of a browser tab.

This is host work, not application work: it owns a runtime, a server and a
shutdown, all of which are Core's. Applications reach it through the public
root and supply only what is theirs - which application to start, and what to
call the window.

Two defaults differ from the browser launcher, and both are about where state
lives rather than how it is drawn:

* the port is chosen at start-up, because a fixed one collides with whatever
  else is already listening on a desktop;
* the session file therefore cannot be derived from the port, and cannot sit
  under the working directory either - an executable started from the shell
  has no meaningful one. It goes to a per-user application directory, so the
  same data comes back regardless of port or launch location.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path


STARTUP_TIMEOUT_SECONDS = 20.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0
LOOPBACK = "127.0.0.1"

# What the window is painted with before the page arrives, and behind it
# while resizing. pywebview defaults to white, which flashes against every
# application in the project - U4 in DESIGN_UI_CONSISTENCY makes them all
# dark. This is the shell's own page colour, so the seam is invisible.
# An application can override it with `window_background` in its config.
WINDOW_BACKGROUND = "#0d1117"


def data_directory(application_name: str) -> Path:
    """The per-user directory holding this application's saved state."""
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(root) / application_name


def free_port() -> int:
    """Ask the OS for a port nothing else is using."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return int(probe.getsockname()[1])


def desktop_config(app_name: str, application_aliases: dict | None,
                   application_name: str, config_path: str | None = None) -> dict:
    from .app_server import load_config

    config = load_config(config_path, app_name, application_aliases)
    # Only fill in what has not already been decided; an explicit storage_file
    # in a config file stays authoritative.
    if not config.get("storage_file"):
        directory = data_directory(application_name)
        directory.mkdir(parents=True, exist_ok=True)
        config["storage_file"] = str(directory / f"{app_name}.json")
    config["bind_host"] = LOOPBACK
    return config


def _wait_until_serving(server, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            return True
        time.sleep(0.05)
    return False


# DWMWA_USE_IMMERSIVE_DARK_MODE. 20 is the stable value from Windows 10
# 20H1 onward; 19 is the same attribute on the handful of earlier 1809/1903
# builds still in use, and is tried only if 20 is rejected.
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY = 19


def _use_dark_titlebar(window) -> None:
    """Ask Windows to draw this window's native titlebar in dark mode.

    background_color paints the content area pywebview controls; the
    titlebar is drawn by the OS and answers to a different setting
    entirely, which is why fixing one left the other white. There is no
    pywebview option for this - it has to go through DwmSetWindowAttribute
    directly, and only after the native window exists, which is what the
    `shown` event guarantees.

    Windows-only and best-effort: an unsupported build, a non-Windows
    platform, or a native handle winforms does not expose all fail
    silently. A plain white titlebar is a cosmetic regression, not a
    reason to crash the window that is already open.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = window.native.Handle.ToInt32()
        dark = ctypes.c_int(1)
        dwmapi = ctypes.windll.dwmapi
        result = dwmapi.DwmSetWindowAttribute(
            hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(dark), 4,
        )
        if result != 0:
            dwmapi.DwmSetWindowAttribute(
                hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY, ctypes.byref(dark), 4,
            )
    except Exception:  # noqa: BLE001 - cosmetic only, never worth failing over
        pass


def ensure_streams() -> None:
    """Give the process real stdout/stderr, which a windowed build lacks.

    A frozen build with no console leaves both as None. uvicorn's default log
    configuration then asks `sys.stdout.isatty()` whether to colourise, which
    raises `AttributeError` inside `logging.config.dictConfig` and surfaces as
    "Unable to configure formatter 'default'" - before the window ever opens.
    Anything else that writes, including a traceback explaining a different
    failure, breaks the same way and for the same reason.

    Discarding is the honest default here: there is no console to read, and a
    window that refuses to start is worse than one whose server logs go
    nowhere. A stream that is already open is left alone, so running from a
    terminal is unaffected.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def run_desktop(app_name: str, window_title: str,
                application_aliases: dict | None = None,
                config_path: str | None = None,
                port: int | None = None,
                check_only: bool = False) -> int:
    """Serve on a background thread and show the window until it is closed.

    With `check_only`, everything up to the window is built and the call
    returns instead of opening one. That is what a frozen build can run on a
    machine with no desktop session, so a startup failure is caught by CI
    rather than by whoever double-clicks the executable first.
    """
    import uvicorn

    from .app_server import build_app, create_runtime

    # Before uvicorn, which is the first thing to touch stdout.
    ensure_streams()
    config = desktop_config(
        app_name, application_aliases, window_title, config_path,
    )
    port = port or free_port()
    runtime = create_runtime(port, config)
    server = uvicorn.Server(uvicorn.Config(
        build_app(runtime),
        host=config["bind_host"],
        port=port,
        log_level="error",
    ))
    if check_only:
        # The server object exists, which means the log configuration was
        # accepted and the application mounted. Nothing is served and no
        # window is opened, so this runs anywhere.
        return 0
    thread = threading.Thread(target=server.run, name="sovereign-host", daemon=True)
    thread.start()
    try:
        if not _wait_until_serving(
            server, time.monotonic() + STARTUP_TIMEOUT_SECONDS,
        ):
            raise RuntimeError("the local host did not start")
        # Imported only once the server is known to be healthy, so a missing
        # GUI toolkit is reported as itself rather than as a startup failure,
        # and so importing this module never requires one.
        import webview

        window = webview.create_window(
            window_title, f"http://{config['bind_host']}:{port}/",
            width=1280, height=860, min_size=(900, 600),
            background_color=config.get("window_background") or WINDOW_BACKGROUND,
        )
        window.events.shown += _use_dark_titlebar
        webview.start()
    finally:
        # The window is gone, so nothing can reach the session any more. Stop
        # serving before writing, so no in-flight request races the save.
        server.should_exit = True
        thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        runtime.persist()
    return 0


def desktop_main(argv: list[str] | None, app_name: str, window_title: str,
                 application_aliases: dict | None = None) -> int:
    """Command-line wrapper shared by every application's desktop entry.

    `--check` builds everything up to the window and exits, so a frozen build
    can prove it starts on a machine with no desktop session.
    """
    # A windowed build has no stdout, so even the usage message below would
    # raise before printing. Fix the streams first, not inside run_desktop.
    ensure_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in argv
    argv = [item for item in argv if item != "--check"]
    if len(argv) > 1:
        print(f"Usage: {app_name}-desktop [--check] [config.json]", file=sys.stderr)
        return 1
    try:
        return run_desktop(
            app_name, window_title, application_aliases,
            argv[0] if argv else None,
            check_only=check_only,
        )
    except ImportError:
        print(
            "The desktop window needs pywebview.\n"
            "Install it with the application's 'desktop' extra.",
            file=sys.stderr,
        )
        return 1
