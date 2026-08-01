# Roadmap

## 0.1

- Publish the S-Protocol specification and application-neutral Core.
- Support Local/SFTP mailbox channels. Direct HTTP was retired on
  2026-07-28: it needs a routable address between peers, which for this
  project's users means port forwarding or a VPN, and it was the one
  channel that could not have a home for a topic.
- Stabilize application registration, perspectives, reactions, and blobs.
- Exercise the public API with S-Initiative and S-Team, and keep the minimal
  notes example in `examples/notes` installed by CI so Core always proves its
  contract against a real application rather than only test stubs.

## Later

- Verify Linux and macOS.
- Add WebDAV after the mailbox storage contract is public and stable.
- Consider 1.0 only after at least two product applications exercise Core.
  S-Team became the second on 2026-07-26, when it left this repository
  for its own.
