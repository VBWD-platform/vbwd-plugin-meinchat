"""Tests for ConversationService — pair uniqueness + start_or_get."""
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from plugins.meinchat.meinchat.services.conversation_service import (
    ConversationService,
    SelfConversationError,
    order_pair,
)


def _conversation_row(low_id, high_id):
    from plugins.meinchat.meinchat.models.conversation import Conversation

    row = Conversation()
    row.id = uuid4()
    row.participant_low_id = low_id
    row.participant_high_id = high_id
    row.unread_low_count = 0
    row.unread_high_count = 0
    return row


@pytest.fixture
def repo():
    r = MagicMock()
    r.find_by_pair.return_value = None
    r.save.side_effect = lambda row: row
    return r


@pytest.fixture
def service(repo):
    return ConversationService(repo=repo)


class TestOrderPair:
    def test_sorts_two_uuids_deterministically(self):
        a = UUID("00000000-0000-0000-0000-000000000001")
        b = UUID("00000000-0000-0000-0000-000000000002")
        assert order_pair(a, b) == (a, b)
        assert order_pair(b, a) == (a, b)

    def test_rejects_equal_ids(self):
        a = uuid4()
        with pytest.raises(SelfConversationError):
            order_pair(a, a)


class TestStartOrGet:
    def test_creates_conversation_on_first_call(self, service, repo):
        a, b = uuid4(), uuid4()
        repo.find_by_pair.return_value = None

        result = service.start_or_get(a, b)
        assert result.participant_low_id is not None
        assert result.participant_high_id is not None
        repo.save.assert_called_once()

    def test_returns_existing_conversation_on_second_call(self, service, repo):
        a, b = uuid4(), uuid4()
        low, high = order_pair(a, b)
        existing = _conversation_row(low, high)
        repo.find_by_pair.return_value = existing

        result = service.start_or_get(a, b)
        assert result is existing
        repo.save.assert_not_called()

    def test_order_does_not_matter(self, service, repo):
        a, b = uuid4(), uuid4()
        low, high = order_pair(a, b)
        existing = _conversation_row(low, high)
        repo.find_by_pair.return_value = existing

        r1 = service.start_or_get(a, b)
        r2 = service.start_or_get(b, a)
        assert r1 is r2

    def test_self_conversation_rejected(self, service):
        a = uuid4()
        with pytest.raises(SelfConversationError):
            service.start_or_get(a, a)


class TestUnreadAccounting:
    def test_peer_id_resolves_to_correct_unread_slot(self, service):
        """`unread_for(user_id, conversation)` returns the slot belonging to
        that user so the frontend doesn't need to know the low/high order."""
        a, b = uuid4(), uuid4()
        low, high = order_pair(a, b)
        conv = _conversation_row(low, high)
        conv.unread_low_count = 3
        conv.unread_high_count = 7

        if a == low:
            assert service.unread_for(a, conv) == 3
            assert service.unread_for(b, conv) == 7
        else:
            assert service.unread_for(a, conv) == 7
            assert service.unread_for(b, conv) == 3

    def test_increment_unread_for_peer(self, service):
        """When alice sends a message to bob, bob's unread counter goes up."""
        alice, bob = uuid4(), uuid4()
        low, high = order_pair(alice, bob)
        conv = _conversation_row(low, high)

        service.increment_unread_for(bob, conv)
        if bob == low:
            assert conv.unread_low_count == 1
            assert conv.unread_high_count == 0
        else:
            assert conv.unread_high_count == 1
            assert conv.unread_low_count == 0

    def test_clear_unread_for_self(self, service):
        alice, bob = uuid4(), uuid4()
        low, high = order_pair(alice, bob)
        conv = _conversation_row(low, high)
        conv.unread_low_count = 5
        conv.unread_high_count = 7

        service.clear_unread_for(alice, conv)
        if alice == low:
            assert conv.unread_low_count == 0
            assert conv.unread_high_count == 7
        else:
            assert conv.unread_high_count == 0
            assert conv.unread_low_count == 5
