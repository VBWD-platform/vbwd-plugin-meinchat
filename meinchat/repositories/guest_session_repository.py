"""Data access for guest widget-session rows (S86.3)."""
from typing import Dict, List, Optional
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

    def all_guest_user_ids(self) -> List[UUID]:
        """Every DISTINCT guest user id (a guest may back several widget rows).

        The core token balance is GLOBAL per ``guest_user_id``, so the admin
        bulk top-up / reset operates on distinct guests, not per-session rows.
        """
        rows = self._session.query(MeinchatGuestSession.guest_user_id).distinct().all()
        return [row[0] for row in rows]

    def list_distinct_guests(
        self,
        *,
        page: int = 1,
        per_page: int = 50,
        query: Optional[str] = None,
    ) -> Dict[str, object]:
        """Paged listing of DISTINCT guests, each represented by its MOST-RECENT
        session row (so the admin table shows the latest widget + display name).

        Optional ``query`` substring-filters on ``display_name``. Dedup is done
        in-Python over the ordered rows because a guest may have multiple session
        rows and the representative is the newest one.
        """
        base = self._session.query(MeinchatGuestSession)
        if query:
            base = base.filter(MeinchatGuestSession.display_name.ilike(f"%{query}%"))
        ordered = base.order_by(
            MeinchatGuestSession.guest_user_id,
            MeinchatGuestSession.updated_at.desc(),
            MeinchatGuestSession.created_at.desc(),
        ).all()

        representatives: Dict[UUID, MeinchatGuestSession] = {}
        for row in ordered:
            if row.guest_user_id not in representatives:
                representatives[row.guest_user_id] = row

        distinct_rows = list(representatives.values())
        total = len(distinct_rows)
        start = max(0, (page - 1)) * per_page
        page_rows = distinct_rows[start : start + per_page]
        return {"items": page_rows, "total": total}
