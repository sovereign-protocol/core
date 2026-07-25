#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Filter Sovereign Core trace JSONL files by event text.",
    )
    parser.add_argument("files", nargs="+")
    parser.add_argument("--contains", "-c", action="append", default=[])
    parser.add_argument("--kind")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    emitted = 0
    for file_name in args.files:
        for line in Path(file_name).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if args.kind and record.get("kind") != args.kind:
                continue
            text = json.dumps(record, sort_keys=True)
            if any(value not in text for value in args.contains):
                continue
            print(format_record(record))
            emitted += 1
            if emitted >= args.limit:
                return


def format_record(record: dict) -> str:
    parts = [
        record.get("ts", ""),
        record.get("node", ""),
        record.get("kind", ""),
    ]
    for key in (
        "peer_addr",
        "peer_id",
        "relay_identity",
        "target",
        "topic_uuid",
        "node_uuid",
        "changed_uuid",
        "event_type",
        "reason",
        "old_state_hash",
        "local_state_hash",
        "peer_state_hash",
        "incoming_state_hash",
        "state_hash",
        "publication_seq",
        "previous_publication_seq",
        "ack_publication_seq",
        "observed_local_publication_seq",
        "current_publication_seq",
        "ack_requested",
        "acknowledgement_pending",
        "phase",
        "duration_ms",
        "next_poll_ms",
        "acknowledgement_wait_ms",
    ):
        if record.get(key) is not None:
            parts.append(f"{key}={record[key]}")
    return " | ".join(str(part) for part in parts if part)


if __name__ == "__main__":
    main()
