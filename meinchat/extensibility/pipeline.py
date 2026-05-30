"""Send-pipeline ports (S28.3a §2.1).

`IBodyCodec` (single-impl) transforms the body on send/read — default identity
(passthrough). meinchat-plus replaces it with a server-side envelope validator
that holds no keys and never decrypts. `IPostSendHook` (multi-impl) runs
after-the-fact side effects; a throwing hook is logged, never propagated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from plugins.meinchat.meinchat.models.message import Message


@dataclass(frozen=True)
class SendContext:
    """Everything a codec/policy needs to encode + authorize one send."""

    sender: Any
    recipients: List[Any]
    conversation: Any
    body_or_envelope: Any
    protocol_hint: str = "plain"
    expected_device_ids: Tuple[Any, ...] = ()
    request_metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EncodedBody:
    """Result of `IBodyCodec.encode` — exactly one of body/envelope is set."""

    protocol: str
    body: Optional[str] = None
    envelope: Optional[bytes] = None


@runtime_checkable
class IBodyCodec(Protocol):
    def encode(self, ctx: SendContext) -> EncodedBody:
        ...

    def decode(self, row: Message, viewer_device: Any) -> str:
        ...


@runtime_checkable
class IPostSendHook(Protocol):
    def on_sent(self, row: Message, *, fetched_by: Any = None) -> None:
        ...


class IdentityBodyCodec:
    """Default passthrough: plaintext body, no envelope, protocol='plain'."""

    def encode(self, ctx: SendContext) -> EncodedBody:
        body = ctx.body_or_envelope
        if not isinstance(body, str):
            raise TypeError("IdentityBodyCodec expects a str body")
        return EncodedBody(protocol="plain", body=body, envelope=None)

    def decode(self, row: Message, viewer_device: Any = None) -> str:
        return row.body or ""
