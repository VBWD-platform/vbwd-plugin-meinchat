"""S66 integration — the full push path against a fake APNs collector.

A message posted through the REAL ``MessageService.send_text`` flows through
``_run_post_send_hooks`` → the core ``push_dispatcher_registry`` → the
registered ``MeinChatPushHook`` → ``ApnsClient``, whose httpx transport is an
in-process MockTransport acting as the collector. The collector must receive
exactly one POST whose JSON matches the S66 §4.3 payload shape byte-for-byte
(+ the S67.1 root ``peer_nickname``).
"""
from __future__ import annotations

import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _write_signing_key(tmp_path) -> str:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_file = tmp_path / "apns_key.p8"
    key_file.write_bytes(pem_bytes)
    return str(key_file)


def _user_with_nickname(email: str, nickname: str):
    """Seed a user + nickname through the real services (no raw SQL)."""
    from flask import current_app

    from vbwd.extensions import db
    from vbwd.repositories.user_repository import UserRepository

    from plugins.meinchat.meinchat.repositories.nickname_repository import (
        NicknameRepository,
    )
    from plugins.meinchat.meinchat.services.nickname_service import NicknameService

    user_repo = UserRepository(db.session)
    existing = user_repo.find_by_email(email)
    if existing is None:
        current_app.container.auth_service().register(
            email=email, password="PushTest123@"
        )
        existing = user_repo.find_by_email(email)
    nickname_service = NicknameService(repo=NicknameRepository(db.session))
    if nickname_service.get_mine(existing.id) is None:
        nickname_service.set_nickname(existing.id, nickname)
    db.session.commit()
    return existing


def _collector_transport(captured_requests, status_code=200, body=None):
    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(status_code, json=body if body is not None else {})

    return httpx.MockTransport(handler)


def _build_hook(apns_client):
    from vbwd.extensions import db
    from vbwd.repositories.device_token_repository import DeviceTokenRepository
    from vbwd.services.device_token_service import DeviceTokenService

    from plugins.meinchat.meinchat.repositories.conversation_repository import (
        ConversationRepository,
    )
    from plugins.meinchat.meinchat.services.push_notification_hook import (
        MeinChatPushHook,
    )

    return MeinChatPushHook(
        apns_client=apns_client,
        device_token_service_provider=lambda: DeviceTokenService(
            repository=DeviceTokenRepository(db.session)
        ),
        conversation_repository_provider=lambda: ConversationRepository(db.session),
        app_name="meinchat",
    )


def _real_send_path(collector_status, collector_body, tmp_path, seed_suffix):
    """Drive the REAL send path once; return the captured collector traffic
    plus the rows the assertions need."""
    from vbwd.extensions import db
    from vbwd.repositories.device_token_repository import DeviceTokenRepository
    from vbwd.services import push_dispatcher_registry
    from vbwd.services.apns_client import ApnsClient
    from vbwd.services.device_token_service import DeviceTokenService

    from plugins.meinchat.meinchat.repositories.conversation_repository import (
        ConversationRepository,
    )
    from plugins.meinchat.meinchat.repositories.message_repository import (
        MessageRepository,
    )
    from plugins.meinchat.meinchat.repositories.nickname_repository import (
        NicknameRepository,
    )
    from plugins.meinchat.meinchat.services.conversation_service import (
        ConversationService,
    )
    from plugins.meinchat.meinchat.services.message_service import MessageService

    sender = _user_with_nickname(
        f"push-sender-{seed_suffix}@example.com", f"push-sender-{seed_suffix}"
    )
    recipient = _user_with_nickname(
        f"push-recipient-{seed_suffix}@example.com",
        f"push-recipient-{seed_suffix}",
    )

    conversation_service = ConversationService(ConversationRepository(db.session))
    conversation = conversation_service.start_or_get(sender.id, recipient.id)
    db.session.commit()

    device_token_service = DeviceTokenService(
        repository=DeviceTokenRepository(db.session)
    )
    device_token_value = f"e2e-collector-token-{seed_suffix}"
    device_token_service.register(
        user_id=recipient.id,
        token=device_token_value,
        platform="ios",
        bundle_id="com.vbwd.app",
        app="meinchat",
    )

    captured_requests: list = []
    apns_client = ApnsClient(
        key_path=_write_signing_key(tmp_path),
        key_id="TESTKEY123",
        team_id="TESTTEAM12",
        bundle_id="com.vbwd.app",
        use_sandbox=True,
        transport=_collector_transport(
            captured_requests, status_code=collector_status, body=collector_body
        ),
    )

    # Replace the boot-registered (env-disabled, no-op) meinchat hook with one
    # whose ApnsClient points at the collector — same key, same dispatch path.
    push_dispatcher_registry.register_push_handler("meinchat", _build_hook(apns_client))
    try:
        message_service = MessageService(
            ConversationRepository(db.session),
            MessageRepository(db.session),
            NicknameRepository(db.session),
        )
        message = message_service.send_text(
            conversation.id,
            sender_user_id=sender.id,
            body=f"hello push {seed_suffix}",
        )
        db.session.commit()
    finally:
        push_dispatcher_registry.unregister_push_handler("meinchat")

    return (
        captured_requests,
        message,
        conversation,
        recipient,
        device_token_value,
        device_token_service,
        f"push-sender-{seed_suffix}",
    )


@pytest.mark.integration
def test_send_text_delivers_exactly_one_push_matching_payload_shape(app, tmp_path):
    with app.app_context():
        (
            captured_requests,
            message,
            conversation,
            _recipient,
            device_token_value,
            _device_token_service,
            sender_nickname,
        ) = _real_send_path(
            collector_status=200,
            collector_body={},
            tmp_path=tmp_path,
            seed_suffix="ok",
        )

        assert len(captured_requests) == 1
        request = captured_requests[0]
        assert request.url.host == "api.sandbox.push.apple.com"
        assert request.url.path == f"/3/device/{device_token_value}"
        assert request.headers["apns-topic"] == "com.vbwd.app"
        assert request.headers["apns-push-type"] == "alert"
        assert request.headers["authorization"].startswith("bearer ")

        # §4.3 payload shape, byte-for-byte (+ S67.1 root peer_nickname).
        assert json.loads(request.content) == {
            "aps": {
                "alert": {
                    "title": f"@{sender_nickname}",
                    "body": "hello push ok",
                },
                "badge": 1,
                "sound": "default",
                "thread-id": str(conversation.id),
            },
            "conversation_id": str(conversation.id),
            "message_id": str(message.id),
            "peer_nickname": sender_nickname,
            "protocol": "plain",
        }


@pytest.mark.integration
def test_410_from_apns_revokes_the_token_and_next_dispatch_skips_it(app, tmp_path):
    with app.app_context():
        (
            captured_requests,
            _message,
            _conversation,
            recipient,
            device_token_value,
            device_token_service,
            _sender_nickname,
        ) = _real_send_path(
            collector_status=410,
            collector_body={"reason": "Unregistered"},
            tmp_path=tmp_path,
            seed_suffix="stale",
        )

        # The push was attempted once; the hook then revoked the stale token.
        assert len(captured_requests) == 1
        revoked_row = device_token_service.find_by_token(device_token_value)
        assert revoked_row is not None
        assert revoked_row.revoked_at is not None
        # …and the active lookup the next dispatch uses excludes it.
        assert device_token_service.find_active(recipient.id, "meinchat") == []
