"""Attachment service — MIME sniff, downscale, EXIF-strip, thumbnail.

All images are re-encoded to WebP on save. EXIF is not copied to the
output. Size + dimension caps are enforced before and after decode.
"""
import io
import uuid
from typing import Any, Dict, Optional

from PIL import Image

from vbwd.interfaces.file_storage import IFileStorage


class AttachmentTooLargeError(ValueError):
    pass


class AttachmentTypeNotAllowedError(ValueError):
    pass


class InvalidEnvelopeError(ValueError):
    """A non-plain attachment was uploaded without an envelope header."""


_ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP"}
_THUMB_MAX_DIM = 256
ATTACHMENT_KIND_FULLRES = "fullres"
ATTACHMENT_KIND_THUMB = "thumb"


class AttachmentService:
    """Single entry point for uploading + purging message attachments."""

    def __init__(
        self,
        storage: IFileStorage,
        *,
        max_bytes: int = 5 * 1024 * 1024,
        max_dim_px: int = 2048,
        path_prefix: str = "meinchat/attachments",
    ) -> None:
        self._storage = storage
        self._max_bytes = max_bytes
        self._max_dim_px = max_dim_px
        self._prefix = path_prefix.strip("/")

    def process_and_store(self, raw: bytes, *, owner_user_id: Any) -> Dict[str, Any]:
        if len(raw) > self._max_bytes:
            raise AttachmentTooLargeError(f"attachment exceeds {self._max_bytes} bytes")

        try:
            img = Image.open(io.BytesIO(raw))
            img.load()  # force actual decode — catches truncated / malformed files
        except Exception as exc:
            raise AttachmentTypeNotAllowedError(f"could not read image: {exc}") from exc

        if img.format not in _ALLOWED_FORMATS:
            raise AttachmentTypeNotAllowedError(
                f"format '{img.format}' not allowed; use PNG, JPEG, or WEBP"
            )

        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        original = self._downscale(img.copy(), self._max_dim_px)
        thumb = self._downscale(img.copy(), _THUMB_MAX_DIM)

        original_bytes = self._encode_webp(original)
        thumb_bytes = self._encode_webp(thumb)

        token = uuid.uuid4().hex
        original_path = f"{self._prefix}/{owner_user_id}/{token}.webp"
        thumb_path = f"{self._prefix}/{owner_user_id}/{token}.thumb.webp"

        self._storage.save(original_bytes, original_path)
        self._storage.save(thumb_bytes, thumb_path)

        return {
            "attachment_url": self._storage.get_url(original_path),
            "attachment_thumb_url": self._storage.get_url(thumb_path),
            "attachment_width_px": original.width,
            "attachment_height_px": original.height,
            "_storage_paths": {
                "original": original_path,
                "thumb": thumb_path,
            },
        }

    def store_encrypted(
        self,
        ciphertext: bytes,
        *,
        owner_user_id: Any,
        kind: str,
        mime: str,
        protocol: str = "e2e_v1",
    ) -> Dict[str, Any]:
        """Store a client-encrypted attachment blob (S28.4).

        The server is a forwarder: the bytes are ALREADY ciphertext, so it
        never decodes, resizes, or strips EXIF — it validates size + shape
        and writes the opaque blob to `IFileStorage`. The per-recipient key
        envelope is carried on the `meinchat_attachment` row by the caller;
        this method only handles the blob + returns its storage coordinates.
        """
        if protocol == "plain":
            raise InvalidEnvelopeError(
                "store_encrypted is for non-plain protocols; use process_and_store"
            )
        if kind not in (ATTACHMENT_KIND_FULLRES, ATTACHMENT_KIND_THUMB):
            raise AttachmentTypeNotAllowedError(f"unknown attachment kind '{kind}'")
        if len(ciphertext) > self._max_bytes:
            raise AttachmentTooLargeError(f"attachment exceeds {self._max_bytes} bytes")
        token = uuid.uuid4().hex
        suffix = "thumb." if kind == ATTACHMENT_KIND_THUMB else ""
        path = f"{self._prefix}/{owner_user_id}/{token}.{suffix}enc"
        self._storage.save(ciphertext, path)
        return {
            "storage_url": self._storage.get_url(path),
            "storage_path": path,
            "mime": mime,
            "bytes_count": len(ciphertext),
            "protocol": protocol,
            "kind": kind,
        }

    def read_blob(self, storage_path: str) -> bytes:
        """Return the raw stored bytes — opaque ciphertext for non-plain
        attachments (the client decrypts). Thin pass-through over storage."""
        return self._storage.read(storage_path)

    def delete_attachment(
        self,
        *,
        original_path: Optional[str],
        thumb_path: Optional[str],
    ) -> None:
        """Drop both the original and the thumb; each call is idempotent."""
        if original_path:
            self._storage.delete(original_path)
        if thumb_path:
            self._storage.delete(thumb_path)

    # ── private ────────────────────────────────────────────────────────────

    @staticmethod
    def _downscale(img: Image.Image, max_dim: int) -> Image.Image:
        if max(img.size) <= max_dim:
            return img
        scale = max_dim / max(img.size)
        new_size = (int(img.width * scale), int(img.height * scale))
        return img.resize(new_size, Image.Resampling.LANCZOS)

    @staticmethod
    def _encode_webp(img: Image.Image) -> bytes:
        buf = io.BytesIO()
        # `exif` arg is not passed → no EXIF is written into the output.
        img.save(buf, format="WEBP", quality=85)
        return buf.getvalue()
