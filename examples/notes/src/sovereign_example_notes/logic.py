"""Domain logic: a shared list of notes.

Deliberately free of Starlette, the host, and anything named `runtime`. Core
enforces that separation on its own modules and on this example, because an
application that reaches for the host is the failure mode the architecture
exists to prevent.
"""

from __future__ import annotations

from sovereign import ApplicationRegistration, ProtocolNode, Session, SessionResult


NOTE_LIST = "example_note_list"
NOTE = "example_note"
APPLICATION_ID = "example-notes"


class NotesLogic:
    def __init__(self, session: Session, config: dict):
        self.session = session
        self.config = config

    def application_registration(self) -> ApplicationRegistration:
        """Claim the root type, which is what makes a note list shareable."""
        return ApplicationRegistration(
            APPLICATION_ID,
            frozenset({NOTE_LIST}),
            self.note_lists,
            self.accept_invitation,
            assignment_scoped=True,
            mount_invitation=True,
        )

    def note_lists(self) -> list[ProtocolNode]:
        return [
            node for node in self.session.protocol.root.children
            if node.data.get("type") == NOTE_LIST and not node.deleted
        ]

    def ensure_list(self) -> ProtocolNode:
        existing = self.note_lists()
        if existing:
            return existing[0]
        return self.session.create_child(
            self.session.protocol.root.uuid,
            {"type": NOTE_LIST, "name": "Notes"},
        ).value

    def accept_invitation(self, tree: ProtocolNode) -> SessionResult:
        return self.session.accept_topic_invitation(
            tree, self.session.protocol.root.uuid,
        )

    def create_note(self, text: str) -> SessionResult:
        return self.session.create_child(
            self.ensure_list().uuid, {"type": NOTE, "text": str(text)},
        )

    def notes(self, note_list: ProtocolNode) -> list[ProtocolNode]:
        return [
            child for child in note_list.children
            if child.data.get("type") == NOTE and not child.deleted
        ]

    def state(self) -> dict:
        note_list = self.ensure_list()
        return {
            "address": self.session.address,
            "list_uuid": note_list.uuid,
            "notes": [
                {"uuid": note.uuid, "text": note.data.get("text", "")}
                for note in self.notes(note_list)
            ],
            "network": self.session.get_network_info(),
        }
