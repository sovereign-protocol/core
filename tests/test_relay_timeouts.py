"""Every wait an SFTP relay can impose on us is bounded.

Paramiko's connect timeout covers the TCP handshake and nothing after it.
A connection that dies once established - a laptop that slept, a network
that dropped mid-session - leaves the socket open and every later read
waiting on a peer that will never answer. Because the relay poll cycle runs
those reads under the relay I/O lock, one such socket stalls the cycle for
as long as the OS keeps the connection: phases of over an hour have been
observed in a real client, during which nothing else that needs relay state
can proceed.

So the interesting assertions here are not "a timeout is set" but "each of
the three distinct waits paramiko can make is bounded, including the ones
that only exist after a connection is established".
"""

import sys
import unittest
from unittest.mock import patch

from sovereign.relay_logic import RelayLogic
from sovereign.relay_storage import SftpRelayStorage


class _Channel:
    def __init__(self):
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value


class _Sftp:
    def __init__(self):
        self.channel = _Channel()

    def get_channel(self):
        return self.channel


class _Transport:
    def __init__(self):
        self.keepalive = None

    def set_keepalive(self, value):
        self.keepalive = value


class _Client:
    def __init__(self):
        self.connect_kwargs = None
        self.transport = _Transport()
        self.sftp = _Sftp()

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs

    def get_transport(self):
        return self.transport

    def open_sftp(self):
        return self.sftp


class _Paramiko:
    """Enough of paramiko to observe how the connection is set up."""

    AuthenticationException = type("AuthenticationException", (Exception,), {})
    SSHException = type("SSHException", (Exception,), {})

    def __init__(self):
        self.client = _Client()

    def SSHClient(self):  # noqa: N802 - mirrors paramiko's own name
        return self.client

    def AutoAddPolicy(self):  # noqa: N802 - mirrors paramiko's own name
        return "auto-add"


class SftpTimeoutTests(unittest.TestCase):
    def connect(self, **kwargs) -> _Paramiko:
        fake = _Paramiko()
        storage = SftpRelayStorage("host", "user", "/relay", **kwargs)
        with patch.dict(sys.modules, {"paramiko": fake}):
            storage._connect()
        return fake

    def test_every_stage_of_connecting_is_bounded(self):
        fake = self.connect(connect_timeout=7.0)

        # Three separate waits, and paramiko bounds each one separately: a
        # host that accepts the connection and then says nothing gets past
        # the first regardless of how short it is.
        self.assertEqual(fake.client.connect_kwargs["timeout"], 7.0)
        self.assertEqual(fake.client.connect_kwargs["banner_timeout"], 7.0)
        self.assertEqual(fake.client.connect_kwargs["auth_timeout"], 7.0)

    def test_reads_on_an_established_connection_are_bounded(self):
        fake = self.connect(operation_timeout=12.0, keepalive_seconds=4.0)

        # The failure that actually stalled a client: the connection is up,
        # the peer is gone, and nothing bounds the read.
        self.assertEqual(fake.client.sftp.channel.timeout, 12.0)
        self.assertEqual(fake.client.transport.keepalive, 4)

    def test_the_bounds_hold_without_anyone_configuring_them(self):
        fake = self.connect()

        # Defaulted rather than required: the stall being prevented must not
        # depend on somebody having thought to configure it.
        for key in ("timeout", "banner_timeout", "auth_timeout"):
            self.assertGreater(fake.client.connect_kwargs[key], 0)
        self.assertGreater(fake.client.sftp.channel.timeout, 0)
        self.assertGreater(fake.client.transport.keepalive, 0)

    def test_a_timed_out_read_resets_and_retries_like_a_dropped_one(self):
        # TimeoutError is an OSError, so it takes the existing reset-and-
        # retry path rather than escaping as an unhandled error. That is
        # what bounds one operation at roughly two timeouts instead of
        # leaving it open ended.
        storage = SftpRelayStorage("host", "user", "/relay")
        storage._sftp = object()
        attempts = []

        def operation(_sftp):
            attempts.append(1)
            if len(attempts) == 1:
                raise TimeoutError("timed out")
            return "second attempt"

        with patch.dict(sys.modules, {"paramiko": _Paramiko()}):
            with patch.object(storage, "_connect") as connect:
                connect.side_effect = lambda: setattr(
                    storage, "_sftp", object(),
                )
                self.assertEqual(storage._with_retry(operation), "second attempt")

        self.assertEqual(len(attempts), 2)

    def test_timeouts_are_configurable_because_links_differ(self):
        # A relay reached over a slow tunnel legitimately needs longer than
        # one on a LAN, so the value belongs in configuration - but only the
        # keys actually given override the defaults.
        self.assertEqual(RelayLogic._sftp_timeouts({}), {})
        self.assertEqual(
            RelayLogic._sftp_timeouts({
                "relay_sftp_connect_timeout": "5",
                "relay_sftp_operation_timeout": 20,
                "relay_sftp_keepalive_seconds": 8,
            }),
            {
                "connect_timeout": 5.0,
                "operation_timeout": 20.0,
                "keepalive_seconds": 8.0,
            },
        )

    def test_configured_timeouts_reach_the_storage(self):
        storage = RelayLogic._build_storage({
            "relay_backend": "sftp",
            "relay_sftp_host": "relay.example",
            "relay_sftp_username": "user",
            "relay_sftp_root": "/relay",
            "relay_sftp_operation_timeout": 45,
        })

        self.assertEqual(storage.operation_timeout, 45.0)


if __name__ == "__main__":
    unittest.main()
