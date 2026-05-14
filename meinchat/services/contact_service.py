"""Business logic for the personal address book."""
from typing import List, Optional
from uuid import UUID

from plugins.meinchat.meinchat.models.user_contact import UserContact
from plugins.meinchat.meinchat.repositories.contact_repository import (
    ContactRepository,
)
from plugins.meinchat.meinchat.repositories.nickname_repository import (
    NicknameRepository,
)
from plugins.meinchat.meinchat.services.nickname_service import (
    NicknameNotFoundError,
)


class ContactAlreadyExistsError(Exception):
    """Owner already has this contact saved."""


class ContactNotFoundError(Exception):
    """Contact missing, or belongs to a different owner (authz leaks look
    identical to not-found so attackers can't probe)."""


class ContactSelfAddError(Exception):
    """Cannot add yourself as your own contact."""


class ContactService:
    """Per-user private address book (decision Q6)."""

    def __init__(
        self,
        contact_repo: ContactRepository,
        nickname_repo: NicknameRepository,
    ) -> None:
        self._repo = contact_repo
        self._nickname_repo = nickname_repo

    def add_contact(
        self,
        owner_user_id: UUID,
        *,
        nickname: str,
        alias: Optional[str] = None,
        note: Optional[str] = None,
        pinned: bool = False,
    ) -> UserContact:
        target = self._nickname_repo.find_by_nickname_ci(nickname)
        if target is None or target.banned or target.search_hidden:
            raise NicknameNotFoundError(f"'{nickname}' not found")
        if target.user_id == owner_user_id:
            raise ContactSelfAddError("cannot add yourself")

        if self._repo.find_by_owner_and_contact(owner_user_id, target.user_id):
            raise ContactAlreadyExistsError(f"'{nickname}' is already in your contacts")

        row = UserContact()
        row.owner_user_id = owner_user_id
        row.contact_user_id = target.user_id
        row.alias = alias
        row.note = note
        row.pinned = pinned
        return self._repo.save(row)

    def list_contacts(self, owner_user_id: UUID) -> List[UserContact]:
        return self._repo.list_for_owner(owner_user_id)

    def update_contact(
        self,
        owner_user_id: UUID,
        contact_id: UUID,
        *,
        alias: Optional[str] = None,
        note: Optional[str] = None,
        pinned: Optional[bool] = None,
    ) -> UserContact:
        row = self._load_owned(owner_user_id, contact_id)
        if alias is not None:
            row.alias = alias
        if note is not None:
            row.note = note
        if pinned is not None:
            row.pinned = pinned
        return self._repo.save(row)

    def remove_contact(self, owner_user_id: UUID, contact_id: UUID) -> None:
        row = self._load_owned(owner_user_id, contact_id)
        self._repo.delete(row)

    def _load_owned(self, owner_user_id: UUID, contact_id: UUID) -> UserContact:
        row = self._repo.find_by_id(contact_id)
        if row is None or row.owner_user_id != owner_user_id:
            raise ContactNotFoundError(f"contact {contact_id} not found")
        return row
