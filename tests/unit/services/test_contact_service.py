"""Tests for ContactService — add / list / update / remove."""
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from plugins.meinchat.meinchat.services.contact_service import (
    ContactAlreadyExistsError,
    ContactNotFoundError,
    ContactSelfAddError,
    ContactService,
)
from plugins.meinchat.meinchat.services.nickname_service import (
    NicknameNotFoundError,
)


@pytest.fixture
def contact_repo():
    r = MagicMock()
    r.find_by_owner_and_contact.return_value = None
    r.save.side_effect = lambda row: row
    return r


@pytest.fixture
def nickname_repo():
    r = MagicMock()
    r.find_by_nickname_ci.return_value = None
    return r


@pytest.fixture
def service(contact_repo, nickname_repo):
    return ContactService(contact_repo=contact_repo, nickname_repo=nickname_repo)


def _nickname_row(user_id, nickname, *, banned=False, search_hidden=False):
    from plugins.meinchat.meinchat.models.user_nickname import UserNickname

    row = UserNickname()
    row.id = uuid4()
    row.user_id = user_id
    row.nickname = nickname
    row.nickname_ci = nickname.lower()
    row.banned = banned
    row.search_hidden = search_hidden
    return row


def _contact_row(owner_id, contact_id, **kwargs):
    from plugins.meinchat.meinchat.models.user_contact import UserContact

    row = UserContact()
    row.id = uuid4()
    row.owner_user_id = owner_id
    row.contact_user_id = contact_id
    row.alias = kwargs.get("alias")
    row.note = kwargs.get("note")
    row.pinned = kwargs.get("pinned", False)
    return row


class TestAddContact:
    def test_adds_contact_by_nickname(self, service, contact_repo, nickname_repo):
        owner_id = uuid4()
        contact_user_id = uuid4()
        nickname_repo.find_by_nickname_ci.return_value = _nickname_row(
            contact_user_id, "bob"
        )

        result = service.add_contact(owner_id, nickname="bob")

        assert result.owner_user_id == owner_id
        assert result.contact_user_id == contact_user_id
        contact_repo.save.assert_called()

    def test_stores_alias_note_pinned(self, service, contact_repo, nickname_repo):
        owner_id = uuid4()
        contact_user_id = uuid4()
        nickname_repo.find_by_nickname_ci.return_value = _nickname_row(
            contact_user_id, "bob"
        )

        result = service.add_contact(
            owner_id, nickname="bob", alias="Bobby", note="Mentor", pinned=True
        )

        assert result.alias == "Bobby"
        assert result.note == "Mentor"
        assert result.pinned is True

    def test_rejects_unknown_nickname(self, service, nickname_repo):
        nickname_repo.find_by_nickname_ci.return_value = None
        with pytest.raises(NicknameNotFoundError):
            service.add_contact(uuid4(), nickname="ghost")

    def test_rejects_banned_nickname(self, service, nickname_repo):
        nickname_repo.find_by_nickname_ci.return_value = _nickname_row(
            uuid4(), "spammer", banned=True
        )
        with pytest.raises(NicknameNotFoundError):
            service.add_contact(uuid4(), nickname="spammer")

    def test_rejects_self_add(self, service, nickname_repo):
        owner_id = uuid4()
        nickname_repo.find_by_nickname_ci.return_value = _nickname_row(
            owner_id, "alice"
        )
        with pytest.raises(ContactSelfAddError):
            service.add_contact(owner_id, nickname="alice")

    def test_rejects_duplicate(self, service, contact_repo, nickname_repo):
        owner_id = uuid4()
        contact_user_id = uuid4()
        nickname_repo.find_by_nickname_ci.return_value = _nickname_row(
            contact_user_id, "bob"
        )
        contact_repo.find_by_owner_and_contact.return_value = _contact_row(
            owner_id, contact_user_id
        )
        with pytest.raises(ContactAlreadyExistsError):
            service.add_contact(owner_id, nickname="bob")


class TestListContacts:
    def test_delegates_to_repo(self, service, contact_repo):
        owner_id = uuid4()
        contact_repo.list_for_owner.return_value = []
        service.list_contacts(owner_id)
        contact_repo.list_for_owner.assert_called_once_with(owner_id)


class TestUpdateContact:
    def test_updates_alias_note_pinned(self, service, contact_repo):
        owner_id = uuid4()
        existing = _contact_row(owner_id, uuid4(), alias="old", pinned=False)
        contact_repo.find_by_id.return_value = existing

        result = service.update_contact(
            owner_id, existing.id, alias="new", note="friend", pinned=True
        )
        assert result.alias == "new"
        assert result.note == "friend"
        assert result.pinned is True

    def test_cannot_touch_others_contact(self, service, contact_repo):
        existing = _contact_row(owner_id=uuid4(), contact_id=uuid4())
        contact_repo.find_by_id.return_value = existing

        attacker_id = uuid4()  # not the owner
        with pytest.raises(ContactNotFoundError):
            service.update_contact(attacker_id, existing.id, alias="hacked")

    def test_unknown_raises_not_found(self, service, contact_repo):
        contact_repo.find_by_id.return_value = None
        with pytest.raises(ContactNotFoundError):
            service.update_contact(uuid4(), uuid4(), alias="x")


class TestRemoveContact:
    def test_removes_own_contact(self, service, contact_repo):
        owner_id = uuid4()
        existing = _contact_row(owner_id, uuid4())
        contact_repo.find_by_id.return_value = existing

        service.remove_contact(owner_id, existing.id)
        contact_repo.delete.assert_called_once_with(existing)

    def test_cannot_remove_others(self, service, contact_repo):
        existing = _contact_row(owner_id=uuid4(), contact_id=uuid4())
        contact_repo.find_by_id.return_value = existing
        with pytest.raises(ContactNotFoundError):
            service.remove_contact(uuid4(), existing.id)
