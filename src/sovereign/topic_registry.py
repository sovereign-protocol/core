"""Application-neutral registry for shared protocol topic roots.

Applications register the root node types they own, how to enumerate their
local topics, and how an invited topic is mounted into the local tree. Channel
implementations consume only this contract and never import an application.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .protocol import ProtocolNode


@dataclass(frozen=True)
class SharedTopicHandler:
    owner: str
    root_types: frozenset[str]
    list_topics: Callable[[], Iterable[str | ProtocolNode]]
    accept_invitation: Callable[[ProtocolNode], Any]
    assignment_scoped: bool
    mount_invitation: bool


class SharedTopicRegistry:
    """Runtime-only application topic handlers for one Session."""

    def __init__(self):
        self._lock = threading.RLock()
        self._handlers_by_owner: dict[str, SharedTopicHandler] = {}
        self._owner_by_root_type: dict[str, str] = {}

    def register(
        self,
        owner: str,
        root_types: Iterable[str],
        list_topics: Callable[[], Iterable[str | ProtocolNode]],
        accept_invitation: Callable[[ProtocolNode], Any],
        *,
        assignment_scoped: bool = True,
        mount_invitation: bool = True,
    ) -> None:
        owner = str(owner or "").strip()
        normalized_types = frozenset(
            str(value).strip() for value in root_types if str(value).strip()
        )
        if not owner or not normalized_types:
            raise ValueError("shared topic handler requires owner and root types")
        with self._lock:
            conflicts = {
                root_type: current_owner
                for root_type in normalized_types
                if (current_owner := self._owner_by_root_type.get(root_type))
                and current_owner != owner
            }
            if conflicts:
                root_type, current_owner = next(iter(conflicts.items()))
                raise ValueError(
                    f"topic type {root_type!r} is already handled by {current_owner!r}"
                )
            self.unregister(owner)
            handler = SharedTopicHandler(
                owner, normalized_types, list_topics, accept_invitation,
                assignment_scoped, mount_invitation,
            )
            self._handlers_by_owner[owner] = handler
            for root_type in normalized_types:
                self._owner_by_root_type[root_type] = owner

    def unregister(self, owner: str) -> None:
        with self._lock:
            handler = self._handlers_by_owner.pop(owner, None)
            if not handler:
                return
            for root_type in handler.root_types:
                if self._owner_by_root_type.get(root_type) == owner:
                    self._owner_by_root_type.pop(root_type, None)

    def handler_for(self, node: ProtocolNode | None) -> SharedTopicHandler | None:
        root_type = str((node.data if node else {}).get("type") or "")
        with self._lock:
            owner = self._owner_by_root_type.get(root_type)
            return self._handlers_by_owner.get(owner) if owner else None

    def supports(self, node: ProtocolNode | None) -> bool:
        return self.handler_for(node) is not None

    def local_topic_uuids(
        self,
        assigned_topic_uuids: Iterable[str] | None = None,
    ) -> list[str]:
        """Return publishable topics, applying assignment scope in one place.

        Application topics are normally scoped by a channel target assignment.
        Core topics such as the local public profile opt out and therefore ride
        every active relationship without channel-specific special cases.
        """
        with self._lock:
            handlers = list(self._handlers_by_owner.values())
        assigned = (
            {str(value) for value in assigned_topic_uuids if str(value)}
            if assigned_topic_uuids is not None else None
        )
        found = set()
        for handler in handlers:
            for item in handler.list_topics() or []:
                topic_uuid = item.uuid if isinstance(item, ProtocolNode) else str(item or "")
                if (topic_uuid and (
                    assigned is None
                    or not handler.assignment_scoped
                    or topic_uuid in assigned
                )):
                    found.add(topic_uuid)
        return sorted(found)

    def accept_invited_topic(self, tree: ProtocolNode):
        handler = self.handler_for(tree)
        return handler.accept_invitation(tree) if handler else None

    def invitation_requires_mount(self, tree: ProtocolNode | None) -> bool:
        """Whether a recognized topic is expected to exist in the local tree."""
        handler = self.handler_for(tree)
        return handler.mount_invitation if handler else True

    def has_assignment_scoped_handlers(self) -> bool:
        with self._lock:
            return any(
                handler.assignment_scoped
                for handler in self._handlers_by_owner.values()
            )
