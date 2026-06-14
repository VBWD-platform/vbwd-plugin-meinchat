"""S66 — MeinChatPushHook unit specs (gnostic payload translation).

The hook owns the meaning: Message → APNs payload (§4.3 + S67.1 §3 root
keys), recipient token resolution for its app, badge = Σ unread from the
inbox query, self-push suppression, and 410 → revoke (the hook, not the
ApnsClient, revokes stale tokens).
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from plugins.meinchat.meinchat.services.push_notification_hook import (
    ENCRYPTED_BODY_PLACEHOLDER,
    MeinChatPushHook,
)
from vbwd.services.push_dispatcher_registry import PushEvent

SENDER_ID = uuid4()
RECIPIENT_ID = uuid4()
CONVERSATION_ID = uuid4()
MESSAGE_ID = uuid4()


def _conversation(unread_for_recipient: int = 1):
    """Recipient is the low participant so unread_low_count is theirs."""
    low_id, high_id = sorted([RECIPIENT_ID, SENDER_ID])
    unread_low = unread_for_recipient if low_id == RECIPIENT_ID else 0
    unread_high = unread_for_recipient if high_id == RECIPIENT_ID else 0
    return SimpleNamespace(
        id=CONVERSATION_ID,
        participant_low_id=low_id,
        participant_high_id=high_id,
        unread_low_count=unread_low,
        unread_high_count=unread_high,
    )


def _message(protocol: str = "plain", body: str = "hello there"):
    return SimpleNamespace(
        id=MESSAGE_ID,
        sender_id=SENDER_ID,
        sender_nickname="alice",
        protocol=protocol,
        body=body,
    )


def _event(message, conversation, recipient_user_id=RECIPIENT_ID):
    return PushEvent(
        message=message,
        conversation=conversation,
        recipient_user_id=recipient_user_id,
        app="meinchat",
    )


def _badge_only_event(
    conversation, recipient_user_id=RECIPIENT_ID, originating_device_token=None
):
    return PushEvent(
        message=None,
        conversation=conversation,
        recipient_user_id=recipient_user_id,
        app="meinchat",
        kind="badge_only",
        originating_device_token=originating_device_token,
    )


def _token_row(token_value: str):
    row = MagicMock()
    row.token = token_value
    return row


@pytest.fixture
def collaborators():
    apns_client = MagicMock()
    apns_client.send_bulk.return_value = []
    device_token_service = MagicMock()
    device_token_service.find_active.return_value = [_token_row("device-token-1")]
    conversation_repository = MagicMock()
    conversation_repository.list_for_user.return_value = []
    return apns_client, device_token_service, conversation_repository


def _hook(apns_client, device_token_service, conversation_repository):
    return MeinChatPushHook(
        apns_client=apns_client,
        device_token_service_provider=lambda: device_token_service,
        conversation_repository_provider=lambda: conversation_repository,
        app_name="meinchat",
    )


def test_plain_message_dispatches_alert_with_preview_and_badge(collaborators):
    apns_client, device_token_service, conversation_repository = collaborators
    conversation = _conversation(unread_for_recipient=3)
    other_conversation = SimpleNamespace(
        id=uuid4(),
        participant_low_id=RECIPIENT_ID,
        participant_high_id=uuid4(),
        unread_low_count=2,
        unread_high_count=0,
    )
    conversation_repository.list_for_user.return_value = [
        conversation,
        other_conversation,
    ]
    hook = _hook(apns_client, device_token_service, conversation_repository)

    hook(_event(_message(body="hello there"), conversation))

    apns_client.send_bulk.assert_called_once()
    tokens, payload = apns_client.send_bulk.call_args[0]
    assert tokens == ["device-token-1"]
    assert payload["aps"]["alert"]["title"] == "@alice"
    assert payload["aps"]["alert"]["body"] == "hello there"
    # Badge = Σ recipient's unread across the inbox (3 + 2).
    assert payload["aps"]["badge"] == 5
    assert payload["aps"]["sound"] == "default"
    assert payload["aps"]["thread-id"] == str(CONVERSATION_ID)
    assert payload["conversation_id"] == str(CONVERSATION_ID)
    assert payload["message_id"] == str(MESSAGE_ID)
    assert payload["peer_nickname"] == "alice"
    assert payload["protocol"] == "plain"


def test_plain_preview_is_capped_at_120_chars(collaborators):
    apns_client, device_token_service, conversation_repository = collaborators
    conversation = _conversation()
    conversation_repository.list_for_user.return_value = [conversation]
    hook = _hook(apns_client, device_token_service, conversation_repository)
    long_body = "x" * 500

    hook(_event(_message(body=long_body), conversation))

    _, payload = apns_client.send_bulk.call_args[0]
    assert payload["aps"]["alert"]["body"] == "x" * 120


def test_e2e_message_dispatches_generic_encrypted_placeholder(collaborators):
    apns_client, device_token_service, conversation_repository = collaborators
    conversation = _conversation()
    conversation_repository.list_for_user.return_value = [conversation]
    hook = _hook(apns_client, device_token_service, conversation_repository)
    message = _message(protocol="e2e_v1", body=None)
    message.envelope = b"opaque-ciphertext"

    hook(_event(message, conversation))

    _, payload = apns_client.send_bulk.call_args[0]
    assert payload["aps"]["alert"]["body"] == ENCRYPTED_BODY_PLACEHOLDER
    assert payload["protocol"] == "e2e_v1"
    # Never leak plaintext or envelope bytes in the push payload.
    serialized = json.dumps(payload)
    assert "opaque-ciphertext" not in serialized
    assert "envelope" not in serialized


def test_no_active_tokens_short_circuits_dispatch(collaborators):
    apns_client, device_token_service, conversation_repository = collaborators
    device_token_service.find_active.return_value = []
    hook = _hook(apns_client, device_token_service, conversation_repository)
    conversation = _conversation()

    hook(_event(_message(), conversation))

    apns_client.send_bulk.assert_not_called()
    conversation_repository.list_for_user.assert_not_called()


def test_self_push_is_suppressed(collaborators):
    apns_client, device_token_service, conversation_repository = collaborators
    hook = _hook(apns_client, device_token_service, conversation_repository)
    conversation = _conversation()

    hook(_event(_message(), conversation, recipient_user_id=SENDER_ID))

    device_token_service.find_active.assert_not_called()
    apns_client.send_bulk.assert_not_called()


def test_410_bad_token_triggers_unregister(collaborators):
    """The hook (not the transport client) revokes a stale token."""
    apns_client, device_token_service, conversation_repository = collaborators
    conversation = _conversation()
    conversation_repository.list_for_user.return_value = [conversation]
    stale_result = SimpleNamespace(
        token="device-token-1", success=False, status=410, reason="Unregistered"
    )
    ok_result = SimpleNamespace(
        token="device-token-2", success=True, status=200, reason=None
    )
    device_token_service.find_active.return_value = [
        _token_row("device-token-1"),
        _token_row("device-token-2"),
    ]
    apns_client.send_bulk.return_value = [stale_result, ok_result]
    hook = _hook(apns_client, device_token_service, conversation_repository)

    hook(_event(_message(), conversation))

    device_token_service.unregister.assert_called_once_with("device-token-1")


def test_badge_only_event_builds_silent_payload_with_zero_alert_keys(collaborators):
    """S68 — the silent payload carries only badge + content-available; no
    alert, sound, thread-id, or any message metadata."""
    apns_client, device_token_service, conversation_repository = collaborators
    conversation = _conversation(unread_for_recipient=2)
    conversation_repository.list_for_user.return_value = [conversation]
    hook = _hook(apns_client, device_token_service, conversation_repository)

    hook(_badge_only_event(conversation))

    _, payload = apns_client.send_bulk.call_args[0]
    assert payload == {"aps": {"badge": 2, "content-available": 1}}
    serialized = json.dumps(payload)
    assert "alert" not in serialized
    assert "sound" not in serialized
    assert "thread-id" not in serialized


def test_badge_only_event_uses_background_push_type_priority_5(collaborators):
    apns_client, device_token_service, conversation_repository = collaborators
    conversation = _conversation()
    conversation_repository.list_for_user.return_value = [conversation]
    hook = _hook(apns_client, device_token_service, conversation_repository)

    hook(_badge_only_event(conversation))

    _tokens, _payload = apns_client.send_bulk.call_args[0]
    assert apns_client.send_bulk.call_args.kwargs["push_type"] == "background"
    assert apns_client.send_bulk.call_args.kwargs["priority"] == 5


def test_badge_only_event_recomputes_badge_from_current_unread(collaborators):
    """Badge = Σ the reader's own unread across the inbox after the read."""
    apns_client, device_token_service, conversation_repository = collaborators
    conversation = _conversation(unread_for_recipient=0)
    other_conversation = SimpleNamespace(
        id=uuid4(),
        participant_low_id=RECIPIENT_ID,
        participant_high_id=uuid4(),
        unread_low_count=4,
        unread_high_count=0,
    )
    conversation_repository.list_for_user.return_value = [
        conversation,
        other_conversation,
    ]
    hook = _hook(apns_client, device_token_service, conversation_repository)

    hook(_badge_only_event(conversation))

    _, payload = apns_client.send_bulk.call_args[0]
    assert payload["aps"]["badge"] == 4


def test_badge_only_event_filters_originating_device_token(collaborators):
    """Same-device suppression (option a): the originating token is removed
    from the recipient's token list before send."""
    apns_client, device_token_service, conversation_repository = collaborators
    conversation = _conversation()
    conversation_repository.list_for_user.return_value = [conversation]
    device_token_service.find_active.return_value = [
        _token_row("device-a"),
        _token_row("device-b"),
    ]
    hook = _hook(apns_client, device_token_service, conversation_repository)

    hook(_badge_only_event(conversation, originating_device_token="device-a"))

    tokens, _payload = apns_client.send_bulk.call_args[0]
    assert tokens == ["device-b"]


def test_badge_only_event_with_only_originating_token_skips_send(collaborators):
    """If the only active token is the originator, there is nothing to push."""
    apns_client, device_token_service, conversation_repository = collaborators
    conversation = _conversation()
    device_token_service.find_active.return_value = [_token_row("device-a")]
    hook = _hook(apns_client, device_token_service, conversation_repository)

    hook(_badge_only_event(conversation, originating_device_token="device-a"))

    apns_client.send_bulk.assert_not_called()


def test_badge_only_410_bad_token_triggers_unregister(collaborators):
    """Silent pushes use the same 410-revoke path as alert pushes."""
    apns_client, device_token_service, conversation_repository = collaborators
    conversation = _conversation()
    conversation_repository.list_for_user.return_value = [conversation]
    device_token_service.find_active.return_value = [_token_row("device-b")]
    apns_client.send_bulk.return_value = [
        SimpleNamespace(
            token="device-b", success=False, status=410, reason="Unregistered"
        )
    ]
    hook = _hook(apns_client, device_token_service, conversation_repository)

    hook(_badge_only_event(conversation))

    device_token_service.unregister.assert_called_once_with("device-b")


def test_hook_resolves_tokens_for_its_own_app(collaborators):
    """Per-app targeting (S67.1 §5): the hook queries tokens for app_name."""
    apns_client, device_token_service, conversation_repository = collaborators
    hook = MeinChatPushHook(
        apns_client=apns_client,
        device_token_service_provider=lambda: device_token_service,
        conversation_repository_provider=lambda: conversation_repository,
        app_name="meinchat-plus",
    )
    conversation = _conversation()
    conversation_repository.list_for_user.return_value = [conversation]

    hook(_event(_message(), conversation))

    device_token_service.find_active.assert_called_once_with(
        RECIPIENT_ID, "meinchat-plus"
    )
