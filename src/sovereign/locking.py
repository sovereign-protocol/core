"""Debug-enforced lock hierarchy for Core's concurrent layers."""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager


MANAGER_LOCK_ORDER = 10
RELAY_IO_LOCK_ORDER = 20
SESSION_LOCK_ORDER = 30

_held = threading.local()


def _stack() -> list["OrderedRLock"]:
    stack = getattr(_held, "stack", None)
    if stack is None:
        stack = []
        _held.stack = stack
    return stack


class OrderedRLock(AbstractContextManager):
    """A re-entrant lock that rejects reverse cross-layer acquisition.

    Checks run in normal/debug Python and disappear with ``python -O``.
    The hierarchy is manager < relay I/O < Session.

    Two distinct locks of the *same* layer are rejected as well. Nothing
    orders one relay connection's I/O lock against another's, so holding
    both at once would deadlock against a thread that takes them the other
    way round; each connection's cycle must complete on its own.
    """

    def __init__(self, order: int, name: str):
        self.order = int(order)
        self.name = str(name)
        self._lock = threading.RLock()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        stack = _stack()
        reentrant = self in stack
        if __debug__ and not reentrant:
            held = next(
                (item for item in reversed(stack) if item.order >= self.order),
                None,
            )
            if held is not None:
                detail = (
                    f"a second {self.name} while one is already held"
                    if held.order == self.order
                    else f"{self.name} after {held.name}"
                )
                raise RuntimeError(
                    f"lock order violation: acquire {detail}; required "
                    "order is manager < relay I/O < Session, one lock per "
                    "layer"
                )
        acquired = (
            self._lock.acquire(blocking)
            if timeout == -1
            else self._lock.acquire(blocking, timeout)
        )
        if acquired:
            stack.append(self)
        return acquired

    def release(self) -> None:
        stack = _stack()
        if __debug__ and (not stack or stack[-1] is not self):
            held = stack[-1].name if stack else "nothing"
            raise RuntimeError(
                f"lock release order violation: release {self.name} while "
                f"{held} is innermost"
            )
        self._lock.release()
        if stack:
            stack.pop()

    def __enter__(self) -> "OrderedRLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()

    def is_owned(self) -> bool:
        return self in _stack()

    def assert_owned(self) -> None:
        if __debug__ and not self.is_owned():
            raise RuntimeError(f"{self.name} must be held by the current thread")
