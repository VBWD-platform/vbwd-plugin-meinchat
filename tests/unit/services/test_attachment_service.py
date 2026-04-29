"""Tests for AttachmentService — MIME sniff, re-encode, thumb, EXIF strip."""
import io

import pytest
from PIL import Image

from plugins.cms.src.services.file_storage import InMemoryFileStorage
from plugins.meinchat.meinchat.services.attachment_service import (
    AttachmentService,
    AttachmentTooLargeError,
    AttachmentTypeNotAllowedError,
)


def _png_bytes(width: int = 600, height: int = 400, color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_with_exif_bytes() -> bytes:
    """JPEG carrying a fabricated EXIF Orientation tag."""
    buf = io.BytesIO()
    # Pillow's `save` with the `exif` kwarg attaches the bytes we pass.
    # A minimal 10-byte "Exif" header is enough for this round-trip test.
    exif_bytes = b"Exif\x00\x00MM\x00*"  # nominal 10 bytes
    Image.new("RGB", (100, 100), (0, 255, 0)).save(buf, format="JPEG", exif=exif_bytes)
    return buf.getvalue()


@pytest.fixture
def storage():
    return InMemoryFileStorage(base_url="/uploads")


@pytest.fixture
def service(storage):
    return AttachmentService(
        storage=storage, max_bytes=5 * 1024 * 1024, max_dim_px=2048
    )


class TestHappyPath:
    def test_png_is_accepted_and_reencoded_to_webp(self, service, storage):
        result = service.process_and_store(_png_bytes(), owner_user_id="user-123")
        assert result["attachment_url"].endswith(".webp")
        assert result["attachment_thumb_url"].endswith(".webp")
        assert result["attachment_width_px"] == 600
        assert result["attachment_height_px"] == 400

    def test_thumb_is_smaller_than_original(self, service, storage):
        result = service.process_and_store(_png_bytes(800, 600), owner_user_id="u")
        original = storage.read(result["_storage_paths"]["original"])
        thumb = storage.read(result["_storage_paths"]["thumb"])
        assert len(thumb) < len(original)
        # verify thumb stays within 256 px on the long side
        thumb_img = Image.open(io.BytesIO(thumb))
        assert max(thumb_img.size) <= 256

    def test_large_image_is_downscaled_to_max_dim(self, service):
        oversize = _png_bytes(4000, 4000)
        result = service.process_and_store(oversize, owner_user_id="u")
        # Re-encoded width should clamp to 2048.
        assert result["attachment_width_px"] == 2048
        assert result["attachment_height_px"] == 2048


class TestRejections:
    def test_over_size_cap_is_rejected(self, service):
        huge = b"x" * (6 * 1024 * 1024)
        with pytest.raises(AttachmentTooLargeError):
            service.process_and_store(huge, owner_user_id="u")

    def test_non_image_bytes_are_rejected(self, service):
        with pytest.raises(AttachmentTypeNotAllowedError):
            service.process_and_store(b"not an image", owner_user_id="u")

    def test_gif_is_rejected(self, service):
        buf = io.BytesIO()
        Image.new("RGB", (100, 100), (0, 0, 255)).save(buf, format="GIF")
        with pytest.raises(AttachmentTypeNotAllowedError):
            service.process_and_store(buf.getvalue(), owner_user_id="u")


class TestExifStripping:
    def test_reencoded_image_has_no_exif(self, service, storage):
        result = service.process_and_store(
            _jpeg_with_exif_bytes(), owner_user_id="u"
        )
        stored = storage.read(result["_storage_paths"]["original"])
        img = Image.open(io.BytesIO(stored))
        # WebP re-encode drops EXIF by default; confirm it's either empty
        # or absent entirely.
        exif = img.getexif()
        assert len(exif) == 0


class TestDelete:
    def test_delete_purges_both_original_and_thumb(self, service, storage):
        result = service.process_and_store(_png_bytes(), owner_user_id="u")
        orig_path = result["_storage_paths"]["original"]
        thumb_path = result["_storage_paths"]["thumb"]
        assert storage.exists(orig_path)
        assert storage.exists(thumb_path)

        service.delete_attachment(
            original_path=orig_path, thumb_path=thumb_path
        )
        assert not storage.exists(orig_path)
        assert not storage.exists(thumb_path)
