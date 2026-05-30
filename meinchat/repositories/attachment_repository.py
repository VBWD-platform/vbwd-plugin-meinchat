"""Data access for `meinchat_attachment` (S28.4)."""
from typing import List, Optional
from uuid import UUID

from plugins.meinchat.meinchat.models.attachment import MeinchatAttachment


class AttachmentRepository:
    def __init__(self, session) -> None:
        self._session = session

    def find_by_id(self, attachment_id) -> Optional[MeinchatAttachment]:
        return self._session.get(MeinchatAttachment, attachment_id)

    def add(
        self,
        *,
        message_id: UUID,
        kind: str,
        storage_url: str,
        protocol: str,
        envelope_header,
        mime: str,
        bytes_count: int,
        width_px=None,
        height_px=None,
    ) -> MeinchatAttachment:
        row = MeinchatAttachment(
            message_id=message_id,
            kind=kind,
            storage_url=storage_url,
            protocol=protocol,
            envelope_header=envelope_header,
            mime=mime,
            bytes_count=bytes_count,
            width_px=width_px,
            height_px=height_px,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def list_by_message(self, message_id: UUID) -> List[MeinchatAttachment]:
        return (
            self._session.query(MeinchatAttachment)
            .filter(MeinchatAttachment.message_id == message_id)
            .order_by(MeinchatAttachment.kind)
            .all()
        )

    def storage_urls_for_message(self, message_id: UUID) -> List[str]:
        return [a.storage_url for a in self.list_by_message(message_id)]
