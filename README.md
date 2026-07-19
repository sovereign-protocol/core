# Sovereign Core

Sovereign Core is the application-neutral reference implementation of the
Sovereign Protocol (S-Protocol): local-first collaboration in which every
participant owns an explicit perspective and decides how differences converge.

It provides the protocol tree, Session transition and reaction mechanics,
application hosting, direct HTTP channels, Local/SFTP mailbox channels, and
content-addressed blob storage. It contains no product-application policy.

## Quickstart

Requires Python 3.10 or newer. Windows 10/11 is supported; Linux and macOS are
currently experimental.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\sovereign-host.exe 9305:manual
```

The public Python surface is documented in `PUBLIC_API.md`. Protocol behavior
and wire formats are defined in `SPECIFICATION_S_PROTOCOL.md`.

## License

Software is `LGPL-3.0-or-later`. Documentation and the normative specification
are `CC-BY-4.0`. See `LICENSE`, `LICENSES/`, and `NOTICE`.
