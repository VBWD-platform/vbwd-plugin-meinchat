"""Data access for guest widget-session rows (S86.3)."""
from typing import Optional
from uuid import UUID

from plugins.meinchat.meinchat.models.guest_session import MeinchatGuestSession


class GuestSessionRepository:
    """Persist + look up the guest ↔ widget ↔ room link."""

    def __init__(self, session) -> None:
        self._session = session

    def save(self, row: MeinchatGuestSession) -> MeinchatGuestSession:
        self._session.add(row)
        self._session.flush()
        return row

    def find_by_id(self, session_id) -> Optional[MeinchatGuestSession]:
        return self._session.get(MeinchatGuestSession, session_id)

    def find_by_guest_user_id(
        self, guest_user_id: UUID
    ) -> Optional[MeinchatGuestSession]:
        return (
            self._session.query(MeinchatGuestSession)
            .filter(MeinchatGuestSession.guest_user_id == guest_user_id)
            .first()
        )

    def find_for_widget(
        self, guest_user_id: UUID, widget_slug: str
    ) -> Optional[MeinchatGuestSession]:
        """The guest's session for a specific widget (D12 return-visitor reuse)."""
        return (
            self._session.query(MeinchatGuestSession)
            .filter(
                MeinchatGuestSession.guest_user_id == guest_user_id,
                MeinchatGuestSession.widget_slug == widget_slug,
            )
            .first()
        )
