"""S68 integration — the silent badge-update push path on ``markRead``.

Extends the S66 end-to-end flow: register TWO device tokens for the same
user, have a peer send a message (alert push to both), then call
``MessageService.mark_read`` with token-A as the originating device. The
fake APNs collector must receive EXACTLY ONE background push — to token-B
only — carrying ``aps.badge = 0`` and ``content-available`` with no
``alert`` / ``sound`` keys (token-A is suppressed; it read in-process).
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


@pytest.mark.integration
def test_mark_read_pushes_silent_badge_to_other_device_only(app, tmp_path):
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

    with app.app_context():
        peer = _user_with_nickname("badge-peer@example.com", "badge-peer")
        reader = _user_with_nickname("badge-reader@example.com", "badge-reader")

        conversation_service = ConversationService(ConversationRepository(db.session))
        conversation = conversation_service.start_or_get(peer.id, reader.id)
        db.session.commit()

        # The reader carries TWO iOS devices (A = the device that reads).
        device_token_service = DeviceTokenService(
            repository=DeviceTokenRepository(db.session)
        )
        token_a = "badge-device-a"
        token_b = "badge-device-b"
        for token_value in (token_a, token_b):
            device_token_service.register(
                user_id=reader.id,
                token=token_value,
                platform="ios",
                bundle_id="com.vbwd.app",
                app="meinchat",
            )
        db.session.commit()

        captured_requests: list = []
        apns_client = ApnsClient(
            key_path=_write_signing_key(tmp_path),
            key_id="TESTKEY123",
            team_id="TESTTEAM12",
            bundle_id="com.vbwd.app",
            use_sandbox=True,
            transport=_collector_transport(captured_requests),
        )

        push_dispatcher_registry.register_push_handler(
            "meinchat", _build_hook(apns_client)
        )
        try:
            message_service = MessageService(
                ConversationRepository(db.session),
                MessageRepository(db.session),
                NicknameRepository(db.session),
            )
            # Peer sends → alert push to BOTH of the reader's devices.
            message_service.send_text(
                conversation.id, sender_user_id=peer.id, body="hi there"
            )
            db.session.commit()
            alert_requests = list(captured_requests)
            assert len(alert_requests) == 2
            assert all(
                request.headers["apns-push-type"] == "alert"
                for request in alert_requests
            )

            captured_requests.clear()

            # Reader reads on device A → silent badge push to device B only.
            message_service.mark_read(
                conversation.id,
                reader_user_id=reader.id,
                originating_device_token=token_a,
            )
            db.session.commit()
        finally:
            push_dispatcher_registry.unregister_push_handler("meinchat")

        assert len(captured_requests) == 1
        silent_request = captured_requests[0]
        assert silent_request.url.path == f"/3/device/{token_b}"
        assert silent_request.headers["apns-push-type"] == "background"
        assert silent_request.headers["apns-priority"] == "5"
        assert json.loads(silent_request.content) == {
            "aps": {"badge": 0, "content-available": 1}
        }
