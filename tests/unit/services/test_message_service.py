"""Tests for MessageService — send text, paginate, mark read, hard delete."""
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from plugins.meinchat.meinchat.services.message_service import (
    MessageBodyTooLongError,
    MessageNotFoundError,
    MessageService,
    NotAConversationMemberError,
)


def _conversation(user_a, user_b):
    from plugins.meinchat.meinchat.models.conversation import Conversation
    from plugins.meinchat.meinchat.services.conversation_service import order_pair

    low, high = order_pair(user_a, user_b)
    conv = Conversation()
    conv.id = uuid4()
    conv.participant_low_id = low
    conv.participant_high_id = high
    conv.unread_low_count = 0
    conv.unread_high_count = 0
    return conv


def _nickname_row(user_id, nickname):
    from plugins.meinchat.meinchat.models.user_nickname import UserNickname

    row = UserNickname()
    row.user_id = user_id
    row.nickname = nickname
    return row


@pytest.fixture
def conv_repo():
    r = MagicMock()
    r.save.side_effect = lambda row: row
    return r


@pytest.fixture
def message_repo():
    r = MagicMock()
    r.save.side_effect = lambda row: row
    return r


@pytest.fixture
def nickname_repo():
    return MagicMock()


@pytest.fixture
def service(conv_repo, message_repo, nickname_repo):
    return MessageService(
        conv_repo=conv_repo,
        message_repo=message_repo,
        nickname_repo=nickname_repo,
    )


class TestSendText:
    def test_sends_text_to_conversation(
        self, service, conv_repo, message_repo, nickname_repo
    ):
        alice, bob = uuid4(), uuid4()
        conv = _conversation(alice, bob)
        conv_repo.find_by_id.return_value = conv
        nickname_repo.find_by_user_id.return_value = _nickname_row(alice, "alice")

        msg = service.send_text(conv.id, sender_user_id=alice, body="hi")

        assert msg.conversation_id == conv.id
        assert msg.sender_id == alice
        assert msg.sender_nickname == "alice"
        assert msg.body == "hi"
        message_repo.save.assert_called_once()

    def test_bumps_peer_unread_count(
        self, service, conv_repo, message_repo, nickname_repo
    ):
        alice, bob = uuid4(), uuid4()
        conv = _conversation(alice, bob)
        conv_repo.find_by_id.return_value = conv
        nickname_repo.find_by_user_id.return_value = _nickname_row(alice, "alice")

        service.send_text(conv.id, sender_user_id=alice, body="hi")

        total_unread = conv.unread_low_count + conv.unread_high_count
        assert total_unread == 1  # bob gets +1, alice stays at 0

    def test_rejects_non_member(self, service, conv_repo, message_repo):
        alice, bob = uuid4(), uuid4()
        outsider = uuid4()
        conv = _conversation(alice, bob)
        conv_repo.find_by_id.return_value = conv

        with pytest.raises(NotAConversationMemberError):
            service.send_text(conv.id, sender_user_id=outsider, body="sneaky")
        message_repo.save.assert_not_called()

    def test_rejects_empty_body(self, service, conv_repo, nickname_repo):
        alice, bob = uuid4(), uuid4()
        conv = _conversation(alice, bob)
        conv_repo.find_by_id.return_value = conv
        nickname_repo.find_by_user_id.return_value = _nickname_row(alice, "alice")

        with pytest.raises(ValueError):
            service.send_text(conv.id, sender_user_id=alice, body="   ")

    def test_rejects_body_over_4000_chars(self, service, conv_repo, nickname_repo):
        alice, bob = uuid4(), uuid4()
        conv = _conversation(alice, bob)
        conv_repo.find_by_id.return_value = conv
        nickname_repo.find_by_user_id.return_value = _nickname_row(alice, "alice")

        with pytest.raises(MessageBodyTooLongError):
            service.send_text(conv.id, sender_user_id=alice, body="x" * 4001)

    def test_unknown_conversation_raises(self, service, conv_repo):
        conv_repo.find_by_id.return_value = None
        with pytest.raises(Exception):
            service.send_text(uuid4(), sender_user_id=uuid4(), body="hi")


class TestMarkRead:
    def test_clears_callers_unread_slot(self, service, conv_repo):
        alice, bob = uuid4(), uuid4()
        conv = _conversation(alice, bob)
        conv_repo.find_by_id.return_value = conv
        if alice == conv.participant_low_id:
            conv.unread_low_count = 4
        else:
            conv.unread_high_count = 4

        service.mark_read(conv.id, reader_user_id=alice)

        if alice == conv.participant_low_id:
            assert conv.unread_low_count == 0
        else:
            assert conv.unread_high_count == 0

    def test_non_member_cannot_mark_read(self, service, conv_repo):
        alice, bob = uuid4(), uuid4()
        conv = _conversation(alice, bob)
        conv_repo.find_by_id.return_value = conv

        with pytest.raises(NotAConversationMemberError):
            service.mark_read(conv.id, reader_user_id=uuid4())


class TestDeleteMessage:
    def test_hard_deletes_and_returns_info_for_sse(
        self, service, conv_repo, message_repo
    ):
        """Q4: delete removes the row on both sides. Service returns the
        info the route needs to fan out the `message_deleted` SSE event."""
        alice, bob = uuid4(), uuid4()
        conv = _conversation(alice, bob)
        from plugins.meinchat.meinchat.models.message import Message

        msg = Message()
        msg.id = uuid4()
        msg.conversation_id = conv.id
        msg.sender_id = alice
        msg.body = "oops"
        message_repo.find_by_id.return_value = msg
        conv_repo.find_by_id.return_value = conv

        info = service.delete_message(msg.id, caller_user_id=alice)

        message_repo.delete.assert_called_once_with(msg)
        assert info["conversation_id"] == conv.id
        assert info["recipient_id"] in (alice, bob)

    def test_only_sender_can_delete(self, service, conv_repo, message_repo):
        alice, bob = uuid4(), uuid4()
        conv = _conversation(alice, bob)
        from plugins.meinchat.meinchat.models.message import Message

        msg = Message()
        msg.id = uuid4()
        msg.conversation_id = conv.id
        msg.sender_id = alice

        message_repo.find_by_id.return_value = msg
        conv_repo.find_by_id.return_value = conv

        with pytest.raises(MessageNotFoundError):
            service.delete_message(msg.id, caller_user_id=bob)
        message_repo.delete.assert_not_called()


class TestAttachmentPurgeOnDelete:
    def test_delete_calls_attachment_service_before_row_delete(
        self, service, conv_repo, message_repo
    ):
        alice, bob = uuid4(), uuid4()
        conv = _conversation(alice, bob)
        from plugins.meinchat.meinchat.models.message import Message

        msg = Message()
        msg.id = uuid4()
        msg.conversation_id = conv.id
        msg.sender_id = alice
        msg.body = ""
        msg.attachment_url = "/uploads/meinchat/attachments/alice/abc.webp"
        msg.attachment_thumb_url = "/uploads/meinchat/attachments/alice/abc.thumb.webp"
        message_repo.find_by_id.return_value = msg
        conv_repo.find_by_id.return_value = conv

        attachments = MagicMock()
        service._attachments = attachments

        service.delete_message(msg.id, caller_user_id=alice)

        attachments.delete_attachment.assert_called_once_with(
            original_path="meinchat/attachments/alice/abc.webp",
            thumb_path="meinchat/attachments/alice/abc.thumb.webp",
        )
        message_repo.delete.assert_called_once_with(msg)

    def test_delete_of_text_only_message_skips_attachment_call(
        self, service, conv_repo, message_repo
    ):
        alice, bob = uuid4(), uuid4()
        conv = _conversation(alice, bob)
        from plugins.meinchat.meinchat.models.message import Message

        msg = Message()
        msg.id = uuid4()
        msg.conversation_id = conv.id
        msg.sender_id = alice
        msg.body = "hi"
        msg.attachment_url = None
        msg.attachment_thumb_url = None
        message_repo.find_by_id.return_value = msg
        conv_repo.find_by_id.return_value = conv

        attachments = MagicMock()
        service._attachments = attachments

        service.delete_message(msg.id, caller_user_id=alice)

        attachments.delete_attachment.assert_not_called()


class TestPaginateMessages:
    def test_delegates_to_repo(self, service, conv_repo, message_repo):
        alice, bob = uuid4(), uuid4()
        conv = _conversation(alice, bob)
        conv_repo.find_by_id.return_value = conv
        message_repo.page.return_value = []

        service.list_messages(conv.id, caller_user_id=alice, before=None, limit=50)

        message_repo.page.assert_called_once_with(conv.id, before=None, limit=50)
