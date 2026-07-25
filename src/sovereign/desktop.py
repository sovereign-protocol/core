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


def run_desktop(app_name: str, window_title: str,
                application_aliases: dict | None = None,
                config_path: str | None = None,
                port: int | None = None) -> int:
    """Serve on a background thread and show the window until it is closed."""
    import uvicorn

    from .app_server import build_app, create_runtime

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

        webview.create_window(
            window_title, f"http://{config['bind_host']}:{port}/",
            width=1280, height=860, min_size=(900, 600),
        )
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
    """Command-line wrapper shared by every application's desktop entry."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) > 1:
        print(f"Usage: {app_name}-desktop [config.json]", file=sys.stderr)
        return 1
    try:
        return run_desktop(
            app_name, window_title, application_aliases,
            argv[0] if argv else None,
        )
    except ImportError:
        print(
            "The desktop window needs pywebview.\n"
            "Install it with the application's 'desktop' extra.",
            file=sys.stderr,
        )
        return 1
