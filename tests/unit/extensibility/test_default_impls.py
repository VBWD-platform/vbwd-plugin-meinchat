"""S28.3a §6.2 — default-impl behaviour locked in."""
import uuid

import pytest

from plugins.meinchat.meinchat.extensibility.identity import (
    DirectoryNotEnabledError,
    NullDeviceDirectory,
)
from plugins.meinchat.meinchat.extensibility.lifecycle import (
    BlockListPolicy,
    PlainCapability,
)
from plugins.meinchat.meinchat.extensibility.pipeline import (
    EncodedBody,
    IdentityBodyCodec,
    SendContext,
)


class _Row:
    def __init__(self, body):
        self.body = body


class TestIdentityBodyCodec:
    def test_encode_produces_plain_body_no_envelope(self):
        codec = IdentityBodyCodec()
        ctx = SendContext(
            sender=object(),
            recipients=[object()],
            conversation=object(),
            body_or_envelope="hello",
        )
        encoded = codec.encode(ctx)
        assert isinstance(encoded, EncodedBody)
        assert encoded.protocol == "plain"
        assert encoded.body == "hello"
        assert encoded.envelope is None

    def test_encode_decode_round_trip_is_byte_equal(self):
        codec = IdentityBodyCodec()
        ctx = SendContext(
            sender=object(),
            recipients=[object()],
            conversation=object(),
            body_or_envelope="round trip ✓",
        )
        encoded = codec.encode(ctx)
        assert codec.decode(_Row(encoded.body)) == "round trip ✓"

    def test_encode_rejects_non_str_body(self):
        codec = IdentityBodyCodec()
        ctx = SendContext(
            sender=object(),
            recipients=[object()],
            conversation=object(),
            body_or_envelope=b"bytes",
        )
        with pytest.raises(TypeError):
            codec.encode(ctx)


class TestNullDeviceDirectory:
    def test_lookup_active_is_empty(self):
        assert NullDeviceDirectory().lookup_active(uuid.uuid4()) == []

    def test_has_any_is_false(self):
        assert NullDeviceDirectory().has_any(uuid.uuid4()) is False

    def test_register_raises(self):
        with pytest.raises(DirectoryNotEnabledError):
            NullDeviceDirectory().register(uuid.uuid4(), b"k", "x25519", None)

    def test_revoke_raises(self):
        with pytest.raises(DirectoryNotEnabledError):
            NullDeviceDirectory().revoke(uuid.uuid4())


class TestPlainCapability:
    def test_set_is_plain_for_any_conversation(self):
        assert PlainCapability().for_conversation(None) == {"plain"}

    def test_idempotent(self):
        cap = PlainCapability()
        assert cap.for_conversation(object()) == cap.for_conversation(object())


class TestBlockListPolicy:
    def test_allows_by_default(self):
        assert BlockListPolicy().may_start(object(), object(), ["plain"]) is None

    def test_idempotent(self):
        policy = BlockListPolicy()
        assert policy.may_start(object(), object(), ["plain"]) is None
        assert policy.may_start(object(), object(), ["e2e_v1"]) is None
