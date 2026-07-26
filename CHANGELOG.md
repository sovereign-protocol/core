# Changelog

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
