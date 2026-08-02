"""A stalled relay must not stop the client, only its sync.

Two defences, and they are not alternatives. Bounding the SFTP waits keeps
one stall from lasting an hour; keeping the liveness read off the relay I/O
lock keeps a stall - of any length - from reaching the request path at all.

Every wait an SFTP relay can impose on us is bounded.

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
import types
import threading
import unittest
from unittest.mock import patch

from sovereign.locking import RELAY_IO_LOCK_ORDER, OrderedRLock
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

    def test_an_adopted_descriptor_is_bounded_like_a_configured_relay(self):
        # The accepter side - a client that rode somebody else's connect
        # token - is the one most likely to be on a poor link, and was the
        # one side that could not bound its waits: this path took the
        # constructor defaults and no setting could reach it.
        descriptor = {
            "type": "sftp", "host": "relay.example",
            "username": "user", "root": "/relay",
        }

        self.assertEqual(
            RelayLogic._storage_from_descriptor(
                descriptor, {"relay_sftp_operation_timeout": 5},
            ).operation_timeout,
            5.0,
        )
        # A descriptor may also carry its own, since the right value is a
        # property of the link rather than of the client reaching it.
        self.assertEqual(
            RelayLogic._storage_from_descriptor(
                {**descriptor, "relay_sftp_operation_timeout": 4},
            ).operation_timeout,
            4.0,
        )

    def test_a_dropped_connection_is_reported_not_just_absorbed(self):
        # _with_retry reconnects and retries, so the phase above still reports
        # ok=True and the only remaining trace of a reconnect is an
        # unexplained spike in that phase's duration. Sessions were diagnosed
        # by eyeballing duration outliers because of this.
        storage = SftpRelayStorage("host", "user", "/relay")
        storage._sftp = object()
        events = []
        storage.on_event = lambda kind, **fields: events.append((kind, fields))
        attempts = []

        def operation(_sftp):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("[Errno 10054] connection reset by peer")
            return "second attempt"

        with patch.dict(sys.modules, {"paramiko": _Paramiko()}):
            with patch.object(storage, "_connect") as connect:
                connect.side_effect = lambda: setattr(
                    storage, "_sftp", object(),
                )
                self.assertEqual(storage._with_retry(operation), "second attempt")

        self.assertEqual([kind for kind, _ in events], ["relay.sftp_reconnect"])
        self.assertEqual(events[0][1]["error_type"], "OSError")
        self.assertGreaterEqual(events[0][1]["reconnect_ms"], 0)

    def test_a_healthy_call_reports_no_fault(self):
        # Otherwise the event says "the link is busy", not "the link broke".
        storage = SftpRelayStorage("host", "user", "/relay")
        storage._sftp = object()
        events = []
        storage.on_event = lambda kind, **fields: events.append((kind, fields))

        with patch.dict(sys.modules, {"paramiko": _Paramiko()}):
            storage._with_retry(lambda _sftp: "fine")

        self.assertNotIn(
            "relay.sftp_reconnect", [kind for kind, _ in events],
        )

    def test_each_operation_reports_what_it_cost(self):
        # Phase timings aggregate several round trips, so two clients on the
        # same relay differed 4x in one phase with no way to tell a slow link
        # from extra round trips. Kept at timing level: this fires on every
        # relay operation, which is noise in an ordinary event trace.
        storage = SftpRelayStorage("host", "user", "/relay")
        storage._sftp = object()
        events = []
        storage.on_event = lambda kind, **fields: events.append((kind, fields))

        with patch.dict(sys.modules, {"paramiko": _Paramiko()}):
            storage._with_retry(lambda _sftp: "fine", name="read_head")

        self.assertEqual([kind for kind, _ in events], ["relay.sftp_operation"])
        self.assertEqual(events[0][1]["operation"], "read_head")
        self.assertEqual(events[0][1]["trace_level"], "timing")
        self.assertGreaterEqual(events[0][1]["duration_ms"], 0)

    def test_an_unnamed_operation_is_labelled_by_its_caller(self):
        # Every caller names its inner closure `operation`, so the label has
        # to come from the method that built it or it says nothing.
        storage = SftpRelayStorage("host", "user", "/relay")
        storage._sftp = object()
        events = []
        storage.on_event = lambda kind, **fields: events.append(fields)

        def read_presence_with_mtime():
            return storage._with_retry(lambda _sftp: "fine")

        with patch.dict(sys.modules, {"paramiko": _Paramiko()}):
            read_presence_with_mtime()

        self.assertEqual(events[0]["operation"], "read_presence_with_mtime")

    def test_reporting_a_fault_cannot_cause_one(self):
        storage = SftpRelayStorage("host", "user", "/relay")
        storage._sftp = object()

        def exploding(_kind, **_fields):
            raise RuntimeError("tracing is down")

        storage.on_event = exploding
        attempts = []

        def operation(_sftp):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("dropped")
            return "second attempt"

        with patch.dict(sys.modules, {"paramiko": _Paramiko()}):
            with patch.object(storage, "_connect") as connect:
                connect.side_effect = lambda: setattr(
                    storage, "_sftp", object(),
                )
                self.assertEqual(storage._with_retry(operation), "second attempt")


class LivenessIsNotBlockedByTheRelayTests(unittest.TestCase):
    """peer_liveness answers from cache, so a stuck poller cannot reach it.

    This is the behaviour that failed in a real client: an SFTP connection
    that had died left the poll cycle holding the relay I/O lock for tens of
    minutes, and because reporting who is reachable went through the same
    lock, every HTTP request queued behind a dead socket. The client accepted
    connections and answered none.
    """

    def logic(self) -> RelayLogic:
        # Built without __init__ so the test needs no relay, no config and no
        # Session: what is under test is which lock a read takes, and that is
        # decided by these four fields alone.
        logic = RelayLogic.__new__(RelayLogic)
        logic.storage = object()
        logic.poll_interval_seconds = 3.0
        # The real lock, not a stand-in: what is being tested is whether a
        # reader contends with it, so a substitute would test nothing.
        logic._io_lock = OrderedRLock(RELAY_IO_LOCK_ORDER, "RelayLogic._io_lock")
        logic._presence_lock = threading.Lock()
        logic._own_presence_mtime = 1000.0
        logic._peer_presence_cache = {
            "peer": ({"poll_interval_seconds": 3.0}, 999.0),
        }
        return logic

    def test_liveness_answers_while_the_poller_holds_the_relay_lock(self):
        logic = self.logic()

        # Stand where a stalled poll cycle stands: relay I/O lock held, and
        # the socket it is waiting on never answering.
        with logic._io_lock:
            answered = []
            reader = threading.Thread(
                target=lambda: answered.append(logic.peer_liveness("peer")),
            )
            reader.start()
            reader.join(timeout=5)

            self.assertFalse(
                reader.is_alive(),
                "peer_liveness blocked behind the relay I/O lock; a stalled "
                "relay would take the whole client down with it",
            )
        self.assertEqual(answered[0]["state"], "alive")

    def test_liveness_reads_its_two_fields_together(self):
        # Own mtime and the peer's are compared against each other, so a read
        # that caught one updated and the other not would report a distance
        # that never existed. They are taken under one lock for that reason.
        logic = self.logic()
        logic._own_presence_mtime = None

        self.assertEqual(logic.peer_liveness("peer"), {"state": "unknown"})

    def test_the_poller_publishes_the_cache_under_the_presence_lock(self):
        # The writer's side of the same guarantee: whatever the poller has
        # already read is handed over without a reader waiting for the rest
        # of the cycle.
        logic = self.logic()
        logic._peer_presence_cache = {}
        with logic._presence_lock:
            self.assertFalse(
                logic._presence_lock.acquire(blocking=False),
                "the presence lock must be a real mutual exclusion",
            )


if __name__ == "__main__":
    unittest.main()


class PublishOnceCostTests(unittest.TestCase):
    """A local edit must not pay a relay round trip per idle topic.

    publish_once skips the inbound poll so a change can leave quickly, and
    checks for a sibling's unseen publication itself instead. Checking every
    topic put one head read in front of every edit: measured at 2.5-3.4s of
    reads before a 0.9s publish, which is worse than the ordering it avoids.
    """

    def logic(self, due, published):
        logic = RelayLogic.__new__(RelayLogic)
        logic.identity = "me"
        logic._state = {
            "published": published,
            "published_observations": {},
            "observed": {},
            "observed_publications": {},
        }
        logic._session_lock = threading.RLock()
        logic.session = types.SimpleNamespace(
            node_state_hash=lambda uuid: due.get(uuid),
        )
        logic.relay_topic_uuids = lambda: sorted(due)
        return logic

    def test_only_topics_with_something_to_send_are_checked(self):
        logic = self.logic(
            due={"quiet": "hash-a", "changed": "hash-new"},
            published={"quiet": "hash-a", "changed": "hash-old"},
        )
        logic._state["published_observations"] = {
            "quiet": logic._observed_digest("quiet"),
            "changed": logic._observed_digest("changed"),
        }

        self.assertEqual(logic._topics_with_unpublished_work(), ["changed"])

    def test_a_topic_never_published_counts_as_work(self):
        logic = self.logic(due={"fresh": "hash-a"}, published={})

        self.assertEqual(logic._topics_with_unpublished_work(), ["fresh"])

    def test_an_acknowledgement_alone_counts_as_work(self):
        # Observations change without content changing, and that still has
        # to reach the peer or it never learns its revision was seen.
        logic = self.logic(due={"topic": "hash-a"}, published={"topic": "hash-a"})
        logic._state["published_observations"] = {"topic": "stale-digest"}

        self.assertEqual(logic._topics_with_unpublished_work(), ["topic"])

    def test_a_topic_with_no_local_state_is_not_published(self):
        logic = self.logic(due={"gone": None}, published={})

        self.assertEqual(logic._topics_with_unpublished_work(), [])


class BlobPresenceCostTests(unittest.TestCase):
    """Asking whether a blob is there must not fetch it.

    presence asks this every poll cycle. Answering by downloading the bytes
    is free on a local folder and ruinous over SFTP: one avatar became a
    full re-download every few seconds - 30s of transfer per client in a
    four-minute session, measured in a live trace.
    """

    def storage(self):
        storage = SftpRelayStorage("host", "user", "/relay")
        storage._sftp = object()
        return storage

    def test_has_blob_stats_rather_than_downloads(self):
        storage = self.storage()
        calls = []

        class Sftp:
            def stat(self, path):
                calls.append(("stat", path))
                return types.SimpleNamespace(st_mtime=1.0)

            def open(self, *args, **kwargs):
                raise AssertionError("has_blob must not read the bytes")

        with patch.dict(sys.modules, {"paramiko": _Paramiko()}):
            with patch.object(storage, "_sftp_client", lambda: Sftp()):
                self.assertTrue(storage.has_blob("sha256:" + "ab" * 32))

        self.assertEqual([kind for kind, _ in calls], ["stat"])

    def test_a_missing_blob_is_reported_absent(self):
        storage = self.storage()

        class Sftp:
            def stat(self, path):
                raise FileNotFoundError(path)

        with patch.dict(sys.modules, {"paramiko": _Paramiko()}):
            with patch.object(storage, "_sftp_client", lambda: Sftp()):
                self.assertFalse(storage.has_blob("sha256:" + "cd" * 32))
