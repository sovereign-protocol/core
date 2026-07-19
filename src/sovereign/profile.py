"""Core-owned minimal public-profile service."""

from __future__ import annotations

from .blob_store import SAFE_IMAGE_MIMES, avatar_attachment, canonical_attachments
from .protocol import ProtocolNode
from .session import Session, SessionResult


CORE_PROFILE_OWNER = "Sovereign Core profile"


class CoreProfileService:
    """Edit and present the one profile shared by every hosted application."""

    def __init__(self, session: Session):
        self.session = session

    @property
    def profile(self) -> ProtocolNode:
        return self.session.identity

    def set_profile(
        self,
        display_name: str,
        picture: str | None = None,
    ) -> SessionResult:
        return self.session.set_identity(display_name, picture)

    def set_avatar(self, reference: dict | None) -> SessionResult:
        profile = self.profile
        data = dict(profile.data)
        attachments = [
            item for item in canonical_attachments(data.get("attachments"))
            if item["role"] != "avatar"
        ]
        if reference is not None:
            normalized = canonical_attachments([reference])
            if not normalized or normalized[0]["role"] != "avatar":
                return SessionResult("error", reason="invalid avatar reference")
            if normalized[0]["mime"] not in SAFE_IMAGE_MIMES:
                return SessionResult("error", reason="unsupported avatar image type")
            attachments.append(normalized[0])
        # Replacing or removing an avatar also removes the legacy URL field.
        data["picture"] = ""
        data["attachments"] = canonical_attachments(attachments)
        return self.session.modify(profile.uuid, data, profile.weights)

    def view(self) -> dict:
        profile = self.profile
        avatar = avatar_attachment(profile.data)
        return {
            "profile": profile.to_dict(),
            "identity_key": profile.data["identity_key"],
            "display_name": profile.data.get("display_name", ""),
            "picture": (
                f"/api/blob/{avatar['blob_id']}" if avatar
                else profile.data.get("picture", "")
            ),
            "avatar": avatar,
        }
