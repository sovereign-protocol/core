# Changelog

## 0.1.2 - 2026-07-26

- **Fixed: a windowed executable crashed on launch.** A frozen build with no
  console leaves `sys.stdout` and `sys.stderr` as `None`, and uvicorn's
  default log configuration asks `sys.stdout.isatty()` whether to colourise.
  That raised inside `logging.config.dictConfig` and surfaced as
  `ValueError: Unable to configure formatter 'default'`, with nothing in the
  message naming stdout. Every `console=False` build was affected, and it
  failed before the window appeared, so nothing was visible to the user
  except a traceback. `run_desktop` now gives the process discard streams
  when it has none.
- `desktop_main` accepts `--check`: it builds the runtime and the server,
  then returns without opening a window. A frozen build can run it on a
  machine with no desktop session, which is what lets CI prove the
  executable starts rather than only that it links.

No API removal, wire or persistence change. `run_desktop` gains an optional
`check_only` argument.

## 0.1.1 - 2026-07-26

- Blob transfer emits trace events. A blob cached, a referenced blob the
  relay does not hold, and a malformed identifier are now all recorded.
  Previously this path was silent, so an avatar reference that synced
  while its bytes did not left nothing in the logs to find.
- A publication held back because a referenced blob is missing locally is
  traced rather than only printed.

No API, wire or persistence change.

## 0.1.0 - 2026-07-26

- Initial public-alpha architecture.
- S-Protocol tree, Session perspectives, transitions, adopt and rollback.
- Direct HTTP and Local/SFTP mailbox channels.
- Generic application host, profile, protocol explorer, and blob storage.
