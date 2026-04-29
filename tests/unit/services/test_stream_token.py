"""Tests for StreamTokenService — short-lived SSE auth JWT."""
import time
from uuid import uuid4

import pytest

from plugins.meinchat.meinchat.services.stream_token import (
    StreamTokenExpiredError,
    StreamTokenInvalidError,
    StreamTokenService,
)


@pytest.fixture
def service():
    return StreamTokenService(secret_key="test-secret", ttl_seconds=60)


class TestMintAndVerify:
    def test_round_trip_returns_user_id(self, service):
        user_id = uuid4()
        token = service.mint(user_id)
        assert service.verify(token) == str(user_id)

    def test_rejects_wrong_audience(self, service):
        # Forge a token signed with the same key but a different aud.
        import jwt as pyjwt

        token = pyjwt.encode(
            {"user_id": "xxx", "aud": "something-else", "exp": time.time() + 60},
            "test-secret",
            algorithm="HS256",
        )
        with pytest.raises(StreamTokenInvalidError):
            service.verify(token)

    def test_rejects_wrong_signature(self, service):
        other = StreamTokenService(secret_key="other-secret", ttl_seconds=60)
        foreign = other.mint(uuid4())
        with pytest.raises(StreamTokenInvalidError):
            service.verify(foreign)

    def test_rejects_expired_token(self):
        short = StreamTokenService(secret_key="test-secret", ttl_seconds=1)
        token = short.mint(uuid4())
        time.sleep(1.2)
        with pytest.raises(StreamTokenExpiredError):
            short.verify(token)

    def test_rejects_garbage(self, service):
        with pytest.raises(StreamTokenInvalidError):
            service.verify("not-a-jwt")
