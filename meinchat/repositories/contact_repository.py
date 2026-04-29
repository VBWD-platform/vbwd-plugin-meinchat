"""Data access for user_contact rows."""
from typing import List, Optional
from uuid import UUID

from plugins.meinchat.meinchat.models.user_contact import UserContact


class ContactRepository:
    """SQL for the personal address book."""

    def __init__(self, session) -> None:
        self._session = session

    def find_by_id(self, contact_id) -> Optional[UserContact]:
        return self._session.query(UserContact).get(contact_id)

    def find_by_owner_and_contact(
        self, owner_user_id, contact_user_id
    ) -> Optional[UserContact]:
        return (
            self._session.query(UserContact)
            .filter(UserContact.owner_user_id == owner_user_id)
            .filter(UserContact.contact_user_id == contact_user_id)
            .one_or_none()
        )

    def list_for_owner(self, owner_user_id: UUID) -> List[UserContact]:
        """Pinned first, then by lowercased alias (nickname tie-break is in
        the service, since alias + nickname JOIN would cross plugin tables)."""
        return (
            self._session.query(UserContact)
            .filter(UserContact.owner_user_id == owner_user_id)
            .order_by(
                UserContact.pinned.desc(),
                UserContact.created_at.asc(),
            )
            .all()
        )

    def save(self, row: UserContact) -> UserContact:
        self._session.add(row)
        self._session.flush()
        return row

    def delete(self, row: UserContact) -> None:
        self._session.delete(row)
        self._session.flush()
