import unittest

from sovereign.locking import (
    MANAGER_LOCK_ORDER,
    RELAY_IO_LOCK_ORDER,
    SESSION_LOCK_ORDER,
    OrderedRLock,
)


class LockOrderTests(unittest.TestCase):
    def setUp(self):
        self.manager = OrderedRLock(MANAGER_LOCK_ORDER, "manager")
        self.io = OrderedRLock(RELAY_IO_LOCK_ORDER, "io")
        self.session = OrderedRLock(SESSION_LOCK_ORDER, "session")

    def test_declared_manager_io_session_order_is_allowed(self):
        with self.manager:
            with self.io:
                with self.session:
                    self.session.assert_owned()

    def test_session_to_manager_reverse_edge_is_rejected(self):
        with self.session:
            with self.assertRaisesRegex(RuntimeError, "lock order violation"):
                self.manager.acquire()

    def test_io_to_manager_reverse_edge_is_rejected(self):
        with self.io:
            with self.assertRaisesRegex(RuntimeError, "lock order violation"):
                self.manager.acquire()

    def test_reentrant_acquisition_remains_supported(self):
        with self.session:
            with self.session:
                self.session.assert_owned()

    def test_two_locks_of_the_same_layer_are_rejected(self):
        # Nothing orders one connection's relay I/O lock against another's.
        other_io = OrderedRLock(RELAY_IO_LOCK_ORDER, "io-other")
        with self.io:
            with self.assertRaisesRegex(RuntimeError, "a second io-other"):
                other_io.acquire()

    def test_same_layer_locks_are_allowed_sequentially(self):
        other_io = OrderedRLock(RELAY_IO_LOCK_ORDER, "io-other")
        with self.io:
            pass
        with other_io:
            other_io.assert_owned()

    def test_positive_ownership_assertion_rejects_unlocked_reads(self):
        with self.assertRaisesRegex(RuntimeError, "must be held"):
            self.session.assert_owned()


if __name__ == "__main__":
    unittest.main()
