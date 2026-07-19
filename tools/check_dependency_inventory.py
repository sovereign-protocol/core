"""Fail when the resolved runtime closure drifts from the reviewed inventory."""

from __future__ import annotations

import importlib.metadata as metadata
import json
from pathlib import Path

from packaging.requirements import Requirement


ROOTS = ("paramiko", "requests", "starlette", "uvicorn")


def normalized(value: str) -> str:
    return value.lower().replace("_", "-")


def resolved_closure() -> dict[str, str]:
    pending = list(ROOTS)
    found: dict[str, str] = {}
    while pending:
        distribution = metadata.distribution(pending.pop())
        name = normalized(distribution.metadata["Name"])
        if name in found:
            continue
        found[name] = distribution.version
        for raw in distribution.requires or ():
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate():
                pending.append(requirement.name)
    return found


def main() -> int:
    inventory_path = Path(__file__).resolve().parents[1] / "dependency-inventory.json"
    expected = json.loads(inventory_path.read_text(encoding="utf-8"))
    actual = resolved_closure()
    expected_versions = {
        normalized(name): item["version"] for name, item in expected.items()
    }
    if actual != expected_versions:
        missing = sorted(set(expected_versions) - set(actual))
        added = sorted(set(actual) - set(expected_versions))
        changed = sorted(
            name for name in set(actual) & set(expected_versions)
            if actual[name] != expected_versions[name]
        )
        raise SystemExit(
            "dependency inventory drift; review licenses and update both inventory "
            f"files (missing={missing}, added={added}, changed={changed})"
        )
    print(f"dependency inventory matches {len(actual)} resolved packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
