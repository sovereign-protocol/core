"""Independent public format versions for the S-Protocol runtime."""

# Keep these domains separate. A change in one format must not accidentally
# force every other consumer to upgrade.
PACKAGE_VERSION = "0.1.0"

PROTOCOL_SCHEMA_VERSION = 1

SESSION_ENVELOPE_FORMAT = "sovereign-session"
SESSION_ENVELOPE_VERSION = 1

CONNECT_TOKEN_VERSION = 1
CHANNEL_DESCRIPTOR_VERSION = 1

# Core-owned data layered on the generic protocol tree. This is not an
# application schema: the minimal public profile exists even with zero apps.
CORE_PROFILE_SCHEMA_VERSION = 1
