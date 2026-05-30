"""S28.4 — MessageService e2e-attachment add/read (unit, MagicMock repos)."""
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from vbwd.interfaces.file_storage import InMemoryFileStorage

from plugins.meinchat.meinchat.services.attachment_service import AttachmentService
from plugins.meinchat.meinchat.services.message_service import (
    AttachmentNotFoundError,
    MessageNotFoundError,
    MessageService,
    PlainAttachmentError,
)

_CIPHERTEXT = b"\x07\x42\xaa" * 30


def _service(storage):
    return MessageService(
        conv_repo=MagicMock(),
        message_repo=MagicMock(),
        nickname_repo=MagicMock(),
        attachment_service=AttachmentService(storage=storage, max_bytes=4096),
        attachment_repo=MagicMock(),
    )


def _e2e_message(sender_id):
    return SimpleNamespace(
        id=uuid4(), sender_id=sender_id, protocol="e2e_v1", conversation_id=uuid4()
    )


def test_add_e2e_attachment_stores_opaque_and_records_row():
    storage = InMemoryFileStorage(base_url="/uploads")
    service = _service(storage)
    sender = uuid4()
    msg = _e2e_message(sender)
    service._message_repo.find_by_id.return_value = msg

    service.add_e2e_attachment(
        msg.id,
        caller_user_id=sender,
        kind="fullres",
        ciphertext=_CIPHERTEXT,
        envelope_header={"per_recipient_key_envelopes": {"d": "k"}},
        mime="image/webp",
    )

    # Server stored the opaque ciphertext byte-for-byte.
    assert list(storage._store.values()) == [_CIPHERTEXT]
    args = service._attachment_repo.add.call_args.kwargs
    assert args["message_id"] == msg.id
    assert args["kind"] == "fullres"
    assert args["protocol"] == "e2e_v1"
    assert args["bytes_count"] == len(_CIPHERTEXT)
    assert args["envelope_header"] == {"per_recipient_key_envelopes": {"d": "k"}}


def test_add_e2e_attachment_unknown_or_foreign_message_raises():
    service = _service(InMemoryFileStorage())
    service._message_repo.find_by_id.return_value = None
    with pytest.raises(MessageNotFoundError):
        service.add_e2e_attachment(
            uuid4(),
            caller_user_id=uuid4(),
            kind="fullres",
            ciphertext=_CIPHERTEXT,
            envelope_header={"x": 1},
            mime="image/webp",
        )


def test_add_e2e_attachment_rejects_non_sender():
    service = _service(InMemoryFileStorage())
    service._message_repo.find_by_id.return_value = _e2e_message(uuid4())
    with pytest.raises(MessageNotFoundError):  # opaque — no authorship probing
        service.add_e2e_attachment(
            uuid4(),
            caller_user_id=uuid4(),
            kind="fullres",
            ciphertext=_CIPHERTEXT,
            envelope_header={"x": 1},
            mime="image/webp",
        )


def test_add_e2e_attachment_rejects_plain_message():
    service = _service(InMemoryFileStorage())
    sender = uuid4()
    service._message_repo.find_by_id.return_value = SimpleNamespace(
        id=uuid4(), sender_id=sender, protocol="plain", conversation_id=uuid4()
    )
    with pytest.raises(PlainAttachmentError):
        service.add_e2e_attachment(
            uuid4(),
            caller_user_id=sender,
            kind="fullres",
            ciphertext=_CIPHERTEXT,
            envelope_header={"x": 1},
            mime="image/webp",
        )


def test_get_attachment_blob_returns_bytes_for_member():
    storage = InMemoryFileStorage(base_url="/uploads")
    service = _service(storage)
    # Seed a stored blob + a row pointing at its URL.
    coords = service._attachments.store_encrypted(
        _CIPHERTEXT, owner_user_id="u1", kind="fullres", mime="image/webp"
    )
    row = SimpleNamespace(
        message_id=uuid4(), storage_url=coords["storage_url"], mime="image/webp"
    )
    service._attachment_repo.find_by_id.return_value = row
    service._message_repo.find_by_id.return_value = SimpleNamespace(
        conversation_id=uuid4()
    )
    service._conv_repo.find_by_id.return_value = object()
    # Patch membership to True for this caller.
    from plugins.meinchat.meinchat.services import message_service as ms

    original = ms.ConversationService.is_member
    ms.ConversationService.is_member = staticmethod(lambda uid, conv: True)
    try:
        blob, mime = service.get_attachment_blob(uuid4(), caller_user_id=uuid4())
    finally:
        ms.ConversationService.is_member = original
    assert blob == _CIPHERTEXT
    assert mime == "image/webp"


def test_get_attachment_blob_missing_raises():
    service = _service(InMemoryFileStorage())
    service._attachment_repo.find_by_id.return_value = None
    with pytest.raises(AttachmentNotFoundError):
        service.get_attachment_blob(uuid4(), caller_user_id=uuid4())


def test_get_attachment_blob_non_member_raises():
    service = _service(InMemoryFileStorage())
    service._attachment_repo.find_by_id.return_value = SimpleNamespace(
        message_id=uuid4(), storage_url="/uploads/meinchat/x.enc", mime="image/webp"
    )
    service._message_repo.find_by_id.return_value = SimpleNamespace(
        conversation_id=uuid4()
    )
    service._conv_repo.find_by_id.return_value = object()
    from plugins.meinchat.meinchat.services import message_service as ms

    original = ms.ConversationService.is_member
    ms.ConversationService.is_member = staticmethod(lambda uid, conv: False)
    try:
        with pytest.raises(AttachmentNotFoundError):
            service.get_attachment_blob(uuid4(), caller_user_id=uuid4())
    finally:
        ms.ConversationService.is_member = original
