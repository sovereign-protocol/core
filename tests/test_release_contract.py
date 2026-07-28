"""A published version keeps one reviewable API and wire-format identity."""

import json
import re
import unittest
from pathlib import Path

import sovereign
from sovereign.versions import (
    CHANNEL_DESCRIPTOR_VERSION,
    CONNECT_TOKEN_VERSION,
    CORE_PROFILE_SCHEMA_VERSION,
    PACKAGE_VERSION,
    PROTOCOL_SCHEMA_VERSION,
    SESSION_ENVELOPE_VERSION,
)


ROOT = Path(__file__).resolve().parents[1]


class ReleaseContractTests(unittest.TestCase):
    def test_current_version_matches_its_immutable_release_contract(self):
        contract_path = ROOT / "release-contracts" / f"{PACKAGE_VERSION}.json"
        self.assertTrue(
            contract_path.is_file(),
            "a new package version needs a new release contract",
        )
        contract = json.loads(contract_path.read_text(encoding="utf-8"))

        self.assertEqual(contract["package_version"], PACKAGE_VERSION)
        self.assertEqual(contract["public_api"], sorted(sovereign.__all__))
        self.assertEqual(contract["format_versions"], {
            "channel_descriptor": CHANNEL_DESCRIPTOR_VERSION,
            "connect_token": CONNECT_TOKEN_VERSION,
            "core_profile_schema": CORE_PROFILE_SCHEMA_VERSION,
            "protocol_schema": PROTOCOL_SCHEMA_VERSION,
            "session_envelope": SESSION_ENVELOPE_VERSION,
        })

    def test_distribution_metadata_uses_the_contract_version(self):
        source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(
            r'(?ms)^\[project\]\s.*?^version\s*=\s*"([^"]+)"',
            source,
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), PACKAGE_VERSION)


if __name__ == "__main__":
    unittest.main()
