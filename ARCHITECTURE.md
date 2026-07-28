# Architecture

Dependency direction is one-way:

```text
applications -> public Sovereign Core API
Core host -> registered application contracts
Session -> Protocol
ChannelManager -> mailbox channel implementation
mailbox channel -> Local/SFTP storage backends
```

Protocol and Session do not import applications, HTTP controllers, channels, or
storage. Applications own domain policy and UI. Channels exchange registered
topic trees without knowing their node types.

See `DESIGN_PUBLIC_ARCHITECTURE_REFACTOR.md` for the accepted boundary decisions.
