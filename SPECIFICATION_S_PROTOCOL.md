# Sovereign Protocol serialization and hashing specification

Status: normative for protocol schema version `1` during the `0.x` series.
Breaking changes are permitted before `1.0`; incompatible input is rejected and
is not migrated automatically.

## Version domains

The following versions are independent and must not share a wire field:

| Domain | Current field/value |
|---|---|
| Python distribution | `0.1.0` |
| Protocol tree envelope | `protocol_schema_version: 1` |
| Session persistence envelope | `format: sovereign-session`, `version: 1` |
| Connect token | `token_version: 1` |
| Channel descriptor | `descriptor_version: 1` |
| Application data schema | owned and versioned by each application |
| Application facade/API | owned and versioned by each application facade |

## Canonical hash input

Hash input is UTF-8 JSON with object keys sorted and separators `,` and `:`;
no insignificant whitespace is present. The digest is SHA-256, represented by
its first 20 lowercase hexadecimal characters.

`content_hash` covers exactly:

```json
{"data":{},"deleted":false,"weights":{}}
```

The shown values are illustrative. A node's UUID, parent, timestamps,
children, revision base, and revision origin are excluded.

`state_hash` covers the node's `content_hash` and a list of every immediate
child's `[uuid, state_hash]` pair. The pairs are sorted lexicographically.
Consequently sibling order is not shared, while replacing a child with a
different UUID is detectable.

## Protocol node

A serialized node contains:

- `uuid`, `created_at`, `updated_at`;
- `data`, `weights`, `deleted`, `parent_uuid`, and recursive `children`;
- verified `content_hash` and `state_hash`;
- `base_hash`, `base_parent_uuid`, and `revision_origin`.

`base_hash` is the node's content hash before the current revision wave.
Successive edits by the same `revision_origin` compound against that base.
Adoption preserves the origin; an independent edit by another origin starts a
new wave. Revision metadata is deliberately excluded from both hashes.

The retired field `revision_origin_identity` is invalid in schema version 1.

## Protocol tree envelope

Every subtree crossing a channel uses:

```json
{
  "protocol_schema_version": 1,
  "subtree": {"...": "Protocol node"},
  "parent_uuid": null
}
```

Missing or unknown protocol versions are rejected. Hash damage inside a known
schema may be repaired on an explicitly repair-capable ingestion path; a schema
version mismatch must never be treated as hash damage.

The executable golden example is
[`tests/fixtures/protocol_tree_v1.json`](tests/fixtures/protocol_tree_v1.json).

## Other envelopes

A saved session contains `format`, `version`, `protocol_schema_version`,
`protocol_root`, and Session-owned metadata. A connect token contains
`token_version`, identity, topic UUIDs, and channel descriptors. Every channel
descriptor contains its own `descriptor_version` and channel-specific fields.

These outer formats may evolve independently from the protocol tree schema.
