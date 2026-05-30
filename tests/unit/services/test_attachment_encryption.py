"""S28.4 — client-encrypted attachment storage (server stores opaque bytes)."""
import pytest

from vbwd.interfaces.file_storage import InMemoryFileStorage

from plugins.meinchat.meinchat.services.attachment_service import (
    AttachmentService,
    AttachmentTooLargeError,
    AttachmentTypeNotAllowedError,
    InvalidEnvelopeError,
)


@pytest.fixture
def storage():
    return InMemoryFileStorage(base_url="/uploads")


@pytest.fixture
def service(storage):
    return AttachmentService(storage=storage, max_bytes=1024, max_dim_px=2048)


_CIPHERTEXT = b"\x00\x9a\xff" * 40  # opaque, not a valid image


def test_store_encrypted_writes_opaque_bytes_unchanged(service, storage):
    result = service.store_encrypted(
        _CIPHERTEXT, owner_user_id="u1", kind="fullres", mime="image/webp"
    )
    # Server never re-encodes ciphertext — stored bytes are byte-equal.
    assert storage.read(result["storage_path"]) == _CIPHERTEXT
    assert result["protocol"] == "e2e_v1"
    assert result["kind"] == "fullres"
    assert result["bytes_count"] == len(_CIPHERTEXT)
    assert result["mime"] == "image/webp"


def test_store_encrypted_supports_thumb_kind(service, storage):
    result = service.store_encrypted(
        b"thumbct", owner_user_id="u1", kind="thumb", mime="image/webp"
    )
    assert result["kind"] == "thumb"
    assert "thumb." in result["storage_path"]


def test_store_encrypted_rejects_plain_protocol(service):
    with pytest.raises(InvalidEnvelopeError):
        service.store_encrypted(
            b"x",
            owner_user_id="u1",
            kind="fullres",
            mime="image/webp",
            protocol="plain",
        )


def test_store_encrypted_rejects_unknown_kind(service):
    with pytest.raises(AttachmentTypeNotAllowedError):
        service.store_encrypted(
            b"x", owner_user_id="u1", kind="banner", mime="image/webp"
        )


def test_store_encrypted_enforces_size_cap(service):
    with pytest.raises(AttachmentTooLargeError):
        service.store_encrypted(
            b"x" * 2048, owner_user_id="u1", kind="fullres", mime="image/webp"
        )


def test_read_blob_returns_stored_ciphertext(service):
    result = service.store_encrypted(
        _CIPHERTEXT, owner_user_id="u1", kind="fullres", mime="image/webp"
    )
    assert service.read_blob(result["storage_path"]) == _CIPHERTEXT
